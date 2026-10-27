"""Tests for the dashboard's fleet wiring (R-095, R-097).

Covers the ``EdgeLink`` adapter over the ground transport, the fleet
supervisor (status, plan, start, loss detection and re-tasking), the
``land_all`` socket event with both of its paths, and an end-to-end run in
which two fake edges connect to a real ``GroundTransportSync`` on 127.0.0.1
and their telemetry and detections reach the browser as ``fleet_status``
and deduplicated ``fleet_detection`` events.

No real drone is involved. The edges are ``EdgeTransportSync`` clients on
loopback or in-process fakes, and every timing assertion has a generous
margin because many test processes may share the machine.
"""

import logging
import threading
import time
import types
from unittest.mock import MagicMock, patch

import pytest

import flightrisk.config as config_module
from flightrisk import fleet_protocol as fp
from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.app import (
    FleetSupervisor,
    SourceConfig,
    TransportEdgeLink,
    _init_pipeline,
    _land_all,
    app,
    socketio,
)
from flightrisk.dashboard.state import app_state
from flightrisk.edge import Detection, DetectionMessage
from flightrisk.transport import EdgeTransportSync
from flightrisk.vision.scorer import MatchScorer

WAIT_S = 15.0


def _wait_for(predicate, timeout=WAIT_S, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return predicate()


class FakeSocketIO:
    """Records ``emit(event, payload)`` calls from any thread."""

    def __init__(self):
        self.events = []
        self._lock = threading.Lock()

    def emit(self, event, payload=None, *args, **kwargs):
        with self._lock:
            self.events.append((event, payload))

    def named(self, name):
        with self._lock:
            return [payload for event, payload in self.events if event == name]


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeGroundTransport:
    """Stands in for ``GroundTransportSync``: records sends, answers clock sync.

    ``connected`` maps drone ids to whether a send reaches them. The edge
    clock runs ``offset_s`` ahead of the ground clock.
    """

    def __init__(self, drone_ids, clock, offset_s=0.25):
        self.clock = clock
        self.offset_s = offset_s
        self.connected = {d: True for d in drone_ids}
        self.link_age_s = {d: 0.1 for d in drone_ids}
        self.sent = []
        self.broadcasts = 0
        self.broadcast_error = None
        self.sync_calls = []

    def now(self):
        return self.clock()

    def send_to_drone(self, drone_id, msg, timeout=None):
        fp.validate_message(msg)
        self.sent.append((drone_id, dict(msg)))
        return self.connected.get(drone_id, False)

    def sync_clock(self, drone_id, rounds=8, round_timeout_s=1.0, timeout=None):
        self.sync_calls.append((drone_id, rounds))
        if not self.connected.get(drone_id, False):
            raise LookupError(f"no connected edge has reported drone_id {drone_id!r}")
        t1 = self.clock()
        t2 = t1 + self.offset_s
        sample = fp.ClockSample(t1=t1, t2=t2, t3=t2, t4=t1)
        return fp.estimate_clock([sample], measured_at_s=t1)

    def emergency_land_all(self, timeout=None):
        if self.broadcast_error is not None:
            raise self.broadcast_error
        self.broadcasts += 1
        return sum(1 for ok in self.connected.values() if ok)

    def fleet_status(self, timeout=None):
        return {
            d: {"connected": ok, "link_age_s": self.link_age_s.get(d)}
            for d, ok in self.connected.items()
        }

    def sent_types(self, drone_id):
        return [msg["type"] for d, msg in self.sent if d == drone_id]


def _supervisor(drone_ids=("drone-1", "drone-2"), **kwargs):
    clock = FakeClock()
    transport = FakeGroundTransport(drone_ids, clock)
    sio = FakeSocketIO()
    supervisor = FleetSupervisor(transport, drone_ids, sio, area_cm=400, strip_cm=75, clock=clock, **kwargs)
    return supervisor, transport, sio, clock


def _telemetry(drone_id, **fields):
    msg = fp.make_telemetry(
        drone_id=drone_id, battery=fields.pop("battery", 90), height_cm=fields.pop("height_cm", 100),
        is_flying=fields.pop("is_flying", False), rel_x_cm=fields.pop("rel_x_cm", 0.0),
        rel_y_cm=fields.pop("rel_y_cm", 0.0), heading_deg=0.0, lane_index=fields.pop("lane_index", None),
        lanes_total=fields.pop("lanes_total", None), link_age_s=0.1, takeoff_at_local_s=None,
    )
    assert not fields
    return msg


def _started_supervisor():
    """A two-drone supervisor that has planned, been acked and started."""
    supervisor, transport, sio, clock = _supervisor()
    supervisor.plan()
    for drone_id in supervisor.links:
        lanes = len(supervisor.coordinator.lanes(drone_id))
        supervisor.handle_control(fp.make_ack_assign_lanes(drone_id, 90, lanes))
    result = supervisor.start()
    assert result.ok, result.refusals
    return supervisor, transport, sio, clock


# ---------------------------------------------------------------------------
# EdgeLink adapter
# ---------------------------------------------------------------------------

def test_link_sends_control_to_its_drone():
    transport = FakeGroundTransport(["drone-1"], FakeClock())
    link = TransportEdgeLink(transport, "drone-1")

    link.send({"type": "land"})

    assert transport.sent == [("drone-1", {"type": "land"})]


def test_link_send_raises_when_the_drone_is_not_connected():
    transport = FakeGroundTransport(["drone-1"], FakeClock())
    transport.connected["drone-1"] = False
    link = TransportEdgeLink(transport, "drone-1")

    with pytest.raises(ConnectionError):
        link.send({"type": "emergency_land_all"})


def test_link_ping_runs_one_real_round_and_answers_with_a_pong():
    transport = FakeGroundTransport(["drone-1"], FakeClock(50.0), offset_s=0.4)
    link = TransportEdgeLink(transport, "drone-1")
    received = []
    link.on_message = received.append

    link.send({"type": "ping", "t1": 49.5})

    assert transport.sync_calls == [("drone-1", 1)]
    assert transport.sent == []  # the ping itself is not forwarded raw
    assert received == [{"type": "pong", "t1": 49.5, "t2": 50.4, "t3": 50.4}]


def test_coordinator_sync_through_links_measures_the_edge_offset():
    supervisor, transport, sio, clock = _supervisor()

    results = supervisor.coordinator.sync()

    for drone_id, result in results.items():
        assert result.synced and result.samples == 8
        assert result.offset_s == pytest.approx(0.25)
        assert transport.sent_types(drone_id) == ["clock"]  # the median, sent once
    assert supervisor.synced is True
    assert supervisor.summary() == {"drones": 2, "net": "station", "synced": True}


# ---------------------------------------------------------------------------
# fleet_status, fleet_plan, start
# ---------------------------------------------------------------------------

def test_status_payload_has_the_fields_the_fleet_panel_reads():
    supervisor, transport, sio, clock = _supervisor()
    supervisor.plan()
    supervisor.handle_control(_telemetry("drone-2", battery=77, height_cm=160, rel_x_cm=10.0,
                                         rel_y_cm=20.0, lane_index=1, lanes_total=3))

    supervisor.emit_status()

    payload = sio.named("fleet_status")[-1]
    assert payload["synced"] is True and payload["net"] == "station"
    drone = payload["drones"]["drone-2"]
    for key in ("battery", "height_cm", "rel_x_cm", "rel_y_cm", "lane_index", "lanes_total",
                "offset_ms", "rtt_ms", "skew_ms"):
        assert key in drone
    assert drone["battery"] == 77 and drone["height_cm"] == 160
    assert drone["lane_index"] == 1 and drone["lanes_total"] == 3
    assert drone["offset_ms"] == pytest.approx(250.0)
    # rel_x_cm/rel_y_cm are on the shared grid: drone-2's zone starts at x=250.
    assert (drone["rel_x_cm"], drone["rel_y_cm"]) == (260.0, 20.0)


def test_plan_emits_fleet_plan_and_assigns_lanes():
    supervisor, transport, sio, clock = _supervisor()

    supervisor.plan()

    plan = sio.named("fleet_plan")[-1]
    assert [z["drone_id"] for z in plan["zones"]] == ["drone-1", "drone-2"]
    assert [z["alt_cm"] for z in plan["zones"]] == [100, 160]
    assert all(z["lanes"] for z in plan["zones"])
    assert plan["grid"] == {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}
    for drone_id in ("drone-1", "drone-2"):
        assert transport.sent_types(drone_id).count("assign_lanes") == 1


def test_start_refused_without_acks_reports_a_fleet_event():
    supervisor, transport, sio, clock = _supervisor()
    supervisor.plan()

    result = supervisor.start()

    assert not result.ok
    event = sio.named("fleet_event")[-1]
    assert event["event"] == "start_refused" and "no assign_lanes ack" in event["detail"]
    assert "start_at" not in transport.sent_types("drone-1")


def test_start_after_acks_sends_one_start_at_each():
    supervisor, transport, sio, clock = _started_supervisor()

    assert sio.named("fleet_event")[-1]["event"] == "start"
    for drone_id in ("drone-1", "drone-2"):
        assert transport.sent_types(drone_id).count("start_at") == 1


def test_control_from_an_unknown_drone_is_ignored(caplog):
    supervisor, transport, sio, clock = _supervisor()
    with caplog.at_level(logging.WARNING):
        supervisor.handle_control(_telemetry("drone-9"))
    assert "drone-9" not in supervisor.coordinator.status()
    assert "not in the fleet" in caplog.text


# ---------------------------------------------------------------------------
# Loss detection and re-tasking
# ---------------------------------------------------------------------------

def test_drone_landed_before_finishing_its_lanes_is_retasked():
    supervisor, transport, sio, clock = _started_supervisor()
    lanes = len(supervisor.coordinator.lanes("drone-2"))
    supervisor.handle_control(_telemetry("drone-2", is_flying=True, lane_index=0, lanes_total=lanes))
    supervisor.handle_control(_telemetry("drone-1", is_flying=True, lane_index=0, lanes_total=lanes))
    assert supervisor.check_losses() == []

    # The local land key on drone-2's laptop lands it mid-mission.
    supervisor.handle_control(_telemetry("drone-2", is_flying=False, lane_index=1, lanes_total=lanes))

    assert supervisor.check_losses() == ["drone-2"]
    events = [e["event"] for e in sio.named("fleet_event")]
    assert "drone_lost" in events and "retask" in events
    assert transport.sent_types("drone-1").count("assign_lanes") == 2
    assert supervisor.coordinator.status()["drone-2"]["lost"] is True
    assert supervisor.check_losses() == []  # declared once only


def test_disconnected_or_silent_drones_are_lost():
    supervisor, transport, sio, clock = _started_supervisor()
    transport.connected["drone-1"] = False
    transport.link_age_s["drone-2"] = 30.0

    assert sorted(supervisor.check_losses()) == ["drone-1", "drone-2"]


def test_drone_that_finished_its_lanes_is_not_lost():
    supervisor, transport, sio, clock = _started_supervisor()
    for drone_id in ("drone-1", "drone-2"):
        lanes = len(supervisor.coordinator.lanes(drone_id))
        supervisor.handle_control(_telemetry(drone_id, is_flying=True, lane_index=0, lanes_total=lanes))
        supervisor.handle_control(_telemetry(drone_id, is_flying=False, lane_index=lanes, lanes_total=lanes))

    assert supervisor.check_losses() == []


def test_drone_that_landed_after_finishing_is_not_lost_when_it_gets_more_lanes():
    supervisor, transport, sio, clock = _started_supervisor()
    for drone_id in ("drone-1", "drone-2"):
        supervisor.handle_control(_telemetry(drone_id, is_flying=True, lane_index=0, lanes_total=3))
    supervisor.handle_control(_telemetry("drone-1", is_flying=False, lane_index=3, lanes_total=3))
    # drone-2's lanes are re-tasked onto drone-1, which is already on the ground.
    supervisor.handle_control(_telemetry("drone-2", is_flying=False, lane_index=1, lanes_total=3))
    assert supervisor.check_losses() == ["drone-2"]
    supervisor.handle_control(_telemetry("drone-1", is_flying=False, lane_index=3, lanes_total=9))

    assert supervisor.check_losses() == []


def test_drone_that_never_takes_off_is_lost_after_the_grace_period():
    supervisor, transport, sio, clock = _started_supervisor()
    lanes = len(supervisor.coordinator.lanes("drone-1"))
    supervisor.handle_control(_telemetry("drone-1", is_flying=True, lane_index=0, lanes_total=lanes))

    clock.now += 5.0
    assert supervisor.check_losses() == []
    clock.now += app_module.FLEET_TAKEOFF_GRACE_S
    assert supervisor.check_losses() == ["drone-2"]


def test_no_loss_detection_before_start_or_after_land_all():
    supervisor, transport, sio, clock = _supervisor()
    supervisor.plan()
    transport.connected["drone-1"] = False
    assert supervisor.check_losses() == []

    supervisor, transport, sio, clock = _started_supervisor()
    supervisor.land_all()
    transport.connected["drone-1"] = False
    assert supervisor.check_losses() == []


# ---------------------------------------------------------------------------
# R-095: the land_all socket event
# ---------------------------------------------------------------------------

@pytest.fixture
def fleet_app_state(clean_app_state, monkeypatch):
    supervisor, transport, sio, clock = _supervisor()
    app_state.fleet_supervisor = supervisor
    app_state.ground_transport = transport
    app_state.fleet = None
    app_state.logger = None
    monkeypatch.setattr(app_module, "socketio", sio)
    return types.SimpleNamespace(supervisor=supervisor, transport=transport, sio=sio)


def _emit_land_all():
    client = socketio.test_client(app)
    client.get_received()
    client.emit("land_all")
    received = client.get_received()
    client.disconnect()
    acks = [r["args"][0] for r in received if r["name"] == "land_all_ack"]
    assert len(acks) == 1
    return acks[0]


def test_land_all_event_calls_coordinator(fleet_app_state):
    f = fleet_app_state
    with patch.object(f.supervisor.coordinator, "land_all", wraps=f.supervisor.coordinator.land_all) as land_all:
        report = _emit_land_all()

    land_all.assert_called_once_with()
    for drone_id in ("drone-1", "drone-2"):
        assert f.transport.sent_types(drone_id) == ["emergency_land_all"]
    assert f.transport.broadcasts == 1  # belt and braces
    assert report == {"coordinator": 2, "transport": 2, "local": None, "reached_any": True}
    assert {"event": "land_all", "detail": "sent to all"} in f.sio.named("fleet_event")


def test_land_all_broadcast_still_runs_when_the_coordinator_path_fails(fleet_app_state, caplog):
    f = fleet_app_state
    with patch.object(f.supervisor.coordinator, "land_all", side_effect=RuntimeError("coordinator broke")), \
            caplog.at_level(logging.ERROR):
        report = _emit_land_all()

    assert f.transport.broadcasts == 1
    assert report["coordinator"] is None and report["transport"] == 2 and report["reached_any"] is True
    assert "coordinator path failed" in caplog.text


def test_land_all_coordinator_path_still_runs_when_the_broadcast_fails(fleet_app_state, caplog):
    f = fleet_app_state
    f.transport.broadcast_error = TimeoutError("transport loop stalled")
    with caplog.at_level(logging.ERROR):
        report = _emit_land_all()

    for drone_id in ("drone-1", "drone-2"):
        assert f.transport.sent_types(drone_id) == ["emergency_land_all"]
    assert report["coordinator"] == 2 and report["transport"] is None and report["reached_any"] is True
    assert "broadcast failed" in caplog.text


def test_land_all_broadcast_runs_even_after_an_interrupt_on_the_coordinator_path(fleet_app_state):
    f = fleet_app_state
    with patch.object(f.supervisor.coordinator, "land_all", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            _land_all()
    assert f.transport.broadcasts == 1


def test_land_all_that_reaches_no_drone_is_loud(fleet_app_state, caplog):
    f = fleet_app_state
    f.transport.connected = {"drone-1": False, "drone-2": False}
    f.transport.broadcast_error = RuntimeError("ground transport is not running")
    with caplog.at_level(logging.CRITICAL):
        report = _land_all()

    assert report == {"coordinator": 0, "transport": None, "local": None, "reached_any": False}
    assert any(r.levelno == logging.CRITICAL and "REACHED NO DRONE" in r.getMessage() for r in caplog.records)
    errors = f.sio.named("error")
    assert errors and "reached no drone" in errors[-1]["message"]


def test_land_all_without_a_fleet_lands_the_local_drones(clean_app_state, monkeypatch):
    sio = FakeSocketIO()
    monkeypatch.setattr(app_module, "socketio", sio)
    fleet = MagicMock()
    fleet.broadcast_command.return_value = {"drone-1": None}
    app_state.fleet_supervisor = None
    app_state.ground_transport = None
    app_state.fleet = fleet
    app_state.logger = None

    report = _emit_land_all()

    fleet.broadcast_command.assert_called_once_with("land")
    assert report == {"coordinator": None, "transport": None, "local": 1, "reached_any": True}


def test_plan_and_start_events_without_a_fleet_report_an_error(clean_app_state):
    app_state.fleet_supervisor = None
    client = socketio.test_client(app)
    client.get_received()
    client.emit("plan_fleet")
    client.emit("start_fleet")
    received = client.get_received()
    client.disconnect()
    assert [r["name"] for r in received] == ["error", "error"]


# ---------------------------------------------------------------------------
# R-097: end to end over a real ground transport with two fake edges
# ---------------------------------------------------------------------------

@pytest.fixture
def edge_pipeline(clean_app_state, monkeypatch):
    """Run ``_init_pipeline`` for the edge source on 127.0.0.1 and clean up after it."""
    monkeypatch.delenv("FLIGHTRISK_EDGE_TOKEN", raising=False)
    # Appearance-only switches the face signal off in the configuration, so
    # the test gets its own configuration singleton.
    monkeypatch.setattr(config_module, "_config", config_module.AmberConfig.from_env())
    sio = FakeSocketIO()
    monkeypatch.setattr(app_module, "socketio", sio)
    for key in ("detector", "reid", "tracker", "logger", "metrics", "db", "obstacle_guard",
                "canon", "signal_registry"):
        setattr(app_state, key, MagicMock())
    app_state.db.create_session.return_value = "session-1"
    app_state.face = None
    app_state.reasoning = None
    app_state.scorer = MatchScorer()
    app_state.running = False
    app_state.ground_transport = None
    app_state.fleet_supervisor = None
    edges = []

    def start(**source_kwargs):
        _init_pipeline(SourceConfig(source="edge", edge_host="127.0.0.1", edge_port=0,
                                    appearance_only=True, enable_llm=False, **source_kwargs))
        return app_state.ground_transport

    def connect_edge(**kwargs):
        edge = EdgeTransportSync(ws_url=f"ws://127.0.0.1:{app_state.ground_transport.port}",
                                 max_retries=0, **kwargs)
        edge.connect(timeout=WAIT_S)
        edges.append(edge)
        return edge

    yield types.SimpleNamespace(sio=sio, start=start, connect_edge=connect_edge)

    if app_state.fleet_supervisor is not None:
        app_state.fleet_supervisor.stop()
    for edge in edges:
        edge.disconnect(timeout=WAIT_S)
    if app_state.ground_transport is not None:
        app_state.ground_transport.stop(timeout=WAIT_S)


def _detections(drone_id, rel_x_cm, rel_y_cm):
    return DetectionMessage(
        timestamp=time.time(), frame_id=1, frame_width=100, frame_height=100,
        detections=[Detection(bbox=(10, 10, 40, 80), confidence=0.9, crop_jpeg=b"\xff\xd8crop")],
        drone_id=drone_id, rel_x_cm=rel_x_cm, rel_y_cm=rel_y_cm,
    )


def test_edge_messages_flow_to_fleet_events(edge_pipeline):
    e = edge_pipeline
    e.start(fleet_drones=("drone-1", "drone-2"))
    supervisor = app_state.fleet_supervisor
    assert supervisor is not None

    landed = {"drone-1": threading.Event(), "drone-2": threading.Event()}
    edges = {}
    for drone_id in ("drone-1", "drone-2"):
        edges[drone_id] = e.connect_edge(on_emergency_land=lambda msg, d=drone_id: landed[d].set())
        # Telemetry identifies the edge's socket to the ground transport.
        assert edges[drone_id].send(_telemetry(drone_id, battery=80 if drone_id == "drone-1" else 70))

    # Heartbeats keep the edges' link-loss watchdogs quiet.
    for edge in edges.values():
        assert _wait_for(lambda edge=edge: _recv_type(edge, "heartbeat"))

    # Both drones show up in fleet_status with their telemetry.
    def both_reported():
        for payload in reversed(e.sio.named("fleet_status")):
            drones = payload["drones"]
            if drones["drone-1"]["battery"] == 80 and drones["drone-2"]["battery"] == 70:
                return payload
        return None
    assert _wait_for(both_reported)

    # Plan: real clock sync through the adapter, then zones and lanes.
    supervisor.plan()
    assert supervisor.synced is True
    status = supervisor.status_payload()["drones"]
    for drone_id in ("drone-1", "drone-2"):
        assert status[drone_id]["offset_ms"] is not None
        assert 0.0 <= status[drone_id]["rtt_ms"] < 5000.0
        assert _wait_for(lambda edge=edges[drone_id]: _recv_type(edge, "assign_lanes"))
    plan = e.sio.named("fleet_plan")[-1]
    assert [z["x_cm"] for z in plan["zones"]] == [0, 250]
    data = app.test_client().get("/api/status").get_json()
    assert data["fleet"] == {"drones": 2, "net": "station", "synced": True}
    assert data["appearance_only"] is True

    # drone-1 sees a person at grid (60, 100); drone-2, whose zone starts at
    # x=250, sees the same spot at its relative (-190, 100): one detection.
    edges["drone-1"].send_detections(_detections("drone-1", 60.0, 100.0))
    assert _wait_for(lambda: e.sio.named("fleet_detection"))
    edges["drone-2"].send_detections(_detections("drone-2", -190.0, 100.0))
    assert _wait_for(lambda: supervisor.coordinator.detections()[0].count == 2)
    edges["drone-2"].send_detections(_detections("drone-2", 50.0, 300.0))
    assert _wait_for(lambda: len(e.sio.named("fleet_detection")) == 2)

    first, second = e.sio.named("fleet_detection")
    assert first["drone_id"] == "drone-1" and (first["rel_x_cm"], first["rel_y_cm"]) == (60.0, 100.0)
    assert first["thumbnail"]
    assert second["drone_id"] == "drone-2" and (second["rel_x_cm"], second["rel_y_cm"]) == (300.0, 300.0)
    assert supervisor.coordinator.detections()[0].drone_ids == ("drone-1", "drone-2")

    # LAND ALL reaches both edges over the real transport.
    report = _land_all()
    assert report["coordinator"] == 2 and report["transport"] == 2
    for drone_id in ("drone-1", "drone-2"):
        assert landed[drone_id].wait(WAIT_S), f"{drone_id} never received emergency_land_all"


def _recv_type(edge, wanted):
    msg = edge.recv(recv_timeout=0.2)
    while msg is not None:
        if msg.get("type") == wanted:
            return True
        msg = edge.recv(recv_timeout=0.05)
    return False


def test_edge_source_without_a_fleet_builds_no_coordinator(edge_pipeline):
    transport = edge_pipeline.start()

    assert transport is not None and transport.port > 0
    assert app_state.fleet_supervisor is None
    data = app.test_client().get("/api/status").get_json()
    assert data["fleet"] == {"drones": 0, "net": "station", "synced": False}
