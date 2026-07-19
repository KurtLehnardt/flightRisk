"""Unit tests for amber.vision.scorer.MatchScorer."""

import pytest

from amber.vision.scorer import MatchScorer


class TestMatchScorerAllSignals:
    """Tests with all three signals present."""

    def test_all_signals_correct_weighted_score(self):
        scorer = MatchScorer()
        result = scorer.score(
            reid_score=0.8,
            face_score=0.9,
            reasoning_result={"match": True, "confidence": "high"},
        )
        # Weights: reid=0.35, face=0.40, reasoning=0.25 (sum=1.0, no redistribution)
        # Combined = 0.8*0.35 + 0.9*0.40 + 0.90*0.25 = 0.28 + 0.36 + 0.225 = 0.865
        assert result["combined_score"] == pytest.approx(0.865, abs=0.01)
        assert result["is_match"] is True
        assert result["signals_used"] == 3

    def test_all_signals_breakdown_keys(self):
        scorer = MatchScorer()
        result = scorer.score(
            reid_score=0.7,
            face_score=0.8,
            reasoning_result={"match": True, "confidence": "medium"},
        )
        assert "reid" in result["breakdown"]
        assert "face" in result["breakdown"]
        assert "reasoning" in result["breakdown"]
        for key in result["breakdown"]:
            entry = result["breakdown"][key]
            assert "raw_score" in entry
            assert "weight" in entry
            assert "contribution" in entry


class TestMatchScorerSingleSignal:
    """Tests with only one signal present (weight redistribution)."""

    def test_reid_only(self):
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.8)
        # Only reid active, weight redistributed to 1.0
        assert result["breakdown"]["reid"]["weight"] == 1.0
        assert result["combined_score"] == pytest.approx(0.8, abs=0.01)
        assert result["signals_used"] == 1

    def test_face_only(self):
        scorer = MatchScorer()
        result = scorer.score(face_score=0.9)
        assert result["breakdown"]["face"]["weight"] == 1.0
        assert result["combined_score"] == pytest.approx(0.9, abs=0.01)
        assert result["signals_used"] == 1

    def test_reasoning_only(self):
        scorer = MatchScorer()
        result = scorer.score(
            reasoning_result={"match": True, "confidence": "high"}
        )
        assert result["breakdown"]["reasoning"]["weight"] == 1.0
        # High confidence → 0.90
        assert result["combined_score"] == pytest.approx(0.9, abs=0.01)
        assert result["signals_used"] == 1


class TestMatchScorerNoSignals:
    """Tests with no signals."""

    def test_no_signals_returns_zero(self):
        scorer = MatchScorer()
        result = scorer.score()
        assert result["combined_score"] == 0.0
        assert result["is_match"] is False
        assert result["breakdown"] == {}
        assert result["confidence_level"] == "none"

    def test_all_zeros_returns_zero(self):
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.0, face_score=0.0)
        assert result["combined_score"] == 0.0
        assert result["is_match"] is False


class TestMatchScorerReasoning:
    """Tests for reasoning result parsing."""

    def test_reasoning_no_match_gives_zero(self):
        scorer = MatchScorer()
        result = scorer.score(
            reid_score=0.5,
            reasoning_result={"match": False, "confidence": "high"},
        )
        # Reasoning score is 0.0 because match=False
        # Only reid is active
        assert "reasoning" not in result["breakdown"]
        assert result["signals_used"] == 1

    def test_reasoning_high_confidence(self):
        scorer = MatchScorer()
        result = scorer.score(
            reasoning_result={"match": True, "confidence": "high"}
        )
        assert result["breakdown"]["reasoning"]["raw_score"] == 0.9

    def test_reasoning_medium_confidence(self):
        scorer = MatchScorer()
        result = scorer.score(
            reasoning_result={"match": True, "confidence": "medium"}
        )
        assert result["breakdown"]["reasoning"]["raw_score"] == 0.65

    def test_reasoning_low_confidence(self):
        scorer = MatchScorer()
        result = scorer.score(
            reasoning_result={"match": True, "confidence": "low"}
        )
        assert result["breakdown"]["reasoning"]["raw_score"] == 0.4

    def test_reasoning_unknown_confidence_defaults(self):
        scorer = MatchScorer()
        result = scorer.score(
            reasoning_result={"match": True, "confidence": "unknown"}
        )
        assert result["breakdown"]["reasoning"]["raw_score"] == 0.5

    def test_reasoning_none_result(self):
        scorer = MatchScorer()
        # _reasoning_to_score(None) should return 0.0
        assert scorer._reasoning_to_score(None) == 0.0


class TestMatchScorerThreshold:
    """Tests for match threshold behavior."""

    def test_score_at_threshold_is_match(self):
        # Use a single signal so we can control the combined score exactly
        scorer = MatchScorer(match_threshold=0.5)
        result = scorer.score(reid_score=0.5)
        # Single signal, weight=1.0 → combined=0.5, threshold=0.5
        assert result["combined_score"] == pytest.approx(0.5, abs=0.001)
        assert result["is_match"] is True

    def test_score_below_threshold_no_match(self):
        scorer = MatchScorer(match_threshold=0.5)
        result = scorer.score(reid_score=0.49)
        assert result["combined_score"] == pytest.approx(0.49, abs=0.001)
        assert result["is_match"] is False


class TestMatchScorerConfidenceLevel:
    """Tests for confidence level classification."""

    def test_high_confidence(self):
        scorer = MatchScorer()
        result = scorer.score(
            reid_score=0.9,
            face_score=0.9,
        )
        # combined >= 0.75 and num_signals >= 2
        assert result["confidence_level"] == "high"

    def test_medium_confidence_high_single_signal(self):
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.8)
        # combined=0.8, only 1 signal → not high (needs >=2)
        # 0.8 >= 0.55 → medium
        assert result["confidence_level"] == "medium"

    def test_medium_confidence_two_signals_moderate(self):
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.5, face_score=0.5)
        # Weights redistributed: reid=0.4667, face=0.5333
        # combined = 0.5*0.4667 + 0.5*0.5333 = 0.5
        # 0.5 >= 0.45 and num_signals >= 2 → medium
        assert result["confidence_level"] == "medium"

    def test_low_confidence(self):
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.2)
        # combined=0.2, 1 signal
        assert result["confidence_level"] == "low"


class TestMatchScorerCustomWeights:
    """Tests for custom weight configuration."""

    def test_custom_weights(self):
        scorer = MatchScorer(reid_weight=0.5, face_weight=0.3, reasoning_weight=0.2)
        result = scorer.score(
            reid_score=1.0,
            face_score=1.0,
            reasoning_result={"match": True, "confidence": "high"},
        )
        # All signals: 1.0*0.5 + 1.0*0.3 + 0.9*0.2 = 0.5+0.3+0.18 = 0.98
        assert result["combined_score"] == pytest.approx(0.98, abs=0.01)

    def test_custom_threshold(self):
        scorer = MatchScorer(match_threshold=0.9)
        result = scorer.score(reid_score=0.85)
        assert result["is_match"] is False


class TestMatchScorerEdgeCases:
    """Edge case tests."""

    def test_all_scores_at_one(self):
        scorer = MatchScorer()
        result = scorer.score(
            reid_score=1.0,
            face_score=1.0,
            reasoning_result={"match": True, "confidence": "high"},
        )
        # 1.0*0.35 + 1.0*0.40 + 0.90*0.25 = 0.35+0.40+0.225 = 0.975
        assert result["combined_score"] == pytest.approx(0.975, abs=0.01)
        assert result["is_match"] is True

    def test_all_scores_at_zero(self):
        scorer = MatchScorer()
        result = scorer.score(
            reid_score=0.0,
            face_score=0.0,
            reasoning_result=None,
        )
        assert result["combined_score"] == 0.0
        assert result["is_match"] is False
