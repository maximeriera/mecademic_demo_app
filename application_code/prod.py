from devices import Device
from typing import Dict

import time

from devices import LMISensor, MecaRobot, AsyrilEyePlus

SCAN_RETRY_NUMBER = 3

SCARA_VACUUM_THRESHOLD = 60

def scara_part_in_hand(devices: Dict[str, Device], index:int=0):
    scara:MecaRobot = devices["scara"]
    parts_in_hand = False
    scara.api.GetRtVacuumPressure()

    if abs(scara.api.GetRtVacuumPressure()) > SCARA_VACUUM_THRESHOLD:
        parts_in_hand = True

    return parts_in_hand


def asyril_pick(devices: Dict[str, Device], index:int=0):
    scara:MecaRobot = devices["scara"]
    asyril:AsyrilEyePlus = devices["asyril"]

    asyril.logger.info("force taking image for pick")
    asyril.api.force_take_image()
    
    # asyril.force_take_image()
    asyril.logger.info("getting part pose for pick")
    pose = asyril.api.get_part()

    while abs(pose['rz'] - 90) < 10:
        # asyril.api.force_take_image()
        pose = asyril.api.get_part()

    asyril.logger.info(f"pose response: {pose}")
    if pose['resp'] == 200:
        # print(f"Part detected at position X: {pose['x']}, Y: {pose['y']}")
        scara.api.SetVariable(name='PickPose.x', value=pose['x'])
        scara.api.SetVariable(name='PickPose.y', value=pose['y'])
        scara.api.SetVariable(name='PickPose.rz', value=pose['rz'])
    else:
        asyril.logger.warning("Failed to get part pose (timeout)")
        scara.logger.warning("Failed to get part pose (timeout) - skiping pick")
        return
    
    scara.api.StartProgram("12")
    scara.api.WaitIdle()
    
    return

def scan_insert_retract(devices: Dict[str, Device], index:int=0):
    lmi_sensor:LMISensor = devices["lmi_sensor"]
    meca_lmi:MecaRobot = devices["meca_lmi"]
    scara:MecaRobot = devices["scara"]
    meca_insert:MecaRobot = devices["meca_insert"]

    meca_lmi.api.StartProgram("22")
    meca_lmi.api.WaitIdle()

    meca_insert.api.StartProgram("32")
    scara.api.StartProgram("13")
    scara.api.WaitIdle()
    meca_insert.api.WaitIdle()

    meca_lmi.api.StartProgram("23")
    meca_lmi.api.WaitIdle()

    i = 0
    scan_ok = False

    while i < SCAN_RETRY_NUMBER:
    
        meca_lmi.api.ExpectExternalCheckpoint(222)
        meca_lmi.api.StartProgram("24")
        meca_lmi.api.WaitForAnyCheckpoint()

        lmi_sensor.api.trigger()

        meca_lmi.api.WaitIdle()
        time.sleep(0.5)

        result = lmi_sensor.api.get_formatted_result()
        x, z = result.split(",")[1:3]

        #result = lmi_sensor.api.get_measurements(0, 1)
        lmi_sensor.logger.info(f"Received measurements: x={x}, z={z}")

        if x != "INVALID" and z !="INVALID":
            scan_ok = True
            break
        else:
            scan_ok = False
            i += 1

    if scan_ok:
        meca_insert.logger.info(f"Setting insert position based on LMI measurements: x={x}, z={z}")
        meca_insert.api.SetVariable("x_offset", -float(x)/1000)  # Convert from microns to mm 
        meca_insert.api.SetVariable("z_offset", -float(z)/1000)  # Convert from microns to mm 
        meca_insert.api.StartProgram("insertion")
        meca_insert.api.WaitIdle()

        #re scan after insertion to verify
        meca_lmi.api.ExpectExternalCheckpoint(222)
        meca_lmi.api.StartProgram("24")
        meca_lmi.api.WaitForAnyCheckpoint()

        lmi_sensor.api.trigger()

        meca_lmi.api.WaitIdle()
        time.sleep(0.5)

    # retract
    meca_insert.api.StartProgram("retract_from_inspect")
    meca_insert.api.WaitIdle()
    meca_insert.api.StartProgram("31")
    scara.api.StartProgram("11")
    scara.api.StartProgram("14")
    scara.api.StartProgram("11")
    scara.api.WaitIdle()
    meca_insert.api.WaitIdle()

    meca_lmi.api.StartProgram("22")
    meca_lmi.api.StartProgram("21")
    

def prod_cycle(devices: Dict[str, Device], index:int):
    """Logic for PROD task."""
    scara:MecaRobot = devices["scara"]
    asyril:AsyrilEyePlus = devices["asyril"]
    lmi_sensor:LMISensor = devices["lmi_sensor"]
    meca_lmi:MecaRobot = devices["meca_lmi"]

    while not scara_part_in_hand(devices=devices):
        asyril_pick(devices=devices)
        asyril.api.prepare_part()

    scan_insert_retract(devices=devices)

    scara.api.WaitIdle()
    meca_lmi.api.WaitIdle()

    return
