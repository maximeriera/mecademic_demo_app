"""
devices/CVProcessDevice.py
--------------------------
Computer-vision processing on the frames of a camera device.

A ``cv_process`` device reads the camera its ``camera:`` key names, hands
each frame to an analysis function the workspace provides, and keeps the
last raw frame, the last processed frame and the last result::

    devices:
      camera:
        type: "gige_camera"
        cti_path: "C:/.../Spinnaker_GenTL_v140.cti"
      vision:
        type: "cv_process"
        camera: "camera"
        analysis: "blob_analysis:analyze"     # "module:function", importable from the workspace
        analysis_params: {threshold: 175}     # passed to the function as `params`
        autostart: true                       # else start()/stop() it, or the Devices tab switch

The analysis contract::

    from devices.CVProcessDevice import AnalysisResult

    def analyze(frame: np.ndarray, params: dict) -> AnalysisResult:
        ...
        return AnalysisResult(image=overlay, data={"x_px": 512.0}, ok=True)

``frame`` is 8-bit, mono ``H×W`` or RGB ``H×W×3``, and read-only: copy it
before drawing on it. The function may also return a bare image, a bare data
dict, ``(image, data)`` or ``None``; it may take ``frame`` alone if it needs
no params. ``examples/gige_cv_analysis/blob_analysis.py`` is a prototype to
start from.

Every public method reads cached values under one condition, so task code,
the custom view and the Devices tab can all use the device at once. While the
analysis runs continuously, its loop thread is the only one reading the
camera through this device.
"""

from __future__ import annotations

import inspect
import threading
import time
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable, Dict, Optional

import numpy as np

from .Device import Device

#: Images :meth:`CVProcessDevice.get_image` serves (Devices tab thumbnails).
IMAGE_NAMES = ("raw", "processed")

#: Package a workspace may keep its code in (the Docker image's layout), tried
#: after the plain module name, as for task modules and the custom view.
WORKSPACE_PACKAGE = "app_logic"

_ERROR_LOG_INTERVAL_S = 10.0


@dataclass
class AnalysisResult:
    """What an analysis function returns."""
    image: Optional[np.ndarray] = None                  # processed/annotated frame; None shows the raw one
    data: Dict[str, Any] = field(default_factory=dict)  # JSON-serialisable measurements
    ok: Optional[bool] = None                           # optional pass/fail verdict


def load_analysis(target: str) -> Callable:
    """The callable that ``"module:function"`` names (``"module:obj.method"`` works too).

    ``module`` is looked up as given, then inside the workspace's ``app_logic``
    package, so ``"blob_analysis:analyze"`` finds ``app_logic/blob_analysis.py``
    in a workspace laid out like the Docker image (``--workspace /webapp``).
    """
    module_name, sep, attr_path = str(target).partition(":")
    if not (module_name and sep and attr_path):
        raise ValueError(f'analysis must be "module:function", got {target!r}')
    candidates = [module_name]
    if module_name != WORKSPACE_PACKAGE and not module_name.startswith(WORKSPACE_PACKAGE + "."):
        candidates.append(f"{WORKSPACE_PACKAGE}.{module_name}")
    obj: Any = None
    for name in candidates:
        try:
            obj = import_module(name)
            break
        except ModuleNotFoundError as e:
            # Only "this candidate does not exist" means try the next one; a
            # missing import *inside* the module (scipy, cv2...) is the error.
            if not (e.name and (name == e.name or name.startswith(e.name + "."))):
                raise
    if obj is None:
        tried = " or ".join(f"'{name}'" for name in candidates)
        raise ModuleNotFoundError(f"No module named {tried} in the workspace", name=module_name)
    for attr in attr_path.split("."):
        obj = getattr(obj, attr)
    if not callable(obj):
        raise TypeError(f"analysis {target!r} is not callable")
    return obj


def _takes_params(fn: Callable) -> bool:
    """True if *fn* accepts a second positional argument (the params dict)."""
    try:
        parameters = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return True  # no signature to read: assume the documented contract
    positional = 0
    for p in parameters:
        if p.kind == p.VAR_POSITIONAL:
            return True
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            positional += 1
    return positional >= 2


def normalize_result(value: Any) -> AnalysisResult:
    """Map whatever an analysis function returned to an :class:`AnalysisResult`."""
    if value is None:
        result = AnalysisResult()
    elif isinstance(value, AnalysisResult):
        result = value
    elif isinstance(value, np.ndarray):
        result = AnalysisResult(image=value)
    elif isinstance(value, dict):
        result = AnalysisResult(data=value)
    elif isinstance(value, tuple) and len(value) == 2:
        result = AnalysisResult(image=value[0], data=value[1] if value[1] is not None else {})
    else:
        raise TypeError(f"analysis returned {type(value).__name__}; "
                        "expected an AnalysisResult, an image, a dict or (image, data)")
    if result.image is not None and not (isinstance(result.image, np.ndarray) and result.image.ndim in (2, 3)):
        raise TypeError("the analysis image must be a NumPy array, H×W or H×W×3")
    if not isinstance(result.data, dict):
        raise TypeError(f"the analysis data must be a dict, got {type(result.data).__name__}")
    if result.ok is not None:
        result.ok = bool(result.ok)
    return result


def _read_only(image: np.ndarray) -> np.ndarray:
    """A read-only view: whoever gets a cached image cannot draw into the cache."""
    view = image.view()
    view.flags.writeable = False
    return view


def _shown(value: Any) -> bool:
    """True for a result value the Live panel can show on one line."""
    scalar = (bool, int, float, str, np.generic, type(None))
    if isinstance(value, scalar):
        return True
    return isinstance(value, (list, tuple)) and len(value) <= 6 and all(isinstance(v, scalar) for v in value)


class CVProcessDevice(Device):
    """Runs a workspace analysis function on the frames of a camera device."""

    def __init__(
        self,
        camera: Device,
        analysis: str,
        analysis_params: dict | None = None,
        autostart: bool = False,
        rate_hz: float = 5.0,
        capture_timeout_s: float = 2.0,
        fault_on_error: bool = False,
        name: str | None = None,
    ):
        super().__init__(device_id=name or "cv_process")
        if not callable(getattr(camera, "capture_frame_8bit", None)):
            raise TypeError(f"camera '{getattr(camera, 'device_id', camera)}' has no capture_frame_8bit(); "
                            "link a gige_camera or simulated_camera device")
        if rate_hz <= 0:
            raise ValueError("rate_hz must be > 0")
        if capture_timeout_s <= 0:
            raise ValueError("capture_timeout_s must be > 0")

        self._camera = camera
        self._analysis_target = analysis
        self._analysis = load_analysis(analysis)
        self._pass_params = _takes_params(self._analysis)
        self._params = dict(analysis_params or {})
        self._autostart = autostart
        self._rate_hz = float(rate_hz)
        self._capture_timeout_s = float(capture_timeout_s)
        self._fault_on_error = fault_on_error

        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._initialized = False
        self._faulted = False
        self._state = "stopped"
        self._abort_gen = 0          # bumped by abort(): wakes wait_for_result()

        # Latest outputs: replaced, never mutated, under _cond.
        self._seq = 0                # one per frame analysed (or attempted)
        self._raw: np.ndarray | None = None
        self._processed: np.ndarray | None = None
        self._result: dict | None = None
        self._frames = 0
        self._errors = 0
        self._capture_errors = 0
        self._last_error: str | None = None
        self._last_logged = 0.0
        self._fps: float | None = None
        self._analysis_ms: float | None = None

    # --- Device interface ---------------------------------------------------

    @property
    def info(self) -> dict:
        return {
            "camera": self._camera.device_id,
            "analysis": self._analysis_target,
            "autostart": self._autostart,
            "rate_hz": self._rate_hz,
            "fault_on_error": self._fault_on_error,
        }

    @property
    def connected(self) -> bool:
        return self._initialized

    @property
    def ready(self) -> bool:
        return self._initialized and not self._faulted

    @property
    def faulted(self) -> bool:
        return self._faulted

    @property
    def api(self) -> Device:
        """The camera device the frames come from."""
        return self._camera

    def initialize(self) -> None:
        self.stop()
        self._initialized = True
        self._faulted = False
        self.logger.info(f"[{self.device_id}] Ready: {self._analysis_target} on '{self._camera.device_id}'.")
        if self._autostart:
            self.start()

    def shutdown(self) -> None:
        self.stop()
        self._initialized = False

    def clear_fault(self) -> None:
        self._faulted = False
        self.logger.info(f"[{self.device_id}] Fault flag cleared.")

    def abort(self) -> None:
        """Wake task code blocked in :meth:`wait_for_result`. The analysis itself keeps running."""
        with self._cond:
            self._abort_gen += 1
            self._cond.notify_all()

    # --- Continuous analysis ------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop_event.is_set()

    def start(self) -> None:
        """Analyse frames continuously, at most ``rate_hz`` per second. No-op if already running."""
        if not self._initialized:
            raise RuntimeError(f"{self.device_id} is not initialized. Press INITIALIZE first.")
        with self._cond:
            if self.running:
                return
            self._stop_event = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop_event,),
                                            name=f"CV-{self.device_id}", daemon=True)
            self._state = "starting"
            self._thread.start()
        self.logger.info(f"[{self.device_id}] Analysis started ({self._rate_hz:g} Hz max).")

    def stop(self) -> None:
        """Stop the continuous analysis and wait for its loop to exit. No-op if stopped."""
        with self._cond:
            thread, self._thread = self._thread, None
            self._stop_event.set()
            self._cond.notify_all()
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=self._capture_timeout_s + 1.0)
        if thread.is_alive():
            self.logger.warning(f"[{self.device_id}] Analysis loop still busy after stop; it exits after this frame.")
        else:
            self.logger.info(f"[{self.device_id}] Analysis stopped.")
        self._state = "stopped"

    def _run(self, stop: threading.Event) -> None:
        period = 1.0 / self._rate_hz
        try:
            while not stop.is_set():
                camera = self._camera
                if not camera.connected or camera.faulted:
                    self._state = "waiting for camera"
                    stop.wait(0.5)
                    continue
                self._state = "running"
                started = time.monotonic()
                try:
                    frame = camera.capture_frame_8bit(timeout_sec=self._capture_timeout_s)
                except Exception as e:
                    # The camera marks itself faulted: the cell stops through it, not us.
                    with self._cond:
                        self._capture_errors += 1
                        self._last_error = f"capture: {type(e).__name__}: {e}"
                    stop.wait(0.5)
                    continue
                if frame is None:
                    stop.wait(0.05)
                    continue
                self._process(frame, stop)
                stop.wait(max(0.0, period - (time.monotonic() - started)))
                loop_s = time.monotonic() - started
                if loop_s > 0:
                    self._fps = 1.0 / loop_s if self._fps is None else 0.8 * self._fps + 0.2 / loop_s
        except Exception as e:  # pragma: no cover - a bug in this loop, not in the analysis
            self.logger.error(f"[{self.device_id}] Analysis loop crashed: {e}", exc_info=True)
            self._last_error = f"loop: {type(e).__name__}: {e}"
        finally:
            self._state = "stopped"

    def _process(self, frame: Any, stop: threading.Event | None = None) -> dict | None:
        """Analyse one frame and publish it. The new result, or None if the analysis failed."""
        frame = _read_only(np.asarray(frame))
        started = time.monotonic()
        try:
            output = self._analysis(frame, self._params) if self._pass_params else self._analysis(frame)
            result, error = normalize_result(output), None
        except Exception as e:
            result, error = None, e
        analysis_ms = (time.monotonic() - started) * 1000.0

        with self._cond:
            if stop is not None and stop.is_set():
                return None  # stopped while this frame was in flight
            self._seq += 1
            self._raw = frame
            published = None
            if result is not None:
                self._processed = frame if result.image is None else _read_only(result.image)
                published = {"seq": self._seq, "time": time.time(), "ok": result.ok,
                             "data": result.data, "analysis_ms": round(analysis_ms, 2)}
                self._result = published
                self._frames += 1
                self._analysis_ms = (analysis_ms if self._analysis_ms is None
                                     else 0.8 * self._analysis_ms + 0.2 * analysis_ms)
            self._cond.notify_all()

        if error is not None:
            self._on_analysis_error(error)
        return published

    def _on_analysis_error(self, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"
        with self._cond:
            self._errors += 1
            self._last_error = message
            count = self._errors
            if self._fault_on_error:
                self._faulted = True
        if self._fault_on_error:
            self.logger.error(f"[{self.device_id}] Analysis failed, device faulted: {message}", exc_info=error)
            return
        now = time.monotonic()
        if count == 1 or now - self._last_logged >= _ERROR_LOG_INTERVAL_S:
            self._last_logged = now
            self.logger.warning(f"[{self.device_id}] Analysis failed ({count} error(s) so far): {message}",
                                exc_info=error)

    # --- Results, for task code and views -------------------------------------

    def last_raw_image(self) -> np.ndarray | None:
        """The last frame acquired (read-only), or None before the first one."""
        with self._cond:
            return self._raw

    def last_processed_image(self) -> np.ndarray | None:
        """The image the last successful analysis returned (read-only), or the raw frame if it returned none."""
        with self._cond:
            return self._processed

    def last_result(self) -> dict | None:
        """``{seq, time, ok, data, analysis_ms}`` of the last successful analysis, or None."""
        with self._cond:
            return dict(self._result) if self._result is not None else None

    def wait_for_result(self, after_seq: int | None = None, timeout: float = 5.0) -> dict:
        """The first result newer than *after_seq* (default: newer than the current one).

        Raises TimeoutError if none comes within *timeout* seconds, and
        RuntimeError if the task is aborted meanwhile.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            if after_seq is None:
                after_seq = self._result["seq"] if self._result is not None else 0
            generation = self._abort_gen
            while True:
                result = self._result
                if result is not None and result["seq"] > after_seq:
                    return dict(result)
                if self._abort_gen != generation:
                    raise RuntimeError(f"{self.device_id}: aborted while waiting for a result.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    hint = f" (last error: {self._last_error})" if self._last_error else ""
                    raise TimeoutError(f"{self.device_id}: no new result within {timeout:g} s{hint}.")
                self._cond.wait(remaining)

    def analyze_once(self, timeout: float = 5.0) -> dict:
        """One fresh result: the next one of the running analysis, or one frame grabbed and analysed now."""
        if self.running:
            return self.wait_for_result(timeout=timeout)
        if not self._initialized:
            raise RuntimeError(f"{self.device_id} is not initialized. Press INITIALIZE first.")
        frame = self._camera.capture_frame_8bit(timeout_sec=min(timeout, self._capture_timeout_s))
        result = self._process(frame)
        if result is None:
            raise RuntimeError(f"{self.device_id}: analysis failed: {self._last_error}")
        return dict(result)

    def video_source(self, kind: str = "processed") -> Callable[[], np.ndarray | None]:
        """A ``grab`` for ``hub.add_video``: the latest *kind* image, None until it changes.

        Returning None lets the stream skip re-encoding a frame it already sent.
        """
        if kind not in IMAGE_NAMES:
            raise ValueError(f"kind must be one of {', '.join(IMAGE_NAMES)}, got {kind!r}")
        sent = [None]

        def grab():
            with self._cond:
                if kind == "raw":
                    seq, image = self._seq, self._raw
                else:
                    seq, image = (self._result or {}).get("seq"), self._processed
            if image is None or seq == sent[0]:
                return None
            sent[0] = seq
            return image

        return grab

    # --- Devices tab ------------------------------------------------------------

    def telemetry(self) -> dict:
        with self._cond:
            result = self._result
            values = {
                "state": (self._state if self.running else "stopped") if self._initialized else "not initialized",
                "frames": self._frames,
                "analysis_fps": round(self._fps, 2) if self._fps and self.running else None,
                "analysis_ms": round(self._analysis_ms, 1) if self._analysis_ms is not None else None,
                "errors": self._errors,
                "capture_errors": self._capture_errors,
                "last_error": self._last_error,
                "ok": result["ok"] if result else None,
            }
        for key, value in (result["data"] if result else {}).items():
            if _shown(value):
                values[key if key not in values else f"result_{key}"] = value
        return values

    def io_points(self) -> list:
        result = self._result
        return [
            {"name": "analysis_running", "kind": "do", "value": self.running, "writable": True},
            {"name": "last_ok", "kind": "di", "value": bool(result and result["ok"]), "writable": False},
        ]

    def write_io(self, name: str, value) -> None:
        if name != "analysis_running":
            raise ValueError(f"{self.device_id} has no writable point '{name}'.")
        if not isinstance(value, bool):
            raise ValueError("analysis_running takes true or false.")
        if value:
            self.start()
        else:
            self.stop()

    def images(self) -> list:
        return list(IMAGE_NAMES)

    def get_image(self, name: str) -> np.ndarray | None:
        if name == "raw":
            return self.last_raw_image()
        if name == "processed":
            return self.last_processed_image()
        raise KeyError(name)
