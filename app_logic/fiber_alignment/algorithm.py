"""Fiber alignment algorithm, ported from the colleague's alignment_demo.py.

Same sequence, constants and decisions as his script. Only the I/O changed:

* the robot and the power meter are reached through ``io`` (alignment.py), which
  keeps every pose inside the alignment window and stops on ABORT;
* his DataFrames are lists of :class:`~alignment.Sample`, his robot-variable
  thresholds are params (context.yaml), and his matplotlib graphs are the
  custom view.

``align`` runs:

1. a cartesian spiral (X/Z) then an angular spiral (alpha/gamma), each stopping
   once past its threshold with the power no longer rising, then settling on
   its best point;
2. if still below the axial threshold, 1-D scans along four axes (two
   rotations, two translations) in a frame turned 15° further every four scans,
   shrinking as they go;
3. a gradient ascent: small circles, alternately cartesian and angular, a
   plane fitted to their readings, and a step up the slope.

The contract with the cell (see alignment.py): move only through ``io``, never
catch exceptions broadly, leave the fiber at the best pose found.
"""

from typing import List, Optional, Tuple

import numpy as np

from . import frames
from .alignment import AlignmentIO, AlignmentSettings, Sample

# --- Spiral search ------------------------------------------------------------
# generate_archimedean_spiral(max_radius, spiral_pitch, point_spacing): mm for
# the cartesian spiral, degrees for the angular one.
CART_SPIRAL = (0.5, 0.1, 0.05)
ANG_SPIRAL = (3.0, 0.5, 0.05)
SPIRAL_MOTION = {"acc": 100, "lin_vel": 5000, "ang_vel": 1000, "blending": 50}
#: Slow move to the best point of a scan.
SETTLE_MOTION = {"acc": 25, "lin_vel": 1, "ang_vel": 5}

# --- Axial search ---------------------------------------------------------------
AXIAL_LIMIT_DEG = 2.0
AXIAL_LIMIT_MM = 0.5
AXIAL_ITERATIONS = 30
AXIAL_MOTION = {"acc": 100, "lin_vel": 20, "ang_vel": 20, "blending": 0}
#: Every four scans: limits and speeds × AXIAL_DECAY, frame turned AXIAL_FRAME_STEP_DEG about Y.
AXIAL_DECAY = 0.8
AXIAL_FRAME_STEP_DEG = 15.0
#: Re-scan, slower and narrower, when the reading at the best pose falls this far below the scan's best.
AXIAL_DISCREPANCY = 0.10
AXIAL_RESCAN_SCALE = 0.2
AXIAL_RESCAN_MOTION = {"acc": 2, "lin_vel": 0.5, "ang_vel": 0.5}
AXIAL_AFTER_RESCAN_MOTION = {"acc": 10, "lin_vel": 2, "ang_vel": 2}

# --- Gradient circles --------------------------------------------------------------
CIRCLE_REPS = 16
CIRCLE_STEP = 0.03
CIRCLE_RESOLUTION = 18
#: mm for cartesian circles, degrees for angular ones.
CIRCLE_RADIUS = 0.03
CIRCLE_MOTION = {"acc": 100, "lin_vel": 5000, "ang_vel": 1000, "blending": 50}
#: Radius and step × CIRCLE_DECAY once a circle ends above the gradient threshold.
CIRCLE_DECAY = 0.8

# --- Power meter averaging: samples of about 3 ms on a PM100x ----------------------
SEARCH_AVERAGING = 1     # his 1 ms average time
GRADIENT_AVERAGING = 3   # his 10 ms


def align(io: AlignmentIO, settings: AlignmentSettings) -> None:
    io.set_power_auto_range(True)
    io.set_power_averaging(SEARCH_AVERAGING)

    spiral_search(io, settings.spiral_cart_threshold_w, angular=False)
    power = spiral_search(io, settings.spiral_ang_threshold_w, angular=True)

    if power < settings.axial_threshold_w:
        axial_search(io, AXIAL_LIMIT_DEG, AXIAL_LIMIT_MM, settings.axial_threshold_w)

    io.set_power_averaging(GRADIENT_AVERAGING)
    looping_circles(io, angular=False, reps=CIRCLE_REPS, step_magnitude=CIRCLE_STEP,
                    circle_resolution=CIRCLE_RESOLUTION, radius=CIRCLE_RADIUS, alternate=True,
                    gradient_threshold=settings.gradient_threshold_w)


# --- Spiral search ------------------------------------------------------------------

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


def spiral_search(io: AlignmentIO, stop_threshold: float, angular: bool) -> float:
    """Spiral around the current pose (X/Z, or alpha/gamma), then settle on its best point.

    Returns the power read there.
    """
    kind = "ang" if angular else "cart"
    spiral = generate_archimedean_spiral((0.0, 0.0), *(ANG_SPIRAL if angular else CART_SPIRAL))
    io.set_motion(**SPIRAL_MOTION)
    s = io.pose()
    if angular:
        targets = [(s[0], s[1], s[2], s[3] + a, s[4], s[5] + b) for a, b in spiral]
    else:
        targets = [(s[0] + a, s[1], s[2] + b, s[3], s[4], s[5]) for a, b in spiral]

    scan_max = float("-inf")

    def past_peak(samples: List[Sample]) -> bool:
        # His rule: once the scan has gone above the threshold, stop as soon as
        # the power is no longer rising over the last five readings.
        nonlocal scan_max
        scan_max = max(scan_max, samples[-1].power_w)
        return scan_max > stop_threshold and len(samples) > 4 and samples[-1].power_w - samples[-5].power_w <= 0

    best = _best(io.scan(targets, kind=kind, stop=past_peak))
    if best is not None:
        io.set_motion(**SETTLE_MOTION)
        io.move_to(best.pose)
    return io.read_power(kind)


# --- Axial search -------------------------------------------------------------------

def axis_scan(io: AlignmentIO, axis_num: int, limit_deg: float, limit_cart: float,
              T_A_ObjYrot, T_B_ObjYrot_anti) -> List[Sample]:
    """One scan along an axis of the rotated frame: 0 = rotation about X, 1 = about Z, 2 = along X, 3 = along Z.

    Both ends are placed relative to the scan's start frame, as in his script:
    the scan runs from +limit to -2 × limit.
    """
    if axis_num == 0:
        cart, orient, cart2, orient2 = [0, 0, 0], [limit_deg, 0, 0], [0, 0, 0], [-2 * limit_deg, 0, 0]
    elif axis_num == 1:
        cart, orient, cart2, orient2 = [0, 0, 0], [0, 0, limit_deg], [0, 0, 0], [0, 0, -2 * limit_deg]
    elif axis_num == 2:
        cart, orient, cart2, orient2 = [limit_cart, 0, 0], [0, 0, 0], [-2 * limit_cart, 0, 0], [0, 0, 0]
    else:
        cart, orient, cart2, orient2 = [0, 0, limit_cart], [0, 0, 0], [0, 0, -2 * limit_cart], [0, 0, 0]

    targets = [
        frames.matrix_pose(T_A_ObjYrot @ frames.make_homogeneous_matrix_extrinsic(c, o) @ T_B_ObjYrot_anti)
        for c, o in ((cart, orient), (cart2, orient2))
    ]
    return io.scan(targets, kind="ang" if axis_num < 2 else "cart")


def axial_search(io: AlignmentIO, limit_deg: float, limit_cart: float, axis_threshold: float) -> None:
    motion = dict(AXIAL_MOTION)
    io.set_motion(**motion)

    angle_y = 0.0
    T_B_ObjYrot = frames.make_homogeneous_matrix_extrinsic([0, 0, 0], [0, angle_y, 0])
    T_B_ObjYrot_anti = frames.make_homogeneous_matrix_extrinsic([0, 0, 0], [0, -angle_y, 0])

    for i in range(AXIAL_ITERATIONS):
        if i % 4 == 0 and i != 0:
            limit_deg *= AXIAL_DECAY
            limit_cart *= AXIAL_DECAY
            for key in ("acc", "lin_vel", "ang_vel"):
                motion[key] *= AXIAL_DECAY
            io.set_motion(**motion)

            angle_y += AXIAL_FRAME_STEP_DEG
            T_B_ObjYrot = frames.make_homogeneous_matrix_extrinsic([0, 0, 0], [0, angle_y, 0])
            T_B_ObjYrot_anti = frames.make_homogeneous_matrix_extrinsic([0, 0, 0], [0, -angle_y, 0])

        axis = i % 4
        kind = "ang" if axis < 2 else "cart"
        T_A_ObjYrot = frames.pose_matrix(io.pose()) @ T_B_ObjYrot

        best = _best(axis_scan(io, axis, limit_deg, limit_cart, T_A_ObjYrot, T_B_ObjYrot_anti))
        if best is None:
            continue
        io.move_to(best.pose)
        power = io.read_power(kind)

        if best.power_w > 0 and (best.power_w - power) / best.power_w >= AXIAL_DISCREPANCY:
            # The best reading did not hold: scan again, slower and narrower.
            io.set_motion(**AXIAL_RESCAN_MOTION)
            rescan = axis_scan(io, axis, limit_deg * AXIAL_RESCAN_SCALE, limit_cart * AXIAL_RESCAN_SCALE,
                               T_A_ObjYrot, T_B_ObjYrot_anti)
            motion.update(AXIAL_AFTER_RESCAN_MOTION)
            io.set_motion(**AXIAL_AFTER_RESCAN_MOTION)
            best = _best(rescan)
            if best is not None:
                io.move_to(best.pose)
                power = io.read_power(kind)

        if power >= axis_threshold:
            break


# --- Gradient circles ------------------------------------------------------------------

def cart_circle(io: AlignmentIO, circle_resolution: int, radius: float, T_A_B) -> List[Tuple[float, float, float]]:
    """Circle of ``radius`` mm in the X/Z plane of frame B. Returns (x_B, z_B, power) per reading."""
    targets = []
    for i in range(circle_resolution + 1):
        angle = np.radians(360 / circle_resolution * i)
        T_B_Obj = frames.make_homogeneous_matrix([np.cos(angle) * radius, 0, np.sin(angle) * radius], [0, 0, 0])
        targets.append(frames.matrix_pose(T_A_B @ T_B_Obj))
    io.move_to(targets[0])

    T_B_A = np.linalg.inv(T_A_B)
    points = []
    for sample in io.scan(targets[1:], kind="cart"):
        pos_B, _ = frames.extract_pose(T_B_A @ frames.pose_matrix(sample.pose))
        points.append((pos_B[0], pos_B[2], sample.power_w))
    return points


def ang_circle(io: AlignmentIO, circle_resolution: int, degs: float, T_A_B) -> List[Tuple[float, float, float]]:
    """Circle of ``degs`` degrees in rotation about X and Z of frame B. Returns (rx_B, rz_B, power) per reading."""
    targets = []
    for i in range(circle_resolution + 1):
        angle = np.radians(360 / circle_resolution * i)
        T_B_Obj = frames.make_homogeneous_matrix_extrinsic([0, 0, 0], [np.cos(angle) * degs, 0, np.sin(angle) * degs])
        targets.append(frames.matrix_pose(T_A_B @ T_B_Obj))
    io.move_to(targets[0])

    T_B_A = np.linalg.inv(T_A_B)
    points = []
    for sample in io.scan(targets[1:], kind="ang"):
        _, ori_B = frames.extract_pose_extrinsic(T_B_A @ frames.pose_matrix(sample.pose))
        points.append((ori_B[0], ori_B[2], sample.power_w))
    return points


def looping_circles(io: AlignmentIO, angular: bool, reps: int, step_magnitude: float, circle_resolution: int,
                    radius: float, alternate: bool, gradient_threshold: float) -> None:
    io.set_motion(**CIRCLE_MOTION)
    for _ in range(reps):
        T_A_B = frames.pose_matrix(io.pose())
        if angular:
            points = np.array(ang_circle(io, circle_resolution, radius, T_A_B))
        else:
            points = np.array(cart_circle(io, circle_resolution, radius, T_A_B))
        if len(points) < 3:
            # Too few readings to fit a plane (a very fast circle): try again.
            continue

        _, normal = frames.fit_plane_to_points(points)
        a, b, c = normal
        if c == 0:
            # His script stops here too (the fit gives no slope).
            return
        # Direction of fastest ascent in the circle's plane.
        ascent_2d = np.array([-a / c, -b / c])
        gradient_mag = np.linalg.norm(ascent_2d)
        if gradient_mag == 0:
            # Flat: nowhere to climb (his script would divide by zero here).
            return
        ascent_2d_unit = ascent_2d / gradient_mag

        current = points[-1]
        if current[2] >= gradient_threshold:
            radius *= CIRCLE_DECAY
            step_magnitude *= CIRCLE_DECAY

        # Step along the slope, from where the circle ended.
        d1 = ascent_2d_unit[0] * step_magnitude - current[0]
        d2 = ascent_2d_unit[1] * step_magnitude - current[1]
        if angular:
            io.move_rel_trf(0, 0, 0, d1, 0, d2)
        else:
            io.move_rel_trf(d1, 0, d2, 0, 0, 0)

        if alternate:
            angular = not angular


def _best(samples: List[Sample]) -> Optional[Sample]:
    """The sample with the highest valid power, or None."""
    valid = [s for s in samples if s.power_w == s.power_w]  # drops NaN
    return max(valid, key=lambda s: s.power_w) if valid else None
