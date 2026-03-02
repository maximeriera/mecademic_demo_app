from devices import Device
from typing import Dict

from devices.MecaRobot import MecaRobot

def home(devices: Dict[str, Device]):
    """Logic for HOME task."""
    scara:MecaRobot = devices["scara"]
    trail:MecaRobot = devices["meca_trail"]
    dispenser:MecaRobot = devices["meca_dispenser"]

    scara.logger.info("Moving to home position...")
    trail.logger.info("Moving to home position...")
    dispenser.logger.info("Moving to home position...")

    scara.api.SetJointVel(40)
    trail.api.SetJointVel(40)
    dispenser.api.SetJointVel(40)

    trail.api.SetGripperRange(5, 20)
    dispenser.api.SetGripperRange(5, 20)

    dispenser.api.GripperClose()
    dispenser.api.Delay(1)
    
    scara.api.MoveJoints(65, -145, -33, 80)
    trail.api.MoveJoints(0, -40, 10, 0, 30, 0)
    dispenser.api.MoveJoints(55.088809, 32.327692, 25.046302, 98.522379, 84.580575, -148.178091)

    trail.api.GripperOpen()
    dispenser.api.GripperOpen()
    
    scara.logger.info("Robot waiting for completion...")
    trail.logger.info("Robot waiting for completion...")
    dispenser.logger.info("Robot waiting for completion...")

    scara.api.WaitIdle()
    trail.api.WaitIdle()
    dispenser.api.WaitIdle()
    
    scara.logger.info("Moving to home position complete.")
    trail.logger.info("Moving to home position complete.")
    dispenser.logger.info("Moving to home position complete.")
    
    return