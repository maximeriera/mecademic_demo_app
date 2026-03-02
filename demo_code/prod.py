from devices import Device
from typing import Dict

import mecademicpy.robot as mdr
import devices.api.AsyrilAPI as asyril_api_module

from typing import Tuple, Any

import time

from devices.MecaRobot import MecaRobot

JOINT_VEL = 15
CARTLIN_VEL = 100
CARTANG_VEL = 45

PARTS_PER_TRAIL = 16
TRAILS_OFFSET = 20

def prod_cycle(devices: Dict[str, Device]):
    
    meca: MecaRobot = devices.get("Meca500")
    meca.api.MoveJoints(10, 0, 0, 0, 0, 0)
    meca.api.MoveJoints(-10, 0, 0, 0, 0, 0)
    meca.api.WaitIdle()
    
    return
    
    def scara_trail(trail_id:int, scara:mdr.Robot, asyril:asyril_api_module.AsyrilEyePlusApi) -> None:
        for i in range(PARTS_PER_TRAIL):
            pose = asyril.get_part()
            if pose['resp'] == 200:
                # print(f"Part detected at position X: {pose['x']}, Y: {pose['y']}")
                scara.SetVariable(name='PickPose.x', value=pose['x'])
                scara.SetVariable(name='PickPose.y', value=pose['y'])
            else:
                return
            
            pose_x = (i // 4) * TRAILS_OFFSET
            pose_y = (i % 4) * TRAILS_OFFSET
            
            scara.SetVariable(name='PlacePose.x', value=pose_x)
            scara.SetVariable(name='PlacePose.y', value=pose_y)
            
            scara.WaitIdle()
            time.sleep(1)
            scara.StartProgram('pick')
            
            scara.WaitIdle()
            time.sleep(1)
            scara.StartProgram('place' + str(trail_id))
    
    """Logic for PROD task."""
    
    asyril:asyril_api_module.AsyrilEyePlusApi = accessory_apis[0]  # Assuming only one Asyril accessory for this demo
    scara:mdr.Robot = robot_apis[0]  # Assuming only one SCARA robot for this demo
    
    scara_trail(trail_id=1, scara=scara, asyril=asyril)