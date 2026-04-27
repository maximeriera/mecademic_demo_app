from devices import Device
from typing import Dict


from devices import MecaRobot

def shipment(devices: Dict[str, Device]):
    """Logic for SHIPMENT task."""
    for device_name, device in devices.items():
        if isinstance(device, MecaRobot.MecaRobot):
            device.api.SetJointVel(40)
            device.api.MoveJoints(0, -60, 60, 0, 30, 0)
            device.logger.info(f"Sent MoveJoints command to {device_name} for zero position.")
            device.api.WaitIdle()
    return