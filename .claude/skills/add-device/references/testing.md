# Testing a driver without hardware

The suite runs without hardware (`README.md` > Tests), and a new driver keeps it that way. Fake the device at the lowest layer you can:

- **Own protocol module over TCP**: a scripted fake TCP instrument on 127.0.0.1. This exercises the real protocol module and the real driver together, framing and error handling included. It's the best option, so use it whenever the transport is a socket.
- **Vendor SDK, pyserial, PyVISA, pymodbus**: monkeypatch the SDK symbol in the driver module with a small fake class (see `tests/test_meca_robot.py`, which replaces `devices.MecaRobot.mdr`).

## The shared fake: `tests/fake_instrument.py`

If this file doesn't exist yet, create it with exactly this content. If it exists, reuse it, and extend it only in a backward-compatible way.

```python
"""Scripted fake instruments for driver tests: no hardware, no vendor SDK."""

import socketserver
import threading


class _LineHandler(socketserver.StreamRequestHandler):
    def handle(self):
        inst = self.server
        with inst.lock:
            inst.connections += 1
        for raw in self.rfile:
            cmd = raw.decode("ascii", errors="replace").strip()
            if not cmd:
                continue
            with inst.lock:
                inst.received.append(cmd)
                drop, inst.drop_next = inst.drop_next, False
            if drop:
                return  # closes the connection: the driver sees the link drop
            reply = inst.reply(cmd)
            if reply is not None:
                self.wfile.write(reply.encode("ascii") + inst.terminator)


class FakeTcpInstrument(socketserver.ThreadingTCPServer):
    """A line-based TCP instrument on 127.0.0.1 that answers from a script.

    ``replies`` maps a command to its reply line: a string, a callable taking
    the command (for state or arguments), or ``None`` for no reply (a setter,
    or silence to provoke a timeout). Commands not in the map get ``unknown``.

    ``received`` lists every command, ``connections`` counts sessions, and
    setting ``drop_next`` closes the connection on the next command instead of
    answering it.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, replies=None, terminator=b"\r\n", unknown=None):
        super().__init__(("127.0.0.1", 0), _LineHandler)
        self.replies = dict(replies or {})
        self.terminator = terminator
        self.unknown = unknown
        self.received = []
        self.connections = 0
        self.drop_next = False
        self.lock = threading.Lock()
        # Short poll: close() waits up to one interval for the loop to notice.
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def reply(self, cmd):
        answer = self.replies.get(cmd, self.unknown)
        return answer(cmd) if callable(answer) else answer

    def close(self):
        self.shutdown()
        self.server_close()


def dead_port() -> int:
    """A local port with nothing listening: connecting to it is refused."""
    inst = FakeTcpInstrument()
    port = inst.port
    inst.close()
    return port
```

Tests import it as `from fake_instrument import FakeTcpInstrument, dead_port`. `tests/` has no `__init__.py`, so pytest puts it on `sys.path`, the same way the existing tests do `from conftest import wait_for`.

Binary protocols need their own handler. Subclass and override `_LineHandler.handle` in the test file, and keep the same `received`/`connections`/`drop_next` attributes.

## What to cover

Each item states the bug it catches. Cover every item that applies to the device, and name each test after the behavior.

| Test | Catches |
|---|---|
| Registry: a minimal entry validates with the expected defaults and coercion (a string `"9101"` for an int key becomes `9101`); a missing required key is an error; `create_device()` builds it with `device_id` equal to the entry name, and it isn't connected yet | A Param that doesn't match a constructor argument, a wrong default, a constructor that connects or doesn't accept `name` |
| `initialize()` → `connected`, `ready`, not `faulted`; `info` shows the identity read from the device | Identity parsing, startup settings not sent (assert they're in `instrument.received`) |
| After initialize, reading `faulted`, `ready`, `connected` and `info` many times sends **no** commands (`len(instrument.received)` unchanged) | A status property that queries the hardware (stalls the fault monitor) |
| Comm error mid-session (`drop_next = True`, then a task call) → the call raises `OSError`, the device is `faulted` and not `ready` | Comm errors not latched: the cell keeps running on a dead link |
| `clear_fault()` after that → a new session (`connections` + 1), not faulted, task calls work again, and **no motion/homing commands** sent | clear_fault that only drops the flag (stale session), or that moves hardware |
| The device refuses a command (its error reply) → `XCommandError` raised, device **not** faulted | Usage errors faulting the whole cell |
| `initialize()` against `dead_port()` → raises, `faulted` is true, `connected` is false | A failed init reported as healthy |
| `shutdown()` on a never-initialized device, and twice in a row → no exception | Errors at app exit or workspace switch |
| `disconnect()` then `connect()` → ready again (if `supports_reconnect`) | Reconnect broken by leftover state |
| `abort()` while another thread is in a blocking `wait()` → the wait raises `XAborted` within about a second, and the stop command was sent (moving devices only) | ABORT that doesn't stop the task thread |
| `abort()` while the move command itself is in flight → the move raises `XAborted` even though the device then reports idle (moving devices only) | The race where the abort counter is read after the command is sent, so a stopped move reports success |
| `abort()`, `shutdown()` and `clear_fault()` send no command that switches an output, light or heater off (`instrument.received`) | Outputs dropped without the user asking (SKILL.md rule 15) |
| `telemetry()` is `{}` when not connected and JSON-serializable when connected; `io_points()` has the documented shape; `write_io()` on a read-only or unknown point raises `ValueError` | Live panel errors |
| Constructor validation: a bad enum-like value raises `ValueError` | Silent misconfiguration from task code |

## Template

```python
"""AcmeLaserSensor driver and the acme_ls protocol module, against a fake TCP sensor."""

import json
import threading
import time

import pytest

from devices.AcmeLaserSensor import AcmeLaserSensor
from devices.api.acme_ls import AcmeLSAborted, AcmeLSCommandError
from devices.registry import create_device, validate_devices_config

from fake_instrument import FakeTcpInstrument, dead_port

REPLIES = {
    "ID?": "ACME,LS-100,SN1234,1.2",
    "MS,00": "12345",      # micrometres
    "MS,07": "ER,2",       # out of range
    "STOP": "OK",
}


@pytest.fixture
def instrument():
    inst = FakeTcpInstrument(REPLIES)
    yield inst
    inst.close()


@pytest.fixture
def sensor(instrument):
    dev = AcmeLaserSensor("127.0.0.1", port=instrument.port, timeout=0.5, name="ls_test")
    yield dev
    dev.shutdown()


def test_registry_validates_and_creates_without_connecting():
    entries, issues = validate_devices_config(
        {"ls": {"type": "acme_ls", "ip_address": "10.0.0.5", "port": "9101"}})
    assert issues == []
    assert entries[0].kwargs == {"ip_address": "10.0.0.5", "port": 9101, "timeout": 5.0}
    dev = create_device(entries[0])
    assert dev.device_id == "ls" and not dev.connected

    entries, issues = validate_devices_config({"ls": {"type": "acme_ls"}})
    assert entries == [] and "missing required 'ip_address'" in issues[0]["message"]


def test_initialize_reads_identity(sensor):
    sensor.initialize()
    assert sensor.connected and sensor.ready and not sensor.faulted
    assert "LS-100" in sensor.info["model"]


def test_status_properties_never_touch_the_device(sensor, instrument):
    sensor.initialize()
    sent = len(instrument.received)
    for _ in range(10):
        sensor.faulted, sensor.ready, sensor.connected, sensor.info
    assert len(instrument.received) == sent


def test_comm_error_latches_fault_and_clear_fault_reconnects(sensor, instrument):
    sensor.initialize()
    instrument.drop_next = True
    with pytest.raises(OSError):
        sensor.api.distance_mm(0)
    assert sensor.faulted and not sensor.ready

    sessions = instrument.connections
    sensor.clear_fault()
    assert not sensor.faulted and sensor.ready
    assert instrument.connections == sessions + 1
    assert sensor.api.distance_mm(0) == pytest.approx(12.345)


def test_refused_command_is_not_a_fault(sensor):
    sensor.initialize()
    with pytest.raises(AcmeLSCommandError):
        sensor.api.distance_mm(7)
    assert not sensor.faulted


def test_initialize_failure_raises_and_faults():
    dev = AcmeLaserSensor("127.0.0.1", port=dead_port(), timeout=0.5, name="dead")
    with pytest.raises(Exception):
        dev.initialize()
    assert dev.faulted and not dev.connected


def test_shutdown_is_safe_when_never_connected_and_twice():
    dev = AcmeLaserSensor("127.0.0.1", name="never")
    dev.shutdown()
    dev.shutdown()
    assert not dev.connected


def test_disconnect_then_connect(sensor):
    sensor.initialize()
    sensor.disconnect()
    assert not sensor.connected
    sensor.connect()
    assert sensor.ready


def test_telemetry(sensor):
    assert sensor.telemetry() == {}
    sensor.initialize()
    json.dumps(sensor.telemetry())


# Moving or blocking devices only, whose abort() calls api.abort() (driver-patterns.md section 5).
# The sensor skeleton's abort() only logs, so this test is for a stage, an axis, a conveyor...
def test_abort_unblocks_a_wait(sensor, instrument):
    instrument.replies["BUSY?"] = "1"          # busy forever
    sensor.initialize()
    raised = []

    def task():
        try:
            sensor.api.wait(timeout=30)
        except AcmeLSAborted as exc:
            raised.append(exc)

    worker = threading.Thread(target=task)
    worker.start()
    time.sleep(0.2)
    started = time.monotonic()
    sensor.abort()
    worker.join(timeout=2)
    assert not worker.is_alive() and raised
    assert time.monotonic() - started < 1.5
    assert "STOP" in instrument.received


# Moving devices only: an abort that lands while the move command is in flight,
# before wait() starts polling, must still fail the move.
def test_abort_during_move_command_is_not_missed(sensor, instrument):
    instrument.replies["BUSY?"] = "0"          # the device already stopped

    def slow_move_reply(cmd):
        threading.Thread(target=sensor.abort).start()   # bumps the counter at once
        time.sleep(0.1)
        return "OK"

    instrument.replies["PA,1000"] = slow_move_reply
    sensor.initialize()
    with pytest.raises(AcmeLSAborted):
        sensor.api.move_to_mm(1.0)
```

Keep timeouts short (`timeout=0.5`) so a failing test fails fast. Never sleep in tests longer than a few hundred milliseconds. Use `conftest.wait_for(predicate)` to wait on a condition.
