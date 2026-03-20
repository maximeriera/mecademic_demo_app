from .Device import Device
from .api.IoLogikE1212Api import IoLogikE1212Api


class IoLogikE1212(Device):
    """
    Device wrapper for the Moxa ioLogik E1212 remote I/O controller.

    This class implements the :class:`~devices.Device` interface (lifecycle,
    status properties) and exposes all Modbus operations through the
    :attr:`api` attribute, which is an :class:`~devices.api.IoLogikE1212Api`
    instance.

    Parameters
    ----------
    ip_address : str
        IP address of the ioLogik E1212.
    port : int
        Modbus TCP port (default: 502).
    slave_id : int
        Modbus slave / unit ID (default: 1).
    name : str, optional
        Custom device identifier used for logging; auto-generated if omitted.

    Example
    -------
    >>> io = IoLogikE1212("192.168.0.100")
    >>> io.initialize()
    >>> io.api.write_do(0, True)
    >>> states = io.api.read_all_di()
    """

    def __init__(self, ip_address: str, port: int = 502, slave_id: int = 1, name=None):
        if name is None:
            name = f"IoLogikE1212_{ip_address}"
        super().__init__(device_id=name)

        self._ip_address = ip_address
        self._port = port
        self._api = IoLogikE1212Api(
            ip_address=ip_address,
            port=port,
            slave_id=slave_id,
            logger=self.logger,
        )

    # ------------------------------------------------------------------
    # Device interface
    # ------------------------------------------------------------------

    @property
    def api(self) -> IoLogikE1212Api:
        """The :class:`IoLogikE1212Api` instance exposing all Modbus methods."""
        return self._api

    @property
    def info(self) -> dict:
        try:
            model = self._api.get_model_name()
            fw    = self._api.get_firmware_version()
            ip    = self._api.get_lan_ip()
        except Exception:
            model, fw, ip = None, None, None
        return {
            "ip_address":       self._ip_address,
            "port":             self._port,
            "model_name":       model,
            "firmware_version": fw,
            "lan_ip":           ip,
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

    def shutdown(self):
        self._api.disconnect()

    def clear_fault(self):
        self._api._faulted = False
        self.logger.info("Fault flag cleared on ioLogik E1212.")

    def abort(self):
        self.logger.warning(f"[{self.device_id}] Abort requested (no motion to clear).")
