from devices import Device
from typing import Dict

from devices.MecaRobot import MecaRobot

def home(devices: Dict[str, Device]):
    """Logic for HOME task."""
    
    scara: MecaRobot = devices.get("scara")
    scara.api.MoveJoints(65, -145, -33, 80)
    
    return