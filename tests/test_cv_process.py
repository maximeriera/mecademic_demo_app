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
    assert result.ok is False and result.data == {"found": False, "threshold": 175, "blobs": 0}
    assert result.image.shape == (120, 160, 3)


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
