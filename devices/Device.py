import logging
from logging.handlers import RotatingFileHandler
import os

from abc import ABC, abstractmethod

class Device(ABC):
    """
    Abstract base class for all devices in the robotic cell.

    Every device (robot, feeder, motion system, …) must inherit from this class
    and implement all abstract members listed below.

    Constructor argument
    --------------------
    device_id : str
        Unique name for this device instance (used as the logger name and log
        file stem, e.g. ``"meca_robot_1"``).

    Attributes set by __init__
    --------------------------
    device_id : str
        The identifier passed at construction time.
    logger : logging.Logger
        A dedicated rotating-file logger at ``logs/devices/<device_id>.log``.

    Abstract properties  (must be implemented as @property)
    --------------------------------------------------------
    info -> dict
        Static device information (model, serial number, IP address, …).
        Returned as a plain dict; all values must be JSON-serialisable.
    connected -> bool
        ``True`` if the device currently has an active communication link.
    ready -> bool
        ``True`` if the device is connected and ready to accept commands.
    faulted -> bool
        ``True`` if the device is in an error/fault state.
    api
        The underlying driver / SDK object for direct hardware access.

    Abstract methods  (must be implemented)
    ----------------------------------------
    initialize()
        Open the connection, perform homing or startup sequence, and bring
        the device to a ``ready`` state.  Raise an exception on failure.
    shutdown()
        Gracefully close the connection and release all resources.
        Must be safe to call even if the device was never initialised.
    clear_fault()
        Reset the fault condition so the device can return to ``ready``.
        Implement as a no-op (``pass``) if the device has no fault-clearing
        mechanism.
    abort()
        Immediately interrupt any in-progress operation (e.g. clear the
        motion queue on a robot so that a blocking ``WaitIdle()`` call
        returns right away).  Called by the task system on emergency stop
        or when any device in the cell faults.

    Example
    -------
    >>> class MyDevice(Device):
    ...     @property
    ...     def info(self):    return {"ip_address": "192.168.0.1"}
    ...     @property
    ...     def connected(self): return self._connected
    ...     @property
    ...     def ready(self):   return self._connected and not self._faulted
    ...     @property
    ...     def faulted(self): return self._faulted
    ...     @property
    ...     def api(self):     return self._driver
    ...     def initialize(self):  ...
    ...     def shutdown(self):    ...
    ...     def clear_fault(self): ...
    ...     def abort(self):       ...
    """
    
    def __init__(self, device_id: str):
        """
        This parent __init__ runs every time a child class is created.
        It automatically sets up the dedicated logger.
        """
        self.device_id = device_id
        self.logger = self._setup_logger()
        self.logger.info(f"[{self.device_id}] device created.")

    def _setup_logger(self):
        """Creates a unique, rotating log file for this specific device."""
        # Ensure the log directory exists
        os.makedirs("logs/devices", exist_ok=True)
        
        logger = logging.getLogger(f"Logger_{self.device_id}")
        logger.setLevel(logging.DEBUG) # Capture everything for local files

        if not logger.handlers:
            file_path = f"logs/devices/{self.device_id}.log"
            handler = RotatingFileHandler(file_path, maxBytes=5*1024*1024, backupCount=2)
            formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            
        return logger
    
    def __del__(self):
        self.logger.info(f"[{self.device_id}] device is being deleted.")
        try:
            self.shutdown()
        except Exception as e:
            pass  # Avoid raising exceptions during garbage collection
        
    @property
    @abstractmethod
    def info(self):
        """
        Access general information about the device.
        This should return a dictionary or structured data containing relevant information about the device.
        """
        pass
    
    @property
    @abstractmethod
    def connected(self):
        """
        Check for connection status.
        This should return True if connected, False otherwise.
        """
        pass
    
    @property
    @abstractmethod
    def ready(self):
        """
        Check if the device is ready for operation.
        This should return True if ready, False otherwise.
        """
        pass
    
    @property
    @abstractmethod
    def faulted(self):
        """
        Check if the device is in a faulted state.
        This should return True if faulted, False otherwise.
        """
        pass
    
    @property
    @abstractmethod
    def api(self):
        """
        Access the device's API or interface for control.
        This should return the API object or interface for the device.
        """
        pass

    @abstractmethod
    def initialize(self):
        pass
    
    @abstractmethod
    def shutdown(self):
        pass

    @abstractmethod
    def clear_fault(self):
        # TO DO
        # To be implemented by accessories that support fault clearing, if needed
        pass

    @abstractmethod
    def abort(self):
        """
        Immediately abort any ongoing operation on the device.
        Called when a fault is detected on any device to halt the whole cell.
        """
        pass