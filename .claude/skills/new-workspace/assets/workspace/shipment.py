"""SHIPMENT task — fold <<TITLE>> into its transport pose.

Runs from the SHIPMENT button only. An exception here faults the cell.
"""

from typing import Dict

from core import AppContext
from devices import Device

from cell_tools import run_program

# TODO: confirm the program name and its end checkpoint on the robot.
SHIPMENT_PROGRAM, SHIPMENT_DONE = "go_shipment", 2


def shipment(devices: Dict[str, Device], context: AppContext):
    """Open the gripper, then run the robot's shipment program."""
    robot = devices["robot"]
    timeout = float(context.get_param("program_timeout_s", 60))

    with context.step("Open gripper"):
        robot.api.GripperOpen()
        robot.api.WaitIdle(timeout=timeout)

    with context.step("Move to shipment pose"):
        run_program(robot, SHIPMENT_PROGRAM, SHIPMENT_DONE, timeout=timeout)
