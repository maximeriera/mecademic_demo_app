from __future__ import annotations

import time
from typing import Dict

from devices import Device


def inspect_only(devices: Dict[str, Device], context=None):
    """Example manual subroutine entrypoint.

    Replace this with project-specific robot/device inspection logic.
    """
    robot = devices.get("my_meca_robot")
    if robot is None:
        raise RuntimeError("Manual action 'inspect_only' requires device key 'my_meca_robot'.")

    robot.logger.info("[manual_actions.inspect_only] started")
    robot.api.MoveJoints(0, 0, 0, 0, 0, 0)
    robot.api.WaitIdle()
    robot.logger.info("[manual_actions.inspect_only] completed")


def get_manual_actions():
    """Return project-specific manual actions exposed in the Manual tab."""
    return {
        "inspect_only": inspect_only,
    }
