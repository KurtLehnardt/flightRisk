"""Each unit's move range and takeoff battery minimum in the fleet coordinator (A4-02, A4-10, item 47).

``FleetCoordinator(units=..., refused_units=...)`` takes the ground's
resolution of each unit (``ground_params.units_for``). These tests check that
a fleet of Tello units plans, transits, re-flies and gates exactly as a
coordinator without units does, that a field unit plans and transits with its
own longer moves, that one unit's battery minimum gates only that unit, and
that a refused unit stops ``plan()`` and ``plan_zones()`` with its text before
anything is sent. Every edge is the in-process fake of
``tests/test_coordinator.py``; no drone, network or real time is involved.
"""
from __future__ import annotations

import dataclasses
import math

import pytest

from flightrisk.drone.coordinator import (
    MIN_START_BATTERY,
    TELLO_MAX_MOVE_CM,
    TELLO_MIN_MOVE_CM,
    FleetCoordinator,
    ZoneAssignment,
    _transit,
)
from flightrisk.drone.obstacle_sensor import CUT_OBSTACLE, SEGMENT_TOO_SHORT
from flightrisk.drone.safety import FIELD_LIMITS
from flightrisk.drone.search import generate_parallel_track
from flightrisk.ground_params import units_for
from tests.test_coordinator import FakeClock, FakeEdge, walk
from tests.test_coordinator_reflight import LANES, REFLIGHT, TRANSIT, Fleet, released

FIELD = "generic_multirotor"
BATTERY_ENV = "FLIGHTRISK_MIN_TAKEOFF_BATTERY"
FIELD_MAX_MOVE_CM = 5000


@pytest.fixture(autouse=True)
def _no_battery_override(monkeypatch):
    # The coordinator without units reads this variable; the units here are resolved from an empty environment.
    monkeypatch.delenv(BATTERY_ENV, raising=False)


def ground(ids, models=None, environ=None):
    """The coordinator keywords for *ids* as the ground resolves them: ``units`` and ``refused_units``."""
    units, refused = units_for(ids, models, environ=environ or {})
    return {"units": units, "refused_units": refused}


def coordinator(ids, *, area=(400, 400), strip=50, battery=80, **kwargs):
    clock = FakeClock()
    edges = {rid: FakeEdge(rid, clock, 0.0, battery=battery) for rid in ids}
    events = []
    coord = FleetCoordinator(edges, area_cm=area, strip_cm=strip, clock=clock,
                             on_event=lambda name, payload: events.append((name, payload)), **kwargs)
    return coord, edges, events


def with_min_move(unit, min_move_cm):
    """*unit* as a field unit whose shortest move is *min_move_cm*."""
    limits = dataclasses.replace(FIELD_LIMITS, min_move_cm=min_move_cm)
    return dataclasses.replace(unit, limits=limits, min_move_cm=min_move_cm, max_move_cm=FIELD_MAX_MOVE_CM)


def sent(edges):
    return {rid: list(edge.sent) for rid, edge in edges.items()}


# ---------------------------------------------------------------------------
# Tello units behave exactly like no units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 3, 4])
@pytest.mark.parametrize("area,strip", [((400, 400), 50), ((1400, 1300), 100), ((3000, 700), 450)])
def test_tello_units_plan_exactly_like_no_units(n, area, strip):
    ids = [f"d{i + 1}" for i in range(n)]
    plain, plain_edges, _ = coordinator(ids, area=area, strip=strip)
    tello, tello_edges, _ = coordinator(ids, area=area, strip=strip, **ground(ids))
    assert tello.plan().to_dict() == plain.plan().to_dict()
    assert sent(tello_edges) == sent(plain_edges)


def test_tello_units_refuse_a_narrow_area_like_no_units():
    ids = ["d1", "d2"]
    plain, plain_edges, _ = coordinator(ids, area=(130, 400))
    tello, tello_edges, _ = coordinator(ids, area=(130, 400), **ground(ids))
    with pytest.raises(ValueError) as without:
        plain.plan()
    with pytest.raises(ValueError) as with_units:
        tello.plan()
    assert str(with_units.value) == str(without.value)
    assert sent(tello_edges) == sent(plain_edges) == {"d1": [], "d2": []}


def lose_drone_2(area, strip, **kwargs):
    """A started two-drone mission in which drone-2 lands on lane 2 and is declared lost."""
    coord, edges, events = coordinator(["drone-1", "drone-2"], area=area, strip=strip, **kwargs)
    coord.sync()
    plan = coord.plan()
    assert coord.start().ok
    edges["drone-1"].telemetry(lane_index=1, is_flying=True)
    edges["drone-2"].telemetry(lane_index=2, is_flying=False)
    assert set(coord.on_drone_lost("drone-2")) == {"drone-1"}
    return coord, edges, events, plan


def retask_transit(edges, plan):
    """drone-1's transit to drone-2's lane 2, where it starts and where it ends, after ``lose_drone_2``."""
    one, two = plan.zones
    unflown = list(two.lanes[2:])
    lanes = edges["drone-1"].of_type("assign_lanes")[-1]["lanes"]
    assert lanes[:len(one.lanes)] == list(one.lanes) and lanes[-len(unflown):] == unflown
    transit = lanes[len(one.lanes):len(lanes) - len(unflown)]
    own_end = walk((one.x_cm, one.y_cm), list(one.lanes))[-1]
    target = walk((two.x_cm, two.y_cm), list(two.lanes))[2]
    assert walk(own_end, transit)[-1] == target
    return transit, own_end, target


def sideways(transit):
    return [lane["distance_cm"] for lane in transit if lane["direction"] in ("left", "right")]


def test_tello_units_retask_a_lost_drone_exactly_like_no_units():
    ids = ["drone-1", "drone-2"]
    plain, plain_edges, plain_events, _ = lose_drone_2((2400, 400), 500)
    tello, tello_edges, tello_events, plan = lose_drone_2((2400, 400), 500, **ground(ids))
    assert sent(tello_edges) == sent(plain_edges)
    assert tello_events == plain_events
    assert tello.current_plan() == plain.current_plan()
    # 1250 cm sideways, split within the Tello's longest move.
    transit, _, _ = retask_transit(tello_edges, plan)
    assert sideways(transit) == [417, 417, 416]


@pytest.mark.parametrize("n", [2, 3])
def test_tello_units_offer_a_reflight_exactly_like_no_units(n):
    ids = [f"drone-{i + 1}" for i in range(n)]
    plain = released(n)
    tello = released(n, **ground(ids))
    assert sent(tello.edges) == sent(plain.edges)
    assert tello.events == plain.events
    assert tello.coord.current_plan() == plain.coord.current_plan()
    if n == 2:
        assert tello.assigns("drone-2")[-1]["lanes"] == list(LANES) + TRANSIT + REFLIGHT


def test_tello_units_gate_the_start_exactly_like_no_units():
    ids = ["d1", "d2"]
    plain, _, _ = coordinator(ids, battery=25)
    tello, _, _ = coordinator(ids, battery=25, **ground(ids))
    for coord in (plain, tello):
        coord.sync()
        coord.plan()
    assert tello.start().refusals == plain.start().refusals == {
        "d1": f"battery 25% below {MIN_START_BATTERY}%", "d2": f"battery 25% below {MIN_START_BATTERY}%"}


def test_the_tello_aliases_stay():
    assert (TELLO_MIN_MOVE_CM, TELLO_MAX_MOVE_CM, MIN_START_BATTERY) == (20, 500, 30)


# ---------------------------------------------------------------------------
# A field unit's longer moves
# ---------------------------------------------------------------------------


def test_each_zone_is_planned_with_its_own_units_move_range():
    ids = ["d1", "d2"]
    plain, _, _ = coordinator(ids, area=(1100, 1200), strip=500)
    mixed, _, _ = coordinator(ids, area=(1100, 1200), strip=500, **ground(ids, {"d2": FIELD}))
    room, field = mixed.plan().zones
    # The Tello's zone is exactly what it is without units: 1200 cm forward in moves of at most 500 cm.
    assert room == plain.plan().zones[0]
    assert [lane["distance_cm"] for lane in room.lanes] == [500, 500, 200]
    # The field unit flies the same zone width in one move.
    assert list(field.lanes) == [{"direction": "forward", "distance_cm": 1200}]
    pattern = generate_parallel_track(500, 1200, 500, limits=FIELD_LIMITS)
    assert [(wp.direction, wp.distance_cm) for wp in pattern] == [("forward", 1200)]


def test_a_field_unit_gets_transit_moves_of_up_to_5000_cm():
    ids = ["drone-1", "drone-2"]
    _, edges, _, plan = lose_drone_2((20000, 400), 2000, **ground(ids, {"drone-1": FIELD, "drone-2": FIELD}))
    transit, own_end, target = retask_transit(edges, plan)
    # 6050 cm sideways: two moves within the field unit's 5000 cm, where a Tello needs 13.
    assert sideways(transit) == [3025, 3025]
    assert len(sideways(transit)) == math.ceil(sum(sideways(transit)) / FIELD_MAX_MOVE_CM)
    assert transit == _transit(own_end, target, min_move_cm=TELLO_MIN_MOVE_CM, max_move_cm=FIELD_MAX_MOVE_CM)
    assert len(sideways(_transit(own_end, target))) == 13


def test_a_field_unit_gets_moves_longer_than_a_tellos_on_a_retask():
    ids = ["drone-1", "drone-2"]
    _, edges, _, plan = lose_drone_2((2400, 400), 500, **ground(ids, {"drone-1": FIELD}))
    transit, _, _ = retask_transit(edges, plan)
    assert sideways(transit) == [1250]


def test_a_field_receiver_flies_the_reflight_transit_in_one_sideways_move():
    ids = ["drone-1", "drone-2"]
    fleet = released(2, **ground(ids, {"drone-2": FIELD}))
    offer = fleet.assigns("drone-2")[-1]
    # The Tello's transit is forward 400, then left 275 twice; the field unit moves 550 cm left at once.
    assert TRANSIT[1:] == [{"direction": "left", "distance_cm": 275}] * 2
    assert offer["lanes"] == list(LANES) + [
        {"direction": "forward", "distance_cm": 400},
        {"direction": "left", "distance_cm": 550},
    ] + REFLIGHT


def test_a_reflight_keeps_only_segments_every_possible_receiver_can_fly():
    ids = ["drone-1", "drone-2"]
    # Cut 30 cm before the end of lane 0: 30 cm left, and a 30 cm shadow on lane 2.
    tello = Fleet(2, **ground(ids))
    tello.done("drone-2")
    tello.cut("drone-1", [(0, 370)], CUT_OBSTACLE, lane_index=2)
    tello.done("drone-1")
    (accepted,) = tello.reflight_events("reflight_accepted")
    assert [lane["distance_cm"] for lane in accepted["lanes"]] == [30, 30]

    keywords = ground(ids, {"drone-2": FIELD})
    keywords["units"]["drone-2"] = with_min_move(keywords["units"]["drone-2"], 50)
    coarse = Fleet(2, **keywords)
    coarse.done("drone-2")
    coarse.cut("drone-1", [(0, 370)], CUT_OBSTACLE, lane_index=2)
    coarse.done("drone-1")
    assert len(coarse.assigns("drone-2")) == 1
    (failed,) = coarse.reflight_events()
    assert failed["event"] == "reflight_failed" and failed["detail"].endswith(SEGMENT_TOO_SHORT)


def test_the_zone_must_hold_the_longest_shortest_move_of_the_units():
    ids = ["d1", "d2"]
    # (220 - 100) // 2 = 60 cm per zone: wide enough for a 20 cm move, not for a 100 cm one.
    tello, _, _ = coordinator(ids, area=(220, 400), **ground(ids))
    assert tello.plan().zones[0].w_cm == 60
    keywords = ground(ids, {"d2": FIELD})
    keywords["units"]["d2"] = with_min_move(keywords["units"]["d2"], 100)
    coarse, edges, _ = coordinator(ids, area=(220, 400), **keywords)
    with pytest.raises(ValueError, match="too narrow for 2 zones"):
        coarse.plan()
    assert coarse.plan_result is None and sent(edges) == {"d1": [], "d2": []}


# ---------------------------------------------------------------------------
# Each unit's takeoff battery minimum
# ---------------------------------------------------------------------------


def test_a_40_percent_minimum_on_one_unit_refuses_only_that_unit():
    units = dict(ground(["d1"], environ={BATTERY_ENV: "40"})["units"])
    units.update(ground(["d2"])["units"])
    coord, _, _ = coordinator(["d1", "d2"], battery=35, units=units, refused_units={})
    coord.sync()
    coord.plan()
    result = coord.start()
    assert not result.ok
    assert result.refusals == {"d1": "battery 35% below 40%"}


def test_a_40_percent_minimum_on_one_unit_keeps_only_that_unit_from_a_reflight():
    units = dict(ground(["drone-2"], environ={BATTERY_ENV: "40"})["units"])
    units.update(ground(["drone-1", "drone-3"])["units"])
    fleet = Fleet(3, units=units, refused_units={})
    for rid in ("drone-2", "drone-3"):
        fleet.done(rid, battery=35)
    with fleet.coord._lock:
        eligible, why, _ = fleet.coord._reflight_candidates_locked("drone-1")
    assert eligible == ["drone-3"]
    assert why == {"drone-2": "has battery 35%, below 40%"}


def test_with_units_the_coordinator_does_not_read_the_battery_variable(monkeypatch):
    # Each unit carries its own resolved minimum (30 % here); the coordinator never reads the variable itself.
    monkeypatch.setenv(BATTERY_ENV, "50")
    coord, _, _ = coordinator(["d1", "d2"], battery=40, **ground(["d1", "d2"]))
    coord.sync()
    coord.plan()
    assert coord.start().ok
    # A malformed variable stops a coordinator without units at construction; with units it is not read.
    monkeypatch.setenv(BATTERY_ENV, "abc")
    with pytest.raises(ValueError, match=BATTERY_ENV):
        coordinator(["d1", "d2"])
    coordinator(["d1", "d2"], **ground(["d1", "d2"]))


# ---------------------------------------------------------------------------
# A refused unit stops Plan (item 47)
# ---------------------------------------------------------------------------


def refused_fleet():
    """d1 a Tello and d2 a field unit, with the takeoff minimum set to 20 %: the Tello is refused."""
    keywords = ground(["d1", "d2"], {"d2": FIELD}, environ={BATTERY_ENV: "20"})
    assert set(keywords["units"]) == {"d2"} and set(keywords["refused_units"]) == {"d1"}
    return coordinator(["d1", "d2"], **keywords) + (keywords["refused_units"]["d1"],)


def test_a_tello_with_a_20_percent_takeoff_minimum_is_refused_at_plan_naming_the_variable():
    coord, edges, events, text = refused_fleet()
    coord.sync()
    before = coord.status()
    with pytest.raises(ValueError) as refused:
        coord.plan()
    message = str(refused.value)
    assert message == f"cannot plan: {text}"
    assert "unit d1" in message and BATTERY_ENV in message and "unit d2" not in message
    assert coord.plan_result is None and coord.current_plan() is None
    assert coord.status() == before
    assert not any(edge.of_type("assign_lanes") for edge in edges.values())
    assert events == []
    assert coord.start().ok is False


def test_a_refused_unit_stops_a_map_plan_too():
    coord, edges, _, _ = refused_fleet()
    zones = [ZoneAssignment(drone_id=rid, x_cm=x, y_cm=0, w_cm=200, h_cm=400, alt_cm=100,
                            lanes=({"direction": "forward", "distance_cm": 400},))
             for rid, x in (("d1", 0), ("d2", 300))]
    with pytest.raises(ValueError, match="cannot plan: unit d1: .*" + BATTERY_ENV):
        coord.plan_zones(zones, grid_cm=(0, 0, 500, 400), gap_cm=100, strip_cm=100)
    assert coord.plan_result is None
    assert not any(edge.of_type("assign_lanes") for edge in edges.values())


def test_every_refused_unit_is_named():
    keywords = ground(["d1", "d2"], environ={BATTERY_ENV: "20"})
    coord, _, _ = coordinator(["d1", "d2"], **keywords)
    with pytest.raises(ValueError) as refused:
        coord.plan()
    assert str(refused.value) == "cannot plan: " + " ".join(keywords["refused_units"][rid] for rid in ("d1", "d2"))


# ---------------------------------------------------------------------------
# What the constructor accepts
# ---------------------------------------------------------------------------


def _bad_keywords():
    tello = ground(["d1", "d2"])["units"]
    wrong_range = dataclasses.replace(tello["d2"], min_move_cm=30)
    return [
        ({"refused_units": {"d1": "unit d1: refused."}}, ValueError, "refused_units needs units"),
        ({"units": [tello["d1"], tello["d2"]]}, TypeError, "units must be a mapping"),
        ({"units": {"d1": tello["d1"]}, "refused_units": ["d2"]}, TypeError, "refused_units must be a mapping"),
        ({"units": {"d1": tello["d1"], "d2": object()}}, TypeError, "must be a ground unit"),
        ({"units": {"d1": tello["d1"], "d2": tello["d1"]}}, ValueError, "is the unit 'd1'"),
        ({"units": {"d1": tello["d1"]}}, ValueError, r"\['d2'\] are in neither"),
        ({"units": tello, "refused_units": {"d2": "unit d2: refused."}}, ValueError, "in both"),
        ({"units": dict(tello, d3=dataclasses.replace(tello["d1"], unit_id="d3"))}, ValueError, "without a link"),
        ({"units": {"d1": tello["d1"]}, "refused_units": {"d2": 7}}, TypeError, "map drone ids to text"),
        ({"units": {"d1": tello["d1"]}, "refused_units": {"d2": " "}}, ValueError, "must say why"),
        ({"units": tello, "min_battery": 35}, ValueError, "min_battery or units"),
        ({"units": dict(tello, d2=wrong_range)}, ValueError, "its limits give 20-500 cm"),
        ({"units": dict(tello, d2=dataclasses.replace(tello["d2"], min_move_cm=20.0))}, TypeError, "whole centimetres"),
        ({"units": dict(tello, d2=dataclasses.replace(tello["d2"], min_takeoff_battery_pct=math.nan))}, TypeError,
         "finite number"),
        ({"units": dict(tello, d2=dataclasses.replace(tello["d2"], limits=None))}, TypeError, "EnvelopeLimits"),
    ]


@pytest.mark.parametrize("keywords,error,match", _bad_keywords())
def test_units_that_do_not_match_the_links_are_refused(keywords, error, match):
    with pytest.raises(error, match=match):
        coordinator(["d1", "d2"], **keywords)


def test_the_units_are_kept_as_read_only_copies():
    keywords = ground(["d1", "d2"], {"d2": FIELD})
    coord, _, _ = coordinator(["d1", "d2"], area=(1100, 1200), strip=500, **keywords)
    keywords["units"]["d2"] = keywords["units"]["d1"]
    assert coord.plan().zones[1].lanes == ({"direction": "forward", "distance_cm": 1200},)
    with pytest.raises(TypeError):
        coord._units["d2"] = keywords["units"]["d1"]
    with pytest.raises(TypeError):
        coord._refused_units["d2"] = "unit d2: refused."
