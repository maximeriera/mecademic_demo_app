"""Beam analysis for a ``cv_process`` device: track a laser beam by its edge.

The device calls ``analyze(frame, params)`` once per frame, on its own thread:

* ``frame``: 8-bit NumPy array, mono ``H×W`` or RGB ``H×W×3``, read-only.
* ``params``: the ``analysis_params`` mapping from config.yaml.
* Returns ``AnalysisResult(image, data, ok)``:

  - ``image``: the annotated view, shown as "processed" (None shows the raw frame);
  - ``data``: JSON-serialisable measurements (see below), read by task code
    with ``vision.last_result()["data"]``;
  - ``ok``: True when a beam edge was found and the beam is not clipped by
    the edge of the active area (and, with ``require_unsaturated``, not
    saturated).

Uses NumPy and OpenCV: add ``opencv-python-headless`` to requirements.txt.
One call takes roughly 15-30 ms on a 5 MP frame (plus ~5-15 ms for the
enhanced display), so it keeps up with 15 fps.

The steps:

1. keep one channel of the frame (``channel``) and the active sensor area
   (``area_*``), leaving out the parts of the sensor the lens clips;
2. fit the background as a tilted plane from the area's border (or use a
   dark frame), and remove hot pixels;
3. find the beam on a downsampled copy, testing the integrated SNR of the
   brightest blob against ``detect_snr``, so dim, large beams are found too;
4. seed with power-weighted moments in a region capped around the beam,
   averaged in blocks for large beams (less noise, faster);
5. cast ``n_rays`` rays from the centre; on each, find where the beam ends
   (the profile falls to background + ``edge_k`` x noise and stays there)
   and take the steepest drop just inside it, so internal bright/dim
   steps are not mistaken for the edge;
6. fit an ellipse to those edge points, rejecting outliers: the beam's
   geometric centre, size and orientation, unaffected by uneven power
   across the beam. Also reported: how unevenly the power is spread.

Meant for hard-edged (flat-top / apertured) beams; a soft Gaussian-like beam
reports "edge not found".

params
------
channel        "luma", "red", "green", "blue" or "max"; ignored    default "max"
               on a mono frame. Pick the colour of your laser.
pixel_size_um  sensor pixel pitch, for the *_um results; null      default 3.45
               reports pixels only
area_width     fraction of the frame width / height analysed       default 1.0
area_height    and shown (to leave out lens-clipped edges)         default 1.0
area_center_x  centre of that area, 0-1 across / down the frame    default 0.5
area_center_y                                                      default 0.5
detect_snr     "beam present" threshold on the integrated SNR;     default 20
               lower if real beams are missed, raise if noise is
               reported as a beam
edge_k         end-of-beam level, in noise sigmas above            default 5
               background; raise if a faint halo pulls the edge out
n_rays         rays for the edge fit                               default 120
dark_frame     path to a dark image (laser blocked), same size     default none
               as the camera frames; or call set_dark() at run time
require_unsaturated  ok is False when the beam saturates           default false
display        "enhanced" (denoised false colour), "raw" (the      default "enhanced"
               frame itself) or "none" (no processed image)
display_width  width of the displayed image, px (before the axes  default 800
               margins); smaller areas are shown at their own
               size; 0 = full size
temporal_alpha enhanced view: frame averaging, motion-aware (no    default 0.3
               trail); 1 = off
noise_floor_k  enhanced view: black level = background + k x noise default 2
smooth_px      enhanced view: blur, display pixels; 0 = off        default 1
remove_stripes enhanced view: hide sensor row/column banding       default true
log_display    enhanced view: logarithmic colour scale             default false
show_text      draw the measurements, status / warning flags and   default false
               target status as text on the image (the shapes are
               always drawn)
axes           scales along the left and bottom edges, in margins  default true
               added outside the image, measured from the top-left
               corner of the displayed area
axis_unit      "mm", "um" or "px" (px when pixel_size_um is null)  default "mm"
flip_y         show the image upside down from the camera's rows,  default true
               with the y scale increasing upward from 0 at the
               bottom (the camera's row numbers increase upward
               physically). Display only: the data keep camera rows,
               so y and dy in the data increase upward on screen.

Alignment targets (one circle at a time; see "Targets" below):
targets_enabled         show targets and report alignment data      default false
                        (enable_targets() overrides it at run time)
target_min_radius_um    random target radius range, um              default 200
target_max_radius_um    (equal values: fixed size)                  default 600
target_match_size       beam diameter must match the circle too     default true
target_tolerance_um     allowed beam-centre error                   default 50
target_size_tolerance_um  allowed beam-diameter error               default 30
target_hold_frames      frames in a row within both tolerances      default 5
                        to complete a target
target_margin           keep circles this fraction of the active    default 0.05
                        area away from its edges
target_min_jump         each new target at least this fraction of   default 0.2
                        the area diagonal from the previous one
target_count            stop after this many targets; null =        default null
                        endless
target_list             fixed targets instead of random:            default null
                        [[x_px, y_px, radius_um], ...], full-frame px
target_seed             random seed, for a repeatable sequence      default null

Advanced (defaults suit most cases): downsample (4), background ("plane" or
"constant"), despeckle (true), noise_k (3), bin_factor ("auto"),
bin_target_px (50), max_bin (16), roi_factor (3), roi_cap_factor (4),
max_roi_half_px (null), max_iter (15), search_level (0.135), mask_k (3),
detect_blur (1.5).

data
----
found, status ("ok" / "no beam" / "edge not found"), and when found:
x_px, y_px            edge-ellipse centre, full-frame pixels
major_px, minor_px    edge-ellipse axes; diameter_px = sqrt(major x minor)
angle_deg             major axis, from the image x axis, clockwise on screen
ellipticity           minor / major
centroid_x_px, _y_px  power-weighted centroid; centroid_offset_px from the
                      edge centre
tilt_pct, tilt_dir_deg  linear power change across the beam (% of mean)
                      and the direction it increases toward
nonuniformity_pct     remaining unevenness once the tilt is removed
*_um                  the same lengths in micrometres (with pixel_size_um)
edge_points, edge_rms_px  rays used and their rms distance from the fit
peak, background, saturated, clipped, detect_snr, bin, area_px
(x, y, w, h of the active area), time_ms, frame (counts every call: use it
to ignore results from frames taken before a robot move finished)

target                {"enabled": false} when targets are off, otherwise:
  index, completed    current target (0, 1, ...) and how many are done;
                      done is true once target_count targets are completed
  x_px, y_px, diameter_px (+ _um)   the current target, full-frame
  dx_px, dy_px (+ _um)  target - beam centre: how far the BEAM must move,
                      in image axes (x right, y down); null without a beam
  distance_px, size_error_px (+ _um)  centre distance; target - beam
                      diameter (+: beam must grow); null without a beam
  position_ok, size_ok, inside  within each tolerance / both this frame
  hold, hold_frames   frames in a row inside so far / needed
  reached             true on the frame the target is completed (the next
                      one then appears); use completed to catch it reliably
  last_completed      {index, distance_um, size_error_um, frame} of the
                      most recent completed target, or null
  tolerance_um, size_tolerance_um, match_size   the criteria in use

Targets
-------
Task code can drive the targets at run time (thread-safe; takes effect on the
next frame): enable_targets(True / False / None), next_target() to skip,
reset_targets() to start over, set_target_size_range(min_um, max_um) to keep
targets to sizes the lens can reach (None, None to go back to the params).
Disabling hides the targets and freezes the sequence; enabling resumes the
same target.
"""

import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional

import cv2
import numpy as np

from devices.CVProcessDevice import AnalysisResult

PIXEL_SIZE_UM = 3.45
BIT_DEPTH = 8                      # frames from the device are 8-bit
E2 = float(np.exp(-2))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
#: BeamProfiler constructor args that can be set from params.
_PROFILER_PARAMS = (
    "detect_snr", "n_rays", "edge_k", "downsample", "background", "despeckle", "noise_k",
    "bin_factor", "bin_target_px", "max_bin", "roi_factor", "roi_cap_factor",
    "max_roi_half_px", "max_iter", "search_level", "mask_k", "detect_blur",
)

_lock = threading.Lock()
_state: dict = {"key": None, "profiler": None, "averager": None,
                "dark": None, "dark_key": None, "user_dark": None, "frame": 0,
                # targets
                "targets": None, "targets_key": None, "targets_override": None,
                "size_range": None, "pending_next": 0, "pending_reset": False}


def analyze(frame: np.ndarray, params: dict) -> AnalysisResult:
    """Find the beam's edge ellipse: centre, size, orientation, power spread."""
    t0 = time.perf_counter()
    params = params or {}
    full_h, full_w = frame.shape[:2]
    box = crop_box(full_h, full_w,
                   float(params.get("area_width", 1.0)), float(params.get("area_height", 1.0)),
                   float(params.get("area_center_x", 0.5)), float(params.get("area_center_y", 0.5)))
    x0, y0, w, h = box
    channel = params.get("channel", "max")
    gray = pick_channel(frame[y0:y0 + h, x0:x0 + w], channel)

    with _lock:
        profiler, averager = _prepare(params, box, channel, gray.shape)
        r = profiler.process(gray)
        status, snr = profiler.last_status, profiler.last_detect_snr

        _state["frame"] += 1
        data = {"found": r is not None, "status": status, "detect_snr": round(snr, 1),
                "area_px": [x0, y0, w, h], "frame": _state["frame"]}
        if r is not None:
            data.update(_result_data(r, x0, y0))

        targets = _prepare_targets(params, box)
        if targets is not None:
            targets.update(r, _state["frame"])
            data["target"] = targets.data(x0, y0)
        else:
            data["target"] = {"enabled": False}

        display = params.get("display", "enhanced")
        image = None
        if display != "none":
            image = _render(frame, gray, box, r, profiler, averager, params,
                            raw=(display == "raw"), targets=targets)

    ok = r is not None and not r.clipped
    if ok and params.get("require_unsaturated", False):
        ok = not r.saturated
    data["time_ms"] = round(1e3 * (time.perf_counter() - t0), 1)
    return AnalysisResult(image=image, data=data, ok=ok)


def set_dark(frame: Optional[np.ndarray]) -> None:
    """Use *frame* (taken with the laser blocked, same format as the camera
    frames) as the dark frame from now on; None goes back to the
    ``dark_frame`` param, if any. Removes fixed-pattern offsets and stray
    light from both the measurement and the display."""
    with _lock:
        _state["user_dark"] = None if frame is None else np.array(frame, copy=True)
        _state["dark_key"] = None                    # re-crop / re-apply on next frame


def reset() -> None:
    """Forget all state (profiler caches, display averaging, dark frames,
    targets and their run-time settings)."""
    with _lock:
        _state.update(key=None, profiler=None, averager=None, dark=None,
                      dark_key=None, user_dark=None, frame=0,
                      targets=None, targets_key=None, targets_override=None,
                      size_range=None, pending_next=0, pending_reset=False)


def enable_targets(on: Optional[bool]) -> None:
    """Show targets and report alignment data (True), hide them and freeze
    the sequence (False, e.g. while the robot switches lenses), or follow
    the targets_enabled param again (None). Re-enabling resumes the same
    target, with its hold count restarted."""
    with _lock:
        _state["targets_override"] = None if on is None else bool(on)


def next_target() -> None:
    """Skip to the next target (on the next frame)."""
    with _lock:
        _state["pending_next"] += 1


def reset_targets() -> None:
    """Start the target sequence over (on the next frame)."""
    with _lock:
        _state["pending_reset"] = True
        _state["pending_next"] = 0


def set_target_size_range(min_radius_um: Optional[float],
                          max_radius_um: Optional[float]) -> None:
    """Keep target radii within [min, max] um, e.g. to what the lens can
    reach with its z travel; the current target is replaced if it is now
    out of range. None, None goes back to the params' range."""
    with _lock:
        _state["size_range"] = (None if min_radius_um is None or max_radius_um is None
                                else (float(min_radius_um), float(max_radius_um)))
        if _state["targets"] is not None:
            _state["targets"].set_size_range(_state["size_range"])


def pick_channel(frame: np.ndarray, channel: str) -> np.ndarray:
    """The frame as one contiguous 8-bit plane: luma, R, G, B, or their max."""
    if frame.ndim == 2:
        return np.ascontiguousarray(frame)
    rgb = frame[..., :3]
    if channel == "luma":
        return cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2GRAY)
    if channel == "max":
        return np.ascontiguousarray(rgb.max(axis=2))
    index = {"red": 0, "green": 1, "blue": 2}.get(channel)
    if index is None:
        raise ValueError(f'channel must be "luma", "red", "green", "blue" or "max", got {channel!r}')
    return np.ascontiguousarray(rgb[..., index])


def _prepare(params, box, channel, shape):
    """Profiler (rebuilt when its params change), display averager, dark frame."""
    pixel = params.get("pixel_size_um", PIXEL_SIZE_UM)
    kwargs = {k: params[k] for k in _PROFILER_PARAMS if k in params}
    key = (pixel, tuple(sorted(kwargs.items())), box, channel)
    if _state["key"] != key:
        _state["profiler"] = BeamProfiler(pixel_size_um=pixel, bit_depth=BIT_DEPTH, **kwargs)
        _state["averager"] = None
        _state["dark_key"] = None
        _state["key"] = key
    profiler = _state["profiler"]

    alpha = float(params.get("temporal_alpha", 0.3))
    if alpha >= 1:
        _state["averager"] = None
    elif _state["averager"] is None or _state["averager"].alpha != alpha:
        _state["averager"] = PreviewAverager(alpha=alpha)

    # dark frame: set_dark() wins over the dark_frame param
    source = _state["user_dark"]
    path = params.get("dark_frame")
    dark_key = (id(source) if source is not None else path, box, channel)
    if _state["dark_key"] != dark_key:
        full = source
        if full is None and path:
            full = _load_image(path)
        dark = None
        if full is not None:
            x0, y0, w, h = box
            dark = pick_channel(full[y0:y0 + h, x0:x0 + w], channel)
            if dark.shape != shape:
                raise ValueError(f"dark frame is {full.shape[:2]}, camera frames give an "
                                 f"area of {shape}: they must be the same size")
        profiler.set_dark(dark)
        _state["dark"] = dark
        _state["dark_key"] = dark_key
    return profiler, _state["averager"]


def _prepare_targets(params, box):
    """The TargetSequencer when targets are enabled, else None. Rebuilt when
    the target params or the active area change; applies pending run-time
    requests (skip / reset)."""
    on = _state["targets_override"]
    if on is None:
        on = bool(params.get("targets_enabled", False))
    seq = _state["targets"]
    if not on:
        if seq is not None:
            seq.pause()
        return None

    pixel = params.get("pixel_size_um", PIXEL_SIZE_UM)
    tp = {k: params[k] for k in sorted(params) if k.startswith("target_")}
    key = (pixel, box, repr(tp))
    if seq is None or _state["targets_key"] != key:
        x0, y0, w, h = box
        fixed = params.get("target_list")
        if fixed is not None:                         # full-frame px -> area px
            fixed = [(float(x) - x0, float(y) - y0, float(r)) for x, y, r in fixed]
        seq = TargetSequencer(
            (h, w), pixel,
            min_radius_um=float(params.get("target_min_radius_um", 200.0)),
            max_radius_um=float(params.get("target_max_radius_um", 600.0)),
            match_size=bool(params.get("target_match_size", True)),
            tolerance_um=float(params.get("target_tolerance_um", 50.0)),
            size_tolerance_um=float(params.get("target_size_tolerance_um", 30.0)),
            hold_frames=int(params.get("target_hold_frames", 5)),
            margin_frac=float(params.get("target_margin", 0.05)),
            min_jump_frac=float(params.get("target_min_jump", 0.2)),
            n_targets=params.get("target_count"),
            targets=fixed, seed=params.get("target_seed"),
            size_range=_state["size_range"])
        _state["targets"], _state["targets_key"] = seq, key
        _state["pending_reset"] = False
    if _state["pending_reset"]:
        seq.reset()
        _state["pending_reset"] = False
    while _state["pending_next"] > 0:
        seq.next_target()
        _state["pending_next"] -= 1
    return seq


def _load_image(path):
    from PIL import Image
    with Image.open(path) as im:
        return np.asarray(im if im.mode == "L" else im.convert("RGB"))


def _result_data(r: "BeamResult", x0: int, y0: int) -> dict:
    """BeamResult -> JSON-friendly dict, positions in full-frame pixels."""
    d = {k: v for k, v in asdict(r).items()}
    rename = {"cx_px": "x_px", "cy_px": "y_px", "cx_um": "x_um", "cy_um": "y_um",
              "peak_counts": "peak", "background_counts": "background", "bin_factor": "bin"}
    d = {rename.get(k, k): v for k, v in d.items()}
    # crop offset -> full-frame coordinates
    for k, off in (("x_px", x0), ("y_px", y0), ("centroid_x_px", x0), ("centroid_y_px", y0)):
        d[k] = d[k] + off
    p = r.major_um / r.major_px if r.major_um is not None and r.major_px else None
    for k in ("x", "y", "centroid_x", "centroid_y"):
        d[f"{k}_um"] = d[f"{k}_px"] * p if p is not None else None
    out = {}
    for k, v in d.items():
        if isinstance(v, (bool, np.bool_)):
            out[k] = bool(v)
        elif isinstance(v, (int, np.integer)):
            out[k] = int(v)
        elif isinstance(v, (float, np.floating)):
            out[k] = round(float(v), 3 if k in ("ellipticity",) else 2)
        else:
            out[k] = v
    out["status"] = "ok"
    return out


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
EDGE_BGR = (0, 140, 255)            # orange


class _ViewMap:
    """Active-area pixel coordinates -> display pixels (scale s, and the
    vertical flip of flip_y); angles mirrored to match."""

    def __init__(self, s, height, flip):
        self.s, self.h, self.flip = s, height, flip

    def pt(self, x, y):
        yd = y * self.s
        return int(round(x * self.s)), int(round(self.h - 1 - yd if self.flip else yd))

    def ang(self, a):
        return -a if self.flip else a
_dark_view_cache: dict = {}


def _render(frame, gray, box, r, profiler, averager, params, raw=False, targets=None):
    """The returned image (RGB): enhanced or raw view of the active area,
    with the edge ellipse and the measurements drawn on."""
    width = int(params.get("display_width", 800))
    w_area = gray.shape[1]
    # Fast whole-number reduction first, never below the requested width;
    # then an exact resize of the (already small) view to display_width, so
    # the output size doesn't jump as the active area changes.
    step = max(1, w_area // width) if 0 < width < w_area else 1
    if raw:
        x0, y0, w, h = box
        crop = frame[y0:y0 + h, x0:x0 + w]
        crop = crop if crop.ndim == 3 else np.repeat(crop[:, :, None], 3, axis=2)
        view = np.ascontiguousarray(crop[..., 2::-1])                # RGB -> BGR
        if step > 1:
            view = cv2.resize(view, (view.shape[1] // step, view.shape[0] // step),
                              interpolation=cv2.INTER_AREA)
    else:
        view = _enhanced_view(gray, r, profiler.dark, step, averager,
                              smooth=float(params.get("smooth_px", 1.0)),
                              noise_floor_k=float(params.get("noise_floor_k", 2.0)),
                              remove_stripes=bool(params.get("remove_stripes", True)),
                              log_display=bool(params.get("log_display", False)))
    covered = step * (w_area // step)               # area columns the view covers
    if 0 < width < view.shape[1]:
        hh = max(1, int(round(view.shape[0] * width / view.shape[1])))
        view = cv2.resize(view, (width, hh), interpolation=cv2.INTER_AREA)
    scale = view.shape[1] / covered                 # display px per area px
    flip = bool(params.get("flip_y", True))
    if flip:
        view = cv2.flip(view, 0)                    # upside down, as the camera sits
    vm = _ViewMap(scale, view.shape[0], flip)
    text = bool(params.get("show_text", False))
    _annotate(view, r, profiler, vm, text)
    if targets is not None:
        targets.draw(view, r, vm, text)
    if params.get("axes", True):
        pixel = params.get("pixel_size_um", PIXEL_SIZE_UM)
        unit = params.get("axis_unit", "mm") if pixel else "px"
        view = _add_axes(view, 1.0 / scale, pixel or 1.0, unit, flip)
    return np.ascontiguousarray(view[..., ::-1])                    # BGR -> RGB


def _enhanced_view(img, r, dark, step, averager, smooth=1.0, noise_floor_k=2.0,
                   remove_stripes=True, log_display=False):
    """Denoised, auto-contrast false-colour view (BGR), display only:
    float area-averaging, dark/banding removal, hot-pixel filter, light blur,
    motion-aware frame averaging, black level just above the noise and white
    at the beam's own peak."""
    h, w = img.shape[0] // step, img.shape[1] // step
    img = cv2.resize(img.astype(np.float32), (w, h), interpolation=cv2.INTER_AREA)
    if dark is not None:
        key = (id(dark), dark.shape, step)
        dsm = _dark_view_cache.get(key)
        if dsm is None:
            _dark_view_cache.clear()
            dsm = cv2.resize(dark.astype(np.float32), (w, h), interpolation=cv2.INTER_AREA)
            _dark_view_cache[key] = dsm
        img = img - dsm
    if remove_stripes:
        img = img - np.median(img[::3, :], axis=0, keepdims=True)   # column banding
        img = img - np.median(img[:, ::3], axis=1, keepdims=True)   # row banding
    s = 1.0 / step

    win = None                                       # beam window, display pixels
    if r is not None:
        size = r.major_px * s
        rad = 0.75 * size + 3
        cxp, cyp = r.cx_px * s, r.cy_px * s
        x0, x1 = int(max(0, cxp - rad)), int(min(w, cxp + rad + 1))
        y0, y1 = int(max(0, cyp - rad)), int(min(h, cyp + rad + 1))
        if x1 > x0 and y1 > y0:
            win = (slice(y0, y1), slice(x0, x1), size)

    unfiltered = img
    img = cv2.medianBlur(img, 3)                     # hot pixels
    if smooth > 0:
        img = cv2.GaussianBlur(img, (0, 0), smooth)
    if win is not None and win[2] < 12:
        # a focused spot only a few display pixels wide would be flattened
        # or erased by the median / blur: show it unfiltered
        img[win[0], win[1]] = unfiltered[win[0], win[1]]

    sub = img[::2, ::2]
    bg = float(np.median(sub))
    noise = 1.4826 * float(np.median(np.abs(sub - bg))) + 1e-6
    if averager is not None:
        img = averager(img, noise)
        sub = img[::2, ::2]
        bg = float(np.median(sub))
        noise = 1.4826 * float(np.median(np.abs(sub - bg))) + 1e-6

    lo = bg + noise_floor_k * noise
    hi = float(img[win[0], win[1]].max()) if win is not None else float(np.percentile(sub, 99.9))
    hi = max(hi, lo + 10 * noise)                    # pure noise never fills the range
    if log_display:
        z = np.maximum(img - lo, 0) / noise
        disp = (255.0 * np.log1p(z) / np.log1p((hi - lo) / noise)).clip(0, 255).astype(np.uint8)
    else:
        alpha = 255.0 / (hi - lo)
        disp = cv2.convertScaleAbs(img, alpha=alpha, beta=-lo * alpha)
    return cv2.LUT(cv2.cvtColor(disp, cv2.COLOR_GRAY2BGR), _JET_LUT)


AXIS_BG = (32, 32, 32)
AXIS_FG = (215, 215, 215)


def _nice_step(span, target_ticks=8):
    """A 1 / 2 / 5 x 10^n tick spacing giving about target_ticks ticks."""
    raw = span / max(target_ticks, 1)
    mag = 10 ** np.floor(np.log10(raw)) if raw > 0 else 1.0
    for m in (1, 2, 5, 10):
        if m * mag >= raw:
            return m * mag, m
    return 10 * mag, 10


def _add_axes(view, step, pixel_um, unit="mm", flip=False):
    """The view with scales added in margins on the left and bottom, measured
    from the top-left corner of the displayed area (bottom-left with flip:
    y then increases upward)."""
    x0 = y0 = 0
    scale = {"mm": pixel_um / 1000.0, "um": pixel_um, "px": 1.0}.get(unit)
    if scale is None:
        raise ValueError(f'axis_unit must be "mm", "um" or "px", got {unit!r}')
    h, w = view.shape[:2]
    fs = max(0.45, w / 1500)                        # label size follows the view
    th = 1
    font = cv2.FONT_HERSHEY_SIMPLEX
    tick, minor = max(5, int(7 * fs / 0.4)), max(3, int(4 * fs / 0.4))
    pad = max(3, int(4 * fs / 0.4))

    def value(i, offset):                           # view pixel -> sensor coordinate
        return (offset + (i + 0.5) * step - 0.5) * scale

    def ticks(n, offset):
        lo, hi = value(0, offset), value(n - 1, offset)
        major, mant = _nice_step(hi - lo)
        sub = major / (5 if mant in (1, 5, 10) else 4)
        dec = max(0, int(np.ceil(-np.log10(major))) if major < 1 else 0)
        out = []
        for v in np.arange(np.ceil(lo / sub) * sub, hi + 1e-9, sub):
            i = (v / scale - offset + 0.5) / step - 0.5          # back to a view pixel
            is_major = abs(v / major - round(v / major)) < 1e-6
            out.append((int(round(i)), is_major, f"{v:.{dec}f}"))
        return out

    xt, yt = ticks(w, x0), ticks(h, y0)
    lab_w = max([cv2.getTextSize(t[2], font, fs, th)[0][0] for t in yt if t[1]] + [10])
    lab_h = cv2.getTextSize("0", font, fs, th)[0][1]
    left = lab_w + tick + 2 * pad
    bottom = lab_h + tick + 2 * pad + lab_h + pad   # labels + axis title line
    top_right = max(pad, lab_w // 2 + pad)          # room for the last x label
    out = np.full((h + bottom + pad, w + left + top_right, 3), AXIS_BG, np.uint8)
    out[pad:pad + h, left:left + w] = view
    oy = pad
    cv2.rectangle(out, (left - 1, oy - 1), (left + w, oy + h), AXIS_FG, 1)

    for i, is_major, txt in xt:                     # bottom scale
        if not 0 <= i < w:
            continue
        x = left + i
        cv2.line(out, (x, oy + h), (x, oy + h + (tick if is_major else minor)), AXIS_FG, 1)
        if is_major:
            tw = cv2.getTextSize(txt, font, fs, th)[0][0]
            cv2.putText(out, txt, (x - tw // 2, oy + h + tick + pad + lab_h), font, fs,
                        AXIS_FG, th, cv2.LINE_AA)
    for i, is_major, txt in yt:                     # left scale
        if not 0 <= i < h:
            continue
        y = oy + (h - 1 - i if flip else i)
        cv2.line(out, (left - 1, y), (left - 1 - (tick if is_major else minor), y), AXIS_FG, 1)
        if is_major:
            tw = cv2.getTextSize(txt, font, fs, th)[0][0]
            cv2.putText(out, txt, (left - tick - pad - tw, y + lab_h // 2), font, fs,
                        AXIS_FG, th, cv2.LINE_AA)
    title = f"x, y ({unit})"
    tw = cv2.getTextSize(title, font, fs, th)[0][0]
    cv2.putText(out, title, (left + (w - tw) // 2, out.shape[0] - pad), font, fs,
                AXIS_FG, th, cv2.LINE_AA)
    return out


def _annotate(disp, r, profiler, vm, text=True):
    """Edge ellipse (orange, centre cross), power centroid
    (white cross) and, with text, the measurements, in place on a BGR view."""
    fs = max(0.5, disp.shape[1] / 1600)              # text scales with the view
    dy = int(round(44 * fs))
    if r is None:
        if text:
            _label(disp, f"{profiler.last_status} (SNR {profiler.last_detect_snr:.1f})",
                   (10, dy), (255, 255, 255), 1.1 * fs, 1)
        return disp

    unit, k = ("um", r.major_um / r.major_px) if r.major_um is not None else ("px", 1.0)
    lw = max(1, int(round(fs * 1.5)))
    s = vm.s
    c = vm.pt(r.cx_px, r.cy_px)
    axes = (max(1, int(round(r.major_px * s / 2))), max(1, int(round(r.minor_px * s / 2))))
    ang = vm.ang(r.angle_deg)
    cv2.ellipse(disp, c, axes, ang, 0, 360, EDGE_BGR, lw, cv2.LINE_AA)
    cv2.drawMarker(disp, c, EDGE_BGR, cv2.MARKER_CROSS, int(30 * fs), lw)
    pc = vm.pt(r.centroid_x_px, r.centroid_y_px)
    cv2.drawMarker(disp, pc, (255, 255, 255), cv2.MARKER_CROSS, int(14 * fs), lw)
    if not text:
        return disp

    lines = [
        (f"Edge {r.major_px * k:.1f} x {r.minor_px * k:.1f} {unit}", EDGE_BGR, 1.1, 1),
        (f"angle {r.angle_deg:.1f} deg  ell {r.ellipticity:.2f}", (255, 255, 255), 1.0, 1),
    ]
    if r.tilt_pct is not None:
        lines.append((f"tilt {r.tilt_pct:.0f}% toward {int(round(r.tilt_dir_deg))} deg  "
                      f"centroid shift {r.centroid_offset_px * k:.1f} {unit}",
                      (255, 255, 255), 1.0, 1))
    for flag, on in (("SATURATED", r.saturated), ("CLIPPED BY AREA EDGE", r.clipped)):
        if on:
            lines.append((flag, (0, 0, 255), 1.1, 2))
    y = dy
    for txt, col, sc, th_ in lines:
        _label(disp, txt, (10, y), col, sc * fs, th_)
        y += dy
    _label(disp, f"bin {r.bin_factor}  SNR {r.detect_snr:.0f}  edge pts {r.edge_points}  "
                 f"rms {r.edge_rms_px * k:.1f} {unit}",
           (10, disp.shape[0] - int(20 * fs)), (255, 255, 255), fs, 1)
    return disp


# ---------------------------------------------------------------------------
# Targets: one circle at a time (position and size) for the robot to align to
# ---------------------------------------------------------------------------
class TargetSequencer:
    """
    Shows one target circle at a time, at a random position in the active
    area and with a random size, and measures the beam against it.

    A target is completed when the beam centre is within tolerance_um of the
    circle's centre AND (with match_size) the beam diameter, sqrt(major x
    minor) of the edge ellipse, is within size_tolerance_um of the circle's
    diameter, for hold_frames frames in a row. The next target then appears.
    Positions here are active-area pixels; data() converts to full-frame.
    """

    def __init__(self, area_shape, pixel_size_um=PIXEL_SIZE_UM,
                 min_radius_um=200.0, max_radius_um=600.0, match_size=True,
                 tolerance_um=50.0, size_tolerance_um=30.0, hold_frames=5,
                 margin_frac=0.05, min_jump_frac=0.2, n_targets=None,
                 targets=None, seed=None, size_range=None):
        self.H, self.W = area_shape[:2]
        self.p = pixel_size_um or 1.0
        self.param_range = (min_radius_um / self.p, max_radius_um / self.p)
        self.rmin_px, self.rmax_px = self.param_range
        self.match_size = match_size
        self.tol_px = tolerance_um / self.p
        self.size_tol_px = size_tolerance_um / self.p
        self.hold_frames = max(1, int(hold_frames))
        self.margin = margin_frac
        self.min_jump = min_jump_frac * np.hypot(self.H, self.W)
        self.fixed = list(targets) if targets is not None else None
        self.n_targets = len(self.fixed) if self.fixed is not None else n_targets
        self.seed = seed
        self.reset()
        if size_range is not None:
            self.set_size_range(size_range)

    def set_size_range(self, size_range):
        """(min_um, max_um) radius limits, or None for the params' range;
        replaces the current random target if it is now out of range."""
        if size_range is None:
            self.rmin_px, self.rmax_px = self.param_range
        else:
            lo, hi = size_range
            self.rmin_px, self.rmax_px = lo / self.p, max(hi, lo) / self.p
        if self.target is not None and self.fixed is None and \
                not (self.rmin_px - 1e-6 <= self.target[2] <= self.rmax_px + 1e-6):
            self.index -= 1
            self.next_target()

    def reset(self):
        """Start over; with a seed, the same sequence repeats."""
        self.rng = np.random.default_rng(self.seed)
        self.index = -1
        self.completed = 0
        self.last_completed = None
        self.target = None
        self.done = False
        self.next_target()

    def pause(self):
        """Targets hidden: the hold count restarts when they come back."""
        self.hold = 0
        self.status = None

    def next_target(self):
        """Advance to a new target (also used to skip one)."""
        self.index += 1
        self.hold = 0
        self.status = None
        if self.n_targets is not None and self.index >= int(self.n_targets):
            self.done, self.target = True, None
            return
        if self.fixed is not None:
            x, y, r_um = self.fixed[self.index]
            self.target = (float(x), float(y), float(r_um) / self.p)
            return
        r_fit = 0.5 * min(self.W, self.H) * (1 - 2 * self.margin)   # largest that fits
        rmax = min(self.rmax_px, r_fit)
        rad = float(self.rng.uniform(min(self.rmin_px, rmax), rmax))
        mx, my = self.margin * self.W + rad, self.margin * self.H + rad
        prev = self.target
        t = None
        for _ in range(100):
            t = (float(self.rng.uniform(mx, max(mx, self.W - mx))),
                 float(self.rng.uniform(my, max(my, self.H - my))), rad)
            if prev is None or np.hypot(t[0] - prev[0], t[1] - prev[1]) >= self.min_jump:
                break
        self.target = t

    def update(self, r: Optional["BeamResult"], frame_no: int):
        """Measure the beam against the current target (once per frame)."""
        self.status = None
        if self.done or self.target is None:
            return
        tx, ty, tr = self.target
        st = {"x_px": tx, "y_px": ty, "diameter_px": 2 * tr, "reached": False,
              "position_ok": False, "size_ok": False, "inside": False}
        if r is None:
            self.hold = 0
            st.update(dx_px=None, dy_px=None, distance_px=None, size_error_px=None)
        else:
            dx, dy = tx - r.cx_px, ty - r.cy_px
            dist = float(np.hypot(dx, dy))
            size_err = 2 * tr - r.diameter_px
            pos_ok = dist <= self.tol_px
            size_ok = (abs(size_err) <= self.size_tol_px) or not self.match_size
            inside = pos_ok and size_ok
            self.hold = self.hold + 1 if inside else 0
            st.update(dx_px=float(dx), dy_px=float(dy), distance_px=dist,
                      size_error_px=float(size_err), position_ok=pos_ok,
                      size_ok=size_ok, inside=inside, reached=self.hold >= self.hold_frames)
        st["index"], st["hold"] = self.index, self.hold
        self.status = st
        if st["reached"]:
            self.completed += 1
            self.last_completed = {
                "index": self.index, "frame": frame_no,
                "distance_um": round(st["distance_px"] * self.p, 2),
                "size_error_um": round(st["size_error_px"] * self.p, 2)}
            self.next_target()
            self.status = st            # this frame still reports the completed target

    def data(self, x0, y0) -> dict:
        """JSON-friendly status, positions in full-frame pixels."""
        d = {"enabled": True, "done": self.done, "index": self.index,
             "completed": self.completed, "hold_frames": self.hold_frames,
             "tolerance_um": round(self.tol_px * self.p, 2),
             "size_tolerance_um": round(self.size_tol_px * self.p, 2),
             "match_size": self.match_size, "last_completed": self.last_completed}
        st = self.status
        if st is None and self.target is not None:      # just advanced / no update yet
            tx, ty, tr = self.target
            st = {"x_px": tx, "y_px": ty, "diameter_px": 2 * tr, "dx_px": None,
                  "dy_px": None, "distance_px": None, "size_error_px": None,
                  "position_ok": False, "size_ok": False, "inside": False,
                  "reached": False, "index": self.index, "hold": self.hold}
        if st is not None:
            reached = st["reached"]
            st = dict(st)
            st["x_px"] += x0
            st["y_px"] += y0
            for k in ("x_px", "y_px", "diameter_px", "dx_px", "dy_px",
                      "distance_px", "size_error_px"):
                v = st[k]
                st[k] = None if v is None else round(float(v), 2)
                st[k[:-3] + "_um"] = None if v is None else round(float(v) * self.p, 2)
            st["position_ok"], st["size_ok"], st["inside"] = (
                bool(st["position_ok"]), bool(st["size_ok"]), bool(st["inside"]))
            st["reached"] = bool(reached)
            if reached:                 # report the target just completed
                st["index"] = self.last_completed["index"]
            d.update(st)
        return d

    def draw(self, disp, r, vm, text=True):
        """Target circle, centre and beam-to-target arrow on a BGR view, and
        with text its status line.
        Green: off target; yellow: position OK, size not; magenta: both OK."""
        fs = max(0.5, disp.shape[1] / 1600)
        y_txt = disp.shape[0] - int(20 * fs) - int(round(44 * fs))
        if self.done:
            if text:
                _label(disp, "all targets done", (10, y_txt), (0, 255, 0), 1.1 * fs, 1)
            return
        if self.target is None:
            return
        st = self.status
        if st is not None and st["inside"]:
            col = (255, 0, 255)
        elif st is not None and st["position_ok"]:
            col = (0, 255, 255)
        else:
            col = (0, 255, 0)
        lw = max(2, int(round(fs * 2)))
        tc = vm.pt(self.target[0], self.target[1])
        cv2.circle(disp, tc, max(2, int(round(self.target[2] * vm.s))), col, lw, cv2.LINE_AA)
        cv2.drawMarker(disp, tc, col, cv2.MARKER_TILTED_CROSS, int(24 * fs), max(1, lw // 2))
        if r is not None:
            bc = vm.pt(r.cx_px, r.cy_px)
            cv2.arrowedLine(disp, bc, tc, col, max(1, lw // 2), cv2.LINE_AA, tipLength=0.04)
        if not text:
            return
        n = f"target {self.index + 1}" + (f"/{self.n_targets}" if self.n_targets else "")
        if st is not None and st["dx_px"] is not None:
            k = self.p
            txt = (f"{n}: dx {st['dx_px'] * k:+.0f} dy {st['dy_px'] * k:+.0f}  "
                   f"D {st['diameter_px'] * k:.0f} (err {st['size_error_px'] * k:+.0f}) um")
            if st["inside"]:
                txt += f"  HOLD {st['hold']}/{self.hold_frames}"
        else:
            txt = f"{n}: no beam"
        _label(disp, txt, (10, y_txt), col, 1.0 * fs, 1)


# ---------------------------------------------------------------------------
# Measurement: background, detection, seed moments, edge fit
# ---------------------------------------------------------------------------
@dataclass
class BeamResult:
    # Edge ellipse (geometric): centre, full axis lengths and orientation of
    # the major axis (degrees, image x-axis, y pointing down)
    cx_px: float
    cy_px: float
    major_px: float
    minor_px: float
    angle_deg: float
    ellipticity: float                 # minor / major (1.0 = round)
    diameter_px: float                 # equivalent round diameter sqrt(major*minor)
    edge_points: int                   # rays that found a usable edge
    edge_rms_px: float                 # rms distance of edge points from the fit
    # Power distribution
    centroid_x_px: float               # power-weighted centroid
    centroid_y_px: float
    centroid_offset_px: float          # |power centroid - edge centre|
    tilt_pct: Optional[float] = None   # linear intensity change across the beam,
                                       # % of mean (central 80 % of the edge)
    tilt_dir_deg: Optional[float] = None   # direction of increasing intensity
    nonuniformity_pct: Optional[float] = None  # rms deviation from that linear
                                               # trend (smoothed), % of mean
    # Image quality / status
    peak_counts: float = 0.0
    background_counts: float = 0.0
    saturated: bool = False
    clipped: bool = False              # edge ellipse runs off the active area
    detect_snr: float = 0.0
    bin_factor: int = 1
    # Physical units (filled if pixel_size_um is set)
    cx_um: Optional[float] = None
    cy_um: Optional[float] = None
    major_um: Optional[float] = None
    minor_um: Optional[float] = None
    diameter_um: Optional[float] = None
    centroid_x_um: Optional[float] = None
    centroid_y_um: Optional[float] = None
    centroid_offset_um: Optional[float] = None

    def as_dict(self):
        return asdict(self)


# ---------------------------------------------------------------------------


def crop_box(full_height, full_width, width_frac=1.0, height_frac=1.0,
             center_x_frac=0.5, center_y_frac=0.5, align=1):
    """
    Active sensor area as (x0, y0, width, height) in pixels.

    width_frac, height_frac : fraction of the sensor's width / height used.
    center_x_frac, center_y_frac : centre of the used area as a fraction of
                  the sensor (0.5, 0.5 = centred). Move it if the lens is not
                  centred on the sensor. The area is shifted to stay on the
                  sensor if it would run off an edge.
    align : round sizes / offsets down to multiples of this (cameras often
            need e.g. 4 or 8 for a hardware ROI).
    """
    w = int(round(full_width * float(np.clip(width_frac, 0.01, 1.0))))
    h = int(round(full_height * float(np.clip(height_frac, 0.01, 1.0))))
    w, h = max(align, w - w % align), max(align, h - h % align)
    x0 = int(round(full_width * center_x_frac - w / 2))
    y0 = int(round(full_height * center_y_frac - h / 2))
    x0 = int(np.clip(x0, 0, full_width - w))
    y0 = int(np.clip(y0, 0, full_height - h))
    x0, y0 = x0 - x0 % align, y0 - y0 % align
    return x0, y0, w, h


def _to_gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def _border_strip(img: np.ndarray, b: int = 8) -> np.ndarray:
    b = max(1, min(b, img.shape[0] // 4, img.shape[1] // 4))
    return np.concatenate([
        img[:b, :].ravel(), img[-b:, :].ravel(),
        img[b:-b, :b].ravel(), img[b:-b, -b:].ravel(),
    ]).astype(np.float32)


def _principal_diameters(sxx, syy, sxy):
    root = np.sqrt((sxx - syy) ** 2 + 4 * sxy ** 2)
    d_major = 2 * np.sqrt(2) * np.sqrt(max(sxx + syy + root, 0))
    d_minor = 2 * np.sqrt(2) * np.sqrt(max(sxx + syy - root, 0))
    angle = 0.5 * np.degrees(np.arctan2(2 * sxy, sxx - syy))
    return d_major, d_minor, angle


# ---------------------------------------------------------------------------
# Profiler


class BeamProfiler:
    """
    Create once, then call `process(frame)` for every frame. Returns a
    BeamResult, or None if no beam (or no usable edge) was found; the reason
    is in `last_status` ("no beam" / "edge not found").

    Main settings
    -------------
    pixel_size_um : pixel pitch (default 3.45 um); results also in microns.
                    Pass None for pixels only.
    bit_depth : sensor bit depth for saturation detection (default 8).
    dark_frame : optional background frame (laser blocked); see set_dark().
    detect_snr : "beam present" threshold on the integrated SNR of the
                 candidate blob. Lower it if real beams are missed; raise it
                 if noise is reported as a beam.
    n_rays : rays used for the edge fit.
    edge_k : on each ray the beam is taken to end where the profile falls to
             background + edge_k x noise (and stays there); the edge is the
             steepest drop just inside that point. This keeps internal
             bright/dim steps from being mistaken for the edge. Raise it if
             faint halo/scatter around the beam pulls the edge outward.

    Advanced (defaults are fine for most cases)
    -------------------------------------------
    downsample : factor for the per-frame coarse search (2 for tiny spots).
    background : "plane" (tilted-plane fit to the frame border) or
                 "constant" (border median).
    despeckle : 3x3 median filter on the analysis region (hot pixels).
    noise_k : noise cut (x sigma) for the moments that seed the edge fit.
    bin_factor, bin_target_px, max_bin : block-averaging of the analysis
                 region for large beams ("auto" = beam size / bin_target_px).
    roi_factor, roi_cap_factor, max_roi_half_px, max_iter : size limits and
                 iterations for the seed-moment region.
    search_level, mask_k, detect_blur : coarse-search outline level, noise
                 floor and smoothing.
    """

    def __init__(self, pixel_size_um=PIXEL_SIZE_UM, bit_depth=BIT_DEPTH, dark_frame=None,
                 detect_snr=20.0, n_rays=120, edge_k=5.0,
                 downsample=4, background="plane", despeckle=True, noise_k=3.0,
                 bin_factor="auto", bin_target_px=50, max_bin=16,
                 roi_factor=3.0, roi_cap_factor=4.0, max_roi_half_px=None, max_iter=15,
                 search_level=E2, mask_k=3.0, detect_blur=1.5):
        self.pixel_size_um = pixel_size_um
        self.bit_depth = bit_depth
        self.detect_snr = detect_snr
        self.n_rays = n_rays
        self.edge_k = edge_k
        self.ds = max(1, int(downsample))
        self.background = background
        self.despeckle = despeckle
        self.noise_k = noise_k
        self.bin_factor = bin_factor
        self.bin_target_px = bin_target_px
        self.max_bin = max_bin
        self.roi_factor = roi_factor
        self.roi_cap_factor = roi_cap_factor
        self.max_roi_half_px = max_roi_half_px
        self.max_iter = max_iter
        self.search_level = search_level
        self.mask_k = mask_k
        self.detect_blur = detect_blur
        self.last_detect_snr = 0.0
        self.last_status = "no beam"
        self.timings = {}
        self._strip_cache = None
        # With 8-bit data the noise is often < 1 count, so the measured sigma
        # can be 0; flooring it at 1 count keeps the noise threshold meaningful.
        self.sigma_floor = 1.0 if (bit_depth or 8) <= 8 else 0.5
        self.set_dark(dark_frame)

    def set_dark(self, dark_frame):
        if dark_frame is None:
            self.dark = self.dark_small = None
            return
        d = _to_gray(dark_frame).astype(np.float32)
        self.dark = d
        self.dark_small = cv2.resize(d, None, fx=1 / self.ds, fy=1 / self.ds,
                                     interpolation=cv2.INTER_AREA)

    # -- background ----------------------------------------------------------
    def _strip_coords(self, H, W, b=8, step=3):
        key = (H, W, b, step)
        if self._strip_cache is None or self._strip_cache[0] != key:
            yy, xx = np.indices((H, W), dtype=np.float32)
            xs = _border_strip(xx, b)[::step]
            ys = _border_strip(yy, b)[::step]
            self._strip_cache = (key, xs, ys)
        return self._strip_cache[1:]

    def _fit_background(self, raw):
        """Returns (a, bx, by, sigma): background = a + bx*x + by*y."""
        H, W = raw.shape
        step = 3                                     # subsample: plenty of points
        v = _border_strip(raw)[::step]
        if self.dark is not None:
            v = v - _border_strip(self.dark)[::step]
        med = float(np.median(v))
        dev = np.abs(v - med)
        sigma = max(1.4826 * float(np.median(dev)), self.sigma_floor)
        if self.background != "plane":
            return med, 0.0, 0.0, sigma
        xs, ys = self._strip_coords(H, W, step=step)
        keep = dev < 4 * sigma                       # reject hot pixels / beam tails
        x, y, z = xs[keep].astype(np.float64), ys[keep].astype(np.float64), v[keep].astype(np.float64)
        # 3x3 normal equations instead of an SVD least-squares solve
        n, sx, sy = x.size, x.sum(), y.sum()
        M = np.array([[n, sx, sy],
                      [sx, (x * x).sum(), (x * y).sum()],
                      [sy, (x * y).sum(), (y * y).sum()]])
        rhs = np.array([z.sum(), (x * z).sum(), (y * z).sum()])
        a, bx, by = np.linalg.solve(M, rhs)
        resid = z - (a + bx * x + by * y)
        sigma = max(1.4826 * float(np.median(np.abs(resid))), self.sigma_floor)
        return float(a), float(bx), float(by), sigma

    def _bg_patch(self, x0, x1, y0, y1, scale=1.0):
        a, bx, by, _ = self._bg
        if bx == 0.0 and by == 0.0:
            return np.float32(a)
        xs = (np.arange(x0, x1, dtype=np.float32) + 0.5) * scale - 0.5
        ys = (np.arange(y0, y1, dtype=np.float32) + 0.5) * scale - 0.5
        return (a + bx * xs[None, :] + by * ys[:, None]).astype(np.float32)

    # -- internals -----------------------------------------------------------
    def _coarse(self):
        raw = self._raw
        small = cv2.resize(raw, None, fx=1 / self.ds, fy=1 / self.ds,
                           interpolation=cv2.INTER_AREA).astype(np.float32)
        if self.dark_small is not None and self.dark_small.shape == small.shape:
            small -= self.dark_small
        sx = raw.shape[1] / small.shape[1]
        sy = raw.shape[0] / small.shape[0]
        small -= self._bg_patch(0, small.shape[1], 0, small.shape[0], scale=sx)
        small = cv2.medianBlur(small, 3)
        sb = float(self.detect_blur)
        if sb > 0:
            small = cv2.GaussianBlur(small, (0, 0), sb)
        # noise of the processed search image, measured (not assumed)
        st = _border_strip(small, 4)
        sig = 1.4826 * float(np.median(np.abs(st - np.median(st)))) + 1e-6
        # blurring correlates neighbouring pixels: noise of a sum over N pixels
        # grows like sig * sqrt(N) * (correlation area)^0.5
        corr = 2 * np.sqrt(np.pi) * sb if sb > 0.5 else 1.0

        peak = cv2.minMaxLoc(small)[1]
        thr = max(peak * self.search_level, self.mask_k * sig)
        mask = (small >= thr).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        if n < 2:
            self.last_detect_snr = 0.0
            return None
        sums = np.bincount(labels.ravel(), weights=small.ravel(), minlength=n)[1:]
        areas = stats[1:, cv2.CC_STAT_AREA].astype(np.float64)
        snr = sums / (sig * corr * np.sqrt(areas))
        k = int(np.argmax(snr))
        self.last_detect_snr = float(snr[k])
        if snr[k] < self.detect_snr:
            return None
        x, y, w, h = stats[k + 1, :4]
        return (x + w / 2) * sx, (y + h / 2) * sy, max(w * sx, 3), max(h * sy, 3)

    def _choose_bin(self, size_px):
        if self.bin_factor != "auto":
            return max(1, int(self.bin_factor))
        return int(np.clip(size_px / self.bin_target_px, 1, self.max_bin))

    def _binned_region(self, cx, cy, half, b):
        """Background-subtracted, despeckled, b x b binned float32 region
        (signed) around (cx, cy). Built once per frame and reused."""
        H, W = self._raw.shape
        x0 = int(max(0, np.floor(cx - half)))
        y0 = int(max(0, np.floor(cy - half)))
        x1 = int(min(W, np.ceil(cx + half) + 1))
        y1 = int(min(H, np.ceil(cy + half) + 1))
        nx, ny = max(1, (x1 - x0) // b), max(1, (y1 - y0) // b)
        x1, y1 = x0 + nx * b, y0 + ny * b
        r = self._raw[y0:y1, x0:x1].astype(np.float32)
        if self.dark is not None:
            r -= self.dark[y0:y1, x0:x1]
        if b > 1:
            r = cv2.resize(r, (nx, ny), interpolation=cv2.INTER_AREA)  # exact box mean
        a, bx, by, _ = self._bg
        if bx == 0.0 and by == 0.0:
            r -= np.float32(a)
        else:
            xs = x0 + (np.arange(nx, dtype=np.float32) + 0.5) * b - 0.5
            ys = y0 + (np.arange(ny, dtype=np.float32) + 0.5) * b - 0.5
            r -= (a + bx * xs[None, :] + by * ys[:, None]).astype(np.float32)
        if self.despeckle and min(r.shape) >= 3:
            r = cv2.medianBlur(r, 3)
        return r, x0, y0

    def _iso_moments(self, cx, cy, dx, dy):
        cap = self.roi_cap_factor * max(dx, dy)
        if self.max_roi_half_px is not None:
            cap = min(cap, self.max_roi_half_px)
        b = self._choose_bin(max(dx, dy))
        B, X0, Y0 = self._binned_region(cx, cy, cap, b)
        self._region = (B, X0, Y0, b, cap)
        ny, nx = B.shape
        thr = self.noise_k * self._bg[3] / b       # averaging b*b pixels: sigma/b
        corr = (b * b - 1) / 12.0                  # within-bin variance (px^2)

        converged = False
        sxx = syy = sxy = 0.0
        roi = None
        for _ in range(self.max_iter):
            hx = min(max(self.roi_factor * dx / 2, 3), cap) / b
            hy = min(max(self.roi_factor * dy / 2, 3), cap) / b
            bcx, bcy = (cx - X0 + 0.5) / b - 0.5, (cy - Y0 + 0.5) / b - 0.5
            i0, i1 = int(max(0, bcx - hx)), int(min(nx, bcx + hx + 1))
            j0, j1 = int(max(0, bcy - hy)), int(min(ny, bcy + hy + 1))
            if i1 - i0 < 2 or j1 - j0 < 2:
                return None
            roi = B[j0:j1, i0:i1].copy()
            cv2.threshold(roi, thr, 0, cv2.THRESH_TOZERO, dst=roi)
            m = cv2.moments(roi)
            if m["m00"] <= 0:
                return None
            ncx = X0 + (m["m10"] / m["m00"] + i0 + 0.5) * b - 0.5
            ncy = Y0 + (m["m01"] / m["m00"] + j0 + 0.5) * b - 0.5
            sxx = m["mu20"] / m["m00"] * b * b + corr
            syy = m["mu02"] / m["m00"] * b * b + corr
            sxy = m["mu11"] / m["m00"] * b * b
            ndx, ndy = 4 * np.sqrt(sxx), 4 * np.sqrt(syy)
            tol = 0.002 * max(ndx, ndy) + 0.05
            done = (abs(ndx - dx) < tol and abs(ndy - dy) < tol
                    and abs(ncx - cx) < tol and abs(ncy - cy) < tol)
            cx, cy, dx, dy = ncx, ncy, ndx, ndy
            if done:
                converged = True
                break
        self._last_roi = (X0 + i0 * b, X0 + i1 * b, Y0 + j0 * b, Y0 + j1 * b)
        peak = float(cv2.minMaxLoc(roi)[1])
        return cx, cy, dx, dy, sxx, syy, sxy, peak, converged, cap

    def _edge_fit(self, cx, cy, d_major):
        """Ellipse fit to steepest-descent points on rays from (cx, cy)."""
        B, X0, Y0, b, _ = self._region
        ny, nx = B.shape
        bcx, bcy = (cx - X0 + 0.5) / b - 0.5, (cy - Y0 + 0.5) / b - 0.5
        sm = max(1.0, 0.01 * d_major / b)          # light smoothing vs beam size
        Bs = cv2.GaussianBlur(B, (0, 0), sm)

        Rb = 1.25 * d_major / b                    # rays out to ~2.5x the radius
        step = 0.5
        t = np.arange(0, Rb, step, dtype=np.float32)
        phi = np.linspace(0, 2 * np.pi, self.n_rays, endpoint=False, dtype=np.float32)
        cph, sph = np.cos(phi)[:, None], np.sin(phi)[:, None]
        mapx = (bcx + t[None, :] * cph).astype(np.float32)
        mapy = (bcy + t[None, :] * sph).astype(np.float32)
        prof = cv2.remap(Bs, mapx, mapy, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=np.nan)
        g = -np.gradient(prof, step, axis=1)       # descent along each ray

        # Background level and noise of the smoothed profiles, measured on
        # the outer part of the rays (well outside the beam).
        tail = prof[:, int(0.8 * t.size):]
        tail = tail[np.isfinite(tail)]
        gt = g[:, int(0.8 * t.size):]
        gt = gt[np.isfinite(gt)]
        if tail.size > 50:
            bg_t = float(np.median(tail))
            sig_t = 1.4826 * float(np.median(np.abs(tail - bg_t))) + 1e-6
            sig_g = 1.4826 * float(np.median(np.abs(gt - np.median(gt)))) + 1e-6
        else:                                       # beam fills the region
            bg_t = 0.0
            sig_t = self._bg[3] / b / (2 * np.sqrt(np.pi) * sm) + 1e-6
            sig_g = sig_t
        level = bg_t + self.edge_k * sig_t          # "back to background"
        run = max(3, int(np.ceil(2 * sm / step)))   # must stay below for this long
        kernel = np.ones(run, dtype=np.float32)

        pts = []
        start = max(2, int(0.05 * Rb / step))      # skip the very centre
        for k in range(self.n_rays):
            pk = prof[k]
            fin = np.isfinite(pk)
            # 1) where does the beam END on this ray: first point (from the
            #    inside) where the profile drops to background and stays there.
            below = ((pk < level) & fin).astype(np.float32)
            runs = np.convolve(below, kernel, mode="valid") >= run
            idx = np.nonzero(runs[start:])[0]
            if idx.size == 0:
                continue                           # never reaches background
            j_end = start + int(idx[0])
            if not fin[start:j_end + 1].all():
                continue                           # ray left the region first
            # 2) the edge is the steepest drop just inside that point, so an
            #    internal bright/dim step further in is never picked.
            w = max(6, int(0.15 * (j_end - start)))
            lo_, hi_ = max(start, j_end - w), min(t.size - 2, j_end + 2)
            if hi_ - lo_ < 3:
                continue
            gw = g[k, lo_:hi_ + 1]
            if not np.isfinite(gw).all():
                continue
            j = lo_ + int(np.argmax(gw))
            if g[k, j] < 4 * sig_g or j <= start or j >= t.size - 1:
                continue                           # no clear edge on this ray
            y0_, y1_, y2_ = g[k, j - 1], g[k, j], g[k, j + 1]
            if not (np.isfinite(y0_) and np.isfinite(y2_)):
                continue
            den = y0_ - 2 * y1_ + y2_
            dj = 0.5 * (y0_ - y2_) / den if den < 0 else 0.0   # den<0: true peak
            tt = (j + np.clip(dj, -1, 1)) * step
            px_ = X0 + (bcx + tt * cph[k, 0] + 0.5) * b - 0.5
            py_ = Y0 + (bcy + tt * sph[k, 0] + 0.5) * b - 0.5
            pts.append((px_, py_))
        P = np.array(pts, dtype=np.float32).reshape(-1, 2)
        P = P[np.isfinite(P).all(axis=1)]
        if len(P) < 8:
            return None

        def fit(P):
            """Ellipse fit; None if both OpenCV fitters fail or give junk."""
            for fn in (cv2.fitEllipseDirect, cv2.fitEllipse):
                try:
                    (ex, ey), (w, h), a = fn(P)
                except cv2.error:
                    continue
                if np.all(np.isfinite([ex, ey, w, h, a])) and w > 0 and h > 0:
                    return (ex, ey, w, h, a) if w >= h else (ex, ey, h, w, a + 90.0)
            return None

        def resid(P, e):
            ex, ey, A, Bm, a = e
            th = np.radians(a)
            dx, dy = P[:, 0] - ex, P[:, 1] - ey
            u = dx * np.cos(th) + dy * np.sin(th)
            v = -dx * np.sin(th) + dy * np.cos(th)
            rho = np.sqrt((u / (A / 2)) ** 2 + (v / (Bm / 2)) ** 2)
            return (rho - 1) * np.sqrt(A * Bm) / 2          # ~ distance in px

        e = fit(P)
        if e is None:
            return None
        for _ in range(2):                                  # robust refit
            r = resid(P, e)
            mad = 1.4826 * np.median(np.abs(r)) + 1e-3
            keep = np.abs(r) < max(3 * mad, 0.5 * b)
            if keep.sum() < 8 or keep.all():
                break
            e2 = fit(P[keep])
            if e2 is None:
                break                                       # keep the previous fit
            P, e = P[keep], e2
        ex, ey, A, Bm, a = e
        a = (a + 90.0) % 180.0 - 90.0
        rms = float(np.sqrt(np.mean(resid(P, e) ** 2)))
        return dict(edge_cx_px=float(ex), edge_cy_px=float(ey), edge_major_px=float(A),
                    edge_minor_px=float(Bm), edge_angle_deg=float(a),
                    edge_points=int(len(P)), edge_rms_px=rms), Bs

    def _uniformity(self, e, Bs):
        """Linear tilt and residual non-uniformity inside 80 % of the edge."""
        B, X0, Y0, b, _ = self._region
        ny, nx = B.shape
        gx = X0 + (np.arange(nx, dtype=np.float32) + 0.5) * b - 0.5
        gy = Y0 + (np.arange(ny, dtype=np.float32) + 0.5) * b - 0.5
        GX, GY = np.meshgrid(gx - e["edge_cx_px"], gy - e["edge_cy_px"])
        th = np.radians(e["edge_angle_deg"])
        u = GX * np.cos(th) + GY * np.sin(th)
        v = -GX * np.sin(th) + GY * np.cos(th)
        rho = np.sqrt((u / (e["edge_major_px"] / 2)) ** 2 + (v / (e["edge_minor_px"] / 2)) ** 2)
        m = rho < 0.8
        if m.sum() < 10:
            return {}
        x, y, z = GX[m].astype(np.float64), GY[m].astype(np.float64), B[m].astype(np.float64)
        A = np.stack([np.ones_like(x), x, y], axis=1)
        (c0, cgx, cgy), *_ = np.linalg.lstsq(A, z, rcond=None)
        if c0 <= 0:
            return {}
        zs = Bs[m].astype(np.float64)
        res = zs - (c0 + cgx * x + cgy * y)
        return dict(tilt_pct=float(100 * np.hypot(cgx, cgy) * e["edge_major_px"] / c0),
                    tilt_dir_deg=float(np.degrees(np.arctan2(cgy, cgx))),
                    nonuniformity_pct=float(100 * np.std(res) / c0))

    # -- public --------------------------------------------------------------
    def process(self, frame: np.ndarray) -> Optional[BeamResult]:
        import time
        t = {}
        t0 = time.perf_counter()
        raw = _to_gray(frame)
        self._raw = raw
        H, W = raw.shape

        self._bg = self._fit_background(raw)
        t1 = time.perf_counter(); t["background"] = t1 - t0

        seed = self._coarse()
        t2 = time.perf_counter(); t["search"] = t2 - t1
        if seed is None:
            self.last_status, self.timings = "no beam", t
            return None
        it = self._iso_moments(*seed)
        t3 = time.perf_counter(); t["seed"] = t3 - t2
        if it is None:
            self.last_status, self.timings = "no beam", t
            return None
        cx, cy, dx, dy, sxx, syy, sxy, peak, converged, cap = it
        d_major, _, _ = _principal_diameters(sxx, syy, sxy)

        out = self._edge_fit(cx, cy, d_major)
        t4 = time.perf_counter(); t["edge"] = t4 - t3
        if out is None:
            self.last_status, self.timings = "edge not found", t
            return None
        e, Bs = out

        # Saturation: checked on the beam region only, after a 3x3 median so
        # isolated hot pixels (which often read full scale) don't trigger it.
        bd = self.bit_depth or (8 if raw.dtype == np.uint8 else 16)
        x0, x1, y0, y1 = self._last_roi
        crop = raw[y0:y1, x0:x1]
        if min(crop.shape) >= 3:
            crop = cv2.medianBlur(np.ascontiguousarray(crop), 3) if crop.dtype == np.uint8 \
                else cv2.medianBlur(crop.astype(np.float32), 3)
        saturated = cv2.minMaxLoc(crop)[1] >= 2 ** bd - 1

        ex, ey = e["edge_cx_px"], e["edge_cy_px"]
        A, Bm, ang = e["edge_major_px"], e["edge_minor_px"], e["edge_angle_deg"]
        res = BeamResult(
            cx_px=ex, cy_px=ey, major_px=A, minor_px=Bm, angle_deg=ang,
            ellipticity=float(Bm / A) if A > 0 else 0.0,
            diameter_px=float(np.sqrt(A * Bm)),
            edge_points=e["edge_points"], edge_rms_px=e["edge_rms_px"],
            centroid_x_px=float(cx), centroid_y_px=float(cy),
            centroid_offset_px=float(np.hypot(cx - ex, cy - ey)),
            peak_counts=peak, background_counts=self._bg[0],
            saturated=bool(saturated), detect_snr=self.last_detect_snr,
            bin_factor=self._region[3],
        )
        for k, v in self._uniformity(e, Bs).items():
            setattr(res, k, v)

        # edge ellipse running off the sensor? (half-extent of a rotated ellipse)
        th = np.radians(ang)
        hx = 0.5 * np.hypot(A * np.cos(th), Bm * np.sin(th))
        hy = 0.5 * np.hypot(A * np.sin(th), Bm * np.cos(th))
        res.clipped = bool(ex - hx < 0 or ey - hy < 0 or ex + hx > W - 1 or ey + hy > H - 1)

        if self.pixel_size_um is not None:
            p = self.pixel_size_um
            for name in ("cx", "cy", "major", "minor", "diameter", "centroid_x", "centroid_y",
                         "centroid_offset"):
                setattr(res, f"{name}_um", getattr(res, f"{name}_px") * p)

        self.last_status, self.timings = "ok", t
        return res


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------
# applyColorMap rebuilds its colour table on every call (~20 ms); build it once
_JET_LUT = cv2.applyColorMap(np.arange(256, dtype=np.uint8).reshape(256, 1),
                             cv2.COLORMAP_JET).reshape(256, 1, 3)


def _label(img, txt, org, col, scale=0.55, thick=1):
    """Text on a dark backing so it stays readable over the beam/noise."""
    (w, h), base = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
    x, y = org
    cv2.rectangle(img, (x - 3, y - h - 4), (x + w + 3, y + base + 2), (0, 0, 0), -1)
    cv2.putText(img, txt, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, col, thick, cv2.LINE_AA)


class PreviewAverager:
    """
    Motion-aware running average of the preview image, for display only.

    Pixels that stay within the noise are averaged over frames (smooth, quiet
    background); pixels that change by more than ~k x noise - the beam
    arriving or leaving - take the new value at once, so a moving beam leaves
    no trail.

    alpha : weight of the newest frame for steady pixels (1 = averaging off,
            0.3 = noise down ~2.4x).
    k : change (in noise sigmas) above which a pixel is treated as moving.
    """
    def __init__(self, alpha: float = 0.3, k: float = 3.0):
        self.alpha, self.k = alpha, k
        self._acc = None

    def reset(self):
        self._acc = None

    def __call__(self, img: np.ndarray, noise: float) -> np.ndarray:
        if self._acc is None or self._acc.shape != img.shape or self.alpha >= 1:
            self._acc = img.copy()
            return self._acc
        d = img - self._acc
        t = np.float32(self.k * noise + 1e-6)
        # 0 below k*noise (steady), ramps to 1 at 2k*noise (moving)
        w = np.clip((np.abs(d) - t) / t, 0.0, 1.0)
        w = np.float32(self.alpha) + np.float32(1 - self.alpha) * w
        self._acc += w * d
        return self._acc