from enum import Enum

# --- Enums for State Management ---

class RobotState(Enum):
    """Defines the operational status of the robot controller."""
    INITIALIZING = "Initializing"
    READY = "Ready"
    BUSY = "Busy"
    FAULTED = "Faulted"

