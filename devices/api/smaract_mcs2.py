"""
smaract_mcs2.py - SmarAct MCS2 positioner controller over its Ethernet ASCII interface
=====================================================================================

Plain TCP socket, no vendor DLL, so the same code runs on Windows, Linux and macOS.

Command syntax comes from open-source drivers (EPICS motorSmarAct, PSI BEC), not
from SmarAct's Programmer's Guide, which is not public. Every command wrapped here
answered on a real MCS2.

Conventions:
  * TCP port 55551, every command terminated with "\\r\\n"
  * queries end with "?" and return one line; setters return nothing
  * errors are not returned inline: they go to a queue, read with
    :SYST:ERR:COUN? / :SYST:ERR:NEXT?
  * linear positions are integers in picometres (rotary stages use another base
    unit); the ``*_mm`` helpers convert. Channel numbers start at 0.

Quick start::

    from devices.api.smaract_mcs2 import MCS2

    with MCS2("192.168.1.200") as m:
        print(m.idn())
        m.move_to_mm(0, 1.0)         # absolute, closed loop (needs a sensor)
        m.wait(0)
        print(m.position_mm(0))

Anything not wrapped here is reachable with ``m.write("...")`` / ``m.query("...")``.

A session may be shared between threads: one lock serializes every exchange, and
holds across the multi-step ones (a setter and its error-queue check, a move and
its mode/velocity/acceleration), so no thread can read another's reply, drain
another's error or change the move mode under another's move.

After a timeout or any other socket error the session closes itself: the late
reply would otherwise be read as the answer to the next query. Call open() again.
"""
from __future__ import annotations

import argparse
import re
import socket
import sys
import threading
import time
from typing import Iterable, Optional

__all__ = [
    "MCS2",
    "MCS2Error",
    "MCS2CommandError",
    "MCS2MotionError",
    "MCS2Aborted",
    "DEFAULT_PORT",
    "PM_PER_MM",
    "STATE_FLAGS",
    "decode_state",
]

DEFAULT_PORT = 55551

#: Linear base unit is the picometre.
PM_PER_MM = 1_000_000_000

# Bits of :CHAN<n>:STAT? (from the EPICS driver's header)
ACTIVELY_MOVING = 0x0001
CLOSED_LOOP_ACTIVE = 0x0002
CALIBRATING = 0x0004
REFERENCING = 0x0008
MOVE_DELAYED = 0x0010
SENSOR_PRESENT = 0x0020
IS_CALIBRATED = 0x0040
IS_REFERENCED = 0x0080
END_STOP_REACHED = 0x0100
RANGE_LIMIT_REACHED = 0x0200
FOLLOWING_LIMIT_REACHED = 0x0400
MOVEMENT_FAILED = 0x0800
STREAMING = 0x1000
OVERTEMP = 0x4000
REFERENCE_MARK = 0x8000

STATE_FLAGS = {
    ACTIVELY_MOVING: "ACTIVELY_MOVING",
    CLOSED_LOOP_ACTIVE: "CLOSED_LOOP_ACTIVE",
    CALIBRATING: "CALIBRATING",
    REFERENCING: "REFERENCING",
    MOVE_DELAYED: "MOVE_DELAYED",
    SENSOR_PRESENT: "SENSOR_PRESENT",
    IS_CALIBRATED: "IS_CALIBRATED",
    IS_REFERENCED: "IS_REFERENCED",
    END_STOP_REACHED: "END_STOP_REACHED",
    RANGE_LIMIT_REACHED: "RANGE_LIMIT_REACHED",
    FOLLOWING_LIMIT_REACHED: "FOLLOWING_LIMIT_REACHED",
    MOVEMENT_FAILED: "MOVEMENT_FAILED",
    STREAMING: "STREAMING",
    OVERTEMP: "OVERTEMP",
    REFERENCE_MARK: "REFERENCE_MARK",
}

# A channel is busy while any of these is set. ACTIVELY_MOVING alone is not
# enough to wait on: it can drop between the phases of a reference search.
_BUSY = ACTIVELY_MOVING | CALIBRATING | REFERENCING

# :CHAN<n>:MMOD values (comment in the EPICS driver + SDK MoveMode enum)
MOVE_ABSOLUTE = 0
MOVE_RELATIVE = 1
MOVE_STEP = 4          # open loop, relative steps

# :CHAN<n>:REF:OPT bit values (EPICS driver)
REF_START_REVERSE = 0x0001   # named START_DIRECTION in the driver header
REF_REVERSE_DIRECTION = 0x0002
REF_AUTO_ZERO = 0x0004
REF_ABORT_ON_END_STOP = 0x0008

# Error codes seen in the EPICS driver
ERROR_TEXT = {
    0: "No error",
    34: "Invalid channel index",
    259: "No sensor present",
    -101: "Invalid character",
    -103: "Invalid separator",
    -104: "Data type error",
    -108: "Parameter not allowed",
    -109: "Missing parameter",
    -113: "Command does not exist",
    -151: "Invalid string",
    -350: "Queue overflow",
    -363: "Buffer overrun",
}

_ERR_RE = re.compile(r'^\s*([+-]?\d+)\s*(?:,\s*"?(.*?)"?)?\s*$')


# --------------------------------------------------------------------------- errors
class MCS2Error(Exception):
    """Usage or protocol error: closed session, unexpected reply, failed check.

    Socket failures (timeouts, resets) are raised as the original ``OSError``
    instead, so callers can tell a broken link from a refused command.
    """


class MCS2CommandError(MCS2Error):
    """The controller's error queue reported one or more errors."""

    def __init__(self, errors: list[tuple[int, str]], command: Optional[str] = None):
        self.errors, self.command = errors, command
        msg = "; ".join(f"[{code}] {text}" for code, text in errors)
        super().__init__(msg + (f"  (after {command!r})" if command else ""))


class MCS2MotionError(MCS2Error):
    """A movement failed, or was still running when wait() timed out."""


class MCS2Aborted(MCS2Error):
    """wait() was interrupted by abort()."""


# --------------------------------------------------------------------------- helpers
def decode_state(state: int) -> list[str]:
    """Names of the flags set in a :CHAN<n>:STAT? value."""
    return [name for bit, name in STATE_FLAGS.items() if state & bit]


def _to_pm(mm: float) -> int:
    return int(round(float(mm) * PM_PER_MM))


# --------------------------------------------------------------------------- main class
class MCS2:
    """An MCS2 session over TCP (context manager).

    Parameters
    ----------
    host:
        IP address or host name of the controller.
    port:
        ASCII interface TCP port.
    timeout:
        Socket timeout in seconds, for connecting and for every reply.
    check_errors:
        After every setter, drain the error queue and raise
        :class:`MCS2CommandError` on failure (costs one extra round trip per set).
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 5.0,
                 check_errors: bool = True):
        self.host, self.port, self.timeout = host, port, timeout
        self.check_errors = check_errors
        #: Errors found (and discarded) in the queue by the last open().
        self.stale_errors: list[tuple[int, str]] = []
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        # Reentrant: compound operations take it, then call write()/query(),
        # which take it again.
        self._lock = threading.RLock()
        # Bumped by abort(). A wait() that sees it change was interrupted. Only
        # abort() writes it, and any change counts, so no lock is needed.
        self._aborts = 0

    # ---------- connection ----------
    def open(self) -> "MCS2":
        """Connect, then empty the error queue so the first setter does not
        raise an error left by a previous session."""
        with self._lock:
            if self._sock is not None:
                return self
            try:
                sock = socket.create_connection((self.host, self.port), self.timeout)
            except OSError as exc:
                raise MCS2Error(f"Cannot connect to {self.host}:{self.port}: {exc}") from exc
            sock.settimeout(self.timeout)
            self._sock = sock
            self._buf = b""
            try:
                self.stale_errors = self.drain_errors()
            except Exception:
                self.close()
                raise
            return self

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            self._buf = b""
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def __enter__(self) -> "MCS2":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<MCS2 {self.host}:{self.port} {'open' if self._sock else 'closed'}>"

    def _session(self) -> socket.socket:
        if self._sock is None:
            raise MCS2Error("Session is closed - call open() or use 'with MCS2(host) as m'")
        return self._sock

    # ---------- raw access ----------
    def write(self, cmd: str) -> None:
        """Send a command that returns nothing."""
        self._send(cmd)

    def query(self, cmd: str) -> str:
        """Send a command ending in '?' and return the one-line reply."""
        with self._lock:
            self._send(cmd)
            sock = self._session()
            try:
                while b"\n" not in self._buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("controller closed the connection")
                    self._buf += chunk
            except OSError:
                self.close()
                raise
            line, self._buf = self._buf.split(b"\n", 1)
            return line.decode("ascii", errors="replace").strip()

    def _send(self, cmd: str) -> None:
        with self._lock:
            sock = self._session()
            try:
                sock.sendall(cmd.encode("ascii") + b"\r\n")
            except OSError:
                self.close()
                raise

    def _query_int(self, cmd: str) -> int:
        reply = self.query(cmd)
        try:
            return int(float(reply))
        except ValueError:
            raise MCS2Error(f"Unexpected reply to {cmd!r}: {reply!r}") from None

    def _set(self, cmd: str) -> None:
        with self._lock:
            self.write(cmd)
            if self.check_errors:
                self.raise_on_error(cmd)

    # ---------- error queue ----------
    def drain_errors(self) -> list[tuple[int, str]]:
        """Read and empty the error queue: ``[(code, text), ...]``."""
        errs = []
        with self._lock:
            for _ in range(self._query_int(":SYST:ERR:COUN?")):
                reply = self.query(":SYST:ERR:NEXT?")
                match = _ERR_RE.match(reply)
                if not match:
                    raise MCS2Error(f"Unparseable error-queue reply: {reply!r}")
                code = int(match.group(1))
                errs.append((code, match.group(2) or ERROR_TEXT.get(code, "Unknown error")))
        return errs

    def raise_on_error(self, command: Optional[str] = None) -> None:
        errs = self.drain_errors()
        if errs:
            raise MCS2CommandError(errs, command)

    # ---------- device ----------
    def idn(self) -> str:
        return self.query("*IDN?")

    def serial_number(self) -> str:
        return self.query(":DEV:SNUM?")

    def n_channels(self) -> int:
        return self._query_int(":DEV:NOCH?")

    # ---------- channel status ----------
    def state(self, ch: int) -> int:
        return self._query_int(f":CHAN{ch}:STAT?")

    def state_flags(self, ch: int) -> list[str]:
        return decode_state(self.state(ch))

    def is_moving(self, ch: int) -> bool:
        return bool(self.state(ch) & ACTIVELY_MOVING)

    def is_busy(self, ch: int) -> bool:
        """Moving, calibrating or referencing."""
        return bool(self.state(ch) & _BUSY)

    def has_sensor(self, ch: int) -> bool:
        return bool(self.state(ch) & SENSOR_PRESENT)

    def is_referenced(self, ch: int) -> bool:
        return bool(self.state(ch) & IS_REFERENCED)

    def is_calibrated(self, ch: int) -> bool:
        return bool(self.state(ch) & IS_CALIBRATED)

    def position(self, ch: int) -> int:
        """Current position, picometres. Needs a sensor."""
        return self._query_int(f":CHAN{ch}:POS?")

    def target(self, ch: int) -> int:
        """Target of the last move, picometres."""
        return self._query_int(f":CHAN{ch}:POS:TARG?")

    def position_mm(self, ch: int) -> float:
        return self.position(ch) / PM_PER_MM

    def target_mm(self, ch: int) -> float:
        return self.target(ch) / PM_PER_MM

    def positioner_name(self, ch: int) -> str:
        return self.query(f":CHAN{ch}:PTYP:NAME?")

    # ---------- motion ----------
    def set_velocity(self, ch: int, pm_per_s: int) -> None:
        self._set(f":CHAN{ch}:VEL {int(pm_per_s)}")

    def set_acceleration(self, ch: int, pm_per_s2: int) -> None:
        self._set(f":CHAN{ch}:ACC {int(pm_per_s2)}")

    def set_velocity_mm_s(self, ch: int, mm_per_s: float) -> None:
        self.set_velocity(ch, _to_pm(mm_per_s))

    def set_acceleration_mm_s2(self, ch: int, mm_per_s2: float) -> None:
        self.set_acceleration(ch, _to_pm(mm_per_s2))

    def move_to(self, ch: int, pos_pm: int, velocity: Optional[int] = None,
                acceleration: Optional[int] = None) -> None:
        """Start an absolute closed-loop move (needs a sensor). Returns at once:
        call :meth:`wait` to block until it ends."""
        self._move(ch, MOVE_ABSOLUTE, pos_pm, velocity, acceleration)

    def move_by(self, ch: int, delta_pm: int, velocity: Optional[int] = None,
                acceleration: Optional[int] = None) -> None:
        """Start a relative closed-loop move. Returns at once: see :meth:`wait`."""
        self._move(ch, MOVE_RELATIVE, delta_pm, velocity, acceleration)

    def move_to_mm(self, ch: int, mm: float, velocity_mm_s: Optional[float] = None,
                   acceleration_mm_s2: Optional[float] = None) -> None:
        """:meth:`move_to` in millimetres (mm/s, mm/s² for the optional profile)."""
        self.move_to(
            ch,
            _to_pm(mm),
            None if velocity_mm_s is None else _to_pm(velocity_mm_s),
            None if acceleration_mm_s2 is None else _to_pm(acceleration_mm_s2),
        )

    def move_by_mm(self, ch: int, mm: float, velocity_mm_s: Optional[float] = None,
                   acceleration_mm_s2: Optional[float] = None) -> None:
        """:meth:`move_by` in millimetres (mm/s, mm/s² for the optional profile)."""
        self.move_by(
            ch,
            _to_pm(mm),
            None if velocity_mm_s is None else _to_pm(velocity_mm_s),
            None if acceleration_mm_s2 is None else _to_pm(acceleration_mm_s2),
        )

    def _move(self, ch, mode, value, velocity, acceleration) -> None:
        # One lock for the whole sequence: another thread's move must not change
        # the mode between this MMOD and this MOVE.
        with self._lock:
            self._set(f":CHAN{ch}:MMOD {mode}")
            if velocity is not None:
                self.set_velocity(ch, velocity)
            if acceleration is not None:
                self.set_acceleration(ch, acceleration)
            self._set(f":MOVE{ch} {int(value)}")

    def stop(self, ch: int) -> None:
        self._set(f":STOP{ch}")

    def wait(self, ch: int, timeout: float = 60.0, poll: float = 0.05) -> None:
        """Block until channel ``ch`` is no longer moving, calibrating or referencing.

        Raises
        ------
        MCS2Aborted
            :meth:`abort` was called (from any thread) while waiting.
        MCS2MotionError
            The movement failed, or it was still running after ``timeout``
            seconds; the channel is stopped first.
        """
        aborts = self._aborts
        deadline = time.monotonic() + timeout
        while True:
            state = self.state(ch)
            # Checked after reading the state, and once more when motion has
            # ended: a move halted by abort() must not look like a success.
            if self._aborts != aborts:
                raise MCS2Aborted(f"Channel {ch}: wait interrupted by abort()")
            if not state & _BUSY:
                break
            if time.monotonic() > deadline:
                self.stop(ch)
                raise MCS2MotionError(f"Channel {ch} still busy after {timeout} s: stopped it")
            time.sleep(poll)
        if state & MOVEMENT_FAILED:
            raise MCS2MotionError(f"Channel {ch}: movement failed ({', '.join(decode_state(state))})")

    def abort(self, channels: Iterable[int]) -> None:
        """Interrupt every :meth:`wait` in progress, then stop ``channels``.

        A wait() started afterwards is not affected. If the session is closed,
        only the waits are interrupted. Every channel is tried; the first error
        is raised afterwards.
        """
        self._aborts += 1
        if self._sock is None:
            return
        first_error = None
        for ch in channels:
            try:
                self.stop(ch)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    # ---------- calibration / referencing ----------
    def calibrate(self, ch: int) -> None:
        """Start the sensor calibration. Returns at once: see :meth:`wait`."""
        self._set(f":CAL{ch}")

    def reference(self, ch: int, reverse: bool = False, auto_zero: bool = True) -> None:
        """Start a reference-mark search. Returns at once: see :meth:`wait`.

        ``auto_zero`` sets the position to 0 at the reference mark.
        """
        opt = (REF_START_REVERSE if reverse else 0) | (REF_AUTO_ZERO if auto_zero else 0)
        with self._lock:
            self._set(f":CHAN{ch}:REF:OPT {opt}")
            self._set(f":REF{ch}")


# --------------------------------------------------------------------------- CLI
def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read (and optionally move) one SmarAct MCS2 channel.")
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("-c", "--channel", type=int, default=0)
    ap.add_argument("--move-um", type=float, help="relative move in micrometres")
    args = ap.parse_args(argv)

    ch = args.channel
    try:
        with MCS2(args.host, args.port) as m:
            print("IDN      :", m.idn())
            print("channels :", m.n_channels())
            print("state    :", ", ".join(m.state_flags(ch)) or "-")
            if not m.has_sensor(ch):
                print("position : n/a (no sensor)")
                return 0
            print(f"position : {m.position_mm(ch):.6f} mm")
            if args.move_um is not None:
                m.move_by_mm(ch, args.move_um / 1000)
                m.wait(ch)
                print(f"now at   : {m.position_mm(ch):.6f} mm")
    except (MCS2Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
