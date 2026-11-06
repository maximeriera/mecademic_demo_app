"""Production history on disk: runs and cycle timelines, CSV export, restart."""

import csv
import io
import logging
import os
from datetime import date, timedelta

import pytest

from core.History import HistoryStore
from core.Task import TaskType

from conftest import wait_for

LOG = logging.getLogger("test.history")


def _cycle(n, status="ok", started_at="2026-10-30T10:00:00+00:00", run_id="r1"):
    return {
        "run_id": run_id, "cycle": n, "task": "Production", "started_at": started_at,
        "duration_s": 3.0, "status": status,
        "frames": [
            {"name": "Pick part", "path": "Pick part", "depth": 0, "offset_s": 0.0, "duration_s": 1.0, "status": "ok"},
            {"name": "Close gripper", "path": "Pick part / Close gripper", "depth": 1,
             "offset_s": 0.5, "duration_s": 0.2, "status": "ok"},
            {"name": "Place part", "path": "Place part", "depth": 0, "offset_s": 1.5, "duration_s": 1.0, "status": status},
        ],
    }


@pytest.fixture
def store(tmp_path):
    s = HistoryStore(str(tmp_path / "history"), LOG)
    yield s
    s.close()


def test_runs_and_cycles_round_trip_with_csv(store):
    store.record_cycle(_cycle(1))
    store.record_cycle(_cycle(2, status="failed"))
    store.record_run({"run_id": "r1", "run_start": "2026-10-30T10:00:00+00:00", "end_event": "prod_stop",
                      "cycle_count": 1, "failed_cycles": 1, "variables": {"part_count": 1},
                      "step_avg_s": {"Pick part": 1.0}})
    assert store.flush()

    day = date.fromisoformat("2026-10-30")  # local date of the timestamps above, ± the TZ
    (summary,) = store.days()
    assert summary["cycles"] == 2 and summary["ok"] == 1 and summary["failed"] == 1
    assert abs(date.fromisoformat(summary["date"]) - day) <= timedelta(days=1)

    runs = list(csv.DictReader(io.StringIO(store.runs_csv())))
    assert runs[0]["var.part_count"] == "1" and runs[0]["step_avg_s.Pick part"] == "1.0"

    cycles = list(csv.DictReader(io.StringIO(store.cycles_csv(summary["date"]))))
    assert [c["status"] for c in cycles] == ["ok", "failed"]
    assert cycles[0]["step_s.Pick part"] == "1.0"
    assert "step_s.Close gripper" not in cycles[0]       # nested steps are not columns
    assert cycles[0]["idle_s"] == "1.0"


def test_a_line_cut_short_by_a_crash_is_skipped(tmp_path):
    root = tmp_path / "history"
    root.mkdir()
    # The previous process died mid-write.
    (root / "runs.jsonl").write_text('{"run_id": "a"}\n{"run_id": "cut sho', encoding="utf-8")
    store = HistoryStore(str(root), LOG)
    try:
        store.record_run({"run_id": "b"})
        assert store.flush()
        assert [r["run_id"] for r in store.runs()] == ["a", "b"]
    finally:
        store.close()


def test_old_cycle_files_are_pruned(tmp_path):
    root = tmp_path / "history"
    root.mkdir()
    old = (date.today() - timedelta(days=40)).isoformat()
    recent = (date.today() - timedelta(days=2)).isoformat()
    for day in (old, recent):
        (root / f"cycles-{day}.jsonl").write_text("{}\n", encoding="utf-8")
    store = HistoryStore(str(root), LOG, retention_days=30)
    store.close()
    assert sorted(os.listdir(root)) == [f"cycles-{recent}.jsonl"]


def test_bad_date_is_rejected(store):
    with pytest.raises(ValueError):
        store.cycles("../../etc")


def test_prod_runs_survive_a_controller_restart(cell_dir, ready_controller):
    from core.ApplicationController import ApplicationController

    ctrl = ready_controller
    assert ctrl.history is not None
    assert ctrl.start_task(TaskType.PROD)
    wait_for(lambda: ctrl.context.metrics.get_cycle_count() >= 2, timeout=10)
    ctrl.stop_current_task()
    wait_for(lambda: ctrl._current_task is None or not ctrl._current_task.is_alive())
    assert ctrl.history.flush()

    (run,) = ctrl.history.runs()
    assert run["end_event"] == "prod_stop"
    assert run["workspace"] == cell_dir.name
    assert "Pick part" in run["step_avg_s"]
    cycles = ctrl.history.cycles(ctrl.history.days()[0]["date"])
    assert len(cycles) >= 2
    assert {c["run_id"] for c in cycles} == {run["run_id"]}   # runs and cycles share the id

    ctrl.shutdown()
    ctrl.close()
    restarted = ApplicationController(config_path=str(cell_dir / "config.yaml"))
    try:
        history = restarted.get_prod_snapshot()["metrics"]["run_history"]
        assert [r["run_id"] for r in history] == [run["run_id"]]
    finally:
        restarted.shutdown()
        restarted.close()


def test_history_can_be_disabled(tmp_path):
    from core.ApplicationController import ApplicationController

    (tmp_path / "config.yaml").write_text(
        "history: {enabled: false}\ndevices:\n  a: {type: simulated}\n", encoding="utf-8")
    ctrl = ApplicationController(config_path=str(tmp_path / "config.yaml"))
    try:
        assert ctrl.history is None
    finally:
        ctrl.shutdown()
        ctrl.close()
