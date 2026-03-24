from devices import Device
from typing import Dict


from devices import IoLogikE1212

def shipment(devices: Dict[str, Device]):
    """Logic for SHIPMENT task."""
    io:IoLogikE1212 = devices["remote_io"]
    
    io.api.write_do(0, False)  # Start signal for the trail
    return