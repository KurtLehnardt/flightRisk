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


def test_sync_wrapper_disconnect_without_connect_does_not_raise():
    et_sync = EdgeTransportSync(ws_url="ws://localhost:1")
    et_sync.disconnect()  # never connected -- must be a safe no-op


def test_sync_wrapper_stop_without_start_does_not_raise():
    gt_sync = GroundTransportSync(host="localhost", port=0)
    gt_sync.stop()  # never started -- must be a safe no-op
