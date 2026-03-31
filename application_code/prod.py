from typing import Dict
from threading import Thread
import time

from devices import Device
from devices import IoLogikE1212

JOINT_VEL = 15
CARTLIN_VEL = 100
CARTANG_VEL = 45

PARTS_PER_TRAIL = [5, 6, 9, 10]
# PARTS_PER_TRAIL = range(16)
TRAILS_OFFSET = 20

def prod_cycle(devices: Dict[str, Device], index:int):
    """Logic for PROD task."""
    
    io:IoLogikE1212 = devices["remote_io"]
    
    io.api.write_do(0, True)  # Start signal for the trail
    
    time.sleep(1)  # Simulate time for the trail to be completed
    
    io.api.write_do(0, False)  # End signal for the trail

    time.sleep(1)  # Simulate time before the next trail starts

    return
