"""HOME task — bring <<TITLE>> to its start position.

Runs from the HOME button, and automatically before the first PROD cycle and
after a graceful STOP. INITIALIZE already activated and homed every robot
(MecaRobot.initialize), so this only moves to the cell's own start pose.
An exception here faults the cell.
"""

from typing import Dict

from core import AppContext
from devices import Device

from cell_tools import run_program

# Robot programs saved on the controller, and the checkpoint each sets on its
# last line. TODO: confirm the names and numbers on the robot.
HOME_PROGRAM, HOME_DONE = "go_home", 1


def home(devices: Dict[str, Device], context: AppContext):
    """Open the gripper, then run the robot's home program."""
    robot = devices["robot"]
    timeout = float(context.get_param("program_timeout_s", 60))

    with context.step("Open gripper"):
        robot.api.GripperOpen()
        robot.api.WaitIdle(timeout=timeout)

    with context.step("Move to home"):
        run_program(robot, HOME_PROGRAM, HOME_DONE, timeout=timeout)
