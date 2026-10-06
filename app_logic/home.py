"""HOME task — bring the cell to its home position.

Runs on demand from the Control tab, and automatically at the start and end of
every production run.

Note the signature: ``home(devices, context=None)``. HOME, SHIPMENT and
CALIBRATION receive ``context`` only if they declare a second parameter, so an
existing one-argument ``def home(devices):`` keeps working unchanged — it just
cannot report steps.
"""

import time
from typing import Dict

from core import AppContext
from devices import Device


def home(devices: Dict[str, Device], context: AppContext | None = None):
    """Open the gripper, then move the robot to its home pose."""
    robot = devices["my_meca_robot"]

    if context is None:
        # Called directly rather than by the task runner: no step reporting.
        robot.api.GripperOpen()
        robot.api.MoveJoints(0, 0, 0, 0, 0, 0)
        robot.api.WaitIdle()
        return

    move_time_s = context.get_param("move_time_s", 0.4)

    with context.step("Open gripper"):
        robot.api.GripperOpen()
        robot.api.WaitIdle()
        time.sleep(move_time_s / 4)

    with context.step("Move to home position"):
        robot.api.MoveJoints(0, 0, 0, 0, 0, 0)
        robot.api.WaitIdle()
        time.sleep(move_time_s)
