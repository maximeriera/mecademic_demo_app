"""Per-cycle step timelines (Production tab Gantt chart)."""

import importlib
import logging
import time

from core.ControllerState import ControllerState
from core.StepTracker import StepTracker
from core.Task import TaskType

from conftest import wait_for


def _tracker():
    tracker = StepTracker(logging.getLogger("test.steps"), {"prod_cycle": ["A", "C", "Skipped"]})
    tracker.begin_task("Production", "prod_cycle")
    return tracker


def test_cycle_records_offsets_and_nesting():
    tracker = _tracker()
    seen = []
    # The callback runs outside the lock: reading the tracker from it must not deadlock.
    tracker.on_cycle_end = lambda record: seen.append((record, tracker.snapshot()["timeline_rev"]))

    tracker.begin_cycle(1)
    with tracker.step("A"):
        time.sleep(0.02)
        with tracker.step("B"):
            time.sleep(0.01)
    time.sleep(0.02)  # idle gap
    with tracker.step("C"):
        pass
    tracker.end_cycle("ok")

    data = tracker.timelines()
    assert data["order"] == ["A", "C", "Skipped"]
    (cycle,) = data["timelines"]
    assert cycle["status"] == "ok" and cycle["cycle"] == 1
    frames = {f["path"]: f for f in cycle["frames"]}
    assert frames["A / B"]["depth"] == 1
    assert frames["A"]["offset_s"] <= frames["A / B"]["offset_s"]
    assert frames["C"]["offset_s"] >= frames["A"]["offset_s"] + frames["A"]["duration_s"] + 0.015
    assert len(seen) == 1 and seen[0][1] == data["rev"]


def test_end_cycle_is_idempotent_after_a_fault_closed_it():
    tracker = _tracker()
    tracker.begin_cycle(1)
    tracker.abandon_open_steps("faulted")      # no step open at that instant
    tracker.end_cycle("failed")                # the task thread unwinding later
    (cycle,) = tracker.timelines()["timelines"]
    assert cycle["status"] == "faulted"


def test_frames_after_a_closed_cycle_do_not_leak_into_it():
    tracker = _tracker()
    tracker.begin_cycle(1)
    tracker.end_cycle("ok")
    with tracker.step("late"):
        pass
    tracker.begin_cycle(None)                  # exit home: no cycle recorded
    with tracker.step("Home (exit)"):
        pass
    tracker.end_task()
    (cycle,) = tracker.timelines()["timelines"]
    assert cycle["frames"] == []


def test_unclosed_cycle_is_closed_by_the_next_one_and_end_task():
    tracker = _tracker()
    tracker.begin_cycle(1)
    tracker.begin_cycle(2)
    tracker.end_task()
    statuses = [t["status"] for t in tracker.timelines()["timelines"]]
    assert statuses == ["abandoned", "abandoned"]


def test_run_ids_are_unique_strings():
    tracker = _tracker()
    first = tracker._task["run_id"]
    tracker.begin_task("Production", "prod_cycle")
    assert isinstance(first, str) and first != tracker._task["run_id"]


def test_prod_run_records_ok_cycles(ready_controller):
    ctrl = ready_controller
    assert ctrl.start_task(TaskType.PROD)
    wait_for(lambda: len(ctrl.get_prod_timelines()["timelines"]) >= 2, timeout=10)
    ctrl.stop_current_task()
    wait_for(lambda: ctrl._current_task is None or not ctrl._current_task.is_alive())

    data = ctrl.get_prod_timelines()
    assert all(t["status"] == "ok" for t in data["timelines"])
    names = {f["name"] for f in data["timelines"][0]["frames"]}
    assert {"Request part from feeder", "Pick part", "Close gripper"} <= names
    assert "Pick part" in data["avg_s"]


def test_failed_cycle_is_recorded_as_failed(ready_controller, monkeypatch):
    def failing_cycle(devices, context):
        with context.step("Pick part"):
            raise RuntimeError("gripper jammed")

    # core/__init__ exports the Task class as core.Task, shadowing the module.
    task_module = importlib.import_module("core.Task")
    real_loader = task_module.load_task_function
    monkeypatch.setattr(
        task_module, "load_task_function",
        lambda name: failing_cycle if name == "prod_cycle" else real_loader(name),
    )
    ctrl = ready_controller
    assert ctrl.start_task(TaskType.PROD)
    wait_for(lambda: ctrl.get_state() == ControllerState.FAULTED)
    (cycle,) = ctrl.get_prod_timelines()["timelines"]
    assert cycle["status"] == "failed"
    assert cycle["frames"][0]["status"] == "failed"
