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

The steps, each one set by a param below:

1. keep one channel of the frame (``channel``);
2. smooth it (``blur_px``), so fringes, speckle and sensor noise do not split
   the blob into pieces;
3. keep the pixels past a grey level (``threshold``): a fixed one, Otsu's, or
   one placed between the background (the median level) and the blob's peak;
4. fill the holes inside each blob (``fill_holes``): dark rings, dust;
5. measure the largest blob of ``min_area`` pixels or more.

A dim laser spot, a red disc with diffraction rings on a dark background,
wants ``channel: red``, ``blur_px: 4``, ``threshold: "20%"`` and
``fill_holes: true``: see config.yaml.

params
------
channel       "luma", "red", "green", "blue" or "max" (brightest  default "luma"
              of R, G, B); ignored on a mono frame
blur_px       Gaussian blur radius (sigma), pixels; 0 = none       default 0
threshold     0-255; "auto" for Otsu's level; or "N%" for N% of    default 175
              the way from the background to the blob's peak
min_contrast  with "auto" or "N%": a frame whose peak is less      default 10
              than this above the background has no blob
fill_holes    count holes inside a blob as part of it              default false
min_area      smallest blob kept, in pixels                        default 500
invert        find a dark blob on a light background               default false
stretch       brighten the processed view so the frame's           default false
              brightest pixel is white (display only)

Try it on a saved image, with the params of config.yaml, from the repository root::

    python -m examples.gige_cv_analysis.blob_analysis spot.jpg   # writes spot_blob.png
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
_RGB_INDEX = {"red": 0, "green": 1, "blue": 2}
OVERLAY_RGB = (255, 196, 0)


def analyze(frame: np.ndarray, params: dict) -> AnalysisResult:
    """Locate the largest blob past the threshold: centre, size, bounding box, orientation."""
    gray = pick_channel(frame, params.get("channel", "luma"))
    blur_px = float(params.get("blur_px", 0))
    # truncate=2: a kernel of ±2 sigma, plenty to smooth and twice as fast as the default.
    smooth = ndimage.gaussian_filter(gray, blur_px, truncate=2.0) if blur_px > 0 else gray
    invert = bool(params.get("invert", False))
    background = median_level(gray)
    peak = int(smooth.min() if invert else smooth.max())   # the blob's extreme level
    min_area = int(params.get("min_area", 500))

    threshold = params.get("threshold", 175)
    adaptive = threshold == "auto" or (isinstance(threshold, str) and threshold.endswith("%"))
    if threshold == "auto":
        level = otsu_threshold(smooth)
    elif adaptive:
        level = round(background + float(threshold[:-1]) / 100.0 * (peak - background), 1)
    else:
        level = int(threshold)

    data = {"found": False, "threshold": level, "blobs": 0, "background": background, "peak": peak}
    blob = None
    min_contrast = float(params.get("min_contrast", 10))
    if adaptive and abs(peak - background) < min_contrast:
        message = f"no blob: peak {peak} within {min_contrast:g} of background {background}"
    else:
        message = f"no blob of {min_area}+ px {'below' if invert else 'above'} {level:g}"
        mask = smooth < level if invert else smooth >= level
        if params.get("fill_holes", False):
            mask = fill_holes(mask)
        labels, count = ndimage.label(mask)
        if count:
            areas = np.bincount(labels.ravel())[1:]   # pixels per label; label 0 is the background
            kept = areas >= min_area
            data["blobs"] = int(kept.sum())
            if kept.any():
                blob = labels == int(np.argmax(np.where(kept, areas, 0))) + 1
                data.update(measure(blob))

    image = draw_overlay(frame, blob, data, message, stretch=bool(params.get("stretch", False)))
    return AnalysisResult(image=image, data=data, ok=data["found"])


def pick_channel(frame: np.ndarray, channel: str) -> np.ndarray:
    """The frame as one 8-bit plane: luma, one of R, G, B, or their maximum."""
    if frame.ndim == 2:
        return frame
    if channel == "luma":
        return (frame[..., :3] @ _LUMA).astype(np.uint8)
    if channel == "max":
        return frame[..., :3].max(axis=2)
    if channel in _RGB_INDEX:
        return frame[..., _RGB_INDEX[channel]]
    raise ValueError(f'channel must be "luma", "red", "green", "blue" or "max", got {channel!r}')


def median_level(gray: np.ndarray) -> int:
    """The median grey level: the background's, while the blob covers under half the frame."""
    counts = np.cumsum(_histogram(gray))
    return int(np.searchsorted(counts, counts[-1] / 2.0))


def otsu_threshold(gray: np.ndarray) -> int:
    """The grey level that best separates dark from bright pixels (Otsu, 1979)."""
    hist = _histogram(gray).astype(np.float64)
    weight = np.cumsum(hist) / hist.sum()              # share of pixels at or below each level
    mean = np.cumsum(hist * np.arange(256)) / hist.sum()
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mean[-1] * weight - mean) ** 2 / (weight * (1.0 - weight))
    return int(np.argmax(np.nan_to_num(between))) + 1   # bright = above the best split


def _histogram(gray: np.ndarray) -> np.ndarray:
    """Pixels per grey level, counted on every other row and column: as good, 4× faster."""
    return np.bincount(gray[::2, ::2].ravel(), minlength=256)


def fill_holes(mask: np.ndarray) -> np.ndarray:
    """*mask* with every region it encloses filled: what is not reachable from the frame's edge."""
    outside, _ = ndimage.label(~mask)
    edge = np.unique(np.concatenate((outside[0], outside[-1], outside[:, 0], outside[:, -1])))
    lut = np.ones(outside.max() + 1, dtype=bool)
    lut[edge] = False                                  # label 0 (the mask itself) stays True
    return lut[outside]


def measure(blob: np.ndarray) -> dict:
    """Centroid, size, bounding box and orientation of a boolean mask.

    ``diameter_px`` is that of the disc of the same area. ``angle_deg`` is
    the long axis, in degrees from the image x axis and clockwise on screen
    (image y points down). It means little when ``elongation`` (long/short
    axis) is close to 1, as for a disc.
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
        "diameter_px": round(2.0 * math.sqrt(xs.size / math.pi), 1),
        "bbox_px": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
    }


def draw_overlay(frame: np.ndarray, blob: np.ndarray | None, data: dict, message: str,
                 stretch: bool = False) -> np.ndarray:
    """The frame in colour with the blob outlined, its box, centre, long axis and a label."""
    rgb = frame if frame.ndim == 3 else np.repeat(frame[:, :, np.newaxis], 3, axis=2)
    image = Image.fromarray(np.array(rgb[..., :3], dtype=np.uint8))   # a copy: the frame is read-only
    if stretch:
        gain = 255.0 / max(1, int(rgb.max()))
        image = image.point([min(255, round(level * gain)) for level in range(256)] * 3)

    draw = ImageDraw.Draw(image)
    line = max(2, image.width // 400)
    if data["found"]:
        x0, y0, x1, y1 = data["bbox_px"]
        inside = blob[y0:y1 + 1, x0:x1 + 1]
        border = inside & ~ndimage.binary_erosion(inside, iterations=line, border_value=0)
        image.paste(OVERLAY_RGB, (x0, y0), Image.fromarray(border))   # the blob's outline
        cx, cy = data["x_px"], data["y_px"]
        draw.rectangle((x0, y0, x1, y1), outline=OVERLAY_RGB, width=max(1, line // 2))
        arm = max(12, image.width // 80)
        draw.line(((cx - arm, cy), (cx + arm, cy)), fill=OVERLAY_RGB, width=line)
        draw.line(((cx, cy - arm), (cx, cy + arm)), fill=OVERLAY_RGB, width=line)
        if data["elongation"] and data["elongation"] > 1.2:
            half = max(x1 - x0, y1 - y0) / 2.0
            a = math.radians(data["angle_deg"])
            draw.line(((cx - half * math.cos(a), cy - half * math.sin(a)),
                       (cx + half * math.cos(a), cy + half * math.sin(a))), fill=OVERLAY_RGB, width=line)
        label = f"x {cx:.0f}  y {cy:.0f}  diam {data['diameter_px']:.0f} px  area {data['area_px']} px"
    else:
        label = message

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


def _main(argv: list[str] | None = None) -> None:
    """Analyse saved images with config.yaml's ``vision.analysis_params``; write ``<name>_blob.png``."""
    import argparse
    import json
    from pathlib import Path

    import yaml

    config = Path(__file__).with_name("config.yaml")
    parser = argparse.ArgumentParser(description=_main.__doc__)
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--device", default="vision", help="cv_process device in config.yaml")
    args = parser.parse_args(argv)
    params = yaml.safe_load(config.read_text(encoding="utf-8"))["devices"][args.device].get("analysis_params") or {}
    for path in args.images:
        with Image.open(path) as source:
            frame = np.asarray(source if source.mode == "L" else source.convert("RGB"))
        result = analyze(frame, params)
        out = path.with_name(f"{path.stem}_blob.png")
        Image.fromarray(result.image).save(out)
        print(f"{path}: {json.dumps(result.data)} -> {out}")


if __name__ == "__main__":
    _main()
