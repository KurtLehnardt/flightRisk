"""Unit tests for amber.recorder.SessionRecorder."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from amber.recorder import SessionRecorder


class TestSessionRecorderLifecycle:
    """Tests for start/stop lifecycle."""

    @patch("amber.recorder.cv2")
    def test_start_creates_path_and_returns_string(self, mock_cv2):
        mock_cv2.VideoWriter_fourcc.return_value = 1234
        mock_cv2.VideoWriter.return_value = MagicMock()
        recorder = SessionRecorder()
        path = recorder.start(filename="test_session.mp4")
        assert isinstance(path, str)
        assert "test_session.mp4" in path

    def test_is_recording_false_initially(self):
        recorder = SessionRecorder()
        assert recorder.is_recording is False

    @patch("amber.recorder.cv2")
    def test_is_recording_true_after_start(self, mock_cv2):
        mock_cv2.VideoWriter_fourcc.return_value = 1234
        mock_cv2.VideoWriter.return_value = MagicMock()
        recorder = SessionRecorder()
        recorder.start(filename="test.mp4")
        assert recorder.is_recording is True

    @patch("amber.recorder.cv2")
    def test_is_recording_false_after_stop(self, mock_cv2):
        mock_cv2.VideoWriter_fourcc.return_value = 1234
        mock_writer = MagicMock()
        mock_cv2.VideoWriter.return_value = mock_writer
        recorder = SessionRecorder()
        recorder.start(filename="test.mp4")
        recorder.stop()
        assert recorder.is_recording is False
        mock_writer.release.assert_called_once()

    def test_frame_count_starts_at_zero(self):
        recorder = SessionRecorder()
        assert recorder.frame_count == 0

    def test_stop_before_start_returns_none(self):
        recorder = SessionRecorder()
        result = recorder.stop()
        assert result is None

    @patch("amber.recorder.cv2")
    def test_start_twice_returns_same_path(self, mock_cv2):
        mock_cv2.VideoWriter_fourcc.return_value = 1234
        mock_cv2.VideoWriter.return_value = MagicMock()
        recorder = SessionRecorder()
        path1 = recorder.start(filename="test.mp4")
        path2 = recorder.start(filename="different.mp4")
        assert path1 == path2  # Second start returns same path, doesn't restart

    @patch("amber.recorder.cv2")
    def test_stop_returns_path_string(self, mock_cv2):
        mock_cv2.VideoWriter_fourcc.return_value = 1234
        mock_cv2.VideoWriter.return_value = MagicMock()
        recorder = SessionRecorder()
        recorder.start(filename="test.mp4")
        result = recorder.stop()
        assert isinstance(result, str)
        assert "test.mp4" in result


class TestSessionRecorderFrameWriting:
    """Tests for frame writing."""

    @patch("amber.recorder.cv2")
    def test_write_frame_increments_frame_count(self, mock_cv2):
        mock_cv2.VideoWriter_fourcc.return_value = 1234
        mock_writer = MagicMock()
        mock_cv2.VideoWriter.return_value = mock_writer
        mock_cv2.resize.return_value = np.zeros((720, 960, 3), dtype=np.uint8)

        recorder = SessionRecorder()
        recorder.start(filename="test.mp4")

        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        recorder.write_frame(frame)
        assert recorder.frame_count == 1

        recorder.write_frame(frame)
        assert recorder.frame_count == 2

    @patch("amber.recorder.cv2")
    def test_write_frame_calls_writer(self, mock_cv2):
        mock_cv2.VideoWriter_fourcc.return_value = 1234
        mock_writer = MagicMock()
        mock_cv2.VideoWriter.return_value = mock_writer
        resized = np.zeros((720, 960, 3), dtype=np.uint8)
        mock_cv2.resize.return_value = resized

        recorder = SessionRecorder()
        recorder.start(filename="test.mp4")

        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        recorder.write_frame(frame)

        mock_cv2.resize.assert_called_once()
        mock_writer.write.assert_called_once()

    def test_write_frame_when_not_recording_does_nothing(self):
        recorder = SessionRecorder()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        # Should not raise
        recorder.write_frame(frame)
        assert recorder.frame_count == 0


class TestSessionRecorderCustomFilename:
    """Tests for custom filename parameter."""

    @patch("amber.recorder.cv2")
    def test_custom_filename_used(self, mock_cv2):
        mock_cv2.VideoWriter_fourcc.return_value = 1234
        mock_cv2.VideoWriter.return_value = MagicMock()
        recorder = SessionRecorder()
        path = recorder.start(filename="my_custom_recording.mp4")
        assert "my_custom_recording.mp4" in path

    @patch("amber.recorder.cv2")
    def test_default_filename_uses_timestamp(self, mock_cv2):
        mock_cv2.VideoWriter_fourcc.return_value = 1234
        mock_cv2.VideoWriter.return_value = MagicMock()
        recorder = SessionRecorder()
        path = recorder.start()
        assert "session_" in path
        assert ".mp4" in path
