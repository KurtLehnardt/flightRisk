"""End-to-end edge<->ground integration test over a REAL loopback WebSocket.

Unlike ``tests/test_dashboard_edge.py`` (which mocks the transport with a
``MagicMock`` and calls the adapter directly) and ``tests/test_edge_device.py``
(which mocks the transport around the loop), this test wires a **real**
``EdgeTransportSync`` to a **real** ``GroundTransportSync`` over an ephemeral
loopback port and drives the **real** edge-device loop (``run_edge_device``)
and the **real** dashboard adapter (``_edge_process_and_emit``). It proves
detection data actually flows across the wire and that the stream-video
toggle actually reaches the edge and changes its output.

What is REAL vs mocked
----------------------
REAL:
    * ``EdgeTransportSync`` / ``GroundTransportSync`` (actual ``websockets``
      server + client on 127.0.0.1, ephemeral ``port=0``, no token —
      loopback is open per the secure-by-default policy).
    * ``EdgeRunner`` (real ``process_frame`` / ``to_dict``, real
      ``set_stream_video`` control application).
    * ``run_edge_device`` (the real capture/detect/stream loop, including
      ``_drain_control`` pulling ground->edge control messages off the wire).
    * ``GroundStation`` + ``MatchScorer`` (real numpy cosine scoring).
    * ``_edge_process_and_emit`` (the real dashboard adapter that scores a
      ``DetectionMessage`` and emits the browser ``frame`` / ``match_alert``
      contract).

MOCKED (only this):
    * The socket.io emit sink — a thread-safe recorder capturing
      ``emit(event, payload)`` so we can inspect what the browser would get.
    * The detector — a tiny fake returning one fixed detection (NO YOLO/torch).
    * The ReID model — a tiny fake returning a fixed embedding, so a match can
      be forced deterministically without a real model.
"""

import base64
import queue
import threading
import time

import numpy as np
import pytest

from amber.dashboard import app as app_module
from amber.dashboard.state import app_state
from amber.edge import EdgeRunner
from amber.edge_device import run_edge_device
from amber.ground import GroundStation
from amber.transport import EdgeTransportSync, GroundTransportSync
from amber.vision.scorer import MatchScorer


# ---------------------------------------------------------------------------
# Test doubles — ONLY the emit sink, the detector, and the ReID model.
# ---------------------------------------------------------------------------


class FakeSocketIO:
    """Thread-safe recorder of ``emit(event, payload)`` calls.

    The ground transport dispatches the on_message callback in an executor
    thread, so ``_edge_process_and_emit`` emits from a non-test thread — the
    recorder locks around its event list.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.events = []

    def emit(self, event, payload=None, *args, **kwargs):
        with self._lock:
            self.events.append((event, payload))

    def named(self, name):
        with self._lock:
            return [payload for (event, payload) in self.events if event == name]


class FakeDetector:
    """Returns one fixed person detection per frame (no YOLO/torch)."""

    def detect(self, frame):
        # bbox well inside a 640x480 frame so the crop is non-empty.
        return [{"bbox": [10, 10, 100, 200], "confidence": 0.9}]


class FakeReID:
    """Returns a fixed embedding so a match can be forced deterministically."""

    def extract_embedding(self, crop):
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)


class QueueFrameSource:
    """Blocking iterable of BGR frames fed by the test thread.

    ``run_edge_device`` iterates this on a background thread; the test thread
    pushes frames (and finally a sentinel) so we can interleave feeding frames
    with toggling stream-video over the wire.
    """

    def __init__(self):
        self._q: queue.Queue = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self):
        item = self._q.get()
        if item is None:  # sentinel -> stop the loop
            raise StopIteration
        return item

    def push(self, frame):
        self._q.put(frame)

    def close(self):
        self._q.put(None)


def _make_frame():
    return np.zeros((480, 640, 3), dtype=np.uint8)


def _feed_until(source, predicate, timeout=6.0, interval=0.03):
    """Push frames while polling ``predicate`` until true or ``timeout``.

    Returns the final predicate value. Feeding continuously (rather than a
    fixed sleep) absorbs async edge->ground->emit latency without flaking.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        source.push(_make_frame())
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# The end-to-end test
# ---------------------------------------------------------------------------


def test_edge_integration_end_to_end(clean_app_state):
    # --- Real dashboard-side state the adapter reads (stream_video OFF). ---
    app_module._alerted_tracks.clear()
    scorer = MatchScorer(match_threshold=0.45)
    station = GroundStation(scorer=scorer)
    # Identical target vs. the fake ReID embedding -> cosine 1.0 -> a match,
    # exercising the scorer + match_alert path with no real model.
    station.set_target(reid_embedding=[1.0, 0.0, 0.0])
    app_state.ground_station = station
    app_state.scorer = scorer
    app_state.stream_video = False
    app_state.db = None
    app_state.session_id = None
    app_state.match_history = []
    app_state.logger = None

    sio = FakeSocketIO()
    fps_state = {"last": None}

    def on_message(msg):
        # Real dashboard adapter, real fake socketio sink.
        app_module._edge_process_and_emit(sio, msg, fps_state)

    ground = GroundTransportSync(host="127.0.0.1", port=0, on_message=on_message)
    edge = None
    source = QueueFrameSource()
    edge_thread = None
    thread_error = {}

    try:
        ground.start()
        port = ground.port
        assert port > 0

        runner = EdgeRunner(detector=FakeDetector(), reid=FakeReID(), stream_video=False)
        edge = EdgeTransportSync(ws_url=f"ws://127.0.0.1:{port}", runner=runner)
        edge.connect()
        assert edge.connected

        def _run():
            try:
                run_edge_device(source, edge, runner, recv_timeout=0.05)
            except Exception as exc:  # pragma: no cover - surfaced via assert
                thread_error["error"] = exc

        edge_thread = threading.Thread(target=_run, daemon=True)
        edge_thread.start()

        # ---- Phase 1: detections-only mode (stream_video OFF) ----
        # Assert the browser `frame` contract flows over the real wire.
        got_frame = _feed_until(source, lambda: len(sio.named("frame")) >= 1)
        assert got_frame, "no `frame` event received over the real wire"
        assert not thread_error, f"edge loop raised: {thread_error.get('error')!r}"

        frame = sio.named("frame")[0]
        # image is None in detections-only mode (edge sent no thumbnail).
        assert frame["image"] is None
        assert frame["persons"] >= 1
        # boxes non-empty with normalized bbox in [0, 1] (640x480 source dims
        # carried across the wire).
        assert len(frame["boxes"]) >= 1
        box = frame["boxes"][0]
        assert len(box["bbox_norm"]) == 4
        assert all(0.0 <= c <= 1.0 for c in box["bbox_norm"])
        # bbox [10,10,100,200] / [640,480] -> known normalized values.
        assert box["bbox_norm"] == pytest.approx(
            [10 / 640, 10 / 480, 100 / 640, 200 / 480]
        )

        # ---- Step 7: a match fires (deterministic, no real model) ----
        # The fake ReID embedding matches the ground target exactly, so the
        # scorer reports a match and the adapter emits `match_alert`. This
        # proves the full edge-embedding -> wire -> GroundStation scoring ->
        # dashboard alert path (not just frame passthrough).
        assert _feed_until(source, lambda: len(sio.named("match_alert")) >= 1), (
            "no match_alert emitted for a matching detection over the wire"
        )
        alert = sio.named("match_alert")[0]
        assert alert["type"] == "edge"
        assert alert["reid_score"] == pytest.approx(1.0)
        assert alert["snapshot"], "match_alert should carry a base64 crop snapshot"
        assert alert["alert_level"] in (
            "confirmed_match", "possible_match", "weak_signal",
        )
        assert frame["match"] is True
        assert box["matched"] is True

        # ---- Phase 2 / Step 6: toggle stream-video ON over the wire ----
        # ground -> edge control message -> EdgeRunner.set_stream_video(True).
        # Mirrors the dashboard `set_stream_video` handler: flip local state +
        # broadcast to connected edge clients. By now the edge client is
        # registered on the ground server (phase-1 frames already flowed).
        app_state.stream_video = True
        ground.broadcast_stream_video(True)

        def _image_frame_arrived():
            return any(p["image"] for p in sio.named("frame"))

        assert _feed_until(source, _image_frame_arrived), (
            "stream-video toggle did not reach the edge over the wire "
            "(no frame with a non-null image after broadcast_stream_video(True))"
        )
        assert not thread_error, f"edge loop raised: {thread_error.get('error')!r}"

        # The image-bearing frame carries a real base64 JPEG thumbnail that
        # round-tripped edge.process_frame -> to_dict -> wire -> from_dict ->
        # adapter emit.
        img_frame = next(p for p in sio.named("frame") if p["image"])
        assert isinstance(img_frame["image"], str) and img_frame["image"]
        decoded = base64.b64decode(img_frame["image"])
        assert len(decoded) > 0

    finally:
        # Hermetic teardown: stop the edge loop (sentinel -> its finally
        # disconnects the edge transport), join it, then stop the ground
        # server. All disconnect/stop calls are idempotent.
        source.close()
        if edge_thread is not None:
            edge_thread.join(timeout=5.0)
        if edge is not None:
            try:
                edge.disconnect()
            except Exception:
                pass
        try:
            ground.stop()
        except Exception:
            pass

    # The background loop must have exited cleanly.
    if edge_thread is not None:
        assert not edge_thread.is_alive(), "edge_device loop thread did not stop"
    assert not thread_error, f"edge loop raised: {thread_error.get('error')!r}"
