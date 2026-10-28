"""Event catalog helpers (flightrisk/obs_events.py and flightrisk/obs_comm.py)."""

import logging
import time

import numpy as np
import pytest

from flightrisk import obs_comm, obs_events
from flightrisk import obs_metrics as M
from flightrisk.drone.fake import FakeDrone
from flightrisk.obs_log import Throttle


class Recorder:
    """Stands in for one OTel instrument and keeps every call."""

    def __init__(self):
        self.calls = []

    def add(self, value, attributes=None):
        self.calls.append((value, dict(attributes or {})))

    record = set = add


@pytest.fixture
def rec(monkeypatch):
    """Replace every instrument in obs_metrics (and the target counter) with a Recorder."""
    recorders = {}
    for name in ("http_requests", "socket_events", "drone_battery", "drone_height", "drone_speed", "drone_attitude",
                 "drone_flying", "flight_safety_events", "obstacle_checks", "obstacle_depth_ratio", "alert_transitions",
                 "alert_response", "reasoning_calls", "reasoning_duration", "reasoning_tokens", "comm_messages",
                 "comm_bytes", "comm_acks", "comm_dropped", "comm_rtt", "clock_offset", "stage_duration", "signal_score",
                 "alert_levels", "frames", "detections_per_frame", "model_loads", "queue_dropped", "drone_commands",
                 "drone_command_duration", "land_duration"):
        r = Recorder()
        monkeypatch.setattr(M, name, r)
        recorders[name] = r
    r = Recorder()
    monkeypatch.setattr(obs_events, "M_target_changes", r)
    recorders["target_changes"] = r
    monkeypatch.setattr(obs_events, "_snapshot_throttle", Throttle(60.0))
    monkeypatch.setattr(obs_events, "_safety_throttle", Throttle(60.0))
    return recorders


def events(caplog, name):
    return [r for r in caplog.records if getattr(r, "fr_event", None) == name]


class TestUserInput:
    def test_content_off_logs_no_operator_text(self, caplog, monkeypatch):
        monkeypatch.delenv("FLIGHTRISK_TELEMETRY_CONTENT", raising=False)
        with caplog.at_level(logging.INFO, logger="flightrisk"):
            obs_events.user_input("set_description", channel="socket",
                                  payload={"description": "red coat near the lake"}, remote_addr="127.0.0.1")
        rec = events(caplog, "user_input")[-1]
        assert rec.fr_fields["input"] == {"description": {"chars": 22}}
        assert rec.fr_fields["content_capture"] is False
        assert "red coat" not in caplog.text

    def test_content_on_keeps_redacted_text_and_caps_notes(self, caplog, monkeypatch):
        monkeypatch.setenv("FLIGHTRISK_TELEMETRY_CONTENT", "1")
        with caplog.at_level(logging.INFO, logger="flightrisk"):
            obs_events.user_input("match_alert_response", channel="socket",
                                  payload={"alert_id": "a1", "action": "dismiss",
                                           "notes": "x" * 900 + " sk-ant-api03-FAKEFAKEFAKE123456"})
        fields = events(caplog, "user_input")[-1].fr_fields["input"]
        assert fields["action"] == "dismiss" and fields["alert_id"] == "a1"
        assert len(fields["notes"]) <= 500 and "FAKEFAKE" not in fields["notes"]

    def test_image_payload_is_reduced_to_its_size(self, caplog, monkeypatch):
        monkeypatch.setenv("FLIGHTRISK_TELEMETRY_CONTENT", "1")
        with caplog.at_level(logging.INFO, logger="flightrisk"):
            obs_events.user_input("set_target", channel="socket", payload={"image": "QUJD" * 1000})
        assert events(caplog, "user_input")[-1].fr_fields["input"] == {"image": {"b64_chars": 4000, "approx_bytes": 3000}}

    def test_queries_are_debug_and_failures_are_errors(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="flightrisk"):
            obs_events.user_input("get_fleet_status", channel="socket")
            obs_events.user_input("set_threshold", channel="socket", outcome="error", error="ValueError")
        levels = [r.levelno for r in events(caplog, "user_input")[-2:]]
        assert levels == [logging.DEBUG, logging.ERROR]


class TestTargetImage:
    def _img(self):
        img = np.zeros((300, 200, 3), np.uint8)
        img[50:250, 40:160] = (30, 30, 200)
        return img

    def test_content_off_numbers_only(self, rec, caplog, monkeypatch, tmp_path):
        monkeypatch.delenv("FLIGHTRISK_TELEMETRY_CONTENT", raising=False)
        monkeypatch.setenv("FLIGHTRISK_ARTIFACT_DIR", str(tmp_path / "art"))
        fields = obs_events.target_image(self._img(), source="upload", raw=b"\x89PNG\r\n\x1a\n" + b"0" * 100,
                                         quality={"overall_score": 0.8123, "grade": "B", "issues": ["blur"]},
                                         face_report={"faces": 0, "best_det_score": 0.0, "reason": "no face"})
        assert fields["width"] == 200 and fields["height"] == 300 and fields["mime"] == "image/png"
        assert fields["quality"] == {"overall_score": 0.8123, "grade": "B"}
        assert fields["face_report"] == {"faces": 0, "best_det_score": 0.0}
        assert "sha256" not in fields and "preview_url" not in fields and "artifact" not in fields
        assert not (tmp_path / "art").exists()
        assert rec["target_changes"].calls == [(1, {"source": "upload"})]

    def test_content_on_stores_a_private_artifact(self, rec, monkeypatch, tmp_path):
        monkeypatch.setenv("FLIGHTRISK_TELEMETRY_CONTENT", "1")
        monkeypatch.setenv("FLIGHTRISK_PREVIEW_PORT", "0")
        from flightrisk import obs_artifacts

        store = obs_artifacts.ArtifactStore(tmp_path / "art")
        monkeypatch.setattr(obs_artifacts, "default_store", lambda: store)
        fields = obs_events.target_image(self._img(), source="revert")
        assert len(fields["sha256"]) == 64
        assert (tmp_path / "art" / fields["artifact"]).exists()
        assert "preview_url" not in fields  # preview server disabled


def test_telemetry_snapshot_metrics_and_sampled_event(rec, caplog):
    drone = FakeDrone(name="sim-9")
    drone.connect()
    drone.takeoff()
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        for _ in range(5):
            obs_events.telemetry_snapshot(drone.snapshot())
    assert rec["drone_battery"].calls[-1] == (100.0, {"drone_id": "sim-9", "backend": "sim"})
    height_value, height_labels = rec["drone_height"].calls[-1]
    assert height_value == pytest.approx(1.0) and height_labels["source"] == "sim.height"
    assert {c[1]["axis"] for c in rec["drone_speed"].calls} == {"ground", "vx", "vy", "vz"}
    snaps = events(caplog, "drone_telemetry_snapshot")
    assert len(snaps) == 1  # one event per drone per interval
    readings = snaps[0].fr_fields["readings"]
    assert readings["height_m"]["source"] == "sim.height" and readings["height_m"]["unit"] == "m"
    assert "age_s" in readings["battery_pct"]


def test_edge_telemetry_feeds_drone_gauges(rec, caplog):
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        obs_comm.ground_received({"type": "telemetry", "drone_id": "drone-2", "battery": 55, "height_cm": 120,
                                  "is_flying": True, "link_age_s": 0.2}, 180)
    assert rec["drone_battery"].calls[-1] == (55, {"drone_id": "drone-2", "backend": "edge"})
    assert rec["drone_height"].calls[-1][0] == pytest.approx(1.2)
    assert rec["comm_messages"].calls[-1][1]["type"] == "telemetry"


def test_route_structured_counts_safety_events(rec):
    assert obs_events.route_structured("lane-executor", "edge_land", {"drone_id": "drone-1", "reason": "link_loss"}) == "flight_safety"
    assert rec["flight_safety_events"].calls[-1][1] == {"kind": "watchdog_land", "reason": "link_loss", "drone_id": "drone-1"}
    obs_events.route_structured("lane-executor", "edge_land_not_executed", {"drone_id": "drone-1", "outcome": "dropped_busy"})
    assert rec["flight_safety_events"].calls[-1][1]["kind"] == "land_failed"
    assert rec["flight_safety_events"].calls[-1][1]["reason"] == "dropped_busy"
    obs_events.route_structured("lane-executor", "edge_geofence_refused", {"drone_id": "drone-1"})
    assert rec["flight_safety_events"].calls[-1][1]["kind"] == "geofence_refused"
    assert obs_events.route_structured("lane-executor", "edge_telemetry_dropped", {"drone_id": "drone-1"}) == "drone_comm"
    assert rec["comm_dropped"].calls[-1][1]["reason"] == "queue_full"
    assert obs_events.route_structured("dashboard", "pipeline_ready", {}) is None


def test_alert_latch_transitions_are_exported(rec, caplog):
    from flightrisk.dashboard.alert_latch import MatchAlertLatch, _export_transition

    now = [100.0]
    latch = MatchAlertLatch(clock=lambda: now[0])
    latch.add_listener(_export_transition)
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        alert_id = latch.try_open(["grid:1_1"])
        latch.attach(alert_id, {"alert_level": "possible_match"})
        now[0] = 112.5
        latch.dismiss(alert_id)
    transitions = [c[1]["transition"] for c in rec["alert_transitions"].calls]
    assert transitions == ["opened", "dismissed"]
    assert rec["alert_response"].calls == [(12.5, {"action": "dismissed"})]
    last = events(caplog, "alert")[-1].fr_fields
    assert last["awaiting_s"] == 12.5 and last["alert_level"] == "possible_match"


def test_latch_listener_errors_never_change_the_latch():
    from flightrisk.dashboard.alert_latch import AlertPhase, MatchAlertLatch

    latch = MatchAlertLatch()
    latch.add_listener(lambda e: 1 / 0)
    alert_id = latch.try_open(["grid:2_2"])
    assert alert_id and latch.phase is AlertPhase.AWAITING_RESPONSE
    assert latch.confirm(alert_id) is not None and latch.phase is AlertPhase.RESOLVED


def test_obstacle_check_ratio_and_trigger(rec, caplog):
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        obs_events.obstacle_check({"action": "clear", "center_depth": 0.2}, min_safe_depth=0.4)
        obs_events.obstacle_check({"action": "reverse", "center_depth": 0.6, "confidence": 0.5}, min_safe_depth=0.4, waypoint=3)
    assert [c[1]["action"] for c in rec["obstacle_checks"].calls] == ["clear", "reverse"]
    assert [round(c[0], 3) for c in rec["obstacle_depth_ratio"].calls] == [0.5, 1.5]
    trigger = events(caplog, "flight_safety")[-1].fr_fields
    assert trigger["kind"] == "obstacle" and trigger["margin"] == pytest.approx(-0.2) and trigger["waypoint"] == 3


def test_reasoning_call_never_logs_text(rec, caplog):
    result = {"match": True, "confidence": "high", "reasoning": "SECRET REASONING TEXT"}
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        obs_events.reasoning_call(provider="claude", model="claude-x", kind="analyze", duration_ms=812.0,
                                  outcome=obs_events.reasoning_outcome(result), tokens_in=1200, tokens_out=40)
    assert "SECRET REASONING" not in caplog.text
    assert rec["reasoning_calls"].calls[-1][1] == {"provider": "claude", "kind": "analyze", "outcome": "match"}
    assert sorted(c[0] for c in rec["reasoning_tokens"].calls) == [40, 1200]
    assert obs_events.reasoning_outcome({"confidence": "error"}) == "error"
    assert obs_events.reasoning_outcome(None) == "error"


def test_timed_reasoning_records_metadata(rec, caplog):
    from flightrisk.dashboard.alerts import _timed_reasoning

    class Agent:
        provider, model = "gemma", "gemma4"

        def __init__(self):
            self.last_usage = {"input_tokens": 300, "output_tokens": 12}

    with caplog.at_level(logging.INFO, logger="flightrisk"):
        out = _timed_reasoning(Agent(), "describe", lambda: {"match": False, "confidence": "low", "reasoning": "hidden"})
    assert out["reasoning"] == "hidden"
    assert "hidden" not in caplog.text
    assert rec["reasoning_calls"].calls[-1][1]["outcome"] == "no_match"


def test_pipeline_stats_summary(rec, caplog):
    now = [0.0]
    stats = obs_events.PipelineStats(interval_s=5.0, clock=lambda: now[0])
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        for i in range(10):
            stats.stage("detect", 20.0 + i)
            stats.signal_scores({"reid": 0.4, "combined": 0.3})
            stats.alert_level("no_match")
            now[0] += 0.6
            stats.frame_done(2, source="sim")
    summary = events(caplog, "detection_pipeline")[-1].fr_fields
    assert summary["frames"] == 9 and summary["persons"] == 18
    assert summary["stages_ms"]["detect"]["max"] == 28.0
    assert summary["scores"]["reid"]["mean"] == 0.4
    assert rec["stage_duration"].calls[0] == (20.0, {"stage": "detect"})
    assert len(rec["detections_per_frame"].calls) == 10


class TestComm:
    def test_commands_logged_individually_in_a_span(self, rec, caplog):
        with caplog.at_level(logging.INFO, logger="flightrisk"):
            obs_comm.ground_sent({"type": "assign_lanes", "drone_id": "drone-1", "cmd_id": "c-7", "epoch": 3,
                                  "plan_id": "p1", "lanes": [{}, {}]}, "drone-1", 512)
        ev = events(caplog, "drone_comm")[-1].fr_fields
        assert ev["type"] == "assign_lanes" and ev["cmd_id"] == "c-7" and ev["epoch"] == 3 and ev["lanes"] == 2
        assert ev["direction"] == "out" and ev["size"] == 512

    def test_high_rate_types_are_aggregated(self, rec, caplog, monkeypatch):
        now = [0.0]
        monkeypatch.setattr(obs_comm, "_agg", obs_comm._Aggregator(5.0, clock=lambda: now[0]))
        with caplog.at_level(logging.INFO, logger="flightrisk"):
            for _ in range(20):
                obs_comm.ground_received({"type": "pong", "t1": 1.0}, 40, "drone-1")
                now[0] += 0.3
        assert len(rec["comm_messages"].calls) == 20
        agg = [r.fr_fields for r in events(caplog, "drone_comm") if r.fr_fields.get("aggregated")]
        assert len(agg) == 1 and agg[0]["count"] >= 17

    def test_acks_and_nacks_counted_with_reason(self, rec):
        obs_comm.ground_received({"type": "nack", "drone_id": "drone-2", "for": "assign_lanes", "reason": "geofence"}, 90)
        assert rec["comm_acks"].calls[-1][1] == {"kind": "nack", "for": "assign_lanes", "reason": "geofence", "drone_id": "drone-2"}

    def test_set_target_never_logs_embeddings(self, rec, caplog):
        with caplog.at_level(logging.INFO, logger="flightrisk"):
            obs_comm.ground_broadcast({"type": "set_target", "reid_embedding": [0.123456] * 512, "face_embedding": None}, 2, 2)
        ev = events(caplog, "drone_comm")[-1].fr_fields
        assert ev["has_reid_embedding"] is True and ev["has_face_embedding"] is False
        assert "0.123456" not in caplog.text

    def test_failed_send_counted_as_dropped(self, rec):
        obs_comm.ground_sent({"type": "land"}, "drone-3", outcome="not_connected")
        assert rec["comm_dropped"].calls[-1][1] == {"side": "ground", "type": "land", "reason": "not_connected"}

    def test_edge_learns_its_drone_id_from_outgoing_messages(self, rec):
        obs_comm.edge_sent({"type": "telemetry", "drone_id": "drone-5"}, True, 100)
        obs_comm.edge_received({"type": "start_at", "t_ground_s": 1.0}, 60)
        assert rec["comm_messages"].calls[-1][1]["drone_id"] == "drone-5"

    def test_hooks_never_raise(self, monkeypatch):
        monkeypatch.setattr(M, "comm_messages", None)  # broken instrument
        obs_comm.ground_received({"type": "ack", "drone_id": "d"}, 10)
        obs_comm.edge_sent(object())


def test_helpers_never_raise_into_callers(monkeypatch):
    monkeypatch.setattr(M, "flight_safety_events", None)
    obs_events.flight_safety("battery_low", reason="warn_threshold")
    obs_events.telemetry_snapshot(object())
    obs_events.target_image(None, source="upload")


def test_queue_drop_is_counted(rec):
    obs_events.queue_dropped("reasoning", kind="analyze")
    obs_events.queue_dropped("reasoning", kind="analyze")
    assert len(rec["queue_dropped"].calls) == 2


def test_frame_age_gauge_is_computed_at_collection():
    M.note_frame()
    first = next(iter(M._observe_frame_age(None)))
    assert first.value < 1.0
    time.sleep(0.05)
    assert next(iter(M._observe_frame_age(None))).value >= 0.05
