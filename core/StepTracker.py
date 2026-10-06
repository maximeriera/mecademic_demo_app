"""
core/StepTracker.py
-------------------
Live sequence-step reporting for every task (PROD, HOME, SHIPMENT, CALIB and
manual actions).

Workspace logic reports what the cell is doing:

    with context.step("Pick part from feeder"):
        robot.MoveJoints(...)
        robot.WaitIdle()

Steps nest (prod_cycle already calls into manual-action subroutines), so open
frames form a stack. The innermost frame is "what the cell is doing right now";
stats are keyed by the full path so a parent and child never collide.

Step tracking is framework behaviour, not user configuration: it works whether
or not the workspace defines a context file.
"""

import contextlib
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple


class StepTracker:
    """Thread-safe step stack, per-step timing stats and declared sequences."""

    _MAX_STEP_DEPTH = 16        # refuse deeper nesting rather than grow without bound
    _MAX_STEP_HISTORY = 200     # completed step frames retained
    _MAX_STEP_STATS = 200       # distinct step paths aggregated
    _MAX_STEP_NAME_LEN = 120

    def __init__(self, logger, sequences: Dict[str, List[str]] | None = None):
        self.logger = logger

        # Never held during I/O, so live step reads at 10 Hz (/api/status) stay
        # fast, and the monitor thread can close a step the task thread opened.
        self._lock = threading.Lock()
        self._stack: List[Dict[str, Any]] = []        # open frames, innermost last
        self._stats: Dict[str, Dict[str, Any]] = {}   # path -> aggregate
        self._observed: List[str] = []                # depth-0 names, first-seen order
        self._history: List[Dict[str, Any]] = []      # completed frames, capped
        self._sequences: Dict[str, List[str]] = dict(sequences or {})  # declared, from config.yaml
        self._sequences_runtime: Dict[str, List[str]] = {}  # declare_sequence() overrides
        self._task: Dict[str, Any] | None = None      # active task + sequence cursor
        self._cycle: int | None = None  # PROD cycle number in progress, None outside one
        self._epoch = 0             # bumped by abandon_open_steps() to stale outstanding tokens
        self._frame_seq = 0         # monotonic frame ids
        self._run_seq = 0           # monotonic task-run ids, see log_cycle_summary
        self._rev = 0               # bumped on every transition, for cheap change detection
        self._stats_capped_warned = False

        # IMMUTABLE live snapshot, or None when idle.
        #
        # INVARIANT: never mutate the dict this points at. Always build a new
        # dict and rebind. Readers (get_live) take no lock and rely on the
        # rebind being atomic; a future `self._live["x"] = y` would silently
        # break lock-free reads for every Flask thread.
        self._live: Dict[str, Any] | None = None

    @classmethod
    def normalize_sequences(cls, raw_sequences: Any, logger) -> Dict[str, List[str]]:
        """Validate a ``sequences:`` block.

        Shape: ``{task_key: [step name, ...]}`` where task_key is a task
        function name (``prod_cycle``, ``home``, ``shipment``, ``calib``) or
        ``manual:<action_key>``. Malformed entries are warned about and
        skipped rather than raising — a bad sequence must not stop production.
        """
        if raw_sequences is None:
            return {}
        if not isinstance(raw_sequences, dict):
            logger.warning("'sequences' must be a mapping of task name to step list. Ignoring.")
            return {}

        result: Dict[str, List[str]] = {}
        for key, steps in raw_sequences.items():
            if not isinstance(steps, list):
                logger.warning(f"sequences.{key} must be a list of step names. Ignoring.")
                continue
            names = [str(s).strip()[: cls._MAX_STEP_NAME_LEN] for s in steps if str(s).strip()]
            if names:
                result[str(key).strip()] = names
        return result

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _rebuild_live_locked(self) -> None:
        """Rebuild the immutable live-step snapshot. Caller holds _lock."""
        self._rev += 1
        if not self._stack:
            self._live = None
            return

        frame = self._stack[-1]
        task = self._task or {}
        declared = task.get("declared") or []
        seq_total = len(declared) or None

        # Sequence position belongs to the TASK, not to the innermost frame.
        # Only depth-0 frames carry a seq_index, so reading it off a nested
        # frame yielded None while seq_total stayed 3 — the UI rendered
        # "Step null of 3". The cursor is advanced by depth-0 steps and is the
        # right answer for every frame beneath them.
        seq_index = task.get("cursor") if (declared and not task.get("off_sequence")) else None

        self._live = {
            "name": frame["name"],
            "path": list(frame["path"]),
            "depth": frame["depth"],
            "task": task.get("label"),
            "started_at": frame["started_at"],
            "index": (seq_index + 1) if seq_index is not None else None,
            "total": seq_total,
            "progress": ((seq_index + 1) / seq_total) if (seq_index is not None and seq_total) else None,
            "off_sequence": bool(task.get("off_sequence")),
            "cycle": self._cycle,
            "rev": self._rev,
            "_started_monotonic": frame["_started_monotonic"],
            # The top-level step this frame runs under, for compact displays
            # (the sidebar) that show "Pick part" rather than the sub-step.
            "_root_started_monotonic": self._stack[0]["_started_monotonic"],
        }

    def get_live(self) -> Dict[str, Any] | None:
        """Current step, or ``None`` when idle.

        Lock-free and allocation-light: safe to call at 10 Hz from Flask
        threads while a task thread is mid-cycle. Relies on ``_live`` being
        rebound rather than mutated (see the invariant in __init__).
        """
        snapshot = self._live
        if snapshot is None:
            return None
        live = dict(snapshot)
        started = live.pop("_started_monotonic", None)
        root_started = live.pop("_root_started_monotonic", None)
        # monotonic, not wall-clock: no ISO re-parsing per request, and immune
        # to clock adjustments during a long step.
        now = time.monotonic()
        live["elapsed_s"] = max(0.0, now - started) if started is not None else None
        live["root_elapsed_s"] = max(0.0, now - root_started) if root_started is not None else None
        return live

    def _advance_sequence_cursor_locked(self, frame: Dict[str, Any]) -> None:
        """Match a depth-0 step against the declared sequence, if any."""
        task = self._task
        if not task or frame["depth"] != 0:
            return
        declared = task.get("declared") or []
        if not declared:
            return

        name = frame["name"]
        try:
            index = declared.index(name, task.get("cursor", 0))
        except ValueError:
            try:
                index = declared.index(name)  # allow looping back to an earlier step
            except ValueError:
                # Unknown step: flag it rather than inventing a position.
                task["off_sequence"] = True
                frame["seq_index"] = None
                return
        task["cursor"] = index
        task["off_sequence"] = False
        frame["seq_index"] = index

    def _close_frame_locked(self, frame: Dict[str, Any], status: str,
                            error: BaseException | None = None) -> None:
        """Record a finished frame into history and stats. Caller holds _lock."""
        duration_s = max(0.0, time.monotonic() - frame["_started_monotonic"])

        record = {
            "name": frame["name"],
            "path": frame["key"],
            "depth": frame["depth"],
            "started_at": frame["started_at"],
            "duration_s": duration_s,
            "status": status,
            "cycle": frame.get("cycle"),
            "run_id": frame.get("run_id"),
        }
        if error is not None:
            record["error"] = str(error)
        self._history.append(record)
        if len(self._history) > self._MAX_STEP_HISTORY:
            del self._history[: -self._MAX_STEP_HISTORY]

        key = frame["key"]
        stat = self._stats.get(key)
        if stat is None:
            if len(self._stats) >= self._MAX_STEP_STATS:
                # Almost always means step names embed data (e.g. a serial
                # number). Degrade to "stop aggregating new names" rather than
                # growing without bound.
                if not self._stats_capped_warned:
                    self._stats_capped_warned = True
                    self.logger.warning(
                        "Step stats cap (%d distinct steps) reached; new step names are no longer "
                        "aggregated. Step names should be stable identifiers, not per-part data.",
                        self._MAX_STEP_STATS,
                    )
                return
            stat = {
                "path": key, "name": frame["name"], "depth": frame["depth"],
                "count": 0, "total_s": 0.0, "last_s": None,
                "min_s": None, "max_s": None, "avg_s": None,
                "fail_count": 0, "abandon_count": 0,
            }
            self._stats[key] = stat

        if status == "ok":
            # Only successful runs feed the timing aggregates. An abort 48s into
            # a 1.9s step would otherwise wreck the averages the operator reads.
            stat["count"] += 1
            stat["total_s"] += duration_s
            stat["last_s"] = duration_s
            stat["min_s"] = duration_s if stat["min_s"] is None else min(stat["min_s"], duration_s)
            stat["max_s"] = duration_s if stat["max_s"] is None else max(stat["max_s"], duration_s)
            stat["avg_s"] = stat["total_s"] / stat["count"]
        elif status == "failed":
            stat["fail_count"] += 1
        else:
            stat["abandon_count"] += 1

        if status == "ok":
            self.logger.debug("Step end: %s in %.3f s", key, duration_s)
        else:
            self.logger.warning("Step %s: %s after %.3f s%s", status, key, duration_s,
                                f" ({error})" if error else "")

    def begin_step(self, name: str, managed: bool = True) -> Tuple[int, int] | None:
        """Open a step. Returns an opaque token for :meth:`end_step`, or None if rejected."""
        name = str(name).strip()[: self._MAX_STEP_NAME_LEN] or "(unnamed step)"
        with self._lock:
            # An unmanaged (set_step) frame is replaced by the next set_step at
            # the same level, so the operator sees one current step, not a pile.
            if not managed and self._stack and not self._stack[-1].get("managed"):
                self._close_frame_locked(self._stack.pop(), "ok")

            if len(self._stack) >= self._MAX_STEP_DEPTH:
                self.logger.warning(
                    "Step nesting limit (%d) reached; '%s' is not tracked.",
                    self._MAX_STEP_DEPTH, name,
                )
                return None

            self._frame_seq += 1
            parent_path = self._stack[-1]["path"] if self._stack else []
            path = list(parent_path) + [name]
            frame = {
                "frame_id": self._frame_seq,
                "epoch": self._epoch,
                "depth": len(self._stack),
                "name": name,
                "path": path,
                "key": " / ".join(path),
                "managed": managed,
                "started_at": self._utc_now(),
                "_started_monotonic": time.monotonic(),
                "cycle": self._cycle,
                "run_id": (self._task or {}).get("run_id"),
                "seq_index": None,
            }
            self._stack.append(frame)

            if frame["depth"] == 0 and name not in self._observed:
                self._observed.append(name)
            self._advance_sequence_cursor_locked(frame)
            self._rebuild_live_locked()
            token = (frame["frame_id"], frame["epoch"])

        self.logger.debug("Step begin: %s", frame["key"])
        return token

    def end_step(self, token: Tuple[int, int] | None, failed: bool = False,
                 error: BaseException | None = None) -> None:
        """Close a step opened by :meth:`begin_step`. Safe to call twice or out of order."""
        if token is None:
            return
        frame_id, epoch = token
        with self._lock:
            if epoch != self._epoch:
                # abandon_open_steps() already closed this frame (abort/fault).
                # Must be a no-op: raising here would throw a second exception
                # out of a `with` block that is already unwinding.
                self.logger.debug("Ignoring stale step close (frame=%s epoch=%s).", frame_id, epoch)
                return

            index = next((i for i, f in enumerate(self._stack) if f["frame_id"] == frame_id), None)
            if index is None:
                return

            # Self-heal mis-nesting: close anything still open above this frame.
            while len(self._stack) > index + 1:
                self._close_frame_locked(self._stack.pop(), "abandoned")
            self._close_frame_locked(self._stack.pop(),
                                     "failed" if failed else "ok", error=error)
            self._rebuild_live_locked()

    @contextlib.contextmanager
    def step(self, name: str):
        """Report the step the cell is executing, timing it automatically.

        ``BaseException`` rather than ``Exception``: an abort unblocks a device
        SDK call and the resulting exception must still close the frame, and
        KeyboardInterrupt during a run should not leave a step open forever.
        """
        token = self.begin_step(name, managed=True)
        try:
            yield
        except BaseException as exc:
            self.end_step(token, failed=True, error=exc)
            raise
        else:
            self.end_step(token)

    def set_step(self, name: str) -> None:
        """Declare the current step without a ``with`` block.

        Closed by the next :meth:`set_step`, by :meth:`clear_step`, or when the
        enclosing task ends. Prefer :meth:`step` — it also times failures.
        """
        self.begin_step(name, managed=False)

    def clear_step(self) -> None:
        """Close the innermost unmanaged step, if any."""
        with self._lock:
            if self._stack and not self._stack[-1].get("managed"):
                self._close_frame_locked(self._stack.pop(), "ok")
                self._rebuild_live_locked()

    def abandon_open_steps(self, reason: str = "abandoned") -> None:
        """Close every open step, from any thread.

        Called on abort/fault paths so the monitor thread can clear a step the
        task thread opened — otherwise a faulted cell keeps showing
        "Pick part — 48 s and counting" while the task thread is stuck in a
        non-interruptible SDK call. Bumping the epoch makes every outstanding
        token stale so the task thread's later end_step() is a quiet no-op.
        """
        with self._lock:
            if not self._stack:
                return
            open_keys = [f["key"] for f in self._stack]
            while self._stack:
                self._close_frame_locked(self._stack.pop(), reason)
            self._epoch += 1
            self._rebuild_live_locked()
        self.logger.warning("Abandoned %d open step(s) (%s): %s",
                            len(open_keys), reason, ", ".join(reversed(open_keys)))

    def declare_sequence(self, steps: List[str], task_key: str | None = None) -> None:
        """Declare the expected step order for the active task at runtime.

        Overrides any ``sequences:`` entry in config.yaml. Optional — without it
        steps are still recorded and timed, just without "step 3 of 7".
        """
        names = [str(s).strip()[: self._MAX_STEP_NAME_LEN] for s in (steps or []) if str(s).strip()]
        with self._lock:
            key = task_key or (self._task or {}).get("sequence_key")
            if key:
                self._sequences_runtime[key] = names
            if self._task is not None:
                self._task["declared"] = names
                self._task["cursor"] = 0
                self._task["off_sequence"] = False
            self._rebuild_live_locked()

    def begin_task(self, label: str, sequence_key: str | None = None) -> None:
        """Start step tracking for a task run, clearing the previous run's stats."""
        with self._lock:
            self._stack.clear()
            self._stats.clear()
            self._observed.clear()
            self._stats_capped_warned = False
            self._cycle = None
            declared = (self._sequences_runtime.get(sequence_key)
                        or self._sequences.get(sequence_key)
                        or [])
            self._run_seq += 1
            self._task = {
                "run_id": self._run_seq,
                "label": label,
                "sequence_key": sequence_key,
                "declared": list(declared),
                "cursor": 0,
                "off_sequence": False,
                "source": "runtime" if sequence_key in self._sequences_runtime
                          else ("config" if declared else None),
            }
            self._epoch += 1
            self._rebuild_live_locked()

    def end_task(self) -> None:
        """Stop step tracking, keeping stats so they stay readable after STOP."""
        with self._lock:
            while self._stack:
                self._close_frame_locked(self._stack.pop(), "abandoned")
            self._epoch += 1
            self._task = None
            self._cycle = None
            self._rebuild_live_locked()

    def begin_cycle(self, cycle_number: int | None) -> None:
        """Mark the start of a PROD cycle: tag new frames with it and refill the progress bar."""
        with self._lock:
            self._cycle = cycle_number
            if self._task is not None:
                self._task["cursor"] = 0
                self._task["off_sequence"] = False
            self._rebuild_live_locked()

    def snapshot(self) -> Dict[str, Any]:
        """Full step state for ``/api/prod/metrics``."""
        with self._lock:
            task = dict(self._task) if self._task else {}
            declared = list(task.get("declared") or [])
            cursor = int(task.get("cursor", 0))
            stats = [dict(s) for s in self._stats.values()]
            history = [dict(h) for h in self._history[-50:]]
            observed = list(self._observed)
            capped = len(self._stats) >= self._MAX_STEP_STATS

        # Precompute each depth-0 step's share of cycle time so the UI needs no math.
        depth0_total = sum(s["total_s"] for s in stats if s["depth"] == 0) or 0.0
        for stat in stats:
            stat["share"] = (stat["total_s"] / depth0_total) if (depth0_total and stat["depth"] == 0) else None
        stats.sort(key=lambda s: (s["depth"], -(s["total_s"] or 0.0)))

        sequence = None
        if declared:
            sequence = {
                "key": task.get("sequence_key"),
                "source": task.get("source"),
                "declared": declared,
                "total": len(declared),
                "current_index": cursor,
                "done": declared[:cursor],
                "upcoming": declared[cursor + 1:],
                "off_sequence": bool(task.get("off_sequence")),
            }

        return {
            "live": self.get_live(),
            "task": task.get("label"),
            "sequence": sequence,
            "observed": observed,
            "stats": stats,
            "history": history,
            "stats_truncated": capped,
        }

    def log_cycle_summary(self, cycle_number: int) -> None:
        """One INFO line per cycle breaking down where the time went.

        Filtered by run as well as cycle: _history deliberately survives across
        runs, and every new PROD run restarts cycle numbering at 1, so cycle
        numbers repeat. Without the run_id guard this reported several runs'
        steps under one cycle heading.
        """
        with self._lock:
            run_id = (self._task or {}).get("run_id")
            parts = [h for h in self._history
                     if h.get("cycle") == cycle_number
                     and h.get("run_id") == run_id
                     and h["depth"] == 0 and h["status"] == "ok"]
        if not parts:
            return
        total = sum(p["duration_s"] for p in parts)
        detail = " | ".join(f"{p['name']} {p['duration_s']:.2f}s" for p in parts)
        self.logger.info("Cycle #%d steps: %s (total %.2fs)", cycle_number, detail, total)
