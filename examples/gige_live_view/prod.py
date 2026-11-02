"""PROD task: a placeholder cycle, so START PROD works in this camera-only example.

Task code that needs images and measurements would declare a cv_process device
on this camera and read its results; see examples/gige_cv_analysis.
"""

import time


def prod_cycle(devices, context=None):
    if context is None:
        time.sleep(1.0)
        return
    with context.step("Wait"):
        time.sleep(1.0)
