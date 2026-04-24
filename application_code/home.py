from devices import Device
from typing import Dict

from devices import MecaRobot

def home(devices: Dict[str, Device]):
    """Logic for HOME task."""
    for device_name, device in devices.items():
        if isinstance(device, MecaRobot.MecaRobot):
            device.api.SetJointVel(40)
            device.api.MoveJoints(0, 0, 0, 0, 0, 0)
            device.logger.info(f"Sent MoveJoints command to {device_name} for zero position.")
            device.api.WaitIdle()
    return