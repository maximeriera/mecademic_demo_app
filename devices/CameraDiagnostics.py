"""
devices/CameraDiagnostics.py
----------------------------
Per-frame diagnostics shared by the camera drivers, shown in their Live panel
(Settings > Devices > camera > Show live data).

A real camera that flashes black frames now and then is usually looking at
light that flickers faster than the eye sees: a modulated or PWM-driven laser
or LED, or lamps on the mains (100/120 Hz), against an ExposureTime shorter
than one flicker period, or an ExposureAuto/GainAuto that hunts. Counting
those frames where they leave the driver tells that apart from a problem
further down (encoding, the network, the browser).

What disappears in such a frame is the light itself, often a small spot on a
dark background whose mean hardly moves. So a frame is judged by its peak,
the mean of its brightest pixels, against the peaks of the frames before it.
"""

from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np

#: A frame whose peak falls under this share of the recent frames' counts as dark.
DARK_RATIO = 0.5
#: Recent frames the comparison uses (their median peak).
HISTORY = 30
#: Below this recent peak the scene has no light worth tracking: nothing counts as dark.
MIN_PEAK = 20.0
_LOG_INTERVAL_S = 10.0


def frame_levels(frame: np.ndarray) -> tuple[float, float]:
    """``(mean, peak)`` of an 8-bit frame, on every 4th pixel each way.

    The peak is the mean of the brightest 0.05% of that sample (at least 8
    values): a spot of a few hundred pixels still makes it, a few hot pixels
    do not.
    """
    sample = np.asarray(frame)[::4, ::4].ravel()
    if sample.size == 0:
        return 0.0, 0.0
    count = min(sample.size, max(8, sample.size // 2000))
    top = np.partition(sample, sample.size - count)[sample.size - count:]
    return float(sample.mean()), float(top.mean())


class FrameDiagnostics:
    """Brightness of the last frame, and how many frames came out dark next to the ones before."""

    def __init__(self, logger, device_id: str):
        self._logger, self._device_id = logger, device_id
        self._lock = threading.Lock()
        self._peaks: deque = deque(maxlen=HISTORY)
        self.brightness: float | None = None
        self.peak: float | None = None
        self.dark_frames = 0
        self._last_logged = 0.0

    def observe(self, frame: np.ndarray) -> None:
        """Record one 8-bit frame (H×W or H×W×3)."""
        mean, peak = frame_levels(frame)
        with self._lock:
            reference = float(np.median(self._peaks)) if len(self._peaks) >= 5 else None
            # Every peak goes in, the dark ones too: if the light stays off,
            # the median follows and the new level stops counting as dark.
            self._peaks.append(peak)
            self.brightness, self.peak = mean, peak
            if reference is None or reference < MIN_PEAK or peak >= DARK_RATIO * reference:
                return
            self.dark_frames += 1
            count = self.dark_frames
            now = time.monotonic()
            log = count == 1 or now - self._last_logged >= _LOG_INTERVAL_S
            if log:
                self._last_logged = now
        if log:
            self._logger.warning(
                f"[{self._device_id}] Dark frame from the camera: peak {peak:.0f}/255 against "
                f"{reference:.0f} for the frames before ({count} so far). The light was off for "
                f"the exposure: a modulated laser/LED or mains flicker against a short "
                f"ExposureTime, or ExposureAuto/GainAuto hunting.")

    def telemetry(self) -> dict:
        with self._lock:
            return {
                "frame_brightness": round(self.brightness, 1) if self.brightness is not None else None,
                "frame_peak": round(self.peak, 1) if self.peak is not None else None,
                "dark_frames": self.dark_frames,
            }
