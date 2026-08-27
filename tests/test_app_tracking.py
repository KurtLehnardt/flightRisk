"""Tests for tracker-integration glue logic in amber.dashboard.app.

Covers the PR #22 review fixes:
  - the bbox->track_id join must not misattribute a new detection to a
    stale, unmatched track that still carries a previous frame's bbox.
  - multi-frame corroboration must gate on the number of actual ReID
    samples recorded, not on the IoU-match frame counter.
"""

import numpy as np
import pytest

from amber.dashboard.app import _build_track_id_by_bbox
from amber.vision.tracker import DetectionTracker

pytestmark = pytest.mark.e2e


def _det(bbox, confidence=0.8, crop=None):
    """Shorthand for a detection dict matching PersonDetector output."""
    if crop is None:
        crop = np.zeros((50, 30, 3), dtype=np.uint8)
    return {"bbox": list(bbox), "confidence": confidence, "crop": crop}


class TestBuildTrackIdByBbox:
    def test_matches_current_frame_detections(self):
        tracker = DetectionTracker()
        detections = [_det((10, 10, 50, 50))]
        tracked = tracker.update(detections)
        join = _build_track_id_by_bbox(tracked, detections)
        assert join[tuple(detections[0]["bbox"])] == 0

    def test_stale_unmatched_track_excluded(self):
        """A track that aged out (no detection this frame) must not be
        joinable to a brand-new, unrelated detection."""
        tracker = DetectionTracker(max_age=30)
        # Frame 1: person at top-left creates track 0.
        tracker.update([_det((10, 10, 50, 50))])
        # Frame 2: person walked away (no detections at all) — track 0
        # ages but survives (max_age=30) with its stale bbox intact.
        tracked = tracker.update([])
        assert len(tracked) == 1
        assert tracked[0].track_id == 0

        # A brand new, unrelated detection this frame.
        new_detections = [_det((500, 500, 600, 600))]
        # Re-run update with the new detection so tracked_detections
        # reflects what the frame loop would actually see.
        tracked = tracker.update(new_detections)
        join = _build_track_id_by_bbox(tracked, new_detections)

        # The new detection's bbox must map to the new track, not the
        # stale track 0 (which still has its old, unrelated bbox and is
        # NOT present in `new_detections`).
        assert join[tuple(new_detections[0]["bbox"])] != 0
        # The stale track's bbox is not part of this frame's detections,
        # so it must not appear as a join key at all.
        assert tuple(_det((10, 10, 50, 50))["bbox"]) not in join

    def test_empty_detections_yields_empty_join(self):
        tracker = DetectionTracker()
        tracker.update([_det((10, 10, 50, 50))])
        tracked = tracker.update([])
        join = _build_track_id_by_bbox(tracked, [])
        assert join == {}

    def test_multiple_current_detections_all_joined(self):
        tracker = DetectionTracker()
        detections = [_det((10, 10, 50, 50)), _det((200, 200, 300, 300))]
        tracked = tracker.update(detections)
        join = _build_track_id_by_bbox(tracked, detections)
        assert len(join) == 2
        for d in detections:
            assert tuple(d["bbox"]) in join


class TestMultiFrameCorroborationCounter:
    """Regression test for gating on actual ReID sample count rather than
    the IoU-match frame counter (`frames_seen`), which can be inflated by
    matches that never got a ReID sample recorded."""

    def test_frames_seen_can_outpace_reid_samples(self):
        """Demonstrates the bug condition the fix guards against: a track
        can reach frames_seen >= 3 while having far fewer reid_scores."""
        tracker = DetectionTracker()
        tracker.update([_det((10, 10, 50, 50))])
        tracker.update([_det((10, 10, 50, 50))])
        tracker.update([_det((10, 10, 50, 50))])
        track = tracker.get_track(0)
        assert track.frames_seen == 3
        # Only one ReID sample was ever recorded for this track.
        tracker.add_scores(0, reid_score=0.9)
        track = tracker.get_track(0)
        assert track.frames_seen == 3
        assert len(track.reid_scores) == 1
        # The correct gate (len(reid_scores) >= 3) must NOT be satisfied
        # here even though frames_seen >= 3 is true.
        assert not (len(track.reid_scores) >= 3)

    def test_len_reid_scores_gate_requires_three_real_samples(self):
        tracker = DetectionTracker()
        tracker.update([_det((10, 10, 50, 50))])
        tracker.add_scores(0, reid_score=0.9)
        tracker.add_scores(0, reid_score=0.8)
        track = tracker.get_track(0)
        assert not (len(track.reid_scores) >= 3)
        tracker.add_scores(0, reid_score=0.85)
        track = tracker.get_track(0)
        assert len(track.reid_scores) >= 3
