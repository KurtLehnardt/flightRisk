"""describe_unit, its replies, unit_rev in the edge hello and the gates (PROTO-1 to PROTO-3)."""
from __future__ import annotations

import dataclasses
import json

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.fleet_protocol.checks import _is_private_key
from flightrisk.fleet_protocol.unit import encoded_size
from flightrisk.hardware.catalogue import BUILTIN_MODELS
from flightrisk.params.report import build_report
from flightrisk.params.resolver import resolve_unit
from flightrisk.params.spec import Scope
from flightrisk.params.table import TABLE

REV = "0123456789ab"


def _hello(**extra):
    return fp.make_edge_hello("drone-1", "edge-1", 0, plan_id=None, lane_index=None, is_flying=False,
                              battery=80, **extra)


@pytest.fixture(scope="module")
def ack():
    report = build_report(BUILTIN_MODELS["tello"], resolve_unit("tello", "drone-1", {}))
    return fp.make_describe_unit_ack("drone-1", "c1", **report.wire(), ap_link_loss="unknown",
                                     accepts=["return_home"])


def _walk(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield None, item
            yield from _walk(item)


def _worst_case() -> dict:
    """A custom model with three cameras, every drone-scope registry row and long floats."""
    base = BUILTIN_MODELS["generic_multirotor"]
    cam = base.cameras[0]
    cameras = tuple(
        dataclasses.replace(cam, id=f"camera-{i}", mount=f"mount-position-{i}", mount_kind="gimbal",
                            pitch_min_deg=-90.123456, pitch_max_deg=30.123456, sensor="thermal_rgb",
                            radiometric=True)
        for i in range(3)
    )
    model = dataclasses.replace(base, id="custom-heavy-lifter-01", name="Custom heavy lifter " + "x" * 60,
                                cameras=cameras)
    report = build_report(model, {}, roles={c.id: "search_primary" for c in cameras},
                          gates={f"capability_gate_{i:02d}": {"on": True, "ok": False, "src": "inventory",
                                                     "why": "a requirement is not met by this unit " + "y" * 20}
                                 for i in range(8)})
    wire = report.wire()
    wire["values"] = {
        spec.id: {"v": 12345.6789, "src": "inventory", "d": 12345.6789, "lo": -1234.56789,
                  "hi": 12345.6789, "edit": "file_env"}
        for spec in TABLE if spec.scope is Scope.DRONE
    }
    return fp.make_describe_unit_ack("drone-1", "c" * 32, **wire, ap_link_loss="return",
                                     accepts=sorted(fp.ACCEPT_NAMES - {"escort_setpoint"}))


# --- the query -------------------------------------------------------------

def test_describe_unit_is_a_query_not_a_configuration_command():
    assert fp.DESCRIBE_UNIT in fp.QUERY_TYPES
    assert fp.DESCRIBE_UNIT not in fp.CONFIG_COMMAND_TYPES
    assert fp.DESCRIBE_UNIT not in fp.COMMAND_TYPES


def test_query_is_v2_only_and_needs_a_cmd_id():
    query = fp.make_describe_unit("drone-1", cmd_id="q1")
    assert query == {"type": "describe_unit", "drone_id": "drone-1", "cmd_id": "q1"}
    fp.validate_describe_unit(query, proto=2)
    with pytest.raises(fp.ProtocolError):
        fp.validate_describe_unit(query, proto=1)
    with pytest.raises(fp.ProtocolError):
        fp.validate_describe_unit({"type": "describe_unit", "drone_id": "drone-1"}, proto=2)


def test_strict_edge_validator_still_refuses_the_query_until_a3():
    with pytest.raises(fp.ProtocolError):
        fp.validate_ground_to_edge(fp.make_describe_unit("drone-1"), proto=2)


# --- the reply -------------------------------------------------------------

def test_reply_validates_on_v2_only(ack):
    assert fp.validate_edge_to_ground(ack, proto=2) == "ack"
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(ack, proto=1)


def test_reply_shape(ack):
    assert {"unit", "gates", "values", "unit_rev", "ap_link_loss", "accepts"} <= set(ack)
    assert ack["unit"]["link"] == "wifi_ap"


@pytest.mark.parametrize("mutate", [
    lambda m: m["unit"].update(link="192.168.10.1"),
    lambda m: m["unit"].update(ssid="TELLO-ABC"),
    lambda m: next(iter(m["unit"]["cameras"].values())).update(host="10.0.0.1"),
    lambda m: next(iter(m["values"].values())).update(edit="fact"),
    lambda m: next(iter(m["values"].values())).update(v=[1, 2, 3]),
    lambda m: m.update(unit_rev="0123456789AB"),
    lambda m: m.update(unit_rev="0123456789abc"),
    lambda m: m.update(ap_link_loss="hover"),
    lambda m: m.update(accepts=["escort_setpoint"]),
    lambda m: m.update(accepts=["return_home", "return_home"]),
    lambda m: m.pop("values"),
])
def test_reply_rejects(ack, mutate):
    bad = json.loads(json.dumps(ack))
    mutate(bad)
    with pytest.raises(fp.ProtocolError):
        fp.validate_edge_to_ground(bad, proto=2)


def test_escort_setpoint_is_allowed_for_a_field_unit(ack):
    good = json.loads(json.dumps(ack))
    good["unit"]["scale"] = "field"
    good["accepts"] = ["escort_setpoint", "return_home"]
    fp.validate_edge_to_ground(good, proto=2)


def test_an_unknown_accepts_name_is_valid_and_ignored(ack):
    good = json.loads(json.dumps(ack))
    good["accepts"] = ["return_home", "some_future_command"]
    fp.validate_edge_to_ground(good, proto=2)
    assert fp.may_send(good, "return_home")
    assert fp.may_send(good, "some_future_command")


def test_nack_busy():
    nack = fp.make_describe_unit_nack("drone-1", "c1", "busy")
    assert fp.validate_edge_to_ground(nack, proto=2) == "nack"
    with pytest.raises(fp.ProtocolError):
        fp.make_describe_unit_nack("drone-1", "c1", "geofence")
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(nack, proto=1)


def test_every_key_passes_the_privacy_screen():
    for key, _ in _walk(_worst_case()):
        if key is not None:
            assert not _is_private_key(key), key
    forbidden = sorted(fp.PRIVACY_FORBIDDEN_KEYS)[0]
    bad = json.loads(json.dumps(_worst_case()))
    bad[forbidden] = "x"
    with pytest.raises(fp.ProtocolError):
        fp.validate_edge_to_ground(bad, proto=2)


def test_no_list_of_numbers():
    for _, value in _walk(_worst_case()):
        if isinstance(value, list):
            assert all(isinstance(item, str) for item in value)


def test_worst_case_is_under_16_kb_and_valid():
    worst = _worst_case()
    assert len(worst["unit"]["cameras"]) == 3
    assert len(worst["values"]) == sum(1 for s in TABLE if s.scope is Scope.DRONE)
    assert encoded_size(worst) < 16 * 1024
    assert fp.validate_edge_to_ground(worst, proto=2) == "ack"


def test_shape_for_peer_refuses_queries_on_v1():
    query = fp.make_describe_unit("drone-1", cmd_id="q1")
    assert fp.shape_for_peer(query, 2) == query
    with pytest.raises(fp.ProtocolError):
        fp.shape_for_peer(query, 1)


# --- unit_rev in the hello ---------------------------------------------------

def test_hello_without_unit_rev_is_byte_identical_to_before():
    expected = ('{"type":"hello","role":"edge","drone_id":"drone-1","node_id":"edge-1","proto":2,'
                '"epoch_seen":0,"plan_id":null,"lane_index":null,"is_flying":false,"battery":80}')
    assert json.dumps(_hello(), separators=(",", ":")) == expected


def test_hello_with_unit_rev_validates_under_todays_rules():
    hello = _hello(unit_rev=REV)
    assert hello["unit_rev"] == REV
    for proto in (1, 2):
        assert fp.validate_message(hello, proto=proto) == "hello"
    assert fp.validate_edge_to_ground(hello, proto=2) == "hello"
    assert fp.parse_hello(hello).unit_rev == REV
    without = dict(hello)
    del without["unit_rev"]
    assert without == _hello()


@pytest.mark.parametrize("rev", ["0123456789AB", "0123456789a", "0123456789abc", 123, None, "g123456789ab"])
def test_hello_rejects_a_bad_unit_rev(rev):
    hello = dict(_hello())
    hello["unit_rev"] = rev
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(hello, proto=2)


def test_shape_for_peer_strips_unit_rev_for_v1():
    hello = _hello(unit_rev=REV)
    assert "unit_rev" not in fp.shape_for_peer(hello, 1)
    assert fp.shape_for_peer(hello, 2)["unit_rev"] == REV


# --- the gates ---------------------------------------------------------------

def test_may_describe_only_an_edge_with_unit_rev():
    assert fp.may_describe(_hello(unit_rev=REV))
    assert not fp.may_describe(_hello())
    old = dict(_hello(unit_rev=REV), proto=1)
    assert not fp.may_describe(old)
    assert not fp.may_describe(None)


def test_may_send(ack):
    assert not fp.may_send(None, "return_home")
    assert fp.may_send(ack, "return_home")
    assert not fp.may_send(ack, "set_speed")
    assert fp.may_send(None, "land")
    assert fp.may_send(None, {"type": "assign_lanes", "drone_id": "drone-1", "lanes": []})
    corridor = {"type": "assign_lanes", "corridor": [{"x_cm": 0, "y_cm": 0}]}
    assert fp.required_accepts(corridor) == {"assign_lanes.corridor"}
    assert not fp.may_send(ack, corridor)
    home = {"type": "assign_lanes", "on_complete": "return_home"}
    assert not fp.may_send(ack, home)
    assert fp.may_send({"accepts": ["assign_lanes.on_complete.return_home"]}, home)
    with pytest.raises(ValueError):
        fp.may_send(ack, "describe_unit")


def test_nack_reason_for_peer():
    query = fp.make_describe_unit("drone-1")
    assert fp.nack_reason_for_peer("straight_line_home", query) == "straight_line_home"
    assert fp.nack_reason_for_peer("straight_line_home", None) == "invalid"
    assert fp.nack_reason_for_peer("geofence", None) == "geofence"
