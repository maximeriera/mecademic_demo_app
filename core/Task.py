"""
core/Task.py
------------
Background task execution for the robotic cell.

Defines two public symbols:

* :class:`TaskType` — enum of all runnable task names.
* :class:`Task`     — ``threading.Thread`` subclass that executes one task
  and reports state transitions back to the
  :class:`~core.ApplicationController` via a callback.

Stop vs. Abort
--------------
* ``stop()``  — sets a flag so the PROD loop exits **after the current cycle**
  completes, then goes home.  Non-PROD tasks are unaffected (they run to
  completion regardless).
* ``abort()`` — sets the same flag **and** calls :meth:`~devices.Device.abort`
  on every device, which unblocks any in-flight blocking call (e.g.
  ``WaitIdle()``) immediately.  No home sequence is run.
"""

import threading
import inspect
import time
import os
import importlib
import sys
from importlib import import_module

import logging

from enum import Enum
from typing import Dict

from .ControllerState import ControllerState
from .ManualActions import load_manual_actions_registry

from devices import Device
from .ProductionContext import ProductionContext


TASK_FUNCTION_MODULE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "prod_cycle": ("prod", "app_logic.prod"),
    "home": ("home", "app_logic.home"),
    "shipment": ("shipment", "app_logic.shipment"),
    "calib": ("calib", "app_logic.calib"),
}


def _is_dev_reload_enabled() -> bool:
    return str(os.environ.get("MECADEMIC_DEV_RELOAD", "")).strip().lower() in {"1", "true", "yes", "on"}


def _load_workspace_task_function(
    function_name: str,
    module_candidates: tuple[str, ...],
    force_reload: bool = False,
):
    """Load a task function from the active workspace before falling back to repo defaults."""
    last_error = None
    for module_name in module_candidates:
        try:
            if force_reload and module_name in sys.modules:
                module = importlib.reload(sys.modules[module_name])
            else:
                module = import_module(module_name)
            return getattr(module, function_name)
        except Exception as error:
            last_error = error

    raise ImportError(
        f"Unable to import task function '{function_name}' from any workspace module: {module_candidates}"
    ) from last_error

# --- Enums for State Management ---

class TaskType(Enum):
    """Enumeration of all tasks the cell can execute.

    Values
    ------
    HOME
        Move all robots to their defined home positions.
    SHIPMENT
        Move all robots to their shipment/storage positions.
    CALIBRATION
        Run the hand-eye calibration sequence for the vision system.
    PROD
        Infinite production loop (pick-and-place cycles).  Runs until
        :meth:`Task.stop` or :meth:`Task.abort` is called.
    """
    HOME = "Home"
    SHIPMENT = "Shipment"
    CALIBRATION = "Calibration"
    PROD = "Production"
    MANUAL_ACTION = "ManualAction"

class Task(threading.Thread):
    """A single-shot background thread that executes one :class:`TaskType`.

    The thread is created by :class:`~core.ApplicationController` and must
    not be reused — create a new instance for each run.

    Parameters
    ----------
    logger : logging.Logger
        Logger from the parent ``ApplicationController``; all task messages
        are written to the same log file as the controller.
    task_type : TaskType
        Which task to execute when :meth:`start` is called.
    state_change_callback : Callable[[ControllerState], None]
        Function to call when the task wants to change the controller state
        (typically ``ApplicationController.set_state``).
    devices : Dict[str, Device]
        Shared device map passed down from the controller.  Keys are the
        ``device_id`` strings defined in ``config.yaml``.

    Thread lifecycle
    ----------------
    1. :meth:`start` — inherited from ``threading.Thread``; calls :meth:`run`.
    2. :meth:`run`   — sets state ``BUSY``, dispatches to the appropriate
       private method, then sets ``READY`` (or ``FAULTED`` on error).
    3. :meth:`stop`  — request a graceful end-of-cycle exit (PROD only).
    4. :meth:`abort` — request an immediate hardware-level stop.
    """

    def __init__(
        self,
        logger: logging.Logger,
        task_type: TaskType,
        state_change_callback,
        devices: Dict[str, Device],
        production_context: ProductionContext,
        manual_action_key: str | None = None,
    ):
        super().__init__()
        
        self.logger = logger
        self.task_type = task_type
        self._stop_event = threading.Event()
        self._is_finished = threading.Event()
        self.state_change_callback = state_change_callback
        self.name = f"TaskThread-{task_type.name}"
        self.devices = devices
        self.production_context = production_context
        self.manual_action_key = (manual_action_key or "").strip() or None

    def _resolve_task_function(self, function_name: str):
        module_candidates = TASK_FUNCTION_MODULE_CANDIDATES[function_name]
        force_reload = _is_dev_reload_enabled()
        fn = _load_workspace_task_function(function_name, module_candidates, force_reload=force_reload)
        self.logger.info(
            "[%s] Resolved task function '%s' from '%s.%s' (dev_reload=%s)",
            self.name,
            function_name,
            fn.__module__,
            getattr(fn, "__name__", "<anonymous>"),
            force_reload,
        )
        return fn

    def run(self):
        """Entry point called by ``threading.Thread.start()``.

        Transitions the controller to ``BUSY``, dispatches to the appropriate
        private method based on :attr:`task_type`, then transitions back to
        ``READY`` on success or ``FAULTED`` if an unhandled exception escapes.
        Always sets :attr:`_is_finished` before returning so callers blocked
        on :meth:`is_done` are unblocked.
        """
        self.state_change_callback(ControllerState.BUSY)
        self.logger.info(
            "[%s] Starting task=%s manual_action_key=%s devices=%s",
            self.name,
            self.task_type.value,
            self.manual_action_key,
            sorted(self.devices.keys()),
        )
        
        faulted = False
        
        try:
            match self.task_type:
                case TaskType.PROD:
                    self._run_prod_loop()
                case TaskType.HOME:
                    self._run_home()
                case TaskType.SHIPMENT:
                    self._run_shipment()
                case TaskType.CALIBRATION:
                    self._run_calib()
                case TaskType.MANUAL_ACTION:
                    self._run_manual_action()
        except Exception as e:
            self.logger.warning(f"[{self.name}] Task failed: {e}")
            if self.task_type == TaskType.PROD:
                self.production_context.apply_event("fault")
                self.production_context.mark_prod_running(False)
            self.state_change_callback(ControllerState.FAULTED)
            faulted = True
        finally:
            self._is_finished.set()
            if not faulted:
                # Only transition to READY if no FAULT was set during execution
                self.state_change_callback(ControllerState.READY)
            self.logger.info(f"[{self.name}] Task finished.")
            
    def _run_home(self):
        """Execute the HOME sequence via :func:`~app_logic.home.home`.

        Raises
        ------
        Exception
            Re-raised from the underlying function so the controller
            transitions to ``FAULTED``.
        """
        try:
            self.logger.info(f"[{self.name}] HOME routine started.")
            home_fn = self._resolve_task_function("home")
            home_fn(self.devices)
            self.logger.info(f"[{self.name}] HOME routine completed.")
        except Exception as e:
            self.logger.warning(f"[{self.name}] HOME task encountered an error: {e}")
            raise e
        # --------------------------------------------------------
    
    def _run_shipment(self):
        """Execute the SHIPMENT sequence via :func:`~app_logic.shipment.shipment`.

        Raises
        ------
        Exception
            Re-raised from the underlying function so the controller
            transitions to ``FAULTED``.
        """
        try:        
            self.logger.info(f"[{self.name}] SHIPMENT routine started.")
            shipment_fn = self._resolve_task_function("shipment")
            shipment_fn(self.devices)
            self.logger.info(f"[{self.name}] SHIPMENT routine completed.")
        except Exception as e:
            self.logger.warning(f"[{self.name}] SHIPMENT task encountered an error: {e}")
            raise e 
        # --------------------------------------------------------

    def _run_calib(self):
        """Execute the calibration sequence via :func:`~app_logic.calib.calib`.

        Raises
        ------
        Exception
            Re-raised from the underlying function so the controller
            transitions to ``FAULTED``.
        """
        try:        
            self.logger.info(f"[{self.name}] CALIB routine started.")
            calib_fn = self._resolve_task_function("calib")
            calib_fn(self.devices)
            self.logger.info(f"[{self.name}] CALIB routine completed.")
        except Exception as e:
            self.logger.warning(f"[{self.name}] CALIB task encountered an error: {e}")
            raise e 
        # --------------------------------------------------------

    def _run_prod_loop(self):
        """Run the infinite production loop.

        Steps
        -----
        1. Execute the home sequence.
          2. Repeatedly call ``prod_cycle(devices, context)`` until
              :meth:`stopped` returns ``True``.
        3. If an exception occurs inside a cycle **and** :meth:`stopped` is
           ``True``, the exception is treated as a clean abort (``ClearMotion``
           unblocked ``WaitIdle``) and the method returns without raising.
        4. If the loop exits normally (stop between cycles), run home again.

        Raises
        ------
        Exception
            Any exception that occurs when :meth:`stopped` is ``False`` is a
            genuine device fault and is re-raised to trigger ``FAULTED`` state.
        """
        try:
            prod_cycle_fn = self._resolve_task_function("prod_cycle")
            self._run_home()
            while not self.stopped():
                cycle_number = int(self.production_context.get_variable("part_count", 0)) + 1
                self.logger.info(f"[{self.name}] PROD cycle start #{cycle_number}")
                self.production_context.mark_cycle_start()
                try:
                    prod_cycle_fn(self.devices, self.production_context)
                    self.production_context.mark_cycle_end()
                    self.logger.info(f"[{self.name}] PROD cycle complete #{cycle_number}")
                except Exception as e:
                    self.production_context.mark_cycle_error(str(e))
                    if self.stopped():
                        # abort() called mid-cycle: ClearMotion() unblocked WaitIdle() — clean exit
                        self.logger.info(f"[{self.name}] Prod cycle interrupted by abort: {e}")
                        self.production_context.mark_prod_running(False)
                        return
                    raise  # genuine device error → propagate → FAULTED
            # stop() called between cycles: finish the loop cleanly
            self.production_context.apply_event("prod_stop")
            self.production_context.mark_prod_running(False)
            self._run_home()
        except Exception as e:
            self.production_context.mark_prod_running(False)
            self.logger.warning(f"[{self.name}] PROD task encountered an error: {e}")
            raise e

    def _run_manual_action(self):
        """Execute one configured manual action function."""
        if not self.manual_action_key:
            raise ValueError("Missing manual action key for MANUAL_ACTION task.")

        start_ts = time.perf_counter()
        registry = load_manual_actions_registry(logger=self.logger)
        action_fn = registry.get(self.manual_action_key)
        if action_fn is None:
            raise ValueError(f"Manual action '{self.manual_action_key}' is not registered.")

        self.logger.info(
            "[%s] Running manual action key='%s' callable='%s.%s'",
            self.name,
            self.manual_action_key,
            action_fn.__module__,
            getattr(action_fn, "__name__", "<anonymous>"),
        )

        params = inspect.signature(action_fn).parameters
        if len(params) >= 2:
            action_fn(self.devices, self.production_context)
        else:
            action_fn(self.devices)
        elapsed_s = time.perf_counter() - start_ts
        self.logger.info(
            "[%s] Manual action '%s' completed in %.3f s.",
            self.name,
            self.manual_action_key,
            elapsed_s,
        )
            
    def stop(self):
        """Request a graceful stop at the end of the current cycle (PROD only).

        Sets :attr:`_stop_event` so the ``while not self.stopped()`` guard in
        :meth:`_run_prod_loop` exits between cycles.  The current cycle runs
        to completion and the home sequence is executed before the thread exits.
        Has no immediate hardware effect.
        """
        self.logger.info(f"[{self.name}] Stopping task...")
        self._stop_event.set()
        
    def stopped(self) -> bool:
        """Return ``True`` if :meth:`stop` or :meth:`abort` has been called."""
        return self._stop_event.is_set()
    
    def abort(self):
        """Immediately interrupt the task, regardless of where in the cycle it is.

        Unlike :meth:`stop`, this method does **not** wait for the current
        cycle to finish.  It:

        1. Sets :attr:`_stop_event` so no new cycle is started.
        2. Calls :meth:`~devices.Device.abort` on every device, which clears
           the hardware motion queue and causes any blocking SDK call (e.g.
           ``WaitIdle()``) to raise an exception immediately.

        The exception propagates up through :meth:`_run_prod_loop`, where it
        is caught and treated as a clean exit because :meth:`stopped` is
        already ``True``.  No home sequence is run.

        Errors from individual device aborts are logged as warnings and do
        not prevent the remaining devices from being aborted.
        """
        self.logger.warning(f"[{self.name}] Aborting task immediately!")
        self._stop_event.set()
        for device in self.devices.values():
            try:
                device.abort()
            except Exception as e:
                self.logger.warning(f"[{self.name}] Error aborting device {device.device_id}: {e}")

    def is_done(self) -> bool:
        """Return ``True`` once the task thread has finished executing.

        Set by :meth:`run` in its ``finally`` block, so it is guaranteed to
        be ``True`` regardless of whether the task succeeded or faulted.
        """
        return self._is_finished.is_set()