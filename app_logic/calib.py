"""CALIBRATION task — hand-eye calibration sequence.

A three-step example showing a task whose steps are worth watching
individually: the capture and the solve have very different durations, which
the per-step timings on the Production tab make obvious.
"""

import time
from typing import Dict

from core import AppContext
from devices import Device


def calib(devices: Dict[str, Device], context: AppContext | None = None):
    """Move to the calibration pose, capture, then solve and store."""
    robot = devices["my_meca_robot"]
    camera = devices["inspection_camera"]

    if context is None:
        robot.api.MovePose(150, 0, 250, 0, 90, 0)
        robot.api.WaitIdle()
        camera.api.TriggerAcquisition()
        return

    move_time_s = context.get_param("move_time_s", 0.4)

    with context.step("Move to calibration pose"):
        robot.api.MovePose(150, 0, 250, 0, 90, 0)
        robot.api.WaitIdle()
        time.sleep(move_time_s)

    with context.step("Capture calibration image"):
        camera.api.TriggerAcquisition()
        camera.api.WaitForFrame()
        time.sleep(move_time_s / 2)

    with context.step("Solve and store calibration"):
        camera.api.SolveHandEye()
        time.sleep(move_time_s * 2)
