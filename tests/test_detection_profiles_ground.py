"""Detection profiles on the ground: the profile board, the dashboard handlers, scoring and metrics."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk import obs_events
from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.app import app, socketio
from flightrisk.dashboard.detection_profiles import EVENT, MAX_DRONES, ProfileBoard
from flightrisk.dashboard.state import app_state
from flightrisk.edge import Detection, DetectionMessage
from flightrisk.ground import GroundStation
from flightrisk.vision.scorer import MatchScorer


class FakeTransport:
    """``send_command`` that records the command and returns a scripted reply."""

    def __init__(self, reply=None):
        self.sent: list[tuple[str, dict]] = []
        self.reply = reply

    def send_command(self, drone_id, message, *, timeout_s, timeout):
        self.sent.append((drone_id, message))
        if callable(self.reply):
            return self.reply(drone_id, message)
        return self.reply


def inline(fn, *args):
    fn(*args)


def make_board(reply=None, **kwargs):
    transport = FakeTransport(reply)
    emitted: list[tuple[str, dict]] = []
    board = ProfileBoard(transport, lambda name, payload: emitted.append((name, payload)), spawn=inline, **kwargs)
    return board, transport, emitted


def detections(drone_id, profile=None):
    return DetectionMessage(timestamp=1.0, frame_id=1, drone_id=drone_id, detection_profile=profile)


def ack_for(profile, **fields):
    def reply(drone_id, message):
        return fp.make_profile_ack(drone_id, message["cmd_id"], profile, requested=message["profile"], **fields)

    return reply


def nack_for(reason, active="default", detail="refused"):
    def reply(drone_id, message):
        return fp.make_profile_nack(drone_id, message["cmd_id"], reason, profile=active,
                                    requested=message["profile"], detail=detail)

    return reply


# ---------------------------------------------------------------------------
# ProfileBoard
# ---------------------------------------------------------------------------


def test_an_edge_that_reports_a_profile_is_listed_as_supported():
    board, transport, emitted = make_board()
    board.observe_detections(detections("drone-1", "default"))
    drone = board.payload()["drones"]["drone-1"]
    assert drone["supported"] and drone["active"] == "default"
    assert emitted and emitted[-1][0] == EVENT
    count = len(emitted)
    board.observe_detections(detections("drone-1", "default"))  # every frame: no repeat event
    assert len(emitted) == count
    assert transport.sent == []


def test_an_older_edge_is_listed_but_never_sent_a_profile_command():
    board, transport, _ = make_board(reply=ack_for("level"))
    board.observe_detections(detections("drone-old"))
    assert board.payload()["drones"]["drone-old"]["supported"] is False
    error = board.select("drone-old", "level")
    assert error and "does not report detection profiles" in error
    assert board.set_fleet_default("level") == (None, [])
    assert transport.sent == []


def test_selecting_a_profile_sends_the_command_and_shows_the_ack():
    board, transport, _ = make_board(reply=ack_for("top_down"))
    board.observe_detections(detections("drone-1", "default"))
    assert board.select("drone-1", "top_down") is None
    (drone_id, msg), = transport.sent
    assert drone_id == "drone-1" and msg["type"] == fp.SET_DETECTION_PROFILE and msg["profile"] == "top_down"
    fp.validate_ground_to_edge(msg, proto=1)
    drone = board.payload()["drones"]["drone-1"]
    assert drone["active"] == "top_down" and drone["override"] == "top_down"
    assert not drone["pending"] and drone["refusal"] is None and not drone["fallback"]


def test_a_fallback_ack_is_shown_as_such():
    board, _, _ = make_board(reply=ack_for("level", fallback=True, fallback_reason="weights_missing"))
    board.observe_detections(detections("drone-1", "default"))
    board.select("drone-1", "top_down")
    drone = board.payload()["drones"]["drone-1"]
    assert drone["active"] == "level" and drone["fallback"] and drone["fallback_reason"] == "weights_missing"
    assert drone["requested"] == "top_down"


def test_an_in_flight_refusal_is_shown_with_the_edges_reason():
    board, _, _ = make_board(reply=nack_for("in_flight", detail="land first"))
    board.observe_detections(detections("drone-1", "default"))
    board.select("drone-1", "level")
    drone = board.payload()["drones"]["drone-1"]
    assert drone["active"] == "default" and not drone["pending"]
    assert drone["refusal"] == {"reason": "in_flight", "detail": "land first"}


def test_no_reply_is_reported():
    board, _, _ = make_board(reply=None)
    board.observe_detections(detections("drone-1", "default"))
    board.select("drone-1", "level")
    drone = board.payload()["drones"]["drone-1"]
    assert drone["note"] == "no_reply" and not drone["pending"]


def test_a_send_error_is_reported_not_raised():
    def boom(drone_id, message):
        raise RuntimeError("transport stopped")

    board, _, _ = make_board(reply=boom)
    board.observe_detections(detections("drone-1", "default"))
    board.select("drone-1", "level")
    assert board.payload()["drones"]["drone-1"]["note"] == "send_failed"


def test_bad_selections_are_refused_without_sending():
    board, transport, _ = make_board()
    board.observe_detections(detections("drone-1", "default"))
    assert "Unknown detection profile" in board.select("drone-1", "nadir")
    assert board.select("not a drone!", "level") == "Unknown drone."
    assert "has not reported" in board.select("drone-9", "level")
    assert "Unknown detection profile" in board.set_fleet_default("nadir")[0]
    assert transport.sent == []


def test_fleet_default_goes_to_followers_only_and_to_drones_that_appear_later():
    board, transport, _ = make_board(reply=lambda d, m: None)
    for drone_id in ("drone-1", "drone-2", "drone-3"):
        board.observe_detections(detections(drone_id, "default"))
    board.select("drone-2", "level")  # drone-2 has its own choice
    transport.sent.clear()
    board.observe_detections(detections("drone-3", "top_down"))  # already on the target
    error, targets = board.set_fleet_default("top_down")
    assert error is None and targets == ["drone-1"]
    assert [d for d, _ in transport.sent] == ["drone-1"]
    board.observe_detections(detections("drone-4", "default"))  # a newcomer gets the default once
    board.observe_detections(detections("drone-4", "default"))
    assert [d for d, _ in transport.sent] == ["drone-1", "drone-4"]
    # Choosing "fleet default" for drone-2 drops its own choice and applies the default.
    board.select("drone-2", "")
    assert transport.sent[-1][0] == "drone-2" and transport.sent[-1][1]["profile"] == "top_down"
    assert board.payload()["drones"]["drone-2"]["override"] is None


def test_without_a_fleet_default_nothing_is_ever_sent():
    board, transport, _ = make_board()
    for drone_id in ("drone-1", "drone-2"):
        board.observe_detections(detections(drone_id, "default"))
    board.select("drone-1", "")
    assert board.set_fleet_default("") == (None, [])
    assert transport.sent == []


def test_telemetry_reports_profile_flight_state_and_fallback():
    board, _, _ = make_board()
    telemetry = {"type": "telemetry", "drone_id": "drone-1", "is_flying": True,
                 "detection_profile": "level", "detection_profile_fallback": True}
    assert board.observe_control(telemetry) is False  # the fleet supervisor still needs it
    drone = board.payload()["drones"]["drone-1"]
    assert drone["active"] == "level" and drone["flying"] is True and drone["fallback"]


def test_only_profile_replies_are_consumed():
    board, _, _ = make_board()
    assert board.observe_control({"type": "ack", "for": "takeoff", "drone_id": "drone-1"}) is False
    reply = fp.make_profile_ack("drone-1", "c1", "level")
    assert board.observe_control(reply) is True


def test_the_board_is_bounded():
    board, _, _ = make_board()
    for i in range(MAX_DRONES + 10):
        board.observe_detections(detections(f"drone-{i}", "default"))
    assert len(board.payload()["drones"]) == MAX_DRONES


def test_configured_fleet_drones_are_listed_before_they_report():
    board, _, _ = make_board(fleet_drones=("drone-1", "drone-2"))
    drones = board.payload()["drones"]
    assert set(drones) == {"drone-1", "drone-2"} and not drones["drone-1"]["supported"]


# ---------------------------------------------------------------------------
# Dashboard Socket.IO handlers
# ---------------------------------------------------------------------------


def _client():
    client = socketio.test_client(app)
    client.get_received()
    return client


def _events(received, name):
    return [r["args"][0] for r in received if r["name"] == name]


def test_handlers_select_profiles_through_the_board(clean_app_state):
    board, transport, _ = make_board(reply=ack_for("level"))
    board.observe_detections(detections("drone-1", "default"))
    app_state.profile_board = board
    app_state.logger = None
    client = _client()
    client.emit("set_detection_profile", {"drone_id": "drone-1", "profile": "level"})
    client.emit("set_fleet_detection_profile", {"profile": "top_down"})
    client.emit("get_detection_profiles")
    received = client.get_received()
    client.disconnect()
    assert transport.sent[0][1]["profile"] == "level"
    payloads = _events(received, EVENT)
    assert payloads[-1]["drones"]["drone-1"]["active"] == "level"
    assert payloads[-1]["fleet_default"] == "top_down"
    assert [p["name"] for p in payloads[-1]["profiles"]] == ["default", "top_down", "level"]


def test_handlers_report_bad_input_and_a_missing_board(clean_app_state):
    board, transport, _ = make_board()
    app_state.profile_board = board
    app_state.logger = None
    client = _client()
    client.emit("set_detection_profile", {"drone_id": "drone-1", "profile": "<b>x</b>"})
    client.emit("set_detection_profile", "not a dict")
    received = client.get_received()
    app_state.profile_board = None
    client.emit("set_fleet_detection_profile", {"profile": "level"})
    client.emit("get_detection_profiles")
    received_without = client.get_received()
    client.disconnect()
    assert len(_events(received, "error")) == 2 and transport.sent == []
    assert _events(received_without, "error")
    assert _events(received_without, EVENT)[-1]["available"] is False


def test_edge_frames_feed_the_profile_label_into_the_detection_metrics(clean_app_state, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module._edge_stats, "frame_done", lambda *a, **k: calls.append((a, k)))
    app_state.ground_station = GroundStation(scorer=None)
    fake = MagicMock()
    app_module._edge_process_and_emit(fake, detections("drone-1", "level"))
    app_module._edge_process_and_emit(fake, detections("drone-1"))
    assert calls == [((0,), {"source": "edge", "profile": "level"}), ((0,), {"source": "edge"})]


# ---------------------------------------------------------------------------
# Scoring hints and metrics
# ---------------------------------------------------------------------------


def _scored(profile, reid=0.5, face=0.9):
    scorer = MatchScorer(match_threshold=0.45, reid_weight=0.35, face_weight=0.40, reasoning_weight=0.25)
    station = GroundStation(scorer=scorer, target_reid_embedding=[1.0, 0.0], target_face_embedding=[1.0, 0.0])
    msg = DetectionMessage(timestamp=1.0, frame_id=1, drone_id="drone-1", detection_profile=profile)
    msg.detections.append(Detection(bbox=(0, 0, 4, 4), confidence=0.9,
                                    reid_embedding=[reid, (1 - reid ** 2) ** 0.5],
                                    face_embedding=[face, (1 - face ** 2) ** 0.5]))
    return station.process_message(msg)[0]["score_result"]["combined_score"]


def test_default_and_unknown_profiles_score_exactly_as_before():
    # 0.35 * 0.5 + 0.40 * 0.9 over 0.75, rounded: the score before profiles existed.
    assert _scored(None) == 0.713
    for profile in ("default", "top_down", "level", "made_up"):
        assert _scored(profile) == 0.713, profile


def test_a_measured_weight_scale_changes_the_score_only_with_both_signals(monkeypatch):
    from flightrisk import ground

    monkeypatch.setattr(ground, "weight_scale_for", lambda name: {"face": 1.5} if name == "scaled" else None)
    assert _scored("scaled") > 0.713
    # ReID alone (an appearance-only edge): the scale cancels out in the normalisation.
    assert _scored("scaled", face=0.0) == _scored(None, face=0.0)


def test_scorer_weight_scale_is_neutral_when_absent():
    scorer = MatchScorer(match_threshold=0.45)
    assert scorer.score(reid_score=0.4, face_score=0.8) == scorer.score(
        reid_score=0.4, face_score=0.8, weight_scale={"face": 1.0}
    )


class Recorder:
    """Stands in for a metric instrument; keeps the labels of each observation."""

    def __init__(self, sink):
        self.sink = sink

    def add(self, value, labels=None):
        self.sink.append(labels)

    record = add


def test_profile_events_are_routed_and_counted_with_bounded_labels(monkeypatch):
    seen = []
    monkeypatch.setattr(obs_events.M, "profile_events", Recorder(seen))
    assert obs_events.route_structured("edge", "profile_fallback",
                                       {"profile": "level", "reason": "weights_missing"}) == "detection_pipeline"
    assert obs_events.route_structured("edge", "profile_switch_refused",
                                       {"profile": "mystery", "reason": "free text"}) == "detection_pipeline"
    assert obs_events.route_structured("edge", "profile_selected", {"profile": "top_down"}) == "detection_pipeline"
    assert seen == [
        {"event": "profile_fallback", "profile": "level", "reason": "weights_missing"},
        {"event": "profile_switch_refused", "profile": "other", "reason": "none"},
        {"event": "profile_selected", "profile": "top_down", "reason": "none"},
    ]


@pytest.mark.parametrize("profile,label", [("level", "level"), ("weird", "other")])
def test_frame_metrics_carry_a_bounded_profile_label(monkeypatch, profile, label):
    records = []
    monkeypatch.setattr(obs_events.M, "detections_per_frame", Recorder(records))
    monkeypatch.setattr(obs_events.M, "frames", Recorder(records))
    stats = obs_events.PipelineStats()
    stats.frame_done(2, source="edge", profile=profile)
    stats.frame_done(2, source="edge")
    assert records == [{"profile": label}, {"source": "edge", "profile": label}, None, {"source": "edge"}]


# ---------------------------------------------------------------------------
# End to end over a loopback WebSocket (port 0, never a live ground)
# ---------------------------------------------------------------------------


def test_profile_switch_round_trip_over_the_real_transport():
    import threading
    import time

    import numpy as np

    from flightrisk import edge_device
    from flightrisk.edge import EdgeRunner
    from flightrisk.edge_profiles import ProfileSwitcher
    from flightrisk.transport import EdgeTransportSync, GroundTransportSync

    class Det:
        def __init__(self, name):
            self.name = name

        def detect(self, frame):
            return []

    holder: dict = {}

    def on_message(msg):
        board = holder.get("board")
        if board is not None:
            board.observe_detections(msg)

    def on_control(msg):
        board = holder.get("board")
        if board is not None:
            board.observe_control(msg)

    ground = GroundTransportSync(host="localhost", port=0, on_message=on_message, on_control=on_control)
    ground.start()
    try:
        board = ProfileBoard(ground, lambda name, payload: None)
        holder["board"] = board
        switcher = ProfileSwitcher(None, drone_id="drone-1", loader=lambda p: Det(p.name),
                                   exists=lambda path: "heridal" not in path)
        detector, name = switcher.startup_detector("")
        runner = EdgeRunner(detector=detector, drone_id="drone-1")
        runner.detection_profile = name
        switcher.bind_runner(runner)
        runner.profile_switcher = switcher
        edge = EdgeTransportSync(ws_url=f"ws://localhost:{ground.port}", runner=runner)
        edge.connect()
        stop = threading.Event()

        def edge_loop():
            frame = np.zeros((8, 8, 3), dtype=np.uint8)
            while not stop.is_set():
                edge.send_detections(runner.process_frame(frame))
                ctrl = edge.recv(recv_timeout=0.05)
                if isinstance(ctrl, dict):
                    edge_device._handle_control(ctrl, runner, switcher._log)

        switcher.bind_sender(edge.send)
        worker = threading.Thread(target=edge_loop, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not board.payload()["drones"].get("drone-1", {}).get("supported"):
                time.sleep(0.02)
            assert board.payload()["drones"]["drone-1"]["active"] == "default"
            assert board.select("drone-1", "top_down") is None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and board.payload()["drones"]["drone-1"]["pending"]:
                time.sleep(0.02)
            drone = board.payload()["drones"]["drone-1"]
            assert drone["active"] == "level" and drone["fallback"] and drone["requested"] == "top_down"
            assert runner.detection_profile == "level" and runner.detector.name == "level"
        finally:
            stop.set()
            worker.join(2)
            edge.disconnect()
    finally:
        ground.stop()


# ---------------------------------------------------------------------------
# Review fixes: mismatch and re-send, standby ground, not connected, start warning
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def telemetry(drone_id, profile, flying, **extra):
    return {"type": "telemetry", "drone_id": drone_id, "is_flying": flying, "detection_profile": profile, **extra}


def test_a_refused_choice_is_flagged_and_sent_again_once_the_drone_lands():
    clock = Clock()
    board, transport, _ = make_board(reply=nack_for("in_flight", detail="land first"), clock=clock)
    board.observe_control(telemetry("drone-1", "default", True))
    board.select("drone-1", "top_down")
    drone = board.payload()["drones"]["drone-1"]
    assert drone["mismatch"] and drone["desired"] == "top_down" and drone["active"] == "default"
    assert len(transport.sent) == 1
    board.observe_control(telemetry("drone-1", "default", True))  # still flying: nothing new
    assert len(transport.sent) == 1
    transport.reply = ack_for("top_down")
    board.observe_control(telemetry("drone-1", "default", False))  # landed: sent again, once
    assert len(transport.sent) == 2 and transport.sent[-1][1]["profile"] == "top_down"
    assert not board.payload()["drones"]["drone-1"]["mismatch"]
    board.observe_control(telemetry("drone-1", "top_down", False))
    assert len(transport.sent) == 2


def test_a_restarted_edge_gets_the_operators_choice_again_when_it_reconnects():
    clock = Clock()
    board, transport, _ = make_board(reply=ack_for("level"), clock=clock)
    board.observe_detections(detections("drone-1", "default"))
    board.select("drone-1", "level")
    board.observe_detections(detections("drone-1", "level"))
    assert len(transport.sent) == 1
    clock.t += 1.0
    board.observe_detections(detections("drone-1", "default"))  # no gap: flagged, not re-sent
    assert board.payload()["drones"]["drone-1"]["mismatch"] and len(transport.sent) == 1
    clock.t += 10.0  # silent for 10 s, then back on its startup profile
    board.observe_detections(detections("drone-1", "default"))
    assert len(transport.sent) == 2 and transport.sent[-1][1]["profile"] == "level"


def test_a_fallback_for_the_chosen_profile_is_not_a_mismatch():
    clock = Clock()
    board, transport, _ = make_board(reply=ack_for("level", fallback=True, fallback_reason="weights_missing"),
                                     clock=clock)
    board.observe_detections(detections("drone-1", "default"))
    board.select("drone-1", "top_down")
    board.observe_control(telemetry("drone-1", "level", True, detection_profile_fallback=True,
                                    detection_profile_requested="top_down"))
    board.observe_control(telemetry("drone-1", "level", False, detection_profile_fallback=True,
                                    detection_profile_requested="top_down"))
    drone = board.payload()["drones"]["drone-1"]
    assert not drone["mismatch"] and drone["fallback"] and len(transport.sent) == 1


def test_a_standby_ground_refuses_locally_and_hides_the_controls():
    board, transport, _ = make_board(reply=ack_for("level"))
    transport.identity = MagicMock(role="standby")
    board.observe_detections(detections("drone-1", "default"))
    assert "standby" in board.select("drone-1", "level")
    assert "standby" in board.set_fleet_default("level")[0]
    payload = board.payload()
    assert payload["controls_enabled"] is False and "leader" in payload["controls_note"]
    assert transport.sent == []
    transport.identity = MagicMock(role="leader")
    assert board.select("drone-1", "level") is None and len(transport.sent) == 1
    assert board.payload()["controls_enabled"] is True


def test_a_disconnected_drone_is_reported_as_not_connected():
    board, transport, _ = make_board(reply=ack_for("level"))
    transport.drone_ids = lambda timeout: ["drone-2"]
    board.observe_detections(detections("drone-1", "default"))
    board.select("drone-1", "level")
    drone = board.payload()["drones"]["drone-1"]
    assert drone["note"] == "not_connected" and not drone["pending"] and transport.sent == []


def test_start_warns_while_a_profile_switch_is_pending(clean_app_state, monkeypatch):
    board, _, _ = make_board(reply=None)
    board._spawn = lambda fn, *args: None  # the command stays pending
    board.observe_detections(detections("drone-1", "default"))
    board.select("drone-1", "level")
    assert board.pending_drones() == ["drone-1"]
    app_state.profile_board = board
    app_state.logger = None
    monkeypatch.setattr(app_module, "_run_fleet_action", lambda action: None)
    client = _client()
    client.emit("start_fleet")
    received = client.get_received()
    client.disconnect()
    events = _events(received, "fleet_event")
    assert events and events[0]["event"] == "profile_switch_pending" and events[0]["drone_ids"] == ["drone-1"]


def test_start_without_a_pending_switch_sends_no_warning(clean_app_state, monkeypatch):
    board, _, _ = make_board()
    app_state.profile_board = board
    app_state.logger = None
    monkeypatch.setattr(app_module, "_run_fleet_action", lambda action: None)
    client = _client()
    client.emit("start_fleet")
    received = client.get_received()
    client.disconnect()
    assert _events(received, "fleet_event") == []


def test_the_ground_transport_reports_its_role():
    from flightrisk.fleet_ground_link import GroundIdentity
    from flightrisk.transport import GroundTransportSync

    assert GroundTransportSync(host="127.0.0.1", port=0).identity is None
    standby = GroundIdentity(node_id="win", role="standby", epoch=0)
    assert GroundTransportSync(host="127.0.0.1", port=0, identity=standby).identity.role == "standby"
