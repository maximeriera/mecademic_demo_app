"""Prototype analysis for a ``cv_process`` device: find the largest bright blob.

Copy this file to start your own analysis. The device calls
``analyze(frame, params)`` once per frame, on its own thread:

* ``frame``: 8-bit NumPy array, mono ``H×W`` or RGB ``H×W×3``. It is
  read-only, so copy it before drawing on it (``Image.fromarray`` does).
* ``params``: the ``analysis_params`` mapping from config.yaml.
* Return an ``AnalysisResult(image, data, ok)``:

  - ``image``: the annotated frame, shown as "processed" (None shows the raw frame);
  - ``data``: JSON-serialisable measurements, which task code reads with
    ``vision.last_result()["data"]`` and the Devices tab and custom view show;
  - ``ok``: a pass/fail verdict, or None.

Raising is fine: the device counts the error and keeps the last good result,
and with ``fault_on_error: true`` it faults the cell. Keep one call well under
``1 / rate_hz`` seconds.

Only NumPy, Pillow and SciPy are used, all installed with the app. OpenCV is
not; add ``opencv-python-headless`` to requirements.txt to use it here.

params
------
threshold   0-255, or "auto" for Otsu's threshold     default 175
min_area    smallest blob kept, in pixels             default 500
invert      find a dark blob on a light background    default false
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

from devices.CVProcessDevice import AnalysisResult

#: ITU-R BT.601 luma weights for R, G, B.
_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)
OVERLAY_RGB = (255, 196, 0)


def analyze(frame: np.ndarray, params: dict) -> AnalysisResult:
    """Locate the largest blob above the threshold: centre, area, bounding box, orientation."""
    gray = luminance(frame)
    threshold = params.get("threshold", 175)
    level = otsu_threshold(gray) if threshold == "auto" else int(threshold)
    mask = gray < level if params.get("invert", False) else gray >= level
    min_area = int(params.get("min_area", 500))

    data = {"found": False, "threshold": level, "blobs": 0}
    blob = None
    labels, count = ndimage.label(mask)
    if count:
        areas = np.bincount(labels.ravel())[1:]   # pixels per label; label 0 is the background
        kept = areas >= min_area
        data["blobs"] = int(kept.sum())
        if kept.any():
            blob = labels == int(np.argmax(np.where(kept, areas, 0))) + 1
            data.update(measure(blob))

    return AnalysisResult(image=draw_overlay(frame, blob, data, min_area), data=data, ok=data["found"])


def luminance(frame: np.ndarray) -> np.ndarray:
    """The frame as 8-bit grey levels."""
    if frame.ndim == 2:
        return frame
    return (frame[..., :3] @ _LUMA).astype(np.uint8)


def otsu_threshold(gray: np.ndarray) -> int:
    """The grey level that best separates dark from bright pixels (Otsu, 1979)."""
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    weight = np.cumsum(hist) / hist.sum()              # share of pixels at or below each level
    mean = np.cumsum(hist * np.arange(256)) / hist.sum()
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mean[-1] * weight - mean) ** 2 / (weight * (1.0 - weight))
    return int(np.argmax(np.nan_to_num(between))) + 1   # bright = above the best split


def measure(blob: np.ndarray) -> dict:
    """Centroid, area, bounding box and orientation of a boolean mask.

    ``angle_deg`` is the long axis, in degrees from the image x axis and
    clockwise on screen (image y points down). It means little when
    ``elongation`` (long/short axis) is close to 1, as for a disc.
    """
    ys, xs = np.nonzero(blob)
    x, y = float(xs.mean()), float(ys.mean())
    dx, dy = xs - x, ys - y
    mu20, mu02, mu11 = float((dx * dx).mean()), float((dy * dy).mean()), float((dx * dy).mean())
    spread = math.hypot((mu20 - mu02) / 2.0, mu11)
    major, minor = (mu20 + mu02) / 2.0 + spread, (mu20 + mu02) / 2.0 - spread
    return {
        "found": True,
        "x_px": round(x, 1),
        "y_px": round(y, 1),
        "angle_deg": round(math.degrees(0.5 * math.atan2(2.0 * mu11, mu20 - mu02)), 1),
        "elongation": round(math.sqrt(major / minor), 2) if minor > 0 else None,
        "area_px": int(xs.size),
        "bbox_px": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
    }


def draw_overlay(frame: np.ndarray, blob: np.ndarray | None, data: dict, min_area: int) -> np.ndarray:
    """The frame in colour with the blob tinted, its box, centre, long axis and a label."""
    rgb = frame if frame.ndim == 3 else np.repeat(frame[:, :, np.newaxis], 3, axis=2)
    out = np.array(rgb[..., :3], dtype=np.uint8)       # a copy: the frame is read-only
    if blob is not None:
        out[blob] = out[blob] // 2 + np.array(OVERLAY_RGB, dtype=np.uint8) // 2

    image = Image.fromarray(out)
    draw = ImageDraw.Draw(image)
    line = max(2, image.width // 400)
    if data["found"]:
        x0, y0, x1, y1 = data["bbox_px"]
        cx, cy = data["x_px"], data["y_px"]
        draw.rectangle((x0, y0, x1, y1), outline=OVERLAY_RGB, width=line)
        arm = max(12, image.width // 80)
        draw.line(((cx - arm, cy), (cx + arm, cy)), fill=OVERLAY_RGB, width=line)
        draw.line(((cx, cy - arm), (cx, cy + arm)), fill=OVERLAY_RGB, width=line)
        if data["elongation"] and data["elongation"] > 1.2:
            half = max(x1 - x0, y1 - y0) / 2.0
            a = math.radians(data["angle_deg"])
            draw.line(((cx - half * math.cos(a), cy - half * math.sin(a)),
                       (cx + half * math.cos(a), cy + half * math.sin(a))), fill=OVERLAY_RGB, width=line)
        label = f"x {cx:.0f}  y {cy:.0f}  area {data['area_px']} px"
    else:
        label = f"no blob of {min_area}+ px above {data['threshold']}"

    font = _font(max(14, image.width // 40))   # still legible once the feed is shrunk
    margin = max(8, image.width // 160)
    box = draw.textbbox((margin, margin), label, font=font)
    draw.rectangle((box[0] - 6, box[1] - 4, box[2] + 6, box[3] + 4), fill=(0, 0, 0))
    draw.text((margin, margin), label, fill=OVERLAY_RGB, font=font)
    return np.asarray(image)


@lru_cache(maxsize=8)
def _font(size: int):
    try:
        return ImageFont.load_default(size=size)   # scalable, when Pillow has FreeType
    except (TypeError, OSError, ImportError):
        return ImageFont.load_default()
