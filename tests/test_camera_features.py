"""Camera features: features_file, Apply / Save / Discard / Forget, the settings tree and its routes."""

import os

import pytest
import yaml

from devices.SimulatedCamera import SimulatedCamera
from devices.registry import validate_devices_config


def _errors(issues):
    return {i["device"]: i["message"] for i in issues if i["level"] == "error"}


@pytest.fixture
def make_camera():
    cameras = []

    def make(**kwargs):
        camera = SimulatedCamera(width=320, height=240, fps=1000, name="cam", **kwargs)
        cameras.append(camera)
        return camera

    yield make
    for camera in cameras:
        camera.shutdown()


def _features(camera, visibility="Beginner"):
    return {f["name"]: f for group in camera.settings(visibility)["groups"] for f in group["features"]}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# --- Registry -----------------------------------------------------------------------------

def test_features_file_is_resolved_against_the_config_folder(tmp_path):
    entries, issues = validate_devices_config({
        "cam": {"type": "simulated_camera", "features_file": "camera_features.yaml"},
        "abs": {"type": "simulated_camera", "features_file": str(tmp_path / "elsewhere.yaml")},
    }, base_dir=str(tmp_path))
    assert issues == []
    by_name = {e.name: e for e in entries}
    assert by_name["cam"].kwargs["features_file"] == str(tmp_path / "camera_features.yaml")
    assert by_name["abs"].kwargs["features_file"] == str(tmp_path / "elsewhere.yaml")
    # Without a base folder the path is kept as written.
    entries, _ = validate_devices_config({"cam": {"type": "simulated_camera", "features_file": "f.yaml"}})
    assert entries[0].kwargs["features_file"] == "f.yaml"


def test_features_and_features_file_are_exclusive():
    entries, issues = validate_devices_config({
        "cam": {"type": "simulated_camera", "features": {"Gain": 1}, "features_file": "f.yaml"},
        "gige": {"type": "gige_camera", "cti_path": "x.cti", "features_file": "  "},
    })
    assert entries == []
    errors = _errors(issues)
    assert "use either 'features' or 'features_file', not both" in errors["cam"]
    assert "expected a file path" in errors["gige"]


# --- The features file -----------------------------------------------------------------------

def test_a_missing_file_means_no_features(make_camera, tmp_path):
    camera = make_camera(features_file=str(tmp_path / "camera_features.yaml"))
    camera.initialize()
    settings = camera.settings()
    assert settings["source"] == "file" and settings["can_save"] and settings["saved_count"] == 0
    assert not (tmp_path / "camera_features.yaml").exists()       # created by the first Save
    assert camera.info["features"] == "camera_features.yaml, 0 saved"


def test_apply_marks_unsaved_until_the_value_comes_back(make_camera, tmp_path):
    camera = make_camera(features_file=str(tmp_path / "f.yaml"))
    camera.initialize()
    assert camera.write_setting("ExposureTime", 5000)["unsaved"] == 1
    assert _features(camera)["ExposureTime"]["pending"] is True
    assert camera.write_setting("ExposureTime", 10000)["unsaved"] == 0   # back to where it was
    assert _features(camera)["ExposureTime"]["pending"] is False


def test_save_keeps_the_order_appends_new_ones_and_drops_forgotten(make_camera, tmp_path):
    path = tmp_path / "f.yaml"
    path.write_text("Gain: 3.0\nExposureTime: 8000.0\n", encoding="utf-8")
    camera = make_camera(features_file=str(path))
    camera.initialize()
    assert camera.get_feature("Gain") == 3.0 and camera.get_feature("ExposureTime") == 8000.0

    camera.write_setting("PixelFormat", "Mono8")
    camera.write_setting("ExposureTime", 5000)
    camera.forget_setting("Gain")
    features = _features(camera)
    assert features["Gain"]["forget"] and features["ExposureTime"]["saved"] == 8000.0
    assert camera.save_settings()["saved"] == 3

    text = _read(path)
    assert text.startswith("# Features of `cam`, written by the app")
    assert list(yaml.safe_load(text).items()) == [("ExposureTime", 5000.0), ("PixelFormat", "Mono8")]
    assert camera.settings()["unsaved"] == 0
    assert camera.get_feature("Gain") == 3.0                            # forgetting leaves the camera as is

    fresh = make_camera(features_file=str(path))                         # what the next INITIALIZE does
    fresh.initialize()
    assert fresh.get_feature("ExposureTime") == 5000.0 and fresh.get_feature("PixelFormat") == "Mono8"


def test_saved_strings_survive_yaml(make_camera, tmp_path):
    camera = make_camera(features_file=str(tmp_path / "f.yaml"))
    camera._write_features_file(str(tmp_path / "f.yaml"), {"ExposureAuto": "Off", "Reverse": "yes"})
    # Unquoted, YAML would read these back as booleans.
    assert yaml.safe_load(_read(tmp_path / "f.yaml")) == {"ExposureAuto": "Off", "Reverse": "yes"}


def test_discard_restores_saved_and_previous_values(make_camera, tmp_path):
    path = tmp_path / "f.yaml"
    path.write_text("ExposureTime: 8000.0\n", encoding="utf-8")
    camera = make_camera(features_file=str(path))
    camera.initialize()
    camera.write_setting("ExposureTime", 5000)
    camera.write_setting("Gain", 6.0)
    assert camera.discard_settings() == {"discarded": 2, "failed": []}
    assert camera.get_feature("ExposureTime") == 8000.0   # its saved value
    assert camera.get_feature("Gain") == 0.0              # the value it had before
    assert camera.settings()["unsaved"] == 0
    assert "Gain" not in yaml.safe_load(_read(path))


def test_save_is_refused_for_inline_features_and_after_a_hand_edit(make_camera, tmp_path):
    inline = make_camera(features={"Gain": 3.0})
    inline.initialize()
    inline.write_setting("Gain", 6.0)                      # Apply still works
    assert inline.settings()["can_save"] is False
    with pytest.raises(ValueError, match="set features_file"):
        inline.save_settings()
    with pytest.raises(ValueError, match="set features_file"):
        inline.forget_setting("Gain")

    path = tmp_path / "f.yaml"
    path.write_text("Gain: 3.0\n", encoding="utf-8")
    camera = make_camera(features_file=str(path))
    camera.initialize()
    camera.write_setting("Gain", 6.0)
    path.write_text("Gain: 12.0\n", encoding="utf-8")       # edited by hand meanwhile
    stamp = os.path.getmtime(path) + 5
    os.utime(path, (stamp, stamp))
    with pytest.raises(ValueError, match="changed on disk"):
        camera.save_settings()
    assert yaml.safe_load(_read(path)) == {"Gain": 12.0}   # never overwritten
    camera.initialize()                                     # INITIALIZE reads the hand edit
    assert camera.get_feature("Gain") == 12.0 and camera.settings()["unsaved"] == 0


def test_a_bad_file_fails_initialize_with_its_name(make_camera, tmp_path):
    path = tmp_path / "f.yaml"
    path.write_text("Gain: [1, 2\n", encoding="utf-8")
    camera = make_camera(features_file=str(path))
    with pytest.raises(RuntimeError, match="cannot read .*f.yaml"):
        camera.initialize()
    assert not camera.connected

    path.write_text("- Gain\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="must map feature names to values"):
        camera.initialize()

    path.write_text("Gain: 99\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="f.yaml: feature Gain=99: .*outside 0-24 dB"):
        camera.initialize()


def test_the_tree_groups_by_category_and_filters_by_visibility(make_camera):
    camera = make_camera()
    camera.initialize()
    beginner = camera.settings("Beginner")
    assert [g["name"] for g in beginner["groups"]] == \
        ["Acquisition Control", "Analog Control", "Image Format Control", "Device Control"]
    assert "DeviceTemperature" not in _features(camera, "Beginner")
    assert _features(camera, "Expert")["DeviceTemperature"]["visibility"] == "Expert"
    with pytest.raises(ValueError, match="visibility must be one of"):
        camera.settings("Wizard")

    width = _features(camera)["Width"]
    assert (width["value"], width["writable"], width["stream_locked"]) == (320, False, False)
    with pytest.raises(ValueError, match="read-only"):
        camera.write_setting("Width", 640)

    camera.capture_frame_8bit()                             # streaming now
    pixel_format = _features(camera)["PixelFormat"]
    assert pixel_format["writable"] is False and pixel_format["stream_locked"] is True
    camera.write_setting("PixelFormat", "Mono8")            # written with the stream stopped
    assert camera.streaming and camera.capture_frame_8bit().ndim == 2


# --- Routes ----------------------------------------------------------------------------------

@pytest.fixture
def feature_routes(flask_app, make_camera, tmp_path):
    """The app's controller with a simulated camera (features_file) added as `feat_cam`."""
    controller = flask_app.APPLICATION
    camera = make_camera(features_file=str(tmp_path / "f.yaml"))
    camera.device_id = "feat_cam"
    camera.initialize()
    controller.devices["feat_cam"] = camera
    try:
        yield flask_app.app.test_client(), camera, tmp_path / "f.yaml"
    finally:
        controller.devices.pop("feat_cam", None)


def test_settings_routes_apply_save_forget_and_discard(feature_routes, flask_app):
    client, camera, path = feature_routes
    info = {d["device_id"]: d for d in client.get("/api/info").get_json()}
    assert info["feat_cam"]["has_settings"] is True and info["cell"]["has_settings"] is False

    tree = client.get("/api/devices/feat_cam/settings?visibility=Expert").get_json()
    assert tree["success"] and tree["locked"] is None and tree["file"] == str(path)
    assert client.get("/api/devices/feat_cam/settings?visibility=Wizard").status_code == 400

    applied = client.post("/api/devices/feat_cam/settings", json={"name": "ExposureTime", "value": 5000})
    assert applied.status_code == 200 and applied.get_json()["unsaved"] == 1
    assert client.post("/api/devices/feat_cam/settings", json={"name": "ExposureTime", "value": -1}).status_code == 400
    assert client.post("/api/devices/feat_cam/settings", json={"name": "Nope", "value": 1}).status_code == 400
    assert client.post("/api/devices/feat_cam/settings", json={"name": "ExposureTime"}).status_code == 400

    saved = client.post("/api/devices/feat_cam/settings/save").get_json()
    assert saved["success"] and saved["saved"] == 1 and yaml.safe_load(_read(path)) == {"ExposureTime": 5000.0}

    assert client.post("/api/devices/feat_cam/settings/forget", json={"name": "ExposureTime"}).status_code == 200
    client.post("/api/devices/feat_cam/settings", json={"name": "Gain", "value": 6.0})
    discarded = client.post("/api/devices/feat_cam/settings/discard").get_json()
    assert discarded["success"] and discarded["discarded"] == 2 and camera.get_feature("Gain") == 0.0

    assert client.get("/api/devices/nope/settings").status_code == 404
    assert client.post("/api/devices/nope/settings/save").status_code == 404
    no_settings = client.post("/api/devices/cell/settings/save")
    assert no_settings.status_code == 409 and "has no settings" in no_settings.get_json()["message"]


def test_settings_writes_are_refused_while_a_task_runs(feature_routes, flask_app, monkeypatch):
    client, camera, _ = feature_routes
    monkeypatch.setattr(flask_app.APPLICATION, "_link_refusal_locked",
                        lambda device_id: "A task is running. Stop or abort it first.")
    tree = client.get("/api/devices/feat_cam/settings").get_json()
    assert tree["success"] and tree["locked"] == "A task is running. Stop or abort it first."   # reads still work
    refused = client.post("/api/devices/feat_cam/settings", json={"name": "Gain", "value": 3.0})
    assert refused.status_code == 409 and "task is running" in refused.get_json()["message"]
    assert client.post("/api/devices/feat_cam/settings/save").status_code == 409
    assert camera.get_feature("Gain") == 0.0
