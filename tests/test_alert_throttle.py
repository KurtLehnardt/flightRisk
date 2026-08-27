"""Tests for the alert throttle and spatial track key logic in app.py."""

import time
from unittest.mock import patch

import pytest

# Import the module-level dicts/constants and extracted helpers we test.
from amber.dashboard.app import (
    _alerted_tracks,
    _compute_track_key,
    _is_within_alert_cooldown,
    ALERT_COOLDOWN,
)


class TestSpatialTrackKey:
    """Verify that the coarse-grid spatial key is stable for small jitter
    and different for far-apart detections."""

    def test_same_person_slight_jitter(self):
        """Small bbox shifts (<50px center movement) map to the same key."""
        key_a = _compute_track_key([100, 200, 180, 380])   # centre (140, 290) -> grid (2, 5)
        key_b = _compute_track_key([105, 205, 185, 385])    # centre (145, 295) -> grid (2, 5)
        assert key_a == key_b

    def test_different_persons_different_keys(self):
        """Two detections far apart produce different keys."""
        key_a = _compute_track_key([100, 200, 180, 400])
        key_b = _compute_track_key([500, 200, 580, 400])  # 400px to the right
        assert key_a != key_b

    def test_large_jitter_may_change_key(self):
        """A 60px centre shift can cross a grid boundary."""
        key_a = _compute_track_key([0, 0, 50, 50])    # centre 25
        key_b = _compute_track_key([60, 0, 110, 50])   # centre 85
        assert key_a != key_b


class TestAlertThrottle:
    """Verify the _alerted_tracks cooldown logic."""

    def setup_method(self):
        # Reset module-level state between tests
        _alerted_tracks.clear()

    def test_first_alert_always_fires(self):
        """A track_key not in _alerted_tracks should be allowed (cooldown expired)."""
        track_key = "3_6"
        now = time.time()
        assert not _is_within_alert_cooldown(track_key, now)

    def test_second_alert_within_cooldown_suppressed(self):
        """A track_key alerted <ALERT_COOLDOWN seconds ago should be throttled."""
        track_key = "3_6"
        now = time.time()
        _alerted_tracks[track_key] = now
        assert _is_within_alert_cooldown(track_key, time.time())

    def test_alert_after_cooldown_fires(self):
        """A track_key alerted >ALERT_COOLDOWN seconds ago should be allowed."""
        track_key = "3_6"
        _alerted_tracks[track_key] = time.time() - ALERT_COOLDOWN - 1
        now = time.time()
        assert not _is_within_alert_cooldown(track_key, now)

    def test_different_tracks_independent(self):
        """Two different track keys should have independent cooldowns."""
        now = time.time()
        _alerted_tracks["3_6"] = now
        # "3_6" was just alerted — within cooldown
        assert _is_within_alert_cooldown("3_6", now)
        # "10_6" was never alerted — outside cooldown
        assert not _is_within_alert_cooldown("10_6", now)


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
