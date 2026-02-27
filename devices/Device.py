import logging
from logging.handlers import RotatingFileHandler
import os

from abc import ABC, abstractmethod

class Device(ABC):
    """
    Base class for all devices in the system, including the robot and accessories.
    This class defines the common interface and properties that all devices must implement.
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
        os.makedirs("device_logs", exist_ok=True)
        
        logger = logging.getLogger(f"Logger_{self.device_id}")
        logger.setLevel(logging.DEBUG) # Capture everything for local files

        if not logger.handlers:
            file_path = f"device_logs/{self.device_id}.log"
            handler = RotatingFileHandler(file_path, maxBytes=5*1024*1024, backupCount=2)
            formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            
        return logger
    
    def __del__(self):
        self.logger.info(f"[{self.device_id}] device is being deleted.")   
        self.shutdown()
        
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