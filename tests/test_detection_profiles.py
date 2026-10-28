"""Detection profiles on the edge: registry, protocol messages and the profile switcher.

No model is loaded here: detectors are fakes built by an injected loader,
and weights files are checked through an injected ``exists``.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import numpy as np
import pytest

from flightrisk import edge_device
from flightrisk import fleet_protocol as fp
from flightrisk.edge import DetectionMessage, EdgeRunner
from flightrisk.edge_profiles import IN_FLIGHT_DETAIL, ProfileSwitcher
from flightrisk.fleet_authority import ACCEPT, IGNORE, EdgeAuthority
from flightrisk.vision import profiles as P
from flightrisk.vision.profiles import (
    DEFAULT_PROFILE,
    REGISTRY,
    DetectionProfile,
    ProfileRegistry,
    ProfileUnavailableError,
    UnknownProfileError,
    build_detector,
)

HERIDAL = "models/heridal_yolo11s.pt"


class RecordingLog:
    """A StructuredLogger stand-in that keeps (level, event, fields)."""

    def __init__(self):
        self.records: list[tuple[str, str, dict]] = []

    def _add(self, level, event, **fields):
        self.records.append((level, event, fields))

    def debug(self, event, **fields):
        self._add("debug", event, **fields)

    def info(self, event, **fields):
        self._add("info", event, **fields)

    def warning(self, event, **fields):
        self._add("warning", event, **fields)

    def error(self, event, **fields):
        self._add("error", event, **fields)

    def events(self, name):
        return [fields for _, event, fields in self.records if event == name]


class FakeDetector:
    """Stands in for PersonDetector; remembers the settings it was built with."""

    def __init__(self, **settings):
        self.settings = settings
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        return [{"bbox": [0, 0, 4, 4], "confidence": 0.9}]


def fake_loader(profile):
    return FakeDetector(name=profile.name, confidence=profile.confidence, tiled=profile.tiled)


def all_weights_present(path):
    return True


def no_heridal(path):
    return "heridal" not in path


def make_switcher(*, exists=all_weights_present, is_active=None, loader=fake_loader, start="default"):
    log = RecordingLog()
    sent: list[dict] = []
    switcher = ProfileSwitcher(
        None, drone_id="drone-1", send=sent.append, is_active=is_active, loader=loader,
        exists=exists, logger=log, run_async=False,
    )
    detector, name = switcher.startup_detector(start)
    runner = EdgeRunner(detector=detector)
    runner.detection_profile = name
    switcher.bind_runner(runner)
    runner.profile_switcher = switcher
    return switcher, runner, sent, log


def request(profile="top_down", cmd_id="c1", **extra):
    return fp.make_set_detection_profile(profile, cmd_id=cmd_id, **extra)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_has_the_builtin_profiles_in_order():
    assert REGISTRY.names() == ("default", "top_down", "level")
    top = REGISTRY.get("top_down")
    assert top.weights == HERIDAL and top.weights_required and top.tiled is True
    assert top.confidence == 0.5 and top.fallback == "level"
    level = REGISTRY.get("level")
    assert level.weights == "yolo11n.pt" and level.tiled is False
    assert level.face_weight_scale > 1.0 > top.face_weight_scale


def test_registry_lookup_of_unknown_or_non_string_names_raises():
    for bad in ("nadir", None, 3, ["level"]):
        with pytest.raises(UnknownProfileError):
            REGISTRY.get(bad)
    assert "nadir" not in REGISTRY and ["level"] not in REGISTRY


def test_registry_accepts_new_profiles_without_code_changes_and_refuses_duplicates():
    reg = ProfileRegistry(P.builtin_profiles())
    thermal = DetectionProfile(name="thermal", label="Thermal", description="IR camera.", weights="ir.pt",
                               weights_required=True, fallback="level")
    reg.register(thermal)
    assert reg.get("thermal") is thermal and reg.names()[-1] == "thermal"
    with pytest.raises(ValueError):
        reg.register(thermal)
    reg.register(thermal, replace=True)
    resolved = reg.resolve("thermal", exists=lambda path: path != "ir.pt" and "ir.pt" not in path)
    assert resolved.profile.name == "level" and resolved.fallback and resolved.fallback_reason == "weights_missing"


@pytest.mark.parametrize("kwargs", [
    {"name": "Top-Down"}, {"name": "x" * 33}, {"name": ""}, {"confidence": 0.0}, {"confidence": 1.0},
    {"nms_iou": 0.0}, {"input_size": 16}, {"tile_overlap": 1.0}, {"tile_size": 0},
    {"face_weight_scale": -1.0}, {"fallback": "same"}, {"weights": ""},
])
def test_invalid_profiles_are_refused(kwargs):
    base = {"name": "same", "label": "L", "description": "D", "weights": "w.pt"}
    with pytest.raises(ValueError):
        DetectionProfile(**{**base, **kwargs})


def test_resolve_falls_back_when_required_weights_are_missing():
    resolved = REGISTRY.resolve("top_down", exists=no_heridal)
    assert resolved.requested == "top_down" and resolved.profile.name == "level"
    assert resolved.fallback is True and resolved.fallback_reason == "weights_missing"
    direct = REGISTRY.resolve("top_down", exists=all_weights_present)
    assert direct.profile.name == "top_down" and not direct.fallback and direct.fallback_reason is None


def test_resolve_raises_when_nothing_in_the_chain_can_load_and_survives_cycles():
    reg = ProfileRegistry([
        DetectionProfile(name="a", label="A", description="", weights="a.pt", weights_required=True, fallback="b"),
        DetectionProfile(name="b", label="B", description="", weights="b.pt", weights_required=True, fallback="a"),
    ])
    with pytest.raises(ProfileUnavailableError):
        reg.resolve("a", exists=lambda path: False)


def test_default_profile_builds_exactly_the_pre_profile_edge_detector():
    calls = []
    build_detector(REGISTRY.get(DEFAULT_PROFILE), detector_cls=lambda **kw: calls.append(kw))
    assert calls == [{"model_name": "yolo11n.pt", "confidence": 0.4}]


def test_top_down_and_level_build_their_own_settings():
    calls = []
    build_detector(REGISTRY.get("top_down"), detector_cls=lambda **kw: calls.append(kw), exists=all_weights_present)
    build_detector(REGISTRY.get("level"), detector_cls=lambda **kw: calls.append(kw))
    assert calls[0] == {"model_name": HERIDAL, "confidence": 0.5, "tiled": True}
    assert calls[1] == {"model_name": "yolo11n.pt", "confidence": 0.4, "tiled": False}


def test_profile_label_and_weight_scale_are_bounded():
    assert P.profile_label("level") == "level"
    assert P.profile_label("made_up") == "other"
    assert P.profile_label(None) == "none"
    assert P.weight_scale_for("default") is None
    assert P.weight_scale_for("made_up") is None
    assert P.weight_scale_for(None) is None
    assert P.weight_scale_for("level") == {"face": 1.5, "reid": 1.0}


# ---------------------------------------------------------------------------
# Protocol messages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("proto", [1, 2])
def test_profile_messages_validate_and_keep_cmd_id_on_every_version(proto):
    msg = request(drone_id="drone-1")
    assert fp.validate_ground_to_edge(msg, proto=proto) == fp.SET_DETECTION_PROFILE
    assert fp.validate_ground_to_edge(msg, proto=proto, require_fence=True) == fp.SET_DETECTION_PROFILE
    assert fp.shape_for_peer(msg, proto) == msg
    ack = fp.make_profile_ack("drone-1", "c1", "level", requested="top_down", fallback=True,
                              fallback_reason="weights_missing")
    nack = fp.make_profile_nack("drone-1", "c1", "in_flight", profile="level", requested="top_down",
                                detail=IN_FLIGHT_DETAIL)
    for reply in (ack, nack):
        assert fp.validate_edge_to_ground(reply, proto=proto) in (fp.ACK, fp.NACK)
        assert fp.shape_for_peer(reply, proto) == reply
        assert fp.decode(fp.encode(reply, direction=fp.DIRECTION_EDGE_TO_GROUND, proto=proto),
                         direction=fp.DIRECTION_EDGE_TO_GROUND, proto=proto) == reply


@pytest.mark.parametrize("bad", [
    {"type": "set_detection_profile", "cmd_id": "c1"},
    {"type": "set_detection_profile", "profile": "level"},
    {"type": "set_detection_profile", "profile": "Level", "cmd_id": "c1"},
    {"type": "set_detection_profile", "profile": "x" * 33, "cmd_id": "c1"},
    {"type": "set_detection_profile", "profile": ["level"], "cmd_id": "c1"},
    {"type": "set_detection_profile", "profile": "level", "cmd_id": "bad id"},
    {"type": "set_detection_profile", "profile": "level", "cmd_id": "c1", "epoch": 3},
    {"type": "set_detection_profile", "profile": "level", "cmd_id": "c1", "drone_id": ""},
    {"type": "set_detection_profile", "profile": "level", "cmd_id": "c1", "face_embedding": [0.1]},
])
@pytest.mark.parametrize("proto", [1, 2])
def test_malformed_profile_commands_are_rejected(bad, proto):
    with pytest.raises(fp.ProtocolError):
        fp.validate_ground_to_edge(bad, proto=proto)


@pytest.mark.parametrize("bad", [
    {"type": "ack", "for": "set_detection_profile", "drone_id": "d", "cmd_id": "c1", "profile": "level"},
    {"type": "ack", "for": "set_detection_profile", "drone_id": "d", "cmd_id": "c1", "profile": "level",
     "fallback": True},
    {"type": "ack", "for": "set_detection_profile", "drone_id": "d", "cmd_id": "c1", "profile": "level",
     "fallback": False, "fallback_reason": "weights_missing"},
    {"type": "ack", "for": "set_detection_profile", "drone_id": "d", "profile": "level", "fallback": False},
    {"type": "nack", "for": "set_detection_profile", "drone_id": "d", "cmd_id": "c1", "reason": "geofence"},
    {"type": "nack", "for": "set_detection_profile", "drone_id": "d", "cmd_id": "c1", "reason": "in_flight",
     "detail": "x" * 201},
    {"type": "nack", "for": ["set_detection_profile"], "drone_id": "d", "cmd_id": "c1", "reason": "busy"},
])
@pytest.mark.parametrize("proto", [1, 2])
def test_malformed_profile_replies_are_rejected(bad, proto):
    with pytest.raises(fp.ProtocolError):
        fp.validate_edge_to_ground(bad, proto=proto)
    with pytest.raises(fp.ProtocolError):
        fp.shape_for_peer(bad, proto)


def test_existing_protocol_sets_and_landing_rules_are_unchanged():
    # Older peers compare these sets; the new command lives in its own set.
    assert fp.SET_DETECTION_PROFILE not in fp.GROUND_TO_EDGE_TYPES_V2
    assert fp.SET_DETECTION_PROFILE not in fp.COMMAND_TYPES | fp.FENCED_COMMAND_TYPES | fp.RETRYABLE_COMMAND_TYPES
    assert fp.CONFIG_COMMAND_TYPES == {fp.SET_DETECTION_PROFILE}
    for proto in (None, 1, 2):
        assert fp.validate_ground_to_edge({"type": "land", "profile": "BAD"}, proto=proto) == "land"


def test_v2_nack_reasons_for_lane_commands_are_not_widened():
    nack = {"type": "nack", "for": "assign_lanes", "drone_id": "d", "cmd_id": "c1", "reason": "in_flight",
            "lanes_total": 1}
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(nack, proto=2)


def test_old_detection_messages_still_round_trip_without_a_profile():
    runner = EdgeRunner()
    msg = DetectionMessage(timestamp=1.0, frame_id=1)
    data = runner.to_dict(msg)
    assert "detection_profile" not in data
    assert EdgeRunner.from_dict(data).detection_profile is None
    data["detection_profile"] = "<script>"
    assert EdgeRunner.from_dict(data).detection_profile is None
    data["detection_profile"] = "level"
    assert EdgeRunner.from_dict(data).detection_profile == "level"


def test_multi_ground_edge_obeys_profile_commands_only_from_its_authority():
    auth = EdgeAuthority("drone-1", "edge-node")
    leader, standby = object(), object()
    auth.opened(leader, "leader")
    auth.opened(standby, "standby")
    auth.on_message(leader, fp.make_ground_hello("mac", "leader", 1))
    auth.on_message(standby, fp.make_ground_hello("win", "standby", 0))
    assert auth.on_message(leader, request()).action == ACCEPT
    assert auth.on_message(standby, request(cmd_id="c2")).action == IGNORE
    # Before any hello nothing but a landing counts.
    fresh = object()
    auth.opened(fresh, "fresh")
    assert auth.on_message(fresh, request(cmd_id="c3")).action == IGNORE


def test_uplink_reply_completion_keeps_the_profile_ack_valid_on_v2():
    auth = EdgeAuthority("drone-1", "edge-node")
    ack = fp.make_profile_ack("drone-1", "c1", "level", requested="level")
    out = auth.outbound(ack)
    assert out["cmd_id"] == "c1" and out["result"] == "ok"
    fp.validate_message(out, proto=2)


# ---------------------------------------------------------------------------
# The switcher
# ---------------------------------------------------------------------------


def test_startup_without_a_selection_is_the_default_profile():
    switcher, runner, sent, log = make_switcher(start="")
    assert switcher.active == "default" and runner.detection_profile == "default"
    assert runner.detector.settings == {"name": "default", "confidence": 0.4, "tiled": None}
    assert log.events("profile_selected")[0]["phase"] == "startup"
    assert sent == []


def test_startup_with_missing_weights_falls_back_and_warns():
    switcher, _runner, _, log = make_switcher(exists=no_heridal, start="top_down")
    assert switcher.active == "level" and switcher.fallback_reason == "weights_missing"
    assert log.events("profile_fallback")[0]["requested"] == "top_down"
    assert switcher.status_fields() == {
        "detection_profile": "level", "detection_profile_fallback": True, "detection_profile_requested": "top_down",
    }


def test_startup_with_a_bad_or_unloadable_profile_runs_the_default():
    switcher, _, _, log = make_switcher(start="nadir")
    assert switcher.active == "default"
    assert log.events("profile_switch_refused")[0]["reason"] == "unknown_profile"

    def fail_top_down(profile):
        if profile.name == "top_down":
            raise RuntimeError("corrupt weights")
        return fake_loader(profile)

    switcher, _, _, log = make_switcher(start="top_down", loader=fail_top_down)
    assert switcher.active == "default"
    assert log.events("profile_switch_refused")[0]["reason"] == "load_failed"


def test_startup_failure_of_the_default_profile_is_raised_as_before():
    def fail(profile):
        raise RuntimeError("no yolo")

    with pytest.raises(RuntimeError):
        make_switcher(start="", loader=fail)


def test_switch_swaps_the_model_and_settings_and_acks():
    switcher, runner, sent, log = make_switcher()
    old = runner.detector
    switcher.handle(request("top_down"))
    assert runner.detector is not old
    assert runner.detector.settings == {"name": "top_down", "confidence": 0.5, "tiled": True}
    assert runner.detection_profile == "top_down" == switcher.active
    assert sent == [fp.make_profile_ack("drone-1", "c1", "top_down", requested="top_down")]
    selected = log.events("profile_selected")[-1]
    assert selected["profile"] == "top_down" and selected["previous"] == "default" and selected["changed"]
    assert selected["drone_id"] == "drone-1"
    # The next frame runs the new detector and says which profile ran.
    msg = runner.process_frame(np.zeros((8, 8, 3), dtype=np.uint8))
    assert runner.detector.calls == 1 and old.calls == 0
    assert msg.detection_profile == "top_down"
    assert runner.to_dict(msg)["detection_profile"] == "top_down"


def test_switch_to_the_active_profile_acks_without_reloading():
    loads = []

    def counting(profile):
        loads.append(profile.name)
        return fake_loader(profile)

    switcher, _runner, sent, _ = make_switcher(loader=counting)
    switcher.handle(request("default"))
    assert loads == ["default"]  # only the startup load
    assert sent[-1]["type"] == "ack" and sent[-1]["profile"] == "default"


def test_missing_weights_fall_back_warn_and_report_the_fallback():
    switcher, runner, sent, log = make_switcher(exists=no_heridal)
    switcher.handle(request("top_down"))
    assert runner.detection_profile == "level"
    reply = sent[-1]
    assert reply["type"] == "ack" and reply["profile"] == "level" and reply["requested"] == "top_down"
    assert reply["fallback"] is True and reply["fallback_reason"] == "weights_missing"
    fallback = log.events("profile_fallback")[-1]
    assert fallback["drone_id"] == "drone-1" and fallback["missing_weights"] == HERIDAL
    assert ("warning", "profile_fallback") in [(lvl, ev) for lvl, ev, _ in log.records]


def test_no_loadable_profile_is_refused_as_unavailable():
    switcher, runner, sent, log = make_switcher(exists=lambda path: False)
    reg = ProfileRegistry(P.builtin_profiles())
    reg.register(DetectionProfile(name="ir", label="IR", description="", weights="ir.pt", weights_required=True))
    switcher._registry = reg
    switcher.handle(request("ir"))
    assert sent[-1]["type"] == "nack" and sent[-1]["reason"] == "unavailable"
    assert runner.detection_profile == "default"
    assert log.events("profile_switch_refused")[-1]["reason"] == "weights_missing"


def test_in_flight_switch_is_refused_with_a_clear_reason_and_nothing_loads():
    loads = []

    def counting(profile):
        loads.append(profile.name)
        return fake_loader(profile)

    switcher, runner, sent, log = make_switcher(is_active=lambda: True, loader=counting)
    switcher.handle(request("top_down"))
    assert loads == ["default"]
    assert runner.detection_profile == "default"
    nack = sent[-1]
    assert nack["type"] == "nack" and nack["reason"] == "in_flight" and nack["profile"] == "default"
    assert "ground" in nack["detail"]
    refused = log.events("profile_switch_refused")[-1]
    assert refused["reason"] == "in_flight" and refused["drone_id"] == "drone-1"


def test_unreadable_flight_state_counts_as_flying():
    def broken():
        raise RuntimeError("state unreadable")

    switcher, runner, sent, _ = make_switcher(is_active=broken)
    switcher.handle(request("level"))
    assert sent[-1]["reason"] == "in_flight" and runner.detection_profile == "default"


def test_takeoff_during_the_load_keeps_the_old_detector():
    flying = {"now": False}

    def loader(profile):
        flying["now"] = True  # the drone takes off while the model loads
        return fake_loader(profile)

    switcher, runner, sent, _ = make_switcher(is_active=lambda: flying["now"])
    old = runner.detector
    switcher._loader = loader
    switcher.handle(request("level"))
    assert runner.detector is old and runner.detection_profile == "default"
    assert sent[-1]["type"] == "nack" and sent[-1]["reason"] == "in_flight"


def test_load_failure_keeps_the_old_detector_and_nacks():
    switcher, runner, sent, log = make_switcher()
    old = runner.detector

    def broken(profile):
        raise RuntimeError("out of memory")

    switcher._loader = broken
    switcher.handle(request("level"))
    assert runner.detector is old and switcher.active == "default"
    assert sent[-1]["reason"] == "load_failed" and "out of memory" in sent[-1]["detail"]
    assert log.events("profile_switch_refused")[-1]["reason"] == "load_failed"
    # The switcher is free again afterwards.
    switcher._loader = fake_loader
    switcher.handle(request("level", cmd_id="c2"))
    assert sent[-1]["type"] == "ack" and runner.detection_profile == "level"


def test_unknown_and_invalid_requests_are_refused():
    switcher, runner, sent, _ = make_switcher()
    switcher.handle(request("nadir"))
    assert sent[-1]["reason"] == "unknown_profile" and sent[-1]["requested"] == "nadir"
    switcher.handle({"type": "set_detection_profile", "profile": "LEVEL", "cmd_id": "c9"})
    assert sent[-1]["reason"] == "invalid" and sent[-1]["cmd_id"] == "c9" and "requested" not in sent[-1]
    count = len(sent)
    switcher.handle({"type": "set_detection_profile", "profile": "level"})  # no cmd_id: nothing to answer
    assert len(sent) == count
    assert runner.detection_profile == "default"


def test_a_command_for_another_drone_is_ignored():
    switcher, runner, sent, _ = make_switcher()
    switcher.handle(request("level", drone_id="drone-2"))
    assert sent == [] and runner.detection_profile == "default"


def test_a_repeated_cmd_id_gets_the_same_reply_without_a_second_load():
    loads = []

    def counting(profile):
        loads.append(profile.name)
        return fake_loader(profile)

    switcher, _, sent, _ = make_switcher(loader=counting)
    switcher.handle(request("level"))
    switcher.handle(request("level"))
    assert loads == ["default", "level"]
    assert sent[0] == sent[1]


def test_a_second_switch_while_one_loads_is_refused_as_busy():
    gate = threading.Event()
    started = threading.Event()

    def slow(profile):
        started.set()
        gate.wait(5)
        return fake_loader(profile)

    switcher, runner, sent, _ = make_switcher()
    switcher._loader = slow
    switcher._run_async = True
    switcher.handle(request("level", cmd_id="c1"))
    assert started.wait(5)
    switcher.handle(request("top_down", cmd_id="c2"))
    switcher.handle(request("level", cmd_id="c1"))  # a retry of the pending command: no reply yet
    assert [m["reason"] for m in sent] == ["busy"]
    gate.set()
    assert switcher.wait(5)
    assert sent[-1]["type"] == "ack" and runner.detection_profile == "level"


def test_handle_never_raises_even_when_sending_fails():
    switcher, runner, _, log = make_switcher()

    def boom(msg):
        raise ConnectionError("link down")

    switcher.bind_sender(boom)
    switcher.handle(request("level"))
    switcher.handle("not a dict")  # type: ignore[arg-type]
    assert runner.detection_profile == "level"
    assert log.events("profile_reply_send_error")


# ---------------------------------------------------------------------------
# Edge device wiring
# ---------------------------------------------------------------------------


def test_edge_control_routes_profile_commands_and_never_lands():
    _switcher, runner, sent, _ = make_switcher(is_active=lambda: True)
    flight = MagicMock()
    edge_device._handle_control(request("level"), runner, RecordingLog(), flight=flight)
    assert sent[-1]["reason"] == "in_flight"
    flight.fail_safe.assert_not_called()
    flight.handle_message.assert_not_called()
    flight.note_ground_message.assert_called_once()


def test_edge_without_a_switcher_ignores_profile_commands():
    runner = EdgeRunner()
    log = RecordingLog()
    flight = MagicMock()
    edge_device._handle_control(request("level"), runner, log, flight=flight)
    assert log.events("edge_control_profile_unsupported")
    flight.fail_safe.assert_not_called()


def test_telemetry_reports_the_active_profile_and_stays_valid():
    _switcher, runner, _, _ = make_switcher(exists=no_heridal, start="top_down")
    out = []
    send = edge_device._with_profile_status(out.append, runner)
    telemetry = {"type": "telemetry", **{key: None for key in fp.TELEMETRY_FIELDS},
                 "drone_id": "drone-1", "battery": 80, "is_flying": False}
    send(telemetry)
    send({"type": "ack", "for": "takeoff", "drone_id": "drone-1"})
    assert out[0]["detection_profile"] == "level" and out[0]["detection_profile_fallback"] is True
    assert out[0]["detection_profile_requested"] == "top_down"
    assert "detection_profile" not in out[1]
    assert "detection_profile" not in telemetry  # the executor's own message is not changed
    fp.validate_message(out[0], proto=1)
    v2 = EdgeAuthority("drone-1", "edge-node").outbound(out[0])
    fp.validate_message(v2, proto=2)


def test_detection_profile_flag_offers_every_registered_profile():
    parser = edge_device._build_parser()
    args = parser.parse_args(["--source", "fake", "--detection-profile", "top_down"])
    assert edge_device._startup_profile_name(args) == "top_down"
    with pytest.raises(SystemExit):
        parser.parse_args(["--detection-profile", "nadir"])


def test_startup_profile_comes_from_the_environment_when_no_flag(monkeypatch):
    from flightrisk.config import reset_config

    monkeypatch.setenv("FLIGHTRISK_DETECTION_PROFILE", "level")
    reset_config()
    try:
        args = edge_device._build_parser().parse_args(["--source", "fake"])
        assert edge_device._startup_profile_name(args) == "level"
    finally:
        monkeypatch.delenv("FLIGHTRISK_DETECTION_PROFILE")
        reset_config()


def test_the_real_lane_executor_state_gates_the_switch():
    from flightrisk.drone.lane_executor import LaneExecutor
    from flightrisk.drone.safety import SafetyEnvelope
    from flightrisk.drone.tello import FakeTelloController

    drone = FakeTelloController(name="drone-1", battery=90)
    drone.connect()
    executor = LaneExecutor(drone, SafetyEnvelope(alt_slot_cm=100), "drone-1", send=lambda msg: None)
    try:
        switcher, runner, sent, _ = make_switcher()
        edge_device._bind_profile_switcher(runner, MagicMock(), RecordingLog(), executor)
        switcher.bind_sender(sent.append)
        drone.state.is_flying = True
        switcher.handle(request("level", cmd_id="c1"))
        assert sent[-1]["reason"] == "in_flight" and runner.detection_profile == "default"
        drone.state.is_flying = False
        switcher.handle(request("level", cmd_id="c2"))
        assert sent[-1]["type"] == "ack" and runner.detection_profile == "level"
    finally:
        executor.shutdown()
        drone.disconnect()
