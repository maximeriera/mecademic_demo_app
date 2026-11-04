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
        features:              # same contract as a gige_camera's (or features_file:)
          ExposureTime: 10000  # µs: brightness scales with it
          Gain: 0              # dB
          PixelFormat: "RGB8"  # or "Mono8"; locked while streaming, like a real camera

It also has read-only Width, Height, DeviceModelName and DeviceTemperature,
so the Features dialog shows categories, read-only rows and visibility levels.
"""

from __future__ import annotations

import math
import threading
import time

import numpy as np

from .CameraFeatures import VISIBILITIES, FeatureSettingsMixin
from .Device import Device

#: The simulated GenICam features: same names, types, categories and errors as
#: on a GigE camera. ``default`` None means the camera fills it in (its size).
FEATURES = {
    "ExposureTime": {"type": "IFloat", "default": 10000.0, "min": 100.0, "max": 1_000_000.0, "unit": "us",
                     "category": "Acquisition Control", "display_name": "Exposure Time",
                     "tooltip": "Exposure time in microseconds. The image brightness scales with it."},
    "Gain": {"type": "IFloat", "default": 0.0, "min": 0.0, "max": 24.0, "unit": "dB",
             "category": "Analog Control", "display_name": "Gain",
             "tooltip": "Amplification of the signal, in dB: +6 dB doubles the brightness."},
    "PixelFormat": {"type": "IEnumeration", "default": "RGB8", "choices": ("RGB8", "Mono8"),
                    "locked_while_streaming": True, "category": "Image Format Control",
                    "display_name": "Pixel Format",
                    "tooltip": "Format of the pixels sent. Locked while the camera streams."},
    "Width": {"type": "IInteger", "default": None, "writable": False, "unit": "px",
              "category": "Image Format Control", "display_name": "Width", "tooltip": "Width of the image."},
    "Height": {"type": "IInteger", "default": None, "writable": False, "unit": "px",
               "category": "Image Format Control", "display_name": "Height", "tooltip": "Height of the image."},
    "DeviceModelName": {"type": "IString", "default": "Test pattern", "writable": False,
                        "category": "Device Control", "display_name": "Device Model Name",
                        "tooltip": "Model of the camera."},
    "DeviceTemperature": {"type": "IFloat", "default": 38.5, "writable": False, "unit": "C",
                          "category": "Device Control", "display_name": "Device Temperature",
                          "tooltip": "Temperature of the sensor board.", "visibility": "Expert"},
}
_REFERENCE_EXPOSURE_US = 10000.0   # the exposure at which the pattern has its nominal brightness
_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


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


class SimulatedCamera(FeatureSettingsMixin, Device):
    """Hardware-free camera serving :class:`TestPattern` frames at ``fps``."""

    def __init__(self, width: int = 1600, height: int = 1200, fps: float = 15.0,
                 fail_to_connect: bool = False, features: dict | None = None,
                 features_file: str | None = None, name: str | None = None):
        super().__init__(device_id=name or "simulated_camera")
        if width < 2 * TestPattern.RADIUS or height < 2 * TestPattern.RADIUS:
            raise ValueError(f"width and height must be at least {2 * TestPattern.RADIUS} pixels")
        if fps <= 0:
            raise ValueError("fps must be > 0")
        self._width, self._height, self._fps = int(width), int(height), float(fps)
        self._fail_to_connect = fail_to_connect
        # Written at every INITIALIZE, in order (devices/CameraFeatures.py).
        self._init_feature_settings(features, features_file)
        self._values = {name: spec["default"] for name, spec in FEATURES.items()}
        self._values.update(Width=self._width, Height=self._height)
        self._lut: np.ndarray | None = None     # brightness lookup table, None at nominal brightness
        self._pattern = TestPattern(self._width, self._height)
        self._lock = threading.Lock()
        self._next_frame = 0.0
        self._connected = False
        self._streaming = False
        self._stream_starts = 0
        self._faulted = False

    @property
    def info(self) -> dict:
        return {
            "model": "Test pattern",
            "width": self._width,
            "height": self._height,
            "fps": self._fps,
            "streaming": self._streaming,
            "features": self._features_summary(),
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
        try:
            self._load_and_apply_features()
        except Exception:
            self._connected = False
            raise
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
        self._stream_starts += 1

    def stop_stream(self) -> None:
        self._streaming = False

    def capture_frame_8bit(self, timeout_sec: float = 2.0) -> np.ndarray:
        """One frame, RGB ``H×W×3`` (``Mono8``: ``H×W``), paced to ``fps`` like a real sensor. Thread-safe."""
        with self._lock:
            if not self._connected:
                raise RuntimeError("Cannot capture frame. Camera is not connected.")
            if not self._streaming:
                self.start_stream()
            now = time.monotonic()
            if self._next_frame > now:
                time.sleep(self._next_frame - now)
            self._next_frame = max(now, self._next_frame) + 1.0 / self._fps
            frame = self._pattern.frame()
            mono, lut = self._values["PixelFormat"] == "Mono8", self._lut
        if mono:
            frame = (frame @ _LUMA).astype(np.uint8)
        return frame if lut is None else lut[frame]

    capture_frame = capture_frame_8bit

    # --- Features (same contract as GigEVisionDevice) -------------------------

    def _feature_spec(self, feature_name: str) -> dict:
        spec = FEATURES.get(feature_name)
        if spec is None:
            raise KeyError(f"camera has no feature '{feature_name}'")
        if not self._connected:
            raise RuntimeError("Cannot access features. Camera is not connected.")
        return spec

    def set_feature(self, feature_name: str, value) -> None:
        """Write a feature now. A locked one (PixelFormat) gets the stream stopped and restarted.

        KeyError for an unknown feature, ValueError for a refused value; never
        a fault. Lasts until the next INITIALIZE writes ``features`` again.
        """
        spec = self._feature_spec(feature_name)
        if not spec.get("writable", True):
            raise ValueError(f"feature '{feature_name}' is read-only.")
        if spec["type"] == "IFloat":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"feature '{feature_name}' (IFloat) cannot take {value!r}")
            value = float(value)
            if not spec["min"] <= value <= spec["max"]:
                raise ValueError(f"feature '{feature_name}' refused {value!r}: "
                                 f"outside {spec['min']:.10g}-{spec['max']:.10g} {spec['unit']}")
        elif value not in spec["choices"]:
            raise ValueError(f"feature '{feature_name}' refused {value!r}: one of {', '.join(spec['choices'])}")
        with self._lock:
            restart = bool(spec.get("locked_while_streaming")) and self._streaming
            if restart:
                self.stop_stream()
            old = self._values[feature_name]
            self._values[feature_name] = value
            scale = (self._values["ExposureTime"] / _REFERENCE_EXPOSURE_US) * 10 ** (self._values["Gain"] / 20)
            self._lut = None if scale == 1.0 else np.minimum(np.arange(256) * scale, 255).astype(np.uint8)
            if restart:
                self.start_stream()
        self.logger.info(f"[{self.device_id}] Feature {feature_name}: {old} -> {value}"
                         + (" (stream restarted)" if restart else ""))

    def get_feature(self, feature_name: str):
        """Current value of a feature. KeyError if the camera has none."""
        self._feature_spec(feature_name)
        return self._values[feature_name]

    def describe_feature(self, feature_name: str) -> dict:
        """``{name, value, type, writable, min, max, inc, unit, choices, display_name, tooltip, visibility}``."""
        spec = self._feature_spec(feature_name)
        return {
            "name": feature_name,
            "value": self._values[feature_name],
            "type": spec["type"],
            "writable": spec.get("writable", True) and not (spec.get("locked_while_streaming") and self._streaming),
            "stream_locked": bool(spec.get("locked_while_streaming")),
            "min": spec.get("min"),
            "max": spec.get("max"),
            "inc": None,
            "unit": spec.get("unit"),
            "choices": list(spec["choices"]) if "choices" in spec else None,
            "display_name": spec["display_name"],
            "tooltip": spec["tooltip"],
            "visibility": spec.get("visibility", "Beginner"),
        }

    def feature_tree(self, visibility: str = "Beginner") -> list:
        """The features up to *visibility*, by category, like ``GigECamera.feature_tree()``."""
        limit = VISIBILITIES.index(visibility)   # ValueError for an unknown level
        if not self._connected:
            raise RuntimeError("Cannot read features. Camera is not connected.")
        groups: dict = {}
        for name, spec in FEATURES.items():
            if VISIBILITIES.index(spec.get("visibility", "Beginner")) <= limit:
                groups.setdefault(spec["category"], []).append(self.describe_feature(name))
        return [{"name": category, "features": features} for category, features in groups.items()]
