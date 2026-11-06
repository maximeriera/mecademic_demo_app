"""PROD task: a placeholder cycle, so START PROD works in this camera-only example.

The custom view's stream thread is the only reader of the camera. Task code
that needs an image would use the camera's own API only while nobody watches
the view, or add the frames it needs to the hub; see custom_view/__init__.py.
"""

import time


def prod_cycle(devices, context=None):
    if context is None:
        time.sleep(1.0)
        return
    with context.step("Wait"):
        time.sleep(1.0)
