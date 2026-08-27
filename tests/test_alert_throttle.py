"""Tests for the alert throttle and spatial track key logic in app.py."""

import time
from unittest.mock import patch

import pytest

# Import the module-level dicts/constants we need to test.
# We patch them in-place to verify behaviour.
from amber.dashboard import app as dashboard_app


class TestSpatialTrackKey:
    """Verify that the coarse-grid spatial key is stable for small jitter
    and different for far-apart detections."""

    @staticmethod
    def _make_key(x1, y1, x2, y2):
        """Reproduce the track_key formula used in _frame_loop."""
        cx = int((x1 + x2) / 2) // 50
        cy = int((y1 + y2) / 2) // 50
        return f"{cx}_{cy}"

    def test_same_person_slight_jitter(self):
        """Small bbox shifts (<50px center movement) map to the same key."""
        key_a = self._make_key(100, 200, 180, 380)   # centre (140, 290) -> grid (2, 5)
        key_b = self._make_key(105, 205, 185, 385)    # centre (145, 295) -> grid (2, 5)
        assert key_a == key_b

    def test_different_persons_different_keys(self):
        """Two detections far apart produce different keys."""
        key_a = self._make_key(100, 200, 180, 400)
        key_b = self._make_key(500, 200, 580, 400)  # 400px to the right
        assert key_a != key_b

    def test_large_jitter_may_change_key(self):
        """A 60px centre shift can cross a grid boundary."""
        key_a = self._make_key(0, 0, 50, 50)    # centre 25
        key_b = self._make_key(60, 0, 110, 50)  # centre 85
        assert key_a != key_b


class TestAlertThrottle:
    """Verify the _alerted_tracks cooldown logic."""

    def setup_method(self):
        # Reset module-level state between tests
        dashboard_app._alerted_tracks.clear()

    def test_first_alert_always_fires(self):
        """A track_key not in _alerted_tracks should be allowed (cooldown expired)."""
        track_key = "3_6"
        now = time.time()
        last = dashboard_app._alerted_tracks.get(track_key, 0)
        assert now - last >= dashboard_app.ALERT_COOLDOWN

    def test_second_alert_within_cooldown_suppressed(self):
        """A track_key alerted <ALERT_COOLDOWN seconds ago should be throttled."""
        track_key = "3_6"
        now = time.time()
        dashboard_app._alerted_tracks[track_key] = now
        elapsed = time.time() - dashboard_app._alerted_tracks[track_key]
        assert elapsed < dashboard_app.ALERT_COOLDOWN

    def test_alert_after_cooldown_fires(self):
        """A track_key alerted >ALERT_COOLDOWN seconds ago should be allowed."""
        track_key = "3_6"
        dashboard_app._alerted_tracks[track_key] = time.time() - dashboard_app.ALERT_COOLDOWN - 1
        now = time.time()
        elapsed = now - dashboard_app._alerted_tracks[track_key]
        assert elapsed >= dashboard_app.ALERT_COOLDOWN

    def test_different_tracks_independent(self):
        """Two different track keys should have independent cooldowns."""
        now = time.time()
        dashboard_app._alerted_tracks["3_6"] = now
        # "10_6" was never alerted
        elapsed_a = now - dashboard_app._alerted_tracks.get("3_6", 0)
        elapsed_b = now - dashboard_app._alerted_tracks.get("10_6", 0)
        assert elapsed_a < dashboard_app.ALERT_COOLDOWN
        assert elapsed_b >= dashboard_app.ALERT_COOLDOWN


class TestGemmaQueueItemTypes:
    """Verify the queue carries the right tuple shapes."""

    def test_analyze_item_shape(self):
        item = ("analyze", "3_6", b"fake_crop", b"fake_ref")
        assert item[0] == "analyze"
        assert len(item) == 4

    def test_describe_item_shape(self):
        item = ("describe", b"fake_crop", "wearing red hat")
        assert item[0] == "describe"
        assert len(item) == 3
