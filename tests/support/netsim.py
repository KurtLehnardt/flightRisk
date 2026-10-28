"""Controllable loopback network simulator for WebSocket links.

``NetSim`` runs a small TCP proxy per link on 127.0.0.1. A real
``EdgeTransport`` connects to the proxy instead of the ground station, and the
proxy connects on to the real ``GroundTransport``. Nothing is stubbed: every
byte still crosses real sockets and the real ``websockets`` library on both
ends. The proxy understands just enough of the WebSocket wire format to treat
each frame as a message, so it can apply faults per message:

* latency and jitter (jitter never reorders, like TCP),
* message loss,
* reordering (a message is held back so later ones overtake it),
* a one-way or full partition that either drops traffic, holds it until the
  link heals, or (``backpressure``) stops reading so the sender's own buffers
  fill and its writes block. In every case nothing is closed while the
  partition lasts, not even when an endpoint closes or dies: that FIN or reset
  is held back too and arrives after the data sent before it, once the link
  heals (a pulled cable),
* sever (an abrupt close of the live connections, like a reset),
* flapping (repeated partition and heal on a seeded schedule).

Every random decision comes from a ``random.Random`` seeded from the sim seed,
the link name and the direction, and each message consumes the same number of
draws whatever the settings, so a given seed and message sequence always gives
the same drops and delays. Wall-clock timing is real, so measured delays vary
by scheduler jitter only.

Directions are named from the client's point of view: ``UP`` is client to
server (edge to ground) and ``DOWN`` is server to client (ground to edge).

The proxy removes ``Sec-WebSocket-Extensions`` from the client handshake so no
compression is negotiated. A dropped or reordered compressed frame would
corrupt the shared deflate state, which no real network can do to a TCP
stream; losing a whole message is a model of an application-level fault.
Control frames (ping, pong, close), fragmented messages and the handshake are
delayed and partitioned like data but never dropped by ``loss`` or reordered.
"""

from __future__ import annotations

import asyncio
import math
import random
import socket
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

UP = "up"
DOWN = "down"
BOTH = "both"
DIRECTIONS = (UP, DOWN)

LOOPBACK = "127.0.0.1"
_MAX_FRAME_BYTES = 64 * 1024 * 1024
_FIFO_EPSILON_S = 1e-6
_RECORDS_KEPT = 20000


def _directions(direction: str) -> tuple[str, ...]:
    if direction == BOTH:
        return DIRECTIONS
    if direction in DIRECTIONS:
        return (direction,)
    raise ValueError(f"direction must be 'up', 'down' or 'both', got {direction!r}")


@dataclass
class Faults:
    """Fault settings for one direction of one link."""

    latency_s: float = 0.0
    jitter_s: float = 0.0
    loss: float = 0.0
    reorder: float = 0.0
    #: A reordered message is held back by 50 to 100 percent of this.
    reorder_delay_s: float = 0.2

    def __post_init__(self) -> None:
        for name in ("latency_s", "jitter_s", "reorder_delay_s"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite number that is not negative, got {value!r}")
        for name in ("loss", "reorder"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:  # false for NaN
                raise ValueError(f"{name} must be within 0 and 1, got {value!r}")

    @property
    def clean(self) -> bool:
        """True when no message can be lost or reordered."""
        return self.loss == 0.0 and self.reorder == 0.0


@dataclass(frozen=True)
class Decision:
    """What the model decided for one data message."""

    drop: bool
    delay_s: float
    reordered: bool


class FaultModel:
    """Seeded, side-effect free decisions for the data messages of one direction.

    Each call consumes exactly four draws, so the stream stays aligned
    whatever the fault settings are when it is called.
    """

    def __init__(self, seed_key: str):
        self._rng = random.Random(seed_key)

    def decide(self, faults: Faults) -> Decision:
        r_loss, r_jitter, r_reorder, r_extra = (self._rng.random() for _ in range(4))
        delay = faults.latency_s + r_jitter * faults.jitter_s
        reordered = r_reorder < faults.reorder
        if reordered:
            delay += (0.5 + 0.5 * r_extra) * faults.reorder_delay_s
        return Decision(drop=r_loss < faults.loss, delay_s=delay, reordered=reordered)


@dataclass
class DirStats:
    """Counters for one direction of one link."""

    frames_in: int = 0
    delivered: int = 0
    dropped_loss: int = 0
    dropped_partition: int = 0
    held: int = 0
    reordered: int = 0
    bytes_delivered: int = 0


@dataclass
class _Frame:
    raw: bytes
    opcode: int
    fin: bool

    @property
    def is_plain_data(self) -> bool:
        """A complete text or binary message: the only kind loss and reordering touch."""
        return self.fin and self.opcode in (0x1, 0x2)


@dataclass(frozen=True)
class _Close:
    """The end of a connection as seen by one pump: a FIN (``abort`` False) or a reset."""

    abort: bool = False


async def _read_frame(reader: asyncio.StreamReader) -> _Frame:
    head = await reader.readexactly(2)
    fin = bool(head[0] & 0x80)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    ext = b""
    if length == 126:
        ext = await reader.readexactly(2)
        length = int.from_bytes(ext, "big")
    elif length == 127:
        ext = await reader.readexactly(8)
        length = int.from_bytes(ext, "big")
    if length > _MAX_FRAME_BYTES:
        raise ConnectionError(f"frame of {length} bytes is too large for the simulator")
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length)
    return _Frame(head + ext + mask + payload, opcode, fin)


def _reset(transport: asyncio.BaseTransport) -> None:
    """Close a socket with a TCP reset (RST) instead of the orderly FIN that ``abort`` often sends."""
    sock = transport.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
    transport.abort()


def _strip_extensions(request: bytes) -> bytes:
    lines = request.split(b"\r\n")
    kept = [line for line in lines if not line.lower().startswith(b"sec-websocket-extensions:")]
    return b"\r\n".join(kept)


@dataclass
class _Direction:
    name: str
    faults: Faults
    model: FaultModel
    partitioned: bool = False
    hold: bool = False
    held: list = field(default_factory=list)
    last_deliver: float = 0.0
    stats: DirStats = field(default_factory=DirStats)
    #: (arrival, delivery) loop times of every delivered data message.
    records: deque = field(default_factory=lambda: deque(maxlen=_RECORDS_KEPT))
    #: Bumped on every change of this direction's fault settings or partition.
    version: int = 0
    #: While True the pump of this direction does not read, so the sender's buffers fill.
    stalled: bool = False
    #: Set while the pump may read; cleared by a ``backpressure`` partition.
    flowing: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.flowing.set()


class _Conn:
    """One proxied client connection: two sockets and the delivery state."""

    def __init__(self, cid: int, client_writer: asyncio.StreamWriter, server_writer: asyncio.StreamWriter):
        self.cid = cid
        self.writers = {UP: server_writer, DOWN: client_writer}  # where each direction writes to
        self.last_t = {UP: 0.0, DOWN: 0.0}
        self.closed = False
        self.tasks: list[asyncio.Task] = []

    def abort(self) -> None:
        self.closed = True
        for writer in self.writers.values():
            transport = writer.transport
            if transport is not None and not transport.is_closing():
                _reset(transport)


class NetLink:
    """One proxied link: a listening port on loopback forwarding to a target."""

    def __init__(self, sim: NetSim, name: str, target_host: str, target_port: int, up: Faults, down: Faults):
        self._sim = sim
        self.name = name
        self.target_host = target_host
        self.target_port = target_port
        self._dirs = {
            UP: _Direction(UP, up, FaultModel(f"{sim.seed}/{name}/{UP}")),
            DOWN: _Direction(DOWN, down, FaultModel(f"{sim.seed}/{name}/{DOWN}")),
        }
        self._flap_rng = random.Random(f"{sim.seed}/{name}/flap")
        self._server: asyncio.AbstractServer | None = None
        self._conns: dict[int, _Conn] = {}
        self._next_cid = 0
        self._flap_task: asyncio.Task | None = None
        #: (monotonic time, "down" or "up") for every flap transition.
        self.flap_history: list[tuple[float, str]] = []
        self.port = 0
        self.accepted = 0
        #: Monotonic time of every accepted client connection.
        self.accept_times: list[float] = []

    # --- addressing ---

    @property
    def url(self) -> str:
        return f"ws://{LOOPBACK}:{self.port}"

    def retarget(self, target_port: int, target_host: str | None = None) -> None:
        """Point new connections at another server (existing ones are untouched)."""
        self.target_port = target_port
        if target_host is not None:
            self.target_host = target_host

    # --- lifecycle (loop thread) ---

    async def _start(self) -> None:
        self._server = await asyncio.start_server(self._on_connect, LOOPBACK, 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _stop(self) -> None:
        await self._cancel_flap()
        server, self._server = self._server, None
        if server is not None:
            server.close()
        self._abort_all()  # the listener's wait_closed() waits for its connections
        if server is not None:
            await asyncio.wait_for(server.wait_closed(), 5)
        tasks = [t for conn in list(self._conns.values()) for t in conn.tasks]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._conns.clear()

    async def _on_connect(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        self.accepted += 1
        self.accept_times.append(time.monotonic())
        try:
            server_reader, server_writer = await asyncio.open_connection(self.target_host, self.target_port)
        except OSError:
            client_writer.transport.abort()
            return
        self._next_cid += 1
        conn = _Conn(self._next_cid, client_writer, server_writer)
        self._conns[conn.cid] = conn
        conn.tasks = [
            asyncio.ensure_future(self._pump(conn, UP, client_reader)),
            asyncio.ensure_future(self._pump(conn, DOWN, server_reader)),
        ]

    # --- the data path (loop thread) ---

    async def _pump(self, conn: _Conn, direction: str, reader: asyncio.StreamReader) -> None:
        d = self._dirs[direction]
        end = _Close()
        try:
            await d.flowing.wait()
            head = await reader.readuntil(b"\r\n\r\n")
            if direction == UP:
                head = _strip_extensions(head)
            self._submit(conn, direction, _Frame(head, 0x0, True), handshake=True)
            while True:
                await d.flowing.wait()  # a stalled direction is not read: the sender's buffers fill
                self._submit(conn, direction, await _read_frame(reader))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # end of stream: the peer closed (FIN)
        except OSError:
            end = _Close(abort=True)  # a reset or another socket error
        finally:
            if conn.closed:  # severed or stopped: nothing more to pass on
                self._conns.pop(conn.cid, None)
            else:
                self._submit_close(conn, direction, end)

    def _submit_close(self, conn: _Conn, direction: str, end: _Close) -> None:
        """The source of *direction* ended. During a partition the peer does not see it until the heal."""
        d = self._dirs[direction]
        if d.partitioned:
            d.held.append((conn, end, asyncio.get_running_loop().time(), False))
            return
        self._close_later(conn, direction, end)

    def _close_later(self, conn: _Conn, direction: str, end: _Close) -> None:
        loop = asyncio.get_running_loop()
        loop.call_at(max(conn.last_t[direction], loop.time()), self._finish, conn, direction, end)

    def _submit(self, conn: _Conn, direction: str, frame: _Frame, handshake: bool = False) -> None:
        d = self._dirs[direction]
        loop = asyncio.get_running_loop()
        now = loop.time()
        d.stats.frames_in += 1
        plain = frame.is_plain_data and not handshake
        decision = d.model.decide(d.faults) if plain else None
        if d.partitioned:
            if d.hold:
                d.held.append((conn, frame, now, handshake))
                d.stats.held += 1
            else:
                d.stats.dropped_partition += 1
            return
        self._deliver_later(conn, d, frame, now, decision, plain)

    def _deliver_later(self, conn: _Conn, d: _Direction, frame: _Frame, now: float,
                       decision: Decision | None, plain: bool) -> None:
        loop = asyncio.get_running_loop()
        if decision is not None and decision.drop:
            d.stats.dropped_loss += 1
            return
        delay = decision.delay_s if decision is not None else d.faults.latency_s
        at = loop.time() + delay
        if decision is not None and decision.reordered:
            d.stats.reordered += 1
        else:
            at = max(at, d.last_deliver + _FIFO_EPSILON_S)
            if frame.opcode == 0x8:  # nothing may follow a close frame, so it waits for held-back messages
                at = max(at, conn.last_t[d.name] + _FIFO_EPSILON_S)
            d.last_deliver = at
        conn.last_t[d.name] = max(conn.last_t[d.name], at)
        loop.call_at(at, self._write, conn, d, frame.raw, now, plain)

    def _write(self, conn: _Conn, d: _Direction, raw: bytes, arrived: float, plain: bool) -> None:
        if conn.closed:
            return
        writer = conn.writers[d.name]
        if writer.transport is None or writer.transport.is_closing():
            return
        writer.write(raw)
        d.stats.delivered += 1
        d.stats.bytes_delivered += len(raw)
        if plain:
            d.records.append((arrived, asyncio.get_running_loop().time()))

    def _finish(self, conn: _Conn, direction: str, end: _Close) -> None:
        """One side ended and its queued messages went out: close or reset the peer socket."""
        if conn.closed:
            return
        writer = conn.writers[direction]
        transport = writer.transport
        if transport is not None and not transport.is_closing():
            if end.abort:
                _reset(transport)
            else:
                writer.close()
        other = conn.writers[DOWN if direction == UP else UP]
        if other.transport is None or other.transport.is_closing():
            conn.closed = True
            self._conns.pop(conn.cid, None)

    def _abort_all(self) -> None:
        for conn in list(self._conns.values()):
            conn.abort()
        for d in self._dirs.values():
            d.held.clear()

    # --- control (callable from any thread) ---

    def set_faults(self, direction: str = BOTH, **changes: float) -> None:
        """Change fault settings, e.g. ``set_faults(DOWN, latency_s=0.05, loss=0.1)``."""
        def apply() -> None:
            for name in _directions(direction):
                d = self._dirs[name]
                values = {f: getattr(d.faults, f) for f in Faults.__dataclass_fields__}
                values.update(changes)
                d.faults = Faults(**values)
                d.version += 1
        self._sim.run(apply)

    def clear_faults(self) -> None:
        """Back to a perfect link. Partitions are untouched (see ``heal``)."""
        def apply() -> None:
            for d in self._dirs.values():
                d.faults = Faults()
                d.version += 1
        self._sim.run(apply)

    def partition(self, direction: str = BOTH, hold: bool = False, backpressure: bool = False) -> None:
        """Stop traffic in *direction* without closing any socket.

        With ``hold=False`` messages are discarded; with ``hold=True`` they are
        queued and delivered, in order, when the link heals. With
        ``backpressure=True`` the proxy stops reading, so nothing is dropped or
        queued by the proxy: the sender's kernel and websockets buffers fill
        and its ``send()`` blocks, exactly as on a pulled cable, and the data
        flows again, in order, on heal. New connections are affected too,
        because their handshake is traffic.

        A close during the partition is not passed on either: if an endpoint
        closes (FIN) or dies (reset), the other end learns of it only on
        heal, after the data the closing endpoint sent first (all of it with
        ``hold`` or ``backpressure``, none of what was dropped otherwise).
        """
        self._sim.run(lambda: self._partition_now(direction, hold, backpressure))

    def heal(self, direction: str = BOTH) -> None:
        """End a partition and release anything held, in arrival order."""
        self._sim.run(lambda: self._heal_now(direction))

    def sever(self) -> int:
        """Abruptly close every live connection (a reset). Returns how many were open."""
        def apply() -> int:
            count = len(self._conns)
            self._abort_all()
            return count
        return self._sim.run(apply)

    def flap(self, down_s: float, up_s: float, cycles: int | None = None, *, direction: str = BOTH,
             hold: bool = False, sever: bool = False, spread: float = 0.25, backpressure: bool = False) -> None:
        """Alternate partition and heal. Durations vary by +-*spread* on the link's seeded schedule.

        ``cycles=None`` flaps until ``stop_flap()``. ``sever=True`` also resets
        the live connections at the start of each down period.
        """
        async def run() -> None:
            n = 0
            try:
                while cycles is None or n < cycles:
                    n += 1
                    self.flap_history.append((time.monotonic(), "down"))
                    self._partition_now(direction, hold, backpressure)
                    if sever:
                        self._abort_all()
                    await asyncio.sleep(down_s * (1 + self._flap_rng.uniform(-spread, spread)))
                    self.flap_history.append((time.monotonic(), "up"))
                    self._heal_now(direction)
                    await asyncio.sleep(up_s * (1 + self._flap_rng.uniform(-spread, spread)))
            except asyncio.CancelledError:
                self._heal_now(direction)
                raise

        def start() -> None:
            if self._flap_task is not None and not self._flap_task.done():
                raise RuntimeError("this link is already flapping")
            self._flap_task = asyncio.ensure_future(run())
        self._sim.run(start)

    def stop_flap(self) -> None:
        self._sim.run_coroutine(self._cancel_flap())

    async def _cancel_flap(self) -> None:
        task, self._flap_task = self._flap_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _partition_now(self, direction: str, hold: bool, backpressure: bool = False) -> None:
        for name in _directions(direction):
            d = self._dirs[name]
            # The one frame being read as the partition starts is held, never lost, under backpressure.
            d.partitioned, d.hold, d.stalled = True, hold or backpressure, backpressure
            if backpressure:
                d.flowing.clear()
            else:
                d.flowing.set()
            d.version += 1

    def _heal_now(self, direction: str) -> None:
        for name in _directions(direction):
            d = self._dirs[name]
            if not d.partitioned:
                continue
            d.partitioned, d.stalled = False, False
            d.flowing.set()
            d.version += 1
            held, d.held = d.held, []
            for conn, item, arrived, handshake in held:  # frames first, a close last, in arrival order
                if conn.closed:
                    continue
                if isinstance(item, _Close):
                    self._close_later(conn, d.name, item)
                else:
                    self._deliver_later(conn, d, item, arrived, None, item.is_plain_data and not handshake)

    # --- observation ---

    def stats(self, direction: str) -> DirStats:
        """A copy of the counters of one direction."""
        return self._sim.run(lambda: DirStats(**vars(self._dirs[direction].stats)))

    def delays(self, direction: str) -> list[float]:
        """Seconds each delivered data message spent in the proxy, oldest first."""
        return self._sim.run(lambda: [out - arrived for arrived, out in self._dirs[direction].records])

    def reset_observations(self) -> None:
        def apply() -> None:
            for d in self._dirs.values():
                d.stats = DirStats()
                d.records.clear()
        self._sim.run(apply)

    def is_partitioned(self, direction: str) -> bool:
        return self._dirs[direction].partitioned

    def fault_version(self, direction: str) -> int:
        """Changes whenever the direction's faults or partition change."""
        return self._dirs[direction].version

    def is_clean(self, direction: str) -> bool:
        """True when the direction is not partitioned and can neither lose nor reorder."""
        d = self._dirs[direction]
        return not d.partitioned and d.faults.clean

    def faults(self, direction: str) -> Faults:
        return self._dirs[direction].faults

    @property
    def open_connections(self) -> int:
        return self._sim.run(lambda: len(self._conns))


class NetSim:
    """A set of ``NetLink`` proxies served by one event loop on a background thread."""

    def __init__(self, seed: int = 0):
        self.seed = seed
        self.links: dict[str, NetLink] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ident: int | None = None

    # --- lifecycle ---

    def start(self) -> NetSim:
        if self._loop is not None:
            return self
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name="netsim-loop", daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        self._ident = threading.get_ident()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def stop(self) -> None:
        """Close every listener and connection, then stop the loop thread. Idempotent."""
        loop, self._loop = self._loop, None
        if loop is None:
            return

        async def shutdown() -> None:
            for link in list(self.links.values()):
                await link._stop()
            current = asyncio.current_task()
            rest = [t for t in asyncio.all_tasks() if t is not current]
            for task in rest:
                task.cancel()
            await asyncio.gather(*rest, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(timeout=15)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=10)
            if not loop.is_running():
                loop.close()
        self.links.clear()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- running things on the loop ---

    def run(self, fn: Callable[[], Any]) -> Any:
        """Call *fn* on the sim's loop thread and return its result."""
        if self._loop is None:
            raise RuntimeError("the simulator is not running")
        if threading.get_ident() == self._ident:
            return fn()

        async def call() -> Any:
            return fn()
        return asyncio.run_coroutine_threadsafe(call(), self._loop).result(timeout=15)

    def run_coroutine(self, coro: Any) -> Any:
        if self._loop is None:
            coro.close()
            raise RuntimeError("the simulator is not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=15)

    # --- topology ---

    def add_link(self, name: str, target_port: int, *, target_host: str = LOOPBACK,
                 up: Faults | None = None, down: Faults | None = None) -> NetLink:
        """Open a proxy listening on a free loopback port that forwards to *target*."""
        if name in self.links:
            raise ValueError(f"link {name!r} already exists")
        link = NetLink(self, name, target_host, target_port, up or Faults(), down or Faults())
        self.run_coroutine(link._start())
        self.links[name] = link
        return link

    def rng(self, label: str) -> random.Random:
        """A deterministic generator for schedulers, independent of every link's stream."""
        return random.Random(f"{self.seed}/rng/{label}")

    def partition_all(self, direction: str = BOTH, hold: bool = False, backpressure: bool = False) -> None:
        for link in self.links.values():
            link.partition(direction, hold, backpressure)

    def heal_all(self) -> None:
        for link in self.links.values():
            link.heal()

    def clear_all(self) -> None:
        """Heal and remove every fault on every link."""
        for link in self.links.values():
            link.stop_flap()
            link.heal()
            link.clear_faults()
