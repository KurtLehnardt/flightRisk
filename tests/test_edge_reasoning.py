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
from flightrisk.edge import Detection, DetectionMessage, EdgeRunner
from flightrisk.ground import GroundStation
from flightrisk.persistence import SessionDB
from flightrisk.vision.scorer import MatchScorer


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
    alert_latch.reset()
    yield agent
    app_state.running = False
    _drain()
    gemma_last_call.clear()
    alert_latch.reset()
    # Every get must have been balanced by task_done, or a later test hangs in join().
    assert gemma_queue.unfinished_tasks == 0


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
    kind, track_key, crop, ref, alert_id = gemma_queue.get_nowait()
    gemma_queue.task_done()
    assert kind == "analyze"
    assert track_key == sio.named("match_alert")[0]["track_id"]
    assert isinstance(crop, np.ndarray) and crop.shape == (40, 20, 3)
    assert isinstance(ref, np.ndarray) and ref.shape == (50, 30, 3)
    entry = sio.named("match_alert")[0]
    assert entry["reasoning"] == "Awaiting Claude reasoning..."
    assert entry["gemma_confidence"] == "pending"
    assert alert_id == entry["alert_id"]


def test_latch_prevents_repeat(edge_env):
    sio = FakeSocketIO()
    for i in range(5):
        _fire(sio, x=10 + 60 * i)
    assert len(sio.named("match_alert")) == 1
    assert gemma_queue.qsize() == 1


def test_rate_limit_holds_per_track(edge_env):
    t0 = 1000.0
    job = alerts_module.prepare_edge_reasoning("k", _jpeg(), "a", now=t0)
    assert job is not None
    assert alerts_module.prepare_edge_reasoning("k", _jpeg(), "a", now=t0 + GEMMA_RATE_LIMIT - 0.1) is None
    assert alerts_module.prepare_edge_reasoning("other", _jpeg(), "a", now=t0 + 1) is not None
    assert alerts_module.prepare_edge_reasoning("k", _jpeg(), "a", now=t0 + GEMMA_RATE_LIMIT) is not None


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
    # The alert must not say "Awaiting ..." for a verdict that will never come.
    alert = sio.named("match_alert")[0]
    assert alert["gemma_confidence"] is None and alert["reasoning"] is None
    assert app_state.match_history[-1]["gemma_confidence"] is None
    assert app_state.match_history[-1]["reasoning"] is None
    pending = alert_latch.pending_alert()
    assert pending["alert_id"] == alert["alert_id"]
    assert pending["gemma_confidence"] is None and pending["reasoning"] is None


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


# ---------------------------------------------------------------------------
# Verdict reaches the alert (latch copy, history, DB), keyed by alert_id
# ---------------------------------------------------------------------------

def test_verdict_reaches_latch_copy_for_reminders_and_reconnects(edge_env):
    sio = FakeSocketIO()
    _fire(sio)
    assert alert_latch.pending_alert()["gemma_confidence"] == "pending"
    _run_worker(sio)
    pending = alert_latch.pending_alert()
    assert pending["gemma_confidence"] == "high"
    assert pending["reasoning"] == "same jacket"
    assert pending["gemma_match"] is True
    alert_id = sio.named("match_alert")[0]["alert_id"]
    assert sio.named("reasoning_result")[0]["alert_id"] == alert_id
    assert sio.named("alert_upgrade")[0]["alert_id"] == alert_id


def test_latch_update_ignores_other_or_resolved_alert(edge_env):
    sio = FakeSocketIO()
    _fire(sio)
    alert_id = sio.named("match_alert")[0]["alert_id"]
    assert alert_latch.update("someone-else", {"reasoning": "x"}) is False
    assert alert_latch.pending_alert()["reasoning"] == "Awaiting Claude reasoning..."
    alert_latch.dismiss(alert_id)
    assert alert_latch.update(alert_id, {"reasoning": "x"}) is False


@pytest.mark.parametrize("answer", ["dismiss", "confirm"])
def test_answered_alert_job_is_skipped_and_resolves_entry(edge_env, answer):
    sio = FakeSocketIO()
    _fire(sio)
    alert_id = sio.named("match_alert")[0]["alert_id"]
    getattr(alert_latch, answer)(alert_id)
    _run_worker(sio)
    edge_env.analyze_match.assert_not_called()  # never sent to the LLM
    entry = app_state.match_history[-1]
    assert entry["gemma_confidence"] == "skipped"
    assert "skipped" in entry["reasoning"]
    res = sio.named("reasoning_result")
    assert res and res[0]["alert_id"] == alert_id
    assert res[0]["result"]["confidence"] == "skipped"
    assert sio.named("alert_upgrade") == []


def test_stale_job_cannot_land_on_another_alert_in_same_cell(edge_env):
    sio = FakeSocketIO()
    _fire(sio)
    first = sio.named("match_alert")[0]["alert_id"]
    alert_latch.reset()          # first alert gone; its job is still queued
    gemma_last_call.clear()
    _fire(sio)                   # second alert, same grid cell
    second = sio.named("match_alert")[1]["alert_id"]
    assert first != second and gemma_queue.qsize() == 2
    _run_worker(sio)
    assert edge_env.analyze_match.call_count == 1
    by_id = {e["alert_id"]: e for e in app_state.match_history}
    assert by_id[first]["gemma_confidence"] == "skipped"
    assert by_id[second]["gemma_confidence"] == "high"
    assert by_id[second]["reasoning"] == "same jacket"


def test_reasoning_off_while_queued_resolves_entry(edge_env):
    sio = FakeSocketIO()
    _fire(sio)
    app_state.reasoning = None
    _run_worker(sio)
    assert app_state.match_history[-1]["gemma_confidence"] == "off"
    assert alert_latch.pending_alert()["gemma_confidence"] == "off"


def test_switching_off_during_prepare_does_not_raise(edge_env):
    class Flipping:
        @property
        def label(self):
            app_state.reasoning = None  # operator switches Off right here
            return "Claude"

    app_state.reasoning = Flipping()
    sio = FakeSocketIO()
    _fire(sio)  # must not raise AttributeError on None.label
    assert sio.named("match_alert")[0]["reasoning"] == "Awaiting Claude reasoning..."


def test_db_row_is_backfilled_by_worker(edge_env, tmp_path):
    db = SessionDB(tmp_path / "t.db")
    app_state.db = db
    app_state.session_id = db.create_session(source="edge")
    sio = FakeSocketIO()
    _fire(sio)
    _run_worker(sio)
    rows = db.get_session_matches(app_state.session_id)
    assert len(rows) == 1
    assert rows[0]["gemma_match"] == 1
    assert rows[0]["gemma_confidence"] == "high"
    assert rows[0]["reasoning"] == "same jacket"


def test_enqueueing_survives_edge_wire_roundtrip(edge_env):
    """EdgeRunner.to_dict -> from_dict -> _edge_process_and_emit still enqueues."""
    scorer = MatchScorer(match_threshold=0.45)
    station = GroundStation(scorer=scorer)
    station.set_target(reid_embedding=[1.0, 0.0, 0.0])
    app_state.ground_station = station
    app_state.scorer = scorer
    app_state.stream_video = False
    det = Detection(bbox=(10, 20, 60, 45), confidence=0.95,
                    reid_embedding=[1.0, 0.0, 0.0], crop_jpeg=_jpeg())
    sent = DetectionMessage(timestamp=1.0, frame_id=1, detections=[det], frame_width=100, frame_height=50)
    msg = EdgeRunner.from_dict(EdgeRunner().to_dict(sent))
    sio = FakeSocketIO()
    app_module._edge_process_and_emit(sio, msg, None)
    assert len(sio.named("match_alert")) == 1
    assert gemma_queue.qsize() == 1
    job = gemma_queue.get_nowait()
    gemma_queue.task_done()
    assert job[0] == "analyze" and job[4] == sio.named("match_alert")[0]["alert_id"]


# ---------------------------------------------------------------------------
# crop_jpeg is untrusted: validate before it reaches the image decoder
# ---------------------------------------------------------------------------

def _encode(shape, ext=".jpg"):
    ok, buf = cv2.imencode(ext, np.full((*shape, 3), 128, dtype=np.uint8))
    assert ok
    return buf.tobytes()


@pytest.fixture
def no_decode(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("decoder reached")
    monkeypatch.setattr(alerts_module.cv2, "imdecode", boom)


def test_crop_wrong_magic_rejected_before_decode(edge_env, no_decode):
    assert alerts_module.prepare_edge_reasoning("k", b"\x00\x01\x02" + _jpeg()[3:], "a") is None


def test_crop_non_jpeg_format_rejected(edge_env, no_decode):
    assert alerts_module.prepare_edge_reasoning("k", _encode((40, 20), ".png"), "a") is None
    assert "k" not in gemma_last_call  # no rate-limit stamp for a rejected crop


def test_crop_oversize_bytes_rejected(edge_env, no_decode):
    big = _jpeg() + b"\x00" * alerts_module.MAX_CROP_BYTES
    assert alerts_module.prepare_edge_reasoning("k", big, "a") is None


def test_crop_oversize_dimensions_rejected_from_header(edge_env, no_decode):
    assert alerts_module.prepare_edge_reasoning("k", _encode((100, 2100)), "a") is None
    assert alerts_module.prepare_edge_reasoning("k", _encode((2100, 100)), "a") is None


def test_crop_header_dimensions_parse():
    assert alerts_module._jpeg_dimensions(_encode((40, 20))) == (20, 40)
    assert alerts_module._jpeg_dimensions(b"\xff\xd8\xff\xd9") is None


def test_crop_garbage_after_valid_header_rejected(edge_env):
    assert alerts_module.prepare_edge_reasoning("k", _jpeg()[:200], "a") is None


def test_crop_large_but_allowed_is_downscaled(edge_env):
    prepared = alerts_module.prepare_edge_reasoning("k", _encode((1200, 1600)), "a")
    assert prepared is not None
    crop = prepared[0][2]
    assert max(crop.shape[:2]) == alerts_module.REASONING_CROP_EDGE_PX
    assert crop.shape[1] / crop.shape[0] == pytest.approx(1600 / 1200, rel=0.02)


# ---------------------------------------------------------------------------
# Reference photo: cached by path + mtime, and revert_target keeps the path current
# ---------------------------------------------------------------------------

def test_reference_photo_cached_until_file_changes(edge_env, tmp_path):
    import os

    path = tmp_path / "r.jpg"
    cv2.imwrite(str(path), np.full((20, 20, 3), 50, dtype=np.uint8))
    a = alerts_module.load_reference_photo(str(path))
    assert alerts_module.load_reference_photo(str(path)) is a
    cv2.imwrite(str(path), np.full((30, 30, 3), 90, dtype=np.uint8))
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    b = alerts_module.load_reference_photo(str(path))
    assert b is not a and b.shape == (30, 30, 3)
    assert alerts_module.load_reference_photo(str(tmp_path / "missing.jpg")) is None


def test_revert_target_updates_reasoning_reference_path(clean_app_state, monkeypatch):
    img = np.full((20, 20, 3), 77, dtype=np.uint8)
    canon = MagicMock()
    canon.revert_to.return_value = img
    app_state.canon = canon
    app_state.reid = None
    app_state.face = None
    app_state.signal_registry = None
    app_state.target_photo_path = "/old/path.jpg"
    written = []
    monkeypatch.setattr(app_module.cv2, "imwrite", lambda p, i, *a: written.append(p) or True)
    monkeypatch.setattr(app_module, "emit", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_push_edge_target", lambda i: None)
    app_module.on_revert_target({"version_id": "v1"})
    assert written and app_state.target_photo_path == written[0]
    assert app_state.target_photo_path != "/old/path.jpg"
