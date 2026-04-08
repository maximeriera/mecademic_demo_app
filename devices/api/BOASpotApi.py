import socket
import threading
import logging
import time
from typing import Optional


class BOASpotError(Exception):
    """Raised when a BOA Spot communication error occurs."""
    pass


class BOASpotApi:
    """
    TCP/IP stream API for Teledyne BOA Spot smart cameras.

    The BOA Spot uses a simple TCP/IP stream for general communication.
    The camera can act as a **server** (default — the camera listens) or a
    **client** (the camera connects to an external server).  In this driver
    the PC acts as a **client** connecting to the BOA Spot's TCP server port.

    Default communication port is **5024** (pre-configured on the camera).
    An additional port (e.g. 5025) can be added in the BOA Spot setup panel
    for result output.

    Protocol
    --------
    Commands are plain ASCII strings terminated by ``\\r\\n``.

    * **WriteString / WriteFormatString** — the camera pushes results or
      messages to connected clients after each inspection.
    * **ReadString** — the camera reads strings from the client (e.g.
      trigger commands, solution-change requests).

    Trigger and solution change
    ---------------------------
    When the camera's *Periodic* script polls ``ReadString`` on the control
    port, the first character is interpreted as a command (``T`` = trigger)
    and the second as a solution number.  Sending ``"T0\\r\\n"`` triggers
    the current solution; ``"T2\\r\\n"`` switches to solution 2 and triggers.

    Parameters
    ----------
    ip_address : str
        IP address of the BOA Spot camera.
    port : int
        TCP port the camera listens on (default **5024**).
    result_port : int, optional
        A second TCP port used by the camera to push inspection results
        (e.g. 5025).  When provided, a dedicated result socket is opened.
    terminator : str
        Line terminator (default ``"\\r\\n"``).
    timeout : float
        Socket timeout in seconds (default 5.0).
    logger : logging.Logger, optional
        Parent logger; a child logger is created if omitted.

    References
    ----------
    *BOA Spot Communication Guide*, Teledyne Imaging, Version 1.9,
    Document 405-00061-00 — TCP/IP Stream chapter (pages 9-11).
    """

    DEFAULT_PORT = 5024
    DEFAULT_RESULT_PORT = 5025

    def __init__(
        self,
        ip_address: str,
        port: int = DEFAULT_PORT,
        result_port: Optional[int] = None,
        terminator: str = "\r\n",
        timeout: float = 5.0,
        logger: Optional[logging.Logger] = None,
    ):
        self.logger = logger or logging.getLogger(__name__)
        self._ip = ip_address
        self._port = port
        self._result_port = result_port
        self._terminator = terminator
        self._timeout = timeout

        self._cmd_sock: Optional[socket.socket] = None
        self._result_sock: Optional[socket.socket] = None

        self._cmd_connected = False
        self._result_connected = False
        self._faulted = False

        self._cmd_lock = threading.Lock()
        self._result_lock = threading.Lock()

        # Async result listener
        self._result_thread: Optional[threading.Thread] = None
        self._result_stop = threading.Event()
        self._result_callback = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the command TCP channel (and optionally the result channel).

        Raises
        ------
        ConnectionError
            If the command channel cannot be established.
        """
        self.logger.info(f"Connecting to BOA Spot at {self._ip}:{self._port} ...")
        try:
            self._cmd_sock = self._open_socket(self._ip, self._port)
            self._cmd_connected = True
            self.logger.info(f"  Command channel connected (port {self._port})")
        except Exception as e:
            raise ConnectionError(
                f"BOA Spot command channel connect failed at {self._ip}:{self._port}: {e}"
            ) from e

        if self._result_port is not None:
            try:
                self._result_sock = self._open_socket(self._ip, self._result_port)
                self._result_connected = True
                self.logger.info(f"  Result channel connected (port {self._result_port})")
            except Exception as e:
                self.logger.warning(
                    f"  Result channel connect failed (port {self._result_port}): {e}"
                )

        self._faulted = False
        self.logger.info("BOA Spot connected.")

    def disconnect(self) -> None:
        """Stop any background listener and close all sockets."""
        self.stop_result_listener()
        for sock_attr, flag_attr in (
            ("_cmd_sock", "_cmd_connected"),
            ("_result_sock", "_result_connected"),
        ):
            sock = getattr(self, sock_attr)
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
                setattr(self, sock_attr, None)
            setattr(self, flag_attr, False)
        self.logger.info("BOA Spot disconnected.")

    @property
    def is_connected(self) -> bool:
        """True if the command channel socket is open."""
        return self._cmd_connected and self._cmd_sock is not None

    # ------------------------------------------------------------------
    # Command channel — camera control
    # ------------------------------------------------------------------

    def trigger(self, solution: Optional[int] = None) -> None:
        """Send a software trigger to the BOA Spot.

        Parameters
        ----------
        solution : int, optional
            If provided, the camera switches to this solution before
            triggering.  Valid solution numbers are 0–8.

        Notes
        -----
        The BOA Spot Periodic script expects the string ``"T<sol>\\r\\n"``
        where ``<sol>`` is the solution number.  ``"T0"`` triggers the
        current solution without switching.
        """
        sol = solution if solution is not None else 0
        self._send_command(f"T{sol}")
        self.logger.debug(f"Trigger sent (solution={sol}).")

    def change_solution(self, solution_id: int) -> None:
        """Request a solution change without triggering.

        Parameters
        ----------
        solution_id : int
            Target solution number (0-8).
        """
        self._send_command(f"S{solution_id}")
        self.logger.info(f"Solution change requested: {solution_id}")

    def send_string(self, message: str) -> None:
        """Send an arbitrary string to the camera's control port.

        Parameters
        ----------
        message : str
            The raw string to send (terminator is appended automatically).
        """
        self._send_command(message)

    def read_string(self, timeout: Optional[float] = None) -> str:
        """Read a string from the camera's command channel.

        Parameters
        ----------
        timeout : float, optional
            Override the default socket timeout for this read.

        Returns
        -------
        str
            The received string with the terminator stripped.
        """
        with self._cmd_lock:
            return self._recv_line(self._cmd_sock, timeout=timeout)

    # ------------------------------------------------------------------
    # Result channel — inspection data
    # ------------------------------------------------------------------

    def read_result(self, timeout: Optional[float] = None) -> str:
        """Block and read one result line from the result channel.

        Parameters
        ----------
        timeout : float, optional
            Override the default socket timeout for this read.

        Returns
        -------
        str
            The result string with the terminator stripped.

        Raises
        ------
        BOASpotError
            If the result channel is not connected.
        """
        if not self._result_connected or self._result_sock is None:
            raise BOASpotError("Result channel is not connected.")
        with self._result_lock:
            return self._recv_line(self._result_sock, timeout=timeout)

    def start_result_listener(self, callback) -> None:
        """Start a background thread that reads result lines and calls
        *callback(line)* for each one.

        Parameters
        ----------
        callback : Callable[[str], None]
            Function invoked for every result line received.
        """
        if self._result_thread is not None and self._result_thread.is_alive():
            self.logger.warning("Result listener already running.")
            return
        if not self._result_connected or self._result_sock is None:
            raise BOASpotError("Cannot start result listener: result channel not connected.")

        self._result_callback = callback
        self._result_stop.clear()
        self._result_thread = threading.Thread(
            target=self._result_listener_loop,
            name="BOASpot-ResultListener",
            daemon=True,
        )
        self._result_thread.start()
        self.logger.info("Result listener started.")

    def stop_result_listener(self) -> None:
        """Stop the background result listener if running."""
        if self._result_thread is not None and self._result_thread.is_alive():
            self._result_stop.set()
            self._result_thread.join(timeout=self._timeout + 1)
            self.logger.info("Result listener stopped.")
        self._result_thread = None
        self._result_callback = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open_socket(self, ip: str, port: int) -> socket.socket:
        """Create and connect a TCP socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(self._timeout)
        sock.connect((ip, port))
        return sock

    def _send_command(self, command: str) -> None:
        """Send a terminated string on the command channel."""
        if not self._cmd_connected or self._cmd_sock is None:
            raise BOASpotError("Command channel is not connected.")
        raw = (command + self._terminator).encode("ascii")
        with self._cmd_lock:
            try:
                self._cmd_sock.sendall(raw)
                self.logger.debug(f"TX → {command!r}")
            except Exception as e:
                self._faulted = True
                self._cmd_connected = False
                raise BOASpotError(f"Send failed: {e}") from e

    def _recv_line(self, sock: socket.socket, timeout: Optional[float] = None) -> str:
        """Receive bytes until the terminator is found and return the stripped line."""
        prev_timeout = sock.gettimeout()
        if timeout is not None:
            sock.settimeout(timeout)
        try:
            buf = b""
            term = self._terminator.encode("ascii")
            while True:
                chunk = sock.recv(1)
                if not chunk:
                    raise BOASpotError("Connection closed by BOA Spot.")
                buf += chunk
                if buf.endswith(term):
                    line = buf[: -len(term)].decode("ascii")
                    self.logger.debug(f"RX ← {line!r}")
                    return line
        except socket.timeout:
            raise TimeoutError("Receive timed out waiting for BOA Spot response.")
        except Exception as e:
            self._faulted = True
            raise BOASpotError(f"Receive failed: {e}") from e
        finally:
            sock.settimeout(prev_timeout)

    def _result_listener_loop(self) -> None:
        """Background loop: reads result lines and dispatches them to the callback."""
        self.logger.debug("Result listener loop started.")
        while not self._result_stop.is_set():
            try:
                with self._result_lock:
                    line = self._recv_line(self._result_sock)
                if self._result_callback:
                    self._result_callback(line)
            except TimeoutError:
                continue
            except BOASpotError as e:
                self.logger.error(f"Result listener error: {e}")
                self._faulted = True
                break
        self.logger.debug("Result listener loop exited.")
        
def example_usage():
    # Example of using the BOASpotApi
    boa = BOASpotApi(ip_address="192.168.0.110", port=5025, result_port=5026)
    try:
        boa.connect()
        boa.start_result_listener(lambda line: print(f"Received result: {line}"))
        boa.trigger()
        time.sleep(2)  # Keep the listener running for a while to receive results
    finally:
        boa.stop_result_listener()
        boa.disconnect()

if __name__ == "__main__":
    example_usage()
