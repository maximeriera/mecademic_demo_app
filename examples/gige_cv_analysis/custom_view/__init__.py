"""Custom view: the raw and processed feeds of the `vision` device, and its latest result.

The CV device already holds the frames, so the videos read its cache
(``vision.video_source(...)``) rather than the camera: the camera keeps a
single reader, and a frame is compressed only once the analysis produced a
new one. The page reads the result from the ``result`` channel and the
running state from the device's Live data (``/api/devices/vision/live``).
"""

#: Device id of the CV process in config.yaml.
VISION = "vision"
FPS = 10                   # a ceiling: the feeds change at the analysis rate_hz
JPEG_QUALITY = 75
MAX_WIDTH = 960            # two feeds side by side


def setup(hub, devices):
    """Declare both feeds and the result channel for this workspace."""
    vision = devices[VISION]
    for kind in ("raw", "processed"):
        hub.add_video(kind, vision.video_source(kind), fps=FPS, quality=JPEG_QUALITY,
                      max_width=MAX_WIDTH, device=VISION)

    sent = [None]

    def latest_result():
        result = vision.last_result()
        if result is None or result["seq"] == sent[0]:
            return None   # nothing new: record nothing
        sent[0] = result["seq"]
        return result

    hub.add_source("result", latest_result, rate_hz=10, history_s=60, device=VISION)
