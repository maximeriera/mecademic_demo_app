from devices import Device
from typing import Dict

import time


def prod_cycle(devices: Dict[str, Device], context=None):
    """Logic for PROD task.

    Parameters
    ----------
    devices : Dict[str, Device]
        Configured device objects.
    context : ProductionContext | None
        Shared production context for params/variables persistence.
    """
    if context is None:
        time.sleep(1)
        return

    wait_s = context.get_param("cycle_wait_s", 1.0)
    part_count = context.get_variable("part_count", 0)

    time.sleep(wait_s)
    context.set_variable("part_count", part_count + 1, force=True)
    return
