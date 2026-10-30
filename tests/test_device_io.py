"""Live data and I/O panel: Device API, controller guards, driver helpers."""

import threading

from core.ControllerState import ControllerState
from core.Task import TaskType
from devices.Device import Device
from devices.SimulatedDevice import SimulatedDevice
from devices.api.IoLogikE1212Api import _SerializedClient

from conftest import wait_for


def test_simulated_device_io_loopback():
    dev = SimulatedDevice(name="sim_io")
    dev.initialize()
    assert dev.has_live_data
    dev.write_io("do1", True)
    points = {p["name"]: p for p in dev.io_points()}
    assert points["do1"]["value"] is True and points["do1"]["writable"]
    assert points["di2"]["value"] is True          # di2 loops back do1
    assert dev.telemetry()["link"] == "control"


def test_base_device_has_no_live_data():
    class Bare(Device):
        info = connected = ready = faulted = api = None

        def initialize(self): pass
        def shutdown(self): pass
        def clear_fault(self): pass
        def abort(self): pass

    assert Bare("bare").has_live_data is False


def test_controller_live_and_write(ready_controller):
    ctrl = ready_controller
    live = ctrl.get_device_live("part_feeder")
    assert live["error"] is None
    assert any(p["name"] == "do1" for p in live["io"])
    assert ctrl.get_device_live("nope") is None

    result = ctrl.write_device_io("part_feeder", "do2", True)
    assert result["success"], result
    assert ctrl.devices["part_feeder"]._outputs[1] is True

    bad = ctrl.write_device_io("part_feeder", "di1", True)
    assert bad["success"] is False and bad.get("bad_request")


def test_write_refused_while_task_runs_or_detached(ready_controller):
    ctrl = ready_controller
    ctrl.disconnect_device("part_feeder")
    assert "released" in ctrl.write_device_io("part_feeder", "do1", True)["message"]
    ctrl.connect_device("part_feeder")

    assert ctrl.start_task(TaskType.PROD)
    wait_for(lambda: ctrl.get_state() == ControllerState.BUSY)
    result = ctrl.write_device_io("part_feeder", "do1", True)
    assert result["success"] is False and "task is running" in result["message"]
    ctrl.abort_current_task()
    wait_for(lambda: ctrl._current_task is None or not ctrl._current_task.is_alive())


def test_serialized_modbus_client_never_overlaps_calls():
    active = []
    overlaps = []

    class SlowClient:
        def read_coils(self, *_args, **_kwargs):
            active.append(1)
            if len(active) > 1:
                overlaps.append(True)
            threading.Event().wait(0.01)
            active.pop()

    client = _SerializedClient(SlowClient())
    threads = [threading.Thread(target=client.read_coils) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlaps == []
