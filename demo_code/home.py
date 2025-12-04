import mecademicpy.robot as mdr
import accessories_api.PlanarMotor as pmp
from typing import Tuple

from typing import Tuple, Any

def home(robot_apis: Tuple[mdr.Robot], accessory_apis: Tuple[Any]):
    """Logic for HOME task."""
    
    # mirror:mdr.Robot = robot_apis[0]
    # dispenser:mdr.Robot = robot_apis[1]
    
    planar_motor:pmp.PlanarMotor = accessory_apis[0]
    num_bot = 4
    planar_motor.api.send_auto_move_command(num_bot=num_bot, xbot_ids= [1,2,3,4], y_positions=[120]*num_bot, x_pos=[120 + i*240 for i in range(num_bot)])

    