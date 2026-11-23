# Protocol module template (`devices/api/<snake_name>.py`)

The low-level session for a device that speaks a text protocol over TCP. The real examples are `devices/api/smaract_mcs2.py` (motion, error queue, blocking wait with abort) and `devices/api/thorlabs_pm.py` (SCPI over PyVISA). Read the closer one before you start: this file is the distilled pattern, not a replacement.

For serial ports, swap the socket for `serial.Serial(port, baudrate, timeout=...)` (pyserial is already in requirements.txt). For VISA instruments, follow thorlabs_pm.py. The rules stay the same.

## Rules and why

| Rule | Why |
|---|---|
| Imports nothing from `devices/` or `core/` | Usable from a notebook, a script, or the `_cli()`, without the app. |
| One `threading.RLock` around every exchange, held across multi-step ones | The task thread and the Live panel (Flask thread) share one session. Without the lock, thread A reads thread B's reply. Reentrant, because compound methods call `query()`. |
| On any `OSError` (timeout, reset, closed): `close()` then re-raise | After a timeout the device may still send the late reply, which the next query would take as its own answer. A fresh session is the only safe state. |
| Socket errors stay `OSError`; protocol/usage errors are `XError` | The driver latches a fault on `OSError` (link broken) but not on `XError` (bad command, refused value), which is the task's problem. |
| Device-reported errors become `XCommandError` with the code and text | Task code and logs show "[34] Invalid channel index (after ':MOVE9 0')", not a silent no-op. |
| A blocking `wait()` checks an abort counter between polls | `abort()` from another thread must end the wait within one poll period. The counter needs no lock: only `abort()` writes it, and any change counts. |
| A command that starts motion reads the counter **before** sending, in the same lock hold as the send, and passes it to `wait(since=...)` | Otherwise an abort that arrives between the move command and the start of `wait()` is missed: the device stopped, the wait sees "idle", and the move reports success. Holding the lock also means abort()'s stop goes out *after* the move command, so it stops this move instead of being used up before it. An abort before the task even calls the move is the task framework's job (its stop flag). (`smaract_mcs2.py` predates this rule and has the race.) |
| Retry only queries, never commands that act | A resent move, step or toggle runs twice. See SKILL.md rule 14. |
| Units in the method name (`position_mm`, `set_velocity_mm_s`) | Devices use odd base units (picometres, counts). The API converts once, so task code never guesses. |
| `write()` / `query()` stay public | Anything not wrapped is still reachable: `dev.api.query("...")`. |
| `__all__`, context manager, `__repr__` | Matches the other modules, and `with X(host) as x:` closes on error. |
| `_cli()` under `if __name__ == "__main__":` | The user can check the hardware at the machine: `python -m devices.api.<module> <ip>`. Keep it read-only by default, with motion behind an explicit flag. |
| Module docstring states the protocol conventions **and their source**, and whether the code has run on real hardware | Port, terminator, reply format, error reporting, units, and where the syntax came from (manual section, open-source driver, or "unverified"). Say "Not yet run against a real <model>" until someone has: the report is read once, but the docstring stays with the code. |

## Skeleton

```python
"""
acme_ls.py - ACME LS-series laser sensor over its Ethernet ASCII interface
=========================================================================

Plain TCP socket, no vendor DLL, so the same code runs on Windows, Linux and macOS.

Command syntax: ACME LS Communication Manual rev. 4, chapter 6. <-- say where it came from;
mark commands not run on real hardware with "# unverified" next to them.

Conventions:
  * TCP port 9100, every command terminated with "\\r\\n"
  * every command gets one reply line; errors reply "ER,<code>"
  * distances are integers in micrometres; the ``*_mm`` helpers convert

Quick start::

    from devices.api.acme_ls import AcmeLS

    with AcmeLS("192.168.0.20") as s:
        print(s.idn())
        print(s.distance_mm(head=0))

Anything not wrapped here is reachable with ``s.query("...")``.

A session may be shared between threads: one lock serializes every exchange.
After a timeout or any other socket error the session closes itself: the late
reply would otherwise be read as the answer to the next query. Call open() again.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from typing import Optional

__all__ = ["AcmeLS", "AcmeLSError", "AcmeLSCommandError", "AcmeLSAborted", "DEFAULT_PORT"]

DEFAULT_PORT = 9100
UM_PER_MM = 1000

ERROR_TEXT = {  # from the manual's error table
    1: "Command not recognised",
    2: "Parameter out of range",
}


class AcmeLSError(Exception):
    """Usage or protocol error: closed session, unexpected reply, failed check.

    Socket failures (timeouts, resets) are raised as the original ``OSError``
    instead, so callers can tell a broken link from a refused command.
    """


class AcmeLSCommandError(AcmeLSError):
    """The sensor refused a command."""

    def __init__(self, code: int, command: str):
        self.code, self.command = code, command
        super().__init__(f"[{code}] {ERROR_TEXT.get(code, 'Unknown error')} (after {command!r})")


class AcmeLSAborted(AcmeLSError):
    """wait() was interrupted by abort()."""


class AcmeLS:
    """An ACME LS session over TCP (context manager)."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 5.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        # Reentrant: compound operations take it, then call query(), which takes it again.
        self._lock = threading.RLock()
        # Bumped by abort(). A wait() that sees it change was interrupted.
        self._aborts = 0

    # ---------- connection ----------
    def open(self) -> "AcmeLS":
        with self._lock:
            if self._sock is not None:
                return self
            try:
                sock = socket.create_connection((self.host, self.port), self.timeout)
            except OSError as exc:
                raise AcmeLSError(f"Cannot connect to {self.host}:{self.port}: {exc}") from exc
            sock.settimeout(self.timeout)
            self._sock, self._buf = sock, b""
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

    def __enter__(self) -> "AcmeLS":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<AcmeLS {self.host}:{self.port} {'open' if self._sock else 'closed'}>"

    def _session(self) -> socket.socket:
        if self._sock is None:
            raise AcmeLSError("Session is closed - call open() or use 'with AcmeLS(host) as s'")
        return self._sock

    # ---------- raw access ----------
    def write(self, cmd: str) -> None:
        """Send a command without reading a reply (only for commands that send none)."""
        with self._lock:
            sock = self._session()
            try:
                sock.sendall(cmd.encode("ascii") + b"\r\n")
            except OSError:
                self.close()
                raise

    def query(self, cmd: str) -> str:
        """Send *cmd* and return its reply line. Raises AcmeLSCommandError on "ER,<code>"."""
        with self._lock:
            self.write(cmd)
            sock = self._session()
            try:
                while b"\n" not in self._buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("sensor closed the connection")
                    self._buf += chunk
            except OSError:
                self.close()
                raise
            line, self._buf = self._buf.split(b"\n", 1)
        reply = line.decode("ascii", errors="replace").strip()
        if reply.startswith("ER,"):
            raise AcmeLSCommandError(int(reply[3:] or -1), cmd)
        return reply

    # ---------- device ----------
    def idn(self) -> str:
        return self.query("ID?")

    def distance_mm(self, head: int = 0) -> float:
        reply = self.query(f"MS,{head:02d}")
        try:
            return int(reply) / UM_PER_MM
        except ValueError:
            raise AcmeLSError(f"Unexpected reply to distance query: {reply!r}") from None

    # ---------- blocking operations (only if the device has any) ----------
    def move_to_mm(self, position_mm: float, wait: bool = True, timeout: float = 30.0) -> None:
        # Read the counter BEFORE sending, in the same lock hold as the send:
        # an abort() that lands between the command and wait() must still fail
        # the move, and abort()'s STOP (which needs the lock) can only go out
        # after the move command, so it always stops this move.
        with self._lock:
            aborts = self._aborts
            self.query(f"PA,{round(position_mm * UM_PER_MM)}")    # unverified
        if wait:
            self.wait(timeout, since=aborts)

    def wait(self, timeout: float = 30.0, poll: float = 0.05, since: Optional[int] = None) -> None:
        """Block until idle. Raises AcmeLSAborted if abort() was called since
        *since* (a value of ``self._aborts`` read before the command was sent),
        or since this call when *since* is None."""
        aborts = self._aborts if since is None else since
        if self._aborts != aborts:     # aborted before we even started polling
            raise AcmeLSAborted("wait interrupted by abort()")
        deadline = time.monotonic() + timeout
        while self.query("BUSY?") == "1":
            if self._aborts != aborts:
                raise AcmeLSAborted("wait interrupted by abort()")
            if time.monotonic() > deadline:
                raise AcmeLSError(f"still busy after {timeout} s")
            time.sleep(poll)          # the lock is free here, so abort() can get in
        if self._aborts != aborts:    # ended *because* of the abort: not a success
            raise AcmeLSAborted("wait interrupted by abort()")

    def abort(self) -> None:
        """Interrupt every wait() in progress, then stop. Safe on a closed session."""
        self._aborts += 1
        if self._sock is not None:
            self.query("STOP")


# --------------------------------------------------------------------------- CLI
def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read one ACME LS sensor.")
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--head", type=int, default=0)
    args = ap.parse_args(argv)
    try:
        with AcmeLS(args.host, args.port) as s:
            print("ID       :", s.idn())
            print(f"distance : {s.distance_mm(args.head):.3f} mm")
    except (AcmeLSError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
```

## Adapting it

- **Error queue instead of inline errors** (SCPI-style `:SYST:ERR?`): drain the queue in `open()` (so a previous session's errors don't surface on the first setter), and check it after each setter under the same lock hold. See `_set()` / `drain_errors()` in smaract_mcs2.py.
- **Setters without a reply**: `write()` then an error check, never `query()`, or the read blocks until the timeout.
- **A minimum gap between messages** (some controllers drop a command sent within 50 ms of the previous one): keep `self._last = time.monotonic()` after each exchange and sleep off the remainder inside the lock, in `write()`. Say so in the docstring, and keep `telemetry()` to one query (SKILL.md rule 13).
- **Binary protocols**: same structure. Use `struct` for framing, and keep a `_recv_exact(n)` helper that closes on `OSError` like `query()` does.
- **Modbus**: use pymodbus, wrapped in a lock-serialized client. See `_SerializedClient` in `devices/api/IoLogikE1212Api.py`.
- **HTTP/REST**: no driver here uses it yet, and `requests` isn't in requirements.txt (add and pin it). Use a `requests.Session` with a timeout on every call, and keep the same split: `requests.ConnectionError`/`Timeout` are link failures, HTTP 4xx are usage errors.
