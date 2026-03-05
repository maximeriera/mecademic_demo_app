from typing import Dict

from devices import Device

from devices.MecaRobot import MecaRobot
from devices.PlanarMotor import PlanarMotor

from devices.api import PlanarMotorApi as pmp


JOINT_VEL = 15
CARTLIN_VEL = 100
CARTANG_VEL = 45

PARTS_PER_TRAIL = [5, 6, 9, 10]
# PARTS_PER_TRAIL = range(16)
TRAILS_OFFSET = 20

def prod_cycle(devices: Dict[str, Device], index:int):
    """Logic for PROD task."""
    
    dispenser:MecaRobot = devices["meca_500_1"]
    nester:MecaRobot = devices["meca_500_2"]
    changer:MecaRobot = devices["meca_500_3"]
    
    planar_motor:PlanarMotor = devices["planar_motor_1"]

    dispenser.api.SetJointVel(75)

    changer.api.SetJointVel(100)

    nester.api.SetJointVel(115)
    nester.api.SetCartLinVel(250)

    
    def swap_nearest_2_positions(planar_motor: PlanarMotor, pose_id_list):

        xlist = [0.120 + (i-1) * 0.240 for i in pose_id_list]
        ylist = [0.120 for i in pose_id_list]

        pos1_bot = planar_motor.api.get_xbot_at_pos(xlist[0], ylist[0])
        pos2_bot = planar_motor.api.get_xbot_at_pos(xlist[1], ylist[1])

        planar_motor.api.send_multi_linear_commands(
            [pmp.PlanarMotorMove(pos1_bot, xlist[0], ylist[0] + 0.060), pmp.PlanarMotorMove(pos2_bot, xlist[1], ylist[1] - 0.060)]
        )
        
        planar_motor.api.send_multi_linear_commands(
            [pmp.PlanarMotorMove(pos1_bot, xlist[1], ylist[0] + 0.060), pmp.PlanarMotorMove(pos2_bot, xlist[0], ylist[1] - 0.060)]
        )

        planar_motor.api.send_multi_linear_commands(
            [pmp.PlanarMotorMove(pos1_bot, xlist[1], ylist[1]), pmp.PlanarMotorMove(pos2_bot, xlist[0], ylist[0])]
        )

    def swap_4_parallel(planar_motor: PlanarMotor):

        xlist = [0.120 + (i-1) * 0.240 for i in [1, 2, 3, 4]]
        ylist = [0.120 for i in [1, 2, 3, 4]]

        pos1_bot = planar_motor.api.get_xbot_at_pos(xlist[0], ylist[0])
        pos2_bot = planar_motor.api.get_xbot_at_pos(xlist[1], ylist[1])
        pos3_bot = planar_motor.api.get_xbot_at_pos(xlist[2], ylist[2])
        pos4_bot = planar_motor.api.get_xbot_at_pos(xlist[3], ylist[3])

        planar_motor.api.send_multi_linear_commands(
            [pmp.PlanarMotorMove(pos1_bot, xlist[0], ylist[0] + 0.060),
             pmp.PlanarMotorMove(pos2_bot, xlist[1], ylist[1] + 0.060),
             pmp.PlanarMotorMove(pos3_bot, xlist[2], ylist[2] - 0.060),
             pmp.PlanarMotorMove(pos4_bot, xlist[3], ylist[3] - 0.060),
             ]
        )
        
        planar_motor.api.send_multi_linear_commands(
            [pmp.PlanarMotorMove(pos1_bot, xlist[2], ylist[0] + 0.060),
             pmp.PlanarMotorMove(pos2_bot, xlist[3], ylist[1] + 0.060),
             pmp.PlanarMotorMove(pos3_bot, xlist[0], ylist[2] - 0.060),
             pmp.PlanarMotorMove(pos4_bot, xlist[1], ylist[3] - 0.060),
             ]
        )

        planar_motor.api.send_multi_linear_commands(
            [pmp.PlanarMotorMove(pos1_bot, xlist[2], ylist[2]),
             pmp.PlanarMotorMove(pos2_bot, xlist[3], ylist[3]),
             pmp.PlanarMotorMove(pos3_bot, xlist[0], ylist[0]),
             pmp.PlanarMotorMove(pos4_bot, xlist[1], ylist[1])
             ]
        )

    def spin_position(planar_motor: PlanarMotor, pose_id_list, time:float=1):
        xlist = [0.120 + (i-1) * 0.240 for i in pose_id_list]
        ylist = [0.120 for i in pose_id_list]

        bot_list = [planar_motor.api.get_xbot_at_pos(xlist[i], ylist[i]) for i in range(len(xlist))]

        for bot_id in bot_list:
            planar_motor.api.send_rotation(bot_id, time, target_angle=0)

    dispenser.api.StartProgram('nest')

    if index % 2 == 1:
        changer.api.StartProgram('change_A')
    else:
        changer.api.StartProgram('change_B')

    nester.api.StartProgram('pick_curring')
    nester.api.StartProgram('drop_inspect')
    nester.api.StartProgram('pick_buffer')
    nester.api.StartProgram('drop_curring')
    nester.api.StartProgram('pick_inspect')
    nester.api.StartProgram('drop_buffer')

    spin_position(planar_motor, [3], time=12)
    spin_position(planar_motor, [2], time=12)
    planar_motor.api.wait_multiple_move_done([1, 2, 3, 4])

    dispenser.api.WaitIdle()

    swap_nearest_2_positions(planar_motor, [4, 3])
    planar_motor.api.wait_multiple_move_done([1, 2, 3, 4])

    dispenser.api.StartProgram('vials')

    swap_nearest_2_positions(planar_motor, [2, 3])
    planar_motor.api.wait_multiple_move_done([1, 2, 3, 4])

    nester.api.StartProgram('pick_conv')
    nester.api.StartProgram('drop_inspect')
    nester.api.StartProgram('pick_buffer')
    nester.api.StartProgram('drop_conv')

    spin_position(planar_motor, [3], time=3)
    planar_motor.api.wait_multiple_move_done([1, 2, 3, 4])

    dispenser.api.WaitIdle()

    swap_nearest_2_positions(planar_motor, [3, 4])
    planar_motor.api.wait_multiple_move_done([1, 2, 3, 4])

    dispenser.api.StartProgram('vials')

    spin_position(planar_motor, [3], time=3)
    planar_motor.api.wait_multiple_move_done([1, 2, 3, 4])

    nester.api.WaitIdle()

    nester.api.StartProgram('pick_inspect')
    nester.api.StartProgram('drop_buffer')

    dispenser.api.WaitIdle()

    swap_4_parallel(planar_motor)
    planar_motor.api.wait_multiple_move_done([1, 2, 3, 4])



