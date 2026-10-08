"""SmarAct gripper: its jaw is a positioner on a SmarAct MCS2 controller.

Opening and closing are closed-loop moves of that channel to the positions set
by the ``gripper_open_mm`` / ``gripper_closed_mm`` params (Variables tab),
followed by a wait until the move has ended. The channel is the first one listed
under ``channels:`` for ``fiber_gripper`` in config.yaml.

* Teach ``gripper_closed_mm`` as the position where the jaws hold the fiber,
  not beyond it. A jaw blocked short of its target ends the move with
  MCS2MotionError (failed move, or still moving at GRIPPER_TIMEOUT_S), which
  faults the task.
* ABORT interrupts the wait (MCS2Aborted), like a robot WaitIdle().
* With ``type: "simulated"`` in config.yaml (no gripper connected), both
  moves are logged no-ops.
"""

from typing import Dict

from core import AppContext
from devices import Device

from .cell import GRIPPER

#: Longest an open or close move may take before it counts as failed (s).
GRIPPER_TIMEOUT_S = 10.0


def open_gripper(devices: Dict[str, Device], context: AppContext) -> None:
    _move_jaw(devices, context.get_param("gripper_open_mm", 0.0))


def close_gripper(devices: Dict[str, Device], context: AppContext) -> None:
    _move_jaw(devices, context.get_param("gripper_closed_mm", 1.0))


def _move_jaw(devices: Dict[str, Device], position_mm: float) -> None:
    gripper = devices[GRIPPER]
    # A simulated stand-in has no channels; its api ignores the number anyway.
    channel = (getattr(gripper, "channels", None) or [0])[0]
    gripper.api.move_to_mm(channel, position_mm)
    gripper.api.wait(channel, timeout=GRIPPER_TIMEOUT_S)
