from Accessory import Accessory

import pyfirmata

class ArduinoBoard(Accessory):
    def __init__(self, port):
        self.api = None
        self.port = port
    
    def initialize(self):
        try:
            self.api = pyfirmata.Arduino(self.port)
            print("ArduinoBoard initialized.")
        except Exception as e:
            print(f"Failed to initialize ArduinoBoard on port {self.port}: {e}")
            raise e
    
    def shutdown(self):
        if self.api:
            self.api.exit()
            self.api = None
        print("ArduinoBoard shut down.")
        
    def isFaulted(self):
        return self.api is None
    
    def set_digital_pin(self, pin_number, value):
        if self.api:
            pin = self.api.get_pin(f'd:{pin_number}:o')  # Digital output
            pin.write(value)
        else:
            raise Exception("ArduinoBoard API not initialized.")