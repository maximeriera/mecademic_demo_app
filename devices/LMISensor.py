from .Device import Device
from .api.LMISensorApi import LMISensorApi


class LMISensor(Device):
    """
    Device wrapper for LMI Gocator sensors (firmware 6.5.x).

    Implements the :class:`~devices.Device` interface and exposes the full
    Gocator 6.5 Ethernet ASCII protocol through the :attr:`api` attribute.

    The Gocator 6.5 ASCII protocol uses **three separate TCP channels**:

    * **Command channel** (default port 3190) – start/stop/trigger/stamp/align
    * **Data channel** (default port 3192) – result polling / async push
    * **Health channel** (default port 3194) – async health indicator stream

    Parameters
    ----------
    ip_address : str
        IP address of the sensor.
    control_port : int
        Command channel TCP port (default 3190).
    data_port : int
        Data channel TCP port (default 3192).
    health_port : int
        Health channel TCP port (default 3194).
    delimiter : str
        Field separator in commands and replies (default ``','``).
    terminator : str
        Command / line terminator (default ``'\\r\\n'``).
    timeout : float
        Socket receive timeout in seconds (default 5.0).
    name : str, optional
        Custom device identifier used for logging; auto-generated if omitted.

    Example
    -------
    >>> sensor = LMISensor("192.168.1.10")
    >>> sensor.initialize()
    >>> sensor.api.trigger()
    >>> results = sensor.api.get_measurements(0, 1)
    >>> for m in results:
    ...     print(m.id, m.sensor_id, m.value, m.passed)
    """

    def __init__(
        self,
        ip_address: str,
        control_port: int = LMISensorApi.DEFAULT_CONTROL_PORT,
        data_port: int = LMISensorApi.DEFAULT_DATA_PORT,
        health_port: int = LMISensorApi.DEFAULT_HEALTH_PORT,
        delimiter: str = ",",
        terminator: str = "\r\n",
        timeout: float = 5.0,
        name: str = None,
    ):
        if name is None:
            name = f"LMISensor_{ip_address}"
        super().__init__(device_id=name)

        self._ip_address = ip_address
        self._control_port = control_port
        self._api = LMISensorApi(
            ip_address=ip_address,
            control_port=control_port,
            data_port=data_port,
            health_port=health_port,
            delimiter=delimiter,
            terminator=terminator,
            timeout=timeout,
            logger=self.logger,
        )

    # ------------------------------------------------------------------
    # Device interface
    # ------------------------------------------------------------------

    @property
    def api(self) -> LMISensorApi:
        """The :class:`LMISensorApi` instance exposing all ASCII protocol methods."""
        return self._api

    @property
    def info(self) -> dict:
        return {
            "ip_address": self._ip_address,
            "control_port": self._control_port,
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
        return self._api._faulted

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self):
        self._api.connect()
        self._api.start()

    def shutdown(self):
        try:
            self._api.stop()
        except Exception:
            pass
        self._api.disconnect()

    def clear_fault(self):
        self._api._faulted = False
        self.logger.info("Fault flag cleared on LMI sensor.")

    def abort(self):
        self.logger.warning(f"[{self.device_id}] Abort: stopping sensor.")
        try:
            self._api.stop()
        except Exception:
            pass
