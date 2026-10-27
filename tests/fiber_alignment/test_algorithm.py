"""The staged alignment (algorithm.py) on the simulated cell.

Mostly on optics like the cell's (collimated ends: broad laterally, narrow in
tilt), from its run of 2026-10-27 to cold starts; and on bare single-mode
fibers, to keep the algorithm free of any one scale.
"""

import math

import pytest

from app_logic.fiber_alignment import algorithm
from app_logic.fiber_alignment.alignment import AlignmentError, AlignmentNoSignal

from .sim import Scenario, simulate

#: Starts: (x, z) µm and (rx, rz) ° from the optimum, and how the cell behaves.
CASES = {
    # The cell's run of 2026-10-27 (~80 µW at the start), which failed in the Angle phase.
    "cell-run": Scenario(offset=(50, 0, 0.45, 0.0)),
    "few-uW": Scenario(offset=(90, -40, 0.5, -0.35), seed=2),
    # No light at the start, the tilt off: the tilt spiral finds it.
    "angle-off": Scenario(offset=(40, 30, 1.2, -0.4), seed=3),
    # No light at the start, the tilt right but 600 µm off: the lateral spiral finds it.
    "lateral-off": Scenario(offset=(600, 0, 0.1, 0.0), seed=4, params={"acquire_radius_um": 800.0}),
    "slow-io": Scenario(offset=(80, 40, 0.3, 0.2), latency_s=0.004, pose_query_s=0.003, seed=7),
    # Bare single-mode fibers: the same algorithm, other widths.
    "smf-near": Scenario(optics="smf", offset=(120, -80, 0.8, -0.5)),
    "smf-far": Scenario(optics="smf", offset=(300, 250, 1.5, 1.0), seed=3),
    "smf-tcp-behind-tip": Scenario(optics="smf", offset=(80, 60, 1.0, -1.0), pivot_um=(0, 500, 0), seed=5),
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


def searches(run):
    return [a["search"] for a in run.context.published("align_acquire")]


def test_light_at_the_start_skips_the_searches():
    for name in ("cell-run", "few-uW"):
        run = simulate(CASES[name])
        assert run.rows[0][6] >= 1e-6, f"{name}: no light at the start"
        assert searches(run) == [None]
        readings = [r for r in run.rows if r[8] == algorithm.PHASES.index("Acquire")]
        assert len(readings) == algorithm.NOISE_READS


def test_no_light_tilts_first_then_moves():
    run = simulate(CASES["angle-off"])
    assert run.rows[0][6] < 1e-6
    assert searches(run) == ["angle", "angle"]
    run = simulate(CASES["lateral-off"])
    assert searches(run) == ["angle", "lateral", "lateral"]


def test_the_walk_shows_the_tcp_off_the_tip():
    run = simulate(CASES["smf-tcp-behind-tip"])
    walks = [p["walk_um_per_deg"] for p in run.context.published("align_profile") if p["axis"] in ("rx", "rz")]
    # 500 µm behind the tip, plus the 20 µm gap the beam crosses, per degree.
    expected = 520 * math.pi / 180
    assert walks and all(w is not None and abs(abs(w) - expected) < 0.5 for w in walks)


def test_no_light_stops_with_a_clear_error():
    run = simulate(Scenario(dark=True))
    assert isinstance(run.error, AlignmentNoSignal)
    assert "tilting" in str(run.error) and "moving" in str(run.error) and "check the laser" in str(run.error)
    assert searches(run) == ["angle", "lateral"]
    end = run.context.events("align_end")[-1]
    assert end["passed"] is False and "AlignmentNoSignal" in end["error"]
    assert run.summary["error"]


@pytest.mark.parametrize("params, name", [
    ({"acquire_radius_um": 1950.0}, "acquire_radius_um"),
    ({"acquire_radius_deg": 7.8}, "acquire_radius_deg"),
])
def test_a_window_too_small_for_a_search_is_refused_before_moving(params, name):
    run = simulate(Scenario(params=params))
    assert isinstance(run.error, AlignmentError)
    assert name in str(run.error)
    assert run.robot.moves == []


# --- One cross scan --------------------------------------------------------------------------

def cross_scan_once(scenario, width_um, start_x):
    """One cross_scan along x from ``start_x`` with a width guess of ``width_um``."""
    out = {}

    def probe(io, settings):
        with io.phase("Probe"):
            state = algorithm._State(settings, width={"x": width_um, "z": width_um}, angle_width=0.3,
                                     scan_range_w=1.2e-3, detect_w=1e-6)
            state.pos["x"] = start_x
            out["lit"] = algorithm.cross_scan(io, state, "x", 0)
            out["state"] = state

    run = simulate(scenario, align=probe, phases=("Probe",), phase_views={})
    assert run.error is None, run.error
    return run, out["lit"], out["state"]


def scanned_span(run):
    spans = [p["span"] for p in run.context.published("align_profile")]
    return min(s[0] for s in spans), max(s[1] for s in spans)


def test_a_window_narrower_than_the_beam_still_finds_its_centre():
    # A ±45 µm window on a beam of 240 µm: the fit over the noise floor still centres it.
    run, lit, state = cross_scan_once(Scenario(), width_um=30.0, start_x=20.0)
    assert lit
    assert abs(state.pos["x"]) < 2.0
    assert 30.0 < state.width["x"] <= 90.0  # grows towards 240 µm, ×3 at most per fit


def test_a_centre_out_of_reach_is_never_extrapolated():
    # The centre 400 µm away: the windows (±45 µm, moving by a half-span) cannot reach it.
    run, lit, state = cross_scan_once(Scenario(offset=(400, 0, 0, 0)), width_um=30.0, start_x=0.0)
    lo, hi = scanned_span(run)
    assert lit
    assert lo <= state.pos["x"] <= hi < 400.0
    best = max((pt for p in run.context.published("align_profile") for q in p["passes"] for pt in q["points"]),
               key=lambda pt: pt[1])
    assert state.pos["x"] == pytest.approx(best[0], abs=1e-3)  # published to the nm


def test_no_light_along_the_line_leaves_the_fiber_where_it_is():
    run, lit, state = cross_scan_once(Scenario(dark=True), width_um=30.0, start_x=12.0)
    assert not lit
    assert state.pos["x"] == 12.0
    # The window doubled before giving up.
    spans = [p["span"][1] - p["span"][0] for p in run.context.published("align_profile")]
    assert len(spans) == algorithm.DARK_WIDENINGS + 1 and spans[-1] > spans[0] * 7
