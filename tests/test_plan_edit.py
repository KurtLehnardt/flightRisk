"""Plans built and edited on the map: spec validation, every check, and the coordinator hand-off.

The supervisor tests reuse the in-process fakes of ``tests/test_fleet_dashboard.py``
(a fake ground transport that validates and records every message). No network
and no drone are involved.
"""

from __future__ import annotations

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone import plan_paths as pp
from flightrisk.drone.coordinator import FleetCoordinator, ZoneAssignment
from flightrisk.drone.plan_edit import PlanSpec, PlanSpecError, build_plan
from tests.test_fleet_dashboard import _supervisor, _telemetry

AREA = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}
IDS = ["drone-1", "drone-2"]


def _spec(**fields):
    data = {"pattern": "parallel_track", "area": dict(AREA)}
    data.update(fields)
    return PlanSpec.from_payload(data)


def _build(spec, ids=IDS, **kwargs):
    kwargs.setdefault("geofence_cm", 450)
    kwargs.setdefault("strip_cm", 75)
    return build_plan(spec, ids, **kwargs)


# ---------------------------------------------------------------- spec validation


@pytest.mark.parametrize("data, message", [
    ("plan", "must be an object"),
    ({"pattern": "parallel_track", "area": AREA, "extra": 1}, "unknown plan fields"),
    ({"pattern": "zigzag", "area": AREA}, "pattern must be one of"),
    ({"pattern": "sector"}, "area must be an object"),
    ({"area": {"x_cm": 0, "y_cm": 0, "w_cm": 10, "h_cm": 400}}, "area w_cm"),
    ({"area": {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 30_000}}, "area h_cm"),
    ({"area": {"x_cm": "0", "y_cm": 0, "w_cm": 400, "h_cm": 400}}, "area x_cm"),
    ({"area": {"x_cm": float("nan"), "y_cm": 0, "w_cm": 400, "h_cm": 400}}, "area x_cm"),
    ({"area": AREA, "strip_cm": 5}, "strip_cm"),
    ({"area": AREA, "version": 2}, "unsupported plan version"),
    ({"area": AREA, "paths": []}, "paths must map"),
    ({"area": AREA, "paths": {"bad id!": [[0, 0], [0, 100]]}}, "not a drone id"),
    ({"area": AREA, "paths": {"drone-1": [[0, 0]]}}, "2 to 200 vertices"),
    ({"area": AREA, "paths": {"drone-1": [[0, 0]] * 201}}, "2 to 200 vertices"),
    ({"area": AREA, "paths": {"drone-1": [[0, 0], [0]]}}, "must be [x_cm, y_cm]"),
    ({"area": AREA, "paths": {"drone-1": [[0, 0], [True, 5]]}}, "vertex 1 x"),
    ({"area": AREA, "paths": {"drone-1": [[0, 0], [0, 99_999]]}}, "vertex 1 y"),
    ({"area": AREA, "paths": {"drone-1": "0,0 0,100"}}, "list of"),
])
def test_malformed_specs_are_refused(data, message):
    with pytest.raises(PlanSpecError, match=message.replace("[", r"\[").replace("]", r"\]")):
        PlanSpec.from_payload(data)


def test_spec_round_trips_and_rounds_vertices():
    spec = _spec(strip_cm=80, paths={"drone-1": [[0, 0], [10.4, 99.6]]})
    assert spec.paths["drone-1"] == ((0, 0), (10, 100))
    assert PlanSpec.from_payload(spec.to_dict()) == spec


# ---------------------------------------------------------------- building and checks


def test_unedited_parallel_track_matches_the_coordinator_plan():
    built = _build(_spec())
    assert built.ok, built.problem_text()

    class Link:
        on_message = None

        def send(self, msg):
            pass

    coordinator = FleetCoordinator({d: Link() for d in IDS}, area_cm=400, strip_cm=75)
    legacy = coordinator.plan()
    for zone, drone in zip(legacy.zones, built.drones, strict=True):
        assert (zone.x_cm, zone.y_cm, zone.w_cm, zone.h_cm) == (drone.zone.x, drone.zone.y, drone.zone.w, drone.zone.h)
        assert pp.trace_lanes((zone.x_cm, zone.y_cm), zone.lanes) == list(drone.converted.trace)
        assert drone.edited is False


def test_editing_one_drone_never_changes_another():
    generated = _build(_spec())
    edited = _build(_spec(paths={"drone-1": [[0, 0], [0, 300], [120, 380], [60, 100]]}))
    assert edited.ok, edited.problem_text()
    one, two = edited.drones
    assert one.edited and not two.edited
    assert two.converted.lanes == generated.drones[1].converted.lanes
    assert one.converted.lanes != generated.drones[0].converted.lanes


def test_a_path_that_leaves_the_geofence_is_refused_with_its_vertex():
    built = _build(_spec(paths={"drone-1": [[0, 0], [0, 300], [100, 300], [100, 500], [0, 0]]}))
    assert not built.ok
    problem = next(p for p in built.problems if p.kind == "geofence")
    assert problem.drone_id == "drone-1" and problem.vertex == 3 and problem.point == (100, 500)
    assert "450 cm geofence" in problem.text
    with pytest.raises(ValueError, match="unchecked plan"):
        built.zone_assignments()


def test_crossing_paths_are_refused_naming_both_drones_and_the_place():
    built = _build(_spec(paths={"drone-1": [[0, 0], [0, 200], [300, 200]]}))
    problem = next(p for p in built.problems if p.kind == "separation")
    assert {problem.drone_id, problem.other_drone_id} == {"drone-1", "drone-2"}
    assert "below the 100 cm gap" in problem.text
    assert problem.point == (250.0, 200.0)


def test_paths_closer_than_the_gap_without_crossing_are_refused():
    built = _build(_spec(paths={"drone-1": [[0, 0], [0, 400], [180, 400], [180, 0]]}))
    problem = next(p for p in built.problems if p.kind == "separation")
    assert "come within 70 cm" in problem.text


def test_the_fleet_plan_check_runs_on_edited_plans():
    built = _build(_spec(), alt_slot_of=lambda drone_id, index: 600)
    assert any(p.kind == "check" and "altitude slot 600 cm is out of range" in p.text for p in built.problems)


def test_a_ceiling_refusal_from_the_slot_source_is_a_problem():
    def slot(drone_id, index):
        raise ValueError(f"{drone_id} slot 160 cm is above the ceiling limit 140 cm")

    built = _build(_spec(), alt_slot_of=slot)
    assert [p.kind for p in built.problems].count("altitude") == 2


def test_a_path_for_a_drone_outside_the_fleet_is_refused():
    built = _build(_spec(paths={"drone-9": [[0, 0], [0, 100]]}))
    assert any("drone-9, which is not in the fleet" in p.text for p in built.problems)


def test_an_area_too_narrow_for_the_fleet_is_refused():
    built = _build(PlanSpec.from_payload({"area": {"x_cm": 0, "y_cm": 0, "w_cm": 100, "h_cm": 400}}), IDS * 1 + ["drone-3"])
    assert not built.ok and "too narrow" in built.problem_text()


def test_uncovered_areas_reflect_the_edited_paths():
    full = _build(_spec(strip_cm=100, area={"x_cm": 0, "y_cm": 0, "w_cm": 300, "h_cm": 400}), ["drone-1"])
    short = _build(
        _spec(strip_cm=100, area={"x_cm": 0, "y_cm": 0, "w_cm": 300, "h_cm": 400},
              paths={"drone-1": [[0, 0], [0, 200]]}),
        ["drone-1"],
    )
    assert short.ok
    assert short.coverage < full.coverage
    assert all(entry["kind"] == "area" and entry["w_cm"] > 0 for entry in short.uncovered)
    covered = 1 - sum(e["w_cm"] * e["h_cm"] for e in short.uncovered) / (300 * 400)
    assert covered == pytest.approx(short.coverage, abs=0.01)
    payload = short.payload()
    assert payload["coverage"]["model"] == "nominal (downward camera model)"
    assert "gimbal" in payload["coverage"]["note"] and "intentional" in payload["coverage"]["note"]
    assert payload["uncovered"] == short.uncovered


def test_payload_shape():
    payload = _build(_spec()).payload()
    assert payload["ok"] is True and payload["mode"] == "fleet"
    drone = payload["drones"][0]
    assert set(drone) >= {"drone_id", "zone", "alt_cm", "takeoff_cm", "edited", "generated", "drawn", "path",
                          "lanes", "moves", "max_deviation_cm"}
    assert payload["snap_tolerance_cm"] == pp.SNAP_TOLERANCE_CM
    assert payload["spec"]["pattern"] == "parallel_track"


MARKS = {"drone-1": (30, -50), "drone-2": (250, 0)}


def test_zone_assignments_carry_the_takeoff_point_and_lanes():
    built = _build(_spec(paths={"drone-1": [[30, -50], [30, 300], [100, 300]]}), takeoffs=MARKS)
    assert built.ok, built.problem_text()
    zone = built.zone_assignments()[0]
    assert (zone.takeoff_x_cm, zone.takeoff_y_cm) == (30, -50)
    assert zone.origin == (30, -50)
    assert (zone.x_cm, zone.y_cm) == (0, -50)  # the split zone grown to hold the whole path
    msg = fp.make_assign_lanes(zone.drone_id, zone.alt_cm, [dict(lane) for lane in zone.lanes], zone.zone_dict())
    fp.validate_message(msg)
    assert msg["zone"]["takeoff_x_cm"] == 30


# ---------------------------------------------------------------- coordinator


class _Link:
    def __init__(self):
        self.on_message = None
        self.sent = []

    def send(self, msg):
        fp.validate_message(msg)
        self.sent.append(msg)


def test_plan_zones_sends_the_lanes_and_places_telemetry_at_the_takeoff_point():
    links = {d: _Link() for d in IDS}
    coordinator = FleetCoordinator(links, area_cm=400, strip_cm=75)
    built = _build(_spec(paths={"drone-1": [[30, -50], [30, 300], [100, 300]]}), takeoffs=MARKS)
    plan = coordinator.plan_zones(built.zone_assignments(), grid_cm=(0, 0, 400, 400), gap_cm=100, strip_cm=75,
                                  uncovered_areas=built.uncovered, details=built.payload())
    sent = links["drone-1"].sent[-1]
    assert sent["type"] == "assign_lanes"
    assert sent["lanes"] == [dict(lane) for lane in built.drones[0].converted.lanes]
    links["drone-1"].on_message(_telemetry("drone-1", rel_x_cm=10.0, rel_y_cm=20.0))
    status = coordinator.status()["drone-1"]
    assert (status["rel_x_cm"], status["rel_y_cm"]) == (40.0, -30.0)
    current = coordinator.current_plan()
    assert current["details"]["ok"] is True
    assert current["grid"] == AREA
    assert [e for e in current["uncovered"] if e.get("kind") == "area"] == built.uncovered
    assert current["zones"][0]["takeoff_x_cm"] == 30
    assert plan.zone_for("drone-2").origin == (250, 0)


def test_plan_zones_refuses_a_zone_set_that_is_not_the_fleet():
    coordinator = FleetCoordinator({d: _Link() for d in IDS}, area_cm=400, strip_cm=75)
    zones = _build(_spec(), ["drone-1"]).zone_assignments()
    with pytest.raises(ValueError, match="exactly the fleet"):
        coordinator.plan_zones(zones, grid_cm=(0, 0, 400, 400), gap_cm=100, strip_cm=75)


def test_zone_without_a_takeoff_keeps_the_corner_origin():
    zone = ZoneAssignment("drone-1", 10, 20, 100, 100, 100, ())
    assert zone.origin == (10, 20) and "takeoff_x_cm" not in zone.zone_dict()


# ---------------------------------------------------------------- supervisor


def test_supervisor_plans_an_edited_spec_and_sends_its_lanes():
    supervisor, transport, sio, _clock = _supervisor()
    spec = _spec(paths={"drone-2": [[250, 0], [250, 300], [380, 380], [300, 100]]})
    supervisor.plan(spec=spec)
    built = supervisor.build_map_plan(spec)
    sent = {d: m for d, m in transport.sent if m["type"] == "assign_lanes"}
    assert sent["drone-2"]["lanes"] == [dict(lane) for lane in built.drones[1].converted.lanes]
    assert sent["drone-1"]["lanes"] == [dict(lane) for lane in built.drones[0].converted.lanes]
    plan = sio.named("fleet_plan")[-1]
    assert plan["details"]["drones"][1]["edited"] is True


def test_supervisor_refuses_an_edited_spec_outside_the_geofence_and_sends_nothing():
    supervisor, transport, sio, _clock = _supervisor(geofence_cm=450)
    spec = _spec(paths={"drone-1": [[0, 0], [0, 460]]})
    with pytest.raises(RuntimeError, match="Plan refused"):
        supervisor.plan(spec=spec)
    assert transport.sent == [] and transport.sync_calls == []
    assert supervisor.coordinator.current_plan() is None
    refusal = [e for e in sio.named("fleet_event") if e["event"] == "plan_refused"][-1]
    assert "geofence" in refusal["detail"]
    preview = sio.named("plan_preview")[-1]
    assert preview["problems"][0]["vertex"] == 1


def test_editing_after_plan_invalidates_start_until_plan_again():
    supervisor, transport, sio, _clock = _supervisor()
    supervisor.plan(spec=_spec())
    for drone_id in supervisor.links:
        supervisor.handle_control(fp.make_ack_assign_lanes(drone_id, 90, len(supervisor.coordinator.lanes(drone_id))))
    assert supervisor.mark_plan_edited() is True
    assert [e for e in sio.named("fleet_event") if e["event"] == "plan_invalidated"]
    result = supervisor.start(spec=_spec())
    assert not result.ok and "edited on the map after Plan" in result.refusals["fleet"]
    assert "start_at" not in transport.sent_types("drone-1")
    supervisor.plan(spec=_spec())
    for drone_id in supervisor.links:
        supervisor.handle_control(fp.make_ack_assign_lanes(drone_id, 90, len(supervisor.coordinator.lanes(drone_id))))
    assert supervisor.start(spec=_spec()).ok


def test_mark_plan_edited_never_touches_a_started_mission_or_no_plan():
    supervisor, _transport, _sio, _clock = _supervisor()
    assert supervisor.mark_plan_edited() is False
    supervisor.plan(spec=_spec())
    for drone_id in supervisor.links:
        supervisor.handle_control(fp.make_ack_assign_lanes(drone_id, 90, len(supervisor.coordinator.lanes(drone_id))))
    assert supervisor.start(spec=_spec()).ok
    assert supervisor.mark_plan_edited() is False


def test_a_fleet_of_one_drone_plans_the_whole_area():
    supervisor, transport, _sio, _clock = _supervisor(("drone-1",))
    supervisor.plan(spec=_spec(paths={"drone-1": [[0, 0], [0, 400], [400, 400]]}))
    lanes = [m["lanes"] for d, m in transport.sent if m["type"] == "assign_lanes"]
    assert lanes == [[{"direction": "forward", "distance_cm": 400}, {"direction": "right", "distance_cm": 400}]]


def test_map_defaults_follow_the_supervisor():
    supervisor, _transport, _sio, _clock = _supervisor(geofence_cm=450)
    defaults = supervisor.map_defaults()
    assert defaults == {
        "mode": "fleet", "drone_ids": IDS, "area": AREA, "strip_cm": 75, "gap_cm": 100, "geofence_cm": 450.0,
        "takeoffs": {"drone-1": [0, 0], "drone-2": [250, 0]}, "camera_footprint_cm": 75,
    }


# ---------------------------------------------------------------- re-task separation (review N4)


def test_retask_offers_keep_the_gap_and_report_lanes_no_drone_can_take():
    from tests.test_coordinator import FakeClock as CoordClock
    from tests.test_coordinator import FakeEdge

    clock = CoordClock()
    edges = {f"drone-{i}": FakeEdge(f"drone-{i}", clock, 0.0) for i in (1, 2, 3)}
    events = []
    coordinator = FleetCoordinator(
        edges, area_cm=(800, 400), strip_cm=75, clock=clock,
        on_event=lambda name, payload: events.append((name, payload)),
        # drone-2 is held for manual control: it receives nothing, but it may still fly.
        retask_excluded=lambda drone_id: drone_id == "drone-2",
    )
    coordinator.sync()
    plan = coordinator.plan()
    assert coordinator.start().ok
    edges["drone-1"].telemetry(lane_index=1)
    appended = coordinator.on_drone_lost("drone-1")
    # Only drone-3 could take drone-1's lanes, and it would cross drone-2's zone.
    assert appended == {}
    assert coordinator.lanes("drone-3") == list(plan.zone_for("drone-3").lanes)
    failed = [payload for name, payload in events if name == "fleet_event" and payload.get("event") == "retask_failed"]
    assert failed and "drone-3 would come within" in failed[-1]["detail"]
    assert "below the 100 cm gap" in failed[-1]["detail"]
    current = coordinator.current_plan()
    assert any(entry.get("drone_id") == "drone-1" and "gap" in entry["reason"] for entry in current["uncovered"])
