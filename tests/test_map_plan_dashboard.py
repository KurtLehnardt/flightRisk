"""Map-edited plans through the real socket path, with fake edges on loopback.

The dashboard's Socket.IO handlers are driven with Flask-SocketIO's test
client. The fleet tests start the real ``GroundTransportSync`` on 127.0.0.1
(``edge_pipeline`` from ``tests/test_fleet_dashboard.py``) and connect two
``EdgeTransportSync`` clients as fake edges, which record what reaches them.
The single-drone tests use a fake directly connected drone. No real drone,
edge or network beyond loopback is involved.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from flightrisk.dashboard.app import app, socketio
from flightrisk.dashboard.state import app_state
from flightrisk.drone import plan_paths as pp
from tests.test_fleet_dashboard import (  # noqa: F401
    WAIT_S,
    _telemetry,
    _wait_for,
    edge_pipeline,
)

AREA = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}


def _spec(**fields):
    spec = {"pattern": "parallel_track", "area": dict(AREA)}
    spec.update(fields)
    return spec


def _received(client, name):
    return [r["args"][0] for r in client.get_received() if r["name"] == name]


def _drain(edge, wanted, timeout=WAIT_S):
    """Every message of type *wanted* the fake edge receives within *timeout*."""
    found = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = edge.recv(recv_timeout=0.1)
        if msg is not None and msg.get("type") == wanted:
            found.append(msg)
            return found
    return found


@pytest.fixture
def fleet(edge_pipeline, monkeypatch):  # noqa: F811 - the imported fixture
    """Two fake edges connected to a real ground transport, and a Socket.IO test client."""
    monkeypatch.setenv("FLIGHTRISK_GEOFENCE_CM", "450")
    edge_pipeline.start(fleet_drones=("drone-1", "drone-2"))
    edges = {}
    for drone_id in ("drone-1", "drone-2"):
        edges[drone_id] = edge_pipeline.connect_edge()
        assert edges[drone_id].send(_telemetry(drone_id))
    supervisor = app_state.fleet_supervisor
    assert _wait_for(lambda: all(
        (supervisor._transport.fleet_status().get(d) or {}).get("connected") for d in edges))
    client = socketio.test_client(app)
    client.get_received()
    yield edge_pipeline.sio, edges, client, supervisor
    client.disconnect()


def test_map_config_and_preview_over_the_socket(fleet):
    _sio, _edges, client, _supervisor = fleet
    client.emit("get_map_config")
    config = _received(client, "map_config")[-1]
    assert config["mode"] == "fleet" and config["drone_ids"] == ["drone-1", "drone-2"]
    assert config["geofence_cm"] == 450.0 and config["patterns"] == list(pp.PATTERNS)
    assert {p["id"] for p in config["providers"]} == {"usgs-imagery", "usgs-topo", "osm"}
    client.emit("preview_plan", {"plan": _spec(pattern="expanding_square")})
    preview = _received(client, "plan_preview")[-1]
    assert preview["ok"] is True and len(preview["drones"]) == 2


def test_an_edited_plan_is_accepted_and_its_lanes_reach_the_edges(fleet):
    sio, edges, client, _supervisor = fleet
    path = [[250, 0], [250, 300], [380, 380], [300, 100]]
    client.emit("plan_fleet", {"plan": _spec(paths={"drone-2": path})})
    got = {d: _drain(edge, "assign_lanes") for d, edge in edges.items()}
    assert got["drone-1"] and got["drone-2"]
    expected = pp.path_to_lanes(path, fence_cm=450)
    assert got["drone-2"][0]["lanes"] == [dict(lane) for lane in expected.lanes]
    assert got["drone-2"][0]["zone"]["takeoff_x_cm"] == 250
    assert _wait_for(lambda: sio.named("fleet_plan"))
    plan = sio.named("fleet_plan")[-1]
    assert plan["details"]["drones"][1]["edited"] is True
    assert all(entry.get("kind") == "area" for entry in plan["uncovered"])


def test_an_edited_plan_that_leaves_the_geofence_is_refused_and_nothing_is_sent(fleet):
    sio, edges, client, supervisor = fleet
    client.emit("plan_fleet", {"plan": _spec(paths={"drone-1": [[0, 0], [0, 300], [100, 470]]})})
    assert _wait_for(lambda: [e for e in sio.named("fleet_event") if e.get("event") == "plan_refused"])
    refusal = [e for e in sio.named("fleet_event") if e.get("event") == "plan_refused"][-1]
    assert "drone-1" in refusal["detail"] and "geofence" in refusal["detail"]
    preview = sio.named("plan_preview")[-1]
    assert preview["problems"][0]["vertex"] == 2
    for edge in edges.values():
        assert _drain(edge, "assign_lanes", timeout=1.0) == []
    assert supervisor.coordinator.current_plan() is None


def test_crossing_paths_are_refused(fleet):
    sio, edges, client, _supervisor = fleet
    client.emit("plan_fleet", {"plan": _spec(paths={"drone-1": [[0, 0], [0, 200], [300, 200]]})})
    assert _wait_for(lambda: [e for e in sio.named("fleet_event") if e.get("event") == "plan_refused"])
    detail = [e for e in sio.named("fleet_event") if e.get("event") == "plan_refused"][-1]["detail"]
    assert "drone-1 and drone-2" in detail and "gap" in detail
    for edge in edges.values():
        assert _drain(edge, "assign_lanes", timeout=1.0) == []


def test_editing_after_plan_invalidates_the_plan_until_plan_again(fleet):
    sio, edges, client, _supervisor = fleet
    client.emit("plan_fleet", {"plan": _spec()})
    for edge in edges.values():
        assert _drain(edge, "assign_lanes")
    assert _wait_for(lambda: sio.named("fleet_plan"))
    client.emit("fleet_plan_edited")
    assert [e for e in sio.named("fleet_event") if e.get("event") == "plan_invalidated"]
    client.emit("start_fleet")
    assert _wait_for(lambda: [e for e in sio.named("fleet_event") if e.get("event") == "start_refused"])
    detail = [e for e in sio.named("fleet_event") if e.get("event") == "start_refused"][-1]["detail"]
    assert "edited on the map after Plan" in detail
    for edge in edges.values():
        assert _drain(edge, "start_at", timeout=1.0) == []


def test_a_malformed_plan_is_refused_over_the_socket(fleet):
    _sio, edges, client, _supervisor = fleet
    client.emit("plan_fleet", {"plan": {"pattern": "parallel_track", "area": {"w_cm": "big"}}})
    received = client.get_received()
    assert any(r["name"] == "error" and "Plan not valid" in r["args"][0]["message"] for r in received)
    for edge in edges.values():
        assert _drain(edge, "assign_lanes", timeout=1.0) == []


def test_a_ground_with_one_drone_plans_the_whole_area(edge_pipeline, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FLIGHTRISK_GEOFENCE_CM", "450")
    edge_pipeline.start(fleet_drones=("drone-1",))
    edge = edge_pipeline.connect_edge()
    assert edge.send(_telemetry("drone-1"))
    supervisor = app_state.fleet_supervisor
    assert _wait_for(lambda: (supervisor._transport.fleet_status().get("drone-1") or {}).get("connected"))
    client = socketio.test_client(app)
    try:
        client.emit("plan_fleet", {"plan": _spec(pattern="track_line")})
        got = _drain(edge, "assign_lanes")
        assert got and got[0]["zone"]["w_cm"] >= 400
    finally:
        client.disconnect()


# ---------------------------------------------------------------- single-drone flow


@pytest.fixture
def flying_drone(clean_app_state, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_GEOFENCE_CM", "450")
    drone = MagicMock()
    drone.state.is_flying = True
    drone.get_frame.return_value = None
    drone.move.return_value = True
    drone.rotate.return_value = True
    app_state.fleet = MagicMock(primary=drone)
    app_state.fleet_supervisor = None
    app_state.obstacle_guard = None
    monkeypatch.setattr("flightrisk.dashboard.app.time.sleep", lambda s: None)
    client = socketio.test_client(app)
    client.get_received()
    yield drone, client
    app_state.search_active = False
    client.disconnect()


def test_single_drone_map_config_and_edited_pattern_flies_the_converted_lanes(flying_drone):
    drone, client = flying_drone
    client.emit("get_map_config")
    config = _received(client, "map_config")[-1]
    assert config["mode"] == "single" and config["drone_ids"] == ["primary"]
    path = [[0, 0], [0, 300], [150, 380], [200, 100]]
    client.emit("start_search", {"pattern": "parallel_track", "plan": _spec(paths={"primary": path})})
    expected = pp.path_to_lanes(path, fence_cm=450)
    assert _wait_for(lambda: drone.move.call_count == len(expected.lanes))
    flown = [c.args for c in drone.move.call_args_list]
    assert flown == [(lane["direction"], lane["distance_cm"]) for lane in expected.lanes]
    drone.rotate.assert_not_called()


def test_single_drone_edited_pattern_outside_the_geofence_is_refused(flying_drone):
    drone, client = flying_drone
    client.emit("start_search", {"pattern": "parallel_track",
                                 "plan": _spec(paths={"primary": [[0, 0], [0, 600]]})})
    received = client.get_received()
    assert any(r["name"] == "error" and "geofence" in r["args"][0]["message"] for r in received)
    preview = [r["args"][0] for r in received if r["name"] == "plan_preview"][-1]
    assert preview["mode"] == "single" and preview["problems"][0]["vertex"] == 1
    time.sleep(0.2)
    drone.move.assert_not_called()


def test_single_drone_legacy_pattern_still_works(flying_drone):
    drone, client = flying_drone
    client.emit("start_search", {"pattern": "parallel_track"})
    assert _wait_for(lambda: drone.move.call_count > 0)


# ---------------------------------------------------------------- saved plans and cache status


def test_saved_plan_round_trip_over_the_socket(clean_app_state, tmp_path, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_PLANS_DIR", str(tmp_path / "plans"))
    client = socketio.test_client(app)
    client.get_received()
    try:
        anchor = {"lat": 40.6894, "lon": -111.731, "heading_deg": 12.0}
        client.emit("save_plan", {"name": "field-a", "plan": _spec(paths={"primary": [[0, 0], [0, 100]]}),
                                  "anchor": anchor, "view": {"mode": "latlon", "zoom": 17}})
        received = client.get_received()
        assert [r["args"][0] for r in received if r["name"] == "plan_saved"] == [{"ok": True, "name": "field-a"}]
        client.emit("list_plans")
        assert [p["name"] for p in _received(client, "plan_list")[-1]["plans"]] == ["field-a"]
        client.emit("load_plan", {"name": "field-a"})
        loaded = _received(client, "plan_loaded")[-1]
        assert loaded["ok"] is True and loaded["anchor"] == anchor
        assert loaded["plan"]["paths"] == {"primary": [[0, 0], [0, 100]]}
        client.emit("load_plan", {"name": "../etc/passwd"})
        assert _received(client, "plan_loaded")[-1]["ok"] is False
        client.emit("save_plan", {"name": "x", "plan": {"pattern": "nope"}})
        assert _received(client, "plan_saved")[-1]["ok"] is False
    finally:
        client.disconnect()
    assert sorted(p.name for p in (tmp_path / "plans").iterdir()) == ["field-a.json"]


def test_map_cache_status_over_the_socket(clean_app_state, tmp_path, monkeypatch):
    from flightrisk.dashboard import map_tiles

    monkeypatch.setenv("FLIGHTRISK_TILES_DIR", str(tmp_path / "tiles"))
    bbox = [40.689, -111.732, 40.690, -111.730]
    x, y = map_tiles.latlon_to_tile(40.6895, -111.731, 16)
    tile = tmp_path / "tiles" / "usgs-imagery" / "16" / str(x) / f"{y}.jpg"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(b"\xff\xd8\xff")
    client = socketio.test_client(app)
    client.get_received()
    try:
        client.emit("map_cache_status", {"provider": "usgs-imagery", "bbox": bbox, "zoom": 16})
        status = _received(client, "map_cache_status")[-1]
        assert status["cacheable"] is True and status["present"] >= 1 and status["total"] >= status["present"]
        client.emit("map_cache_status", {"provider": "osm", "bbox": bbox, "zoom": 16})
        assert _received(client, "map_cache_status")[-1]["cacheable"] is False
        for bad in ({"provider": "../x", "bbox": bbox, "zoom": 3}, {"provider": "usgs-imagery", "bbox": [1, 2], "zoom": 3},
                    {"provider": "usgs-imagery", "bbox": bbox, "zoom": 99}, None):
            client.emit("map_cache_status", bad)
            assert _received(client, "map_cache_status")[-1].get("error") == "invalid request"
    finally:
        client.disconnect()
    # The tile is served by the dashboard's own app, with the cache headers.
    resp = app.test_client().get(f"/tiles/usgs-imagery/16/{x}/{y}")
    assert resp.status_code == 200 and resp.headers["Cache-Control"] == "public, max-age=86400"
    assert resp.mimetype == "image/jpeg"
    resp.close()
    assert app.test_client().get("/tiles/usgs-imagery/16/0/..%2f..%2fetc").status_code == 404


def test_gps_drones_report_their_position_to_the_map_and_tellos_do_not():
    from flightrisk.drone.controller import DroneState
    from flightrisk.drone.snapshot import gps_payload, legacy_telemetry_payload, snapshot_from_state

    gps = snapshot_from_state(DroneState(is_flying=True, latitude=40.6894, longitude=-111.731, heading=42), drone_id="m1")
    assert gps_payload(gps) == {"lat": 40.6894, "lon": -111.731, "heading_deg": 42.0}
    # The legacy telemetry shape is unchanged.
    assert not {"latitude", "longitude", "heading", "lat", "lon"} & set(legacy_telemetry_payload(gps))
    assert gps_payload(snapshot_from_state(DroneState(is_flying=True), drone_id="t1")) is None
