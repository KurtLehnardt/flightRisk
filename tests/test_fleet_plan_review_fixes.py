"""Regression tests for the retroactive review of the planner size caps (PR #100)."""

from __future__ import annotations

import json

import pytest

from flightrisk import fleet_protocol as proto
from flightrisk.drone import fleet_patterns as fp
from flightrisk.fleet_protocol import Fence, make_assign_lanes


def _lane(direction, distance):
    return {"direction": direction, "distance_cm": distance}


def _hand_plan(drones, area=(200, 200)):
    return fp.FleetSearchPlan(fp.PatternKind.PARALLEL_SPLIT, area, 100, 0, tuple(drones), geofence_cm=None, max_standoff_cm=None)


def _drone(drone_id, lanes=None, area=(200, 200)):
    lanes = (_lane("forward", 100),) if lanes is None else lanes
    return fp.DronePlan(drone_id, (100, 50), fp.Region(0, 0, *area), 100, tuple(lanes))


# --- P1: drone ids and roster size match the peer-link and authority checks ---


@pytest.mark.parametrize("bad_id", ["drone 1", "é1", "-d1", "d" * (proto.MAX_ID_LEN + 1), ""])
def test_planner_refuses_ids_the_protocol_does_not_accept(bad_id):
    with pytest.raises(ValueError, match="not a drone id"):
        fp.plan_search([bad_id, "ok"], (400, 400), 100, footprint=fp.SQUARE_FOOTPRINT, geofence_cm=None)


@pytest.mark.parametrize("bad_id", ["drone 1", "é1", "-d1"])
def test_check_plan_flags_a_non_identifier_drone_id(bad_id):
    problems = fp.check_plan(_hand_plan([_drone(bad_id)]), min_coverage=None)
    assert any("not a drone id" in p for p in problems), problems


def test_valid_identifiers_still_plan():
    plan = fp.plan_search(["drone-1", "edge_2.a:b"], (400, 400), 100, footprint=fp.SQUARE_FOOTPRINT, geofence_cm=None)
    assert fp.check_plan(plan, min_coverage=None) == []


def test_more_drones_than_the_roster_cap_are_refused():
    ids = [f"d{i}" for i in range(proto.MAX_ROSTER + 1)]
    with pytest.raises(ValueError, match="at most"):
        fp.plan_search(ids, (3000, 600), 100, footprint=fp.SQUARE_FOOTPRINT, geofence_cm=None)
    plan = _hand_plan([_drone(i) for i in ids])
    assert any("roster cap" in p for p in fp.check_plan(plan, min_coverage=None))


def test_a_full_roster_still_plans():
    ids = [f"d{i}" for i in range(proto.MAX_ROSTER)]
    plan = fp.plan_search(ids, (3000, 600), 100, footprint=fp.SQUARE_FOOTPRINT, geofence_cm=None)
    assert len(plan.drones) == proto.MAX_ROSTER and fp.check_plan(plan, min_coverage=None) == []


# --- P2: the worst-case v2 assign_lanes fits, with the reserve covering the envelope ---


def test_worst_case_v2_assign_lanes_fits_and_the_reserve_covers_the_envelope():
    drone_id = "d" * proto.MAX_ID_LEN
    lanes = [_lane("forward" if i % 2 == 0 else "back", 500) for i in range(proto.MAX_LANES)]
    zone = {"x_cm": 20_000, "y_cm": -20_000, "w_cm": 20_000, "h_cm": 20_000, "takeoff_x_cm": 20_000, "takeoff_y_cm": -20_000}

    def encoded(msg):
        return len(json.dumps(msg, separators=(",", ":"), allow_nan=False).encode("utf-8"))

    plain = make_assign_lanes(drone_id, 300, lanes, zone)
    full = make_assign_lanes(
        drone_id, 300, lanes, zone,
        fence=Fence(proto.MAX_EPOCH, "l" * proto.MAX_ID_LEN),
        cmd_id="c" * proto.MAX_ID_LEN, plan_id="p" * proto.MAX_ID_LEN,
        plan_seq=2**53 - 1, on_complete="hover", hover_timeout_s=120.0,
    )
    wire = proto.encode(full, direction=proto.DIRECTION_GROUND_TO_EDGE, proto=2)
    assert len(wire.encode("utf-8")) <= proto.MAX_MESSAGE_BYTES
    assert encoded(full) - encoded(plain) <= fp.MESSAGE_RESERVE_BYTES
    assert encoded(plain) <= proto.MAX_MESSAGE_BYTES - fp.MESSAGE_RESERVE_BYTES


# --- structured detection of a drone with no lanes ------------------------------


def test_a_drone_id_containing_no_lanes_cannot_hide_broken_lanes():
    bad_lane = {"direction": "up", "distance_cm": 100}
    plan = _hand_plan([_drone("no lanes", lanes=[bad_lane])])
    problems = fp.check_plan(plan)
    assert any("coverage was not simulated" in p for p in problems), problems


def test_a_drone_with_no_lanes_is_reported_once_and_does_not_block_the_simulation():
    problems = fp.check_plan(_hand_plan([_drone("a", lanes=[])]))
    assert any("has no lanes" in p for p in problems)
    assert not any("coverage was not simulated" in p for p in problems)
