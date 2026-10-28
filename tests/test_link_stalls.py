"""One dead link must not stall the others (a pulled cable, plan section 2.7).

When a laptop-to-laptop cable is pulled, nothing closes: packets just stop.
The keepalive notices 20 to 40 s later and the connection then spends its
``close_timeout`` closing, during which a WebSocket ``send`` on it waits.
These tests reproduce that with real sockets through ``BlackHoleProxy``, a
TCP proxy that stops forwarding in both directions without closing
anything. The keepalive is shortened to 1 s so it fails within about 2 s,
and the close timeout is forced to the websockets default of 10 s, the
worst case, so the tests prove the links are independent rather than that
the close is short.

Edge side (``EdgeUplink``): a dead standby link must not delay telemetry to
the authority or a landing from it, and the watchdog must still land the
drone when the authority's link dies. Ground side (``GroundTransport``): a
dead follower edge must not delay the heartbeats or the
``emergency_land_all`` of a healthy edge.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import secrets
import socket
import threading
import time

import pytest
from websockets.protocol import State
from websockets.sync.client import connect as sync_connect

import flightrisk.transport as transport_module
from flightrisk import fleet_protocol as fp
from flightrisk.fleet_ground_link import GroundIdentity
from flightrisk.link_outbox import BULK, TELEMETRY, URGENT, LinkOutbox, is_open
from flightrisk.observability import StructuredLogger
from flightrisk.transport import GroundTransportSync, _call_on_loop
from tests.test_edge_uplink import (
    DRONE,
    FlyingEdge,
    Ground,
    connected_to_both,
    leader,
    standby,
    wait_for,
)

KEEPALIVE_S = 1.0
WORST_CLOSE_TIMEOUT_S = 10.0  # the websockets default
ZONE = {"x_cm": -300, "y_cm": -300, "w_cm": 600, "h_cm": 600}


class BlackHoleProxy:
    """A TCP proxy to 127.0.0.1:*port* that can stop forwarding both ways without closing (a pulled cable)."""

    def __init__(self, port: int):
        self._target = port
        self._frozen = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._server = socket.create_server(("127.0.0.1", 0))
        self._server.settimeout(0.1)
        self.port = self._server.getsockname()[1]
        self._start(self._accept)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def freeze(self) -> None:
        self._frozen.set()

    def _start(self, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        with self._lock:
            self._threads.append(thread)
        thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                upstream = socket.create_connection(("127.0.0.1", self._target), timeout=5)
            except OSError:
                client.close()
                continue
            client.settimeout(None)
            upstream.settimeout(None)
            with self._lock:
                self._sockets += [client, upstream]
            self._start(self._pipe, client, upstream)
            self._start(self._pipe, upstream, client)

    def _pipe(self, source: socket.socket, sink: socket.socket) -> None:
        while True:
            try:
                data = source.recv(65536)
            except OSError:
                return
            if not data:
                if not self._frozen.is_set():  # a pulled cable carries no FIN either
                    with contextlib.suppress(OSError):
                        sink.shutdown(socket.SHUT_WR)
                return
            if self._frozen.is_set():
                continue  # swallowed: no error, no close, nothing arrives
            try:
                sink.sendall(data)
            except OSError:
                return

    def close(self) -> None:
        self._stop.set()
        self._server.close()
        with self._lock:
            sockets, threads = list(self._sockets), list(self._threads)
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        for thread in threads:
            thread.join(timeout=2)


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


@pytest.fixture
def proxies():
    made: list[BlackHoleProxy] = []

    def build(port):
        proxy = BlackHoleProxy(port)
        made.append(proxy)
        return proxy

    yield build
    for proxy in made:
        proxy.close()


@pytest.fixture
def edge_keepalive(monkeypatch):
    """Edge connections ping every second and keep the 10 s default close timeout."""
    real = transport_module.websockets.connect

    def connect(url, **kwargs):
        kwargs.update(ping_interval=KEEPALIVE_S, ping_timeout=KEEPALIVE_S, close_timeout=WORST_CLOSE_TIMEOUT_S)
        return real(url, **kwargs)

    monkeypatch.setattr(transport_module.websockets, "connect", connect)


@pytest.fixture
def ground_keepalive(monkeypatch):
    """Ground connections ping every second and keep the 10 s default close timeout."""
    real = transport_module.websockets.serve

    def serve(handler, host, port, **kwargs):
        kwargs.update(ping_interval=KEEPALIVE_S, ping_timeout=KEEPALIVE_S, close_timeout=WORST_CLOSE_TIMEOUT_S)
        return real(handler, host, port, **kwargs)

    monkeypatch.setattr(transport_module.websockets, "serve", serve)


class Beater:
    """The leader's heartbeat loop, from a thread, as the dashboard drives it."""

    def __init__(self, ground, interval_s=0.3):
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ground, interval_s), daemon=True)
        self._thread.start()

    def _run(self, ground, interval_s):
        while not self._stop.is_set():
            # A frozen link may time the call out; keep beating.
            with contextlib.suppress(Exception):
                ground.transport.send_heartbeat(timeout=2)
            self._stop.wait(interval_s)

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)


class Arrivals:
    """Records when a ground's telemetry count grows."""

    def __init__(self, ground):
        self.times: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ground,), daemon=True)
        self._thread.start()

    def _run(self, ground):
        seen = len(ground.telemetry())
        while not self._stop.is_set():
            count = len(ground.telemetry())
            if count > seen:
                self.times.append(time.monotonic())
                seen = count
            time.sleep(0.01)

    def max_gap(self, start, end):
        points = [start] + [t for t in self.times if start < t <= end] + [end]
        return max(b - a for a, b in itertools.pairwise(points))

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)


def record_landings(edge):
    calls: list[tuple[str, float]] = []
    land_now = edge.flight.land_now
    edge.flight.land_now = lambda reason: (calls.append((reason, time.monotonic())), land_now(reason))[1]
    return calls


def link_state(edge, index):
    ws = edge.uplink._transports[index]._ws
    return None if ws is None else ws.state


# ---------------------------------------------------------------------------
# Edge side
# ---------------------------------------------------------------------------


def test_uplink_connections_use_a_short_close_timeout(grounds, edges, token):
    a = grounds(leader())
    edge = edges([a.url], token=token)
    assert wait_for(lambda: a.transport.drone_ids() == [DRONE])
    assert edge.uplink._transports[0]._ws.close_timeout == 1.0


def test_dead_standby_link_does_not_stall_telemetry_or_landing_for_the_authority(
    grounds, edges, token, proxies, edge_keepalive
):
    a, b = grounds(leader()), grounds(standby())
    proxy = proxies(b.port)
    edge = edges([a.url, proxy.url], token=token)
    assert connected_to_both(a, b)
    beater = Beater(a)
    arrivals = Arrivals(a)
    try:
        a.transport.send_to_drone(DRONE, fp.make_takeoff(fence=leader().fence))
        assert wait_for(lambda: edge.flying)
        time.sleep(1.0)

        frozen_at = time.monotonic()
        proxy.freeze()
        # The keepalive fails within about 2 s; the link then spends 10 s closing.
        assert wait_for(lambda: link_state(edge, 1) not in (None, State.OPEN), timeout=8)
        closing_at = time.monotonic()

        # The executor acks a plan from the control thread, then the authority lands the drone.
        plan = fp.make_assign_lanes(DRONE, 100, [{"direction": "left", "distance_cm": 20}], ZONE,
                                    fence=leader().fence, plan_id="p-1", plan_seq=1)
        a.transport.send_to_drone(DRONE, plan)
        time.sleep(0.3)
        landings = record_landings(edge)
        sent_at = time.monotonic()
        a.transport.send_to_drone(DRONE, fp.make_land(cmd_id="land-auth"))
        assert wait_for(lambda: landings, timeout=12)
        land_latency = landings[0][1] - sent_at

        time.sleep(max(0.0, closing_at + 3.5 - time.monotonic()))
        end = time.monotonic()
        still_closing = link_state(edge, 1) is not State.OPEN
        gap = arrivals.max_gap(frozen_at, end)
    finally:
        beater.stop()
        arrivals.stop()
    assert still_closing, "the measurement must cover the dead link's close"
    assert landings[0][0] == "ground_land"
    assert land_latency < 0.1, f"land from the authority took {land_latency:.3f} s"
    assert gap < 1.0, f"telemetry to the authority stopped for {gap:.2f} s"
    assert any(m["type"] == "ack" and m["for"] == "land" and m.get("cmd_id") == "land-auth" for m in a.controls)


def test_watchdog_still_lands_when_the_authority_link_dies(grounds, edges, token, proxies, edge_keepalive,
                                                            monkeypatch):
    link_loss_s = 1.5
    monkeypatch.setenv("FLIGHTRISK_LINK_LOSS_LAND_S", str(link_loss_s))
    a, b = grounds(leader()), grounds(standby())
    proxy = proxies(a.port)
    edge = edges([proxy.url, b.url], token=token)
    assert connected_to_both(a, b)
    beater = Beater(a)
    arrivals = Arrivals(b)
    try:
        a.transport.send_to_drone(DRONE, fp.make_takeoff(fence=leader().fence))
        assert wait_for(lambda: edge.flying)
        landings = record_landings(edge)
        time.sleep(1.0)
        frozen_at = time.monotonic()
        proxy.freeze()
        assert wait_for(lambda: landings, timeout=link_loss_s + 4)
        assert wait_for(lambda: link_state(edge, 0) not in (None, State.OPEN), timeout=8)
        time.sleep(1.5)
        gap = arrivals.max_gap(frozen_at, time.monotonic())
    finally:
        beater.stop()
        arrivals.stop()
    reason, landed_at = landings[0]
    assert reason == "link_loss"
    assert landed_at - frozen_at < link_loss_s + 1.0
    assert gap < 1.0, f"telemetry to the standby stopped for {gap:.2f} s while the authority link died"


# ---------------------------------------------------------------------------
# Ground side
# ---------------------------------------------------------------------------


class RawEdge:
    """A bare authenticated WebSocket client that records every frame with its arrival time."""

    def __init__(self, url, token):
        self.frames: list[tuple[float, dict]] = []
        self._session = sync_connect(url, close_timeout=0.5, open_timeout=5)
        self.ws = self._session.__enter__()
        self.ws.send(json.dumps({"type": "auth", "token": token}))
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        with contextlib.suppress(Exception):  # the connection ends at teardown
            for raw in self.ws:
                self.frames.append((time.monotonic(), json.loads(raw)))

    def times(self, mtype):
        return [t for t, m in self.frames if m.get("type") == mtype]

    def close(self):
        self._session.__exit__(None, None, None)
        self._thread.join(timeout=5)


def ground_states(ground):
    inner = ground._transport
    return ground._bridge.run(_call_on_loop(lambda: [ws.state for ws in inner.clients]), timeout=5)


def test_ground_connections_use_a_short_close_timeout(token):
    ground = GroundTransportSync(host="127.0.0.1", port=0, token=token, identity=GroundIdentity("mac", "leader", 1))
    ground.start()
    edge = RawEdge(f"ws://127.0.0.1:{ground.port}", token)
    try:
        inner = ground._transport
        assert wait_for(lambda: len(inner.clients) == 1)
        timeouts = ground._bridge.run(_call_on_loop(lambda: [ws.close_timeout for ws in inner.clients]), timeout=5)
        assert timeouts == [1.0]
    finally:
        edge.close()
        ground.stop()


def test_dead_follower_edge_does_not_stall_heartbeats_or_landing_of_a_healthy_edge(token, proxies,
                                                                                    ground_keepalive):
    interval = 1.0
    ground = GroundTransportSync(host="127.0.0.1", port=0, token=token, identity=GroundIdentity("mac", "leader", 1),
                                 heartbeat_interval_s=interval)
    ground.start()
    proxy = proxies(ground.port)
    healthy = RawEdge(f"ws://127.0.0.1:{ground.port}", token)
    follower = RawEdge(proxy.url, token)
    closing_seen = threading.Event()
    stop = threading.Event()

    def watch_states():
        while not stop.is_set():
            if any(state is not State.OPEN for state in ground_states(ground)):
                closing_seen.set()
            stop.wait(0.1)

    watcher = threading.Thread(target=watch_states, daemon=True)
    try:
        assert wait_for(lambda: len(healthy.times("heartbeat")) >= 2 and len(follower.times("heartbeat")) >= 2)
        watcher.start()
        frozen_at = time.monotonic()
        proxy.freeze()
        # Land everything while the follower's connection is closing (it closes 2 to 12 s after the freeze).
        assert closing_seen.wait(8)
        time.sleep(1.0)
        called_at = time.monotonic()
        reached = ground.emergency_land_all(timeout=15)
        returned_at = time.monotonic()
        window_end = frozen_at + 15.0
        time.sleep(max(0.0, window_end - time.monotonic()))
    finally:
        stop.set()
        watcher.join(timeout=5)
        healthy.close()
        follower.close()
        ground.stop()
    beats = [frozen_at] + [t for t in healthy.times("heartbeat") if frozen_at < t <= window_end] + [window_end]
    worst = max(b - a for a, b in itertools.pairwise(beats))
    assert worst < 1.5 * interval, f"the healthy edge went {worst:.2f} s without a heartbeat"
    landings = [t for t in healthy.times("emergency_land_all") if t >= called_at]
    assert landings and landings[0] - called_at < 0.2
    assert returned_at - called_at < 1.0, f"emergency_land_all took {returned_at - called_at:.2f} s to return"
    assert reached >= 1


# ---------------------------------------------------------------------------
# LinkOutbox
# ---------------------------------------------------------------------------


class SlowWebSocket:
    def __init__(self, block=False):
        self.sent: list[str] = []
        self.block = block
        self.state = State.OPEN

    async def send(self, payload):
        if self.block:
            await asyncio.Event().wait()
        self.sent.append(payload)


def test_outbox_serves_replies_first_and_keeps_only_the_latest_telemetry():
    async def scenario():
        ws = SlowWebSocket()
        outbox = LinkOutbox(ws, label="x", send_timeout_s=1.0, logger=StructuredLogger(component="test"),
                            telemetry_max=2, bulk_max=1)
        for n in range(5):
            outbox.put(TELEMETRY, f"t{n}")
        outbox.put(BULK, "d0")
        outbox.put(BULK, "d1")
        outbox.put(URGENT, "ack")
        outbox.put(URGENT, lambda: None)  # a reply that decides not to go out
        outbox.start()
        for _ in range(20):
            await asyncio.sleep(0)
        outbox.close()
        return ws.sent, outbox.dropped

    sent, dropped = asyncio.run(scenario())
    assert sent == ["ack", "t3", "t4", "d1"]
    assert dropped == {URGENT: 0, TELEMETRY: 3, BULK: 1}


def test_outbox_send_timeout_and_closing_connection():
    async def scenario():
        ws = SlowWebSocket(block=True)
        outbox = LinkOutbox(ws, label="x", send_timeout_s=0.05, logger=StructuredLogger(component="test"))
        outbox.start()
        outbox.put(URGENT, "a")
        outbox.put(URGENT, "b")
        await asyncio.sleep(0.3)
        timeouts = outbox.timeouts
        ws.state = State.CLOSING
        refused = outbox.put(URGENT, "c")
        outbox.close()
        return timeouts, refused

    timeouts, refused = asyncio.run(scenario())
    assert timeouts == 2 and refused is False
    closed = SlowWebSocket()
    closed.state = State.CLOSED
    assert not is_open(closed) and is_open(object())


def test_ground_send_skips_a_closing_connection_and_gives_up_on_a_stalled_one():
    ground = transport_module.GroundTransport(host="127.0.0.1", port=0, send_timeout_s=0.3)

    async def timed_send(ws):
        loop = asyncio.get_running_loop()
        started = loop.time()
        ok = await asyncio.wait_for(ground._send_json(ws, "{}"), timeout=5)  # fail, never hang
        return ok, loop.time() - started

    closing = SlowWebSocket(block=True)
    closing.state = State.CLOSING
    ok, took = asyncio.run(timed_send(closing))
    assert ok is False and took < 0.05 and closing.sent == []
    ok, took = asyncio.run(timed_send(SlowWebSocket(block=True)))  # open, but its buffer never drains
    assert ok is False and 0.25 < took < 1.0
    ok, _ = asyncio.run(timed_send(SlowWebSocket()))
    assert ok is True
