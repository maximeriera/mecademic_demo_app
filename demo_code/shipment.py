from devices import Device
from typing import Dict

from devices.MecaRobot import MecaRobot

def shipment(devices: Dict[str, Device]):
    """Logic for SHIPMENT task."""
    meca: MecaRobot = devices.get("Meca500")
    meca.api.MoveJoints(0, -60, 60, 0, 30, 0)
    meca.api.WaitIdle()
    
    return
