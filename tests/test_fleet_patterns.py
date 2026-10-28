"""Property tests for the axis-aligned fleet search patterns (plan sections 3 and 5.1).

``hypothesis`` is not installed in the project venv, so the properties are
checked over seeded random sweeps: each test draws a fixed number of random
areas, sweep widths, gaps, LKPs and geofences from ``random.Random(seed)``, so
a failure is reproducible from the printed case.

Separation rule under test: every drone's whole path (takeoff, transit, lanes
and any overshoot point) stays inside its own closed region, and regions are at
least ``gap_cm`` apart, so two drones are at least ``gap_cm`` apart at every
instant whatever the timing. The tests check the static rule, the exact
simulated minimum distance under +/-30% per-move timing errors and the exact
mirror symmetry of the split expanding square when its seam is centred.

Every builder now requires an explicit ``footprint``. The random cases here
use ``fp.SQUARE_FOOTPRINT`` because they test geometry, separation and the
square model's coverage; ``tests/test_fleet_plan_check.py`` covers the real
forward camera, the coverage gate, the placement-tolerance margin, input
validation and the takeoff contract.
"""

from __future__ import annotations

import itertools
import math
import random

import pytest

from flightrisk.drone import coverage_sim as cs
from flightrisk.drone import fleet_patterns as fp
from flightrisk.drone.coverage import tello_sweep_model
from flightrisk.drone.lane_executor import LaneExecutor, parse_lanes
from flightrisk.drone.safety import (
    HORIZONTAL_DIRECTIONS,
    MAX_MOVE_CM,
    MIN_MOVE_CM,
    SafetyEnvelope,
)
from flightrisk.drone.search import generate_expanding_square
from flightrisk.drone.tello import FakeTelloController
from flightrisk.fleet_protocol import validate_message
from flightrisk.observability import StructuredLogger

CASES = 120
SQUARE = fp.SQUARE_FOOTPRINT


# ---------------------------------------------------------------------------
# Random case generators
# ---------------------------------------------------------------------------


def _parallel_case(rng: random.Random) -> dict:
    n = rng.choice((1, 2, 2, 2, 3))
    sweep = rng.randint(fp.MIN_SWEEP_CM, 400)
    gap = rng.randint(1, 400)
    width = rng.randint(n * MIN_MOVE_CM + gap * (n - 1), 2000)
    depth = rng.randint(MIN_MOVE_CM + 1, 2000)
    return {
        "drone_ids": [f"drone-{i + 1}" for i in range(n)],
        "area_cm": (width, depth),
        "sweep_cm": sweep,
        "gap_cm": gap,
        "footprint": SQUARE,
    }


def _square_case(rng: random.Random) -> dict:
    # Redraw until the two regions and their takeoff offsets fit; with a gap
    # wider than one sweep the seam is off-centre, which needs more room.
    while True:
        sweep = rng.randint(fp.MIN_SWEEP_CM, 400)
        gap = rng.randint(1, 400)
        offset = rng.choice((0, MIN_MOVE_CM, 50, 120))
        s = (gap + 1) // 2
        width = rng.randint(2 * (s + offset), 2000)
        depth = rng.randint(2 * MIN_MOVE_CM, 2000)
        lkp = (rng.randint(s + offset, width - s - offset), rng.randint(0, depth))
        if fp._split_square_seams(lkp[0], width, gap, sweep, offset) is not None:
            break
    return {
        "drone_ids": ["drone-1", "drone-2"],
        "area_cm": (width, depth),
        "lkp_cm": lkp,
        "sweep_cm": sweep,
        "gap_cm": gap,
        "placement_offset_cm": offset,
        "stagger_s": rng.choice((0.0, 2.0)),
        "footprint": SQUARE,
    }


def _all_plans(seed: int, cases: int = CASES):
    """Random parallel splits, split squares and square spirals with their inputs."""
    rng = random.Random(seed)
    for _ in range(cases):
        case = _parallel_case(rng)
        yield case, fp.plan_parallel_split(min_separation_cm=None, **case)
        case = _square_case(rng)
        yield case, fp.plan_split_expanding_square(min_separation_cm=None, **case)
        width, depth = rng.randint(MIN_MOVE_CM * 2, 2000), rng.randint(MIN_MOVE_CM * 2, 2000)
        case = {
            "drone_id": "drone-1",
            "area_cm": (width, depth),
            "lkp_cm": (rng.randint(0, width), rng.randint(0, depth)),
            "sweep_cm": rng.randint(fp.MIN_SWEEP_CM, 400),
            "footprint": SQUARE,
        }
        yield case, fp.plan_square_spiral(**case)


# ---------------------------------------------------------------------------
# Executor limits and message shape
# ---------------------------------------------------------------------------


def test_every_move_is_one_the_lane_executor_accepts():
    for case, plan in _all_plans(seed=1):
        for drone in plan.drones:
            if not drone.lanes:
                # One footprint already covers the area; the executor would refuse an empty plan.
                assert any("no lanes" in p for p in plan.problems), case
                continue
            for lane in drone.lanes:
                assert set(lane) == {"direction", "distance_cm"}, (case, lane)
                assert lane["direction"] in HORIZONTAL_DIRECTIONS, (case, lane)
                assert isinstance(lane["distance_cm"], int), (case, lane)
                assert MIN_MOVE_CM <= lane["distance_cm"] <= MAX_MOVE_CM, (case, lane)
            # parse_lanes is the executor's own validator; it must not change anything.
            assert parse_lanes(list(drone.lanes)) == [(lane["direction"], lane["distance_cm"]) for lane in drone.lanes]
            # Every generated plan, feasible or not, must still produce a valid
            # message shape; refused plans need the explicit preview override.
            msg = plan.assign_lanes_message(drone.drone_id, allow_infeasible=True)
            assert validate_message(msg) == "assign_lanes"
            # The zone now carries the takeoff point next to the region rectangle.
            assert msg["zone"] == drone.zone_dict()
            assert {k: msg["zone"][k] for k in ("x_cm", "y_cm", "w_cm", "h_cm")} == drone.region.zone_dict()
            assert (msg["zone"]["takeoff_x_cm"], msg["zone"]["takeoff_y_cm"]) == drone.takeoff_cm
            assert MIN_MOVE_CM <= msg["alt_cm"] <= MAX_MOVE_CM


def test_no_rotation_or_vertical_move_is_ever_generated():
    lanes = fp.expanding_square_lanes(initial_side_cm=450, growth_cm=200, num_expansions=5)
    assert {lane["direction"] for lane in lanes} <= set(HORIZONTAL_DIRECTIONS)
    assert all(MIN_MOVE_CM <= lane["distance_cm"] <= MAX_MOVE_CM for lane in lanes)


def test_feasible_plans_are_accepted_by_a_real_lane_executor():
    # The 4 m demo pairs now plan with the real Tello forward camera (walls at
    # 4 m). With the 170 cm default gap, two drones on 75 cm square-model
    # lanes leave a 95 cm strip that neither may enter, which the coverage
    # gate refuses, so those square-model pairs are no longer feasible.
    room = tello_sweep_model(max_range_m=4.0)
    # (plan, allow_unchecked): the square-model plan is a model study, which
    # assign_lanes_message refuses without the explicit override.
    plans = [
        (fp.plan_search(["drone-1", "drone-2"], 400, None, footprint=room, geofence_cm=450), False),
        (fp.plan_search(["drone-1", "drone-2"], 400, None, (200, 200), footprint=room, geofence_cm=450), False),
        (fp.plan_search(["drone-1"], (400, 300), 60, (120, 90), footprint=SQUARE, geofence_cm=450), True),
    ]
    for plan, allow_unchecked in plans:
        assert plan.feasible, plan.problems
        assert bool(plan.unchecked) == allow_unchecked, plan.unchecked
        for drone in plan.drones:
            fake = FakeTelloController(name=drone.drone_id, battery=90)
            fake.connect()
            sent: list[dict] = []
            executor = LaneExecutor(
                fake,
                # The pattern's 160 cm slot needs a room configured for it: the
                # default ceiling limit is 140 cm (docs/ceiling-and-altitude.md).
                SafetyEnvelope(geofence_cm=450, ceiling_cm=250),
                drone.drone_id,
                send=sent.append,
                logger=StructuredLogger(component="test-fleet-patterns"),
            )
            try:
                assert executor.handle_assign_lanes(
                    plan.assign_lanes_message(drone.drone_id, allow_unchecked=allow_unchecked)
                )
            finally:
                executor.shutdown(timeout=2.0)
            acks = [m for m in sent if m.get("type") == "ack" and m.get("for") == "assign_lanes"]
            assert acks and acks[-1]["lanes_total"] == len(drone.lanes)


# ---------------------------------------------------------------------------
# Regions, partitions and the geofence
# ---------------------------------------------------------------------------


def test_every_waypoint_stays_in_its_region_and_the_area():
    for case, plan in _all_plans(seed=2):
        width, depth = plan.area_cm
        for drone in plan.drones:
            for point in drone.waypoints():
                assert drone.region.contains(point), (case, drone.drone_id, point)
                assert 0 <= point[0] <= width and 0 <= point[1] <= depth, (case, point)
        # check_plan without a fence, a minimum separation or the coverage gate
        # finds nothing wrong, apart from a degenerate area that one footprint
        # already covers. (These checks now default to the plan's own limits.)
        problems = fp.check_plan(plan, geofence_cm=None, min_separation_cm=None, min_coverage=None)
        assert [p for p in problems if "no lanes" not in p] == [], case


def test_parallel_split_strips_partition_the_width_with_exact_gaps():
    rng = random.Random(3)
    for _ in range(CASES):
        case = _parallel_case(rng)
        plan = fp.plan_parallel_split(min_separation_cm=None, **case)
        regions = [drone.region for drone in plan.drones]
        width, depth = plan.area_cm
        assert regions[0].x0 == 0 and regions[-1].x1 == width, case
        for a, b in itertools.pairwise(regions):
            assert b.x0 - a.x1 == case["gap_cm"], case
        widths = [r.x1 - r.x0 for r in regions]
        assert max(widths) - min(widths) <= 1, case
        assert all(r.y0 == 0 and r.y1 == depth for r in regions), case


def test_split_square_half_planes_are_disjoint_and_cover_the_area_but_the_seam():
    rng = random.Random(4)
    for _ in range(CASES):
        case = _square_case(rng)
        plan = fp.plan_split_expanding_square(min_separation_cm=None, **case)
        left, right = (drone.region for drone in plan.drones)
        width, depth = plan.area_cm
        lx, _ = case["lkp_cm"]
        s = (case["gap_cm"] + 1) // 2
        half = case["sweep_cm"] // 2
        assert (left.x0, right.x1) == (0, width), case
        if 2 * s <= case["sweep_cm"]:
            # Centred seam: the two seam passes meet over the LKP.
            assert (left.x1, right.x0) == (lx - s, lx + s), case
        else:
            # Off-centre seam (the centred one would leave the LKP unsearched):
            # one region reaches half a sweep past the LKP, the other starts
            # exactly one gap further on.
            assert (left.x1, right.x0) in ((lx + half, lx + half + case["gap_cm"]),
                                           (lx - half - case["gap_cm"], lx - half)), case
        assert left.y0 == right.y0 == 0 and left.y1 == right.y1 == depth, case
        assert right.x0 - left.x1 >= case["gap_cm"], case


def test_geofence_is_respected_or_the_plan_is_refused():
    rng = random.Random(5)
    for _ in range(CASES):
        fence = rng.randint(100, 800)
        builders = (
            lambda c, fence=fence: fp.plan_parallel_split(geofence_cm=fence, min_separation_cm=None, **c),
            lambda c, fence=fence: fp.plan_split_expanding_square(geofence_cm=fence, min_separation_cm=None, **c),
        )
        for builder, case in zip(builders, (_parallel_case(rng), _square_case(rng))):
            plan = builder(case)
            for drone in plan.drones:
                inside = all(abs(x) <= fence and abs(y) <= fence for x, y in drone.relative_waypoints())
                fence_problem = any(drone.drone_id in p and "geofence" in p for p in plan.problems)
                assert inside != fence_problem, (case, fence, plan.problems)
            assert plan.feasible == (not plan.problems)


# ---------------------------------------------------------------------------
# Separation
# ---------------------------------------------------------------------------


def test_guaranteed_separation_is_at_least_the_gap():
    for case, plan in _all_plans(seed=6):
        if len(plan.drones) < 2:
            assert math.isinf(plan.separation_cm)
            continue
        assert plan.separation_cm >= case["gap_cm"], case


def test_simulated_separation_holds_under_thirty_percent_timing_errors():
    rng = random.Random(7)
    timing = cs.TimingModel(speed_cm_s=100.0, move_overhead_s=1.5)
    for _ in range(40):
        plans = [
            fp.plan_parallel_split(min_separation_cm=None, **_parallel_case(rng)),
            fp.plan_split_expanding_square(min_separation_cm=None, **_square_case(rng)),
        ]
        for plan in plans:
            if len(plan.drones) < 2:
                continue
            simulated = cs.min_separation_under_jitter(plan, timing, jitter=0.3, trials=6, seed=rng.randint(0, 10**6))
            assert simulated >= plan.gap_cm - 1e-6, (plan.to_dict(), simulated)
            assert simulated >= plan.separation_cm - 1e-6


def test_split_square_is_an_exact_mirror_when_the_lkp_is_centred():
    rng = random.Random(8)
    for _ in range(60):
        sweep = rng.randint(fp.MIN_SWEEP_CM, 400)
        # Only the centred seam (2 * ceil(gap / 2) <= sweep) is a mirror; a
        # wider gap moves the seam to one side so the LKP is still searched
        # (test_split_square_searches_the_lkp_* in test_fleet_plan_check.py).
        gap = rng.randint(1, 2 * (sweep // 2))
        half_width = rng.randint((gap + 1) // 2 + 50, 1000)
        depth = rng.randint(100, 2000)
        lkp = (half_width, rng.randint(0, depth))
        plan = fp.plan_split_expanding_square(
            ["a", "b"], (2 * half_width, depth), lkp, sweep, footprint=SQUARE, gap_cm=gap, stagger_s=0.0,
            min_separation_cm=None,
        )
        a, b = plan.drones
        assert len(a.lanes) == len(b.lanes)
        mirror = {"left": "right", "right": "left", "forward": "forward", "back": "back"}
        for la, lb in zip(a.lanes, b.lanes):
            assert lb == {"direction": mirror[la["direction"]], "distance_cm": la["distance_cm"]}
        tracks = cs.plan_tracks(plan)
        times = sorted({seg.t0 for seg in tracks["a"]} | {seg.t1 for seg in tracks["a"]})
        s = (gap + 1) // 2
        for t in times:
            (xa, ya), (xb, yb) = cs._position(tracks["a"], t), cs._position(tracks["b"], t)
            assert ya == pytest.approx(yb)
            assert xa + xb == pytest.approx(2 * lkp[0])
            assert xb - xa >= 2 * s - 1e-9


def test_two_drone_parallel_split_is_balanced_in_time():
    rng = random.Random(13)
    for _ in range(CASES):
        case = _parallel_case(rng)
        case["drone_ids"] = ["a", "b"]
        if case["area_cm"][0] < 2 * MIN_MOVE_CM + case["gap_cm"]:
            continue
        plan = fp.plan_parallel_split(min_separation_cm=None, **case)
        totals = [cs.timeline(d.takeoff_cm, d.lanes)[-1].t1 for d in plan.drones]
        assert max(totals) <= 1.25 * min(totals), (case, totals)


def test_plan_below_the_minimum_separation_is_refused():
    # A 20 m area needs a 20 m geofence to be flyable at all (the default is
    # the edge's 500 cm). A 150 cm gap used to pass; it is now refused because
    # D_min must survive 10 cm of placement error on each drone (170 cm).
    kw = {"footprint": SQUARE, "geofence_cm": 2000}
    plan = fp.plan_parallel_split(["a", "b"], 2000, 400, gap_cm=100, **kw)
    assert not plan.feasible
    assert any("minimum" in p for p in plan.problems)
    for gap in (150, 169):
        plan = fp.plan_parallel_split(["a", "b"], 2000, 400, gap_cm=gap, **kw)
        expected = (
            f"guaranteed separation {gap} cm is below the 170 cm minimum "
            "(150 cm D_min plus 2 x 10 cm placement tolerance)"
        )
        assert plan.problems == (expected,)
    assert fp.plan_parallel_split(["a", "b"], 2000, 400, gap_cm=170, **kw).feasible
    assert fp.plan_parallel_split(["a", "b"], 2000, 400, **kw).feasible
    square = fp.plan_split_expanding_square(["a", "b"], 2000, (1000, 1000), 400, gap_cm=100, **kw)
    assert not square.feasible
    assert fp.plan_split_expanding_square(["a", "b"], 2000, (1000, 1000), 400, **kw).feasible


def test_required_gap_grows_with_drift_and_path_length():
    assert fp.required_gap_cm(150, 0.0, 10_000, placement_tolerance_cm=0) == 150
    assert fp.required_gap_cm(150, 0.03, 2_000, placement_tolerance_cm=0) == 270
    assert fp.required_gap_cm(150, 0.03, 2_000) == 290
    plan = fp.plan_parallel_split(
        ["a", "b"], 2000, 400, footprint=SQUARE, gap_cm=200, geofence_cm=2000,
        min_separation_cm=fp.required_gap_cm(150, 0.03, 2_000, placement_tolerance_cm=0),
    )
    assert not plan.feasible
    assert any("290 cm minimum" in p for p in plan.problems)
    with pytest.raises(ValueError):
        fp.required_gap_cm(150, -0.01, 100)


# ---------------------------------------------------------------------------
# Coverage completeness
# ---------------------------------------------------------------------------


def _coverage(plan, cell_cm: float = 10.0) -> float:
    return cs.estimate_plan(plan, cell_cm=cell_cm).coverage


def test_parallel_split_covers_the_whole_area_including_the_gaps():
    rng = random.Random(9)
    for _ in range(60):
        n = rng.choice((1, 2, 3))
        sweep = rng.randint(fp.MIN_SWEEP_CM, 400)
        gap = rng.randint(1, sweep - 1)
        width = rng.randint(n * MIN_MOVE_CM + gap * (n - 1), max(1200, n * MIN_MOVE_CM + gap * (n - 1)))
        case = {
            "drone_ids": [f"d{i}" for i in range(n)],
            "area_cm": (width, rng.randint(MIN_MOVE_CM + 1, 1200)),
            "sweep_cm": sweep,
            "gap_cm": gap,
            "footprint": SQUARE,
        }
        plan = fp.plan_parallel_split(min_separation_cm=None, **case)
        assert _coverage(plan) == 1.0, case


def test_split_square_covers_the_whole_area_when_the_gap_is_within_one_sweep():
    rng = random.Random(10)
    for _ in range(60):
        case = _square_case(rng)
        case["gap_cm"] = rng.randint(1, case["sweep_cm"] - 1)
        s = (case["gap_cm"] + 1) // 2
        width, depth = case["area_cm"]
        width = min(max(width, 2 * (s + case["placement_offset_cm"])), 1200)
        depth = min(depth, 1200)
        lkp = (rng.randint(s + case["placement_offset_cm"], width - s - case["placement_offset_cm"]),
               rng.randint(0, depth))
        case.update(area_cm=(width, depth), lkp_cm=lkp)
        plan = fp.plan_split_expanding_square(min_separation_cm=None, **case)
        assert _coverage(plan) == 1.0, case


def test_square_spiral_covers_the_whole_area():
    rng = random.Random(11)
    for _ in range(60):
        width, depth = rng.randint(40, 1200), rng.randint(40, 1200)
        plan = fp.plan_square_spiral(
            "a", (width, depth), (rng.randint(0, width), rng.randint(0, depth)), rng.randint(40, 400),
            footprint=SQUARE,
        )
        assert _coverage(plan) == 1.0, plan.to_dict()


def test_gap_wider_than_a_sweep_is_reported_as_uncovered_not_entered():
    plan = fp.plan_parallel_split(["a", "b"], 1000, 100, footprint=SQUARE, gap_cm=300, min_separation_cm=None)
    assert plan.separation_cm == 300
    coverage = _coverage(plan)
    assert coverage < 1.0
    # Only the middle 200 cm of the 300 cm gap is out of reach of both drones.
    assert coverage == pytest.approx(0.8, abs=0.011)
    # The coverage gate refuses it.
    assert plan.coverage == pytest.approx(coverage)
    assert any("sees only 80.0% of the area" in p for p in plan.problems)


def test_parallel_lane_count_rounds_up_so_the_remainder_strip_is_searched():
    # search.generate_parallel_track uses width // strip and would fly 2 lanes
    # here; the third is needed to reach the far edge.
    plan = fp.plan_parallel_split(["a"], (400, 300), 150, footprint=SQUARE, min_separation_cm=None)
    lane_xs = sorted({x for x, _ in plan.drones[0].waypoints()})
    assert lane_xs == [75, 200, 325]
    assert _coverage(plan) == 1.0


# ---------------------------------------------------------------------------
# Plan geometry details
# ---------------------------------------------------------------------------


def test_split_square_matches_the_plan_example_geometry():
    plan = fp.plan_split_expanding_square(
        ["drone-1", "drone-2"], 2000, (1000, 1000), 400, footprint=SQUARE, gap_cm=100, min_separation_cm=100
    )
    left, right = plan.drones
    assert plan.placements() == {"drone-1": (900, 1000, 0), "drone-2": (1100, 1000, 0)}
    assert (left.start_offset_s, right.start_offset_s) == (0.0, 2.0)
    assert (left.alt_cm, right.alt_cm) == (100, 160)
    pattern = right.without_transit()
    # Rings at radii 200, 600 and 800 (the last clamped to keep the pass inside the area).
    corners = [(1050, 800), (1200, 800), (1200, 1200), (1050, 1200), (1050, 1600), (1600, 1600),
               (1600, 400), (1050, 400), (1050, 200), (1800, 200), (1800, 1800), (1050, 1800)]
    simplified = fp._simplify(pattern.waypoints())
    assert simplified == corners
    assert len(pattern.lanes) == 20
    assert pattern.path_length_cm == 6700
    assert right.transit_lanes == 2
    assert right.lanes[:2] == ({"direction": "back", "distance_cm": 200}, {"direction": "left", "distance_cm": 50})


def test_parallel_split_takes_off_at_the_first_lane_and_needs_no_transit():
    plan = fp.plan_parallel_split(["drone-1", "drone-2"], 2000, 400, footprint=SQUARE, gap_cm=150)
    for drone in plan.drones:
        assert drone.transit_lanes == 0
        assert drone.takeoff_cm == drone.waypoints()[0]
        assert drone.takeoff_cm[1] == 0
    assert plan.placements()["drone-1"][0] == 200


def test_end_inset_shortens_the_lanes():
    full = fp.plan_parallel_split(["a"], (400, 1000), 200, footprint=SQUARE, min_separation_cm=None)
    inset = fp.plan_parallel_split(["a"], (400, 1000), 200, footprint=SQUARE, end_inset_cm=100, min_separation_cm=None)
    assert inset.drones[0].path_length_cm < full.drones[0].path_length_cm
    ys = [y for _, y in inset.drones[0].waypoints()]
    assert (min(ys), max(ys)) == (100, 900)


def test_overshoot_pair_never_leaves_the_region():
    region = fp.Region(0, 0, 100, 100)
    moves = fp._leg_moves((90, 50), (95, 50), region)
    assert moves == [{"direction": "left", "distance_cm": 20}, {"direction": "right", "distance_cm": 25}]
    moves = fp._leg_moves((10, 50), (15, 50), region)
    assert moves == [{"direction": "right", "distance_cm": 25}, {"direction": "left", "distance_cm": 20}]
    with pytest.raises(ValueError):
        fp._leg_moves((5, 50), (10, 50), fp.Region(0, 0, 20, 100))


def test_plans_are_deterministic():
    for _ in range(2):
        a = list(_all_plans(seed=12, cases=10))
        b = list(_all_plans(seed=12, cases=10))
        assert [p.to_dict() for _, p in a] == [p.to_dict() for _, p in b]


# ---------------------------------------------------------------------------
# Legacy expanding square, rewritten axis-aligned
# ---------------------------------------------------------------------------


def test_expanding_square_keeps_the_legacy_leg_lengths_without_rotations():
    for side, growth, expansions in ((100, 100, 4), (60, 40, 3), (300, 250, 2)):
        legacy_legs, run = [], 0
        for wp in generate_expanding_square(side, growth, expansions):
            if wp.rotate_degrees:
                legacy_legs.append(run)
                run = 0
            else:
                run += wp.distance_cm
        new = fp.expanding_square_lanes(side, growth, expansions)
        legs: list[list] = []
        for lane in new:
            if legs and legs[-1][0] == lane["direction"]:
                legs[-1][1] += lane["distance_cm"]
            else:
                legs.append([lane["direction"], lane["distance_cm"]])
        assert [length for _, length in legs] == legacy_legs
        order = ("forward", "right", "back", "left")
        assert [direction for direction, _ in legs] == [order[i % 4] for i in range(len(legs))]


def test_expanding_square_rejects_bad_parameters():
    with pytest.raises(ValueError):
        fp.expanding_square_lanes(initial_side_cm=10)
    with pytest.raises(ValueError):
        fp.expanding_square_lanes(growth_cm=-5)
    with pytest.raises(ValueError):
        fp.expanding_square_lanes(num_expansions=0)


# ---------------------------------------------------------------------------
# Decision function (plan section 3.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n, lkp, expected",
    [
        (2, (1000, 1000), fp.PatternKind.SPLIT_EXPANDING_SQUARE),
        (2, (300, 1900), fp.PatternKind.SPLIT_EXPANDING_SQUARE),
        (2, None, fp.PatternKind.PARALLEL_SPLIT),
        (2, (50, 1000), fp.PatternKind.PARALLEL_SPLIT),  # too close to the side edge for two half-planes
        (1, (1000, 1000), fp.PatternKind.SQUARE_SPIRAL),
        (1, None, fp.PatternKind.PARALLEL_SPLIT),
        (3, (1000, 1000), fp.PatternKind.PARALLEL_SPLIT),
        (3, None, fp.PatternKind.PARALLEL_SPLIT),
    ],
)
def test_choose_pattern_follows_the_decision_table(n, lkp, expected):
    choice = fp.choose_pattern(n, 2000, lkp)
    assert choice.kind is expected
    assert choice.reason
    plan = fp.plan_search([f"d{i}" for i in range(n)], 2000, 400, lkp, footprint=SQUARE)
    assert plan.pattern is expected
    assert plan.reason == choice.reason
    if lkp is not None:
        assert plan.lkp_cm == lkp


def test_lkp_with_an_area_beyond_the_geofence_is_refused_not_silently_flown():
    plan = fp.plan_search(["a", "b"], 2000, 400, (1000, 1000), footprint=SQUARE, geofence_cm=450)
    assert plan.pattern is fp.PatternKind.SPLIT_EXPANDING_SQUARE
    assert not plan.feasible
    assert any("geofence" in p for p in plan.problems)


def test_invalid_inputs_are_rejected():
    with pytest.raises(ValueError):
        fp.choose_pattern(2, 2000, (2500, 100))
    kw = {"footprint": SQUARE}
    with pytest.raises(ValueError):
        fp.plan_split_expanding_square(["a"], 2000, (1000, 1000), 400, **kw)
    with pytest.raises(ValueError):
        fp.plan_split_expanding_square(["a", "b"], 2000, (1000, 1000), 400, gap_cm=0, **kw)
    with pytest.raises(ValueError):
        fp.plan_split_expanding_square(["a", "b"], 2000, (1000, 1000), 400, placement_offset_cm=10, **kw)
    with pytest.raises(ValueError):
        fp.plan_parallel_split(["a", "b"], 2000, fp.MIN_SWEEP_CM - 1, **kw)
    with pytest.raises(ValueError):
        fp.plan_parallel_split(["a", "a"], 2000, 400, **kw)
    with pytest.raises(ValueError):
        fp.plan_parallel_split(["a", "b"], 2000, 400, alt_slots_cm=(10,), **kw)
    with pytest.raises(ValueError):
        fp.plan_parallel_split(["a", "b"], (2000, 30), 400, end_inset_cm=10, **kw)
    with pytest.raises(ValueError):
        fp.plan_parallel_split(["a", "b", "c"], 100, 400, gap_cm=40, **kw)
    # The footprint is a required, explicit choice.
    with pytest.raises(TypeError):
        fp.plan_parallel_split(["a", "b"], 2000, 400)
