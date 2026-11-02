"""
devices/SimulatedCamera.py
--------------------------
A camera without hardware: a moving test pattern (gradient, grid and a ball)
behind the same ``capture_frame_8bit()`` as :class:`GigEVisionDevice`, so a
live view or a ``cv_process`` device runs on any machine::

    devices:
      camera:
        type: "simulated_camera"
        fps: 15
"""

from __future__ import annotations

import math
import threading
import time

import numpy as np

from .Device import Device


class TestPattern:
    """Moving test image: gradient, grid and a ball (RGB, uint8)."""

    __test__ = False  # not a pytest test class

    RADIUS = 70
    BALL_RGB = (93, 239, 191)

    def __init__(self, width: int = 1600, height: int = 1200):
        self.width, self.height = width, height
        y, x = np.mgrid[0:height, 0:width]
        self._grid = ((x % 100 < 2) | (y % 100 < 2)).astype(np.uint8) * 90
        by, bx = np.mgrid[-self.RADIUS:self.RADIUS, -self.RADIUS:self.RADIUS]
        self._ball = bx ** 2 + by ** 2 < self.RADIUS ** 2
        self._columns = np.arange(width)
        self._started = time.monotonic()

    def ball_center(self, t: float) -> tuple[int, int]:
        """``(x, y)`` of the ball centre, in pixels, *t* seconds into the pattern."""
        r = self.RADIUS
        cx = int(self.width / 2 + math.cos(t) * (self.width / 2 - r))
        cy = int(self.height / 2 + math.sin(t * 1.3) * (self.height / 2 - r))
        return cx, cy

    def frame(self, t: float | None = None) -> np.ndarray:
        """The pattern *t* seconds in (default: now)."""
        if t is None:
            t = time.monotonic() - self._started
        # One row of gradient, broadcast down the image: cheap enough for 15 fps.
        row = ((self._columns + int(t * 120)) % self.width * 160 // self.width).astype(np.uint8)
        mono = np.maximum(row[np.newaxis, :], self._grid)
        rgb = np.repeat(mono[:, :, np.newaxis], 3, axis=2)
        r = self.RADIUS
        cx, cy = self.ball_center(t)
        rgb[cy - r:cy + r, cx - r:cx + r][self._ball] = self.BALL_RGB
        return rgb


class SimulatedCamera(Device):
    """Hardware-free camera serving :class:`TestPattern` frames at ``fps``."""

    def __init__(self, width: int = 1600, height: int = 1200, fps: float = 15.0,
                 fail_to_connect: bool = False, name: str | None = None):
        super().__init__(device_id=name or "simulated_camera")
        if width < 2 * TestPattern.RADIUS or height < 2 * TestPattern.RADIUS:
            raise ValueError(f"width and height must be at least {2 * TestPattern.RADIUS} pixels")
        if fps <= 0:
            raise ValueError("fps must be > 0")
        self._width, self._height, self._fps = int(width), int(height), float(fps)
        self._fail_to_connect = fail_to_connect
        self._pattern = TestPattern(self._width, self._height)
        self._lock = threading.Lock()
        self._next_frame = 0.0
        self._connected = False
        self._streaming = False
        self._faulted = False

    @property
    def info(self) -> dict:
        return {
            "model": "Test pattern",
            "width": self._width,
            "height": self._height,
            "fps": self._fps,
            "streaming": self._streaming,
        }

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def ready(self) -> bool:
        return self._connected and not self._faulted

    @property
    def faulted(self) -> bool:
        return self._faulted

    @property
    def api(self) -> TestPattern:
        return self._pattern

    @property
    def streaming(self) -> bool:
        return self._streaming

    def initialize(self) -> None:
        if self._fail_to_connect:
            self._connected = False
            raise ConnectionError(f"{self.device_id}: simulated connection failure (fail_to_connect).")
        self._connected = True
        self._faulted = False
        self._streaming = False
        self.logger.info(f"[{self.device_id}] Simulated camera ready ({self._width}×{self._height}, {self._fps:g} fps).")

    def shutdown(self) -> None:
        self._connected = False
        self._streaming = False

    def clear_fault(self) -> None:
        self._faulted = False

    def abort(self) -> None:
        self._streaming = False  # like GigEVisionDevice: the next capture restarts it

    def start_stream(self) -> None:
        if not self._connected:
            raise RuntimeError("Cannot start stream. Camera is not connected.")
        self._streaming = True

    def stop_stream(self) -> None:
        self._streaming = False

    def capture_frame_8bit(self, timeout_sec: float = 2.0) -> np.ndarray:
        """One RGB ``H×W×3`` frame, paced to ``fps`` like a real sensor. Thread-safe."""
        with self._lock:
            if not self._connected:
                raise RuntimeError("Cannot capture frame. Camera is not connected.")
            if not self._streaming:
                self.start_stream()
            now = time.monotonic()
            if self._next_frame > now:
                time.sleep(self._next_frame - now)
            self._next_frame = max(now, self._next_frame) + 1.0 / self._fps
            return self._pattern.frame()

    capture_frame = capture_frame_8bit
