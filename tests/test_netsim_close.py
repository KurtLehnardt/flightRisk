"""How the simulator treats connection ends: FIN, reset, partitions, reordering and backpressure.

A partition must behave like a pulled cable. Nothing, not even the end of a
connection, may cross it while it lasts, and what was sent before a close must
arrive before that close once the link heals. These tests use raw TCP endpoints
so that an orderly close (FIN) and a reset (RST) can be told apart exactly, and
a real ``websockets`` pair for the reordering and backpressure cases.
"""

import asyncio
import socket
import struct

import pytest
import websockets
from websockets.exceptions import ConnectionClosed

from tests.support.netsim import BOTH, DOWN, UP, Faults, NetSim

HEAD = b"GET / HTTP/1.1\r\nHost: sim\r\n\r\n"
REPLY = b"HTTP/1.1 101 Switching Protocols\r\n\r\n"


def frame(text: str, masked: bool) -> bytes:
    payload = text.encode()
    assert len(payload) < 126
    if masked:
        return bytes([0x81, 0x80 | len(payload)]) + b"\0\0\0\0" + payload
    return bytes([0x81, len(payload)]) + payload


def texts(buf: bytes) -> list[str]:
    """The text messages in *buf*, which starts with a handshake head."""
    buf = buf[buf.index(b"\r\n\r\n") + 4:]
    out = []
    while buf:
        masked = bool(buf[1] & 0x80)
        n = buf[1] & 0x7F
        start = 2 + (4 if masked else 0)
        out.append(buf[start:start + n].decode())
        buf = buf[start + n:]
    return out


class Endpoint:
    """One raw end of a proxied connection: records bytes and how the stream ended."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        self.buf = b""
        self.ended: str | None = None  # "fin" or "rst"
        self.task = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        try:
            while True:
                data = await self.reader.read(65536)
                if not data:
                    self.ended = "fin"
                    return
                self.buf += data
        except (ConnectionResetError, BrokenPipeError):
            self.ended = "rst"

    def close(self, how: str) -> None:
        if self.writer.transport.is_closing():
            return
        if how == "rst":
            sock = self.writer.get_extra_info("socket")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.writer.transport.abort()
        else:
            self.writer.close()

    @property
    def messages(self) -> list[str]:
        return texts(self.buf) if b"\r\n\r\n" in self.buf else []


async def _pair(sim: NetSim):
    """A client and a server endpoint connected through a fresh link, handshakes exchanged."""
    accepted: asyncio.Future = asyncio.get_running_loop().create_future()
    server = await asyncio.start_server(lambda r, w: accepted.set_result(Endpoint(r, w)), "127.0.0.1", 0)
    link = sim.add_link("l", server.sockets[0].getsockname()[1])
    client = Endpoint(*await asyncio.open_connection("127.0.0.1", link.port))
    client.writer.write(HEAD)
    srv = await asyncio.wait_for(accepted, 5)
    srv.writer.write(REPLY)
    await asyncio.sleep(0.15)
    return server, link, client, srv


async def _scenario(closer_side: str, end: str, mode: str, direction: str, sent: int = 2):
    """The closer sends 'before', the link partitions, it sends *sent* more messages, then closes."""
    with NetSim(seed=3) as sim:
        server, link, client, srv = await _pair(sim)
        closer, peer = (client, srv) if closer_side == "client" else (srv, client)
        masked = closer_side == "client"
        closer.writer.write(frame("before", masked))
        await asyncio.sleep(0.15)
        link.partition(direction, hold=(mode == "hold"), backpressure=(mode == "backpressure"))
        for k in range(sent):
            closer.writer.write(frame(f"during-{k}", masked))
        await asyncio.sleep(0.1)
        closer.close(end)
        await asyncio.sleep(0.5)
        while_partitioned = (peer.ended, list(peer.messages))
        link.heal()
        await asyncio.sleep(0.5)
        result = (while_partitioned, (peer.ended, list(peer.messages)))
        server.close()
        for ep in (client, srv):
            ep.close("rst")
        await server.wait_closed()
        return result


CASES = [(c, e, m, d) for c in ("client", "server") for e in ("fin", "rst")
         for m in ("hold", "drop", "backpressure") for d in ("both", "one_way")]
CASES += [(c, e, "hold", "other") for c in ("client", "server") for e in ("fin", "rst")]


def _direction(closer: str, scope: str) -> str:
    # The partitioned direction is the one that carries the closer's bytes and its close.
    own, other = (UP, DOWN) if closer == "client" else (DOWN, UP)
    return {"both": BOTH, "one_way": own, "other": other}[scope]


@pytest.fixture(scope="module")
def outcomes():
    """Every scenario, played side by side so the matrix costs about two seconds."""
    async def run_all():
        results = await asyncio.gather(*(_scenario(c, e, m, _direction(c, d)) for c, e, m, d in CASES))
        return dict(zip(CASES, results))
    return asyncio.run(run_all())


@pytest.mark.parametrize("closer", ["client", "server"])
@pytest.mark.parametrize("end", ["fin", "rst"])
@pytest.mark.parametrize("mode", ["hold", "drop", "backpressure"])
@pytest.mark.parametrize("scope", ["both", "one_way"])
def test_a_close_does_not_cross_a_partition_and_arrives_after_the_data_on_heal(outcomes, closer, end, mode, scope):
    during, after = outcomes[(closer, end, mode, scope)]

    assert during == (None, ["before"]), "nothing, not even the close, may cross a partition"
    ended, messages = after
    if mode == "drop":
        assert messages == ["before"]  # what was sent during the outage is gone, as with a cut cable
    elif not (mode == "backpressure" and end == "rst"):  # a reset legitimately discards unread data
        assert messages == ["before", "during-0", "during-1"], "data sent before the close comes first, in order"
    # The kind of close is preserved: an orderly close stays a FIN and a reset stays a reset.
    assert ended == end


@pytest.mark.parametrize("closer", ["client", "server"])
@pytest.mark.parametrize("end", ["fin", "rst"])
def test_a_close_in_the_unpartitioned_direction_passes_at_once(outcomes, closer, end):
    ended, messages = outcomes[(closer, end, "hold", "other")][0]
    assert ended == end
    assert messages == ["before", "during-0", "during-1"]


def test_a_partition_is_not_ended_by_one_side_closing_and_the_link_stays_open():
    async def run():
        with NetSim(seed=3) as sim:
            server, link, client, srv = await _pair(sim)
            link.partition(BOTH, hold=True)
            client.close("fin")
            await asyncio.sleep(0.5)
            open_while_down = link.open_connections
            link.heal()
            await asyncio.sleep(0.5)
            result = (open_while_down, srv.ended, link.open_connections)
            server.close()
            srv.close("rst")
            await server.wait_closed()
            return result
    open_while_down, srv_end, open_after = asyncio.run(run())
    assert open_while_down == 1
    assert srv_end == "fin" and open_after == 0


def test_a_websocket_client_that_dies_in_a_held_partition_still_delivers_its_messages_first():
    got: list[str] = []
    codes: list[int | None] = []

    async def handler(ws):
        try:
            async for message in ws:
                got.append(message)
        except ConnectionClosed:
            pass
        codes.append(ws.close_code)

    async def run():
        server = await websockets.serve(handler, "127.0.0.1", 0)
        with NetSim(seed=1) as sim:
            link = sim.add_link("l", server.sockets[0].getsockname()[1])
            ws = await websockets.connect(link.url)
            await ws.send("one")
            await asyncio.sleep(0.15)
            link.partition(BOTH, hold=True)
            await ws.send("two")
            await ws.send("three")
            ws.transport.abort()  # the process dies without a close handshake
            await asyncio.sleep(0.5)
            assert got == ["one"] and codes == []
            link.heal()
            await asyncio.sleep(0.5)
        server.close()
        await server.wait_closed()

    asyncio.run(run())
    assert got == ["one", "two", "three"]
    assert codes == [1006]


def test_a_reordered_message_is_not_delivered_after_the_close_frame():
    got: list[str] = []
    ends: list[tuple[int | None, str | None]] = []

    async def handler(ws):
        try:
            async for message in ws:
                got.append(message)
        except ConnectionClosed:
            pass
        ends.append((ws.close_code, ws.close_reason))

    async def run():
        server = await websockets.serve(handler, "127.0.0.1", 0)
        with NetSim(seed=1) as sim:
            link = sim.add_link("l", server.sockets[0].getsockname()[1], up=Faults(reorder=1.0, reorder_delay_s=0.2))
            ws = await websockets.connect(link.url)
            await ws.send("last-words")
            await ws.close()  # the close frame must wait for the held-back message
            await asyncio.sleep(0.5)
            stats = link.stats(UP)
            client_code = ws.close_code
        server.close()
        await server.wait_closed()
        return stats, client_code

    stats, client_code = asyncio.run(run())
    assert got == ["last-words"]
    assert ends == [(1000, "")] and client_code == 1000
    assert stats.reordered == 1


def _stalled_sender(mode: str):
    """Send 128 KiB messages through a partition; return (sent, sent_while_blocked, sent_after, received, stats)."""
    payload = "x" * (128 * 1024)
    received: list[int] = []

    async def handler(ws):
        try:
            async for message in ws:
                received.append(int(message[:8]))
        except ConnectionClosed:
            pass

    async def run():
        server = await websockets.serve(handler, "127.0.0.1", 0, max_size=None)
        with NetSim(seed=1) as sim:
            link = sim.add_link("l", server.sockets[0].getsockname()[1])
            ws = await websockets.connect(link.url, max_size=None)
            state = {"sent": 0, "stop": False}

            async def sender():
                while not state["stop"] and state["sent"] < 150:
                    await ws.send(f"{state['sent']:08d}" + payload)
                    state["sent"] += 1

            link.partition(UP, hold=(mode == "hold"), backpressure=(mode == "backpressure"))
            task = asyncio.ensure_future(sender())
            await asyncio.sleep(1.0)
            mid = state["sent"]
            await asyncio.sleep(0.6)
            blocked = task.done() is False and state["sent"] == mid
            link.heal()
            for _ in range(100):
                await asyncio.sleep(0.1)
                if task.done() or state["sent"] > mid + 5:
                    break
            sent_after = state["sent"]
            state["stop"] = True
            await asyncio.wait_for(task, 20)
            for _ in range(200):
                if len(received) >= state["sent"]:
                    break
                await asyncio.sleep(0.05)
            stats = link.stats(UP)
            await ws.close()
        server.close()
        await server.wait_closed()
        return blocked, mid, sent_after, state["sent"], stats

    blocked, mid, sent_after, total, stats = asyncio.run(run())
    return blocked, mid, sent_after, total, received, stats


def test_backpressure_blocks_the_sender_until_the_link_heals():
    blocked, mid, sent_after, total, received, stats = _stalled_sender("backpressure")
    assert mid > 0
    assert blocked, f"send() must block once the buffers are full, but {mid} messages went through"
    assert sent_after > mid, "the blocked send resumes on heal"
    assert received == list(range(total)), "everything arrives, in order, nothing dropped by the proxy"
    assert stats.dropped_partition == 0


def test_a_held_partition_never_blocks_the_sender_because_the_proxy_keeps_reading():
    blocked, _, _, total, received, stats = _stalled_sender("hold")
    assert not blocked
    assert total == 150 and received == list(range(150))
    assert stats.held > 0
