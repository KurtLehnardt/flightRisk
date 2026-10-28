"""Regression tests for the review of the face-recognition PR (#99).

Model-free: face recognizers are stubs, the transport is a MagicMock.
"""

import logging
import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pytest

from flightrisk.dashboard import pipeline as pipeline_mod
from flightrisk.dashboard.pipeline import _frame_loop, _safe_face_call
from flightrisk.dashboard.state import app_state
from flightrisk.vision import face as face_mod
from flightrisk.vision.face import _force_cpu



# ---------------------------------------------------------------------------
# Item 1: detector provider + frame loop isolation
# ---------------------------------------------------------------------------

def test_force_cpu_sets_cpu_provider_on_detector_session():
    session = MagicMock()
    det = types.SimpleNamespace(session=session)
    _force_cpu(det)
    session.set_providers.assert_called_once_with(["CPUExecutionProvider"])


def test_force_cpu_tolerates_detector_without_session():
    _force_cpu(types.SimpleNamespace())  # must not raise


def test_safe_face_call_returns_default_and_logs_once_per_episode(caplog):
    pipeline_mod._face_error_logged = False

    def boom():
        raise RuntimeError("coreml shape")

    with caplog.at_level(logging.ERROR):
        assert _safe_face_call(boom, (None, 0.0)) == (None, 0.0)
        assert _safe_face_call(boom, (None, 0.0)) == (None, 0.0)
    assert len([r for r in caplog.records if "face_match_failed" in r.getMessage()]) == 1
    # A success re-arms the logging for the next episode.
    assert _safe_face_call(lambda: 5, 0) == 5
    with caplog.at_level(logging.ERROR):
        _safe_face_call(boom, None)
    assert len([r for r in caplog.records if "face_match_failed" in r.getMessage()]) == 2
    pipeline_mod._face_error_logged = False


class _FakeTrack:
    def __init__(self, bbox, track_id):
        self.bbox = bbox
        self.track_id = track_id


def _run_frames(monkeypatch, clean, face, n_frames=2, scorer=None):
    """Run the real frame loop for n frames with one person in view and a drone."""
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    crop = np.zeros((40, 20, 3), dtype=np.uint8)
    det = {"bbox": [10, 10, 30, 50], "crop": crop, "confidence": 0.9}
    detector = MagicMock()
    detect_calls = []

    def detect(f):
        # Bounded run: a loop that swallows an error before emitting would
        # otherwise spin forever instead of failing the assertions below.
        detect_calls.append(1)
        if len(detect_calls) >= n_frames + 3:
            app_state.running = False
        return [dict(det)]

    detector.detect.side_effect = detect
    detector.annotate.return_value = frame
    tracker = MagicMock()
    tracker.update.side_effect = lambda dets: [_FakeTrack([10, 10, 30, 50], 7)]

    state = MagicMock()
    state.battery, state.height, state.temperature = 80, 10, 20
    state.flight_time, state.is_flying, state.is_connected = 5, True, True
    drone = MagicMock()
    drone.get_frame.return_value = frame
    drone.state = state
    fleet = MagicMock()
    fleet.primary, fleet.count = drone, 1

    battery = MagicMock()
    monkeypatch.setattr(pipeline_mod, "_apply_battery_policy", battery)
    pipeline_mod._face_error_logged = False

    app_state.running = True
    app_state.detector, app_state.tracker = detector, tracker
    reid = None
    if scorer is not None:
        reid = MagicMock()
        reid.find_match.return_value = (None, 0.0)
        reid.compare.return_value = 0.0
    app_state.reid, app_state.face, app_state.scorer = reid, face, scorer
    app_state.target_photo = "x"
    app_state.cap, app_state.fleet = None, fleet
    app_state.logger = app_state.metrics = app_state.recorder = None
    app_state.tracer = app_state.otel_metrics = None
    app_state.target_description = app_state.reasoning = None

    frames = []

    def emit(event, data=None, *a, **k):
        if event == "frame":
            frames.append(data)
            if len(frames) >= n_frames:
                app_state.running = False

    sio = MagicMock()
    sio.emit = emit
    _frame_loop(sio)
    return frames, battery


def test_face_model_failure_does_not_skip_frame_emit_or_battery_policy(clean_app_state, monkeypatch, caplog):
    face = MagicMock()
    face.has_target = True
    face.find_match.side_effect = RuntimeError("CoreML static output shape mismatch")
    with caplog.at_level(logging.ERROR):
        frames, battery = _run_frames(monkeypatch, clean_app_state, face, n_frames=2)
    assert len(frames) == 2, "frame must still be emitted when the face model raises"
    assert battery.call_count == 2, "battery policy must still run every frame"
    assert len([r for r in caplog.records if "face_match_failed" in r.getMessage()]) == 1


def test_compare_failure_after_a_match_is_contained(clean_app_state, monkeypatch):
    face = MagicMock()
    face.has_target = True
    face.match_threshold = 0.4
    face.find_match.return_value = (0, 0.9)
    face.compare.side_effect = RuntimeError("boom")
    scorer = MagicMock()
    scorer.score.return_value = {"combined_score": 0.0}
    scorer.alert_level.return_value = "no_match"
    frames, battery = _run_frames(monkeypatch, clean_app_state, face, n_frames=1, scorer=scorer)
    face.compare.assert_called()
    assert len(frames) == 1 and battery.call_count == 1


def test_pipeline_attaches_tracker_id_to_detections_passed_to_face(clean_app_state, monkeypatch):
    seen = []
    face = MagicMock()
    face.has_target = True

    def find_match(dets):
        seen.append([d.get("track_id") for d in dets])
        return None, 0.0

    face.find_match.side_effect = find_match
    _run_frames(monkeypatch, clean_app_state, face, n_frames=1)
    assert seen == [[7]]


# ---------------------------------------------------------------------------
# Item 6: a broken insightface must not break importing the dashboard
# ---------------------------------------------------------------------------

def test_broken_insightface_does_not_break_face_module_import(monkeypatch):
    import importlib

    broken = types.ModuleType("insightface.app")

    def _raise(name):
        if name == "FaceAnalysis":
            raise ValueError("numpy.dtype size changed, may indicate binary incompatibility")
        raise AttributeError(name)

    broken.__getattr__ = _raise  # `from insightface.app import FaceAnalysis` -> ValueError
    monkeypatch.setitem(sys.modules, "insightface.app", broken)
    try:
        reloaded = importlib.reload(face_mod)
        assert reloaded.HAS_INSIGHTFACE is False
        assert reloaded.TargetReport is not None
        with pytest.raises(RuntimeError):
            reloaded.FaceRecognizer()
    finally:
        monkeypatch.undo()
        importlib.reload(face_mod)


def test_dashboard_imports_with_broken_insightface_in_a_fresh_interpreter():
    import subprocess
    code = (
        "import sys, types\n"
        "m = types.ModuleType('insightface.app')\n"
        "def g(n):\n"
        "    if n == 'FaceAnalysis': raise ValueError('numpy.dtype size changed')\n"
        "    raise AttributeError(n)\n"
        "m.__getattr__ = g\n"
        "sys.modules['insightface.app'] = m\n"
        "import flightrisk.dashboard.app as a\n"
        "from flightrisk.vision import face\n"
        "assert face.HAS_INSIGHTFACE is False\n"
        "print('OK')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "OK" in out.stdout
