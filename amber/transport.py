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
``amber.drone.mavlink.MavlinkController``, so the threading-based Flask/
SocketIO app (``amber/dashboard/app.py``) can drive them without itself
becoming async.

This module is a transport layer only: it does not modify or depend on the
internals of ``amber/edge.py`` or ``amber/ground.py`` beyond their public
``EdgeRunner``/``DetectionMessage``/``GroundStation`` API (``to_dict`` /
``from_dict`` / ``process_message``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from amber.edge import DetectionMessage, EdgeRunner

logger = logging.getLogger(__name__)

__all__ = [
    "EdgeTransport",
    "GroundTransport",
    "EdgeTransportSync",
    "GroundTransportSync",
]

_CMD_TIMEOUT = 30.0  # seconds for sync-over-async calls


# ---------------------------------------------------------------------------
# Async transports
# ---------------------------------------------------------------------------


class EdgeTransport:
    """Sends DetectionMessages to the ground station via WebSocket."""

    def __init__(self, ws_url: str = "ws://localhost:9000", runner: EdgeRunner | None = None):
        self._ws_url = ws_url
        self._ws = None
        self._connected = False
        self._runner = runner if runner is not None else EdgeRunner()

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Connect to the ground station WebSocket."""
        self._ws = await websockets.connect(self._ws_url)
        self._connected = True

    async def send_detections(self, msg: DetectionMessage) -> None:
        """Serialize and send a DetectionMessage.

        No-ops if not currently connected. If the connection has dropped
        underneath us, marks the transport disconnected rather than raising,
        so callers can decide whether/how to reconnect.
        """
        if not self._connected or self._ws is None:
            return
        data = self._runner.to_dict(msg)
        try:
            await self._ws.send(json.dumps(data))
        except ConnectionClosed:
            logger.warning("send_detections: connection closed")
            self._connected = False

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
    ):
        self._host = host
        self._port = port
        self._on_message = on_message  # callback: (DetectionMessage) -> None
        self._server = None
        self._clients: set = set()

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

    async def _handle_client(self, websocket) -> None:
        """Handle incoming messages from an edge client."""
        self._clients.add(websocket)
        try:
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
                        try:
                            self._on_message(msg)
                        except Exception:
                            logger.exception("on_message callback raised")
                elif msg_type == "set_target":
                    # Target updates normally flow ground -> edge (see
                    # send_target/broadcast_target below). This branch exists
                    # so a client echoing/relaying a set_target message is
                    # tolerated rather than logged as "unknown type".
                    pass
                else:
                    logger.warning("Unknown message type: %r", msg_type)
        except ConnectionClosed:
            logger.info("Edge client disconnected")
        finally:
            self._clients.discard(websocket)

    async def start(self) -> None:
        """Start the WebSocket server."""
        self._server = await websockets.serve(self._handle_client, self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._clients.clear()

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
        await websocket.send(json.dumps(msg))

    async def broadcast_target(self, reid_embedding, face_embedding=None) -> None:
        """Send target embeddings to every currently connected edge client."""
        for ws in list(self._clients):
            try:
                await self.send_target(ws, reid_embedding, face_embedding)
            except ConnectionClosed:
                self._clients.discard(ws)


# ---------------------------------------------------------------------------
# Sync wrappers (background-thread event loop bridge)
# ---------------------------------------------------------------------------


class _AsyncBridge:
    """Runs an asyncio event loop on a background thread.

    Mirrors the bridge pattern in ``amber.drone.mavlink.MavlinkController``:
    a dedicated event loop runs forever on a daemon thread, and sync callers
    submit coroutines onto it via ``run_coroutine_threadsafe`` and block for
    the result. This lets threading-based code (like the Flask/SocketIO
    dashboard app) drive an async API without itself becoming async.
    """

    def __init__(self, name: str):
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return bool(self._loop and self._loop.is_running())

    def start_loop(self) -> None:
        if self.is_running:
            return
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name=f"{self._name}-loop",
        )
        self._loop_thread.start()

    def stop_loop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5.0)
        if self._loop and not self._loop.is_closed():
            self._loop.close()
        self._loop = None
        self._loop_thread = None

    def run(self, coro, timeout: float = _CMD_TIMEOUT):
        """Submit *coro* to the event loop and block for the result."""
        if not self.is_running:
            raise RuntimeError("Event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            logger.error("[%s] Command timed out after %.0fs", self._name, timeout)
            raise


class EdgeTransportSync:
    """Synchronous wrapper around ``EdgeTransport`` for threaded callers."""

    def __init__(self, ws_url: str = "ws://localhost:9000", runner: EdgeRunner | None = None):
        self._transport = EdgeTransport(ws_url=ws_url, runner=runner)
        self._bridge = _AsyncBridge("edge-transport")

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
    ):
        self._transport = GroundTransport(host=host, port=port, on_message=on_message)
        self._bridge = _AsyncBridge("ground-transport")

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

    def stop(self, timeout: float = _CMD_TIMEOUT) -> None:
        if self._bridge.is_running:
            try:
                self._bridge.run(self._transport.stop(), timeout=timeout)
            except Exception:
                logger.warning("[ground-transport] stop error", exc_info=True)
        self._bridge.stop_loop()
