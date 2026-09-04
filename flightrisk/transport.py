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
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from flightrisk.async_bridge import AsyncBridge
from flightrisk.edge import DetectionMessage, EdgeRunner

logger = logging.getLogger(__name__)

__all__ = [
    "EdgeTransport",
    "GroundTransport",
    "EdgeTransportSync",
    "GroundTransportSync",
]

_CMD_TIMEOUT = 30.0  # seconds for sync-over-async calls

# Hosts treated as local-only. Binding one of these without a token is
# allowed (open, for local dev); binding anything else without a token is
# refused at start() -- see GroundTransport.start().
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


# ---------------------------------------------------------------------------
# Async transports
# ---------------------------------------------------------------------------


class EdgeTransport:
    """Sends DetectionMessages to the ground station via WebSocket."""

    def __init__(
        self,
        ws_url: str = "ws://localhost:9000",
        runner: EdgeRunner | None = None,
        token: str | None = None,
        max_retries: int | None = None,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
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

    @property
    def connected(self) -> bool:
        return self._connected

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
        if self._token is not None:
            await self._ws.send(json.dumps({"type": "auth", "token": self._token}))

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
        """Receive and decode one message from the ground station.

        Used to pick up ``set_target`` updates pushed down from the ground
        station. Returns ``None`` if not connected, the connection drops, the
        wait times out, or the message can't be decoded as JSON -- callers
        should treat ``None`` as "nothing to do" rather than an error.
        """
        if not self._connected or self._ws is None:
            return None
        try:
            if timeout is not None:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
            else:
                raw = await self._ws.recv()
        except ConnectionClosed:
            logger.warning("recv: connection closed")
            self._connected = False
            self._schedule_reconnect()
            return None
        except (asyncio.TimeoutError, TimeoutError):
            return None

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("recv: malformed message, ignoring", exc_info=True)
            return None

        if not isinstance(data, dict):
            logger.warning("recv: non-object message, ignoring: %r", data)
            return None

        return data

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
        self._connected = False


class GroundTransport:
    """Receives DetectionMessages from edge via WebSocket server."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9000,
        on_message: Callable[[DetectionMessage], None] | None = None,
        token: str | None = None,
    ):
        self._host = host
        self._port = port
        self._on_message = on_message  # callback: (DetectionMessage) -> None
        self._server = None
        self._clients: set = set()
        self._token = token
        self._authenticated_clients: set = set()

    @property
    def clients(self) -> set:
        """Currently connected edge client sockets."""
        return set(self._clients)

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
            and data.get("token") == self._token
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
        try:
            if self._token is not None:
                if not await self._authenticate(websocket):
                    return

            async for raw in websocket:
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
                    if self._on_message:
                        # Dispatch off the event loop thread so a slow
                        # scoring callback doesn't block other clients.
                        loop = asyncio.get_event_loop()
                        try:
                            await loop.run_in_executor(None, self._on_message, msg)
                        except Exception:
                            logger.exception("on_message callback raised")
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

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._clients.clear()
        self._authenticated_clients.clear()

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
    ):
        self._transport = EdgeTransport(
            ws_url=ws_url,
            runner=runner,
            token=token,
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
        )
        self._bridge = AsyncBridge(name="edge-transport", cmd_timeout=_CMD_TIMEOUT)

    @property
    def connected(self) -> bool:
        return self._transport.connected

    def connect(self, timeout: float = _CMD_TIMEOUT) -> None:
        self._bridge.start_loop()
        self._bridge.run(self._transport.connect(), timeout=timeout)

    def send_detections(self, msg: DetectionMessage, timeout: float = _CMD_TIMEOUT) -> None:
        self._bridge.run(self._transport.send_detections(msg), timeout=timeout)

    def recv(self, recv_timeout: float | None = None, timeout: float = _CMD_TIMEOUT) -> dict[str, Any] | None:
        return self._bridge.run(self._transport.recv(timeout=recv_timeout), timeout=timeout)

    def disconnect(self, timeout: float = _CMD_TIMEOUT) -> None:
        if self._bridge.is_running:
            try:
                self._bridge.run(self._transport.disconnect(), timeout=timeout)
            except Exception:
                logger.warning("[edge-transport] disconnect error", exc_info=True)
        self._bridge.stop_loop()


class GroundTransportSync:
    """Synchronous wrapper around ``GroundTransport`` for threaded callers."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9000,
        on_message: Callable[[DetectionMessage], None] | None = None,
        token: str | None = None,
    ):
        self._transport = GroundTransport(host=host, port=port, on_message=on_message, token=token)
        self._bridge = AsyncBridge(name="ground-transport", cmd_timeout=_CMD_TIMEOUT)

    @property
    def port(self) -> int:
        return self._transport.port

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

    def stop(self, timeout: float = _CMD_TIMEOUT) -> None:
        if self._bridge.is_running:
            try:
                self._bridge.run(self._transport.stop(), timeout=timeout)
            except Exception:
                logger.warning("[ground-transport] stop error", exc_info=True)
        self._bridge.stop_loop()
