from enum import Enum
import threading
import time
import queue

import logging

import mecademicpy.robot as mdr

from typing import Tuple, Any

from TaskType import TaskType
from RobotState import RobotState

from demo_code.prod import prod_cycle
from demo_code.home import home
from demo_code.shipment import shipment

class Task(threading.Thread):
    def __init__(self, task_type: TaskType, state_change_callback, robot_apis: Tuple[mdr.Robot], accessory_apis: Tuple[Any]):
        super().__init__()
        
        self.logger = logging.getLogger(__name__)
        
        self.task_type = task_type
        self._stop_event = threading.Event()
        self._is_finished = threading.Event()
        self.state_change_callback = state_change_callback
        self.name = f"TaskThread-{task_type.name}"
        # Placeholder for robot connection object (e.g., Mecademic Meca 500 API client)
        self.robot_apis = robot_apis
        self.accessory_apis = accessory_apis

    def run(self):
        """The main execution loop for the thread."""
        self.state_change_callback(RobotState.BUSY)
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
        except Exception as e:
            self.logger.warning(f"[{self.name}] Task failed: {e}")
            self.state_change_callback(RobotState.FAULTED)
            faulted = True
        finally:
            self._is_finished.set()
            if not faulted:
                # Only transition to READY if no FAULT was set during execution
                self.state_change_callback(RobotState.READY)
            self.logger.info(f"[{self.name}] Task finished.")
            
    def _run_home(self):
        """Logic for HOME task."""
        try:
            home(self.robot_apis, self.accessory_apis)
        except Exception as e:
            self.logger.warning(f"[{self.name}] HOME task encountered an error: {e}")
        # --------------------------------------------------------
    
    def _run_shipment(self):
        """Logic for SHIPMENT task."""
        try:        
            shipment(self.robot_apis, self.accessory_apis)
        except Exception as e:
            self.logger.warning(f"[{self.name}] SHIPMENT task encountered an error: {e}")
            raise e 
        # --------------------------------------------------------

    def _run_prod_loop(self):
        """Logic for the infinite PROD task."""
        try:
            while not self.stopped():
                prod_cycle(self.robot_apis, self.accessory_apis)
        except Exception as e:
            self.logger.warning(f"[{self.name}] PROD task encountered an error: {e}")
            raise e
            # -------------------------------------------------------------------------
            
    def stop(self):
        """Signal the task to stop execution gracefully."""
        self.logger.info(f"[{self.name}] Stopping task...")
        self._stop_event.set()
        
    def stopped(self):
        """Check if the stop signal has been received."""
        return self._stop_event.is_set()

    def is_done(self):
        """Check if the task has completed."""
        return self._is_finished.is_set()