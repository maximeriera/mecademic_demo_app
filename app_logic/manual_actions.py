"""Manual runtime actions — subroutines exposed as buttons on the Manual tab.

Any callable registered in :func:`get_manual_actions` becomes a button, and
must also be listed under ``manual_actions:`` in ``config.yaml`` (the key there
is the key here). A manual action runs as an exclusive task, exactly like
HOME or PROD, so the controller must be READY to start one.

Signatures
----------
A manual action may take ``(devices)`` or ``(devices, context)``. The second
argument is passed only when the signature can accept it, so one-argument
actions keep working unchanged.
"""

from __future__ import annotations

import time
from typing import Dict

from devices import Device


def inspect_part(devices: Dict[str, Device], context=None) -> bool:
    """Inspect the part currently held, and report whether it passed.

    Called two ways, which is the point of putting it here:
    * from :func:`~app_logic.prod.prod_cycle`, as part of a full cycle
    * on its own from the Manual tab, via :func:`inspect_only`

    Returns
    -------
    bool
        ``True`` if the part passed inspection.
    """
    camera = devices["inspection_camera"]
    move_time_s = context.get_param("move_time_s", 0.4) if context else 0.4

    camera.api.TriggerAcquisition()
    camera.api.WaitForFrame()
    time.sleep(move_time_s / 2)

    # A real cell would evaluate the captured frame here. The demo instead
    # rejects every Nth part so the reject branch, the reject_count variable
    # and the conditional step are all exercised without any hardware.
    if context is None:
        return True

    reject_every_n = int(context.get_param("reject_every_n", 5))
    if reject_every_n <= 0:
        return True

    part_number = (
        int(context.get_variable("part_count", 0))
        + int(context.get_variable("reject_count", 0))
        + 1
    )
    return part_number % reject_every_n != 0


def inspect_only(devices: Dict[str, Device], context=None):
    """Manual action: run the inspection routine on its own.

    Reports its own steps, so the Manual tab shows progress the same way the
    production loop does.
    """
    robot = devices["my_meca_robot"]
    move_time_s = context.get_param("move_time_s", 0.4) if context else 0.4

    with context.step("Move to inspection pose"):
        robot.api.MovePose(150, 0, 200, 0, 90, 0)
        robot.api.WaitIdle()
        time.sleep(move_time_s)

    with context.step("Capture and evaluate image"):
        passed = inspect_part(devices, context=context)

    robot.logger.info(f"[inspect_only] inspection {'passed' if passed else 'failed'}")


def open_gripper(devices: Dict[str, Device], context=None):
    """Manual action: release whatever the gripper is holding.

    The smallest useful example: one device, one step, no parameters.
    """
    robot = devices["my_meca_robot"]

    with context.step("Open gripper"):
        robot.api.GripperOpen()
        robot.api.WaitIdle()
        time.sleep(0.2)


def get_manual_actions():
    """Return the manual actions exposed on the Manual tab.

    Keys must match the ``key:`` entries under ``manual_actions:`` in
    ``config.yaml``; the controller refuses to start if one is missing.
    """
    return {
        "inspect_only": inspect_only,
        "open_gripper": open_gripper,
    }
