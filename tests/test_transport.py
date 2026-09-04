"""Tests for the WebSocket transport layer (EdgeTransport / GroundTransport).

These tests exercise real WebSocket connections over localhost using
OS-assigned ephemeral ports (port=0), rather than mocking the ``websockets``
library, so they cover the actual wire protocol between edge and ground.

Async scenarios are driven with plain ``asyncio.run()`` (no pytest-asyncio
dependency), matching the manual event-loop style already used for the
MAVSDK async bridge in ``tests/test_mavlink.py``.
"""

import asyncio
import json
import time

import pytest
import websockets
from websockets.exceptions import ConnectionClosed

from amber.edge import Detection, DetectionMessage
from amber.transport import (
    EdgeTransport,
    EdgeTransportSync,
    GroundTransport,
    GroundTransportSync,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_detection(seed: int = 0) -> Detection:
    return Detection(
        bbox=(10 + seed, 20 + seed, 110 + seed, 220 + seed),
        confidence=0.5 + (seed % 5) * 0.05,
        reid_embedding=[float(seed), 0.1 * seed, -0.2 * seed],
        face_embedding=[0.9 - 0.01 * seed, 0.05 * seed],
        crop_jpeg=bytes([seed % 256, (seed + 1) % 256, (seed + 2) % 256]),
    )


def _make_message(frame_id: int = 1, n_detections: int = 2) -> DetectionMessage:
    return DetectionMessage(
        timestamp=1_700_000_000.5 + frame_id,
        frame_id=frame_id,
        thumbnail_jpeg=bytes([1, 2, 3, 4, 5]),
        detections=[_make_detection(i) for i in range(n_detections)],
    )


def _assert_messages_equal(a: DetectionMessage, b: DetectionMessage) -> None:
    assert a.timestamp == b.timestamp
    assert a.frame_id == b.frame_id
    assert a.thumbnail_jpeg == b.thumbnail_jpeg
    assert len(a.detections) == len(b.detections)
    for da, db in zip(a.detections, b.detections):
        assert da.bbox == db.bbox
        assert da.confidence == pytest.approx(db.confidence)
        assert da.reid_embedding == db.reid_embedding
        assert da.face_embedding == db.face_embedding
        assert da.crop_jpeg == db.crop_jpeg


async def _start_ground(on_message=None, host: str = "localhost") -> GroundTransport:
    """Start a GroundTransport on an OS-assigned ephemeral port."""
    gt = GroundTransport(host=host, port=0, on_message=on_message)
    await gt.start()
    return gt


async def _connect_edge(gt: GroundTransport) -> EdgeTransport:
    et = EdgeTransport(ws_url=f"ws://localhost:{gt.port}")
    await et.connect()
    return et


async def _wait_until(predicate, attempts: int = 100, interval: float = 0.02) -> bool:
    """Poll *predicate* on the running loop until true or attempts exhausted."""
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# ===========================================================================
# Connect + exchange
# ===========================================================================

def test_connect_and_exchange_detection_message():
    received = []

    async def scenario():
        gt = await _start_ground(on_message=received.append)
        et = await _connect_edge(gt)
        try:
            msg = _make_message(frame_id=1)
            await et.send_detections(msg)
            assert await _wait_until(lambda: len(received) == 1)
            _assert_messages_equal(received[0], msg)
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_multiple_messages_in_sequence_preserve_order():
    received = []

    async def scenario():
        gt = await _start_ground(on_message=received.append)
        et = await _connect_edge(gt)
        try:
            sent = [_make_message(frame_id=i, n_detections=i % 3) for i in range(5)]
            for msg in sent:
                await et.send_detections(msg)
            assert await _wait_until(lambda: len(received) == len(sent))
            assert [m.frame_id for m in received] == [m.frame_id for m in sent]
            for got, want in zip(received, sent):
                _assert_messages_equal(got, want)
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


# ===========================================================================
# Round-trip field preservation
# ===========================================================================

def test_roundtrip_preserves_fields_including_none_embeddings():
    received = []

    async def scenario():
        gt = await _start_ground(on_message=received.append)
        et = await _connect_edge(gt)
        try:
            msg = DetectionMessage(
                timestamp=42.0,
                frame_id=7,
                thumbnail_jpeg=None,
                detections=[
                    Detection(
                        bbox=(0, 0, 1, 1),
                        confidence=0.1,
                        reid_embedding=None,
                        face_embedding=None,
                        crop_jpeg=None,
                    ),
                ],
            )
            await et.send_detections(msg)
            assert await _wait_until(lambda: len(received) == 1)
            _assert_messages_equal(received[0], msg)
            assert received[0].thumbnail_jpeg is None
            assert received[0].detections[0].reid_embedding is None
            assert received[0].detections[0].face_embedding is None
            assert received[0].detections[0].crop_jpeg is None
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_roundtrip_with_no_detections():
    received = []

    async def scenario():
        gt = await _start_ground(on_message=received.append)
        et = await _connect_edge(gt)
        try:
            msg = _make_message(frame_id=3, n_detections=0)
            await et.send_detections(msg)
            assert await _wait_until(lambda: len(received) == 1)
            _assert_messages_equal(received[0], msg)
            assert received[0].detections == []
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


# ===========================================================================
# Callback behavior
# ===========================================================================

def test_callback_fires_exactly_once_per_message():
    calls = []

    async def scenario():
        gt = await _start_ground(on_message=lambda m: calls.append(m.frame_id))
        et = await _connect_edge(gt)
        try:
            await et.send_detections(_make_message(frame_id=11))
            assert await _wait_until(lambda: len(calls) == 1)
            await asyncio.sleep(0.1)  # make sure nothing double-fires
            assert calls == [11]
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_no_callback_configured_does_not_raise():
    async def scenario():
        gt = await _start_ground(on_message=None)
        et = await _connect_edge(gt)
        try:
            await et.send_detections(_make_message())
            await asyncio.sleep(0.1)
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_callback_exception_does_not_crash_server():
    received = []

    def bad_callback(msg):
        received.append(msg.frame_id)
        raise ValueError("boom")

    async def scenario():
        gt = await _start_ground(on_message=bad_callback)
        et = await _connect_edge(gt)
        try:
            await et.send_detections(_make_message(frame_id=1))
            assert await _wait_until(lambda: len(received) == 1)
            # Server must still be alive for a second message despite the
            # first callback raising.
            await et.send_detections(_make_message(frame_id=2))
            assert await _wait_until(lambda: len(received) == 2)
            assert received == [1, 2]
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


# ===========================================================================
# Disconnection handling
# ===========================================================================

def test_send_detections_after_explicit_disconnect_does_not_raise():
    async def scenario():
        gt = await _start_ground()
        et = await _connect_edge(gt)
        await et.disconnect()
        assert et.connected is False
        # No-op, must not raise.
        await et.send_detections(_make_message())
        await gt.stop()

    asyncio.run(scenario())


def test_server_removes_client_when_client_disconnects():
    async def scenario():
        gt = await _start_ground()
        et = await _connect_edge(gt)
        assert len(gt.clients) == 1
        await et.disconnect()
        assert await _wait_until(lambda: len(gt.clients) == 0)
        await gt.stop()

    asyncio.run(scenario())


def test_send_detections_when_server_closes_connection_does_not_raise():
    async def scenario():
        gt = await _start_ground()
        et = await _connect_edge(gt)
        await gt.stop()
        # The close handshake is asynchronous; poll send_detections (a no-op
        # once disconnected) until the client side observes the closure.
        for _ in range(50):
            await et.send_detections(_make_message())
            if not et.connected:
                break
            await asyncio.sleep(0.02)
        assert et.connected is False

    asyncio.run(scenario())


def test_recv_after_disconnect_returns_none():
    async def scenario():
        gt = await _start_ground()
        et = await _connect_edge(gt)
        await et.disconnect()
        assert await et.recv(timeout=0.5) is None
        await gt.stop()

    asyncio.run(scenario())


# ===========================================================================
# Malformed message handling
# ===========================================================================

def test_handles_malformed_messages_without_crashing():
    received = []

    async def scenario():
        gt = await _start_ground(on_message=received.append)
        raw_ws = await websockets.connect(f"ws://localhost:{gt.port}")
        try:
            await raw_ws.send("not json {{{")
            await raw_ws.send(json.dumps([1, 2, 3]))  # valid JSON, not an object
            await raw_ws.send(json.dumps("just a string"))
            await raw_ws.send(json.dumps({"type": "bogus"}))
            await raw_ws.send(json.dumps({"type": "detections"}))  # missing required fields
            await raw_ws.send(json.dumps({"type": "detections", "frame_id": 1}))  # missing timestamp
            await asyncio.sleep(0.3)
            assert received == []

            # Server must still be alive and able to process a real message
            # sent right after the garbage.
            valid = _make_message(frame_id=99)
            et = EdgeTransport(ws_url=f"ws://localhost:{gt.port}")
            await et.connect()
            try:
                await et.send_detections(valid)
                assert await _wait_until(lambda: len(received) == 1)
                _assert_messages_equal(received[0], valid)
            finally:
                await et.disconnect()
        finally:
            await raw_ws.close()
            await gt.stop()

    asyncio.run(scenario())


# ===========================================================================
# Target update flow (ground -> edge)
# ===========================================================================

def test_target_update_ground_to_edge():
    async def scenario():
        gt = await _start_ground()
        et = await _connect_edge(gt)
        try:
            assert await _wait_until(lambda: len(gt.clients) == 1)
            server_ws = next(iter(gt.clients))
            await gt.send_target(server_ws, reid_embedding=[1.0, 2.0, 3.0], face_embedding=[0.5, 0.25])

            data = await et.recv(timeout=2.0)
            assert data is not None
            assert data["type"] == "set_target"
            assert data["reid_embedding"] == [1.0, 2.0, 3.0]
            assert data["face_embedding"] == [0.5, 0.25]
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_target_update_with_no_face_embedding():
    async def scenario():
        gt = await _start_ground()
        et = await _connect_edge(gt)
        try:
            assert await _wait_until(lambda: len(gt.clients) == 1)
            server_ws = next(iter(gt.clients))
            await gt.send_target(server_ws, reid_embedding=[4.0])

            data = await et.recv(timeout=2.0)
            assert data == {"type": "set_target", "reid_embedding": [4.0], "face_embedding": None}
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_broadcast_target_reaches_all_connected_clients():
    async def scenario():
        gt = await _start_ground()
        et1 = await _connect_edge(gt)
        et2 = await _connect_edge(gt)
        try:
            assert await _wait_until(lambda: len(gt.clients) == 2)

            await gt.broadcast_target(reid_embedding=[9.0], face_embedding=None)

            data1 = await et1.recv(timeout=2.0)
            data2 = await et2.recv(timeout=2.0)
            for data in (data1, data2):
                assert data is not None
                assert data["type"] == "set_target"
                assert data["reid_embedding"] == [9.0]
                assert data["face_embedding"] is None
        finally:
            await et1.disconnect()
            await et2.disconnect()
            await gt.stop()

    asyncio.run(scenario())


# ===========================================================================
# Stream-video toggle flow (ground -> edge)
# ===========================================================================

def test_stream_video_toggle_ground_to_edge():
    async def scenario():
        gt = await _start_ground()
        et = await _connect_edge(gt)
        try:
            assert await _wait_until(lambda: len(gt.clients) == 1)
            server_ws = next(iter(gt.clients))
            await gt.send_stream_video(server_ws, True)

            data = await et.recv(timeout=2.0)
            # Payload shape must be exactly this.
            assert data == {"type": "stream_video", "enabled": True}
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_stream_video_toggle_off_payload_shape():
    async def scenario():
        gt = await _start_ground()
        et = await _connect_edge(gt)
        try:
            assert await _wait_until(lambda: len(gt.clients) == 1)
            server_ws = next(iter(gt.clients))
            await gt.send_stream_video(server_ws, False)

            data = await et.recv(timeout=2.0)
            assert data == {"type": "stream_video", "enabled": False}
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_broadcast_stream_video_reaches_all_connected_clients():
    async def scenario():
        gt = await _start_ground()
        et1 = await _connect_edge(gt)
        et2 = await _connect_edge(gt)
        try:
            assert await _wait_until(lambda: len(gt.clients) == 2)

            await gt.broadcast_stream_video(True)

            data1 = await et1.recv(timeout=2.0)
            data2 = await et2.recv(timeout=2.0)
            for data in (data1, data2):
                assert data == {"type": "stream_video", "enabled": True}
        finally:
            await et1.disconnect()
            await et2.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_broadcast_stream_video_skips_unauthenticated_clients_when_token_configured():
    async def scenario():
        gt = GroundTransport(host="localhost", port=0, token="s3cr3t")
        await gt.start()
        try:
            et = EdgeTransport(ws_url=f"ws://localhost:{gt.port}", token="s3cr3t")
            await et.connect()
            # A raw client that connects but never completes the auth
            # handshake must not receive broadcast stream-video toggles.
            raw_ws = await websockets.connect(f"ws://localhost:{gt.port}")
            try:
                assert await _wait_until(lambda: len(gt.clients) == 2)
                await gt.broadcast_stream_video(True)

                data = await et.recv(timeout=1.0)
                assert data == {"type": "stream_video", "enabled": True}

                # The unauthenticated client receives nothing.
                with pytest.raises((asyncio.TimeoutError, TimeoutError)):
                    await asyncio.wait_for(raw_ws.recv(), timeout=0.5)
            finally:
                await et.disconnect()
                await raw_ws.close()
        finally:
            await gt.stop()

    asyncio.run(scenario())


# ===========================================================================
# Repeated connect()/start() must not leak connections/servers
# ===========================================================================

def test_connect_called_twice_does_not_leak_connection():
    async def scenario():
        gt = await _start_ground()
        et = EdgeTransport(ws_url=f"ws://localhost:{gt.port}")
        try:
            await et.connect()
            assert await _wait_until(lambda: len(gt.clients) == 1)
            first_ws = et._ws

            # A second connect() while already connected must be a no-op:
            # no new socket opened, no second client registered on the
            # server.
            await et.connect()
            assert et._ws is first_ws
            await asyncio.sleep(0.1)
            assert len(gt.clients) == 1
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_start_called_twice_does_not_leak_server():
    async def scenario():
        gt = GroundTransport(host="localhost", port=0)
        try:
            await gt.start()
            first_server = gt._server
            first_port = gt.port

            # A second start() while already started must be a no-op: the
            # original server (and its bound port) stays in place rather
            # than being replaced/orphaned.
            await gt.start()
            assert gt._server is first_server
            assert gt.port == first_port
        finally:
            await gt.stop()

    asyncio.run(scenario())


# ===========================================================================
# send_target against a closed/gone websocket
# ===========================================================================

def test_send_target_against_closed_websocket_does_not_raise():
    async def scenario():
        gt = await _start_ground()
        et = await _connect_edge(gt)
        try:
            assert await _wait_until(lambda: len(gt.clients) == 1)
            server_ws = next(iter(gt.clients))

            await et.disconnect()
            assert await _wait_until(lambda: len(gt.clients) == 0)

            # server_ws now refers to a closed connection. Sending on it
            # must not raise -- it should be handled the same way a
            # mid-send disconnect is.
            await gt.send_target(server_ws, reid_embedding=[1.0])
        finally:
            await gt.stop()

    asyncio.run(scenario())


def test_broadcast_target_survives_one_dead_client_among_several():
    async def scenario():
        gt = await _start_ground()
        et1 = await _connect_edge(gt)
        et2 = await _connect_edge(gt)
        try:
            assert await _wait_until(lambda: len(gt.clients) == 2)
            dead_ws = next(iter(gt.clients))
            await dead_ws.close()

            # One client's socket is now closed underneath the server, but
            # broadcasting must still reach the surviving client rather
            # than raising/aborting partway through.
            await gt.broadcast_target(reid_embedding=[7.0])

            data1 = await et1.recv(timeout=2.0)
            data2 = await et2.recv(timeout=2.0)
            assert [d for d in (data1, data2) if d is not None]
        finally:
            await et1.disconnect()
            await et2.disconnect()
            await gt.stop()

    asyncio.run(scenario())


# ===========================================================================
# Auth token handshake
# ===========================================================================

def test_auth_handshake_success_allows_detections_through():
    received = []

    async def scenario():
        gt = GroundTransport(host="localhost", port=0, token="s3cr3t", on_message=received.append)
        await gt.start()
        try:
            et = EdgeTransport(ws_url=f"ws://localhost:{gt.port}", token="s3cr3t")
            await et.connect()
            try:
                msg = _make_message(frame_id=42)
                await et.send_detections(msg)
                assert await _wait_until(lambda: len(received) == 1)
                _assert_messages_equal(received[0], msg)
            finally:
                await et.disconnect()
        finally:
            await gt.stop()

    asyncio.run(scenario())


def test_auth_handshake_wrong_token_is_rejected_and_connection_closed():
    async def scenario():
        gt = GroundTransport(host="localhost", port=0, token="s3cr3t")
        await gt.start()
        try:
            et = EdgeTransport(ws_url=f"ws://localhost:{gt.port}", token="wrong-token")
            await et.connect()
            # The server closes the connection right after rejecting the
            # handshake; the client should observe that as a closed
            # connection rather than being able to exchange messages.
            data = await et.recv(timeout=1.0)
            assert data is None
            assert et.connected is False
        finally:
            await gt.stop()

    asyncio.run(scenario())


def test_auth_handshake_missing_token_configured_server_rejects_plain_client():
    async def scenario():
        gt = GroundTransport(host="localhost", port=0, token="s3cr3t")
        await gt.start()
        try:
            raw_ws = await websockets.connect(f"ws://localhost:{gt.port}")
            try:
                # A client that never sends the auth handshake (e.g. an
                # attacker connecting directly) and instead sends a normal
                # message must be rejected, not treated as authenticated.
                await raw_ws.send(json.dumps({"type": "detections", "frame_id": 1}))
                with pytest.raises(ConnectionClosed):
                    await raw_ws.recv()
            finally:
                await raw_ws.close()
        finally:
            await gt.stop()

    asyncio.run(scenario())


def test_broadcast_target_skips_unauthenticated_clients_when_token_configured():
    async def scenario():
        gt = GroundTransport(host="localhost", port=0, token="s3cr3t")
        await gt.start()
        try:
            et = EdgeTransport(ws_url=f"ws://localhost:{gt.port}", token="s3cr3t")
            await et.connect()
            # A raw client that connects but never completes the auth
            # handshake must not receive broadcast target data.
            raw_ws = await websockets.connect(f"ws://localhost:{gt.port}")
            try:
                assert await _wait_until(lambda: len(gt.clients) == 2)
                await gt.broadcast_target(reid_embedding=[1.0])

                data = await et.recv(timeout=1.0)
                assert data is not None
                assert data["reid_embedding"] == [1.0]
            finally:
                await et.disconnect()
                await raw_ws.close()
        finally:
            await gt.stop()

    asyncio.run(scenario())


def test_non_loopback_bind_requires_token():
    """Secure-by-default: exposing the socket on a non-loopback host without
    a token must fail fast, but succeeds once a token is configured."""

    async def scenario():
        # 0.0.0.0 + no token -> refuse with a clear, actionable error.
        gt_open = GroundTransport(host="0.0.0.0", port=0)
        with pytest.raises((ValueError, RuntimeError)) as exc_info:
            await gt_open.start()
        assert "FLIGHTRISK_EDGE_TOKEN" in str(exc_info.value)

        # 0.0.0.0 + a token -> starts fine.
        gt_auth = GroundTransport(host="0.0.0.0", port=0, token="s3cr3t")
        try:
            await gt_auth.start()
            assert gt_auth._server is not None
        finally:
            await gt_auth.stop()

    asyncio.run(scenario())


def test_no_token_configured_preserves_backward_compatible_behavior():
    """When no token is configured, clients are treated as trusted
    immediately (no handshake required), matching pre-auth behavior."""
    received = []

    async def scenario():
        gt = await _start_ground(on_message=received.append)
        et = await _connect_edge(gt)
        try:
            await et.send_detections(_make_message(frame_id=5))
            assert await _wait_until(lambda: len(received) == 1)

            await gt.broadcast_target(reid_embedding=[3.0])
            data = await et.recv(timeout=1.0)
            assert data is not None
            assert data["reid_embedding"] == [3.0]
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


# ===========================================================================
# Sync wrappers
# ===========================================================================

def test_sync_wrappers_connect_and_exchange_message():
    received = []
    gt_sync = GroundTransportSync(host="localhost", port=0, on_message=received.append)
    gt_sync.start()
    try:
        et_sync = EdgeTransportSync(ws_url=f"ws://localhost:{gt_sync.port}")
        et_sync.connect()
        try:
            msg = _make_message(frame_id=3)
            et_sync.send_detections(msg)

            deadline = time.time() + 2.0
            while not received and time.time() < deadline:
                time.sleep(0.02)

            assert len(received) == 1
            _assert_messages_equal(received[0], msg)
        finally:
            et_sync.disconnect()
    finally:
        gt_sync.stop()


def test_sync_wrapper_target_update_flow():
    gt_sync = GroundTransportSync(host="localhost", port=0)
    gt_sync.start()
    try:
        et_sync = EdgeTransportSync(ws_url=f"ws://localhost:{gt_sync.port}")
        et_sync.connect()
        try:
            gt_sync.broadcast_target(reid_embedding=[1.0, 1.0], face_embedding=[2.0])
            data = et_sync.recv(recv_timeout=2.0)
            assert data is not None
            assert data["type"] == "set_target"
            assert data["reid_embedding"] == [1.0, 1.0]
            assert data["face_embedding"] == [2.0]
        finally:
            et_sync.disconnect()
    finally:
        gt_sync.stop()


def test_sync_wrapper_stream_video_broadcast_flow():
    gt_sync = GroundTransportSync(host="localhost", port=0)
    gt_sync.start()
    try:
        et_sync = EdgeTransportSync(ws_url=f"ws://localhost:{gt_sync.port}")
        et_sync.connect()
        try:
            gt_sync.broadcast_stream_video(True)
            data = et_sync.recv(recv_timeout=2.0)
            assert data == {"type": "stream_video", "enabled": True}
        finally:
            et_sync.disconnect()
    finally:
        gt_sync.stop()


def test_sync_wrapper_disconnect_without_connect_does_not_raise():
    et_sync = EdgeTransportSync(ws_url="ws://localhost:1")
    et_sync.disconnect()  # never connected -- must be a safe no-op


def test_sync_wrapper_stop_without_start_does_not_raise():
    gt_sync = GroundTransportSync(host="localhost", port=0)
    gt_sync.stop()  # never started -- must be a safe no-op


# ===========================================================================
# Reconnection with exponential backoff
# ===========================================================================
#
# EdgeTransport previously marked itself disconnected on ConnectionClosed
# and never tried to reconnect, so an edge device that dropped its
# connection to the ground station would stop sending detections forever.
# These tests exercise the exponential-backoff reconnect path added to fix
# that.

def test_reconnect_reestablishes_connection_after_unexpected_drop():
    """_reconnect() succeeds once the ground station is reachable again."""
    received = []

    async def scenario():
        gt = await _start_ground(on_message=received.append)
        et = await _connect_edge(gt)
        try:
            assert et.connected is True

            # Simulate an unexpected drop (e.g. a network blip) rather than
            # an explicit disconnect() -- the socket dies underneath us but
            # _should_reconnect stays True.
            await et._ws.close()
            et._connected = False
            et._base_delay = 0.01
            et._max_delay = 0.01

            reconnected = await et._reconnect()
            assert reconnected is True
            assert et.connected is True

            # The new connection should work end-to-end.
            msg = _make_message(frame_id=21)
            await et.send_detections(msg)
            assert await _wait_until(lambda: len(received) == 1)
            _assert_messages_equal(received[0], msg)
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_reconnect_gives_up_after_max_retries_exhausted():
    """_reconnect() stops trying once max_retries attempts have failed."""

    async def scenario():
        # Port 1 is not a WebSocket server -- every connection attempt
        # fails immediately (connection refused), so this exercises
        # exhaustion quickly and deterministically.
        et = EdgeTransport(ws_url="ws://localhost:1", max_retries=3, base_delay=0.01, max_delay=0.01)
        et._should_reconnect = True

        reconnected = await et._reconnect()

        assert reconnected is False
        assert et.connected is False
        assert et._retry_count == 3

    asyncio.run(scenario())


def test_reconnect_retries_forever_when_max_retries_is_none():
    """max_retries=None (the default) means _reconnect() keeps trying."""

    async def scenario():
        et = EdgeTransport(ws_url="ws://localhost:1", base_delay=0.01, max_delay=0.01)
        assert et._max_retries is None
        et._should_reconnect = True

        # Bound the loop from the test side (rather than via max_retries)
        # by cancelling the reconnect coroutine after it has clearly made
        # multiple attempts against the always-refusing port.
        task = asyncio.ensure_future(et._reconnect())
        for _ in range(50):
            if et._retry_count >= 5:
                break
            await asyncio.sleep(0.01)
        assert et._retry_count >= 5

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_reconnect_stops_immediately_when_reconnection_no_longer_wanted():
    """Flipping _should_reconnect off (as disconnect() does) stops retrying."""

    async def scenario():
        et = EdgeTransport(ws_url="ws://localhost:1", base_delay=0.01, max_delay=0.01)
        et._should_reconnect = False  # never had -- or no longer wants -- a connection

        reconnected = await et._reconnect()

        assert reconnected is False
        assert et._retry_count == 0  # loop body never ran

    asyncio.run(scenario())


def test_reconnect_backoff_delays_grow_exponentially_and_cap_at_max_delay(monkeypatch):
    """Each retry should sleep roughly double the last, capped at max_delay."""
    recorded_delays = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        recorded_delays.append(delay)
        await real_sleep(0)  # yield control without actually waiting

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def scenario():
        et = EdgeTransport(
            ws_url="ws://localhost:1",
            max_retries=5,
            base_delay=1.0,
            max_delay=4.0,
        )
        et._should_reconnect = True
        reconnected = await et._reconnect()
        assert reconnected is False

    asyncio.run(scenario())

    assert recorded_delays == [1.0, 2.0, 4.0, 4.0, 4.0]


def test_send_detections_schedules_background_reconnect_after_drop():
    """send_detections() must not block waiting for reconnection -- it
    should schedule a background attempt and return immediately, with the
    connection recovering shortly afterwards."""
    received = []

    async def scenario():
        gt = await _start_ground(on_message=received.append)
        et = await _connect_edge(gt)
        et._base_delay = 0.01
        et._max_delay = 0.01
        try:
            await et._ws.close()

            start = time.monotonic()
            await et.send_detections(_make_message(frame_id=5))
            elapsed = time.monotonic() - start

            # This call only observes the closed socket and schedules a
            # background reconnect -- it must return fast, not block for
            # the reconnect to complete.
            assert elapsed < 1.0
            assert et._reconnect_task is not None

            # The background task brings the connection back up shortly.
            assert await _wait_until(lambda: et.connected is True, attempts=200, interval=0.02)

            await et.send_detections(_make_message(frame_id=6))
            assert await _wait_until(lambda: len(received) == 1)
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_send_detections_before_any_connect_does_not_schedule_reconnect():
    """A transport that has never connected shouldn't try to reconnect --
    matches the pre-existing 'no-op if not connected' contract."""

    async def scenario():
        et = EdgeTransport(ws_url="ws://localhost:1")
        await et.send_detections(_make_message())
        assert et._reconnect_task is None

    asyncio.run(scenario())


def test_explicit_disconnect_suppresses_further_reconnection():
    """disconnect() must cancel any in-flight reconnect and prevent new
    ones from being scheduled -- an intentional teardown shouldn't silently
    resurrect the connection."""

    async def scenario():
        gt = await _start_ground()
        et = await _connect_edge(gt)
        et._base_delay = 0.05
        et._max_delay = 0.05

        await et._ws.close()
        et._connected = False
        et._reconnect_task = asyncio.ensure_future(et._reconnect())
        await asyncio.sleep(0)  # let the background task start

        await et.disconnect()

        assert et._should_reconnect is False
        assert et.connected is False

        # Give any lingering activity a moment; it must not resurrect the
        # connection after an explicit disconnect().
        await asyncio.sleep(0.3)
        assert et.connected is False

        await gt.stop()

    asyncio.run(scenario())


def test_sync_wrapper_accepts_reconnect_kwargs():
    """EdgeTransportSync must forward reconnect tuning params to EdgeTransport."""
    et_sync = EdgeTransportSync(
        ws_url="ws://localhost:1",
        max_retries=7,
        base_delay=0.5,
        max_delay=2.0,
    )
    assert et_sync._transport._max_retries == 7
    assert et_sync._transport._base_delay == 0.5
    assert et_sync._transport._max_delay == 2.0
