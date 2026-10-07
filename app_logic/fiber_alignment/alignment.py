"""The contract between the cell and the alignment algorithm.

The algorithm (algorithm.py, supplied by a colleague) never touches a device.
It receives an :class:`AlignmentIO`, which can move the held fiber in the
robot's X/Y plane and read the power meter, plus the :class:`AlignmentSettings`.
It must leave the fiber at the best position it found, and it may stop as soon
as the threshold is reached. Everything else is handled here:

* Every reading is recorded and published to the custom view (``align_sample``).
  The graph therefore shows exactly what the algorithm measured, and moves only
  while an alignment runs.
* Moves are confined to ``align_range_um`` around the start, because the fiber
  tip is only microns from the fixed one. The run is capped at ``align_timeout_s``.
* The verdict is a fresh reading at the final position, compared with
  ``power_threshold_mw``. The algorithm's own claim is not used.

Units at this boundary are mm (robot coordinates, WRF) and W (power meter). The
lateral plane is the robot's WRF X/Y, i.e. the fibers face each other along Z.
Z and the orientation stay as they were at the start.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

from core import AppContext


class AlignmentError(RuntimeError):
    """The alignment cannot run, or the algorithm broke the contract."""


class AlignmentRangeError(AlignmentError):
    """The algorithm asked for a move outside the search window."""


class AlignmentInterrupted(AlignmentError):
    """ABORT was pressed, or a device faulted, while the alignment was running."""


class AlignmentTimeout(Exception):
    """Raised by every AlignmentIO call once ``timeout_s`` has elapsed.

    Not an error: run_alignment() catches it, moves back to the best position
    read so far and judges that. An algorithm may catch it to clean up, but it
    must not move again.
    """


@dataclass(frozen=True)
class AlignmentSettings:
    #: The alignment passes once the power meter reads at least this (W).
    threshold_w: float
    #: Suggested search step (mm).
    step_mm: float
    #: How far from the start the fiber may go, in X and in Y (mm).
    range_mm: float
    timeout_s: float
    #: Pause after each move, before a reading (s).
    settle_s: float

    @classmethod
    def from_context(cls, context: AppContext) -> "AlignmentSettings":
        """Read the params from context.yaml, converted to W and mm."""
        return cls(
            threshold_w=float(context.get_param("power_threshold_mw", 0.5)) * 1e-3,
            step_mm=float(context.get_param("align_step_um", 5.0)) * 1e-3,
            range_mm=float(context.get_param("align_range_um", 100.0)) * 1e-3,
            timeout_s=float(context.get_param("align_timeout_s", 60.0)),
            settle_s=float(context.get_param("align_settle_s", 0.05)),
        )


@dataclass(frozen=True)
class AlignmentResult:
    fiber: int
    #: The final reading reached the threshold.
    passed: bool
    #: That final reading (W).
    power_w: float
    #: Final position (mm, WRF).
    x: float
    y: float
    #: Power readings taken, the final one included.
    reads: int
    duration_s: float
    timed_out: bool


class AlignmentIO:
    """What the algorithm may do: move the fiber in X/Y and read the power."""

    def __init__(self, robot: Any, meter: Any, context: AppContext, settings: AlignmentSettings):
        self._robot = robot
        self._meter = meter
        self._context = context
        self._settings = settings

        pose = robot.GetRtTargetCartPos(synchronous_update=True)
        if not pose or len(pose) < 6:
            raise AlignmentError(f"The robot reported no position ({pose!r}): alignment needs the real robot.")
        x, y, *rest = (float(v) for v in pose[:6])
        #: Where the search started (mm, WRF). The window is ``start ± range_mm``.
        self.start: Tuple[float, float] = (x, y)
        self._z_and_orientation = tuple(rest)
        self._xy = (x, y)
        self._t0 = time.monotonic()
        self._deadline = self._t0 + settings.timeout_s
        #: Every reading so far, as (x, y, power_w).
        self.samples: List[Tuple[float, float, float]] = []
        #: The highest reading so far, as (x, y, power_w), or None before the first.
        self.best: Optional[Tuple[float, float, float]] = None

    # --- For the algorithm ---------------------------------------------------

    def position(self) -> Tuple[float, float]:
        """Current X, Y (mm, WRF): the last position reached."""
        return self._xy

    def elapsed_s(self) -> float:
        return time.monotonic() - self._t0

    def move_to(self, x: float, y: float) -> None:
        """Move the fiber to X, Y (mm, WRF) in a straight line. Returns once settled."""
        self._check()
        x0, y0 = self.start
        r = self._settings.range_mm
        # The epsilon absorbs float noise on points computed as start + k * step.
        if abs(x - x0) > r + 1e-9 or abs(y - y0) > r + 1e-9:
            raise AlignmentRangeError(
                f"Move to ({x:.4f}, {y:.4f}) mm is outside the search window "
                f"({x0:.4f} ± {r:.4f}, {y0:.4f} ± {r:.4f}) mm."
            )
        self._move(x, y)

    def move_by(self, dx: float, dy: float) -> None:
        """Move the fiber by dX, dY (mm, WRF) from the current position."""
        x, y = self._xy
        self.move_to(x + dx, y + dy)

    def read_power(self) -> float:
        """Power (W) at the current position. Recorded, and drawn on the custom view."""
        self._check()
        return self._read()

    # --- For run_alignment (no time limit) --------------------------------------

    def _check(self) -> None:
        self._check_interrupted()
        if time.monotonic() >= self._deadline:
            raise AlignmentTimeout(f"Alignment timed out after {self._settings.timeout_s:g} s.")

    def _check_interrupted(self) -> None:
        # ABORT clears the robot's motion queue, which makes a WaitIdle() in
        # progress raise. Between two moves (settling, reading the meter) there
        # is none to interrupt, and the robot accepts the next move again. So
        # the loop also stops on the abort/fault event the controller marks
        # first. run_alignment marks align_start before the algorithm runs, so
        # an older abort can never be the latest event here.
        event = self._context.latest("events") or {}
        if event.get("name") in ("abort", "fault"):
            raise AlignmentInterrupted(f"Alignment interrupted ({event['name']}).")

    def _move(self, x: float, y: float) -> None:
        self._robot.MoveLin(x, y, *self._z_and_orientation)
        self._robot.WaitIdle()
        self._xy = (x, y)
        time.sleep(self._settings.settle_s)

    def _read(self) -> float:
        # A direct reading on this (the task) thread: it is taken after the
        # move has settled. The custom view's sampler shares the meter, and
        # the driver serializes the two.
        value = self._meter.read_power()
        if value is None:
            raise AlignmentError("The power meter returned no reading: alignment needs the real power meter.")
        watts = float(value)
        x, y = self._xy
        self.samples.append((x, y, watts))
        self._context.publish("align_sample", [x, y, watts])
        # NaN never compares greater, so an invalid reading is never the best.
        if self.best is None or watts > self.best[2]:
            self.best = (x, y, watts)
        return watts

    def _return_to_best(self) -> None:
        if self.best is not None and (self.best[0], self.best[1]) != self._xy:
            self._move(self.best[0], self.best[1])


#: The algorithm's signature, see algorithm.py.
Algorithm = Callable[[AlignmentIO, AlignmentSettings], None]


def run_alignment(robot: Any, meter: Any, context: AppContext, fiber: int,
                  align: Algorithm) -> AlignmentResult:
    """Run ``align`` from the robot's current position, then judge the result.

    Brackets the run with ``align_start`` / ``align_end`` events and publishes
    an ``align_result`` summary (with every point) for the custom view.
    """
    settings = AlignmentSettings.from_context(context)
    io = AlignmentIO(robot, meter, context, settings)
    x0, y0 = io.start
    r = settings.range_mm
    window = {"x": [x0 - r, x0 + r], "y": [y0 - r, y0 + r]}
    context.mark("align_start", fiber=fiber, threshold_w=settings.threshold_w, **window)

    timed_out = False
    try:
        try:
            align(io, settings)
        except AlignmentTimeout:
            timed_out = True
            io._check_interrupted()
            io._return_to_best()
        # The verdict: a fresh reading where the algorithm left the fiber.
        io._check_interrupted()
        power_w = io._read()
    except Exception as e:
        # An abort lands here too, from WaitIdle() or as AlignmentInterrupted.
        # Close the run on the view, then let the task runner handle it.
        context.mark("align_end", fiber=fiber, passed=False, error=f"{type(e).__name__}: {e}")
        raise

    x, y = io.position()
    result = AlignmentResult(
        fiber=fiber,
        passed=power_w >= settings.threshold_w,
        power_w=power_w,
        x=x,
        y=y,
        reads=len(io.samples),
        duration_s=io.elapsed_s(),
        timed_out=timed_out,
    )
    context.mark("align_end", fiber=fiber, passed=result.passed, power_w=power_w)
    context.publish("align_result", {
        "fiber": fiber,
        "passed": result.passed,
        "power_w": power_w,
        "threshold_w": settings.threshold_w,
        "final": [x, y],
        "reads": result.reads,
        "duration_s": round(result.duration_s, 2),
        "timed_out": timed_out,
        "points": [list(sample) for sample in io.samples],
        **window,
    })
    return result
