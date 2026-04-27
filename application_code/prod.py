from devices import Device
from devices import MecaRobot
from devices import ArduinoBoard

import time


def prod_cycle(devices: Dict[str, Device], index:int):
    """Logic for PROD task."""
    
    mirror:MecaRobot = devices["mirror_robot"]
    dispenser:MecaRobot = devices["dispenser_robot"]
    board:ArduinoBoard = devices["arduino_io"]
    
    mirror.api.SetBlending(100)
    mirror.api.SetJointVel(JOINT_VEL + 10)
    mirror.api.SetCartLinVel(CARTLIN_VEL)
    mirror.api.SetCartAngVel(CARTANG_VEL)

    dispenser.api.SetBlending(100)
    dispenser.api.SetJointVel(JOINT_VEL)
    dispenser.api.SetCartLinVel(CARTLIN_VEL)
    dispenser.api.SetCartAngVel(CARTANG_VEL)
    
    mirror.api.WaitIdle()
    dispenser.api.WaitIdle()

    i = index + 1

    cmd = f"optic{i}_pick"
    mirror.api.StartProgram(cmd)
    mirror.api.WaitIdle()
    dispenser.api.WaitIdle()

    mirror.api.Delay(1)
    cmd = f"alignment{i}"
    mirror.api.StartProgram(cmd)
    # mirror.api.Delay(0)
    mirror.api.WaitIdle()
    dispenser.api.WaitIdle()

    dispenser.api.StartProgram('dispensing')
    mirror.api.WaitIdle()
    dispenser.api.WaitIdle()

    dispenser.api.StartProgram('curing1')
    mirror.api.WaitIdle()
    dispenser.api.WaitIdle()

    board.api.digital[8].write(1)

    dispenser.api.StartProgram('curingLED')
    mirror.api.WaitIdle()
    dispenser.api.WaitIdle()

    board.api.digital[8].write(0)

    dispenser.api.StartProgram('curing2')
    mirror.api.WaitIdle()
    dispenser.api.WaitIdle()

    cmd = f"optic{i}_place"
    mirror.api.StartProgram(cmd)
    mirror.api.WaitIdle()
    dispenser.api.WaitIdle()
    
    return
