from .Device import Device

from .api.AsyrilAPI import AsyrilEyePlusApi

class AsyrilEyePlus(Device):
    def __init__(self, ip_address: str, recipe:int, port: int = 7171, name=None):
        if name is None:
            name = f"AsyrilEyePlus_{ip_address}"
        super().__init__(device_id=name)
        self._ip_address = ip_address
        self._port = port
        
        self._api = AsyrilEyePlusApi(logger=self.logger, ip_address=ip_address, recipe=recipe, port=port)

        
    @property
    def info(self):
        return {
            "ip_address": self._ip_address,
            "port": self._port,
            "recipe": self._api.recipe
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
            self.api.connect()
            self.api.start_production()
            self.api.set_part_timeout()
        except Exception as e:
            self._faulted = True
            print(f"Failed to initialize: {e}")

    def shutdown(self):
        try:
            self.api.stop_production()
            self.api.disconnect()
        except Exception as e:
            self._faulted = True
            print(f"Failed to shutdown: {e}")
    
    def clear_fault(self):
        pass
    
def example_usage():
    print("Initializing Asyril Eye Plus...")
    eye_plus = AsyrilEyePlus(ip_address="192.168.0.50", recipe=63083)
    print("Initializing device...")
    eye_plus.initialize()
    print(eye_plus.api.get_part_timeout())
    print(f"Connected: {eye_plus._api._connected}")
    print(f"Ready: {eye_plus.ready}")
    pose = eye_plus.api.get_part()
    print(f"Part pose: {pose}")
    eye_plus.api.stop_production()
    
if __name__ == "__main__":
    example_usage()