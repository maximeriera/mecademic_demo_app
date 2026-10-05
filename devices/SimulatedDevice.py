from .Device import Device

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

    def __getattr__(self, name: str):
        def _call(*args, **kwargs):
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
    """

    def __init__(
        self,
        name: str = "simulated_device",
        connect_delay: float = 0.0,
        fail_to_connect: bool = False,
        fail_rate: float = 0.0,
        info: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(device_id=name)
        self._connect_delay = connect_delay
        self._fail_to_connect = fail_to_connect
        self._fail_rate = fail_rate
        self._info = info or {"model": "SimulatedDevice", "serial_number": "SIM-0000"}
        self._connected = False
        self._faulted = False
        self._api = _FakeApi(self)

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

    def initialize(self) -> None:
        self.logger.info(f"[{self.device_id}] Simulated initialize (delay={self._connect_delay}s)")
        if self._connect_delay:
            time.sleep(self._connect_delay)
        if self._fail_to_connect:
            self._connected = False
            raise ConnectionError(f"Simulated connection failure for '{self.device_id}'")
        self._connected = True
        self._faulted = False

    def shutdown(self) -> None:
        self.logger.info(f"[{self.device_id}] Simulated shutdown")
        self._connected = False

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
