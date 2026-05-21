from .Device import Device
from .api.PlanarMotorApi import PlanarMotorApi


class PlanarMotor(Device):
    def __init__(self, ip_address: str = '192.168.10.200', name: str = None):
        if name is None:
            name = f"PlanarMotor_{ip_address}"
        super().__init__(device_id=name)
        self._ip_address = ip_address
        self._api = PlanarMotorApi(ip=ip_address)
        self._connected = False
        self._faulted = False
        
    @property
    def info(self):
        return {
            "ip_address": self._ip_address,
            }
        
    @property
    def api(self):
        return self._api
    
    @property
    def ready(self):
        return self.connected and not self.faulted
    
    @property
    def faulted(self):
        if self._api is None:
            return True
        self._faulted = self._api.faulted or not self._api.is_connected
        return self._faulted
    
    @property
    def connected(self):
        if self._api is None:
            return False
        self._connected = self._api.is_connected
        return self._connected
    
    def initialize(self):
        try:
            self.api.clear_abort()
            connected = self.api.connect()
            if not connected:
                raise Exception("Failed to connect to Planar Motor system.")    
            self.api.initialize()
            self._connected = True
            self._faulted = False
            
        except Exception as e:
            self._faulted = True
            self.logger.error(f"Failed to initialize PlanarMotor: {e}")
            raise e
        
    def shutdown(self):
        if self._api:
            self._api.shutdown()
        self._connected = False
        self._faulted = False
        self.logger.info("PlanarMotor shut down.")
        
    def isFaulted(self):
        return self.faulted

    def clear_fault(self):
        if self._api is None:
            return
        self._api.clear_fault()
        self._faulted = self._api.faulted

    def abort(self):
        if self._api is None:
            return
        self.logger.warning(f"[{self.device_id}] Abort requested for planar motor.")
        self._api.abort()
