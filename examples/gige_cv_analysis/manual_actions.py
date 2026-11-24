"""Manual actions: the "Inspect once" button of the Manual tab (listed in config.yaml)."""

from __future__ import annotations
from core import AppContext

from .beam_alignment import AlignConfig, MecademicLensRobot, align, calibrate
from .beam_analysis import enable_targets
from time import sleep

def inspect_once(devices, context=None):
    """Analyse the next software-triggered frame and log what blob_analysis found.

    ``analyze_once()`` waits for the next result while the analysis triggers
    the camera at its rate_hz, and triggers and analyses one frame itself when
    it is stopped.
    """
    vision = devices["vision"]
    if context is None:
        return vision.analyze_once()
    with context.step("Inspect"):
        result = vision.analyze_once(timeout=5.0)
    data = result["data"]
    if data.get("found"):
        vision.logger.info(f"Inspect once: blob at x={data['x_px']} y={data['y_px']}, "
                           f"{data['area_px']} px ({result['analysis_ms']} ms).")
    else:
        vision.logger.info(f"Inspect once: no blob (threshold {data.get('threshold')}).")
    return result

def full_run_test(devices, context=AppContext):
    vision = devices["vision"]
    robot = devices["robot"]

    while(True):
        enable_targets(False)
        pick_up(devices, context)
        align_once(devices, context)
        enable_targets(False)
        drop_off(devices, context)
        context.set_variable("held_lens", context.get_variable("held_lens", 1) + 1)
        if(context.get_variable("held_lens", 1) == 5):
            context.set_variable("held_lens", 1)



def align_once(devices, context=None):
    vision = devices["vision"]
    robot = devices["robot"]

    robot.api.SetCartLinVel(25)
    robot.api.SetCartAcc(25)
    robot.api.SetTrf(0, 0, 83,  90,  0,  0)
    robot.api.MoveLin(16.619713, 112.256849, 104.758734, 179.999937, -89.733337, -90.000077)
    robot.api.WaitIdle()

    cfg = AlignConfig(z_limits_mm=(-5, 5), n_targets=50, max_dz_mm = 10, cal_step_mm=1.0)
    cal = calibrate(lambda: vision.last_result()["data"], MecademicLensRobot(robot, "trf"), cfg)
    enable_targets(None)
    summary = align(lambda: vision.last_result()["data"], MecademicLensRobot(robot, "trf"), cal, cfg)
    print(summary.stopped_because, [(t.moves, t.distance_um) for t in summary.targets])
    

def pick_up(devices, context=AppContext):
    lens_holder_seperation = 8.333
    if(context.get_variable("held_lens", 1) == 1):
        up = [9.169599, 200.34877, 40.313489, -89.999994, -90, 0]
        down  = [9.1696, 200.348771, 10.43849, -89.999994, -90, 0]
    elif(context.get_variable("held_lens", 1) == 2):
        up = [9.169599+lens_holder_seperation*1, 200.34877, 40.313489, -89.999994, -90, 0]
        down  = [9.1696+lens_holder_seperation*1, 200.348771, 10.43849, -89.999994, -90, 0]
    elif(context.get_variable("held_lens", 1) == 3):
        up = [9.169599+lens_holder_seperation*2, 200.34877, 40.313489, -89.999994, -90, 0]
        down  = [9.1696+lens_holder_seperation*2, 200.348771, 10.43849, -89.999994, -90, 0]
    elif(context.get_variable("held_lens", 1) == 4):
        up = [9.169599+lens_holder_seperation*3, 200.34877, 40.313489, -89.999994, -90, 0]
        down  = [9.1696+lens_holder_seperation*3, 200.348771, 10.43849, -89.999994, -90, 0]
    else:
        raise ValueError("Not a valid lens_num")


    robot = devices["robot"]
    gripper = devices["gripper"]

    robot.api.SetCartLinVel(25)
    robot.api.SetCartAcc(25)    
    robot.api.SetTrf(0, 0, 83,  90,  0,  0)
    gripper.release()
    sleep(5)
    robot.api.MoveLin(*up)
    robot.api.WaitIdle()
    robot.api.MoveLin(*down)
    robot.api.WaitIdle()
    gripper.grip()
    sleep(2)
    robot.api.MoveLin(*up)
    robot.api.WaitIdle()

def drop_off(devices, context=AppContext):
    lens_holder_seperation = 8.333
    if(context.get_variable("held_lens", 1) == 1):
        up = [9.169599, 200.34877, 40.313489, -89.999994, -90, 0]
        down  = [9.1696, 200.348771, 10.43849, -89.999994, -90, 0]
    elif(context.get_variable("held_lens", 1) == 2):
        up = [9.169599+lens_holder_seperation*1, 200.34877, 40.313489, -89.999994, -90, 0]
        down  = [9.1696+lens_holder_seperation*1, 200.348771, 10.43849, -89.999994, -90, 0]
    elif(context.get_variable("held_lens", 1) == 3):
        up = [9.169599+lens_holder_seperation*2, 200.34877, 40.313489, -89.999994, -90, 0]
        down  = [9.1696+lens_holder_seperation*2, 200.348771, 10.43849, -89.999994, -90, 0]
    elif(context.get_variable("held_lens", 1) == 4):
        up = [9.169599+lens_holder_seperation*3, 200.34877, 40.313489, -89.999994, -90, 0]
        down  = [9.1696+lens_holder_seperation*3, 200.348771, 10.43849, -89.999994, -90, 0]
    else:
        raise ValueError("Not a valid lens_num")

    robot = devices["robot"]
    gripper = devices["gripper"]

    robot.api.SetCartLinVel(25)
    robot.api.SetCartAcc(25)
    robot.api.SetTrf(0, 0, 83,  90,  0,  0)
    robot.api.MoveLin(*up)
    robot.api.WaitIdle()
    robot.api.MoveLin(*down)
    robot.api.WaitIdle()
    gripper.release()
    sleep(5)
    robot.api.MoveLin(*up)
    robot.api.WaitIdle()

def get_manual_actions():
    return {"inspect_once": inspect_once,
            "align_once": align_once,
            "pick_up": pick_up,
            "drop_off": drop_off,
            "full_run_test": full_run_test,}
