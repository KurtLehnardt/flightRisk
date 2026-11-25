"""The edge sends ``unit_rev`` in every v2 hello, again on reconnect (PROTO-2, plan step S23).

Both the single-ground uplink and the multi-ground authority build the hello
in ``EdgeAuthority``. An edge with no resolution sends no ``unit_rev``, and a
v1 peer never sees one.
"""
from __future__ import annotations

from types import SimpleNamespace

from flightrisk import fleet_protocol as fp
from flightrisk.edge import EdgeRunner
from flightrisk.edge_query import EdgeQuery, LinkQueries
from flightrisk.edge_uplink import EdgeUplink
from flightrisk.fleet_protocol.unit import validate_edge_hello_unit_rev
from flightrisk.fleet_authority import EdgeAuthority
from flightrisk.hardware.catalogue import BUILTIN_MODELS
from flightrisk.params.report import build_report
from flightrisk.params.resolver import resolve_unit

DRONE = "drone-1"
OTHER_MODEL = next(name for name in BUILTIN_MODELS if name != "tello")


def resolution(model="tello"):
    report = build_report(BUILTIN_MODELS[model], resolve_unit(model, DRONE, {}))
    return SimpleNamespace(report=report)


def authority(res=None):
    answerer = EdgeQuery(res, DRONE) if res is not None else None
    return EdgeAuthority(DRONE, "edge-1", query=(lambda: answerer))


def hello_reply(auth, key="A", proto=2):
    auth.opened(key, "ws://" + key)
    decision = auth.on_message(key, fp.make_ground_hello("gs-a", "leader", 1, proto=proto))
    (reply,) = decision.replies
    assert reply["type"] == "hello"
    return reply


def test_the_authority_hello_carries_the_resolved_unit_rev():
    res = resolution()
    reply = hello_reply(authority(res))
    assert reply["unit_rev"] == res.report.unit_rev
    validate_edge_hello_unit_rev(reply)


def test_the_single_ground_uplink_hello_carries_it_too():
    res = resolution()
    runner = EdgeRunner()
    runner.edge_query = LinkQueries(EdgeQuery(res, DRONE), None, lambda msg: None)
    uplink = EdgeUplink(["ws://127.0.0.1:1"], drone_id=DRONE, node_id="edge-1", runner=runner)
    uplink.authority.opened("A", "ws://a")
    (reply,) = uplink.authority.on_message("A", fp.make_ground_hello("gs-a", "leader", 1)).replies
    assert reply["unit_rev"] == res.report.unit_rev


def test_a_reconnect_sends_the_same_unit_rev_again():
    res = resolution()
    auth = authority(res)
    first = hello_reply(auth, "A")
    auth.closed("A")
    second = hello_reply(auth, "B")
    assert first["unit_rev"] == second["unit_rev"] == res.report.unit_rev


def test_no_resolution_means_no_unit_rev():
    assert "unit_rev" not in hello_reply(authority(None))


def test_a_v1_peer_gets_no_unit_rev():
    auth = EdgeAuthority(DRONE, "edge-1", legacy=True, query=lambda: EdgeQuery(resolution(), DRONE))
    assert "unit_rev" not in hello_reply(auth, proto=1)


def test_a_changed_resolution_gives_a_different_unit_rev():
    plain = hello_reply(authority(resolution()))["unit_rev"]
    other = hello_reply(authority(resolution(OTHER_MODEL)))["unit_rev"]
    assert plain != other
