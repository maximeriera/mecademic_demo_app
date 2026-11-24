"""Align the laser beam to beam_analysis's target circles by moving the lens.

The ``cv_process`` device runs ``beam_analysis.analyze`` on every frame and
publishes its data; this script only READS that data (it never imports or
calls beam_analysis). It moves the lens with the robot until each target is
completed (position, and with target_match_size the beam diameter, held
within tolerance for target_hold_frames frames), then follows the next one.

Lens axes: x, y move the lens across the beam (beam moves on the camera);
z moves it toward / away from the camera (beam size changes).

It needs two things from the rest of the code:

* ``get_data()``: the latest analysis data dict, e.g.
  ``lambda: vision.last_result()["data"]``;
* ``robot``: any object with ``move_relative(dx_mm, dy_mm, dz_mm)`` that
  makes a relative lens move and returns once the robot has stopped
  (``MecademicLensRobot`` below wraps a mecademicpy Robot).

Targets are switched on and off through the device's params
(``targets_enabled`` in config.yaml): the script checks they are on at the
start and stops cleanly when they are turned off (e.g. for a lens change).

Steps:

1. x/y calibration (calibrate()): move the lens +/- cal_step_mm along robot x and y and
   measure how the beam moves (rotation, mirroring and scale between robot
   and camera axes).
2. focus calibration: step z through +/- z_scan_mm, measuring the beam
   diameter and centre at each step, then repeat the x/y calibration at a
   second z, because how far the beam moves per mm of lens depends on the
   lens-camera distance. Saved to a JSON file and reused (the z scan runs
   every time; z is tracked from where the lens starts).
   It reports the target radii the lens can reach within z_limits_mm: set
   target_min_radius_um / target_max_radius_um in config.yaml inside that
   range, or targets outside it can't be completed.
3. alignment (align()): for each target, one move computed from the models, then
   short corrections if needed. After every move:
   * the script waits until the beam has SETTLED (position and size steady
     over several frames, and, for a sizeable move, actually moved), so
     stale frames from before the move ended or vibration don't count;
   * the x/y model is refitted to all moves so far (recent ones weighted
     more), including how it changes with z;
   * the measured (z, diameter) point is added to the focus curve, which
     is interpolated from measurements rather than an assumed formula.
   A beam clipped by the edge of the active area is still moved toward the
   target, x/y only (its diameter reads too small while clipped), until it
   is fully in view.

Every move is logged with its predicted and observed beam shift, so any
remaining systematic error is visible (summary.moves).

Safety: z never leaves z_limits_mm (relative to the z at the start of
calibration), single moves are capped, and the script stops if no beam is
found for beam_timeout_s, a target can't be completed within
target_timeout_s, or targets are turned off.
CHECK THAT THE LENS CANNOT REACH THE CAMERA WITHIN z_limits_mm.

Calibration and alignment are separate calls, so the display can be
changed in between (e.g. targets off while calibrating)::

    from beam_alignment import AlignConfig, MecademicLensRobot, align, calibrate

    get_data = lambda: vision.last_result()["data"]
    lens_robot = MecademicLensRobot(robot)
    cfg = AlignConfig(z_limits_mm=(-1.0, 1.0), n_targets=5)

    cal = calibrate(get_data, lens_robot, cfg)        # targets may be off
    # ... turn targets on (targets_enabled) ...
    summary = align(get_data, lens_robot, cal, cfg)
    print(summary.stopped_because, [(t.moves, t.distance_um) for t in summary.targets])

The lens is tracked from where calibration started, so nothing else may move
it between calibrate() and align(). align() can be called again with the same
calibration (models keep improving); align(get_data, robot, cfg) on its own
calibrates first.
"""

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@dataclass
class AlignConfig:
    # calibration
    lateral_cal_file: Optional[str] = None   # None: always recalibrate.
                                    # A relative path is relative to the current working
                                    # directory; the full path is printed when used
    cal_step_mm: float = 0.5        # x/y calibration move: big enough for the beam to move
                                    # well beyond measurement noise, small enough to stay
                                    # inside the active area
    cal_frames: int = 5             # settled frames averaged per calibration measurement
    recheck_tolerance: float = 0.05 # recalibrate x/y if the saved calibration predicts the
                                    # delay test move worse than this (fraction)
    match_size: bool = True         # also drive z to match the target diameter
    z_scan_mm: float = 1.0          # focus calibration scans start z +/- this
    z_steps: int = 9
    z_limits_mm: tuple = (-1.0, 1.0)   # z always stays within start z + this range
    focus_side: Optional[int] = None   # None: stay on the side of focus the lens starts
                                       # on; +1 / -1 to force z above / below focus
    # settling after each move
    settle_frames: int = 3          # fresh frames always skipped after a move
    settle_time_s: float = 0.15     # ... and, on top of the measured camera/processing
                                    # delay (x 1.25), at least this long
    max_delay_s: float = 3.0        # longest delay the delay measurement looks for
    max_drift_um_s: float = 50.0    # a steady trend faster than this means the lens is
                                    # still moving (not settled); slower = drift, allowed
    stable_frames: int = 3          # then this many consecutive frames must agree ...
    stable_um: float = 8.0          # ... within this (centre; 2x this for diameter), after
                                    # removing a steady drift
    settle_timeout_s: float = 3.0   # give up waiting (that move is then not learned from)
    # moves
    gain_xy: float = 1.0            # fraction of each computed move; 1 = full move
    gain_z: float = 1.0
    max_step_mm: float = 5.0        # largest single x/y move
    max_dz_mm: float = 1.0          # largest single z move
    learn: bool = True              # refit the models from every move
    learn_memory: float = 0.9       # weight of each older move (1 = all moves equal)
    # run
    n_targets: Optional[int] = None  # stop after this many; None: until the device's
                                     # target sequence is done
    beam_timeout_s: float = 10.0    # stop if no beam is found for this long (a clipped
                                    # beam still counts as found and is moved in)
    target_timeout_s: float = 120.0 # stop if a target takes longer than this (it may need
                                    # a beam size the lens can't reach: see reachable_radii)
    frame_timeout_s: float = 5.0    # stop if the device produces no new results
    verbose: bool = True


# ---------------------------------------------------------------------------
# Robot adapter
# ---------------------------------------------------------------------------
class MecademicLensRobot:
    """Relative lens moves with a mecademicpy Robot (already connected,
    activated and homed). CHECK the move call and the reference frame against
    your mecademicpy version and how the lens is mounted: the axes only need
    to be consistent (calibration measures signs and scale), but they must be
    x, y across the beam and z along it."""

    def __init__(self, robot, frame: str = "trf"):
        self.robot = robot
        self.frame = frame

    def move_relative(self, dx_mm: float, dy_mm: float, dz_mm: float) -> None:
        if self.frame == "trf":
            self.robot.api.MoveLinRelTrf(dx_mm, dy_mm, dz_mm, 0, 0, 0)
        else:
            self.robot.api.MoveLinRelWrf(dx_mm, dy_mm, dz_mm, 0, 0, 0)
        self.robot.api.WaitIdle()


# ---------------------------------------------------------------------------
# Reading the device's results
# ---------------------------------------------------------------------------
@dataclass
class Meas:
    """A beam measurement: averaged over settled frames, or a single frame."""
    found: bool
    xy: Optional[np.ndarray]      # beam centre, um (full frame)
    d: Optional[float]            # beam diameter, um
    clipped: bool
    data: dict                    # the last result's full data dict
    settled: bool                 # averaged over steady frames


def _detrended_spread(a):
    """Peak-to-peak of each column after removing its straight-line trend."""
    k = np.arange(len(a), dtype=float)
    k -= k.mean()
    slope = (k @ (a - a.mean(0))) / max(k @ k, 1e-12)
    return np.ptp(a - np.outer(k, slope), axis=0)


class ResultReader:
    """Waits for fresh analysis results (data["frame"]) and for the beam to
    settle after a move."""

    def __init__(self, get_data: Callable[[], dict], cfg: AlignConfig, poll_s: float = 0.01):
        self.get_data = get_data
        self.cfg = cfg
        self.poll = poll_s
        self.delay_s = 0.0               # camera + processing delay (measured in calibrate)
        self._seen = (None, 0.0)         # (frame, when it first appeared)

    def latest(self) -> dict:
        d = self.get_data()
        if d is None or "frame" not in d:
            raise RuntimeError("no analysis data yet (is the vision device running "
                               "beam_analysis?)")
        return d

    def arrival(self, d: dict) -> float:
        """When result d was first seen (monotonic seconds)."""
        f = d.get("frame")
        if self._seen[0] != f:
            self._seen = (f, time.monotonic())
        return self._seen[1]

    def next(self, after_frame: int, skip: int = 0) -> dict:
        """First result whose frame number is > after_frame + skip."""
        t0 = time.monotonic()
        while True:
            d = self.get_data()
            if d is not None and d.get("frame", -1) > after_frame + skip:
                self.arrival(d)
                return d
            if time.monotonic() - t0 > self.cfg.frame_timeout_s:
                raise TimeoutError(f"no new analysis result for {self.cfg.frame_timeout_s:.0f} s")
            time.sleep(self.poll)

    @staticmethod
    def single(d: dict) -> Meas:
        ok = bool(d.get("found")) and d.get("x_um") is not None
        return Meas(ok, np.array([d["x_um"], d["y_um"]]) if ok else None,
                    d.get("diameter_um") if ok else None, bool(d.get("clipped")), d, False)

    def settle(self, n: Optional[int] = None, before: Optional[np.ndarray] = None,
               expected_um: Optional[np.ndarray] = None, d_before: Optional[float] = None,
               d_expected: Optional[float] = None) -> Meas:
        """Wait (from now) until the beam is steady, then average the last n
        frames. With before / expected_um (a move's starting position and
        predicted shift) and d_before / d_expected (diameter before and
        predicted after), frames still showing the beam where or as large as
        it was before the move are not accepted as settled: they are stale."""
        cfg = self.cfg
        n = max(2, n or cfg.stable_frames)
        t0 = time.monotonic()
        # results arriving before this were (partly) captured before the lens
        # stopped: the camera / processing delay, measured in calibrate()
        t_valid = t0 + 1.25 * self.delay_s + cfg.settle_time_s
        d = self.next(self.latest()["frame"], cfg.settle_frames)
        while self.arrival(d) < t_valid:
            d = self.next(d["frame"])
        win = []                                       # (x, y, D, clipped, t) of found frames
        while True:
            m = self.single(d)
            if m.found:
                win.append((m.xy[0], m.xy[1], m.d, m.clipped, self.arrival(d)))
                win = win[-n:]
            else:
                win = []
            waited = time.monotonic() - t0
            if len(win) == n:
                a = np.array([w[:3] for w in win], dtype=float)
                # steady = little scatter about a straight-line trend (vibration
                # and ringing fail it), and that trend slow: a slow drift (laser
                # pointing, thermal) is fine, a lens still moving is not
                r = _detrended_spread(a)
                tt = np.array([w[4] for w in win])
                speed = 0.0
                if np.ptp(tt) > 1e-3:
                    speed = float(np.hypot(np.polyfit(tt, a[:, 0], 1)[0],
                                           np.polyfit(tt, a[:, 1], 1)[0]))
                steady = (r[0] <= cfg.stable_um and r[1] <= cfg.stable_um
                          and r[2] <= 2 * cfg.stable_um and speed <= cfg.max_drift_um_s)
                moved = True
                if before is not None and expected_um is not None:
                    exp = float(np.hypot(*expected_um))
                    if exp > 3 * cfg.stable_um:            # a move big enough to check
                        moved = np.hypot(*(a[:, :2].mean(0) - before)) >= 0.3 * exp
                if moved and d_before is not None and d_expected is not None:
                    dexp = d_expected - d_before
                    if abs(dexp) > 6 * cfg.stable_um:      # a size change big enough to check
                        moved = (np.median(a[:, 2]) - d_before) / dexp >= 0.3
                if steady and moved:
                    return Meas(True, a[:, :2].mean(0), float(np.median(a[:, 2])),
                                any(w[3] for w in win), d, True)
            if waited > cfg.settle_timeout_s + 1.25 * self.delay_s:
                if win:                                # best effort, not trusted for learning
                    a = np.array([w[:3] for w in win], dtype=float)
                    return Meas(True, a[:, :2].mean(0), float(np.median(a[:, 2])),
                                any(w[3] for w in win), d, False)
                return Meas(False, None, None, False, d, False)
            d = self.next(d["frame"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class LensModel:
    """How the beam moves on the camera (um) when the lens moves, in terms of
    the lens position p = (x, y, z) in mm (relative to where it started):

        f(p) = (J + z*B) @ (x, y) + a*z,   beam shift p0 -> p1 = f(p1) - f(p0)

    J (2x2): x/y map (rotation, mirroring, scale) at z = 0; B (2x2): its
    change per mm of z (the beam moves more per mm of lens shift the further
    the lens is from the camera); a (2): sideways shift per mm of z (z axis
    not quite parallel to the beam).

    In terms of position rather than per move: with the lens off-axis, a z
    move also shifts the beam by z*B @ (x, y), which a per-move model gets
    wrong. Fitted to the CHANGE between consecutive settled measurements, so
    a slow drift of where the beam lands (laser pointing, thermal) doesn't
    corrupt it. Ridge regression toward the prior, older pairs weighted by
    `memory` per pair."""

    def __init__(self, memory=0.9):
        self.theta = np.zeros((2, 5))                    # [J | a | B] over phi(p)
        self.theta0 = np.zeros((2, 5))
        self.lam = np.full(5, 1e-6)                      # prior strength (none by default)
        self.memory = memory
        self.pairs = []                                  # (phi1 - phi0, c1 - c0)

    @staticmethod
    def phi(p):
        x, y, z = p
        return np.array([x, y, z, z * x, z * y])

    def add_pair(self, p0, c0, p1, c1, refit=True):
        """Two settled measurements: beam centre c0 at lens p0, c1 at p1."""
        dphi = self.phi(p1) - self.phi(p0)
        if np.abs(dphi).max() < 1e-6:
            return
        self.pairs.append((dphi, np.asarray(c1, float) - np.asarray(c0, float)))
        if refit:
            self.fit()

    def fit(self):
        n = len(self.pairs)
        if n == 0:
            if np.any(self.lam > 1e-3):                  # a saved prior alone is fine
                self.theta = self.theta0.copy()
                return self
            raise RuntimeError("lens model: no usable calibration measurements (beam lost, "
                               "clipped or never steady)")
        w = self.memory ** np.arange(n - 1, -1, -1)
        Phi = np.array([q[0] for q in self.pairs])
        S = np.array([q[1] for q in self.pairs])
        L = np.diag(self.lam)
        G = (Phi * w[:, None]).T @ Phi + L
        R = (S * w[:, None]).T @ Phi + self.theta0 @ L
        self.theta = np.linalg.solve(G.T, R.T).T
        return self

    def f(self, p):
        return self.theta @ self.phi(p)

    def shift(self, p0, p1):
        """Predicted beam shift (um) for moving the lens from p0 to p1."""
        return self.f(p1) - self.f(p0)

    def J_at(self, z):
        return self.theta[:, 0:2] + z * self.theta[:, 3:5]

    def xy_for(self, p0, shift_um, z1):
        """Lens x, y (mm) at height z1 that moves the beam by shift_um from
        where it is with the lens at p0."""
        rhs = self.f(p0) + np.asarray(shift_um, float) - self.theta[:, 2] * z1
        return np.linalg.solve(self.J_at(z1), rhs)

    def describe(self, z=0.0):
        J = self.J_at(z)
        sx, sy = np.hypot(*J[:, 0]), np.hypot(*J[:, 1])
        rot = np.degrees(np.arctan2(J[1, 0], J[0, 0]))
        flip = np.linalg.det(J) < 0
        chg = 100 * np.linalg.norm(self.theta[:, 3:5]) / max(np.linalg.norm(J), 1e-9)
        a = self.theta[:, 2]
        return (f"robot x: {sx:.0f} um/mm, robot y: {sy:.0f} um/mm, rotation {rot:.1f} deg"
                f"{', mirrored' if flip else ''}; changes {chg:.1f} %/mm of z; z shifts the "
                f"beam ({a[0]:+.0f}, {a[1]:+.0f}) um/mm")

    def save(self, path, z_focus):
        """J and B, with J given at the focus z (the start z differs run to run)."""
        with open(path, "w") as f:
            json.dump({"J_at_focus": self.J_at(z_focus).tolist(),
                       "B": self.theta[:, 3:5].tolist()}, f, indent=1)

    def use_saved(self, path, z_focus, strength=20.0):
        """Use a saved J / B (relative to this run's focus z) as the prior;
        a comes from this run's z scan."""
        with open(path) as f:
            c = json.load(f)
        B = np.array(c["B"], float)
        J0 = np.array(c["J_at_focus"], float) - z_focus * B
        self.theta0 = np.zeros((2, 5))
        self.theta0[:, 0:2], self.theta0[:, 3:5] = J0, B
        self.lam = np.array([strength, strength, 1e-6, strength, strength])
        return self.fit()


class Chain:
    """Feeds consecutive valid measurements to the model as pairs; an
    invalid one (lost, clipped, not settled) breaks the chain."""

    def __init__(self, model):
        self.model, self.last = model, None

    def add(self, p, m: "Meas", refit=True):
        if m.found and m.settled and not m.clipped:
            if self.last is not None:
                self.model.add_pair(self.last[0], self.last[1], p, m.xy, refit)
            self.last = (np.array(p, float), m.xy.copy())
        else:
            self.last = None


class FocusModel:
    """Beam diameter vs lens z (mm, relative to the start of calibration).

    A fitted D(z)^2 = D0^2 + k^2 (z - zf)^2 gives the focus position and
    extends beyond the measured range; within it, z for a diameter is
    interpolated from the measured (z, D) points on the working side of
    focus, so a beam that doesn't follow that formula exactly is still
    reached in one move. Points measured during alignment are added."""

    def __init__(self):
        self.zf, self.d0, self.k = 0.0, 0.0, 1.0
        self.points = []                                # (z, D)

    def add(self, z, d, refit=True):
        self.points.append((float(z), float(d)))
        if refit:
            self.fit()

    def fit(self):
        P = np.array(self.points, float)
        if len(P) < 3:
            raise ValueError("focus calibration: fewer than 3 z positions had a usable beam")
        a, b, c = np.polyfit(P[:, 0], P[:, 1] ** 2, 2)
        if a <= 0:
            raise ValueError("focus calibration: beam size does not change with z as "
                             "expected (increase z_scan_mm?)")
        self.k = float(np.sqrt(a))
        self.zf = float(-b / (2 * a))
        self.d0 = float(np.sqrt(max(c - b * b / (4 * a), 0.0)))
        return self

    def _hyp_z(self, d, side):
        return self.zf + side * np.sqrt(max(d * d - self.d0 ** 2, 0.0)) / self.k

    def _side_curve(self, side):
        """Measured points on one side, binned in z, diameter made monotonic
        (growing away from focus). Returns (D, z) sorted by D."""
        P = [(z, d) for z, d in self.points if (z - self.zf) * side >= 0]
        if len(P) < 2:
            return None
        bins = {}
        for z, d in P:
            bins.setdefault(round(z / 0.005), []).append((z, d))
        pts = sorted((np.mean([p[0] for p in v]), np.mean([p[1] for p in v]))
                     for v in bins.values())
        pts.sort(key=lambda p: abs(p[0] - self.zf))     # from focus outward
        z = np.array([p[0] for p in pts])
        d = np.maximum.accumulate(np.array([p[1] for p in pts]))
        if np.ptp(d) <= 0:
            return None
        return d, z

    def z_for(self, d_um, side):
        side = 1 if side >= 0 else -1
        cur = self._side_curve(side)
        if cur is None:
            return float(self._hyp_z(d_um, side))
        D, Z = cur
        if D[0] <= d_um <= D[-1]:
            return float(np.interp(d_um, D, Z))
        i = 0 if d_um < D[0] else -1                      # extend with the fitted curve
        return float(Z[i] + self._hyp_z(d_um, side) - self._hyp_z(D[i], side))

    def d_at(self, z):
        return float(np.hypot(self.d0, self.k * (z - self.zf)))

    def reachable(self, side, z_min, z_max):
        lo, hi = (max(z_min, self.zf), z_max) if side >= 0 else (z_min, min(z_max, self.zf))
        if hi < lo:
            return None
        a, b = self.d_at(lo), self.d_at(hi)
        return min(a, b), max(a, b)

    def describe(self):
        return (f"focus at z = {self.zf:+.3f} mm, smallest beam {self.d0:.0f} um, "
                f"{self.k:.0f} um of diameter per mm of z ({len(self.points)} points)")


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
class Lens:
    """The robot plus the lens position it has been moved to (mm, relative
    to where it started)."""

    def __init__(self, robot):
        self.robot = robot
        self.p = np.zeros(3)

    def move(self, d):
        d = np.asarray(d, float)
        self.robot.move_relative(*d)
        self.p = self.p + d

    def go(self, p):
        self.move(np.asarray(p, float) - self.p)


def _measure_delay(reader: ResultReader, lens: Lens, cfg: AlignConfig, log=print):
    """Time from the robot reporting a move done to the results showing the
    beam at its new position: the camera + processing delay. One test move
    of cal_step_mm along x, then back. Returns (delay_s, observed beam shift
    of the test move in um, or None)."""
    base = reader.settle(cfg.cal_frames)                # lens has been still
    if not base.found or base.clipped:
        raise RuntimeError("delay measurement: no unclipped beam (start with the beam "
                           "inside the active area)")
    lens.move([cfg.cal_step_mm, 0.0, 0.0])
    t_end = time.monotonic()
    track, f, t_arrive = [], reader.latest()["frame"], []
    deadline = t_end + cfg.max_delay_s + 1.5
    while time.monotonic() < deadline:
        d = reader.next(f)
        f = d["frame"]
        m = reader.single(d)
        if m.found:
            track.append(m.xy)
            t_arrive.append(reader.arrival(d))
            # done once the last 1 s of results agree
            recent = [p for p, t in zip(track, t_arrive) if t > t_arrive[-1] - 1.0]
            if t_arrive[-1] - t_end > 1.0 and len(recent) >= 4 and \
                    np.ptp(np.array(recent), axis=0).max() <= cfg.stable_um and \
                    np.hypot(*(recent[-1] - base.xy)) > 5 * cfg.stable_um:
                break
    fps = (len(t_arrive) - 1) / max(t_arrive[-1] - t_arrive[0], 1e-6) if len(t_arrive) > 1 else 0
    lens.move([-cfg.cal_step_mm, 0.0, 0.0])
    if len(track) < 4:
        raise RuntimeError("delay measurement: beam lost during the test move")
    P = np.array(track)
    final = P[-3:].mean(0)
    total = float(np.hypot(*(final - base.xy)))
    if total < 5 * cfg.stable_um:
        log(f"  delay measurement: the beam barely moved; assuming {cfg.max_delay_s:.1f} s "
            f"(increase cal_step_mm)")
        return cfg.max_delay_s, None
    # first result after which the beam stays within 5 % of the move of its final place
    dist = np.hypot(*(P - final).T)
    far = np.nonzero(dist > max(0.05 * total, cfg.stable_um))[0]
    i = 0 if far.size == 0 else min(far[-1] + 1, len(P) - 1)
    delay = max(0.0, t_arrive[i] - t_end)
    log(f"camera/processing delay after a move: {delay:.2f} s (~{delay * fps:.0f} frames at "
        f"{fps:.1f} fps); results are ignored for {1.25 * delay + cfg.settle_time_s:.2f} s "
        f"after each move")
    return delay, final - base.xy


def _xy_points(reader: ResultReader, lens: Lens, cfg: AlignConfig, chain: "Chain"):
    """Measure the beam at the current lens position and at +/- cal_step_mm
    along x and y; lens returned. Pairs go into the model (not refitted)."""
    base = lens.p.copy()
    for d in ([0, 0, 0], [1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0]):
        lens.go(base + cfg.cal_step_mm * np.array(d, float))
        m = reader.settle(cfg.cal_frames)
        if not m.found or m.clipped:
            lens.go(base)
            raise RuntimeError("x/y calibration: beam lost or clipped (reduce cal_step_mm, "
                               "or start with the beam inside the active area)")
        if not m.settled:
            lens.go(base)
            raise RuntimeError("x/y calibration: the beam never held steady (vibration?): "
                               "increase stable_um or settle_timeout_s")
        chain.add(lens.p, m, refit=False)
    lens.go(base)


def _focus_points(reader: ResultReader, lens: Lens, cfg: AlignConfig, chain: "Chain",
                  log=print) -> "FocusModel":
    """z scan: diameter (focus model) and centre (lens model) at each step;
    lens returned."""
    zmin, zmax = cfg.z_limits_mm
    base = lens.p.copy()
    focus = FocusModel()
    for z in np.linspace(max(-cfg.z_scan_mm, zmin), min(cfg.z_scan_mm, zmax), cfg.z_steps):
        lens.go(base + [0.0, 0.0, z])
        m = reader.settle(cfg.cal_frames)
        chain.add(lens.p, m, refit=False)
        if m.found and not m.clipped:
            focus.add(lens.p[2], m.d, refit=False)
        else:
            log(f"  z {z:+.3f} mm: no usable beam (skipped)")
    lens.go(base)
    focus.fit()
    log("focus calibration: " + focus.describe())
    return focus


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------
@dataclass
class TargetLog:
    index: int
    moves: int
    distance_um: Optional[float]
    size_error_um: Optional[float]
    seconds: float
    timed_out: bool = False         # not completed within target_timeout_s


@dataclass
class MoveLog:
    target: int
    move_mm: list
    predicted_um: list              # predicted beam shift
    observed_um: Optional[list]     # measured (settled) beam shift
    d_before_um: Optional[float]
    d_after_um: Optional[float]
    d_goal_um: Optional[float]
    settled: bool


@dataclass
class AlignSummary:
    targets: list = field(default_factory=list)
    moves: list = field(default_factory=list)
    lens: Optional[LensModel] = None
    focus: Optional[FocusModel] = None
    reachable_radii_um: Optional[tuple] = None
    stopped_because: str = ""


@dataclass
class Calibration:
    """Everything calibrate() measured, handed to align(). Holds the robot
    and the lens position tracked since calibration started: nothing else
    should move the lens between calibrate() and align()."""
    lens: Lens                           # robot + tracked lens position (mm)
    model: LensModel                     # beam position vs lens x, y, z
    focus: Optional[FocusModel]          # beam diameter vs lens z (None: no size matching)
    side: int                            # side of focus used (+1: z above, -1: below)
    z_limits_mm: tuple
    cal_path: Optional[str]              # where the x/y calibration is saved
    reachable_d_um: Optional[tuple]      # beam diameters reachable within z_limits_mm
    delay_s: float = 0.0                 # camera + processing delay after a move
    chain: Optional[Chain] = None        # (internal) learning from consecutive points


def calibrate(get_data: Callable[[], dict], robot, cfg: Optional[AlignConfig] = None,
              log: Callable[[str], None] = print) -> Calibration:
    """Measure how the beam responds to lens moves; returns a Calibration
    for align(). Targets can be on or off (only the beam is used). The lens
    ends where it started.

    get_data : returns the latest analysis data dict.
    robot    : object with move_relative(dx_mm, dy_mm, dz_mm), blocking."""
    cfg = cfg or AlignConfig()
    if not cfg.verbose:
        log = lambda *_: None                            # noqa: E731
    reader = ResultReader(get_data, cfg)
    reader.next(reader.latest()["frame"])                # device is producing results

    zmin, zmax = cfg.z_limits_mm
    lens = Lens(robot)
    model = LensModel(cfg.learn_memory)
    chain = Chain(model)
    reader.delay_s, test_shift = _measure_delay(reader, lens, cfg, log)
    cal_path = os.path.abspath(cfg.lateral_cal_file) if cfg.lateral_cal_file else None
    if cal_path:
        log(f"calibration file: {cal_path}"
            + ("" if os.path.exists(cal_path) else " (not there yet: will be created)"))
    saved = False
    if cal_path and os.path.exists(cal_path):
        try:
            with open(cal_path) as fh:
                saved = "J_at_focus" in json.load(fh)
        except (OSError, ValueError):
            saved = False
        if not saved:
            log(f"{cal_path} is from an older version: recalibrating")
    if not saved:
        _xy_points(reader, lens, cfg, chain)             # x/y map at the start z
    focus, side = None, 1
    if cfg.match_size:
        focus = _focus_points(reader, lens, cfg, chain, log)
        side = cfg.focus_side if cfg.focus_side is not None else (1 if 0.0 >= focus.zf else -1)
    if saved:
        model.use_saved(cal_path, focus.zf if focus is not None else 0.0)
        log(f"x/y calibration loaded from {cal_path}")
        if test_shift is not None:                       # does it still fit this setup?
            pred = model.shift(np.zeros(3), np.array([cfg.cal_step_mm, 0.0, 0.0]))
            err = float(np.hypot(*(test_shift - pred)) / max(np.hypot(*test_shift), 1e-9))
            if err > cfg.recheck_tolerance:
                log(f"  the saved x/y calibration is {100 * err:.0f} % off on the test move: "
                    f"recalibrating")
                saved = False
                model.theta0 = np.zeros((2, 5))
                model.lam = np.full(5, 1e-6)
                _xy_points(reader, lens, cfg, chain)
    if not saved:
        if focus is not None:                            # x/y map at a second z
            z2 = float(np.clip(side * 0.6 * cfg.z_scan_mm, zmin, zmax))
            if abs(z2) < 0.1:
                z2 = float(np.clip(-side * 0.6 * cfg.z_scan_mm, zmin, zmax))
            lens.go([0.0, 0.0, z2])
            _xy_points(reader, lens, cfg, chain)
            lens.go([0.0, 0.0, 0.0])
        model.fit()
        if cal_path:
            model.save(cal_path, focus.zf if focus is not None else 0.0)
            log(f"x/y calibration saved to {cal_path}")
    log("x/y calibration: " + model.describe())

    reach_d = None
    if focus is not None:
        reach_d = focus.reachable(side, zmin, zmax)
        if reach_d is None:
            raise RuntimeError("no beam sizes reachable on this side of focus within "
                               "z_limits_mm")
        log(f"reachable beam diameters {reach_d[0]:.0f}-{reach_d[1]:.0f} um "
            f"({'above' if side > 0 else 'below'} focus)")
    return Calibration(lens, model, focus, side, (zmin, zmax), cal_path, reach_d,
                       reader.delay_s, chain)


def align(get_data: Callable[[], dict], robot, calibration: Optional[Calibration] = None,
          cfg: Optional[AlignConfig] = None,
          log: Callable[[str], None] = print) -> AlignSummary:
    """Move the lens onto each target in turn.

    get_data    : returns the latest analysis data dict.
    robot       : object with move_relative(dx_mm, dy_mm, dz_mm), blocking.
    calibration : from calibrate(); None calibrates first. The same
                  Calibration can be used for several align() calls (the
                  models keep improving); don't move the lens in between.
    Targets must be enabled in the device's params (targets_enabled).
    (align(get_data, robot, cfg) also works: it calibrates first.)"""
    if isinstance(calibration, AlignConfig):             # old call form
        calibration, cfg = None, calibration
    cfg = cfg or AlignConfig()
    if not cfg.verbose:
        log = lambda *_: None                            # noqa: E731
    reader = ResultReader(get_data, cfg)
    summary = AlignSummary()

    # --- targets must be on (device params)
    d = reader.next(reader.latest()["frame"])
    if not d.get("target", {}).get("enabled"):
        raise RuntimeError("targets are off in the analysis results: set "
                           "targets_enabled: true in the device's analysis_params")
    if d["target"].get("done"):
        raise RuntimeError("the device's target sequence is already finished "
                           "(target_count reached); restart it to align again")

    if calibration is None:
        calibration = calibrate(get_data, robot, cfg, log)
    cal = calibration
    reader.delay_s = cal.delay_s
    cal.lens.robot = robot                               # same robot, maybe a new wrapper
    lens, model, focus, side, cal_path = cal.lens, cal.model, cal.focus, cal.side, cal.cal_path
    chain = cal.chain or Chain(model)
    cal.chain = chain
    # z limits: the stricter of the calibration's and this call's
    zmin = max(cal.z_limits_mm[0], cfg.z_limits_mm[0])
    zmax = min(cal.z_limits_mm[1], cfg.z_limits_mm[1])
    summary.lens, summary.focus = model, focus

    reach_d = None
    if focus is not None:
        reach_d = focus.reachable(side, zmin, zmax)
        if reach_d is None:
            raise RuntimeError("no beam sizes reachable on this side of focus within "
                               "z_limits_mm")
        tol = d["target"].get("size_tolerance_um", 0.0)
        rmin, rmax = 0.5 * (reach_d[0] + tol), 0.5 * (reach_d[1] - tol)
        summary.reachable_radii_um = (rmin, rmax)
        log(f"reachable beam diameters {reach_d[0]:.0f}-{reach_d[1]:.0f} um: for every "
            f"target to be reachable set target_min_radius_um >= {rmin:.0f} and "
            f"target_max_radius_um <= {rmax:.0f} in config.yaml")

    # --- align
    cur = reader.settle()
    chain.last = None                                    # learning restarts here
    done_start = completed_seen = cur.data["target"].get("completed", 0)
    cur_index, moves, t_start = None, 0, time.monotonic()
    last_beam = time.monotonic()
    was_clipped = False
    try:
        while True:
            d = cur.data
            t = d.get("target", {})
            if t.get("completed", 0) > completed_seen:   # a target just finished
                completed_seen = t["completed"]
                lc = t["last_completed"]
                summary.targets.append(TargetLog(lc["index"], moves, lc["distance_um"],
                                                 lc["size_error_um"],
                                                 time.monotonic() - t_start))
                log(f"target {lc['index'] + 1} done: {moves} move(s), centre error "
                    f"{lc['distance_um']:.1f} um, size error {lc['size_error_um']:+.1f} um")
                cur_index = None
            if not t.get("enabled"):
                summary.stopped_because = "targets were disabled"
                break
            if t.get("done"):
                summary.stopped_because = "target sequence done"
                break
            if cfg.n_targets is not None and t["completed"] - done_start >= cfg.n_targets:
                summary.stopped_because = f"{cfg.n_targets} targets completed"
                break
            if t.get("reached"):                         # this frame still shows the old one
                cur = reader.single(reader.next(d["frame"]))
                continue

            if t["index"] != cur_index:                  # a new target
                cur_index, moves, t_start = t["index"], 0, time.monotonic()
                log(f"target {cur_index + 1}: centre ({t['x_um']:.0f}, {t['y_um']:.0f}) um, "
                    f"diameter {t['diameter_um']:.0f} um")
                if reach_d is not None and t.get("match_size", True) and not (
                        reach_d[0] - t["size_tolerance_um"] <= t["diameter_um"]
                        <= reach_d[1] + t["size_tolerance_um"]):
                    log(f"  WARNING: diameter {t['diameter_um']:.0f} um is outside the "
                        f"reachable {reach_d[0]:.0f}-{reach_d[1]:.0f} um; getting as close "
                        f"as z_limits_mm allows (it can't be completed)")

            if time.monotonic() - t_start > cfg.target_timeout_s:
                log(f"target {cur_index + 1}: not completed within {cfg.target_timeout_s:.0f} s;"
                    f" stopping")
                summary.targets.append(TargetLog(cur_index, moves, t.get("distance_um"),
                                                 t.get("size_error_um"),
                                                 time.monotonic() - t_start, timed_out=True))
                summary.stopped_because = "target timed out"
                break

            if not d.get("found") or t.get("dx_um") is None:
                if time.monotonic() - last_beam > cfg.beam_timeout_s:
                    summary.stopped_because = "beam lost"
                    log("beam lost: stopping")
                    break
                cur = reader.single(reader.next(d["frame"]))
                continue
            last_beam = time.monotonic()

            if t["inside"]:                              # holding: just watch
                cur = reader.single(reader.next(d["frame"]))
                continue
            if not cur.settled:                          # moves start from a settled,
                cur = reader.settle()                    # averaged measurement
                continue

            # Clipped by the edge of the active area: the centre is still usable
            # (the edge fit uses the visible part of the edge) but the diameter
            # reads too small, so move x/y only until the whole beam is in view.
            clipped = cur.clipped
            if clipped and not was_clipped:
                log("  beam clipped by the edge of the active area: moving it in "
                    "(x/y only until it is fully visible)")
            was_clipped = clipped

            # --- one move toward the target, from the settled measurement
            target_xy = np.array([t["x_um"], t["y_um"]])
            err_xy = target_xy - cur.xy
            pos_ok = float(np.hypot(*err_xy)) <= t["tolerance_um"]
            size_ok = (not t.get("match_size", True)) or \
                abs(t["diameter_um"] - cur.d) <= t["size_tolerance_um"]
            p0 = lens.p.copy()
            dz, d_goal = 0.0, None
            if focus is not None and not size_ok and not clipped:
                d_goal = t["diameter_um"]
                # move by the curve's z difference, anchored at the measured diameter
                dz = cfg.gain_z * (focus.z_for(d_goal, side) - focus.z_for(cur.d, side))
                dz = float(np.clip(dz, -cfg.max_dz_mm, cfg.max_dz_mm))
                dz = float(np.clip(p0[2] + dz, zmin, zmax) - p0[2])
            z_new = p0[2] + dz
            if clipped or not pos_ok or dz != 0.0:
                # lens x, y at the new z that moves the beam from where it is now
                # onto the target (size only: that keeps it where it is)
                want = np.zeros(2) if (pos_ok and not clipped) else err_xy
                mxy = model.xy_for(p0, want, z_new) - p0[:2]
                if not (pos_ok and not clipped):
                    mxy *= cfg.gain_xy
                n = float(np.hypot(*mxy))
                if n > cfg.max_step_mm:
                    mxy *= cfg.max_step_mm / n
            else:
                mxy = np.zeros(2)
            move = np.array([mxy[0], mxy[1], dz])
            if np.abs(move).max() < 1e-5:                # nothing sensible to do
                cur = reader.single(reader.next(d["frame"]))
                continue

            pred = model.shift(p0, p0 + move)
            if not cur.clipped:
                chain.add(p0, cur, refit=False)          # start of the pair (if valid)
            lens.move(move)
            moves += 1
            new = reader.settle(before=cur.xy, expected_um=pred,
                                d_before=cur.d, d_expected=d_goal if dz != 0.0 else None)
            obs = (new.xy - cur.xy) if new.found else None
            summary.moves.append(MoveLog(cur_index, move.round(5).tolist(), pred.round(1).tolist(),
                                         None if obs is None else obs.round(1).tolist(),
                                         cur.d, new.d, d_goal, new.settled))
            if obs is not None:
                ratio = float(obs @ pred / max(pred @ pred, 1e-9))
                log(f"  move {moves}: ({move[0]:+.3f}, {move[1]:+.3f}, {move[2]:+.3f}) mm, beam "
                    f"shift {np.hypot(*obs):.0f} of {np.hypot(*pred):.0f} um predicted "
                    f"({100 * (ratio - 1):+.1f} %)"
                    + (f", diameter {cur.d:.0f} -> {new.d:.0f} (goal {d_goal:.0f}) um"
                       if d_goal is not None else "")
                    + ("" if new.settled else "  [not settled: not learned from]"))
            if cfg.learn:
                chain.add(lens.p, new)                   # pair with cur -> refit
                if focus is not None and new.found and new.settled and not new.clipped:
                    focus.add(lens.p[2], new.d)
            cur = new
    except KeyboardInterrupt:
        summary.stopped_because = "interrupted"
        log("interrupted: no further moves")

    if cfg.learn and cal_path:
        # keep what was learned (x/y map at the focus z, and its change with z)
        model.save(cal_path, focus.zf if focus is not None else 0.0)
        log(f"updated calibration saved to {cal_path}")
    n_ok = sum(not t.timed_out for t in summary.targets)
    log(f"finished ({summary.stopped_because}): {n_ok} target(s) completed"
        + (f", average {np.mean([t.moves for t in summary.targets if not t.timed_out]):.1f} "
           f"move(s) each" if n_ok else ""))
    return summary