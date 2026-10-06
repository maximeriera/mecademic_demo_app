"""SHIPMENT task — fold the cell into its transport/storage pose."""

import time
from typing import Dict

from devices import Device


def shipment(devices: Dict[str, Device], context=None):
    """Release any held part, then move the robot to its shipment pose."""
    robot = devices["my_meca_robot"]

    if context is None:
        robot.api.GripperOpen()
        robot.api.MoveJoints(0, -60, 60, 0, 90, 0)
        robot.api.WaitIdle()
        return

    move_time_s = context.get_param("move_time_s", 0.4)

    with context.step("Open gripper"):
        robot.api.GripperOpen()
        robot.api.WaitIdle()
        time.sleep(move_time_s / 4)

    with context.step("Move to shipment position"):
        robot.api.MoveJoints(0, -60, 60, 0, 90, 0)
        robot.api.WaitIdle()
        time.sleep(move_time_s)
