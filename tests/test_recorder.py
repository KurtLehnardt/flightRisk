"""Tests for amber.recorder.SessionRecorder."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from amber.recorder import SessionRecorder


class TestSessionRecorderInit:
    """Tests for initial state."""

    def test_is_recording_false_initially(self):
        rec = SessionRecorder()
        assert rec.is_recording is False

    def test_frame_count_starts_at_zero(self):
        rec = SessionRecorder()
        assert rec.frame_count == 0


class TestSessionRecorderStop:
    """Tests for stop behavior."""

    def test_stop_before_start_returns_none(self):
        rec = SessionRecorder()
        result = rec.stop()
        assert result is None


class TestSessionRecorderStart:
    """Tests for start behavior."""

    @patch("amber.recorder.cv2.VideoWriter")
    def test_start_returns_string_path(self, mock_writer_cls):
        mock_writer_cls.return_value = MagicMock()
        rec = SessionRecorder()
        path = rec.start(filename="test_session.mp4")
        assert isinstance(path, str)
        assert "test_session.mp4" in path

    @patch("amber.recorder.cv2.VideoWriter")
    def test_start_sets_is_recording_true(self, mock_writer_cls):
        mock_writer_cls.return_value = MagicMock()
        rec = SessionRecorder()
        rec.start(filename="test_session.mp4")
        assert rec.is_recording is True

    @patch("amber.recorder.cv2.VideoWriter")
    def test_start_twice_returns_same_path(self, mock_writer_cls):
        mock_writer_cls.return_value = MagicMock()
        rec = SessionRecorder()
        path1 = rec.start(filename="test_session.mp4")
        path2 = rec.start(filename="different.mp4")
        assert path1 == path2

    @patch("amber.recorder.cv2.VideoWriter")
    def test_custom_filename_works(self, mock_writer_cls):
        mock_writer_cls.return_value = MagicMock()
        rec = SessionRecorder()
        path = rec.start(filename="my_custom_file.mp4")
        assert "my_custom_file.mp4" in path


class TestSessionRecorderWriteFrame:
    """Tests for write_frame behavior."""

    def test_write_frame_when_not_recording_does_nothing(self):
        rec = SessionRecorder()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        # Should not raise
        rec.write_frame(frame)
        assert rec.frame_count == 0

    @patch("amber.recorder.cv2.VideoWriter")
    @patch("amber.recorder.cv2.resize")
    def test_write_frame_increments_frame_count(self, mock_resize, mock_writer_cls):
        mock_writer = MagicMock()
        mock_writer_cls.return_value = mock_writer
        mock_resize.return_value = np.zeros((720, 960, 3), dtype=np.uint8)

        rec = SessionRecorder()
        rec.start(filename="test_session.mp4")

        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        rec.write_frame(frame)
        assert rec.frame_count == 1

        rec.write_frame(frame)
        assert rec.frame_count == 2

        # Verify writer.write was called
        assert mock_writer.write.call_count == 2


class TestSessionRecorderStopAfterStart:
    """Tests for stop after recording."""

    @patch("amber.recorder.cv2.VideoWriter")
    def test_stop_returns_path_and_sets_not_recording(self, mock_writer_cls):
        mock_writer = MagicMock()
        mock_writer_cls.return_value = mock_writer

        rec = SessionRecorder()
        rec.start(filename="test_session.mp4")
        assert rec.is_recording is True

        path = rec.stop()
        assert isinstance(path, str)
        assert "test_session.mp4" in path
        assert rec.is_recording is False

        # Verify writer was released
        mock_writer.release.assert_called_once()
