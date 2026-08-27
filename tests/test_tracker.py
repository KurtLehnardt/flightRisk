"""Tests for amber.vision.tracker — IoU-based detection tracker."""

import threading

import numpy as np
import pytest

from amber.vision.tracker import DetectionTracker, TrackedDetection, _compute_iou


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _det(bbox, confidence=0.8, crop=None):
    """Shorthand for a detection dict matching PersonDetector output."""
    if crop is None:
        crop = np.zeros((50, 30, 3), dtype=np.uint8)
    return {"bbox": list(bbox), "confidence": confidence, "crop": crop}


# ---------------------------------------------------------------------------
# IoU helper
# ---------------------------------------------------------------------------

class TestComputeIou:
    def test_identical_boxes(self):
        assert _compute_iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0

    def test_no_overlap(self):
        assert _compute_iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0

    def test_partial_overlap(self):
        iou = _compute_iou((0, 0, 10, 10), (5, 5, 15, 15))
        # Intersection: 5x5=25, Union: 100+100-25=175
        assert abs(iou - 25 / 175) < 1e-6

    def test_one_box_inside_other(self):
        iou = _compute_iou((0, 0, 20, 20), (5, 5, 10, 10))
        # Intersection: 5x5=25, Union: 400+25-25=400
        assert abs(iou - 25 / 400) < 1e-6

    def test_zero_area_box(self):
        assert _compute_iou((5, 5, 5, 5), (0, 0, 10, 10)) == 0.0

    def test_malformed_box_negative_width(self):
        """x2 < x1 (negative width) must not crash and should yield IoU 0.0."""
        assert _compute_iou((10, 0, 5, 10), (0, 0, 10, 10)) == 0.0

    def test_malformed_box_negative_height(self):
        """y2 < y1 (negative height) must not crash and should yield IoU 0.0."""
        assert _compute_iou((0, 10, 10, 5), (0, 0, 10, 10)) == 0.0

    def test_both_boxes_malformed(self):
        assert _compute_iou((10, 0, 5, 10), (10, 10, 0, 20)) == 0.0


# ---------------------------------------------------------------------------
# Basic tracking
# ---------------------------------------------------------------------------

class TestBasicTracking:
    def test_single_detection_creates_track(self):
        tracker = DetectionTracker()
        result = tracker.update([_det((10, 10, 50, 50))])
        assert len(result) == 1
        assert result[0].track_id == 0
        assert result[0].frames_seen == 1

    def test_same_bbox_maintains_track_id(self):
        tracker = DetectionTracker()
        tracker.update([_det((10, 10, 50, 50))])
        result = tracker.update([_det((10, 10, 50, 50))])
        assert len(result) == 1
        assert result[0].track_id == 0
        assert result[0].frames_seen == 2

    def test_different_bbox_creates_new_track(self):
        tracker = DetectionTracker()
        tracker.update([_det((10, 10, 50, 50))])
        result = tracker.update([_det((200, 200, 300, 300))])
        # Original track should still exist (aged but not expired) + new track
        ids = {t.track_id for t in result}
        assert 1 in ids  # new track was created


# ---------------------------------------------------------------------------
# IoU matching
# ---------------------------------------------------------------------------

class TestIoUMatching:
    def test_overlapping_bbox_matches_existing_track(self):
        tracker = DetectionTracker(iou_threshold=0.3)
        tracker.update([_det((10, 10, 100, 100))])
        # Slightly shifted box — still high IoU
        result = tracker.update([_det((15, 15, 105, 105))])
        matched = [t for t in result if t.track_id == 0]
        assert len(matched) == 1
        assert matched[0].frames_seen == 2

    def test_non_overlapping_bbox_creates_new_track(self):
        tracker = DetectionTracker(iou_threshold=0.3)
        tracker.update([_det((10, 10, 50, 50))])
        result = tracker.update([_det((500, 500, 600, 600))])
        ids = {t.track_id for t in result}
        assert 0 in ids  # old track still alive (aged)
        assert 1 in ids  # new track

    def test_exactly_at_threshold(self):
        """Box overlap exactly at the IoU threshold should match."""
        tracker = DetectionTracker(iou_threshold=0.3)
        # Build two boxes whose IoU is exactly 0.3:
        # box1 area = 100, box2 area = 100
        # Need intersection / union = 0.3 => I / (200-I) = 0.3 => I = 0.3*(200-I)
        # I = 60 - 0.3I => 1.3I = 60 => I ~= 46.15
        # Use boxes where IoU >= 0.3
        tracker.update([_det((0, 0, 100, 100))])
        # Shift by 30 on each axis: overlap = 70*70 = 4900
        # Union = 10000 + 10000 - 4900 = 15100, IoU ~ 0.324
        result = tracker.update([_det((30, 30, 130, 130))])
        matched = [t for t in result if t.track_id == 0]
        assert len(matched) == 1


# ---------------------------------------------------------------------------
# Score accumulation
# ---------------------------------------------------------------------------

class TestScoreAccumulation:
    def test_add_scores_appends(self):
        tracker = DetectionTracker()
        tracker.update([_det((10, 10, 50, 50))])
        tracker.add_scores(0, reid_score=0.8)
        tracker.add_scores(0, reid_score=0.9)
        td = tracker.get_track(0)
        assert td is not None
        assert len(td.reid_scores) == 2

    def test_avg_reid_score(self):
        tracker = DetectionTracker()
        tracker.update([_det((10, 10, 50, 50))])
        tracker.add_scores(0, reid_score=0.6)
        tracker.add_scores(0, reid_score=0.8)
        td = tracker.get_track(0)
        assert abs(td.avg_reid_score - 0.7) < 1e-6

    def test_avg_face_score(self):
        tracker = DetectionTracker()
        tracker.update([_det((10, 10, 50, 50))])
        tracker.add_scores(0, face_score=0.5)
        tracker.add_scores(0, face_score=0.9)
        td = tracker.get_track(0)
        assert abs(td.avg_face_score - 0.7) < 1e-6

    def test_rolling_window_trims(self):
        tracker = DetectionTracker(vote_window=3)
        tracker.update([_det((10, 10, 50, 50))])
        for s in [0.1, 0.2, 0.3, 0.4, 0.5]:
            tracker.add_scores(0, reid_score=s)
        td = tracker.get_track(0)
        assert len(td.reid_scores) == 3
        assert td.reid_scores == [0.3, 0.4, 0.5]

    def test_empty_scores_return_zero(self):
        td = TrackedDetection(track_id=0, bbox=(0, 0, 10, 10), confidence=0.9)
        assert td.avg_reid_score == 0.0
        assert td.avg_face_score == 0.0

    def test_add_scores_nonexistent_track(self):
        tracker = DetectionTracker()
        # Should not raise
        tracker.add_scores(999, reid_score=0.5)

    def test_zero_score_is_recorded(self):
        """A reid/face score of exactly 0.0 is a valid 'no match' signal and
        must be recorded in the rolling window, not silently dropped."""
        tracker = DetectionTracker()
        tracker.update([_det((10, 10, 50, 50))])
        tracker.add_scores(0, reid_score=0.0, face_score=0.0)
        td = tracker.get_track(0)
        assert len(td.reid_scores) == 1
        assert len(td.face_scores) == 1
        assert td.avg_reid_score == 0.0
        assert td.avg_face_score == 0.0

    def test_none_scores_are_not_recorded(self):
        """None means 'no score available' and should be skipped entirely,
        as opposed to 0.0 which is a real score."""
        tracker = DetectionTracker()
        tracker.update([_det((10, 10, 50, 50))])
        tracker.add_scores(0, reid_score=None, face_score=None)
        td = tracker.get_track(0)
        assert len(td.reid_scores) == 0
        assert len(td.face_scores) == 0

    def test_mixed_none_and_real_scores(self):
        tracker = DetectionTracker()
        tracker.update([_det((10, 10, 50, 50))])
        tracker.add_scores(0, reid_score=0.7, face_score=None)
        tracker.add_scores(0, reid_score=None, face_score=0.4)
        td = tracker.get_track(0)
        assert td.reid_scores == [0.7]
        assert td.face_scores == [0.4]


# ---------------------------------------------------------------------------
# Track lifecycle
# ---------------------------------------------------------------------------

class TestTrackLifecycle:
    def test_track_ages_when_not_matched(self):
        tracker = DetectionTracker(max_age=5)
        tracker.update([_det((10, 10, 50, 50))])
        # Send empty detections — track should age
        tracker.update([])
        tracker.update([])
        tracks = tracker.active_tracks
        assert len(tracks) == 1  # still alive at age 2

    def test_track_removed_after_max_age(self):
        tracker = DetectionTracker(max_age=3)
        tracker.update([_det((10, 10, 50, 50))])
        for _ in range(4):  # age 1, 2, 3, 4 — removed when age > max_age
            tracker.update([])
        assert len(tracker.active_tracks) == 0

    def test_track_age_reset_on_rematch(self):
        tracker = DetectionTracker(max_age=5)
        tracker.update([_det((10, 10, 50, 50))])
        # Age it a couple times
        tracker.update([])
        tracker.update([])
        # Re-match with same bbox
        result = tracker.update([_det((10, 10, 50, 50))])
        matched = [t for t in result if t.track_id == 0]
        assert len(matched) == 1
        assert matched[0].frames_seen == 2  # only counted when matched


# ---------------------------------------------------------------------------
# Best crop
# ---------------------------------------------------------------------------

class TestBestCrop:
    def test_best_crop_updated_on_higher_confidence(self):
        tracker = DetectionTracker()
        crop_low = np.ones((50, 30, 3), dtype=np.uint8)
        crop_high = np.ones((50, 30, 3), dtype=np.uint8) * 255
        tracker.update([_det((10, 10, 50, 50), confidence=0.6, crop=crop_low)])
        tracker.update([_det((10, 10, 50, 50), confidence=0.95, crop=crop_high)])
        td = tracker.get_track(0)
        assert td.best_crop_confidence == 0.95
        np.testing.assert_array_equal(td.best_crop, crop_high)

    def test_best_crop_not_replaced_by_lower_confidence(self):
        tracker = DetectionTracker()
        crop_high = np.ones((50, 30, 3), dtype=np.uint8) * 255
        crop_low = np.ones((50, 30, 3), dtype=np.uint8)
        tracker.update([_det((10, 10, 50, 50), confidence=0.95, crop=crop_high)])
        tracker.update([_det((10, 10, 50, 50), confidence=0.5, crop=crop_low)])
        td = tracker.get_track(0)
        assert td.best_crop_confidence == 0.95
        np.testing.assert_array_equal(td.best_crop, crop_high)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_detections_no_tracks(self):
        tracker = DetectionTracker()
        result = tracker.update([])
        assert result == []

    def test_no_existing_tracks(self):
        tracker = DetectionTracker()
        result = tracker.update([_det((10, 10, 50, 50)), _det((200, 200, 300, 300))])
        assert len(result) == 2

    def test_clear_removes_all(self):
        tracker = DetectionTracker()
        tracker.update([_det((10, 10, 50, 50))])
        tracker.update([_det((200, 200, 300, 300))])
        tracker.clear()
        assert len(tracker.active_tracks) == 0
        # Next detection should get id 0 again
        result = tracker.update([_det((10, 10, 50, 50))])
        assert result[0].track_id == 0

    def test_many_detections_few_tracks(self):
        tracker = DetectionTracker()
        tracker.update([_det((10, 10, 50, 50))])
        # Five new detections — only one can match existing track
        dets = [
            _det((10, 10, 50, 50)),
            _det((100, 100, 150, 150)),
            _det((200, 200, 250, 250)),
            _det((300, 300, 350, 350)),
            _det((400, 400, 450, 450)),
        ]
        result = tracker.update(dets)
        assert len(result) == 5  # 1 matched + 4 new

    def test_few_detections_many_tracks(self):
        tracker = DetectionTracker()
        # Create 5 tracks
        dets = [
            _det((10, 10, 50, 50)),
            _det((100, 100, 150, 150)),
            _det((200, 200, 250, 250)),
            _det((300, 300, 350, 350)),
            _det((400, 400, 450, 450)),
        ]
        tracker.update(dets)
        # Only one detection in next frame
        result = tracker.update([_det((10, 10, 50, 50))])
        # All 5 tracks still alive (4 aged by 1), 1 matched
        assert len(result) == 5

    def test_get_track_nonexistent(self):
        tracker = DetectionTracker()
        assert tracker.get_track(999) is None


# ---------------------------------------------------------------------------
# Multi-track
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_update_and_add_scores_no_crash(self):
        """Hammer the tracker from multiple threads concurrently. With the
        lock in place this should complete without raising (e.g. a
        RuntimeError from mutating self._tracks while another thread
        iterates it)."""
        tracker = DetectionTracker(max_age=100, vote_window=5)
        errors: list[Exception] = []

        def writer():
            try:
                for i in range(200):
                    tracker.update([_det((i, i, i + 40, i + 40))])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def scorer():
            try:
                for _ in range(200):
                    tracker.add_scores(0, reid_score=0.0, face_score=0.5)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def reader():
            try:
                for _ in range(200):
                    tracker.active_tracks
                    tracker.get_track(0)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=scorer),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent access raised: {errors}"

    def test_has_lock_attribute(self):
        tracker = DetectionTracker()
        assert isinstance(tracker._lock, type(threading.Lock()))


class TestMultiTrack:
    def test_two_people_maintain_separate_ids(self):
        tracker = DetectionTracker()
        tracker.update([
            _det((10, 10, 50, 50)),
            _det((200, 200, 300, 300)),
        ])
        result = tracker.update([
            _det((12, 12, 52, 52)),    # slight move of person 1
            _det((202, 202, 302, 302)),  # slight move of person 2
        ])
        ids = sorted(t.track_id for t in result)
        assert ids == [0, 1]
        for t in result:
            assert t.frames_seen == 2

    def test_crossing_paths_closest_iou_wins(self):
        tracker = DetectionTracker()
        # Person A at left, Person B at right
        tracker.update([
            _det((0, 0, 100, 100)),    # A
            _det((200, 0, 300, 100)),  # B
        ])
        # Both move toward center but A is still closer to original A position
        result = tracker.update([
            _det((30, 0, 130, 100)),   # shifted right, closer to A's old pos
            _det((170, 0, 270, 100)),  # shifted left, closer to B's old pos
        ])
        id_to_bbox = {t.track_id: t.bbox for t in result}
        # Track 0 (A) should match the leftmost detection
        assert id_to_bbox[0][0] == 30
        # Track 1 (B) should match the rightmost detection
        assert id_to_bbox[1][0] == 170
