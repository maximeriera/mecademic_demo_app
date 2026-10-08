from __future__ import annotations

from typing import Callable, Optional

from .Device import Device
from .api.smaract_mcs2 import DEFAULT_PORT, MCS2, MCS2Error

REFERENCE_MODES = ("if_needed", "always", "never")


class _CellMCS2(MCS2):
    """:class:`MCS2` that reports communication failures to its device.

    Every exchange goes through :meth:`write` or :meth:`query`, so these two
    overrides see a timeout or a lost link whichever method the task code
    called. Errors from the controller's error queue (a bad parameter, …) are
    usage errors, not link failures, and are not reported.
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


class SmarActMCS2(Device):
    """
    Device wrapper for a SmarAct MCS2 controller and its positioners (linear
    stages), over the controller's Ethernet ASCII interface.

    Plain TCP (see :mod:`devices.api.smaract_mcs2`), so no vendor DLL and the
    same code on every OS. The full driver is exposed through :attr:`api`; one
    device covers every channel of the controller, and the session is safe to
    share between the task thread and a custom-view sampler.

    At Initialize, every channel used must have a sensor (closed-loop moves
    need one) and is referenced according to ``reference``.

    Status is never read from the controller (the fault monitor polls it every
    200 ms). Instead, any socket error on an exchange latches :attr:`faulted`
    until :meth:`clear_fault`, which opens a fresh session without moving
    anything. A failed move is not a device fault: :meth:`api.wait` raises in
    the task, which faults the controller on its own.

    Parameters
    ----------
    ip_address : str
        IP address of the MCS2.
    port : int
        ASCII interface TCP port (default 55551).
    timeout : float
        Socket timeout in seconds, for connecting and for every reply.
    channels : list of int, optional
        Channels this cell uses (they are checked, referenced and stopped).
        Default: every channel of the controller.
    reference : str
        At Initialize: ``"if_needed"`` references only the channels that are
        not referenced yet, ``"always"`` references every time, ``"never"``
        leaves it to task code (``api.reference(ch)``).
    reference_reverse : bool
        Start the reference search in the reverse direction.
    reference_timeout_s : float
        Longest a reference search may take, per channel.
    velocity_mm_s, acceleration_mm_s2 : float, optional
        Motion profile set on every channel at Initialize. Omit to keep the
        controller's current values.
    name : str, optional
        Custom device identifier used for logging; auto-generated if omitted.

    Example
    -------
    >>> stage = SmarActMCS2("192.168.1.200", channels=[0])
    >>> stage.initialize()
    >>> stage.api.move_to_mm(0, 12.5)
    >>> stage.api.wait(0)        # MCS2Aborted on ABORT, MCS2MotionError if the move failed
    >>> stage.api.position_mm(0)
    """

    def __init__(
        self,
        ip_address: str,
        port: int = DEFAULT_PORT,
        timeout: float = 5.0,
        channels: list[int] | int | None = None,
        reference: str = "if_needed",
        reference_reverse: bool = False,
        reference_timeout_s: float = 120.0,
        velocity_mm_s: float | None = None,
        acceleration_mm_s2: float | None = None,
        name: str | None = None,
    ):
        reference = str(reference).lower()
        if reference not in REFERENCE_MODES:
            raise ValueError(f"reference must be one of {', '.join(REFERENCE_MODES)}, not {reference!r}")
        if isinstance(channels, int):
            channels = [channels]

        if name is None:
            name = f"SmarActMCS2_{ip_address}"
        super().__init__(device_id=name)

        self._ip_address = ip_address
        self._port = port
        self._wanted_channels = None if channels is None else [int(ch) for ch in channels]
        self._reference = reference
        self._reference_reverse = bool(reference_reverse)
        self._reference_timeout_s = reference_timeout_s
        self._velocity_mm_s = velocity_mm_s
        self._acceleration_mm_s2 = acceleration_mm_s2

        # Channels in use, resolved at connect.
        self._channels: list[int] = []
        self._open = False
        # Why the device is faulted, or None. A plain attribute so the monitor
        # thread can read it without touching the controller.
        self._fault: str | None = None
        self._identity: dict = {}
        self._api = _CellMCS2(
            ip_address,
            port,
            timeout=timeout,
            on_comm_error=self._on_comm_error,
        )

    # ------------------------------------------------------------------
    # Device interface
    # ------------------------------------------------------------------

    @property
    def api(self) -> MCS2:
        """The :class:`~devices.api.smaract_mcs2.MCS2` session."""
        return self._api

    @property
    def channels(self) -> list[int]:
        """Channels in use, known once connected."""
        return list(self._channels)

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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self):
        self._close()
        self._connect(home=True)

    def shutdown(self):
        if self._open and self._fault is None:
            try:
                self._api.abort(self._channels)
            except Exception as exc:
                self.logger.warning(f"[{self.device_id}] Error stopping channels at shutdown: {exc}")
        self._close()

    def clear_fault(self):
        if self._fault is None:
            return
        # Reconnect rather than just drop the flag: the driver closed the
        # session on the socket error. No referencing here: clearing a fault
        # must not move hardware.
        self.logger.info(f"[{self.device_id}] Clearing fault ({self._fault}): reopening the session.")
        self._close()
        self._connect(home=False)

    def abort(self):
        # Interrupts a wait() in progress even when the link is down, so the
        # task thread unwinds instead of polling a dead socket.
        self.logger.warning(f"[{self.device_id}] Abort requested: stopping channels {self._channels}.")
        try:
            self._api.abort(self._channels)
        except Exception as exc:
            self.logger.error(f"[{self.device_id}] Error during abort: {exc}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _connect(self, home: bool):
        """Open the session, check the channels, apply the motion profile and,
        if ``home``, reference. Raises, and leaves the device faulted, on any
        failure."""
        api = self._api
        try:
            api.open()
            if api.stale_errors:
                self.logger.warning(f"[{self.device_id}] Discarded errors left in the controller queue: {api.stale_errors}")
            idn = api.idn()
            serial = api.serial_number()
            channels = self._resolve_channels(api.n_channels())
            names = {}
            for ch in channels:
                if not api.has_sensor(ch):
                    raise MCS2Error(f"Channel {ch} reports no sensor: closed-loop moves are not possible.")
                names[ch] = api.positioner_name(ch)
                if self._velocity_mm_s is not None:
                    api.set_velocity_mm_s(ch, self._velocity_mm_s)
                if self._acceleration_mm_s2 is not None:
                    api.set_acceleration_mm_s2(ch, self._acceleration_mm_s2)
            for ch in channels:
                self._check_reference(ch, home)
            referenced = {ch: api.is_referenced(ch) for ch in channels}
        except Exception as exc:
            self._fault = str(exc) or type(exc).__name__
            api.close()
            self.logger.error(f"[{self.device_id}] Connection failed: {self._fault}")
            raise

        idn_parts = [part.strip() for part in idn.split(",")]
        self._channels = channels
        self._identity = {
            "model": idn_parts[1] if len(idn_parts) > 1 else idn,
            "serial_number": serial,
            "firmware_version": idn_parts[3] if len(idn_parts) > 3 else None,
            **{
                f"channel_{ch}": f"{names[ch]}, {'referenced' if referenced[ch] else 'not referenced'}"
                for ch in channels
            },
        }
        # Open first: in between, the monitor sees "still faulted", not
        # "healthy but disconnected".
        self._open = True
        self._fault = None
        self.logger.info(
            f"[{self.device_id}] Connected: {idn} | S/N {serial} | channels "
            + ", ".join(f"{ch}={names[ch]}" for ch in channels)
        )

    def _resolve_channels(self, n_channels: int) -> list[int]:
        if self._wanted_channels is None:
            if n_channels == 0:
                raise MCS2Error("The controller reports no channels.")
            return list(range(n_channels))
        bad = [ch for ch in self._wanted_channels if not 0 <= ch < n_channels]
        if bad:
            raise MCS2Error(f"Channel(s) {bad} do not exist: the controller has {n_channels} (0-{n_channels - 1}).")
        return list(self._wanted_channels)

    def _check_reference(self, ch: int, home: bool):
        api = self._api
        referenced = api.is_referenced(ch)
        if home and (self._reference == "always" or (self._reference == "if_needed" and not referenced)):
            self.logger.info(f"[{self.device_id}] Referencing channel {ch}...")
            api.reference(ch, reverse=self._reference_reverse)
            api.wait(ch, timeout=self._reference_timeout_s)
            if not api.is_referenced(ch):
                raise MCS2Error(f"Channel {ch}: the reference search ended without finding the reference mark.")
            self.logger.info(f"[{self.device_id}] Channel {ch} referenced, at {api.position_mm(ch):.6f} mm.")
        elif not referenced:
            if self._reference == "never":
                self.logger.warning(
                    f"[{self.device_id}] Channel {ch} is not referenced: absolute positions are not repeatable."
                )
            else:
                # Only reachable from clear_fault: the controller lost the
                # reference (power cycle?), so absolute moves would go astray.
                raise MCS2Error(f"Channel {ch} is not referenced (controller power-cycled?): run INITIALIZE.")

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
