from devices import Device
from typing import Dict

from devices.MecaRobot import MecaRobot

def home(devices: Dict[str, Device]):
    """Logic for HOME task."""
    mirror:MecaRobot = devices["mirror_robot"]
    dispenser:MecaRobot = devices["dispenser_robot"]

    JOINT_VEL = 30

    mirror.api.SetJointVel(JOINT_VEL + 10)
    dispenser.api.SetJointVel(JOINT_VEL)

    mirror.api.MoveJoints(-3.53824, 38.97176, 20.74122, -4.09573, -59.77675, 2.0643)
    dispenser.api.MoveJoints(9.83462, -1.54648, 25.02203, -48.45816, 31.12531, 116.25669)

    mirror.api.WaitIdle()
    dispenser.api.WaitIdle()
    
    return