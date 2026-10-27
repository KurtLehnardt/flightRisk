"""Tests of the network simulator itself (tests/support/netsim.py).

A real ``websockets`` server and client talk through the simulator's proxy on
127.0.0.1. Every fault is checked by what the endpoints observe, never by what
the simulator says it did, and the seeded behaviour is checked for repeatability.
"""

import asyncio
import statistics
import time

import pytest
import websockets
from websockets.exceptions import ConnectionClosed

from tests.support.leaks import snapshot
from tests.support.netsim import BOTH, DOWN, UP, FaultModel, Faults, NetSim


class Collector:
    """A server that records every text message, and can push messages to the client."""

    def __init__(self):
        self.received: list[tuple[float, str]] = []
        self.sockets: list = []
        self.server = None

    async def start(self):
        self.server = await websockets.serve(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _handle(self, ws):
        self.sockets.append(ws)
        try:
            async for message in ws:
                self.received.append((time.monotonic(), message))
        except ConnectionClosed:
            pass

    @property
    def texts(self) -> list[str]:
        return [m for _, m in self.received]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


def run(coro_fn):
    """Run an async scenario on a fresh loop, away from the simulator's own thread."""
    return asyncio.run(coro_fn())


def with_link(sim: NetSim, port: int, name: str = "l", **kw):
    return sim.add_link(name, port, **kw)


# ---------------------------------------------------------------------------
# The seeded model
# ---------------------------------------------------------------------------

def _decisions(key: str, faults: Faults, n: int = 200):
    model = FaultModel(key)
    return [model.decide(faults) for _ in range(n)]


def test_same_seed_gives_the_same_decisions_and_other_seeds_differ():
    faults = Faults(latency_s=0.01, jitter_s=0.02, loss=0.2, reorder=0.1)
    assert _decisions("1/x/up", faults) == _decisions("1/x/up", faults)
    assert _decisions("1/x/up", faults) != _decisions("2/x/up", faults)
    assert _decisions("1/x/up", faults) != _decisions("1/x/down", faults)


def test_the_random_stream_stays_aligned_when_settings_change():
    quiet = _decisions("s", Faults(loss=0.3))
    busy = _decisions("s", Faults(loss=0.3, latency_s=0.5, jitter_s=1.0, reorder=0.5))
    assert [d.drop for d in quiet] == [d.drop for d in busy]


def test_decisions_match_the_configured_rates():
    faults = Faults(latency_s=0.05, jitter_s=0.04, loss=0.25, reorder=0.1, reorder_delay_s=0.2)
    decisions = _decisions("rates", faults, 4000)
    assert abs(sum(d.drop for d in decisions) / 4000 - 0.25) < 0.03
    assert abs(sum(d.reordered for d in decisions) / 4000 - 0.10) < 0.03
    plain = [d.delay_s for d in decisions if not d.reordered]
    assert min(plain) >= 0.05 and max(plain) <= 0.09 + 1e-9
    assert all(0.15 - 1e-9 <= d.delay_s <= 0.29 + 1e-9 for d in decisions if d.reordered)


def test_invalid_faults_are_rejected():
    for bad in ({"loss": 1.5}, {"reorder": -0.1}, {"latency_s": -1}):
        with pytest.raises(ValueError):
            Faults(**bad)


# ---------------------------------------------------------------------------
# The proxy on real sockets
# ---------------------------------------------------------------------------

def test_a_clean_link_passes_everything_in_order_and_negotiates_no_compression(netsim):
    async def scenario():
        server = Collector()
        link = with_link(netsim, await server.start())
        async with websockets.connect(link.url) as ws:
            assert ws.protocol.extensions == []
            for i in range(50):
                await ws.send(f"m{i}")
            await server.sockets[0].send("hello")
            assert await asyncio.wait_for(ws.recv(), 5) == "hello"
            await asyncio.sleep(0.1)
        await server.stop()
        return server.texts

    assert run(scenario) == [f"m{i}" for i in range(50)]


def test_latency_is_applied_in_both_directions(netsim):
    async def scenario():
        server = Collector()
        link = with_link(netsim, await server.start(), up=Faults(latency_s=0.1), down=Faults(latency_s=0.05))
        async with websockets.connect(link.url) as ws:
            sent_at = time.monotonic()
            await ws.send("up")
            await asyncio.sleep(0.25)
            up_delay = server.received[-1][0] - sent_at
            sent_at = time.monotonic()
            await server.sockets[0].send("down")
            await ws.recv()
            down_delay = time.monotonic() - sent_at
        await server.stop()
        return link, up_delay, down_delay

    link, up_delay, down_delay = run(scenario)
    assert 0.1 <= up_delay < 0.1 + 0.15
    assert 0.05 <= down_delay < 0.05 + 0.15
    assert statistics.mean(link.delays(UP)) == pytest.approx(0.1, abs=0.03)


def test_jitter_stays_inside_its_bounds_and_never_reorders(netsim):
    async def scenario():
        server = Collector()
        link = with_link(netsim, await server.start(), up=Faults(latency_s=0.02, jitter_s=0.06))
        async with websockets.connect(link.url) as ws:
            for i in range(40):
                await ws.send(f"{i:03d}")
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.25)
        await server.stop()
        return link, server.texts

    link, texts = run(scenario)
    assert texts == [f"{i:03d}" for i in range(40)]
    delays = link.delays(UP)
    assert len(delays) >= 40
    assert min(delays) >= 0.02 - 1e-3
    assert max(delays) <= 0.02 + 0.06 + 0.12  # FIFO clamping may add a little on top of the jitter
    assert statistics.pstdev(delays) > 0.005


def _loss_run(seed: int, loss: float = 0.3, n: int = 250):
    async def scenario():
        with NetSim(seed=seed) as sim:
            server = Collector()
            link = sim.add_link("l", await server.start(), up=Faults(loss=loss))
            async with websockets.connect(link.url) as ws:
                for i in range(n):
                    await ws.send(str(i))
                await asyncio.sleep(0.2)
            await server.stop()
            return [int(t) for t in server.texts], link.stats(UP)
    return run(scenario)


def test_loss_matches_the_rate_and_repeats_for_the_same_seed():
    first, stats = _loss_run(seed=7)
    again, _ = _loss_run(seed=7)
    other, _ = _loss_run(seed=8)
    assert first == again
    assert first != other
    assert abs(len(first) / 250 - 0.7) < 0.1
    assert stats.dropped_loss == 250 - len(first)
    assert first == sorted(first)  # loss alone never reorders


def test_reordering_delivers_everything_but_out_of_order(netsim):
    async def scenario():
        server = Collector()
        link = with_link(netsim, await server.start(), up=Faults(reorder=0.25, reorder_delay_s=0.1))
        async with websockets.connect(link.url) as ws:
            await asyncio.sleep(0.1)
            for i in range(70):
                await ws.send(str(i))
                await asyncio.sleep(0.004)
            await asyncio.sleep(0.3)
        await server.stop()
        return link, [int(t) for t in server.texts]

    link, got = run(scenario)
    assert sorted(got) == list(range(70))
    assert got != sorted(got)
    assert link.stats(UP).reordered > 10


def test_a_one_way_partition_drops_one_direction_only(netsim):
    async def scenario():
        server = Collector()
        link = with_link(netsim, await server.start())
        async with websockets.connect(link.url) as ws:
            await ws.send("before")
            await asyncio.sleep(0.1)
            link.partition(UP)
            await ws.send("lost-1")
            await ws.send("lost-2")
            await server.sockets[0].send("still-works")
            assert await asyncio.wait_for(ws.recv(), 3) == "still-works"
            link.heal(UP)
            await ws.send("after")
            await asyncio.sleep(0.15)
            assert ws.state.name == "OPEN"
        await server.stop()
        return link, server.texts

    link, texts = run(scenario)
    assert texts == ["before", "after"]
    assert link.stats(UP).dropped_partition == 2


def test_a_held_partition_delivers_in_order_after_healing_without_closing(netsim):
    async def scenario():
        server = Collector()
        link = with_link(netsim, await server.start(), up=Faults(latency_s=0.02))
        async with websockets.connect(link.url) as ws:
            await ws.send("0")
            await asyncio.sleep(0.1)
            link.partition(BOTH, hold=True)
            for i in range(1, 6):
                await ws.send(str(i))
            await asyncio.sleep(0.25)
            during = list(server.texts)
            assert ws.state.name == "OPEN"  # a pulled cable: no FIN, no reset
            link.heal()
            await asyncio.sleep(0.4)
        await server.stop()
        return during, server.texts

    during, after = run(scenario)
    assert during == ["0"]
    assert after == ["0", "1", "2", "3", "4", "5"]


def test_a_full_partition_blocks_new_connections_until_healed(netsim):
    async def scenario():
        server = Collector()
        link = with_link(netsim, await server.start())
        link.partition(BOTH)
        try:
            await websockets.connect(link.url, open_timeout=0.5)
            blocked = False
        except (TimeoutError, asyncio.TimeoutError):
            blocked = True
        link.heal()
        async with websockets.connect(link.url) as ws:
            await ws.send("hi")
            await asyncio.sleep(0.1)
        await server.stop()
        return blocked, server.texts

    blocked, texts = run(scenario)
    assert blocked
    assert texts == ["hi"]


def test_sever_resets_live_connections_and_new_ones_work(netsim):
    async def scenario():
        server = Collector()
        link = with_link(netsim, await server.start())
        async with websockets.connect(link.url) as ws:
            await ws.send("x")
            await asyncio.sleep(0.1)
            assert link.sever() == 1
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 3)
        async with websockets.connect(link.url) as ws2:
            await ws2.send("again")
            await asyncio.sleep(0.1)
        await server.stop()
        return server.texts

    assert run(scenario) == ["x", "again"]


def test_flap_alternates_on_a_seeded_schedule_and_ends_healed(netsim):
    async def scenario():
        server = Collector()
        link = with_link(netsim, await server.start())
        async with websockets.connect(link.url) as ws:
            link.flap(down_s=0.1, up_s=0.07, cycles=2, hold=False)
            sent = 0
            end = time.monotonic() + 0.45
            while time.monotonic() < end:
                await ws.send(str(sent))
                sent += 1
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            await ws.send("final")
            await asyncio.sleep(0.1)
        await server.stop()
        return link, sent, server.texts

    link, sent, texts = run(scenario)
    states = [state for _, state in link.flap_history]
    assert states == ["down", "up"] * 2
    downs = [b - a for (a, s), (b, _) in zip(link.flap_history, link.flap_history[1:]) if s == "down"]
    assert all(0.1 * 0.7 <= d <= 0.1 * 1.3 + 0.05 for d in downs)
    assert not link.is_partitioned(UP)
    assert 0 < len(texts) - 1 < sent  # some got through, some were dropped while down
    assert texts[-1] == "final"


def test_stop_flap_while_down_heals_the_link(netsim):
    async def scenario():
        server = Collector()
        link = with_link(netsim, await server.start())
        async with websockets.connect(link.url) as ws:
            link.flap(down_s=30, up_s=30)
            await asyncio.sleep(0.1)
            assert link.is_partitioned(UP)
            link.stop_flap()
            assert not link.is_partitioned(UP)
            await ws.send("ok")
            await asyncio.sleep(0.1)
        await server.stop()
        return server.texts

    assert run(scenario) == ["ok"]


def test_fault_version_and_is_clean_follow_changes(netsim):
    link = netsim.add_link("v", 1)
    assert link.is_clean(UP)
    version = link.fault_version(UP)
    link.set_faults(UP, loss=0.1)
    assert not link.is_clean(UP) and link.fault_version(UP) > version
    link.clear_faults()
    link.partition(DOWN)
    assert link.is_clean(UP) and not link.is_clean(DOWN)
    link.heal()
    assert link.is_clean(DOWN)


def test_retarget_sends_new_connections_to_another_server(netsim):
    async def scenario():
        one, two = Collector(), Collector()
        link = with_link(netsim, await one.start())
        async with websockets.connect(link.url) as ws:
            await ws.send("to-one")
            await asyncio.sleep(0.1)
        link.retarget(await two.start())
        async with websockets.connect(link.url) as ws:
            await ws.send("to-two")
            await asyncio.sleep(0.1)
        await one.stop()
        await two.stop()
        return one.texts, two.texts

    assert run(scenario) == (["to-one"], ["to-two"])


def test_stopping_the_simulator_releases_every_socket_task_and_thread():
    before = snapshot()

    async def scenario():
        with NetSim(seed=3) as sim:
            server = Collector()
            port = await server.start()
            links = [sim.add_link(f"l{i}", port) for i in range(3)]
            clients = [await websockets.connect(link.url) for link in links]
            links[0].flap(0.05, 0.05)
            links[1].partition(BOTH, hold=True)
            await clients[1].send("held")
            await asyncio.sleep(0.15)
        # The simulator stops with three live connections, a flap and held frames.
        for client in clients:
            await asyncio.wait_for(client.wait_closed(), 5)
        await server.stop()

    run(scenario)
    assert snapshot().leaked_since(before) == {}


def test_stop_is_idempotent_and_the_port_stops_listening():
    sim = NetSim(seed=1).start()
    link = sim.add_link("l", 1)
    port = link.port
    sim.stop()
    sim.stop()

    async def connect():
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
    run(connect)
