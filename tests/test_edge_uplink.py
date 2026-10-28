"""Two real ground stations and one edge over loopback WebSockets.

Everything here uses real ``websockets`` connections, real
``GroundTransportSync`` instances with protocol v2 identities, the real
``EdgeUplink`` and, for the flying edge, the real ``LaneExecutor`` on a
``FakeTelloController`` driven by ``edge_device.run_edge_device``. No socket
is stubbed: the PR #95 review found that stub-only tests missed real asyncio
behaviour.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time

import pytest
import websockets

from flightrisk import fleet_protocol as fp
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController
from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import (
    DroneFrameSource,
    _make_sender,
    _wire_emergency_callback,
    run_edge_device,
)
from flightrisk.edge_uplink import EdgeUplink, parse_ground_urls, safe_url
from flightrisk.fleet_ground_link import GroundIdentity
from flightrisk.observability import StructuredLogger
from flightrisk.transport import ClockSyncError, GroundTransportSync, _call_on_loop

WAIT_S = 15.0
DRONE = "drone-2"


def wait_for(predicate, timeout=WAIT_S, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return predicate()


class Ground:
    """A real ground transport that records what reaches it."""

    def __init__(self, identity=None, token=None, port=0):
        self.controls: list[dict] = []
        self.detections: list = []
        self.stale: list[int] = []
        self.transport = GroundTransportSync(
            host="127.0.0.1",
            port=port,
            token=token,
            on_control=self.controls.append,
            on_message=self.detections.append,
            identity=identity,
            on_stale_epoch=self.stale.append,
        )
        self.transport.start()

    @property
    def port(self):
        return self.transport.port

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}"

    def client_count(self):
        inner = self.transport._transport
        return self.transport._bridge.run(_call_on_loop(lambda: len(inner.clients)), timeout=5)

    def telemetry(self):
        return [m for m in self.controls if m["type"] == "telemetry"]

    def stop(self):
        self.transport.stop()


class FlyingEdge:
    """``edge_device --source fake --ground-urls ...`` in-process."""

    def __init__(self, urls, token=None, legacy=False, inflight=False):
        self.log = StructuredLogger(component="test-uplink-edge")
        self.runner = EdgeRunner(detector=None)
        self.uplink = EdgeUplink(
            urls, drone_id=DRONE, node_id="win", runner=self.runner, token=token,
            max_delay=0.5, base_delay=0.2, legacy=legacy, inflight_handover=inflight,
        )
        self.drone = FakeTelloController(name=DRONE, speed_cm_s=400.0)
        self.flight = LaneExecutor(
            self.drone, SafetyEnvelope.from_env(alt_slot_cm=100), DRONE,
            send=_make_sender(self.uplink, self.log), logger=self.log,
        )
        self.uplink.bind_executor(self.flight)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        run_edge_device(
            DroneFrameSource(self.drone, self.stop_event, min_interval_s=0.1),
            self.uplink, self.runner, logger=self.log, stop_event=self.stop_event, flight=self.flight,
        )

    def start(self):
        assert self.drone.connect()
        self.flight.start()
        _wire_emergency_callback(self.uplink, self.flight)
        self.uplink.connect(timeout=WAIT_S)
        self.thread.start()
        return self

    @property
    def flying(self):
        return bool(self.drone.state.is_flying)

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=10)
        self.flight.shutdown(timeout=2.0)
        self.uplink.disconnect(timeout=WAIT_S)


@pytest.fixture
def token():
    return secrets.token_urlsafe(12)


@pytest.fixture
def grounds(token):
    made: list[Ground] = []

    def build(identity=None, port=0):
        ground = Ground(identity, token=token, port=port)
        made.append(ground)
        return ground

    yield build
    for ground in made:
        ground.stop()


@pytest.fixture
def edges():
    made: list[FlyingEdge] = []

    def build(*args, **kwargs):
        edge = FlyingEdge(*args, **kwargs).start()
        made.append(edge)
        return edge

    yield build
    for edge in made:
        edge.close()


def leader(node="mac", epoch=1):
    return GroundIdentity(node, "leader", epoch)


def standby(node="win", epoch=0):
    return GroundIdentity(node, "standby", epoch)


def connected_to_both(a, b):
    return wait_for(lambda: a.transport.drone_ids() == [DRONE] and b.transport.drone_ids() == [DRONE])


# ---------------------------------------------------------------------------


def test_leader_is_the_authority_and_both_grounds_see_the_drone(grounds, edges, token):
    a, b = grounds(leader()), grounds(standby())
    edge = edges([a.url, b.url], token=token)
    assert connected_to_both(a, b)
    assert edge.uplink.authority.authority_leader == "mac" and edge.uplink.authority.epoch_seen == 1
    assert a.transport.peer_proto(DRONE) == 2 and b.transport.peer_proto(DRONE) == 2
    # Telemetry, completed for v2, reaches both grounds.
    assert wait_for(lambda: a.telemetry() and b.telemetry())
    for ground in (a, b):
        tel = ground.telemetry()[-1]
        assert tel["epoch"] == 1 and tel["state"] == "idle" and fp.validate_message(tel, proto=2)
    # A standby is behind the edge's epoch by design: that is not a stale leader.
    assert b.stale == []


def test_detections_go_to_the_authority_only(grounds, edges, token):
    a, b = grounds(leader()), grounds(standby())
    # The standby is listed first, so "the first connected ground" is wrong.
    edges([b.url, a.url], token=token)
    assert connected_to_both(a, b)
    assert wait_for(lambda: len(a.detections) >= 3)
    time.sleep(0.3)
    assert b.detections == []


def test_tied_leader_cannot_fly_or_feed_the_watchdog_and_its_land_still_lands(grounds, edges, token, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_LINK_LOSS_LAND_S", "1.5")
    a, b = grounds(leader("mac", 1)), grounds(leader("win", 1))
    edge = edges([a.url, b.url], token=token)
    assert connected_to_both(a, b)
    assert edge.uplink.authority.authority_leader in ("mac", "win")
    first, second = (a, b) if edge.uplink.authority.authority_leader == "mac" else (b, a)
    first_fence = first.transport._transport.identity.fence
    second_fence = second.transport._transport.identity.fence

    reply = second.transport.send_command(DRONE, fp.make_takeoff(fence=second_fence), timeout_s=2.0)
    assert reply["type"] == "nack" and reply["reason"] == "not_authority" and reply["epoch_seen"] == 1
    assert not edge.flying

    reasons = []
    land_now = edge.flight.land_now
    edge.flight.land_now = lambda reason: (reasons.append(reason), land_now(reason))[1]
    first.transport.send_heartbeat()
    first.transport.send_to_drone(DRONE, fp.make_takeoff(fence=first_fence))
    assert wait_for(lambda: edge.flying)
    # Only the second ground keeps sending heartbeats: they must not hold the
    # drone up, so the link-loss watchdog lands it.
    stop = threading.Event()

    def beat():
        while not stop.is_set():
            second.transport.send_heartbeat()
            stop.wait(0.2)

    beater = threading.Thread(target=beat, daemon=True)
    beater.start()
    try:
        assert wait_for(lambda: not edge.flying, timeout=8.0)
    finally:
        stop.set()
        beater.join(timeout=2)
    assert reasons and reasons[0] == "link_loss"


def test_standby_land_lands_the_drone_the_leader_flies(grounds, edges, token):
    a, b = grounds(leader()), grounds(standby())
    edge = edges([a.url, b.url], token=token)
    assert connected_to_both(a, b)
    a.transport.send_to_drone(DRONE, fp.make_takeoff(fence=leader().fence))
    assert wait_for(lambda: edge.flying)
    reply = b.transport.send_command(DRONE, fp.make_land(cmd_id="land-b"), timeout_s=2.0)
    assert reply["type"] == "ack" and reply["for"] == "land" and reply["cmd_id"] == "land-b"
    assert wait_for(lambda: not edge.flying)


def test_new_leader_waits_for_the_ground_and_the_old_one_becomes_stale(grounds, edges, token):
    a, b = grounds(leader("mac", 1)), grounds(standby("win", 0))
    edge = edges([a.url, b.url], token=token)
    assert connected_to_both(a, b)
    a.transport.send_to_drone(DRONE, fp.make_takeoff(fence=leader("mac", 1).fence))
    assert wait_for(lambda: edge.flying)

    b.transport.set_identity(leader("win", 2))
    b.transport.send_heartbeat()
    time.sleep(0.3)
    assert edge.uplink.authority.epoch_seen == 1, "adopted a new epoch in flight"
    a.transport.send_heartbeat()

    b.transport.send_command(DRONE, fp.make_land(), timeout_s=2.0)
    assert wait_for(lambda: not edge.flying)
    assert wait_for(lambda: (b.transport.send_heartbeat(), edge.uplink.authority.epoch_seen == 2)[1])
    assert edge.uplink.authority.authority_leader == "win"

    reply = a.transport.send_command(DRONE, fp.make_takeoff(fence=leader("mac", 1).fence), timeout_s=2.0)
    assert reply["reason"] == "stale_epoch" and reply["epoch_seen"] == 2
    assert wait_for(lambda: a.stale == [2] or 2 in a.stale)
    assert not edge.flying


def test_clock_sync_works_only_with_the_authority(grounds, edges, token):
    a, b = grounds(leader()), grounds(leader("win", 1))
    edge = edges([a.url, b.url], token=token)
    assert connected_to_both(a, b)
    owner, other = (a, b) if edge.uplink.authority.authority_leader == "mac" else (b, a)
    estimate = owner.transport.sync_clock(DRONE, rounds=4, round_timeout_s=1.0)
    assert estimate.rounds >= 3
    assert wait_for(lambda: edge.uplink.clock.offset_s is not None)
    with pytest.raises(ClockSyncError):
        other.transport.sync_clock(DRONE, rounds=3, round_timeout_s=0.2)


def test_same_leader_reconnecting_keeps_the_authority(grounds, edges, token):
    a, b = grounds(leader()), grounds(standby())
    edge = edges([a.url, b.url], token=token)
    assert connected_to_both(a, b)
    first_key = edge.uplink.authority.authority_key
    port = a.port
    a.stop()
    assert wait_for(lambda: edge.uplink.authority.authority_key is None)
    a2 = grounds(leader(), port=port)
    assert wait_for(lambda: edge.uplink.authority.authority_key not in (None, first_key))
    assert edge.uplink.authority.epoch_seen == 1
    # The edge adopts the new connection on the ground's hello; the ground
    # can address the drone only once the edge's hello has reached it.
    assert wait_for(lambda: a2.transport.drone_ids() == [DRONE])
    assert a2.transport.send_to_drone(DRONE, fp.make_takeoff(fence=leader().fence))
    assert wait_for(lambda: edge.flying)


def test_v1_ground_is_closed_in_strict_mode_but_its_emergency_lands(grounds, edges, token):
    a, v1 = grounds(leader()), grounds(None)
    edge = edges([a.url, v1.url], token=token)
    assert wait_for(lambda: a.transport.drone_ids() == [DRONE] and v1.client_count() == 1)
    closes = []
    v1_link = edge.uplink._transports[1]
    drop = v1_link.drop_connection

    async def record_drop(ws, code, reason):
        closes.append((code, reason))
        await drop(ws, code, reason)

    v1_link.drop_connection = record_drop
    a.transport.send_to_drone(DRONE, fp.make_takeoff(fence=leader().fence))
    assert wait_for(lambda: edge.flying)
    assert v1.transport.emergency_land_all() == 1
    assert wait_for(lambda: not edge.flying)
    # Its first fleet message (a heartbeat) makes it a v1 link: closed.
    v1.transport.send_heartbeat()
    assert wait_for(lambda: closes) == [(4010, "v1_link_refused")]
    assert v1.telemetry() == [] and v1.detections == []


def test_legacy_mode_keeps_a_v1_ground_as_monitor_only(grounds, edges, token):
    a, v1 = grounds(leader()), grounds(None)
    edge = edges([a.url, v1.url], token=token, legacy=True)
    assert wait_for(lambda: a.transport.drone_ids() == [DRONE] and v1.client_count() == 1)
    v1.transport.send_heartbeat()
    assert wait_for(lambda: v1.telemetry())
    tel = v1.telemetry()[-1]
    assert "epoch" not in tel and "state" not in tel  # shaped for v1
    v1.transport.send_to_drone(DRONE, fp.make_takeoff())
    time.sleep(0.5)
    assert not edge.flying
    assert v1.detections == []


# ---------------------------------------------------------------------------
# A scripted ground on a raw websockets server
# ---------------------------------------------------------------------------


class ScriptedGround:
    """A websockets server that sends a script of raw frames and records the edge's replies."""

    def __init__(self, frames):
        self.frames = frames
        self.received: list[dict] = []
        self.close_code = None
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())

    async def _serve(self):
        async def handler(ws):
            for frame in self.frames:
                await ws.send(frame)
                await asyncio.sleep(0.05)
            try:
                async for raw in ws:
                    self.received.append(json.loads(raw))
            except websockets.ConnectionClosed:
                pass
            self.close_code = ws.close_code

        self.server = await websockets.serve(handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.stop_future = self.loop.create_future()
        self.ready.set()
        await self.stop_future
        self.server.close()
        await self.server.wait_closed()

    def stop(self):
        self.loop.call_soon_threadsafe(self.stop_future.set_result, None)
        self.thread.join(5)


def _uplink(port):
    uplink = EdgeUplink([f"ws://127.0.0.1:{port}"], drone_id=DRONE, node_id="win", max_delay=30.0, base_delay=30.0)
    uplink.connect(timeout=WAIT_S)
    return uplink


def test_lenient_landing_without_cmd_id_executes_and_is_acked_without_one():
    hello = json.dumps(fp.make_ground_hello("mac", "leader", 1))
    ground = ScriptedGround([hello, '{"type": "heartbeat", "type": "land", "cmd_id": "x"}'])
    uplink = _uplink(ground.port)
    try:
        got = wait_for(lambda: uplink.recv(0.1) or None)
        assert got == {"type": "land"}
        acks = wait_for(lambda: [m for m in ground.received if m["type"] == "ack"])
        assert acks[0]["for"] == "land" and "cmd_id" not in acks[0] and acks[0]["drone_id"] == DRONE
    finally:
        uplink.disconnect()
        ground.stop()


def test_node_id_change_on_a_live_connection_closes_it():
    frames = [json.dumps(fp.make_ground_hello("mac", "leader", 1)), json.dumps(fp.make_ground_hello("evil", "leader", 9))]
    ground = ScriptedGround(frames)
    uplink = _uplink(ground.port)
    try:
        assert wait_for(lambda: ground.close_code == 4010)
        assert uplink.authority.epoch_seen == 1
    finally:
        uplink.disconnect()
        ground.stop()


def test_large_frames_are_decoded_off_the_loop_and_a_landing_behind_junk_still_lands():
    hello = json.dumps(fp.make_ground_hello("mac", "leader", 1))
    big_target = json.dumps({"type": "set_target", "reid_embedding": [0.001 * i for i in range(2000)]})
    junk = '{"type": "land' + "x" * 900_000 + '"'  # names "land" but is not JSON: costs both parses
    frames = [hello, big_target, junk, json.dumps(fp.make_emergency_land_all())]
    ground = ScriptedGround(frames)
    uplink = _uplink(ground.port)
    try:
        seen = []
        assert wait_for(lambda: (seen.append(uplink.recv(0.1)), any(
            m and m["type"] == "emergency_land_all" for m in seen))[1])
        assert any(m and m["type"] == "set_target" for m in seen)
    finally:
        uplink.disconnect()
        ground.stop()


def test_hello_announcing_v1_is_refused_without_legacy():
    ground = ScriptedGround([json.dumps(fp.make_ground_hello("mac", "leader", 1, proto=1))])
    uplink = _uplink(ground.port)
    try:
        assert wait_for(lambda: ground.close_code == 4010)
        assert uplink.authority.epoch_seen == 0
    finally:
        uplink.disconnect()
        ground.stop()


def test_ground_url_parsing():
    assert parse_ground_urls(" ws://127.0.0.1:9000 , ws://10.42.0.2:9000,ws://127.0.0.1:9000") == (
        "ws://127.0.0.1:9000", "ws://10.42.0.2:9000")
    assert parse_ground_urls("") == () and parse_ground_urls(None) == ()
    for bad in ("ws://a:1,,ws://b:2", "http://x:1", "ws://", "127.0.0.1:9000"):
        with pytest.raises(ValueError):
            parse_ground_urls(bad)
    assert safe_url("ws://user:secret@10.42.0.2:9000/x") == "ws://10.42.0.2:9000/x"


# ---------------------------------------------------------------------------
# Review round 2: the reader never waits, and a landing is never dropped
# ---------------------------------------------------------------------------


class FakeSocket:
    """Stands in for a WebSocket when the uplink's frame handler is driven directly."""

    def __init__(self, block_sends=False):
        self.sent: list[str] = []
        self.block_sends = block_sends

    async def send(self, payload):
        if self.block_sends:
            await asyncio.Event().wait()
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        return None


def _open_fake_link(uplink, ws):
    """What ``EdgeTransport`` does when a connection opens, without a socket (on the running loop)."""
    uplink._inbox_event = asyncio.Event()
    transport = uplink._transports[0]
    transport._ws, transport._connected = ws, True
    transport._on_connected(ws)
    return transport


@pytest.mark.parametrize("frame", [
    json.dumps(fp.make_land(cmd_id="L1")),
    json.dumps({**fp.make_land(cmd_id="L1"), "pad": "y" * 5000}),  # decoded in a worker thread
    json.dumps(fp.make_emergency_land_all()),
], ids=["land", "large-land", "emergency"])
def test_landing_on_a_connection_already_marked_down_still_lands(frame):
    emergencies = []

    async def scenario():
        uplink = EdgeUplink(["ws://127.0.0.1:1"], drone_id=DRONE, node_id="win", on_emergency_land=emergencies.append)
        ws = FakeSocket()
        transport = _open_fake_link(uplink, ws)
        await uplink._on_frame(transport, ws, json.dumps(fp.make_ground_hello("win", "standby", 0)), 0.0)
        # Another task's send saw the connection close and marked it down
        # while frames already received on it were still being read.
        transport._connected = False
        await uplink._on_frame(transport, ws, frame, 0.0)
        # The reader then ends; a frame it had buffered is still handled.
        transport._on_closed(ws)
        await uplink._on_frame(transport, ws, frame, 0.0)
        await uplink._on_frame(transport, ws, json.dumps(fp.make_heartbeat(1, fence=fp.Fence(1, "win"))), 0.0)
        return [m["type"] for m, _ in uplink._inbox], ws in uplink.authority._conns

    inbox, conn_kept = asyncio.run(scenario())
    expected = json.loads(frame)["type"]
    assert inbox == [expected, expected]
    assert not conn_kept, "a closed connection was re-created"
    if expected == "emergency_land_all":
        assert wait_for(lambda: len(emergencies) == 2, timeout=5)


def test_landing_read_after_the_link_was_marked_down_lands_and_is_acked_over_real_sockets():
    frames = [json.dumps(fp.make_ground_hello("win", "standby", 0)), json.dumps(fp.make_land(cmd_id="L-late"))]
    ground = ScriptedGround(frames)
    uplink = EdgeUplink([f"ws://127.0.0.1:{ground.port}"], drone_id=DRONE, node_id="win",
                        max_delay=30.0, base_delay=30.0)
    handle = uplink._on_frame

    async def mark_down_before_the_landing(transport, ws, raw, received_at):
        if '"land"' in raw:
            transport._connected = False  # as when a concurrent send hits ConnectionClosed
        await handle(transport, ws, raw, received_at)

    uplink._on_frame = mark_down_before_the_landing
    uplink.connect(timeout=WAIT_S)
    try:
        got = wait_for(lambda: uplink.recv(0.1) or None)
        assert got is not None and got["type"] == "land" and got["cmd_id"] == "L-late"
        acks = wait_for(lambda: [m for m in ground.received if m["type"] == "ack"])
        assert acks and acks[0]["for"] == "land" and acks[0]["cmd_id"] == "L-late"
    finally:
        uplink.disconnect()
        ground.stop()


def test_reader_never_waits_for_a_reply_to_be_written():
    async def scenario():
        uplink = EdgeUplink(["ws://127.0.0.1:1"], drone_id=DRONE, node_id="win")
        ws = FakeSocket(block_sends=True)  # a peer that stopped reading
        transport = _open_fake_link(uplink, ws)
        for frame in (fp.make_ground_hello("mac", "leader", 1), {"type": "ping", "t1": 1.0}, fp.make_land(cmd_id="L")):
            await asyncio.wait_for(uplink._on_frame(transport, ws, json.dumps(frame), 0.0), timeout=0.5)
        inbox = [m["type"] for m, _ in uplink._inbox]
        transport._on_closed(ws)
        return inbox

    assert asyncio.run(scenario()) == ["land"]


def test_frames_after_a_refusal_are_not_processed_but_a_landing_still_lands():
    seen: list[str] = []
    drops: list[tuple[int, str]] = []

    async def scenario():
        uplink = EdgeUplink(["ws://127.0.0.1:1"], drone_id=DRONE, node_id="win")
        ws = FakeSocket()
        transport = _open_fake_link(uplink, ws)

        async def drop(target, code, reason):
            drops.append((code, reason))

        transport.drop_connection = drop
        on_message = uplink.authority.on_message
        uplink.authority.on_message = lambda key, msg: (seen.append(msg["type"]), on_message(key, msg))[1]
        # A v1 heartbeat (no hello first) is refused in strict mode; the
        # frames already behind it on the connection must not count.
        for frame in (fp.make_heartbeat(1), fp.make_ground_hello("mac", "leader", 1), fp.make_land()):
            await uplink._on_frame(transport, ws, json.dumps(frame), 0.0)
        await asyncio.sleep(0.05)
        inbox = [m["type"] for m, _ in uplink._inbox]
        transport._on_closed(ws)
        return inbox, uplink.authority.epoch_seen

    inbox, epoch = asyncio.run(scenario())
    assert seen == ["heartbeat", "land"]
    assert drops == [(4010, "v1_link_refused")]
    assert inbox == ["land"] and epoch == 0


def test_refused_v1_ground_is_not_reconnected_in_a_tight_loop(token):
    from tests.test_fleet_authority_hardening import RecordingLog

    v1 = GroundTransportSync(host="127.0.0.1", port=0, token=token, heartbeat_interval_s=0.1)
    v1.start()
    log = RecordingLog()
    uplink = EdgeUplink([f"ws://127.0.0.1:{v1.port}"], drone_id=DRONE, node_id="win", token=token,
                        base_delay=0.05, max_delay=0.1, logger=log)
    link = uplink._transports[0]
    opened: list[float] = []
    on_connected = link._on_connected
    link._on_connected = lambda ws: (opened.append(time.monotonic()), on_connected(ws))[1]
    try:
        uplink.connect(timeout=WAIT_S)
        time.sleep(3.0)
        count = len(opened)
    finally:
        uplink.disconnect()
        v1.stop()
    # Refused at its first heartbeat each time; reconnects wait 1 s, then 2 s.
    assert 1 <= count <= 3, f"{count} connections in 3 s"
    assert log.count("edge_connection_refused") == 1


def test_connect_does_not_wait_for_a_ground_that_never_answers(grounds, token):
    import socket

    a = grounds(leader())
    silent = socket.create_server(("127.0.0.1", 0))  # completes TCP, never answers the WebSocket handshake
    uplink = EdgeUplink([f"ws://127.0.0.1:{silent.getsockname()[1]}", a.url], drone_id=DRONE, node_id="win",
                        token=token)
    try:
        started = time.monotonic()
        uplink.connect(timeout=WAIT_S)
        took = time.monotonic() - started
        assert took < 4.0, f"connect() blocked for {took:.1f} s"
        assert wait_for(lambda: uplink._transports[1].connected)
    finally:
        uplink.disconnect()
        silent.close()
