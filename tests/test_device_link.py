"""Per-device link control: detach, monitoring link, reconnect."""

import time

from core.ControllerState import ControllerState
from core.Task import Task, TaskType

from conftest import wait_for


def _task_done(ctrl):
    task = ctrl._current_task
    return task is None or not task.is_alive()


def test_detached_device_keeps_cell_ready_and_only_manual_actions_run(ready_controller):
    ctrl = ready_controller

    result = ctrl.disconnect_device("part_feeder")
    assert result["success"], result
    assert result["link"] == "disconnected"
    assert ctrl.get_detached() == {"part_feeder": "disconnected"}

    time.sleep(0.5)  # a few monitor ticks
    assert ctrl.get_state() == ControllerState.READY

    assert ctrl.start_task(TaskType.PROD) is False
    assert "part_feeder" in ctrl.task_refusal_reason(TaskType.PROD)
    assert ctrl.task_refusal_reason(TaskType.MANUAL_ACTION) is None

    assert ctrl.start_manual_action("open_gripper") is True
    wait_for(lambda: _task_done(ctrl))
    wait_for(lambda: ctrl.get_state() == ControllerState.READY)


def test_reconnect_restores_cell_tasks(ready_controller):
    ctrl = ready_controller
    ctrl.disconnect_device("part_feeder")

    result = ctrl.connect_device("part_feeder")
    assert result["success"], result
    assert ctrl.get_detached() == {}

    assert ctrl.start_task(TaskType.PROD) is True
    wait_for(lambda: ctrl.context.metrics.get_cycle_count() >= 1)
    ctrl.stop_current_task()
    wait_for(lambda: _task_done(ctrl))


def test_monitor_ignores_a_faulted_detached_device(ready_controller):
    ctrl = ready_controller
    ctrl.disconnect_device("inspection_camera")
    ctrl.devices["inspection_camera"]._faulted = True

    time.sleep(0.6)
    assert ctrl.get_state() == ControllerState.READY


def test_monitoring_link_keeps_data_sources_running(ready_controller):
    ctrl = ready_controller

    result = ctrl.disconnect_device("my_meca_robot")
    assert result["success"], result
    assert result["link"] == "monitoring"
    assert ctrl.get_detached() == {"my_meca_robot": "monitoring"}
    assert ctrl._device_available("my_meca_robot") is True

    ctrl.disconnect_device("part_feeder")
    assert ctrl._device_available("part_feeder") is False
    assert ctrl._device_available("inspection_camera") is True


def test_link_actions_refused_while_a_task_runs(ready_controller):
    ctrl = ready_controller
    assert ctrl.start_task(TaskType.PROD) is True
    wait_for(lambda: ctrl.get_state() == ControllerState.BUSY)

    result = ctrl.disconnect_device("part_feeder")
    assert result["success"] is False
    assert "task is running" in result["message"]
    assert ctrl.get_detached() == {}

    ctrl.abort_current_task()
    wait_for(lambda: _task_done(ctrl))


def test_failed_connect_keeps_device_detached(ready_controller):
    ctrl = ready_controller
    ctrl.disconnect_device("part_feeder")
    ctrl.devices["part_feeder"]._fail_to_connect = True

    result = ctrl.connect_device("part_feeder")
    assert result["success"] is False
    assert result["detached"] is True
    assert "part_feeder" in ctrl.get_detached()

    time.sleep(0.5)
    assert ctrl.get_state() == ControllerState.READY


def test_initialize_leaves_detached_devices_alone(ready_controller):
    ctrl = ready_controller
    ctrl.disconnect_device("part_feeder")

    ctrl.initialize()
    assert ctrl.devices["part_feeder"].link == "disconnected"
    assert "part_feeder" in ctrl.get_detached()
    assert ctrl.devices["my_meca_robot"].link == "control"


def test_shutdown_clears_detached_devices(ready_controller):
    ctrl = ready_controller
    ctrl.disconnect_device("my_meca_robot")

    ctrl.shutdown()
    assert ctrl.get_detached() == {}
    assert ctrl.devices["my_meca_robot"].link == "disconnected"
    assert ctrl.get_state() == ControllerState.OFF


def test_device_without_reconnect_support_is_refused(ready_controller):
    ctrl = ready_controller
    ctrl.devices["part_feeder"].supports_reconnect = False

    result = ctrl.disconnect_device("part_feeder")
    assert result["success"] is False
    assert ctrl.get_detached() == {}


def test_unknown_device_is_refused(ready_controller):
    result = ready_controller.connect_device("nope")
    assert result["success"] is False
    assert result["message"].startswith("Unknown device")


def test_clear_faults_skips_detached_devices(ready_controller):
    ctrl = ready_controller
    ctrl.disconnect_device("inspection_camera")
    ctrl.devices["inspection_camera"]._faulted = True
    ctrl.set_state(ControllerState.FAULTED)

    result = ctrl.clear_faults()
    assert result["success"], result
    assert "inspection_camera" not in result["cleared"]
    assert ctrl.get_state() == ControllerState.READY


def test_clear_device_fault_leaves_faulted_when_healthy(ready_controller):
    ctrl = ready_controller
    ctrl.devices["part_feeder"]._faulted = True
    wait_for(lambda: ctrl.get_state() == ControllerState.FAULTED)

    result = ctrl.clear_device_fault("part_feeder")
    assert result["success"], result
    assert ctrl.get_state() == ControllerState.READY


class _CountingDevice:
    def __init__(self, device_id):
        self.device_id = device_id
        self.aborts = 0

    def abort(self):
        self.aborts += 1


def test_task_abort_skips_detached_devices(controller):
    devices = {"a": _CountingDevice("a"), "b": _CountingDevice("b")}
    task = Task(
        logger=controller.logger,
        task_type=TaskType.HOME,
        state_change_callback=lambda _state: None,
        devices=devices,
        context=controller.context,
        is_detached=lambda device_id: device_id == "b",
    )
    task.abort()
    assert devices["a"].aborts == 1
    assert devices["b"].aborts == 0
