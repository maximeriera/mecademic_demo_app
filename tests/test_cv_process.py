"""cv_process devices: device links, the CV device, the simulated camera and the prototype analysis."""

import importlib
import io
import os
import sys
import threading
import time
import uuid

import numpy as np
import pytest
from PIL import Image

from conftest import REPO_ROOT, wait_for
from devices.CVProcessDevice import AnalysisResult, CVProcessDevice, normalize_result
from devices.SimulatedCamera import SimulatedCamera, TestPattern
from devices.SimulatedDevice import SimulatedDevice
from devices.registry import create_device, dependency_order, validate_devices_config

EXAMPLE_DIR = os.path.join(REPO_ROOT, "examples", "gige_cv_analysis")

ANALYSIS_MODULE = '''
from devices.CVProcessDevice import AnalysisResult

MODE = {"value": "ok"}


def analyze(frame, params):
    if MODE["value"] == "raise":
        raise ValueError("boom")
    return AnalysisResult(image=255 - frame, data={"mean": float(frame.mean()), "scale": params.get("scale")}, ok=True)


def frame_only(frame):
    return {"shape": list(frame.shape)}


def draw_in_place(frame, params):
    frame[0, 0] = 0
'''


def _errors(issues):
    return {i["device"]: i["message"] for i in issues if i["level"] == "error"}


@pytest.fixture
def analysis(tmp_path, monkeypatch):
    """A workspace analysis module, importable under a name unique to the test."""
    name = f"cv_test_analysis_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{name}.py").write_text(ANALYSIS_MODULE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield importlib.import_module(name)
    sys.modules.pop(name, None)


@pytest.fixture
def camera():
    cam = SimulatedCamera(width=320, height=240, fps=200, name="cam")
    cam.initialize()
    yield cam
    cam.shutdown()


@pytest.fixture
def make_vision(camera, analysis):
    created = []

    def make(fn="analyze", **kwargs):
        kwargs.setdefault("rate_hz", 100)
        vision = CVProcessDevice(camera=camera, analysis=f"{analysis.__name__}:{fn}", name="vision", **kwargs)
        created.append(vision)
        return vision

    yield make
    for vision in created:
        vision.shutdown()


# --- Device links (registry) --------------------------------------------------

def test_device_key_links_entries_and_orders_them():
    entries, issues = validate_devices_config({
        "vision": {"type": "cv_process", "camera": "cam", "analysis": "m:f"},
        "cam": {"type": "simulated_camera"},
        "other": {"type": "simulated"},
    })
    assert issues == []
    by_name = {e.name: e for e in entries}
    assert by_name["vision"].depends_on == ("cam",)
    assert by_name["cam"].depends_on == ()
    deps = {e.name: e.depends_on for e in entries}
    assert dependency_order(by_name, deps) == (["cam", "vision", "other"], [])
    assert dependency_order(["cam", "vision", "other"], deps, dependents_first=True) == (["vision", "cam", "other"], [])
    # Without links, the config order stands both ways.
    assert dependency_order(["a", "b", "c"], {}) == (["a", "b", "c"], [])
    assert dependency_order(["a", "b", "c"], {}, dependents_first=True) == (["a", "b", "c"], [])


def test_broken_links_are_errors():
    entries, issues = validate_devices_config({
        "cam": {"type": "simulated_camera"},
        "off_cam": {"type": "simulated_camera", "enabled": False},
        "bad_cam": {"type": "simulated_camera", "width": "wide"},
        "typo": {"type": "cv_process", "camera": "cma", "analysis": "m:f"},
        "self": {"type": "cv_process", "camera": "self", "analysis": "m:f"},
        "disabled": {"type": "cv_process", "camera": "off_cam", "analysis": "m:f"},
        "invalid": {"type": "cv_process", "camera": "bad_cam", "analysis": "m:f"},
        "chained": {"type": "cv_process", "camera": "invalid", "analysis": "m:f"},
        "empty": {"type": "cv_process", "camera": "  ", "analysis": "m:f"},
    })
    assert [e.name for e in entries] == ["cam"]
    errors = _errors(issues)
    assert "no device named 'cma' (did you mean 'cam'?)" in errors["typo"]
    assert "cannot depend on itself" in errors["self"]
    assert "'off_cam' is disabled" in errors["disabled"]
    assert "'bad_cam' has errors" in errors["invalid"]
    assert "'invalid' has errors" in errors["chained"]
    assert "name of another device" in errors["empty"]


def test_dependency_cycles_are_errors():
    entries, issues = validate_devices_config({
        "a": {"type": "cv_process", "camera": "b", "analysis": "m:f"},
        "b": {"type": "cv_process", "camera": "a", "analysis": "m:f"},
        "c": {"type": "cv_process", "camera": "a", "analysis": "m:f"},
        "cam": {"type": "simulated_camera"},
    })
    assert [e.name for e in entries] == ["cam"]
    errors = _errors(issues)
    assert "dependency cycle between 'a', 'b'" in errors["a"]
    assert "dependency cycle" in errors["b"]
    assert "'a' has errors" in errors["c"]   # depends on the cycle, is not part of it


def test_create_device_injects_the_linked_device(analysis):
    entries, issues = validate_devices_config({
        "vision": {"type": "cv_process", "camera": "cam", "analysis": f"{analysis.__name__}:analyze"},
        "cam": {"type": "simulated_camera"},
    })
    assert issues == []
    by_name = {e.name: e for e in entries}
    with pytest.raises(RuntimeError, match="depends on 'cam', which could not be created"):
        create_device(by_name["vision"])
    cam = create_device(by_name["cam"])
    vision = create_device(by_name["vision"], {"cam": cam})
    assert vision.api is cam
    assert vision.info["camera"] == "cam"


# --- Device links (controller) ------------------------------------------------

def _controller(tmp_path, analysis, extra=""):
    from core.ApplicationController import ApplicationController

    (tmp_path / "config.yaml").write_text(f"""
devices:
  vision:
    type: "cv_process"
    camera: "cam"
    analysis: "{analysis.__name__}:analyze"
    autostart: true
    rate_hz: 50
  cam:
    type: "simulated_camera"
    width: 320
    height: 240
    fps: 100
{extra}""", encoding="utf-8")
    return ApplicationController(config_path=str(tmp_path / "config.yaml"))


def test_controller_brings_the_camera_up_first_and_down_last(tmp_path, analysis, monkeypatch):
    from core.ControllerState import ControllerState

    ctrl = _controller(tmp_path, analysis)
    try:
        assert ctrl.config_issues == []
        assert list(ctrl.devices) == ["vision", "cam"]   # config order, for the UI
        vision, cam = ctrl.devices["vision"], ctrl.devices["cam"]
        calls = []
        for device in (vision, cam):
            for method in ("initialize", "shutdown"):
                def spy(original=getattr(device, method), name=f"{device.device_id}.{method}"):
                    calls.append((name, vision.running))
                    return original()
                monkeypatch.setattr(device, method, spy)

        ctrl.initialize()
        assert ctrl.get_state() == ControllerState.READY
        assert [name for name, _ in calls] == ["vision.shutdown", "cam.initialize", "vision.initialize"]
        wait_for(lambda: vision.last_result() is not None)   # autostart

        calls.clear()
        ctrl.initialize()   # again: the analysis stops before the camera reconnects
        assert calls == [("vision.shutdown", True), ("cam.initialize", False), ("vision.initialize", False)]
        assert vision.running

        calls.clear()
        ctrl.shutdown()
        assert [name for name, _ in calls] == ["vision.shutdown", "cam.shutdown"]
        assert not vision.running
    finally:
        ctrl.close()


def test_a_bad_analysis_path_is_a_config_error(tmp_path, analysis):
    ctrl = _controller(tmp_path, analysis, extra="""  vision2:
    type: "cv_process"
    camera: "cam"
    analysis: "no_such_module_xyz:analyze"
""")
    try:
        errors = _errors(ctrl.config_issues)
        assert "could not be created: ModuleNotFoundError" in errors["vision2"]
        assert "vision2" not in ctrl.devices and "vision" in ctrl.devices
        with pytest.raises(RuntimeError, match="Fix the devices"):
            ctrl.initialize()
    finally:
        ctrl.close()


# --- The CV device ----------------------------------------------------------------

def test_camera_must_capture_frames(analysis):
    with pytest.raises(TypeError, match="capture_frame_8bit"):
        CVProcessDevice(camera=SimulatedDevice(name="not_a_camera"), analysis=f"{analysis.__name__}:analyze")
    cam = SimulatedCamera(name="cam")
    with pytest.raises(ValueError, match="module:function"):
        CVProcessDevice(camera=cam, analysis="no_colon")
    with pytest.raises(AttributeError):
        CVProcessDevice(camera=cam, analysis=f"{analysis.__name__}:missing")


def test_analysis_is_also_found_in_the_workspace_package(tmp_path, monkeypatch):
    """The Docker image's layout: --workspace /webapp, the analysis in app_logic/."""
    import devices.CVProcessDevice as cv_module

    package = f"ws_pkg_{uuid.uuid4().hex[:8]}"   # stands in for app_logic, already imported here
    (tmp_path / package).mkdir()
    (tmp_path / package / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / package / "blob_like.py").write_text(ANALYSIS_MODULE, encoding="utf-8")
    (tmp_path / package / "needs_sdk.py").write_text("import not_installed_sdk_xyz\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(cv_module, "WORKSPACE_PACKAGE", package)
    try:
        assert cv_module.load_analysis("blob_like:analyze").__module__ == f"{package}.blob_like"
        # A missing import inside the module is reported as itself, not as "no module".
        with pytest.raises(ModuleNotFoundError, match="not_installed_sdk_xyz"):
            cv_module.load_analysis("needs_sdk:analyze")
        with pytest.raises(ModuleNotFoundError, match=f"'nowhere' or '{package}.nowhere'"):
            cv_module.load_analysis("nowhere:analyze")
    finally:
        for name in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
            sys.modules.pop(name, None)


def test_continuous_analysis_publishes_raw_processed_and_result(make_vision):
    vision = make_vision(analysis_params={"scale": 2})
    with pytest.raises(RuntimeError, match="not initialized"):
        vision.start()
    vision.initialize()
    assert vision.ready and not vision.running   # autostart is off
    assert vision.last_raw_image() is None and vision.last_result() is None

    vision.start()
    vision.start()   # idempotent
    result = vision.wait_for_result(timeout=5)
    assert result["ok"] is True and result["data"]["scale"] == 2
    raw, processed = vision.last_raw_image(), vision.last_processed_image()
    assert raw.shape == (240, 320, 3) and processed.shape == raw.shape
    assert not raw.flags.writeable and not processed.flags.writeable
    with pytest.raises(ValueError):
        raw[0, 0] = 0
    assert vision.get_image("raw") is not None and vision.images() == ["raw", "processed"]

    telemetry = vision.telemetry()
    assert telemetry["state"] == "running" and telemetry["frames"] >= 1
    assert telemetry["mean"] == pytest.approx(result["data"]["mean"], abs=200)   # data values are shown
    assert [p["name"] for p in vision.io_points()] == ["analysis_running", "last_ok"]

    vision.stop()
    assert not vision.running and vision.telemetry()["state"] == "stopped"
    frames = vision.telemetry()["frames"]
    time.sleep(0.1)
    assert vision.telemetry()["frames"] == frames


def test_io_switch_starts_and_stops_the_analysis(make_vision):
    vision = make_vision()
    vision.initialize()
    vision.write_io("analysis_running", True)
    wait_for(lambda: vision.last_result() is not None)
    assert vision.io_points()[0]["value"] is True and vision.io_points()[1]["value"] is True
    vision.write_io("analysis_running", False)
    assert not vision.running
    with pytest.raises(ValueError):
        vision.write_io("analysis_running", "yes")
    with pytest.raises(ValueError):
        vision.write_io("last_ok", True)


def test_wait_for_result_and_analyze_once(make_vision):
    vision = make_vision()
    vision.initialize()

    # Stopped: analyze_once grabs and analyses a frame in this thread.
    first = vision.analyze_once()
    assert first["seq"] == 1 and vision.last_result()["seq"] == 1
    with pytest.raises(TimeoutError, match="no new result"):
        vision.wait_for_result(timeout=0.1)

    # Running: both return the next fresh result.
    vision.start()
    newer = vision.wait_for_result(timeout=5)
    assert newer["seq"] > first["seq"]
    assert vision.analyze_once(timeout=5)["seq"] > newer["seq"]
    assert vision.wait_for_result(after_seq=0, timeout=0.1)["seq"] >= newer["seq"]   # already there


def test_analysis_may_take_the_frame_alone(make_vision):
    vision = make_vision(fn="frame_only")
    vision.initialize()
    result = vision.analyze_once()
    assert result["data"] == {"shape": [240, 320, 3]} and result["ok"] is None
    # No image returned: the processed image is the raw frame.
    assert vision.last_processed_image() is vision.last_raw_image()


def test_normalize_result_accepts_every_documented_shape():
    image = np.zeros((4, 4), np.uint8)
    assert normalize_result(None) == AnalysisResult()
    assert normalize_result(image).image is image
    assert normalize_result({"a": 1}).data == {"a": 1}
    tupled = normalize_result((image, {"b": 2}))
    assert tupled.image is image and tupled.data == {"b": 2}
    assert normalize_result(AnalysisResult(ok=1)).ok is True
    for bad in (42, (image, {}, 3), np.zeros(4), AnalysisResult(data=[1])):
        with pytest.raises(TypeError):
            normalize_result(bad)


def test_the_frame_is_read_only(make_vision):
    vision = make_vision(fn="draw_in_place")
    vision.initialize()
    with pytest.raises(RuntimeError, match="read-only"):
        vision.analyze_once()
    assert vision.telemetry()["errors"] == 1
    assert vision.last_raw_image() is not None and vision.last_result() is None


def test_analysis_errors_are_counted_unless_they_fault(make_vision, analysis):
    vision = make_vision()
    vision.initialize()
    good = vision.analyze_once()
    processed = vision.last_processed_image()

    analysis.MODE["value"] = "raise"
    with pytest.raises(RuntimeError, match="boom"):
        vision.analyze_once()
    assert not vision.faulted and vision.ready
    assert vision.telemetry()["errors"] == 1 and "boom" in vision.telemetry()["last_error"]
    assert vision.last_result()["seq"] == good["seq"]           # the last good result stays
    assert vision.last_processed_image() is processed
    assert vision.last_raw_image() is not processed              # the raw image moved on


def test_fault_on_error_faults_until_cleared(make_vision, analysis):
    vision = make_vision(fault_on_error=True)
    vision.initialize()
    analysis.MODE["value"] = "raise"
    with pytest.raises(RuntimeError):
        vision.analyze_once()
    assert vision.faulted and not vision.ready
    analysis.MODE["value"] = "ok"
    vision.clear_fault()
    assert not vision.faulted and vision.analyze_once()["ok"] is True


def test_abort_wakes_a_waiter_and_the_analysis_keeps_running(make_vision, camera):
    vision = make_vision()
    vision.initialize()
    vision.start()
    vision.wait_for_result(timeout=5)
    camera._faulted = True   # no new results while the camera is faulted
    wait_for(lambda: vision.telemetry()["state"] == "waiting for camera")

    outcome = {}

    def waiter():
        try:
            vision.wait_for_result(timeout=5)
        except Exception as e:
            outcome["error"] = e

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.1)
    vision.abort()
    thread.join(2)
    assert isinstance(outcome.get("error"), RuntimeError) and "aborted" in str(outcome["error"])
    assert vision.running

    camera._faulted = False   # cleared: the loop resumes on its own
    vision.wait_for_result(timeout=5)


def test_video_source_skips_frames_already_sent(make_vision):
    vision = make_vision()
    vision.initialize()
    grab = vision.video_source("processed")
    assert grab() is None                  # nothing yet
    vision.analyze_once()
    assert grab() is vision.last_processed_image()
    assert grab() is None                  # unchanged
    vision.analyze_once()
    assert grab() is not None
    with pytest.raises(ValueError):
        vision.video_source("thermal")


# --- Simulated camera ---------------------------------------------------------------

def test_simulated_camera_paces_its_frames():
    cam = SimulatedCamera(width=200, height=150, fps=20, name="cam")
    with pytest.raises(RuntimeError, match="not connected"):
        cam.capture_frame_8bit()
    cam.initialize()
    started = time.monotonic()
    frames = [cam.capture_frame_8bit() for _ in range(3)]
    assert time.monotonic() - started >= 0.09    # 2 intervals of 50 ms
    assert frames[0].shape == (150, 200, 3) and frames[0].dtype == np.uint8
    assert cam.streaming and cam.info["model"] == "Test pattern"
    with pytest.raises(ConnectionError):
        SimulatedCamera(fail_to_connect=True, name="broken").initialize()


# --- Prototype analysis ---------------------------------------------------------------

@pytest.fixture
def blob_analysis(monkeypatch):
    pytest.importorskip("scipy")
    monkeypatch.syspath_prepend(EXAMPLE_DIR)
    yield importlib.import_module("blob_analysis")
    sys.modules.pop("blob_analysis", None)


def _read_only(frame):
    frame = frame.view()
    frame.flags.writeable = False
    return frame


def test_prototype_finds_the_test_pattern_ball(blob_analysis):
    pattern = TestPattern(800, 600)
    for t in (0.0, 1.0, 2.5):
        frame = _read_only(pattern.frame(t))
        result = blob_analysis.analyze(frame, {"threshold": 175, "min_area": 500})
        cx, cy = pattern.ball_center(t)
        assert result.ok is True and result.data["blobs"] == 1
        assert result.data["x_px"] == pytest.approx(cx, abs=1.5)
        assert result.data["y_px"] == pytest.approx(cy, abs=1.5)
        assert result.data["area_px"] == pytest.approx(np.pi * TestPattern.RADIUS ** 2, rel=0.03)
        assert result.data["elongation"] == pytest.approx(1.0, abs=0.05)
        assert result.image.shape == frame.shape and result.image.dtype == np.uint8


def test_prototype_reports_nothing_on_a_blank_frame(blob_analysis):
    result = blob_analysis.analyze(_read_only(np.zeros((120, 160), np.uint8)), {})
    assert result.ok is False
    assert result.data == {"found": False, "threshold": 175, "blobs": 0, "background": 0, "peak": 0}
    assert result.image.shape == (120, 160, 3)


def _laser_spot(center=(352.0, 228.0), radius=110.0):
    """A dim red disc (peak ~45 of 255) with diffraction rings, a dimmer top and a dust speck."""
    height, width = 480, 640
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    r = np.hypot(x - center[0], y - center[1])
    level = 0.6 + 0.4 * (y - (center[1] - radius)) / (2 * radius)   # the top is dimmer
    level *= 0.75 + 0.25 * np.cos(r / 4.0)                            # rings
    disc = np.clip((radius - r) / 3.0 + 0.5, 0.0, 1.0)                # soft edge, half at `radius`
    frame = np.full((height, width, 3), 7.0, np.float32)
    frame[..., 0] += 38.0 * level * disc
    frame[(x - center[0] - 80) ** 2 + (y - center[1]) ** 2 < 16] = 7.0   # dust on the disc
    frame += np.random.default_rng(0).normal(0.0, 1.5, frame.shape)
    return np.clip(frame, 0, 255).astype(np.uint8)


LASER_PARAMS = {"channel": "red", "blur_px": 4, "threshold": "20%", "fill_holes": True,
                "min_area": 2000, "stretch": True}


def test_prototype_finds_a_dim_ringed_laser_spot(blob_analysis):
    frame = _read_only(_laser_spot())
    assert blob_analysis.analyze(frame, {"threshold": 175}).ok is False   # far too dim for the default
    result = blob_analysis.analyze(frame, LASER_PARAMS)
    assert result.ok is True and result.data["blobs"] == 1
    assert result.data["background"] == 7 and 30 < result.data["peak"] < 50
    assert result.data["x_px"] == pytest.approx(352.0, abs=1.5)
    assert result.data["y_px"] == pytest.approx(228.0, abs=1.5)
    assert result.data["diameter_px"] == pytest.approx(220.0, rel=0.03)
    assert result.data["elongation"] == pytest.approx(1.0, abs=0.05)
    assert result.image.shape == frame.shape and result.image.max() == 255   # stretched for display


def test_prototype_adaptive_threshold_needs_contrast(blob_analysis):
    noise = np.random.default_rng(1).normal(7.0, 1.5, (240, 320, 3))
    result = blob_analysis.analyze(_read_only(np.clip(noise, 0, 255).astype(np.uint8)), LASER_PARAMS)
    assert result.ok is False and result.data["blobs"] == 0
    with pytest.raises(ValueError, match="channel"):
        blob_analysis.analyze(_read_only(_laser_spot()), {"channel": "infrared"})


def test_prototype_measures_orientation_and_auto_threshold(blob_analysis):
    frame = np.full((200, 300), 40, np.uint8)
    frame[90:110, 50:250] = 220                  # a horizontal bar
    result = blob_analysis.analyze(_read_only(frame), {"threshold": "auto", "min_area": 100})
    assert 40 < result.data["threshold"] <= 220
    assert result.data["angle_deg"] == pytest.approx(0.0, abs=1.0)
    assert result.data["elongation"] > 5
    assert result.data["bbox_px"] == [50, 90, 249, 109]
    dark = blob_analysis.analyze(_read_only(255 - frame), {"threshold": 100, "invert": True, "min_area": 100})
    assert dark.data["x_px"] == pytest.approx(149.5)
    halfway = blob_analysis.analyze(_read_only(255 - frame), {"threshold": "50%", "invert": True, "min_area": 100})
    assert halfway.data["threshold"] == pytest.approx(125.0) and halfway.data["bbox_px"] == [50, 90, 249, 109]


# --- Image route ------------------------------------------------------------------------

def test_image_route_serves_a_devices_cached_images(flask_app, make_vision):
    controller = flask_app.APPLICATION
    vision = make_vision()
    vision.initialize()
    controller.devices["vision_test"] = vision
    try:
        client = flask_app.app.test_client()
        live = client.get("/api/devices/vision_test/live").get_json()
        assert [i["name"] for i in live["images"]] == ["raw", "processed"]
        assert live["images"][0]["url"] == "/api/devices/vision_test/image/raw.jpg"

        assert client.get("/api/devices/vision_test/image/raw.jpg").status_code == 503
        vision.analyze_once()
        response = client.get("/api/devices/vision_test/image/processed.jpg?w=100")
        assert response.status_code == 200 and response.mimetype == "image/jpeg"
        assert Image.open(io.BytesIO(response.data)).size == (80, 60)   # 320 px reduced by 4

        assert client.get("/api/devices/vision_test/image/thermal.jpg").status_code == 404
        assert client.get("/api/devices/nope/image/raw.jpg").status_code == 404
    finally:
        controller.devices.pop("vision_test", None)


# --- Camera features (simulated camera) ---------------------------------------------------

def _mean(cam):
    return float(cam.capture_frame_8bit().mean())


def test_simulated_exposure_and_gain_scale_the_brightness():
    cam = SimulatedCamera(width=320, height=240, fps=1000, name="cam")
    cam.initialize()
    nominal = _mean(cam)
    cam.set_feature("ExposureTime", 5000)
    assert cam.get_feature("ExposureTime") == 5000.0
    assert _mean(cam) == pytest.approx(nominal / 2, rel=0.1)
    cam.set_feature("Gain", 6.0206)                        # ×2: back to nominal
    assert _mean(cam) == pytest.approx(nominal, rel=0.1)

    described = cam.describe_feature("ExposureTime")
    assert (described["type"], described["min"], described["max"], described["unit"]) == ("IFloat", 100.0, 1e6, "us")
    with pytest.raises(KeyError, match="no feature 'Exposure'"):
        cam.set_feature("Exposure", 1000)
    with pytest.raises(ValueError, match="outside 100-1000000 us"):
        cam.set_feature("ExposureTime", 5)
    with pytest.raises(ValueError, match="cannot take 'long'"):
        cam.set_feature("ExposureTime", "long")
    with pytest.raises(ValueError, match="one of RGB8, Mono8"):
        cam.set_feature("PixelFormat", "BayerRG8")
    assert not cam.faulted and cam.ready


def test_simulated_pixel_format_is_locked_while_streaming():
    cam = SimulatedCamera(width=320, height=240, fps=1000, name="cam")
    cam.initialize()
    assert cam.capture_frame_8bit().shape == (240, 320, 3) and cam._stream_starts == 1
    assert cam.describe_feature("PixelFormat")["writable"] is False
    cam.set_feature("PixelFormat", "Mono8")
    assert cam._stream_starts == 2 and cam.streaming      # stopped, written, restarted
    assert cam.capture_frame_8bit().shape == (240, 320)
    cam.set_feature("Gain", 3.0)                           # not locked: no restart
    assert cam._stream_starts == 2


def test_simulated_config_features_apply_at_initialize():
    cam = SimulatedCamera(width=320, height=240, name="cam",
                          features={"PixelFormat": "Mono8", "ExposureTime": 5000})
    cam.initialize()
    assert cam.get_feature("PixelFormat") == "Mono8" and cam.get_feature("ExposureTime") == 5000.0
    assert cam.info["features"] == "PixelFormat=Mono8, ExposureTime=5000"

    broken = SimulatedCamera(name="broken", features={"Gain": 99})
    with pytest.raises(RuntimeError, match="feature Gain=99: .*outside 0-24 dB"):
        broken.initialize()
    assert not broken.connected


def test_features_must_be_a_mapping():
    _, issues = validate_devices_config({
        "sim": {"type": "simulated_camera", "features": ["ExposureTime"]},
        "gige": {"type": "gige_camera", "cti_path": "x.cti", "features": "ExposureTime=1"},
    })
    errors = _errors(issues)
    assert "'features': expected dict" in errors["sim"] and "'features': expected dict" in errors["gige"]


def test_exposure_change_reaches_the_analysis(camera, blob_analysis):
    vision = CVProcessDevice(camera=camera, analysis="blob_analysis:analyze",
                             analysis_params={"threshold": 175, "min_area": 500}, name="vision")
    try:
        vision.initialize()
        assert vision.analyze_once()["data"]["found"] is True
        camera.set_feature("ExposureTime", 4000)           # the ball drops to ~75, under 175
        assert vision.analyze_once()["data"]["found"] is False
        camera.set_feature("ExposureTime", 10000)
        assert vision.analyze_once()["data"]["found"] is True
    finally:
        vision.shutdown()


# --- Trigger acquisition -----------------------------------------------------------------

def test_simulated_hardware_trigger_waits_for_the_next_pulse():
    camera = SimulatedCamera(width=320, height=240, fps=2, trigger="hardware", name="cam")
    camera.initialize()
    camera.capture_frame_8bit(timeout_sec=1.0)            # the first pulse is due at once
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="no trigger within 0.1 s"):
        camera.capture_frame_8bit(timeout_sec=0.1)        # the next one is 0.5 s away
    assert 0.08 <= time.monotonic() - started < 0.4
    assert not camera.faulted and camera.telemetry()["trigger"] == "hardware"
    camera.capture_frame_8bit(timeout_sec=1.0)            # waits for it within a longer timeout


def test_vision_analyses_each_hardware_pulse_without_errors(analysis):
    # A pulse every 0.67 s: longer than the loop's 0.5 s wait, so it shows it is waiting.
    camera = SimulatedCamera(width=320, height=240, fps=1.5, trigger="hardware", name="cam")
    camera.initialize()
    vision = CVProcessDevice(camera=camera, analysis=f"{analysis.__name__}:analyze", rate_hz=50, name="vision")
    try:
        vision.initialize()
        vision.start()
        first = vision.wait_for_result(timeout=2)
        wait_for(lambda: vision.telemetry()["state"] == "waiting for trigger", timeout=2)
        second = vision.wait_for_result(timeout=2)
        assert second["seq"] == first["seq"] + 1                    # one result per pulse
        assert 0.5 < second["time"] - first["time"] < 0.9           # at the pulses' pace, not rate_hz
        telemetry = vision.telemetry()
        assert telemetry["capture_errors"] == 0 and telemetry["trigger"] == "hardware"
        assert not vision.faulted and not camera.faulted

        vision.stop()
        with pytest.raises(TimeoutError):                 # stopped: analyze_once waits for one pulse
            vision.analyze_once(timeout=0.05)
        assert vision.analyze_once(timeout=1.0)["ok"] is True
    finally:
        vision.shutdown()
        camera.shutdown()


class _BufferingCamera:
    """A hardware-triggered camera whose pulses' frames wait in its buffers until read, like a GigE one."""

    device_id, connected, faulted, trigger = "cam", True, False, "hardware"

    def __init__(self):
        self.queued = [np.full((4, 4), level, np.uint8) for level in (10, 20, 30)]   # pulses long gone

    def discard_queued(self):
        dropped, self.queued = len(self.queued), []
        return dropped

    def capture_frame_8bit(self, timeout_sec=2.0):
        return self.queued.pop(0) if self.queued else np.full((4, 4), 200, np.uint8)   # the next pulse


def test_analyze_once_skips_frames_buffered_while_stopped(analysis):
    camera = _BufferingCamera()
    vision = CVProcessDevice(camera=camera, analysis=f"{analysis.__name__}:analyze", name="vision")
    vision.initialize()                                    # not started: nobody read the pulses
    try:
        assert vision.analyze_once(timeout=1.0)["data"]["mean"] == 200.0   # the next pulse, not the oldest
        assert camera.queued == []
    finally:
        vision.shutdown()


def test_trigger_keys_are_validated():
    entries, issues = validate_devices_config({
        "cam": {"type": "simulated_camera", "trigger": "software"},
        "bad": {"type": "simulated_camera", "trigger": "hardware", "trigger_activation": "Sideways"},
    })
    assert [e.name for e in entries] == ["cam"] and entries[0].kwargs["trigger"] == "software"
    assert "'trigger_activation' must be one of" in _errors(issues)["bad"]


def test_the_example_triggers_the_camera_at_the_vision_rate(blob_analysis):
    import yaml

    with open(os.path.join(EXAMPLE_DIR, "config.yaml"), encoding="utf-8") as f:
        config = yaml.safe_load(f)
    entries, issues = validate_devices_config(config["devices"], base_dir=EXAMPLE_DIR)
    assert issues == []
    by_name = {e.name: e for e in entries}
    assert by_name["camera"].kwargs["trigger"] == "software"
    rate_hz = by_name["vision"].kwargs["rate_hz"]
    assert by_name["camera"].kwargs["fps"] > rate_hz          # the sensor keeps up: rate_hz sets the pace
    assert by_name["vision"].kwargs["autostart"] is True      # triggers and analyses from INITIALIZE on
    camera = create_device(by_name["camera"])
    vision = create_device(by_name["vision"], {"camera": camera})
    try:
        camera.initialize()
        vision.initialize()
        first = vision.wait_for_result(timeout=3)
        for _ in range(3):
            last = vision.wait_for_result(timeout=3)
        assert last["seq"] == first["seq"] + 3                # one result per trigger
        telemetry = vision.telemetry()
        assert telemetry["trigger"] == "software"
        assert telemetry["analysis_fps"] <= rate_hz + 0.5     # at rate_hz, not the sensor's fps
        assert telemetry["errors"] == 0 and telemetry["capture_errors"] == 0
    finally:
        vision.shutdown()
        camera.shutdown()
