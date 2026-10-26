"""The staged alignment (algorithm.py) on the simulated cell: cold, warm, off-tip TCP, dark."""

import pytest

from app_logic.fiber_alignment import algorithm
from app_logic.fiber_alignment.alignment import AlignmentError, AlignmentNoSignal

from .sim import Scenario, simulate

#: Cold and warm starts: (x, z) µm and (rx, rz) ° from the optimum, TCP error (µm), slow I/O.
CASES = {
    "warm": Scenario(offset=(0, 0, 0, 0)),
    "near": Scenario(offset=(120, -80, 0.8, -0.5)),
    "far": Scenario(offset=(300, 250, 1.5, 1.0), seed=3),
    "edge": Scenario(offset=(-450, 30, -2.0, 2.0), seed=4),
    "tcp-behind-tip": Scenario(offset=(80, 60, 1.0, -1.0), pivot_um=(0, 500, 0), seed=5),
    "tcp-askew": Scenario(offset=(80, 60, 1.0, -1.0), pivot_um=(300, -200, 150), seed=6),
    "slow-io": Scenario(offset=(150, 0, 0.5, 0.5), latency_s=0.004, pose_query_s=0.003, seed=7),
}


@pytest.fixture(scope="module", params=sorted(CASES))
def run(request):
    return simulate(CASES[request.param])


def test_it_reaches_the_peak(run):
    assert run.error is None, run.error
    assert run.result.passed
    assert run.result.power_w >= 0.97 * run.optics.best_power_w()
    assert run.result.duration_s < 120


def test_it_never_moves_along_the_fiber(run):
    # Rows are the TCP's offsets in the start's tool frame: y is the fiber axis.
    assert max(abs(row[1]) for row in run.rows) < 1e-3


def test_it_ends_on_the_best_reading_at_rest(run):
    best, final = run.summary["best_still"], run.summary["final_local"]
    assert abs(best[0] - final[0]) <= 1.0 and abs(best[2] - final[2]) <= 1.0


def test_it_runs_every_phase_in_order(run):
    names = [p["name"] for p in run.summary["phases"] if "t0" in p]
    assert names == list(algorithm.PHASES)
    assert all(p["status"] == "done" for p in run.summary["phases"])


def test_a_warm_start_skips_the_spiral():
    run = simulate(CASES["warm"])
    acquire = run.context.published("align_acquire")
    assert acquire[0]["warm"] and acquire[0]["found"]
    readings = [r for r in run.rows if r[8] == algorithm.PHASES.index("Acquire")]
    assert len(readings) == algorithm.NOISE_READS


def test_the_tcp_offset_shows_in_the_tilt_profiles():
    run = simulate(CASES["tcp-behind-tip"])
    pivots = [p["pivot_um"] for p in run.context.published("align_profile") if p["axis"] in ("rx", "rz")]
    # 500 µm behind the tip, plus the 20 µm gap the beam crosses.
    assert all(abs(p - 520) < 15 for p in pivots)


def test_no_light_stops_with_a_clear_error():
    run = simulate(Scenario(dark=True))
    assert isinstance(run.error, AlignmentNoSignal)
    assert "check the laser" in str(run.error)
    end = run.context.events("align_end")[-1]
    assert end["passed"] is False and "AlignmentNoSignal" in end["error"]
    assert run.summary["error"]


def test_a_window_too_small_for_the_spiral_is_refused_before_moving():
    run = simulate(Scenario(params={"acquire_radius_um": 1950.0}))
    assert isinstance(run.error, AlignmentError)
    assert "acquire_radius_um" in str(run.error)
    assert run.robot.moves == []
