"""Claude/Gemma match reasoning for edge-sourced alerts (layout B, ``--source edge``).

Edge alerts carry the edge's own crop JPEG. ``_emit_edge_alert`` turns it into
the same ``analyze`` job the frame loop queues, behind the single-alert latch
and the per-track rate limit, with a non-blocking enqueue.
"""

from __future__ import annotations

import queue
import time
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest

from flightrisk.dashboard import app as app_module
from flightrisk.dashboard import alerts as alerts_module
from flightrisk.dashboard.alert_latch import alert_latch
from flightrisk.dashboard.alerts import _gemma_worker
from flightrisk.dashboard.state import GEMMA_RATE_LIMIT, app_state, gemma_last_call, gemma_queue


class FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload=None, *a, **k):
        self.events.append((event, payload))

    def named(self, name):
        return [p for (e, p) in self.events if e == name]


def _drain():
    while True:
        try:
            gemma_queue.get_nowait()
            gemma_queue.task_done()
        except queue.Empty:
            return


def _jpeg():
    ok, buf = cv2.imencode(".jpg", np.full((40, 20, 3), 128, dtype=np.uint8))
    assert ok
    return buf.tobytes()


@pytest.fixture
def edge_env(clean_app_state, tmp_path):
    _drain()
    gemma_last_call.clear()
    ref = tmp_path / "ref.jpg"
    cv2.imwrite(str(ref), np.full((50, 30, 3), 200, dtype=np.uint8))
    agent = MagicMock()
    agent.label = "Claude"
    agent.analyze_match.return_value = {"match": True, "confidence": "high", "reasoning": "same jacket"}
    app_state.reasoning = agent
    app_state.target_photo_path = str(ref)
    app_state.scorer = None
    app_state.db = None
    app_state.match_history = []
    app_state.running = True
    yield agent
    app_state.running = False
    _drain()
    gemma_last_call.clear()


def _fire(sio, x=10, crop=True):
    det = MagicMock(crop_jpeg=_jpeg() if crop else None)
    result = {"bbox": (x, 10, x + 40, 50), "reid_score": 0.9, "face_score": 0.0}
    app_module._maybe_emit_edge_match(sio, det, result, None, time.time())


def _run_worker(sio, seconds=0.4):
    import threading

    def stop():
        time.sleep(seconds)
        app_state.running = False

    threading.Thread(target=stop, daemon=True).start()
    _gemma_worker(sio)


def test_edge_alert_with_crop_enqueues_once_with_pipeline_payload(edge_env):
    sio = FakeSocketIO()
    _fire(sio)
    assert gemma_queue.qsize() == 1
    kind, track_key, crop, ref = gemma_queue.get_nowait()
    assert kind == "analyze"
    assert track_key == sio.named("match_alert")[0]["track_id"]
    assert isinstance(crop, np.ndarray) and crop.shape == (40, 20, 3)
    assert isinstance(ref, np.ndarray) and ref.shape == (50, 30, 3)
    entry = sio.named("match_alert")[0]
    assert entry["reasoning"] == "Awaiting Claude reasoning..."
    assert entry["gemma_confidence"] == "pending"


def test_latch_prevents_repeat(edge_env):
    sio = FakeSocketIO()
    for i in range(5):
        _fire(sio, x=10 + 60 * i)
    assert len(sio.named("match_alert")) == 1
    assert gemma_queue.qsize() == 1


def test_rate_limit_holds_per_track(edge_env):
    t0 = 1000.0
    job = alerts_module.prepare_edge_reasoning("k", _jpeg(), now=t0)
    assert job is not None
    assert alerts_module.prepare_edge_reasoning("k", _jpeg(), now=t0 + GEMMA_RATE_LIMIT - 0.1) is None
    assert alerts_module.prepare_edge_reasoning("other", _jpeg(), now=t0 + 1) is not None
    assert alerts_module.prepare_edge_reasoning("k", _jpeg(), now=t0 + GEMMA_RATE_LIMIT) is not None
    assert GEMMA_RATE_LIMIT == 5


def test_rate_limit_applies_through_emit(edge_env):
    gemma_last_call[app_module._compute_track_key((10, 10, 50, 50))] = time.time()
    sio = FakeSocketIO()
    _fire(sio)
    assert len(sio.named("match_alert")) == 1
    assert gemma_queue.qsize() == 0
    assert sio.named("match_alert")[0]["reasoning"] is None


@pytest.mark.parametrize("mode", ["provider_off", "no_photo", "no_crop"])
def test_no_enqueue_when_reasoning_off_or_unusable(edge_env, mode):
    if mode == "provider_off":
        app_state.reasoning = None  # not opted in / --no-llm / provider off
    elif mode == "no_photo":
        app_state.target_photo_path = None
    sio = FakeSocketIO()
    _fire(sio, crop=(mode != "no_crop"))
    assert len(sio.named("match_alert")) == 1
    assert gemma_queue.qsize() == 0
    assert sio.named("match_alert")[0]["reasoning"] is None


def test_full_queue_drops_without_raising(edge_env):
    for i in range(gemma_queue.maxsize):
        gemma_queue.put_nowait(("describe", f"x{i}", None, "d"))
    sio = FakeSocketIO()
    _fire(sio)  # must not raise or block
    assert len(sio.named("match_alert")) == 1
    assert gemma_queue.qsize() == gemma_queue.maxsize


def test_reasoning_result_attaches_to_edge_alert(edge_env):
    sio = FakeSocketIO()
    _fire(sio)
    track_key = sio.named("match_alert")[0]["track_id"]
    _run_worker(sio)
    edge_env.analyze_match.assert_called_once()
    res = sio.named("reasoning_result")
    assert res and res[0]["track_id"] == track_key and res[0]["type"] == "analyze"
    entry = app_state.match_history[-1]
    assert entry["gemma_match"] is True
    assert entry["reasoning"] == "same jacket"
    assert entry["gemma_confidence"] == "high"
    assert sio.named("alert_upgrade")[0]["track_id"] == track_key
