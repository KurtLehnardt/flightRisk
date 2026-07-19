"""Regression tests for edge cases and boundary conditions."""

import numpy as np
import pytest

pytestmark = pytest.mark.regression


class TestMatchScorerEdgeCases:
    def test_all_zero_scores(self):
        from amber.vision.scorer import MatchScorer
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.0, face_score=0.0, reasoning_result=None)
        assert result["combined_score"] == 0.0
        assert result["is_match"] is False

    def test_very_high_scores_no_crash(self):
        from amber.vision.scorer import MatchScorer
        scorer = MatchScorer()
        result = scorer.score(reid_score=1.5, face_score=2.0, reasoning_result=None)
        assert isinstance(result["combined_score"], float)
        assert result["is_match"] is True or result["is_match"] is False  # doesn't crash

    def test_none_reasoning_result(self):
        from amber.vision.scorer import MatchScorer
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.5, face_score=0.5, reasoning_result=None)
        # reasoning should contribute 0.0
        assert "reasoning" not in result["breakdown"]

    def test_empty_dict_reasoning_result(self):
        from amber.vision.scorer import MatchScorer
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.5, face_score=0.5, reasoning_result={})
        # empty dict → match=False → score 0.0
        assert "reasoning" not in result["breakdown"]

    def test_reasoning_unknown_confidence(self):
        from amber.vision.scorer import MatchScorer
        scorer = MatchScorer()
        result = scorer.score(
            reid_score=0.5,
            face_score=0.5,
            reasoning_result={"match": True, "confidence": "unknown"},
        )
        # "unknown" maps to 0.50
        assert "reasoning" in result["breakdown"]
        assert result["breakdown"]["reasoning"]["raw_score"] == 0.5


class TestReIDEdgeCases:
    def test_tiny_crop_no_crash(self, tiny_crop):
        from amber.vision.reid import PersonReID
        reid = PersonReID(match_threshold=0.55)
        # Set a target first so compare actually runs
        target = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        reid.set_target(target)
        score = reid.compare(tiny_crop)
        assert isinstance(score, float)

    def test_large_crop_no_crash(self, large_crop):
        from amber.vision.reid import PersonReID
        reid = PersonReID(match_threshold=0.55)
        target = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        reid.set_target(target)
        score = reid.compare(large_crop)
        assert isinstance(score, float)


class TestSearchPatternEdgeCases:
    def test_zero_expansions_expanding_square(self):
        from amber.drone.search import generate_expanding_square
        result = generate_expanding_square(num_expansions=0)
        assert isinstance(result, list)
        assert len(result) == 0

    def test_clamp_distance_at_20(self):
        from amber.drone.search import _clamp_distance
        assert _clamp_distance(20) == 20

    def test_clamp_distance_at_500(self):
        from amber.drone.search import _clamp_distance
        assert _clamp_distance(500) == 500

    def test_clamp_distance_below_min(self):
        from amber.drone.search import _clamp_distance
        assert _clamp_distance(5) == 20

    def test_clamp_distance_above_max(self):
        from amber.drone.search import _clamp_distance
        assert _clamp_distance(1000) == 500


class TestRecorderEdgeCases:
    def test_write_frame_when_not_recording(self):
        from amber.recorder import SessionRecorder
        recorder = SessionRecorder()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        # Should silently return without error
        recorder.write_frame(frame)
        assert recorder.frame_count == 0

    def test_stop_when_not_recording(self):
        from amber.recorder import SessionRecorder
        recorder = SessionRecorder()
        result = recorder.stop()
        assert result is None

    def test_is_recording_initially_false(self):
        from amber.recorder import SessionRecorder
        recorder = SessionRecorder()
        assert recorder.is_recording is False
