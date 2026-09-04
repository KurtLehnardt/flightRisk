"""Tests for flightrisk.dashboard.pipeline -- frame processing loop and helpers."""

import base64
import queue
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import numpy as np
import pytest

from flightrisk.dashboard.pipeline import _build_track_id_by_bbox, _frame_loop
from flightrisk.dashboard.state import (
    app_state,
    alerted_tracks,
    match_history_lock,
    gemma_queue,
    gemma_last_call,
)


class TestBuildTrackIdByBbox:
    """Verify the bbox -> track_id join helper."""

    def test_empty_detections(self):
        assert _build_track_id_by_bbox([], []) == {}

    def test_matches_current_frame(self):
        class FakeTrack:
            def __init__(self, bbox, track_id):
                self.bbox = bbox
                self.track_id = track_id

        tracked = [FakeTrack([10, 10, 50, 50], 0)]
        detections = [{"bbox": [10, 10, 50, 50]}]
        result = _build_track_id_by_bbox(tracked, detections)
        assert result[(10, 10, 50, 50)] == 0

    def test_stale_track_excluded(self):
        class FakeTrack:
            def __init__(self, bbox, track_id):
                self.bbox = bbox
                self.track_id = track_id

        # Track 0 has a stale bbox not in current detections
        tracked = [
            FakeTrack([10, 10, 50, 50], 0),  # stale
            FakeTrack([200, 200, 300, 300], 1),  # current
        ]
        detections = [{"bbox": [200, 200, 300, 300]}]
        result = _build_track_id_by_bbox(tracked, detections)
        assert (10, 10, 50, 50) not in result
        assert result[(200, 200, 300, 300)] == 1

    def test_multiple_detections(self):
        class FakeTrack:
            def __init__(self, bbox, track_id):
                self.bbox = bbox
                self.track_id = track_id

        tracked = [
            FakeTrack([10, 10, 50, 50], 0),
            FakeTrack([100, 100, 200, 200], 1),
        ]
        detections = [
            {"bbox": [10, 10, 50, 50]},
            {"bbox": [100, 100, 200, 200]},
        ]
        result = _build_track_id_by_bbox(tracked, detections)
        assert len(result) == 2


def _make_mock_frame():
    """Create a small BGR frame for testing."""
    return np.zeros((100, 100, 3), dtype=np.uint8)


class TestFrameLoopNoMatch:
    """Test frame loop with no detections or no target -- should emit frames
    without match alerts."""

    def test_emits_frame_with_no_detections(self):
        """When detector finds no persons, frame should still be emitted."""
        mock_socketio = MagicMock()
        frame = _make_mock_frame()

        # Save originals
        originals = {
            "running": app_state.running,
            "detector": app_state.detector,
            "tracker": app_state.tracker,
            "reid": app_state.reid,
            "face": app_state.face,
            "scorer": app_state.scorer,
            "target_photo": app_state.target_photo,
            "cap": app_state.cap,
            "fleet": app_state.fleet,
            "logger": app_state.logger,
            "metrics": app_state.metrics,
            "recorder": app_state.recorder,
            "tracer": app_state.tracer,
            "otel_metrics": app_state.otel_metrics,
        }

        mock_detector = MagicMock()
        mock_detector.detect.return_value = []
        mock_detector.annotate.return_value = frame

        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (True, frame)

        app_state.running = True
        app_state.detector = mock_detector
        app_state.tracker = None
        app_state.reid = None
        app_state.face = None
        app_state.scorer = None
        app_state.target_photo = None
        app_state.cap = mock_cap
        app_state.fleet = None
        app_state.logger = None
        app_state.metrics = None
        app_state.recorder = None
        app_state.tracer = None
        app_state.otel_metrics = None

        try:
            # Run one frame then stop
            frame_count = [0]
            original_emit = mock_socketio.emit

            def counting_emit(event, *args, **kwargs):
                if event == "frame":
                    frame_count[0] += 1
                    if frame_count[0] >= 1:
                        app_state.running = False

            mock_socketio.emit = counting_emit
            _frame_loop(mock_socketio)

            assert frame_count[0] >= 1
        finally:
            for k, v in originals.items():
                setattr(app_state, k, v)


class TestFrameLoopMatchAlert:
    """Test frame loop emits match_alert when ReID score is high enough."""

    def test_emits_match_alert_on_reid_match(self):
        """A high ReID score with a target photo should emit match_alert."""
        mock_socketio = MagicMock()
        frame = _make_mock_frame()
        crop = np.zeros((50, 50, 3), dtype=np.uint8)

        originals = {
            "running": app_state.running,
            "detector": app_state.detector,
            "tracker": app_state.tracker,
            "reid": app_state.reid,
            "face": app_state.face,
            "scorer": app_state.scorer,
            "target_photo": app_state.target_photo,
            "target_photo_path": app_state.target_photo_path,
            "cap": app_state.cap,
            "fleet": app_state.fleet,
            "logger": app_state.logger,
            "metrics": app_state.metrics,
            "recorder": app_state.recorder,
            "tracer": app_state.tracer,
            "otel_metrics": app_state.otel_metrics,
            "reasoning": app_state.reasoning,
            "db": app_state.db,
            "session_id": app_state.session_id,
            "search_active": app_state.search_active,
            "target_description": app_state.target_description,
        }

        # Clear alerted tracks so cooldown doesn't suppress
        alerted_tracks.clear()

        mock_detector = MagicMock()
        mock_detector.detect.return_value = [
            {"bbox": [10, 10, 50, 50], "confidence": 0.9, "crop": crop}
        ]
        mock_detector.annotate.return_value = frame

        mock_reid = MagicMock()
        mock_reid.find_match.return_value = (0, 0.85)
        mock_reid.compare.return_value = 0.85
        mock_reid.match_threshold = 0.55

        mock_scorer = MagicMock()
        mock_scorer.score.return_value = {"combined_score": 0.85}
        mock_scorer.alert_level.return_value = "possible_match"

        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (True, frame)

        app_state.running = True
        app_state.detector = mock_detector
        app_state.tracker = None
        app_state.reid = mock_reid
        app_state.face = None
        app_state.scorer = mock_scorer
        app_state.target_photo = "base64data"
        app_state.target_photo_path = "/fake/target.jpg"
        app_state.cap = mock_cap
        app_state.fleet = None
        app_state.logger = None
        app_state.metrics = None
        app_state.recorder = None
        app_state.tracer = None
        app_state.otel_metrics = None
        app_state.reasoning = None
        app_state.db = None
        app_state.session_id = None
        app_state.search_active = False
        app_state.target_description = None

        emitted_events = []

        try:
            def tracking_emit(event, *args, **kwargs):
                emitted_events.append(event)
                if event == "frame":
                    app_state.running = False

            mock_socketio.emit = tracking_emit

            with patch("flightrisk.dashboard.pipeline._save_match_snapshot"):
                _frame_loop(mock_socketio)

            assert "match_alert" in emitted_events
        finally:
            for k, v in originals.items():
                setattr(app_state, k, v)


class TestFrameLoopNoMatchBelowThreshold:
    """Test that low ReID scores do not trigger alerts."""

    def test_no_alert_when_score_below_threshold(self):
        mock_socketio = MagicMock()
        frame = _make_mock_frame()
        crop = np.zeros((50, 50, 3), dtype=np.uint8)

        originals = {
            "running": app_state.running,
            "detector": app_state.detector,
            "tracker": app_state.tracker,
            "reid": app_state.reid,
            "face": app_state.face,
            "scorer": app_state.scorer,
            "target_photo": app_state.target_photo,
            "target_photo_path": app_state.target_photo_path,
            "cap": app_state.cap,
            "fleet": app_state.fleet,
            "logger": app_state.logger,
            "metrics": app_state.metrics,
            "recorder": app_state.recorder,
            "tracer": app_state.tracer,
            "otel_metrics": app_state.otel_metrics,
            "reasoning": app_state.reasoning,
            "db": app_state.db,
            "target_description": app_state.target_description,
        }

        mock_detector = MagicMock()
        mock_detector.detect.return_value = [
            {"bbox": [10, 10, 50, 50], "confidence": 0.9, "crop": crop}
        ]
        mock_detector.annotate.return_value = frame

        mock_reid = MagicMock()
        mock_reid.find_match.return_value = (0, 0.3)  # Below threshold
        mock_reid.compare.return_value = 0.3

        mock_scorer = MagicMock()
        mock_scorer.score.return_value = {"combined_score": 0.3}
        mock_scorer.alert_level.return_value = "no_match"

        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (True, frame)

        app_state.running = True
        app_state.detector = mock_detector
        app_state.tracker = None
        app_state.reid = mock_reid
        app_state.face = None
        app_state.scorer = mock_scorer
        app_state.target_photo = "base64data"
        app_state.target_photo_path = "/fake/target.jpg"
        app_state.cap = mock_cap
        app_state.fleet = None
        app_state.logger = None
        app_state.metrics = None
        app_state.recorder = None
        app_state.tracer = None
        app_state.otel_metrics = None
        app_state.reasoning = None
        app_state.db = None
        app_state.target_description = None

        emitted_events = []

        try:
            def tracking_emit(event, *args, **kwargs):
                emitted_events.append(event)
                if event == "frame":
                    app_state.running = False

            mock_socketio.emit = tracking_emit
            _frame_loop(mock_socketio)

            assert "match_alert" not in emitted_events
        finally:
            for k, v in originals.items():
                setattr(app_state, k, v)


class TestFrameLoopDescriptionMatching:
    """Test description-based matching queues to Gemma."""

    def test_queues_description_match_to_gemma(self):
        """When no photo target but description exists, should queue to Gemma."""
        mock_socketio = MagicMock()
        frame = _make_mock_frame()
        crop = np.zeros((50, 50, 3), dtype=np.uint8)

        originals = {
            "running": app_state.running,
            "detector": app_state.detector,
            "tracker": app_state.tracker,
            "reid": app_state.reid,
            "face": app_state.face,
            "scorer": app_state.scorer,
            "target_photo": app_state.target_photo,
            "target_description": app_state.target_description,
            "reasoning": app_state.reasoning,
            "cap": app_state.cap,
            "fleet": app_state.fleet,
            "logger": app_state.logger,
            "metrics": app_state.metrics,
            "recorder": app_state.recorder,
            "tracer": app_state.tracer,
            "otel_metrics": app_state.otel_metrics,
            "db": app_state.db,
        }

        # Drain queue
        while not gemma_queue.empty():
            try:
                gemma_queue.get_nowait()
            except queue.Empty:
                break

        mock_detector = MagicMock()
        mock_detector.detect.return_value = [
            {"bbox": [10, 10, 80, 80], "confidence": 0.9, "crop": crop}
        ]
        mock_detector.annotate.return_value = frame

        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (True, frame)

        mock_reasoning = MagicMock()

        app_state.running = True
        app_state.detector = mock_detector
        app_state.tracker = None
        app_state.reid = None
        app_state.face = None
        app_state.scorer = None
        app_state.target_photo = None  # No photo
        app_state.target_description = "red hat, blue coat"
        app_state.reasoning = mock_reasoning
        app_state.cap = mock_cap
        app_state.fleet = None
        app_state.logger = None
        app_state.metrics = None
        app_state.recorder = None
        app_state.tracer = None
        app_state.otel_metrics = None
        app_state.db = None

        emitted_events = []

        try:
            def tracking_emit(event, *args, **kwargs):
                emitted_events.append(event)
                if event == "frame":
                    app_state.running = False

            mock_socketio.emit = tracking_emit
            _frame_loop(mock_socketio)

            # Should have queued a describe item
            assert not gemma_queue.empty()
            item = gemma_queue.get_nowait()
            assert item[0] == "describe"
            assert item[3] == "red hat, blue coat"
        finally:
            for k, v in originals.items():
                setattr(app_state, k, v)
            # Drain queue
            while not gemma_queue.empty():
                try:
                    gemma_queue.get_nowait()
                except queue.Empty:
                    break


class TestFrameLoopDroneTelemetry:
    """Test frame loop emits telemetry from drone."""

    def test_emits_telemetry_from_fleet(self):
        mock_socketio = MagicMock()
        frame = _make_mock_frame()

        originals = {
            "running": app_state.running,
            "detector": app_state.detector,
            "tracker": app_state.tracker,
            "reid": app_state.reid,
            "face": app_state.face,
            "scorer": app_state.scorer,
            "target_photo": app_state.target_photo,
            "cap": app_state.cap,
            "fleet": app_state.fleet,
            "logger": app_state.logger,
            "metrics": app_state.metrics,
            "recorder": app_state.recorder,
            "tracer": app_state.tracer,
            "otel_metrics": app_state.otel_metrics,
            "target_description": app_state.target_description,
            "reasoning": app_state.reasoning,
        }

        mock_detector = MagicMock()
        mock_detector.detect.return_value = []
        mock_detector.annotate.return_value = frame

        mock_drone_state = MagicMock()
        mock_drone_state.battery = 85
        mock_drone_state.height = 120
        mock_drone_state.temperature = 25
        mock_drone_state.flight_time = 60
        mock_drone_state.is_flying = True
        mock_drone_state.is_connected = True

        mock_drone = MagicMock()
        mock_drone.get_frame.return_value = frame
        mock_drone.state = mock_drone_state

        mock_fleet = MagicMock()
        mock_fleet.primary = mock_drone
        mock_fleet.count = 1

        app_state.running = True
        app_state.detector = mock_detector
        app_state.tracker = None
        app_state.reid = None
        app_state.face = None
        app_state.scorer = None
        app_state.target_photo = None
        app_state.fleet = mock_fleet
        app_state.cap = None
        app_state.logger = None
        app_state.metrics = None
        app_state.recorder = None
        app_state.tracer = None
        app_state.otel_metrics = None
        app_state.target_description = None
        app_state.reasoning = None

        frame_data = []

        try:
            def tracking_emit(event, data=None, *args, **kwargs):
                if event == "frame":
                    frame_data.append(data)
                    app_state.running = False

            mock_socketio.emit = tracking_emit
            _frame_loop(mock_socketio)

            assert len(frame_data) >= 1
            telemetry = frame_data[0]["telemetry"]
            assert telemetry["battery"] == 85
            assert telemetry["is_flying"] is True
        finally:
            for k, v in originals.items():
                setattr(app_state, k, v)
