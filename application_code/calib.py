from devices import Device
from typing import Dict

from devices import LMISensor, MecaRobot, AsyrilEyePlus

def calib(devices: Dict[str, Device]):
    """Logic for CALIB task."""
    CalibPoses_x = [42, 42, 0, 0]
    CalibPoses_y =[-170, -200, -170, -200]
    
    scara:MecaRobot = devices["scara"]
    asyril:AsyrilEyePlus = devices["asyril"]

    asyril.api.stop_production()
    asyril.api.start_calibration(13186)

    if not asyril.api._in_calib:
        raise Exception("Failed to start calibration")

    # scara.api.StartProgram("calib_pick_part")
    scara.api.WaitIdle()

    for i in range(4):
        scara.api.SetVariable("CalibPoses.x", CalibPoses_x[i])
        scara.api.SetVariable("CalibPoses.y", CalibPoses_y[i])
        asyril.api.set_calibration_pose(CalibPoses_x[i], CalibPoses_y[i])
        scara.api.StartProgram("calib_place")
        scara.api.WaitIdle()
        asyril.api.take_calibration_image()
        if i < 3:
            scara.api.StartProgram("calib_pick")
            scara.api.WaitIdle()

    asyril.api.calibrate()

    asyril.api.start_production()
    return

    return