"""Placeholder gripper. This is the one place to adapt when the real gripper driver exists.

For now the gripper is declared ``type: "simulated"`` in config.yaml. Its
``.api`` accepts any call and logs it as a no-op, so the process runs end to
end and every gripper action shows in ``logs/devices/fiber_gripper.log``.

When the real driver lands, declare its type in config.yaml, then replace the
two ``api`` calls below with the driver's own. If the gripper reports its state,
wait on that instead of GRIPPER_SETTLE_S. Nothing else in the process changes.
"""

import time
from typing import Dict

from devices import Device

from .cell import GRIPPER

#: The placeholder gives no feedback: time allowed for the jaws to move.
GRIPPER_SETTLE_S = 0.5


def open_gripper(devices: Dict[str, Device]) -> None:
    devices[GRIPPER].api.open()  # TODO: the real driver's call
    time.sleep(GRIPPER_SETTLE_S)


def close_gripper(devices: Dict[str, Device]) -> None:
    devices[GRIPPER].api.close()  # TODO: the real driver's call
    time.sleep(GRIPPER_SETTLE_S)
