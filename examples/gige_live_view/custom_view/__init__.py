"""Custom view: the live feed of the `camera` device.

``hub.add_video()`` does the work: one thread grabs a frame, shrinks it to
``MAX_WIDTH`` pixels and compresses it to JPEG at ``JPEG_QUALITY``, once for
every viewer, and only while the page is open. The page shows it with a plain
``<img src="/api/data/video/camera.mjpg">``.

Tuning the feed:
* lower ``JPEG_QUALITY`` (or ``MAX_WIDTH``) when the network is slow: the
  stream shows the size of each frame;
* ``FPS`` is a ceiling; the camera's own frame rate and exposure set the floor.
"""

from devices.SimulatedCamera import TestPattern
from devices.SimulatedDevice import SimulatedDevice

#: Device id of the camera in config.yaml.
CAMERA = "camera"
FPS = 15
JPEG_QUALITY = 75          # 1-95: ~75 is visually clean at a fraction of the size
MAX_WIDTH = 1280           # frames are reduced by an integer factor to fit


def setup(hub, devices):
    """Declare the live feed for this workspace."""
    camera = devices[CAMERA]
    if isinstance(camera, SimulatedDevice):
        grab = TestPattern().frame
    else:
        # 8-bit and colour-correct whatever the PixelFormat (Mono12, BayerRG8...).
        # The 2 s timeout absorbs a slow exposure; the stream thread retries.
        # A simulated_camera answers the same call with its test pattern.
        def grab():
            return camera.capture_frame_8bit(timeout_sec=2.0)

    hub.add_video(CAMERA, grab, fps=FPS, quality=JPEG_QUALITY, max_width=MAX_WIDTH, device=CAMERA)
