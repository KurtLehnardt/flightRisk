"""Tests for the planner's third review round (PR #97): the gate must not fail open.

Covers the per-slot camera rule for a ``ForwardFootprint``, the altitude and
placement margins of the coverage gate, the split-square takeoff and the
``max_standoff_cm`` cap, the frozen and re-checked ``assign_lanes`` gate,
the protocol v2 lane and size caps, the exact coverage measure, bounded
runtimes, footprint validation, and direct ``check_plan`` tests on
hand-built bad plans.
"""

from __future__ import annotations

import json
import math
import random
import time
from dataclasses import replace

import numpy as np
import pytest

from flightrisk.drone import coverage_sim as cs
from flightrisk.drone import fleet_patterns as fp
from flightrisk.drone.coverage import tello_sweep_model
from flightrisk.drone.lane_executor import parse_lanes
from flightrisk.drone.safety import MIN_MOVE_CM, SafetyEnvelope
from flightrisk.fleet_protocol import validate_message

SQUARE = fp.SQUARE_FOOTPRINT
ROOM = tello_sweep_model(max_range_m=4.0)  # the 4 m demo: walls cap the view at 4 m; diagonal FOV by default
ROOM_HORIZONTAL = tello_sweep_model(max_range_m=4.0, fov_is_diagonal=False)


def _exact_coverage(plan, cameras, shift=(0, 0), cell_cm=5.0):
    """Coverage with the given per-drone cameras and every drone shifted, from exact first-detection times."""
    drones = tuple(
        replace(d, takeoff_cm=(d.takeoff_cm[0] + shift[0], d.takeoff_cm[1] + shift[1])) for d in plan.drones
    )
    moved = replace(plan, drones=drones)
    width, depth = plan.area_cm
    raster = cs.Raster(width, depth, cell_cm)
    times = cs.first_detection_times(raster, cs.plan_tracks(moved).values(), cameras)
    return float(np.isfinite(times).mean())


def _hand_plan(drones, area=(200, 200), sweep=100, standoff=0, **kwargs):
    """A square-model plan built by hand (no planning footprint, never checked)."""
    return fp.FleetSearchPlan(
        fp.PatternKind.PARALLEL_SPLIT, area, sweep, 0, tuple(drones), standoff_cm=standoff,
        geofence_cm=kwargs.pop("geofence_cm", None), max_standoff_cm=kwargs.pop("max_standoff_cm", None), **kwargs,
    )


def _lane(direction, distance):
    return {"direction": direction, "distance_cm": distance}


# ---------------------------------------------------------------------------
# Finding 1: a ForwardFootprint describes one altitude
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lkp", [None, (200, 200)])
def test_forward_footprint_is_refused_when_the_drones_fly_different_slots(lkp):
    # Reviewer's repro: the 1 m footprint was used unchanged for drone 2 at
    # 160 cm, so the gate said 100% while drone 2 really saw 92.8-93.7%.
    footprint = cs.ForwardFootprint.from_sweep_model(tello_sweep_model(max_range_m=4.0))
    with pytest.raises(ValueError, match="one altitude"):
        fp.plan_search(["d1", "d2"], 400, None, lkp, footprint=footprint, geofence_cm=450)


@pytest.mark.parametrize("lkp", [None, (200, 200)])
def test_forward_footprint_plan_is_gated_per_slot(lkp):
    footprint = cs.ForwardFootprint.from_sweep_model(tello_sweep_model(max_range_m=4.0))
    plan = fp.plan_search(["d1", "d2"], 400, None, lkp, footprint=footprint, alt_slots_cm=(100, 100), geofence_cm=450)
    assert plan.feasible, plan.problems
    assert plan.footprint.slot_cm == 100
    # Moving drone 2 to 160 cm: the gate models its camera there (near edge
    # scaled with height, as for a level camera) and refuses the plan.
    a, b = plan.drones
    raised = replace(plan, drones=(a, replace(b, alt_cm=160)))
    near = [g.near_cm for g in fp.gate_footprints(raised)]
    assert near[1] == pytest.approx(footprint.near_cm * (160 + fp.ALTITUDE_MARGIN_CM) / 100 + 10)
    assert any("sees only" in p for p in fp.check_plan(raised))


def test_sweep_model_gate_camera_is_the_camera_at_each_slot_plus_the_margin():
    plan = fp.plan_search(["d1", "d2"], 400, None, footprint=ROOM, geofence_cm=450)
    for drone, gate in zip(plan.drones, fp.gate_footprints(plan)):
        at_margin = cs.ForwardFootprint.from_sweep_model(replace(ROOM, height_m=(drone.alt_cm + 30) / 100))
        assert gate == at_margin.eroded(fp.DEFAULT_PLACEMENT_TOLERANCE_CM)
    nominal = plan.camera_footprints()
    assert nominal[0].near_cm < nominal[1].near_cm  # 100 cm and 160 cm slots differ


# ---------------------------------------------------------------------------
# Finding 2: altitude and placement margins
# ---------------------------------------------------------------------------


def test_altitude_margin_matches_the_executor_deadband():
    assert fp.ALTITUDE_DEADBAND_CM == MIN_MOVE_CM == 20
    assert fp.ALTITUDE_MARGIN_CM == fp.ALTITUDE_DEADBAND_CM + fp.ALTITUDE_TOLERANCE_CM == 30
    envelope = SafetyEnvelope(alt_slot_cm=100)
    # The executor holds anything that rounds to under 20 cm above the slot...
    assert envelope.slot_move(80.6) is None
    # ...and corrects from 20 cm on, so the deadband is within the margin.
    assert envelope.slot_move(80) == ("up", 20)


@pytest.mark.parametrize(
    "model, area, lkp",
    [
        (ROOM, 400, None),
        (ROOM, 400, (200, 200)),
        (ROOM_HORIZONTAL, 400, None),
        (ROOM_HORIZONTAL, 400, (200, 200)),
        (ROOM_HORIZONTAL, 400, (200, 0)),
        (tello_sweep_model(max_range_m=6.0), 600, (300, 300)),
    ],
)
def test_feasible_plans_keep_99_5_percent_at_the_deadband_and_with_placement_error(model, area, lkp):
    # Reviewer's measurements: +10 cm of altitude gave 99.25%, +19 cm 98.7%
    # and drones 10 cm forward 99.44%. The plan must hold at the worst
    # corner of both margins, measured independently of the planner's gate.
    plan = fp.plan_search(["d1", "d2"], area, None, lkp, footprint=model, geofence_cm=None)
    assert plan.feasible, plan.problems
    high = [cs.ForwardFootprint.from_sweep_model(replace(model, height_m=(d.alt_cm + 19) / 100)) for d in plan.drones]
    tolerance = fp.DEFAULT_PLACEMENT_TOLERANCE_CM
    for shift in ((0, 0), (0, tolerance), (0, -tolerance), (tolerance, 0), (-tolerance, 0), (tolerance, tolerance)):
        coverage = _exact_coverage(plan, high, shift)
        assert coverage >= fp.MIN_COVERAGE, (lkp, shift, coverage)


def test_placement_tolerance_starts_the_lanes_further_back():
    exact = fp.plan_parallel_split(["a"], 400, None, footprint=ROOM, placement_tolerance_cm=0, alt_slots_cm=(100,))
    tolerant = fp.plan_parallel_split(["a"], 400, None, footprint=ROOM, alt_slots_cm=(100,))
    assert tolerant.standoff_cm >= exact.standoff_cm + fp.DEFAULT_PLACEMENT_TOLERANCE_CM
    assert tolerant.footprint.lateral_cm <= exact.footprint.lateral_cm - 2 * fp.DEFAULT_PLACEMENT_TOLERANCE_CM


# ---------------------------------------------------------------------------
# Finding 3: split-square takeoff and the standoff cap
# ---------------------------------------------------------------------------


def test_split_square_takeoff_never_moves_behind_the_first_ring():
    # Reviewer's repro: LKP (200, 0) put the takeoff at y = -321, behind the
    # rings; the standoff grew without bound with the LKP's distance to the edge.
    standoffs = set()
    for ly in range(0, 401, 20):
        plan = fp.plan_search(["d1", "d2"], 400, None, (200, ly), footprint=ROOM_HORIZONTAL, geofence_cm=450)
        assert plan.pattern is fp.PatternKind.SPLIT_EXPANDING_SQUARE, ly
        assert plan.feasible, (ly, plan.problems)
        standoffs.add(plan.standoff_cm)
        for drone in plan.drones:
            first_ring_row = drone.pattern_start_cm[1]
            assert first_ring_row <= drone.takeoff_cm[1] <= plan.area_cm[1], (ly, drone.takeoff_cm)
    # The standoff is the camera's planning window start, whatever the LKP.
    assert standoffs == {fp.plan_search(["d1", "d2"], 400, None, footprint=ROOM_HORIZONTAL).standoff_cm}


def test_uncapped_camera_standoff_does_not_grow_with_the_area():
    # Reviewer's repro: a 20 m area with an uncapped camera and the LKP on
    # the near edge took off 1243 cm behind it.
    model = tello_sweep_model()
    plans = [fp.plan_search(["d1", "d2"], area, None, (area // 2, 0), footprint=model, geofence_cm=None)
             for area in (1000, 2000)]
    assert plans[0].standoff_cm == plans[1].standoff_cm == plans[1].footprint.near_cm
    assert plans[1].standoff_cm < 400


def test_four_metre_demo_needs_more_floor_than_four_metres():
    # Honest numbers for the 4 m demo (default slots 100/160 cm, 450 cm
    # fence, 10 cm placement and 30 cm altitude margins). With the default
    # diagonal FOV the ring window is too short, so it flies the parallel
    # split from 371 cm behind the near edge: 7.71 m of floor. Read as a
    # horizontal FOV, 2.99 m behind it: 6.99 m.
    for model, standoff in ((ROOM, 371), (ROOM_HORIZONTAL, 299)):
        for lkp in (None, (200, 200)):
            plan = fp.plan_search(["d1", "d2"], 400, None, lkp, footprint=model, geofence_cm=450)
            assert plan.feasible, plan.problems
            assert plan.standoff_cm == standoff
            assert plan.flight_area.zone_dict()["h_cm"] == 400 + standoff
    assert fp.plan_search(["d1", "d2"], 400, None, (200, 200), footprint=ROOM, geofence_cm=450).pattern is (
        fp.PatternKind.PARALLEL_SPLIT
    )


def test_standoff_beyond_max_standoff_is_refused():
    plan = fp.plan_search(["d1", "d2"], 400, None, footprint=ROOM, geofence_cm=450, max_standoff_cm=300)
    assert plan.problems == ("the plan flies 371 cm behind the near edge of the area, beyond the 300 cm max_standoff_cm",)
    with pytest.raises(ValueError, match="max_standoff_cm"):
        plan.assign_lanes_message("d1")


def test_max_standoff_defaults_to_the_geofence():
    assert fp.plan_search(["a"], 400, None, footprint=ROOM, geofence_cm=450).max_standoff_cm == 450
    assert fp.plan_search(["a"], 400, None, footprint=ROOM, geofence_cm=None).max_standoff_cm == (
        fp.DEFAULT_PLAN_GEOFENCE_CM
    )
    # With the default cap, a 360 cm fence refuses the 371 cm standoff of the two-drone demo.
    plan = fp.plan_search(["a", "b"], 400, None, footprint=ROOM, geofence_cm=360)
    assert any("beyond the 360 cm max_standoff_cm" in p for p in plan.problems), plan.problems
    for bad in (math.nan, -1, 0, "fence"):
        with pytest.raises(ValueError):
            fp.plan_search(["a"], 400, None, footprint=ROOM, max_standoff_cm=bad)


# ---------------------------------------------------------------------------
# Finding 4: the assign_lanes gate fails closed
# ---------------------------------------------------------------------------


def _demo_plan():
    return fp.plan_search(["d1", "d2"], 400, None, footprint=ROOM, geofence_cm=450)


def test_lanes_are_immutable_but_still_valid_executor_lanes():
    plan = _demo_plan()
    lane = plan.drones[0].lanes[0]
    for mutate in (
        lambda: lane.__setitem__("direction", "right"),
        lambda: lane.update(distance_cm=400),
        lambda: lane.pop("direction"),
        lambda: lane.clear(),
        lambda: lane.__delitem__("direction"),
        lambda: lane.setdefault("x", 1),
    ):
        with pytest.raises(TypeError):
            mutate()
    assert isinstance(plan.drones[0].lanes, tuple)
    assert parse_lanes(list(plan.drones[0].lanes)) == [(ln["direction"], ln["distance_cm"]) for ln in plan.drones[0].lanes]
    assert json.loads(json.dumps(lane)) == dict(lane)
    copy = dict(lane)
    copy["direction"] = "right"  # a plain copy is mutable and leaves the plan alone
    assert plan.drones[0].lanes[0] == lane and plan.feasible


def test_replaced_plan_is_rechecked_before_any_message():
    # Reviewer's repro: drone 1 moved to x = 250, 50 cm from drone 2's lane,
    # with the stale problems() of the original plan.
    plan = _demo_plan()
    a, b = plan.drones
    moved = replace(plan, drones=(replace(a, takeoff_cm=(250, a.takeoff_cm[1])), b))
    assert not moved.feasible
    with pytest.raises(ValueError, match="leaves its region"):
        moved.assign_lanes_message("d1")
    # Unchanged fields such as reason keep the check.
    assert replace(plan, reason="renamed").feasible


def test_lane_edited_behind_the_freeze_is_rechecked():
    plan = _demo_plan()
    lane = plan.drones[0].lanes[0]
    dict.__setitem__(lane, "direction", "left")  # deliberately bypasses FrozenLane
    assert not plan.feasible
    with pytest.raises(ValueError, match="infeasible"):
        plan.assign_lanes_message("d1")


def test_hand_built_plan_is_never_feasible_without_a_check():
    region = fp.Region(0, 0, 400, 400)
    drone = fp.DronePlan("x", (200, 0), region, 100, (_lane("forward", 400),))
    plan = _hand_plan([drone], area=(400, 400), sweep=400, geofence_cm=500, max_standoff_cm=500)
    assert plan.problems == ()
    assert not plan.feasible
    assert fp.check_plan(plan) == []
    # The fresh check passes, but a hand-built plan has no camera model: it is square-model.
    with pytest.raises(ValueError, match="square footprint"):
        plan.assign_lanes_message("x")
    assert validate_message(plan.assign_lanes_message("x", allow_unchecked=True)) == "assign_lanes"
    bad = _hand_plan([fp.DronePlan("x", (0, 0), fp.Region(0, 0, 10, 10), 100, (_lane("forward", 500),))],
                     area=(400, 400))
    with pytest.raises(ValueError, match="leaves its region"):
        bad.assign_lanes_message("x", allow_unchecked=True)


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"gap_cm": 1, "min_separation_cm": None}, "min_separation_cm is None"),
        ({"geofence_cm": None}, "geofence_cm is None"),
        ({"max_standoff_cm": None}, "max_standoff_cm is None"),
    ],
)
def test_plans_with_a_check_switched_off_need_an_explicit_override(kwargs, reason):
    kwargs = {"geofence_cm": 450, **kwargs}
    plan = fp.plan_parallel_split(["a", "b"], 400, None, footprint=ROOM, **kwargs)
    assert plan.feasible, plan.problems
    assert any(reason in r for r in plan.unchecked)
    with pytest.raises(ValueError, match="allow_unchecked"):
        plan.assign_lanes_message("a")
    assert validate_message(plan.assign_lanes_message("a", allow_unchecked=True)) == "assign_lanes"
    assert plan.to_dict()["unchecked"] == list(plan.unchecked)


def test_fully_checked_plan_needs_no_override():
    plan = _demo_plan()
    assert plan.unchecked == ()
    assert validate_message(plan.assign_lanes_message("d1")) == "assign_lanes"


# ---------------------------------------------------------------------------
# Finding 5: protocol v2 caps
# ---------------------------------------------------------------------------


def test_lane_and_message_caps_match_the_fleet_protocol_v2():
    # PR #96 (flightrisk/fleet_protocol.py, protocol v2): MAX_LANES = 1024 and
    # MAX_MESSAGE_BYTES = 64 * 1024, measured as compact JSON. The planner
    # keeps local copies until PR #96 is merged; change both together.
    assert fp.MAX_LANES_PER_DRONE == fp.MAX_MOVES_PER_DRONE == 1024
    assert fp.MAX_MESSAGE_BYTES == 65536
    assert 0 < fp.MESSAGE_RESERVE_BYTES < fp.MAX_MESSAGE_BYTES


def test_more_lanes_than_one_message_carries_are_refused():
    lanes = tuple(_lane("forward" if i % 2 == 0 else "back", 20) for i in range(fp.MAX_LANES_PER_DRONE + 1))
    plan = _hand_plan([fp.DronePlan("a", (100, 50), fp.Region(0, 0, 200, 200), 100, lanes)])
    assert any("lane cap" in p for p in fp.check_plan(plan, min_coverage=None))
    ok = _hand_plan([fp.DronePlan("a", (100, 50), fp.Region(0, 0, 200, 200), 100, lanes[:-1])])
    assert not any("lane cap" in p for p in fp.check_plan(ok, min_coverage=None))
    # A 200 m area at a 200 cm sweep needs about 2,000 moves per drone: refused when built.
    with pytest.raises(ValueError, match="move cap"):
        fp.plan_search(["a", "b"], 20_000, 200, footprint=SQUARE, geofence_cm=None)


def test_message_above_the_size_cap_is_refused():
    drone_id = "d" * (fp.MAX_MESSAGE_BYTES - fp.MESSAGE_RESERVE_BYTES)
    plan = _hand_plan([fp.DronePlan(drone_id, (100, 0), fp.Region(0, 0, 200, 200), 100, (_lane("forward", 200),))])
    problems = fp.check_plan(plan, min_coverage=None)
    assert any("byte limit" in p for p in problems), problems
    short = _hand_plan([fp.DronePlan("d", (100, 0), fp.Region(0, 0, 200, 200), 100, (_lane("forward", 200),))])
    assert fp.check_plan(short, min_coverage=None) == []


def test_every_feasible_plan_fits_the_protocol_caps():
    rng = random.Random(31)
    for _ in range(30):
        area = rng.randint(400, 20_000)
        model = rng.choice((ROOM, tello_sweep_model(), tello_sweep_model(max_range_m=8.0)))
        try:
            plan = fp.plan_search(["a", "b"], area, None, (area // 2, area // 3), footprint=model, geofence_cm=None)
        except ValueError as exc:
            assert "move cap" in str(exc)
            continue
        for drone in plan.drones:
            message = plan.assign_lanes_message(drone.drone_id, allow_infeasible=True)
            assert len(message["lanes"]) <= fp.MAX_LANES_PER_DRONE
            assert len(json.dumps(message, separators=(",", ":")).encode()) <= fp.MAX_MESSAGE_BYTES


# ---------------------------------------------------------------------------
# Finding 6: the coverage measure is exact, not sampled at cell centres
# ---------------------------------------------------------------------------


def test_strip_narrower_than_a_cell_is_measured():
    # Reviewer's repro: lanes at x = 65 and 235 with 162 cm square passes
    # leave an 8 cm strip (146-154) that fell between 10 cm cell centres.
    plan = fp.plan_search(["a", "b"], (300, 123), 162, footprint=SQUARE)
    assert plan.coverage == pytest.approx(1 - 8 / 300, abs=1e-9)
    assert not plan.feasible


def test_covered_fraction_is_exact_for_a_trapezoid_and_its_erosion():
    footprint = cs.ForwardFootprint(near_cm=20, far_cm=80, half_tan=0.5)
    # Hovering at (50, 0): visible area = integral of dy over [20, 80] = 3000 cm^2.
    assert cs.covered_fraction(100, 100, [[(50, 0)]], footprint) == pytest.approx(0.3, abs=1e-9)
    capped = cs.ForwardFootprint(near_cm=20, far_cm=80, half_tan=0.5, max_half_width_cm=15)
    # Width min(dy, 30): integral over [20, 30] of dy plus 30 * 50.
    assert cs.covered_fraction(100, 100, [[(50, 0)]], capped) == pytest.approx((450 - 200 + 1500) / 1e4, abs=1e-9)
    eroded = footprint.eroded(5)  # |dx| <= 0.5 * dy - 7.5 for dy in [25, 75]
    assert cs.covered_fraction(100, 100, [[(50, 0)]], eroded) == pytest.approx(
        (75**2 - 25**2) / 2 / 1e4 - 15 * 50 / 1e4, abs=1e-9
    )
    # A lane along y sweeps the widest part: 2 * 40 wide from y = 20 + start.
    lane = [(50, 0), (50, 20)]
    assert cs.covered_fraction(100, 100, [lane], footprint) == pytest.approx(
        (3000 + 20 * 80) / 1e4, abs=1e-9
    )


def test_eroded_footprint_survives_any_offset_within_the_tolerance():
    rng = random.Random(32)
    for _ in range(200):
        base = cs.ForwardFootprint(near_cm=rng.uniform(0, 200), far_cm=rng.uniform(200, 800),
                                   half_tan=rng.uniform(0.1, 2), max_half_width_cm=rng.choice((math.inf, 150.0)))
        t = rng.uniform(0, 30)
        eroded = base.eroded(t)
        dy = rng.uniform(eroded.near_cm, max(eroded.near_cm, eroded.far_cm))
        half = float(eroded.lateral_halfwidth(dy))
        if half < 0:
            continue
        for ex, ey in ((t, t), (-t, t), (t, -t), (-t, -t)):
            assert float(base.lateral_halfwidth(dy + ey)) >= half + ex - 1e-9 or half + ex <= 0


# ---------------------------------------------------------------------------
# Finding 7: bounded runtime
# ---------------------------------------------------------------------------


def test_estimate_of_a_200_m_plan_finishes_in_seconds():
    plan = fp.plan_search(["a", "b"], 20_000, 500, footprint=SQUARE, geofence_cm=None)
    start = time.perf_counter()
    assert cs.estimate_plan(plan).coverage == 1.0
    assert time.perf_counter() - start < 10.0
    uncapped = fp.plan_search(["a", "b"], 20_000, None, footprint=tello_sweep_model(), geofence_cm=None)
    start = time.perf_counter()
    assert cs.estimate_plan(uncapped).coverage == 1.0
    assert time.perf_counter() - start < 20.0


def test_first_detection_work_above_the_cap_is_refused_quickly(monkeypatch):
    plan = fp.plan_search(["a", "b"], 2000, 400, footprint=SQUARE, geofence_cm=None)
    monkeypatch.setattr(cs, "MAX_DETECTION_WORK", 1000)
    start = time.perf_counter()
    with pytest.raises(ValueError, match="larger cell_cm"):
        cs.estimate_plan(plan)
    assert time.perf_counter() - start < 1.0


def test_expanding_square_is_capped_before_it_is_built():
    start = time.perf_counter()
    with pytest.raises(ValueError, match="move cap"):
        fp.expanding_square_lanes(20_000, 20_000, 100)
    assert time.perf_counter() - start < 0.5
    assert len(fp.expanding_square_lanes(100, 100, 4)) <= fp.MAX_MOVES_PER_DRONE


# ---------------------------------------------------------------------------
# Finding 8: footprint and jitter validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"half_tan": math.inf},
        {"half_tan": math.nan},
        {"half_tan": 0},
        {"half_tan": -1.0},
        {"near_cm": math.inf},
        {"near_cm": -1},
        {"far_cm": math.nan},
        {"max_half_width_cm": math.nan},
        {"max_half_width_cm": -5},
        {"inset_cm": -1},
        {"near_cm": True},
    ],
)
def test_forward_footprint_refuses_bad_numbers(kwargs):
    with pytest.raises(ValueError):
        cs.ForwardFootprint(**{"near_cm": 100, "far_cm": 300, "half_tan": 1.0, **kwargs})


@pytest.mark.parametrize("trials", [-5, 1.5, True, "3"])
def test_jitter_trials_must_be_a_whole_number(trials):
    plan = fp.plan_search(["a", "b"], 2000, 400, footprint=SQUARE, geofence_cm=None)
    with pytest.raises(ValueError, match="trials"):
        cs.min_separation_under_jitter(plan, trials=trials)


# ---------------------------------------------------------------------------
# Finding 9: direct check_plan tests on hand-built bad plans
# ---------------------------------------------------------------------------


def test_check_plan_reports_a_waypoint_outside_its_region():
    # Inside the flight area, outside the drone's own region.
    drone = fp.DronePlan("a", (50, 0), fp.Region(0, 0, 100, 100), 100, (_lane("forward", 150),))
    problems = fp.check_plan(_hand_plan([drone]), min_coverage=None)
    assert problems == ["a: waypoint (50, 150) leaves its region {'x_cm': 0, 'y_cm': 0, 'w_cm': 100, 'h_cm': 100}"]


def test_check_plan_reports_a_waypoint_outside_the_flight_area():
    # Inside its region, which reaches 100 cm behind a plan with no standoff.
    drone = fp.DronePlan("a", (50, -50), fp.Region(0, -100, 100, 200), 100, (_lane("forward", 250),))
    problems = fp.check_plan(_hand_plan([drone]), min_coverage=None)
    assert problems == ["a: waypoint (50, -50) leaves the flight area (0, 0, 200, 200)"]
    assert fp.check_plan(_hand_plan([drone], standoff=50), min_coverage=None) == []


def _holed_plan(hole_cm):
    """Two drones with 100 cm square passes that leave ``hole_cm`` of a 1000 x 200 area unseen."""
    drones = []
    for drone_id, xs, region in (
        ("a", [50, 150, 250, 350, 450], fp.Region(0, 0, 500, 200)),
        ("b", [550 + hole_cm + 100 * k for k in range(4)] + [950], fp.Region(500 + hole_cm, 0, 1000, 200)),
    ):
        points = []
        for k, x in enumerate(xs):
            ends = (50, 150) if k % 2 == 0 else (150, 50)
            points += [(x, ends[0]), (x, ends[1])]
        drones.append(fp.DronePlan(drone_id, points[0], region, 100, tuple(fp._polyline_lanes(points, region))))
    return _hand_plan(drones, area=(1000, 200))


def test_coverage_gate_boundary_is_exactly_99_5_percent():
    holed = _holed_plan(7)
    coverage = fp.simulated_coverage(holed)
    assert coverage == pytest.approx(0.993, abs=1e-9)
    assert any("sees only 99.3%" in p for p in fp.check_plan(holed))
    assert fp.check_plan(holed, min_coverage=coverage) == []
    assert any("sees only" in p for p in fp.check_plan(holed, min_coverage=coverage + 1e-6))
    assert fp.MIN_COVERAGE == 0.995
    assert fp.simulated_coverage(_holed_plan(5)) == pytest.approx(0.995, abs=1e-9)
    assert any("sees only 99.4%" in p for p in fp.check_plan(_holed_plan(6)))
    assert not any("sees only" in p for p in fp.check_plan(_holed_plan(4)))


def test_coverage_gate_line_spacing_is_pinned():
    # The exact row scan only approximates where two slanted sides cross;
    # its extra evaluation lines are at most 10 cm apart.
    assert fp.COVERAGE_LINE_SPACING_CM == 10.0
