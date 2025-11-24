from enum import Enum

# --- Enums for State Management ---

class TaskType(Enum):
    """Defines the types of tasks the robot can execute."""
    HOME = "Home"
    SHIPMENT = "Shipment"
    PROD = "Production"