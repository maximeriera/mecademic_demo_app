"""PROD task — one production cycle of <<TITLE>>.

The app calls ``prod_cycle(devices, context)`` in a loop while PROD runs:
home() first, then cycles until STOP, then home() again. ABORT interrupts
the cycle where it is (a robot wait raises) and skips the exit home. Nothing
here needs to check for STOP or ABORT.

Sequence (declared under ``sequences: prod_cycle`` in config.yaml):
  1. Request part from feeder
  2. Pick part        robot program "pick_feeder", end checkpoint 11
  3. Place part       robot program "place_tray",  end checkpoint 12
  4. Reject part      only when the feeder found nothing pickable

Robot programs, checkpoints and robot variables this file relies on are
listed in README.md ("Robot programs").
"""

from typing import Dict

from core import AppContext
from devices import Device

from cell_tools import or_simulated, run_program

# Robot programs saved on the controller, and the checkpoint each sets on its
# last line. TODO: confirm the names and numbers on the robot.
PICK_PROGRAM, PICK_DONE = "pick_feeder", 11
PLACE_PROGRAM, PLACE_DONE = "place_tray", 12

#: What the simulated feeder "finds", so the smoke test runs the full cycle.
SIMULATED_PART = {"resp": 200, "x": 0.0, "y": 0.0, "rz": 0.0}


def request_part(devices: Dict[str, Device], context: AppContext):
    """Ask the feeder for a pickable part, up to ``feeder_retries`` times. Returns its pose or None.

    Shared with the "Pick one part" manual action (manual_actions.py).
    """
    feeder = devices["feeder"]
    retries = int(context.get_param("feeder_retries", 3))
    for attempt in range(1, retries + 1):
        part = or_simulated(feeder.api.get_part(), SIMULATED_PART)   # blocks until the Eye+ answers
        if part.get("resp") == 200:
            return part
        feeder.logger.warning(f"[feeder] no pickable part (attempt {attempt}/{retries}): {part}")
    return None


def pick_part(devices: Dict[str, Device], context: AppContext, part) -> None:
    """Hand the part pose to the robot program and run it."""
    robot = devices["robot"]
    timeout = float(context.get_param("program_timeout_s", 60))
    # Robot variables created on the controller (CreateVariable). TODO: confirm names.
    robot.api.SetVariable("pick_x", part["x"])
    robot.api.SetVariable("pick_y", part["y"])
    robot.api.SetVariable("pick_rz", part["rz"])
    run_program(robot, PICK_PROGRAM, PICK_DONE, timeout=timeout)


def prod_cycle(devices: Dict[str, Device], context: AppContext):
    """Request a part, pick it, place it; count a reject when the feeder has none."""
    robot = devices["robot"]
    timeout = float(context.get_param("program_timeout_s", 60))

    with context.step("Request part from feeder"):
        part = request_part(devices, context)

    if part is None:
        # Declared last in the sequence: a normal cycle ends at step 3 of 4.
        with context.step("Reject part"):
            devices["io"].api.write_do(0, True)        # TODO: confirm DO0 = "feeder empty" lamp
        context.set_variable("reject_count", context.get_variable("reject_count", 0) + 1)
        return

    with context.step("Pick part"):
        pick_part(devices, context, part)

    with context.step("Place part"):
        run_program(robot, PLACE_PROGRAM, PLACE_DONE, timeout=timeout)

    context.set_variable("part_count", context.get_variable("part_count", 0) + 1)
