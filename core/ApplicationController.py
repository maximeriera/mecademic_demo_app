
"""
core/ApplicationController.py
------------------------------
Central orchestrator for the robotic cell.

The ``ApplicationController`` owns all :class:`~devices.Device` instances,
drives the :class:`~core.ControllerState` state machine, and manages the
lifecycle of :class:`~core.Task` threads.  It also runs a lightweight
background monitor thread that polls every device for faults and automatically
aborts the running task if any device enters an error state.

Typical lifecycle
-----------------
1. Instantiate: ``ctrl = ApplicationController()``
2. Initialize: ``ctrl.initialize()``  →  state becomes ``READY``
3. Start tasks: ``ctrl.start_task(TaskType.PROD)``  →  state becomes ``BUSY``
4. Stop / abort: ``ctrl.stop_current_task()`` or ``ctrl.abort_current_task()``
5. Shutdown: ``ctrl.shutdown()``  →  state becomes ``OFF``
"""

import os
import threading
import time
import yaml

from typing import Dict, Any

import logging
from logging.handlers import RotatingFileHandler

from devices import Device

from .Task import Task, TaskType
from .ControllerState import ControllerState

class ApplicationController:
    """
    Orchestrates the full robotic cell: device creation, state management,
    task execution, fault monitoring, and graceful shutdown.

    Parameters
    ----------
    config_path : str
        Path to the YAML configuration file that declares all devices.
        Defaults to ``'config.yaml'`` (relative to the current working
        directory).

    Attributes
    ----------
    devices : Dict[str, Device]
        Map of ``device_id → Device`` for every device loaded from config.
    config : Dict
        Raw configuration dictionary loaded from ``config_path``.

    State machine
    -------------
    OFF  ──► INITIALIZING  ──► READY  ──► BUSY
                ▲         
                └─ FAULTED
    """

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
        """Create a rotating log file at ``logs/app/ApplicationController.log``.

        Returns
        -------
        logging.Logger
        """
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
        """Instantiate all devices declared in the config and store them in
        :attr:`devices`.

        Supported ``type`` values (case-insensitive):
        ``mecademic``, ``asyril``, ``arduino``, ``planarmotor``, ``iologik``.
        Unknown types are skipped with a warning.
        """
        for device_name, device_info in self.config.get('devices', {}).items():
            device_type = device_info.get('type', '').lower()
            if device_type == 'mecademic':
                from devices import MecaRobot
                self.logger.info(f"Creating Mecademic Robot API for device: {device_name}")
                device = MecaRobot(ip_address=device_info.get('ip_address', ''), name=device_name)
                self.devices[device_name] = device
            
            #elif device_type == 'zaber':
            #    self.logger.info(f"Creating Zaber Stage API for device: {device_name}")
            #    import accessories_api.ZaberAxis as zaber_api_module
            #    # Placeholder for actual Zaber API creation
            #    zaber_api = zaber_api_module.ZaberAxis(port=device_info.get('Port', 'COM3'))
            #    
            elif device_type == 'planarmotor':
                from devices import PlanarMotor
                self.logger.info(f"Creating Planar Motor API for device: {device_name}")
                device = PlanarMotor(ip_address=device_info.get('ip_address', '192.168.10.200'), name=device_name)
                self.devices[device_name] = device

            elif device_type == 'asyril':
                from devices import AsyrilEyePlus
                self.logger.info(f"Creating Asyril API for device: {device_name}")
                device = AsyrilEyePlus(ip_address=device_info.get('ip_address', ''), recipe=device_info.get('recipe', 0), name=device_name)
                self.devices[device_name] = device
                
            elif device_type == 'arduino':
                from devices import ArduinoBoard
                self.logger.info(f"Creating Arduino IO API for device: {device_name}")
                device = ArduinoBoard(port=device_info.get('port', 'COM3'), name=device_name)
                self.devices[device_name] = device

            elif device_type == 'iologik':
                from devices import IoLogikE1212
                self.logger.info(f"Creating ioLogik E1212 API for device: {device_name}")
                device = IoLogikE1212(
                    ip_address=device_info.get('ip_address', ''),
                    port=device_info.get('port', 502),
                    slave_id=device_info.get('slave_id', 1),
                    name=device_name,
                )
                self.devices[device_name] = device

            else:
                self.logger.warning(f"Unknown device type '{device_type}' for device '{device_name}'. Skipping API creation.")
                
    def initialize(self):
        """Connect and initialise every device, then transition to ``READY``.

        Steps
        -----
        1. Set state to ``INITIALIZING``.
        2. Call :meth:`~devices.Device.initialize` on each device in order.
        3. Restart the monitor thread if it is not alive.
        4. Set state to ``READY``.

        Raises
        ------
        Exception
            Re-raised from the failing device's ``initialize()`` after the
            controller is transitioned to ``FAULTED``.
        """
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

    def set_state(self, new_state: ControllerState) -> ControllerState:
        """Thread-safe state transition.

        Logs the transition only when the state actually changes.

        Parameters
        ----------
        new_state : ControllerState
            The desired next state.

        Returns
        -------
        ControllerState
            The current state after the call (may be unchanged).
        """
        with self._state_lock:
            if self._state != new_state:
                self.logger.info(f"--- State Change: {self._state.value} -> {new_state.value} ---")
                self._state = new_state
            return self._state

    def get_state(self) -> ControllerState:
        """Return the current controller state (thread-safe)."""
        with self._state_lock:
            return self._state
        
    def get_devices_info(self) -> Dict[str, Dict[str, Any]]:
        """Return static info for every device, keyed by ``device_id``.

        Each value is the dict returned by :attr:`~devices.Device.info`.
        Used by the ``/api/info`` endpoint to populate the device cards.

        Returns
        -------
        Dict[str, Dict[str, Any]]
            ``{ device_id: { ...info fields... } }``
        """
        all_info = {}
        
        # FIX 2: Iterate over all stored RobotInfo objects
        for _, device in self.devices.items():
            info = device.info
            all_info[device.device_id] = info

        return all_info

    # --- Task Management ---

    def start_task(self, task_type: TaskType) -> bool:
        """Spawn a new :class:`~core.Task` thread if the controller is ``READY``.

        Parameters
        ----------
        task_type : TaskType
            The task to execute (``HOME``, ``SHIPMENT``, ``PROD``, ``CALIBRATION``).

        Returns
        -------
        bool
            ``True`` if the task was successfully started, ``False`` if the
            controller is not in ``READY`` state or a task is already running.
        """
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
        """Gracefully stop the running task (user-initiated).

        Signals the task to stop after its current cycle completes, then runs
        the home sequence before returning to ``READY``.  Has no effect if
        the controller is not in ``BUSY`` state.
        """
        if self.get_state() != ControllerState.BUSY or not self._current_task:
            self.logger.info("No active task to stop.")
            return
        
        self._current_task.stop()
        # The Task thread will handle the transition back to READY or FAULTED


    def abort_current_task(self):
        """Immediately abort the running task (user-initiated emergency stop).

        Calls :meth:`~core.Task.abort` which sets the stop flag **and** calls
        :meth:`~devices.Device.abort` on every device, unblocking any in-flight
        hardware call (e.g. ``WaitIdle()``) right away.  Has no effect if no
        task is currently alive.
        """
        if not self._current_task or not self._current_task.is_alive():
            self.logger.info("No active task to abort.")
            return
        self._abort_current_task()

    def _abort_current_task(self):
        """Internal abort — used by both the fault monitor and :meth:`abort_current_task`.

        Safe to call regardless of the current controller state.
        """
        if self._current_task and self._current_task.is_alive():
            self.logger.warning("Aborting current task due to device fault or stop request.")
            self._current_task.abort()
            # The Task thread will handle the transition back to READY or FAULTED

    # --- Monitoring Thread ---

    def _monitor_devices_status(self):
        """Background monitor thread — polls device health every 200 ms.

        Behaviour
        ---------
        - Skips polling while the controller is ``OFF`` or ``INITIALIZING``.
        - If any device reports ``faulted == True``, transitions to ``FAULTED``
          and calls :meth:`_abort_current_task` to interrupt the running task.
        - If all devices are healthy and ready and no task is running, promotes
          state back to ``READY`` (covers automatic recovery after clear-fault).
        - Joins and clears :attr:`_current_task` once the task thread exits.

        Stops when :attr:`_monitor_stop_event` is set (via :meth:`shutdown`).
        """
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
        """Call :meth:`~devices.Device.clear_fault` on every device.

        Errors from individual devices are logged as warnings and do not
        prevent the remaining devices from being cleared.
        """
        for _, device in self.devices.items():
            try:
                device.clear_fault()
            except Exception as e:
                self.logger.warning(f"Error clearing faults on device {device.device_id}: {e}")
        
    def shutdown(self):
        """Gracefully shut down the controller and all devices.

        Steps
        -----
        1. Set state to ``FAULTED`` so no new tasks can start.
        2. Signal the monitor thread to stop.
        3. Stop the running task (if any) and wait up to 5 s.
        4. Wait up to 10 s for the monitor thread to exit.
        5. Call :meth:`~devices.Device.shutdown` on each device.
        6. Set state to ``OFF``.

        Raises
        ------
        Exception
            If the monitor thread does not exit within the timeout.
        """
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
    def get_devices_config(config_file_path: str = 'config.yaml') -> Dict[str, Any]:
        """Load and return the raw configuration from a YAML file.

        Parameters
        ----------
        config_file_path : str
            Path to the YAML file.  Defaults to ``'config.yaml'``.

        Returns
        -------
        Dict[str, Any]
            Parsed YAML content, or an empty dict if the file is empty.

        Raises
        ------
        FileNotFoundError
            If the file does not exist at the given path.
        yaml.YAMLError
            If the file is not valid YAML.
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