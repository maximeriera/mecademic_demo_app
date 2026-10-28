"""The five steps of the fiber alignment process.

Shared by the PROD cycle (prod.py) and the Manual tab (manual_actions.py), so a
step tested by hand is exactly the step production runs. Each function:

* reports itself as one step, whose name matches ``sequences:`` in config.yaml;
* checks the cell state first and raises ProcessError, before anything moves,
  when the step would be unsafe or meaningless (e.g. Pick with a fiber already
  in the gripper);
* ends in a safe pose, so the Manual tab can run the steps in any order;
* keeps the ``held_fiber`` and ``at_alignment`` variables (context.yaml) up to
  date. They are how the cell knows what it holds and where it is, across steps
  and app restarts.

Absolute moves are robot programs (see poses.py); only the alignment search
moves the robot from Python.
"""

from __future__ import annotations

import math
from typing import Dict

from core import AppContext
from devices import Device

from . import algorithm, gripper, memory, poses
from .alignment import AlignmentResult, AlignmentSettings, run_alignment
from .cell import ROBOT, ProcessError, power_meter, prepare_robot, run_program


def pick_fiber(devices: Dict[str, Device], context: AppContext, number: int) -> None:
    """Take fiber ``number`` from its slot: ``fiber<N>_in``, close the gripper, ``fiber<N>_out``."""
    with context.step("Pick fiber"):
        held = context.get_variable("held_fiber", 0)
        if held:
            raise ProcessError(
                f"The gripper already holds fiber {held}: put it back first (Manual: Place Fiber)."
            )
        if context.get_variable("at_alignment", False):
            raise ProcessError("The robot is at the alignment station: Retract first.")
        slot = poses.fiber_slot(number)
        robot = prepare_robot(devices, context)

        gripper.open_gripper(devices, context)
        run_program(devices, robot, slot.program_in)
        with context.step("Close gripper"):
            gripper.close_gripper(devices, context)
        context.set_variable("held_fiber", slot.number)
        run_program(devices, robot, slot.program_out)


def move_to_alignment_start(devices: Dict[str, Device], context: AppContext) -> None:
    """Bring the held fiber in front of the fixed fiber: the ``align_start`` program."""
    with context.step("Move to alignment start"):
        if context.get_variable("at_alignment", False):
            raise ProcessError("The robot is already at the alignment station: Retract first.")
        if not context.get_variable("held_fiber", 0):
            devices[ROBOT].logger.warning("Moving to the alignment start with no fiber in the gripper.")
        robot = prepare_robot(devices, context)

        # Set before the program: if it is interrupted near the fixed fiber,
        # HOME still backs out with align_retract first.
        context.set_variable("at_alignment", True)
        run_program(devices, robot, poses.ALIGN_START_PROGRAM)


def align_fiber(devices: Dict[str, Device], context: AppContext) -> AlignmentResult:
    """Run the alignment algorithm from the current pose, then judge it against the threshold.

    The search stays within ``align_range_um`` / ``align_range_deg`` of where it
    starts. It only makes sense at the alignment station, with a fiber. It
    starts the search where this fiber aligned last (see memory.py), and
    remembers where it ends, whenever that is in the light.
    """
    with context.step("Align fiber"):
        logger = devices[ROBOT].logger
        if not context.get_variable("at_alignment", False):
            logger.warning("Aligning away from the alignment station (at_alignment is false).")
        fiber = context.get_variable("held_fiber", 0)
        robot = prepare_robot(devices, context)

        result = run_alignment(robot, power_meter(devices), context, fiber=fiber, align=algorithm.align,
                               logger=logger, phases=algorithm.PHASES, phase_views=algorithm.PHASE_VIEWS,
                               seed=memory.remembered_pose(context, fiber, logger))

        # In the light, even under the threshold: a better start for this fiber than align_start.
        if math.isfinite(result.power_w) and result.power_w >= AlignmentSettings.from_context(context).signal_detect_w:
            memory.remember(context, fiber, result.pose, result.power_w, logger)

        power_mw = result.power_w * 1e3
        # Overrange reads +inf, which the Variables tab could not display.
        context.set_variable("last_align_power_mw", power_mw if math.isfinite(power_mw) else 0.0)
        log = logger.info if result.passed else logger.warning
        log(f"Fiber {fiber}: alignment {'passed' if result.passed else 'FAILED'} at {power_mw:.4g} mW "
            f"({result.reads} readings, {result.duration_s:.1f} s{', timed out' if result.timed_out else ''}).")
        return result


def retract_from_alignment(devices: Dict[str, Device], context: AppContext) -> None:
    """Back the fiber out of the alignment station: the ``align_retract`` program."""
    with context.step("Retract from alignment"):
        if not context.get_variable("at_alignment", False):
            raise ProcessError("The robot is not at the alignment station: nothing to retract from.")
        robot = prepare_robot(devices, context)

        run_program(devices, robot, poses.ALIGN_RETRACT_PROGRAM)
        context.set_variable("at_alignment", False)


def place_fiber(devices: Dict[str, Device], context: AppContext) -> None:
    """Put the held fiber back into its own slot: ``fiber<N>_in``, open the gripper, ``fiber<N>_out``."""
    with context.step("Place fiber"):
        held = context.get_variable("held_fiber", 0)
        if not held:
            raise ProcessError("The gripper holds no fiber (held_fiber is 0): nothing to place.")
        if context.get_variable("at_alignment", False):
            raise ProcessError("The robot is at the alignment station: Retract first.")
        slot = poses.fiber_slot(held)
        robot = prepare_robot(devices, context)

        run_program(devices, robot, slot.program_in)
        with context.step("Open gripper"):
            gripper.open_gripper(devices, context)
        context.set_variable("held_fiber", 0)
        run_program(devices, robot, slot.program_out)


def run_cycle(devices: Dict[str, Device], context: AppContext, number: int) -> AlignmentResult:
    """Pick fiber ``number``, align it, retract and put it back.

    A fiber that does not reach the threshold is still put back. The caller
    decides what a failed alignment means (prod.py counts it and moves on).
    """
    pick_fiber(devices, context, number)
    move_to_alignment_start(devices, context)
    result = align_fiber(devices, context)
    retract_from_alignment(devices, context)
    place_fiber(devices, context)
    return result
