"""Tests for the coverage and timing model, including the plan's illustrative numbers.

The two-laptop plan (section 3.4) reports, for a 20 m x 20 m area, a 4 m
square footprint equal to the lane spacing, 1 m/s, 1.5 s per move, 5 m
maximum move, a 1 m gap and a Gaussian LKP prior of sigma 4 m at the centre:

    one drone, parallel track        152.0 s  24 moves  E[T] 74.1 s uniform, 74.2 s LKP
    two drones, parallel split        88.5 s  14 moves  E[T] 36.3 s uniform, 38.5 s LKP
    two drones, split expanding sq.   97.0 s  20 moves  E[T] 42.3 s uniform, 20.9 s LKP

Makespans and move counts reproduce exactly. Expected times reproduce within
0.15 s; ours are about 0.08 s lower throughout, consistent with the plan's
scratch simulation sampling positions at discrete time steps where this module
computes the first-detection instant exactly. The plan's parallel-split
layout (reconstructed below) puts the shared middle lane on each zone's inner
edge; ``plan_parallel_split`` spaces lanes evenly instead and is 1.5 s faster
(87.0 s), so its expected times differ (38.6 s uniform, 38.7 s LKP).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from flightrisk.drone import coverage_sim as cs
from flightrisk.drone import fleet_patterns as fp
from flightrisk.drone.coverage import tello_sweep_model

AREA = 2000
SWEEP = 400
LKP = (1000, 1000)
SIGMA = 400
TIMING = cs.TimingModel(speed_cm_s=100.0, move_overhead_s=1.5)
E_TOL = 0.15


def _estimates(plan, **kwargs):
    uniform = cs.estimate_plan(plan, timing=TIMING, prior="uniform", **kwargs)
    lkp = cs.estimate_plan(plan, timing=TIMING, prior="lkp", sigma_cm=SIGMA, **kwargs)
    return uniform, lkp


def _plan_example_parallel_split() -> fp.FleetSearchPlan:
    """The parallel-split layout behind the plan's 88.5 s / 36.3 s / 38.5 s row."""
    drones = []
    for drone_id, xs, region in (
        ("drone-1", (750, 350, 0), fp.Region(0, 0, 950, AREA)),
        ("drone-2", (1800, 1400, 1050), fp.Region(1050, 0, AREA, AREA)),
    ):
        points = []
        for k, x in enumerate(xs):
            ends = (0, AREA) if k % 2 == 0 else (AREA, 0)
            points += [(x, ends[0]), (x, ends[1])]
        drones.append(fp.DronePlan(drone_id, points[0], region, 100, tuple(fp._polyline_lanes(points, region))))
    return fp.FleetSearchPlan(fp.PatternKind.PARALLEL_SPLIT, (AREA, AREA), SWEEP, 100, tuple(drones), lkp_cm=LKP)


# ---------------------------------------------------------------------------
# The plan's illustrative numbers
# ---------------------------------------------------------------------------


def test_single_drone_parallel_track_reproduces_the_plan():
    plan = fp.plan_search(["drone-1"], AREA, SWEEP, min_separation_cm=None)
    plan = fp.FleetSearchPlan(plan.pattern, plan.area_cm, plan.sweep_cm, plan.gap_cm, plan.drones, lkp_cm=LKP)
    uniform, lkp = _estimates(plan)
    assert uniform.makespan_s == pytest.approx(152.0)
    assert uniform.drones[0].moves == 24
    assert uniform.coverage == 1.0
    assert uniform.expected_time_to_detection_s == pytest.approx(74.1, abs=E_TOL)
    assert lkp.expected_time_to_detection_s == pytest.approx(74.2, abs=E_TOL)


def test_plan_layout_of_the_parallel_split_reproduces_the_plan():
    uniform, lkp = _estimates(_plan_example_parallel_split())
    assert uniform.makespan_s == pytest.approx(88.5)
    assert [d.moves for d in uniform.drones] == [14, 14]
    assert uniform.coverage == 1.0
    assert uniform.expected_time_to_detection_s == pytest.approx(36.3, abs=E_TOL)
    assert lkp.expected_time_to_detection_s == pytest.approx(38.5, abs=E_TOL)


def test_generated_parallel_split_is_no_slower_than_the_plan():
    plan = fp.plan_search(["drone-1", "drone-2"], AREA, SWEEP, gap_cm=100, min_separation_cm=100)
    assert plan.pattern is fp.PatternKind.PARALLEL_SPLIT
    plan = fp.FleetSearchPlan(plan.pattern, plan.area_cm, plan.sweep_cm, plan.gap_cm, plan.drones, lkp_cm=LKP)
    uniform, lkp = _estimates(plan)
    assert uniform.makespan_s == pytest.approx(87.0)
    assert uniform.makespan_s <= 88.5
    assert [d.moves for d in uniform.drones] == [14, 14]
    assert uniform.coverage == 1.0
    assert uniform.expected_time_to_detection_s == pytest.approx(38.6, abs=E_TOL)
    assert lkp.expected_time_to_detection_s == pytest.approx(38.7, abs=E_TOL)


def test_split_expanding_square_reproduces_the_plan():
    plan = fp.plan_search(["drone-1", "drone-2"], AREA, SWEEP, LKP, gap_cm=100, min_separation_cm=100)
    assert plan.pattern is fp.PatternKind.SPLIT_EXPANDING_SQUARE
    # The plan's numbers exclude the takeoff transit and the 2 s stagger.
    uniform, lkp = _estimates(plan, include_transit=False)
    assert uniform.makespan_s == pytest.approx(97.0)
    assert [d.moves for d in uniform.drones] == [20, 20]
    assert uniform.coverage == 1.0
    assert uniform.expected_time_to_detection_s == pytest.approx(42.3, abs=E_TOL)
    assert lkp.expected_time_to_detection_s == pytest.approx(20.9, abs=E_TOL)
    # Flown as planned (back 2 m and in 0.5 m from the takeoff marks, the
    # second drone 2 s later) it costs 7.5 s more in total.
    full_uniform, full_lkp = _estimates(plan)
    assert full_uniform.makespan_s == pytest.approx(104.5)
    assert full_uniform.drone("drone-2").transit_s == pytest.approx(5.5)
    assert full_uniform.coverage == 1.0
    assert full_lkp.expected_time_to_detection_s < 38.5


def test_split_square_halves_time_to_detection_against_the_parallel_split():
    square = fp.plan_search(["a", "b"], AREA, SWEEP, LKP, gap_cm=100, min_separation_cm=100)
    _, square_lkp = _estimates(square, include_transit=False)
    _, parallel_lkp = _estimates(_plan_example_parallel_split())
    assert square_lkp.expected_time_to_detection_s < 0.6 * parallel_lkp.expected_time_to_detection_s


# ---------------------------------------------------------------------------
# Timing model
# ---------------------------------------------------------------------------


def test_total_time_is_overhead_plus_distance_over_speed_plus_fixed_costs():
    lanes = [{"direction": "forward", "distance_cm": 1200}, {"direction": "right", "distance_cm": 50}]
    timing = cs.TimingModel(speed_cm_s=50.0, move_overhead_s=2.0, takeoff_s=3.0, land_s=4.0)
    track = cs.timeline((0, 0), lanes, timing, start_offset_s=1.0)
    # 1200 cm is three executor moves of 400 cm: four moves in all.
    expected = 1.0 + 3.0 + 4 * 2.0 + 1250 / 50.0 + 4.0
    assert track[-1].t1 == pytest.approx(expected)
    assert (track[-1].x1, track[-1].y1) == (50, 1200)
    assert not track[0].airborne and not track[-1].airborne


def test_turn_cost_applies_only_when_the_direction_changes():
    lanes = [{"direction": "forward", "distance_cm": 100}] * 2 + [{"direction": "right", "distance_cm": 100}]
    base = cs.timeline((0, 0), lanes, cs.TimingModel(move_overhead_s=1.0))[-1].t1
    turned = cs.timeline((0, 0), lanes, cs.TimingModel(move_overhead_s=1.0, turn_extra_s=2.5))[-1].t1
    assert turned - base == pytest.approx(2.5)


def test_scales_stretch_each_move():
    lanes = [{"direction": "forward", "distance_cm": 100}, {"direction": "back", "distance_cm": 100}]
    track = cs.timeline((0, 0), lanes, cs.TimingModel(move_overhead_s=1.0), scales=[1.3, 0.7])
    assert track[-1].t1 == pytest.approx(1.3 * 2.0 + 0.7 * 2.0)
    with pytest.raises(ValueError):
        cs.timeline((0, 0), lanes, scales=[1.0])


def test_invalid_timing_is_rejected():
    with pytest.raises(ValueError):
        cs.TimingModel(speed_cm_s=0)
    with pytest.raises(ValueError):
        cs.TimingModel(move_overhead_s=-1)


def test_transit_time_is_reported_separately():
    plan = fp.plan_split_expanding_square(["a", "b"], AREA, LKP, SWEEP, gap_cm=100, min_separation_cm=100)
    estimate = cs.estimate_plan(plan, timing=TIMING)
    for drone in estimate.drones:
        assert drone.transit_s == pytest.approx(2.0 + 0.5 + 2 * 1.5)
    assert estimate.drone("b").start_offset_s == 2.0


# ---------------------------------------------------------------------------
# Detection model
# ---------------------------------------------------------------------------


def test_first_detection_time_is_exact_along_a_lane():
    raster = cs.Raster(1000, 100, cell_cm=100)
    track = cs.timeline((0, 50), [{"direction": "right", "distance_cm": 500}], cs.TimingModel(move_overhead_s=1.0))
    times = cs.first_detection_times(raster, [track], cs.RectFootprint.square(100)).reshape(10, 1)[:, 0]
    # Cell centres at x = 50, 150, ...; a centre enters the footprint 50 cm before the drone reaches it.
    assert times[0] == 0.0
    assert times[1] == pytest.approx(1.0 + 1.0)
    assert times[4] == pytest.approx(1.0 + 4.0)
    assert times[5] == pytest.approx(1.0 + 5.0)
    assert math.isinf(times[6])


def test_forward_footprint_sees_ahead_not_behind():
    footprint = cs.ForwardFootprint(near_cm=100, far_cm=300, half_tan=1.0)
    raster = cs.Raster(1000, 1000, cell_cm=100)
    hover = [cs.Segment(0.0, 1.0, 500, 500, 500, 500)]
    times = cs.first_detection_times(raster, [hover], footprint).reshape(10, 10)
    seen = {(i, j) for i in range(10) for j in range(10) if np.isfinite(times[i, j])}
    # Drone at (500, 500): cell centres 150-250 cm ahead (y 650, 750) and
    # within dy * tan of the axis are visible; nothing behind or beside.
    assert (5, 6) in seen and (5, 7) in seen and (4, 6) in seen
    assert (5, 4) not in seen and (5, 5) not in seen and (2, 6) not in seen


def test_forward_footprint_from_the_tello_sweep_model():
    model = tello_sweep_model(height_m=1.0, min_person_px=32.0, max_range_m=4.0)
    footprint = cs.ForwardFootprint.from_sweep_model(model)
    assert footprint.near_cm == pytest.approx(model.near_m * 100)
    assert footprint.far_cm == pytest.approx(400)
    assert footprint.max_half_width_cm == pytest.approx(model.sweep_width_m * 50)
    plan = fp.plan_parallel_split(["a"], (400, 400), model.lane_spacing_cm(), min_separation_cm=None)
    estimate = cs.estimate_plan(plan, footprint=footprint)
    # A forward camera never sees the band just ahead of the near edge at the start of the first lane.
    assert 0 < estimate.coverage < 1


def test_priors_are_normalised():
    raster = cs.Raster(500, 300, cell_cm=10)
    assert cs.uniform_prior(raster).sum() == pytest.approx(1.0)
    gauss = cs.gaussian_prior(raster, (250, 150), 80)
    assert gauss.sum() == pytest.approx(1.0)
    assert gauss.argmax() == np.argmin(
        (raster.centres()[0] - 250) ** 2 + (raster.centres()[1] - 150) ** 2
    )


def test_coverage_levels_are_ordered():
    plan = fp.plan_parallel_split(["a", "b"], 1000, 200)
    estimate = cs.estimate_plan(plan)
    levels = [estimate.time_to_coverage_s[k] for k in (0.5, 0.9, 0.99)]
    assert levels == sorted(levels)
    assert levels[-1] <= estimate.makespan_s


def test_uncovered_prior_mass_is_reported():
    plan = fp.plan_parallel_split(["a", "b"], 1000, 100, gap_cm=300, min_separation_cm=None)
    estimate = cs.estimate_plan(plan)
    assert estimate.prob_covered == pytest.approx(estimate.coverage)
    assert estimate.prob_covered < 1


# ---------------------------------------------------------------------------
# Separation
# ---------------------------------------------------------------------------


def test_min_separation_is_exact_between_breakpoints():
    a = [cs.Segment(0.0, 10.0, 0, 0, 1000, 0)]
    b = [cs.Segment(0.0, 10.0, 1000, 300, 0, 300)]
    # They pass each other 300 cm apart at t = 5, between breakpoints.
    assert cs.min_separation_cm(a, b) == pytest.approx(300)
    c = [cs.Segment(0.0, 10.0, 500, -500, 500, 500)]
    assert cs.min_separation_cm(a, c) == pytest.approx(0, abs=1e-9)


def test_min_separation_holds_positions_before_start_and_after_landing():
    a = [cs.Segment(0.0, 1.0, 0, 0, 0, 0), cs.Segment(1.0, 2.0, 0, 0, 100, 0)]
    b = [cs.Segment(5.0, 6.0, 400, 0, 120, 0)]
    # b sits at 400 until t = 5 while a already rests at 100; then b closes to 20 cm.
    assert cs.min_separation_cm(a, b) == pytest.approx(20)


def test_jitter_never_reports_more_than_the_nominal_separation():
    plan = fp.plan_parallel_split(["a", "b"], 1000, 200)
    tracks = cs.plan_tracks(plan)
    nominal = cs.min_separation_cm(tracks["a"], tracks["b"])
    jittered = cs.min_separation_under_jitter(plan, trials=5)
    assert plan.separation_cm <= jittered <= nominal
    assert math.isinf(cs.min_separation_under_jitter(fp.plan_parallel_split(["a"], 1000, 200)))
