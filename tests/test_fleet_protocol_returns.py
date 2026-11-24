"""Return commands and the return keys of ``assign_lanes`` (Phase A2 step S5, RTH-11)."""
from __future__ import annotations

import json

import pytest

import flightrisk.fleet_protocol as fp
from flightrisk.fleet_protocol.checks import _is_private_key

FENCE = (3, "ground-a")
ZONE = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 300}
LANES = [{"direction": "forward", "distance_cm": 300}, {"direction": "right", "distance_cm": 100}]
SQUARE = [{"x_cm": 0, "y_cm": 0}, {"x_cm": 300, "y_cm": 0}, {"x_cm": 300, "y_cm": 300}]
AREA = {"id": "cue-1", "kind": "person_cue", "points": SQUARE}
LANDING = {"area_id": "home-a", "place": 1, "interval_s": 10}


def _lanes(**extra):
    msg = {
        "type": "assign_lanes", "drone_id": "drone-1", "alt_cm": 120, "lanes": LANES, "zone": ZONE,
        "epoch": 3, "leader_id": "ground-a", "cmd_id": "c-1", "plan_id": "p-1", "plan_seq": 2,
    }
    msg.update(extra)
    return msg


def _commands():
    return [
        fp.make_return_home("drone-1", cmd_id="r-1"),
        fp.make_return_home("drone-1", cmd_id="r-2", fence=FENCE, wait=True, max_wait_s=40),
        fp.make_return_home("drone-1", cmd_id="r-3", fence=FENCE, wait=False),
        fp.make_set_no_entry("drone-1", [AREA], fence=FENCE, cmd_id="n-1"),
        fp.make_set_no_entry("drone-1", [], fence=FENCE, cmd_id="n-2"),
        fp.make_set_speed("drone-1", 150, fence=FENCE, cmd_id="s-1"),
        fp.make_escort_setpoint("drone-1", 1200, -400, 1500, 90, 2, fence=FENCE, cmd_id="e-1"),
    ]


# -- vocabulary --------------------------------------------------------------


def test_vocabulary():
    assert fp.RETURN_COMMAND_TYPES == {"return_home", "set_no_entry", "set_speed", "escort_setpoint"}
    assert fp.QUERY_TYPES == {fp.DESCRIBE_UNIT} == {"describe_unit"}
    assert fp.ON_COMPLETE_ACTIONS == {"hover", "land"}
    assert fp.ON_COMPLETE_ACTIONS_V2 == {"hover", "land", "return_home"}
    assert fp.AP_LINK_LOSS_ACTIONS == {"return", "land", "unknown"}
    assert "rth_above_home" in fp.RTH_STATES and "cannot_be_recalled" in fp.RTH_STATES
    assert len(fp.RTH_REASONS) == 17 and {"no_return_order", "ceiling_breach"} <= fp.RTH_REASONS
    assert fp.MIN_RETURN_HOVER_TIMEOUT_S == 1.0
    assert {"unit_rev", "rth"} <= fp.V2_FIELDS


def test_straight_line_home_is_v2_only():
    assert "straight_line_home" in fp.NACK_REASONS_V2
    assert "straight_line_home" not in fp.NACK_REASONS


def test_protocol_version_unchanged():
    assert fp.PROTOCOL_VERSION == 2 and fp.MIN_PROTOCOL_VERSION == 1


def test_max_roster_is_eight():
    assert fp.MAX_ROSTER == 8


def test_return_types_stay_out_of_todays_command_sets():
    for name in ("GROUND_TO_EDGE_TYPES_V2", "FENCED_COMMAND_TYPES", "COMMAND_TYPES",
                 "V2_ONLY_COMMAND_TYPES", "RETRYABLE_COMMAND_TYPES"):
        assert not fp.RETURN_COMMAND_TYPES & getattr(fp, name), name
    assert not fp.QUERY_TYPES & fp.GROUND_TO_EDGE_TYPES_V2


def test_every_new_key_passes_the_privacy_screen():
    keys = {"corridor", "landing", "no_entry", "speed_cm_s", "wait", "max_wait_s", "areas", "kind",
            "points", "area_id", "place", "interval_s", "bearing_deg", "valid_s", "alt_cm", "x_cm",
            "y_cm", "unit_rev", "rth", "ap_link_loss", "accepts"}
    assert not [key for key in keys if _is_private_key(key)]


# -- the four commands -------------------------------------------------------


@pytest.mark.parametrize("index", range(7))
def test_commands_validate_at_proto_2(index):
    msg = _commands()[index]
    assert fp.validate_message(msg, proto=2) == msg["type"]
    assert fp.shape_for_peer(msg, 2) == msg


@pytest.mark.parametrize("index", range(7))
def test_the_live_edge_still_refuses_return_commands_at_decode(index):
    # Until the edge handles them (C1), the strict validator and the edge's
    # decode refuse them, as on main, so they reach on_decode_error and are
    # never silently ignored as an unexpected type.
    msg = _commands()[index]
    assert msg["type"] in fp.RETURN_COMMAND_TYPES
    for fenced in (False, True):
        with pytest.raises(fp.ProtocolError):
            fp.validate_ground_to_edge(msg, proto=2, require_fence=fenced)
    with pytest.raises(fp.ProtocolError):
        fp.decode(json.dumps(msg), direction=fp.DIRECTION_GROUND_TO_EDGE, proto=2)


@pytest.mark.parametrize("index", range(7))
def test_commands_rejected_at_proto_1(index):
    msg = _commands()[index]
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(msg, proto=1)
    with pytest.raises(fp.ProtocolError):
        fp.validate_ground_to_edge(msg, proto=1)
    with pytest.raises(fp.ProtocolError):
        fp.shape_for_peer(msg, 1)


def test_commands_are_not_edge_to_ground():
    for msg in _commands():
        with pytest.raises(fp.ProtocolError):
            fp.validate_edge_to_ground(msg, proto=2)


def test_builders_accept_a_fence_object():
    msg = fp.make_set_speed("drone-1", 150, fence=fp.Fence(3, "ground-a"), cmd_id="s-1")
    assert (msg["epoch"], msg["leader_id"]) == FENCE


def _bad(msg, **change):
    out = dict(msg)
    for key, value in change.items():
        if value is ...:
            out.pop(key, None)
        else:
            out[key] = value
    return out


_RH = {"type": "return_home", "drone_id": "drone-1", "cmd_id": "r-1"}
_RH_WAIT = {**_RH, "epoch": 3, "leader_id": "ground-a", "wait": True, "max_wait_s": 40}
_SNE = {"type": "set_no_entry", "drone_id": "drone-1", "cmd_id": "n-1", "epoch": 3, "leader_id": "ground-a",
        "areas": [AREA]}
_SPD = {"type": "set_speed", "drone_id": "drone-1", "cmd_id": "s-1", "epoch": 3, "leader_id": "ground-a",
        "speed_cm_s": 150}
_ESC = {"type": "escort_setpoint", "drone_id": "drone-1", "cmd_id": "e-1", "epoch": 3, "leader_id": "ground-a",
        "x_cm": 1200, "y_cm": -400, "alt_cm": 1500, "bearing_deg": 90, "valid_s": 2}


@pytest.mark.parametrize("msg", [
    _bad(_RH, cmd_id=...),
    _bad(_RH, drone_id=...),
    _bad(_RH, epoch=3),  # half a fence
    _bad(_RH, max_wait_s=10),  # max_wait_s without wait
    _bad(_RH_WAIT, epoch=..., leader_id=...),  # an ordered wait needs the fence
    _bad(_RH_WAIT, max_wait_s=...),
    _bad(_RH_WAIT, max_wait_s=0),
    _bad(_RH_WAIT, max_wait_s=fp.MAX_RETURN_WAIT_S + 1),
    _bad(_RH_WAIT, wait="yes"),
    _bad(_RH_WAIT, wait=False),  # a release carries no max_wait_s
    _bad(_SNE, epoch=..., leader_id=...),
    _bad(_SNE, areas=...),
    _bad(_SNE, areas={"id": "a"}),
    _bad(_SNE, areas=[{**AREA, "kind": "storm"}]),
    _bad(_SNE, areas=[{**AREA, "points": SQUARE[:2]}]),
    _bad(_SNE, areas=[{**AREA, "points": [[0, 0], [1, 1], [2, 0]]}]),
    _bad(_SNE, areas=[{**AREA, "colour": "red"}]),
    _bad(_SNE, areas=[{**AREA, "drone_id": ""}]),
    _bad(_SNE, areas=[{**AREA, "points": [*SQUARE[:2], {"x_cm": 1, "y_cm": 2, "z_cm": 3}]}]),
    _bad(_SNE, areas=[AREA] * (fp.MAX_NO_ENTRY_AREAS + 1)),
    _bad(_SPD, speed_cm_s=0),
    _bad(_SPD, speed_cm_s=fp.MAX_SPEED_CM_S + 1),
    _bad(_SPD, speed_cm_s="fast"),
    _bad(_SPD, cmd_id=...),
    _bad(_ESC, valid_s=0),
    _bad(_ESC, valid_s=fp.MAX_SETPOINT_VALID_S + 1),
    _bad(_ESC, bearing_deg=360),
    _bad(_ESC, alt_cm=0),
    _bad(_ESC, x_cm=...),
    _bad(_ESC, leader_id=...),
])
def test_bad_commands_rejected(msg):
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(msg, proto=2)


def test_return_home_without_wait_takes_an_optional_fence():
    fp.validate_message(_RH, proto=2)
    fp.validate_message({**_RH, "epoch": 3, "leader_id": "ground-a"}, proto=2)
    fp.validate_message(_bad(_RH_WAIT, wait=False, max_wait_s=...), proto=2)


def test_set_no_entry_area_may_name_its_drone():
    fp.validate_message({**_SNE, "areas": [{**AREA, "kind": "lost_drone", "drone_id": "drone-2"}]}, proto=2)


def test_commands_pass_the_privacy_screen_with_a_private_key():
    with pytest.raises(fp.ProtocolError):
        fp.validate_message({**_SPD, "thumbnail": "x"}, proto=2)


# -- replies -----------------------------------------------------------------


def test_replies_validate_at_proto_2_only():
    for command in _commands():
        for reply in (fp.make_return_ack(command, epoch_seen=3),
                      fp.make_return_nack(command, "landing", detail="landing")):
            assert fp.validate_edge_to_ground(reply, proto=2) == reply["type"]
            assert fp.shape_for_peer(reply, 2) == reply
            with pytest.raises(fp.ProtocolError):
                fp.validate_message(reply, proto=1)
            with pytest.raises(fp.ProtocolError):
                fp.shape_for_peer(reply, 1)


def test_bad_replies_rejected():
    command = _commands()[0]
    ack = fp.make_return_ack(command)
    nack = fp.make_return_nack(command, "not_authority")
    for msg in (_bad(ack, cmd_id=...), _bad(ack, result="no"), _bad(ack, result=...),
                _bad(nack, reason="geofencey"), _bad(nack, drone_id=...)):
        with pytest.raises(fp.ProtocolError):
            fp.validate_message(msg, proto=2)
    with pytest.raises(fp.ProtocolError):
        fp.make_return_nack(command, "nope")


# -- assign_lanes ------------------------------------------------------------


def test_assign_lanes_return_keys_accepted_at_proto_2():
    corridor = [{"x_cm": 0, "y_cm": 10 * i} for i in range(5)]
    msg = _lanes(corridor=corridor, landing=LANDING, no_entry=[AREA], speed_cm_s=120,
                 on_complete="return_home", hover_timeout_s=5)
    assert fp.validate_message(msg, proto=2) == "assign_lanes"
    assert fp.validate_ground_to_edge(msg, proto=2, require_fence=True) == "assign_lanes"
    built = fp.make_assign_lanes(
        "drone-1", 120, LANES, ZONE, fence=fp.Fence(3, "ground-a"), cmd_id="c-1", plan_id="p-1", plan_seq=2,
        on_complete="return_home", hover_timeout_s=5, corridor=corridor, landing=LANDING, no_entry=[AREA],
        speed_cm_s=120,
    )
    assert built == msg


@pytest.mark.parametrize("extra", [
    {"corridor": [{"x_cm": 0, "y_cm": 0}, {"x_cm": 0, "y_cm": 100}]},
    {"landing": LANDING},
    {"no_entry": [AREA]},
    {"speed_cm_s": 100},
    {"on_complete": "return_home"},
    {"on_complete": "return_home", "hover_timeout_s": 1},
])
def test_assign_lanes_return_keys_rejected_at_proto_1(extra):
    v1 = {"type": "assign_lanes", "drone_id": "drone-1", "alt_cm": 120, "lanes": LANES, "zone": ZONE, **extra}
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(v1, proto=1)
    fp.validate_message(_lanes(**extra), proto=2)
    # shape_for_peer refuses every one of them for a v1 peer, the return home
    # order included: stripping it would leave the drone hovering unrefused.
    with pytest.raises(fp.ProtocolError):
        fp.shape_for_peer(_lanes(**extra), 1)


@pytest.mark.parametrize("action", ["hover", "land"])
def test_shape_for_v1_peer_still_strips_hover_and_land(action):
    extra = {"hover_timeout_s": 5} if action == "hover" else {}
    shaped = fp.shape_for_peer(_lanes(on_complete=action, **extra), 1)
    assert "on_complete" not in shaped and "hover_timeout_s" not in shaped


def test_hover_timeout_with_return_home_has_a_one_second_floor():
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(_lanes(on_complete="return_home", hover_timeout_s=0.5), proto=2)
    fp.validate_message(_lanes(on_complete="return_home", hover_timeout_s=1), proto=2)
    fp.validate_message(_lanes(on_complete="return_home", hover_timeout_s=fp.MAX_HOVER_TIMEOUT_S), proto=2)
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(_lanes(on_complete="return_home", hover_timeout_s=fp.MAX_HOVER_TIMEOUT_S + 1), proto=2)
    # hover keeps its old rule, and land still takes no timeout.
    fp.validate_message(_lanes(on_complete="hover", hover_timeout_s=0.5), proto=2)
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(_lanes(on_complete="land", hover_timeout_s=5), proto=2)


@pytest.mark.parametrize("extra", [
    {"corridor": [{"x_cm": 0, "y_cm": 0}]},
    {"corridor": [[0, 0], [0, 100]]},
    {"corridor": [{"x_cm": 0, "y_cm": 0}] * (fp.MAX_CORRIDOR_POINTS + 1)},
    {"corridor": "home"},
    {"landing": {**LANDING, "place": fp.MAX_ROSTER}},
    {"landing": {**LANDING, "place": -1}},
    {"landing": {**LANDING, "interval_s": 0}},
    {"landing": {**LANDING, "interval_s": fp.MAX_LANDING_INTERVAL_S + 1}},
    {"landing": {**LANDING, "extra": 1}},
    {"landing": {"place": 0, "interval_s": 10}},
    {"no_entry": AREA},
    {"no_entry": [{**AREA, "id": ""}]},
    {"speed_cm_s": -1},
    {"on_complete": "return"},
])
def test_assign_lanes_bad_return_keys_rejected(extra):
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(_lanes(**extra), proto=2)


def test_two_hundred_point_corridor_is_within_the_number_caps():
    corridor = [{"x_cm": i, "y_cm": -i} for i in range(200)]
    msg = _lanes(corridor=corridor)
    assert 2 * len(corridor) > fp.MAX_LISTED_NUMBERS
    assert fp.validate_message(msg, proto=2) == "assign_lanes"
    assert len(fp.encode(msg, direction=fp.DIRECTION_GROUND_TO_EDGE, proto=2)) <= fp.MAX_MESSAGE_BYTES


# Taken from ``make_assign_lanes`` on origin/main before step S5.
_SNAPSHOTS = [
    ({}, ('{"type":"assign_lanes","drone_id":"drone-1","alt_cm":120,"lanes":[{"direction":"forward",'
         '"distance_cm":300},{"direction":"right","distance_cm":100}],"zone":{"x_cm":0,"y_cm":0,'
         '"w_cm":400,"h_cm":300}}')),
    ({"on_complete": "hover"},
     ('{"type":"assign_lanes","drone_id":"drone-1","alt_cm":120,"lanes":[{"direction":"forward",'
     '"distance_cm":300},{"direction":"right","distance_cm":100}],"zone":{"x_cm":0,"y_cm":0,"w_cm":400,'
     '"h_cm":300},"plan_id":"p-1","plan_seq":2,"on_complete":"hover","hover_timeout_s":20.0,"epoch":3,'
     '"leader_id":"ground-a","cmd_id":"c-1"}')),
    ({"on_complete": "hover", "hover_timeout_s": 5},
     ('{"type":"assign_lanes","drone_id":"drone-1","alt_cm":120,"lanes":[{"direction":"forward",'
     '"distance_cm":300},{"direction":"right","distance_cm":100}],"zone":{"x_cm":0,"y_cm":0,"w_cm":400,'
     '"h_cm":300},"plan_id":"p-1","plan_seq":2,"on_complete":"hover","hover_timeout_s":5,"epoch":3,'
     '"leader_id":"ground-a","cmd_id":"c-1"}')),
    ({"on_complete": "land"},
     ('{"type":"assign_lanes","drone_id":"drone-1","alt_cm":120,"lanes":[{"direction":"forward",'
     '"distance_cm":300},{"direction":"right","distance_cm":100}],"zone":{"x_cm":0,"y_cm":0,"w_cm":400,'
     '"h_cm":300},"plan_id":"p-1","plan_seq":2,"on_complete":"land","epoch":3,"leader_id":"ground-a",'
     '"cmd_id":"c-1"}')),
]


@pytest.mark.parametrize(("kwargs", "expected"), _SNAPSHOTS)
def test_make_assign_lanes_with_todays_arguments_is_byte_identical(kwargs, expected):
    if kwargs:
        kwargs = {"fence": fp.Fence(3, "ground-a"), "cmd_id": "c-1", "plan_id": "p-1", "plan_seq": 2, **kwargs}
    msg = fp.make_assign_lanes("drone-1", 120, LANES, ZONE, **kwargs)
    assert json.dumps(msg, separators=(",", ":")) == expected


def test_repeated_no_entry_area_ids_are_refused():
    other = dict(AREA, points=[{"x_cm": 0, "y_cm": 0}, {"x_cm": 50, "y_cm": 0}, {"x_cm": 0, "y_cm": 50}])
    with pytest.raises(fp.ProtocolError, match="repeats the area id"):
        fp.make_set_no_entry("drone-1", [AREA, other], fence=FENCE, cmd_id="n-3")
    with pytest.raises(fp.ProtocolError, match="repeats the area id"):
        fp.validate_message(_lanes(no_entry=[AREA, other]), proto=2)
    fp.make_set_no_entry("drone-1", [AREA, dict(other, id="area-other")], fence=FENCE, cmd_id="n-4")
