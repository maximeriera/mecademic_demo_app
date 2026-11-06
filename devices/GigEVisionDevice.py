from __future__ import annotations

import re
import threading

import numpy as np

from .CameraDiagnostics import FrameDiagnostics
from .CameraFeatures import FeatureSettingsMixin, check_trigger, trigger_features
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


class GigEVisionDevice(FeatureSettingsMixin, Device):
    """Device wrapper around the low-level GigECamera transport class."""

    def __init__(
        self,
        cti_path: str,
        camera_index: int = 0,
        autostart_stream: bool = False,
        features: dict | None = None,
        features_file: str | None = None,
        trigger: str = "off",
        trigger_source: str = "Line0",
        trigger_activation: str = "RisingEdge",
        name: str | None = None,
    ):
        if name is None:
            name = f"gige_camera_{camera_index}"
        super().__init__(device_id=name)
        check_trigger(trigger, trigger_activation)

        self._cti_path = cti_path
        self._camera_index = camera_index
        self._autostart_stream = autostart_stream
        # off: free-run; software: one TriggerSoftware per capture; hardware: an input line.
        self._trigger = trigger
        self._trigger_source = trigger_source
        self._trigger_activation = trigger_activation
        # GenICam features written at every INITIALIZE, in order (devices/CameraFeatures.py).
        self._init_feature_settings(features, features_file)

        self._api: GigECamera | None = None
        self._connected = False
        self._streaming = False
        self._faulted = False
        # Serialises captures, so two readers (a CV process and a live view)
        # take turns on the acquirer instead of racing on fetch(). abort()
        # stays outside it: it must be able to interrupt a fetch in progress.
        self._capture_lock = threading.RLock()
        # Brightness and near-black frames, for the Live panel (devices/CameraDiagnostics.py).
        self._diagnostics = FrameDiagnostics(self.logger, self.device_id)

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
            "trigger": self._trigger_summary(),
            "features": self._features_summary(),
        }

    def _trigger_summary(self) -> str:
        if self._trigger == "hardware":
            return f"hardware ({self._trigger_source}, {self._trigger_activation})"
        return self._trigger

    @property
    def trigger(self) -> str:
        """``"off"`` (free-run), ``"software"`` or ``"hardware"``."""
        return self._trigger

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
            # A re-INITIALIZE finds the stream running: features go in with it stopped.
            self._api.stop_stream()
            self._streaming = False
            self._connected = True   # set_feature() needs it; undone below on failure
            try:
                self._apply_trigger()
                self._load_and_apply_features()   # after: the file may tune TriggerDelay...
            except Exception:
                try:
                    self._api.disconnect()
                except Exception:
                    pass
                raise
            self._faulted = False
            if self._autostart_stream:
                self.start_stream()
            self.logger.info(f"[{self.device_id}] GigE camera initialized.")
        except Exception:
            self._connected = False
            self._streaming = False
            self._faulted = True
            raise

    def _apply_trigger(self):
        """Put the camera in free-run, software- or hardware-triggered acquisition."""
        for feature_name, value, optional in trigger_features(self._trigger, self._trigger_source,
                                                              self._trigger_activation):
            try:
                self.set_feature(feature_name, value)
            except KeyError:
                if not optional:
                    raise RuntimeError(f"trigger {self._trigger}: the camera has no {feature_name}") from None
            except Exception as e:
                raise RuntimeError(f"trigger {self._trigger}: {feature_name}={value!r}: {e}") from e
        if self._trigger != "off":
            self.logger.info(f"[{self.device_id}] Trigger: {self._trigger_summary()}.")

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
        with self._capture_lock:
            try:
                return self._api.capture_frame(timeout_sec=timeout_sec)
            except TimeoutError:
                # With a hardware trigger, no frame only means no pulse came.
                if self._trigger != "hardware":
                    self._faulted = True
                raise
            except Exception:
                self._faulted = True
                raise

    def capture_frame_8bit(self, timeout_sec: float = 2.0) -> np.ndarray:
        """One frame ready to display: 8-bit mono ``H×W``, or RGB ``H×W×3``.

        Samples deeper than 8 bits are scaled by the pixel format's bit depth
        (not by the image content, so brightness does not pump), and Bayer
        mosaics become half-resolution RGB. Starts the stream if needed, e.g.
        after an abort stopped it. Safe to call from several threads.

        With ``trigger: software`` it fires TriggerSoftware for a fresh frame;
        with ``trigger: hardware`` it waits for the next pulse, raising
        TimeoutError (without faulting) if none comes within *timeout_sec*.
        """
        with self._capture_lock:
            if not self._streaming:
                self.start_stream()
            if self._trigger == "software":
                try:
                    self._api.discard_queued()   # a late frame from an earlier trigger is stale
                    self._api.execute("TriggerSoftware")
                except Exception:
                    self._faulted = True
                    raise
            frame = self.capture_frame(timeout_sec=timeout_sec)
            pixel_format = self._api.last_pixel_format or ""
        bits = _bit_depth(pixel_format) or 8
        if pixel_format.startswith("Bayer") and frame.ndim == 2:
            frame = _debayer_half(frame, pixel_format[5:7])
        if frame.dtype != np.uint8 or bits > 8:
            frame = (frame >> max(0, bits - 8)).astype(np.uint8)
        if pixel_format.startswith("BGR") and frame.ndim == 3:
            frame = frame[:, :, 2::-1]
        self._diagnostics.observe(frame)
        return frame

    def telemetry(self) -> dict:
        """Stream health for the Live panel: cached values only, nothing is read from the camera."""
        api = self._api
        return {
            "streaming": self._streaming,
            "trigger": self._trigger,
            "pixel_format": api.last_pixel_format if api else None,
            "incomplete_frames": getattr(api, "incomplete_frames", 0),
            **self._diagnostics.telemetry(),
        }

    def set_feature(self, feature_name: str, value):
        """Write a GenICam feature now, e.g. ``camera.set_feature("ExposureTime", 20000.0)``.

        Waits for the frame being fetched. A feature the camera locks while
        streaming (PixelFormat, Width...) is written with the stream stopped,
        then the stream restarts. Errors go to the caller and never fault the
        cell: KeyError for an unknown feature, ValueError for a refused value
        or a read-only feature. The change lasts until the next INITIALIZE,
        which writes ``features`` from config.yaml again.
        """
        if not self._api or not self._connected:
            raise RuntimeError("Cannot set feature. Camera is not connected.")
        with self._capture_lock:
            restart = False
            if not self._api.is_writable(feature_name):
                if not self._streaming:
                    raise ValueError(f"feature '{feature_name}' is read-only.")
                restart = True
                self.stop_stream()
            try:
                if restart and not self._api.is_writable(feature_name):
                    raise ValueError(f"feature '{feature_name}' is read-only, even with the stream stopped.")
                try:
                    old = self._api.get_feature(feature_name)
                except Exception:
                    old = "?"
                self._api.set_feature(feature_name, value)
            finally:
                if restart:
                    self.start_stream()
        self.logger.info(f"[{self.device_id}] Feature {feature_name}: {old} -> {value}"
                         + (" (stream restarted)" if restart else ""))

    def get_feature(self, feature_name: str):
        """Current value of a GenICam feature. KeyError if the camera has none."""
        if not self._api or not self._connected:
            raise RuntimeError("Cannot read feature. Camera is not connected.")
        return self._api.get_feature(feature_name)

    def describe_feature(self, feature_name: str) -> dict:
        """``{name, value, type, writable, min, max, inc, unit, choices, display_name, tooltip, visibility}``."""
        if not self._api or not self._connected:
            raise RuntimeError("Cannot read feature. Camera is not connected.")
        return self._api.describe_feature(feature_name)

    def feature_tree(self, visibility: str = "Beginner") -> list:
        """Every value feature up to *visibility*, by GenICam category (commands are left out)."""
        if not self._api or not self._connected:
            raise RuntimeError("Cannot read features. Camera is not connected.")
        return self._api.feature_tree(visibility)
