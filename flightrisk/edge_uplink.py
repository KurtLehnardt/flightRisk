"""Multi-ground edge uplink: one connection per ground station, one authority.

``EdgeUplink`` replaces ``EdgeTransportSync`` on a flying edge when
``FLIGHTRISK_GROUND_URLS`` (or ``--ground-urls``) lists the ground stations,
for example ``ws://127.0.0.1:9000,ws://10.42.0.2:9000``. It exposes the same
calls the edge device uses (``connect``, ``send_detections``, ``recv``,
``send``, ``disconnect``, ``on_emergency_land``, ``link_age_s``), so the
lane executor and the edge loop do not change. The single-URL path
(``--ws-url``/``FLIGHTRISK_EDGE_WS``) keeps using ``EdgeTransportSync``
unchanged, as protocol v1.

How it works:

* One ``EdgeTransport`` per URL, all on one background event loop, each in
  raw mode: every frame comes here. Each keeps reconnecting on its own with
  the fleet backoff (1 s doubling to 2 s), so one ground being down never
  affects the link to another.
* Every frame is decoded with ``fleet_protocol.decode`` in the
  ground-to-edge direction, with the connection's agreed version (1 until a
  ``hello``; never ``None``), so a landing is never refused. Frames up to
  ``inline_decode_bytes`` are decoded on the loop (microseconds); larger
  ones in a worker thread, because a hostile 1 MiB frame can take tens of
  milliseconds and the loop also carries the other ground's traffic.
* ``EdgeAuthority`` (``flightrisk.fleet_authority``) decides what each
  message does. Landings are executed from any ground; everything else only
  from the authority. Only the authority's messages feed the link-loss
  watchdog (``feeds_watchdog`` tells the edge device not to feed it again).
* Outbound: telemetry, acks and nacks go to every ground that has agreed a
  version (completed for v2 by ``EdgeAuthority.outbound`` and shaped per
  link); detections (crops, embeddings, thumbnails) go to the authority
  only, and are dropped while there is none.
* A message accepted from the authority is re-checked when the executor
  takes it (``begin_dispatch``): if a new leader was adopted in between, it
  is dropped and answered with ``nack{stale_epoch}``.

Privacy: logs carry URLs without credentials, ids, types and sizes only.
"""
from __future__ import annotations

import asyncio
import os
import re
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Hashable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from flightrisk import fleet_protocol as fp
from flightrisk.async_bridge import AsyncBridge
from flightrisk.edge import DetectionMessage, EdgeRunner
from flightrisk.fleet_authority import ACCEPT, CLOSE, LAND, EdgeAuthority
from flightrisk.fleet_ground_link import CLOSE_PROTOCOL
from flightrisk.observability import StructuredLogger
from flightrisk.transport import (
    EdgeTransport,
    _is_superseded_by_emergency,
    _run_emergency_callback,
)

__all__ = [
    "ENV_FLEET_LEGACY",
    "ENV_GROUND_URLS",
    "ENV_INFLIGHT_HANDOVER",
    "ENV_NODE_ID",
    "EdgeUplink",
    "LaneExecutorHooks",
    "default_node_id",
    "env_flag",
    "parse_ground_urls",
]

ENV_GROUND_URLS = "FLIGHTRISK_GROUND_URLS"
ENV_NODE_ID = "FLIGHTRISK_NODE_ID"
ENV_FLEET_LEGACY = "FLIGHTRISK_FLEET_LEGACY"
ENV_INFLIGHT_HANDOVER = "FLIGHTRISK_INFLIGHT_HANDOVER"

# Frames up to this size are decoded on the event loop; larger ones in a
# worker thread. Every fleet control message is far smaller.
INLINE_DECODE_BYTES = 4096
_CMD_TIMEOUT = 30.0
_TRUE_VALUES = ("1", "true", "yes", "on")
_NOT_ID_CHARS = re.compile(r"[^A-Za-z0-9._:-]+")


def parse_ground_urls(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    """The ground URLs from a comma-separated string (or a list), in order, without duplicates.

    Raises ``ValueError`` for an empty entry or a URL that is not ``ws://``
    or ``wss://`` with a host. ``None`` or an empty string gives ``()``.
    """
    if raw is None:
        return ()
    entries = raw.split(",") if isinstance(raw, str) else list(raw)
    if isinstance(raw, str) and not raw.strip():
        return ()
    urls: list[str] = []
    for entry in entries:
        url = str(entry).strip()
        if not url:
            raise ValueError(f"{ENV_GROUND_URLS} has an empty entry; separate ground URLs with single commas")
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss") or not parts.hostname:
            raise ValueError(f"{ENV_GROUND_URLS} entry {safe_url(url)!r} is not a ws:// or wss:// URL with a host")
        if url not in urls:
            urls.append(url)
    return tuple(urls)


def safe_url(url: str) -> str:
    """*url* without any user name or password, for logs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid url>"
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def default_node_id(environ: Mapping[str, str] | None = None) -> str:
    """This laptop's node id: ``FLIGHTRISK_NODE_ID``, else the host name as an identifier."""
    env = os.environ if environ is None else environ
    configured = env.get(ENV_NODE_ID, "").strip()
    if configured:
        return configured
    name = _NOT_ID_CHARS.sub("-", socket.gethostname().split(".")[0]).strip("-._:")[: fp.MAX_ID_LEN]
    return name if fp.is_identifier(name) else "edge-node"


def env_flag(name: str, environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(name, "").strip().lower() in _TRUE_VALUES


class LaneExecutorHooks:
    """``fleet_authority.ExecutorHooks`` over a ``LaneExecutor``; PR-5's additions are used by name."""

    def __init__(self, executor: Any):
        self._executor = executor

    def is_active(self) -> bool:
        executor = self._executor
        try:
            probe = getattr(executor, "is_active", None)
            if callable(probe):
                return bool(probe())
            flying = bool(getattr(executor.drone.state, "is_flying", False))
            return flying or bool(executor.is_busy)
        except Exception:  # noqa: BLE001 - an unreadable state counts as flying
            return True

    def supports(self, mtype: str) -> bool:
        return mtype in getattr(self._executor, "CONTROL_TYPES", frozenset())

    def snapshot(self) -> Mapping[str, Any]:
        telemetry = self._executor.telemetry()
        return {key: telemetry.get(key) for key in ("is_flying", "lane_index", "battery")}

    def fleet_status(self) -> Mapping[str, Any] | None:
        status = getattr(self._executor, "fleet_status", None)
        return status() if callable(status) else None

    def invalidate_plan(self, reason: str) -> None:
        invalidate = getattr(self._executor, "invalidate_plan", None)
        if callable(invalidate):
            invalidate(reason)


class EdgeUplink:
    """Connects one edge to several ground stations and obeys one of them; see the module docstring."""

    # The uplink feeds the link watchdog itself, from the authority only.
    feeds_watchdog = True

    def __init__(
        self,
        urls: Iterable[str],
        *,
        drone_id: str,
        node_id: str,
        runner: EdgeRunner | None = None,
        token: str | None = None,
        base_delay: float = 1.0,
        max_delay: float = 2.0,
        legacy: bool = False,
        inflight_handover: bool = False,
        clock: Callable[[], float] = time.monotonic,
        logger: StructuredLogger | None = None,
        inline_decode_bytes: int = INLINE_DECODE_BYTES,
        on_emergency_land: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._urls = parse_ground_urls(list(urls))
        if not self._urls:
            raise ValueError("EdgeUplink needs at least one ground URL")
        self._log = logger if logger is not None else StructuredLogger(component="edge-uplink")
        self._clock = clock
        self.clock = fp.EdgeClock(clock)
        self._inline_decode_bytes = int(inline_decode_bytes)
        self._on_emergency_land = on_emergency_land
        self._watchdog_feed: Callable[[], None] | None = None
        self._last_authority_msg_s: float | None = None
        self.authority = EdgeAuthority(
            drone_id, node_id, legacy=legacy, inflight_handover=inflight_handover, logger=self._log
        )
        self._bridge = AsyncBridge(name="edge-uplink", cmd_timeout=_CMD_TIMEOUT)
        self._inbox: deque[tuple[dict[str, Any], Any]] = deque()
        self._inbox_event: asyncio.Event | None = None
        self._tasks: set[asyncio.Task] = set()
        self._dropped_detections = 0
        self._transports: list[EdgeTransport] = []
        for index, url in enumerate(self._urls):
            label = safe_url(url)
            self._transports.append(EdgeTransport(
                ws_url=url,
                runner=runner,
                token=token,
                base_delay=base_delay,
                max_delay=max_delay,
                clock=clock,
                on_frame=self._frame_handler(index),
                on_connected=lambda ws, label=label: self.authority.opened(ws, label),
                on_closed=self.authority.closed,
            ))

    # -- Properties the edge device uses ------------------------------------

    @property
    def urls(self) -> tuple[str, ...]:
        return self._urls

    @property
    def connected(self) -> bool:
        return any(t.connected for t in self._transports)

    @property
    def link_age_s(self) -> float | None:
        """Seconds since the last message from the authority, or ``None`` before the first."""
        last = self._last_authority_msg_s
        return None if last is None else max(0.0, self._clock() - last)

    @property
    def on_emergency_land(self) -> Callable[[dict[str, Any]], None] | None:
        return self._on_emergency_land

    @on_emergency_land.setter
    def on_emergency_land(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._on_emergency_land = callback

    def bind_executor(self, flight: Any) -> None:
        """Wire the lane executor: authority hooks and the watchdog feed."""
        self.authority.bind_hooks(LaneExecutorHooks(flight))
        self._watchdog_feed = flight.note_ground_message

    # -- Sync API -----------------------------------------------------------

    def connect(self, timeout: float = _CMD_TIMEOUT) -> None:
        """Start connecting to every ground; a ground that is down is retried in the background."""
        self._bridge.start_loop()
        self._bridge.run(self._connect_all(), timeout=timeout)
        self._log.info(
            "edge_uplink_start",
            drone_id=self.authority.drone_id,
            node_id=self.authority.node_id,
            grounds=[safe_url(url) for url in self._urls],
            connected=[safe_url(t.ws_url) for t in self._transports if t.connected],
            legacy=self.authority.legacy,
        )

    def send_detections(self, msg: DetectionMessage, timeout: float = _CMD_TIMEOUT) -> None:
        if self._bridge.is_running:
            self._bridge.run(self._send_detections(msg), timeout=timeout)

    def recv(self, recv_timeout: float | None = None, timeout: float = _CMD_TIMEOUT) -> dict[str, Any] | None:
        """The next message for the executor, or ``None``; see ``EdgeAuthority.begin_dispatch``."""
        self.authority.begin_dispatch(None)
        while True:
            if not self._bridge.is_running:
                return None
            item = self._bridge.run(self._recv(recv_timeout), timeout=timeout)
            if item is None:
                return None
            msg, admitted = item
            out = self.authority.begin_dispatch(msg, admitted)
            if out is not None:
                return out
            stale = self.authority.stale_reply()
            if stale is not None:
                self._bridge.run(self._broadcast(stale), timeout=timeout)

    def send(self, message: dict[str, Any], timeout: float = _CMD_TIMEOUT) -> bool:
        """Send an executor message (telemetry, ack, nack) to every ground; True if any got it."""
        completed = self.authority.outbound(message)
        if not self._bridge.is_running:
            return False
        return self._bridge.run(self._broadcast(completed), timeout=timeout)

    def disconnect(self, timeout: float = _CMD_TIMEOUT) -> None:
        if self._bridge.is_running:
            try:
                self._bridge.run(self._disconnect_all(), timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - teardown must finish
                self._log.warning("edge_uplink_disconnect_error", error=repr(exc))
        self._bridge.stop_loop()

    # -- Event loop side -----------------------------------------------------

    async def _connect_all(self) -> None:
        if self._inbox_event is None:
            self._inbox_event = asyncio.Event()
        await asyncio.gather(*(t.connect(retry_in_background=True) for t in self._transports))

    async def _disconnect_all(self) -> None:
        await asyncio.gather(*(t.disconnect() for t in self._transports), return_exceptions=True)
        for task in list(self._tasks):
            task.cancel()
        if self._inbox_event is not None:
            self._inbox_event.set()

    def _frame_handler(self, index: int):
        async def on_frame(ws: Any, raw: Any, received_at: float) -> None:
            transport = self._transports[index]
            try:
                await self._on_frame(transport, ws, raw, received_at)
            except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the reader
                self._log.error("edge_uplink_frame_error", url=safe_url(transport.ws_url), error=repr(exc))

        return on_frame

    async def _on_frame(self, transport: EdgeTransport, ws: Any, raw: Any, received_at: float) -> None:
        if transport.websocket is not ws:
            return
        proto = self.authority.proto_for(ws)
        size = len(raw) if isinstance(raw, (str, bytes, bytearray)) else 0
        try:
            if size <= self._inline_decode_bytes:
                msg = fp.decode(raw, direction=fp.DIRECTION_GROUND_TO_EDGE, proto=proto)
            else:
                msg = await asyncio.to_thread(fp.decode, raw, direction=fp.DIRECTION_GROUND_TO_EDGE, proto=proto)
        except fp.ProtocolError:
            self.authority.on_decode_error(ws, size)
            return
        if transport.websocket is not ws and msg.get("type") not in fp.FAILSAFE_TYPES:
            # The connection went away during a threaded decode. A landing
            # still lands; anything else belongs to a connection that is gone.
            return
        decision = self.authority.on_message(ws, msg)
        if decision.feed_watchdog:
            self._feed(received_at)
        if decision.action == LAND:
            self._land(msg, decision.admitted)
        elif decision.action == ACCEPT:
            self._accept(transport, ws, msg, decision.admitted, received_at)
        transport.proto = self.authority.proto_for(ws)
        for reply in decision.replies:
            await self._reply(transport, ws, reply)
        if decision.action == CLOSE:
            await transport.drop_connection(ws, CLOSE_PROTOCOL, decision.reason)

    def _feed(self, received_at: float) -> None:
        self._last_authority_msg_s = received_at
        feed = self._watchdog_feed
        if feed is not None:
            feed()

    def _accept(self, transport: EdgeTransport, ws: Any, msg: dict[str, Any], admitted: Any, received_at: float) -> None:
        mtype = msg.get("type")
        if mtype == fp.PING:
            self._spawn(self._answer_ping(transport, ws, msg, received_at))
            return
        if mtype == fp.CLOCK:
            try:
                self.clock.apply(msg)
            except fp.ProtocolError:
                self._log.warning("edge_clock_invalid", url=safe_url(transport.ws_url))
                return
        self._enqueue(msg, admitted)

    def _land(self, msg: dict[str, Any], admitted: Any) -> None:
        if msg.get("type") != fp.EMERGENCY_LAND_ALL:
            self._enqueue(msg, admitted)
            return
        before = len(self._inbox)
        self._inbox = deque(item for item in self._inbox if not _is_superseded_by_emergency(item[0]))
        if len(self._inbox) != before:
            self._log.warning("edge_queued_motion_discarded", count=before - len(self._inbox))
        self._inbox.appendleft((dict(msg), admitted))
        self._wake()
        callback = self._on_emergency_land
        if callback is None:
            return
        try:
            threading.Thread(
                target=_run_emergency_callback, args=(callback, dict(msg)), name="edge-emergency-land", daemon=True
            ).start()
        except Exception as exc:  # noqa: BLE001 - recv() still delivers the landing
            self._log.error("edge_emergency_thread_error", error=repr(exc))

    def _enqueue(self, msg: dict[str, Any], admitted: Any) -> None:
        self._inbox.append((msg, admitted))
        self._wake()

    def _wake(self) -> None:
        if self._inbox_event is not None:
            self._inbox_event.set()

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer_ping(self, transport: EdgeTransport, ws: Any, msg: dict[str, Any], received_at: float) -> None:
        try:
            pong = fp.answer_ping(msg, t2=received_at, clock=self._clock)
        except fp.ProtocolError:
            return
        await self._reply(transport, ws, pong)

    async def _reply(self, transport: EdgeTransport, ws: Hashable, reply: dict[str, Any]) -> bool:
        try:
            return await transport.send(reply, proto=self.authority.proto_for(ws), ws=ws)
        except fp.ProtocolError as exc:
            self._log.warning("edge_reply_invalid", type=reply.get("type"), error=str(exc)[:200])
            return False

    async def _recv(self, timeout: float | None) -> tuple[dict[str, Any], Any] | None:
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        event = self._inbox_event
        while True:
            if self._inbox:
                return self._inbox.popleft()
            if event is None or not self.connected:
                return None
            event.clear()
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return None
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except (asyncio.TimeoutError, TimeoutError):
                return None

    async def _broadcast(self, msg: dict[str, Any]) -> bool:
        keys = set(self.authority.broadcast_keys())
        sends = []
        for transport in self._transports:
            ws = transport.websocket
            if ws is None or ws not in keys:
                continue
            shaped = self.authority.shape_for(ws, msg)
            if shaped is None:
                continue
            sends.append(self._send_shaped(transport, ws, *shaped))
        if not sends:
            return False
        results = await asyncio.gather(*sends, return_exceptions=True)
        return any(result is True for result in results)

    async def _send_shaped(self, transport: EdgeTransport, ws: Any, msg: dict[str, Any], proto: int) -> bool:
        try:
            return await transport.send(msg, proto=proto, ws=ws)
        except fp.ProtocolError as exc:
            self._log.warning(
                "edge_send_invalid", type=msg.get("type"), url=safe_url(transport.ws_url), error=str(exc)[:200]
            )
            return False

    async def _send_detections(self, msg: DetectionMessage) -> None:
        key = self.authority.authority_key
        for transport in self._transports:
            if key is not None and transport.websocket is key:
                await transport.send_detections(msg, ws=key)
                return
        self._dropped_detections += 1
        if self._dropped_detections == 1 or self._dropped_detections % 100 == 0:
            self._log.warning(
                "edge_detections_dropped_no_authority",
                drone_id=self.authority.drone_id,
                dropped=self._dropped_detections,
            )
