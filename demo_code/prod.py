import mecademicpy.robot as mdr
import pyfirmata
import accessories_api.PlanarMotor as pmp

from typing import Tuple, Any

JOINT_VEL = 15
CARTLIN_VEL = 100
CARTANG_VEL = 45

def prod_cycle(robot_apis: Tuple[mdr.Robot], accessory_apis: Tuple[Any]):
    """Logic for PROD task."""
    
    planar_motor:pmp.PlanarMotor = accessory_apis[0]
    num_bot = 4
    
    def swap_nearest_2_positions(planarmotor:pmp.PlanarMotor, xlist, ylist):
        pos1_bot = planar_motor.api.get_xbot_at_pos(xlist[0], ylist[0])
        pos2_bot = planar_motor.api.get_xbot_at_pos(xlist[1], ylist[1])
        
        planar_motor.api.send_single_linear_command(num_bot=2, xbot_ids=[pos1_bot, pos2_bot], x_positions=[xlist[0], xlist[1]], y_positions=[ylist[0] + 60, ylist[1] - 60])
        planar_motor.api.send_single_linear_command(num_bot=2, xbot_ids=[pos1_bot, pos2_bot], x_positions=[xlist[1], xlist[0]], y_positions=[ylist[0] + 60, ylist[1] - 60])
        planar_motor.api.send_single_linear_command(num_bot=2, xbot_ids=[pos1_bot, pos2_bot], x_positions=[xlist[1], xlist[0]], y_positions=[ylist[1] , ylist[0]])
        
        planar_motor.api.wait_multiple_move_done([pos1_bot, pos2_bot])
        
    def spin_positions(xlist, ylist):
        for i in range(len(xlist)):
            bot_id = planar_motor.api.get_xbot_at_pos(xlist[i], ylist[i])
            planar_motor.api.send_single_rotation_command(xbot_id=bot_id, angle_degrees=360, speed_deg_per_sec=180)
        planar_motor.api.wait_multiple_move_done([planar_motor.api.get_xbot_at_pos(xlist[i], ylist[i]) for i in range(len(xlist))])
    
    swap_nearest_2_positions(planar_motor, [120, 360], [120, 120])