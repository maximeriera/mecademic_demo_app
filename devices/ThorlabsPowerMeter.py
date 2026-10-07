from __future__ import annotations

from typing import Callable, Optional

import pyvisa

from .Device import Device
from .api.thorlabs_pm import PowerMeter, PowerMeterError


class _CellPowerMeter(PowerMeter):
    """:class:`PowerMeter` that reports communication failures to its device.

    Every exchange goes through :meth:`write` or :meth:`query`, so these two
    overrides see a timeout or a lost link whichever method the task code
    called. SCPI errors (an out-of-range wavelength, …) are usage errors, not
    link failures, and are not reported.
    """

    def __init__(self, *args, on_comm_error: Optional[Callable[[Exception], None]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_comm_error = on_comm_error

    def write(self, command: str) -> None:
        try:
            super().write(command)
        except (pyvisa.VisaIOError, OSError) as exc:
            self._report(exc)
            raise

    def query(self, command: str) -> str:
        try:
            return super().query(command)
        except (pyvisa.VisaIOError, OSError) as exc:
            self._report(exc)
            raise

    def _report(self, exc: Exception) -> None:
        if self._on_comm_error is not None:
            self._on_comm_error(exc)


class ThorlabsPowerMeter(Device):
    """
    Device wrapper for Thorlabs PM-series power meters (PM100A/D/USB, PM101-103,
    PM16x, PM400, PM5020, …).

    Talks SCPI through PyVISA (see :mod:`devices.api.thorlabs_pm`), so it runs
    on Windows with the Thorlabs/NI VISA runtime and on Linux or macOS with the
    pure-Python ``pyvisa-py`` backend — no vendor DLL.

    The full driver is exposed through :attr:`api`. The session is safe to share
    between the task thread and a custom-view sampler: the driver serializes
    every exchange.

    Status is never read from the instrument (the fault monitor polls it every
    200 ms). Instead, any VISA/OS error on an exchange latches :attr:`faulted`
    until :meth:`clear_fault`, which opens a fresh session.

    Parameters
    ----------
    resource : str, optional
        VISA resource name, e.g. ``"USB0::0x1313::0x8078::P0012345::INSTR"``.
        Omit to auto-detect; that fails if more than one meter is attached.
    backend : str, optional
        PyVISA backend (``"@py"``, ``"@ivi"``). Omit to let PyVISA choose: the
        installed VISA runtime, else ``pyvisa-py``.
    timeout_ms : int
        I/O timeout. A reading takes about ``averaging * 3 ms`` on a PM100x,
        so raise this when averaging over more than ~1500 samples.
    wavelength : float, optional
        Wavelength in nm, set at every initialize.
    averaging : int, optional
        Samples averaged per reading, set at every initialize.
    power_unit : str, optional
        ``"W"`` or ``"DBM"``, set at every initialize. The meter keeps its last
        unit across power cycles, so set it if task code assumes one.
    name : str, optional
        Custom device identifier used for logging; auto-generated if omitted.

    Example
    -------
    >>> pm = ThorlabsPowerMeter(wavelength=1064, averaging=10)
    >>> pm.initialize()
    >>> pm.api.read_power()          # W, or dBm if power_unit is "DBM"
    >>> pm.api.wavelength = 532      # every driver setting is on .api
    """

    def __init__(
        self,
        resource: str | None = None,
        backend: str | None = None,
        timeout_ms: int = 5000,
        wavelength: float | None = None,
        averaging: int | None = None,
        power_unit: str | None = None,
        name: str | None = None,
    ):
        if name is None:
            name = f"ThorlabsPM_{resource}" if resource else "ThorlabsPM"
        super().__init__(device_id=name)

        self._resource = resource
        self._wavelength = wavelength
        self._averaging = averaging
        self._power_unit = power_unit

        self._open = False
        # Why the device is faulted, or None. A plain attribute so the monitor
        # thread can read it without touching the instrument.
        self._fault: str | None = None
        self._identity: dict = {}
        self._api = _CellPowerMeter(
            resource,
            backend=backend,
            timeout_ms=timeout_ms,
            on_comm_error=self._on_comm_error,
        )

    # ------------------------------------------------------------------
    # Device interface
    # ------------------------------------------------------------------

    @property
    def api(self) -> PowerMeter:
        """The :class:`~devices.api.thorlabs_pm.PowerMeter` session."""
        return self._api

    @property
    def info(self) -> dict:
        # Cached at initialize: /api/info reads this every second from the
        # Flask thread, and must not queue behind a long reading.
        return {"resource": self._resource or "auto-detect", **self._identity}

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
        self._connect()

    def shutdown(self):
        self._close()

    def clear_fault(self):
        if self._fault is None:
            return
        # Reconnect rather than just drop the flag: after a timeout the meter
        # can still deliver the late reply, which the next query on this
        # session would take as its own answer.
        self.logger.info(f"[{self.device_id}] Clearing fault ({self._fault}): reopening the session.")
        self._close()
        self._connect()

    def abort(self):
        # Nothing moves, and a reading in progress ends within the I/O timeout.
        # Not touching the session here also keeps abort() from blocking the
        # fault monitor behind the session lock.
        self.logger.warning(f"[{self.device_id}] Abort requested (no operation to stop on a power meter).")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _connect(self):
        """Open the session, check a sensor is attached and apply the startup
        settings. Raises, and leaves the device faulted, on any failure."""
        try:
            self._api.open()
            sensor = self._api.sensor
            if not sensor.connected:
                raise PowerMeterError("No sensor head is connected to the power meter console.")
            if self._wavelength is not None:
                self._api.wavelength = self._wavelength
            if self._averaging is not None:
                self._api.averaging = self._averaging
            if self._power_unit is not None:
                self._api.power_unit = self._power_unit
        except Exception as exc:
            self._fault = str(exc) or type(exc).__name__
            self._api.close()
            self.logger.error(f"[{self.device_id}] Connection failed: {self._fault}")
            raise

        idn = [part.strip() for part in self._api.idn.split(",")] + [""] * 4
        self._identity = {
            "model": idn[1],
            "serial_number": idn[2],
            "firmware_version": idn[3],
            "sensor": f"{sensor.name} ({sensor.kind})",
            "sensor_serial_number": sensor.serial,
        }
        # Open first: in between, the monitor sees "still faulted", not
        # "healthy but disconnected".
        self._open = True
        self._fault = None
        self.logger.info(
            f"[{self.device_id}] Connected: {self._api.idn} | sensor {sensor.name} S/N {sensor.serial}"
        )

    def _close(self):
        self._open = False
        self._identity = {}
        try:
            self._api.close()
        except Exception as exc:
            self.logger.warning(f"[{self.device_id}] Error closing the VISA session: {exc}")

    def _on_comm_error(self, exc: Exception):
        # Runs on whichever thread hit the error: the task, or a sampler.
        # While connecting (_open is False), _connect reports the failure.
        if self._open and self._fault is None:
            self._fault = f"Communication error: {exc}"
            self.logger.error(f"[{self.device_id}] {self._fault}")
