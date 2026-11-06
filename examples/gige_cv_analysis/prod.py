"""PROD task: one inspection per cycle, the way a pick cycle would locate its part.

Task code never reads the camera itself: the `vision` device analyses the
frame of every trigger pulse, and ``analyze_once()`` hands back the next one's
result, or raises TimeoutError if no pulse comes within the timeout.
"""

import time


def prod_cycle(devices, context=None):
    vision = devices["vision"]
    if context is None:
        vision.analyze_once()
        time.sleep(1.0)
        return
    with context.step("Locate part"):
        data = vision.analyze_once(timeout=5.0)["data"]
    if data.get("found"):
        vision.logger.info(f"Cycle: part at x={data['x_px']} y={data['y_px']} px.")
    else:
        vision.logger.info("Cycle: no part in view.")
    with context.step("Wait"):
        time.sleep(1.0)
