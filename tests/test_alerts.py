"""Tests for amber.dashboard.alerts -- Gemma worker and alert logic."""

import queue
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from amber.dashboard.alerts import (
    _compute_track_key,
    _gemma_worker,
    _is_within_alert_cooldown,
    _save_match_snapshot,
    CAPTURES_DIR,
)
from amber.dashboard.state import (
    app_state,
    alerted_tracks,
    match_history_lock,
    gemma_queue,
    ALERT_COOLDOWN,
)


class TestComputeTrackKey:
    def test_deterministic(self):
        key = _compute_track_key([100, 200, 180, 380])
        assert key == _compute_track_key([100, 200, 180, 380])

    def test_format(self):
        key = _compute_track_key([100, 200, 180, 380])
        parts = key.split("_")
        assert len(parts) == 2
        assert all(p.isdigit() for p in parts)


class TestIsWithinAlertCooldown:
    def setup_method(self):
        alerted_tracks.clear()

    def test_no_prior_alert(self):
        assert not _is_within_alert_cooldown("new_key", time.time())

    def test_recent_alert(self):
        alerted_tracks["k"] = time.time()
        assert _is_within_alert_cooldown("k", time.time())

    def test_expired_alert(self):
        alerted_tracks["k"] = time.time() - ALERT_COOLDOWN - 1
        assert not _is_within_alert_cooldown("k", time.time())


class TestSaveMatchSnapshot:
    def test_saves_files(self, tmp_path, monkeypatch):
        """Verify snapshot saving writes frame, crop, and metadata files."""
        monkeypatch.setattr("amber.dashboard.alerts.CAPTURES_DIR", tmp_path)
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        crop = np.zeros((50, 50, 3), dtype=np.uint8)

        # Suppress logger
        original_logger = app_state.logger
        app_state.logger = None
        try:
            _save_match_snapshot(frame, crop, 0.85, {"match": True})
        finally:
            app_state.logger = original_logger

        files = list(tmp_path.iterdir())
        names = [f.name for f in files]
        assert any("frame" in n for n in names)
        assert any("crop" in n for n in names)
        assert any("meta" in n for n in names)

    def test_handles_empty_crop(self, tmp_path, monkeypatch):
        """Snapshot saving should not crash on an empty crop array."""
        monkeypatch.setattr("amber.dashboard.alerts.CAPTURES_DIR", tmp_path)
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        crop = np.array([], dtype=np.uint8)

        original_logger = app_state.logger
        app_state.logger = None
        try:
            _save_match_snapshot(frame, crop, 0.5, None)
        finally:
            app_state.logger = original_logger

        # Should at least save the frame
        files = list(tmp_path.iterdir())
        assert any("frame" in f.name for f in files)


class TestGemmaWorkerAnalyze:
    """Test the 'analyze' path of the Gemma worker."""

    def setup_method(self):
        alerted_tracks.clear()
        # Drain any leftover items
        while not gemma_queue.empty():
            try:
                gemma_queue.get_nowait()
            except queue.Empty:
                break

    def test_analyze_emits_reasoning_result(self):
        """Worker should emit reasoning_result for an analyze item."""
        mock_socketio = MagicMock()
        mock_reasoning = MagicMock()
        mock_reasoning.analyze_match.return_value = {
            "match": True,
            "confidence": "high",
            "reasoning": "Same clothing",
        }

        original_reasoning = app_state.reasoning
        original_running = app_state.running
        original_history = app_state.match_history

        app_state.reasoning = mock_reasoning
        app_state.running = True
        app_state.match_history = [{"track_id": "3_6", "gemma_match": None, "match_id": 42}]
        app_state.db = MagicMock()

        try:
            crop = np.zeros((50, 50, 3), dtype=np.uint8)
            ref = np.zeros((50, 50, 3), dtype=np.uint8)
            gemma_queue.put_nowait(("analyze", "3_6", crop, ref))

            # Run one iteration then stop
            def stop_after_one():
                time.sleep(0.2)
                app_state.running = False

            import threading
            stopper = threading.Thread(target=stop_after_one, daemon=True)
            stopper.start()
            _gemma_worker(mock_socketio)
            stopper.join(timeout=2)

            mock_reasoning.analyze_match.assert_called_once()
            mock_socketio.emit.assert_any_call("reasoning_result", {
                "track_id": "3_6",
                "result": {"match": True, "confidence": "high", "reasoning": "Same clothing"},
                "type": "analyze",
            })
        finally:
            app_state.reasoning = original_reasoning
            app_state.running = original_running
            app_state.match_history = original_history

    def test_analyze_high_confidence_emits_alert_upgrade(self):
        """Worker should emit alert_upgrade when confidence is high."""
        mock_socketio = MagicMock()
        mock_reasoning = MagicMock()
        mock_reasoning.analyze_match.return_value = {
            "match": True,
            "confidence": "high",
            "reasoning": "Confirmed",
        }

        original = (app_state.reasoning, app_state.running, app_state.match_history, app_state.db)
        app_state.reasoning = mock_reasoning
        app_state.running = True
        app_state.match_history = [{"track_id": "5_5"}]
        app_state.db = None

        try:
            gemma_queue.put_nowait(("analyze", "5_5", np.zeros((10, 10, 3), dtype=np.uint8), np.zeros((10, 10, 3), dtype=np.uint8)))

            def stop():
                time.sleep(0.2)
                app_state.running = False

            import threading
            t = threading.Thread(target=stop, daemon=True)
            t.start()
            _gemma_worker(mock_socketio)
            t.join(timeout=2)

            # Check that alert_upgrade was emitted
            calls = [c for c in mock_socketio.emit.call_args_list if c[0][0] == "alert_upgrade"]
            assert len(calls) == 1
            assert calls[0][0][1]["new_level"] == "confirmed_match"
        finally:
            app_state.reasoning, app_state.running, app_state.match_history, app_state.db = original


class TestGemmaWorkerDescribe:
    """Test the 'describe' path of the Gemma worker."""

    def setup_method(self):
        alerted_tracks.clear()
        while not gemma_queue.empty():
            try:
                gemma_queue.get_nowait()
            except queue.Empty:
                break

    def test_describe_match_emits_match_alert(self):
        """A confirmed description match should emit match_alert."""
        mock_socketio = MagicMock()
        mock_reasoning = MagicMock()
        mock_reasoning.match_description.return_value = {
            "match": True,
            "confidence": "high",
            "reasoning": "Matches description",
        }
        mock_scorer = MagicMock()
        mock_scorer.score.return_value = {"combined_score": 0.7}
        mock_scorer.alert_level.return_value = "possible_match"

        original = (app_state.reasoning, app_state.running, app_state.match_history,
                     app_state.scorer, app_state.db, app_state.session_id)
        app_state.reasoning = mock_reasoning
        app_state.running = True
        app_state.match_history = []
        app_state.scorer = mock_scorer
        app_state.db = None
        app_state.session_id = None

        try:
            crop = np.zeros((50, 50, 3), dtype=np.uint8)
            gemma_queue.put_nowait(("describe", "7_7", crop, "red shirt, blue jeans"))

            def stop():
                time.sleep(0.3)
                app_state.running = False

            import threading
            t = threading.Thread(target=stop, daemon=True)
            t.start()
            _gemma_worker(mock_socketio)
            t.join(timeout=2)

            mock_reasoning.match_description.assert_called_once()
            # Should emit reasoning_result and match_alert
            event_names = [c[0][0] for c in mock_socketio.emit.call_args_list]
            assert "reasoning_result" in event_names
            assert "match_alert" in event_names
        finally:
            app_state.reasoning, app_state.running, app_state.match_history, \
                app_state.scorer, app_state.db, app_state.session_id = original

    def test_describe_no_match_does_not_emit_alert(self):
        """A negative description match should not emit match_alert."""
        mock_socketio = MagicMock()
        mock_reasoning = MagicMock()
        mock_reasoning.match_description.return_value = {
            "match": False,
            "confidence": "low",
            "reasoning": "Does not match",
        }

        original = (app_state.reasoning, app_state.running, app_state.match_history)
        app_state.reasoning = mock_reasoning
        app_state.running = True
        app_state.match_history = []

        try:
            crop = np.zeros((50, 50, 3), dtype=np.uint8)
            gemma_queue.put_nowait(("describe", "7_7", crop, "red shirt"))

            def stop():
                time.sleep(0.2)
                app_state.running = False

            import threading
            t = threading.Thread(target=stop, daemon=True)
            t.start()
            _gemma_worker(mock_socketio)
            t.join(timeout=2)

            event_names = [c[0][0] for c in mock_socketio.emit.call_args_list]
            assert "match_alert" not in event_names
        finally:
            app_state.reasoning, app_state.running, app_state.match_history = original


class TestGemmaWorkerErrorHandling:
    """Test that the worker handles errors gracefully."""

    def setup_method(self):
        while not gemma_queue.empty():
            try:
                gemma_queue.get_nowait()
            except queue.Empty:
                break

    def test_exception_does_not_crash_worker(self):
        """An exception in reasoning should be caught and the worker continues."""
        mock_socketio = MagicMock()
        mock_reasoning = MagicMock()
        mock_reasoning.analyze_match.side_effect = RuntimeError("Model error")

        original = (app_state.reasoning, app_state.running)
        app_state.reasoning = mock_reasoning
        app_state.running = True

        try:
            crop = np.zeros((10, 10, 3), dtype=np.uint8)
            gemma_queue.put_nowait(("analyze", "1_1", crop, crop))

            def stop():
                time.sleep(0.3)
                app_state.running = False

            import threading
            t = threading.Thread(target=stop, daemon=True)
            t.start()
            # Should not raise
            _gemma_worker(mock_socketio)
            t.join(timeout=2)
        finally:
            app_state.reasoning, app_state.running = original
