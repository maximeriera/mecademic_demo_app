"""PLACEHOLDER alignment algorithm, to be replaced by the colleague's.

Contract (see alignment.py)::

    def align(io: AlignmentIO, settings: AlignmentSettings) -> None

* Move with ``io.move_to(x, y)`` / ``io.move_by(dx, dy)`` (mm, robot WRF X/Y)
  and read with ``io.read_power()`` (W). ``io.start`` is where the search
  starts. ``io.best`` is the best ``(x, y, power_w)`` read so far.
* Stay within ``settings.range_mm`` of ``io.start`` in X and in Y. A move
  outside that window raises AlignmentRangeError.
* Leave the fiber at the best position found. The verdict is a fresh reading
  there, compared with ``settings.threshold_w``, so stopping as soon as a
  reading reaches it is fine.
* After ``settings.timeout_s`` every io call raises AlignmentTimeout. Let it
  propagate: the caller moves back to ``io.best`` and judges that.
* Never catch exceptions broadly. An ABORT surfaces as an exception from any
  io call and must reach the task runner.

To plug in the colleague's code, either replace the body of :func:`align`, or
drop their module next to this one and call it from here, e.g.::

    from .colleague_align import optimise

    def align(io, settings):
        optimise(move=io.move_to, read=io.read_power, start=io.start, ...)

What follows is a deliberately simple stand-in, good enough to exercise the
cell, the contract and the custom view. It walks a square spiral around the
start, takes one reading per point, and stops at the first reading that
reaches the threshold.
"""

from typing import Iterator, Tuple

from .alignment import AlignmentIO, AlignmentSettings


def align(io: AlignmentIO, settings: AlignmentSettings) -> None:
    if io.read_power() >= settings.threshold_w:
        return
    x0, y0 = io.start
    step = settings.step_mm
    rings = int(settings.range_mm / step + 1e-9)
    for ring in range(1, rings + 1):
        for i, j in _ring(ring):
            io.move_to(x0 + i * step, y0 + j * step)
            if io.read_power() >= settings.threshold_w:
                return
    # Threshold never reached: settle on the best point found.
    best_x, best_y, _ = io.best
    io.move_to(best_x, best_y)


def _ring(k: int) -> Iterator[Tuple[int, int]]:
    """Grid points (in steps) of the square ring at distance ``k``, walked as one continuous spiral.

    Starts one step right of where ring ``k - 1`` ended, then goes up the right
    side, left along the top, down the left side and right along the bottom.
    """
    for j in range(-k + 1, k + 1):
        yield k, j
    for i in range(k - 1, -k - 1, -1):
        yield i, k
    for j in range(k - 1, -k - 1, -1):
        yield -k, j
    for i in range(-k + 1, k + 1):
        yield i, -k
