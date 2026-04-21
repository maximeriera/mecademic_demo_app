from .Device import Device

from zaber_motion import Library
from zaber_motion import Units
from zaber_motion.ascii import Connection


class ZaberAxis(Device):
    def __init__(self, port: str, axis_number: int = 1, name: str = None):
        if name is None:
            name = f"ZaberAxis_{port}"
        super().__init__(device_id=name)

        self._port = port
        self._axis_number = axis_number
        self._api = None
        self._axis = None
        self._device_list = []
        self._faulted = False

    @property
    def info(self):
        return {
            "port": self._port,
            "axis_number": self._axis_number,
            "connected": self.connected,
            "faulted": self.faulted,
        }

    @property
    def connected(self):
        return self._api is not None and self._axis is not None

    @property
    def ready(self):
        return self.connected and not self._faulted

    @property
    def faulted(self):
        return self._faulted or not self.connected

    @property
    def api(self):
        return self._api

    def initialize(self):
        try:
            Library.enable_device_db_store()
            self._api = Connection.open_serial_port(self._port)
            self._device_list = self._api.detect_devices()
            if not self._device_list:
                raise RuntimeError(f"No Zaber devices detected on port {self._port}.")

            self._axis = self._device_list[0].get_axis(self._axis_number)
            self._axis.home()
            self._axis.wait_until_idle()
            self._faulted = False
            self.logger.info(f"ZaberAxis initialized on port {self._port}.")
        except Exception as e:
            self._faulted = True
            self.logger.error(f"Failed to initialize ZaberAxis on port {self._port}: {e}")
            self.shutdown()
            raise

    def shutdown(self):
        if self._api:
            try:
                self._api.close()
            finally:
                self._api = None
                self._axis = None
                self._device_list = []
        self.logger.info("ZaberAxis shut down.")

    def clear_fault(self):
        self.logger.info("Clearing faults on ZaberAxis.")
        self._faulted = False
        if not self.connected:
            self.initialize()

    def abort(self):
        self.logger.warning(f"[{self.device_id}] Abort requested.")
        if not self._axis:
            return
        try:
            self._axis.stop()
            self._axis.wait_until_idle()
        except Exception as e:
            self._faulted = True
            self.logger.error(f"Failed to abort ZaberAxis motion: {e}")
            raise

    def move_axis(self, position: float, speed: float = 250):
        if not self.ready:
            raise RuntimeError("ZaberAxis is not ready.")

        self._axis.settings.set(
            "maxspeed", speed, Units.VELOCITY_MILLIMETRES_PER_SECOND
        )
        self._axis.move_absolute(position, Units.LENGTH_MILLIMETRES)
        self._axis.wait_until_idle()