from devices import Device
from typing import Dict

from devices.MecaRobot import MecaRobot

def home(devices: Dict[str, Device]):
    """Logic for HOME task."""
    meca_robot_1:MecaRobot = devices["meca_robot_1"]
    
    meca_robot_1.api.MoveJoints(0, 0, 0, 0, 0, 0)

    meca_robot_1.logger.info("Robot waiting for completion...")

    meca_robot_1.api.WaitIdle()
    
    meca_robot_1.logger.info("Moving to home position complete.")
    
    return