from devices import Device, MecaRobot
from typing import Dict


def home(devices: Dict[str, Device]):
    """Logic for HOME task."""
    scara:MecaRobot = devices["scara"]
    asyril:AsyrilEyePlus = devices["asyril"]
    lmi_sensor:LMISensor = devices["lmi_sensor"]
    meca_lmi:MecaRobot = devices["meca_lmi"]
    meca_insert:MecaRobot = devices["meca_insert"]

    meca_lmi.api.StartProgram("22")
    meca_lmi.api.WaitIdle()

    scara.api.StartProgram("11")
    scara.api.WaitIdle()
    scara.api.StartProgram("14")
    scara.api.StartProgram("11")

    
    meca_insert.api.StartProgram("31")
    meca_insert.api.WaitIdle()

    meca_lmi.api.StartProgram("21")
    meca_lmi.api.WaitIdle()
    scara.api.WaitIdle()