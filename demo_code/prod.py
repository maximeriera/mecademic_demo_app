import mecademicpy.robot as mdr
import pyfirmata

from typing import Tuple, Any

JOINT_VEL = 15
CARTLIN_VEL = 100
CARTANG_VEL = 45

def prod_cycle(robot_apis: Tuple[mdr.Robot], accessory_apis: Tuple[Any]):
    """Logic for PROD task."""
    
    mirror:mdr.Robot = robot_apis[0]
    dispenser:mdr.Robot = robot_apis[1]
    board:pyfirmata.Arduino = accessory_apis[0] 
    
    mirror.SetBlending(100)
    mirror.SetJointVel(JOINT_VEL)
    mirror.SetCartLinVel(CARTLIN_VEL)
    mirror.SetCartAngVel(CARTANG_VEL)

    dispenser.SetBlending(100)
    dispenser.SetJointVel(JOINT_VEL)
    dispenser.SetCartLinVel(CARTLIN_VEL)
    dispenser.SetCartAngVel(CARTANG_VEL)
    
    mirror.WaitIdle()
    dispenser.WaitIdle()

    mirror.StartProgram('optic1_pick')
    mirror.WaitIdle()
    dispenser.WaitIdle()

    mirror.Delay(1)
    mirror.StartProgram('alignment1')
    mirror.Delay(2)
    mirror.WaitIdle()
    dispenser.WaitIdle()

    dispenser.StartProgram('dispensing')
    mirror.WaitIdle()
    dispenser.WaitIdle()

    dispenser.StartProgram('curing1')
    mirror.WaitIdle()
    dispenser.WaitIdle()

    board.digital[8].write(1)

    dispenser.StartProgram('curingLED')
    mirror.WaitIdle()
    dispenser.WaitIdle()
    board.digital[8].write(0)



    dispenser.StartProgram('curing2')
    mirror.WaitIdle()
    dispenser.WaitIdle()

    mirror.StartProgram('optic1_place')
    mirror.WaitIdle()
    dispenser.WaitIdle()


    mirror.StartProgram('optic2_pick')
    mirror.WaitIdle()
    dispenser.WaitIdle()

    mirror.Delay(1)
    mirror.StartProgram('alignment2')
    mirror.Delay(2)
    mirror.WaitIdle()
    dispenser.WaitIdle()

    dispenser.StartProgram('dispensing')
    mirror.WaitIdle()
    dispenser.WaitIdle()

    dispenser.StartProgram('curing1')
    mirror.WaitIdle()
    dispenser.WaitIdle()



    board.digital[8].write(1)
    dispenser.StartProgram('curingLED')
    mirror.WaitIdle()
    dispenser.WaitIdle()
    board.digital[8].write(0)


    dispenser.StartProgram('curing2')
    mirror.WaitIdle()
    dispenser.WaitIdle()


    mirror.StartProgram('optic2_place')
    mirror.WaitIdle()
    dispenser.WaitIdle()


    mirror.StartProgram('optic3_pick')
    mirror.WaitIdle()
    dispenser.WaitIdle()

    mirror.Delay(1)
    mirror.StartProgram('alignment3')
    mirror.Delay(2)
    mirror.WaitIdle()
    dispenser.WaitIdle()

    dispenser.StartProgram('dispensing')
    mirror.WaitIdle()
    dispenser.WaitIdle()

    dispenser.StartProgram('curing1')
    mirror.WaitIdle()
    dispenser.WaitIdle()

    board.digital[8].write(1)


    dispenser.StartProgram('curingLED')
    mirror.WaitIdle()
    dispenser.WaitIdle()

    board.digital[8].write(0)

    dispenser.StartProgram('curing2')
    mirror.WaitIdle()
    dispenser.WaitIdle()


    mirror.StartProgram('optic3_place')
    mirror.WaitIdle()
    dispenser.WaitIdle()
    
    # --------------------------------------------------------