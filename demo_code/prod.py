from typing import Dict
from threading import Thread
import time

from devices import Device

from devices import MecaRobot, AsyrilEyePlus

JOINT_VEL = 15
CARTLIN_VEL = 100
CARTANG_VEL = 45

PARTS_PER_TRAIL = [5, 6, 9, 10]
# PARTS_PER_TRAIL = range(16)
TRAILS_OFFSET = 20

def prod_cycle(devices: Dict[str, Device], index:int):
    """Logic for PROD task."""
    
    scara:MecaRobot = devices["scara"]
    trail:MecaRobot = devices["meca_trail"]
    dispenser:MecaRobot = devices["meca_dispenser"]
    asyril:AsyrilEyePlus = devices["asyril_1"]
    
    if not scara_trail:
        def scara_trail(trail_id:int, scara:MecaRobot, asyril:AsyrilEyePlus) -> None:
            try:
                asyril.logger.info("force taking image for pick")
                asyril.api.force_take_image()
                for i in PARTS_PER_TRAIL:
                    # asyril.force_take_image()
                    asyril.logger.info("getting part pose for pick")
                    pose = asyril.api.get_part()
                    asyril.logger.info(f"pose response: {pose}")
                    if pose['resp'] == 200:
                        # print(f"Part detected at position X: {pose['x']}, Y: {pose['y']}")
                        scara.api.SetVariable(name='PickPose.x', value=pose['x'])
                        scara.api.SetVariable(name='PickPose.y', value=pose['y'])
                    else:
                        asyril.logger.error("Failed to get part pose")
                        return

                    pose_x = (i // 4) * TRAILS_OFFSET
                    pose_y = (i % 4) * TRAILS_OFFSET

                    scara.logger.info(f"Setting place pose for part {i} to X: {pose_x}, Y: {pose_y}")
                    scara.api.SetVariable(name='PlacePose.x', value=pose_x)
                    scara.api.SetVariable(name='PlacePose.y', value=pose_y)

                    scara.logger.info(f"waiting for scara to be IDLE (timeout=30s) before starting pick_place_{trail_id} program")
                    scara.api.WaitIdle(timeout=30)
                    scara.logger.info(f"scara IDLE")
                    # scara.StartProgram('pick')

                    # scara.WaitIdle()
                    # scara.StartProgram('place' + str(trail_id))

                    scara.logger.info(f"starting pick_place_{trail_id} program")
                    scara.api.ExpectExternalCheckpoint(1)
                    scara.api.StartProgram(f'pick_place_{trail_id}')
                    scara.logger.info(f"waiting for scara checkpoint")
                    scara.api.WaitForAnyCheckpoint(timeout=3)

                scara.logger.info(f"waiting for IDLE (timeout=30s)")
                scara.api.WaitIdle(timeout=30)
                scara.logger.info(f"scara IDLE")
            except Exception as e:
                scara.logger.error(f"Exception while handling scara vision pick: {e}")
                asyril.logger.error(f"Exception while handling scara vision pick: {e}")

    if not scara_capture:
        def scara_capture(scara:MecaRobot) -> None:
            scara.logger.info("Capturing image with scara IO")
            data = scara.api.GetRtOutputState().data
            data[4] = 1
            scara.api.SetOutputState_Immediate(1, *data[:])
            time.sleep(0.1)
            data = scara.api.GetRtOutputState().data
            data[4] = 0
            scara.api.SetOutputState_Immediate(1, *data[:])
            time.sleep(0.1)
    
    scara.logger.info("Waiting IDLE (no timeout)")
    scara.api.WaitIdle()
    scara.logger.info("Idle")
    trail.logger.info("Waiting IDLE (no timeout)")
    trail.api.WaitIdle()
    trail.logger.info("Idle")
    
    trail.api.ExpectExternalCheckpoint(11)
    trail.api.StartProgram(f"pick{index}")
    trail.api.StartProgram("place_exchange")
    trail.logger.info("Waiting for any checkpoint (no timeout)")
    trail.api.WaitForAnyCheckpoint()
    trail.logger.info("Checkpoint reached, waiting for IDLE (no timeout)")
    trail.logger.info("Waiting IDLE (no timeout)")
    trail.api.WaitIdle()
    trail.logger.info("Idle")

    vision_pick_thread = Thread(target=scara_trail, args=(index, scara, asyril))
    vision_pick_thread.start()
    
    trail.logger.info("Waiting IDLE (no timeout)")
    trail.api.WaitIdle()
    trail.logger.info("Idle")
    dispenser.logger.info("Waiting IDLE (no timeout)")
    dispenser.api.WaitIdle()
    dispenser.logger.info("Idle")
    
    dispenser.api.GripperClose()
    trail.api.Delay(0.5)
    trail.api.GripperOpen()
    trail.api.Delay(1)
    dispenser.api.Delay(1)
    
    trail.api.ExpectExternalCheckpoint(1)
    trail.logger.info("Starting change program")
    trail.api.StartProgram("change")
    dispenser.logger.info("Starting dispense program")
    dispenser.api.StartProgram("Dispense")
    
    trail.api.ExpectExternalCheckpoint(1)
    trail.api.ExpectExternalCheckpoint(2)
    trail.api.ExpectExternalCheckpoint(3)
    trail.api.ExpectExternalCheckpoint(4)
    trail.api.ExpectExternalCheckpoint(5)
    trail.api.ExpectExternalCheckpoint(6)
    trail.logger.info("Starting scan program")
    trail.api.StartProgram("scan")
    
    trail.logger.info("Waiting for any checkpoint (timeout=3s)")
    trail.api.WaitForAnyCheckpoint(timeout=3)
    trail.logger.info("Checkpoint reached")
    scara_capture(scara)
    trail.logger.info("Waiting for any checkpoint (timeout=3s)")
    trail.api.WaitForAnyCheckpoint(timeout=3)
    trail.logger.info("Checkpoint reached")
    scara_capture(scara)
    trail.logger.info("Waiting for any checkpoint (timeout=3s)")
    trail.api.WaitForAnyCheckpoint(timeout=3)
    trail.logger.info("Checkpoint reached")
    scara_capture(scara)
    trail.logger.info("Waiting for any checkpoint (timeout=3s)")
    trail.api.WaitForAnyCheckpoint(timeout=3)
    trail.logger.info("Checkpoint reached")
    scara_capture(scara)
    trail.logger.info("Waiting for any checkpoint (timeout=3s)")
    trail.api.WaitForAnyCheckpoint(timeout=3)
    trail.logger.info("Checkpoint reached")
    scara_capture(scara)
    trail.logger.info("Waiting for any checkpoint (timeout=3s)")
    trail.api.WaitForAnyCheckpoint(timeout=3)
    trail.logger.info("Checkpoint reached")
    scara_capture(scara)
    # 
    vision_pick_thread.join()
    scara.logger.info("Waiting for scara to be IDLE (timeout=30s)")
    scara.api.WaitIdle(timeout=30)

    # trail.api.StartProgram(f"pick{index}")
    # trail.api.StartProgram("dump")
    # trail.api.StartProgram(f"drop{index}")
    #
    # trail.api.WaitIdle()

    trail.logger.info("Starting pick and place programs")
    trail.api.StartProgram("dump")
    trail.api.StartProgram(f"drop{index}")



