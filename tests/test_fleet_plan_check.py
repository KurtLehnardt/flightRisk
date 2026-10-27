"""Tests for the planner's safety gates (PR #97 review findings).

Covers the split-square seam with a gap wider than one sweep, the real forward
camera footprint and the 99.5% coverage gate, the placement-tolerance margin
on separation, input validation, the refusal to build messages for an
infeasible plan, the takeoff/zone contract for the coordinator, the size caps,
the linear-time separation walk and the expected time to detection of an
incomplete plan.

``hypothesis`` is not installed, so properties are checked over seeded random
sweeps; every assertion message carries the failing case.
"""

from __future__ import annotations

import itertools
import math
import random
import time
from dataclasses import replace

import numpy as np
import pytest

from flightrisk.drone import coverage_sim as cs
from flightrisk.drone import fleet_patterns as fp
from flightrisk.drone.coverage import SweepModel, tello_sweep_model
from flightrisk.drone.safety import DEFAULT_GEOFENCE_CM
from flightrisk.fleet_protocol import validate_message

SQUARE = fp.SQUARE_FOOTPRINT
ROOM = tello_sweep_model(max_range_m=4.0)  # the 4 m demo room: walls cap the view at 4 m


def _cell_time(plan, point, cell_cm=10.0):
    """First-detection time of the raster cell containing ``point`` (exact timing model)."""
    width, depth = plan.area_cm
    raster = cs.Raster(width, depth, cell_cm)
    times = cs.first_detection_times(raster, cs.plan_tracks(plan).values(), plan.camera_footprints())
    cx, cy = raster.centres()
    return times[np.argmin((cx - point[0]) ** 2 + (cy - point[1]) ** 2)]


def _random_sweep_model(rng: random.Random) -> SweepModel:
    return tello_sweep_model(
        min_person_px=rng.choice((24.0, 32.0, 48.0)),
        max_range_m=rng.choice((None, 3.0, 4.0, 6.0, 10.0)),
        efficiency=rng.choice((1.0, 0.7, 0.5)),
        calibrated_sweep_width_m=rng.choice((None, None, 1.5, 2.4)),
        fov_is_diagonal=rng.choice((False, True)),
    )


# Input errors a random draw may legitimately hit: a camera that sees nothing
# at those altitudes, a lane spacing wider than the view, an area too narrow
# for the strips. Anything else is a bug.
_INPUT_ERRORS = ("sees nothing", "narrower sweep_cm", "too narrow for", "minimum sweep")


def _random_forward_plan(rng: random.Random):
    while True:
        n = rng.choice((1, 2, 2, 3))
        width, depth = rng.randint(100, 1500), rng.randint(100, 1500)
        lkp = None if rng.random() < 0.3 else (rng.randint(0, width), rng.randint(0, depth))
        kwargs = {
            "footprint": _random_sweep_model(rng),
            "alt_slots_cm": rng.choice(((100, 160), (100, 100), (60, 120), (200, 300))),
            "gap_cm": rng.choice((fp.DEFAULT_GAP_CM, 200, 300)),
            "geofence_cm": rng.choice((None, 450, 2000)),
        }
        sweep = rng.choice((None, None, rng.randint(fp.MIN_SWEEP_CM, 600)))
        case = (n, (width, depth), sweep, lkp, kwargs)
        try:
            return case, fp.plan_search([f"d{i}" for i in range(n)], (width, depth), sweep, lkp, **kwargs)
        except ValueError as exc:
            assert any(text in str(exc) for text in _INPUT_ERRORS), (case, exc)


# ---------------------------------------------------------------------------
# Finding 1: the split square must search the LKP whatever the gap
# ---------------------------------------------------------------------------


def test_split_square_searches_the_lkp_when_the_gap_is_wider_than_a_sweep():
    # Reviewer's repro, in an area wide enough for the off-centre seam: the
    # centred seam put the LKP in a never-searched strip (t_first = inf).
    plan = fp.plan_split_expanding_square(
        ["a", "b"], 800, (400, 400), 75, footprint=SQUARE, gap_cm=150, geofence_cm=None, min_separation_cm=None
    )
    left, right = (d.region for d in plan.drones)
    assert right.x0 - left.x1 == 150
    assert left.x1 == 400 + 37  # drone 1 owns half a sweep past the LKP
    assert _cell_time(plan, (400, 400)) == 0.0
    # The strip neither drone may see (150 - 75 cm wide) is still there, away
    # from the LKP, and the coverage gate refuses the plan.
    assert plan.coverage < fp.MIN_COVERAGE
    assert not plan.feasible


def test_four_metre_room_with_seventy_five_cm_square_lanes_is_refused_not_flown_blind():
    # The original finding: 4 m room, 75 cm lanes, LKP at the centre.
    for gap in (fp.DEFAULT_GAP_CM, 150):
        plan = fp.plan_search(["a", "b"], 400, 75, (200, 200), footprint=SQUARE, gap_cm=gap)
        assert not plan.feasible
        assert any("sees only" in p for p in plan.problems), plan.problems


def test_lkp_is_seen_and_regions_stay_apart_for_any_gap_property():
    rng = random.Random(21)
    checked = 0
    while checked < 60:
        sweep = rng.randint(fp.MIN_SWEEP_CM, 300)
        gap = rng.randint(sweep // 2, 3 * sweep)  # mostly wider than one sweep
        offset = rng.choice((0, 20, 50))
        width, depth = rng.randint(200, 2000), rng.randint(fp.MIN_SWEEP_CM, 1500)
        lkp = (rng.randint(0, width), rng.randint(0, depth))
        if fp._split_square_seams(lkp[0], width, gap, sweep, offset) is None:
            continue
        checked += 1
        plan = fp.plan_split_expanding_square(
            ["a", "b"], (width, depth), lkp, sweep, footprint=SQUARE, gap_cm=gap, placement_offset_cm=offset,
            geofence_cm=None, min_separation_cm=None,
        )
        case = (width, depth, lkp, sweep, gap, offset)
        a, b = plan.drones
        assert b.region.x0 - a.region.x1 >= gap, case
        for drone in plan.drones:
            assert all(drone.region.contains(p) for p in drone.waypoints()), case
        # The LKP is seen, and so is everything within half a sweep of it.
        width_cells = cs.Raster(width, depth, 10.0)
        seen = cs.covered_cells(width_cells, [d.waypoints() for d in plan.drones], plan.camera_footprints())
        cx, cy = width_cells.centres()
        near_lkp = (np.abs(cx - lkp[0]) <= sweep // 2) & (np.abs(cy - lkp[1]) <= sweep // 2)
        assert seen[near_lkp].all(), case
        assert np.isfinite(_cell_time(plan, lkp)), case
        simulated = cs.min_separation_under_jitter(plan, jitter=0.3, trials=4, seed=checked)
        assert simulated >= gap - 1e-6, case


# ---------------------------------------------------------------------------
# Finding 2: the real forward camera
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "area, lkp, model",
    [
        (400, (200, 200), ROOM),
        (400, None, ROOM),
        (400, (200, 200), tello_sweep_model(calibrated_sweep_width_m=2.4, max_range_m=4.0)),
        (2000, (1000, 1000), tello_sweep_model(max_range_m=8.0)),
        (2000, (1000, 1000), tello_sweep_model()),
    ],
)
def test_reviewer_forward_camera_scenarios_now_see_the_whole_area(area, lkp, model):
    # Before: 12-77% coverage, all reported feasible.
    plan = fp.plan_search(["a", "b"], area, None, lkp, footprint=model, geofence_cm=None)
    assert plan.feasible, plan.problems
    assert plan.footprint.kind == "forward"
    assert cs.estimate_plan(plan).coverage == 1.0


def test_a_square_model_plan_is_refused_against_the_real_camera():
    square = fp.plan_search(["a", "b"], 400, 240, (200, 200), footprint=SQUARE, geofence_cm=None)
    assert square.feasible, square.problems
    checked_as_flown = replace(square, footprint=fp.planning_footprint(ROOM, 240, (100, 160), 400))
    problems = fp.check_plan(checked_as_flown)
    assert any("forward camera footprint sees only" in p for p in problems), problems


def test_every_feasible_forward_plan_sees_at_least_99_5_percent_property():
    rng = random.Random(22)
    feasible = 0
    for _ in range(40):
        case, plan = _random_forward_plan(rng)
        if not plan.feasible:
            assert plan.problems
            continue
        feasible += 1
        # Independent of the planner's own gate: exact first-detection times
        # with each drone's footprint at its altitude slot.
        coverage = cs.estimate_plan(plan, cell_cm=20.0).coverage
        assert coverage >= fp.MIN_COVERAGE, (case, plan.footprint, coverage)
    assert feasible >= 20, feasible


def test_forward_plans_fly_behind_the_near_edge_to_see_it():
    plan = fp.plan_search(["a", "b"], 400, None, footprint=ROOM, geofence_cm=None)
    highest = tello_sweep_model(height_m=1.6, max_range_m=4.0)
    assert plan.standoff_cm >= highest.near_m * 100
    assert plan.flight_area == fp.Region(0, -plan.standoff_cm, 400, 400)
    for drone in plan.drones:
        assert drone.region.y0 == -plan.standoff_cm
        assert min(y for _, y in drone.waypoints()) >= -plan.standoff_cm
        assert drone.takeoff_cm[1] < 0  # the takeoff line moves back by the standoff
    raster = cs.Raster(400, 400, 10.0)
    seen = cs.covered_cells(raster, [d.waypoints() for d in plan.drones], plan.camera_footprints())
    _, cy = raster.centres()
    assert seen[cy < 160].all()  # the near band the old plans never saw
    assert plan.to_dict()["standoff_cm"] == plan.standoff_cm


# A calibrated 2.0 m sweep caps the camera's true lateral reach at the planned
# lane spacing while an 8 m range gives a much longer along-track window, so
# legs along the heading must be spaced by lateral_cm and legs across it by
# along_cm: swapping them leaves holes the real camera cannot fill. The
# uncapped model checks the case where the window covers the whole depth.
_CAPPED_LATERAL = tello_sweep_model(max_range_m=8.0, calibrated_sweep_width_m=2.0)


@pytest.mark.parametrize(
    "model, area, n, lkp",
    [
        (_CAPPED_LATERAL, 3000, 1, (1500, 1500)),
        (_CAPPED_LATERAL, 3000, 1, (700, 2200)),
        (_CAPPED_LATERAL, 3000, 2, (1500, 1500)),
        (_CAPPED_LATERAL, 3000, 2, (1100, 900)),
        (tello_sweep_model(), 2000, 1, (300, 1700)),
        (tello_sweep_model(), 2000, 2, (700, 300)),
    ],
)
def test_ring_patterns_space_legs_by_their_own_axis(model, area, n, lkp):
    plan = fp.plan_search([f"d{i}" for i in range(n)], area, None, lkp, footprint=model, geofence_cm=None)
    assert plan.pattern is not fp.PatternKind.PARALLEL_SPLIT
    assert plan.footprint.along_cm > 2 * plan.footprint.lateral_cm
    assert plan.feasible, plan.problems
    assert cs.estimate_plan(plan, cell_cm=20.0).coverage == 1.0


def test_lane_spacing_wider_than_the_view_is_refused_with_a_clear_error():
    with pytest.raises(ValueError, match="narrower sweep_cm"):
        fp.plan_search(["a", "b"], 400, ROOM.lane_spacing_cm(), (200, 200), footprint=ROOM)


def test_ring_pattern_falls_back_when_the_along_track_window_is_too_short():
    shallow_view = cs.ForwardFootprint(near_cm=100, far_cm=130, half_tan=1.0)
    plan = fp.plan_search(["a", "b"], 600, None, (300, 300), footprint=shallow_view, geofence_cm=None)
    assert plan.pattern is fp.PatternKind.PARALLEL_SPLIT
    assert "cannot be built here" in plan.reason
    assert plan.lkp_cm == (300, 300)


def test_covered_cells_matches_the_exact_first_detection_times():
    rng = random.Random(23)
    for _ in range(25):
        _, plan = _random_forward_plan(rng)
        width, depth = plan.area_cm
        raster = cs.Raster(width, depth, 10.0)
        fast = cs.covered_cells(raster, [d.waypoints() for d in plan.drones], plan.camera_footprints())
        exact = np.isfinite(cs.first_detection_times(raster, cs.plan_tracks(plan).values(), plan.camera_footprints()))
        assert np.array_equal(fast, exact), plan.to_dict()


# ---------------------------------------------------------------------------
# Finding 3: placement tolerance
# ---------------------------------------------------------------------------


def test_default_gap_leaves_room_for_placement_error():
    assert fp.DEFAULT_GAP_CM == fp.DEFAULT_MIN_SEPARATION_CM + 2 * fp.DEFAULT_PLACEMENT_TOLERANCE_CM == 170
    plan = fp.plan_parallel_split(["a", "b"], 2000, 400, footprint=SQUARE, gap_cm=169, geofence_cm=None)
    assert not plan.feasible
    assert fp.check_plan(plan, placement_tolerance_cm=0) == []


def _shifted_toward(plan, left: str, right: str, tolerance: float):
    """The plan with ``left`` placed ``tolerance`` cm to the right and ``right`` to the left."""
    drones = []
    for drone in plan.drones:
        shift = tolerance if drone.drone_id == left else -tolerance if drone.drone_id == right else 0
        drones.append(replace(drone, takeoff_cm=(drone.takeoff_cm[0] + shift, drone.takeoff_cm[1])))
    return replace(plan, drones=tuple(drones))


def test_feasible_plans_keep_d_min_with_both_drones_misplaced_toward_each_other():
    rng = random.Random(24)
    checked = 0
    for _ in range(80):
        if rng.random() < 0.5:
            _, plan = _random_forward_plan(rng)
        else:
            n = rng.choice((2, 3))
            width = rng.randint(600, 2000)
            lkp = (rng.randint(0, width), rng.randint(0, width)) if n == 2 and rng.random() < 0.5 else None
            plan = fp.plan_search([f"d{i}" for i in range(n)], width, rng.randint(170, 400), lkp, footprint=SQUARE,
                                  geofence_cm=None)
        if not plan.feasible or len(plan.drones) < 2:
            continue
        ordered = sorted(plan.drones, key=lambda d: d.region.x0)
        for a, b in itertools.pairwise(ordered):
            moved = _shifted_toward(plan, a.drone_id, b.drone_id, fp.DEFAULT_PLACEMENT_TOLERANCE_CM)
            tracks = cs.plan_tracks(moved)
            true_separation = cs.min_separation_cm(tracks[a.drone_id], tracks[b.drone_id])
            assert true_separation >= fp.DEFAULT_MIN_SEPARATION_CM - 1e-6, plan.to_dict()
            checked += 1
    assert checked >= 20


# ---------------------------------------------------------------------------
# Finding 4: input validation and the default geofence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"geofence_cm": math.nan},
        {"geofence_cm": math.inf},
        {"geofence_cm": 0},
        {"geofence_cm": -1},
        {"min_separation_cm": math.nan},
        {"min_separation_cm": -150},
        {"placement_tolerance_cm": math.nan},
        {"placement_tolerance_cm": -1},
        {"stagger_s": math.nan},
        {"stagger_s": -1.0},
        {"placement_offset_cm": 10},
        {"gap_cm": math.nan},
        {"end_inset_cm": -5},
    ],
)
def test_non_finite_or_out_of_range_inputs_are_refused(kwargs):
    # The reviewer's repro passed gap_cm=10 with NaN limits and got feasible=True.
    kwargs = {"gap_cm": 10, **kwargs}
    with pytest.raises(ValueError):
        fp.plan_search(["a", "b"], 2000, 400, (1000, 1000), footprint=SQUARE, **kwargs)


def test_bad_area_sweep_lkp_and_footprint_are_refused():
    with pytest.raises(ValueError):
        fp.plan_search(["a", "b"], (math.nan, 400), 100, footprint=SQUARE)
    with pytest.raises(ValueError):
        fp.plan_search(["a", "b"], 400, math.inf, footprint=SQUARE)
    with pytest.raises(ValueError):
        fp.plan_search(["a", "b"], 400, 100, (1, 2, 3), footprint=SQUARE)
    with pytest.raises(ValueError):
        fp.plan_search(["a", "b"], 400, None, footprint=SQUARE)
    with pytest.raises(ValueError):
        fp.plan_search(["a", "b"], 400, 100, footprint="circle")
    with pytest.raises(TypeError):
        fp.plan_search(["a", "b"], 400, 100, footprint=cs.RectFootprint.square(100))
    with pytest.raises(ValueError):
        fp.check_plan(fp.plan_search(["a"], 400, 100, footprint=SQUARE), geofence_cm=math.nan)


def test_geofence_is_checked_by_default_with_the_edge_default():
    plan = fp.plan_search(["a", "b"], 2000, 400, footprint=SQUARE)
    assert plan.geofence_cm == fp.DEFAULT_PLAN_GEOFENCE_CM == DEFAULT_GEOFENCE_CM
    assert any("geofence" in p for p in plan.problems)


# ---------------------------------------------------------------------------
# Findings 5 and 6: messages and the takeoff contract
# ---------------------------------------------------------------------------


def test_infeasible_plan_builds_no_assign_lanes_without_an_explicit_override():
    plan = fp.plan_search(["a", "b"], 2000, 400, footprint=SQUARE)
    assert not plan.feasible
    with pytest.raises(ValueError, match="infeasible"):
        plan.assign_lanes_message("a")
    assert validate_message(plan.assign_lanes_message("a", allow_infeasible=True)) == "assign_lanes"


@pytest.mark.parametrize("lkp", [None, (200, 200)])
def test_zone_carries_the_takeoff_point_which_is_the_origin_of_the_lanes(lkp):
    plan = fp.plan_search(["drone-1", "drone-2"], 400, None, lkp, footprint=ROOM, geofence_cm=450)
    assert plan.feasible, plan.problems
    corner_is_takeoff = []
    for drone in plan.drones:
        msg = plan.assign_lanes_message(drone.drone_id)
        zone = msg["zone"]
        assert validate_message(msg) == "assign_lanes"
        takeoff = (zone["takeoff_x_cm"], zone["takeoff_y_cm"])
        assert takeoff == drone.takeoff_cm == plan.placements()[drone.drone_id][:2]
        assert fp.Region(zone["x_cm"], zone["y_cm"], zone["x_cm"] + zone["w_cm"], zone["y_cm"] + zone["h_cm"]) == drone.region
        # The edge reports positions relative to the takeoff point; adding the
        # takeoff (not the zone corner) puts them on the shared grid.
        for (rx, ry), (x, y) in zip(drone.relative_waypoints(), drone.waypoints()):
            assert (rx + takeoff[0], ry + takeoff[1]) == (x, y)
        corner_is_takeoff.append(takeoff == (zone["x_cm"], zone["y_cm"]))
        assert plan.to_dict()["drones"][plan.drones.index(drone)]["zone"] == zone
    # Pin that the corner is not the takeoff, so PR-8 cannot rely on it.
    assert not any(corner_is_takeoff)


# ---------------------------------------------------------------------------
# Finding 7: caps and performance
# ---------------------------------------------------------------------------


def test_oversized_inputs_are_refused_quickly():
    for area in ((20_001, 400), (400, 20_001)):
        with pytest.raises(ValueError, match="at most 20000"):
            fp.plan_search(["a", "b"], area, 400, footprint=SQUARE)
    with pytest.raises(ValueError, match="move cap"):
        fp.plan_search(["a"], 20_000, fp.MIN_SWEEP_CM, footprint=SQUARE, geofence_cm=None)
    with pytest.raises(ValueError, match="above the"):
        cs.Raster(30_000, 30_000, 10.0)


def test_large_area_plan_and_separation_finish_in_seconds():
    for footprint, sweep in ((SQUARE, 200), (tello_sweep_model(max_range_m=8.0), None)):
        start = time.perf_counter()
        plan = fp.plan_search(["a", "b"], 10_000, sweep, footprint=footprint, geofence_cm=None)
        separation = cs.min_separation_under_jitter(plan, trials=50)
        elapsed = time.perf_counter() - start
        assert separation >= plan.gap_cm
        assert elapsed < 5.0, elapsed


def _reference_position(track, t):
    """The original linear-scan position lookup, kept as the reference."""
    if t <= track[0].t0:
        return track[0].x0, track[0].y0
    for seg in track:
        if t <= seg.t1:
            if seg.t1 == seg.t0:
                return seg.x1, seg.y1
            f = (t - seg.t0) / (seg.t1 - seg.t0)
            return seg.x0 + f * (seg.x1 - seg.x0), seg.y0 + f * (seg.y1 - seg.y0)
    return track[-1].x1, track[-1].y1


def test_two_pointer_separation_matches_the_linear_scan_and_is_linear():
    rng = random.Random(25)
    for _ in range(30):
        tracks = []
        for start in ((0, 0), (300, 0)):
            lanes = [{"direction": rng.choice(("forward", "back", "left", "right")), "distance_cm": rng.randint(20, 700)}
                     for _ in range(rng.randint(1, 15))]
            tracks.append(cs.timeline(start, lanes, cs.TimingModel(move_overhead_s=rng.choice((0.0, 1.5))),
                                      start_offset_s=rng.choice((0.0, 2.0))))
        a, b = tracks
        times = sorted({0.0} | {s.t0 for s in a} | {s.t1 for s in a} | {s.t0 for s in b} | {s.t1 for s in b})
        for t in times:
            assert cs._position(a, t) == pytest.approx(_reference_position(a, t))
        offsets = [np.subtract(_reference_position(a, t), _reference_position(b, t)) for t in times]
        reference = min(cs._segment_origin_distance(*p, *q) for p, q in itertools.pairwise(offsets))
        reference = min(reference, math.hypot(*offsets[0]))
        assert cs.min_separation_cm(a, b) == pytest.approx(reference)
    lanes = [{"direction": d, "distance_cm": 100} for d in ("forward", "back") * 5000]
    a, b = cs.timeline((0, 0), lanes), cs.timeline((500, 0), lanes)
    start = time.perf_counter()
    assert cs.min_separation_cm(a, b) == pytest.approx(500)
    assert time.perf_counter() - start < 2.0


# ---------------------------------------------------------------------------
# Finding 8: expected time to detection of an incomplete plan
# ---------------------------------------------------------------------------


def test_expected_time_is_infinite_when_part_of_the_prior_is_never_searched():
    plan = fp.plan_split_expanding_square(
        ["a", "b"], 800, (400, 400), 75, footprint=SQUARE, gap_cm=150, geofence_cm=None, min_separation_cm=None
    )
    for prior in ("uniform", "lkp"):
        estimate = cs.estimate_plan(plan, prior=prior, sigma_cm=200)
        assert estimate.prob_covered < 1
        assert math.isinf(estimate.expected_time_to_detection_s)
        assert math.isfinite(estimate.conditional_time_to_detection_s)
    complete = fp.plan_search(["a", "b"], 400, None, (200, 200), footprint=ROOM, geofence_cm=None)
    estimate = cs.estimate_plan(complete, prior="lkp", sigma_cm=100)
    assert estimate.prob_covered == pytest.approx(1.0)
    assert estimate.expected_time_to_detection_s == pytest.approx(estimate.conditional_time_to_detection_s)


# ---------------------------------------------------------------------------
# Finding 11: very shallow areas
# ---------------------------------------------------------------------------


def test_shallow_areas_fall_back_to_the_parallel_split_or_fail_clearly():
    rng = random.Random(26)
    for _ in range(300):
        width, depth = rng.randint(400, 3000), rng.randint(20, fp.MIN_SWEEP_CM - 1)
        lkp = (rng.randint(0, width), rng.randint(0, depth))
        n = rng.choice((1, 2))
        plan = fp.plan_search([f"d{i}" for i in range(n)], (width, depth), rng.randint(40, 600), lkp,
                              footprint=SQUARE, geofence_cm=None)
        assert plan.pattern is fp.PatternKind.PARALLEL_SPLIT, (width, depth, lkp)
    with pytest.raises(ValueError, match="at least 40 cm"):
        fp.plan_split_expanding_square(["a", "b"], (2000, 30), (1000, 15), 100, footprint=SQUARE)


# ---------------------------------------------------------------------------
# Finding 12: a diagonal field of view
# ---------------------------------------------------------------------------


def test_diagonal_fov_reading_is_planned_for():
    # The sweep model itself is tested in tests/test_coverage.py.
    diagonal = tello_sweep_model(max_range_m=4.0, fov_is_diagonal=True)
    plan = fp.plan_search(["a", "b"], 400, None, (200, 200), footprint=diagonal, geofence_cm=None)
    assert plan.feasible, plan.problems
    assert cs.estimate_plan(plan).coverage == 1.0
