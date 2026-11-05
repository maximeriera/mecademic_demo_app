"""Live camera feed: JPEG pipeline, the stream thread, the MJPEG route, GigE frame conversion."""

import io
import threading
import time

import numpy as np
import pytest
from PIL import Image

from core.DataHub import DataHub
from core.VideoStream import VideoStream, encode_jpeg, to_uint8

from conftest import wait_for


def _decode(jpeg):
    return Image.open(io.BytesIO(jpeg))


def test_encode_mono_colour_and_reduction():
    jpeg, w, h = encode_jpeg(np.full((960, 1600), 128, np.uint8), quality=70, max_width=1280)
    assert jpeg[:2] == b"\xff\xd8"                     # a JPEG
    assert (w, h) == (800, 480)                        # integer factor 2 to fit 1280
    assert _decode(jpeg).mode == "L"

    jpeg, w, h = encode_jpeg(np.zeros((100, 120, 3), np.uint8), max_width=None)
    assert (w, h) == (120, 100) and _decode(jpeg).mode == "RGB"


def test_to_uint8_uses_the_bit_depth():
    frame = np.array([[0, 4095]], np.uint16)
    assert to_uint8(frame, bit_depth=12).tolist() == [[0, 255]]
    assert to_uint8(frame).tolist() == [[0, 255]]      # guessed 12 bits from the peak
    assert to_uint8(np.array([[0.0, 1.0]])).tolist() == [[0, 255]]


def test_stream_grabs_only_while_watched_and_closes():
    grabs = []

    def grab():
        grabs.append(1)
        return np.zeros((40, 60), np.uint8)

    video = VideoStream("cam", grab, fps=50, quality=60)
    stop = threading.Event()
    thread = threading.Thread(target=video.run, args=(stop, lambda _d: True), daemon=True)
    thread.start()
    try:
        threading.Event().wait(0.2)
        assert grabs == []                             # nobody watching: camera untouched
        with video.viewer():
            frame = video.wait_frame(None, timeout=2)
            assert frame is not None and (frame.width, frame.height) == (60, 40)
            newer = video.wait_frame(frame.seq, timeout=2)
            assert newer.seq > frame.seq
        video.close()
        assert video.wait_frame(None, timeout=1) is None
    finally:
        stop.set()
        thread.join(2)


def test_stream_pauses_while_its_device_is_released():
    available = {"cam": False}
    video = VideoStream("cam", lambda: np.zeros((10, 10), np.uint8), fps=50, device="cam")
    stop = threading.Event()
    thread = threading.Thread(target=video.run, args=(stop, lambda d: available[d]), daemon=True)
    thread.start()
    try:
        with video.viewer():
            wait_for(lambda: video.paused)
            assert video.wait_frame(None, timeout=0.2) is None
            assert video.describe()["fps"] is None          # no stale rate while paused
            available["cam"] = True
            assert video.wait_frame(None, timeout=2) is not None
    finally:
        stop.set()
        thread.join(2)


def test_hub_serves_the_feed_as_mjpeg(flask_app):
    hub = DataHub()
    hub.add_video("cam", lambda: np.full((48, 64), 200, np.uint8), fps=30)
    hub.start()
    controller = flask_app.APPLICATION
    original_hub = controller.context.data
    controller.context.data = hub
    try:
        client = flask_app.app.test_client()
        assert client.get("/api/data").get_json()["videos"]["cam"]["url"] == "/api/data/video/cam.mjpg"

        response = client.get("/api/data/video/cam.mjpg", buffered=False)
        assert response.mimetype == "multipart/x-mixed-replace"
        chunk = next(iter(response.response))
        assert chunk.startswith(b"--frame\r\nContent-Type: image/jpeg")
        jpeg = chunk.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n")
        assert _decode(jpeg).size == (64, 48)
        response.close()

        snapshot = client.get("/api/data/video/cam.jpg")
        assert snapshot.status_code == 200 and snapshot.mimetype == "image/jpeg"
        assert client.get("/api/data/video/nope.mjpg").status_code == 404
    finally:
        controller.context.data = original_hub
        hub.clear()


class _FakeGigE:
    """Stands in for devices.api.gigE_vision.GigECamera."""

    def __init__(self, frame, pixel_format):
        self.frame = frame
        self.last_pixel_format = pixel_format
        self.started = 0

    def start_stream(self):
        self.started += 1

    def capture_frame(self, timeout_sec=2.0):
        return self.frame.copy()


def _gige(frame, pixel_format, monkeypatch):
    pytest.importorskip("harvesters")
    from devices.GigEVisionDevice import GigEVisionDevice

    device = GigEVisionDevice(cti_path="unused.cti", name="cam_test")
    device._api = _FakeGigE(frame, pixel_format)
    device._connected = True
    return device


def test_gige_mono12_is_scaled_by_its_bit_depth(monkeypatch):
    device = _gige(np.array([[0, 2048, 4095]], np.uint16), "Mono12", monkeypatch)
    out = device.capture_frame_8bit()
    assert out.dtype == np.uint8 and out.tolist() == [[0, 128, 255]]
    assert device._api.started == 1 and device.streaming   # restarted the stopped stream


def test_gige_bayer_becomes_half_resolution_rgb(monkeypatch):
    # One RGGB tile: R=200, G=100 and 60 (mean 80), B=20.
    tile = np.array([[200, 100], [60, 20]], np.uint8)
    device = _gige(np.tile(tile, (2, 3)), "BayerRG8", monkeypatch)
    out = device.capture_frame_8bit()
    assert out.shape == (2, 3, 3)
    assert out[0, 0].tolist() == [200, 80, 20]


def test_gige_bgr_is_swapped_to_rgb(monkeypatch):
    device = _gige(np.array([[[1, 2, 3]]], np.uint8), "BGR8", monkeypatch)
    assert device.capture_frame_8bit()[0, 0].tolist() == [3, 2, 1]


# --- GigE features ------------------------------------------------------------

def _fake_inode(name, display_name, tooltip, visibility, locked):
    """The ``node.node`` (INode) of a fake: name, display name, tooltip, visibility, pIsLocked."""
    from types import SimpleNamespace
    from genicam import genapi
    properties = {"pIsLocked": ("TLParamsLocked", "")} if locked else {}
    return SimpleNamespace(name=name, display_name=display_name or name, tooltip=tooltip,
                           visibility=getattr(genapi.EVisibility, visibility),
                           property_names=list(properties), get_property=lambda key: properties[key])


class _FakeNode:
    """A GenICam node: typed, with an access mode, a range or choices."""

    def __init__(self, name, kind, value, log, mode="RW", lo=None, hi=None, choices=None,
                 locked_while_streaming=False, display_name=None, tooltip="", visibility="Beginner"):
        self.name, self.kind, self._value, self.log, self.mode = name, kind, value, log, mode
        self.min, self.max = lo, hi
        if choices:
            self.symbolics = list(choices)
        self.locked_while_streaming = locked_while_streaming
        self.writes = []
        self.node = _fake_inode(name, display_name, tooltip, visibility, locked_while_streaming)

    def get_access_mode(self):
        from genicam import genapi
        return getattr(genapi.EAccessMode, self.mode)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        if self.mode not in ("RW", "WO"):
            raise RuntimeError("AccessException: node is not writable")
        if self.min is not None and not self.min <= value <= self.max:
            raise RuntimeError("OutOfRangeException")
        if hasattr(self, "symbolics") and value not in self.symbolics:
            raise RuntimeError("InvalidArgumentException")
        self.writes.append(value)
        self.log.append(self.name)
        self._value = value


class _FakeCategory:
    """A GenICam category: its children are nodes or other categories."""

    kind = "ICategory"
    locked_while_streaming = False

    def __init__(self, name, features, display_name=None, visibility="Beginner"):
        self.name, self.features = name, list(features)
        self.node = _fake_inode(name, display_name, "", visibility, False)


class _FakeNodeMap:
    """A node map: every node as an attribute, and get_node(name)."""

    def __init__(self, nodes):
        self.__dict__.update(nodes)

    def get_node(self, name):
        return self.__dict__.get(name)


class _FakeAcquirer:
    """ImageAcquirer stand-in: start/stop lock some nodes, like TLParamsLocked does."""

    def __init__(self, nodes, log):
        from types import SimpleNamespace
        self.nodes, self.log = nodes, log
        self.remote_device = SimpleNamespace(node_map=_FakeNodeMap(nodes))

    def _lock(self, mode):
        for node in self.nodes.values():
            if node.locked_while_streaming:
                node.mode = mode

    def start(self):
        self.log.append("start")
        self._lock("RO")

    def stop(self):
        self.log.append("stop")
        self._lock("RW")


@pytest.fixture
def gige_features(tmp_path, monkeypatch):
    """A GigEVisionDevice over a real GigECamera whose acquirer and node map are fakes."""
    pytest.importorskip("harvesters")
    from devices.GigEVisionDevice import GigEVisionDevice
    from devices.api import gigE_vision

    # Node classes are pybind types the fakes cannot be: read the kind from the fake.
    monkeypatch.setattr(gigE_vision, "_node_type", lambda node: node.kind)
    log = []   # node writes, stream starts/stops and disconnects, in order
    nodes = {node.name: node for node in (
        _FakeNode("ExposureAuto", "IEnumeration", "Continuous", log, choices=("Off", "Continuous"),
                  display_name="Exposure Auto"),
        _FakeNode("ExposureTime", "IFloat", 10000.0, log, lo=10.0, hi=1e6, display_name="Exposure Time",
                  tooltip="Exposure time in microseconds."),
        _FakeNode("Width", "IInteger", 1600, log, lo=16, hi=1600, locked_while_streaming=True),
        _FakeNode("PixelFormat", "IEnumeration", "BayerRG8", log, choices=("BayerRG8", "Mono8"),
                  locked_while_streaming=True),
        _FakeNode("OffsetX", "IInteger", 0, log, lo=0, hi=800, visibility="Expert"),
        _FakeNode("DeviceTemperature", "IFloat", 41.5, log, mode="RO", visibility="Expert"),
        _FakeNode("AcquisitionStart", "ICommand", None, log),
        _FakeNode("BinningUnavailable", "IInteger", 1, log, mode="NA"),
        _FakeNode("DeviceRegister", "IRegister", b"", log),
        _FakeNode("Secret", "IInteger", 7, log),
    )}
    nodes["Root"] = _FakeCategory("Root", [
        _FakeCategory("AcquisitionControl", [nodes[n] for n in ("ExposureAuto", "ExposureTime", "AcquisitionStart",
                                                                "BinningUnavailable")],
                      display_name="Acquisition Control"),
        _FakeCategory("ImageFormatControl", [nodes["Width"], nodes["PixelFormat"],
                                             _FakeCategory("Region", [nodes["OffsetX"]])],
                      display_name="Image Format Control"),
        _FakeCategory("DeviceControl", [nodes["DeviceTemperature"], nodes["DeviceRegister"]],
                      display_name="Device Control"),
        _FakeCategory("Hidden", [nodes["Secret"]], visibility="Invisible"),
    ])

    cti = tmp_path / "fake.cti"
    cti.write_bytes(b"")
    api = gigE_vision.GigECamera(cti_path=str(cti))
    api.ia = _FakeAcquirer(nodes, log)

    def connect():
        api._is_connected = True

    def disconnect():
        api._is_connected = False
        log.append("disconnect")

    monkeypatch.setattr(api, "connect", connect)
    monkeypatch.setattr(api, "disconnect", disconnect)

    def make(**kwargs):
        device = GigEVisionDevice(cti_path=str(cti), name="cam_features", **kwargs)
        device._api = api
        return device

    make.nodes, make.log = nodes, log
    return make


def test_gige_feature_values_are_converted_and_errors_never_fault(gige_features):
    camera = gige_features()
    camera.initialize()
    camera.set_feature("Width", 640.0)                    # a whole float, for an integer node
    camera.set_feature("ExposureTime", 5000)              # an int, for a float node
    assert gige_features.nodes["Width"].writes == [640] and isinstance(camera.get_feature("Width"), int)
    assert camera.get_feature("ExposureTime") == 5000.0

    with pytest.raises(KeyError, match="no feature 'ExposureTme'"):
        camera.set_feature("ExposureTme", 1.0)
    with pytest.raises(ValueError, match="cannot take 'fast'"):
        camera.set_feature("ExposureTime", "fast")
    with pytest.raises(ValueError, match="refused 1.0"):
        camera.set_feature("ExposureTime", 1.0)              # below the node's minimum
    with pytest.raises(ValueError, match="Width.*cannot take 12.5"):
        camera.set_feature("Width", 12.5)
    assert not camera.faulted and camera.ready

    described = camera.describe_feature("PixelFormat")
    assert described["type"] == "IEnumeration" and described["choices"] == ["BayerRG8", "Mono8"]
    assert camera.describe_feature("ExposureTime")["min"] == 10.0


def test_gige_locked_feature_is_written_with_the_stream_stopped(gige_features):
    camera = gige_features()
    camera.initialize()
    camera.start_stream()
    gige_features.log.clear()

    camera.set_feature("PixelFormat", "Mono8")
    assert gige_features.log == ["stop", "PixelFormat", "start"]
    assert camera.streaming and camera.get_feature("PixelFormat") == "Mono8"

    camera.set_feature("ExposureTime", 3000.0)            # writable while streaming: no restart
    assert gige_features.log[-1] == "ExposureTime" and gige_features.log.count("stop") == 1

    with pytest.raises(ValueError, match="read-only"):
        camera.set_feature("DeviceTemperature", 20.0)
    assert camera.streaming and not camera.faulted          # the stream came back anyway


def test_gige_config_features_apply_in_order_at_initialize(gige_features):
    camera = gige_features(features={"ExposureAuto": "Off", "ExposureTime": 20000, "PixelFormat": "Mono8"},
                           autostart_stream=True)
    camera.initialize()
    assert gige_features.log == ["ExposureAuto", "ExposureTime", "PixelFormat", "start"]
    assert camera.info["features"] == "ExposureAuto=Off, ExposureTime=20000, PixelFormat=Mono8"

    broken = gige_features(features={"ExposureTime": 20000, "Gian": 3})
    with pytest.raises(RuntimeError, match="feature Gian=3: .*no feature 'Gian'"):
        broken.initialize()
    assert gige_features.log[-1] == "disconnect" and not broken.connected


def test_gige_set_feature_waits_for_the_frame_being_fetched(gige_features):
    camera = gige_features()
    camera.initialize()
    fetching = threading.Event()

    def fetch():
        with camera._capture_lock:
            fetching.set()
            time.sleep(0.2)

    thread = threading.Thread(target=fetch)
    thread.start()
    fetching.wait(1)
    started = time.monotonic()
    camera.set_feature("ExposureTime", 4000.0)
    assert time.monotonic() - started >= 0.15
    thread.join(1)


def test_gige_feature_tree_groups_filters_and_describes(gige_features):
    camera = gige_features()
    camera.initialize()

    def names(tree):
        return {group["name"]: [f["name"] for f in group["features"]] for group in tree}

    # Commands, registers, unavailable and invisible nodes never show; Expert ones only from Expert.
    assert names(camera.feature_tree("Beginner")) == {
        "Acquisition Control": ["ExposureAuto", "ExposureTime"],
        "Image Format Control": ["Width", "PixelFormat"],
    }
    assert names(camera.feature_tree("Expert")) == {
        "Acquisition Control": ["ExposureAuto", "ExposureTime"],
        "Image Format Control": ["Width", "PixelFormat"],
        "Image Format Control / Region": ["OffsetX"],
        "Device Control": ["DeviceTemperature"],
    }
    with pytest.raises(ValueError):
        camera.feature_tree("Wizard")

    features = {f["name"]: f for g in camera.feature_tree("Expert") for f in g["features"]}
    exposure = features["ExposureTime"]
    assert (exposure["display_name"], exposure["tooltip"], exposure["visibility"]) == \
        ("Exposure Time", "Exposure time in microseconds.", "Beginner")
    assert exposure["writable"] and not exposure["stream_locked"]
    assert features["DeviceTemperature"]["writable"] is False and features["DeviceTemperature"]["visibility"] == "Expert"

    camera.start_stream()
    pixel_format = camera.describe_feature("PixelFormat")
    assert pixel_format["writable"] is False and pixel_format["stream_locked"] is True   # Apply restarts the stream


# --- GigE stream diagnostics ------------------------------------------------------

def test_gige_counts_near_black_frames(monkeypatch):
    device = _gige(np.zeros((32, 32), np.uint8), "Mono8", monkeypatch)
    device.capture_frame_8bit()
    assert device.telemetry()["dark_frames"] == 1 and device.telemetry()["frame_brightness"] == 0.0
    device._api.frame = np.full((32, 32), 120, np.uint8)
    device.capture_frame_8bit()
    telemetry = device.telemetry()
    assert telemetry["dark_frames"] == 1 and telemetry["frame_brightness"] == 120.0
    assert telemetry["pixel_format"] == "Mono8" and device.has_live_data


def test_gige_counts_the_incomplete_frames_harvesters_discards(gige_features):
    from devices.api.gigE_vision import _IncompleteFrameCounter

    camera = gige_features()
    camera.initialize()
    counter = _IncompleteFrameCounter(camera.api)
    counter.emit(context=None)
    counter.emit(context=None)
    assert camera.telemetry()["incomplete_frames"] == 2
