"""
devices/CameraDiagnostics.py
----------------------------
Per-frame diagnostics shared by the camera drivers, shown in their Live panel
(Settings > Devices > camera > Show live data).

A real camera that flashes black frames now and then is usually lit by light
that flickers faster than the eye sees: PWM-dimmed LEDs, or lamps on the
mains (100/120 Hz), against an ExposureTime shorter than one flicker period,
or an ExposureAuto/GainAuto that hunts. Counting near-black frames where they
leave the driver tells that apart from a problem further down (encoding, the
network, the browser).
"""

from __future__ import annotations

import threading
import time

import numpy as np

#: Mean 8-bit level under which a frame counts as near-black.
DARK_LEVEL = 10.0
_LOG_INTERVAL_S = 10.0


class FrameDiagnostics:
    """Brightness of the last frame, and how many near-black frames the camera delivered."""

    def __init__(self, logger, device_id: str):
        self._logger, self._device_id = logger, device_id
        self._lock = threading.Lock()
        self.brightness: float | None = None
        self.dark_frames = 0
        self._last_logged = 0.0

    def observe(self, frame: np.ndarray) -> None:
        """Record one 8-bit frame (H×W or H×W×3). A sparse sample keeps it cheap at any size."""
        mean = float(np.asarray(frame)[::8, ::8].mean())
        with self._lock:
            self.brightness = mean
            if mean >= DARK_LEVEL:
                return
            self.dark_frames += 1
            count = self.dark_frames
            now = time.monotonic()
            log = count == 1 or now - self._last_logged >= _LOG_INTERVAL_S
            if log:
                self._last_logged = now
        if log:
            self._logger.warning(
                f"[{self._device_id}] Near-black frame from the camera (mean {mean:.1f}/255, "
                f"{count} so far). Check the lighting (PWM LEDs or mains flicker against a short "
                f"ExposureTime) and ExposureAuto/GainAuto.")

    def telemetry(self) -> dict:
        with self._lock:
            return {
                "frame_brightness": round(self.brightness, 1) if self.brightness is not None else None,
                "dark_frames": self.dark_frames,
            }
