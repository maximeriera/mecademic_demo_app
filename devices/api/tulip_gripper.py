"""
tulip_gripper.py - Percipio Robotics Tulip gripper driver box, text protocol v2.0
================================================================================

Plain TCP socket (Ethernet), or pyserial (USB-C, virtual COM port). No vendor DLL,
so the same code runs on Windows, Linux and macOS.

Command syntax: "Tulip Gripper Driver - Communication protocol (V2.0)", Percipio
Robotics, 06/09/2024, cross-checked with Percipio's example script
(tulipGripper.py). Every command wrapped here is documented there, but none has
been run against a real driver from this module yet. Behaviour the manual does
not describe is marked "# inferred" next to the code.

Conventions:
  * TCP port 2000 (factory IP 200.0.0.100), or serial at 115200 baud
  * commands are ``INSTRUCTION:ARG1:ARG2\\n``; the driver never talks unprompted
  * queries are answered with the instruction echoed, then the values:
    ``GETSTATE`` -> ``GETSTATE:POS:SPEED:SPEEDMAX:ACCMAX:JERKMAX:TRAVELTIME``
  * setters (SETPOS, SETSPEED, SETMOTIONMAX, SETTRAVELTIME, SETIP) get no reply
  * the manual does not say what an invalid command returns, so arguments are
    checked here, before sending
  * errors are read with GETERROR (codes 0-5, see ERROR_TEXT), cleared with
    CLEARERROR, which answers ``GETERROR:X``
  * opening is in % (0 = fully closed, 100 = fully open), speed in %/s,
    acceleration in %/s^2, jerk in %/s^3, travel time in s
  * SETTRAVELTIME and SETMOTIONMAX write the driver's flash memory

Quick start::

    from devices.api.tulip_gripper import TulipDriver

    with TulipDriver("200.0.0.100") as g:          # or TulipDriver(serial_port="COM4")
        print(g.version(), g.error())
        g.move_to(100)                             # open fully, wait until settled
        g.move_to(20)
        print(g.state())

Anything not wrapped here is reachable with ``g.write("...")`` / ``g.query("...")``.

A session may be shared between threads: one lock serializes every exchange.
After a timeout or any other link error the session closes itself: the late
reply would otherwise be read as the answer to the next query. Call open() again.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional

__all__ = [
    "TulipDriver",
    "TulipState",
    "TulipError",
    "TulipReplyError",
    "TulipDeviceError",
    "TulipMotionError",
    "TulipAborted",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_BAUDRATE",
    "ERROR_TEXT",
]

DEFAULT_HOST = "200.0.0.100"
DEFAULT_PORT = 2000
DEFAULT_BAUDRATE = 115200

#: Seconds to wait after opening the serial port before the first command.
#: The manual says an "electronic start delay is mandatory"; an Arduino-like
#: board may also reboot when the port is opened (DTR). # inferred
SERIAL_STARTUP_DELAY_S = 2.0

#: GETERROR codes (manual, table 2).
ERROR_TEXT = {
    0: "No error",
    1: "Undefined error",
    2: "Overtemperature of the driving module",
    3: "Power supply problem (12 V unplugged or undervoltage)",
    4: "Output problem (output short-circuited)",
    5: "Internal error of the driver",
}


# --------------------------------------------------------------------------- errors
class TulipError(Exception):
    """Usage or protocol error: closed session, unexpected reply, failed check.

    Link failures (timeouts, resets, a serial port that vanished) are raised as
    the original ``OSError`` instead, so callers can tell a broken link from a
    refused value.
    """


class TulipReplyError(TulipError):
    """The reply did not echo the command sent. The session was out of step,
    so it has been closed: call open() again."""


class TulipDeviceError(TulipError):
    """The driver reports an error code (GETERROR)."""

    def __init__(self, code: int, context: str = ""):
        self.code = code
        text = ERROR_TEXT.get(code, "Unknown error")
        super().__init__(f"Gripper error {code}: {text}" + (f" ({context})" if context else ""))


class TulipMotionError(TulipError):
    """A move did not settle on its target in time."""


class TulipAborted(TulipError):
    """wait() was interrupted by abort()."""


# --------------------------------------------------------------------------- helpers
@dataclass(frozen=True)
class TulipState:
    """One GETSTATE reply."""

    position: float      # current opening, %
    speed: float         # current opening speed, %/s (> 0 opening, < 0 closing)
    speed_max: float     # motion law limit, %/s
    accel_max: float     # motion law limit, %/s^2
    jerk_max: float      # motion law limit, %/s^3
    travel_time: float   # duration of a full opening or closing, s

    @property
    def moving(self) -> bool:
        return abs(self.speed) > 1e-9

    def as_dict(self) -> dict:
        return asdict(self)


def _num(value: float) -> str:
    """Format a number for the protocol: no exponent, no trailing zeros."""
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _check_range(name: str, value: float, low: float, high: float) -> float:
    value = float(value)
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {_num(low)} and {_num(high)}, got {value}")
    return value


def _check_positive(name: str, value: float) -> float:
    value = float(value)
    if not value > 0:
        raise ValueError(f"{name} must be > 0, got {value}")
    return value


# --------------------------------------------------------------------------- main class
class TulipDriver:
    """A Tulip gripper driver session over TCP or serial (context manager).

    Parameters
    ----------
    host:
        IP address of the driver (TCP). Mutually exclusive with ``serial_port``.
    port:
        TCP port.
    serial_port:
        Serial port of the driver over USB (``"COM4"``, ``"/dev/ttyACM0"``).
    baudrate:
        Serial baud rate.
    timeout:
        Seconds, for connecting and for every reply.
    serial_startup_delay:
        Seconds to wait after opening the serial port.
    """

    def __init__(self, host: Optional[str] = None, port: int = DEFAULT_PORT, *,
                 serial_port: Optional[str] = None, baudrate: int = DEFAULT_BAUDRATE,
                 timeout: float = 2.0, serial_startup_delay: float = SERIAL_STARTUP_DELAY_S):
        if (host is None) == (serial_port is None):
            raise ValueError("Give either host (TCP) or serial_port (USB), not both or neither.")
        self.host, self.port = host, int(port)
        self.serial_port, self.baudrate = serial_port, int(baudrate)
        self.timeout = float(timeout)
        self.serial_startup_delay = float(serial_startup_delay)
        self._sock: Optional[socket.socket] = None
        self._ser = None  # serial.Serial when open over USB
        self._buf = b""
        # Target of the last SETPOS/SETSPEED, for wait(). None: unknown.
        self._target: Optional[float] = None
        # Reentrant: compound operations take it, then call write()/query(),
        # which take it again.
        self._lock = threading.RLock()
        # Bumped by abort(). A wait() that sees it change was interrupted. Only
        # abort() writes it, and any change counts, so no lock is needed.
        self._aborts = 0

    # ---------- connection ----------
    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}" if self.serial_port is None else self.serial_port

    def open(self) -> "TulipDriver":
        with self._lock:
            if self.is_open:
                return self
            self._buf = b""
            if self.serial_port is None:
                try:
                    sock = socket.create_connection((self.host, self.port), self.timeout)
                except OSError as exc:
                    raise TulipError(f"Cannot connect to {self.address}: {exc}") from exc
                sock.settimeout(self.timeout)
                self._sock = sock
            else:
                import serial  # pyserial; imported here so TCP use does not need it

                try:
                    ser = serial.Serial(self.serial_port, self.baudrate, timeout=self.timeout,
                                        write_timeout=self.timeout)
                except (serial.SerialException, OSError) as exc:
                    raise TulipError(f"Cannot open {self.serial_port}: {exc}") from exc
                if self.serial_startup_delay > 0:
                    time.sleep(self.serial_startup_delay)
                try:
                    ser.reset_input_buffer()  # boot messages, if any  # inferred
                except Exception:
                    pass
                self._ser = ser
            return self

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            ser, self._ser = self._ser, None
            self._buf = b""
            for link in (sock, ser):
                if link is not None:
                    try:
                        link.close()
                    except Exception:
                        pass

    @property
    def is_open(self) -> bool:
        return self._sock is not None or self._ser is not None

    def __enter__(self) -> "TulipDriver":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<TulipDriver {self.address} {'open' if self.is_open else 'closed'}>"

    def _check_open(self) -> None:
        if not self.is_open:
            raise TulipError("Session is closed - call open() or use 'with TulipDriver(...) as g'")

    # ---------- raw access ----------
    def write(self, cmd: str) -> None:
        """Send a command that gets no reply (the SET* commands)."""
        with self._lock:
            self._check_open()
            data = cmd.encode("ascii") + b"\n"
            try:
                if self._sock is not None:
                    self._sock.sendall(data)
                else:
                    self._ser.write(data)
                    self._ser.flush()
            except OSError:  # serial.SerialException is an OSError too
                self.close()
                raise

    def query(self, cmd: str, expect: Optional[str] = None) -> str:
        """Send *cmd* and return its reply line.

        The reply must start with ``expect`` (default: the instruction of
        *cmd*), otherwise the session is out of step: it is closed and
        :class:`TulipReplyError` is raised. ``expect=""`` skips the check.
        """
        if expect is None:
            expect = cmd.split(":", 1)[0]
        with self._lock:
            self.write(cmd)
            reply = self._readline()
            if expect and reply != expect and not reply.startswith(expect + ":"):
                self.close()
                raise TulipReplyError(f"Unexpected reply to {cmd!r}: {reply!r} (session closed)")
            return reply

    def _readline(self) -> str:
        try:
            if self._sock is not None:
                while b"\n" not in self._buf:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("driver closed the connection")
                    self._buf += chunk
                line, self._buf = self._buf.split(b"\n", 1)
            else:
                line = self._ser.read_until(b"\n")
                if not line.endswith(b"\n"):
                    # pyserial returns what it has on timeout instead of raising.
                    raise TimeoutError(f"no reply from {self.serial_port} within {self.timeout} s")
        except OSError:
            self.close()
            raise
        return line.decode("ascii", errors="replace").strip()

    def _values(self, cmd: str, count: int, expect: Optional[str] = None) -> list[str]:
        """Query *cmd* and return the ``count`` last fields of the reply."""
        reply = self.query(cmd, expect)
        fields = reply.split(":")
        if len(fields) < count + 1:
            raise TulipError(f"Unexpected reply to {cmd!r}: {reply!r}")
        return fields[-count:]

    # ---------- identity ----------
    def help(self) -> list[str]:
        """Instructions the driver knows (HELP)."""
        return self.query("HELP").split(":")[1:]

    def version(self) -> str:
        """Firmware version, ``"A.B.C"`` (GETVERSION)."""
        return self._values("GETVERSION", 1)[0]

    def ip(self) -> tuple[str, int]:
        """The driver's configured IP address and port (GETIP)."""
        address, port = self._values("GETIP", 2)
        return address, int(port)

    def set_ip(self, address: str, port: int = DEFAULT_PORT) -> None:
        """Change the driver's IP address and port (SETIP). Takes effect after
        the driver is power-cycled."""
        socket.inet_aton(address)  # raises OSError on a malformed address
        port = int(_check_range("port", port, 1, 65535))
        self.write(f"SETIP:{address}:{port}")

    # ---------- state ----------
    def state(self) -> TulipState:
        """Opening, speed, motion law limits and travel time (GETSTATE)."""
        fields = self._values("GETSTATE", 6)
        try:
            return TulipState(*(float(f) for f in fields))
        except ValueError:
            raise TulipError(f"Unexpected GETSTATE values: {fields!r}") from None

    def position(self) -> float:
        """Current opening, % (0 closed, 100 open)."""
        return self.state().position

    def speed(self) -> float:
        """Current opening speed, %/s."""
        return self.state().speed

    @property
    def target(self) -> Optional[float]:
        """Target of the last move sent by this session, %, or None."""
        return self._target

    # ---------- errors ----------
    def error(self) -> tuple[int, str]:
        """``(code, text)`` of the driver's error state (GETERROR); 0 is no error."""
        return self._parse_error(self._values("GETERROR", 1)[0])

    def clear_error(self) -> tuple[int, str]:
        """Clear the error and run the driver's recovery (CLEARERROR). Returns
        the error state afterwards, ``(0, ...)`` if it worked."""
        return self._parse_error(self._values("CLEARERROR", 1, expect="GETERROR")[0])

    def raise_on_error(self, context: str = "") -> None:
        code, _ = self.error()
        if code:
            raise TulipDeviceError(code, context)

    @staticmethod
    def _parse_error(value: str) -> tuple[int, str]:
        try:
            code = int(float(value))
        except ValueError:
            raise TulipError(f"Unexpected error code: {value!r}") from None
        return code, ERROR_TEXT.get(code, "Unknown error")

    # ---------- motion ----------
    def set_position(self, percent: float) -> None:
        """Start a move to ``percent`` opening (SETPOS). Returns at once: see :meth:`wait`."""
        percent = _check_range("position", percent, 0, 100)
        with self._lock:
            self.write(f"SETPOS:{_num(percent)}")
            self._target = percent

    def set_speed(self, percent_per_s: float) -> None:
        """Drive in speed (SETSPEED): > 0 opens to 100 %, < 0 closes to 0 %,
        0 holds the current opening. Returns at once."""
        value = float(percent_per_s)
        with self._lock:
            self.write(f"SETSPEED:{_num(value)}")
            self._target = 100.0 if value > 0 else 0.0 if value < 0 else None

    def stop(self) -> None:
        """Hold the current opening (SETSPEED:0)."""
        self.set_speed(0)

    def set_motion_max(self, speed_max: float, accel_max: float, jerk_max: float) -> None:
        """Set the motion law limits, %/s, %/s^2, %/s^3 (SETMOTIONMAX). Saved to flash."""
        values = [_check_positive(n, v) for n, v in
                  (("speed_max", speed_max), ("accel_max", accel_max), ("jerk_max", jerk_max))]
        self.write("SETMOTIONMAX:" + ":".join(_num(v) for v in values))

    def set_travel_time(self, seconds: float) -> None:
        """Tune the motion law for a full opening or closing in ``seconds``
        (SETTRAVELTIME). Saved to flash."""
        self.write(f"SETTRAVELTIME:{_num(_check_positive('travel time', seconds))}")

    def move_to(self, percent: float, wait: bool = True, timeout: float = 5.0,
                tolerance: float = 0.5) -> None:
        """Move to ``percent`` opening and, if ``wait``, block until settled."""
        self.set_position(percent)
        if wait:
            self.wait(timeout=timeout, tolerance=tolerance)

    def wait(self, timeout: float = 5.0, tolerance: float = 0.5, poll: float = 0.01) -> None:
        """Block until the gripper is steady on the target of the last move.

        Settled means GETSTATE reports a null speed and, when the target is
        known, an opening within ``tolerance`` % of it.  # inferred: the manual
        only says that a null speed means steady.

        Raises
        ------
        TulipAborted
            :meth:`abort` was called (from any thread) while waiting.
        TulipDeviceError
            The driver reports an error, at the end or after the timeout.
        TulipMotionError
            Not settled after ``timeout`` seconds.
        """
        aborts = self._aborts
        target = self._target
        deadline = time.monotonic() + timeout
        while True:
            st = self.state()
            if self._aborts != aborts:
                raise TulipAborted("wait interrupted by abort()")
            if not st.moving and (target is None or abs(st.position - target) <= tolerance):
                break
            if time.monotonic() > deadline:
                self.raise_on_error("move did not settle")
                raise TulipMotionError(
                    f"Not settled after {timeout} s: opening {st.position:g} %"
                    + (f", target {target:g} %" if target is not None else "")
                    + f", speed {st.speed:g} %/s")
            time.sleep(poll)  # the lock is free here, so abort() can get in
        self.raise_on_error("after move")

    def abort(self) -> None:
        """Interrupt every :meth:`wait` in progress, then hold the current
        opening (SETSPEED:0). Safe on a closed session.

        Does not queue behind a long exchange of another thread: if the
        session stays busy for more than 0.5 s the stop is skipped (the
        interrupted wait still raises).
        """
        self._aborts += 1
        if not self.is_open:
            return
        if not self._lock.acquire(timeout=0.5):
            return
        try:
            if self.is_open:
                self.stop()
        finally:
            self._lock.release()


# --------------------------------------------------------------------------- CLI
def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Read (and optionally move) a Percipio Tulip gripper. Read-only unless --move is given.")
    ap.add_argument("target", nargs="?", default=DEFAULT_HOST,
                    help=f"driver IP address (default {DEFAULT_HOST}), or a serial port such as COM4")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port")
    ap.add_argument("--move", type=float, metavar="PERCENT", help="move to this opening and wait")
    ap.add_argument("--clear-error", action="store_true", help="send CLEARERROR first")
    args = ap.parse_args(argv)

    is_serial = args.target.upper().startswith("COM") or args.target.startswith("/dev/")
    driver = (TulipDriver(serial_port=args.target) if is_serial
              else TulipDriver(args.target, args.port))
    try:
        with driver as g:
            print("version  :", g.version())
            print("help     :", " ".join(g.help()))
            if args.clear_error:
                print("clear    :", g.clear_error())
            print("error    :", g.error())
            for key, value in g.state().as_dict().items():
                print(f"{key:<9}: {value:g}")
            if args.move is not None:
                started = time.monotonic()
                g.move_to(args.move)
                print(f"moved    : {g.position():g} % in {time.monotonic() - started:.3f} s")
    except (TulipError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
