from typing import Dict
from threading import Thread
import time

from devices import Device
from devices import LMISensor


def prod_cycle(devices: Dict[str, Device], index:int):
    """Logic for PROD task."""
    
    lmi_sensor: LMISensor = devices["my_lmi_sensor"]
    
    result = lmi_sensor.api.get_result(3, 4)  # Get the latest measurement from sensor 0
    lmi_sensor.logger.info(f"Latest measurement from sensor 0: {result}")
    

    time.sleep(1)  # Simulate time before the next trail starts

    return
