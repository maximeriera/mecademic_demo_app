"""What the alignment publishes is what the custom view plots (custom_view/__init__.py).

The view draws one card per declared phase, as PHASE_VIEWS says, from the
rows and the payloads below. These checks keep the two in step: a payload the
view would not plot, a phase it would not know how to draw, or rows that are
not where the robot was, fail here.
"""

import math
import re
from pathlib import Path

import numpy as np
import pytest

from app_logic.fiber_alignment import algorithm, alignment, frames

from .sim import START_POSE, Scenario, simulate

VIEW_JS = Path(__file__).resolve().parents[2] / "app_logic" / "custom_view" / "view.js"
AXES = {"x", "y", "z", "rx", "ry", "rz"}
KINDS = {"cart", "ang", None}


@pytest.fixture(scope="module")
def run():
    run = simulate(Scenario(offset=(160, -120, 1.0, -0.8), seed=11))
    assert run.error is None, run.error
    return run


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def test_every_phase_declares_a_view_the_page_draws():
    renderers = set(re.findall(r"^\s*(\w+): render\w+,\s*$", VIEW_JS.read_text(encoding="utf-8"), re.M))
    assert set(algorithm.PHASE_VIEWS) == set(algorithm.PHASES)
    for name, view in algorithm.PHASE_VIEWS.items():
        assert view["type"] in renderers, f"{name}: no renderer for {view['type']!r} in view.js"
        assert set(view.get("axes", [])) <= AXES and set(view.get("slots", [])) <= AXES


def test_align_start_announces_the_phases_and_the_row_format(run):
    start = run.context.events("align_start")[-1]
    assert start["phases"] == list(algorithm.PHASES)
    assert start["phase_views"] == algorithm.PHASE_VIEWS
    assert start["sample_format"] == alignment.SAMPLE_FORMAT == 2
    assert start["source_power_w"] == pytest.approx(1e-3)


def test_rows_have_the_documented_shape(run):
    for row in run.rows:
        assert len(row) == 11
        assert all(is_num(v) for v in row[:7])
        assert row[7] in KINDS
        assert isinstance(row[8], int) and -1 <= row[8] < len(algorithm.PHASES)
        assert is_num(row[9]) and isinstance(row[10], bool)


def test_payloads_go_to_phases_whose_card_plots_them(run):
    names = algorithm.PHASES
    for p in run.context.published("align_profile"):
        view = algorithm.PHASE_VIEWS[names[p["phase"]]]
        assert view["type"] == "profiles" and p["axis"] in view["slots"]
        assert p["unit"] == ("°" if p["axis"].startswith("r") else "µm")
        lo, hi = p["span"]
        assert lo < hi
        assert p["passes"] and all(q["dir"] in (1, -1) for q in p["passes"])
        assert all(len(pt) == 2 and is_num(pt[0]) for q in p["passes"] for pt in q["points"])
        if p["fit"] is not None:
            assert p["fit"]["model"] in ("gauss", "parabola")
            assert is_num(p["fit"]["center"])
    for p in run.context.published("align_peak"):
        assert algorithm.PHASE_VIEWS[names[p["phase"]]]["type"] == "peak"
        assert len(p["center"]) == 2 and is_num(p["step_um"]) and is_num(p["radius_um"])
        assert all(len(pt) == 3 for pt in p["points"])
    for p in run.context.published("align_acquire"):
        assert algorithm.PHASE_VIEWS[names[p["phase"]]]["type"] == "map"
        assert {"noise_w", "detect_w", "radius_um", "found", "warm"} <= set(p)


def test_still_readings_and_payloads_survive_into_the_summary(run):
    summary = run.summary
    assert sum(1 for r in summary["points"] if r[10]) == sum(1 for r in run.rows if r[10])
    for name in ("acquire", "profile", "peak"):
        assert len(summary["published"][name]) == len(run.context.published(f"align_{name}"))
    assert summary["phase_views"] == algorithm.PHASE_VIEWS


def test_plotted_spiral_readings_lie_on_the_commanded_path(run):
    # Rule: what the view plots is where the robot was. Spiral readings are
    # interpolated between poses: they must sit on the spiral the robot ran.
    inverse = np.linalg.inv(frames.pose_matrix(START_POSE))
    path = np.array([(inverse @ frames.pose_matrix(m))[[0, 2], 3] * 1000.0 for m in run.robot.moves])
    acquire = algorithm.PHASES.index("Acquire")
    spiral = np.array([[r[0], r[2]] for r in run.rows if r[8] == acquire and not r[10]])[algorithm.NOISE_READS:]
    assert len(spiral) > 100
    a, b = path[:-1], path[1:]
    ab = b - a
    distances = []
    for chunk in np.array_split(spiral, max(1, len(spiral) // 200)):
        ap = chunk[:, None, :] - a[None, :, :]
        t = np.clip(np.einsum("nmk,mk->nm", ap, ab) / np.maximum(np.einsum("mk,mk->m", ab, ab), 1e-12), 0, 1)
        distances.extend(np.linalg.norm(ap - t[..., None] * ab[None, :, :], axis=2).min(axis=1))
    # A reading's pose lies on the chord between the poses read around it,
    # which cuts the spiral's turns by about spacing² / (8 radius): up to
    # ~1.5 µm on the tight turns at its centre, a fraction of a µm beyond.
    radius = np.hypot(spiral[:, 0], spiral[:, 1])
    excess = np.array(distances) - (0.3 + 15.0 / np.maximum(radius, 1.0))
    worst = int(np.argmax(excess))
    assert excess[worst] <= 0, (f"a spiral reading {radius[worst]:.0f} µm from the start is "
                                f"{distances[worst]:.2f} µm off the commanded path")


def test_the_result_matches_the_verdict_event(run):
    end = run.context.events("align_end")[-1]
    assert end["power_w"] == run.summary["power_w"] == run.result.power_w
    assert end["passed"] == run.summary["passed"] == run.result.passed
    assert math.isfinite(run.summary["best_still"][6])
