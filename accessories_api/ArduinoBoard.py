from Device import Device

import pyfirmata

class ArduinoBoard(Device):
    def __init__(self, port):
        super().__init__(device_id=f"ArduinoBoard_{port}")
        self._connected = False
        self._ready = False
        self._faulted = False
        self._api: pyfirmata.Arduino | None = None
        self._port = port
    
    @property
    def api(self):
        return self._api
    
    @property
    def connected(self):
        self._connected = self._api is not None
        return self._connected

    @property
    def ready(self):
        self._ready = self.connected
        return self._ready
    
    @property
    def faulted(self):
        self._faulted = not self.connected
        return self._faulted
    
    @property
    def info(self):
        if self.connected:
            return {
                "port": self._port,
                "firmware": self._api.firmware,
                "firmware_version": self._api.firmware_version
            }
        else:
            return {
                "port": self._port
            }
    
    def initialize(self):
        try:
            self.api = pyfirmata.Arduino(self._port)
            self.logger.info("ArduinoBoard initialized.")
        except Exception as e:
            self.logger.error(f"Failed to initialize ArduinoBoard on port {self._port}: {e}")
            raise e
    
    def shutdown(self):
        if self.api:
            self.api.exit()
            self.api = None
        self.logger.info("ArduinoBoard shut down.")
        
    def isFaulted(self):
        return self._api is None
    
    def set_digital_pin(self, pin_number, value):
        if self._api:
            pin = self._api.get_pin(f'd:{pin_number}:o')  # Digital output
            pin.write(value)
        else:
            raise Exception("ArduinoBoard API not initialized.")