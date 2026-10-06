"""
core/ProductionMetrics.py
-------------------------
Cycle and run statistics for the PROD loop: cycle count and durations, run
start/stop timestamps, last error, and a capped history of archived runs.

Framework-owned and always present. It holds no user data of its own: an
archived run carries a snapshot of the context variables handed in by the
caller, so it never needs to know any variable by name.
"""

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Dict


class ProductionMetrics:
    """Thread-safe PROD cycle/run bookkeeping. In memory only."""

    _MAX_CYCLE_HISTORY = 100
    _MAX_RUN_HISTORY = 20

    def __init__(self, logger):
        self.logger = logger
        self._lock = threading.Lock()
        self._metadata: Dict[str, Any] = {
            "running": False,
            "cycle_count": 0,
            "last_cycle_start": None,
            "last_cycle_end": None,
            "last_cycle_duration_s": None,
            "total_cycle_time_s": 0.0,
            "cycle_history": [],
            "run_history": [],
            "last_prod_start": None,
            "last_prod_stop": None,
            "last_archived_run_start": None,
            "last_abort": None,
            "last_fault": None,
            "last_initialize": None,
            "last_error": None,
        }

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _parse_utc(self, value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _seconds_between(self, start: Any, end: Any) -> float | None:
        start_dt = self._parse_utc(start)
        end_dt = self._parse_utc(end)
        if start_dt is None or end_dt is None:
            return None
        return max(0.0, (end_dt - start_dt).total_seconds())

    def _build_current_run_summary_locked(self, end_event: str, end_time: str,
                                          variables: Dict[str, Any]) -> Dict[str, Any] | None:
        run_start = self._metadata.get("last_prod_start")
        if not run_start:
            return None

        elapsed_s = self._seconds_between(run_start, end_time)
        cycle_count = int(self._metadata.get("cycle_count", 0))
        total_cycle_time_s = float(self._metadata.get("total_cycle_time_s", 0.0))

        return {
            "run_start": run_start,
            "run_end": end_time,
            "end_event": end_event,
            "running": bool(self._metadata.get("running")),
            "cycle_count": cycle_count,
            "elapsed_production_s": elapsed_s,
            "last_cycle_duration_s": self._metadata.get("last_cycle_duration_s"),
            "average_cycle_duration_s": (total_cycle_time_s / cycle_count) if cycle_count > 0 else None,
            "total_cycle_time_s": total_cycle_time_s,
            "last_cycle_start": self._metadata.get("last_cycle_start"),
            "last_cycle_end": self._metadata.get("last_cycle_end"),
            "last_error": self._metadata.get("last_error"),
            "variables": copy.deepcopy(variables),
        }

    def _archive_run_locked(self, end_event: str, end_time: str, variables: Dict[str, Any]) -> None:
        summary = self._build_current_run_summary_locked(end_event, end_time, variables)
        if summary is None:
            return

        run_start = summary.get("run_start")
        if run_start and self._metadata.get("last_archived_run_start") == run_start:
            return

        run_history = self._metadata.setdefault("run_history", [])
        run_history.append(summary)
        if run_start:
            self._metadata["last_archived_run_start"] = run_start
        if len(run_history) > self._MAX_RUN_HISTORY:
            del run_history[:-self._MAX_RUN_HISTORY]

    def record_event(self, event: str, variables: Dict[str, Any] | None = None) -> None:
        """Timestamp a lifecycle event and archive the run it ends, if any.

        ``variables`` is the context's variable values *before* the event's
        resets are applied, so an archived run records what it actually
        produced rather than freshly reset counters.
        """
        variables = variables or {}
        with self._lock:
            now = self._utc_now()
            if event == "initialize":
                self._metadata["last_initialize"] = now
            elif event == "prod_start":
                if self._metadata.get("last_prod_start") and (
                    self._metadata.get("cycle_count", 0) > 0
                    or self._metadata.get("last_cycle_start")
                    or self._metadata.get("last_cycle_end")
                ) and self._metadata.get("last_archived_run_start") != self._metadata.get("last_prod_start"):
                    self._archive_run_locked("superseded", now, variables)
                self._metadata["last_prod_start"] = now
                self._metadata["last_prod_stop"] = None
                self._metadata["cycle_count"] = 0
                self._metadata["last_cycle_start"] = None
                self._metadata["last_cycle_end"] = None
                self._metadata["last_cycle_duration_s"] = None
                self._metadata["total_cycle_time_s"] = 0.0
                self._metadata["cycle_history"] = []
                # Previously never cleared, so a single bad cycle left the
                # "Last Error State" card populated for the process lifetime.
                self._metadata["last_error"] = None
            elif event == "prod_stop":
                self._metadata["last_prod_stop"] = now
                self._archive_run_locked(event, now, variables)
            elif event == "abort":
                self._metadata["last_abort"] = now
                self._archive_run_locked(event, now, variables)
            elif event == "fault":
                self._metadata["last_fault"] = now
                self._archive_run_locked(event, now, variables)

    def mark_prod_running(self, running: bool) -> None:
        with self._lock:
            self._metadata["running"] = bool(running)

    def mark_cycle_start(self) -> None:
        with self._lock:
            self._metadata["running"] = True
            self._metadata["last_cycle_start"] = self._utc_now()

    def mark_cycle_end(self) -> None:
        with self._lock:
            cycle_start = self._metadata.get("last_cycle_start")
            cycle_end = self._utc_now()
            duration_s = self._seconds_between(cycle_start, cycle_end)
            self._metadata["cycle_count"] += 1
            self._metadata["last_cycle_end"] = cycle_end
            self._metadata["last_cycle_duration_s"] = duration_s
            if duration_s is not None:
                self._metadata["total_cycle_time_s"] = float(self._metadata.get("total_cycle_time_s", 0.0)) + duration_s
            history = self._metadata.setdefault("cycle_history", [])
            history.append(
                {
                    "cycle_number": self._metadata["cycle_count"],
                    "started_at": cycle_start,
                    "ended_at": cycle_end,
                    "duration_s": duration_s,
                }
            )
            if len(history) > self._MAX_CYCLE_HISTORY:
                del history[:-self._MAX_CYCLE_HISTORY]

    def mark_cycle_error(self, error_text: str) -> None:
        with self._lock:
            self._metadata["last_error"] = str(error_text)

    def get_cycle_count(self) -> int:
        """Authoritative completed-cycle count for the current run."""
        with self._lock:
            return int(self._metadata.get("cycle_count", 0))

    def snapshot(self) -> Dict[str, Any]:
        """Derived metrics for ``/api/prod/metrics`` and ``/api/prod/runs``."""
        with self._lock:
            now = self._utc_now()
            prod_start = self._metadata.get("last_prod_start")
            prod_stop = self._metadata.get("last_prod_stop")
            cycle_start = self._metadata.get("last_cycle_start")
            cycle_end = self._metadata.get("last_cycle_end")

            elapsed_s = None
            if prod_start:
                elapsed_s = self._seconds_between(prod_start, now if self._metadata.get("running") else (prod_stop or now))

            current_cycle_elapsed_s = None
            if self._metadata.get("running") and cycle_start:
                current_cycle_elapsed_s = self._seconds_between(cycle_start, now)

            completed_cycles = int(self._metadata.get("cycle_count", 0))
            total_cycle_time_s = float(self._metadata.get("total_cycle_time_s", 0.0))
            average_cycle_duration_s = None
            if completed_cycles > 0:
                average_cycle_duration_s = total_cycle_time_s / completed_cycles

            return {
                "running": bool(self._metadata.get("running")),
                "completed_cycles": completed_cycles,
                "elapsed_production_s": elapsed_s,
                "current_cycle_elapsed_s": current_cycle_elapsed_s,
                "last_cycle_duration_s": self._metadata.get("last_cycle_duration_s"),
                "average_cycle_duration_s": average_cycle_duration_s,
                "total_cycle_time_s": total_cycle_time_s,
                "last_cycle_start": cycle_start,
                "last_cycle_end": cycle_end,
                "last_prod_start": prod_start,
                "last_prod_stop": prod_stop,
                "last_abort": self._metadata.get("last_abort"),
                "last_fault": self._metadata.get("last_fault"),
                "last_initialize": self._metadata.get("last_initialize"),
                "last_error": self._metadata.get("last_error"),
                "cycle_history": copy.deepcopy(self._metadata.get("cycle_history", [])),
                "run_history": copy.deepcopy(self._metadata.get("run_history", [])),
            }
