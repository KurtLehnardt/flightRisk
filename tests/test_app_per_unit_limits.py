"""The dashboard resolves each fleet drone's unit and plans with it (A4-06, section 27 item 47).

``FleetSupervisor`` resolves its drones once with ``ground_params.units_for``
and hands the units and the refusals to its ``FleetCoordinator``; a map plan
builds each drone's path with its unit's envelope profile, and the map editor's
snap tolerance follows the units' move ranges. A fleet of Tellos (the implicit
inventory's model while no edge reports one) must plan exactly as a
coordinator without units does.

A value that today's readers accept but the resolver refuses (K-3: a takeoff
minimum at or under the Tello's return floor) starts the dashboard and is
refused at Plan, naming the drone and the variable, with nothing sent. A value
today's reader refuses still stops the ground at construction, as before.

No real drone or network is involved: the transport is the recording fake of
``tests/test_fleet_dashboard.py``.
"""

from __future__ import annotations

import dataclasses
import types

import pytest

from flightrisk import ground_params
from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.app import FleetSupervisor, TransportEdgeLink, app, fleet_geofence_cm, socketio
from flightrisk.dashboard.state import app_state
from flightrisk.drone import plan_edit
from flightrisk.drone import plan_paths as pp
from flightrisk.drone.coordinator import FleetCoordinator
from flightrisk.drone.plan_edit import PlanSpec, build_plan
from flightrisk.drone.safety import ROOM_LIMITS, min_takeoff_battery_from_env
from tests.test_fleet_dashboard import FakeClock, FakeGroundTransport, _supervisor

IDS = ("drone-1", "drone-2")
AREA = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}
BATTERY_ENV = "FLIGHTRISK_MIN_TAKEOFF_BATTERY"
FOOTPRINT_ENV = "FLIGHTRISK_CAMERA_FOOTPRINT_CM"


@pytest.fixture(autouse=True)
def _no_ground_names(monkeypatch):
    """Every name a ground resolution reads is unset, so the shell never changes a result."""
    for name in ground_params.GROUND_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _spec(**fields):
    data = {"pattern": "parallel_track", "area": dict(AREA)}
    data.update(fields)
    return PlanSpec.from_payload(data)


def _assign_lanes(transport):
    return [(drone_id, msg) for drone_id, msg in transport.sent if msg["type"] == "assign_lanes"]


def _reference(geofence_cm):
    """A coordinator without units over its own recording transport, built as the supervisor's is."""
    clock = FakeClock()
    transport = FakeGroundTransport(IDS, clock)
    links = {drone_id: TransportEdgeLink(transport, drone_id) for drone_id in IDS}
    coordinator = FleetCoordinator(links, area_cm=400, strip_cm=75, clock=clock, geofence_cm=geofence_cm)
    coordinator.sync()
    return coordinator, transport


# ---------------------------------------------------------------- a fleet of Tellos


def test_a_tello_fleet_hands_the_coordinator_tello_units():
    supervisor, _transport, _sio, _clock = _supervisor(IDS)
    assert set(supervisor.units) == set(IDS) and dict(supervisor.refused_units) == {}
    for drone_id, unit in supervisor.units.items():
        assert unit.unit_id == drone_id and unit.model_id == "tello"
        assert unit.limits is ROOM_LIMITS
        assert (unit.min_move_cm, unit.max_move_cm) == (20, 500)
        assert unit.min_takeoff_battery_pct == 30
    # The coordinator holds the same units and gates with them, not with the environment's minimum.
    assert dict(supervisor.coordinator._units) == dict(supervisor.units)
    assert dict(supervisor.coordinator._refused_units) == {}
    assert supervisor.coordinator._min_battery is None
    assert supervisor.unit_limits() == {drone_id: ROOM_LIMITS for drone_id in IDS}


def test_a_tello_fleet_hands_over_todays_fence_and_area():
    supervisor, _transport, _sio, _clock = _supervisor(IDS)
    assert supervisor.geofence_cm == fleet_geofence_cm()
    assert supervisor.coordinator._geofence_cm == fleet_geofence_cm()
    assert supervisor.map_area_cm == (400, 400) and supervisor.map_strip_cm == 75


def test_a_tello_fleet_plans_exactly_as_a_coordinator_without_units():
    supervisor, transport, _sio, _clock = _supervisor(IDS)
    supervisor.plan()
    reference, reference_transport = _reference(supervisor.geofence_cm)
    reference.plan()
    sent = _assign_lanes(transport)
    assert sent and sent == _assign_lanes(reference_transport)


@pytest.mark.parametrize("pattern", sorted(pp.PATTERNS))
def test_a_tello_fleet_map_plan_is_exactly_todays(pattern):
    supervisor, transport, _sio, _clock = _supervisor(IDS)
    spec = _spec(pattern=pattern)
    built = supervisor.build_map_plan(spec)
    today = build_plan(
        spec, list(IDS), strip_cm=75, gap_cm=supervisor.map_gap_cm, geofence_cm=supervisor.geofence_cm,
        alt_slot_of=supervisor.coordinator.zone_alt_cm, takeoffs=supervisor.layout_takeoffs(),
        sidestep_margin_of=supervisor.coordinator.sidestep_margin_of,
    )
    assert built.payload() == today.payload()
    if not built.ok:
        return
    supervisor.plan(spec=spec)
    reference, reference_transport = _reference(supervisor.geofence_cm)
    area = today.spec.area
    reference.plan_zones(
        today.zone_assignments(), grid_cm=(area.x, area.y, area.w, area.h), gap_cm=today.gap_cm,
        strip_cm=today.strip_cm, uncovered_areas=today.uncovered, details=today.payload(),
    )
    sent = _assign_lanes(transport)
    assert sent and sent == _assign_lanes(reference_transport)


def test_map_config_snap_tolerance_is_the_room_profiles_for_tellos(monkeypatch, tmp_path):
    monkeypatch.setenv("FLIGHTRISK_TILES_DIR", str(tmp_path / "tiles"))
    supervisor, _transport, _sio, _clock = _supervisor(IDS)
    monkeypatch.setattr(app_state, "fleet_supervisor", supervisor)
    assert app_module._map_config()["snap_tolerance_cm"] == pp.SNAP_TOLERANCE_CM == 40
    monkeypatch.setattr(app_state, "fleet_supervisor", None)
    assert app_module._map_config()["snap_tolerance_cm"] == pp.SNAP_TOLERANCE_CM


def test_a_map_plan_builds_each_drone_with_its_units_limits(monkeypatch):
    supervisor, _transport, _sio, _clock = _supervisor(IDS)
    seen = []
    real = plan_edit.build_plan

    def spy(*args, **kwargs):
        seen.append(kwargs.get("unit_limits"))
        return real(*args, **kwargs)

    monkeypatch.setattr(app_module.plan_edit, "build_plan", spy)
    supervisor.build_map_plan(_spec())
    assert seen == [{drone_id: ROOM_LIMITS for drone_id in IDS}]


def test_the_snap_tolerance_follows_the_longest_shortest_move_of_the_units(monkeypatch):
    supervisor, _transport, _sio, _clock = _supervisor(IDS)
    wide = dataclasses.replace(ROOM_LIMITS, name="wide", min_move_cm=100)
    unit = dataclasses.replace(supervisor.units["drone-2"], limits=wide, min_move_cm=100)
    monkeypatch.setattr(supervisor, "units", types.MappingProxyType({**supervisor.units, "drone-2": unit}))
    assert supervisor.snap_tolerance_cm() == pp.snap_tolerance_cm(wide) == 200


def test_a_supervisor_built_without_init_has_no_units():
    supervisor = FleetSupervisor.__new__(FleetSupervisor)
    assert supervisor.units is None and supervisor.refused_units is None
    assert supervisor.unit_limits() is None
    assert supervisor.snap_tolerance_cm() == pp.SNAP_TOLERANCE_CM
    assert supervisor._refused_units_detail() is None


# ---------------------------------------------------------------- refused at Plan (item 47)


def _refused_plan(supervisor, transport, sio, **plan_kwargs):
    with pytest.raises(RuntimeError, match="Plan refused") as refused:
        supervisor.plan(**plan_kwargs)
    events = [p for p in sio.named("fleet_event") if p.get("event") == "plan_refused"]
    assert len(events) == 1
    assert str(refused.value) == f"Plan refused: {events[0]['detail']}"
    # Refused before any clock sync and before anything is sent.
    assert transport.sent == [] and transport.sync_calls == []
    assert supervisor.coordinator.current_plan() is None
    return events[0]["detail"]


@pytest.mark.parametrize("raw", ["15", "20"])
def test_a_takeoff_minimum_at_the_return_floor_starts_the_ground_and_refuses_plan(monkeypatch, raw):
    monkeypatch.setenv(BATTERY_ENV, raw)
    assert min_takeoff_battery_from_env() == float(raw)  # today's reader accepts it
    supervisor, transport, sio, _clock = _supervisor(IDS)
    assert dict(supervisor.units) == {} and set(supervisor.refused_units) == set(IDS)
    detail = _refused_plan(supervisor, transport, sio)
    for drone_id in IDS:
        assert f"unit {drone_id}:" in detail
        assert supervisor.refused_units[drone_id] in detail
    assert BATTERY_ENV in detail and "battery.min_takeoff_pct" in detail
    assert detail.index("unit drone-1:") < detail.index("unit drone-2:")
    # The coordinator refuses the same fleet by itself too.
    with pytest.raises(ValueError, match=f"^cannot plan: .*{BATTERY_ENV}"):
        supervisor.coordinator.plan()
    assert transport.sent == []


def test_a_map_plan_of_a_refused_unit_is_refused_and_its_preview_shows_the_refusal(monkeypatch, tmp_path):
    monkeypatch.setenv(BATTERY_ENV, "20")
    supervisor, transport, sio, _clock = _supervisor(IDS)
    detail = _refused_plan(supervisor, transport, sio, spec=_spec())
    assert BATTERY_ENV in detail
    # The preview never builds a path for the refused unit: it shows the refusal as its error.
    with pytest.raises(ValueError) as refused:
        supervisor.build_map_plan(_spec())
    assert str(refused.value) == detail
    monkeypatch.setenv("FLIGHTRISK_TILES_DIR", str(tmp_path / "tiles"))
    monkeypatch.setattr(app_state, "fleet_supervisor", supervisor)
    client = socketio.test_client(app)
    try:
        client.get_received()
        client.emit("preview_plan", {"plan": {"pattern": "parallel_track", "area": dict(AREA)}})
        received = client.get_received()
    finally:
        client.disconnect()
    assert not [r for r in received if r["name"] == "plan_preview"]
    errors = [r["args"][0]["message"] for r in received if r["name"] == "error"]
    assert errors == [f"Map plan unavailable: {detail}"]
    assert transport.sent == []


def test_a_paused_drone_is_named_before_a_refused_unit(monkeypatch):
    monkeypatch.setenv(BATTERY_ENV, "20")
    supervisor, transport, sio, _clock = _supervisor(IDS)
    monkeypatch.setattr(supervisor.manual, "paused_drones", lambda: ["drone-1"])
    detail = _refused_plan(supervisor, transport, sio)
    assert "under manual control" in detail and BATTERY_ENV not in detail


def test_a_malformed_camera_footprint_refuses_a_plan_without_the_map_too(monkeypatch):
    """A change from today, recorded for the dispatcher (item 47's ground names include this one).

    Today only a map plan and the map editor read ``FLIGHTRISK_CAMERA_FOOTPRINT_CM``, so a malformed
    value never stopped Plan without the map. The ground resolution reads it for every unit and
    refuses the unit, so that Plan is now refused too, naming the drone and the variable.
    """
    monkeypatch.setenv(FOOTPRINT_ENV, "abc")
    supervisor, transport, sio, _clock = _supervisor(IDS)
    detail = _refused_plan(supervisor, transport, sio)
    assert FOOTPRINT_ENV in detail and "unit drone-1:" in detail


# ---------------------------------------------------------------- refused at construction, as today


@pytest.mark.parametrize("raw", ["abc", "10", "101", "nan"])
def test_a_takeoff_minimum_todays_reader_refuses_still_stops_the_ground(monkeypatch, raw):
    monkeypatch.setenv(BATTERY_ENV, raw)
    with pytest.raises(ValueError) as today:
        min_takeoff_battery_from_env()
    with pytest.raises(ValueError) as refused:
        _supervisor(IDS)
    assert str(refused.value) == str(today.value)


def test_a_malformed_geofence_still_stops_the_ground(monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_GEOFENCE_CM", "abc")
    with pytest.raises(ValueError, match="FLIGHTRISK_GEOFENCE_CM"):
        _supervisor(IDS)


def test_the_supervisor_resolves_with_the_ground_environment(monkeypatch):
    calls = []
    real = ground_params.units_for

    def spy(unit_ids, reported_models, *, environ, flags=None):
        calls.append((list(unit_ids), dict(reported_models or {}), environ, flags))
        return real(unit_ids, reported_models, environ=environ, flags=flags)

    monkeypatch.setattr(app_module.ground_params, "units_for", spy)
    _supervisor(IDS)
    assert len(calls) == 1
    unit_ids, reported, environ, flags = calls[0]
    assert unit_ids == list(IDS) and reported == {} and flags is None
    assert environ is app_module.os.environ


def test_a_refused_unit_never_stops_the_status_or_summary(monkeypatch):
    monkeypatch.setenv(BATTERY_ENV, "20")
    supervisor, _transport, _sio, _clock = _supervisor(IDS)
    assert set(supervisor.status_payload()["drones"]) == set(IDS)
    assert supervisor.summary()["drones"] == len(IDS)
    assert isinstance(supervisor.refused_units, types.MappingProxyType)
