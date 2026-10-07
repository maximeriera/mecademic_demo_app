"""
thorlabs_pm.py - cross-platform interface to Thorlabs PM-series power meters
===========================================================================

Talks SCPI through PyVISA instead of the vendor TLPMX DLL, so the same code
runs on Windows, Linux and macOS.

Backends (chosen by PyVISA, override with ``backend=`` or $PYVISA_LIBRARY):
  * Windows / any machine with a VISA runtime  -> default (IVI / NI-VISA)
  * Linux without VISA                         -> "@py"  (pip install pyvisa-py pyusb)
  * Simulated instrument for testing           -> "definition.yaml@sim" (pip install pyvisa-sim)

Quick start::

    from thorlabs_pm import PowerMeter, list_devices

    print(list_devices())
    with PowerMeter() as pm:          # auto-picks the only Thorlabs meter found
        pm.wavelength = 1064          # nm
        pm.averaging = 10             # ~3 ms per sample on PM100x
        print(pm.read_power())        # W (or dBm if pm.power_unit == "DBM")

Anything not wrapped here is reachable with ``pm.write("...")`` / ``pm.query("...")``.

A session may be shared between threads: one lock serializes every exchange, and
holds across the multi-step ones (a setter and its error-queue check, a zero
adjustment), so no thread can read another's reply or drain another's error.

Not covered (use the vendor DLL / extend via query()): Bluetooth LE devices,
firmware (DFU) updates, and the binary fast-stream / scope / burst modes.
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import pyvisa
from pyvisa import rname

__all__ = [
    "PowerMeter",
    "PowerMeterError",
    "SCPIError",
    "SensorInfo",
    "list_devices",
]

THORLABS_VID = 0x1313

# Product IDs taken from the constants in TLPMX.py (DFU variants = "interface 1 is DFU").
USB_PRODUCT_IDS = {
    0x8070: "PM100D (DFU)",
    0x8071: "PM100A (DFU)",
    0x8072: "PM100USB",
    0x8073: "PM160 (DFU)",
    0x8074: "PM160T (DFU)",
    0x8075: "PM400 (DFU)",
    0x8076: "PM101 (DFU)",
    0x8077: "PM102 (DFU)",
    0x8078: "PM100D",
    0x8079: "PM100A",
    0x807A: "PM103",
    0x807B: "PM160",
    0x807C: "PM160T",
    0x807D: "PM400",
    0x807E: "PM101",
    0x80B0: "PM200",
    0x80B4: "PM6x (DFU)",
    0x80BB: "PM5020",
    0x8099: "PM100D2/D3",
}

# SCPI convention: +/-9.9E37 means +/-infinity (overrange), 9.91E37 means NaN.
_SCPI_INF = 9.9e37
_ERR_RE = re.compile(r'^\s*([+-]?\d+)\s*,\s*"?(.*?)"?\s*$')

_MEASURE_CMDS = {
    "power": "MEAS:POW?",
    "current": "MEAS:CURR:DC?",
    "voltage": "MEAS:VOLT:DC?",
    "energy": "MEAS:ENER?",
    "power_density": "MEAS:PDEN?",
    "energy_density": "MEAS:EDEN?",
    "frequency": "MEAS:FREQ?",
    "temperature": "MEAS:TEMP?",
}


# --------------------------------------------------------------------------- errors
class PowerMeterError(Exception):
    """Communication / usage error."""


class SCPIError(PowerMeterError):
    """The instrument's error queue reported one or more errors."""

    def __init__(self, errors: list[tuple[int, str]]):
        self.errors = errors
        super().__init__("; ".join(f"[{code}] {msg}" for code, msg in errors))


# --------------------------------------------------------------------------- helpers
def _scpi_float(text: str) -> float:
    value = float(text)
    if abs(value) < _SCPI_INF:
        return value
    if abs(value) > 9.905e37:
        return float("nan")
    return float("inf") if value > 0 else float("-inf")


def _fmt(value: float) -> str:
    return format(float(value), ".9g")


def _usb_ids(resource_name: str) -> Optional[tuple[int, int]]:
    """(vendor id, product id) of a USB resource name, whether it spells them
    in hex (NI-VISA: 0x1313) or decimal (pyvisa-py: 4883)."""
    try:
        parsed = rname.parse_resource_name(resource_name)
    except ValueError:
        return None
    if getattr(parsed, "interface_type", "") != "USB":
        return None
    try:
        return int(parsed.manufacturer_id, 0), int(parsed.model_code, 0)
    except (AttributeError, ValueError):
        return None


def _make_rm(backend: Optional[str]) -> pyvisa.ResourceManager:
    return pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()


def list_devices(
    rm: Optional[pyvisa.ResourceManager] = None,
    *,
    backend: Optional[str] = None,
    known_only: bool = True,
) -> list[str]:
    """VISA resource names of attached Thorlabs USB power meters.

    ``known_only`` restricts the result to product IDs listed in USB_PRODUCT_IDS;
    set it to False to see every Thorlabs (VID 0x1313) USB instrument.
    """
    own = rm is None
    if own:
        rm = _make_rm(backend)
    try:
        found = []
        for name in rm.list_resources():
            ids = _usb_ids(name)
            if ids is None or ids[0] != THORLABS_VID:
                continue
            if known_only and ids[1] not in USB_PRODUCT_IDS:
                continue
            found.append(name)
        return found
    finally:
        if own:
            rm.close()


# --------------------------------------------------------------------------- sensor info
@dataclass(frozen=True)
class SensorInfo:
    """Parsed reply of ``SYST:SENS:IDN?`` (name, serial, cal date, type, subtype, flags)."""

    name: str
    serial: str
    calibration_date: str
    type: int
    subtype: int
    flags: int
    raw: str

    _KINDS = {0: "none", 1: "photodiode", 2: "thermopile", 3: "pyroelectric", 4: "4-quadrant thermopile"}

    @classmethod
    def parse(cls, text: str) -> "SensorInfo":
        parts = [p.strip().strip('"') for p in text.split(",")]
        parts += [""] * (6 - len(parts))

        def as_int(s: str) -> int:
            try:
                return int(float(s))
            except ValueError:
                return 0

        return cls(parts[0], parts[1], parts[2], as_int(parts[3]), as_int(parts[4]), as_int(parts[5]), text)

    @property
    def kind(self) -> str:
        return self._KINDS.get(self.type, f"unknown ({self.type})")

    @property
    def connected(self) -> bool:
        return self.type != 0 and "no sensor" not in self.name.lower()

    # Legacy flag bits (PM100x). Newer consoles may report extended flags - see ``flags``.
    @property
    def is_power(self) -> bool:
        return bool(self.flags & 0x0001)

    @property
    def is_energy(self) -> bool:
        return bool(self.flags & 0x0002)

    @property
    def wavelength_settable(self) -> bool:
        return bool(self.flags & 0x0020)


# --------------------------------------------------------------------------- main class
class PowerMeter:
    """A Thorlabs power meter session (context manager).

    Parameters
    ----------
    resource:
        VISA resource name, e.g. ``"USB0::0x1313::0x8078::P0012345::INSTR"`` or
        ``"TCPIP0::192.168.1.50::2000::SOCKET"``. ``None`` auto-detects a single
        attached USB meter.
    backend:
        PyVISA backend string (``"@py"``, ``"@ivi"``, ``"file.yaml@sim"``); ``None``
        lets PyVISA decide (VISA runtime if installed, else pyvisa-py).
    timeout_ms:
        I/O timeout. With large ``averaging`` values a reading takes
        ``averaging * ~3 ms`` - make sure the timeout is longer.
    check_errors:
        After every setter, drain the instrument error queue and raise
        :class:`SCPIError` on failure (costs one extra round trip per set).
    """

    def __init__(
        self,
        resource: Optional[str] = None,
        *,
        backend: Optional[str] = None,
        timeout_ms: int = 5000,
        check_errors: bool = True,
    ):
        self._resource_name = resource
        self._backend = backend
        self._timeout_ms = timeout_ms
        self.check_errors = check_errors
        self._rm: Optional[pyvisa.ResourceManager] = None
        self._inst = None
        self._idn = ""
        # Reentrant: compound operations take it, then call write()/query(),
        # which take it again.
        self._lock = threading.RLock()

    # ---- lifecycle
    @classmethod
    def from_instrument(cls, inst, *, check_errors: bool = True) -> "PowerMeter":
        """Wrap an already-open PyVISA resource (or any object with write/query/close)."""
        pm = cls(check_errors=check_errors)
        pm._inst = inst
        pm._init_session()
        return pm

    def open(self) -> "PowerMeter":
        with self._lock:
            if self._inst is not None:
                return self
            self._rm = _make_rm(self._backend)
            try:
                name = self._resource_name or self._autodetect()
                try:
                    inst = self._rm.open_resource(name)
                except pyvisa.VisaIOError as exc:
                    raise PowerMeterError(f"Cannot open {name!r}: {exc}") from exc
                inst.timeout = self._timeout_ms
                # PyVISA's default write terminator is "\r\n"; the instruments expect a bare
                # "\n" (mandatory for sockets/serial, harmless over USBTMC).
                inst.read_termination = "\n"
                inst.write_termination = "\n"
                self._inst = inst
                self._init_session()
            except Exception:
                self.close()
                raise
            return self

    def close(self) -> None:
        with self._lock:
            if self._inst is not None:
                try:
                    self._inst.close()
                finally:
                    self._inst = None
            if self._rm is not None:
                try:
                    self._rm.close()
                finally:
                    self._rm = None

    def __enter__(self) -> "PowerMeter":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        state = self._idn or ("closed" if self._inst is None else "open")
        return f"<PowerMeter {state}>"

    def _autodetect(self) -> str:
        devices = list_devices(self._rm)
        if not devices:
            raise PowerMeterError(
                "No Thorlabs power meter found. Check the cable, the backend "
                "(Linux needs pyvisa-py + pyusb and a udev rule) or pass resource=..."
            )
        if len(devices) > 1:
            raise PowerMeterError("Several meters found, pass resource=...: " + ", ".join(devices))
        return devices[0]

    def _init_session(self) -> None:
        self.write("*CLS")
        self._idn = self.query("*IDN?")
        if "thorlabs" not in self._idn.lower():
            raise PowerMeterError(f"Not a Thorlabs instrument: {self._idn!r}")

    def _session(self):
        if self._inst is None:
            raise PowerMeterError("Session is closed - call open() or use 'with PowerMeter() as pm'")
        return self._inst

    # ---- raw SCPI access
    def write(self, command: str) -> None:
        with self._lock:
            self._session().write(command)

    def query(self, command: str) -> str:
        with self._lock:
            return self._session().query(command).strip()

    def _set(self, command: str, value) -> None:
        with self._lock:
            self.write(f"{command} {value}")
            if self.check_errors:
                self.raise_on_error()

    # ---- error queue
    def read_error(self) -> tuple[int, str]:
        reply = self.query("SYST:ERR?")
        match = _ERR_RE.match(reply)
        if not match:
            raise PowerMeterError(f"Unparseable error-queue reply: {reply!r}")
        return int(match.group(1)), match.group(2)

    def drain_errors(self, limit: int = 20) -> list[tuple[int, str]]:
        errors = []
        with self._lock:
            for _ in range(limit):
                code, message = self.read_error()
                if code == 0:
                    break
                errors.append((code, message))
        return errors

    def raise_on_error(self) -> None:
        errors = self.drain_errors()
        if errors:
            raise SCPIError(errors)

    def reset(self) -> None:
        """``*RST`` - restore factory settings."""
        with self._lock:
            self.write("*RST")
            if self.check_errors:
                self.raise_on_error()

    # ---- identity
    @property
    def idn(self) -> str:
        return self._idn

    @property
    def sensor(self) -> SensorInfo:
        return SensorInfo.parse(self.query("SYST:SENS:IDN?"))

    # ---- settings
    @property
    def wavelength(self) -> float:
        """Operating wavelength in nm."""
        return float(self.query("SENS:CORR:WAV?"))

    @wavelength.setter
    def wavelength(self, nm: float) -> None:
        self._set("SENS:CORR:WAV", _fmt(nm))

    @property
    def wavelength_limits(self) -> tuple[float, float]:
        return (float(self.query("SENS:CORR:WAV? MIN")), float(self.query("SENS:CORR:WAV? MAX")))

    @property
    def averaging(self) -> int:
        """Number of samples averaged per reading (about 3 ms each on PM100x)."""
        return int(float(self.query("SENS:AVER:COUN?")))

    @averaging.setter
    def averaging(self, count: int) -> None:
        self._set("SENS:AVER:COUN", int(count))

    @property
    def auto_range(self) -> bool:
        return bool(int(float(self.query("SENS:POW:DC:RANG:AUTO?"))))

    @auto_range.setter
    def auto_range(self, enabled: bool) -> None:
        self._set("SENS:POW:DC:RANG:AUTO", int(bool(enabled)))

    @property
    def power_range(self) -> float:
        """Upper end of the power range in W. Setting it normally disables auto-ranging."""
        return float(self.query("SENS:POW:DC:RANG:UPP?"))

    @power_range.setter
    def power_range(self, watts: float) -> None:
        self._set("SENS:POW:DC:RANG:UPP", _fmt(watts))

    @property
    def power_unit(self) -> str:
        """``"W"`` or ``"DBM"``."""
        return self.query("SENS:POW:DC:UNIT?").upper()

    @power_unit.setter
    def power_unit(self, unit: str) -> None:
        unit = unit.upper()
        if unit not in ("W", "DBM"):
            raise ValueError("unit must be 'W' or 'DBM'")
        self._set("SENS:POW:DC:UNIT", unit)

    @property
    def low_bandwidth(self) -> bool:
        """Photodiode sensors: True limits the input bandwidth to suppress noise on CW signals."""
        return bool(int(float(self.query("INP:PDI:FILT:LPAS:STAT?"))))

    @low_bandwidth.setter
    def low_bandwidth(self, enabled: bool) -> None:
        self._set("INP:PDI:FILT:LPAS:STAT", int(bool(enabled)))

    @property
    def accelerator(self) -> bool:
        """Thermopile sensors: response-time accelerator on/off."""
        return bool(int(float(self.query("INP:THER:ACC:STAT?"))))

    @accelerator.setter
    def accelerator(self, enabled: bool) -> None:
        self._set("INP:THER:ACC:STAT", int(bool(enabled)))

    # ---- measurements
    def measure(self, quantity: str = "power") -> float:
        """One reading. ``quantity``: power, current, voltage, energy, power_density,
        energy_density, frequency, temperature. Overrange -> +/-inf, invalid -> nan."""
        try:
            command = _MEASURE_CMDS[quantity]
        except KeyError:
            raise ValueError(f"quantity must be one of {sorted(_MEASURE_CMDS)}") from None
        return _scpi_float(self.query(command))

    def read_power(self) -> float:
        """Power in the current unit (see :attr:`power_unit`)."""
        return self.measure("power")

    def acquire(self, n: int, interval: float = 0.0, quantity: str = "power") -> tuple[list[float], list[float]]:
        """Take ``n`` readings, one every ``interval`` seconds (0 = back to back).

        Returns ``(times, values)``; times are seconds since the first request,
        stamped when each reading arrived.
        """
        times: list[float] = []
        values: list[float] = []
        start = time.perf_counter()
        next_due = start
        for _ in range(n):
            values.append(self.measure(quantity))
            times.append(time.perf_counter() - start)
            if interval > 0:
                next_due += interval
                time.sleep(max(0.0, next_due - time.perf_counter()))
        return times, values

    def zero(self, timeout: float = 30.0, poll: float = 0.2) -> float:
        """Dark/zero adjustment. COVER THE SENSOR first.

        Blocks until finished and returns the zero offset (A for photodiodes, V for
        thermal sensors). Other threads using this session wait until it is done,
        rather than taking readings mid-adjustment.
        """
        with self._lock:
            self.write("SENS:CORR:COLL:ZERO:INIT")
            if self.check_errors:
                self.raise_on_error()
            deadline = time.monotonic() + timeout
            while int(float(self.query("SENS:CORR:COLL:ZERO:STAT?"))) != 0:
                if time.monotonic() > deadline:
                    self.write("SENS:CORR:COLL:ZERO:ABOR")
                    raise PowerMeterError(f"Zero adjustment did not finish within {timeout} s")
                time.sleep(poll)
            return float(self.query("SENS:CORR:COLL:ZERO:MAGN?"))


# --------------------------------------------------------------------------- CLI
def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read a Thorlabs power meter over PyVISA.")
    ap.add_argument("--list", action="store_true", help="list attached meters and exit")
    ap.add_argument("-r", "--resource", help="VISA resource name (default: auto-detect)")
    ap.add_argument("-b", "--backend", help='PyVISA backend, e.g. "@py", "@ivi", "sim.yaml@sim"')
    ap.add_argument("-w", "--wavelength", type=float, help="set wavelength in nm")
    ap.add_argument("-a", "--average", type=int, help="set averaging count")
    ap.add_argument("--zero", action="store_true", help="run dark adjustment first (cover the sensor!)")
    ap.add_argument("-n", "--count", type=int, default=5, help="number of readings")
    ap.add_argument("-i", "--interval", type=float, default=0.2, help="seconds between readings")
    args = ap.parse_args(argv)

    try:
        if args.list:
            for name in list_devices(backend=args.backend):
                pid = (_usb_ids(name) or (0, 0))[1]
                print(f"{name}   {USB_PRODUCT_IDS.get(pid, '')}")
            return 0

        with PowerMeter(args.resource, backend=args.backend) as pm:
            print("Instrument :", pm.idn)
            sensor = pm.sensor
            print("Sensor     :", f"{sensor.name} ({sensor.kind}), S/N {sensor.serial}" if sensor.connected else "none connected")
            if args.wavelength is not None:
                pm.wavelength = args.wavelength
            if args.average is not None:
                pm.averaging = args.average
            if args.zero:
                print(f"Zero offset: {pm.zero():.3e}")
            print(f"Wavelength : {pm.wavelength:g} nm   Averaging: {pm.averaging}   Unit: {pm.power_unit}")
            _, values = pm.acquire(args.count, args.interval)
            for i, v in enumerate(values, 1):
                print(f"{i:3d}  {v:.6e}")
    except (PowerMeterError, pyvisa.VisaIOError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
