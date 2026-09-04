"""Tests for flightrisk.dashboard.__main__ CLI argument validation.

Covers the PR #26 review fix: `--source file` without `--video` used to
silently build a pipeline with no video source (cv2.VideoCapture(None)
via a falsy fallback). It must now fail fast with a usage error. Also
covers that a bad FLIGHTRISK_SOURCE env var (which argparse's `choices=` does
NOT validate, since it only checks values passed on the command line, not
defaults) is now rejected explicitly.
"""

import os
import sys
from unittest.mock import patch

import pytest

import flightrisk.dashboard.__main__ as dashboard_main


def _run_main(argv):
    with patch.object(sys, "argv", ["flightrisk-dashboard"] + argv):
        dashboard_main.main()


class TestSourceFileRequiresVideo:
    def test_source_file_without_video_errors(self):
        with patch.object(dashboard_main, "run_dashboard") as mock_run:
            with pytest.raises(SystemExit):
                _run_main(["--source", "file"])
            mock_run.assert_not_called()

    def test_source_file_with_video_succeeds(self):
        with patch.object(dashboard_main, "run_dashboard") as mock_run:
            _run_main(["--source", "file", "--video", "clip.mp4"])
            mock_run.assert_called_once()
            source_config = mock_run.call_args[0][0]
            assert source_config.source == "file"
            assert source_config.video_path == "clip.mp4"

    def test_other_sources_do_not_require_video(self):
        with patch.object(dashboard_main, "run_dashboard") as mock_run:
            _run_main(["--source", "webcam"])
            mock_run.assert_called_once()
            source_config = mock_run.call_args[0][0]
            assert source_config.source == "webcam"
            assert source_config.video_path is None


class TestSourceEnvVarValidation:
    def test_invalid_source_from_env_var_errors(self):
        """argparse's `choices=` never validates a `default=` value, so a
        bad FLIGHTRISK_SOURCE must be checked explicitly after parsing."""
        with patch.object(dashboard_main, "run_dashboard") as mock_run:
            with patch.dict(os.environ, {"FLIGHTRISK_SOURCE": "bogus"}):
                with pytest.raises(SystemExit):
                    _run_main([])
            mock_run.assert_not_called()

    def test_valid_source_from_env_var_is_used(self):
        with patch.object(dashboard_main, "run_dashboard") as mock_run:
            with patch.dict(os.environ, {"FLIGHTRISK_SOURCE": "mavlink"}):
                _run_main([])
            mock_run.assert_called_once()
            source_config = mock_run.call_args[0][0]
            assert source_config.source == "mavlink"

    def test_invalid_source_from_cli_still_rejected_by_argparse(self):
        with patch.object(dashboard_main, "run_dashboard") as mock_run:
            with pytest.raises(SystemExit):
                _run_main(["--source", "bogus"])
            mock_run.assert_not_called()
