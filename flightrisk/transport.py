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
  edge's messages (refusing a second live connection that claims a
  ``drone_id`` already in use), and hands validated ``ack``, ``nack`` and
  ``telemetry`` messages to an ``on_control`` callback.
* ``EdgeTransport`` runs a background reader once connected. The reader
  answers ``ping`` with ``pong`` immediately (the frame loop never sees
  pings), stores the ``clock`` offset in ``EdgeTransport.clock``, fires the
  ``on_emergency_land`` callback on its own thread the moment an
  ``emergency_land_all`` arrives and puts that message at the front of the
  ``recv()`` queue, and queues every other message for ``recv()`` in arrival
  order.

``emergency_land_all`` is never queued behind other traffic on either side
and is never dropped without an error log.

The ground can listen on several addresses at once, for example
``"127.0.0.1,10.42.0.1"``: loopback for the edge on the same laptop and the
laptop-to-laptop link address for the other laptop's edge. Each address gets
its own listener, so losing the link address (a Thunderbolt bridge can lose
its address when the cable is pulled) never affects the loopback listener or
the local edge's connection. See ``GroundTransport`` and
``check_bind_hosts``.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import socket
import threading
import time
from collections import deque
from typing import Any, Callable, Iterable

import websockets
from websockets.exceptions import ConnectionClosed

from flightrisk import fleet_protocol as fp
from flightrisk.async_bridge import AsyncBridge
from flightrisk.edge import DetectionMessage, EdgeRunner
from flightrisk.observability import StructuredLogger

logger = logging.getLogger(__name__)

__all__ = [
    "EdgeTransport",
    "GroundTransport",
    "EdgeTransportSync",
    "GroundTransportSync",
    "ClockSyncError",
    "parse_bind_hosts",
    "is_wildcard_host",
    "check_bind_hosts",
]

_CMD_TIMEOUT = 30.0  # seconds for sync-over-async calls

# Hosts treated as local-only. Binding one of these without a token is
# allowed (open, for local dev); binding anything else without a token is
# refused at start() -- see GroundTransport.start().
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Hosts that listen on every interface of this machine. A Tello's access
# point is open Wi-Fi, so on a fleet laptop a wildcard bind would expose the
# edge socket to anyone who joins a drone's network. A list of bind hosts
# never accepts one (see check_bind_hosts); a single wildcard host keeps its
# legacy meaning and still needs a token.
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})

# How often the ground re-checks its non-loopback bind addresses. It retries
# a listener that could not bind (for example while the link cable is
# unplugged and the interface has no address) and logs when an address goes
# away or comes back.
_BIND_CHECK_INTERVAL_S = 2.0

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

# Default wait for the WebSocket pong of a drone's current connection when a
# second connection claims the same drone_id. A connection that answers in
# time is live, so the second one is refused; one that does not is treated as
# dropped, so the second one is a reconnect and takes over.
_DUPLICATE_PING_TIMEOUT_S = 1.0

# Close code for a connection that loses a drone_id conflict: a second
# connection refused while the first is live, or a dropped connection that a
# reconnect replaced.
_CLOSE_DRONE_ID_CONFLICT = 4009


class ClockSyncError(RuntimeError):
    """Clock sync with an edge failed (connection lost or too few pongs)."""


def _is_superseded_by_emergency(msg: dict[str, Any]) -> bool:
    mtype = msg.get("type")
    return isinstance(mtype, str) and mtype in _SUPERSEDED_BY_EMERGENCY


def _run_emergency_callback(callback: Callable[[dict[str, Any]], None], msg: dict[str, Any]) -> None:
    try:
        callback(msg)
    except Exception:
        logger.exception("on_emergency_land callback raised")


# ---------------------------------------------------------------------------
# Bind hosts
# ---------------------------------------------------------------------------


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """*host* as an IP address, or ``None`` when it is a name rather than a literal."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_list_form(host: str | Iterable[str]) -> bool:
    """Whether a bind-host setting was given as a list rather than as one legacy host string."""
    return not (isinstance(host, str) and "," not in host)


def parse_bind_hosts(host: str | Iterable[str]) -> tuple[str, ...]:
    """Return the ground's bind hosts as an ordered tuple without duplicates.

    A string without a comma is one host and keeps its legacy meaning; only
    surrounding whitespace is removed, so ``""`` still means every interface.
    A string with commas, such as ``"127.0.0.1,10.42.0.1"`` from
    ``FLIGHTRISK_EDGE_HOST``, is a list, and so is a list or tuple of
    strings. An empty entry in a list raises ``ValueError`` instead of being
    read as the wildcard ``""``.
    """
    if isinstance(host, str):
        if "," not in host:
            return (host.strip(),)
        entries: list[Any] = host.split(",")
    else:
        entries = list(host)
    hosts: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            raise TypeError(f"bind hosts must be strings, got {type(entry).__name__}")
        entry = entry.strip()
        if not entry:
            raise ValueError(
                "The list of bind hosts has an empty entry. Name each address, for example "
                "127.0.0.1,10.42.0.1; an empty entry would listen on every interface."
            )
        if entry not in hosts:
            hosts.append(entry)
    if not hosts:
        raise ValueError("No bind host was given. Name at least one address, for example 127.0.0.1.")
    return tuple(hosts)


def is_wildcard_host(host: str) -> bool:
    """Whether binding *host* listens on every interface (``0.0.0.0``, ``::`` and equivalents)."""
    host = host.strip()
    if host in _WILDCARD_HOSTS:
        return True
    address = _ip_literal(host)
    if address is None:
        return False
    if address.is_unspecified:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped is not None and mapped.is_unspecified


def _has_token(token: str | None) -> bool:
    # An empty token would let any client pass the handshake with an empty
    # token, so it does not count as authentication for an exposed bind.
    return token is not None and token != ""


def check_bind_hosts(hosts: Iterable[str], token: str | None, *, list_form: bool = False) -> None:
    """Refuse an unsafe ground bind; raises ``ValueError`` with an operator-facing message.

    The rules, in order:

    * With ``list_form`` (the hosts came as a list, for example
      ``127.0.0.1,10.42.0.1``), no entry may be a wildcard address, because
      it would also listen on the drone's open Wi-Fi, and every entry that is
      not loopback must be a literal IP address, because a name can resolve
      to more interfaces than intended.
    * Any host outside ``127.0.0.1``, ``localhost`` and ``::1`` needs a
      non-empty token (``FLIGHTRISK_EDGE_TOKEN``), as before.
    """
    hosts = tuple(hosts)
    if not hosts:
        raise ValueError("No bind host was given. Name at least one address, for example 127.0.0.1.")
    if list_form:
        for host in hosts:
            if is_wildcard_host(host):
                raise ValueError(
                    f"Refusing to bind GroundTransport to the wildcard address {host!r} in a list of "
                    f"bind hosts: it listens on every interface, including a drone's open Wi-Fi. "
                    f"List the loopback address and the link address instead, for example "
                    f"127.0.0.1,10.42.0.1."
                )
        for host in hosts:
            if host not in _LOOPBACK_HOSTS and _ip_literal(host) is None:
                raise ValueError(
                    f"Refusing to bind GroundTransport to {host!r}: in a list of bind hosts every "
                    f"address other than loopback must be a literal IP address such as 10.42.0.1, "
                    f"because a name can resolve to more interfaces than intended."
                )
    if not _has_token(token):
        for host in hosts:
            if host not in _LOOPBACK_HOSTS:
                raise ValueError(
                    f"Refusing to bind GroundTransport to non-loopback host "
                    f"{host!r} without authentication. Set the "
                    f"FLIGHTRISK_EDGE_TOKEN environment variable (or pass "
                    f"token=) to enable the WebSocket auth handshake, or bind a "
                    f"loopback host (e.g. 127.0.0.1) for local development."
                )


def _address_is_local(host: str) -> bool:
    """Whether *host* is an address of one of this machine's interfaces right now.

    Checked by binding a throwaway TCP socket to it on an ephemeral port; the
    socket never listens and is closed at once. A name (not a literal) cannot
    be checked this way and counts as present, so the real bind decides.
    """
    address = _ip_literal(host)
    if address is None:
        return True
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((host, 0))
    except OSError:
        return False
    return True


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
        # Queue it first (this cannot fail), ahead of everything else, and
        # drop any takeoff or start_at that was waiting behind it.
        superseded = [m for m in self._inbox if _is_superseded_by_emergency(m)]
        if superseded:
            self._inbox = deque(m for m in self._inbox if not _is_superseded_by_emergency(m))
            logger.warning(
                "[%s] Discarded %d queued %s message(s) superseded by emergency_land_all",
                self._ws_url,
                len(superseded),
                sorted({m["type"] for m in superseded}),
            )
        self._inbox.appendleft(dict(data))
        self._inbox_event.set()

        callback = self._on_emergency_land
        if callback is None:
            logger.warning(
                "[%s] No on_emergency_land callback set; emergency_land_all is "
                "delivered through recv() only",
                self._ws_url,
            )
            return
        try:
            # A dedicated thread, not the shared executor, so landing never
            # waits behind other queued work.
            threading.Thread(
                target=_run_emergency_callback,
                args=(callback, dict(data)),
                name="edge-emergency-land",
                daemon=True,
            ).start()
        except Exception:
            logger.exception(
                "[%s] Could not start the emergency-land thread; emergency_land_all "
                "is still delivered through recv()",
                self._ws_url,
            )

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
        """Send one edge -> ground control message (``pong``, ``ack``, ``nack``, ``telemetry``).

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
        on_control: Called with each validated ``ack``, ``nack`` and
            ``telemetry`` message from an edge, off the event loop and in
            per-client order.
        heartbeat_interval_s: When set, ``heartbeat`` messages are broadcast
            at this interval while the server runs. Off by default; the fleet
            ground sets it to ``fleet_protocol.HEARTBEAT_INTERVAL_S`` (or calls
            ``send_heartbeat`` itself), because the edges land when ground
            messages stop.
        clock: The ground's monotonic clock (injectable for tests). It stamps
            ping ``t1`` and pong ``t4``; ``start_at`` times must use it too.
        duplicate_ping_timeout_s: How long a drone's current connection has
            to answer a WebSocket ping when a second connection claims the
            same ``drone_id`` (see ``_claim_drone``).
        host: One bind host (the legacy form, unchanged), or several as a
            comma-separated string or a list, for example
            ``"127.0.0.1,10.42.0.1"``. Every host gets its own listener on
            the same port, and ``check_bind_hosts`` decides at ``start()``
            whether the set is safe. When a list includes a loopback host, a
            non-loopback host that cannot bind (its address is missing, for
            example while the link cable is unplugged) does not stop the
            ground: the loopback listeners run, the failure is logged, and
            the bind is retried every ``bind_check_interval_s``.
        bind_check_interval_s: How often non-loopback bind addresses are
            re-checked (see ``host``).
        address_probe: Returns whether an address is currently assigned to
            this machine (injectable for tests).
        event_log: ``StructuredLogger`` for bind events (injectable for tests).
    """

    def __init__(
        self,
        host: str | Iterable[str] = "0.0.0.0",
        port: int = 9000,
        on_message: Callable[[DetectionMessage], None] | None = None,
        token: str | None = None,
        on_control: Callable[[dict[str, Any]], None] | None = None,
        heartbeat_interval_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        duplicate_ping_timeout_s: float = _DUPLICATE_PING_TIMEOUT_S,
        bind_check_interval_s: float = _BIND_CHECK_INTERVAL_S,
        address_probe: Callable[[str], bool] = _address_is_local,
        event_log: Any = None,
    ):
        self._host = host
        self._hosts = parse_bind_hosts(host)
        self._hosts_list_form = _is_list_form(host)
        self._port = port
        self._on_message = on_message  # callback: (DetectionMessage) -> None
        # One listener per bind host. ``_server`` is the first one that bound
        # (a loopback one when the list has any) and doubles as the
        # "server is running" flag.
        self._server = None
        self._servers: dict[str, Any] = {}
        self._bound_port: int | None = None
        self._bind_check_interval_s = bind_check_interval_s
        self._address_probe = address_probe
        self._event_log = event_log if event_log is not None else StructuredLogger(component="ground-transport")
        self._bind_watch_task: asyncio.Task | None = None
        self._address_present: dict[str, bool] = {}
        self._bind_errors: dict[str, int | None] = {}
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
        self._duplicate_ping_timeout_s = duplicate_ping_timeout_s
        self._closing_tasks: set[asyncio.Task] = set()

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

    @property
    def hosts(self) -> tuple[str, ...]:
        """Every configured bind host, in the order given."""
        return self._hosts

    @property
    def bound_hosts(self) -> tuple[str, ...]:
        """The bind hosts that currently have a listener."""
        return tuple(host for host in self._hosts if host in self._servers)

    @property
    def unbound_hosts(self) -> tuple[str, ...]:
        """While running, the bind hosts that have no listener yet (being retried)."""
        if self._server is None:
            return ()
        return tuple(host for host in self._hosts if host not in self._servers)

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
                    if not await self._claim_drone(websocket, data.get("drone_id"), received_at):
                        return
                    if self._on_message:
                        # Dispatch off the event loop thread so a slow
                        # scoring callback doesn't block other clients, and
                        # through a per-client queue so it doesn't delay
                        # reading this client's pongs and acks either.
                        await detections.submit(self._on_message, msg)
                elif msg_type == fp.PONG:
                    self._resolve_pong(websocket, data, received_at)
                elif msg_type in (fp.ACK, fp.NACK, fp.TELEMETRY):
                    try:
                        fp.validate_message(data)
                    except fp.ProtocolError as exc:
                        logger.warning("Malformed %s message, ignoring: %s", msg_type, exc)
                        continue
                    if not await self._claim_drone(websocket, data["drone_id"], received_at):
                        return
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

    async def _claim_drone(self, websocket, drone_id: Any, received_at: float) -> bool:
        """Bind *drone_id* to *websocket*, unless another live connection carries it.

        Routing for a ``drone_id`` stays with the connection that claimed it
        first while that connection is live. A second connection claiming
        the same ``drone_id`` (for example a fake edge started while the real
        edge is still running) is refused: a warning is logged and the second
        connection is closed with code 4009, so the two edges never take
        turns receiving that drone's commands. The current connection is live
        when it answers a WebSocket ping within ``duplicate_ping_timeout_s``.

        A reconnect is never refused. When the previous connection has
        closed, it no longer holds the ``drone_id``. When it is still open
        but does not answer the ping (a link that dropped without a TCP
        reset), the new connection takes over the ``drone_id`` and the old
        one is closed. Returns ``False`` when the claim was refused; the
        caller then stops reading from *websocket*.
        """
        if not isinstance(drone_id, str) or not drone_id:
            return True
        while True:
            previous = self._drone_sockets.get(drone_id)
            if previous is None or previous is websocket:
                break
            live = await self._answers_ping(previous)
            if self._drone_sockets.get(drone_id) is not previous:
                # The drone's connection changed while the ping was out: check again.
                continue
            if live:
                logger.warning(
                    "Refusing a second connection for drone %r: its current connection is "
                    "still live. Stop the other edge that uses drone_id %r.",
                    drone_id, drone_id,
                )
                try:
                    await websocket.close(code=_CLOSE_DRONE_ID_CONFLICT, reason="drone_id already connected")
                except ConnectionClosed:
                    pass
                return False
            logger.warning(
                "Drone %r reconnected; its previous connection did not answer a ping, "
                "so the new connection takes over and the old one is closed",
                drone_id,
            )
            del self._drone_sockets[drone_id]
            self._socket_drones.pop(previous, None)
            self._close_in_background(previous, "drone_id reconnected on a new connection")
            break
        self._note_drone(websocket, drone_id, received_at)
        return True

    async def _answers_ping(self, websocket) -> bool:
        """Whether *websocket* answers a WebSocket ping within ``duplicate_ping_timeout_s``."""
        if websocket not in self._clients:
            return False

        async def round_trip() -> None:
            pong = await websocket.ping()
            await pong

        try:
            await asyncio.wait_for(round_trip(), timeout=self._duplicate_ping_timeout_s)
        except (asyncio.TimeoutError, TimeoutError, ConnectionClosed):
            return False
        except Exception:
            logger.warning("Could not ping %s", self._describe(websocket), exc_info=True)
            return False
        return True

    def _close_in_background(self, websocket, reason: str) -> None:
        """Close *websocket* without waiting: a dropped link can take seconds to close."""

        async def close() -> None:
            try:
                await websocket.close(code=_CLOSE_DRONE_ID_CONFLICT, reason=reason)
            except Exception:
                logger.debug("Closing a replaced edge connection failed", exc_info=True)

        task = asyncio.ensure_future(close())
        self._closing_tasks.add(task)
        task.add_done_callback(self._closing_tasks.discard)

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
        unauthenticated edge clients. A list of bind hosts is also refused
        when it contains a wildcard address or a non-loopback name (see
        ``check_bind_hosts``).

        Loopback hosts bind first, and a loopback bind failure always
        raises. A non-loopback host that fails to bind raises too, unless the
        list also has a loopback host: then the ground runs on the listeners
        it has, logs ``ground_bind_unavailable`` and retries that host every
        ``bind_check_interval_s``. Nothing is ever bound more widely than
        configured.
        """
        if self._server is not None:
            return
        check_bind_hosts(self._hosts, self._token, list_form=self._hosts_list_form)
        # The local edge depends on the loopback listeners, and with port=0
        # the first one fixes the port that every other listener reuses.
        ordered = sorted(self._hosts, key=lambda host: host not in _LOOPBACK_HOSTS)
        can_degrade = len(self._hosts) > 1 and any(host in _LOOPBACK_HOSTS for host in self._hosts)
        port = self._port
        try:
            for host in ordered:
                try:
                    server = await self._serve_host(host, port)
                except OSError as exc:
                    if host in _LOOPBACK_HOSTS or not can_degrade:
                        raise
                    self._address_present[host] = False
                    self._note_bind_failure(host, port, exc)
                    continue
                self._add_server(host, server, port)
                if port == 0 and server.sockets:
                    port = server.sockets[0].getsockname()[1]
        except BaseException:
            await self._close_servers()
            self._address_present.clear()
            self._bind_errors.clear()
            raise
        self._bound_port = port
        if self._heartbeat_interval_s is not None:
            self._heartbeat_task = asyncio.ensure_future(
                self._heartbeat_loop(self._heartbeat_interval_s)
            )
        if self._watched_hosts():
            self._bind_watch_task = asyncio.ensure_future(self._bind_watch_loop())

    async def _serve_host(self, host: str, port: int):
        """Start one listener on *host*.

        If the caller is cancelled while the bind is in flight (``stop()``
        cancelling the bind watcher), the listener the bind produces is
        closed rather than left listening with nothing tracking it.
        """
        bind = asyncio.ensure_future(websockets.serve(self._handle_client, host, port))
        try:
            return await asyncio.shield(bind)
        except asyncio.CancelledError:
            try:
                server = await bind
            except BaseException:
                pass
            else:
                server.close()
                await server.wait_closed()
            raise

    def _add_server(self, host: str, server, port: int) -> None:
        self._servers[host] = server
        if self._server is None:
            self._server = server
        self._address_present[host] = True
        self._bind_errors.pop(host, None)
        actual = server.sockets[0].getsockname()[1] if server.sockets else port
        self._event_log.info(
            "ground_bind_listening",
            host=host,
            port=actual,
            loopback=host in _LOOPBACK_HOSTS,
        )

    def _note_bind_failure(self, host: str, port: int, exc: OSError) -> None:
        """Log a failed bind once per distinct error, so a retry every few seconds stays quiet."""
        code = exc.errno
        if host in self._bind_errors and self._bind_errors[host] == code:
            return
        self._bind_errors[host] = code
        self._event_log.error(
            "ground_bind_unavailable",
            host=host,
            port=port,
            errno=code,
            retry_s=self._bind_check_interval_s,
            listening=list(self.bound_hosts),
        )

    def _watched_hosts(self) -> list[str]:
        """Non-loopback literal hosts whose address the bind watcher re-checks."""
        return [
            host for host in self._hosts
            if host not in _LOOPBACK_HOSTS and not is_wildcard_host(host) and _ip_literal(host) is not None
        ]

    async def _bind_watch_loop(self) -> None:
        """Re-check the non-loopback bind addresses until ``stop()``. Never raises."""
        while True:
            await asyncio.sleep(self._bind_check_interval_s)
            try:
                await self.recheck_binds()
            except Exception:
                logger.exception("Bind address re-check failed")

    async def recheck_binds(self) -> None:
        """Re-check each non-loopback bind address once.

        A host without a listener is bound again once its address is back
        (``ground_bind_recovered``). A host with a listener keeps it whatever
        happens to its address: the loss and the return are logged
        (``ground_bind_address_lost``, ``ground_bind_address_restored``), and
        no connection is closed, so an edge whose link comes back within its
        link-loss timeout carries on. Loopback listeners are never touched.
        """
        if self._server is None:
            return
        for host in self._watched_hosts():
            try:
                present = bool(self._address_probe(host))
            except Exception:
                logger.warning("Address probe failed", exc_info=True)
                continue
            was_present = self._address_present.get(host)
            self._address_present[host] = present
            if host in self._servers:
                if present and was_present is False:
                    self._event_log.info("ground_bind_address_restored", host=host, port=self._bound_port)
                elif not present and was_present is not False:
                    self._event_log.warning(
                        "ground_bind_address_lost",
                        host=host,
                        port=self._bound_port,
                        listener="kept",
                        listening=list(self.bound_hosts),
                    )
                continue
            if not present or self._bound_port is None:
                continue
            try:
                server = await self._serve_host(host, self._bound_port)
            except OSError as exc:
                self._note_bind_failure(host, self._bound_port, exc)
                continue
            if self._server is None:
                # stop() ran while the bind was in flight.
                server.close()
                await server.wait_closed()
                return
            self._add_server(host, server, self._bound_port)
            self._event_log.info("ground_bind_recovered", host=host, port=self._bound_port)

    async def _close_servers(self) -> None:
        servers = list(self._servers.values())
        self._servers.clear()
        self._server = None
        for server in servers:
            server.close()
        for server in servers:
            await server.wait_closed()

    async def stop(self) -> None:
        watch = self._bind_watch_task
        self._bind_watch_task = None
        if watch is not None and not watch.done():
            watch.cancel()
            try:
                await watch
            except asyncio.CancelledError:
                pass
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        closing = list(self._closing_tasks)
        for task in closing:
            task.cancel()
        if closing:
            await asyncio.gather(*closing, return_exceptions=True)
        await self._close_servers()
        self._bound_port = None
        self._address_present.clear()
        self._bind_errors.clear()
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
        host: str | Iterable[str] = "0.0.0.0",
        port: int = 9000,
        on_message: Callable[[DetectionMessage], None] | None = None,
        token: str | None = None,
        on_control: Callable[[dict[str, Any]], None] | None = None,
        heartbeat_interval_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        duplicate_ping_timeout_s: float = _DUPLICATE_PING_TIMEOUT_S,
    ):
        self._transport = GroundTransport(
            host=host,
            port=port,
            on_message=on_message,
            token=token,
            on_control=on_control,
            heartbeat_interval_s=heartbeat_interval_s,
            clock=clock,
            duplicate_ping_timeout_s=duplicate_ping_timeout_s,
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
