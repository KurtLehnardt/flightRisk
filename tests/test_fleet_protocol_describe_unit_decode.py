"""PROTO-4 (decode): the edge accepts ``describe_unit`` from a ground on protocol v2 only.

A query is read-only, so it is never fenced: a fence it carries is not looked
at, an edge that requires a fence still accepts it, and a v1 link refuses it
exactly as before A3, like any other type v1 does not define.
"""
from __future__ import annotations

import json

import pytest

from flightrisk import fleet_protocol as fp

QUERY = {"type": "describe_unit", "drone_id": "drone-1", "cmd_id": "q-1"}


def _decode(msg, *, proto, require_fence=False):
    return fp.decode(json.dumps(msg), direction=fp.DIRECTION_GROUND_TO_EDGE, proto=proto, require_fence=require_fence)


@pytest.mark.parametrize("require_fence", [False, True])
def test_a_v2_query_is_accepted(require_fence):
    assert fp.validate_ground_to_edge(QUERY, proto=2, require_fence=require_fence) == fp.DESCRIBE_UNIT
    assert fp.validate_for_direction(
        QUERY, fp.DIRECTION_GROUND_TO_EDGE, proto=2, require_fence=require_fence
    ) == fp.DESCRIBE_UNIT
    assert _decode(QUERY, proto=2, require_fence=require_fence) == QUERY


def test_the_builders_query_is_accepted():
    query = fp.make_describe_unit("drone-1")
    assert _decode(query, proto=2) == query


@pytest.mark.parametrize("proto", [1, None, 0, "2", 2.0, True])
@pytest.mark.parametrize("require_fence", [False, True])
def test_a_query_on_a_v1_or_unknown_link_is_refused_as_before(proto, require_fence):
    with pytest.raises(fp.ProtocolError) as exc:
        fp.validate_ground_to_edge(QUERY, proto=proto, require_fence=require_fence)
    # The same refusal as any type the link version does not define.
    assert "is not a ground-to-edge message in protocol v" in str(exc.value)
    with pytest.raises(fp.ProtocolError):
        _decode(QUERY, proto=proto, require_fence=require_fence)


@pytest.mark.parametrize("fence", [
    {"epoch": 7, "leader_id": "ground-a"},
    {"epoch": 1, "leader_id": "ground-old"},
    {"epoch": fp.MAX_EPOCH, "leader_id": "ground-b"},
])
@pytest.mark.parametrize("require_fence", [False, True])
def test_a_fenced_or_epoch_mismatched_query_is_still_accepted(fence, require_fence):
    # A query is never fenced: any ground may ask, whatever epoch it carries.
    msg = {**QUERY, **fence}
    assert fp.validate_ground_to_edge(msg, proto=2, require_fence=require_fence) == fp.DESCRIBE_UNIT
    assert _decode(msg, proto=2, require_fence=require_fence) == msg


@pytest.mark.parametrize("msg", [
    {"type": "describe_unit", "drone_id": "drone-1"},
    {"type": "describe_unit", "cmd_id": "q-1"},
    {**QUERY, "drone_id": ""},
    {**QUERY, "drone_id": 7},
    {**QUERY, "drone_id": "d" * 65},
    {**QUERY, "cmd_id": None},
    {**QUERY, "cmd_id": 12},
    {**QUERY, "cmd_id": "has space"},
    {**QUERY, "cmd_id": "q" * 65},
    {**QUERY, "note": "data:image/png;base64," + "A" * 64},
], ids=lambda m: ",".join(f"{k}={str(v)[:12]}" for k, v in m.items() if k != "type") or "bare")
@pytest.mark.parametrize("require_fence", [False, True])
def test_a_malformed_query_is_refused(msg, require_fence):
    with pytest.raises(fp.ProtocolError):
        fp.validate_ground_to_edge(msg, proto=2, require_fence=require_fence)
    with pytest.raises(fp.ProtocolError):
        _decode(msg, proto=2, require_fence=require_fence)


def test_an_edge_never_sends_a_query_to_a_ground():
    with pytest.raises(fp.ProtocolError):
        fp.validate_edge_to_ground(QUERY, proto=2)
