from devices import Device
from typing import Dict

from devices.MecaRobot import MecaRobot

def shipment(devices: Dict[str, Device]):
    """Logic for SHIPMENT task."""
    meca_robot_1:MecaRobot = devices["meca_robot_1"]
    
    meca_robot_1.api.MoveJoints(0, -60, 60, 0, 30, 0)

    meca_robot_1.logger.info("Robot waiting for completion...")

    meca_robot_1.api.WaitIdle()
    
    meca_robot_1.logger.info("Moving to shipment position complete.")
    
    return