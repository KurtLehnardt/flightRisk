"""Each unit's geofence, zone slots and zone gap in the fleet coordinator (A4-06, GND-1).

``FleetCoordinator(units=...)`` plans each unit against its own resolved
``envelope.geofence_cm`` (within the coordinator's ``geofence_cm``), gives each
zone a slot its unit can fly (the fleet's ``alt_slots_cm`` when they fit the
unit's slot range, else the unit's own ``plan.alt_slots_cm``) and keeps each
gap at least as wide as both neighbours' ``plan.zone_gap_cm``. These tests check
that a fleet of Tello units gets exactly the fences, slots, zones, lanes and
re-task chunks of a coordinator without units, that two units with different
fences each get lanes inside their own fence, that a field unit gets field
slots and that a slot outside its unit's slot range is refused at Plan before
anything is sent. Every edge is the in-process fake of
``tests/test_coordinator.py``; no drone, network or real time is involved.
"""
from __future__ import annotations

import dataclasses
from types import MappingProxyType

import pytest

from flightrisk import ground_params
from flightrisk.dashboard.app import fleet_geofence_cm
from flightrisk.drone.coordinator import FleetCoordinator
from flightrisk.ground_params import resolve_ground_unit, units_for
from tests.test_coordinator import FakeClock, FakeEdge, walk

FIELD = "generic_multirotor"
GEOFENCE_ENV = "FLIGHTRISK_GEOFENCE_CM"
FENCE_ROW = "envelope.geofence_cm"
SLOTS_ROW = "plan.alt_slots_cm"
GAP_ROW = "plan.zone_gap_cm"


@pytest.fixture(autouse=True)
def _no_ground_names(monkeypatch):
    """Every name a ground resolution reads is unset, so the shell never changes a result."""
    for name in ground_params.GROUND_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def ground(ids, models=None, environ=None):
    """The coordinator keywords for *ids* as the ground resolves them: ``units`` and ``refused_units``."""
    units, refused = units_for(ids, models, environ=environ or {})
    assert refused == {}
    return {"units": units, "refused_units": refused}


def coordinator(ids, *, area=(400, 400), strip=50, battery=80, **kwargs):
    clock = FakeClock()
    edges = {rid: FakeEdge(rid, clock, 0.0, battery=battery) for rid in ids}
    events = []
    if "units" not in kwargs:
        kwargs.setdefault("min_battery", 30)
    coord = FleetCoordinator(edges, area_cm=area, strip_cm=strip, clock=clock,
                             on_event=lambda name, payload: events.append((name, payload)), **kwargs)
    return coord, edges, events


def with_value(unit, row, value):
    """*unit* with its resolved *row* set to *value*."""
    values = dict(unit.values)
    values[row] = dataclasses.replace(values[row], value=value)
    return dataclasses.replace(unit, values=MappingProxyType(values))


def sent(edges):
    return {rid: list(edge.sent) for rid, edge in edges.items()}


def outcome(coord):
    """The plan ``coord.plan()`` makes, or the text of its refusal."""
    try:
        return coord.plan().to_dict()
    except ValueError as exc:
        return str(exc)


def lose_last(coord, edges, lost_index=2):
    """Start *coord*'s mission, fly every drone but the last, and lose the last after it landed on *lost_index*."""
    ids = list(edges)
    coord.sync()
    plan = coord.plan()
    assert coord.start().ok
    for rid in ids[:-1]:
        edges[rid].telemetry(lane_index=1, is_flying=True)
    edges[ids[-1]].telemetry(lane_index=lost_index, is_flying=False)
    return plan, coord.on_drone_lost(ids[-1])


def path_of(coord, rid):
    """Every point of *rid*'s current lanes on the shared grid, from its takeoff point."""
    zone = coord.plan_result.zone_for(rid)
    return walk((zone.x_cm, zone.y_cm), coord.lanes(rid))


def inside(path, origin, fence_cm):
    return all(abs(x - origin[0]) <= fence_cm and abs(y - origin[1]) <= fence_cm for x, y in path)


# ---------------------------------------------------------------------------
# A fleet of Tellos: today's fence for every value both readers accept
# ---------------------------------------------------------------------------


ACCEPTED_BY_BOTH = [None, "500", "450.5", "1e3", " 600 ", "\t700\n", "39.99", "40.000000001", "0.5", "1e-3",
                    "5001", "+500", "500.", ".5e3", "1_000.5", "６００", "450.123456789012345"]


@pytest.mark.parametrize("raw", ACCEPTED_BY_BOTH)
def test_a_tello_units_fence_is_todays_for_every_value_both_readers_accept(monkeypatch, raw):
    if raw is not None:
        monkeypatch.setenv(GEOFENCE_ENV, raw)
    today = fleet_geofence_cm()
    ids = ["d1", "d2"]
    keywords = ground(ids, environ={GEOFENCE_ENV: raw} if raw is not None else {})
    # The resolver keeps the number the dashboard reads, so the min with it changes nothing.
    for unit in keywords["units"].values():
        assert float(unit.values[FENCE_ROW].value) == today
    plain, _, _ = coordinator(ids, geofence_cm=today)
    tello, _, _ = coordinator(ids, geofence_cm=today, **keywords)
    for rid in ids:
        assert tello._fence_cm(rid) == plain._fence_cm(rid) == today
        assert type(tello._fence_cm(rid)) is float


def test_tello_units_without_a_configured_fence_plan_without_one_as_today():
    ids = ["d1", "d2"]
    plain, _, _ = coordinator(ids)
    tello, _, _ = coordinator(ids, **ground(ids))
    assert [tello._fence_cm(rid) for rid in ids] == [plain._fence_cm(rid) for rid in ids] == [None, None]


@pytest.mark.parametrize("n", [1, 2, 3, 4])
@pytest.mark.parametrize("area,strip", [((400, 400), 50), ((1400, 1300), 100), ((3000, 700), 450)])
@pytest.mark.parametrize("alt_slots", [(100, 130), (120,), (150, 200, 250)])
@pytest.mark.parametrize("gap", [100, 250])
def test_tello_units_get_todays_slots_zones_gap_and_lanes(n, area, strip, alt_slots, gap):
    ids = [f"d{i + 1}" for i in range(n)]
    common = {"area": area, "strip": strip, "alt_slots_cm": alt_slots, "zone_gap_cm": gap, "geofence_cm": 500}
    plain, plain_edges, _ = coordinator(ids, **common)
    tello, tello_edges, _ = coordinator(ids, **common, **ground(ids))
    # A narrow area is refused with the same text by both.
    assert outcome(tello) == outcome(plain)
    assert sent(tello_edges) == sent(plain_edges)
    assert [tello.zone_alt_cm(rid, i) for i, rid in enumerate(ids)] == [
        plain.zone_alt_cm(rid, i) for i, rid in enumerate(ids)]


def test_tello_units_refuse_a_narrow_area_with_todays_text():
    ids = ["d1", "d2", "d3"]
    plain, _, _ = coordinator(ids, area=(250, 400))
    tello, tello_edges, _ = coordinator(ids, area=(250, 400), **ground(ids))
    with pytest.raises(ValueError) as without:
        plain.plan()
    with pytest.raises(ValueError) as with_units:
        tello.plan()
    assert str(with_units.value) == str(without.value) == "area width 250 cm is too narrow for 3 zones with 100 cm gaps"
    assert sent(tello_edges) == {rid: [] for rid in ids}


@pytest.mark.parametrize("lost_index", [0, 2, 4])
def test_tello_units_split_and_offer_a_lost_drones_lanes_exactly_as_today(lost_index):
    # The 500 cm fence keeps drone-1 from the far end of drone-3's zone, so the lanes are split by fence.
    ids = ["drone-1", "drone-2", "drone-3"]
    common = {"area": (1200, 400), "strip": 100, "geofence_cm": 500, "retask_wait_s": 0.0}
    plain, plain_edges, plain_events = coordinator(ids, **common)
    tello, tello_edges, tello_events = coordinator(ids, **common, **ground(ids))
    _, plain_appended = lose_last(plain, plain_edges, lost_index)
    _, tello_appended = lose_last(tello, tello_edges, lost_index)
    assert tello_appended == plain_appended
    assert sent(tello_edges) == sent(plain_edges)
    assert tello_events == plain_events
    assert tello.current_plan() == plain.current_plan()
    assert [tello._fence_cm(rid) for rid in ids] == [plain._fence_cm(rid) for rid in ids] == [500.0] * 3


# ---------------------------------------------------------------------------
# Each unit against its own fence
# ---------------------------------------------------------------------------


def two_fences(geofence_cm):
    """drone-1 a Tello at its 500 cm fence, drone-2 one set to 1500 cm, drone-3 a Tello at 500 cm."""
    ids = ["drone-1", "drone-2", "drone-3"]
    units = {rid: resolve_ground_unit(rid, "tello", environ={}) for rid in ids}
    units["drone-2"] = resolve_ground_unit("drone-2", "tello", environ={GEOFENCE_ENV: "1500"})
    return coordinator(ids, area=(1200, 400), strip=100, geofence_cm=geofence_cm, retask_wait_s=0.0,
                       units=units, refused_units={})


def test_two_units_with_different_fences_each_get_lanes_inside_their_own():
    coord, edges, events = two_fences(2000)
    assert [coord._fence_cm(rid) for rid in edges] == [500.0, 1500.0, 500.0]
    plan, appended = lose_last(coord, edges)
    one, two, three = plan.zones
    # drone-3's lanes from lane 2 on lie more than 500 cm from drone-1's and drone-2's takeoff points, and within
    # drone-2's 1500 cm: drone-2 takes all of them, drone-1 none.
    assert set(appended) == {"drone-2"}
    unflown = list(three.lanes[2:])
    assert coord.lanes("drone-2")[-len(unflown):] == unflown
    assert coord.lanes("drone-1") == list(one.lanes)
    assert walk((two.x_cm, two.y_cm), coord.lanes("drone-2"))[-len(unflown) - 1:] == (
        walk((three.x_cm, three.y_cm), list(three.lanes))[2:])
    assert not any(p["event"] == "retask_failed" for name, p in events if name == "fleet_event")
    assert coord.current_plan().get("uncovered", []) == []
    assert inside(path_of(coord, "drone-1"), one.origin, 500)
    assert inside(path_of(coord, "drone-2"), two.origin, 1500)
    assert not inside(path_of(coord, "drone-2"), two.origin, 500)


def test_without_units_the_same_lanes_find_no_drone_inside_its_fence():
    ids = ["drone-1", "drone-2", "drone-3"]
    coord, edges, events = coordinator(ids, area=(1200, 400), strip=100, geofence_cm=500, retask_wait_s=0.0)
    plan, appended = lose_last(coord, edges)
    assert appended == {}
    assert coord.lanes("drone-2") == list(plan.zones[1].lanes)
    assert any(p["event"] == "retask_failed" for name, p in events if name == "fleet_event")


def test_a_units_fence_never_widens_the_configured_one():
    coord, edges, events = two_fences(500)
    assert [coord._fence_cm(rid) for rid in edges] == [500.0, 500.0, 500.0]
    _, appended = lose_last(coord, edges)
    assert appended == {}
    assert any(p["event"] == "retask_failed" for name, p in events if name == "fleet_event")


def test_a_geofence_nack_still_narrows_a_units_fence():
    coord, _, _ = two_fences(2000)
    coord.sync()
    coord.plan()
    nack = {"type": "nack", "for": "assign_lanes", "drone_id": "drone-2", "reason": "geofence",
            "lanes_total": len(coord.lanes("drone-2")), "geofence_cm": 700.0}
    coord._on_nack("drone-2", nack)
    assert coord._fence_cm("drone-2") == 700.0
    assert coord._fence_cm("drone-1") == 500.0


def test_a_field_unit_is_planned_against_its_own_fence_within_the_configured_one():
    ids = ["d1", "d2"]
    coord, _, _ = coordinator(ids, area=(1200, 400), strip=100, geofence_cm=5000,
                              **ground(ids, {"d2": FIELD}))
    assert [coord._fence_cm(rid) for rid in ids] == [500.0, 3000.0]


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------


def test_a_field_unit_gets_field_slots_and_a_tello_keeps_its_own():
    ids = ["d1", "d2", "d3", "d4"]
    coord, edges, _ = coordinator(ids, area=(1600, 400), strip=100,
                                  **ground(ids, {"d2": FIELD, "d4": FIELD}))
    plan = coord.plan()
    assert [zone.alt_cm for zone in plan.zones] == [100, 1500, 100, 1500]
    assert [edges[rid].of_type("assign_lanes")[-1]["alt_cm"] for rid in ids] == [100, 1500, 100, 1500]
    # The map plan takes its slots from the same rule.
    assert [coord.zone_alt_cm(rid, i) for i, rid in enumerate(ids)] == [100, 1500, 100, 1500]


def test_neighbouring_field_units_alternate_their_own_slots():
    ids = ["d1", "d2", "d3"]
    coord, _, _ = coordinator(ids, area=(1500, 400), strip=100, **ground(ids, {rid: FIELD for rid in ids}))
    assert [zone.alt_cm for zone in coord.plan().zones] == [1000, 1500, 1000]


def test_a_field_unit_takes_the_fleets_slots_when_they_fit_its_range():
    ids = ["d1", "d2"]
    coord, _, _ = coordinator(ids, area=(800, 400), strip=100, alt_slots_cm=(2000, 2500),
                              **ground(ids, {rid: FIELD for rid in ids}))
    assert [zone.alt_cm for zone in coord.plan().zones] == [2000, 2500]


def assert_refused_before_sending(coord, edges, events, message):
    before = coord.status()
    with pytest.raises(ValueError) as refused:
        coord.plan()
    assert str(refused.value) == message
    assert coord.plan_result is None and coord.current_plan() is None
    assert coord.status() == before
    assert not any(edge.sent for edge in edges.values())
    assert events == []


def test_a_slot_above_a_tellos_range_is_refused_at_plan():
    ids = ["d1", "d2"]
    coord, edges, events = coordinator(ids, alt_slot_of=lambda rid, i: 600 if rid == "d2" else 100,
                                       **ground(ids))
    assert_refused_before_sending(
        coord, edges, events,
        "cannot plan: unit d2: its altitude slot 600 cm is outside its slot range 50-500 cm.")


def test_a_room_slot_for_a_field_unit_is_refused_at_plan():
    ids = ["d1", "d2"]
    coord, edges, events = coordinator(ids, area=(800, 400), strip=100, alt_slot_of=lambda rid, i: 130,
                                       **ground(ids, {"d2": FIELD}))
    assert_refused_before_sending(
        coord, edges, events,
        "cannot plan: unit d2: its altitude slot 130 cm is outside its slot range 1000-12000 cm.")


def test_a_units_own_slots_outside_its_range_are_refused_at_plan():
    ids = ["d1", "d2"]
    keywords = ground(ids, {rid: FIELD for rid in ids})
    keywords["units"]["d1"] = with_value(keywords["units"]["d1"], SLOTS_ROW, (900, 13000))
    coord, edges, events = coordinator(ids, area=(800, 400), strip=100, **keywords)
    assert_refused_before_sending(
        coord, edges, events,
        "cannot plan: unit d1: its altitude slot 900 cm is outside its slot range 1000-12000 cm.")


def test_every_unit_with_a_slot_outside_its_range_is_named():
    ids = ["d1", "d2"]
    coord, edges, events = coordinator(ids, alt_slot_of=lambda rid, i: 20, **ground(ids))
    assert_refused_before_sending(
        coord, edges, events,
        "cannot plan: unit d1: its altitude slot 20 cm is outside its slot range 50-500 cm. "
        "unit d2: its altitude slot 20 cm is outside its slot range 50-500 cm.")


def test_without_units_the_slot_is_left_to_the_edges_as_today():
    ids = ["d1", "d2"]
    coord, edges, _ = coordinator(ids, alt_slot_of=lambda rid, i: 600)
    assert [zone.alt_cm for zone in coord.plan().zones] == [600, 600]
    assert all(edge.of_type("assign_lanes") for edge in edges.values())


# ---------------------------------------------------------------------------
# Zone gaps
# ---------------------------------------------------------------------------


def test_each_gap_is_the_widest_its_two_neighbours_ask_for():
    ids = ["d1", "d2", "d3", "d4"]
    keywords = ground(ids)
    keywords["units"]["d2"] = with_value(keywords["units"]["d2"], GAP_ROW, 250)
    coord, _, _ = coordinator(ids, area=(1600, 400), strip=100, **keywords)
    plan = coord.plan()
    # Gaps 250 (d1-d2), 250 (d2-d3) and 100 (d3-d4): each zone is (1600 - 600) // 4 = 250 wide.
    assert [(zone.x_cm, zone.w_cm) for zone in plan.zones] == [(0, 250), (500, 250), (1000, 250), (1350, 250)]
    # Re-tasks keep the widest gap any planned unit asks for.
    assert plan.gap_cm == 250


def test_a_units_gap_never_narrows_the_configured_one():
    ids = ["d1", "d2"]
    coord, _, _ = coordinator(ids, area=(800, 400), strip=100, **ground(ids, {rid: FIELD for rid in ids}))
    plan = coord.plan()
    assert plan.gap_cm == 100
    assert [(zone.x_cm, zone.w_cm) for zone in plan.zones] == [(0, 350), (450, 350)]


def test_a_tellos_gap_applies_under_a_narrower_configured_one():
    ids = ["d1", "d2"]
    coord, _, _ = coordinator(ids, zone_gap_cm=40, **ground(ids))
    plan = coord.plan()
    assert plan.gap_cm == 100
    assert [(zone.x_cm, zone.w_cm) for zone in plan.zones] == [(0, 150), (250, 150)]


def test_a_narrow_area_names_each_gap_when_they_differ():
    ids = ["d1", "d2", "d3"]
    keywords = ground(ids)
    keywords["units"]["d1"] = with_value(keywords["units"]["d1"], GAP_ROW, 250)
    coord, edges, _ = coordinator(ids, area=(400, 400), **keywords)
    with pytest.raises(ValueError, match=r"^area width 400 cm is too narrow for 3 zones with gaps of 250, 100 cm$"):
        coord.plan()
    assert not any(edge.sent for edge in edges.values())


# ---------------------------------------------------------------------------
# Unusable unit values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row,value,error", [
    (FENCE_ROW, 0, ValueError),
    (FENCE_ROW, float("nan"), ValueError),
    (FENCE_ROW, True, ValueError),
    (FENCE_ROW, 10 ** 400, ValueError),
    (SLOTS_ROW, (), ValueError),
    (SLOTS_ROW, (100, 130.5), ValueError),
    (SLOTS_ROW, (0,), ValueError),
    (GAP_ROW, -1, ValueError),
    (GAP_ROW, 100.0, ValueError),
])
def test_a_unit_whose_fence_slots_or_gap_cannot_be_planned_is_refused_at_construction(row, value, error):
    ids = ["d1", "d2"]
    keywords = ground(ids)
    keywords["units"]["d1"] = with_value(keywords["units"]["d1"], row, value)
    with pytest.raises(error, match=r"units\['d1'\]"):
        coordinator(ids, **keywords)


def test_a_unit_without_a_resolved_row_is_refused_at_construction():
    ids = ["d1", "d2"]
    keywords = ground(ids)
    unit = keywords["units"]["d1"]
    keywords["units"]["d1"] = dataclasses.replace(
        unit, values=MappingProxyType({k: v for k, v in unit.values.items() if k != GAP_ROW}))
    with pytest.raises(TypeError, match=r"units\['d1'\] has no resolved `plan.zone_gap_cm` value"):
        coordinator(ids, **keywords)
