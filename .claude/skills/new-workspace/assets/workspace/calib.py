"""CALIBRATION task — <<TITLE>>.

Runs from the CALIBRATION button only. An exception here faults the cell.

This cell has no calibration routine yet (WORKSPACE_SPEC.md, "Calibration"):
the robot only moves to its calibration pose so an operator can check the
tool by eye. The file must exist anyway: without it the app would run the
demo calib() shipped in mecademic_demo_app/app_logic/, written for other
devices.
"""

from typing import Dict

from core import AppContext
from devices import Device

from cell_tools import run_program

# TODO: confirm the program name and its end checkpoint on the robot.
CALIB_PROGRAM, CALIB_DONE = "go_calib_pose", 3


def calib(devices: Dict[str, Device], context: AppContext):
    """Move the robot to its calibration pose."""
    robot = devices["robot"]
    timeout = float(context.get_param("program_timeout_s", 60))

    with context.step("Move to calibration pose"):
        run_program(robot, CALIB_PROGRAM, CALIB_DONE, timeout=timeout)
