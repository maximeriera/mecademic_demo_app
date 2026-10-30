"""
core/History.py
---------------
Production history on disk: one JSON line per archived run and per finished
PROD cycle, so a show's runs survive restarts and can be exported afterwards.

Layout, under ``<log root>/history/<workspace>/`` (next to ``logs/``, which
both compose files already mount)::

    runs.jsonl                  one archived run per line
    cycles-YYYY-MM-DD.jsonl     one cycle timeline per line, by local date

Writes go through a single background thread fed by a queue, so the task
thread never waits on the disk. A write error is logged once and latched
(like the context state file), and logged again when writes recover.
Local only: nothing is ever sent anywhere.
"""

from __future__ import annotations

import csv
import io
import json
import os
import queue
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List

RUNS_FILE = "runs.jsonl"
_CYCLES_RE = re.compile(r"^cycles-(\d{4}-\d{2}-\d{2})\.jsonl$")
_STOP = object()


def _local_date(iso_timestamp: Any) -> str:
    """Local calendar date of an ISO timestamp (today if it cannot be parsed)."""
    try:
        return datetime.fromisoformat(str(iso_timestamp)).astimezone().date().isoformat()
    except (TypeError, ValueError):
        return date.today().isoformat()


class HistoryStore:
    """Append-only JSON Lines history for one workspace."""

    def __init__(self, root: str, logger, retention_days: int = 30):
        self.root = root
        self.logger = logger
        self.retention_days = max(1, int(retention_days))
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._write_failed = False
        self._file_lock = threading.Lock()   # writer appends vs. readers parsing
        self._checked_files: set[str] = set()
        os.makedirs(root, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="HistoryWriter", daemon=True)
        self._thread.start()
        self._prune()

    # --- Writing -------------------------------------------------------------

    def record_run(self, run: Dict[str, Any]) -> None:
        self._queue.put((RUNS_FILE, run))

    def record_cycle(self, cycle: Dict[str, Any]) -> None:
        self._queue.put((f"cycles-{_local_date(cycle.get('started_at'))}.jsonl", cycle))

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued record is on disk. False on timeout."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() > deadline or not self._thread.is_alive():
                return False
            time.sleep(0.01)
        return True

    def close(self, timeout: float = 5.0) -> None:
        """Flush, then stop the writer thread."""
        self._queue.put(_STOP)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            self.logger.warning("History writer did not stop within %.1fs.", timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                filename, record = item
                self._append(filename, record)
            finally:
                self._queue.task_done()

    def _ends_mid_line(self, path: str) -> bool:
        """True if *path* ends without a newline: a record cut short by a crash."""
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                if f.tell() == 0:
                    return False
                f.seek(-1, os.SEEK_END)
                return f.read(1) != b"\n"
        except FileNotFoundError:
            return False

    def _append(self, filename: str, record: Dict[str, Any]) -> None:
        line = json.dumps(record, default=str, ensure_ascii=False)
        path = os.path.join(self.root, filename)
        try:
            with self._file_lock:
                # Only a previous process can have left a partial last line, so
                # check each file once: without it, the first record written
                # after a crash would be glued to the broken one and lost too.
                prefix = ""
                if filename not in self._checked_files:
                    prefix = "\n" if self._ends_mid_line(path) else ""
                    self._checked_files.add(filename)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(prefix + line + "\n")
                    f.flush()
        except OSError as e:
            if not self._write_failed:
                self._write_failed = True
                self.logger.error("Production history write failed (%s): %s. Further failures are not logged.",
                                  filename, e)
            return
        if self._write_failed:
            self._write_failed = False
            self.logger.info("Production history writes recovered.")

    def _prune(self) -> None:
        """Delete cycle files older than the retention period. runs.jsonl stays (a few lines a day)."""
        cutoff = (date.today() - timedelta(days=self.retention_days)).isoformat()
        for name in self._cycle_files():
            day = _CYCLES_RE.match(name).group(1)
            if day < cutoff:
                try:
                    os.remove(os.path.join(self.root, name))
                    self.logger.info("Pruned production history %s (older than %d days).",
                                     name, self.retention_days)
                except OSError as e:
                    self.logger.warning("Could not prune %s: %s", name, e)

    # --- Reading -------------------------------------------------------------

    def _cycle_files(self) -> List[str]:
        try:
            return sorted(n for n in os.listdir(self.root) if _CYCLES_RE.match(n))
        except OSError:
            return []

    def _read_lines(self, filename: str) -> List[Dict[str, Any]]:
        path = os.path.join(self.root, filename)
        records = []
        try:
            with self._file_lock, open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        except OSError as e:
            self.logger.warning("Could not read %s: %s", path, e)
            return []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue  # a line cut short by a crash: skip it, keep the rest
        return records

    def runs(self, limit: int | None = None) -> List[Dict[str, Any]]:
        """Archived runs, oldest first (the last *limit* if given)."""
        runs = self._read_lines(RUNS_FILE)
        return runs[-limit:] if limit else runs

    def cycles(self, day: str) -> List[Dict[str, Any]]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day or "")):
            raise ValueError("date must be YYYY-MM-DD")
        return self._read_lines(f"cycles-{day}.jsonl")

    def days(self) -> List[Dict[str, Any]]:
        """Per-day cycle summary, newest first."""
        out = []
        for name in reversed(self._cycle_files()):
            day = _CYCLES_RE.match(name).group(1)
            cycles = self._read_lines(name)
            statuses: Dict[str, int] = {}
            for c in cycles:
                statuses[c.get("status") or "unknown"] = statuses.get(c.get("status") or "unknown", 0) + 1
            ok_durations = [c.get("duration_s") or 0.0 for c in cycles if c.get("status") == "ok"]
            starts = sorted(c.get("started_at") for c in cycles if c.get("started_at"))
            out.append({
                "date": day,
                "cycles": len(cycles),
                "ok": statuses.get("ok", 0),
                "failed": statuses.get("failed", 0),
                "interrupted": sum(v for k, v in statuses.items() if k not in ("ok", "failed")),
                "cycle_time_s": round(sum(c.get("duration_s") or 0.0 for c in cycles), 3),
                "average_ok_cycle_s": round(sum(ok_durations) / len(ok_durations), 3) if ok_durations else None,
                "first_cycle": starts[0] if starts else None,
                "last_cycle": starts[-1] if starts else None,
            })
        return out

    # --- CSV export ----------------------------------------------------------

    @staticmethod
    def _csv(rows: Iterable[Dict[str, Any]], first_columns: List[str]) -> str:
        rows = list(rows)
        columns = list(first_columns)
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
        return buffer.getvalue()

    def runs_csv(self) -> str:
        """One row per run; variables and step averages flattened into columns."""
        rows = []
        for run in self.runs():
            row = {k: v for k, v in run.items() if not isinstance(v, (dict, list))}
            for name, value in (run.get("variables") or {}).items():
                row[f"var.{name}"] = value
            for name, value in (run.get("step_avg_s") or {}).items():
                row[f"step_avg_s.{name}"] = value
            rows.append(row)
        return self._csv(rows, ["run_id", "workspace", "run_start", "run_end", "end_event",
                                "cycle_count", "failed_cycles", "elapsed_production_s",
                                "average_cycle_duration_s", "last_error"])

    def cycles_csv(self, day: str) -> str:
        """One row per cycle of *day*, with each top-level step's duration in its own column."""
        rows = []
        for cycle in self.cycles(day):
            row = {k: cycle.get(k) for k in ("run_id", "cycle", "started_at", "duration_s", "status", "task")}
            busy = 0.0
            for frame in cycle.get("frames") or []:
                if frame.get("depth") != 0:
                    continue
                key = f"step_s.{frame.get('name')}"
                row[key] = round((row.get(key) or 0.0) + (frame.get("duration_s") or 0.0), 4)
                busy += frame.get("duration_s") or 0.0
            row["idle_s"] = round(max(0.0, (cycle.get("duration_s") or 0.0) - busy), 4)
            rows.append(row)
        return self._csv(rows, ["run_id", "cycle", "started_at", "duration_s", "status", "idle_s"])
