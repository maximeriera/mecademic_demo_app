from enum import Enum

# --- Enums for State Management ---

class ControllerState(Enum):
    """Defines the operational status of the robot controller."""
    OFF = "Off"
    INITIALIZING = "Initializing"
    READY = "Ready"
    BUSY = "Busy"
    FAULTED = "Faulted"