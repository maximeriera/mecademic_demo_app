"""PROD task — one production cycle.

This is the default example: a pick / inspect / place cycle running against the
simulated devices declared in ``config.yaml``. It needs no hardware, and is
meant to be read as a template for your own cell.

What it demonstrates
--------------------
* ``with context.step(...)`` — what the cell is doing, shown live in the
  sidebar on every tab and on the Production tab. Each step is timed
  automatically and closed even if the body raises or the run is aborted.
* **params** — tunable inputs you can edit from the Variables tab while idle.
* **variables** — counters carried across cycles (declared in ``context.yaml``).
* **nesting** — a step opened inside another step is timed separately.
* **a conditional step** — the reject branch is only taken on some cycles.

Swapping in real hardware does not change this file: a SimulatedDevice's
``.api`` accepts any method call, so ``robot.api.MovePose(...)`` is the same
line whether the device is simulated or a real Meca500.
"""

import time
from typing import Dict

from core import AppContext
from devices import Device

# Imported both ways on purpose: core/Task.py resolves this module as flat
# "prod" when an external --workspace is on sys.path, and as "app_logic.prod"
# otherwise. The relative form has no parent package in the first case.
try:
    from .manual_actions import inspect_part
except ImportError:  # pragma: no cover - depends on how the workspace is loaded
    from manual_actions import inspect_part


def prod_cycle(devices: Dict[str, Device], context: AppContext | None = None):
    """Run one production cycle.

    Parameters
    ----------
    devices : Dict[str, Device]
        Devices from ``config.yaml``, keyed by the names declared there.
    context : AppContext
        Always supplied by the task runner. Carries params, variables, and the
        ``step()`` reporting API.
    """
    if context is None:
        # Only reachable if this function is called directly rather than by the
        # task runner. Nothing to report to, so just pace and return.
        time.sleep(1)
        return

    robot = devices["my_meca_robot"]
    feeder = devices["part_feeder"]

    move_time_s = context.get_param("move_time_s", 0.4)
    cycle_wait_s = context.get_param("cycle_wait_s", 1.0)
    reject_every_n = context.get_param("reject_every_n", 5)

    part_count = context.get_variable("part_count", 0)
    reject_count = context.get_variable("reject_count", 0)
    part_number = part_count + reject_count + 1

    # --- 1. Ask the feeder for a part -----------------------------------
    with context.step("Request part from feeder"):
        feeder.api.get_part()
        time.sleep(move_time_s / 2)

    # --- 2. Pick it up ---------------------------------------------------
    with context.step("Pick part"):
        robot.api.MovePose(200, 0, 150, 0, 90, 0)
        robot.api.WaitIdle()
        time.sleep(move_time_s)

        # A step opened inside another step nests: the gripper close is timed
        # on its own AND counted inside "Pick part".
        with context.step("Close gripper"):
            robot.api.GripperClose()
            robot.api.WaitIdle()
            time.sleep(move_time_s / 4)

    # --- 3. Inspect ------------------------------------------------------
    with context.step("Inspect part"):
        passed = inspect_part(devices, context=context)
    # Shown by the custom view (custom_view/), next to its own profile analysis.
    context.publish("inspection_passed", passed)

    # --- 4. Place, or discard --------------------------------------------
    # Only one of these two runs per cycle. "Discard rejected part" is declared
    # last in the sequence (see `sequences:` in config.yaml), so a passing cycle
    # finishes at step 4 of 5 rather than going "off sequence".
    if passed:
        with context.step("Place part in tray"):
            robot.api.MovePose(100, 200, 150, 0, 90, 0)
            robot.api.GripperOpen()
            robot.api.WaitIdle()
            time.sleep(move_time_s)
        context.set_variable("part_count", part_count + 1)
    else:
        with context.step("Discard rejected part"):
            robot.api.MovePose(-100, 200, 150, 0, 90, 0)
            robot.api.GripperOpen()
            robot.api.WaitIdle()
            time.sleep(move_time_s / 2)
        context.set_variable("reject_count", reject_count + 1)
        robot.logger.warning(f"Part #{part_number} rejected by inspection.")

    # Pacing only — remove this in a real cell, where the hardware sets the pace.
    #
    # Deliberately NOT wrapped in a context.step(): any time not inside a step
    # is reported as idle, so this pause shows up as a gap on the Production
    # tab. That is the useful behaviour — dead time between steps is exactly
    # what you want to see. Raise `cycle_wait_s` and watch the gap grow.
    time.sleep(cycle_wait_s)
