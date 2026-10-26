"""The contract between the cell and the alignment algorithm.

The algorithm (algorithm.py) never touches a device. It receives an
:class:`AlignmentIO`, which moves the held fiber anywhere in 3D (position and
orientation) and reads the power meter, plus the :class:`AlignmentSettings`. It
must leave the fiber at the best position it found. Everything else is handled
here:

* Every reading is recorded with the robot's pose at that moment, and published
  to the custom view (``align_sample``). The graph therefore shows exactly what
  the algorithm measured, and moves only while an alignment runs.
* Every target pose is checked **before anything is sent**: each coordinate
  must stay within ``align_range_um`` / ``align_range_deg`` of the start pose,
  and be a finite number.
* ABORT, a device fault and ``align_timeout_s`` are checked before every
  command and every reading. A scan interrupted for any reason clears the
  robot's motion queue, so queued moves never run on after it.
* The verdict is a fresh reading at the final position, compared with
  ``power_threshold_mw``. The algorithm's own claim is not used.

A pose is ``(x, y, z, alpha, beta, gamma)``: mm in the robot's WRF, then Euler
angles in degrees (XYZ mobile), exactly as the robot reports it. Power is in W.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from core import AppContext

from . import frames
from .frames import Pose6

#: Pose axes the custom view plots: WRF X and Z, the lateral plane (the fibers
#: face each other along Y).
PLOT_AXES = (0, 2)
#: Most points kept in the align_result summary for the custom view.
RESULT_POINTS_MAX = 5000
#: Pause between two readings during a scan (s). The meter reading and the
#: pose query take a few ms more.
SCAN_SAMPLE_PERIOD_S = 0.002
#: Checkpoint ids used by scans (the robot allows 1-8000).
CHECKPOINT_IDS = range(7000, 8000)
#: Motion for the return to the best pose after a timeout: slow, no blending.
RETURN_MOTION = {"acc": 25, "lin_vel": 1, "ang_vel": 5, "blending": 0}
#: A scan whose robot pose has not changed for this long (s) has stalled: the
#: robot is not executing it (paused, refused the moves, ...).
SCAN_STALL_S = 3.0
#: Pose change (mm or °) that counts as motion for the stall check.
SCAN_MOTION_EPS = 1e-3
#: Longest a point-to-point alignment move may take (s).
MOVE_TIMEOUT_S = 30.0


class AlignmentError(RuntimeError):
    """The alignment cannot run, or the algorithm broke the contract."""


class AlignmentRangeError(AlignmentError):
    """The algorithm asked for a pose outside the search window (or not a number)."""


class AlignmentInterrupted(AlignmentError):
    """ABORT was pressed, or a device faulted, while the alignment was running."""


class AlignmentTimeout(Exception):
    """Raised by every AlignmentIO call once ``timeout_s`` has elapsed.

    Not an error: run_alignment() catches it, moves back to the best pose read
    so far and judges that. An algorithm may catch it to clean up, but it must
    not move again.
    """


@dataclass(frozen=True)
class Sample:
    """One power reading and the robot's actual pose right after it."""

    pose: Pose6
    power_w: float
    #: "cart" (lateral moves), "ang" (rotations) or None. The view plots all but "ang".
    kind: Optional[str] = None


@dataclass(frozen=True)
class AlignmentSettings:
    #: The alignment passes once the final reading reaches this (W).
    threshold_w: float
    #: How far each coordinate may go from the start pose (mm, degrees).
    range_mm: float
    range_deg: float
    timeout_s: float
    #: Pause after a point-to-point move, before anything else (s).
    settle_s: float
    #: Thresholds of the algorithm's phases (W).
    spiral_cart_threshold_w: float
    spiral_ang_threshold_w: float
    axial_threshold_w: float
    gradient_threshold_w: float

    @classmethod
    def from_context(cls, context: AppContext) -> "AlignmentSettings":
        """Read the params from context.yaml, converted to W, mm and degrees."""
        def mw(name: str, default: float) -> float:
            return float(context.get_param(name, default)) * 1e-3

        return cls(
            threshold_w=mw("power_threshold_mw", 0.9),
            range_mm=float(context.get_param("align_range_um", 2000.0)) * 1e-3,
            range_deg=float(context.get_param("align_range_deg", 8.0)),
            timeout_s=float(context.get_param("align_timeout_s", 300.0)),
            settle_s=float(context.get_param("align_settle_s", 0.05)),
            spiral_cart_threshold_w=mw("spiral_cart_threshold_mw", 0.1),
            spiral_ang_threshold_w=mw("spiral_ang_threshold_mw", 0.2),
            axial_threshold_w=mw("axial_threshold_mw", 0.25),
            gradient_threshold_w=mw("gradient_threshold_mw", 0.5),
        )


@dataclass(frozen=True)
class AlignmentResult:
    fiber: int
    #: The final reading reached the threshold.
    passed: bool
    #: That final reading (W).
    power_w: float
    #: Final pose.
    pose: Pose6
    #: Power readings taken, the final one included.
    reads: int
    duration_s: float
    timed_out: bool


class AlignmentIO:
    """What the algorithm may do: move the fiber, scan, and read the power."""

    def __init__(self, robot: Any, meter: Any, context: AppContext, settings: AlignmentSettings,
                 logger: Optional[logging.Logger] = None):
        self._robot = robot
        self._meter = meter
        self._context = context
        self._settings = settings
        self._log = logger or logging.getLogger(__name__)
        #: Where the search started: the centre of the window.
        self.start: Pose6 = self._target_pose()
        self._t0 = time.monotonic()
        self._deadline = self._t0 + settings.timeout_s
        #: Every reading so far.
        self.samples: List[Sample] = []
        #: The highest reading so far, or None before the first.
        self.best: Optional[Sample] = None
        self._checkpoints = iter(())
        # Power meter settings changed by the algorithm, with their original values.
        self._meter_originals: Dict[str, Any] = {}

    # --- For the algorithm ---------------------------------------------------

    def pose(self) -> Pose6:
        """The robot's actual pose now."""
        self._check()
        return self._actual_pose()

    def elapsed_s(self) -> float:
        return time.monotonic() - self._t0

    def move_to(self, pose: Sequence[float]) -> None:
        """Move in a straight line to ``pose`` and return once there and settled."""
        self._check()
        self._move(self._checked(pose))

    def move_rel_trf(self, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0,
                     drx: float = 0.0, dry: float = 0.0, drz: float = 0.0) -> None:
        """Move relative to the tool frame, like ``MoveLinRelTrf``.

        The target is computed here and sent as an absolute ``MoveLin``, so the
        pose checked against the window is exactly the pose the robot receives.
        """
        self._check()
        current = frames.pose_matrix(self._target_pose())
        relative = frames.make_homogeneous_matrix([dx, dy, dz], [drx, dry, drz])
        self._move(self._checked(frames.matrix_pose(current @ relative)))

    def read_power(self, kind: Optional[str] = None) -> float:
        """Power (W) now. Recorded with the pose, and drawn on the custom view."""
        self._check()
        return self._read(kind).power_w

    def scan(self, poses: Sequence[Sequence[float]], kind: Optional[str] = None,
             stop: Optional[Callable[[List[Sample]], bool]] = None) -> List[Sample]:
        """Move through ``poses`` in one continuous motion, reading the power all along.

        The moves are queued while the robot is paused, then run with the
        current blending, and readings are taken until the last pose is
        reached. If ``stop(samples)`` returns True (called after every reading,
        with this scan's samples), the motion is cleared and the scan ends
        there. Returns this scan's samples.
        """
        self._check()
        targets = [self._checked(pose) for pose in poses]
        if not targets:
            return []
        robot = self._robot
        scanned: List[Sample] = []
        t0 = time.monotonic()
        self._log.info(f"Alignment scan ({kind or '-'}): {len(targets)} pose(s) queued")
        try:
            robot.PauseMotion()
            for target in targets:
                robot.MoveLin(*target)
            done = robot.SetCheckpoint(self._next_checkpoint())
            robot.ResumeMotion()
            stopped = False
            moved_from, moved_at = None, time.monotonic()
            while not done.is_set():
                # mecademicpy never "sets" a checkpoint the robot discarded
                # (motion cleared, robot in error or not activated): without
                # this, the loop would wait for it until the timeout.
                try:
                    done.check_interrupted()
                except Exception as e:
                    raise AlignmentError(f"The robot dropped the scan ({e}). Robot: {self._robot_state()}") from e
                self._check()
                sample = self._read(kind)
                scanned.append(sample)
                if moved_from is None or _moved(sample.pose, moved_from):
                    moved_from, moved_at = sample.pose, time.monotonic()
                elif time.monotonic() - moved_at > SCAN_STALL_S:
                    raise AlignmentError(
                        f"The robot is not executing the scan: no motion for {SCAN_STALL_S:g} s. "
                        f"Robot: {self._robot_state()}"
                    )
                if stop is not None and stop(scanned):
                    self._clear_motion()
                    stopped = True
                    break
                time.sleep(SCAN_SAMPLE_PERIOD_S)
        except BaseException as e:
            self._clear_motion()
            self._log.warning(f"Alignment scan ({kind or '-'}) ended by {type(e).__name__} after "
                              f"{time.monotonic() - t0:.2f} s, {len(scanned)} reading(s)")
            raise
        best = max((s.power_w for s in scanned if s.power_w == s.power_w), default=float("nan"))
        self._log.info(f"Alignment scan ({kind or '-'}) {'stopped by the algorithm' if stopped else 'done'}: "
                       f"{len(scanned)} reading(s) in {time.monotonic() - t0:.2f} s, best {best:.4g} W")
        return scanned

    def set_motion(self, acc: Optional[float] = None, lin_vel: Optional[float] = None,
                   ang_vel: Optional[float] = None, blending: Optional[float] = None) -> None:
        """Cartesian acceleration (%), linear (mm/s) and angular (°/s) velocity, blending (%)."""
        self._check()
        self._apply_motion(acc, lin_vel, ang_vel, blending)

    def set_power_averaging(self, count: int) -> None:
        """Samples averaged per reading (about 3 ms each on a PM100x). Restored afterwards."""
        self._check()
        self._set_meter("averaging", int(count))

    def set_power_auto_range(self, enabled: bool) -> None:
        """Power meter auto range. Restored afterwards."""
        self._check()
        self._set_meter("auto_range", bool(enabled))

    # --- For run_alignment ---------------------------------------------------------

    def _check(self) -> None:
        self._check_interrupted()
        if time.monotonic() >= self._deadline:
            raise AlignmentTimeout(f"Alignment timed out after {self._settings.timeout_s:g} s.")

    def _check_interrupted(self) -> None:
        # ABORT clears the robot's motion queue, which makes a WaitIdle() in
        # progress raise. Between two moves (settling, reading the meter, or a
        # scan sampling) there is none to interrupt, and the robot accepts the
        # next move again. So the alignment also stops on the abort/fault event
        # the controller marks first. run_alignment marks align_start before the
        # algorithm runs, so an older abort can never be the latest event here.
        event = self._context.latest("events") or {}
        if event.get("name") in ("abort", "fault"):
            raise AlignmentInterrupted(f"Alignment interrupted ({event['name']}).")

    def _checked(self, pose: Sequence[float]) -> Pose6:
        """``pose`` as six floats, or AlignmentRangeError if it leaves the window."""
        try:
            target = tuple(float(v) for v in pose)
        except (TypeError, ValueError):
            raise AlignmentRangeError(f"Not a pose: {pose!r}") from None
        if len(target) != 6 or not all(math.isfinite(v) for v in target):
            raise AlignmentRangeError(f"Not a valid pose: {pose!r}")
        names = ("X", "Y", "Z", "alpha", "beta", "gamma")
        for i, name in enumerate(names):
            if i < 3:
                offset, limit, unit = target[i] - self.start[i], self._settings.range_mm, "mm"
            else:
                offset, limit, unit = _wrap_deg(target[i] - self.start[i]), self._settings.range_deg, "°"
            # The epsilon absorbs float noise on poses computed from the start.
            if abs(offset) > limit + 1e-9:
                raise AlignmentRangeError(
                    f"{name} {target[i]:.4f} is {offset:+.4f} {unit} from the start: "
                    f"outside the ±{limit:g} {unit} window. Pose: {target}"
                )
        return target

    def _move(self, target: Pose6) -> None:
        self._log.debug(f"Alignment move to {_fmt_pose(target)}")
        self._robot.MoveLin(*target)
        try:
            # Bounded: a robot that does not execute the move (paused, refused
            # it, ...) must not hold the task forever.
            self._robot.WaitIdle(timeout=MOVE_TIMEOUT_S)
        except Exception as e:
            self._clear_motion()
            raise AlignmentError(f"The robot did not complete a move to {_fmt_pose(target)} "
                                 f"({type(e).__name__}: {e}). Robot: {self._robot_state()}") from e
        time.sleep(self._settings.settle_s)

    def _read(self, kind: Optional[str]) -> Sample:
        # A direct reading on this (the task) thread. The custom view's sampler
        # shares the meter, and the driver serializes the two.
        value = self._meter.read_power()
        if value is None:
            raise AlignmentError("The power meter returned no reading: alignment needs the real power meter.")
        sample = Sample(self._actual_pose(), float(value), kind)
        self.samples.append(sample)
        self._context.publish("align_sample", [*sample.pose, sample.power_w, kind])
        # NaN never compares greater, so an invalid reading never stays the best.
        if self.best is None or math.isnan(self.best.power_w) or sample.power_w > self.best.power_w:
            self.best = sample
        return sample

    def _actual_pose(self) -> Pose6:
        return self._query_pose("GetRtCartPos")

    def _target_pose(self) -> Pose6:
        return self._query_pose("GetRtTargetCartPos")

    def _query_pose(self, command: str) -> Pose6:
        try:
            value = getattr(self._robot, command)(synchronous_update=True)
        except Exception as e:
            raise AlignmentError(f"{command} failed ({type(e).__name__}: {e}). Robot: {self._robot_state()}") from e
        return self._pose_from(value)

    def _robot_state(self) -> str:
        """The robot's status for an error message (activated, homed, sim, error, paused, ...). Never raises."""
        try:
            return str(self._robot.GetStatusRobot())
        except Exception as e:
            return f"status unavailable ({type(e).__name__})"

    @staticmethod
    def _pose_from(value: Any) -> Pose6:
        try:
            pose = tuple(float(v) for v in value[:6])
        except (TypeError, ValueError):
            pose = ()
        if len(pose) != 6:
            raise AlignmentError(f"The robot reported no position ({value!r}): alignment needs the real robot.")
        return pose

    def _apply_motion(self, acc, lin_vel, ang_vel, blending) -> None:
        robot = self._robot
        if acc is not None:
            robot.SetCartAcc(acc)
        if lin_vel is not None:
            robot.SetCartLinVel(lin_vel)
        if ang_vel is not None:
            robot.SetCartAngVel(ang_vel)
        if blending is not None:
            robot.SetBlending(blending)

    def _clear_motion(self) -> None:
        """Stop and empty the motion queue, then accept new moves. Never raises."""
        try:
            self._robot.ClearMotion()
            self._robot.ResumeMotion()
        except Exception:
            pass

    def _next_checkpoint(self) -> int:
        checkpoint = next(self._checkpoints, None)
        if checkpoint is None:
            self._checkpoints = iter(CHECKPOINT_IDS)
            checkpoint = next(self._checkpoints)
        return checkpoint

    def _set_meter(self, name: str, value: Any) -> None:
        if name not in self._meter_originals:
            self._meter_originals[name] = getattr(self._meter, name)
        setattr(self._meter, name, value)

    def _restore_meter(self) -> None:
        """Put back the meter settings the algorithm changed. Never raises."""
        for name, value in self._meter_originals.items():
            try:
                setattr(self._meter, name, value)
            except Exception:
                pass

    def _return_to_best(self) -> None:
        """Slowly back to the best pose read (not window-checked: the robot was there)."""
        if self.best is None:
            return
        self._apply_motion(**RETURN_MOTION)
        self._move(self._pose_from(self.best.pose))


#: The algorithm's signature, see algorithm.py.
Algorithm = Callable[[AlignmentIO, AlignmentSettings], None]


def run_alignment(robot: Any, meter: Any, context: AppContext, fiber: int,
                  align: Algorithm, logger: Optional[logging.Logger] = None) -> AlignmentResult:
    """Run ``align`` from the robot's current pose, then judge the result.

    Brackets the run with ``align_start`` / ``align_end`` events and publishes
    an ``align_result`` summary (with the points) for the custom view. Each
    phase is logged to ``logger`` (the robot's device log).
    """
    log = logger or logging.getLogger(__name__)
    settings = AlignmentSettings.from_context(context)
    io = AlignmentIO(robot, meter, context, settings, logger=log)
    log.info(f"Alignment of fiber {fiber}: start {_fmt_pose(io.start)}, window ±{settings.range_mm:g} mm / "
             f"±{settings.range_deg:g}°, threshold {settings.threshold_w:.4g} W, timeout {settings.timeout_s:g} s. "
             f"Robot: {io._robot_state()}")
    context.mark("align_start", fiber=fiber, threshold_w=settings.threshold_w, start=list(io.start),
                 range_mm=settings.range_mm, range_deg=settings.range_deg, plot_axes=list(PLOT_AXES))

    timed_out = False
    try:
        try:
            align(io, settings)
        except AlignmentTimeout:
            timed_out = True
            io._check_interrupted()
            log.warning(f"Alignment timed out after {settings.timeout_s:g} s: back to the best pose read "
                        f"({io.best.power_w if io.best else float('nan'):.4g} W)")
            io._return_to_best()
        # The verdict: a fresh reading where the algorithm left the fiber.
        io._check_interrupted()
        final = io._read(None)
    except Exception as e:
        # An abort lands here too, from WaitIdle() or as AlignmentInterrupted.
        # Close the run on the view, then let the task runner handle it.
        log.error(f"Alignment of fiber {fiber} failed after {io.elapsed_s():.1f} s: {type(e).__name__}: {e}")
        context.mark("align_end", fiber=fiber, passed=False, error=f"{type(e).__name__}: {e}")
        raise
    finally:
        io._restore_meter()

    result = AlignmentResult(
        fiber=fiber,
        passed=final.power_w >= settings.threshold_w,
        power_w=final.power_w,
        pose=final.pose,
        reads=len(io.samples),
        duration_s=io.elapsed_s(),
        timed_out=timed_out,
    )
    context.mark("align_end", fiber=fiber, passed=result.passed, power_w=final.power_w)
    kept = io.samples[::max(1, math.ceil(len(io.samples) / RESULT_POINTS_MAX))]
    if kept[-1] is not final:
        kept.append(final)
    context.publish("align_result", {
        "fiber": fiber,
        "passed": result.passed,
        "power_w": final.power_w,
        "threshold_w": settings.threshold_w,
        "start": list(io.start),
        "final": list(final.pose),
        "plot_axes": list(PLOT_AXES),
        "reads": result.reads,
        "duration_s": round(result.duration_s, 2),
        "timed_out": timed_out,
        "points": [[*s.pose, s.power_w, s.kind] for s in kept],
    })
    return result


def _wrap_deg(angle: float) -> float:
    """``angle`` brought into [-180, 180)."""
    return (angle + 180.0) % 360.0 - 180.0


def _moved(pose: Pose6, reference: Pose6) -> bool:
    """True once ``pose`` is more than SCAN_MOTION_EPS (mm or °) away from ``reference``."""
    return any(abs(_wrap_deg(a - b) if i >= 3 else a - b) > SCAN_MOTION_EPS
               for i, (a, b) in enumerate(zip(pose, reference)))


def _fmt_pose(pose: Sequence[float]) -> str:
    return "(" + ", ".join(f"{v:.4f}" for v in pose) + ")"
