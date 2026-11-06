from .Device import Device

import math
import random
import time
from typing import Any, Dict, Optional


class _FakeApi:
    """Stands in for a real SDK object (``mdr()``, Modbus client, …).

    Any attribute access returns a callable that logs the call and returns
    ``None``, so app_logic code written against a real driver — e.g.
    ``robot.api.MoveJoints(0, 0, 0, 0, 0, 0)`` then ``robot.api.WaitIdle()`` —
    runs unmodified against a SimulatedDevice.
    """

    def __init__(self, device: "SimulatedDevice"):
        self._device = device
        self.calls = 0
        self.last_call: Optional[str] = None

    def __getattr__(self, name: str):
        def _call(*args, **kwargs):
            self.calls += 1
            self.last_call = name
            self._device.logger.info(
                f"[{self._device.device_id}] api.{name}(args={args}, kwargs={kwargs}) -> simulated no-op"
            )
            return None
        return _call


class SimulatedDevice(Device):
    """Hardware-free stand-in for any real :class:`~devices.Device.Device`.

    Declare it in ``config.yaml`` with ``type: "simulated"`` to exercise the
    Flask routes, task flow, and controller state machine with no robot,
    feeder, or I/O hardware connected.

    Parameters
    ----------
    name : str
        Device id / logger name.
    connect_delay : float
        Seconds :meth:`initialize` sleeps for, to mimic real connection latency.
    fail_to_connect : bool
        If True, :meth:`initialize` raises ``ConnectionError`` — for testing
        the controller's FAULTED-on-init-failure path.
    fail_rate : float
        Probability in [0, 1] that :attr:`faulted` flips True on any given
        check, for exercising fault-handling and ``clear_fault`` recovery.
    info : dict, optional
        Static info dict returned by :attr:`info`. Defaults to a generic
        placeholder.
    supports_monitoring : bool
        If True, :meth:`disconnect` can keep a read-only link, like a
        Mecademic robot released to another client.
    """

    supports_reconnect = True

    def __init__(
        self,
        name: str = "simulated_device",
        connect_delay: float = 0.0,
        fail_to_connect: bool = False,
        fail_rate: float = 0.0,
        info: Optional[Dict[str, Any]] = None,
        supports_monitoring: bool = False,
    ):
        super().__init__(device_id=name)
        self._connect_delay = connect_delay
        self._fail_to_connect = fail_to_connect
        self._fail_rate = fail_rate
        self._info = info or {"model": "SimulatedDevice", "serial_number": "SIM-0000"}
        self._connected = False
        self._monitoring = False
        self._faulted = False
        self._api = _FakeApi(self)
        self.supports_monitoring = bool(supports_monitoring)
        self._started = time.monotonic()
        # Fake I/O for the Devices tab: 4 outputs kept in memory, and inputs
        # that move on their own (di1 blinks, di2 loops back do1).
        self._outputs = [False] * 4

    @property
    def info(self) -> dict:
        return dict(self._info)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def ready(self) -> bool:
        return self._connected and not self._faulted

    @property
    def faulted(self) -> bool:
        if self._fail_rate and random.random() < self._fail_rate:
            self._faulted = True
        return self._faulted

    @property
    def api(self) -> Any:
        return self._api

    @property
    def link(self) -> str:
        if self._connected:
            return "control"
        return "monitoring" if self._monitoring else "disconnected"

    def initialize(self) -> None:
        self.logger.info(f"[{self.device_id}] Simulated initialize (delay={self._connect_delay}s)")
        if self._connect_delay:
            time.sleep(self._connect_delay)
        if self._fail_to_connect:
            self._connected = False
            raise ConnectionError(f"Simulated connection failure for '{self.device_id}'")
        self._connected = True
        self._monitoring = False
        self._faulted = False

    def shutdown(self) -> None:
        self.logger.info(f"[{self.device_id}] Simulated shutdown")
        self._connected = False
        self._monitoring = False

    def disconnect(self, keep_monitoring: bool = False) -> None:
        self.shutdown()
        self._monitoring = keep_monitoring and self.supports_monitoring

    def telemetry(self) -> dict:
        uptime = time.monotonic() - self._started
        return {
            "link": self.link,
            "api_calls": self._api.calls,
            "last_api_call": self._api.last_call,
            "signal": round(math.sin(uptime / 2.0), 3),
        }

    def io_points(self) -> list:
        blink = int(time.monotonic() - self._started) % 2 == 0
        inputs = [blink, self._outputs[0], False, False]
        return ([{"name": f"di{i + 1}", "kind": "di", "value": v, "writable": False}
                 for i, v in enumerate(inputs)]
                + [{"name": f"do{i + 1}", "kind": "do", "value": v, "writable": True}
                   for i, v in enumerate(self._outputs)])

    def write_io(self, name: str, value) -> None:
        if not name.startswith("do") or not name[2:].isdigit() or not 1 <= int(name[2:]) <= len(self._outputs):
            raise ValueError(f"Unknown or read-only point '{name}'.")
        if not self._connected:
            raise RuntimeError(f"{self.device_id} is not connected.")
        self._outputs[int(name[2:]) - 1] = bool(value)
        self.logger.info(f"[{self.device_id}] Simulated {name} = {bool(value)}")

    def clear_fault(self) -> None:
        self.logger.info(f"[{self.device_id}] Simulated clear_fault")
        self._faulted = False

    def abort(self) -> None:
        self.logger.warning(f"[{self.device_id}] Simulated abort: clearing motion queue.")


def example_usage():
    dev = SimulatedDevice(name="sim_1", connect_delay=0.1)
    dev.initialize()
    print(f"Connected: {dev.connected}, Ready: {dev.ready}, Faulted: {dev.faulted}")
    dev.api.MoveJoints(0, 0, 0, 0, 0, 0)
    dev.api.WaitIdle()
    dev.shutdown()


if __name__ == "__main__":
    example_usage()
