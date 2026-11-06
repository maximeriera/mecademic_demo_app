"""Live camera feed: JPEG pipeline, the stream thread, the MJPEG route, GigE frame conversion."""

import io
import threading

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
