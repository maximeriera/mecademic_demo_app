from devices import Device
from typing import Dict


from devices import LMISensor, MecaRobot, AsyrilEyePlus
import time

def shipment(devices: Dict[str, Device]):
    """Logic for SHIPMENT task."""
    '''
    for device_name, device in devices.items():
        if isinstance(device, MecaRobot.MecaRobot):
            device.api.SetJointVel(40)
            device.api.MoveJoints(0, -60, 60, 0, 30, 0)
            device.logger.info(f"Sent MoveJoints command to {device_name} for zero position.")
            device.api.WaitIdle()
    '''
    return
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

    meca_insert.logger.info(f"Setting insert position based on LMI measurements: x={x}, z={z}")
    meca_insert.api.SetVariable("x_offset", float(x)/1000)  # Convert from microns to mm 
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
    scara.api.WaitIdle()
    meca_insert.api.WaitIdle()

    meca_lmi.api.StartProgram("22")
    meca_lmi.api.StartProgram("21")
    meca_lmi.api.WaitIdle()

    return