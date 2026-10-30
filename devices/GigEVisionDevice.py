from __future__ import annotations

import re

import numpy as np

from .Device import Device
from .api.gigE_vision import GigECamera

# Offsets of the R and B sites in each 2×2 Bayer tile, by pattern name.
_BAYER_RB = {"RG": ((0, 0), (1, 1)), "BG": ((1, 1), (0, 0)), "GR": ((0, 1), (1, 0)), "GB": ((1, 0), (0, 1))}


def _bit_depth(pixel_format: str | None) -> int | None:
    """Bits per sample of a GenICam pixel format name ("Mono12" -> 12), or None."""
    match = re.search(r"(\d+)(?:p|Packed)?$", pixel_format or "")
    return int(match.group(1)) if match else None


def _debayer_half(raw: np.ndarray, pattern: str) -> np.ndarray:
    """RGB at half resolution from a Bayer mosaic: one pixel per 2×2 tile, no interpolation.

    Plenty for a live view, and needs no OpenCV.
    """
    (ry, rx), (by, bx) = _BAYER_RB[pattern]
    raw = raw[: raw.shape[0] // 2 * 2, : raw.shape[1] // 2 * 2].astype(np.uint32)
    red = raw[ry::2, rx::2]
    blue = raw[by::2, bx::2]
    green = (raw[ry::2, 1 - rx::2] + raw[1 - ry::2, rx::2]) // 2
    return np.dstack((red, green, blue))


class GigEVisionDevice(Device):
    """Device wrapper around the low-level GigECamera transport class."""

    def __init__(
        self,
        cti_path: str,
        camera_index: int = 0,
        autostart_stream: bool = False,
        name: str | None = None,
    ):
        if name is None:
            name = f"gige_camera_{camera_index}"
        super().__init__(device_id=name)

        self._cti_path = cti_path
        self._camera_index = camera_index
        self._autostart_stream = autostart_stream

        self._api: GigECamera | None = None
        self._connected = False
        self._streaming = False
        self._faulted = False

    @property
    def info(self) -> dict:
        model = None
        serial = None
        if self._api and self._api.harvester and self._connected:
            try:
                device = self._api.harvester.device_info_list[self._camera_index]
                model = getattr(device, "model", None)
                serial = getattr(device, "serial_number", None)
            except Exception:
                pass

        return {
            "cti_path": self._cti_path,
            "camera_index": self._camera_index,
            "connected": self._connected,
            "streaming": self._streaming,
            "faulted": self._faulted,
            "model": model,
            "serial_number": serial,
        }

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def ready(self) -> bool:
        return self._connected

    @property
    def faulted(self) -> bool:
        return self._faulted

    @property
    def api(self) -> GigECamera | None:
        return self._api

    @property
    def streaming(self) -> bool:
        return self._streaming

    def _ensure_api(self):
        if self._api is None:
            self._api = GigECamera(cti_path=self._cti_path, camera_index=self._camera_index)

    def initialize(self):
        self._ensure_api()
        try:
            self._api.connect()
            self._connected = True
            self._faulted = False
            self._streaming = False
            if self._autostart_stream:
                self.start_stream()
            self.logger.info(f"[{self.device_id}] GigE camera initialized.")
        except Exception:
            self._connected = False
            self._streaming = False
            self._faulted = True
            raise

    def shutdown(self):
        if not self._api:
            self._connected = False
            self._streaming = False
            return

        try:
            if self._streaming:
                self._api.stop_stream()
        except Exception:
            pass

        try:
            self._api.disconnect()
        except Exception:
            pass
        finally:
            self._connected = False
            self._streaming = False

    def clear_fault(self):
        self._faulted = False
        self.logger.info(f"[{self.device_id}] Fault flag cleared.")

    def abort(self):
        self.logger.warning(f"[{self.device_id}] Aborting camera stream.")
        if not self._api:
            return
        try:
            self._api.stop_stream()
        except Exception:
            pass
        self._streaming = False

    def start_stream(self):
        if not self._api or not self._connected:
            raise RuntimeError("Cannot start stream. Camera is not connected.")
        try:
            self._api.start_stream()
            self._streaming = True
        except Exception:
            self._faulted = True
            raise

    def stop_stream(self):
        if not self._api:
            return
        try:
            self._api.stop_stream()
        finally:
            self._streaming = False

    def capture_frame(self, timeout_sec: float = 2.0):
        if not self._api:
            raise RuntimeError("Cannot capture frame. Camera API is not initialized.")
        try:
            return self._api.capture_frame(timeout_sec=timeout_sec)
        except Exception:
            self._faulted = True
            raise

    def capture_frame_8bit(self, timeout_sec: float = 2.0) -> np.ndarray:
        """One frame ready to display: 8-bit mono ``H×W``, or RGB ``H×W×3``.

        Samples deeper than 8 bits are scaled by the pixel format's bit depth
        (not by the image content, so brightness does not pump), and Bayer
        mosaics become half-resolution RGB. Starts the stream if needed, e.g.
        after an abort stopped it.
        """
        if not self._streaming:
            self.start_stream()
        frame = self.capture_frame(timeout_sec=timeout_sec)
        pixel_format = self._api.last_pixel_format or ""
        bits = _bit_depth(pixel_format) or 8
        if pixel_format.startswith("Bayer") and frame.ndim == 2:
            frame = _debayer_half(frame, pixel_format[5:7])
        if frame.dtype != np.uint8 or bits > 8:
            frame = (frame >> max(0, bits - 8)).astype(np.uint8)
        if pixel_format.startswith("BGR") and frame.ndim == 3:
            frame = frame[:, :, 2::-1]
        return frame

    def set_feature(self, feature_name: str, value):
        if not self._api:
            raise RuntimeError("Cannot set feature. Camera API is not initialized.")
        try:
            self._api.set_feature(feature_name=feature_name, value=value)
        except Exception:
            self._faulted = True
            raise
