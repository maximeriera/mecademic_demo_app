from devices import Device
from typing import Dict

from devices.MecaRobot import MecaRobot

def shipment(devices: Dict[str, Device]):
    """Logic for SHIPMENT task."""
    mirror:MecaRobot = devices["mirror_robot"]
    dispenser:MecaRobot = devices["dispenser_robot"]

    JOINT_VEL = 30

    mirror.api.SetJointVel(JOINT_VEL + 10)
    dispenser.api.SetJointVel(JOINT_VEL)

    mirror.api.MoveJoints(0, -60, 60, 90, 0, 0)
    dispenser.api.MoveJoints(0, -60, 60, 90, 0, -45)

    mirror.api.WaitIdle()
    dispenser.api.WaitIdle()
    return