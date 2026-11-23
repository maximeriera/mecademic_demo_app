# Faking an existing driver's device

How to test a driver that already exists, without hardware. The drivers written with `add-device` use a raw socket, so `tests/fake_instrument.py` covers them. The older ones sit on libraries (pymodbus, pmclib, harvesters, pyfirmata2, mecademicpy) or on hand-rolled socket code with odd habits. Fake at the lowest layer that still runs the driver's own logic, the same principle as `add-device/references/testing.md`.

pytest-mock is **not** installed, so use pytest's built-in `monkeypatch`. All the vendor SDKs in requirements.txt are installed in `.venv`. Patch the name where the code looks it up, not where it's defined.

## Contents

1. Choosing the layer, per driver
2. Library-backed: monkeypatch the client class
3. SDK not installed: inject a fake module
4. Proving A1: status reads do no I/O and don't wait on the lock
5. Proving B1: a link drop latches the fault
6. Proving C5: abort unblocks a wait
7. Building the device

## 1. Choosing the layer

| Driver | Transport | Fake |
|---|---|---|
| SmarActMCS2, TulipGripper (TCP), ThorlabsPowerMeter (TCPIP) | raw socket in devices/api | `FakeTcpInstrument` (testing.md) |
| AsyrilEyePlus | raw socket, `devices/api/AsyrilAPI.py` | `FakeTcpInstrument` with the Eye+ replies. Check its framing and terminator in AsyrilAPI.py first |
| LMISensor | 3 sockets: control, data, health | three `FakeTcpInstrument`s, one per port argument |
| IoLogikE1212 | pymodbus `ModbusTcpClient` in IoLogikE1212Api.py | monkeypatch `devices.api.IoLogikE1212Api.ModbusTcpClient` |
| PlanarMotor | pmclib `system_commands`/`xbot_commands` in PlanarMotorApi.py | monkeypatch the `sys`/`bot` module aliases in `devices.api.PlanarMotorApi` (note: `sys` there is `pmclib.system_commands`, not Python's sys) |
| ArduinoBoard | `pyfirmata2.Arduino` | monkeypatch `devices.ArduinoBoard.pyfirmata` with a fake module holding `Arduino` |
| GigEVisionDevice | harvesters `Harvester` | `tests/test_video.py` already fakes it: reuse that |
| BrainboxesEDDigital | asyncio socket in ed_device.py | `FakeTcpInstrument` if the protocol is line-based, else a subclass with its own `handle()` |
| MecaRobot | mecademicpy | `tests/test_meca_robot.py` replaces `devices.MecaRobot.mdr`: reuse its `FakeRobot` |

Before writing a fake, read the API module's methods the driver calls, and copy their exact reply format into the fake. A fake that answers in the wrong format tests nothing.

## 2. Library-backed: monkeypatch the client class

```python
import devices.api.IoLogikE1212Api as io_api
from devices.IoLogikE1212 import IoLogikE1212


class FakeModbusClient:
    """Stands in for pymodbus.client.ModbusTcpClient. Counts every call, and
    can be told to fail like a dropped link."""

    def __init__(self, host, port=502, **kwargs):
        self.calls = []
        self.connected = False
        self.fail_with = None          # an exception instance raised by the next call
        self.di = [False] * 8
        self.coils = [False] * 8

    def _call(self, name):
        self.calls.append(name)
        if self.fail_with is not None:
            exc, self.fail_with = self.fail_with, None
            raise exc

    def connect(self):
        self._call("connect"); self.connected = True; return True

    def close(self):
        self.calls.append("close"); self.connected = False

    def read_discrete_inputs(self, address, count=1, **kw):
        self._call("read_discrete_inputs")
        return _Result(bits=self.di[address:address + count])
    # ...only the methods the API module calls. Read it to know which ones.


@pytest.fixture
def fake_client(monkeypatch):
    made = []
    def factory(*a, **kw):
        made.append(FakeModbusClient(*a, **kw))
        return made[-1]
    monkeypatch.setattr(io_api, "ModbusTcpClient", factory)
    return made          # made[-1] is the live client; len(made) counts sessions
```

`len(made)` plays the role of `instrument.connections`: after `clear_fault()`, a new client means the session was reopened (B4).

## 3. SDK not installed: inject a fake module

If the driver imports an SDK that isn't installed in this environment, put a fake in `sys.modules` before importing the driver, and import the driver inside the fixture:

```python
import importlib, sys, types

@pytest.fixture
def driver_module(monkeypatch):
    fake = types.ModuleType("vendor_sdk")
    fake.Client = FakeClient
    monkeypatch.setitem(sys.modules, "vendor_sdk", fake)
    monkeypatch.delitem(sys.modules, "devices.VendorThing", raising=False)
    return importlib.import_module("devices.VendorThing")
```

Name it in the report's "Not verified" section: the real SDK's behavior wasn't exercised.

## 4. Proving A1: no I/O, no waiting on the lock

Two tests. The first catches queries, the second catches a status read that queues behind a long task exchange.

```python
def test_status_properties_never_touch_the_device(device, fake):
    device.initialize()
    before = len(fake.calls)          # or len(instrument.received) for a socket fake
    for _ in range(20):
        device.faulted, device.ready, device.connected, device.info
    assert len(fake.calls) == before


def test_status_properties_dont_wait_for_a_task_exchange(device):
    device.initialize()
    lock = device.api._lock           # whatever lock the API serializes exchanges with
    held = threading.Event(); release = threading.Event()

    def long_exchange():
        with lock:
            held.set(); release.wait(2)

    t = threading.Thread(target=long_exchange); t.start(); held.wait(1)
    try:
        started = time.monotonic()
        device.faulted, device.ready, device.connected, device.info
        assert time.monotonic() - started < 0.1
    finally:
        release.set(); t.join()
```

**Socket peeks are invisible to a fake server.** `AsyrilAPI.connected` uses `recv(1, MSG_PEEK)`, which sends nothing, so `instrument.received` doesn't change. When the chain ends in a peek, count calls on the socket object instead:

```python
peeks = []
real = device.api._connection
class Spy:
    def __getattr__(self, n): return getattr(real, n)
    def recv(self, *a): peeks.append(a); return real.recv(*a)
device.api._connection = Spy()
```

Or test the property the driver actually reads. If the driver reads `api._connected` (a plain attribute) rather than `api.connected` (the peeking property), A1 passes for the driver even though the API property does I/O. Report the API property as a Minor note.

## 5. Proving B1: a link drop latches the fault

- Socket fake: `instrument.drop_next = True`, then a task call, which raises `OSError`. Then `assert device.faulted and not device.ready`.
- Library fake: `fake.fail_with = <the library's link error>` (pymodbus: `ConnectionException("lost")`, pmclib: whatever the API module catches, or an `OSError`), then a task call through `device.api`.

Then B4: `device.clear_fault()` gives `not device.faulted`, a new session (connections or factory calls + 1), a task call that works, and no motion/homing calls in the fake's log.

A legacy API that **swallows** the error (catches it, logs it, returns None) still has to latch `faulted`. Assert on `device.faulted`, not on the exception.

## 6. Proving C5: abort unblocks a wait

Only for devices that move or block (a planar motor's `wait_for_move`, an Asyril `get_part` waiting for parts, a homing call). Make the fake report "busy" forever, start the blocking call in a thread, call `device.abort()` from the test thread, and assert the worker ends within about 1.5 s. Use the template in testing.md. If the API has no abort mechanism at all, the test fails: keep it, and mark it xfail (strict) with the finding ID.

## 7. Building the device

Build it the way the app does at least once, so registry and constructor mismatches show up (F1):

```python
from devices.registry import validate_devices_config, create_device
entries, issues = validate_devices_config({"dut": {"type": "<type>", "ip_address": "127.0.0.1", "port": instrument.port}})
assert issues == []
dev = create_device(entries[0])
```

Use short timeouts (`timeout=0.5`) where the constructor takes one, and `conftest.wait_for()` rather than long sleeps.
