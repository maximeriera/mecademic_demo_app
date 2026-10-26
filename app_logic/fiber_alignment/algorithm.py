"""Fiber alignment algorithm: find the light, centre on it, square the angle, climb to the peak.

Built for a demo cell that may start cold: the fiber can start where no light
reaches the meter, as long as the optimum is within ``acquire_radius_um``
laterally. It never moves the fiber along its axis: the gap stays the one the
``align_start`` program taught.

``align`` runs four phases, each inside ``with io.phase(name):`` and plotted by
the custom view as PHASE_VIEWS declares:

1. Acquire: the noise floor at the start, then, unless light already reaches
   the meter, an outward spiral in the lateral plane until it does, and the
   move to its best reading.
2. Center: two-way scans across the light along x then z, each fitted with a
   Gaussian; the fiber moves to the fitted centre until it no longer shifts.
   The two directions also measure what is left of the reading/pose lag.
3. Angle: for rx then rz, a few tilts around the current one, re-centred
   laterally at each, so that a TCP off the fiber tip (which turns a tilt into
   a shift) does not fool it. A parabola through the log of the peak power
   against the tilt gives the best tilt.
4. Peak: a 3×3 pattern of readings at rest around the centre, a quadratic fit
   of the log power and a Newton step to its top, until the steps are below
   the robot's resolution; then to the best reading taken at rest.

Positions are offsets from the start in the start's tool frame (see
``io.local`` / ``io.pose_at``): x and z lateral (µm), y along the fiber (never
moved), rx and rz rotations about the tool axes (degrees). Scan speeds come
from the measured time between readings (``io.sample_period_s``), so readings
are as dense as intended whatever the meter and the network do.

The contract with the cell (see alignment.py): move only through ``io``, never
catch exceptions broadly, leave the fiber at the best pose found. The custom
view plots the readings and what is published here with ``io.publish()``:
"acquire", "profile" and "peak" (their fields: custom_view/__init__.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import fit
from .alignment import AlignmentError, AlignmentIO, AlignmentNoSignal, AlignmentSettings, Sample
from .frames import Pose6

#: The phases, in order. The custom view lists them and plots each as PHASE_VIEWS says.
PHASES = ("Acquire", "Center", "Angle", "Peak")

#: How the custom view plots each phase: "map" is a 3D landscape of the power
#: over two axes, "profiles" the latest 1-D scan per axis (one plot per slot),
#: "peak" the pattern readings and the steps of the peak search. Axes are
#: io.local's: x, y, z (µm), rx, ry, rz (°).
PHASE_VIEWS = {
    "Acquire": {"type": "map", "axes": ["x", "z"]},
    "Center": {"type": "profiles", "slots": ["x", "z"]},
    "Angle": {"type": "profiles", "slots": ["rx", "rz", "x", "z"]},
    "Peak": {"type": "peak", "axes": ["x", "z"]},
}

# --- Meter ---------------------------------------------------------------------------
#: Samples averaged per reading (about 3 ms each on a PM100x): while scanning, and at rest.
SCAN_AVERAGING = 1
STILL_AVERAGING = 10
#: Fixed meter range of the scans, × the source power: no range switch mid-scan.
SCAN_RANGE = 1.2
#: Highest range tried when light overranges it (W).
MAX_RANGE_W = 1.0

# --- Motion ----------------------------------------------------------------------------
#: Point-to-point moves: to a scan's start, to a pattern point, to a tilt.
MOVE_MOTION = {"acc": 50, "lin_vel": 2.0, "ang_vel": 5.0, "blending": 0}
#: Slowest scan speed (mm/s).
MIN_SCAN_SPEED = 0.01

# --- Acquire -----------------------------------------------------------------------------
#: Readings at the start that give the noise floor.
NOISE_READS = 20
#: Light is a reading this many noise standard deviations above the floor, and
#: at least signal_detect_uw.
NOISE_SIGMAS = 8.0
#: A start already reading this fraction of the source is close: no spiral.
WARM_FRACTION = 0.01
#: Readings every pitch × SPIRAL_SPACING along the spiral; its path vertices at most this far apart (µm).
SPIRAL_SPACING = 0.5
SPIRAL_VERTEX_UM = 10.0
#: Moves queued per scan: the spiral runs as consecutive scans.
SPIRAL_CHUNK = 400
#: The spiral stops once the power falls under this fraction of the best since the light was found.
PAST_PEAK = 0.5
#: Room (mm) the window must leave around the spiral, for the centring scans.
WINDOW_MARGIN_MM = 0.1

# --- Center --------------------------------------------------------------------------------
#: 1/e² radius (µm) of the power against the lateral offset before the first
#: fit: single-mode fibers at a small gap.
WIDTH_PRIOR_UM = 10.0
#: Fitted widths are kept within these bounds (µm).
WIDTH_MIN_UM, WIDTH_MAX_UM = 1.0, 200.0
#: A cross scan spans ± SCAN_HALF_WIDTHS widths, with SCAN_DENSITY readings per width.
SCAN_HALF_WIDTHS = 2.5
SCAN_DENSITY = 8
SCAN_HALF_MIN_UM, SCAN_HALF_MAX_UM = 5.0, 250.0
SCAN_SPACING_MIN_UM = 0.25
#: A fitted centre within this fraction of the span from its ends is at the
#: edge: scan again around it, at most EDGE_RESCANS times.
EDGE_FRACTION = 0.15
EDGE_RESCANS = 2
#: Centring stops once the centre moves less than max(CENTER_TOL_WIDTHS widths, CENTER_TOL_MIN_UM).
CENTER_TOL_WIDTHS = 0.1
CENTER_TOL_MIN_UM = 0.5
CENTER_ITERATIONS = 3

# --- Angle -----------------------------------------------------------------------------------
#: Tilts per line search, across ± the span (degrees, halved each round).
ANGLE_POINTS = 5
ANGLE_SPAN_DEG = 1.5
ANGLE_ROUNDS = 2
#: A round gaining less than this (dB) ends the phase.
ANGLE_MIN_GAIN_DB = 0.1
#: Room (degrees) the window must leave around the tilts.
ANGLE_MARGIN_DEG = 0.5

# --- Peak --------------------------------------------------------------------------------------
#: Pattern radius: PEAK_RADIUS_WIDTHS × the mean width, at least PEAK_RADIUS_MIN_UM.
PEAK_RADIUS_WIDTHS = 0.5
PEAK_RADIUS_MIN_UM = 1.0
#: The search stops on a step under PEAK_MIN_STEP_UM (about the robot's
#: resolution), a gain under PEAK_MIN_GAIN_DB, or after PEAK_ITERATIONS.
PEAK_MIN_STEP_UM = 0.5
PEAK_MIN_GAIN_DB = 0.05
PEAK_ITERATIONS = 6
#: The 3×3 pattern in pattern radii, in an order that needs no long moves.
PATTERN = ((-1, -1), (0, -1), (1, -1), (1, 0), (0, 0), (-1, 0), (-1, 1), (0, 1), (1, 1))

#: Index of each lateral axis in io.local's offsets.
LOCAL_INDEX = {"x": 0, "z": 2}


@dataclass
class _State:
    """What the phases learn and hand on, in io.local's coordinates."""

    settings: AlignmentSettings
    #: The best estimate of the optimum so far: x, z (µm), rx, rz (°).
    pos: Dict[str, float] = field(default_factory=lambda: {"x": 0.0, "z": 0.0, "rx": 0.0, "rz": 0.0})
    #: 1/e² radius (µm) of the power against x and z.
    width: Dict[str, float] = field(default_factory=lambda: {"x": WIDTH_PRIOR_UM, "z": WIDTH_PRIOR_UM})
    #: The power expected at pos (W).
    peak_w: float = 0.0
    #: Fixed range of the scans (W); raised if light overranges it.
    scan_range_w: float = 0.0
    #: Direction of the last one-way scan.
    flip: bool = False
    # Meter and motion settings in force, so that only changes are sent.
    averaging: Optional[int] = None
    range_w: Optional[float] = -1.0
    motion: Dict[str, float] = field(default_factory=dict)

    def pose(self, io: AlignmentIO, **override: float) -> Pose6:
        p = {**self.pos, **override}
        return io.pose_at(x=p["x"], z=p["z"], rx=p["rx"], rz=p["rz"])


def align(io: AlignmentIO, settings: AlignmentSettings) -> None:
    if settings.acquire_radius_mm + WINDOW_MARGIN_MM > settings.range_mm:
        raise AlignmentError(
            f"acquire_radius_um ({settings.acquire_radius_mm * 1e3:g}) needs {WINDOW_MARGIN_MM * 1e3:g} µm of room "
            f"inside align_range_um ({settings.range_mm * 1e3:g}): lower it, or widen the window.")
    if ANGLE_SPAN_DEG + ANGLE_MARGIN_DEG > settings.range_deg:
        raise AlignmentError(f"align_range_deg ({settings.range_deg:g}) leaves no room for the "
                             f"±{ANGLE_SPAN_DEG:g}° tilts: widen it to {ANGLE_SPAN_DEG + ANGLE_MARGIN_DEG:g}° or more.")
    state = _State(settings, scan_range_w=SCAN_RANGE * settings.source_power_w)
    with io.phase("Acquire"):
        acquire(io, state)
    with io.phase("Center"):
        center(io, state)
    with io.phase("Angle"):
        angle(io, state)
    with io.phase("Peak"):
        peak(io, state)


# --- Acquire -------------------------------------------------------------------------------

def generate_archimedean_spiral(center=(0.0, 0.0), max_radius=0.5, spiral_pitch=0.002, point_spacing=0.001):
    """
    Generates a 2-DOF Archimedean spiral path with uniform point spacing.

    Parameters:
    -----------
    center : tuple of float
        (axis1_center, axis2_center) center of the search space.
    max_radius : float
        Maximum radial distance to search from origin.
    spiral_pitch : float
        Radial distance added per 360-degree loop (p).
    point_spacing : float
        Linear arc length distance between consecutive points (ds).

    Returns:
    --------
    np.ndarray : Array of shape (N, 2) containing (axis1, axis2) coordinates.
    """
    c1_0, c2_0 = center
    b = spiral_pitch / (2.0 * np.pi)
    if b <= 0:
        raise ValueError("spiral_pitch must be greater than 0.")
    theta_max = max_radius / b

    # Iteratively calculate theta values to preserve constant arc length
    theta_list = [0.0]
    while theta_list[-1] < theta_max:
        r_curr = b * theta_list[-1]
        # Prevent division by zero near the origin by clamping the minimum radius
        d_theta = point_spacing / max(r_curr, point_spacing)
        theta_list.append(theta_list[-1] + d_theta)

    thetas = np.array(theta_list)
    r = b * thetas
    return np.column_stack((c1_0 + r * np.cos(thetas), c2_0 + r * np.sin(thetas)))


def acquire(io: AlignmentIO, state: _State) -> None:
    """The noise floor, then a spiral until light shows, unless it already does; ends on the best reading."""
    s = state.settings
    _averaging(io, state, SCAN_AVERAGING)
    _range(io, state, None)
    noise, sigma = fit.noise_stats([io.read_power("cart") for _ in range(NOISE_READS)])
    detect = max(s.signal_detect_w, noise + NOISE_SIGMAS * sigma) if math.isfinite(noise) else s.signal_detect_w
    warm = math.isfinite(noise) and noise >= WARM_FRACTION * s.source_power_w
    radius_um, pitch_um = s.acquire_radius_mm * 1e3, s.acquire_pitch_mm * 1e3
    info = {"noise_w": noise, "sigma_w": sigma, "detect_w": detect, "radius_um": radius_um, "pitch_um": pitch_um,
            "warm": warm}
    if warm:
        state.peak_w = noise
        io.publish("acquire", {**info, "found": True, "at": [0.0, 0.0], "power_w": noise})
        return
    io.publish("acquire", {**info, "found": False})

    spacing_um = pitch_um * SPIRAL_SPACING
    path = generate_archimedean_spiral(max_radius=radius_um, spiral_pitch=pitch_um,
                                       point_spacing=min(SPIRAL_VERTEX_UM, spacing_um))
    _motion(io, state, acc=MOVE_MOTION["acc"], lin_vel=_scan_speed(io, spacing_um), blending=100)
    found: Dict[str, Optional[Sample]] = {"best": None}
    stopped = {"done": False}

    def past_peak(samples: List[Sample]) -> bool:
        sample = samples[-1]
        # An overrange (+inf) is light too.
        if sample.power_w >= detect and (found["best"] is None or sample.power_w > found["best"].power_w):
            found["best"] = sample
        stopped["done"] = found["best"] is not None and sample.power_w < PAST_PEAK * found["best"].power_w
        return stopped["done"]

    # The path starts at the start pose, where the fiber is.
    for first in range(1, len(path), SPIRAL_CHUNK):
        io.scan([io.pose_at(x=u, z=v) for u, v in path[first:first + SPIRAL_CHUNK]], kind="cart", stop=past_peak)
        if stopped["done"]:
            break
    best = found["best"]
    if best is None:
        raise AlignmentNoSignal(
            f"No light above {detect:.3g} W within {radius_um:g} µm of the start (noise {noise:.3g} W): "
            f"check the laser, the fibers and the align_start pose.")
    x, _, z, _, _, _ = io.local(best.pose)
    state.pos.update(x=x, z=z)
    _goto(io, state)
    state.peak_w = _still(io, state, "cart")
    io.publish("acquire", {**info, "found": True, "at": [round(x, 3), round(z, 3)], "power_w": state.peak_w})


# --- Center --------------------------------------------------------------------------------

def center(io: AlignmentIO, state: _State) -> None:
    """Cross scans along x and z until the fitted centre stops moving; ends there, at rest."""
    for iteration in range(CENTER_ITERATIONS):
        x0, z0 = state.pos["x"], state.pos["z"]
        for axis in ("x", "z"):
            cross_scan(io, state, axis, iteration)
        tolerance = max(CENTER_TOL_MIN_UM, CENTER_TOL_WIDTHS * min(state.width.values()))
        if math.hypot(state.pos["x"] - x0, state.pos["z"] - z0) < tolerance:
            break
    _goto(io, state)
    state.peak_w = _still(io, state, "cart")


def cross_scan(io: AlignmentIO, state: _State, axis: str, iteration: int, two_way: bool = True) -> fit.Gauss:
    """Scan across the light along ``axis`` ("x" or "z"), and move pos[axis] to the fitted centre.

    Both ways by default: the readings of each direction are fitted on their
    own, their centres differ by twice what remains of the reading/pose lag
    ("lag" in the profile), and their mean is free of it. One way (each time
    the other way round) when only the peak power matters most.
    """
    index = LOCAL_INDEX[axis]
    width = state.width[axis]
    half = min(max(SCAN_HALF_WIDTHS * width, SCAN_HALF_MIN_UM), SCAN_HALF_MAX_UM)
    spacing = max(width / SCAN_DENSITY, SCAN_SPACING_MIN_UM)
    middle = state.pos[axis]
    attempts = EDGE_RESCANS + 1
    attempt = 0
    while True:
        lo, hi = middle - half, middle + half
        if two_way:
            legs = ((1, hi), (-1, lo))
        else:
            state.flip = not state.flip
            legs = ((-1, lo),) if state.flip else ((1, hi),)
        _averaging(io, state, SCAN_AVERAGING)
        _range(io, state, state.scan_range_w)
        _goto(io, state, **{axis: hi if legs[0][0] < 0 else lo})
        _motion(io, state, lin_vel=_scan_speed(io, spacing), blending=0)
        passes = []
        for direction, end in legs:
            samples = io.scan([state.pose(io, **{axis: end})], kind="cart")
            passes.append((direction, [(io.local(s.pose)[index], s.power_w) for s in samples]))
        if any(math.isinf(p) for _, points in passes for _, p in points):
            # More light than the range: a range ten times higher, and again.
            state.scan_range_w *= 10.0
            if state.scan_range_w > MAX_RANGE_W:
                raise AlignmentError(f"The power meter overranges at {state.scan_range_w / 10:.3g} W: "
                                     f"check source_power_mw and the meter.")
            continue
        fits = [fit.gauss1d(*zip(*points)) if len(points) >= 4 else None for _, points in passes]
        everything = [pt for _, points in passes for pt in points]
        both = fit.gauss1d(*zip(*everything)) if len(everything) >= 4 else None
        if len(fits) == 2 and all(fits):
            forward, backward = fits
            found = fit.Gauss(center=0.5 * (forward.center + backward.center),
                              width=0.5 * (forward.width + backward.width),
                              peak=0.5 * (forward.peak + backward.peak),
                              baseline=0.5 * (forward.baseline + backward.baseline))
            lag = 0.5 * (forward.center - backward.center)
        else:
            found, lag = both, None
        attempt += 1
        if found is None:
            chosen = None
        else:
            chosen = found.center
        edge = chosen is None or not (lo + EDGE_FRACTION * (hi - lo) <= chosen <= hi - EDGE_FRACTION * (hi - lo))
        io.publish("profile", {
            "axis": axis,
            "iteration": iteration,
            "rescan": attempt > 1,
            "unit": "µm",
            "span": [round(lo, 3), round(hi, 3)],
            "passes": [{"dir": d, "points": [[round(c, 3), p] for c, p in points]} for d, points in passes],
            "fit": None if found is None else {
                "model": "gauss", "center": round(found.center, 3), "width": round(found.width, 3),
                "peak": found.top, "baseline": found.baseline, "lag": None if lag is None else round(lag, 3),
            },
            "chosen": None if chosen is None or edge else round(chosen, 3),
        })
        if not edge or attempt >= attempts:
            break
        if found is None:
            # No peak in the span: around its best reading, twice as wide.
            points = [pt for _, pts in passes for pt in pts if math.isfinite(pt[1])]
            middle = max(points, key=lambda pt: pt[1])[0] if points else middle
            half = min(2 * half, SCAN_HALF_MAX_UM)
        else:
            # The peak is at (or past) an end: centre the next scan on it.
            middle = min(max(found.center, lo - half), hi + half)
    if found is None:
        raise AlignmentNoSignal(f"Lost the light while centring along {axis}: no peak within ±{half:.1f} µm "
                                f"of {middle:+.1f} µm. Check the laser and the fibers.")
    state.pos[axis] = found.center
    state.width[axis] = min(max(found.width, WIDTH_MIN_UM), WIDTH_MAX_UM)
    state.peak_w = found.top
    return found


# --- Angle ----------------------------------------------------------------------------------

def angle(io: AlignmentIO, state: _State) -> None:
    """Tilt line searches in rx then rz, re-centred at each tilt; again, narrower, while it gains."""
    span = ANGLE_SPAN_DEG
    for round_ in range(ANGLE_ROUNDS):
        before = state.peak_w
        for axis in ("rx", "rz"):
            tilt_search(io, state, axis, span, round_)
        if _db(state.peak_w, before) < ANGLE_MIN_GAIN_DB:
            break
        span /= 2
    _goto(io, state)
    state.peak_w = _still(io, state, "cart")


def tilt_search(io: AlignmentIO, state: _State, axis: str, span: float, round_: int) -> None:
    """Try tilts around pos[axis], each re-centred laterally; go to the tilt a parabola through log power picks."""
    limit = state.settings.range_deg - ANGLE_MARGIN_DEG
    middle = state.pos[axis]
    tilts = sorted({round(min(max(middle + d, -limit), limit), 6) for d in np.linspace(-span, span, ANGLE_POINTS)})
    tried = []  # (tilt, peak power re-centred, x, z)
    for tilt in tilts:
        state.pos[axis] = tilt
        # One way: the peak power is what counts here, the centre is scanned
        # both ways once the tilt is chosen.
        for lateral in ("x", "z"):
            cross_scan(io, state, lateral, round_, two_way=False)
        tried.append((tilt, state.peak_w, state.pos["x"], state.pos["z"]))
    parabola = fit.parabola1d([t[0] for t in tried], [t[1] for t in tried])
    if parabola is not None and parabola.curvature < 0 and math.isfinite(parabola.vertex):
        chosen = min(max(parabola.vertex, tilts[0]), tilts[-1])
    else:
        chosen = max(tried, key=lambda t: t[1])[0]
    # The lateral centre moves with the tilt when the TCP is not at the fiber
    # tip: start the re-centring where the tilts say it is, then scan.
    state.pos[axis] = chosen
    state.pos["x"] = float(np.interp(chosen, [t[0] for t in tried], [t[2] for t in tried]))
    state.pos["z"] = float(np.interp(chosen, [t[0] for t in tried], [t[3] for t in tried]))
    for lateral in ("x", "z"):
        cross_scan(io, state, lateral, round_)
    # How far the lateral centre moves per radian of tilt (a tilt about x
    # moves it along z): about the gap with the TCP at the fiber tip, far more
    # when it is not there.
    moved = [-t[3] for t in tried] if axis == "rx" else [t[2] for t in tried]
    radians = [math.radians(t[0]) for t in tried]
    pivot = float(np.polyfit(radians, moved, 1)[0]) if len(tried) >= 2 and np.ptp(radians) > 0 else None
    io.publish("profile", {
        "axis": axis,
        "iteration": round_,
        "rescan": False,
        "unit": "°",
        "span": [tilts[0], tilts[-1]],
        "passes": [{"dir": 1, "points": [[t[0], t[1]] for t in tried]}],
        "fit": None if parabola is None or not parabola.curvature < 0 else {
            "model": "parabola", "center": round(parabola.vertex, 5), "width": round(parabola.width, 5),
            "peak": parabola.peak,
        },
        "chosen": round(chosen, 5),
        "pivot_um": None if pivot is None else round(pivot, 1),
    })


# --- Peak -----------------------------------------------------------------------------------

def peak(io: AlignmentIO, state: _State) -> None:
    """Newton steps on a quadratic fit of log power over a 3×3 pattern at rest; ends on the best reading at rest."""
    radius = max(PEAK_RADIUS_MIN_UM, PEAK_RADIUS_WIDTHS * 0.5 * (state.width["x"] + state.width["z"]))
    _range(io, state, None)
    previous = None
    for iteration in range(PEAK_ITERATIONS):
        cx, cz = state.pos["x"], state.pos["z"]
        readings = []
        for i, j in PATTERN:
            _goto(io, state, x=cx + i * radius, z=cz + j * radius)
            readings.append((i * radius, j * radius, _still(io, state, "cart")))
        middle = readings[PATTERN.index((0, 0))][2]
        (du, dz), newton = fit.quad2d_step(readings, radius)
        step = math.hypot(du, dz)
        io.publish("peak", {
            "iter": iteration,
            "center": [round(cx, 3), round(cz, 3)],
            "radius_um": round(radius, 3),
            "step": [round(du, 3), round(dz, 3)],
            "step_um": round(step, 3),
            "newton": newton,
            "power_w": middle,
            "points": [[round(cx + u, 3), round(cz + v, 3), p] for u, v, p in readings],
        })
        state.pos.update(x=cx + du, z=cz + dz)
        gain = _db(middle, previous) if previous else math.inf
        previous = middle
        if step < PEAK_MIN_STEP_UM or gain < PEAK_MIN_GAIN_DB:
            break
    _goto(io, state)
    state.peak_w = _still(io, state, "cart")
    best = io.best_still
    if best is not None and best.power_w > state.peak_w:
        _motion(io, state, **MOVE_MOTION)
        io.move_to(best.pose)


# --- Helpers ---------------------------------------------------------------------------------

def _goto(io: AlignmentIO, state: _State, **override: float) -> None:
    """Point-to-point to pos, with ``override`` coordinates."""
    _motion(io, state, **MOVE_MOTION)
    io.move_to(state.pose(io, **override))


def _still(io: AlignmentIO, state: _State, kind: str) -> float:
    """A reading at rest, averaged. A scan's fixed range may be too low for it: then in auto range."""
    _averaging(io, state, STILL_AVERAGING)
    power = io.read_still(kind)
    if math.isinf(power):
        _range(io, state, None)
        power = io.read_still(kind)
    return power


def _scan_speed(io: AlignmentIO, spacing_um: float) -> float:
    """Scan speed (mm/s) that leaves ``spacing_um`` between two readings."""
    return max(MIN_SCAN_SPEED, spacing_um * 1e-3 / io.sample_period_s)


def _motion(io: AlignmentIO, state: _State, **motion: float) -> None:
    changed = {k: v for k, v in motion.items() if state.motion.get(k) != v}
    if changed:
        io.set_motion(**changed)
        state.motion.update(changed)


def _averaging(io: AlignmentIO, state: _State, count: int) -> None:
    if state.averaging != count:
        io.set_power_averaging(count)
        state.averaging = count


def _range(io: AlignmentIO, state: _State, watts: Optional[float]) -> None:
    """A fixed meter range (W), or auto range with None."""
    if state.range_w != watts:
        io.set_power_range(watts)
        state.range_w = watts


def _db(power: float, reference: Optional[float]) -> float:
    """``power`` against ``reference`` in dB (-inf if either is not positive)."""
    if not reference or not power or power <= 0 or reference <= 0:
        return -math.inf
    return 10 * math.log10(power / reference)
