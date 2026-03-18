
import mecademicpy.robot as mdr

import threading
import time

from devices import Device

import yaml
from typing import Dict, List, Any

import logging
from logging.handlers import RotatingFileHandler
import os

from .Task import Task, TaskType
from .ControllerState import ControllerState


class ApplicationController:
    def __init__(self, config_path: str = 'config.yaml'):
        
        self.logger = self._setup_logger()
        
        self._state = ControllerState.OFF
        self._current_task: Task | None = None
        self._state_lock = threading.Lock() # Protects state changes
        
        # Threads for monitoring and task execution
        
        self._monitor_thread = threading.Thread(target=self._monitor_devices_status, name="MonitorThread", daemon=True)
        self._monitor_stop_event = threading.Event()
        
        self.config: Dict = ApplicationController.get_devices_config(config_path) 
        self.devices: Dict[str, Device] = {}

        self._create_devices()
        self.logger.info("ApplicationController initialized with devices: " + ", ".join(self.devices.keys())) 
        self._monitor_thread.start()

    
    def _setup_logger(self):
        """Creates a unique, rotating log file for this specific device."""
        # Ensure the log directory exists
        os.makedirs("logs/app", exist_ok=True)
        
        logger = logging.getLogger(f"Logger_ApplicationController")
        logger.setLevel(logging.DEBUG) # Capture everything for local files

        if not logger.handlers:
            file_path = f"logs/app/ApplicationController.log"
            handler = RotatingFileHandler(file_path, maxBytes=5*1024*1024, backupCount=2)
            formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            
        return logger
    
    def _create_devices(self):
        for device_name, device_info in self.config.get('devices', {}).items():
            device_type = device_info.get('type', '').lower()
            if device_type == 'mecademic':
                from devices import MecaRobot
                self.logger.info(f"Creating Mecademic Robot API for device: {device_name}")
                device = MecaRobot(ip_address=device_info.get('ip_address', ''), name=device_name)
                self.devices[device_name] = device
            
            #elif device_type == 'arduino':
            #    from devices import Arduino
            #    self.logger.info(f"Creating Arduino IO API for device: {device_name}")
            #    import accessories_api.ArduinoBoard as arduino_api_module
            #    # Placeholder for actual Arduino API creation
            #    arduino_api = arduino_api_module.ArduinoBoard(port=device_info.get('Port', 'COM3'))
            #    
            #elif device_type == 'zaber':
            #    self.logger.info(f"Creating Zaber Stage API for device: {device_name}")
            #    import accessories_api.ZaberAxis as zaber_api_module
            #    # Placeholder for actual Zaber API creation
            #    zaber_api = zaber_api_module.ZaberAxis(port=device_info.get('Port', 'COM3'))
            #    
            #elif device_type == 'planarmotor':
            #    self.logger.info(f"Creating Planar Motor API for device: {device_name}")
            #    import accessories_api.PlanarMotor as planar_motor_module
            #    planar_motor_api = planar_motor_module.PlanarMotor(add=device_info.get('ip_address', '192.168.10.200'))
            
            elif device_type == 'asyril':
                from devices import AsyrilEyePlus
                self.logger.info(f"Creating Asyril API for device: {device_name}")
                device = AsyrilEyePlus(ip_address=device_info.get('ip_address', ''), recipe=device_info.get('recipe', 0), name=device_name)
                self.devices[device_name] = device
                
            else:
                self.logger.warning(f"Unknown device type '{device_type}' for device '{device_name}'. Skipping API creation.")
                
    def initialize(self):
        """Perform initial setup and start the monitor."""
        self.set_state(ControllerState.INITIALIZING)   
        for _, device in self.devices.items():
            try:
                self.logger.info(f"Initializing device: {device.device_id}")
                device.initialize()
                    
            except Exception as e:
                self.logger.error(f"Initialization failed for device {device.device_id}: {e}", exc_info=True)
                self.set_state(ControllerState.FAULTED)
                raise Exception(f"Initialization failed for device {device.device_id}: {e}")
        
        self.logger.info("All devices Initialized and Ready.") 
        
        if not self._monitor_thread.is_alive():
            self.logger.warning("Monitor thread is not alive after initialization. Attempting to restart.")
            self._monitor_thread = threading.Thread(target=self._monitor_devices_status, name="MonitorThread", daemon=True)
            self._monitor_stop_event.clear()
            self._monitor_thread.start()
        
        self.set_state(ControllerState.READY)

    def set_state(self, new_state: ControllerState):
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
        
    def get_devices_info(self) -> List[Dict[str, Any]]:
        """
        Gathers and returns static information about ALL devices in a list of dictionaries.
        """
        all_info = {}
        
        # FIX 2: Iterate over all stored RobotInfo objects
        for _, device in self.devices.items():
            info = device.info
            all_info[device.device_id] = info

        return all_info

    # --- Task Management ---

    def start_task(self, task_type: TaskType):
        """Starts a new task if the robot is READY."""
        if self.get_state() != ControllerState.READY:
            self.logger.info(f"Cannot start task. Robot is {self.get_state().value}.")
            return False

        if self._current_task and self._current_task.is_alive():
             # Should not happen if state is READY, but good safety check
             self.logger.info("A task is already running.")
             return False

        # Start the new task
        self._current_task = Task(
            logger=self.logger,
            task_type=task_type, 
            state_change_callback=self.set_state, 
            devices=self.devices,
        )
        self._current_task.start()
        return True

    def stop_current_task(self):
        """Stops the currently running task."""
        if self.get_state() != ControllerState.BUSY or not self._current_task:
            self.logger.info("No active task to stop.")
            return
        
        self._current_task.stop()
        # The Task thread will handle the transition back to READY or FAULTED


    def abort_current_task(self):
        """Public: immediately abort the current task (user-initiated emergency stop)."""
        if not self._current_task or not self._current_task.is_alive():
            self.logger.info("No active task to abort.")
            return
        self._abort_current_task()

    def _abort_current_task(self):
        """Internal: abort the current task regardless of controller state (fault or user)."""
        if self._current_task and self._current_task.is_alive():
            self.logger.warning("Aborting current task due to device fault or stop request.")
            self._current_task.abort()
            # The Task thread will handle the transition back to READY or FAULTED

    # --- Monitoring Thread ---

    def _monitor_devices_status(self):
        """Dedicated thread to monitor the devices' hardware status."""
        while not self._monitor_stop_event.is_set():
            # self.logger.debug("Monitoring devices status...")
            if self.get_state() in [ControllerState.INITIALIZING, ControllerState.OFF]:
                # Skip monitoring during initialization or when off
                time.sleep(0.2)
                continue
            
            # Assume all devices must be healthy for the controller to be READY
            all_healthy = True
            all_ready = True
            
            for _, device in self.devices.items():
                if device.faulted:
                    if self.get_state() != ControllerState.FAULTED:
                        self.logger.warning(f"Device {device.device_id} is faulted. Transitioning controller to FAULTED state and aborting task.")
                        self.set_state(ControllerState.FAULTED)
                        self._abort_current_task()
                    all_healthy = False
                    all_ready = False
                    break # Break the inner loop, controller is faulted
            
                if not device.ready:
                    self.logger.warning(f"Device {device.device_id} is not ready. Controller cannot be READY.")
                    all_ready = False
                    break
            
            
            if all_healthy and all_ready and self.get_state() != ControllerState.BUSY and self.get_state() != ControllerState.INITIALIZING and self.get_state() != ControllerState.FAULTED:
                # Only return to READY if monitoring thread detects no issues AND no task is running
                self.set_state(ControllerState.READY)

            if self._current_task and self._current_task.is_done() and self._current_task.is_alive():
                # Handle cases where Task finished but thread is still cleaning up
                self._current_task.join()
                self._current_task = None
            
            time.sleep(0.2) # Check frequency

        self.logger.warning("[MonitorThread] Shutdown complete.")
        
    def _check_reference_position(self):
        pass
    
    def clear_faults(self):
        for _, device in self.devices.items():
            try:
                device.clear_fault()
            except Exception as e:
                self.logger.warning(f"Error clearing faults on device {device.device_id}: {e}")
        
    def shutdown(self):
        """Gracefully stop all threads and clean up."""
        self.logger.info("Shutting down Robot Controller...")
        self.set_state(ControllerState.FAULTED)
        self._monitor_stop_event.set()
        if self._current_task and self._current_task.is_alive():
            self.stop_current_task()
            self._current_task.join(timeout=5) # Wait for task to finish gracefully
        
        # Wait for monitor thread
        self._monitor_thread.join(timeout=10)
        
        if self._monitor_thread.is_alive():
            self.logger.warning("Monitor thread did not shut down gracefully.")
            raise Exception("Monitor thread did not shut down gracefully.")
        
        for _, device in self.devices.items():
            try:
                device.shutdown()
            except Exception as e:
                self.logger.warning(f"Error during shutdown of device {device.device_id}: {e}")

        self.set_state(ControllerState.OFF) # Final state after shutdown
        self.logger.info("Controller shutdown complete.")
        
        
    @staticmethod
    # FIX 3: Remove 'self' from static method definition
    def get_devices_config(config_file_path: str = 'config.yaml') -> Dict[str, Any]:
        """
        Loads and returns the configuration data from the specified YAML file.
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

def example_usage():
    controller = ApplicationController(config_path='config.yaml')
    controller.initialize()
    print(controller.get_devices_info())
    
if __name__ == "__main__":
    example_usage()