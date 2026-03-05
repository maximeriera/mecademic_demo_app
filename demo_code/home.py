from devices import Device
from typing import Dict

from devices.MecaRobot import MecaRobot
from devices.PlanarMotor import PlanarMotor

def home(devices: Dict[str, Device]):
    """Logic for HOME task."""
    dispenser:MecaRobot = devices["meca_500_1"]
    nester:MecaRobot = devices["meca_500_2"]
    changer:MecaRobot = devices["meca_500_3"]
    
    planar_motor:PlanarMotor = devices["planar_motor_1"]

    dispenser.api.MoveJoints(0, 0, 0, 0, 0, 0)

    nester.api.GripperOpen()
    nester.api.MoveJoints(90, -20, 65, 0, -45, 0)

    changer.api.GripperOpen()
    changer.api.MoveJoints(0, 0, 0, 0, 0, 0)

    changer.api.WaitIdle()
    nester.api.WaitIdle()
    dispenser.api.WaitIdle()
    
    num_bot = 4
    planar_motor.api.send_auto_move_command(num_bot=num_bot, xbot_ids= [1,2,3,4], y_pos=[0.120]*num_bot, x_pos=[0.120 + i*0.240 for i in range(num_bot)])
    planar_motor.api.wait_multiple_move_done([1,2,3,4])
    
    return