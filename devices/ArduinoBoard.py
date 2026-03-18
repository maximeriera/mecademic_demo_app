from .Device import Device

import pyfirmata2 as pyfirmata

class ArduinoBoard(Device):
    def __init__(self, port, name=None):
        if name is None:
            name = f"ArduinoBoard_{port}"
        super().__init__(device_id=name)
        self._connected = False
        self._ready = False
        self._faulted = False
        self._api: pyfirmata.Arduino | None = None
        self._port = port
    
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
    def api(self) -> pyfirmata.Arduino | None:
        return self._api
    
    def initialize(self):
        try:
            self._api = pyfirmata.Arduino(self._port)
            self.logger.info("ArduinoBoard initialized.")
        except Exception as e:
            self.logger.error(f"Failed to initialize ArduinoBoard on port {self._port}: {e}")
            raise e
    
    def shutdown(self):
        if self._api:
            self._api.exit()
            self._api = None
        self.logger.info("ArduinoBoard shut down.")
        
    def clear_fault(self):
        self.logger.info("Clearing faults on ArduinoBoard.")
        # For Arduino, we can attempt to reinitialize the connection
        try:
            self.shutdown()
            self.initialize()
        except Exception as e:
            self.logger.error(f"Failed to clear faults on ArduinoBoard: {e}")
            raise e
        
    def abort(self):
        return super().abort()
    
    def set_digital_pin(self, pin_number, value):
        if self._api:
            pin = self._api.get_pin(f'd:{pin_number}:o')  # Digital output
            pin.write(value)
        else:
            raise Exception("ArduinoBoard API not initialized.")