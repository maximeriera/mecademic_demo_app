from typing import Dict
from threading import Thread
import time

from devices import Device
from devices import MecaRobot

JOINT_VEL = 15
CARTLIN_VEL = 100
CARTANG_VEL = 45

PARTS_PER_TRAIL = [5, 6, 9, 10]
# PARTS_PER_TRAIL = range(16)
TRAILS_OFFSET = 20

def prod_cycle(devices: Dict[str, Device], index:int):
    """Logic for PROD task."""
    
    meca_robot_1:MecaRobot = devices["meca_robot_1"]
    
    meca_robot_1.api.MoveJoints(40, 0, 0, 0, 0, 0)
    meca_robot_1.api.MoveJoints(-40, 0, 0, 0, 0, 0)

    meca_robot_1.logger.info("Robot waiting for completion...")

    meca_robot_1.api.WaitIdle()
    
    meca_robot_1.logger.info("Moving complete.")
    
    return



