"""Tests for fleet protocol v2 (two-laptop plan, PR-3).

Covers the new messages (``hello``, ``hold``, ``resume``, ``nudge``,
``handover``, generic ``ack``/``nack`` and the ground-to-ground peer
messages), epoch fencing, the fail-safe exemption for landing, version
negotiation, v1 compatibility, size caps and the privacy key check.

The property-style tests use seeded ``random`` sweeps (``hypothesis`` is not
a project dependency): round trips through ``encode``/``decode`` and fuzzed
malformed input that must never raise anything except ``ProtocolError``.
Everything here is pure; no socket, drone or file is touched except the
protocol reference doc, which is read to check it lists every message type.
"""

from __future__ import annotations

import copy
import json
import random
import re
from pathlib import Path

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone.lane_executor import CONTROL_TYPES

DOC = Path(__file__).resolve().parent.parent / "docs" / "fleet-protocol-v2.md"

FENCE = fp.Fence(7, "mac")
CMD = "c0ffee00c0ffee00c0ffee00c0ffee00"
LANES = [{"direction": "forward", "distance_cm": 200}, {"direction": "right", "distance_cm": 75}]
ZONE = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}

G2E = fp.DIRECTION_GROUND_TO_EDGE
E2G = fp.DIRECTION_EDGE_TO_GROUND
PEER = fp.DIRECTION_PEER


def node_state(**overrides):
    """The ``node_state`` example from the plan (section 2.10)."""
    msg = fp.make_node_state(
        "mac",
        "leader",
        7,
        roster=[{"drone_id": "drone-1", "node_id": "mac", "connected": True, "is_flying": False,
                 "epoch_seen": 7}],
        mission={"plan_id": "p-1", "pattern": "split_expanding_square", "state": "idle",
                 "area_cm": [2000, 2000], "lkp_cm": [0, 0],
                 "placements": {"drone-1": [-50, 0, 0], "drone-2": [50, 0, 0]}},
        latch={"phase": "searching", "alert_id": None},
        detections=[{"id": 3, "x_cm": 120, "y_cm": -40, "drone_ids": ["drone-2"], "first_seen_s": 81.2}],
    )
    msg.update(overrides)
    return msg


def v2_ground_to_edge():
    return [
        fp.make_ground_hello("mac", "leader", 7),
        fp.make_ground_hello("win", "standby", 0),
        fp.make_ping(1.5),
        fp.make_clock(0.25, fence=FENCE),
        fp.make_heartbeat(12, fence=FENCE),
        fp.make_assign_lanes("drone-2", 160, LANES, ZONE, fence=FENCE, plan_id="p-1", plan_seq=1,
                             on_complete="hover"),
        fp.make_assign_lanes("drone-2", 160, LANES, ZONE, fence=FENCE, plan_id="p-1", plan_seq=1,
                             on_complete="land"),
        fp.make_start_at(123.4, fence=FENCE, plan_id="p-1"),
        fp.make_takeoff(fence=FENCE),
        fp.make_hold(FENCE, reason="alert"),
        fp.make_hold(FENCE),
        fp.make_resume(FENCE, plan_id="p-1"),
        fp.make_resume(FENCE, plan_id="p-2", cmd_id=CMD),
        fp.make_nudge(FENCE, "left", 50),
        fp.make_handover(FENCE, "win"),
        fp.make_land(cmd_id=CMD),
        fp.make_land(),
        fp.make_emergency_land_all(),
    ]


def v2_edge_to_ground():
    return [
        fp.make_edge_hello("drone-2", "win", 7, plan_id="p-1", lane_index=3, is_flying=False, battery=71),
        fp.make_edge_hello("drone-2", "win", 0, plan_id=None, lane_index=None, is_flying=False, battery=None),
        fp.make_pong(1.0, 2.0, 2.5),
        fp.make_telemetry("drone-2", 71, 160, True, 10.0, -20.0, 0.0, 1, 4, 0.2, None,
                          v2=fp.TelemetryV2(7, "p-1", "flying_lanes", True)),
        fp.make_telemetry("drone-2", None, None, False, None, None, None, None, None, None, None,
                          v2=fp.TelemetryV2(0, None, "idle", False)),
        fp.make_ack("hold", "drone-2", CMD),
        fp.make_ack("land", "drone-2", None),
        fp.make_ack_start_at("drone-2", 42.0, cmd_id=CMD),
        fp.make_ack_assign_lanes("drone-2", 80, 2, cmd_id=CMD),
        fp.make_nack("resume", "drone-2", "no_valid_plan", CMD),
        fp.make_nack("takeoff", "drone-2", "stale_epoch", CMD, epoch_seen=8),
        fp.make_nack("assign_lanes", "drone-2", "busy", CMD, lanes_total=2, detail="worker running"),
    ]


def peer_messages():
    return [
        node_state(),
        fp.make_node_state("win", "standby", 7),
        fp.make_handover_offer(8, node_state()),
        fp.make_handover_accept(8),
        fp.make_handover_reject("drone-1 flying"),
        fp.make_handover_reject("drone-1 flying", epoch_next=8),
        fp.make_peer_heartbeat(4, 7, node_id="mac"),
    ]


def v1_messages():
    """Every v1 builder output, and the literal dicts the v1 lane executor emits."""
    return [
        fp.make_ping(1.5),
        fp.make_pong(1.5, 2.0, 2.25),
        fp.make_clock(-4000.0),
        fp.make_start_at(12.5),
        fp.make_takeoff(),
        fp.make_land(),
        fp.make_emergency_land_all(),
        fp.make_heartbeat(3),
        fp.make_assign_lanes("drone-1", 100, LANES, ZONE),
        fp.make_telemetry("drone-1", 87, 160, True, 10.0, -20.0, 90.0, 1, 4, 0.2, None),
        fp.make_ack_start_at("drone-1", 42.0),
        fp.make_ack_assign_lanes("drone-1", 80, 4),
        fp.make_nack_assign_lanes("drone-1", "geofence", 3),
        {"type": "nack", "for": "assign_lanes", "drone_id": "drone-1", "reason": "other_drone", "lanes_total": 0},
        {"type": "assign_lanes", "drone_id": "drone-1", "alt_cm": 100, "lanes": LANES, "zone": ZONE},
    ]


# ===========================================================================
# Schemas
# ===========================================================================


def test_v2_builders_produce_plan_schemas():
    assert fp.make_ground_hello("mac", "leader", 7) == {
        "type": "hello", "role": "leader", "node_id": "mac", "epoch": 7, "proto": 2,
    }
    assert fp.make_heartbeat(12, fence=FENCE) == {"type": "heartbeat", "seq": 12, "epoch": 7, "leader_id": "mac"}
    assert fp.make_hold(FENCE, cmd_id=CMD, reason="alert") == {
        "type": "hold", "reason": "alert", "epoch": 7, "leader_id": "mac", "cmd_id": CMD,
    }
    assert fp.make_resume(FENCE, cmd_id=CMD, plan_id="p-1") == {
        "type": "resume", "plan_id": "p-1", "epoch": 7, "leader_id": "mac", "cmd_id": CMD,
    }
    assert fp.make_nudge(FENCE, "left", 50, cmd_id=CMD) == {
        "type": "nudge", "direction": "left", "distance_cm": 50, "epoch": 7, "leader_id": "mac", "cmd_id": CMD,
    }
    assert fp.make_handover(FENCE, "win", cmd_id=CMD) == {
        "type": "handover", "to_node": "win", "epoch_next": 8, "epoch": 7, "leader_id": "mac", "cmd_id": CMD,
    }
    assert fp.make_start_at(123.4, fence=FENCE, cmd_id=CMD, plan_id="p-1") == {
        "type": "start_at", "t_ground_s": 123.4, "plan_id": "p-1", "epoch": 7, "leader_id": "mac", "cmd_id": CMD,
    }
    assert fp.make_assign_lanes(
        "drone-2", 160, LANES, ZONE, fence=FENCE, cmd_id=CMD, plan_id="p-1", plan_seq=3, on_complete="hover",
    ) == {
        "type": "assign_lanes", "drone_id": "drone-2", "alt_cm": 160, "lanes": LANES, "zone": ZONE,
        "plan_id": "p-1", "plan_seq": 3, "on_complete": "hover", "hover_timeout_s": fp.DEFAULT_HOVER_TIMEOUT_S,
        "epoch": 7, "leader_id": "mac", "cmd_id": CMD,
    }
    assert fp.make_land(cmd_id=CMD) == {"type": "land", "cmd_id": CMD}
    assert fp.make_edge_hello("drone-2", "win", 7, plan_id="p-1", lane_index=3, is_flying=False, battery=71) == {
        "type": "hello", "role": "edge", "drone_id": "drone-2", "node_id": "win", "proto": 2,
        "epoch_seen": 7, "plan_id": "p-1", "lane_index": 3, "is_flying": False, "battery": 71,
    }
    assert fp.make_ack("hold", "drone-2", CMD) == {
        "type": "ack", "for": "hold", "drone_id": "drone-2", "result": "ok", "cmd_id": CMD,
    }
    assert fp.make_nack("resume", "drone-2", "no_valid_plan", CMD) == {
        "type": "nack", "for": "resume", "drone_id": "drone-2", "cmd_id": CMD, "reason": "no_valid_plan",
    }
    telemetry = fp.make_telemetry("drone-2", 71, 160, True, 0, 0, 0, 0, 1, 0.1, None,
                                  v2=fp.TelemetryV2(7, "p-1", "holding", True))
    assert set(telemetry) == {"type", *fp.TELEMETRY_FIELDS_V2}
    assert (telemetry["epoch"], telemetry["plan_id"], telemetry["state"], telemetry["plan_valid"]) == (
        7, "p-1", "holding", True)


def test_v1_builders_are_unchanged_without_a_fence():
    assert fp.make_start_at(12.5) == {"type": "start_at", "t_ground_s": 12.5}
    assert fp.make_takeoff() == {"type": "takeoff"}
    assert fp.make_land() == {"type": "land"}
    assert fp.make_heartbeat(3) == {"type": "heartbeat", "seq": 3}
    assert fp.make_clock(0.5) == {"type": "clock", "offset_s": 0.5}
    assert fp.GROUND_TO_EDGE_TYPES == {
        "takeoff", "land", "emergency_land_all", "assign_lanes", "start_at", "ping", "clock", "heartbeat",
    }
    assert fp.EDGE_TO_GROUND_TYPES == {"pong", "ack", "nack", "telemetry"}


def test_v2_type_sets():
    assert fp.GROUND_TO_EDGE_TYPES_V2 == fp.GROUND_TO_EDGE_TYPES | {"hello", "hold", "resume", "nudge", "handover"}
    assert fp.EDGE_TO_GROUND_TYPES_V2 == fp.EDGE_TO_GROUND_TYPES | {"hello"}
    assert fp.FAILSAFE_TYPES == {"land", "emergency_land_all"}
    assert not fp.FAILSAFE_TYPES & (fp.FENCED_COMMAND_TYPES | fp.FENCED_CONTROL_TYPES)
    # Everything that can move a drone is fenced.
    assert {"takeoff", "assign_lanes", "start_at", "hold", "resume", "nudge", "handover"} == fp.FENCED_COMMAND_TYPES
    # Section 2.9: takeoff, start_at and nudge are never retried automatically.
    assert not {"takeoff", "start_at", "nudge"} & fp.RETRYABLE_COMMAND_TYPES
    assert fp.FAILSAFE_TYPES <= fp.RETRYABLE_COMMAND_TYPES
    assert fp.NACK_REASONS < fp.NACK_REASONS_V2
    assert fp.CMD_ID_CACHE_SIZE == 256


def test_v1_lane_executor_handles_exactly_the_v1_set():
    # A v1 edge silently ignores any other type, so a v2-only command sent to
    # it would be lost; shape_for_peer refuses to do that.
    assert CONTROL_TYPES == fp.GROUND_TO_EDGE_TYPES
    for mtype in fp.V2_ONLY_COMMAND_TYPES:
        assert mtype not in CONTROL_TYPES


@pytest.mark.parametrize("msg", v2_ground_to_edge(), ids=lambda m: m["type"])
def test_v2_ground_to_edge_messages_validate(msg):
    assert fp.validate_ground_to_edge(msg, proto=2) == msg["type"]


@pytest.mark.parametrize("msg", v2_edge_to_ground(), ids=lambda m: m["type"])
def test_v2_edge_to_ground_messages_validate(msg):
    assert fp.validate_edge_to_ground(msg, proto=2) == msg["type"]


@pytest.mark.parametrize("msg", peer_messages(), ids=lambda m: m["type"])
def test_peer_messages_validate(msg):
    assert fp.validate_peer_message(msg) == msg["type"]


def test_cmd_ids_are_fresh_uuid4_hex_and_reused_on_retry():
    ids = {fp.new_cmd_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(re.fullmatch(r"[0-9a-f]{32}", value) for value in ids)
    first = fp.make_hold(FENCE)
    retry = fp.make_hold(FENCE, cmd_id=first["cmd_id"])
    assert first == retry
    assert fp.make_hold(FENCE)["cmd_id"] != first["cmd_id"]


# ===========================================================================
# Fencing
# ===========================================================================

FENCED_V2 = [m for m in v2_ground_to_edge() if m["type"] in fp.FENCED_COMMAND_TYPES]
CONTROL_V2 = [m for m in v2_ground_to_edge() if m["type"] in fp.FENCED_CONTROL_TYPES]


@pytest.mark.parametrize("msg", FENCED_V2, ids=lambda m: m["type"])
@pytest.mark.parametrize("key", ["epoch", "leader_id", "cmd_id"])
def test_v2_fenced_commands_need_epoch_leader_and_cmd_id(msg, key):
    broken = {k: v for k, v in msg.items() if k != key}
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(broken, proto=2)
    with pytest.raises(fp.ProtocolError):
        fp.validate_ground_to_edge(broken, proto=2)
    if msg["type"] in fp.V2_ONLY_COMMAND_TYPES:
        # A v2-only command is fenced whatever the link version.
        with pytest.raises(fp.ProtocolError):
            fp.validate_message(broken, proto=1)


@pytest.mark.parametrize("msg", CONTROL_V2, ids=lambda m: m["type"])
@pytest.mark.parametrize("key", ["epoch", "leader_id"])
def test_v2_heartbeat_and_clock_need_a_fence(msg, key):
    broken = {k: v for k, v in msg.items() if k != key}
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(broken, proto=2)


@pytest.mark.parametrize("msg", [m for m in v1_messages() if m["type"] in fp.FENCED_COMMAND_TYPES
                                 | fp.FENCED_CONTROL_TYPES], ids=lambda m: m["type"])
def test_unfenced_v1_motion_is_refused_on_a_v2_link(msg):
    assert fp.validate_message(msg, proto=1) == msg["type"]
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(msg, proto=2)


def test_fence_fields_travel_as_a_pair_under_every_version():
    for proto in (1, 2):
        with pytest.raises(fp.ProtocolError):
            fp.validate_message({"type": "takeoff", "epoch": 7}, proto=proto)
        with pytest.raises(fp.ProtocolError):
            fp.validate_message({"type": "takeoff", "leader_id": "mac"}, proto=proto)
    # Present v2 fields are checked on a v1 link too.
    with pytest.raises(fp.ProtocolError):
        fp.validate_message({"type": "takeoff", "epoch": 0, "leader_id": "mac"}, proto=1)
    with pytest.raises(fp.ProtocolError):
        fp.validate_message({"type": "start_at", "t_ground_s": 1.0, "cmd_id": "has space"}, proto=1)


@pytest.mark.parametrize("epoch", [0, -1, True, 7.0, "7", None, [7], fp.MAX_EPOCH + 1, 10**400])
def test_bad_epochs_are_rejected(epoch):
    msg = dict(fp.make_hold(FENCE), epoch=epoch)
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(msg, proto=2)
    with pytest.raises(fp.ProtocolError):
        fp.Fence(epoch, "mac")


@pytest.mark.parametrize("ident", ["", " mac", "mac\n", "-mac", "a" * 65, "mäc", 7, None, ["mac"]])
def test_bad_identifiers_are_rejected(ident):
    for key in ("leader_id", "cmd_id"):
        msg = dict(fp.make_resume(FENCE, plan_id="p-1"), **{key: ident})
        with pytest.raises(fp.ProtocolError):
            fp.validate_message(msg, proto=2)


def test_epoch_bounds_accepted():
    for epoch in (1, fp.MAX_EPOCH):
        fp.validate_message(fp.make_hold(fp.Fence(epoch, "mac")), proto=2)


def test_fence_of_and_cmd_id_of_never_raise():
    assert fp.fence_of(fp.make_hold(FENCE)) == FENCE
    assert fp.cmd_id_of(fp.make_hold(FENCE, cmd_id=CMD)) == CMD
    for junk in (None, [], "land", {"epoch": [1], "leader_id": {}}, {"epoch": 0, "leader_id": "mac"},
                 {"cmd_id": ["x"]}, {"epoch": 10**400, "leader_id": "m"}):
        assert fp.fence_of(junk) is None
        assert fp.cmd_id_of(junk) is None


# ===========================================================================
# Fail-safe landing
# ===========================================================================

GARBAGE_LANDS = [
    {"type": "land"},
    {"type": "land", "epoch": "stale", "leader_id": None, "cmd_id": ["x"]},
    {"type": "land", "epoch": 1, "leader_id": "old-leader", "cmd_id": CMD},
    {"type": "land", "epoch": -5},
    {"type": "land", "face_embedding": [0.1] * 4, "nested": {"a": [[[[[[[[[[1]]]]]]]]]]}},
    {"type": "emergency_land_all"},
    {"type": "emergency_land_all", "epoch": 0, "cmd_id": 3, **{f"f{i}": i for i in range(100)}},
]


@pytest.mark.parametrize("msg", GARBAGE_LANDS)
def test_landing_is_accepted_whatever_else_it_carries(msg):
    for proto in (1, 2):
        assert fp.validate_message(msg, proto=proto) == msg["type"]
        assert fp.validate_ground_to_edge(msg, proto=proto) == msg["type"]
        assert fp.shape_for_peer(msg, proto) == msg
        assert fp.decode(json.dumps(msg), direction=G2E, proto=proto) == msg


def test_land_from_a_stale_epoch_is_still_valid():
    stale = fp.make_land(cmd_id=CMD, fence=fp.Fence(1, "old"))
    assert fp.validate_ground_to_edge(stale, proto=2) == "land"
    assert fp.fence_of(stale) == fp.Fence(1, "old")


def test_landing_is_never_nacked_but_may_be_acked():
    for target in ("land", "emergency_land_all"):
        with pytest.raises(fp.ProtocolError):
            fp.make_nack(target, "drone-1", "busy", CMD)
        assert fp.validate_message(fp.make_ack(target, "drone-1", None), proto=2) == "ack"


# ===========================================================================
# Field rules for the new messages
# ===========================================================================


@pytest.mark.parametrize("direction", ["forward", "back", "left", "right"])
@pytest.mark.parametrize("distance", [fp.NUDGE_MIN_CM, 50, fp.NUDGE_MAX_CM])
def test_nudge_accepts_horizontal_moves_in_range(direction, distance):
    assert fp.validate_message(fp.make_nudge(FENCE, direction, distance), proto=2) == "nudge"


@pytest.mark.parametrize("direction, distance", [
    ("up", 50), ("down", 50), ("cw", 90), ("ccw", 90), ("LEFT", 50), (["left"], 50), (None, 50),
    ("left", 19), ("left", 501), ("left", 0), ("left", -50), ("left", 50.0), ("left", True), ("left", "50"),
    ("left", 10**400),
])
def test_nudge_rejects_vertical_rotation_and_out_of_range(direction, distance):
    with pytest.raises(fp.ProtocolError):
        fp.make_nudge(FENCE, direction, distance)
    with pytest.raises(fp.ProtocolError):
        fp.validate_message({"type": "nudge", "direction": direction, "distance_cm": distance,
                             **FENCE.fields(), "cmd_id": CMD}, proto=2)


def test_hold_reason_is_optional_but_checked():
    with pytest.raises(fp.ProtocolError):
        fp.make_hold(FENCE, reason="because")
    for reason in fp.HOLD_REASONS:
        fp.make_hold(FENCE, reason=reason)


def test_handover_rules():
    with pytest.raises(fp.ProtocolError):
        fp.make_handover(FENCE, "win", epoch_next=7)
    with pytest.raises(fp.ProtocolError):
        fp.make_handover(FENCE, "mac")
    with pytest.raises(fp.ProtocolError):
        fp.make_handover(FENCE, "")
    assert fp.make_handover(FENCE, "win", epoch_next=8)["epoch_next"] == 8
    with pytest.raises(fp.ProtocolError):
        fp.make_handover(FENCE, "win", epoch_next=9)  # must be exactly epoch + 1, as in handover_offer
    with pytest.raises(fp.ProtocolError):
        fp.make_hold(None)


def test_assign_lanes_v2_rules():
    with pytest.raises(fp.ProtocolError):
        fp.make_assign_lanes("drone-1", 100, LANES, ZONE, fence=FENCE, plan_seq=1)  # no plan_id
    with pytest.raises(fp.ProtocolError):
        fp.make_assign_lanes("drone-1", 100, LANES, ZONE, fence=FENCE, plan_id="p", plan_seq=1, on_complete="return")
    with pytest.raises(fp.ProtocolError):
        fp.make_assign_lanes("drone-1", 100, LANES, ZONE, fence=FENCE, plan_id="p", plan_seq=1, on_complete="land",
                             hover_timeout_s=5)
    for timeout in (0, -1, fp.MAX_HOVER_TIMEOUT_S + 1, float("nan")):
        with pytest.raises(fp.ProtocolError):
            fp.make_assign_lanes("drone-1", 100, LANES, ZONE, fence=FENCE, plan_id="p", plan_seq=1, on_complete="hover",
                                 hover_timeout_s=timeout)
    msg = fp.make_assign_lanes("drone-1", 100, LANES, ZONE, fence=FENCE, plan_id="p", plan_seq=1, on_complete="hover",
                               hover_timeout_s=fp.MAX_HOVER_TIMEOUT_S)
    assert msg["hover_timeout_s"] == fp.MAX_HOVER_TIMEOUT_S
    with pytest.raises(fp.ProtocolError):
        fp.make_start_at(1.0, fence=FENCE)  # no plan_id


def test_telemetry_v2_rules():
    base = fp.make_telemetry("drone-2", 71, 160, True, 0, 0, 0, 0, 1, 0.1, None,
                             v2=fp.TelemetryV2(7, None, "hovering", False))
    for key in ("epoch", "plan_id", "state", "plan_valid"):
        broken = {k: v for k, v in base.items() if k != key}
        with pytest.raises(fp.ProtocolError):
            fp.validate_message(broken, proto=2)
        # A v1 ground does not need them.
        assert fp.validate_message(broken, proto=1) == "telemetry"
    for key, value in (("state", "dancing"), ("plan_valid", 1), ("epoch", -1), ("plan_id", "")):
        with pytest.raises(fp.ProtocolError):
            fp.validate_message(dict(base, **{key: value}), proto=2)


def test_ack_and_nack_v2_rules():
    with pytest.raises(fp.ProtocolError):
        fp.validate_message({k: v for k, v in fp.make_ack("hold", "d", CMD).items() if k != "result"}, proto=2)
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(dict(fp.make_ack("hold", "d", CMD), result="fail"), proto=2)
    with pytest.raises(fp.ProtocolError):
        fp.make_ack("hold", "d", None)  # a fenced command's ack needs its cmd_id
    with pytest.raises(fp.ProtocolError):
        fp.make_ack("ping", "d", CMD)
    with pytest.raises(fp.ProtocolError):
        fp.make_ack("start_at", "d", CMD)  # executed_at_ground_s is still required
    with pytest.raises(fp.ProtocolError):
        fp.make_nack("assign_lanes", "d", "busy", CMD)  # lanes_total is still required
    with pytest.raises(fp.ProtocolError):
        fp.make_nack("hold", "d", "tired", CMD)
    with pytest.raises(fp.ProtocolError):
        fp.make_nack("hold", "d", "busy", None)
    with pytest.raises(fp.ProtocolError):
        fp.make_nack("hold", "d", "busy", CMD, detail="x" * (fp.MAX_TEXT_LEN + 1))
    with pytest.raises(fp.ProtocolError):
        fp.make_nack("hold", "d", "busy", CMD, detail="line\nbreak")


def test_v2_nacks_are_refused_on_a_v1_link():
    # v1 rule kept: a v1 ground only understands lane-assignment nacks with v1 reasons.
    nack = fp.make_nack("resume", "drone-2", "no_valid_plan", CMD)
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(nack, proto=1)
    busy = fp.make_nack("assign_lanes", "drone-2", "busy", CMD, lanes_total=1)
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(busy, proto=1)
    v1_reason = fp.make_nack("assign_lanes", "drone-2", "geofence", CMD, lanes_total=1)
    assert fp.validate_message(v1_reason, proto=1) == "nack"


def test_hello_rules():
    with pytest.raises(fp.ProtocolError):
        fp.make_ground_hello("mac", "leader", 0)  # a leader holds an epoch >= 1
    assert fp.make_ground_hello("win", "standby", 0)["epoch"] == 0
    with pytest.raises(fp.ProtocolError):
        fp.make_ground_hello("mac", "pilot", 1)
    for proto in (0, -1, 256, True, 2.0, "2"):
        with pytest.raises(fp.ProtocolError):
            fp.validate_message(dict(fp.make_ground_hello("mac", "leader", 7), proto=proto))
    ground = fp.make_ground_hello("mac", "leader", 7)
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(dict(ground, drone_id="drone-1"))
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(dict(ground, leader_id="win"))
    assert fp.validate_message(dict(ground, leader_id="mac")) == "hello"
    edge = fp.make_edge_hello("drone-2", "win", 7, plan_id=None, lane_index=None, is_flying=True, battery=50)
    for key, value in (("battery", 101), ("battery", -1), ("is_flying", "yes"), ("epoch_seen", -1),
                       ("role", "pilot"), ("lane_index", -1)):
        with pytest.raises(fp.ProtocolError):
            fp.validate_message(dict(edge, **{key: value}))
    # The plan's edge hello carries no role; it is still an edge hello.
    assert fp.validate_edge_to_ground({k: v for k, v in edge.items() if k != "role"}, proto=1) == "hello"
    # Each direction only accepts its own form.
    with pytest.raises(fp.ProtocolError):
        fp.validate_ground_to_edge(edge, proto=2)
    with pytest.raises(fp.ProtocolError):
        fp.validate_edge_to_ground(ground, proto=2)


def test_parse_hello():
    ground = fp.parse_hello(fp.make_ground_hello("mac", "leader", 7))
    assert ground == fp.GroundHello("mac", "leader", 7, 2)
    assert ground.to_message() == fp.make_ground_hello("mac", "leader", 7)
    edge = fp.parse_hello(fp.make_edge_hello("drone-2", "win", 7, plan_id="p-1", lane_index=3,
                                             is_flying=False, battery=71))
    assert isinstance(edge, fp.EdgeHello)
    assert (edge.drone_id, edge.epoch_seen, edge.plan_id, edge.proto) == ("drone-2", 7, "p-1", 2)
    with pytest.raises(fp.ProtocolError):
        fp.parse_hello(fp.make_ping(1.0))
    with pytest.raises(fp.ProtocolError):
        fp.parse_hello({"type": "hello", "role": "leader"})


def test_peer_message_rules():
    with pytest.raises(fp.ProtocolError):
        fp.make_handover_offer(9, node_state())  # must offer exactly epoch + 1
    with pytest.raises(fp.ProtocolError):
        fp.make_handover_offer(8, node_state(role="standby"))
    with pytest.raises(fp.ProtocolError):
        fp.make_handover_offer(8, dict(node_state(), type="telemetry"))
    with pytest.raises(fp.ProtocolError):
        fp.make_handover_accept(1)
    with pytest.raises(fp.ProtocolError):
        fp.make_handover_reject("")
    roster = [{"drone_id": f"drone-{i}", "node_id": "mac", "connected": True, "is_flying": False,
               "epoch_seen": 7} for i in range(fp.MAX_ROSTER + 1)]
    with pytest.raises(fp.ProtocolError):
        fp.make_node_state("mac", "leader", 7, roster=roster)
    det = {"id": 1, "x_cm": 0, "y_cm": 0, "drone_ids": ["drone-1"], "first_seen_s": 1.0}
    with pytest.raises(fp.ProtocolError):
        fp.make_node_state("mac", "leader", 7, detections=[det] * (fp.MAX_DETECTIONS + 1))
    mission = node_state()["mission"]
    for key, value in (("state", "flying"), ("area_cm", [0, 10]), ("area_cm", [10]), ("lkp_cm", "here"),
                       ("placements", {"drone-1": [0, 0]})):
        with pytest.raises(fp.ProtocolError):
            fp.validate_peer_message(dict(node_state(), mission=dict(mission, **{key: value})))
    with pytest.raises(fp.ProtocolError):
        fp.validate_peer_message(dict(node_state(), latch={"phase": "panic", "alert_id": None}))
    assert fp.validate_peer_message(dict(node_state(), latch={"phase": "awaiting_response", "alert_id": "a-3"}))


# ===========================================================================
# Directions, unknown commands and version negotiation
# ===========================================================================


def test_strict_validators_reject_unknown_and_misdirected_types():
    for msg in ({"type": "warp_drive"}, {"type": "rotate", **FENCE.fields(), "cmd_id": CMD},
                {"type": "rc_control"}, {"type": "emergency"}, {"type": "telemetry"}, {"type": "auth"}):
        with pytest.raises(fp.ProtocolError):
            fp.validate_ground_to_edge(msg, proto=2)
    # The lenient v1 entry point still passes unknown types through.
    assert fp.validate_message({"type": "warp_drive"}) == "warp_drive"
    # v2-only commands are not ground-to-edge types on a v1 link.
    with pytest.raises(fp.ProtocolError):
        fp.validate_ground_to_edge(fp.make_hold(FENCE), proto=1)
    for msg in (fp.make_heartbeat(1), fp.make_land(), {"type": "warp_drive"}, node_state()):
        with pytest.raises(fp.ProtocolError):
            fp.validate_edge_to_ground(msg, proto=2)
    for msg in (fp.make_heartbeat(1), fp.make_ground_hello("mac", "leader", 7), {"type": "land"}):
        with pytest.raises(fp.ProtocolError):
            fp.validate_peer_message(msg)
    with pytest.raises(fp.ProtocolError):
        fp.validate_for_direction(fp.make_land(), "sideways", proto=2)


def test_legacy_messages_pass_the_strict_validators_by_type_only():
    # The existing opt-in rules for embeddings live with these messages' own
    # handlers; this module neither adds to nor inspects them.
    target = {"type": "set_target", "reid_embedding": [0.1, 0.2], "face_embedding": None}
    assert fp.validate_ground_to_edge(target, proto=2) == "set_target"
    assert fp.validate_ground_to_edge({"type": "stream_video", "enabled": True}, proto=1) == "stream_video"
    assert fp.validate_edge_to_ground({"type": "detections", "drone_id": "d"}, proto=2) == "detections"


def test_negotiate_proto():
    assert fp.negotiate_proto(1) == 1
    assert fp.negotiate_proto(2) == 2
    assert fp.negotiate_proto(3) == 2  # a newer peer talks our version
    assert fp.negotiate_proto(2, local_max=1) == 1
    assert fp.negotiate_proto(2, current=2) == 2
    for bad in (0, -1, 256, True, 2.0, "2", None):
        with pytest.raises(fp.ProtocolError):
            fp.negotiate_proto(bad)
    with pytest.raises(fp.ProtocolError):
        fp.negotiate_proto(1, current=2)  # no downgrade on a live connection


def test_shape_for_peer_keeps_v1_peers_on_the_v1_flow():
    for msg in v2_ground_to_edge() + v2_edge_to_ground():
        mtype = msg["type"]
        if mtype in fp.V2_ONLY_COMMAND_TYPES:
            with pytest.raises(fp.ProtocolError):
                fp.shape_for_peer(msg, 1)
            continue
        if mtype == "nack" and (msg["for"] != "assign_lanes" or msg["reason"] not in fp.NACK_REASONS):
            with pytest.raises(fp.ProtocolError):
                fp.shape_for_peer(msg, 1)
            continue
        shaped = fp.shape_for_peer(msg, 1)
        if mtype in fp.FAILSAFE_TYPES or mtype == "hello":
            assert shaped == msg
            continue
        assert not set(shaped) & fp.V2_FIELDS
        assert fp.validate_message(shaped, proto=1) == mtype
        assert shaped == {k: v for k, v in msg.items() if k not in fp.V2_FIELDS}
        assert fp.shape_for_peer(msg, 2) == msg
    for msg in peer_messages():
        with pytest.raises(fp.ProtocolError):
            fp.shape_for_peer(msg, 1)
    with pytest.raises(fp.ProtocolError):
        fp.shape_for_peer(fp.make_takeoff(), 2)  # unfenced motion cannot go to a v2 peer
    with pytest.raises(fp.ProtocolError):
        fp.shape_for_peer(fp.make_land(), 0)
    with pytest.raises(fp.ProtocolError):
        fp.shape_for_peer({"type": "warp_drive", "epoch": 7}, 1)
    target = {"type": "set_target", "reid_embedding": [0.5], "state": "kept"}
    assert fp.shape_for_peer(target, 1) == target


# ===========================================================================
# v1 compatibility
# ===========================================================================


@pytest.mark.parametrize("msg", v1_messages(), ids=lambda m: m["type"])
def test_v1_messages_still_validate_on_a_v1_link(msg):
    assert fp.validate_message(msg) == msg["type"]
    assert fp.validate_message(msg, proto=1) == msg["type"]
    direction = G2E if msg["type"] in fp.GROUND_TO_EDGE_TYPES else E2G
    assert fp.validate_for_direction(msg, direction, proto=1) == msg["type"]
    assert fp.decode(fp.encode(msg, direction=direction, proto=1), direction=direction, proto=1) == msg


def test_v2_edge_reports_are_accepted_by_a_v1_ground():
    # A v2 edge talking to a v1 ground: telemetry and the ack forms a v1
    # ground knows are supersets of v1 and pass the v1 rules unchanged.
    for msg in v2_edge_to_ground():
        if msg["type"] in ("telemetry", "pong") or (msg["type"] == "ack" and msg["for"] in ("start_at",
                                                                                           "assign_lanes")):
            assert fp.validate_message(msg, proto=1) == msg["type"]


def test_v1_validation_bugs_now_raise_protocol_error():
    # Before v2 these raised TypeError / OverflowError out of validate_message.
    with pytest.raises(fp.ProtocolError):
        fp.validate_message({"type": "assign_lanes", "drone_id": "d", "alt_cm": 100,
                             "lanes": [{"direction": ["forward"], "distance_cm": 50}], "zone": ZONE})
    telemetry = fp.make_telemetry("d", 50, 0, False, 0, 0, 0, None, None, None, None)
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(dict(telemetry, battery=10**400))
    with pytest.raises(fp.ProtocolError):
        fp.validate_message({"type": "ping", "t1": 10**400})


# ===========================================================================
# Caps and privacy
# ===========================================================================


def test_size_caps():
    with pytest.raises(fp.ProtocolError):
        fp.make_telemetry("d" * (fp.MAX_ID_LEN + 1), 50, 0, False, 0, 0, 0, None, None, None, None)
    fp.make_telemetry("d" * fp.MAX_ID_LEN, 50, 0, False, 0, 0, 0, None, None, None, None)
    lane = {"direction": "forward", "distance_cm": 10}
    fp.make_assign_lanes("d", 100, [lane] * fp.MAX_LANES, ZONE)
    with pytest.raises(fp.ProtocolError):
        fp.make_assign_lanes("d", 100, [lane] * (fp.MAX_LANES + 1), ZONE)
    with pytest.raises(fp.ProtocolError):
        fp.make_assign_lanes("d", 100, [{"direction": "forward", "distance_cm": fp.MAX_LANE_DISTANCE_CM + 1}], ZONE)
    too_many = dict(fp.make_heartbeat(1), **{f"x{i}": i for i in range(fp.MAX_FIELDS)})
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(too_many)
    deep: object = 1
    for _ in range(fp.MAX_DEPTH + 1):
        deep = [deep]
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(dict(fp.make_heartbeat(1), extra=deep))
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(dict(fp.make_heartbeat(1), extra=list(range(fp.MAX_NODES))))
    with pytest.raises(fp.ProtocolError):
        fp.validate_message({"type": "heartbeat", "seq": 1, 5: "non-string key"})


@pytest.mark.parametrize("key", ["face_embedding", "Face_Embedding", "crop", "photo", "thumbnail", "image",
                                 "reid_embedding", "frame"])
def test_no_fleet_message_may_carry_imagery_or_biometrics(key):
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(dict(fp.make_telemetry("d", 50, 0, False, 0, 0, 0, None, None, None, None),
                                 **{key: "x"}))
    state = node_state()
    state["detections"][0][key] = "x"
    with pytest.raises(fp.ProtocolError):
        fp.validate_peer_message(state)
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(dict(fp.make_hold(FENCE), meta={"inner": {key: 1}}), proto=2)


# ===========================================================================
# Wire encoding
# ===========================================================================


def test_encode_decode_round_trip_for_every_message():
    for direction, messages in ((G2E, v2_ground_to_edge()), (E2G, v2_edge_to_ground()), (PEER, peer_messages())):
        for msg in messages:
            text = fp.encode(msg, direction=direction, proto=2)
            assert " " not in text.replace('"drone-1 flying"', "").replace('"worker running"', "")
            assert fp.decode(text, direction=direction, proto=2) == msg
            assert fp.decode(text.encode("utf-8"), direction=direction, proto=2) == msg


@pytest.mark.parametrize("raw", [
    b"\xff\xfe",
    "not json",
    "[1, 2]",
    '"hold"',
    "null",
    '{"type": "heartbeat", "seq": 1, "seq": 2, "epoch": 7, "leader_id": "mac"}',
    '{"type": "clock", "offset_s": NaN, "epoch": 7, "leader_id": "mac"}',
    '{"type": "clock", "offset_s": Infinity, "epoch": 7, "leader_id": "mac"}',
    '{"type": "clock", "offset_s": 1e999, "epoch": 7, "leader_id": "mac"}',
    '{"type": "heartbeat", "seq": ' + "9" * 5000 + ', "epoch": 7, "leader_id": "mac"}',
    "[" * 50_000 + "]" * 50_000,
    '{"type": "land", "x": ' + "[" * 30_000 + "]" * 30_000 + "}",
    " " * (fp.MAX_MESSAGE_BYTES + 1),
    b" " * (fp.MAX_MESSAGE_BYTES + 1),
    "é" * (fp.MAX_MESSAGE_BYTES // 2 + 1),
    12,
    None,
])
def test_decode_rejects_malformed_input_with_protocol_error_only(raw):
    with pytest.raises(fp.ProtocolError):
        fp.decode(raw, direction=G2E, proto=2)


def test_encode_rejects_what_json_cannot_carry():
    with pytest.raises(fp.ProtocolError):
        fp.encode(dict(fp.make_heartbeat(1, fence=FENCE), extra={1, 2}), direction=G2E, proto=2)
    with pytest.raises(fp.ProtocolError):
        fp.encode(dict(fp.make_heartbeat(1, fence=FENCE), extra=float("nan")), direction=G2E, proto=2)
    with pytest.raises(fp.ProtocolError):
        fp.encode(dict(fp.make_heartbeat(1, fence=FENCE), extra="x" * fp.MAX_MESSAGE_BYTES), direction=G2E,
                  proto=2)


# ===========================================================================
# Property-style sweeps (seeded)
# ===========================================================================

_FIELD_POOL = sorted({
    "type", "epoch", "leader_id", "cmd_id", "plan_id", "drone_id", "node_id", "role", "proto", "seq",
    "t1", "t2", "t3", "offset_s", "t_ground_s", "alt_cm", "lanes", "zone", "direction", "distance_cm",
    "to_node", "epoch_next", "reason", "for", "result", "battery", "lanes_total", "is_flying", "epoch_seen",
    "state", "plan_valid", "on_complete", "hover_timeout_s", "snapshot", "roster", "mission", "latch",
    "detections", "executed_at_ground_s", "lane_index", "detail", "face_embedding", "x_cm", "w_cm",
})
_STRING_POOL = sorted(
    fp.GROUND_TO_EDGE_TYPES_V2 | fp.EDGE_TO_GROUND_TYPES_V2 | fp.PEER_TYPES | fp.NACK_REASONS_V2
    | fp.HOLD_REASONS | fp.EDGE_STATES | fp.GROUND_ROLES | fp.LANE_DIRECTIONS | {"edge", "ok", "", "mac", CMD}
)


def _random_value(rng: random.Random, depth: int = 0):
    kind = rng.randrange(12 if depth < 4 else 8)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.choice([0, 1, -1, 7, 20, 500, 501, 2**53, 10**400, -(10**30)])
    if kind == 3:
        return rng.choice([0.0, -0.0, 0.5, 1e308, float("nan"), float("inf"), -float("inf"), 123.4])
    if kind in (4, 5, 6, 7):
        return rng.choice(_STRING_POOL + ["x" * rng.randrange(0, 300), "\x00", "é", "a b"])
    if kind in (8, 9):
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {rng.choice(_FIELD_POOL): _random_value(rng, depth + 1) for _ in range(rng.randrange(5))}


def _mutate(rng: random.Random, msg: dict) -> dict:
    out = copy.deepcopy(msg)
    for _ in range(rng.randrange(1, 4)):
        op = rng.randrange(5)
        if op == 0 and out:
            out.pop(rng.choice(sorted(out, key=str)))
        elif op == 1:
            out[rng.choice(_FIELD_POOL)] = _random_value(rng)
        elif op == 2 and out:
            out[rng.choice(sorted(out, key=str))] = _random_value(rng)
        elif op == 3:
            out["type"] = rng.choice(_STRING_POOL)
        else:
            nested = [k for k, v in out.items() if isinstance(v, (dict, list))]
            if nested:
                target = out[rng.choice(nested)]
                if isinstance(target, dict):
                    target[rng.choice(_FIELD_POOL)] = _random_value(rng)
                elif target:
                    target[rng.randrange(len(target))] = _random_value(rng)
    return out


def _only_protocol_errors(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except fp.ProtocolError:
        return False
    return True


def _assert_invariants(msg, mtype: str, proto: int) -> None:
    """What a message that passed validation guarantees."""
    if mtype in fp.FENCED_COMMAND_TYPES and (proto >= 2 or mtype in fp.V2_ONLY_COMMAND_TYPES):
        assert fp.fence_of(msg) is not None and fp.cmd_id_of(msg) is not None, msg
    if mtype in fp.FENCED_CONTROL_TYPES and proto >= 2:
        assert fp.fence_of(msg) is not None, msg
    if mtype == "nudge":
        assert msg["direction"] in fp.NUDGE_DIRECTIONS
        assert fp.NUDGE_MIN_CM <= msg["distance_cm"] <= fp.NUDGE_MAX_CM
    if mtype == "nack" and proto >= 2:
        assert msg["for"] not in fp.FAILSAFE_TYPES


SEEDS = range(6)


@pytest.mark.parametrize("seed", SEEDS)
def test_fuzzed_mutations_raise_only_protocol_error(seed):
    rng = random.Random(seed)
    corpus = v1_messages() + v2_ground_to_edge() + v2_edge_to_ground() + peer_messages()
    for _ in range(1500):
        msg = _mutate(rng, rng.choice(corpus))
        for proto in (1, 2):
            if _only_protocol_errors(fp.validate_message, msg, proto=proto):
                _assert_invariants(msg, msg["type"], proto)
            for direction in (G2E, E2G, PEER):
                _only_protocol_errors(fp.validate_for_direction, msg, direction, proto=proto)
            _only_protocol_errors(fp.shape_for_peer, msg, proto)
        _only_protocol_errors(fp.parse_hello, msg)
        fp.fence_of(msg)
        fp.cmd_id_of(msg)
        try:
            text = json.dumps(msg)
        except ValueError:
            continue
        _only_protocol_errors(fp.decode, text, direction=G2E, proto=2)


@pytest.mark.parametrize("seed", SEEDS)
def test_fuzzed_random_values_raise_only_protocol_error(seed):
    rng = random.Random(1000 + seed)
    for _ in range(1500):
        value = _random_value(rng)
        if isinstance(value, dict) and rng.random() < 0.7:
            value["type"] = rng.choice(_STRING_POOL)
        for proto in (1, 2):
            _only_protocol_errors(fp.validate_message, value, proto=proto)
            _only_protocol_errors(fp.validate_ground_to_edge, value, proto=proto)
            _only_protocol_errors(fp.validate_edge_to_ground, value, proto=proto)
        _only_protocol_errors(fp.validate_peer_message, value)
        _only_protocol_errors(fp.negotiate_proto, value)
        try:
            text = json.dumps(value)
        except (ValueError, TypeError):
            continue
        _only_protocol_errors(fp.decode, text, direction=E2G, proto=1)
        _only_protocol_errors(fp.decode, text.encode("utf-8")[: rng.randrange(len(text) + 1)],
                              direction=PEER, proto=2)


@pytest.mark.parametrize("seed", SEEDS)
def test_every_landing_survives_fuzzing(seed):
    rng = random.Random(2000 + seed)
    for _ in range(500):
        msg = _random_value(rng, depth=1)
        msg = msg if isinstance(msg, dict) else {}
        msg["type"] = rng.choice(sorted(fp.FAILSAFE_TYPES))
        for proto in (1, 2):
            assert fp.validate_ground_to_edge(msg, proto=proto) == msg["type"]


@pytest.mark.parametrize("seed", SEEDS)
def test_random_valid_commands_round_trip(seed):
    rng = random.Random(3000 + seed)
    for _ in range(300):
        fence = fp.Fence(rng.randrange(1, fp.MAX_EPOCH), rng.choice(["mac", "win", "node-7"]))
        builders = [
            lambda fence=fence: fp.make_nudge(fence, rng.choice(sorted(fp.NUDGE_DIRECTIONS)),
                                              rng.randrange(fp.NUDGE_MIN_CM, fp.NUDGE_MAX_CM + 1)),
            lambda fence=fence: fp.make_hold(fence, reason=rng.choice([None, *sorted(fp.HOLD_REASONS)])),
            lambda fence=fence: fp.make_resume(fence, plan_id=rng.choice(["p-1", "p-2"])),
            lambda fence=fence: fp.make_heartbeat(rng.randrange(0, 2**40), fence=fence),
            lambda fence=fence: fp.make_start_at(rng.uniform(-1e6, 1e6), fence=fence, plan_id="p-9"),
            lambda fence=fence: fp.make_takeoff(fence=fence),
            lambda fence=fence: fp.make_land(cmd_id=fp.new_cmd_id(), fence=rng.choice([None, fence])),
            lambda fence=fence: fp.make_assign_lanes(
                f"drone-{rng.randrange(8)}", rng.uniform(50, 300),
                [{"direction": rng.choice(sorted(fp.LANE_DIRECTIONS)), "distance_cm": rng.randrange(1, 2000)}
                 for _ in range(rng.randrange(0, 40))],
                {"x_cm": rng.uniform(-500, 500), "y_cm": 0, "w_cm": 400, "h_cm": 400},
                fence=fence, plan_id=f"p-{rng.randrange(100)}", plan_seq=rng.randrange(1, 2**40),
                on_complete=rng.choice(["hover", "land"])),
        ]
        if fence.epoch < fp.MAX_EPOCH:
            builders.append(lambda fence=fence: fp.make_handover(fence, "other"))
        msg = rng.choice(builders)()
        assert fp.decode(fp.encode(msg, direction=G2E, proto=2), direction=G2E, proto=2) == msg
        _assert_invariants(msg, msg["type"], 2)


# ===========================================================================
# Reference doc
# ===========================================================================


def test_reference_doc_lists_every_message_type():
    text = DOC.read_text(encoding="utf-8")
    every = fp.GROUND_TO_EDGE_TYPES_V2 | fp.EDGE_TO_GROUND_TYPES_V2 | fp.PEER_TYPES
    missing = sorted(t for t in every if f"`{t}`" not in text)
    assert not missing, f"docs/fleet-protocol-v2.md does not mention {missing}"
    for reason in fp.NACK_REASONS_V2:
        assert f"`{reason}`" in text, reason
