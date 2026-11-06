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

    def execute(self):
        self.log.append(f"{self.name}()")


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

    def try_fetch(self, timeout):
        return None

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
        _FakeNode("TriggerSelector", "IEnumeration", "FrameStart", log, choices=("FrameStart", "LineStart")),
        _FakeNode("TriggerMode", "IEnumeration", "Off", log, choices=("Off", "On")),
        _FakeNode("TriggerSource", "IEnumeration", "Software", log, choices=("Software", "Line0", "Line1")),
        _FakeNode("TriggerActivation", "IEnumeration", "RisingEdge", log,
                  choices=("RisingEdge", "FallingEdge", "AnyEdge")),
        _FakeNode("TriggerSoftware", "ICommand", None, log),
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
    # trigger: off puts the camera in free-run first (every trigger checked, FrameStart selected
    # back), then the features go in, in order.
    assert gige_features.log == ["TriggerSelector", "TriggerSelector", "TriggerMode",
                                 "ExposureAuto", "ExposureTime", "PixelFormat", "start"]
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

def test_gige_counts_frames_darker_than_the_ones_before(monkeypatch):
    device = _gige(np.full((32, 32), 120, np.uint8), "Mono8", monkeypatch)
    for _ in range(6):
        device.capture_frame_8bit()
    lit = device.telemetry()
    assert lit["dark_frames"] == 0 and (lit["frame_brightness"], lit["frame_peak"]) == (120.0, 120.0)
    device._api.frame = np.zeros((32, 32), np.uint8)       # the light went out for this exposure
    device.capture_frame_8bit()
    telemetry = device.telemetry()
    assert telemetry["dark_frames"] == 1 and telemetry["frame_peak"] == 0.0
    assert telemetry["pixel_format"] == "Mono8" and device.has_live_data


def test_gige_counts_the_incomplete_frames_harvesters_discards(gige_features):
    from devices.api.gigE_vision import _IncompleteFrameCounter

    camera = gige_features()
    camera.initialize()
    counter = _IncompleteFrameCounter(camera.api)
    counter.emit(context=None)
    counter.emit(context=None)
    assert camera.telemetry()["incomplete_frames"] == 2


# --- GigE trigger -----------------------------------------------------------------------

def test_gige_trigger_modes_are_set_before_the_features(gige_features):
    nodes = gige_features.nodes
    nodes["TriggerSource"]._value = "Line0"
    software = gige_features(trigger="software", features={"ExposureTime": 20000})
    software.initialize()
    # Every trigger checked (FrameStart selected back), then FrameStart set up, then the features.
    assert gige_features.log == ["TriggerSelector", "TriggerSelector", "TriggerSelector", "TriggerMode",
                                 "TriggerSource", "TriggerMode", "ExposureTime"]
    assert (nodes["TriggerMode"].value, nodes["TriggerSource"].value) == ("On", "Software")
    assert software.info["trigger"] == "software" and software.telemetry()["trigger"] == "software"

    gige_features.log.clear()
    hardware = gige_features(trigger="hardware", trigger_source="Line1", trigger_activation="FallingEdge")
    hardware.initialize()
    assert (nodes["TriggerSource"].value, nodes["TriggerActivation"].value, nodes["TriggerMode"].value) ==         ("Line1", "FallingEdge", "On")
    assert hardware.info["trigger"] == "hardware (Line1, FallingEdge)"

    free = gige_features()
    free.initialize()
    assert nodes["TriggerMode"].value == "Off" and free.trigger == "off"

    with pytest.raises(ValueError, match="trigger must be one of"):
        gige_features(trigger="sometimes")
    broken = gige_features(trigger="hardware", trigger_source="Line9")
    with pytest.raises(RuntimeError, match="trigger hardware: TriggerSource='Line9'"):
        broken.initialize()
    assert not broken.connected


def test_gige_software_trigger_fires_before_each_capture(gige_features, monkeypatch):
    camera = gige_features(trigger="software")
    camera.initialize()
    api = camera.api

    def fetch(timeout_sec):
        gige_features.log.append("fetch")
        api.last_pixel_format = "Mono8"
        return np.full((8, 8), 50, np.uint8)

    monkeypatch.setattr(api, "capture_frame", fetch)
    gige_features.log.clear()
    camera.capture_frame_8bit()
    camera.capture_frame_8bit()
    assert gige_features.log == ["start", "TriggerSoftware()", "fetch", "TriggerSoftware()", "fetch"]


def test_gige_no_pulse_is_not_a_fault_with_a_hardware_trigger(gige_features, monkeypatch):
    def no_frame(timeout_sec):
        raise TimeoutError(f"no frame within {timeout_sec:g} s")

    hardware = gige_features(trigger="hardware")
    hardware.initialize()
    monkeypatch.setattr(hardware.api, "capture_frame", no_frame)
    with pytest.raises(TimeoutError):
        hardware.capture_frame_8bit(timeout_sec=0.1)
    assert not hardware.faulted                     # just no pulse yet

    free = gige_features()
    free.initialize()
    with pytest.raises(TimeoutError):
        free.capture_frame_8bit(timeout_sec=0.1)
    assert free.faulted                             # a free-running camera should have answered


class _ParamsLock(_FakeNode):
    """TLParamsLocked: while 1, the camera locks *locked* (as a FLIR does TriggerSource)."""

    def __init__(self, log, locked, value=1):
        super().__init__("TLParamsLocked", "IInteger", value, log, lo=0, hi=1)
        self.locked = locked
        for node in locked:
            node.mode = "RO" if value else "RW"

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        _FakeNode.value.fset(self, value)
        for node in self.locked:
            node.mode = "RO" if value else "RW"


def test_gige_initialize_stops_an_acquisition_an_earlier_session_left_running(gige_features):
    # A process killed while streaming: the camera still acquires, TriggerSource is locked,
    # and TriggerMode is not (a FLIR camera's "TriggerSource is read-only" at INITIALIZE).
    nodes, log = gige_features.nodes, gige_features.log
    nodes["TriggerSource"]._value = "Line0"               # the earlier session's hardware trigger
    camera = gige_features(trigger="software")
    node_map = camera.api.ia.remote_device.node_map
    node_map.TLParamsLocked = _ParamsLock(log, [nodes["TriggerSource"]])
    node_map.AcquisitionStop = _FakeNode("AcquisitionStop", "ICommand", None, log)
    camera.initialize()
    assert log.index("AcquisitionStop()") < log.index("TLParamsLocked") < log.index("TriggerSource")
    assert node_map.TLParamsLocked.value == 0 and nodes["TriggerSource"].value == "Software"
    assert camera.connected and not camera.faulted

    log.clear()
    camera.initialize()                                   # nothing left running now: nothing to stop
    assert "AcquisitionStop()" not in log and "TLParamsLocked" not in log


class _PerSelector(_FakeNode):
    """A feature holding one value per TriggerSelector entry, as a camera's TriggerMode does."""

    def __init__(self, base, selector, values):
        super().__init__(base.name, base.kind, base._value, base.log, choices=base.symbolics)
        self.selector, self.values = selector, dict(values)

    @property
    def value(self):
        return self.values.get(self.selector.value, self._value)

    @value.setter
    def value(self, value):
        _FakeNode.value.fset(self, value)
        self.values[self.selector.value] = value


def test_gige_trigger_switches_off_every_trigger_first(gige_features):
    # Another program left the LineStart trigger on: free-run or not, it goes off first.
    nodes, log = gige_features.nodes, gige_features.log
    camera = gige_features(trigger="software")
    node_map = camera.api.ia.remote_device.node_map
    node_map.TriggerMode = _PerSelector(nodes["TriggerMode"], nodes["TriggerSelector"], {"LineStart": "On"})
    camera.initialize()
    assert node_map.TriggerMode.values == {"LineStart": "Off", "FrameStart": "On"}
    assert nodes["TriggerSelector"].value == "FrameStart" and nodes["TriggerSource"].value == "Software"


def test_gige_trigger_source_already_set_is_not_written(gige_features):
    # Locked, but already what the trigger wants: nothing to write, nothing to fail.
    nodes = gige_features.nodes
    nodes["TriggerSource"].mode = "RO"
    camera = gige_features(trigger="software")                # the fake's TriggerSource is "Software"
    camera.initialize()
    assert nodes["TriggerSource"].writes == [] and nodes["TriggerMode"].value == "On"


def test_gige_trigger_error_shows_what_locks_the_feature(gige_features):
    from types import SimpleNamespace

    nodes, log = gige_features.nodes, gige_features.log
    nodes["TriggerSource"]._value = "Line0"
    nodes["TriggerSource"].mode = "RO"                    # locked by something this cannot release
    camera = gige_features(trigger="software")
    node_map = camera.api.ia.remote_device.node_map
    node_map.TLParamsLocked = _ParamsLock(log, [], value=0)
    # The camera describes the lock: TriggerSource's pIsLocked is a SwissKnife over ExposureMode.
    properties = {"pIsLocked": ("TriggerSourceLock", "")}
    nodes["TriggerSource"].node = SimpleNamespace(property_names=list(properties),
                                                  get_property=lambda key: properties[key])
    lock = _FakeNode("TriggerSourceLock", "IInteger", 1, log, mode="RO")
    knife = {"Formula": ("E = 1", ""), "pVariable": ("ExposureMode", "E")}
    lock.node = SimpleNamespace(property_names=list(knife), get_property=lambda key: knife[key])
    node_map.TriggerSourceLock = lock
    node_map.ExposureMode = _FakeNode("ExposureMode", "IInteger", 1, log)
    with pytest.raises(RuntimeError) as failed:
        camera.initialize()
    message = str(failed.value)
    assert "TriggerSource='Software': feature 'TriggerSource' is read-only." in message
    assert "TriggerMode=Off, TLParamsLocked=0" in message
    assert "TriggerSource: access RO, pIsLocked TriggerSourceLock=1 (= E = 1; E=ExposureMode=1)" in message


def test_gige_discard_queued_drops_the_buffered_frames(gige_features, monkeypatch):
    from types import SimpleNamespace

    camera = gige_features(trigger="hardware")
    camera.initialize()
    requeued = []
    waiting = [SimpleNamespace(queue=lambda i=i: requeued.append(i)) for i in range(3)]   # 3 old pulses
    monkeypatch.setattr(camera.api.ia, "try_fetch", lambda timeout: waiting.pop(0) if waiting else None)
    assert camera.discard_queued() == 0 and len(waiting) == 3     # stream stopped: nothing buffered
    camera.start_stream()
    assert camera.discard_queued() == 3 and requeued == [0, 1, 2]   # each buffer goes back to the stream
    assert camera.discard_queued() == 0 and not camera.faulted


def test_gige_fetch_timeout_becomes_a_timeout_error(gige_features, monkeypatch):
    from genicam.gentl import TimeoutException

    camera = gige_features()
    camera.initialize()
    camera.start_stream()

    def fetch(timeout):
        raise TimeoutException()

    monkeypatch.setattr(camera.api.ia, "fetch", fetch, raising=False)
    with pytest.raises(TimeoutError, match="no frame within 0.2 s"):
        camera.api.capture_frame(timeout_sec=0.2)
