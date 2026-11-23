"""Manual actions of <<TITLE>> — the buttons of the Manual tab.

Each key returned by :func:`get_manual_actions` must also be listed under
``manual_actions:`` in config.yaml (label, description, confirmation text),
and every key listed there must be returned here, or the app refuses to
start the workspace.

A manual action runs as an exclusive task, like HOME, so the cell must be
READY. It is the only kind of task allowed while a device is released.
An exception here faults the cell.

Reuse: the action and the production cycle call the same functions
(request_part, pick_part from prod.py), so testing a piece by hand tests the
code PROD runs.
"""

from __future__ import annotations

from typing import Dict

from core import AppContext
from devices import Device

from prod import pick_part, request_part


def open_gripper(devices: Dict[str, Device], context: AppContext):
    """Release whatever the gripper holds."""
    robot = devices["robot"]
    timeout = float(context.get_param("program_timeout_s", 60))
    with context.step("Open gripper"):
        robot.api.GripperOpen()
        robot.api.WaitIdle(timeout=timeout)


def pick_one_part(devices: Dict[str, Device], context: AppContext):
    """Ask the feeder for one part and pick it, without placing it."""
    with context.step("Request part from feeder"):
        part = request_part(devices, context)
    if part is None:
        raise RuntimeError("The feeder found no pickable part.")
    with context.step("Pick part"):
        pick_part(devices, context, part)


def get_manual_actions():
    """Keys must match the ``key:`` entries under ``manual_actions:`` in config.yaml."""
    return {
        "open_gripper": open_gripper,
        "pick_one_part": pick_one_part,
    }
