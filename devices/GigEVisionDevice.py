from __future__ import annotations

from .Device import Device
from .api.gigE_vision import GigECamera


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

    def set_feature(self, feature_name: str, value):
        if not self._api:
            raise RuntimeError("Cannot set feature. Camera API is not initialized.")
        try:
            self._api.set_feature(feature_name=feature_name, value=value)
        except Exception:
            self._faulted = True
            raise
