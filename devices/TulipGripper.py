from __future__ import annotations

from typing import Callable, Optional

from .Device import Device
from .api.tulip_gripper import (
    DEFAULT_BAUDRATE,
    DEFAULT_PORT,
    ERROR_TEXT,
    TulipDeviceError,
    TulipDriver,
    TulipReplyError,
)


class _CellTulip(TulipDriver):
    """:class:`TulipDriver` that reports problems to its device.

    Every exchange goes through :meth:`write` or :meth:`query`, so these
    overrides see a lost link (``OSError``) or a session out of step
    (:class:`TulipReplyError`) whichever method the task code called. Every
    error read goes through :meth:`error` / :meth:`clear_error`, so a non-zero
    code the driver reports is seen too, whoever asked. A value refused before
    sending (``ValueError``) is a usage error and is not reported.
    """

    def __init__(self, *args, on_comm_error: Optional[Callable[[Exception], None]] = None,
                 on_device_error: Optional[Callable[[int, str], None]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_comm_error = on_comm_error
        self._on_device_error = on_device_error

    def write(self, command: str) -> None:
        try:
            super().write(command)
        except OSError as exc:
            self._report(exc)
            raise

    def query(self, command: str, expect: Optional[str] = None) -> str:
        try:
            return super().query(command, expect)
        except (OSError, TulipReplyError) as exc:
            self._report(exc)
            raise

    def error(self) -> tuple[int, str]:
        code, text = super().error()
        if code and self._on_device_error is not None:
            self._on_device_error(code, text)
        return code, text

    def _report(self, exc: Exception) -> None:
        if self._on_comm_error is not None:
            self._on_comm_error(exc)


class TulipGripper(Device):
    """
    Device wrapper for a Percipio Robotics Tulip gripper and its driver box,
    over the driver's text protocol (v2.0), on Ethernet or USB.

    Plain TCP, or pyserial for USB (see :mod:`devices.api.tulip_gripper`), so
    no vendor DLL and the same code on every OS. The full driver is exposed
    through :attr:`api`; the session is safe to share between the task thread
    and the Live panel. The driver box accepts one client at a time, so close
    Percipio's AP2M before Initialize.

    Status is never read from the driver (the fault monitor polls it every
    200 ms). Instead, the device faults when an exchange loses the link, and
    when the driver reports an error code (GETERROR: overtemperature, power,
    output, internal) on any read of it: at Initialize, after every move that
    waits, and in the Live panel. :meth:`clear_fault` reopens the session and
    sends CLEARERROR; it never moves the gripper.

    Openings are in % (0 = fully closed, 100 = fully open).

    Parameters
    ----------
    ip_address : str, optional
        IP address of the driver box (factory default 200.0.0.100).
    port : int
        TCP port (default 2000).
    serial_port : str, optional
        Serial port of the driver box over USB (``"COM4"``, ``"/dev/ttyACM0"``),
        instead of ``ip_address``.
    baudrate : int
        Serial baud rate (default 115200).
    timeout : float
        Seconds, for connecting and for every reply.
    travel_time_s : float, optional
        Duration of a full opening or closing, applied at Initialize when the
        driver's differs (the driver saves it to flash). Omit to keep the
        driver's motion law.
    open_position, close_position : float
        Openings used by :meth:`release` and :meth:`grip` (default 100 and 0).
    name : str, optional
        Custom device identifier used for logging; auto-generated if omitted.

    Example
    -------
    >>> gripper = TulipGripper("200.0.0.100", close_position=20)
    >>> gripper.initialize()
    >>> gripper.release()                 # open, wait until settled
    >>> gripper.grip()                    # close to 20 %
    >>> gripper.api.move_to(55.5)         # any opening; TulipAborted on ABORT
    >>> gripper.api.state().position
    """

    supports_reconnect = True

    def __init__(
        self,
        ip_address: str | None = None,
        port: int = DEFAULT_PORT,
        serial_port: str | None = None,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = 2.0,
        travel_time_s: float | None = None,
        open_position: float = 100.0,
        close_position: float = 0.0,
        name: str | None = None,
    ):
        if (ip_address is None) == (serial_port is None):
            raise ValueError("TulipGripper needs either ip_address (Ethernet) or serial_port (USB), not both.")
        for key, value in (("open_position", open_position), ("close_position", close_position)):
            if not 0 <= float(value) <= 100:
                raise ValueError(f"{key} must be between 0 and 100 %, got {value}")
        if travel_time_s is not None and not float(travel_time_s) > 0:
            raise ValueError(f"travel_time_s must be > 0, got {travel_time_s}")

        if name is None:
            name = f"TulipGripper_{ip_address or serial_port}"
        super().__init__(device_id=name)

        self._ip_address = ip_address
        self._port = int(port)
        self._serial_port = serial_port
        self._travel_time_s = None if travel_time_s is None else float(travel_time_s)
        self.open_position = float(open_position)
        self.close_position = float(close_position)

        self._open = False
        # Why the device is faulted, or None. A plain attribute so the monitor
        # thread can read it without touching the driver box.
        self._fault: str | None = None
        self._identity: dict = {}
        self._api = _CellTulip(
            ip_address,
            port,
            serial_port=serial_port,
            baudrate=baudrate,
            timeout=timeout,
            on_comm_error=self._on_comm_error,
            on_device_error=self._on_device_error,
        )

    # ------------------------------------------------------------------
    # Device interface
    # ------------------------------------------------------------------

    @property
    def api(self) -> TulipDriver:
        """The :class:`~devices.api.tulip_gripper.TulipDriver` session."""
        return self._api

    @property
    def info(self) -> dict:
        # Cached at connect: /api/info reads this every second from the Flask
        # thread, and must not queue behind the task's exchanges.
        link = ({"ip_address": self._ip_address, "port": self._port} if self._serial_port is None
                else {"serial_port": self._serial_port})
        return {**link, **self._identity}

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
        # Two queries, serialised with the task's by the session lock. Reading
        # the error here also latches a fault the driver reports while idle.
        if not self.connected:
            return {}
        st = self._api.state()
        code, text = self._api.error()
        return {
            "opening_pct": round(st.position, 2),
            "speed_pct_s": round(st.speed, 2),
            "travel_time_s": round(st.travel_time, 4),
            "error": f"{code}: {text}",
        }

    def io_points(self) -> list:
        # One toggle on the Live panel: open (release) / closed (grip).
        if not self.connected:
            return []
        position = self._api.position()
        midpoint = (self.open_position + self.close_position) / 2
        is_open = position >= midpoint if self.open_position >= self.close_position else position <= midpoint
        return [{"name": "open", "kind": "do", "value": is_open, "writable": True}]

    def write_io(self, name: str, value) -> None:
        if name != "open":
            raise ValueError(f"{self.device_id} has no I/O point {name!r} (only 'open').")
        # No wait: the UI request returns at once, and the next poll shows the move.
        self._api.set_position(self.open_position if value else self.close_position)

    # ------------------------------------------------------------------
    # Task helpers
    # ------------------------------------------------------------------

    def grip(self, wait: bool = True, timeout: float = 5.0) -> None:
        """Close to :attr:`close_position` and, if ``wait``, block until settled."""
        self._api.move_to(self.close_position, wait=wait, timeout=timeout)

    def release(self, wait: bool = True, timeout: float = 5.0) -> None:
        """Open to :attr:`open_position` and, if ``wait``, block until settled."""
        self._api.move_to(self.open_position, wait=wait, timeout=timeout)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self):
        self._close()
        self._connect()

    def shutdown(self):
        if self._open and self._fault is None:
            try:
                self._api.stop()  # hold the current opening: never drop a part
            except Exception as exc:
                self.logger.warning(f"[{self.device_id}] Error stopping the gripper at shutdown: {exc}")
        self._close()

    def clear_fault(self):
        if self._fault is None:
            return
        # Reconnect rather than just drop the flag: the API closed the session
        # on a link error. Then CLEARERROR if the driver reports an error. No
        # SETPOS/SETSPEED here: clearing a fault must not move the gripper.
        self.logger.info(f"[{self.device_id}] Clearing fault ({self._fault}): reopening the session.")
        self._close()
        self._connect(clear_errors=True)

    def abort(self):
        # Interrupts a wait() in progress even when the link is down, so the
        # task thread unwinds instead of polling a dead link; then holds the
        # current opening (SETSPEED:0), so a gripped part is not dropped.
        self.logger.warning(f"[{self.device_id}] Abort requested: holding the current opening.")
        try:
            self._api.abort()
        except Exception as exc:
            self.logger.error(f"[{self.device_id}] Error during abort: {exc}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _connect(self, clear_errors: bool = False):
        """Open the session, check the driver's error state (clearing it if
        ``clear_errors``), apply the travel time and read the identity. Raises,
        and leaves the device faulted, on any failure."""
        api = self._api
        try:
            api.open()
            version = api.version()
            code, text = api.error()
            if code and clear_errors:
                self.logger.info(f"[{self.device_id}] Driver reports error {code} ({text}): sending CLEARERROR.")
                code, text = api.clear_error()
            if code:
                raise TulipDeviceError(
                    code, "fix the cause, then Clear faults" if not clear_errors else "still present after CLEARERROR")
            state = api.state()
            if self._travel_time_s is not None and not _close_enough(state.travel_time, self._travel_time_s):
                self.logger.info(
                    f"[{self.device_id}] Travel time {state.travel_time:g} s -> {self._travel_time_s:g} s "
                    "(saved in the driver's flash).")
                api.set_travel_time(self._travel_time_s)
                state = api.state()
                if not _close_enough(state.travel_time, self._travel_time_s, rel=0.05):
                    self.logger.warning(
                        f"[{self.device_id}] Driver reports a travel time of {state.travel_time:g} s "
                        f"after asking for {self._travel_time_s:g} s.")
        except Exception as exc:
            self._fault = str(exc) or type(exc).__name__
            api.close()
            self.logger.error(f"[{self.device_id}] Connection failed: {self._fault}")
            raise

        self._identity = {
            "model": "Percipio Tulip gripper",
            "firmware_version": version,
            "travel_time_s": state.travel_time,
        }
        # Open first: in between, the monitor sees "still faulted", not
        # "healthy but disconnected".
        self._open = True
        self._fault = None
        self.logger.info(
            f"[{self.device_id}] Connected to {api.address}: firmware {version}, "
            f"opening {state.position:g} %, travel time {state.travel_time:g} s")

    def _close(self):
        self._open = False
        self._identity = {}
        try:
            self._api.close()
        except Exception as exc:
            self.logger.warning(f"[{self.device_id}] Error closing the session: {exc}")

    def _on_comm_error(self, exc: Exception):
        # Runs on whichever thread hit the error: the task, or the Live panel.
        # While connecting (_open is False), _connect reports the failure.
        if self._open and self._fault is None:
            self._fault = f"Communication error: {exc!r}"
            self.logger.error(f"[{self.device_id}] {self._fault}")

    def _on_device_error(self, code: int, text: str):
        if self._open and self._fault is None:
            self._fault = f"Gripper error {code}: {ERROR_TEXT.get(code, text)}"
            self.logger.error(f"[{self.device_id}] {self._fault}")


def _close_enough(actual: float, wanted: float, rel: float = 0.01) -> bool:
    return abs(actual - wanted) <= max(1e-4, rel * abs(wanted))
