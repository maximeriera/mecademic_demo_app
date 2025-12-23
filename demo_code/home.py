import mecademicpy.robot as mdr


from typing import Tuple

from typing import Tuple, Any

def home(robot_apis: Tuple[mdr.Robot], accessory_apis: Tuple[Any]):
    """Logic for HOME task."""
    
    scara:mdr.Robot = robot_apis[0]
    trail:mdr.Robot = robot_apis[1]
    dispenser:mdr.Robot = robot_apis[2]
    
    scara.MoveJoints(65, -145, -33, 80)
    trail.MoveJoints(0, -40, 10, 0, 30, 0)
    dispenser.MoveJoints(-15, -30, 30, 0, 0, 0)

    return