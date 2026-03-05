from devices import Device
from typing import Dict

from devices.MecaRobot import MecaRobot

def shipment(devices: Dict[str, Device]):
    """Logic for SHIPMENT task."""
    dispenser:MecaRobot = devices["meca_500_1"]
    nester:MecaRobot = devices["meca_500_2"]
    changer:MecaRobot = devices["meca_500_3"]
    
    
    dispenser.api.MoveJoints(0, -60, 40, 0, 20, 0)

    nester.api.GripperOpen()
    nester.api.MoveJoints(90, -20, 60, 0, 30, 0)

    changer.api.GripperOpen()
    changer.api.MoveJoints(0, -40, 40, 0, 40, 0)

    changer.api.WaitIdle()
    nester.api.WaitIdle()
    dispenser.api.WaitIdle()

    return
