import time

from devices import Device
from typing import Dict

from devices import MecaRobot

def home(devices: Dict[str, Device]):
    """Logic for HOME task."""
    time.sleep(1)
    return