from TaskType import TaskType
from RobotState import RobotState

from Task import Task

import mecademicpy.robot as mdr

from enum import Enum
import threading
import time
import queue

import logging

class RobotController:
    def __init__(self, robot_api_config):
        
        self.logger = logging.getLogger(__name__)
        
        self._state = RobotState.FAULTED
        self._current_task: Task | None = None
        self._state_lock = threading.Lock() # Protects state changes
        
        # Threads
        self._monitor_thread = threading.Thread(target=self._monitor_robot_status, name="MonitorThread", daemon=True)
        self._monitor_stop_event = threading.Event()
        
        # Placeholder for Meca 500 API connection
        self.robot_api_config = robot_api_config
        self.robot_api = self._create_robot() 
        
        self.robot_info = mdr.RobotInfo()
        
        self._monitor_thread.start()
        
        # self.initialize()

    def _create_robot(self):
        # --- Placeholder: Implement actual Meca 500 connection logic here ---
        self.logger.info("Connecting to Meca 500...")
        robot = mdr.Robot()
        return robot # Return a mock/actual API client object

    def initialize(self):
        """Perform initial setup and start the monitor."""
        try:
            self.set_state(RobotState.INITIALIZING)
            self.robot_api.Connect(**self.robot_api_config)
            self.robot_info = self.robot_api.GetRobotInfo()
            self.robot_api.ActivateAndHome()
            self.robot_api.WaitHomed()
            self.set_state(RobotState.READY)
            self.logger.info("Robot Controller Initialized and Ready.")
        except Exception as e:
            self.logger.warning(f"Exception catched while calling initialize: {e}")
            self.set_state(RobotState.FAULTED)
            raise Exception(f"{e} --> check E-Stop and reset")

    def set_state(self, new_state: RobotState):
        """Thread-safe state change with transition checks."""
        with self._state_lock:
            if self._state != new_state:
                self.logger.info(f"--- State Change: {self._state.value} -> {new_state.value} ---")
                self._state = new_state
            return self._state

    def get_state(self):
        """Get the current robot state."""
        with self._state_lock:
            return self._state
        
    def get_robot_info(self) -> dict:
        """
        Gathers and returns static information about the robot system.
        NOTE: In a real implementation, you would use the 'self.robot_api' 
        object to query these details from the Meca 500 hardware upon connection.
        """
        # --- Placeholder Information ---
        info = {
            "model": self.robot_info.model,
            "ip_address": self.robot_info.ip_address,
            "version": str(self.robot_info.version),
            "revision": self.robot_info.revision,
        }
        
        # -------------------------------
        
        # Example of fetching dynamic info (e.g., connection status) on demand
        # info["Is_Connected"] = self.robot_api.is_connected() # Hypothetical API call

        return info

    # --- Task Management ---

    def start_task(self, task_type: TaskType):
        """Starts a new task if the robot is READY."""
        if self.get_state() != RobotState.READY:
            self.logger.info(f"Cannot start task. Robot is {self.get_state().value}.")
            return False

        if self._current_task and self._current_task.is_alive():
             # Should not happen if state is READY, but good safety check
             self.logger.info("A task is already running.")
             return False

        # Start the new task
        self._current_task = Task(
            task_type=task_type, 
            state_change_callback=self.set_state, 
            robot_api=self.robot_api
        )
        self._current_task.start()
        return True

    def stop_current_task(self):
        """Stops the currently running task."""
        if self.get_state() != RobotState.BUSY or not self._current_task:
            self.logger.info("No active task to stop.")
            return

        self._current_task.stop()
        # The Task thread will handle the transition back to READY or FAULTED

    # --- Monitoring Thread ---

    def _monitor_robot_status(self):
        """Dedicated thread to monitor the robot's hardware status."""
        while not self._monitor_stop_event.is_set():
            # Example check: If connection drops, set FAULTED
            # if not self.robot_api.is_connected():
            #     self.set_state(RobotState.FAULTED)
            #     self._monitor_stop_event.set() # Stop monitoring if faulted
            
            if self.get_state() == RobotState.INITIALIZING:
                continue
            
            if not self.robot_api.IsConnected():
                self.set_state(RobotState.FAULTED)
                self.robot_info = mdr.RobotInfo()
                
            if not self.robot_api.IsControlling():
                self.set_state(RobotState.FAULTED)
                
            if not self.robot_api.IsAllowedToMove():
                self.set_state(RobotState.FAULTED)

            if self._current_task and self._current_task.is_done() and self._current_task.is_alive():
                 # Handle cases where Task finished but thread is still cleaning up
                 self._current_task.join()
                 self._current_task = None

            time.sleep(0.1) # Check frequency

        self.logger.info("[MonitorThread] Shutdown complete.")
        
    def _check_reference_position(self):
        pass
        
    def shutdown(self):
        """Gracefully stop all threads and clean up."""
        self.logger.info("Shutting down Robot Controller...")
        self.set_state(RobotState.FAULTED)
        self._monitor_stop_event.set()
        if self._current_task and self._current_task.is_alive():
            self.stop_current_task()
            self._current_task.join(timeout=5) # Wait for task to finish gracefully
        
        # Wait for monitor thread
        self._monitor_thread.join(timeout=2)
        
        self.robot_api.DeactivateRobot()
        self.robot_api.WaitDeactivated()
        self.robot_api.Disconnect()

        self.logger.info("Controller shutdown complete.")