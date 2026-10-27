"""Fiber alignment algorithm: find the light, centre on it, square the angle, climb to the peak.

Built for a demo cell that may start cold, with collimated (or lensed) fiber
ends: the coupling tolerates a lateral offset of a few hundred µm but a tilt
of a few tenths of a degree only, so first light is mostly a matter of angle.
It never moves the fiber along its axis: the gap stays the one the
``align_start`` program taught.

How wide the coupling is, against a lateral offset and against a tilt, sets
every scale here (scan spans, steps, spiral pitches). The params give starting
guesses (``lateral_width_um``, ``angle_width_deg``); each fit re-measures them.

``align`` runs four phases, each inside ``with io.phase(name):`` and plotted by
the custom view as PHASE_VIEWS declares:

1. Acquire: the noise floor at the start. Unless light already reaches the
   meter, a spiral in tilt (Rx/Rz) around the start, then, still dark, a
   spiral in the lateral plane, each until light shows; then the move to the
   best reading.
2. Center: two-way scans across the light along x then z, each fitted with a
   Gaussian over the noise floor; the fiber moves to the fitted centre until
   it no longer shifts. The two directions also measure what is left of the
   reading/pose lag.
3. Angle: for rx then rz, tilts stepping away from the current one while the
   power rises, each re-centred along the lateral axis the tilt walks the
   beam on. A parabola through the log of the power at the best tilt and its
   two neighbours gives the tilt to keep.
4. Peak: a 3×3 pattern of readings at rest around the centre, a quadratic fit
   of the log power and a Newton step to its top, until the steps are small;
   then to the best reading taken at rest.

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
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import fit
from .alignment import AlignmentError, AlignmentIO, AlignmentNoSignal, AlignmentSettings, Sample
from .frames import Pose6

#: The phases, in order. The custom view lists them and plots each as PHASE_VIEWS says.
PHASES = ("Acquire", "Center", "Angle", "Peak")

#: How the custom view plots each phase: "map" is a 3D landscape of the power
#: over two axes (for Acquire, the axes of the search that ran: "search_axes"),
#: "profiles" the latest 1-D scan per axis (one plot per slot), "peak" the
#: pattern readings and the steps of the peak search. Axes are io.local's:
#: x, y, z (µm), rx, ry, rz (°).
PHASE_VIEWS = {
    "Acquire": {"type": "map", "axes": ["x", "z"], "search_axes": {"angle": ["rx", "rz"], "lateral": ["x", "z"]}},
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
#: Slowest scan speeds (mm/s, °/s).
MIN_SCAN_SPEED = 0.01
MIN_SCAN_ANG_SPEED = 0.01

# --- Widths ------------------------------------------------------------------------------
#: A fitted width changes at most by this factor per fit, and stays within these bounds.
WIDTH_CHANGE = 3.0
LATERAL_WIDTH_BOUNDS_UM = (1.0, 2000.0)
ANGLE_WIDTH_BOUNDS_DEG = (0.005, 10.0)

# --- Acquire -----------------------------------------------------------------------------
#: Readings at the start that give the noise floor.
NOISE_READS = 20
#: Light is a reading this many noise standard deviations above the floor, and
#: at least signal_detect_uw.
NOISE_SIGMAS = 8.0
#: Spiral pitch, in widths, and readings per pitch along the spiral.
SPIRAL_PITCH_WIDTHS = 1.5
SPIRAL_READINGS_PER_PITCH = 3
#: Moves queued per scan: a spiral runs as consecutive scans.
SPIRAL_CHUNK = 400
#: A spiral stops once the power falls under this fraction of the best since the light was found.
PAST_PEAK = 0.5
#: Room the window must leave around the searches (mm, degrees).
WINDOW_MARGIN_MM = 0.1
ANGLE_MARGIN_DEG = 0.5

# --- Center --------------------------------------------------------------------------------
#: A cross scan spans ± SCAN_HALF_WIDTHS widths, with SCAN_DENSITY readings per width.
SCAN_HALF_WIDTHS = 1.5
SCAN_DENSITY = 10
SCAN_SPACING_MIN_UM = 0.25
#: A fitted centre within this fraction of the span from its ends (or past
#: them) is at the edge: the window moves towards it, by at most a half-span,
#: at most EDGE_RESCANS times. A window without light doubles, at most
#: DARK_WIDENINGS times (TILT_DARK_WIDENINGS when re-centring after a tilt).
EDGE_FRACTION = 0.15
EDGE_RESCANS = 3
DARK_WIDENINGS = 3
TILT_DARK_WIDENINGS = 2
#: Centring stops once the centre moves less than max(CENTER_TOL_WIDTHS widths, CENTER_TOL_MIN_UM).
CENTER_TOL_WIDTHS = 0.05
CENTER_TOL_MIN_UM = 0.5
CENTER_ITERATIONS = 3

# --- Angle -----------------------------------------------------------------------------------
#: The lateral axis a tilt walks the beam along.
TILT_WALK = {"rx": "z", "rz": "x"}
#: First tilt step, in angle widths (halved each round), and its growth while the power rises.
TILT_STEP_WIDTHS = 0.5
TILT_GROWTH = 1.5
#: Most tilts tried per line search.
TILT_MAX = 10
#: Rounds of rx then rz, while a round gains at least ANGLE_MIN_GAIN_DB.
ANGLE_ROUNDS = 3
ANGLE_MIN_GAIN_DB = 0.1

# --- Peak --------------------------------------------------------------------------------------
#: Pattern radius: PEAK_RADIUS_WIDTHS × the mean width, at least PEAK_RADIUS_MIN_UM.
PEAK_RADIUS_WIDTHS = 0.5
PEAK_RADIUS_MIN_UM = 1.0
#: The search stops on a step under max(PEAK_MIN_STEP_UM, PEAK_MIN_STEP_WIDTHS widths)
#: (about the robot's resolution, or nothing that matters), a gain under PEAK_MIN_GAIN_DB,
#: or after PEAK_ITERATIONS.
PEAK_MIN_STEP_UM = 0.5
PEAK_MIN_STEP_WIDTHS = 0.01
PEAK_MIN_GAIN_DB = 0.05
PEAK_ITERATIONS = 6
#: The 3×3 pattern in pattern radii, in an order that needs no long moves.
PATTERN = ((-1, -1), (0, -1), (1, -1), (1, 0), (0, 0), (-1, 0), (-1, 1), (0, 1), (1, 1))

#: Index of each axis in io.local's offsets.
LOCAL_INDEX = {"x": 0, "z": 2, "rx": 3, "rz": 5}


@dataclass
class _State:
    """What the phases learn and hand on, in io.local's coordinates."""

    settings: AlignmentSettings
    #: The best estimate of the optimum so far: x, z (µm), rx, rz (°).
    pos: Dict[str, float] = field(default_factory=lambda: {"x": 0.0, "z": 0.0, "rx": 0.0, "rz": 0.0})
    #: 1/e² radius (µm) of the power against x and z.
    width: Dict[str, float] = field(default_factory=dict)
    #: 1/e half-width (°) of the power against a tilt.
    angle_width: float = 0.0
    #: How far the beam walks along the lateral axis per degree of rx / rz (µm/°), once measured.
    walk: Dict[str, float] = field(default_factory=dict)
    #: The power expected at pos (W).
    peak_w: float = 0.0
    #: The meter's reading without light (W), and the lowest reading counted as light.
    baseline_w: float = 0.0
    detect_w: float = 0.0
    #: Fixed range of the scans (W); raised if light overranges it.
    scan_range_w: float = 0.0
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
    if settings.acquire_radius_deg + ANGLE_MARGIN_DEG > settings.range_deg:
        raise AlignmentError(
            f"acquire_radius_deg ({settings.acquire_radius_deg:g}) needs {ANGLE_MARGIN_DEG:g}° of room inside "
            f"align_range_deg ({settings.range_deg:g}): lower it, or widen the window.")
    lateral = settings.lateral_width_mm * 1e3
    state = _State(settings, width={"x": lateral, "z": lateral}, angle_width=settings.angle_width_deg,
                   scan_range_w=SCAN_RANGE * settings.source_power_w)
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
    """The noise floor; unless light shows already, a tilt spiral, then a lateral one; ends on the best reading."""
    s = state.settings
    _averaging(io, state, SCAN_AVERAGING)
    _range(io, state, None)
    noise, sigma = fit.noise_stats([io.read_power("cart") for _ in range(NOISE_READS)])
    warm = math.isfinite(noise) and noise >= s.signal_detect_w
    if warm:
        # Light already: the noise floor is unknown, and negligible against it.
        state.baseline_w, state.detect_w = 0.0, s.signal_detect_w
    else:
        state.baseline_w = noise if math.isfinite(noise) else 0.0
        state.detect_w = max(s.signal_detect_w, noise + NOISE_SIGMAS * sigma) if math.isfinite(noise) else s.signal_detect_w
    info = {"noise_w": noise, "sigma_w": sigma, "detect_w": state.detect_w, "warm": warm}
    if warm:
        state.peak_w = noise
        io.publish("acquire", {**info, "search": None, "found": True, "at": [0.0, 0.0], "power_w": noise})
        return
    for search in ("angle", "lateral"):
        if spiral(io, state, search, info):
            return
    raise AlignmentNoSignal(
        f"No light above {state.detect_w:.3g} W (noise {noise:.3g} W) tilting ±{s.acquire_radius_deg:g}° "
        f"nor moving ±{s.acquire_radius_mm * 1e3:g} µm around the start: check the laser, the fibers "
        f"and the align_start pose.")


def spiral(io: AlignmentIO, state: _State, search: str, info: Dict) -> bool:
    """A spiral in tilt ("angle": rx/rz) or in the lateral plane ("lateral": x/z) around pos, until light shows.

    Ends on its best reading, at rest. False if it found no light.
    """
    s = state.settings
    if search == "angle":
        axes, radius, width, unit, kind = ("rx", "rz"), s.acquire_radius_deg, state.angle_width, "°", "ang"
    else:
        axes, radius, width, unit, kind = ("x", "z"), s.acquire_radius_mm * 1e3, state.width["x"], "µm", "cart"
    pitch = SPIRAL_PITCH_WIDTHS * width
    spacing = pitch / SPIRAL_READINGS_PER_PITCH
    payload = {**info, "search": search, "radius": radius, "pitch": round(pitch, 5), "unit": unit}
    io.publish("acquire", {**payload, "found": False})
    centre = (state.pos[axes[0]], state.pos[axes[1]])
    path = generate_archimedean_spiral(centre, max_radius=radius, spiral_pitch=pitch, point_spacing=spacing)
    if search == "angle":
        _motion(io, state, acc=MOVE_MOTION["acc"], ang_vel=_scan_ang_speed(io, spacing), blending=100)
    else:
        _motion(io, state, acc=MOVE_MOTION["acc"], lin_vel=_scan_speed(io, spacing), blending=100)
    found: Dict[str, Optional[Sample]] = {"best": None}
    stopped = {"done": False}

    def past_peak(samples: List[Sample]) -> bool:
        sample = samples[-1]
        # An overrange (+inf) is light too.
        if sample.power_w >= state.detect_w and (found["best"] is None or sample.power_w > found["best"].power_w):
            found["best"] = sample
        stopped["done"] = found["best"] is not None and sample.power_w < PAST_PEAK * found["best"].power_w
        return stopped["done"]

    # The path starts where the fiber is.
    for first in range(1, len(path), SPIRAL_CHUNK):
        targets = [state.pose(io, **{axes[0]: u, axes[1]: v}) for u, v in path[first:first + SPIRAL_CHUNK]]
        io.scan(targets, kind=kind, stop=past_peak)
        if stopped["done"]:
            break
    best = found["best"]
    if best is None:
        return False
    offsets = io.local(best.pose)
    state.pos.update({axis: offsets[LOCAL_INDEX[axis]] for axis in axes})
    _goto(io, state)
    state.peak_w = _still(io, state, kind)
    io.publish("acquire", {**payload, "found": True,
                           "at": [round(state.pos[axes[0]], 5), round(state.pos[axes[1]], 5)],
                           "power_w": state.peak_w})
    return True


# --- Center --------------------------------------------------------------------------------

def center(io: AlignmentIO, state: _State) -> None:
    """Cross scans along x and z until the fitted centre stops moving; ends there, at rest."""
    for iteration in range(CENTER_ITERATIONS):
        x0, z0 = state.pos["x"], state.pos["z"]
        for axis in ("x", "z"):
            if not cross_scan(io, state, axis, iteration):
                raise AlignmentNoSignal(f"Lost the light while centring along {axis}: no reading above "
                                        f"{state.detect_w:.3g} W. Check the laser and the fibers.")
        tolerance = max(CENTER_TOL_MIN_UM, CENTER_TOL_WIDTHS * min(state.width.values()))
        if math.hypot(state.pos["x"] - x0, state.pos["z"] - z0) < tolerance:
            break
    _goto(io, state)
    state.peak_w = _still(io, state, "cart")


def cross_scan(io: AlignmentIO, state: _State, axis: str, iteration: int,
               widenings: int = DARK_WIDENINGS) -> bool:
    """Scan across the light along ``axis`` ("x" or "z") both ways, and move pos[axis] to its centre.

    Each direction's readings are fitted with a Gaussian over the noise floor
    (exact in log power, so the centre and the width come out right even from
    a window narrower than the beam). Their centres differ by twice what
    remains of the reading/pose lag ("lag" in the profile), and their mean is
    free of it. A centre at or past the window's edge moves the window towards
    it by at most a half-span, and scans again; if no window catches the
    centre, the fiber goes to the best reading, never to an extrapolated
    centre. The width is updated from a centre caught inside a window only.
    A window without light doubles around the same middle, ``widenings``
    times at most.

    Returns False when no reading reaches the detection level (pos unchanged):
    no light along this line.
    """
    width = state.width[axis]
    half = SCAN_HALF_WIDTHS * width
    spacing = max(width / SCAN_DENSITY, SCAN_SPACING_MIN_UM)
    limit = _lateral_limit(state, axis)
    middle = state.pos[axis]
    best: Optional[Tuple[float, float]] = None  # (coordinate, power) of the best reading of all windows
    caught: Optional[fit.Gauss] = None
    dark = 0
    for attempt in range(EDGE_RESCANS + widenings + 1):
        lo, hi = max(middle - half, -limit), min(middle + half, limit)
        passes = _scan_both_ways(io, state, axis, lo, hi, spacing)
        points = [pt for _, pts in passes for pt in pts if math.isfinite(pt[1])]
        top = max(points, key=lambda pt: pt[1]) if points else None
        if top is not None and (best is None or top[1] > best[1]):
            best = top
        light = top is not None and top[1] >= state.detect_w
        found, lag = _fit_passes(passes, state.baseline_w) if light else (None, None)
        inside = found is not None and lo + EDGE_FRACTION * (hi - lo) <= found.center <= hi - EDGE_FRACTION * (hi - lo)
        io.publish("profile", {
            "axis": axis,
            "iteration": iteration,
            "rescan": attempt > 0,
            "unit": "µm",
            "span": [round(lo, 3), round(hi, 3)],
            "passes": [{"dir": d, "points": [[round(c, 3), p] for c, p in pts]} for d, pts in passes],
            "fit": None if found is None else {
                "model": "gauss", "center": round(found.center, 3), "width": round(found.width, 3),
                "peak": found.top, "baseline": found.baseline, "lag": None if lag is None else round(lag, 3),
            },
            "chosen": round(found.center, 3) if inside else None,
        })
        if inside:
            caught = found
            break
        if not light:
            if best is not None and best[1] >= state.detect_w:
                break  # an earlier window had the light: settle there
            dark += 1
            if dark > widenings:
                break
            half *= 2  # no light here: a wider window around the same middle
            continue
        if attempt - dark >= EDGE_RESCANS:
            break
        # Towards the fitted centre (else the best reading), by at most a half-span.
        target = found.center if found is not None else top[0]
        middle += max(-half, min(half, target - middle))
    if caught is not None:
        state.pos[axis] = caught.center
        state.width[axis] = _new_width(state.width[axis], caught.width, LATERAL_WIDTH_BOUNDS_UM)
        state.peak_w = caught.top
        return True
    if best is not None and best[1] >= state.detect_w:
        state.pos[axis] = best[0]
        state.peak_w = best[1]
        return True
    state.peak_w = best[1] if best is not None else 0.0
    return False


def _scan_both_ways(io: AlignmentIO, state: _State, axis: str, lo: float, hi: float,
                    spacing: float) -> List[Tuple[int, List[Tuple[float, float]]]]:
    """Readings (coordinate, power) from lo to hi, then back, at a fixed meter range; raised on an overrange."""
    index = LOCAL_INDEX[axis]
    while True:
        _averaging(io, state, SCAN_AVERAGING)
        _range(io, state, state.scan_range_w)
        _goto(io, state, **{axis: lo})
        _motion(io, state, lin_vel=_scan_speed(io, spacing), blending=0)
        passes = []
        for direction, end in ((1, hi), (-1, lo)):
            samples = io.scan([state.pose(io, **{axis: end})], kind="cart")
            passes.append((direction, [(io.local(s.pose)[index], s.power_w) for s in samples]))
        if not any(math.isinf(p) for _, pts in passes for _, p in pts):
            return passes
        # More light than the range: a range ten times higher, and again.
        state.scan_range_w *= 10.0
        if state.scan_range_w > MAX_RANGE_W:
            raise AlignmentError(f"The power meter overranges at {state.scan_range_w / 10:.3g} W: "
                                 f"check source_power_mw and the meter.")


def _fit_passes(passes, baseline: float) -> Tuple[Optional[fit.Gauss], Optional[float]]:
    """A Gaussian per direction and their mean (with the lag between them), else one through all readings."""
    fits = [fit.gauss1d(*zip(*pts), baseline=baseline) if len(pts) >= 4 else None for _, pts in passes]
    if len(fits) == 2 and all(fits):
        forward, backward = fits
        return fit.Gauss(center=0.5 * (forward.center + backward.center),
                         width=0.5 * (forward.width + backward.width),
                         peak=0.5 * (forward.peak + backward.peak),
                         baseline=baseline), 0.5 * (forward.center - backward.center)
    everything = [pt for _, pts in passes for pt in pts]
    return (fit.gauss1d(*zip(*everything), baseline=baseline) if len(everything) >= 4 else None), None


# --- Angle ----------------------------------------------------------------------------------

def angle(io: AlignmentIO, state: _State) -> None:
    """Tilt line searches in rx then rz, re-centred as the tilt walks the beam; again, finer, while it gains."""
    for round_ in range(ANGLE_ROUNDS):
        before = state.peak_w
        for axis in ("rx", "rz"):
            tilt_search(io, state, axis, round_)
        if _db(state.peak_w, before) < ANGLE_MIN_GAIN_DB:
            break
    _goto(io, state)
    state.peak_w = _still(io, state, "cart")


def tilt_search(io: AlignmentIO, state: _State, axis: str, round_: int) -> None:
    """Step the tilt away from pos[axis] while the power rises; keep the tilt a parabola through the best picks.

    At each tilt, the fiber is re-centred along the lateral axis the tilt
    walks the beam on, and the power there counts. A tilt where no light
    shows counts as no power: it ends that direction.
    """
    walk = TILT_WALK[axis]
    other = "x" if walk == "z" else "z"
    step = TILT_STEP_WIDTHS * state.angle_width * 0.5 ** round_
    limit = _tilt_limit(state, axis)
    tried: Dict[float, Tuple[float, float]] = {}  # tilt -> (power re-centred, centre along `walk`)

    def lit() -> List[float]:
        return sorted(t for t, (p, _) in tried.items() if p >= state.detect_w)

    def walk_slope() -> float:
        # µm per degree: from this search's lit tilts, else an earlier search, else none yet.
        tilts = lit()
        if len(tilts) >= 2:
            return float(np.polyfit(tilts, [tried[t][1] for t in tilts], 1)[0])
        return state.walk.get(axis, 0.0)

    def measure(tilt: float) -> float:
        tilt = round(min(max(tilt, -limit), limit), 6)
        if tilt not in tried:
            # Where the beam should land at this tilt: from the nearest lit tilt, along the walk.
            tilts = lit()
            if tilts:
                near = min(tilts, key=lambda t: abs(t - tilt))
                state.pos[walk] = tried[near][1] + walk_slope() * (tilt - near)
            state.pos[axis] = tilt
            cross_scan(io, state, walk, round_, widenings=TILT_DARK_WIDENINGS)
            tried[tilt] = (state.peak_w, state.pos[walk])
        return tried[tilt][0]

    start = round(state.pos[axis], 6)
    current = measure(start)
    for direction in (1, -1):
        tilt, power, size = start, current, step
        while len(tried) < TILT_MAX:
            nxt = round(min(max(tilt + direction * size, -limit), limit), 6)
            if nxt == tilt:
                break
            p = measure(nxt)
            if not p > power:
                break
            tilt, power, size = nxt, p, size * TILT_GROWTH
        if direction == 1 and len(tried) > 1 and max(tried.values())[0] > current:
            break  # the power rose this way: the other side cannot hold the peak
    tilts = sorted(tried)
    powers = [tried[t][0] for t in tilts]
    i = int(np.argmax(powers))
    parabola = None
    if 0 < i < len(tilts) - 1 and all(p >= state.detect_w for p in powers[i - 1:i + 2]):
        parabola = fit.parabola1d(tilts[i - 1:i + 2], powers[i - 1:i + 2])
    if parabola is not None and parabola.curvature < 0 and math.isfinite(parabola.vertex):
        chosen = min(max(parabola.vertex, tilts[i - 1]), tilts[i + 1])
        state.angle_width = _new_width(state.angle_width, parabola.width, ANGLE_WIDTH_BOUNDS_DEG)
    else:
        chosen = tilts[i]
    # Start the re-centring where the tilts put the centre, then scan both lateral axes.
    lit_tilts = lit() or tilts
    slope = walk_slope() if len(lit()) >= 2 else None
    if slope is not None:
        state.walk[axis] = slope
    state.pos[axis] = chosen
    state.pos[walk] = float(np.interp(chosen, lit_tilts, [tried[t][1] for t in lit_tilts]))
    for lateral in (walk, other):
        cross_scan(io, state, lateral, round_)
    # The walk (µm/°) is in effect the distance between the fiber ends (or from the TCP to the tip).
    io.publish("profile", {
        "axis": axis,
        "iteration": round_,
        "rescan": False,
        "unit": "°",
        "span": [tilts[0], tilts[-1]] if len(tilts) > 1 else [tilts[0] - step, tilts[0] + step],
        "passes": [{"dir": 1, "points": [[t, p] for t, p in zip(tilts, powers)]}],
        "fit": None if parabola is None or not parabola.curvature < 0 else {
            "model": "parabola", "center": round(parabola.vertex, 5), "width": round(parabola.width, 5),
            "peak": parabola.peak,
        },
        "chosen": round(chosen, 5),
        "walk_um_per_deg": None if slope is None else round(slope, 1),
    })


# --- Peak -----------------------------------------------------------------------------------

def peak(io: AlignmentIO, state: _State) -> None:
    """Newton steps on a quadratic fit of log power over a 3×3 pattern at rest; ends on the best reading at rest."""
    mean_width = 0.5 * (state.width["x"] + state.width["z"])
    radius = max(PEAK_RADIUS_MIN_UM, PEAK_RADIUS_WIDTHS * mean_width)
    min_step = max(PEAK_MIN_STEP_UM, PEAK_MIN_STEP_WIDTHS * mean_width)
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
        if step < min_step or gain < PEAK_MIN_GAIN_DB:
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


def _new_width(old: float, fitted: float, bounds: Tuple[float, float]) -> float:
    """A fitted width, changed from ``old`` by at most WIDTH_CHANGE either way, within ``bounds``."""
    if not math.isfinite(fitted) or fitted <= 0:
        return old
    changed = min(max(fitted, old / WIDTH_CHANGE), old * WIDTH_CHANGE)
    return min(max(changed, bounds[0]), bounds[1])


def _lateral_limit(state: _State, axis: str) -> float:
    """How far along ``axis`` (µm) the window lets the fiber go, the other lateral axis where it is."""
    other = state.pos["z" if axis == "x" else "x"]
    room = state.settings.range_mm * 1e3 - WINDOW_MARGIN_MM * 1e3
    return math.sqrt(max(room * room - other * other, 0.0))


def _tilt_limit(state: _State, axis: str) -> float:
    """How far in ``axis`` (°) the window lets the fiber tilt, the other tilt where it is."""
    other = state.pos["rz" if axis == "rx" else "rx"]
    room = state.settings.range_deg - ANGLE_MARGIN_DEG
    return math.sqrt(max(room * room - other * other, 0.0))


def _scan_speed(io: AlignmentIO, spacing_um: float) -> float:
    """Scan speed (mm/s) that leaves ``spacing_um`` between two readings."""
    return max(MIN_SCAN_SPEED, spacing_um * 1e-3 / io.sample_period_s)


def _scan_ang_speed(io: AlignmentIO, spacing_deg: float) -> float:
    """Angular scan speed (°/s) that leaves ``spacing_deg`` between two readings."""
    return max(MIN_SCAN_ANG_SPEED, spacing_deg / io.sample_period_s)


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
