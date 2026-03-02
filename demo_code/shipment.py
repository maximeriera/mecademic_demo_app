from devices import Device
from typing import Dict

from devices.MecaRobot import MecaRobot

def shipment(devices: Dict[str, Device]):
    """Logic for SHIPMENT task."""
    scara:MecaRobot = devices["scara"]
    trail:MecaRobot = devices["meca_trail"]
    dispenser:MecaRobot = devices["meca_dispenser"]

    scara.logger.info("Moving to shipment position...")
    trail.logger.info("Moving to shipment position...")
    dispenser.logger.info("Moving to shipment position...")
    
    scara.api.SetJointVel(40)
    trail.api.SetJointVel(40)
    dispenser.api.SetJointVel(40)

    dispenser.api.GripperClose()
    dispenser.api.Delay(1)

    scara.api.MoveJoints(65, -145, -33, 80)
    trail.api.MoveJoints(0, 30, 60, 0, 0, 0)
    dispenser.api.MoveJoints(0, -60, 60, 0, 30, 0)
    
    scara.logger.info("Robot waiting for completion...")
    trail.logger.info("Robot waiting for completion...")
    dispenser.logger.info("Robot waiting for completion...")

    scara.api.WaitIdle()
    trail.api.WaitIdle()
    dispenser.api.WaitIdle()
    
    scara.logger.info("Moving to shipment position complete.")
    trail.logger.info("Moving to shipment position complete.")
    dispenser.logger.info("Moving to shipment position complete.")
    
    return
