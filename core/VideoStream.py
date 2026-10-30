"""
core/VideoStream.py
-------------------
A camera feed for a workspace custom view.

One thread grabs frames from the device, reduces them to a maximum width and
JPEG-compresses them **once**; every viewer then receives the same bytes as
an MJPEG stream (``/api/data/video/<name>.mjpg``), which a browser shows in
a plain ``<img>``. Compressing on the server is what keeps the feed fluid: a
5 MP mono frame is 5 MB raw and ~150 kB as a JPEG at 1280 px, quality 75.

The thread only reads the camera while at least one page is watching (unless
``always_on``), so an idle cell never streams. It is the only reader of the
camera: task code must not fetch frames from the same device concurrently.

Registered from a custom view's ``setup(hub, devices)``::

    hub.add_video("camera", lambda: camera.capture_frame_8bit(), fps=15,
                  quality=75, max_width=1280, device="camera")
"""

from __future__ import annotations

import io
import itertools
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import numpy as np
from PIL import Image

_stream_ids = itertools.count(1)


@dataclass(frozen=True)
class EncodedFrame:
    seq: int
    jpeg: bytes
    width: int
    height: int
    t: float


def to_uint8(frame: Any, bit_depth: int | None = None) -> np.ndarray:
    """An 8-bit view of *frame* (mono ``H×W`` or colour ``H×W×3``).

    Integer frames deeper than 8 bits are shifted down by ``bit_depth - 8``;
    without ``bit_depth`` it is guessed from the frame's maximum, which can
    flicker on dark scenes, so drivers that know their pixel format should
    pass it (see ``GigEVisionDevice.capture_frame_8bit``).
    """
    arr = np.asarray(frame)
    if arr.dtype == np.uint8:
        return arr
    if np.issubdtype(arr.dtype, np.floating):
        return np.clip(arr * 255.0 if arr.max(initial=0) <= 1.0 else arr, 0, 255).astype(np.uint8)
    if bit_depth is None:
        peak = int(arr.max(initial=0))
        bit_depth = 16 if peak > 4095 else 12 if peak > 1023 else 10 if peak > 255 else 8
    return (arr >> max(0, bit_depth - 8)).astype(np.uint8)


def encode_jpeg(frame: Any, quality: int = 75, max_width: int | None = 1280) -> tuple[bytes, int, int]:
    """JPEG bytes of *frame*, shrunk by an integer factor to at most *max_width* pixels wide."""
    arr = to_uint8(frame)
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[:, :, 0]
    if arr.ndim == 3 and arr.shape[2] == 4:
        arr = arr[:, :, :3]
    image = Image.fromarray(np.ascontiguousarray(arr))
    if max_width and image.width > max_width:
        # Integer box reduction: much faster than a resampling filter, and
        # averaging pixels also takes out sensor noise before compression.
        image = image.reduce(math.ceil(image.width / max_width))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=int(quality))
    return buffer.getvalue(), image.width, image.height


class VideoStream:
    """Grab → reduce → JPEG on one thread; any number of MJPEG viewers."""

    _ERROR_LOG_INTERVAL_S = 10.0

    def __init__(self, name: str, grab: Callable[[], Any], fps: float = 10.0, quality: int = 75,
                 max_width: int | None = 1280, device: str | None = None, always_on: bool = False,
                 logger=None):
        if fps <= 0:
            raise ValueError(f"Video '{name}': fps must be > 0.")
        if not 1 <= int(quality) <= 95:
            raise ValueError(f"Video '{name}': quality must be between 1 and 95.")
        self.name = name
        self.grab = grab
        self.fps = float(fps)
        self.quality = int(quality)
        self.max_width = max_width
        self.device = device
        self.always_on = always_on
        self.logger = logger
        #: Changes whenever the hub rebuilds the stream (each Initialize), so
        #: a page knows to reopen it.
        self.stream_id = next(_stream_ids)

        self._cond = threading.Condition()
        self._latest: Optional[EncodedFrame] = None
        self._seq = 0
        self._viewers = 0
        self.closed = False
        self.paused = False

        self.errors = 0
        self.last_error: Optional[str] = None
        self._last_logged = 0.0
        self._measured_fps: Optional[float] = None
        self._encode_ms: Optional[float] = None

    # --- Viewers -----------------------------------------------------------

    @contextmanager
    def viewer(self):
        """Count one watching page for the duration of the block."""
        with self._cond:
            self._viewers += 1
            self._cond.notify_all()
        try:
            yield self
        finally:
            with self._cond:
                self._viewers -= 1

    def wait_frame(self, after_seq: int | None, timeout: float) -> Optional[EncodedFrame]:
        """The latest frame newer than *after_seq*, waiting up to *timeout*; None if none came."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self.closed:
                frame = self._latest
                if frame is not None and (after_seq is None or frame.seq > after_seq):
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
        return None

    def close(self) -> None:
        """End every viewer's stream (the hub stopped or is being rebuilt)."""
        with self._cond:
            self.closed = True
            self._cond.notify_all()

    # --- Grabbing thread ---------------------------------------------------

    def run(self, stop: threading.Event, device_available: Callable[[str], bool]) -> None:
        period = 1.0 / self.fps
        while not stop.is_set():
            with self._cond:
                idle = not self.always_on and self._viewers == 0
                if idle:
                    self._cond.wait(0.5)
            if idle:
                continue
            if self.device is not None and not device_available(self.device):
                self.paused = True
                stop.wait(0.5)
                continue
            self.paused = False

            started = time.monotonic()
            try:
                frame = self.grab()
                if frame is None:
                    stop.wait(min(period, 0.05))
                    continue
                encode_started = time.monotonic()
                jpeg, width, height = encode_jpeg(frame, self.quality, self.max_width)
                self._encode_ms = (time.monotonic() - encode_started) * 1000.0
            except Exception as e:
                self._record_error(e)
                stop.wait(0.5)
                continue

            with self._cond:
                self._seq += 1
                self._latest = EncodedFrame(self._seq, jpeg, width, height, time.monotonic())
                self._cond.notify_all()

            elapsed = time.monotonic() - started
            stop.wait(max(0.0, period - elapsed))
            loop_s = time.monotonic() - started
            if loop_s > 0:
                rate = 1.0 / loop_s
                self._measured_fps = rate if self._measured_fps is None else 0.8 * self._measured_fps + 0.2 * rate

    def _record_error(self, error: Exception) -> None:
        self.errors += 1
        self.last_error = f"{type(error).__name__}: {error}"
        now = time.monotonic()
        if self.logger and now - self._last_logged >= self._ERROR_LOG_INTERVAL_S:
            self._last_logged = now
            self.logger.warning("Video '%s' grab failed (%d error(s) so far): %s",
                                self.name, self.errors, self.last_error)

    # --- Manifest -------------------------------------------------------------

    def describe(self) -> Dict[str, Any]:
        frame = self._latest
        return {
            "stream_id": self.stream_id,
            "url": f"/api/data/video/{self.name}.mjpg",
            "snapshot_url": f"/api/data/video/{self.name}.jpg",
            "fps_target": self.fps,
            "fps": (round(self._measured_fps, 1)
                    if self._measured_fps and self._viewers and not self.paused else None),
            "quality": self.quality,
            "max_width": self.max_width,
            "width": frame.width if frame else None,
            "height": frame.height if frame else None,
            "frame_kb": round(len(frame.jpeg) / 1024, 1) if frame else None,
            "encode_ms": round(self._encode_ms, 1) if self._encode_ms is not None else None,
            "viewers": self._viewers,
            "paused": self.paused,
            "errors": self.errors,
            "last_error": self.last_error,
            "device": self.device,
        }
