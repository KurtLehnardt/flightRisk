"""The preview's snap tolerance follows the units' minimum move; a non-default altitude margin is recorded."""

from __future__ import annotations

import dataclasses

from flightrisk.drone import fleet_plan_check as fpc
from flightrisk.drone import plan_paths as pp
from flightrisk.drone.plan_edit import PlanSpec, build_plan
from flightrisk.drone.safety import ROOM_LIMITS

METRE_MIN = dataclasses.replace(ROOM_LIMITS, name="metre-min", min_move_cm=100, max_move_cm=1000)
HALF_METRE_MIN = dataclasses.replace(ROOM_LIMITS, name="half-metre-min", min_move_cm=50, max_move_cm=1000)


def test_room_limits_give_the_fixed_tolerance():
    assert pp.snap_tolerance_cm() == pp.SNAP_TOLERANCE_CM
    assert pp.snap_tolerance_cm(ROOM_LIMITS) == pp.SNAP_TOLERANCE_CM
    assert type(pp.snap_tolerance_cm(ROOM_LIMITS)) is int


def test_a_longer_minimum_move_gives_twice_the_minimum_move():
    assert pp.snap_tolerance_cm(METRE_MIN) == 200
    assert pp.snap_tolerance_cm(HALF_METRE_MIN) == 100


def test_a_mixed_plan_gives_the_largest():
    assert pp.snap_tolerance_cm([ROOM_LIMITS, METRE_MIN, HALF_METRE_MIN]) == 200
    assert pp.snap_tolerance_cm([ROOM_LIMITS, ROOM_LIMITS]) == pp.SNAP_TOLERANCE_CM


def test_staircase_stays_within_the_reported_tolerance():
    for limits in (ROOM_LIMITS, METRE_MIN):
        bound = pp.snap_tolerance_cm(limits)
        points = pp.staircase((0, 0), (900, 700), limits=limits)
        assert points
        for x, y in points:
            distance = abs(700 * x - 900 * y) / (900 ** 2 + 700 ** 2) ** 0.5
            assert distance < bound


def _build(unit_limits):
    spec = PlanSpec("parallel_track", pp.Rect(0, 0, 450, 3000))
    return build_plan(spec, list(unit_limits), strip_cm=150, geofence_cm=None, unit_limits=unit_limits,
                      alt_slot_of=lambda drone_id, index: int(unit_limits[drone_id].default_alt_slot_cm))


def test_a_built_plan_payload_uses_its_drones_limits():
    assert _build({"a": ROOM_LIMITS}).payload()["snap_tolerance_cm"] == pp.SNAP_TOLERANCE_CM
    assert _build({"a": ROOM_LIMITS, "b": METRE_MIN}).payload()["snap_tolerance_cm"] == 200
    assert build_plan(PlanSpec("parallel_track", pp.Rect(0, 0, 450, 300)), ["a"], strip_cm=150,
                      geofence_cm=None).payload()["snap_tolerance_cm"] == pp.SNAP_TOLERANCE_CM


def _footprint(**kwargs):
    return fpc.PlanningFootprint("forward", 0, 0, 0, 0, 0, "src", 100, **kwargs)


def test_a_default_footprint_records_no_margin():
    assert "altitude_margin_cm" not in _footprint().to_dict()
    assert "altitude_margin_cm" not in _footprint(altitude_margin_cm=fpc.ALTITUDE_MARGIN_CM).to_dict()


def test_a_non_default_margin_is_recorded():
    margin = fpc.ALTITUDE_MARGIN_CM + 7
    assert _footprint(altitude_margin_cm=margin).to_dict()["altitude_margin_cm"] == margin
