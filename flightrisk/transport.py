"""WebSocket transport layer for the edge/ground compute split.

``EdgeTransport`` runs on the edge device (e.g. a Jetson Orin Nano) and
streams ``DetectionMessage``s produced by ``EdgeRunner`` to the ground
station over a WebSocket connection. ``GroundTransport`` runs on the ground
station (laptop) as a WebSocket server: it receives those messages, hands
them to a callback for scoring via ``GroundStation``, and can push target
updates back down to connected edge clients.

Both classes expose a plain ``async``/``await`` API built directly on top of
the ``websockets`` library. ``EdgeTransportSync`` / ``GroundTransportSync``
wrap that async API on a dedicated background asyncio event loop thread,
using the same run_coroutine_threadsafe bridge pattern as
``flightrisk.drone.mavlink.MavlinkController``, so the threading-based Flask/
SocketIO app (``flightrisk/dashboard/app.py``) can drive them without itself
becoming async.

This module is a transport layer only: it does not modify or depend on the
internals of ``flightrisk/edge.py`` or ``flightrisk/ground.py`` beyond their public
``EdgeRunner``/``DetectionMessage``/``GroundStation`` API (``to_dict`` /
``from_dict`` / ``process_message``).

Fleet control (design section 3, schemas in ``flightrisk/fleet_protocol.py``)
rides on the same connection:

* ``GroundTransport`` sends ``ping``, ``clock``, ``assign_lanes``,
  ``start_at``, ``takeoff``, ``land``, ``emergency_land_all`` and
  ``heartbeat`` messages, runs the eight-round clock sync per edge
  (``sync_clock``), learns which socket belongs to which ``drone_id`` from the
  edge's messages, and hands validated ``ack`` and ``telemetry`` messages to an
  ``on_control`` callback.
* ``EdgeTransport`` runs a background reader once connected. The reader
  answers ``ping`` with ``pong`` immediately (the frame loop never sees
  pings), stores the ``clock`` offset in ``EdgeTransport.clock``, fires the
  ``on_emergency_land`` callback on its own thread the moment an
  ``emergency_land_all`` arrives and puts that message at the front of the
  ``recv()`` queue, and queues every other message for ``recv()`` in arrival
  order.

``emergency_land_all`` is never queued behind other traffic on either side
and is never dropped without an error log.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from flightrisk import fleet_protocol as fp
from flightrisk.async_bridge import AsyncBridge
from flightrisk.edge import DetectionMessage, EdgeRunner

logger = logging.getLogger(__name__)

__all__ = [
    "EdgeTransport",
    "GroundTransport",
    "EdgeTransportSync",
    "GroundTransportSync",
    "ClockSyncError",
]

_CMD_TIMEOUT = 30.0  # seconds for sync-over-async calls

# Hosts treated as local-only. Binding one of these without a token is
# allowed (open, for local dev); binding anything else without a token is
# refused at start() -- see GroundTransport.start().
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Queued commands that an emergency_land_all supersedes: once the ground has
# ordered every drone down, a takeoff or start_at that was still waiting in
# the edge's recv() queue must not fly the drone back up afterwards.
_SUPERSEDED_BY_EMERGENCY = frozenset({fp.TAKEOFF, fp.START_AT})

# Per-client callback backlogs on the ground. When a callback falls this far
# behind, the client's read loop waits (backpressure) instead of buffering
# without bound.
_DETECTIONS_BACKLOG = 32
_CONTROL_BACKLOG = 256

# Default wait for one pong during clock sync.
_PING_ROUND_TIMEOUT_S = 1.0


class ClockSyncError(RuntimeError):
    """Clock sync with an edge failed (connection lost or too few pongs)."""


def _run_emergency_callback(callback: Callable[[dict[str, Any]], None], msg: dict[str, Any]) -> None:
    try:
        callback(msg)
    except Exception:
        logger.exception("on_emergency_land callback raised")


# ---------------------------------------------------------------------------
# Async transports
# ---------------------------------------------------------------------------


class EdgeTransport:
    """Sends DetectionMessages to the ground station via WebSocket.

    Once connected, a background reader owns the receive side of the socket
    (see the module docstring): it answers pings, applies clock offsets,
    fires ``on_emergency_land`` immediately on ``emergency_land_all``, and
    queues the remaining ground messages for ``recv()``.

    Args:
        on_emergency_land: Called with the message on a dedicated thread as
            soon as ``emergency_land_all`` arrives, so landing never waits
            for the frame loop. The message is also delivered through
            ``recv()``, ahead of everything else.
        clock: The edge's monotonic clock (injectable for tests). It stamps
            pong ``t2``/``t3`` and backs ``clock`` and ``link_age_s``.
    """

    def __init__(
        self,
        ws_url: str = "ws://localhost:9000",
        runner: EdgeRunner | None = None,
        token: str | None = None,
        max_retries: int | None = None,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        on_emergency_land: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._ws_url = ws_url
        self._ws = None
        self._connected = False
        self._runner = runner if runner is not None else EdgeRunner()
        self._token = token

        # Reconnection (exponential backoff). ``max_retries=None`` means
        # retry forever -- appropriate for a long-lived edge device that
        # should keep trying to reach the ground station.
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._retry_count = 0
        # True once a connection has been established at least once and
        # hasn't been torn down by an explicit disconnect() -- gates
        # automatic reconnection so a deliberate disconnect() doesn't
        # silently reconnect on the next send_detections()/recv() call.
        self._should_reconnect = False
        self._reconnect_task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()

        # Fleet control state, owned by the background reader.
        self._clock = clock
        self.clock = fp.EdgeClock(clock)
        self._on_emergency_land = on_emergency_land
        self._reader_task: asyncio.Task | None = None
        self._pong_tasks: set[asyncio.Task] = set()
        self._inbox: deque[dict[str, Any]] = deque()
        self._inbox_event = asyncio.Event()
        self._last_ground_msg_s: float | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def on_emergency_land(self) -> Callable[[dict[str, Any]], None] | None:
        return self._on_emergency_land

    @on_emergency_land.setter
    def on_emergency_land(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._on_emergency_land = callback

    @property
    def link_age_s(self) -> float | None:
        """Seconds since the last message from the ground, or ``None`` if none yet.

        Any ground message counts (heartbeat, ping, command), so this is the
        input for the edge's link-loss watchdog and the telemetry
        ``link_age_s`` field.
        """
        last = self._last_ground_msg_s
        if last is None:
            return None
        return max(0.0, self._clock() - last)

    async def connect(self) -> None:
        """Connect to the ground station WebSocket.

        No-ops if already connected -- calling ``connect()`` twice without an
        intervening ``disconnect()`` would otherwise orphan the first
        WebSocket (leaking the connection and its background reader task).
        Guarded by ``_connect_lock`` so a concurrent call (e.g. an explicit
        ``connect()`` racing the background ``_reconnect()`` task) can't slip
        past the ``_connected`` check and open a second, orphaned socket.
        """
        async with self._connect_lock:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        """Connection logic, assumes ``_connect_lock`` is already held."""
        if self._connected:
            return
        self._ws = await websockets.connect(self._ws_url)
        self._connected = True
        self._should_reconnect = True
        self._retry_count = 0
        # Nothing can be waiting in recv() while disconnected, so a fresh
        # event is safe here, and it binds to the loop this connection runs
        # on (the sync wrapper starts a new loop after a full disconnect).
        self._inbox_event = asyncio.Event()
        # The reader owns the receive side from here on, so a ping or an
        # emergency_land_all is handled even while the frame loop is busy.
        self._reader_task = asyncio.ensure_future(self._read_loop(self._ws))
        if self._token is not None:
            await self._ws.send(json.dumps({"type": "auth", "token": self._token}))

    async def _read_loop(self, ws) -> None:
        """Read every message from the ground on *ws* until it closes."""
        cancelled = False
        try:
            async for raw in ws:
                received_at = self._clock()
                if self._ws is ws:
                    self._last_ground_msg_s = received_at
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    logger.warning("recv: malformed message, ignoring", exc_info=True)
                    continue
                if not isinstance(data, dict):
                    logger.warning("recv: non-object message, ignoring: %r", data)
                    continue
                self._dispatch_incoming(ws, data, received_at)
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            logger.exception("[%s] Edge reader failed", self._ws_url)
        finally:
            # Only the reader of the current socket may mark the transport
            # disconnected; a stale reader from before a reconnect must not.
            if not cancelled and self._ws is ws and self._connected:
                self._connected = False
                if self._should_reconnect:
                    logger.warning("[%s] Connection to ground closed", self._ws_url)
                    self._schedule_reconnect()
            self._inbox_event.set()

    def _dispatch_incoming(self, ws, data: dict[str, Any], received_at: float) -> None:
        """Handle one ground message without ever waiting, so the next is read at once."""
        mtype = data.get("type")
        if mtype == fp.PING:
            # The pong goes out from its own task: if the socket's send buffer
            # is full, waiting for it here would hold up an emergency_land_all
            # arriving right behind the ping.
            task = asyncio.ensure_future(self._answer_ping(ws, data, received_at))
            self._pong_tasks.add(task)
            task.add_done_callback(self._pong_tasks.discard)
            return
        if mtype == fp.EMERGENCY_LAND_ALL:
            self._handle_emergency(data)
            return
        if mtype == fp.CLOCK:
            try:
                offset = self.clock.apply(data)
            except fp.ProtocolError as exc:
                logger.warning("[%s] Ignoring malformed clock message: %s", self._ws_url, exc)
                return
            logger.info("[%s] Clock offset set to %.6f s", self._ws_url, offset)
        self._inbox.append(data)
        self._inbox_event.set()

    async def _answer_ping(self, ws, data: dict[str, Any], received_at: float) -> None:
        # t3 is stamped here, immediately before the write.
        try:
            pong = fp.answer_ping(data, t2=received_at, clock=self._clock)
        except fp.ProtocolError as exc:
            logger.warning("[%s] Ignoring malformed ping: %s", self._ws_url, exc)
            return
        try:
            await ws.send(json.dumps(pong))
        except ConnectionClosed:
            # The read loop observes the closure on its next iteration.
            pass
        except Exception:
            logger.exception("[%s] Failed to send pong", self._ws_url)

    def _handle_emergency(self, data: dict[str, Any]) -> None:
        logger.warning("[%s] emergency_land_all received from ground", self._ws_url)
        callback = self._on_emergency_land
        if callback is not None:
            # A dedicated thread, not the shared executor, so landing never
            # waits behind other queued work.
            threading.Thread(
                target=_run_emergency_callback,
                args=(callback, dict(data)),
                name="edge-emergency-land",
                daemon=True,
            ).start()
        else:
            logger.warning(
                "[%s] No on_emergency_land callback set; emergency_land_all is "
                "delivered through recv() only",
                self._ws_url,
            )
        superseded = [m for m in self._inbox if m.get("type") in _SUPERSEDED_BY_EMERGENCY]
        if superseded:
            self._inbox = deque(
                m for m in self._inbox if m.get("type") not in _SUPERSEDED_BY_EMERGENCY
            )
            logger.warning(
                "[%s] Discarded %d queued %s message(s) superseded by emergency_land_all",
                self._ws_url,
                len(superseded),
                sorted({m["type"] for m in superseded}),
            )
        self._inbox.appendleft(dict(data))
        self._inbox_event.set()

    async def _reconnect(self) -> bool:
        """Attempt to reconnect with exponential backoff.

        Sleeps with a delay that doubles each attempt (capped at
        ``max_delay``), then tries to connect. Keeps trying until it
        succeeds, ``max_retries`` is exhausted, or reconnection is no longer
        wanted (e.g. ``disconnect()`` ran concurrently). Returns ``True`` once
        reconnected, ``False`` otherwise.

        Acquires ``_connect_lock`` around the connection attempt itself
        (via ``_connect_locked``, not the public ``connect()``) so it can't
        race an explicit ``connect()`` call without deadlocking on its own
        non-reentrant lock.
        """
        while self._should_reconnect and (
            self._max_retries is None or self._retry_count < self._max_retries
        ):
            delay = min(self._base_delay * (2**self._retry_count), self._max_delay)
            logger.info(
                "[%s] Reconnecting in %.1fs (attempt %d)",
                self._ws_url, delay, self._retry_count + 1,
            )
            await asyncio.sleep(delay)
            if not self._should_reconnect:
                return False
            try:
                async with self._connect_lock:
                    await self._connect_locked()
            except Exception as exc:
                self._retry_count += 1
                logger.warning("[%s] Reconnect attempt failed: %s", self._ws_url, exc)
                continue
            logger.info("[%s] Reconnected successfully", self._ws_url)
            return True

        if self._should_reconnect:
            logger.error(
                "[%s] Giving up reconnecting after %d attempts",
                self._ws_url, self._retry_count,
            )
        return False

    def _schedule_reconnect(self) -> None:
        """Kick off a background reconnect attempt, if one isn't already running.

        Runs ``_reconnect()`` as a fire-and-forget task so callers such as
        ``send_detections`` never block the caller (e.g. the frame-processing
        loop) waiting for the network to come back -- the connection is
        simply unavailable until the background attempt succeeds.
        """
        if not self._should_reconnect:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.ensure_future(self._reconnect())

    async def send_detections(self, msg: DetectionMessage) -> None:
        """Serialize and send a DetectionMessage.

        No-ops if not currently connected, scheduling a background
        reconnection attempt first if one is warranted (i.e. a previous
        connection dropped unexpectedly rather than via explicit
        ``disconnect()``). If the connection drops mid-send, marks the
        transport disconnected and schedules a reconnect rather than
        raising.
        """
        if not self._connected or self._ws is None:
            self._schedule_reconnect()
            return
        data = self._runner.to_dict(msg)
        try:
            await self._ws.send(json.dumps(data))
        except ConnectionClosed:
            logger.warning("send_detections: connection closed")
            self._connected = False
            self._schedule_reconnect()

    async def recv(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Return the next queued message from the ground station.

        Used to pick up control messages (``set_target``, ``stream_video`` and
        the fleet commands) pushed down from the ground station. Messages
        already queued are returned even after the connection drops, so an
        ``emergency_land_all`` received just before a disconnect is never
        lost; an ``emergency_land_all`` is always returned first. Pings are
        answered by the reader and never returned. Returns ``None`` when
        nothing is queued and the transport is disconnected, the connection
        drops while waiting, or the wait times out -- callers should treat
        ``None`` as "nothing to do" rather than an error.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            if self._inbox:
                return self._inbox.popleft()
            if not self._connected or self._ws is None:
                return None
            self._inbox_event.clear()
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._inbox_event.wait(), timeout=remaining)
            except (asyncio.TimeoutError, TimeoutError):
                return None

    async def send(self, message: dict[str, Any]) -> bool:
        """Send one edge -> ground control message (``pong``, ``ack``, ``telemetry``).

        Fleet messages are validated against the protocol schema first and
        raise ``fleet_protocol.ProtocolError`` when malformed, so a bad
        message fails loudly at the sender. Returns ``True`` once the message
        is written, or ``False`` when not connected or the connection drops
        mid-send (a background reconnect is scheduled, as for
        ``send_detections``).
        """
        fp.validate_message(message)
        if not self._connected or self._ws is None:
            self._schedule_reconnect()
            return False
        try:
            await self._ws.send(json.dumps(message))
        except ConnectionClosed:
            logger.warning("send: connection closed")
            self._connected = False
            self._schedule_reconnect()
            return False
        return True

    async def disconnect(self) -> None:
        """Close the connection and suppress any further reconnection.

        This is an explicit, user-initiated teardown: it cancels a
        background reconnect attempt if one is in flight and prevents
        ``send_detections``/``recv`` from auto-reconnecting afterwards.
        """
        self._should_reconnect = False
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except ConnectionClosed:
                pass
        reader = self._reader_task
        self._reader_task = None
        if reader is not None and not reader.done():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        self._connected = False
        self._inbox_event.set()


class _SerialDispatcher:
    """Runs callbacks one at a time, in submission order, on the default executor.

    The ground keeps one of these per client and per message class, so a slow
    scoring callback never delays reading the next message from the socket
    (pong receive times stay accurate) while per-client ordering is kept.
    """

    def __init__(self, name: str, maxsize: int):
        self._name = name
        self._maxsize = maxsize
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None

    async def submit(self, callback: Callable[[Any], None], arg: Any) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._maxsize)
            self._worker = asyncio.ensure_future(self._run())
        await self._queue.put((callback, arg))

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            item = await self._queue.get()
            if item is None:
                return
            callback, arg = item
            try:
                await loop.run_in_executor(None, callback, arg)
            except Exception:
                logger.exception("%s callback raised", self._name)

    async def close(self) -> None:
        """Finish every queued callback, then stop the worker."""
        if self._worker is None:
            return
        await self._queue.put(None)
        await self._worker


class GroundTransport:
    """Receives DetectionMessages from edge via WebSocket server.

    Also the ground end of the fleet control protocol (see the module
    docstring).

    Args:
        on_control: Called with each validated ``ack`` and ``telemetry``
            message from an edge, off the event loop and in per-client order.
        heartbeat_interval_s: When set, ``heartbeat`` messages are broadcast
            at this interval while the server runs. Off by default; the fleet
            ground sets it to ``fleet_protocol.HEARTBEAT_INTERVAL_S`` (or calls
            ``send_heartbeat`` itself), because the edges land when ground
            messages stop.
        clock: The ground's monotonic clock (injectable for tests). It stamps
            ping ``t1`` and pong ``t4``; ``start_at`` times must use it too.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9000,
        on_message: Callable[[DetectionMessage], None] | None = None,
        token: str | None = None,
        on_control: Callable[[dict[str, Any]], None] | None = None,
        heartbeat_interval_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._host = host
        self._port = port
        self._on_message = on_message  # callback: (DetectionMessage) -> None
        self._server = None
        self._clients: set = set()
        self._token = token
        self._authenticated_clients: set = set()

        # Fleet control state. All of it is touched only on the event loop.
        self._on_control = on_control
        self._heartbeat_interval_s = heartbeat_interval_s
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_seq = 0
        self._clock = clock
        self._drone_sockets: dict[str, Any] = {}  # drone_id -> websocket
        self._socket_drones: dict[Any, str] = {}  # websocket -> drone_id
        self._pending_pings: dict[Any, dict[float, asyncio.Future]] = {}
        self._clock_estimates: dict[str, fp.ClockEstimate] = {}
        self._last_seen_s: dict[str, float] = {}
        self._telemetry: dict[str, dict[str, Any]] = {}
        self._start_acks: dict[str, float] = {}  # drone_id -> executed_at_ground_s

    @property
    def clients(self) -> set:
        """Currently connected edge client sockets."""
        return set(self._clients)

    @property
    def drone_ids(self) -> list[str]:
        """Drone ids of the connected edges that have identified themselves."""
        return sorted(self._drone_sockets)

    def now(self) -> float:
        """The ground's monotonic clock, the time base for ``start_at``."""
        return self._clock()

    @property
    def port(self) -> int:
        """The actual bound port (useful when constructed with port=0)."""
        if self._server is not None and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self._port

    async def _authenticate(self, websocket) -> bool:
        """Consume the first message as an auth handshake.

        Only called when ``self._token`` is set. The first message from the
        client must be ``{"type": "auth", "token": "..."}`` with a matching
        token; anything else (wrong token, wrong shape, malformed JSON, or
        the connection closing before a message arrives) rejects and closes
        the connection.
        """
        try:
            raw = await websocket.recv()
        except ConnectionClosed:
            return False

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data = None

        if (
            isinstance(data, dict)
            and data.get("type") == "auth"
            and hmac.compare_digest(str(data.get("token", "")), self._token)
        ):
            self._authenticated_clients.add(websocket)
            return True

        logger.warning("Rejecting client: failed auth handshake")
        try:
            await websocket.close(code=4001, reason="unauthorized")
        except ConnectionClosed:
            pass
        return False

    async def _handle_client(self, websocket) -> None:
        """Handle incoming messages from an edge client."""
        self._clients.add(websocket)
        detections = _SerialDispatcher("on_message", _DETECTIONS_BACKLOG)
        controls = _SerialDispatcher("on_control", _CONTROL_BACKLOG)
        try:
            if self._token is not None:
                if not await self._authenticate(websocket):
                    return

            async for raw in websocket:
                # Stamp arrival first: for a pong this is t4.
                received_at = self._clock()
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Received malformed message, ignoring", exc_info=True)
                    continue

                if not isinstance(data, dict):
                    logger.warning("Received non-object message, ignoring: %r", data)
                    continue

                msg_type = data.get("type")
                if msg_type == "detections":
                    try:
                        msg = EdgeRunner.from_dict(data)
                    except (KeyError, TypeError, ValueError):
                        logger.warning("Malformed detections message, ignoring", exc_info=True)
                        continue
                    self._note_drone(websocket, data.get("drone_id"), received_at)
                    if self._on_message:
                        # Dispatch off the event loop thread so a slow
                        # scoring callback doesn't block other clients, and
                        # through a per-client queue so it doesn't delay
                        # reading this client's pongs and acks either.
                        await detections.submit(self._on_message, msg)
                elif msg_type == fp.PONG:
                    self._resolve_pong(websocket, data, received_at)
                elif msg_type in (fp.ACK, fp.TELEMETRY):
                    try:
                        fp.validate_message(data)
                    except fp.ProtocolError as exc:
                        logger.warning("Malformed %s message, ignoring: %s", msg_type, exc)
                        continue
                    self._note_drone(websocket, data["drone_id"], received_at)
                    self._record_control(data)
                    if self._on_control:
                        await controls.submit(self._on_control, data)
                elif msg_type == "set_target":
                    # Target updates normally flow ground -> edge (see
                    # send_target/broadcast_target below). This branch exists
                    # so a client echoing/relaying a set_target message is
                    # tolerated rather than logged as "unknown type".
                    pass
                elif msg_type == "stream_video":
                    # Stream-video toggles normally flow ground -> edge (see
                    # send_stream_video/broadcast_stream_video below). Tolerate
                    # a client echoing/relaying one rather than logging it as
                    # an "unknown type".
                    pass
                else:
                    logger.warning("Unknown message type: %r", msg_type)
        except ConnectionClosed:
            logger.info("Edge client disconnected")
        finally:
            self._clients.discard(websocket)
            self._authenticated_clients.discard(websocket)
            self._forget_socket(websocket)
            await detections.close()
            await controls.close()

    def _note_drone(self, websocket, drone_id: Any, received_at: float) -> None:
        """Remember which socket carries *drone_id* and when it was last heard."""
        if not isinstance(drone_id, str) or not drone_id:
            return
        previous = self._drone_sockets.get(drone_id)
        if previous is not websocket:
            if previous is not None:
                logger.warning("Drone %r moved to a new connection", drone_id)
                self._socket_drones.pop(previous, None)
            self._drone_sockets[drone_id] = websocket
            self._socket_drones[websocket] = drone_id
        self._last_seen_s[drone_id] = received_at

    def _record_control(self, data: dict[str, Any]) -> None:
        drone_id = data["drone_id"]
        if data["type"] == fp.TELEMETRY:
            self._telemetry[drone_id] = dict(data)
        elif data["type"] == fp.ACK and data["for"] == fp.START_AT:
            self._start_acks[drone_id] = float(data["executed_at_ground_s"])

    def _forget_socket(self, websocket) -> None:
        drone_id = self._socket_drones.pop(websocket, None)
        if drone_id is not None and self._drone_sockets.get(drone_id) is websocket:
            del self._drone_sockets[drone_id]
            logger.warning("Drone %r disconnected", drone_id)
        pending = self._pending_pings.pop(websocket, None) or {}
        for future in pending.values():
            if not future.done():
                future.set_exception(ClockSyncError("edge disconnected during clock sync"))

    def _resolve_pong(self, websocket, data: dict[str, Any], t4: float) -> None:
        drone_id = self._socket_drones.get(websocket)
        if drone_id is not None:
            self._last_seen_s[drone_id] = t4
        try:
            sample = fp.sample_from_pong(data, t4)
        except fp.ProtocolError as exc:
            logger.warning("Ignoring malformed pong: %s", exc)
            return
        pending = self._pending_pings.get(websocket)
        future = pending.pop(sample.t1, None) if pending else None
        if future is None or future.done():
            logger.debug("Ignoring unsolicited or late pong (t1=%r)", sample.t1)
            return
        future.set_result(sample)

    async def start(self) -> None:
        """Start the WebSocket server.

        No-ops if already started -- calling ``start()`` twice without an
        intervening ``stop()`` would otherwise leak the first server (it
        keeps listening and accepting connections with no way to reach it
        again).

        Secure-by-default: binding a non-loopback host (i.e. exposing the
        edge socket beyond the local machine) without a configured token is
        refused, so a deployed ground station can't accidentally accept
        unauthenticated edge clients.
        """
        if self._server is not None:
            return
        if self._host not in _LOOPBACK_HOSTS and self._token is None:
            raise ValueError(
                f"Refusing to bind GroundTransport to non-loopback host "
                f"{self._host!r} without authentication. Set the "
                f"FLIGHTRISK_EDGE_TOKEN environment variable (or pass "
                f"token=) to enable the WebSocket auth handshake, or bind a "
                f"loopback host (e.g. 127.0.0.1) for local development."
            )
        self._server = await websockets.serve(self._handle_client, self._host, self._port)
        if self._heartbeat_interval_s is not None:
            self._heartbeat_task = asyncio.ensure_future(
                self._heartbeat_loop(self._heartbeat_interval_s)
            )

    async def stop(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for websocket in list(self._pending_pings):
            self._forget_socket(websocket)
        self._clients.clear()
        self._authenticated_clients.clear()
        self._drone_sockets.clear()
        self._socket_drones.clear()

    async def send_target(
        self,
        websocket,
        reid_embedding,
        face_embedding=None,
    ) -> None:
        """Send target embeddings to a single edge client."""
        msg = {
            "type": "set_target",
            "reid_embedding": reid_embedding,
            "face_embedding": face_embedding,
        }
        try:
            await websocket.send(json.dumps(msg))
        except ConnectionClosed:
            self._clients.discard(websocket)
            self._authenticated_clients.discard(websocket)

    async def broadcast_target(self, reid_embedding, face_embedding=None) -> None:
        """Send target embeddings to every currently connected edge client.

        When a shared-secret token is configured, only clients that have
        completed the auth handshake receive target updates (which include
        biometric embeddings); otherwise all connected clients are treated
        as eligible, matching pre-auth behavior.
        """
        clients = list(self._authenticated_clients) if self._token is not None else list(self._clients)
        if not clients:
            return
        await asyncio.gather(
            *(self.send_target(ws, reid_embedding, face_embedding) for ws in clients),
            return_exceptions=True,
        )

    async def send_stream_video(self, websocket, enabled: bool) -> None:
        """Send a stream-video toggle to a single edge client."""
        msg = {
            "type": "stream_video",
            "enabled": enabled,
        }
        try:
            await websocket.send(json.dumps(msg))
        except ConnectionClosed:
            self._clients.discard(websocket)
            self._authenticated_clients.discard(websocket)

    async def broadcast_stream_video(self, enabled: bool) -> None:
        """Send a stream-video toggle to every connected edge client.

        When a shared-secret token is configured, only clients that have
        completed the auth handshake receive the toggle; otherwise all
        connected clients are treated as eligible, matching broadcast_target.
        """
        clients = list(self._authenticated_clients) if self._token is not None else list(self._clients)
        if not clients:
            return
        await asyncio.gather(
            *(self.send_stream_video(ws, enabled) for ws in clients),
            return_exceptions=True,
        )

    # -- Fleet control (design section 3) ----------------------------------

    def _eligible_clients(self) -> list:
        """Clients allowed to receive control: authenticated ones when a token is set."""
        return list(self._authenticated_clients) if self._token is not None else list(self._clients)

    def _describe(self, websocket) -> str:
        drone_id = self._socket_drones.get(websocket)
        return repr(drone_id) if drone_id is not None else "an unidentified edge"

    async def _send_json(self, websocket, payload: str) -> bool:
        try:
            await websocket.send(payload)
        except ConnectionClosed:
            self._clients.discard(websocket)
            self._authenticated_clients.discard(websocket)
            return False
        return True

    async def send_to_drone(self, drone_id: str, message: dict[str, Any]) -> bool:
        """Send one control message to the edge that reported *drone_id*.

        Fleet messages are validated first and raise
        ``fleet_protocol.ProtocolError`` when malformed. Returns ``False``
        (and logs) when that drone is not connected or the send fails.
        """
        mtype = fp.validate_message(message)
        level = logging.ERROR if mtype == fp.EMERGENCY_LAND_ALL else logging.WARNING
        websocket = self._drone_sockets.get(drone_id)
        if websocket is None:
            logger.log(level, "Cannot send %r: drone %r is not connected", mtype, drone_id)
            return False
        if await self._send_json(websocket, json.dumps(message)):
            return True
        logger.log(level, "Failed to send %r to drone %r: connection closed", mtype, drone_id)
        return False

    async def broadcast_control(self, message: dict[str, Any]) -> int:
        """Send one control message to every eligible edge; returns how many it reached.

        When a token is configured only authenticated clients are eligible,
        as for ``broadcast_target``. An ``emergency_land_all`` is routed
        through ``emergency_land_all()``.
        """
        mtype = fp.validate_message(message)
        if mtype == fp.EMERGENCY_LAND_ALL:
            return await self.emergency_land_all()
        clients = self._eligible_clients()
        if not clients:
            return 0
        payload = json.dumps(message)
        results = await asyncio.gather(
            *(self._send_json(ws, payload) for ws in clients),
            return_exceptions=True,
        )
        return sum(1 for result in results if result is True)

    async def emergency_land_all(self) -> int:
        """Order every connected edge to land now; returns how many it reached.

        Written straight to every connected socket at once, with no queue in
        front of it. It goes to every connected client, including one still
        completing the auth handshake, because a land order carries no data
        and reaching an extra drone is the safe failure. A failure to reach a
        client, or having no client at all, is logged as an error rather than
        dropped silently.
        """
        payload = json.dumps(fp.make_emergency_land_all())
        clients = list(self._clients)
        if not clients:
            logger.error("emergency_land_all: no edge clients are connected; nothing was sent")
            return 0
        results = await asyncio.gather(
            *(self._send_json(ws, payload) for ws in clients),
            return_exceptions=True,
        )
        sent = 0
        for websocket, result in zip(clients, results):
            if result is True:
                sent += 1
            else:
                logger.error(
                    "emergency_land_all: failed to reach %s: %r",
                    self._describe(websocket),
                    result,
                )
        logger.warning("emergency_land_all sent to %d of %d edge client(s)", sent, len(clients))
        return sent

    async def send_heartbeat(self) -> int:
        """Broadcast the next ``heartbeat`` message; returns how many edges it reached."""
        seq = self._heartbeat_seq
        self._heartbeat_seq += 1
        return await self.broadcast_control(fp.make_heartbeat(seq))

    async def _heartbeat_loop(self, interval_s: float) -> None:
        while True:
            try:
                await self.send_heartbeat()
            except Exception:
                logger.exception("Heartbeat broadcast failed")
            await asyncio.sleep(interval_s)

    def _resolve_target(self, target: Any) -> tuple[Any, str | None]:
        if isinstance(target, str):
            websocket = self._drone_sockets.get(target)
            if websocket is None:
                raise LookupError(f"no connected edge has reported drone_id {target!r}")
            return websocket, target
        if target not in self._eligible_clients():
            raise LookupError("clock sync target is not a connected, authenticated edge")
        return target, self._socket_drones.get(target)

    async def _ping_once(self, websocket, timeout_s: float) -> fp.ClockSample | None:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pending = self._pending_pings.setdefault(websocket, {})
        t1 = self._clock()
        pending[t1] = future
        try:
            await websocket.send(json.dumps(fp.make_ping(t1)))
            return await asyncio.wait_for(future, timeout=timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("Clock sync round to %s timed out after %.2f s", self._describe(websocket), timeout_s)
            return None
        finally:
            pending.pop(t1, None)

    async def sync_clock(
        self,
        target: Any,
        rounds: int = fp.CLOCK_SYNC_ROUNDS,
        round_timeout_s: float = _PING_ROUND_TIMEOUT_S,
    ) -> fp.ClockEstimate:
        """Measure one edge's clock offset and send it a ``clock`` message.

        *target* is a ``drone_id`` (or a connected client socket). Runs
        *rounds* sequential ping/pong rounds, keeps the median offset (edge
        clock minus ground clock) and median round-trip time, stores the
        estimate for that drone, and sends ``clock{offset_s}`` with the
        median unchanged. Raises ``LookupError`` for an unknown target and
        ``ClockSyncError`` when the connection drops or fewer than
        ``fleet_protocol.CLOCK_SYNC_MIN_SAMPLES`` rounds succeed.
        """
        websocket, drone_id = self._resolve_target(target)
        label = self._describe(websocket)
        needed = min(rounds, fp.CLOCK_SYNC_MIN_SAMPLES)
        samples: list[fp.ClockSample] = []
        try:
            for _ in range(rounds):
                sample = await self._ping_once(websocket, round_timeout_s)
                if sample is not None:
                    samples.append(sample)
        except ConnectionClosed as exc:
            raise ClockSyncError(f"connection to {label} closed during clock sync") from exc
        if len(samples) < needed:
            raise ClockSyncError(
                f"clock sync with {label} got {len(samples)} of {rounds} pongs; need {needed}"
            )
        estimate = fp.estimate_clock(samples, measured_at_s=self._clock())
        if drone_id is not None:
            self._clock_estimates[drone_id] = estimate
        if not await self._send_json(websocket, json.dumps(fp.make_clock(estimate.offset_s))):
            raise ClockSyncError(f"connection to {label} closed before the clock offset was sent")
        logger.info(
            "Clock sync with %s: offset %.6f s, rtt %.6f s over %d rounds",
            label, estimate.offset_s, estimate.rtt_s, estimate.rounds,
        )
        return estimate

    def clock_estimate(self, drone_id: str) -> fp.ClockEstimate | None:
        """The latest clock estimate for *drone_id*, if it has been synced."""
        return self._clock_estimates.get(drone_id)

    def start_skew_s(self) -> float | None:
        """Spread between the earliest and latest ``start_at`` acks, or ``None``."""
        if not self._start_acks:
            return None
        return max(self._start_acks.values()) - min(self._start_acks.values())

    def fleet_status(self) -> dict[str, dict[str, Any]]:
        """Per-drone link, clock and start state for the dashboard fleet panel."""
        now = self._clock()
        skews = fp.start_skews_s(self._start_acks)
        drone_ids = set(self._drone_sockets) | set(self._clock_estimates) | set(self._telemetry)
        status: dict[str, dict[str, Any]] = {}
        for drone_id in sorted(drone_ids):
            estimate = self._clock_estimates.get(drone_id)
            last_seen = self._last_seen_s.get(drone_id)
            telemetry = self._telemetry.get(drone_id)
            status[drone_id] = {
                "connected": drone_id in self._drone_sockets,
                "offset_s": estimate.offset_s if estimate is not None else None,
                "rtt_s": estimate.rtt_s if estimate is not None else None,
                "link_age_s": now - last_seen if last_seen is not None else None,
                "executed_at_ground_s": self._start_acks.get(drone_id),
                "start_skew_s": skews.get(drone_id),
                "telemetry": dict(telemetry) if telemetry is not None else None,
            }
        return status


# ---------------------------------------------------------------------------
# Sync wrappers (background-thread event loop bridge)
# ---------------------------------------------------------------------------
#
# Both sync wrappers below use the shared ``flightrisk.async_bridge.AsyncBridge``
# (the same bridge pattern used by ``flightrisk.drone.mavlink.MavlinkController``)
# to drive their async transport from threading-based callers like the
# Flask/SocketIO dashboard app.


class EdgeTransportSync:
    """Synchronous wrapper around ``EdgeTransport`` for threaded callers."""

    def __init__(
        self,
        ws_url: str = "ws://localhost:9000",
        runner: EdgeRunner | None = None,
        token: str | None = None,
        max_retries: int | None = None,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        on_emergency_land: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._transport = EdgeTransport(
            ws_url=ws_url,
            runner=runner,
            token=token,
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
            on_emergency_land=on_emergency_land,
            clock=clock,
        )
        self._bridge = AsyncBridge(name="edge-transport", cmd_timeout=_CMD_TIMEOUT)

    @property
    def connected(self) -> bool:
        return self._transport.connected

    @property
    def clock(self) -> fp.EdgeClock:
        """The edge's view of the ground clock, updated by ``clock`` messages."""
        return self._transport.clock

    @property
    def link_age_s(self) -> float | None:
        return self._transport.link_age_s

    @property
    def on_emergency_land(self) -> Callable[[dict[str, Any]], None] | None:
        return self._transport.on_emergency_land

    @on_emergency_land.setter
    def on_emergency_land(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._transport.on_emergency_land = callback

    def connect(self, timeout: float = _CMD_TIMEOUT) -> None:
        self._bridge.start_loop()
        self._bridge.run(self._transport.connect(), timeout=timeout)

    def send_detections(self, msg: DetectionMessage, timeout: float = _CMD_TIMEOUT) -> None:
        self._bridge.run(self._transport.send_detections(msg), timeout=timeout)

    def recv(self, recv_timeout: float | None = None, timeout: float = _CMD_TIMEOUT) -> dict[str, Any] | None:
        return self._bridge.run(self._transport.recv(timeout=recv_timeout), timeout=timeout)

    def send(self, message: dict[str, Any], timeout: float = _CMD_TIMEOUT) -> bool:
        """Send one edge -> ground control message; see ``EdgeTransport.send``."""
        return self._bridge.run(self._transport.send(message), timeout=timeout)

    def disconnect(self, timeout: float = _CMD_TIMEOUT) -> None:
        if self._bridge.is_running:
            try:
                self._bridge.run(self._transport.disconnect(), timeout=timeout)
            except Exception:
                logger.warning("[edge-transport] disconnect error", exc_info=True)
        self._bridge.stop_loop()


async def _call_on_loop(fn: Callable[[], Any]) -> Any:
    """Run *fn* on the transport's event loop, where its state is mutated."""
    return fn()


class GroundTransportSync:
    """Synchronous wrapper around ``GroundTransport`` for threaded callers."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9000,
        on_message: Callable[[DetectionMessage], None] | None = None,
        token: str | None = None,
        on_control: Callable[[dict[str, Any]], None] | None = None,
        heartbeat_interval_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._transport = GroundTransport(
            host=host,
            port=port,
            on_message=on_message,
            token=token,
            on_control=on_control,
            heartbeat_interval_s=heartbeat_interval_s,
            clock=clock,
        )
        self._bridge = AsyncBridge(name="ground-transport", cmd_timeout=_CMD_TIMEOUT)

    @property
    def port(self) -> int:
        return self._transport.port

    def now(self) -> float:
        """The ground's monotonic clock, the time base for ``start_at``."""
        return self._transport.now()

    def start(self, timeout: float = _CMD_TIMEOUT) -> None:
        self._bridge.start_loop()
        self._bridge.run(self._transport.start(), timeout=timeout)

    def broadcast_target(self, reid_embedding, face_embedding=None, timeout: float = _CMD_TIMEOUT) -> None:
        self._bridge.run(
            self._transport.broadcast_target(reid_embedding, face_embedding),
            timeout=timeout,
        )

    def broadcast_stream_video(self, enabled: bool, timeout: float = _CMD_TIMEOUT) -> None:
        self._bridge.run(
            self._transport.broadcast_stream_video(enabled),
            timeout=timeout,
        )

    def send_to_drone(self, drone_id: str, message: dict[str, Any], timeout: float = _CMD_TIMEOUT) -> bool:
        return self._bridge.run(self._transport.send_to_drone(drone_id, message), timeout=timeout)

    def broadcast_control(self, message: dict[str, Any], timeout: float = _CMD_TIMEOUT) -> int:
        return self._bridge.run(self._transport.broadcast_control(message), timeout=timeout)

    def emergency_land_all(self, timeout: float = _CMD_TIMEOUT) -> int:
        """Order every connected edge to land now; see ``GroundTransport.emergency_land_all``.

        Raises ``RuntimeError`` when the transport is not running, so the
        order can never vanish silently.
        """
        if not self._bridge.is_running:
            logger.error("emergency_land_all: ground transport is not running; nothing was sent")
            raise RuntimeError("ground transport is not running; emergency_land_all was not sent")
        return self._bridge.run(self._transport.emergency_land_all(), timeout=timeout)

    def send_heartbeat(self, timeout: float = _CMD_TIMEOUT) -> int:
        return self._bridge.run(self._transport.send_heartbeat(), timeout=timeout)

    def sync_clock(
        self,
        drone_id: str,
        rounds: int = fp.CLOCK_SYNC_ROUNDS,
        round_timeout_s: float = _PING_ROUND_TIMEOUT_S,
        timeout: float = _CMD_TIMEOUT,
    ) -> fp.ClockEstimate:
        return self._bridge.run(
            self._transport.sync_clock(drone_id, rounds=rounds, round_timeout_s=round_timeout_s),
            timeout=timeout,
        )

    def drone_ids(self, timeout: float = _CMD_TIMEOUT) -> list[str]:
        return self._bridge.run(_call_on_loop(lambda: self._transport.drone_ids), timeout=timeout)

    def clock_estimate(self, drone_id: str, timeout: float = _CMD_TIMEOUT) -> fp.ClockEstimate | None:
        return self._bridge.run(
            _call_on_loop(lambda: self._transport.clock_estimate(drone_id)), timeout=timeout,
        )

    def start_skew_s(self, timeout: float = _CMD_TIMEOUT) -> float | None:
        return self._bridge.run(_call_on_loop(self._transport.start_skew_s), timeout=timeout)

    def fleet_status(self, timeout: float = _CMD_TIMEOUT) -> dict[str, dict[str, Any]]:
        return self._bridge.run(_call_on_loop(self._transport.fleet_status), timeout=timeout)

    def stop(self, timeout: float = _CMD_TIMEOUT) -> None:
        if self._bridge.is_running:
            try:
                self._bridge.run(self._transport.stop(), timeout=timeout)
            except Exception:
                logger.warning("[ground-transport] stop error", exc_info=True)
        self._bridge.stop_loop()
