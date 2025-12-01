from Accessory import Accessory

from zaber_motion import Library
from zaber_motion import DeviceDbSourceType
from zaber_motion.ascii import Connection
from zaber_motion import Units

class ZaberAxis(Accessory):
    def __init__(self, port):
        self.api = None
        self.port = port
    
    def initialize(self):
        self.api = Connection.open_serial_port(self.port)
        self.device_list = self.api.detect_devices()
        self.axis = self.device_list[0].get_axis(1)
        
        self.axis.home()
        self.axis.wait_until_idle()
    
    def shutdown(self):
        if self.api:
            self.api.close()
            self.api = None
            self.axis = None
        print("ZaberAxis shut down.")
        
    def isFaulted(self):
        return self.api is None
    
    def move_axis(self, position):
        self.axis.settings.set(
            "maxspeed", 250, Units.VELOCITY_MILLIMETRES_PER_SECOND)
        self.axis.move_absolute(position, Units.LENGTH_MILLIMETRES)
        self.axis.wait_until_idle()