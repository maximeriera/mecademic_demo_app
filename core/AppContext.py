"""
core/AppContext.py
------------------
The ``context`` object every workspace task function receives.

    def prod_cycle(devices, context):
        speed = context.get_param("move_time_s", 0.4)
        with context.step("Pick part"):
            ...
        context.set_variable("part_count", context.get_variable("part_count", 0) + 1)

It is a thin facade over four framework components:

* ``context.store``   — :class:`~core.ContextStore` params/variables (optional:
  disabled when the workspace has no context file)
* ``context.steps``   — :class:`~core.StepTracker` live step reporting
* ``context.metrics`` — :class:`~core.ProductionMetrics` PROD cycle/run stats
* ``context.data``    — :class:`~core.DataHub` dynamic data for the optional
  custom view

Workspace code should stick to the methods defined here; the components are
the framework's side of the contract.
"""

from typing import Any, List

from .ContextStore import ContextStore
from .DataHub import DataHub
from .ProductionMetrics import ProductionMetrics
from .StepTracker import StepTracker


class AppContext:
    """App-wide context shared by the controller, every task, and the API."""

    def __init__(self, store: ContextStore, steps: StepTracker, metrics: ProductionMetrics,
                 data: DataHub):
        self.store = store
        self.steps = steps
        self.metrics = metrics
        self.data = data

    # --- Params & variables -------------------------------------------

    def get_param(self, name: str, default: Any = None) -> Any:
        """Current value of a param, or ``default`` if it is not defined."""
        return self.store.get_param(name, default)

    def get_variable(self, name: str, default: Any = None) -> Any:
        """Current value of a variable, or ``default`` if it is not defined."""
        return self.store.get_variable(name, default)

    def set_param(self, name: str, value: Any, force: bool = False) -> None:
        """Set a param from task logic. Raises ContextError if it is not defined.

        ``force`` is accepted for backward compatibility and ignored: code may
        always write, ``editable_when_idle`` only governs edits from the UI.
        """
        self.store.set_param(name, value)

    def set_variable(self, name: str, value: Any, force: bool = False) -> None:
        """Set a variable from task logic. Raises ContextError if it is not defined.

        ``force`` is accepted for backward compatibility and ignored.
        """
        self.store.set_variable(name, value)

    # --- Step reporting -----------------------------------------------

    def step(self, name: str):
        """Context manager reporting (and timing) the step the cell is executing."""
        return self.steps.step(name)

    def set_step(self, name: str) -> None:
        """Declare the current step without a ``with`` block. Prefer :meth:`step`."""
        self.steps.set_step(name)

    def clear_step(self) -> None:
        """Close the step opened by :meth:`set_step`."""
        self.steps.clear_step()

    def declare_sequence(self, steps: List[str], task_key: str | None = None) -> None:
        """Declare the expected step order for the running task at runtime."""
        self.steps.declare_sequence(steps, task_key)

    # --- Dynamic data (custom view) ------------------------------------

    def publish(self, channel: str, value: Any) -> None:
        """Publish a value computed by task code (e.g. an inspection result) to the custom view."""
        self.data.publish(channel, value)

    def mark(self, name: str, **data: Any) -> None:
        """Timestamp an event; a ``<x>_start`` / ``<x>_end`` pair brackets a segment for analysis."""
        self.data.mark(name, **data)

    def latest(self, channel: str, default: Any = None) -> Any:
        """Latest value of a channel — how task code reads a device owned by a data source."""
        return self.data.latest(channel, default)

    # --- Framework ----------------------------------------------------

    def apply_event(self, event: str) -> None:
        """Record a lifecycle event (initialize, prod_start, prod_stop, abort, fault).

        Variables are snapshotted *before* resets run, so a run archived by
        this event records the values it ended with, not reset defaults.
        """
        variables = self.store.values("variables")
        self.metrics.record_event(event, variables)
        self.store.apply_reset(event)
        self.data.mark(event)
