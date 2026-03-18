from enum import Enum
import threading
import time
import queue

import logging

import mecademicpy.robot as mdr

from typing import Dict

from .ControllerState import ControllerState
from devices import Device

from application_code.prod import prod_cycle
from application_code.home import home
from application_code.shipment import shipment
from application_code.calib import calib

from enum import Enum

# --- Enums for State Management ---

class TaskType(Enum):
    """Defines the types of tasks the robot can execute."""
    HOME = "Home"
    SHIPMENT = "Shipment"
    CALIBRATION = "Calibration"
    PROD = "Production"

class Task(threading.Thread):
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
        """The main execution loop for the thread."""
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
        """Logic for HOME task."""
        try:
            home(self.devices)
        except Exception as e:
            self.logger.warning(f"[{self.name}] HOME task encountered an error: {e}")
        # --------------------------------------------------------
    
    def _run_shipment(self):
        """Logic for SHIPMENT task."""
        try:        
            shipment(self.devices)
        except Exception as e:
            self.logger.warning(f"[{self.name}] SHIPMENT task encountered an error: {e}")
            raise e 
        # --------------------------------------------------------

    def _run_calib(self):
        """Logic for CALIB task."""
        try:        
            calib(self.devices)
        except Exception as e:
            self.logger.warning(f"[{self.name}] CALIB task encountered an error: {e}")
            raise e 
        # --------------------------------------------------------

    def _run_prod_loop(self):
        """Logic for the infinite PROD task."""
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
        """Signal the task to stop execution gracefully."""
        self.logger.info(f"[{self.name}] Stopping task...")
        self._stop_event.set()
        
    def stopped(self):
        """Check if the stop signal has been received."""
        return self._stop_event.is_set()
    
    def abort(self):
        """Abort the task immediately: unblocks any in-flight hardware call right away.
        
        Unlike stop(), this does NOT wait for the current cycle to finish.
        Sets the stop flag AND calls device.abort() on every device so that
        blocking calls like WaitIdle() raise immediately.
        """
        self.logger.warning(f"[{self.name}] Aborting task immediately!")
        self._stop_event.set()
        for device in self.devices.values():
            try:
                device.abort()
            except Exception as e:
                self.logger.warning(f"[{self.name}] Error aborting device {device.device_id}: {e}")

    def is_done(self):
        """Check if the task has completed."""
        return self._is_finished.is_set()