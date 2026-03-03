from typing import Dict

from devices import Device

from devices import MecaRobot, AsyrilEyePlus

def calib(devices: Dict[str, Device]):
    """Logic for PROD task."""

    CalibPoses_x = [180, 180, 115, 115]
    CalibPoses_y =[-100, 60, -100, 60]
    
    scara:MecaRobot = devices["scara"]
    asyril:AsyrilEyePlus = devices["asyril_1"]

    asyril.api.start_calibration()

    if not asyril.api._in_calib:
        raise Exception("Failed to start calibration")

    for i in range(4):
        scara.api.SetVariable("CalibPoses.x", CalibPoses_x[i])
        scara.api.SetVariable("CalibPoses.y", CalibPoses_y[i])
        asyril.api.set_calibration_pose(CalibPoses_x[i], CalibPoses_y[i])
        scara.api.StartProgram("calib_place")
        scara.api.WaitIdle()
        asyril.api.take_calibration_image()
        scara.api.StartProgram("calib_pick")
        scara.api.WaitIdle()

    asyril.api.calibrate()

