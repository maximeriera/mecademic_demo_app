"""Frame math for the alignment, from the colleague's alignment_demo.py.

A pose is ``(x, y, z, alpha, beta, gamma)``: mm, then Euler angles in degrees,
XYZ *mobile* (intrinsic, scipy's ``'XYZ'``), the robot's convention. The
``*_extrinsic`` variants use fixed-axis ``'xyz'`` angles. His algorithm uses
them for small rotations about a frame's own axes.
"""

from typing import Sequence, Tuple

import numpy as np
from scipy.spatial.transform import Rotation as R

Pose6 = Tuple[float, float, float, float, float, float]


def make_homogeneous_matrix(translation, euler_angles, degrees=True):
    """Combines translation and euler angles into a 4x4 transformation matrix."""
    T = np.eye(4)
    T[:3, :3] = R.from_euler('XYZ', euler_angles, degrees=degrees).as_matrix()
    T[:3, 3] = translation
    return T


def make_homogeneous_matrix_extrinsic(translation, euler_angles, degrees=True):
    """Same as :func:`make_homogeneous_matrix`, with extrinsic ``'xyz'`` angles."""
    T = np.eye(4)
    T[:3, :3] = R.from_euler('xyz', euler_angles, degrees=degrees).as_matrix()
    T[:3, 3] = translation
    return T


def extract_pose(T, degrees=True):
    """Extracts translation and euler angles from a 4x4 matrix."""
    return T[:3, 3], R.from_matrix(T[:3, :3]).as_euler('XYZ', degrees=degrees)


def extract_pose_extrinsic(T, degrees=True):
    """Same as :func:`extract_pose`, with extrinsic ``'xyz'`` angles."""
    return T[:3, 3], R.from_matrix(T[:3, :3]).as_euler('xyz', degrees=degrees)


def pose_matrix(pose: Sequence[float]):
    """4x4 matrix of a robot pose."""
    return make_homogeneous_matrix(list(pose[:3]), list(pose[3:6]))


def matrix_pose(T) -> Pose6:
    """Robot pose of a 4x4 matrix, as plain floats."""
    translation, angles = extract_pose(T)
    return tuple(float(v) for v in (*translation, *angles))


def fit_plane_to_points(points):
    """Fits a plane to a collection of 3D points using SVD.

    Parameters:
    points (np.ndarray): An (N, 3) array of 3D points.

    Returns:
    centroid (np.ndarray): A point on the plane (mean of all points).
    normal (np.ndarray): The normal vector of the best-fit plane.
    """
    centroid = np.mean(points, axis=0)
    # The normal is the right singular vector of the smallest singular value.
    _, _, vh = np.linalg.svd(points - centroid)
    return centroid, vh[-1, :]
