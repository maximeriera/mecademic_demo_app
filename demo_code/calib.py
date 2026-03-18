from typing import Dict

from devices import Device

from devices import MecaRobot

def calib(devices: Dict[str, Device]):
    """Logic for PROD task."""

    meca_robot_1:MecaRobot = devices["meca_robot_1"]
    
    meca_robot_1.api.Home()

    meca_robot_1.logger.info("Robot waiting for homing completion...")

    meca_robot_1.api.WaitHomed()
    
    meca_robot_1.logger.info("Homing complete.")
    
    return