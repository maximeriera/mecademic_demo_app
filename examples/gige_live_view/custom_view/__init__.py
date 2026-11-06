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

import math
import time

import numpy as np

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
        def grab():
            return camera.capture_frame_8bit(timeout_sec=2.0)

    hub.add_video(CAMERA, grab, fps=FPS, quality=JPEG_QUALITY, max_width=MAX_WIDTH, device=CAMERA)


class TestPattern:
    """Moving test image for the simulated camera: gradient, grid and a ball."""

    WIDTH, HEIGHT = 1600, 1200
    RADIUS = 70

    def __init__(self):
        y, x = np.mgrid[0:self.HEIGHT, 0:self.WIDTH]
        self._grid = ((x % 100 < 2) | (y % 100 < 2)).astype(np.uint8) * 90
        by, bx = np.mgrid[-self.RADIUS:self.RADIUS, -self.RADIUS:self.RADIUS]
        self._ball = bx ** 2 + by ** 2 < self.RADIUS ** 2
        self._columns = np.arange(self.WIDTH)
        self._started = time.monotonic()

    def frame(self) -> np.ndarray:
        t = time.monotonic() - self._started
        # One row of gradient, broadcast down the image: cheap enough for 15 fps.
        row = ((self._columns + int(t * 120)) % self.WIDTH * 160 // self.WIDTH).astype(np.uint8)
        mono = np.maximum(row[np.newaxis, :], self._grid)
        rgb = np.repeat(mono[:, :, np.newaxis], 3, axis=2)
        r = self.RADIUS
        cx = int(self.WIDTH / 2 + math.cos(t) * (self.WIDTH / 2 - r))
        cy = int(self.HEIGHT / 2 + math.sin(t * 1.3) * (self.HEIGHT / 2 - r))
        rgb[cy - r:cy + r, cx - r:cx + r][self._ball] = (93, 239, 191)
        return rgb
