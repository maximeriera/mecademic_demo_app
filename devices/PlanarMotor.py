from .Device import Device
from .api.PlanarMotorApi import PlanarMotorApi

class PlanarMotor(Device):
    def __init__(self, ip_address:str = '192.168.10.200', name: str = None):
        if name is None:
            name = f"PlanarMotor_{ip_address}"
        super().__init__(device_id=name)
        self._ip_address = ip_address
        self._api = PlanarMotorApi(ip=ip_address)
        
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
        self._faulted = self._api._faulted
        return self._faulted
    
    @property
    def connected(self):
        self._connected = self._api._connected
        return self._connected
    
    def initialize(self):
        try:
            connected = self.api.connect()
            if not connected:
                raise Exception("Failed to connect to Planar Motor system.")    
            self.api.initialize()
            self.api.activate_bots()
            
        except Exception as e:
            self.logger.error(f"Failed to initialize PlanarMotor: {e}")
            raise e
        
    def shutdown(self):
        if self.api:
            self.api.shutdown()
            self.api = None
        self.logger.info("PlanarMotor shut down.")

    def clear_fault(self):
        self.logger.info("Clearing faults on PlanarMotor.")
        # Placeholder: implement device-specific fault clearing if supported
        pass

    def abort(self):
        self.logger.warning(f"[{self.device_id}] Aborting: stopping all movers.")
        try:
            self._api.stop_all()
        except Exception as e:
            self.logger.error(f"[{self.device_id}] Error during abort: {e}")

    def isFaulted(self):
        if not self.api.is_connected:
            return True
        return self.api.get_pmc_status() == pmc_types.PMCSTATUS.PMC_ERROR
