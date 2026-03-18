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

import logging

from enum import Enum
from typing import Dict

from .ControllerState import ControllerState

from devices import Device

from application_code.prod import prod_cycle
from application_code.home import home
from application_code.shipment import shipment
from application_code.calib import calib

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

    def __init__(self, logger: logging.Logger, task_type: TaskType, state_change_callback, devices: Dict[str, Device]):
        super().__init__()
        
        self.logger = logger
        self.task_type = task_type
        self._stop_event = threading.Event()
        self._is_finished = threading.Event()
        self.state_change_callback = state_change_callback
        self.name = f"TaskThread-{task_type.name}"
        self.devices = devices

    def run(self):
        """Entry point called by ``threading.Thread.start()``.

        Transitions the controller to ``BUSY``, dispatches to the appropriate
        private method based on :attr:`task_type`, then transitions back to
        ``READY`` on success or ``FAULTED`` if an unhandled exception escapes.
        Always sets :attr:`_is_finished` before returning so callers blocked
        on :meth:`is_done` are unblocked.
        """
        self.state_change_callback(ControllerState.BUSY)
        self.logger.info(f"[{self.name}] Starting task: {self.task_type.value}")
        
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
        except Exception as e:
            self.logger.warning(f"[{self.name}] Task failed: {e}")
            self.state_change_callback(ControllerState.FAULTED)
            faulted = True
        finally:
            self._is_finished.set()
            if not faulted:
                # Only transition to READY if no FAULT was set during execution
                self.state_change_callback(ControllerState.READY)
            self.logger.info(f"[{self.name}] Task finished.")
            
    def _run_home(self):
        """Execute the HOME sequence via :func:`~application_code.home.home`.

        Errors are logged but **not re-raised** so a failed home call does not
        fault the controller on its own.  Used both as a standalone task and
        as the entry/exit step of the PROD loop.
        """
        try:
            home(self.devices)
        except Exception as e:
            self.logger.warning(f"[{self.name}] HOME task encountered an error: {e}")
        # --------------------------------------------------------
    
    def _run_shipment(self):
        """Execute the SHIPMENT sequence via :func:`~application_code.shipment.shipment`.

        Raises
        ------
        Exception
            Re-raised from the underlying function so the controller
            transitions to ``FAULTED``.
        """
        try:        
            shipment(self.devices)
        except Exception as e:
            self.logger.warning(f"[{self.name}] SHIPMENT task encountered an error: {e}")
            raise e 
        # --------------------------------------------------------

    def _run_calib(self):
        """Execute the calibration sequence via :func:`~application_code.calib.calib`.

        Raises
        ------
        Exception
            Re-raised from the underlying function so the controller
            transitions to ``FAULTED``.
        """
        try:        
            calib(self.devices)
        except Exception as e:
            self.logger.warning(f"[{self.name}] CALIB task encountered an error: {e}")
            raise e 
        # --------------------------------------------------------

    def _run_prod_loop(self):
        """Run the infinite production loop.

        Steps
        -----
        1. Execute the home sequence.
        2. Alternate ``prod_cycle(devices, index)`` with ``index`` cycling
           between 1 and 2 until :meth:`stopped` returns ``True``.
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
            self._run_home()
            index = 1
            while not self.stopped():
                try:
                    prod_cycle(self.devices, index)
                except Exception as e:
                    if self.stopped():
                        # abort() called mid-cycle: ClearMotion() unblocked WaitIdle() — clean exit
                        self.logger.info(f"[{self.name}] Prod cycle interrupted by abort: {e}")
                        return
                    raise  # genuine device error → propagate → FAULTED
                index = (index) % 2 + 1
            # stop() called between cycles: finish the loop cleanly
            self._run_home()
        except Exception as e:
            self.logger.warning(f"[{self.name}] PROD task encountered an error: {e}")
            raise e
            
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