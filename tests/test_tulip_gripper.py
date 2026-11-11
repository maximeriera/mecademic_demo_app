"""TulipGripper driver and the tulip_gripper protocol module, against a fake driver box."""

import json
import threading
import time

import pytest

from devices.TulipGripper import TulipGripper
from devices.api.tulip_gripper import (
    TulipAborted,
    TulipDeviceError,
    TulipDriver,
    TulipMotionError,
    TulipReplyError,
)
from devices.registry import create_device, validate_devices_config

from conftest import wait_for
from fake_instrument import FakeTcpInstrument, dead_port

MOTION = ("SETPOS", "SETSPEED")


class FakeTulip:
    """The driver box, as protocol v2.0 describes it. Moves are instantaneous
    unless ``stuck`` (then it keeps moving and never arrives)."""

    def __init__(self):
        self.position = 50.0
        self.speed = 0.0
        self.travel_time = 0.05
        self.error = 0
        self.clearable = True
        self.stuck = False
        self.garble = False  # answer GETSTATE with another instruction's reply

    def handle(self, cmd):
        op, *args = cmd.split(":")
        if op == "HELP":
            return ("HELP:GETVERSION:GETIP:SETIP:GETSTATE:SETPOS:SETSPEED:SETMOTIONMAX:"
                    "SETTRAVELTIME:GETERROR:CLEARERROR")
        if op == "GETVERSION":
            return "GETVERSION:2.0.0"
        if op == "GETIP":
            return "GETIP:200.0.0.100:2000"
        if op == "GETSTATE":
            if self.garble:
                return "GETERROR:0"
            return f"GETSTATE:{self.position}:{self.speed}:4000:320000:25600000:{self.travel_time}"
        if op == "GETERROR":
            return f"GETERROR:{self.error}"
        if op == "CLEARERROR":
            if self.clearable:
                self.error = 0
            return f"GETERROR:{self.error}"
        if op == "SETPOS":
            if self.stuck:
                self.speed = 10.0
            else:
                self.position = float(args[0])
            return None
        if op == "SETSPEED":
            if float(args[0]) == 0:
                self.speed = 0.0
            return None
        if op == "SETTRAVELTIME":
            self.travel_time = float(args[0])
            return None
        return None  # setters, and anything unknown: no reply


@pytest.fixture
def box():
    return FakeTulip()


@pytest.fixture
def instrument(box):
    inst = FakeTcpInstrument(terminator=b"\r\n", unknown=box.handle)
    yield inst
    inst.close()


@pytest.fixture
def gripper(instrument):
    dev = TulipGripper("127.0.0.1", port=instrument.port, timeout=0.5, name="tulip_test")
    yield dev
    dev.shutdown()


def motion_commands(instrument, since=0):
    return [c for c in instrument.received[since:] if c.split(":")[0] in MOTION]


# --- registry ------------------------------------------------------------------

def test_registry_validates_and_creates_without_connecting():
    entries, issues = validate_devices_config(
        {"gripper": {"type": "tulip", "ip_address": "200.0.0.100", "port": "2001"}})
    assert issues == []
    assert entries[0].type == "percipio_tulip"
    assert entries[0].kwargs == {
        "ip_address": "200.0.0.100", "port": 2001, "serial_port": None, "baudrate": 115200,
        "timeout": 2.0, "travel_time_s": None, "open_position": 100.0, "close_position": 0.0,
    }
    dev = create_device(entries[0])
    assert dev.device_id == "gripper" and not dev.connected and not dev.faulted

    entries, issues = validate_devices_config(
        {"gripper": {"type": "percipio_tulip", "ip_address": "200.0.0.100", "serial_port": "COM4"}})
    assert entries == [] and "not both" in issues[0]["message"]


def test_constructor_validation():
    with pytest.raises(ValueError):
        TulipGripper(name="neither")
    with pytest.raises(ValueError):
        TulipGripper("10.0.0.1", close_position=120, name="bad_close")
    with pytest.raises(ValueError):
        TulipGripper("10.0.0.1", travel_time_s=0, name="bad_travel")


# --- lifecycle -----------------------------------------------------------------

def test_initialize_reads_identity_without_moving(gripper, instrument):
    gripper.initialize()
    assert gripper.connected and gripper.ready and not gripper.faulted
    assert gripper.info["firmware_version"] == "2.0.0"
    assert gripper.info["ip_address"] == "127.0.0.1"
    assert motion_commands(instrument) == []
    assert "SETTRAVELTIME" not in " ".join(instrument.received)


def test_travel_time_applied_only_when_different(instrument, box):
    dev = TulipGripper("127.0.0.1", port=instrument.port, timeout=0.5, travel_time_s=0.03, name="tt")
    dev.initialize()
    assert "SETTRAVELTIME:0.03" in instrument.received
    assert dev.info["travel_time_s"] == pytest.approx(0.03)
    dev.shutdown()

    sent = len(instrument.received)
    dev.initialize()   # flash already holds 0.03: no write
    assert not [c for c in instrument.received[sent:] if c.startswith("SETTRAVELTIME")]
    dev.shutdown()


def test_status_properties_never_touch_the_device(gripper, instrument):
    gripper.initialize()
    sent = len(instrument.received)
    for _ in range(10):
        gripper.faulted, gripper.ready, gripper.connected, gripper.info
    assert len(instrument.received) == sent


def test_comm_error_latches_fault_and_clear_fault_reconnects(gripper, instrument):
    gripper.initialize()
    instrument.drop_next = True
    with pytest.raises(OSError):
        gripper.api.state()
    assert gripper.faulted and not gripper.ready and not gripper.connected

    sessions, sent = instrument.connections, len(instrument.received)
    gripper.clear_fault()
    assert not gripper.faulted and gripper.ready
    assert instrument.connections == sessions + 1
    assert motion_commands(instrument, sent) == []
    assert gripper.api.position() == pytest.approx(50.0)


def test_reply_out_of_step_faults(gripper, box):
    gripper.initialize()
    box.garble = True
    with pytest.raises(TulipReplyError):
        gripper.api.state()
    assert gripper.faulted and not gripper.api.is_open


def test_gripper_error_at_initialize_then_clear_fault(gripper, instrument, box):
    box.error = 3
    with pytest.raises(TulipDeviceError, match="error 3"):
        gripper.initialize()
    assert gripper.faulted and not gripper.connected
    assert "CLEARERROR" not in instrument.received

    sent = len(instrument.received)
    gripper.clear_fault()
    assert "CLEARERROR" in instrument.received[sent:]
    assert gripper.ready
    assert motion_commands(instrument, sent) == []


def test_gripper_error_that_does_not_clear_stays_faulted(gripper, box):
    box.error, box.clearable = 2, False
    with pytest.raises(TulipDeviceError):
        gripper.initialize()
    with pytest.raises(TulipDeviceError, match="still present"):
        gripper.clear_fault()
    assert gripper.faulted


def test_gripper_error_while_running_latches_fault(gripper, box):
    gripper.initialize()
    box.error = 4
    gripper.telemetry()               # Live panel poll reads GETERROR
    assert gripper.faulted and "Gripper error 4" in gripper._fault

    gripper.clear_fault()
    assert gripper.ready


def test_gripper_error_after_a_move_raises_and_faults(gripper, box):
    gripper.initialize()
    box.error = 2
    with pytest.raises(TulipDeviceError):
        gripper.release()
    assert gripper.faulted


def test_bad_value_is_refused_before_sending_and_is_not_a_fault(gripper, instrument):
    gripper.initialize()
    sent = len(instrument.received)
    with pytest.raises(ValueError):
        gripper.api.set_position(120)
    assert len(instrument.received) == sent
    assert not gripper.faulted


def test_initialize_failure_raises_and_faults():
    dev = TulipGripper("127.0.0.1", port=dead_port(), timeout=0.5, name="dead")
    with pytest.raises(Exception):
        dev.initialize()
    assert dev.faulted and not dev.connected


def test_shutdown_is_safe_when_never_connected_and_twice():
    for dev in (TulipGripper("127.0.0.1", name="never"), TulipGripper(serial_port="COM99", name="never_usb")):
        dev.shutdown()
        dev.shutdown()
        assert not dev.connected


def test_disconnect_then_connect(gripper):
    gripper.initialize()
    gripper.disconnect()
    assert not gripper.connected
    gripper.connect()
    assert gripper.ready


# --- motion --------------------------------------------------------------------

def test_grip_and_release_use_configured_positions(instrument, box):
    dev = TulipGripper("127.0.0.1", port=instrument.port, timeout=0.5, close_position=20, name="pos")
    dev.initialize()
    dev.grip()
    assert box.position == pytest.approx(20)
    dev.release()
    assert box.position == pytest.approx(100)
    assert motion_commands(instrument) == ["SETPOS:20", "SETPOS:100"]
    dev.api.move_to(55.5)
    assert "SETPOS:55.5" in instrument.received
    dev.shutdown()


def test_wait_times_out_when_the_move_does_not_settle(gripper, box):
    gripper.initialize()
    box.stuck = True
    with pytest.raises(TulipMotionError, match="target 0"):
        gripper.api.move_to(0, timeout=0.2)


def test_abort_unblocks_a_wait_and_holds_position(gripper, instrument, box):
    gripper.initialize()
    box.stuck = True
    raised = []

    def task():
        try:
            gripper.api.move_to(0, timeout=30)
        except TulipAborted as exc:
            raised.append(exc)

    worker = threading.Thread(target=task)
    worker.start()
    time.sleep(0.2)
    started = time.monotonic()
    gripper.abort()
    worker.join(timeout=2)
    assert not worker.is_alive() and raised
    assert time.monotonic() - started < 1.5
    assert "SETSPEED:0" in instrument.received
    assert not gripper.faulted


def test_abort_with_link_down_does_not_raise():
    TulipGripper("127.0.0.1", name="down").abort()


def test_number_formatting(gripper, instrument):
    gripper.initialize()
    gripper.api.set_motion_max(6667, 888889, 118518519)
    gripper.api.set_travel_time(0.0333)
    gripper.api.set_speed(-12.5)
    # Setters get no reply: wait for the fake to log them.
    wait_for(lambda: instrument.received[-3:] == [
        "SETMOTIONMAX:6667:888889:118518519", "SETTRAVELTIME:0.0333", "SETSPEED:-12.5"])


# --- live data -----------------------------------------------------------------

def test_telemetry_and_io(gripper, instrument, box):
    assert gripper.telemetry() == {} and gripper.io_points() == []
    gripper.initialize()
    telemetry = gripper.telemetry()
    json.dumps(telemetry)
    assert telemetry["opening_pct"] == pytest.approx(50.0)

    box.position = 100.0
    assert gripper.io_points() == [{"name": "open", "kind": "do", "value": True, "writable": True}]
    gripper.write_io("open", False)
    wait_for(lambda: instrument.received[-1] == "SETPOS:0")   # no reply to wait for
    with pytest.raises(ValueError):
        gripper.write_io("do0", True)


# --- serial transport ----------------------------------------------------------

class FakeSerial:
    """Stands in for serial.Serial: answers like the box, or stays silent."""

    def __init__(self, port, baudrate, timeout=None, write_timeout=None):
        self.port, self.baudrate = port, baudrate
        self.box = FakeSerial.box
        self.out = b""

    def write(self, data):
        reply = self.box.handle(data.decode().strip())
        if reply is not None:
            self.out += reply.encode() + b"\r\n"

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.out = b""

    def read_until(self, terminator):
        line, sep, self.out = self.out.partition(terminator)
        return line + sep   # no terminator: timed out

    def close(self):
        pass


def test_serial_transport(monkeypatch, box):
    import serial

    FakeSerial.box = box
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    dev = TulipGripper(serial_port="COM7", name="usb")
    dev._api.serial_startup_delay = 0
    dev.initialize()
    assert dev.ready and dev.info == {
        "serial_port": "COM7", "model": "Percipio Tulip gripper",
        "firmware_version": "2.0.0", "travel_time_s": 0.05}
    dev.release()
    assert box.position == 100

    # A query nobody answers is a timeout (an OSError), so a fault.
    with pytest.raises(OSError):
        dev.api.query("NOSUCH")
    assert dev.faulted


def test_api_needs_exactly_one_transport():
    with pytest.raises(ValueError):
        TulipDriver()
    with pytest.raises(ValueError):
        TulipDriver("10.0.0.1", serial_port="COM3")
