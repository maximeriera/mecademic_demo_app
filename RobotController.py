from TaskType import TaskType
from RobotState import RobotState

from Task import Task

import mecademicpy.robot as mdr

from enum import Enum
import threading
import time
import queue

import yaml
from typing import Dict, Tuple, List, Any

import logging

class RobotController:
    def __init__(self, config_path: str = 'config.yaml'):
        
        self.logger = logging.getLogger(__name__)
        
        self._state = RobotState.FAULTED
        self._current_task: Task | None = None
        self._state_lock = threading.Lock() # Protects state changes
        
        # Threads
        self._monitor_thread = threading.Thread(target=self._monitor_robot_status, name="MonitorThread", daemon=True)
        self._monitor_stop_event = threading.Event()
        
        # FIX 3: Call static method correctly
        self.config: Dict = RobotController.get_robot_config(config_path) 
        
        # FIX 1: Initialize tuples as empty tuples ()
        # Containers to hold the API and Info objects
        self.robot_apis: Tuple[mdr.Robot] = () 
        self.robot_infos: Tuple[mdr.RobotInfo] = ()
        self.accessory_apis: Tuple[Any] = ()
        
        self._create_apis() 
        self._monitor_thread.start()


    def _create_apis(self):
        for device_name, device_info in self.config.get('devices', {}).items():
            device_type = device_info.get('type', '').lower()
            if device_type == 'mecademic':
                self.logger.info(f"Creating Mecademic Robot API for device: {device_name}")
                robot_api = mdr.Robot()
                
                # Create a placeholder RobotInfo object from config data
                robot_info = mdr.RobotInfo() 
                robot_info.ip_address = device_info.get('ip_address', '')
                robot_info.model = device_name # Use device name as a temporary identifier
                
                # Append to tuples
                self.robot_apis += (robot_api,)
                self.robot_infos += (robot_info,)
                
            elif device_type == 'arduino':
                self.logger.info(f"Creating Arduino IO API for device: {device_name}")
                import accessories_api.ArduinoBoard as arduino_api_module
                # Placeholder for actual Arduino API creation
                arduino_api = arduino_api_module.ArduinoBoard(port=device_info.get('Port', 'COM3'))
                self.accessory_apis += (arduino_api,)
                
            elif device_type == 'zaber':
                self.logger.info(f"Creating Zaber Stage API for device: {device_name}")
                import accessories_api.ZaberAxis as zaber_api_module
                # Placeholder for actual Zaber API creation
                zaber_api = zaber_api_module.ZaberAxis(port=device_info.get('Port', 'COM3'))
                self.accessory_apis += (zaber_api,)
                
            else:
                self.logger.warning(f"Unknown device type '{device_type}' for device '{device_name}'. Skipping API creation.")
                
    def initialize(self):
        """Perform initial setup and start the monitor."""
        self.set_state(RobotState.INITIALIZING)
        
        # List to temporarily store the actual info fetched from the robot
        updated_robot_infos: List[mdr.RobotInfo] = []
        
        for i, (robot_api, robot_info) in enumerate(zip(self.robot_apis, self.robot_infos)):
            try:
                self.logger.info(f"Connecting and initializing {robot_info.model} at {robot_info.ip_address}...")
                
                # Connect
                robot_api.Connect(robot_info.ip_address, disconnect_on_exception=False)
                
                # Get ACTUAL robot info after connection and replace placeholder
                # We need to save the actual RobotInfo object that Mecademic returns
                actual_robot_info = robot_api.GetRobotInfo()
                actual_robot_info.ip_address = robot_info.ip_address # Preserve the configured IP
                updated_robot_infos.append(actual_robot_info)
                
                # Activate and Home
                robot_api.ActivateAndHome()
                robot_api.WaitHomed()
                
                self.logger.info(f"Robot {actual_robot_info.model} initialized successfully.")
                
            except Exception as e:
                self.logger.error(f"Initialization failed for robot {robot_info.model}: {e}", exc_info=True)
                self.set_state(RobotState.FAULTED)
                # Ensure all robots that connected are disconnected on failure
                robot_api.Disconnect()
                # Re-raise to signal failure to the web API
                raise Exception(f"Initialization failed for robot {robot_info.model}. Check E-Stop and reset. Error: {e}")
        
        # Update the main controller tuple with the real RobotInfo objects
        self.robot_infos = tuple(updated_robot_infos) 
        
        self.logger.info("Robot Controller Initialized and Ready.") 
        
        for accessory_api in self.accessory_apis:
            try:
                self.logger.info(f"Initializing accessory API: {type(accessory_api).__name__}")
                accessory_api.initialize()
            except Exception as e:
                self.logger.error(f"Accessory API initialization failed: {e}", exc_info=True)
                self.set_state(RobotState.FAULTED)
                raise Exception(f"Accessory API initialization failed: {e}")
        
        self.set_state(RobotState.READY)

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
        
    def get_robot_info(self) -> List[Dict[str, Any]]:
        """
        Gathers and returns static information about ALL robots in a list of dictionaries.
        """
        all_info = []
        
        # FIX 2: Iterate over all stored RobotInfo objects
        for robot_info in self.robot_infos:
            info = {
                "model": robot_info.model or "Unknown Model",
                "ip_address": robot_info.ip_address or "N/A",
                # The .version attribute is an int/float, convert to string
                "version": str(robot_info.version) if robot_info.version is not None else "N/A", 
                "revision": robot_info.revision or "N/A",
            }
            # Add connectivity status if API supports it and we're not faulted
            
            # NOTE: Getting connection status dynamically requires a map from info object to API object.
            # For simplicity, we'll rely on the status set during initialize/monitor.
            all_info.append(info)

        return all_info

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
            robot_apis=self.robot_apis,
            accessory_apis=self.accessory_apis
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
            
            # Assume all robots must be healthy for the controller to be READY
            all_healthy = True 
            
            for robot_api in self.robot_apis:
                
                if self.get_state() == RobotState.INITIALIZING:
                    continue
                
                # Check critical Meca 500 status flags
                if not robot_api.IsConnected() or not robot_api.IsControlling() or not robot_api.IsAllowedToMove():
                    self.set_state(RobotState.FAULTED)
                    all_healthy = False
                    break # Break the inner loop, controller is faulted

            if all_healthy and self.get_state() != RobotState.BUSY and self.get_state() != RobotState.INITIALIZING and self.get_state() != RobotState.FAULTED:
                # Only return to READY if monitoring thread detects no issues AND no task is running
                self.set_state(RobotState.READY)

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
        
        for robot_api in self.robot_apis:
            robot_api.DeactivateRobot()
            robot_api.WaitDeactivated()
            robot_api.Disconnect()
            
        for accessory_api in self.accessory_apis:
            accessory_api.shutdown()

        self.logger.info("Controller shutdown complete.")
        
    @staticmethod
    # FIX 3: Remove 'self' from static method definition
    def get_robot_config(config_file_path: str = 'config.yaml') -> Dict[str, Any]:
        """
        Loads and returns the configuration data from the specified YAML file.
        ...
        """
        try:
            with open(config_file_path, 'r') as file:
                config = yaml.safe_load(file)

            if config is None:
                print("Warning: Config file is empty.")
                return {}

            return config

        except FileNotFoundError:
            print(f"Error: Configuration file not found at '{config_file_path}'")
            raise
        except yaml.YAMLError as e:
            print(f"Error parsing YAML file: {e}")
            raise