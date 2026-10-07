"""PROD task: one fiber per cycle. Pick it, align it, retract, put it back.

The cycle works through the rack in order. ``next_fiber`` (context.yaml) says
which slot comes next, and advances 1 → N → 1 after each complete cycle. A
cycle that faults leaves it unchanged, so production resumes on the same fiber
once the cell is recovered.

A fiber that does not reach the threshold is still put back and counted in
``failed_count``, and the loop goes on with the next fiber.

The steps themselves live in fiber_alignment/process.py, shared with the
Manual tab.
"""

import time
from typing import Dict

from core import AppContext
from devices import Device

# Imported both ways on purpose: core/Task.py resolves this module as flat
# "prod" when --workspace points at a folder holding the task modules
# directly, and as "app_logic.prod" when it holds an app_logic/ folder. The
# relative form has no parent package in the first case.
try:
    from .fiber_alignment import poses, process
except ImportError:  # pragma: no cover - depends on how the workspace is loaded
    from fiber_alignment import poses, process


def prod_cycle(devices: Dict[str, Device], context: AppContext):
    """Run one production cycle on fiber ``next_fiber``."""
    fiber = context.get_variable("next_fiber", 1)

    result = process.run_cycle(devices, context, fiber)

    counter = "aligned_count" if result.passed else "failed_count"
    context.set_variable(counter, context.get_variable(counter, 0) + 1)
    context.set_variable("next_fiber", poses.next_slot_number(fiber))

    # Demo pacing, deliberately outside any step: it shows as idle time on the
    # Production tab. Remove it, or set cycle_pause_s to 0, to run flat out.
    time.sleep(context.get_param("cycle_pause_s", 1.0))
