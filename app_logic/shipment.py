import time

from devices import Device
from typing import Dict


from devices import MecaRobot

def shipment(devices: Dict[str, Device]):
    """Logic for SHIPMENT task."""
    time.sleep(1)
    return