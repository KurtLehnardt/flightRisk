"""Per-unit limits in the map plan helpers: path conversion, patterns, the zone split and ``build_plan``.

The path, edit and search helpers take one ``safety.EnvelopeLimits`` per
drone (``ROOM_LIMITS`` by default), and ``plan_edit.build_plan`` takes
``unit_limits`` and ``max_area_side_cm`` and forwards both to
``fleet_plan_check.check_plan``. These tests check that the room profile,
given or not, keeps today's results, that a profile with a long move range
gives metre-scale moves instead of 500 cm ones, and that each drone of a
mixed fleet is planned and checked with its own profile.
"""

from __future__ import annotations

import dataclasses
import json
import random
from pathlib import Path

import pytest

from flightrisk.drone import fleet_plan_check as fpc
from flightrisk.drone import plan_edit
from flightrisk.drone import plan_paths as pp
from flightrisk.drone import search
from flightrisk.drone.coordinator import DEFAULT_ZONE_GAP_CM
from flightrisk.drone.fleet_plan_check import MAX_AREA_SIDE_CM
from flightrisk.drone.plan_edit import PlanSpec, build_plan
from flightrisk.drone.safety import FIELD_LIMITS, ROOM_LIMITS

ROOT = Path(__file__).resolve().parent.parent
VECTORS = json.loads((ROOT / "tests" / "fixtures" / "plan_geometry_vectors.json").read_text())

# The room profile with a 50 m longest move; its slots stay the room profile's.
LONG_MOVES = dataclasses.replace(ROOM_LIMITS, name="long-moves", max_move_cm=5000)
# A profile whose shortest move is 1 m.
METRE_MIN = dataclasses.replace(ROOM_LIMITS, name="metre-min", min_move_cm=100, max_move_cm=1000)
# A profile the planner cannot use: its longest move is less than twice its shortest.
NARROW = dataclasses.replace(ROOM_LIMITS, name="narrow", max_move_cm=30)
FIELD_SLOT_CM = int(FIELD_LIMITS.default_alt_slot_cm)


def _lane(direction: str, distance: int) -> dict:
    return {"direction": direction, "distance_cm": distance}


def _payload(built: plan_edit.BuiltPlan) -> str:
    return json.dumps(built.payload(), sort_keys=True)


def _drawn_paths() -> list[list]:
    paths = [case["vertices"] for case in VECTORS["paths"]]
    rng = random.Random(7)
    for _ in range(20):
        paths.append([(rng.randint(-600, 600), rng.randint(-600, 600)) for _ in range(rng.randint(2, 6))])
    return paths


# ---------------------------------------------------------------- the room profile keeps today's results


def test_room_limits_give_todays_search_moves():
    for distance in (-10, 0, 5, 20, 250, 500, 600, 1000, 3000):
        assert search._clamp_distance(distance, limits=ROOM_LIMITS) == search._clamp_distance(distance)
        assert search._split_long_move("left", distance, limits=ROOM_LIMITS) == search._split_long_move("left", distance)
    for pattern in search.PatternType:
        assert search.get_search_pattern(pattern, limits=ROOM_LIMITS) == search.get_search_pattern(pattern)
    assert search.split_parallel_track_zones(3, 1200, 900, 75, limits=ROOM_LIMITS) == search.split_parallel_track_zones(
        3, 1200, 900, 75
    )
    assert search.split_sector_zones(3, 700, limits=ROOM_LIMITS) == search.split_sector_zones(3, 700)
    assert [wp.distance_cm for wp in search._split_long_move("forward", 3000)] == [500] * 6


def test_room_limits_reproduce_the_path_vectors():
    for case in VECTORS["paths"]:
        converted = pp.path_to_lanes(case["vertices"], fence_cm=case["fence_cm"], limits=ROOM_LIMITS)
        assert [dict(lane) for lane in converted.lanes] == case["lanes"]
        assert [list(p) for p in converted.trace] == case["trace"]
        assert list(converted.trace_segment) == case["trace_segment"]


def test_room_limits_give_todays_paths_patterns_and_zones():
    for drawn in _drawn_paths():
        assert pp.path_to_lanes(drawn, limits=ROOM_LIMITS) == pp.path_to_lanes(drawn)
        assert pp.path_to_lanes(drawn, fence_cm=300, limits=ROOM_LIMITS) == pp.path_to_lanes(drawn, fence_cm=300)
        assert pp.count_moves(drawn, limits=ROOM_LIMITS) == pp.count_moves(drawn)
        assert pp.snap_path(drawn, limits=ROOM_LIMITS) == pp.snap_path(drawn)
        assert pp.staircase(drawn[0], drawn[1], limits=ROOM_LIMITS) == pp.staircase(drawn[0], drawn[1])
    area = pp.Rect(0, 0, 1200, 900)
    for count in (1, 2, 3, 4):
        zones = pp.split_area(area, count, DEFAULT_ZONE_GAP_CM)
        assert pp.split_area(area, count, DEFAULT_ZONE_GAP_CM, zone_limits=[ROOM_LIMITS] * count) == zones
        for zone in zones:
            for pattern in pp.PATTERNS:
                for strip in (5, 75, 300):
                    assert pp.pattern_vertices(pattern, zone, strip, limits=ROOM_LIMITS) == pp.pattern_vertices(
                        pattern, zone, strip
                    )


@pytest.mark.parametrize("pattern", pp.PATTERNS)
@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_room_limits_for_every_drone_give_todays_built_plan(pattern, count):
    ids = [f"d{i + 1}" for i in range(count)]
    spec = PlanSpec(pattern, pp.Rect(0, 0, 400, 400))
    kwargs = {"strip_cm": 75, "gap_cm": DEFAULT_ZONE_GAP_CM, "geofence_cm": 450.0}
    today = _payload(build_plan(spec, ids, **kwargs))
    room = {drone_id: ROOM_LIMITS for drone_id in ids}
    assert _payload(build_plan(spec, ids, unit_limits=room, **kwargs)) == today
    assert _payload(build_plan(spec, ids, unit_limits=room, max_area_side_cm=MAX_AREA_SIDE_CM, **kwargs)) == today


def test_an_edited_path_with_room_limits_gives_todays_built_plan():
    spec = PlanSpec.from_payload({
        "pattern": "parallel_track",
        "area": {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400},
        "paths": {"a": [[0, 0], [130, 370], [400, 20]]},
    })
    today = _payload(build_plan(spec, ["a"], geofence_cm=450))
    assert _payload(build_plan(spec, ["a"], geofence_cm=450, unit_limits={"a": ROOM_LIMITS})) == today


# ---------------------------------------------------------------- a long move range gives metre-scale moves


def test_a_long_move_range_flies_a_30_m_leg_as_one_move():
    assert search._split_long_move("forward", 3000, limits=FIELD_LIMITS) == [search.Waypoint("forward", 3000)]
    assert search._clamp_distance(4000, limits=FIELD_LIMITS) == 4000
    assert search._clamp_distance(9000, limits=FIELD_LIMITS) == 5000
    track = search.generate_parallel_track(450, 3000, 150, limits=FIELD_LIMITS)
    assert [(wp.direction, wp.distance_cm) for wp in track] == [
        ("forward", 3000), ("right", 150), ("back", 3000), ("right", 150), ("forward", 3000),
    ]
    assert search.get_search_pattern(search.PatternType.PARALLEL_TRACK, depth_cm=3000, limits=FIELD_LIMITS)[0] == (
        search.Waypoint("forward", 3000)
    )
    zones = search.split_parallel_track_zones(2, 900, 3000, 150, limits=FIELD_LIMITS)
    assert all(wp.distance_cm in (150, 450, 3000) for zone in zones for wp in zone)

    room = pp.path_to_lanes([(0, 0), (0, 3000)])
    assert [dict(lane) for lane in room.lanes] == [_lane("forward", 500)] * 6
    field = pp.path_to_lanes([(0, 0), (0, 3000)], limits=FIELD_LIMITS)
    assert [dict(lane) for lane in field.lanes] == [_lane("forward", 3000)]
    assert field.trace == ((0, 0), (0, 3000))


def test_long_legs_split_into_equal_whole_centimetre_moves_of_the_profile():
    float_field = dataclasses.replace(FIELD_LIMITS, max_move_cm=5000.0)
    converted = pp.path_to_lanes([(0, 0), (7777, 0), (7777, -10)], limits=float_field)
    assert [dict(lane) for lane in converted.lanes] == [
        _lane("right", 3889), _lane("right", 3888), _lane("back", 30), _lane("forward", 20),
    ]
    assert all(type(lane["distance_cm"]) is int for lane in converted.lanes)
    assert pp.count_moves([(0, 0), (7777, 0), (7777, -10)], limits=float_field) == len(converted.lanes)
    assert all(type(wp.distance_cm) is int for wp in search._split_long_move("left", 7777, limits=float_field))


def test_a_slanted_segment_with_a_long_move_range_keeps_the_room_staircase():
    # The staircase depends on the shortest move only, so the field profile's
    # corners equal the room profile's; only the moves along them get longer.
    drawn = [(0, 0), (2400, 1800)]
    assert pp.snap_path(drawn, limits=FIELD_LIMITS) == pp.snap_path(drawn)
    assert len(pp.path_to_lanes(drawn, limits=FIELD_LIMITS).lanes) <= len(pp.path_to_lanes(drawn).lanes)


def test_build_plan_gives_metre_scale_moves_with_field_limits():
    spec = PlanSpec("parallel_track", pp.Rect(0, 0, 450, 3000))
    room = build_plan(spec, ["a"], strip_cm=150, geofence_cm=None)
    assert room.ok
    assert max(lane["distance_cm"] for lane in room.drones[0].converted.lanes) == 500
    field = build_plan(
        spec, ["a"], strip_cm=150, geofence_cm=None, unit_limits={"a": FIELD_LIMITS},
        alt_slot_of=lambda drone_id, index: FIELD_SLOT_CM,
    )
    assert field.ok, field.problem_text()
    assert [dict(lane) for lane in field.drones[0].converted.lanes] == [
        _lane("forward", 3000), _lane("right", 150), _lane("back", 3000), _lane("right", 150), _lane("forward", 3000),
    ]
    assert field.drones[0].generated == room.drones[0].generated


# ---------------------------------------------------------------- build_plan forwards the limits to check_plan


def test_build_plan_forwards_unit_limits_and_the_area_cap_to_check_plan(monkeypatch):
    calls = []
    real = plan_edit.check_plan

    def spy(plan, **kwargs):
        calls.append(kwargs)
        return real(plan, **kwargs)

    monkeypatch.setattr(plan_edit, "check_plan", spy)
    spec = PlanSpec("parallel_track", pp.Rect(0, 0, 400, 400))
    build_plan(spec, ["a", "b"], geofence_cm=450)
    assert calls[-1] == {"min_coverage": None, "unit_limits": None, "max_area_side_cm": MAX_AREA_SIDE_CM}
    limits = {"a": ROOM_LIMITS, "b": LONG_MOVES, "unused": FIELD_LIMITS}
    build_plan(spec, ["a", "b"], geofence_cm=450, unit_limits=limits, max_area_side_cm=None)
    assert calls[-1]["unit_limits"] is limits
    assert calls[-1]["max_area_side_cm"] is None


def test_a_3000_cm_lane_refused_with_room_limits_is_accepted_with_a_long_move_profile():
    spec = PlanSpec.from_payload({
        "pattern": "parallel_track",
        "area": {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 3000},
        "paths": {"a": [[0, 0], [0, 3000]]},
    })
    built = build_plan(spec, ["a"], geofence_cm=None, unit_limits={"a": LONG_MOVES})
    assert built.ok, built.problem_text()
    assert [dict(lane) for lane in built.drones[0].converted.lanes] == [_lane("forward", 3000)]
    # The same lanes are refused when check_plan is given the room profile.
    room_problems = plan_edit._check_plan_problems(built.drones, spec.area, built.strip_cm, None)
    assert any("3000" in text for text in room_problems), room_problems
    assert plan_edit._check_plan_problems(
        built.drones, spec.area, built.strip_cm, None, unit_limits={"a": LONG_MOVES}
    ) == []
    # With the room profile the leg is flown as six moves.
    assert len(build_plan(spec, ["a"], geofence_cm=None).drones[0].converted.lanes) == 6


def test_the_area_cap_is_passed_on_for_the_geofence():
    spec = PlanSpec("parallel_track", pp.Rect(0, 0, 400, 400))
    with pytest.raises(ValueError, match="geofence_cm must be within"):
        build_plan(spec, ["a"], geofence_cm=30_000)
    assert build_plan(spec, ["a"], geofence_cm=30_000, max_area_side_cm=None).ok


def test_unit_limits_missing_a_drone_are_refused():
    spec = PlanSpec("parallel_track", pp.Rect(0, 0, 400, 400))
    with pytest.raises(ValueError, match="unit_limits has no envelope limits for 'b'"):
        build_plan(spec, ["a", "b"], geofence_cm=450, unit_limits={"a": ROOM_LIMITS})


@pytest.mark.parametrize("unit_limits, error", [
    ([ROOM_LIMITS], TypeError),
    ({"a": "room"}, TypeError),
    ({"a": NARROW}, ValueError),
])
def test_unusable_unit_limits_are_refused_before_planning(unit_limits, error):
    spec = PlanSpec("parallel_track", pp.Rect(0, 0, 400, 400))
    with pytest.raises(error):
        build_plan(spec, ["a"], geofence_cm=450, unit_limits=unit_limits)


@pytest.mark.parametrize("cap", [0, -5, float("nan"), float("inf"), True, "20000"])
def test_a_bad_area_cap_is_refused_even_without_drones(cap):
    spec = PlanSpec("parallel_track", pp.Rect(0, 0, 400, 400))
    with pytest.raises(ValueError, match="max_area_side_cm"):
        build_plan(spec, [], geofence_cm=450, max_area_side_cm=cap)


def test_the_helpers_refuse_a_profile_the_planner_cannot_use():
    message = "narrow max_move_cm must be at least twice min_move_cm"
    for call in (
        lambda: pp.path_to_lanes([(0, 0), (0, 100)], limits=NARROW),
        lambda: pp.count_moves([(0, 0), (0, 100)], limits=NARROW),
        lambda: pp.staircase((0, 0), (50, 50), limits=NARROW),
        lambda: pp.pattern_vertices("parallel_track", pp.Rect(0, 0, 400, 400), 75, limits=NARROW),
        lambda: pp.split_area(pp.Rect(0, 0, 400, 400), 1, 50, zone_limits=[NARROW]),
        lambda: search._split_long_move("forward", 100, limits=NARROW),
        lambda: search.generate_parallel_track(limits=NARROW),
    ):
        with pytest.raises(ValueError, match=message):
            call()
    with pytest.raises(TypeError, match="safety.EnvelopeLimits"):
        pp.path_to_lanes([(0, 0), (0, 100)], limits="room")
    with pytest.raises(TypeError, match="safety.EnvelopeLimits"):
        search._clamp_distance(100, limits=None)


# ---------------------------------------------------------------- the zone split and a mixed fleet


def test_the_zone_split_keeps_every_zone_one_longest_shortest_move_wide():
    area = pp.Rect(0, 0, 240, 400)
    assert [zone.w for zone in pp.split_area(area, 2, 50)] == [95, 95]
    assert [zone.w for zone in pp.split_area(area, 2, 50, zone_limits=[ROOM_LIMITS, LONG_MOVES])] == [95, 95]
    with pytest.raises(ValueError, match="area width 240 cm is too narrow for 2 zones with 50 cm gaps"):
        pp.split_area(area, 2, 50, zone_limits=[ROOM_LIMITS, METRE_MIN])
    assert [zone.w for zone in pp.split_area(pp.Rect(0, 0, 250, 400), 2, 50, zone_limits=[ROOM_LIMITS, METRE_MIN])] == [
        100, 100,
    ]
    with pytest.raises(ValueError, match="zone_limits has 1 profiles for 2 zones"):
        pp.split_area(area, 2, 50, zone_limits=[ROOM_LIMITS])
    with pytest.raises(TypeError, match="zone_limits must be a sequence"):
        pp.split_area(area, 2, 50, zone_limits=ROOM_LIMITS)
    with pytest.raises(ValueError, match="at least one zone is needed"):
        pp.split_area(area, 0, 50, zone_limits=[])


def test_a_long_shortest_move_sizes_the_staircase_and_the_patterns():
    drawn = [(0, 0), (900, 700)]
    corners, _ = pp.snap_path(drawn, limits=METRE_MIN)
    legs = [abs(bx - ax) + abs(by - ay) for (ax, ay), (bx, by) in zip(corners, corners[1:], strict=False)]
    assert min(legs) >= 100
    converted = pp.path_to_lanes(drawn, limits=METRE_MIN)
    assert all(100 <= lane["distance_cm"] <= 1000 for lane in converted.lanes)
    assert converted.max_deviation_cm < 2 * 100
    # A leg shorter than the shortest move is flown as an overshoot pair of that length.
    assert [dict(lane) for lane in pp.path_to_lanes([(0, 0), (0, 30)], limits=METRE_MIN).lanes] == [
        _lane("forward", 130), _lane("back", 100),
    ]
    zone = pp.Rect(0, 0, 800, 800)
    for pattern in pp.PATTERNS:
        vertices = pp.pattern_vertices(pattern, zone, 40, limits=METRE_MIN)
        lanes = pp.path_to_lanes(vertices, limits=METRE_MIN).lanes
        assert all(100 <= lane["distance_cm"] <= 1000 for lane in lanes), pattern
    strips = pp.pattern_vertices("parallel_track", zone, 40, limits=METRE_MIN)
    assert {abs(b[0] - a[0]) for a, b in zip(strips, strips[1:], strict=False)} - {0} == {100}


def test_a_snapped_path_stays_within_twice_the_shortest_move():
    rng = random.Random(11)
    for _ in range(200):
        drawn = [(rng.randint(-3000, 3000), rng.randint(-3000, 3000)) for _ in range(rng.randint(2, 5))]
        assert pp.path_to_lanes(drawn, limits=METRE_MIN).max_deviation_cm < 2 * 100


def test_a_mixed_fleet_plans_and_checks_each_drone_with_its_own_limits():
    spec = PlanSpec("parallel_track", pp.Rect(0, 0, 1000, 3000))
    unit_limits = {"room": ROOM_LIMITS, "field": FIELD_LIMITS}
    slots = {"room": 100, "field": FIELD_SLOT_CM}
    built = build_plan(
        spec, ["room", "field"], strip_cm=150, geofence_cm=None, unit_limits=unit_limits,
        alt_slot_of=lambda drone_id, index: slots[drone_id],
    )
    assert built.ok, built.problem_text()
    room, field = built.drones
    assert max(lane["distance_cm"] for lane in room.converted.lanes) == 500
    assert max(lane["distance_cm"] for lane in field.converted.lanes) == 3000
    assert room.generated == tuple((x - field.zone.x + room.zone.x, y) for x, y in field.generated)
    # Each drone's slot is checked against its own profile too.
    swapped = build_plan(
        spec, ["room", "field"], strip_cm=150, geofence_cm=None, unit_limits=unit_limits,
        alt_slot_of=lambda drone_id, index: slots["field" if drone_id == "room" else "room"],
    )
    assert not swapped.ok
    texts = [problem.text for problem in swapped.problems]
    assert any(text.startswith("room: altitude slot") for text in texts), texts
    assert any(text.startswith("field: altitude slot") for text in texts), texts
    assert not any("lane" in text for text in texts), texts


def test_check_plan_problems_size_the_area_for_the_checked_profiles(monkeypatch):
    plans = []
    real = plan_edit.check_plan

    def spy(plan, **kwargs):
        plans.append(plan)
        return real(plan, **kwargs)

    monkeypatch.setattr(plan_edit, "check_plan", spy)
    spec = PlanSpec.from_payload({
        "pattern": "parallel_track",
        "area": {"x_cm": 0, "y_cm": 0, "w_cm": 100, "h_cm": 20},
        "paths": {"a": [[0, 0], [30, 0]]},
    })
    # The area and every path are 20 cm deep, the room profile's shortest move.
    assert build_plan(spec, ["a"], geofence_cm=None).ok
    assert plans[-1].area_cm[1] == fpc._move_range(ROOM_LIMITS)[0]
    assert build_plan(spec, ["a"], geofence_cm=None, unit_limits={"a": METRE_MIN}).ok
    assert plans[-1].area_cm[1] == 100
