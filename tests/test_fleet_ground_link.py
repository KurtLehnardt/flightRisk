"""Ground side of protocol v2 links (``flightrisk/fleet_ground_link.py`` and ``GroundTransport``).

Real ``GroundTransport`` listeners on loopback with raw ``websockets``
clients playing the edge, plus unit tests of the bookkeeping classes.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque

import pytest
import websockets

from flightrisk import fleet_protocol as fp
from flightrisk.fleet_ground_link import (
    CommandTracker,
    GroundIdentity,
    GroundLinks,
    PeerVersionError,
)
from flightrisk.transport import (
    _SUPERSEDED_BY_EMERGENCY,
    EdgeTransport,
    GroundTransport,
)

LEADER = GroundIdentity("mac", "leader", 3)
TOKEN = "t0ken-for-tests"


def edge_hello(drone_id="drone-1", node_id="win", proto=2, epoch_seen=3):
    return fp.make_edge_hello(drone_id, node_id, epoch_seen, plan_id=None, lane_index=None,
                              is_flying=False, battery=90, proto=proto)


class RawEdge:
    """A raw websockets client standing in for an edge."""

    def __init__(self, ws):
        self.ws = ws
        self.frames: deque[dict] = deque()

    @classmethod
    async def connect(cls, port):
        ws = await websockets.connect(f"ws://127.0.0.1:{port}")
        await ws.send(json.dumps({"type": "auth", "token": TOKEN}))
        return cls(ws)

    async def next(self, timeout=2.0):
        return json.loads(await asyncio.wait_for(self.ws.recv(), timeout))

    async def send(self, msg):
        await self.ws.send(json.dumps(msg))

    async def closed_with(self, timeout=3.0):
        try:
            while True:
                await asyncio.wait_for(self.ws.recv(), timeout)
        except websockets.ConnectionClosed:
            return self.ws.close_code


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


async def ground(**kwargs):
    controls = []
    transport = GroundTransport(host="127.0.0.1", port=0, token=TOKEN, on_control=controls.append, **kwargs)
    await transport.start()
    return transport, controls


async def settle(predicate, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# Hello first, fencing, versions
# ---------------------------------------------------------------------------


def test_hello_is_the_first_frame_even_with_a_fast_heartbeat_and_heartbeats_are_fenced():
    async def main():
        transport, _ = await ground(identity=LEADER, heartbeat_interval_s=0.005)
        try:
            for _ in range(5):
                edge = await RawEdge.connect(transport.port)
                first = await edge.next()
                assert first == fp.make_ground_hello("mac", "leader", 3)
                beat = await edge.next()
                assert beat["type"] == "heartbeat" and beat["epoch"] == 3 and beat["leader_id"] == "mac"
                await edge.ws.close()
        finally:
            await transport.stop()

    run(main())


def test_standby_sends_no_heartbeats():
    async def main():
        transport, _ = await ground(identity=GroundIdentity("win", "standby", 0))
        try:
            edge = await RawEdge.connect(transport.port)
            assert (await edge.next())["role"] == "standby"
            assert await transport.send_heartbeat() == 0
        finally:
            await transport.stop()

    run(main())


def test_v2_nack_is_validated_with_the_agreed_version():
    nack = fp.make_nack("hold", "drone-1", "not_flying", "c-1", epoch_seen=3)

    async def main(identity):
        transport, controls = await ground(identity=identity)
        try:
            edge = await RawEdge.connect(transport.port)
            if identity is not None:
                await edge.next()
                await edge.send(edge_hello())
            await edge.send(nack)
            await settle(lambda: controls, 0.5)
            return controls
        finally:
            await transport.stop()

    assert run(main(LEADER)) == [nack]
    # Without an identity the link is v1, which has no nack for hold.
    assert run(main(None)) == []


def test_late_edge_hello_closes_the_connection():
    async def main():
        transport, _ = await ground(identity=LEADER)
        transport._links.hello_timeout_s = 0.05
        try:
            edge = await RawEdge.connect(transport.port)
            await edge.next()
            await asyncio.sleep(0.1)
            tel = {"type": "telemetry", "drone_id": "drone-1", "is_flying": False, "battery": 50}
            await edge.send({**tel, **{k: None for k in ("height_cm", "rel_x_cm", "rel_y_cm", "heading_deg",
                                                          "lane_index", "lanes_total", "link_age_s",
                                                          "takeoff_at_local_s")}})
            await asyncio.sleep(0.05)
            await edge.send(edge_hello())
            assert await edge.closed_with() == 4010
        finally:
            await transport.stop()

    run(main())


def test_node_id_change_closes_the_connection():
    async def main():
        transport, _ = await ground(identity=LEADER)
        try:
            edge = await RawEdge.connect(transport.port)
            await edge.next()
            await edge.send(edge_hello(node_id="win"))
            await edge.send(edge_hello(node_id="other"))
            assert await edge.closed_with() == 4010
        finally:
            await transport.stop()

    run(main())


def test_ground_requiring_v2_closes_v1_edges():
    async def main():
        transport, _ = await ground(identity=LEADER, local_min_proto=2)
        try:
            edge = await RawEdge.connect(transport.port)
            await edge.next()
            await edge.send(edge_hello(proto=1))
            assert await edge.closed_with() == 4010
        finally:
            await transport.stop()

    run(main())


def test_hold_cannot_be_sent_to_a_v1_edge_and_broadcast_skips_it():
    hold = fp.make_hold(LEADER.fence, cmd_id="h-1")

    async def main():
        transport, _ = await ground(identity=LEADER)
        transport._links.hello_timeout_s = 0.0
        try:
            edge = await RawEdge.connect(transport.port)
            await edge.next()
            ack = fp.make_ack_assign_lanes("drone-1", 80, 1)
            await edge.send(ack)
            await settle(lambda: transport.drone_ids == ["drone-1"])
            with pytest.raises(PeerVersionError):
                await transport.send_to_drone("drone-1", hold)
            assert await transport.broadcast_control(hold) == 0
            assert await transport.send_to_drone("drone-1", fp.make_land()) is True
            assert (await edge.next())["type"] == "land"
        finally:
            await transport.stop()

    run(main())


def test_set_identity_announces_the_new_epoch_and_fences_with_it():
    async def main():
        transport, _ = await ground(identity=GroundIdentity("win", "standby", 0))
        try:
            edge = await RawEdge.connect(transport.port)
            await edge.next()
            assert await transport.set_identity(GroundIdentity("win", "leader", 4)) == 1
            assert (await edge.next()) == fp.make_ground_hello("win", "leader", 4)
            await transport.send_heartbeat()
            beat = await edge.next()
            assert (beat["epoch"], beat["leader_id"]) == (4, "win")
            with pytest.raises(ValueError):
                await transport.set_identity(GroundIdentity("mac", "leader", 5))
        finally:
            await transport.stop()

    run(main())


def test_stale_leader_signal_from_a_nack_and_from_an_edge_hello():
    seen = []

    async def main():
        transport, _ = await ground(identity=LEADER, on_stale_epoch=seen.append)
        try:
            edge = await RawEdge.connect(transport.port)
            await edge.next()
            await edge.send(edge_hello(epoch_seen=5))
            await edge.send(fp.make_nack("takeoff", "drone-1", "stale_epoch", "c-1", epoch_seen=6))
            await settle(lambda: len(seen) == 2)
        finally:
            await transport.stop()

    run(main())
    assert seen == [5, 6]


# ---------------------------------------------------------------------------
# Ack correlation and retries
# ---------------------------------------------------------------------------


async def two_v2_edges(transport):
    edges = {}
    for drone in ("drone-1", "drone-2"):
        edge = await RawEdge.connect(transport.port)
        await edge.next()
        await edge.send(edge_hello(drone_id=drone, node_id=f"n-{drone}"))
        edges[drone] = edge
    await settle(lambda: transport.drone_ids == ["drone-1", "drone-2"])
    return edges


def test_broadcast_hold_acks_are_matched_by_drone_and_command():
    async def main():
        transport, _ = await ground(identity=LEADER)
        try:
            edges = await two_v2_edges(transport)
            hold = fp.make_hold(LEADER.fence, cmd_id="same")
            waits = [asyncio.ensure_future(transport.send_command(d, hold, timeout_s=2.0)) for d in edges]
            for edge in edges.values():
                assert (await edge.next())["cmd_id"] == "same"
            await edges["drone-2"].send(fp.make_ack("hold", "drone-2", "same"))
            await edges["drone-1"].send(fp.make_nack("hold", "drone-1", "not_flying", "same"))
            first, second = await asyncio.gather(*waits)
            assert (first["type"], first["drone_id"]) == ("nack", "drone-1")
            assert (second["type"], second["drone_id"]) == ("ack", "drone-2")
        finally:
            await transport.stop()

    run(main())


def test_landing_ack_without_cmd_id_completes_the_pending_landing():
    async def main():
        transport, _ = await ground(identity=LEADER)
        try:
            edges = await two_v2_edges(transport)
            wait = asyncio.ensure_future(
                transport.send_command("drone-1", fp.make_land(cmd_id="L-1"), timeout_s=0.3, attempts=5))
            await edges["drone-1"].next()
            await edges["drone-1"].send(fp.make_ack("land", "drone-1", None))
            reply = await wait
            assert reply["for"] == "land" and "cmd_id" not in reply
        finally:
            await transport.stop()

    run(main())


@pytest.mark.parametrize("mtype,attempts,expected", [
    ("takeoff", 3, 1),
    ("start_at", 3, 1),
    ("nudge", 3, 1),
    ("resume", 3, 1),
    ("hold", 3, 3),
])
def test_only_retryable_commands_are_resent_with_the_same_cmd_id(mtype, attempts, expected):
    builders = {
        "takeoff": lambda: fp.make_takeoff(fence=LEADER.fence),
        "start_at": lambda: fp.make_start_at(1.0, fence=LEADER.fence, plan_id="p-1"),
        "nudge": lambda: fp.make_nudge(LEADER.fence, "left", 50),
        "resume": lambda: fp.make_resume(LEADER.fence, plan_id="p-1"),
        "hold": lambda: fp.make_hold(LEADER.fence),
    }

    async def main():
        transport, _ = await ground(identity=LEADER)
        try:
            edge = (await two_v2_edges(transport))["drone-1"]
            msg = builders[mtype]()
            assert await transport.send_command("drone-1", msg, timeout_s=0.1, attempts=attempts) is None
            got = []
            while True:
                try:
                    got.append(await edge.next(timeout=0.2))
                except (asyncio.TimeoutError, TimeoutError):
                    break
            sent = [m for m in got if m["type"] == mtype]
            assert len(sent) == expected and {m["cmd_id"] for m in sent} == {msg["cmd_id"]}
        finally:
            await transport.stop()

    run(main())


def test_newer_hold_ends_pending_assign_lanes_retries():
    async def main():
        transport, _ = await ground(identity=LEADER)
        try:
            await two_v2_edges(transport)
            lanes = fp.make_assign_lanes("drone-1", 100, [{"direction": "forward", "distance_cm": 100}],
                                         {"x_cm": 0, "y_cm": 0, "w_cm": 100, "h_cm": 100},
                                         fence=LEADER.fence, plan_id="p-1", plan_seq=1)
            pending = asyncio.ensure_future(transport.send_command("drone-1", lanes, timeout_s=5.0, attempts=5))
            await asyncio.sleep(0.05)
            asyncio.ensure_future(transport.send_command("drone-1", fp.make_hold(LEADER.fence), timeout_s=0.1))
            assert await asyncio.wait_for(pending, 1.0) is None
        finally:
            await transport.stop()

    run(main())


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_links_send_only_landing_before_hello_and_decide_v1_after_the_timeout():
    links = GroundLinks(LEADER, hello_timeout_s=2.0)
    links.opened("ws", now=0.0)
    assert links.may_send("ws", {"type": "land"}) and links.may_send("ws", {"type": "emergency_land_all"})
    assert not links.may_send("ws", fp.make_heartbeat(1, fence=LEADER.fence))
    links.mark_hello_sent("ws")
    assert links.may_send("ws", fp.make_heartbeat(1, fence=LEADER.fence))
    assert links.proto("ws", now=1.0) == 1 and links.peer_proto("ws") is None
    assert links.on_edge_hello("ws", edge_hello(), now=1.5).close is False
    assert links.proto("ws", now=9.0) == 2
    late = GroundLinks(LEADER, hello_timeout_s=2.0)
    late.opened("ws", now=0.0)
    assert late.on_edge_hello("ws", edge_hello(), now=2.5).reason == "late_hello"


def test_pending_version_keeps_v2_fields_and_holds_back_v2_only_commands():
    links = GroundLinks(LEADER, hello_timeout_s=2.0)
    links.opened("ws", now=0.0)
    links.mark_hello_sent("ws")
    beat = fp.make_heartbeat(1, fence=LEADER.fence)
    assert links.shape("ws", beat, now=0.5) == beat
    with pytest.raises(PeerVersionError):
        links.shape("ws", fp.make_hold(LEADER.fence), now=0.5)
    # After the timeout the link is v1: the fence is stripped, hold refused.
    assert links.shape("ws", beat, now=2.5) == {"type": "heartbeat", "seq": 1}
    with pytest.raises(PeerVersionError):
        links.shape("ws", fp.make_hold(LEADER.fence), now=2.5)


def test_identity_validates_and_fences_only_a_leader():
    assert LEADER.fence == fp.Fence(3, "mac")
    assert GroundIdentity("win", "standby", 0).fence is None
    for bad in (("mac", "leader", 0), ("bad id", "leader", 1), ("mac", "boss", 1)):
        with pytest.raises(fp.ProtocolError):
            GroundIdentity(*bad)


def test_emergency_supersedes_every_pending_motion_retry():
    async def main():
        tracker = CommandTracker()
        one = tracker.register("drone-1", fp.make_hold(LEADER.fence))
        two = tracker.register("drone-2", fp.make_takeoff(fence=LEADER.fence))
        land = tracker.register("drone-2", fp.make_land())
        assert tracker.supersede(None, "emergency_land_all") == 2
        assert one.future.result() is None and two.future.result() is None and not land.future.done()

    run(main())


# ---------------------------------------------------------------------------
# EdgeTransport changes
# ---------------------------------------------------------------------------


def test_emergency_also_discards_queued_resume_and_nudge():
    assert {"takeoff", "start_at", "resume", "nudge"} <= _SUPERSEDED_BY_EMERGENCY


def test_edge_send_validates_with_the_agreed_version():
    async def main():
        edge = EdgeTransport(ws_url="ws://127.0.0.1:1")
        nack = fp.make_nack("hold", "drone-1", "not_flying", "c-1")
        with pytest.raises(fp.ProtocolError):
            await edge.send(nack)  # v1 link: no nack for hold
        edge.proto = 2
        assert await edge.send(nack) is False  # valid, but not connected
        assert await edge.send(nack, proto=2) is False

    run(main())
