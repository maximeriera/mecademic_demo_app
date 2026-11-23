# Driver patterns (`devices/<ClassName>.py`)

The full skeleton behind the rules in SKILL.md Step 3, with the reason for each choice. It matches `devices/SmarActMCS2.py` and `devices/ThorlabsPowerMeter.py`. Read whichever is closer to the new device. Neither is perfect for every device, but both follow the pattern exactly.

## Contents

1. Threading model: why status properties never do I/O
2. Skeleton for a driver with its own protocol module
3. Variant: a vendor SDK
4. Variant: an I/O module
5. abort() by device kind
6. Optional members

## 1. Threading model

The controller (`core/ApplicationController.py`) and Flask touch every device concurrently:

- **The fault monitor** (`_monitor_devices_status`) reads `faulted` and then `ready` on every attached device, every 200 ms. If `faulted` blocks for 5 s behind a long task query, fault detection for the *whole cell* stalls for 5 s. If `faulted` raises, the monitor thread can die.
- **Flask** reads `info` and `connected`/`link` for `/api/info` every second, for every open browser tab. If `info` queries the device, every open tab adds traffic on the link the task is using.
- **The Live panel** calls `telemetry()` / `io_points()` about twice a second while open. These may talk to the device, but only through the API lock, so they interleave with the task cleanly.
- **`abort()`** is called on STOP/ABORT and when *any* device faults, while the task thread may be inside a blocking call on this device.

So: status comes from attributes the driver updates as a side effect of real exchanges (connect, comm errors). The identity is read once at connect. Hardware I/O happens only in lifecycle methods, live-data methods, and task calls.

## 2. Skeleton: driver over its own protocol module

```python
from __future__ import annotations

from typing import Callable, Optional

from .Device import Device
from .api.acme_ls import DEFAULT_PORT, AcmeLS, AcmeLSError


class _CellAcmeLS(AcmeLS):
    """:class:`AcmeLS` that reports communication failures to its device.

    Every exchange goes through :meth:`write` or :meth:`query`, so these two
    overrides see a timeout or a lost link whichever method the task code
    called. Errors the sensor reports (a bad parameter, ...) are usage errors,
    not link failures, and are not reported.
    """

    def __init__(self, *args, on_comm_error: Optional[Callable[[Exception], None]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_comm_error = on_comm_error

    def write(self, command: str) -> None:
        try:
            super().write(command)
        except OSError as exc:
            self._report(exc)
            raise

    def query(self, command: str) -> str:
        try:
            return super().query(command)
        except OSError as exc:
            self._report(exc)
            raise

    def _report(self, exc: Exception) -> None:
        if self._on_comm_error is not None:
            self._on_comm_error(exc)


class AcmeLaserSensor(Device):
    """
    Device wrapper for ACME LS-series laser displacement sensors, over the
    sensor's Ethernet ASCII interface.

    Plain TCP (see :mod:`devices.api.acme_ls`), so no vendor DLL and the same
    code on every OS. The full driver is exposed through :attr:`api`, and the
    session is safe to share between the task thread and a custom-view sampler.

    Status is never read from the sensor (the fault monitor polls it every
    200 ms). Instead, any socket error on an exchange latches :attr:`faulted`
    until :meth:`clear_fault`, which opens a fresh session.

    Parameters
    ----------
    ip_address : str
        IP address of the sensor.
    port : int
        TCP port (default 9100).
    timeout : float
        Socket timeout in seconds, for connecting and for every reply.
    name : str, optional
        Custom device identifier used for logging; auto-generated if omitted.

    Example
    -------
    >>> sensor = AcmeLaserSensor("192.168.0.20")
    >>> sensor.initialize()
    >>> sensor.api.distance_mm(head=0)
    """

    supports_reconnect = True

    def __init__(self, ip_address: str, port: int = DEFAULT_PORT, timeout: float = 5.0,
                 name: str | None = None):
        if name is None:
            name = f"AcmeLS_{ip_address}"
        super().__init__(device_id=name)

        self._ip_address = ip_address
        self._port = port
        self._open = False
        # Why the device is faulted, or None. A plain attribute so the monitor
        # thread can read it without touching the sensor.
        self._fault: str | None = None
        self._identity: dict = {}
        self._api = _CellAcmeLS(ip_address, port, timeout=timeout, on_comm_error=self._on_comm_error)

    # --- Device interface ---------------------------------------------

    @property
    def api(self) -> AcmeLS:
        """The :class:`~devices.api.acme_ls.AcmeLS` session."""
        return self._api

    @property
    def info(self) -> dict:
        # Cached at connect: /api/info reads this every second from the Flask
        # thread, and must not queue behind the task's exchanges.
        return {"ip_address": self._ip_address, "port": self._port, **self._identity}

    @property
    def connected(self) -> bool:
        return self._open and self._fault is None

    @property
    def ready(self) -> bool:
        return self.connected and not self.faulted

    @property
    def faulted(self) -> bool:
        return self._fault is not None

    def telemetry(self) -> dict:
        # One query, serialised with the task's by the session lock.
        if not self.connected:
            return {}
        return {"distance_mm": round(self._api.distance_mm(0), 4)}

    # --- Lifecycle -----------------------------------------------------

    def initialize(self):
        self._close()
        self._connect()

    def shutdown(self):
        self._close()

    def clear_fault(self):
        if self._fault is None:
            return
        # Reconnect rather than just drop the flag: the API closed the session
        # on the socket error. Nothing moves here.
        self.logger.info(f"[{self.device_id}] Clearing fault ({self._fault}): reopening the session.")
        self._close()
        self._connect()

    def abort(self):
        # Nothing moves, and a query in progress ends within the socket timeout.
        self.logger.warning(f"[{self.device_id}] Abort requested (no operation to stop on a sensor).")

    # --- Internals -----------------------------------------------------

    def _connect(self):
        """Open the session and read the identity. Raises, and leaves the
        device faulted, on any failure."""
        try:
            self._api.open()
            idn = self._api.idn()
        except Exception as exc:
            self._fault = str(exc) or type(exc).__name__
            self._api.close()
            self.logger.error(f"[{self.device_id}] Connection failed: {self._fault}")
            raise
        self._identity = {"model": idn}
        # Open first: in between, the monitor sees "still faulted", not
        # "healthy but disconnected".
        self._open = True
        self._fault = None
        self.logger.info(f"[{self.device_id}] Connected: {idn}")

    def _close(self):
        self._open = False
        self._identity = {}
        try:
            self._api.close()
        except Exception as exc:
            self.logger.warning(f"[{self.device_id}] Error closing the session: {exc}")

    def _on_comm_error(self, exc: Exception):
        # Runs on whichever thread hit the error: the task, or a sampler.
        # While connecting (_open is False), _connect reports the failure.
        if self._open and self._fault is None:
            self._fault = f"Communication error: {exc!r}"
            self.logger.error(f"[{self.device_id}] {self._fault}")
```

Things to keep when adapting it:

- **`_connect()` does the whole startup**: open, verify the hardware is what the config says (a channel exists, a sensor head is attached), apply the configured startup settings, and read the identity. Any failure is a fault with a clear message, such as `"Channel 3 does not exist: the controller has 2 (0-1)."`. "Connection failed: None" helps nobody.
- **Homing/referencing** happens in `initialize()` only (`_connect(home=True)`), never in `clear_fault()` (`_connect(home=False)`). See SmarActMCS2.
- **`__init__` validates** enum-like arguments (`if mode not in MODES: raise ValueError(...)`) and normalizes types (`int` channel to `[int]`). The registry already coerced YAML values, but task code and notebooks construct drivers directly.

## 3. Variant: vendor SDK on PyPI

There's no protocol module. Import the SDK at the top of the driver module: registry.py and devices/__init__.py import drivers lazily, so a missing SDK only breaks configs that use this type. The controller reports it as "could not be created: ModuleNotFoundError".

- Keep the SDK object in `self._api` and return it from `api`.
- Status: prefer the SDK's cached state (as `MecaRobot` reads `GetStatusRobot()` without a synchronous update) over a round trip. If the SDK has only blocking getters, cache them yourself, or poll them in `telemetry()` and not in `faulted`.
- Comm errors: the SDK raises its own exception types. Catch them in the driver's public helpers, or wrap the SDK object, and latch `_fault` the same way.
- Testing: tests monkeypatch the SDK symbol in the driver module (`monkeypatch.setattr(driver_module, "SdkClass", FakeSdk)`), so import it as a module-level name the test can replace: `from vendor_sdk import Client`, then `Client(...)` in the code.

## 4. Variant: remote I/O module

Look at `IoLogikE1212.py` and `BrainboxesED.py`. They add:

- `io_points()`: `di0..diN` (read-only) and `do0..doN` (`writable: True`) with boolean values, or `ai`/`ao` with floats. The `channels` config key decides N when the hardware can't report it.
- `write_io(name, value)`: parse the name, refuse unknown or read-only points with `ValueError` (the controller turns that into a 400), and write through the API.
- `abort()`: only logs. Outputs keep their state. If the device has a "safe state" or watchdog feature, mention it in the docstring, but don't drive outputs on abort unless the user asks: a dropped output can drop a part.

## 5. abort() by device kind

| Device | abort() does |
|---|---|
| Measures only (sensor, meter, camera) | Log a warning. A read in progress ends within the timeout. |
| Moves (stage, axis, robot, conveyor) | 1) Bump the API's abort counter so any `wait()` raises `XAborted` on its next poll. 2) Send stop to every channel in use. 3) Log. Catch and log errors: abort must not raise, and it must work when the link is down (step 1 needs no link). |
| Long operation (homing, a recipe run) | Same as "moves": a counter the blocking call checks, then the device's stop/cancel command. |

The task thread unwinds because the wait raises, and the controller then decides the state. abort() doesn't fault the device itself.

## 6. Optional members

| Member | Add it when |
|---|---|
| `supports_reconnect = True` | Almost always: `initialize()` and `shutdown()` already work in any order. |
| `supports_monitoring = True` + `disconnect(keep_monitoring)` + `link` | Only when the device has a separate read-only channel (the Mecademic monitoring port). Rare. |
| `telemetry()` | A few live values worth seeing on the Devices tab: position, power, temperature, state flags. Two or three short queries at most (it runs about twice a second, on the task's link), one on a slow or rate-limited link, and use `round()` on floats. |
| `io_points()` / `write_io()` | The device has digital or analog I/O. |
| `images()` / `get_image()` | The device produces images. Return a cached frame and never grab one here. |
| `settings()` and the related methods | Cameras get these from `CameraFeatures.FeatureSettingsMixin`. For other devices, skip it unless the user asks for a settings dialog. |
| Public helpers on the class itself | Only for a few convenience calls task code makes constantly, like `BrainboxesED`'s pass-throughs. Otherwise task code uses `.api`. |
