from devices import Device
from typing import Dict

from devices.MecaRobot import MecaRobot

def home(devices: Dict[str, Device]):
    """Logic for HOME task."""
    
    meca: MecaRobot = devices.get("Meca500")
    meca.api.MoveJoints(0, 0, 0, 0, 0, 0)
    meca.api.WaitIdle()
    
    return