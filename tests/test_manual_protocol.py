"""Manual takeover protocol (``flightrisk/fleet_protocol/manual.py``) and its edge-authority fencing.

Covers encode/decode of every manual command and reply, the nack reasons,
fencing (required in v2 and in multi-ground mode, optional on the v1 link
of today's dashboard), ``cmd_id`` handling, and that the manual land stays
an ordinary, never-fenced, never-nackable ``land``.
"""
from __future__ import annotations

import json

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.fleet_authority import ACCEPT, LAND, REFUSE, EdgeAuthority

FENCE = fp.Fence(3, "mac")


def _manual_commands(fence=None):
    return [
        fp.make_manual_hold(drone_id="drone-1", fence=fence),
        fp.make_manual_move("forward", distance_cm=30, fence=fence),
        fp.make_manual_move("up", distance_cm=100, fence=fence),
        fp.make_manual_move("ccw", degrees=45, fence=fence),
        fp.make_manual_takeoff(fence=fence),
        fp.make_resume_search(fence=fence),
    ]


@pytest.mark.parametrize("msg", _manual_commands(), ids=lambda m: m["type"] + "-" + str(m.get("direction")))
def test_manual_commands_round_trip_on_a_v1_link(msg):
    raw = fp.encode(msg, direction=fp.DIRECTION_GROUND_TO_EDGE, proto=1)
    assert fp.decode(raw, direction=fp.DIRECTION_GROUND_TO_EDGE, proto=1) == msg
    assert fp.cmd_id_of(msg) == msg["cmd_id"]


@pytest.mark.parametrize("msg", _manual_commands(FENCE), ids=lambda m: m["type"])
def test_fenced_manual_commands_pass_v2_and_require_fence(msg):
    raw = json.dumps(msg)
    assert fp.decode(raw, direction=fp.DIRECTION_GROUND_TO_EDGE, proto=2, require_fence=True) == msg
    # shape_for_peer copies them unchanged, so the cmd_id survives a v1 link.
    assert fp.shape_for_peer(msg, 1) == msg


@pytest.mark.parametrize("msg", _manual_commands(), ids=lambda m: m["type"])
def test_unfenced_manual_commands_are_refused_in_v2_and_when_a_fence_is_required(msg):
    with pytest.raises(fp.ProtocolError):
        fp.validate_ground_to_edge(msg, proto=2)
    with pytest.raises(fp.ProtocolError, match="requires the v2 form"):
        fp.validate_ground_to_edge(msg, proto=1, require_fence=True)


@pytest.mark.parametrize("bad", [
    {"type": "manual_move", "direction": "forward", "distance_cm": 30},  # no cmd_id
    {"type": "manual_move", "cmd_id": "c1", "direction": "forward", "distance_cm": 101},
    {"type": "manual_move", "cmd_id": "c1", "direction": "forward", "distance_cm": 19},
    {"type": "manual_move", "cmd_id": "c1", "direction": "cw", "degrees": 46},
    {"type": "manual_move", "cmd_id": "c1", "direction": "cw", "distance_cm": 30},
    {"type": "manual_move", "cmd_id": "c1", "direction": "sideways", "distance_cm": 30},
    {"type": "manual_hold", "cmd_id": "c1", "epoch": 3},  # half a fence
    {"type": "resume_search"},
])
def test_malformed_manual_commands_are_refused(bad):
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(bad, proto=1)


def test_manual_types_stay_out_of_the_existing_command_sets():
    assert fp.MANUAL_COMMAND_TYPES == {"manual_hold", "manual_move", "manual_takeoff", "resume_search"}
    assert not fp.MANUAL_COMMAND_TYPES & (fp.FENCED_COMMAND_TYPES | fp.COMMAND_TYPES | fp.GROUND_TO_EDGE_TYPES_V2)
    assert not fp.MANUAL_COMMAND_TYPES & fp.RETRYABLE_COMMAND_TYPES


def test_replies_validate_on_every_link_version_with_the_manual_reasons():
    ack = fp.make_manual_ack("manual_move", "drone-1", "c1", control_mode="manual", x_cm=10.0, y_cm=200.0)
    for reason in ("geofence", "ceiling", "floor", "not_manual", "drone_unreachable", "busy", "landing",
                   "not_flying", "stale_epoch", "not_authority", "unsupported", "invalid"):
        nack = fp.make_manual_nack("manual_move", "drone-1", "c1", reason, detail="why", control_mode="held")
        for proto in (1, 2):
            assert fp.validate_edge_to_ground(nack, proto=proto) == "nack"
    for proto in (1, 2):
        assert fp.validate_edge_to_ground(ack, proto=proto) == "ack"
    with pytest.raises(fp.ProtocolError):
        fp.make_manual_nack("manual_move", "drone-1", "c1", "no_such_reason")
    # The generic v2 nack builder (used by the edge authority) produces valid manual nacks too.
    fp.make_nack("manual_move", "drone-1", "stale_epoch", "c1", epoch_seen=4)


def test_telemetry_control_mode_is_optional_and_checked():
    msg = fp.make_telemetry("drone-1", 90, 100, True, 0, 0, 0, 0, 3, 0.1, None)
    assert fp.validate_edge_to_ground(msg, proto=1) == "telemetry"
    for mode in fp.CONTROL_MODES:
        fp.validate_edge_to_ground(dict(msg, control_mode=mode), proto=1)
    with pytest.raises(fp.ProtocolError):
        fp.validate_edge_to_ground(dict(msg, control_mode="piloted"), proto=1)


def test_manual_land_is_an_ordinary_land_never_fenced_and_never_nackable():
    land = fp.make_manual_land()
    assert land["type"] == "land" and fp.is_manual_land(land)
    # Accepted whatever the protocol version or fence rule, like any landing.
    for proto in (1, 2, None):
        assert fp.validate_ground_to_edge(land, proto=proto, require_fence=True) == "land"
    assert fp.shape_for_peer(land, 1) == land
    # A landing is never refused, so a nack for one is invalid.
    with pytest.raises(fp.ProtocolError):
        fp.validate_edge_to_ground(
            {"type": "nack", "for": "land", "drone_id": "drone-1", "cmd_id": land["cmd_id"], "reason": "busy"}, proto=2
        )


# ---------------------------------------------------------------------------
# Edge authority (multi-ground mode): epoch and cmd_id fencing
# ---------------------------------------------------------------------------


class _Hooks:
    def __init__(self):
        self.active = True

    def is_active(self):
        return self.active

    def supports(self, mtype):
        return mtype in fp.MANUAL_COMMAND_TYPES

    def snapshot(self):
        return {"is_flying": True, "lane_index": 0, "battery": 80}

    def fleet_status(self):
        return None

    def invalidate_plan(self, reason):
        pass


def _authority():
    hooks = _Hooks()
    hooks.active = False
    auth = EdgeAuthority("drone-1", "win", hooks=hooks)
    auth.opened("mac", "ws://mac")
    auth.on_message("mac", fp.make_ground_hello("mac", "leader", 3))
    auth.opened("other", "ws://other")
    auth.on_message("other", fp.make_ground_hello("other", "standby", 0))
    hooks.active = True
    return auth


def test_authority_accepts_manual_commands_only_from_the_leader_at_its_epoch():
    auth = _authority()
    move = fp.make_manual_move("left", distance_cm=30, fence=FENCE)
    assert auth.on_message("mac", move).action == ACCEPT
    stale = fp.make_manual_move("left", distance_cm=30, fence=fp.Fence(2, "mac"))
    decision = auth.on_message("mac", stale)
    assert decision.action == REFUSE and decision.replies[0]["reason"] == "stale_epoch"
    foreign = fp.make_manual_move("left", distance_cm=30, fence=fp.Fence(3, "other"))
    decision = auth.on_message("other", foreign)
    assert decision.action == REFUSE and decision.replies[0]["reason"] == "not_authority"
    fp.validate_edge_to_ground(decision.replies[0], proto=2)


def test_authority_runs_a_manual_cmd_id_at_most_once():
    auth = _authority()
    move = fp.make_manual_move("left", distance_cm=30, fence=FENCE)
    first = auth.on_message("mac", move)
    assert first.action == ACCEPT
    assert auth.begin_dispatch(move, first.admitted) is move
    auth.outbound(fp.make_manual_ack("manual_move", "drone-1", move["cmd_id"]))
    again = auth.on_message("mac", move)
    assert again.action == REFUSE and again.reason == "duplicate"


def test_dispatch_lets_manual_hold_through_without_the_authority_link_but_never_a_move():
    auth = _authority()
    hold = fp.make_manual_hold(fence=FENCE)
    move = fp.make_manual_move("left", distance_cm=30, fence=FENCE)
    hold_decision = auth.on_message("mac", hold)
    move_decision = auth.on_message("mac", move)
    auth.closed("mac")
    # Pausing is always safe; moving into a dead authority link never is.
    assert auth.begin_dispatch(hold, hold_decision.admitted) is hold
    assert auth.begin_dispatch(move, move_decision.admitted) is None
    assert auth.stale_reply()["reason"] == "not_authority"


def test_authority_never_fences_the_manual_land():
    auth = _authority()
    decision = auth.on_message("other", fp.make_manual_land())
    assert decision.action == LAND
