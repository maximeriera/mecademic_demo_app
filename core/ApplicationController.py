
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

from importlib import import_module
from typing import Dict, Any

import logging
from logging.handlers import RotatingFileHandler

from devices import Device

from .Task import Task, TaskType
from .ControllerState import ControllerState
from .AppContext import AppContext
from .ContextStore import ContextStore
from .DataHub import DataHub
from .ProductionMetrics import ProductionMetrics
from .StepTracker import StepTracker
from .ManualActions import load_manual_actions_registry, normalize_manual_actions_config
from .LogSetup import get_controller_logger

#: Workspace module candidates for the optional custom view, tried in order
#: (same flat-then-package convention as ``Task.TASK_FUNCTION_MODULES``).
CUSTOM_VIEW_MODULES = ("custom_view", "app_logic.custom_view")

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

    def __init__(self, config_path: str = 'config.yaml', context_path: str | None = None):
        
        self.logger = self._setup_logger()
        
        self._state = ControllerState.OFF
        self._current_task: Task | None = None
        self._state_lock = threading.Lock() # Protects state changes
        
        # Threads for monitoring and task execution
        
        self._monitor_thread = threading.Thread(target=self._monitor_devices_status, name="MonitorThread", daemon=True)
        self._monitor_stop_event = threading.Event()
        self._last_not_ready_device_id: str | None = None
        
        self.logger.info(f"Loading controller config from: {config_path}")
        self.config: Dict = ApplicationController.get_devices_config(config_path) 
        self.devices: Dict[str, Device] = {}
        self._manual_actions = self._load_manual_actions_config()
        
        self.context = self._create_context(config_path, context_path)

        self._create_devices()
        self.logger.info("ApplicationController initialized with devices: " + ", ".join(self.devices.keys())) 
        self._monitor_thread.start()

    def _resolve_context_path(self, config_path: str, context_path: str | None) -> str | None:
        """Locate the optional context file. Relative paths resolve against config.yaml's folder.

        Order: explicit ``context_path`` -> ``context_file`` in config.yaml
        (default ``context.yaml``) -> legacy ``production_context_file`` /
        ``production_context.yaml``. ``None`` means the workspace has no
        context, which is a supported setup, not an error.
        """
        if context_path:
            return context_path

        config_dir = os.path.dirname(os.path.abspath(config_path))

        def resolve(path: str) -> str:
            return path if os.path.isabs(path) else os.path.join(config_dir, path)

        configured = self.config.get('context_file')
        if configured:
            # Returned even when missing, so the Variables tab reports the
            # broken path instead of silently showing "no context".
            return resolve(str(configured))

        default_path = resolve('context.yaml')
        if os.path.isfile(default_path):
            return default_path

        legacy_configured = self.config.get('production_context_file')
        legacy_path = resolve(str(legacy_configured or 'production_context.yaml'))
        if legacy_configured or os.path.isfile(legacy_path):
            self.logger.warning(
                "Using legacy context file %s. Rename it to context.yaml (or set `context_file:` "
                "in config.yaml); `production_context_file` / production_context.yaml are deprecated.",
                legacy_path,
            )
            return legacy_path

        return None

    def _create_context(self, config_path: str, context_path: str | None) -> AppContext:
        """Build the app-wide context: optional params/variables, step tracking, PROD metrics."""
        resolved = self._resolve_context_path(config_path, context_path)
        if resolved is None:
            self.logger.info(
                "No context file (context.yaml) in the workspace: params/variables are disabled. "
                "Step tracking and production metrics work as usual."
            )
        store = ContextStore(self.logger, resolved)

        sequences = self.config.get('sequences')
        if sequences is None and store.legacy_sequences is not None:
            self.logger.warning(
                "Reading `sequences:` from %s (legacy location). Move the block to config.yaml.",
                store.path,
            )
            sequences = store.legacy_sequences
        elif sequences is not None and store.legacy_sequences is not None:
            self.logger.warning(
                "`sequences:` in %s is ignored: config.yaml defines its own.", store.path,
            )

        steps = StepTracker(self.logger, StepTracker.normalize_sequences(sequences, self.logger))
        return AppContext(store=store, steps=steps, metrics=ProductionMetrics(self.logger),
                          data=DataHub(self.logger))

    def _start_custom_view(self) -> None:
        """Load the workspace's optional ``custom_view`` module and start its data sources.

        Never raises. The custom view is a demo add-on: no module simply leaves
        it disabled, and a broken one is reported through
        ``context.data.setup_error`` while the controller still reaches READY.
        """
        hub = self.context.data
        module = None
        for name in CUSTOM_VIEW_MODULES:
            try:
                module = import_module(name)
                break
            except Exception as e:
                # Only "this candidate does not exist" means try the next one; a
                # missing import *inside* the module is a real setup error.
                if (isinstance(e, ModuleNotFoundError) and e.name
                        and (name == e.name or name.startswith(e.name + "."))):
                    continue
                hub.setup_error = f"{type(e).__name__}: {e}"
                self.logger.error("Custom view module '%s' failed to import: %s", name, e, exc_info=True)
                return

        if module is None:
            self.logger.info("No custom_view module in the workspace: custom view disabled.")
            return

        setup = getattr(module, "setup", None)
        if not callable(setup):
            hub.setup_error = f"{module.__name__} has no setup(hub, devices) function."
            self.logger.error(hub.setup_error)
            return

        try:
            setup(hub, self.devices)
            hub.start()
            self.logger.info("Custom view '%s' started.", module.__name__)
        except Exception as e:
            hub.clear()
            hub.setup_error = f"{type(e).__name__}: {e}"
            self.logger.error("Custom view setup failed: %s", e, exc_info=True)

    def _load_manual_actions_config(self) -> list[dict[str, str]]:
        """Load and validate manual action metadata from config and workspace registry."""
        configured_actions = normalize_manual_actions_config(self.config.get("manual_actions"))
        if not configured_actions:
            self.logger.info("No manual actions configured in config.yaml.")
            return []

        registry = load_manual_actions_registry(logger=self.logger)
        configured_keys = [item["key"] for item in configured_actions]
        self.logger.info(
            "Manual actions configured in YAML: %s | registry keys discovered: %s",
            configured_keys,
            sorted(registry.keys()),
        )
        missing = [item["key"] for item in configured_actions if item["key"] not in registry]
        if missing:
            missing_txt = ", ".join(missing)
            raise ValueError(
                "Manual actions configured in config.yaml are missing in manual action registry: "
                f"{missing_txt}"
            )

        self.logger.info(f"Manual actions ready: {configured_keys}")
        return configured_actions

    
    def _setup_logger(self):
        """Return the shared controller logger (``logs/app/ApplicationController.log``).

        Configuration lives in :mod:`core.LogSetup` so the Flask layer, the
        controller and every device share one format, one combined log and one
        set of level switches.

        Returns
        -------
        logging.Logger
        """
        return get_controller_logger()
    
    def _create_devices(self):
        """Instantiate all devices declared in the config and store them in
        :attr:`devices`.

        Supported ``type`` values (case-insensitive):
        ``mecademic``, ``asyril``, ``arduino``, ``planarmotor``, ``iologik``,
        ``brainboxes_ed_digital``, ``zaber``, ``gige_camera``, ``lmi``,
        ``thorlabs_pm`` (alias: ``thorlabs_power_meter``),
        ``smaract_mcs2`` (aliases: ``mcs2``, ``smaract``),
        ``simulated`` (aliases: ``dummy``, ``fake``).
        Unknown types are skipped with a warning.
        """
        devices_config = self.config.get('devices', {})
        if devices_config is None:
            devices_config = {}
        for device_name, device_info in devices_config.items():
            device_type = device_info.get('type', '').lower()
            if device_type == 'mecademic':
                from devices.MecaRobot import MecaRobot
                self.logger.info(f"Creating Mecademic Robot API for device: {device_name}")
                device = MecaRobot(ip_address=device_info.get('ip_address', ''), name=device_name)
                self.devices[device_name] = device
            
            elif device_type == 'zaber':
                from devices.OLD_ZaberAxis import ZaberAxis
                self.logger.info(f"Creating Zaber Stage API for device: {device_name}")
                device = ZaberAxis(
                    port=device_info.get('port', 'COM3'),
                    axis_number=device_info.get('axis_number', 1),
                    name=device_name,
                )
                self.devices[device_name] = device

            elif device_type == 'planarmotor':
                from devices.PlanarMotor import PlanarMotor
                self.logger.info(f"Creating Planar Motor API for device: {device_name}")
                device = PlanarMotor(ip_address=device_info.get('ip_address', '192.168.10.200'), name=device_name)
                self.devices[device_name] = device

            elif device_type == 'asyril':
                from devices.Asyril import AsyrilEyePlus
                self.logger.info(f"Creating Asyril API for device: {device_name}")
                device = AsyrilEyePlus(ip_address=device_info.get('ip_address', ''), recipe=device_info.get('recipe', 0), name=device_name)
                self.devices[device_name] = device
                
            elif device_type == 'arduino':
                from devices.ArduinoBoard import ArduinoBoard
                self.logger.info(f"Creating Arduino IO API for device: {device_name}")
                device = ArduinoBoard(port=device_info.get('port', 'COM3'), name=device_name)
                self.devices[device_name] = device

            elif device_type == 'iologik':
                from devices.IoLogikE1212 import IoLogikE1212
                self.logger.info(f"Creating ioLogik E1212 API for device: {device_name}")
                device = IoLogikE1212(
                    ip_address=device_info.get('ip_address', ''),
                    port=device_info.get('port', 502),
                    slave_id=device_info.get('slave_id', 1),
                    name=device_name,
                )
                self.devices[device_name] = device

            elif device_type in ('brainboxes_ed_digital', 'brainboxes_dio', 'ed_digital'):
                from devices.BrainboxesED import BrainboxesEDDigital
                self.logger.info(f"Creating Brainboxes ED Digital API for device: {device_name}")
                device = BrainboxesEDDigital(
                    ip_address=device_info.get('ip_address', ''),
                    address=device_info.get('address', 0x01),
                    port=device_info.get('port', 9500),
                    timeout=device_info.get('timeout', 5.0),
                    name=device_name,
                )
                self.devices[device_name] = device

            elif device_type == 'lmi':
                from devices.LMISensor import LMISensor
                self.logger.info(f"Creating LMI Sensor API for device: {device_name}")
                device = LMISensor(
                    ip_address=device_info.get('ip_address', ''),
                    control_port=device_info.get('control_port', 3190),
                    data_port=device_info.get('data_port', 3192),
                    health_port=device_info.get('health_port', 3194),
                    delimiter=device_info.get('delimiter', ','),
                    terminator=device_info.get('terminator', '\r\n'),
                    name=device_name,
                )
                self.devices[device_name] = device

            elif device_type == 'gige_camera':
                from devices.GigEVisionDevice import GigEVisionDevice

                cti_path = device_info.get('cti_path', '')
                if not cti_path:
                    raise ValueError(
                        f"Missing required 'cti_path' for gige_camera device '{device_name}'."
                    )

                self.logger.info(f"Creating GigE Vision camera API for device: {device_name}")
                device = GigEVisionDevice(
                    cti_path=cti_path,
                    camera_index=device_info.get('camera_index', 0),
                    autostart_stream=device_info.get('autostart_stream', False),
                    name=device_name,
                )
                self.devices[device_name] = device

            elif device_type in ('thorlabs_pm', 'thorlabs_power_meter'):
                from devices.ThorlabsPowerMeter import ThorlabsPowerMeter
                self.logger.info(f"Creating Thorlabs power meter API for device: {device_name}")
                device = ThorlabsPowerMeter(
                    resource=device_info.get('resource'),
                    backend=device_info.get('backend'),
                    timeout_ms=device_info.get('timeout_ms', 5000),
                    wavelength=device_info.get('wavelength'),
                    averaging=device_info.get('averaging'),
                    power_unit=device_info.get('power_unit'),
                    name=device_name,
                )
                self.devices[device_name] = device

            elif device_type in ('smaract_mcs2', 'mcs2', 'smaract'):
                from devices.SmarActMCS2 import SmarActMCS2
                self.logger.info(f"Creating SmarAct MCS2 API for device: {device_name}")
                device = SmarActMCS2(
                    ip_address=device_info.get('ip_address', ''),
                    port=device_info.get('port', 55551),
                    timeout=device_info.get('timeout', 5.0),
                    channels=device_info.get('channels'),
                    reference=device_info.get('reference', 'if_needed'),
                    reference_reverse=device_info.get('reference_reverse', False),
                    reference_timeout_s=device_info.get('reference_timeout_s', 120.0),
                    velocity_mm_s=device_info.get('velocity_mm_s'),
                    acceleration_mm_s2=device_info.get('acceleration_mm_s2'),
                    name=device_name,
                )
                self.devices[device_name] = device

            elif device_type in ('simulated', 'dummy', 'fake'):
                from devices.SimulatedDevice import SimulatedDevice
                self.logger.info(f"Creating Simulated (no-hardware) device for: {device_name}")
                device = SimulatedDevice(
                    name=device_name,
                    connect_delay=device_info.get('connect_delay', 0.0),
                    fail_to_connect=device_info.get('fail_to_connect', False),
                    fail_rate=device_info.get('fail_rate', 0.0),
                    info=device_info.get('info'),
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
        # No sampler may read a device while it (re)connects.
        self.context.data.clear()
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

        self._start_custom_view()
        self.context.apply_event("initialize")
        self.set_state(ControllerState.READY)

    def set_state(self, new_state: ControllerState) -> ControllerState:
        """Thread-safe state transition.

        Logs the transition only when the state actually changes.

        ``FAULTED`` is a latching state: it can only be left through
        ``INITIALIZING`` (re-initialisation / clear-faults) or ``OFF``
        (shutdown).  A direct ``FAULTED -> READY`` transition is refused.
        Without this guard a task that is aborted *because* a device faulted
        would clear the fault state on its way out: the monitor sets
        ``FAULTED`` and aborts the task, the task then unwinds cleanly and its
        ``finally`` block reports ``READY``, leaving a faulted cell advertised
        as ready to accept new tasks.

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
            if self._state == new_state:
                return self._state

            if self._state == ControllerState.FAULTED and new_state == ControllerState.READY:
                self.logger.warning(
                    "Refusing FAULTED -> READY transition. Clear faults or re-initialize the controller."
                )
                return self._state

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

    def start_task(self, task_type: TaskType, manual_action_key: str | None = None) -> bool:
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
        self.logger.info(
            "Task start requested: type=%s manual_action_key=%s state=%s",
            task_type.name,
            manual_action_key,
            self.get_state().value,
        )
        if self.get_state() != ControllerState.READY:
            self.logger.info(f"Cannot start task. Robot is {self.get_state().value}.")
            return False

        if self._current_task and self._current_task.is_alive():
             # Should not happen if state is READY, but good safety check
             self.logger.info("A task is already running.")
             return False

        # Start the new task
        if task_type == TaskType.PROD:
            self.context.apply_event("prod_start")
            self.context.metrics.mark_prod_running(True)

        self._current_task = Task(
            logger=self.logger,
            task_type=task_type, 
            state_change_callback=self.set_state, 
            devices=self.devices,
            context=self.context,
            manual_action_key=manual_action_key,
        )
        self.logger.info(
            "Starting task thread: name=%s type=%s manual_action_key=%s",
            self._current_task.name,
            task_type.name,
            manual_action_key,
        )
        self._current_task.start()
        return True

    def get_manual_actions(self) -> list[dict[str, str]]:
        """Return configured manual action metadata for UI rendering."""
        return [dict(item) for item in self._manual_actions]

    def start_manual_action(self, action_key: str) -> bool:
        """Start one configured manual action as an exclusive task."""
        action_key = str(action_key or "").strip()
        configured_keys = {item["key"] for item in self._manual_actions}
        self.logger.info(f"Manual action request received: key='{action_key}'")
        if action_key not in configured_keys:
            self.logger.warning(f"Unknown manual action requested: '{action_key}'")
            return False
        return self.start_task(TaskType.MANUAL_ACTION, manual_action_key=action_key)

    def stop_current_task(self):
        """Gracefully stop the running task (user-initiated).

        Signals the task to stop after its current cycle completes, then runs
        the home sequence before returning to ``READY``.  Has no effect if
        the controller is not in ``BUSY`` state.
        """
        if self.get_state() != ControllerState.BUSY or not self._current_task:
            self.logger.info("No active task to stop.")
            return
        
        self.logger.info(
            "Stop requested for task thread: name=%s type=%s",
            self._current_task.name,
            self._current_task.task_type.name,
        )
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
        self.logger.warning(
            "Abort requested for active task thread: name=%s type=%s",
            self._current_task.name,
            self._current_task.task_type.name,
        )
        self._abort_current_task()

    def _abort_current_task(self):
        """Internal abort — used by both the fault monitor and :meth:`abort_current_task`.

        Safe to call regardless of the current controller state.
        """
        if self._current_task and self._current_task.is_alive():
            self.logger.warning("Aborting current task due to device fault or stop request.")
            self.context.apply_event("abort")
            self.context.metrics.mark_prod_running(False)
            # Close the open step from THIS thread. The task thread may sit in a
            # non-interruptible SDK call for seconds, and until it unwinds the UI
            # would keep showing "Pick part - 48 s and counting" on a dead cell.
            self.context.steps.abandon_open_steps("aborted")
            self._current_task.abort()
            # The Task thread will handle the transition back to READY or FAULTED

    def get_current_task_label(self) -> str | None:
        """Display name of the running task ("Production", "Manual: Inspect Part"), or None.

        Read on the 10 Hz /api/status path. One attribute read and no lock: the
        monitor thread may clear ``_current_task`` at any moment, so it is read
        once into a local.
        """
        task = self._current_task
        if task is None or not task.is_alive():
            return None
        if task.task_type == TaskType.MANUAL_ACTION:
            label = next((a["label"] for a in self._manual_actions
                          if a["key"] == task.manual_action_key), None)
            return f"Manual: {label or task.manual_action_key or 'action'}"
        return task.task_type.value

    def get_live_step(self) -> Dict[str, Any] | None:
        """Current sequence step, or None when idle.

        Lock-free in StepTracker, so this is safe on the 10 Hz /api/status path.
        """
        return self.context.steps.get_live()

    def get_prod_snapshot(self) -> Dict[str, Any]:
        """PROD metrics and step state for the Production tab."""
        return {
            "state": self.get_state().value,
            "metrics": self.context.metrics.snapshot(),
            "steps": self.context.steps.snapshot(),
        }

    def get_context_snapshot(self) -> Dict[str, Any]:
        """Params/variables for the Variables tab. ``enabled`` is False without a context file."""
        snapshot = self.context.store.snapshot()
        snapshot["state"] = self.get_state().value
        snapshot["locked"] = self.get_state() == ControllerState.BUSY
        return snapshot

    def update_context(self, payload: Dict[str, Any]) -> tuple[list[str], list[str]]:
        """Apply value edits from the API. Refused while a task is running."""
        locked = self.get_state() == ControllerState.BUSY
        return self.context.store.update_from_payload(payload, locked=locked)

    def reset_context(self, namespace: str, key: str | None = None) -> None:
        """Reset params/variables to their defaults. Refused while a task is running."""
        locked = self.get_state() == ControllerState.BUSY
        self.context.store.reset(namespace, key=key, locked=locked)

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
                        self.context.apply_event("fault")
                        self.context.metrics.mark_prod_running(False)
                        self.context.steps.abandon_open_steps("faulted")
                        self.set_state(ControllerState.FAULTED)
                        self._abort_current_task()
                    all_healthy = False
                    all_ready = False
                    break # Break the inner loop, controller is faulted
            
                if not device.ready:
                    if self._last_not_ready_device_id != device.device_id:
                        self.logger.warning(f"Device {device.device_id} is not ready. Controller cannot be READY.")
                        self._last_not_ready_device_id = device.device_id
                    all_ready = False
                    break
            
            
            if all_healthy and all_ready and self.get_state() != ControllerState.BUSY and self.get_state() != ControllerState.INITIALIZING and self.get_state() != ControllerState.FAULTED:
                # Only return to READY if monitoring thread detects no issues AND no task is running
                if self._last_not_ready_device_id is not None:
                    self.logger.info("All devices report ready again.")
                self._last_not_ready_device_id = None
                self.set_state(ControllerState.READY)

            if self._current_task and self._current_task.is_done() and self._current_task.is_alive():
                # Handle cases where Task finished but thread is still cleaning up
                self.logger.info(f"Joining completed task thread: {self._current_task.name}")
                self._current_task.join()
                self._current_task = None
            
            time.sleep(0.2) # Check frequency

        self.logger.warning("[MonitorThread] Shutdown complete.")
        
    def _check_reference_position(self):
        pass
    
    def clear_faults(self) -> Dict[str, Any]:
        """Call :meth:`~devices.Device.clear_fault` on every device, then return
        to ``READY`` if the cell actually came back healthy.

        This is the *light* recovery path and deliberately moves no hardware.
        It used to call ``shutdown()`` followed by ``initialize()``, which
        disconnected every device and re-homed every robot — slow, physically
        surprising for an operator who only asked to clear a fault, and it left
        the cell stuck in ``OFF`` whenever ``shutdown()`` raised (its monitor
        join can time out), because ``initialize()`` never ran.  Use
        :meth:`initialize` when a full reconnect-and-home really is wanted;
        that is what the INITIALIZE button already does.

        Errors from individual devices are logged and reported, and do not
        prevent the remaining devices from being cleared.

        Returns
        -------
        Dict[str, Any]
            ``cleared``       — device ids whose ``clear_fault()`` succeeded.
            ``errors``        — ``{device_id: message}`` for devices that raised.
            ``still_faulted`` — device ids still reporting ``faulted`` afterwards.
            ``state``         — controller state after the attempt.
            ``success``       — ``True`` only if nothing errored, nothing is
                                still faulted, and the controller left ``FAULTED``.
        """
        cleared: list[str] = []
        errors: Dict[str, str] = {}

        # Never clear faults underneath a live task.
        if self._current_task and self._current_task.is_alive():
            self.logger.warning("Clear-faults requested while a task is still running. Aborting it first.")
            self._abort_current_task()
            self._current_task.join(timeout=5)

        for _, device in self.devices.items():
            try:
                device.clear_fault()
                cleared.append(device.device_id)
            except Exception as e:
                self.logger.warning(f"Error clearing faults on device {device.device_id}: {e}")
                errors[device.device_id] = str(e)

        # Re-poll health: clear_fault() only asks, it does not guarantee.
        still_faulted: list[str] = []
        for _, device in self.devices.items():
            try:
                if device.faulted:
                    still_faulted.append(device.device_id)
            except Exception as e:
                self.logger.warning(f"Could not read fault state of {device.device_id}: {e}")
                still_faulted.append(device.device_id)

        if still_faulted or errors:
            self.logger.warning(
                "Clear-faults incomplete. still_faulted=%s errors=%s. Controller stays %s.",
                still_faulted,
                sorted(errors),
                self.get_state().value,
            )
        else:
            # FAULTED is latching, so step out of it explicitly. Going through
            # INITIALIZING keeps the documented transition order intact without
            # reconnecting or re-homing anything.
            if self.get_state() == ControllerState.FAULTED:
                self.set_state(ControllerState.INITIALIZING)
                self._last_not_ready_device_id = None
                self.set_state(ControllerState.READY)
            self.logger.info("Faults cleared on all devices. Controller is %s.", self.get_state().value)

        state = self.get_state()
        return {
            "cleared": cleared,
            "errors": errors,
            "still_faulted": still_faulted,
            "state": state.value,
            "success": not errors and not still_faulted and state != ControllerState.FAULTED,
        }
        
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
        self.context.metrics.mark_prod_running(False)
        self.set_state(ControllerState.FAULTED)
        self._monitor_stop_event.set()
        if self._current_task and self._current_task.is_alive():
            # Abort, not stop. Two reasons:
            #
            # 1. stop_current_task() returns immediately unless the state is
            #    BUSY, and we just set FAULTED above — so it never signalled
            #    the task at all. The join below then burned its full timeout
            #    and shutdown carried on disconnecting devices with the task
            #    thread still mid-cycle.
            # 2. Even if it did signal, stop() only ends the PROD loop
            #    *between* cycles. We are about to drop the device
            #    connections, so leaving queued motion running is worse than
            #    interrupting it; abort() also unblocks any in-flight
            #    blocking SDK call (e.g. WaitIdle()).
            self._abort_current_task()
            self._current_task.join(timeout=5)
            if self._current_task.is_alive():
                self.logger.warning(
                    "Task thread %s did not exit within 5s; continuing with device shutdown.",
                    self._current_task.name,
                )


        # Samplers must be gone before their devices disconnect.
        self.context.data.stop()

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