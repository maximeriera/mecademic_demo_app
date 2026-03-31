from devices import Device
from typing import Dict

from devices import IoLogikE1212

def home(devices: Dict[str, Device]):
    """Logic for HOME task."""
    io:IoLogikE1212 = devices["remote_io"]
    
    io.api.write_do(0, True)  # Start signal for the trail
    return