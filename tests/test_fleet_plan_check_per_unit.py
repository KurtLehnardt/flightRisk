"""Per-unit limits in the fleet plan checks: each drone's move and slot range, and the area cap.

The checks take one ``safety.EnvelopeLimits`` per drone (``ROOM_LIMITS`` by
default) and the area cap as ``max_area_side_cm`` (``MAX_AREA_SIDE_CM`` by
default, ``None`` for no cap). With the room profile every result, refusal and
message is today's; a profile with longer moves or no area cap accepts what the
room profile refuses, and only for the drones it is given to.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from flightrisk.drone import fleet_patterns as fp
from flightrisk.drone import fleet_plan_check as fpc
from flightrisk.drone import fleet_plan_model as fpm
from flightrisk.drone.coverage import tello_sweep_model
from flightrisk.drone.safety import FIELD_LIMITS, ROOM_LIMITS

# The 4 m demo room, as the existing pattern tests model the room camera.
ROOM_CAMERA = tello_sweep_model(max_range_m=4.0)
# The room profile with field-sized moves; every other range stays the room's.
LONG_MOVES = dataclasses.replace(ROOM_LIMITS, name="long-moves", max_move_cm=5000)


def _lane(direction, distance):
    return {"direction": direction, "distance_cm": distance}


def _hand_plan(drones, area=(200, 3200), **kwargs):
    """A square-model plan built by hand, with the fence and standoff checks off."""
    return fp.FleetSearchPlan(
        fp.PatternKind.PARALLEL_SPLIT, area, 100, 0, tuple(drones),
        geofence_cm=kwargs.pop("geofence_cm", None), max_standoff_cm=kwargs.pop("max_standoff_cm", None), **kwargs,
    )


def _drone(drone_id, lanes, *, x0=0, alt=100, depth=3200):
    return fp.DronePlan(drone_id, (x0 + 100, 50), fp.Region(x0, 0, x0 + 200, depth), alt, tuple(lanes))


def _room_plans():
    """Plans of one to four drones as the builders make them today, plus hand plans with problems."""
    builds = [
        ((400, 400), None, {"footprint": ROOM_CAMERA}),
        ((1200, 800), None, {"footprint": ROOM_CAMERA}),
        ((1200, 600), 120, {"footprint": fp.SQUARE_FOOTPRINT, "geofence_cm": None}),
    ]
    plans = []
    for n in (1, 2, 3, 4):
        ids = [f"d{i}" for i in range(1, n + 1)]
        for area, sweep, kwargs in builds:
            try:
                plans.append(fp.plan_search(ids, area, sweep, **kwargs))
            except ValueError:
                continue  # refused by the builder today; nothing to check
    assert len(plans) >= 9
    plans.append(_hand_plan([_drone("a", [_lane("forward", 3000)])]))
    plans.append(_hand_plan([_drone("a", [_lane("forward", 10), _lane("left", 501)], alt=30)]))
    plans.append(_hand_plan([_drone("a", [_lane("forward", 100)], alt=501)]))
    return plans


# --- Room units give today's results ---


def test_room_limits_for_every_drone_give_the_default_check_result():
    for plan in _room_plans():
        room = {drone.drone_id: ROOM_LIMITS for drone in plan.drones}
        assert fpc._check(plan, unit_limits=room) == fpc._check(plan)
        assert fp.check_plan(plan, unit_limits=room, max_area_side_cm=fp.MAX_AREA_SIDE_CM) == fp.check_plan(plan)
        assert fp.check_plan(plan, unit_limits=room, min_coverage=None) == fp.check_plan(plan, min_coverage=None)


def test_room_refusals_keep_their_messages():
    problems = fp.check_plan(_room_plans()[-2], min_coverage=None)
    assert problems == [
        "a: lane 0 {'direction': 'forward', 'distance_cm': 10} is not a 20-500 cm horizontal move",
        "a: lane 1 {'direction': 'left', 'distance_cm': 501} is not a 20-500 cm horizontal move",
        "a: altitude slot 30 cm is out of range (" + fpc.slot_below_minimum(30) + ")",
    ]
    with pytest.raises(ValueError) as default:
        fpc._slots([20])
    with pytest.raises(ValueError) as room:
        fpc._slots([20], limits=ROOM_LIMITS)
    assert str(room.value) == str(default.value)
    assert str(default.value).startswith("altitude slot must be within 50-500 cm, got 20: ")
    for bad in (19, 20_001):
        with pytest.raises(ValueError) as default:
            fpc._area(bad)
        with pytest.raises(ValueError) as room:
            fpc._area(bad, unit_limits={"a": ROOM_LIMITS}, max_area_side_cm=fp.MAX_AREA_SIDE_CM)
        assert str(room.value) == str(default.value)
    assert fpc._area((400, 300), unit_limits={"a": ROOM_LIMITS}) == fpc._area((400, 300)) == (400, 300)


def test_room_limits_give_todays_moves():
    region = fp.Region(0, 0, 1300, 1300)
    points = [(10, 10), (1210, 10), (1210, 25), (10, 25), (10, 1290)]
    assert fpm._polyline_lanes(points, region, limits=ROOM_LIMITS) == fpm._polyline_lanes(points, region)
    assert [lane["distance_cm"] for lane in fpm._split_distance("forward", 1234)] == [412, 411, 411]
    assert fpm._split_distance("forward", 1234, limits=ROOM_LIMITS) == fpm._split_distance("forward", 1234)
    assert fpm._leg_moves((90, 50), (95, 50), fp.Region(0, 0, 100, 100), limits=ROOM_LIMITS) == [
        _lane("left", 20), _lane("right", 25),
    ]


def test_default_altitude_margins_give_todays_footprint():
    for footprint, sweep in ((ROOM_CAMERA, None), (ROOM_CAMERA, 60), (fp.SQUARE_FOOTPRINT, 120)):
        default = fp.planning_footprint(footprint, sweep, (100, 160), 400)
        explicit = fp.planning_footprint(
            footprint, sweep, (100, 160), 400, min_sweep_cm=fp.MIN_SWEEP_CM,
            altitude_deadband_cm=fp.ALTITUDE_DEADBAND_CM, altitude_tolerance_cm=fp.ALTITUDE_TOLERANCE_CM,
        )
        assert explicit == default
        assert default.altitude_margin_cm == fp.ALTITUDE_MARGIN_CM
        assert "altitude_margin_cm" not in default.to_dict()


# --- Longer moves for the drones that have them ---


def test_long_moves_accept_a_3000_cm_lane_the_room_profile_refuses():
    plan = _hand_plan([_drone("a", [_lane("forward", 3000)])])
    assert fp.check_plan(plan, min_coverage=None) == [
        "a: lane 0 {'direction': 'forward', 'distance_cm': 3000} is not a 20-500 cm horizontal move"
    ]
    assert fp.check_plan(plan, min_coverage=None, unit_limits={"a": LONG_MOVES}) == []
    too_long = _hand_plan([_drone("a", [_lane("forward", 5001)], depth=5100)], area=(200, 5100))
    assert fp.check_plan(too_long, min_coverage=None, unit_limits={"a": LONG_MOVES}) == [
        "a: lane 0 {'direction': 'forward', 'distance_cm': 5001} is not a 20-5000 cm horizontal move"
    ]


def test_long_moves_split_legs_into_fewer_whole_centimetre_moves():
    assert [lane["distance_cm"] for lane in fpm._split_distance("forward", 3000)] == [500] * 6
    assert fpm._split_distance("forward", 3000, limits=LONG_MOVES) == [_lane("forward", 3000)]
    lanes = fpm._split_distance("right", 7777, limits=dataclasses.replace(FIELD_LIMITS, max_move_cm=5000.0))
    assert [lane["distance_cm"] for lane in lanes] == [3889, 3888]
    assert all(type(lane["distance_cm"]) is int for lane in lanes)
    region = fp.Region(0, 0, 4000, 4000)
    lanes = fpm._polyline_lanes([(0, 0), (3500, 0), (3500, 3999)], region, limits=LONG_MOVES)
    assert lanes == [_lane("right", 3500), _lane("forward", 3999)]
    assert fpm._move_count((0, 0), (3500, 0), limits=LONG_MOVES) == 1
    assert fpm._move_count((0, 0), (3500, 0)) == 7


def test_each_drone_is_checked_against_its_own_limits():
    room = _drone("a", [_lane("forward", 3000)], alt=1500)
    field = _drone("b", [_lane("forward", 3000)], x0=1000, alt=1500)
    plan = _hand_plan([room, field], area=(1200, 3200))
    problems = fp.check_plan(plan, min_coverage=None, min_separation_cm=None, unit_limits={"a": ROOM_LIMITS, "b": FIELD_LIMITS})
    assert problems == [
        "a: lane 0 {'direction': 'forward', 'distance_cm': 3000} is not a 20-500 cm horizontal move",
        "a: altitude slot 1500 cm is out of range",
    ]
    low = _hand_plan([_drone("a", [_lane("forward", 100)], alt=30), _drone("b", [_lane("forward", 100)], x0=1000, alt=30)],
                     area=(1200, 3200))
    problems = fp.check_plan(low, min_coverage=None, min_separation_cm=None, unit_limits={"a": ROOM_LIMITS, "b": FIELD_LIMITS})
    # The room profile names its own minimum; another profile's range check says enough.
    assert problems == [
        "a: altitude slot 30 cm is out of range (" + fpc.slot_below_minimum(30) + ")",
        "b: altitude slot 30 cm is out of range",
    ]


def test_a_field_profile_lets_nothing_below_the_room_minimums_through_for_a_room_drone():
    plan = _hand_plan([_drone("a", [_lane("forward", 10)], alt=40), _drone("b", [_lane("forward", 3000)], x0=1000, alt=1500)],
                      area=(1200, 3200))
    problems = fp.check_plan(plan, min_coverage=None, min_separation_cm=None, unit_limits={"a": ROOM_LIMITS, "b": FIELD_LIMITS})
    assert [p.split(":")[0] for p in problems] == ["a", "a"]
    with pytest.raises(ValueError, match="at least 50 cm"):
        fpc._slots([40], limits=ROOM_LIMITS)
    assert fpc._slots([1500], limits=FIELD_LIMITS) == [1500]
    with pytest.raises(ValueError, match="altitude slot must be within 1000-12000 cm, got 500$"):
        fpc._slots([500], limits=FIELD_LIMITS)


def test_assign_lanes_message_checks_with_the_limits_it_is_given():
    # One 100 cm square sweep along the whole 100 x 3000 cm area, so the coverage gate passes.
    drone = fp.DronePlan("a", (50, 0), fp.Region(0, 0, 100, 3000), 100, (_lane("forward", 3000),))
    plan = _hand_plan([drone], area=(100, 3000))
    with pytest.raises(ValueError, match="20-500 cm horizontal move"):
        plan.assign_lanes_message("a", allow_unchecked=True)
    message = plan.assign_lanes_message("a", allow_unchecked=True, unit_limits={"a": LONG_MOVES})
    assert message["lanes"] == [_lane("forward", 3000)]


# --- The area cap ---


def test_no_area_cap_accepts_an_area_the_room_profile_refuses():
    with pytest.raises(ValueError, match="area width must be at most 20000 cm, got 30000"):
        fpc._area((30_000, 30_000))
    assert fpc._area((30_000, 30_000), max_area_side_cm=None) == (30_000, 30_000)
    assert fpc._area(30_000, max_area_side_cm=30_000.7) == (30_000, 30_000)
    with pytest.raises(ValueError, match="area depth must be at most 30000 cm, got 30001"):
        fpc._area((100, 30_001), max_area_side_cm=30_000)


def test_the_area_cap_also_bounds_the_plan_distances():
    plan = _hand_plan([_drone("a", [_lane("forward", 100)])], geofence_cm=25_000)
    with pytest.raises(ValueError, match=r"geofence_cm must be within \(0, 20000\] cm or None"):
        fp.check_plan(plan, min_coverage=None)
    assert fp.check_plan(plan, min_coverage=None, max_area_side_cm=None) == []
    assert fp.check_plan(plan, min_coverage=None, max_area_side_cm=25_000) == []
    with pytest.raises(ValueError, match="geofence_cm must be a positive distance or None"):
        fp.check_plan(plan, min_coverage=None, geofence_cm=-1, max_area_side_cm=None)


def test_the_shortest_area_side_is_the_largest_minimum_move_of_the_units():
    coarse = dataclasses.replace(ROOM_LIMITS, name="coarse", min_move_cm=100, max_move_cm=500)
    assert fpc._area(50, unit_limits={"a": ROOM_LIMITS}) == (50, 50)
    with pytest.raises(ValueError, match="area width must be at least 100 cm, got 50"):
        fpc._area(50, unit_limits={"a": ROOM_LIMITS, "b": coarse})
    with pytest.raises(ValueError, match="below the 100 cm shortest area side"):
        fpc._area(500, unit_limits={"b": coarse}, max_area_side_cm=99)


# --- Refused arguments ---


def test_a_mapping_that_lacks_a_planned_drone_is_refused():
    plan = _hand_plan([_drone("a", [_lane("forward", 100)]), _drone("b", [_lane("forward", 100)], x0=1000)],
                      area=(1200, 3200))
    with pytest.raises(ValueError, match="no envelope limits for 'b'"):
        fp.check_plan(plan, min_coverage=None, unit_limits={"a": FIELD_LIMITS})
    with pytest.raises(ValueError, match="no envelope limits for 'a', 'b'"):
        fp.check_plan(plan, min_coverage=None, unit_limits={})
    # Limits of drones outside the plan are ignored.
    room = {"a": ROOM_LIMITS, "b": ROOM_LIMITS, "spare": FIELD_LIMITS}
    assert fp.check_plan(plan, min_coverage=None, min_separation_cm=None, unit_limits=room) == []


@pytest.mark.parametrize("bad", [ROOM_LIMITS, [("a", ROOM_LIMITS)], "room"])
def test_unit_limits_must_be_a_mapping(bad):
    plan = _hand_plan([_drone("a", [_lane("forward", 100)])])
    with pytest.raises(TypeError, match="unit_limits must map drone ids"):
        fp.check_plan(plan, min_coverage=None, unit_limits=bad)
    with pytest.raises(TypeError, match="unit_limits must map drone ids"):
        fpc._area(100, unit_limits=bad)


def test_unit_limits_values_must_be_envelope_limits():
    plan = _hand_plan([_drone("a", [_lane("forward", 100)])])
    with pytest.raises(TypeError, match="safety.EnvelopeLimits"):
        fp.check_plan(plan, min_coverage=None, unit_limits={"a": "room"})
    with pytest.raises(TypeError, match="safety.EnvelopeLimits"):
        fpm._split_distance("forward", 100, limits=None)
    with pytest.raises(ValueError, match="at least one drone"):
        fpc._area(100, unit_limits={})


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"min_move_cm": 0}, "min_move_cm must be at least 1 cm"),
        ({"min_move_cm": -20}, "min_move_cm must be at least 1 cm"),
        ({"min_move_cm": True}, "min_move_cm must be a finite number"),
        ({"min_move_cm": math.nan}, "min_move_cm must be a finite number"),
        ({"max_move_cm": math.inf}, "max_move_cm must be a finite number"),
        ({"min_move_cm": 20.5}, "min_move_cm must be a whole number"),
        ({"min_move_cm": 600}, "max_move_cm must be at least twice min_move_cm"),
        ({"max_move_cm": 39}, "max_move_cm must be at least twice min_move_cm"),
        ({"min_alt_slot_cm": 600}, "altitude slot range must be positive and not empty"),
        ({"min_alt_slot_cm": 0}, "altitude slot range must be positive and not empty"),
        ({"max_alt_slot_cm": math.nan}, "max_alt_slot_cm must be a finite number"),
        ({"min_alt_slot_cm": False}, "min_alt_slot_cm must be a finite number"),
    ],
)
def test_a_malformed_envelope_profile_is_refused(changes, message):
    bad = dataclasses.replace(ROOM_LIMITS, name="bad", **changes)
    plan = _hand_plan([_drone("a", [_lane("forward", 100)])])
    with pytest.raises(ValueError, match=message):
        fp.check_plan(plan, min_coverage=None, unit_limits={"a": bad})


@pytest.mark.parametrize("bad", [True, math.nan, math.inf, 0, -5, "20000"])
def test_a_malformed_area_cap_is_refused(bad):
    with pytest.raises(ValueError, match="max_area_side_cm"):
        fpc._area(100, max_area_side_cm=bad)
    with pytest.raises(ValueError, match="max_area_side_cm"):
        fp.check_plan(_hand_plan([_drone("a", [_lane("forward", 100)])]), min_coverage=None, max_area_side_cm=bad)


# --- Sweep and altitude margins ---


def test_min_sweep_cm_sets_the_narrowest_sweep():
    with pytest.raises(ValueError, match="sweep_cm must be at least 40 cm, got 30"):
        fp.planning_footprint(fp.SQUARE_FOOTPRINT, 30, (100,), 400)
    assert fp.planning_footprint(fp.SQUARE_FOOTPRINT, 30, (100,), 400, min_sweep_cm=20).lateral_cm == 30
    with pytest.raises(ValueError, match="sweep_cm must be at least 80 cm, got 60"):
        fp.planning_footprint(fp.SQUARE_FOOTPRINT, 60, (100,), 400, min_sweep_cm=80)


@pytest.mark.parametrize("bad", [True, 0, -1, 12.5, math.nan, 2 * fp.MAX_AREA_SIDE_CM + 1])
def test_a_malformed_min_sweep_is_refused(bad):
    with pytest.raises(ValueError, match="min_sweep_cm"):
        fp.planning_footprint(ROOM_CAMERA, None, (100,), 400, min_sweep_cm=bad)


@pytest.mark.parametrize("name", ["altitude_deadband_cm", "altitude_tolerance_cm"])
@pytest.mark.parametrize("bad", [True, 0, -10, math.nan, math.inf, "10"])
def test_a_malformed_altitude_margin_is_refused(name, bad):
    with pytest.raises(ValueError, match=name):
        fp.planning_footprint(ROOM_CAMERA, None, (100,), 400, **{name: bad})


def test_the_coverage_gate_uses_the_altitude_margin_the_plan_was_derived_with():
    default = fp.planning_footprint(ROOM_CAMERA, None, (100,), 400)
    wider = fp.planning_footprint(ROOM_CAMERA, None, (100,), 400, altitude_deadband_cm=50, altitude_tolerance_cm=30)
    assert wider.altitude_margin_cm == 80
    # A level camera sees less near ground the higher it sits, so the window starts further out.
    assert wider.gate_camera(100, 10).near_cm > default.gate_camera(100, 10).near_cm
    assert wider.near_cm >= default.near_cm
    plan = fp.plan_search(["d1"], (400, 400), None, footprint=ROOM_CAMERA)
    assert plan.feasible
    same = dataclasses.replace(plan, footprint=dataclasses.replace(plan.footprint, altitude_margin_cm=fp.ALTITUDE_MARGIN_CM))
    assert same.content_digest() == plan.content_digest() and same.feasible
    raised = dataclasses.replace(plan, footprint=dataclasses.replace(plan.footprint, altitude_margin_cm=80))
    assert raised.content_digest() != plan.content_digest() and not raised.feasible
    gates = fp.gate_footprints(raised)
    assert gates[0].near_cm == pytest.approx(raised.footprint.gate_camera(plan.drones[0].alt_cm, 10).near_cm)
    assert gates[0].near_cm > fp.gate_footprints(plan)[0].near_cm
