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

    supports_reconnect = True

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
        # Read once per connection: /api/info polls this every second.
        self._identity = {"model_name": None, "firmware_version": None, "lan_ip": None}

    # ------------------------------------------------------------------
    # Device interface
    # ------------------------------------------------------------------

    @property
    def api(self) -> IoLogikE1212Api:
        """The :class:`IoLogikE1212Api` instance exposing all Modbus methods."""
        return self._api

    @property
    def info(self) -> dict:
        return {"ip_address": self._ip_address, "port": self._port, **self._identity}

    @property
    def connected(self) -> bool:
        return self._api.is_connected

    @property
    def ready(self) -> bool:
        return not self.api._faulted and self.api.is_connected

    @property
    def faulted(self) -> bool:
        return self._api._faulted or not self._api.is_connected

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self):
        self._api.connect()
        try:
            self._identity = {
                "model_name": self._api.get_model_name(),
                "firmware_version": self._api.get_firmware_version(),
                "lan_ip": self._api.get_lan_ip(),
            }
        except Exception as e:
            self.logger.warning(f"[{self.device_id}] Could not read the unit identity: {e}")

    def shutdown(self):
        self._api.disconnect()

    def clear_fault(self):
        self._api._faulted = False
        self.logger.info("Fault flag cleared on ioLogik E1212.")

    def abort(self):
        self.logger.warning(f"[{self.device_id}] Abort requested (no motion to clear).")

    # ------------------------------------------------------------------
    # Live I/O (Devices tab)
    # ------------------------------------------------------------------

    def io_points(self) -> list:
        if not self._api.is_connected:
            return []
        inputs = self._api.read_all_di()
        outputs = self._api.read_all_do()
        return ([{"name": f"di{i}", "kind": "di", "value": bool(v), "writable": False}
                 for i, v in enumerate(inputs)]
                + [{"name": f"do{i}", "kind": "do", "value": bool(v), "writable": True}
                   for i, v in enumerate(outputs)])

    def write_io(self, name: str, value) -> None:
        if not name.startswith("do") or not name[2:].isdigit():
            raise ValueError(f"Unknown or read-only point '{name}'.")
        self._api.write_do(int(name[2:]), bool(value))
