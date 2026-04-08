from .Device import Device
from .api.BOASpotApi import BOASpotApi

from typing import Optional


class BOASpot(Device):
    """
    Device wrapper for Teledyne BOA Spot smart cameras.

    Implements the :class:`~devices.Device` interface and exposes the BOA Spot
    TCP/IP stream protocol through the :attr:`api` attribute.

    The BOA Spot uses a simple TCP/IP stream for communication.  The default
    command/control port is **5024** (pre-configured on the camera).  An
    optional second port (e.g. **5025**) can be configured in the camera's
    *Setup Connections* panel for inspection result output.

    Communication overview
    ----------------------
    * **Command channel** (port 5024) — send triggers, solution changes, and
      arbitrary strings; read camera responses.
    * **Result channel** (port 5025, optional) — receive inspection result
      strings pushed by the camera's *Post Processing* script via
      ``WriteString`` / ``WriteFormatString``.

    Parameters
    ----------
    ip_address : str
        IP address of the BOA Spot camera (default ``192.168.0.100``).
    port : int
        Command channel TCP port (default 5024).
    result_port : int, optional
        Result channel TCP port.  Pass ``None`` to skip opening a result
        channel.
    terminator : str
        Line terminator used by the camera (default ``"\\r\\n"``).
    timeout : float
        Socket timeout in seconds (default 5.0).
    name : str, optional
        Custom device identifier used for logging; auto-generated if omitted.

    Example
    -------
    >>> cam = BOASpot("192.168.0.100")
    >>> cam.initialize()
    >>> cam.api.trigger()
    >>> result = cam.api.read_result()
    >>> print(result)

    References
    ----------
    *BOA Spot Communication Guide*, Teledyne Imaging, Version 1.9,
    Document 405-00061-00.
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.100",
        port: int = BOASpotApi.DEFAULT_PORT,
        result_port: Optional[int] = None,
        terminator: str = "\r\n",
        timeout: float = 5.0,
        name: Optional[str] = None,
    ):
        if name is None:
            name = f"BOASpot_{ip_address}"
        super().__init__(device_id=name)

        self._ip_address = ip_address
        self._port = port
        self._api = BOASpotApi(
            ip_address=ip_address,
            port=port,
            result_port=result_port,
            terminator=terminator,
            timeout=timeout,
            logger=self.logger,
        )

    # ------------------------------------------------------------------
    # Device interface
    # ------------------------------------------------------------------

    @property
    def api(self) -> BOASpotApi:
        """The :class:`BOASpotApi` instance exposing all TCP/IP stream methods."""
        return self._api

    @property
    def info(self) -> dict:
        return {
            "ip_address": self._ip_address,
            "port": self._port,
            "connected": self.connected,
        }

    @property
    def connected(self) -> bool:
        return self._api.is_connected

    @property
    def ready(self) -> bool:
        return self.connected and not self._api._faulted

    @property
    def faulted(self) -> bool:
        return self._api._faulted or not self.connected

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self):
        """Connect to the BOA Spot camera over TCP/IP.

        Opens the command channel (and the result channel if configured).
        The camera should already be powered on and running a solution.
        """
        self._api.connect()
        self.logger.info(f"BOA Spot at {self._ip_address} initialized.")

    def shutdown(self):
        """Disconnect from the BOA Spot camera."""
        self._api.disconnect()
        self.logger.info(f"BOA Spot at {self._ip_address} shut down.")

    def clear_fault(self):
        """Clear the internal fault flag."""
        self._api._faulted = False
        self.logger.info("Fault flag cleared on BOA Spot camera.")

    def abort(self):
        """Abort: stop the result listener if running."""
        self.logger.warning(f"[{self.device_id}] Abort: stopping BOA Spot listener.")
        try:
            self._api.stop_result_listener()
        except Exception:
            pass
