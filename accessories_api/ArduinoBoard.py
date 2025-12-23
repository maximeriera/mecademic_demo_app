from Accessory import Accessory

import pyfirmata
import logging

class ArduinoBoard(Accessory):
    def __init__(self, port):
        self.api = None
        self.port = port
        self.logger = logging.getLogger(__name__)
    
    def initialize(self):
        try:
            self.api = pyfirmata.Arduino(self.port)
            self.logger.info("ArduinoBoard initialized.")
        except Exception as e:
            self.logger.error(f"Failed to initialize ArduinoBoard on port {self.port}: {e}")
            raise e
    
    def shutdown(self):
        if self.api:
            self.api.exit()
            self.api = None
        self.logger.info("ArduinoBoard shut down.")
        
    def isFaulted(self):
        return self.api is None
    
    def set_digital_pin(self, pin_number, value):
        if self.api:
            pin = self.api.get_pin(f'd:{pin_number}:o')  # Digital output
            pin.write(value)
        else:
            raise Exception("ArduinoBoard API not initialized.")