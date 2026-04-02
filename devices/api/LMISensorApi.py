import socket
import threading
import logging
from dataclasses import dataclass
from typing import Callable, Optional, List


@dataclass
class MeasurementResult:
    """A parsed measurement from the Gocator 6.5 standard result format.

    Gocator 6.5 wire format per measurement::

        M<id>,<sensor_id>,V<value>,D<decision>

    Values are scaled × 1000 on the wire (e.g. 151290 → 151.290 mm).
    Decision word: Bit 0 = pass/fail; Bits 1-7 = validity flags
    (0 = OK, 1 = invalid value, 2 = invalid anchor).
    """
    id: int
    sensor_id: int        # Sub-sensor ID (e.g. 0 = left camera, 1 = right camera)
    value: Optional[float]  # Physical value in sensor units; None when INVALID
    decision: int         # Raw decision word
    value_valid: bool     # True when value is numeric (not INVALID)
    passed: bool          # True when decision bit 0 == 1
    anchor_valid: bool    # True when decision bit 1 == 0 (anchor reference valid)


class LMISensorError(Exception):
    """Raised when the sensor replies with an ERROR status."""
    pass


class LMISensorApi:
    """
    Ethernet ASCII protocol API for LMI Gocator sensors (firmware 6.5.x).

    The Gocator 6.5 ASCII protocol uses **three separate TCP channels**:

    * **Command channel** (default port 3190) – start/stop/trigger, job
      loading, stamp, alignment and runtime variable commands.
    * **Data channel** (default port 3192)  – result polling; also receives
      sensor-pushed data when in asynchronous mode.
    * **Health channel** (default port 3194) – receives health indicator
      strings pushed by the sensor.

    Protocol framing::

        Command:  <CMD>,<PARAM1>,<PARAM2>\\r\\n
        Reply:    OK,<token1>,<token2>\\r\\n  |  ERROR,<message>\\r\\n

    Ref: https://am.lmi3d.com/manuals/gocator/gocator-6.5/G3/Default.htm

    Parameters
    ----------
    ip_address : str
        IP address of the sensor.
    control_port : int
        Command / control channel TCP port (default 3190).
    data_port : int
        Data channel TCP port (default 3192).
    health_port : int
        Health channel TCP port (default 3194).
    delimiter : str
        Field separator used in commands and replies (default ``','``).
    terminator : str
        Command / line terminator (default ``'\\r\\n'``).
    timeout : float
        Socket receive timeout in seconds (default 5.0).
    logger : logging.Logger, optional
        Parent logger; a child logger is created if omitted.

    Notes
    -----
    Data channel polling (:meth:`get_result`, :meth:`get_measurements`) and
    async listening (:meth:`start_async_listener`) are mutually exclusive on
    the same socket.  Stop the async listener before calling polling methods.
    """

    DEFAULT_CONTROL_PORT = 3190
    DEFAULT_DATA_PORT = 3192
    DEFAULT_HEALTH_PORT = 3194
    INVALID_MARKER = "INVALID"

    def __init__(
        self,
        ip_address: str,
        control_port: int = DEFAULT_CONTROL_PORT,
        data_port: int = DEFAULT_DATA_PORT,
        health_port: int = DEFAULT_HEALTH_PORT,
        delimiter: str = ",",
        terminator: str = "\r\n",
        timeout: float = 5.0,
        logger: logging.Logger = None,
    ):
        self.logger = logger or logging.getLogger(__name__)
        self._ip = ip_address
        self._control_port = control_port
        self._data_port = data_port
        self._health_port = health_port
        self._delimiter = delimiter
        self._terminator = terminator
        self._timeout = timeout

        self._ctrl_sock: Optional[socket.socket] = None
        self._data_sock: Optional[socket.socket] = None
        self._health_sock: Optional[socket.socket] = None

        self._ctrl_connected = False
        self._data_connected = False
        self._health_connected = False
        self._faulted = True

        self._ctrl_lock = threading.Lock()
        self._data_lock = threading.Lock()

        # Async data listener
        self._data_thread: Optional[threading.Thread] = None
        self._data_stop = threading.Event()
        self._data_callback: Optional[Callable[[str], None]] = None

        # Async health listener
        self._health_thread: Optional[threading.Thread] = None
        self._health_stop = threading.Event()
        self._health_callback: Optional[Callable[[str], None]] = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open all three TCP channels to the sensor.

        The command channel is mandatory.  Data and health channel failures
        are logged as warnings so the device can still be used for basic
        control if a channel is unavailable.
        """
        self.logger.info(f"Connecting to Gocator at {self._ip} ...")
        try:
            self._ctrl_sock = self._open_socket(self._control_port)
            self._ctrl_connected = True
            self.logger.debug(f"  Command channel connected (port {self._control_port})")
        except OSError as e:
            raise ConnectionError(f"Command channel connect failed at {self._ip}:{self._control_port}: {e}") from e

        try:
            self._data_sock = self._open_socket(self._data_port)
            self._data_connected = True
            self.logger.debug(f"  Data channel connected (port {self._data_port})")
        except OSError as e:
            self.logger.warning(f"  Data channel connect failed (port {self._data_port}): {e}")

        try:
            self._health_sock = self._open_socket(self._health_port)
            self._health_connected = True
            self.logger.debug(f"  Health channel connected (port {self._health_port})")
        except OSError as e:
            self.logger.warning(f"  Health channel connect failed (port {self._health_port}): {e}")

        self._faulted = False
        self.logger.info("Gocator connected.")

    def disconnect(self) -> None:
        """Stop all background listeners and close all three sockets."""
        self.stop_async_listener()
        self.stop_health_listener()
        for sock_attr, flag_attr in (
            ("_ctrl_sock", "_ctrl_connected"),
            ("_data_sock", "_data_connected"),
            ("_health_sock", "_health_connected"),
        ):
            sock = getattr(self, sock_attr)
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
                setattr(self, sock_attr, None)
            setattr(self, flag_attr, False)
        self.logger.info("Gocator disconnected.")

    @property
    def is_connected(self) -> bool:
        """True if the command channel socket is open."""
        return self._ctrl_connected and self._ctrl_sock is not None

    # ------------------------------------------------------------------
    # Command channel — sensor control
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the sensor (transition to Running state)."""
        self._ctrl_command("Start")
        self.logger.info("Sensor started.")

    def stop(self) -> None:
        """Stop the sensor (transition to Ready state)."""
        self._ctrl_command("Stop")
        self.logger.info("Sensor stopped.")

    def trigger(self) -> None:
        """Send a software trigger to the sensor."""
        self._ctrl_command("Trigger")
        self.logger.debug("Software trigger sent.")

    def load_job(self, job_name: str = None) -> str:
        """Load a job file on the sensor.

        Parameters
        ----------
        job_name : str, optional
            Job filename.  The ``.job`` extension is appended automatically
            by the firmware if omitted.  Pass ``None`` (default) to query
            the currently loaded job name.

        Returns
        -------
        str
            The reply payload, e.g. ``'test.job loaded successfully'`` or
            the current job name when called with no argument.
        """
        if job_name is not None:
            parts = self._ctrl_command("LoadJob", job_name)
        else:
            parts = self._ctrl_command("LoadJob")
        return self._delimiter.join(parts)

    def get_stamp(self, *fields: str) -> dict:
        """Retrieve the current time, encoder, and/or frame stamp.

        Parameters
        ----------
        *fields : str
            Optional subset of ``'time'``, ``'encoder'``, ``'frame'``.
            When no fields are given the sensor returns all three.

        Returns
        -------
        dict
            Keys from ``{'time', 'encoder', 'frame'}`` with integer values.
            ``time`` is in sensor ticks; divide by 1 024 000 for seconds.

        Examples
        --------
        >>> api.get_stamp()
        {'time': 9226989840, 'encoder': 0, 'frame': 6}
        >>> api.get_stamp('frame')
        {'frame': 6}
        """
        parts = self._ctrl_command("Stamp", *[f.lower() for f in fields])
        result: dict = {}
        it = iter(parts)
        if len(fields) == 1:
            # Single-field reply is a bare value  e.g. "OK,6"
            key = fields[0].lower()
            val = next(it, "0")
            result[key] = int(val) if val.lstrip("-").isdigit() else val
        else:
            # Paired reply  e.g. "OK,Time,9226989840,Encoder,0,Frame,6"
            for token in it:
                key = token.strip().lower()
                val = next(it, "0").strip()
                result[key] = int(val) if val.lstrip("-").isdigit() else val
        return result

    def clear_alignment(self) -> None:
        """Clear the previously stored sensor alignment.

        .. note::
            The exact ASCII command string (``ClearAlignment``) is inferred
            from the Gocator 6.5 TOC.  Verify against your firmware if you
            receive an error.
        """
        self._ctrl_command("ClearAlignment")
        self.logger.info("Sensor alignment cleared.")

    def stationary_alignment(self) -> None:
        """Perform a stationary alignment on the sensor.

        .. note::
            The exact ASCII command string (``StationaryAlignment``) is
            inferred from the Gocator 6.5 TOC.
        """
        self._ctrl_command("StationaryAlignment")
        self.logger.info("Stationary alignment completed.")

    def set_runtime_variable(self, name: str, value) -> None:
        """Set a sensor runtime variable.

        Parameters
        ----------
        name : str
            Runtime variable name as defined in the sensor job.
        value :
            New value; converted to string before sending.

        .. note::
            Command string (``SetRuntimeVariable``) inferred from the
            Gocator 6.5 documentation TOC.
        """
        self._ctrl_command("SetRuntimeVariable", name, str(value))
        self.logger.debug(f"Runtime variable '{name}' set to '{value}'.")

    def get_runtime_variable(self, name: str) -> str:
        """Get the current value of a sensor runtime variable.

        Returns
        -------
        str
            The raw value string returned by the sensor.

        .. note::
            Command string (``GetRuntimeVariable``) inferred from the
            Gocator 6.5 documentation TOC.
        """
        parts = self._ctrl_command("GetRuntimeVariable", name)
        return self._delimiter.join(parts)

    # ------------------------------------------------------------------
    # Data channel — result retrieval
    # ------------------------------------------------------------------

    def get_formatted_result(self) -> str:
        """Return the latest measurement data formatted by the sensor's custom format.

        Sends ``Result`` with **no arguments**.  The sensor evaluates its
        configured custom format string and returns the result, e.g. the
        default format ``%time, %value[0] %decision[0]`` produces::

            3878095400960,151.290,0

        If no scan has run yet, some tokens may be substituted with error
        messages such as ``Value for ID0 not found.``  Trigger a scan first
        (or use :meth:`get_result` / :meth:`get_measurements` with explicit
        IDs for standard-format data).

        Returns
        -------
        str
            The evaluated, comma-joined result string.
        """
        parts = self._data_command("Result")
        return self._delimiter.join(parts)

    def get_result(self, *measurement_ids: int) -> str:
        """Return raw standard-format result tokens for the given measurement IDs.

        The sensor returns one group per ID::

            M<id>,<sensor_id>,V<value>,D<decision>[,M<id>,...]*

        Parameters
        ----------
        *measurement_ids : int
            One or more measurement IDs defined in the sensor connection map.
            **At least one ID is required** — call :meth:`get_formatted_result`
            to get data in the sensor's custom format instead.

        Returns
        -------
        str
            Raw comma-joined payload (after ``OK,``).

        Raises
        ------
        ValueError
            If no measurement IDs are provided.
        """
        if not measurement_ids:
            raise ValueError(
                "get_result() requires at least one measurement_id. "
                "Call get_custom_format_string() to query the format template."
            )
        parts = self._data_command("Result", *measurement_ids)
        return self._delimiter.join(parts)

    def get_measurements(self, *measurement_ids: int) -> List[MeasurementResult]:
        """Return parsed measurements for the given IDs from the data channel.

        Parameters
        ----------
        *measurement_ids : int
            One or more measurement IDs defined in the sensor connection map.
            **At least one ID is required.**

        Returns
        -------
        list of :class:`MeasurementResult`

        Raises
        ------
        ValueError
            If no measurement IDs are provided.
        """
        if not measurement_ids:
            raise ValueError(
                "get_measurements() requires at least one measurement_id."
            )
        parts = self._data_command("Result", *measurement_ids)
        return self._parse_measurements(parts)

    # ------------------------------------------------------------------
    # Async data listener (data channel)
    # ------------------------------------------------------------------

    def set_async_callback(self, callback: Callable[[str], None]) -> None:
        """Register a callback invoked for every line received on the data channel.

        Parameters
        ----------
        callback : callable
            ``callback(line: str)`` – called from the listener thread with
            each raw result line pushed by the sensor.
        """
        self._data_callback = callback

    def start_async_listener(self) -> None:
        """Start a daemon thread that forwards data-channel pushes to the callback.

        Does nothing if already running.  Raises :class:`ConnectionError` if
        the data channel was not connected during :meth:`connect`.
        """
        if self._data_thread and self._data_thread.is_alive():
            return
        if not self._data_connected:
            raise ConnectionError("Data channel not connected; cannot start async listener.")
        self._data_stop.clear()
        self._data_thread = threading.Thread(
            target=self._async_listener_loop,
            name="lmi_data_listener",
            daemon=True,
        )
        self._data_thread.start()
        self.logger.info("Data async listener started.")

    def stop_async_listener(self) -> None:
        """Stop the async data listener thread."""
        if self._data_thread and self._data_thread.is_alive():
            self._data_stop.set()
            self._data_thread.join(timeout=3.0)
        self._data_thread = None

    # ------------------------------------------------------------------
    # Health channel listener
    # ------------------------------------------------------------------

    def set_health_callback(self, callback: Callable[[str], None]) -> None:
        """Register a callback for health messages from the health channel.

        Parameters
        ----------
        callback : callable
            ``callback(line: str)`` – called for each health line received.
        """
        self._health_callback = callback

    def start_health_listener(self) -> None:
        """Start a daemon thread that forwards health-channel messages to the callback.

        Does nothing if already running.  Raises :class:`ConnectionError` if
        the health channel was not connected during :meth:`connect`.
        """
        if self._health_thread and self._health_thread.is_alive():
            return
        if not self._health_connected:
            raise ConnectionError("Health channel not connected; cannot start health listener.")
        self._health_stop.clear()
        self._health_thread = threading.Thread(
            target=self._health_listener_loop,
            name="lmi_health_listener",
            daemon=True,
        )
        self._health_thread.start()
        self.logger.info("Health listener started.")

    def stop_health_listener(self) -> None:
        """Stop the health channel listener thread."""
        if self._health_thread and self._health_thread.is_alive():
            self._health_stop.set()
            self._health_thread.join(timeout=3.0)
        self._health_thread = None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _open_socket(self, port: int) -> socket.socket:
        """Create and connect a new TCP socket to self._ip:port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        sock.connect((self._ip, port))
        return sock

    def _ctrl_command(self, command: str, *params) -> list:
        """Send *command* on the Command channel; return result tokens."""
        return self._send_on(self._ctrl_sock, self._ctrl_lock, command, *params)

    def _data_command(self, command: str, *params) -> list:
        """Send *command* on the Data channel; return result tokens."""
        return self._send_on(self._data_sock, self._data_lock, command, *params)

    def _send_on(
        self, sock: socket.socket, lock: threading.Lock, command: str, *params
    ) -> list:
        """Serialise, send, receive and parse a command on *sock*."""
        tokens = [command] + [str(p) for p in params]
        msg = self._delimiter.join(tokens) + self._terminator
        with lock:
            try:
                sock.sendall(msg.encode("ascii"))
                reply = self._recv_line(sock)
            except OSError as e:
                self._faulted = True
                raise LMISensorError(f"Socket error during '{command}': {e}") from e
        ok, parts = self._parse_response(reply)
        if not ok:
            err = self._delimiter.join(parts)
            self.logger.error(f"Sensor error for '{command}': {err}")
            self._faulted = True
            raise LMISensorError(f"Sensor error for '{command}': {err}")
        return parts

    def _recv_line(self, sock: socket.socket) -> str:
        """Read bytes from *sock* until :attr:`_terminator` is found."""
        buf = b""
        term = self._terminator.encode("ascii")
        while not buf.endswith(term):
            chunk = sock.recv(4096)
            if not chunk:
                raise LMISensorError("Connection closed by sensor.")
            buf += chunk
        return buf.decode("ascii").strip()

    def _parse_response(self, response: str) -> tuple:
        """Split ``OK,...`` or ``ERROR,...`` into ``(ok_bool, [tokens])``."""
        tokens = [t.strip() for t in response.split(self._delimiter)]
        status = tokens[0].upper() if tokens else ""
        rest = tokens[1:] if len(tokens) > 1 else []
        return status == "OK", rest

    def _parse_measurements(self, parts: list) -> List[MeasurementResult]:
        """Parse Gocator 6.5 standard-format tokens into :class:`MeasurementResult` objects.

        Wire format per measurement::

            M<id>,<sensor_id>,V<value>,D<decision>

        Example input tokens::

            ['M00', '00', 'V151290', 'D0', 'M01', '01', 'V18520', 'D0']
        """
        results = []
        it = iter(parts)
        for token in it:
            token = token.strip()
            if not token.startswith("M"):
                continue
            try:
                mid = int(token[1:])
            except ValueError:
                continue

            # sensor_id token (e.g. "00")
            sensor_id_raw = next(it, "00").strip()
            try:
                sensor_id = int(sensor_id_raw)
            except ValueError:
                sensor_id = 0

            # value token e.g. "V151290" or "VINVALID"
            raw_v = next(it, "VINVALID").strip()
            value_str = raw_v[1:] if raw_v.upper().startswith("V") else raw_v
            value_valid = value_str.upper() != self.INVALID_MARKER
            value: Optional[float] = None
            if value_valid:
                try:
                    value = int(value_str) / 1000.0
                except ValueError:
                    value_valid = False

            # decision token e.g. "D0"
            raw_d = next(it, "D0").strip()
            d_str = raw_d[1:] if raw_d.upper().startswith("D") else raw_d
            try:
                decision_raw = int(d_str)
            except ValueError:
                decision_raw = 0

            results.append(MeasurementResult(
                id=mid,
                sensor_id=sensor_id,
                value=value,
                decision=decision_raw,
                value_valid=value_valid,
                passed=bool(decision_raw & 0x01),
                anchor_valid=not bool(decision_raw & 0x02),
            ))
        return results

    def _async_listener_loop(self) -> None:
        """Background thread: read data-channel lines and dispatch to callback."""
        buf = b""
        term = self._terminator.encode("ascii")
        self._data_sock.settimeout(1.0)
        while not self._data_stop.is_set():
            try:
                chunk = self._data_sock.recv(4096)
                if not chunk:
                    self.logger.warning("Data async listener: connection closed by sensor.")
                    self._data_connected = False
                    break
                buf += chunk
                while term in buf:
                    line, buf = buf.split(term, 1)
                    text = line.decode("ascii").strip()
                    if text and self._data_callback:
                        try:
                            self._data_callback(text)
                        except Exception as e:
                            self.logger.error(f"Async data callback raised: {e}")
            except socket.timeout:
                continue
            except OSError as e:
                if not self._data_stop.is_set():
                    self.logger.error(f"Data listener socket error: {e}")
                break

    def _health_listener_loop(self) -> None:
        """Background thread: read health-channel messages and dispatch to callback."""
        buf = b""
        term = self._terminator.encode("ascii")
        self._health_sock.settimeout(1.0)
        while not self._health_stop.is_set():
            try:
                chunk = self._health_sock.recv(4096)
                if not chunk:
                    self.logger.warning("Health listener: connection closed by sensor.")
                    self._health_connected = False
                    break
                buf += chunk
                while term in buf:
                    line, buf = buf.split(term, 1)
                    text = line.decode("ascii").strip()
                    if text and self._health_callback:
                        try:
                            self._health_callback(text)
                        except Exception as e:
                            self.logger.error(f"Health callback raised: {e}")
            except socket.timeout:
                continue
            except OSError as e:
                if not self._health_stop.is_set():
                    self.logger.error(f"Health listener socket error: {e}")
                break


def example_usage():
    """Demonstrates typical Gocator 6.5 ASCII protocol usage."""
    IP = "127.0.0.1"  # Change to your sensor's IP address

    api = LMISensorApi(ip_address=IP, control_port=8190, data_port=8190, health_port=8190)  # default ports: 3190 / 3192 / 3194

    # --- Connect (opens all three channels) ---
    print("Connecting...")
    api.connect()
    print(f"  Command channel : {api.is_connected}")
    print(f"  Data channel    : {api._data_connected}")
    print(f"  Health channel  : {api._health_connected}")

    # --- Load a job and start ---
    reply = api.load_job()
    print(f"\nLoadJob reply: {reply}")
    api.start()

    # --- Polling mode: software trigger + read ---
    print("\n-- Polling --")
    # api.trigger()

    fmt = api.get_formatted_result()                # result in sensor's custom format
    print(f"  Custom format result: {fmt}")

    raw = api.get_result(4, 1, 3)                   # raw standard-format tokens
    print(f"  Raw result          : {raw}")

    measurements = api.get_measurements(4, 1, 3)          # parsed standard format
    for m in measurements:
        print(
            f"  M{m.id}[sensor {m.sensor_id}]: "
            f"value={m.value}  passed={m.passed}  "
            f"value_valid={m.value_valid}  anchor_valid={m.anchor_valid}"
        )

    # --- Stamp ---
    stamp = api.get_stamp()
    print(f"\n-- Stamp: {stamp}")
    frame_only = api.get_stamp("frame")
    print(f"  Frame only: {frame_only}")

    # --- Query current job ---
    current_job = api.load_job()
    print(f"\n-- Current job: {current_job}")

    # --- Async data listener ---
    print("\n-- Async data listener (2 s) --")

    def on_data(line: str):
        print(f"  [DATA] {line}")

    api.set_async_callback(on_data)
    api.start_async_listener()

    # --- Health listener ---
    def on_health(line: str):
        print(f"  [HEALTH] {line}")

    api.set_health_callback(on_health)
    api.start_health_listener()

    import time
    time.sleep(2)

    # --- Stop and disconnect ---
    api.stop()
    api.disconnect()
    print("\nDone.")


if __name__ == "__main__":
    example_usage()
