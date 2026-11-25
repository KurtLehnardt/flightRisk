"""A multi-ground edge answers describe_unit through its fleet authority (PROTO-4, plan step S22).

Any ground with an agreed v2 hello may ask, the authority or a standby. The
query is handled before any authority, fence or epoch check, never feeds the
link-loss watchdog, never adopts an epoch and never touches the authority's
command cache (``EdgeAuthority._cache``). Each connection has its own
``edge_query`` reply cache and 2 s limit. A v1 link gets no reply (S20).
"""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from flightrisk import edge_query
from flightrisk import fleet_protocol as fp
from flightrisk.edge import EdgeRunner
from flightrisk.edge_query import EdgeQuery
from flightrisk.edge_uplink import EdgeUplink
from flightrisk.fleet_authority import ACCEPT, HANDLED, IGNORE, EdgeAuthority
from flightrisk.hardware.catalogue import BUILTIN_MODELS
from flightrisk.params.report import build_report
from flightrisk.params.resolver import resolve_unit

DRONE = "drone-1"


class Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Hooks:
    def __init__(self):
        self.active = False

    def is_active(self):
        return self.active

    def supports(self, mtype):
        return mtype in fp.FENCED_COMMAND_TYPES

    def snapshot(self):
        return {"is_flying": self.active, "lane_index": 0, "battery": 80}

    def fleet_status(self):
        return None

    def invalidate_plan(self, reason):
        return None


@pytest.fixture(scope="module")
def resolution():
    report = build_report(BUILTIN_MODELS["tello"], resolve_unit("tello", DRONE, {}))
    return SimpleNamespace(report=report)


@pytest.fixture
def unit_rev(resolution):
    return resolution.report.wire()["unit_rev"]


def query(cmd_id, **extra):
    return {**fp.make_describe_unit(DRONE, cmd_id=cmd_id), **extra}


def authority(resolution, clock=None):
    """An authority with ground A as its authority at epoch 1 and ground B as a standby."""
    answerer = EdgeQuery(resolution, DRONE)
    auth = EdgeAuthority(DRONE, "edge-1", hooks=Hooks(), clock=clock or Clock(), query=lambda: answerer)
    auth.opened("A", "ws://a")
    auth.opened("B", "ws://b")
    auth.on_message("A", fp.make_ground_hello("gs-a", "leader", 1))
    assert auth.on_message("A", fp.make_heartbeat(1, fence=fp.Fence(1, "gs-a"))).action == ACCEPT
    auth.on_message("B", fp.make_ground_hello("gs-b", "standby", 0))
    assert auth.authority_key == "A"
    return auth


def only_reply(decision):
    assert len(decision.replies) == 1
    return decision.replies[0]


def cache_snapshot(auth):
    return [(cmd_id, copy.deepcopy(vars(entry))) for cmd_id, entry in auth._cache.items()], dict(auth._latest)


# -- Authority rules -------------------------------------------------------


def test_the_authority_and_a_standby_ground_each_get_the_resolved_unit(resolution, unit_rev):
    auth = authority(resolution)
    for key, cmd_id in (("A", "qa"), ("B", "qb")):
        decision = auth.on_message(key, query(cmd_id))
        assert decision.action == HANDLED and not decision.feed_watchdog
        reply = only_reply(decision)
        fp.validate_describe_unit_reply(reply, proto=2)
        assert (reply["type"], reply["for"], reply["cmd_id"], reply["unit_rev"]) == ("ack", "describe_unit", cmd_id, unit_rev)
        assert reply["unit"] == resolution.report.wire()["unit"]
    # The standby gained nothing by asking.
    assert (auth.authority_key, auth.authority_leader, int(auth.epoch_seen)) == ("A", "gs-a", 1)


def test_a_standby_query_before_any_authority_adopts_nothing(resolution, unit_rev):
    answerer = EdgeQuery(resolution, DRONE)
    auth = EdgeAuthority(DRONE, "edge-1", hooks=Hooks(), clock=Clock(), query=lambda: answerer)
    auth.opened("B", "ws://b")
    auth.on_message("B", fp.make_ground_hello("gs-b", "standby", 0))
    assert only_reply(auth.on_message("B", query("q1")))["unit_rev"] == unit_rev
    assert (auth.authority_key, auth.authority_leader, int(auth.epoch_seen)) == (None, None, 0)


def test_busy_is_per_connection(resolution):
    clock = Clock()
    auth = authority(resolution, clock)
    assert only_reply(auth.on_message("A", query("a1")))["type"] == "ack"
    busy = only_reply(auth.on_message("A", query("a2")))
    assert (busy["type"], busy["reason"]) == ("nack", "busy")
    # Ground A's limit does not hold up ground B, in the same instant.
    assert only_reply(auth.on_message("B", query("b1")))["type"] == "ack"
    assert only_reply(auth.on_message("B", query("b2")))["reason"] == "busy"
    # A repeated cmd_id gets its cached reply on its own connection only.
    assert only_reply(auth.on_message("A", query("a1")))["type"] == "ack"
    clock.now += edge_query.MIN_INTERVAL_S + 0.01
    assert only_reply(auth.on_message("A", query("a2")))["type"] == "ack"
    assert only_reply(auth.on_message("B", query("b2")))["type"] == "ack"


def test_a_reconnect_starts_with_a_fresh_query_state(resolution):
    auth = authority(resolution)
    assert only_reply(auth.on_message("B", query("b1")))["type"] == "ack"
    auth.closed("B")
    auth.opened("B", "ws://b")
    auth.on_message("B", fp.make_ground_hello("gs-b", "standby", 0))
    assert only_reply(auth.on_message("B", query("b2")))["type"] == "ack"


def test_queries_never_touch_the_command_cache(resolution):
    auth = authority(resolution)
    takeoff = fp.make_takeoff(fence=fp.Fence(1, "gs-a"), cmd_id="c1")
    assert auth.on_message("A", takeoff).action == ACCEPT
    before = cache_snapshot(auth)
    assert before[0], "the authority's command was not cached"
    # The same cmd_id as the command, and more ids than either cache holds, from both grounds.
    for n in range(fp.CMD_ID_CACHE_SIZE + 4):
        for key in ("A", "B"):
            auth.on_message(key, query("c1" if n == 0 else f"{key}-{n}"))
    assert cache_snapshot(auth) == before
    # The command is still guarded against running twice.
    assert auth.on_message("A", takeoff).reason == "duplicate"


@pytest.mark.parametrize("extra", [
    {"epoch": 0, "leader_id": "gs-b"},
    {"epoch": 7, "leader_id": "gs-a"},
    {"epoch": 1, "leader_id": "gs-x"},
], ids=["standby-fence", "future-epoch", "leader-mismatch"])
def test_a_fenced_or_epoch_mismatched_query_is_still_answered(resolution, unit_rev, extra):
    auth = authority(resolution)
    for key in ("A", "B"):
        decision = auth.on_message(key, query(f"q-{key}", **extra))
        assert decision.action == HANDLED and not decision.feed_watchdog
        assert only_reply(decision)["unit_rev"] == unit_rev
    assert (auth.authority_key, int(auth.epoch_seen)) == ("A", 1)


def test_a_v1_peer_gets_no_reply(resolution):
    answerer = EdgeQuery(resolution, DRONE)
    auth = EdgeAuthority(DRONE, "edge-1", hooks=Hooks(), legacy=True, clock=Clock(), query=lambda: answerer)
    auth.opened("old", "ws://old")
    # No hello yet, then a v1 hello: neither link may be answered.
    decision = auth.on_message("old", query("q1"))
    assert (decision.action, decision.replies, decision.feed_watchdog) == (IGNORE, (), False)
    auth.on_message("old", fp.make_ground_hello("gs-old", "standby", 0, proto=1))
    decision = auth.on_message("old", query("q2"))
    assert (decision.action, decision.replies, decision.feed_watchdog) == (IGNORE, (), False)


def test_an_edge_without_a_resolved_unit_gives_no_reply():
    auth = EdgeAuthority(DRONE, "edge-1", hooks=Hooks(), clock=Clock(), query=lambda: None)
    auth.opened("A", "ws://a")
    auth.on_message("A", fp.make_ground_hello("gs-a", "standby", 0))
    decision = auth.on_message("A", query("q1"))
    assert (decision.action, decision.replies, decision.feed_watchdog) == (IGNORE, (), False)


def test_nacks_to_a_peer_use_that_connections_own_query(resolution):
    auth = authority(resolution)
    auth.on_message("B", query("qb"))
    msg = fp.make_takeoff(fence=fp.Fence(1, "gs-a"), cmd_id="c9")
    with auth._lock:
        to_b = auth._nack(auth._conns["B"], msg, fp.TAKEOFF, "c9", "straight_line_home", key="B")
        to_a = auth._nack(auth._conns["A"], msg, fp.TAKEOFF, "c9", "straight_line_home", key="A")
    # B asked, so it understands the newer reason; A never asked.
    assert (to_b[0]["reason"], to_a[0]["reason"]) == ("straight_line_home", "invalid")


# -- Through the uplink ----------------------------------------------------


class FakeSocket:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        return None


def open_link(uplink, index, ws):
    transport = uplink._transports[index]
    transport._ws, transport._connected = ws, True
    transport._on_connected(ws)
    return transport


async def replies_on(ws, count, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(ws.sent) < count and loop.time() < deadline:
        await asyncio.sleep(0.01)
    return [json.loads(p) for p in ws.sent]


def test_through_the_uplink_both_grounds_are_answered_and_the_watchdog_is_not_fed(resolution, unit_rev):
    fed: list[int] = []

    async def scenario():
        runner = EdgeRunner()
        uplink = EdgeUplink(["ws://127.0.0.1:1", "ws://127.0.0.1:2"], drone_id=DRONE, node_id="edge-1",
                            runner=runner)
        runner.edge_query = edge_query.for_link(resolution, DRONE, uplink, lambda msg: None)
        uplink._watchdog_feed = lambda: fed.append(1)
        uplink._inbox_event = asyncio.Event()
        ws_a, ws_b = FakeSocket(), FakeSocket()
        link_a, link_b = open_link(uplink, 0, ws_a), open_link(uplink, 1, ws_b)
        await uplink._on_frame(link_a, ws_a, json.dumps(fp.make_ground_hello("gs-a", "leader", 1)), 1.0)
        await uplink._on_frame(link_a, ws_a, json.dumps(fp.make_heartbeat(1, fence=fp.Fence(1, "gs-a"))), 2.0)
        await uplink._on_frame(link_b, ws_b, json.dumps(fp.make_ground_hello("gs-b", "standby", 0)), 3.0)
        assert uplink.authority.authority_key is ws_a
        fed.clear()
        last_feed = uplink._last_authority_msg_s
        queued = len(uplink._inbox)
        await uplink._on_frame(link_a, ws_a, json.dumps(query("qa")), 50.0)
        await uplink._on_frame(link_b, ws_b, json.dumps(query("qb")), 51.0)
        await uplink._on_frame(link_b, ws_b, json.dumps(query("qb2")), 52.0)
        got_a, got_b = await replies_on(ws_a, 2), await replies_on(ws_b, 3)
        state = (fed[:], uplink._last_authority_msg_s == last_feed, list(uplink._inbox)[queued:])
        for outbox in list(uplink._outboxes.values()):
            outbox.close()
        return got_a, got_b, state

    got_a, got_b, (feeds, feed_time_kept, inbox) = asyncio.run(scenario())
    queries_a = [m for m in got_a if m.get("for") == "describe_unit"]
    queries_b = [m for m in got_b if m.get("for") == "describe_unit"]
    assert [(m["type"], m["cmd_id"], m["unit_rev"]) for m in queries_a] == [("ack", "qa", unit_rev)]
    assert [(m["type"], m["cmd_id"]) for m in queries_b] == [("ack", "qb"), ("nack", "qb2")]
    assert queries_b[1]["reason"] == "busy"
    assert feeds == [] and feed_time_kept, "a query fed the link-loss watchdog"
    assert inbox == [], "a query reached the executor"


def test_through_the_uplink_a_v1_link_gets_no_reply(resolution):
    async def scenario():
        runner = EdgeRunner()
        uplink = EdgeUplink(["ws://127.0.0.1:1"], drone_id=DRONE, node_id="edge-1", runner=runner, legacy=True)
        runner.edge_query = edge_query.for_link(resolution, DRONE, uplink, lambda msg: None)
        uplink._inbox_event = asyncio.Event()
        ws = FakeSocket()
        link = open_link(uplink, 0, ws)
        await uplink._on_frame(link, ws, json.dumps(fp.make_ground_hello("gs-old", "standby", 0, proto=1)), 1.0)
        sent_before = len(await replies_on(ws, 1))
        await uplink._on_frame(link, ws, json.dumps(query("q1")), 2.0)
        await asyncio.sleep(0.05)
        replies = [json.loads(p) for p in ws.sent[sent_before:]]
        for outbox in list(uplink._outboxes.values()):
            outbox.close()
        return replies, list(uplink._inbox)

    replies, inbox = asyncio.run(scenario())
    assert replies == [] and inbox == []
