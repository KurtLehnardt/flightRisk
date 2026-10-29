"""Review fixes of PR #142 (map plan editing): B1, B2, N1, N2 and N3.

B1: Start flies only the plan the map shows. B2: every map plan keeps the
drones on their configured takeoff marks, and its separation check uses
those marks. N1: no plan can be loaded while drones fly. N2: a map plan in
the single-drone flow stops and hovers at an obstacle instead of
side-stepping, skipping or turning. N3: coverage is reported with a
configurable nominal footprint and never blocks anything.

The socket tests drive the real handlers with Flask-SocketIO test clients
and the real ground transport with fake edges on loopback; nothing reaches a
real edge, drone or the internet.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.dashboard.app import app, socketio
from flightrisk.dashboard.state import app_state
from flightrisk.drone import plan_paths as pp
from flightrisk.drone.plan_edit import PlanSpec, build_plan, camera_footprint_cm
from tests.test_fleet_dashboard import (  # noqa: F401
    WAIT_S,
    _supervisor,
    _telemetry,
    _wait_for,
    edge_pipeline,
)

AREA = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}


def _spec(**fields):
    data = {"pattern": "parallel_track", "area": dict(AREA), "strip_cm": 75}
    data.update(fields)
    return data


def _ack_all(supervisor):
    for drone_id in supervisor.links:
        supervisor.handle_control(fp.make_ack_assign_lanes(drone_id, 90, len(supervisor.coordinator.lanes(drone_id))))


# ---------------------------------------------------------------- B1: Start flies only the plan shown


def test_an_edit_reported_while_plan_runs_leaves_the_plan_stale():
    supervisor, _transport, _sio, _clock = _supervisor()
    real_sync = supervisor.coordinator.sync

    def sync_with_an_edit(*args, **kwargs):
        # The operator edits the map while Plan waits for the clocks.
        supervisor.mark_plan_edited()
        return real_sync(*args, **kwargs)

    supervisor.coordinator.sync = sync_with_an_edit
    spec = PlanSpec.from_payload(_spec())
    supervisor.plan(spec=spec)
    _ack_all(supervisor)
    supervisor.coordinator.sync = real_sync
    result = supervisor.start(spec=spec)
    assert not result.ok and "edited on the map after Plan" in result.refusals["fleet"]


def test_start_refuses_a_plan_other_than_the_one_sent():
    supervisor, transport, _sio, _clock = _supervisor()
    sent = PlanSpec.from_payload(_spec())
    supervisor.plan(spec=sent)
    _ack_all(supervisor)
    shown = PlanSpec.from_payload(_spec(area=dict(AREA, w_cm=600)))
    for spec in (shown, None):
        result = supervisor.start(spec=spec)
        assert not result.ok and "not the plan that was sent" in result.refusals["fleet"]
    assert "start_at" not in transport.sent_types("drone-1")
    assert supervisor.start(spec=PlanSpec.from_payload(_spec())).ok


def test_a_plan_made_without_the_map_still_starts_without_a_spec():
    supervisor, _transport, _sio, _clock = _supervisor()
    supervisor.plan()
    _ack_all(supervisor)
    assert not supervisor.start(spec=PlanSpec.from_payload(_spec())).ok
    assert supervisor.start().ok


@pytest.fixture
def two_windows(edge_pipeline, monkeypatch):  # noqa: F811 - the imported fixture
    """Two browser windows (test clients) on a fleet of two fake edges on loopback."""
    monkeypatch.setenv("FLIGHTRISK_GEOFENCE_CM", "450")
    edge_pipeline.start(fleet_drones=("drone-1", "drone-2"))
    edges = {d: edge_pipeline.connect_edge() for d in ("drone-1", "drone-2")}
    for drone_id, edge in edges.items():
        assert edge.send(_telemetry(drone_id))
    supervisor = app_state.fleet_supervisor
    assert _wait_for(lambda: all((supervisor._transport.fleet_status().get(d) or {}).get("connected") for d in edges))
    a, b = socketio.test_client(app), socketio.test_client(app)
    yield edge_pipeline.sio, edges, a, b, supervisor
    a.disconnect()
    b.disconnect()


def _events(sio, kind):
    return [e for e in sio.named("fleet_event") if e.get("event") == kind]


def _ack_edges(supervisor):
    _ack_all(supervisor)


def test_two_windows_start_only_the_plan_both_were_sent(two_windows):
    sio, edges, a, b, supervisor = two_windows
    s1, s600 = _spec(), _spec(area=dict(AREA, w_cm=600))
    # Window A plans S1.
    a.emit("plan_fleet", {"plan": s1})
    assert _wait_for(lambda: sio.named("fleet_plan"))
    _ack_edges(supervisor)
    # Window B edits (width 600) and reports it; its Start with its own map is refused.
    b.emit("fleet_plan_edited")
    b.emit("start_fleet", {"plan": s600})
    assert _wait_for(lambda: _events(sio, "start_refused"))
    # Window A plans the unchanged S1 again, which clears the edit flag.
    count = len(sio.named("fleet_plan"))
    a.emit("plan_fleet", {"plan": s1})
    assert _wait_for(lambda: len(sio.named("fleet_plan")) > count)
    _ack_edges(supervisor)
    # Window B still shows width 600: Start from it is refused, the drones hold S1.
    refused = len(_events(sio, "start_refused"))
    b.emit("start_fleet", {"plan": s600})
    assert _wait_for(lambda: len(_events(sio, "start_refused")) > refused)
    assert "not the plan that was sent" in _events(sio, "start_refused")[-1]["detail"]
    for edge in edges.values():
        msg = edge.recv(recv_timeout=0.5)
        while msg is not None:
            assert msg.get("type") != "start_at"
            msg = edge.recv(recv_timeout=0.1)


# ---------------------------------------------------------------- B2: drones stay on their marks


MARKS = {"drone-1": (0, 0), "drone-2": (250, 0)}  # the configured layout of a 400 cm area


def _build(spec, **kwargs):
    return build_plan(PlanSpec.from_payload(spec), ["drone-1", "drone-2"], geofence_cm=450, takeoffs=MARKS, **kwargs)


def _separations_hold(built):
    a, b = built.drones
    if a.converted is None or b.converted is None:
        return True
    distance, _, _ = pp.closest_approach(a.converted.trace, b.converted.trace)
    reported = any(p.kind == "separation" for p in built.problems)
    return reported or distance >= 100


def test_widening_the_area_keeps_each_drone_on_its_mark():
    for pattern in pp.PATTERNS:
        built = _build(_spec(pattern=pattern, area=dict(AREA, w_cm=600)))
        assert [tuple(d.drawn[0]) for d in built.drones] == [(0, 0), (250, 0)], pattern
        # The separation check uses the marks the drones really stand on.
        assert _separations_hold(built), pattern
        if built.drones[1].converted is not None:
            assert built.drones[1].converted.trace[0] == (250, 0)


def test_the_review_case_is_checked_from_the_physical_marks():
    # Expanding square, width 400 to 600: drone-2's zone starts at x=350, but it stands at its 250 cm mark.
    built = _build(_spec(pattern="expanding_square", area=dict(AREA, w_cm=600)))
    two = built.drones[1]
    assert two.drawn[0] == (250, 0) and two.zone.x == 350
    assert _separations_hold(built)


def test_a_moved_start_marker_is_refused():
    built = _build(_spec(paths={"drone-2": [[350, 0], [350, 400], [300, 400]]}))
    problem = next(p for p in built.problems if p.kind == "takeoff")
    assert problem.drone_id == "drone-2" and problem.vertex == 0
    assert "must start at its takeoff mark (250, 0)" in problem.text
    assert not built.ok


def test_moving_the_area_never_moves_a_mark():
    ahead = _build(_spec(area={"x_cm": 0, "y_cm": 50, "w_cm": 400, "h_cm": 350}))
    assert [tuple(d.drawn[0]) for d in ahead.drones] == [(0, 0), (250, 0)]
    assert ahead.ok, ahead.problem_text()
    # Moved sideways, drone-1's lanes reach to 75 cm of drone-2's mark: refused, from the real marks.
    sideways = _build(_spec(area={"x_cm": 100, "y_cm": 50, "w_cm": 400, "h_cm": 350}))
    assert [tuple(d.drawn[0]) for d in sideways.drones] == [(0, 0), (250, 0)]
    assert any(p.kind == "separation" and "75 cm" in p.text for p in sideways.problems)


def test_the_plan_lists_where_each_drone_stands():
    payload = _build(_spec()).payload()
    assert payload["takeoffs"] == {"drone-1": [0, 0], "drone-2": [250, 0]}
    assert payload["takeoff_layout"] == ["drone-1 on its mark", "drone-2 250 cm right of drone-1, same heading"]


def test_the_supervisor_uses_the_configured_layout():
    supervisor, _transport, _sio, _clock = _supervisor()
    assert supervisor.layout_takeoffs() == {"drone-1": (0, 0), "drone-2": (250, 0)}
    built = supervisor.build_map_plan(PlanSpec.from_payload(_spec(area=dict(AREA, w_cm=600))))
    assert built.takeoffs == {"drone-1": (0, 0), "drone-2": (250, 0)}


# ---------------------------------------------------------------- N1: no plan replaced while flying


def test_no_plan_is_loaded_while_drones_fly(clean_app_state, tmp_path, monkeypatch):
    from flightrisk.dashboard import plan_store

    monkeypatch.setenv("FLIGHTRISK_PLANS_DIR", str(tmp_path))
    plan_store.save_plan("field", _spec())
    supervisor = MagicMock()
    supervisor.flight_activity.return_value = "drone-1 reported flying"
    client = socketio.test_client(app)
    app_state.fleet_supervisor = supervisor
    try:
        client.get_received()
        client.emit("load_plan", {"name": "field"})
        loaded = [r["args"][0] for r in client.get_received() if r["name"] == "plan_loaded"][-1]
        assert loaded["ok"] is False and "drone-1 reported flying" in loaded["message"]
        supervisor.flight_activity.return_value = None
        client.emit("load_plan", {"name": "field"})
        assert [r["args"][0] for r in client.get_received() if r["name"] == "plan_loaded"][-1]["ok"] is True
        app_state.fleet_supervisor = None
        app_state.search_active = True
        client.emit("load_plan", {"name": "field"})
        loaded = [r["args"][0] for r in client.get_received() if r["name"] == "plan_loaded"][-1]
        assert loaded["ok"] is False and "search pattern is running" in loaded["message"]
    finally:
        app_state.search_active = False
        client.disconnect()


# ---------------------------------------------------------------- N2: obstacles stop a map plan


def test_a_map_plan_stops_and_hovers_at_an_obstacle(clean_app_state, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_GEOFENCE_CM", "450")
    drone = MagicMock()
    drone.state.is_flying = True
    drone.get_frame.return_value = np.zeros((4, 4, 3), dtype=np.uint8)
    app_state.fleet = MagicMock(primary=drone)
    app_state.fleet_supervisor = None
    guard = MagicMock()
    guard.check_path.return_value = {"safe": False, "action": "reverse", "center_depth": 0.1}
    app_state.obstacle_guard = guard
    monkeypatch.setattr("flightrisk.dashboard.app.time.sleep", lambda s: None)
    warnings = []
    monkeypatch.setattr(socketio, "emit", lambda name, payload=None, *a, **k: warnings.append((name, payload)))
    client = socketio.test_client(app)
    try:
        client.emit("start_search", {"pattern": "parallel_track", "plan": _spec(paths={"primary": [[0, 0], [0, 300]]})})
        assert _wait_for(lambda: drone.hover.called)
        assert _wait_for(lambda: any(n == "search_complete" for n, _ in warnings))
        drone.move.assert_not_called()
        drone.rotate.assert_not_called()
        texts = [p["message"] for n, p in warnings if n == "warning"]
        assert any("map plan stopped and the drone hovers" in t for t in texts)
    finally:
        app_state.search_active = False
        client.disconnect()


def test_the_legacy_pattern_still_avoids_obstacles(clean_app_state, monkeypatch):
    drone = MagicMock()
    drone.state.is_flying = True
    drone.get_frame.return_value = np.zeros((4, 4, 3), dtype=np.uint8)
    drone.move.return_value = True
    app_state.fleet = MagicMock(primary=drone)
    app_state.fleet_supervisor = None
    guard = MagicMock()
    guard.check_path.side_effect = [{"safe": False, "action": "go_left", "center_depth": 0.1}] + [
        {"safe": True, "action": "none", "center_depth": 2.0}] * 500
    app_state.obstacle_guard = guard
    monkeypatch.setattr("flightrisk.dashboard.app.time.sleep", lambda s: None)
    client = socketio.test_client(app)
    try:
        client.emit("start_search", {"pattern": "parallel_track"})
        assert _wait_for(lambda: drone.move.call_count > 1)
        assert drone.move.call_args_list[0].args == ("left", 30)
    finally:
        app_state.search_active = False
        time.sleep(0.1)
        client.disconnect()


# ---------------------------------------------------------------- N3: nominal, configurable coverage


def test_the_camera_footprint_is_configurable_and_never_blocks(monkeypatch):
    narrow = _build(_spec(), footprint_cm=40)
    wide = _build(_spec(), footprint_cm=300)
    assert narrow.ok and wide.ok  # coverage never refuses
    assert narrow.coverage < wide.coverage
    assert wide.payload()["coverage"]["footprint_cm"] == 300
    assert "300 cm square" in wide.payload()["coverage"]["note"]
    monkeypatch.setenv("FLIGHTRISK_CAMERA_FOOTPRINT_CM", "250")
    assert camera_footprint_cm() == 250
    for bad in ("0", "abc", "12.5", "99999", "nan"):
        monkeypatch.setenv("FLIGHTRISK_CAMERA_FOOTPRINT_CM", bad)
        with pytest.raises(ValueError):
            camera_footprint_cm()
    monkeypatch.delenv("FLIGHTRISK_CAMERA_FOOTPRINT_CM")
    assert camera_footprint_cm() is None


def test_uncovered_areas_are_worded_neutrally():
    built = _build(_spec(), footprint_cm=40)
    assert built.uncovered and all("may not be covered by the nominal" in e["reason"] for e in built.uncovered)
    assert "intentional" in built.payload()["coverage"]["note"]
