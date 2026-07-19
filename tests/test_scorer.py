"""Tests for amber.vision.scorer.MatchScorer."""

import pytest

from amber.vision.scorer import MatchScorer


class TestMatchScorerAllSignals:
    """Tests with all three signals present."""

    def test_all_three_signals_correct_weighted_score(self):
        scorer = MatchScorer()
        result = scorer.score(
            reid_score=0.8,
            face_score=0.9,
            reasoning_result={"match": True, "confidence": "high"},
        )
        # Weights: reid=0.35, face=0.40, reasoning=0.25 (already sum to 1.0)
        expected = round(0.8 * 0.35 + 0.9 * 0.40 + 0.90 * 0.25, 3)
        assert result["combined_score"] == expected
        assert result["is_match"] is True
        assert result["signals_used"] == 3

    def test_all_scores_one_combined_near_one(self):
        scorer = MatchScorer()
        result = scorer.score(
            reid_score=1.0,
            face_score=1.0,
            reasoning_result={"match": True, "confidence": "high"},
        )
        # 1.0*0.35 + 1.0*0.40 + 0.90*0.25 = 0.35 + 0.40 + 0.225 = 0.975
        assert result["combined_score"] == 0.975
        assert result["is_match"] is True

    def test_all_scores_zero_combined_zero(self):
        scorer = MatchScorer()
        result = scorer.score(
            reid_score=0.0,
            face_score=0.0,
            reasoning_result=None,
        )
        assert result["combined_score"] == 0.0
        assert result["is_match"] is False

    def test_signals_used_count_correct(self):
        scorer = MatchScorer()
        # Two signals
        result = scorer.score(reid_score=0.5, face_score=0.7)
        assert result["signals_used"] == 2

        # One signal
        result = scorer.score(reid_score=0.5)
        assert result["signals_used"] == 1

        # Three signals
        result = scorer.score(
            reid_score=0.5,
            face_score=0.7,
            reasoning_result={"match": True, "confidence": "medium"},
        )
        assert result["signals_used"] == 3


class TestMatchScorerSingleSignal:
    """Tests with single signal — weight redistribution."""

    def test_reid_only_redistributes_weight(self):
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.8)
        # Only reid active, so weight normalized to 1.0
        assert result["combined_score"] == 0.8
        assert result["signals_used"] == 1
        assert "reid" in result["breakdown"]
        assert result["breakdown"]["reid"]["weight"] == 1.0

    def test_face_only_redistributes_weight(self):
        scorer = MatchScorer()
        result = scorer.score(face_score=0.7)
        assert result["combined_score"] == 0.7
        assert result["signals_used"] == 1
        assert "face" in result["breakdown"]
        assert result["breakdown"]["face"]["weight"] == 1.0

    def test_reasoning_only_redistributes_weight(self):
        scorer = MatchScorer()
        result = scorer.score(
            reasoning_result={"match": True, "confidence": "high"},
        )
        # reasoning_score = 0.90, sole signal, weight = 1.0
        assert result["combined_score"] == 0.9
        assert result["signals_used"] == 1
        assert "reasoning" in result["breakdown"]
        assert result["breakdown"]["reasoning"]["weight"] == 1.0


class TestMatchScorerNoSignals:
    """Tests when no signals are provided."""

    def test_no_signals_returns_zero(self):
        scorer = MatchScorer()
        result = scorer.score()
        assert result["combined_score"] == 0.0
        assert result["is_match"] is False
        assert result["breakdown"] == {}
        assert result["confidence_level"] == "none"

    def test_no_signals_with_explicit_zeros(self):
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.0, face_score=0.0, reasoning_result=None)
        assert result["combined_score"] == 0.0
        assert result["is_match"] is False


class TestMatchScorerReasoning:
    """Tests for reasoning result conversion."""

    def test_reasoning_match_false_gives_zero(self):
        scorer = MatchScorer()
        result = scorer.score(
            reasoning_result={"match": False, "confidence": "high"},
        )
        # match=False => reasoning_score=0.0 => no signal
        assert result["combined_score"] == 0.0
        assert result["is_match"] is False

    def test_confidence_high_maps_to_090(self):
        scorer = MatchScorer()
        result = scorer.score(
            reasoning_result={"match": True, "confidence": "high"},
        )
        assert result["breakdown"]["reasoning"]["raw_score"] == 0.9

    def test_confidence_medium_maps_to_065(self):
        scorer = MatchScorer()
        result = scorer.score(
            reasoning_result={"match": True, "confidence": "medium"},
        )
        assert result["breakdown"]["reasoning"]["raw_score"] == 0.65

    def test_confidence_low_maps_to_040(self):
        scorer = MatchScorer()
        result = scorer.score(
            reasoning_result={"match": True, "confidence": "low"},
        )
        assert result["breakdown"]["reasoning"]["raw_score"] == 0.4

    def test_unknown_confidence_maps_to_050(self):
        scorer = MatchScorer()
        result = scorer.score(
            reasoning_result={"match": True, "confidence": "unknown"},
        )
        assert result["breakdown"]["reasoning"]["raw_score"] == 0.5

    def test_reasoning_none_gives_no_reasoning_signal(self):
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.5, reasoning_result=None)
        assert "reasoning" not in result["breakdown"]


class TestMatchScorerThreshold:
    """Tests for threshold boundary behavior."""

    def test_score_at_threshold_is_match(self):
        # Set threshold = 0.5, construct a scenario that produces exactly 0.5
        scorer = MatchScorer(match_threshold=0.5)
        result = scorer.score(reid_score=0.5)
        # Single signal, weight = 1.0, combined = 0.5
        assert result["combined_score"] == 0.5
        assert result["is_match"] is True

    def test_score_below_threshold_not_match(self):
        scorer = MatchScorer(match_threshold=0.5)
        result = scorer.score(reid_score=0.49)
        assert result["combined_score"] == 0.49
        assert result["is_match"] is False

    def test_score_above_threshold_is_match(self):
        scorer = MatchScorer(match_threshold=0.5)
        result = scorer.score(reid_score=0.51)
        assert result["combined_score"] == 0.51
        assert result["is_match"] is True


class TestMatchScorerConfidenceLevel:
    """Tests for confidence level classification."""

    def test_high_confidence_requires_high_score_and_two_signals(self):
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.9, face_score=0.9)
        # combined = 0.9, signals = 2 => high
        assert result["confidence_level"] == "high"

    def test_high_score_single_signal_not_high_confidence(self):
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.9)
        # combined = 0.9, signals = 1 => not "high" (needs >= 2 signals)
        assert result["confidence_level"] == "medium"

    def test_medium_confidence_at_055(self):
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.55)
        # combined = 0.55, 1 signal => medium (>= 0.55)
        assert result["confidence_level"] == "medium"

    def test_low_confidence_below_thresholds(self):
        scorer = MatchScorer()
        result = scorer.score(reid_score=0.3)
        # combined = 0.3, 1 signal => low
        assert result["confidence_level"] == "low"

    def test_medium_confidence_with_two_signals_at_045(self):
        scorer = MatchScorer()
        # Need combined >= 0.45 and num_signals >= 2
        result = scorer.score(reid_score=0.5, face_score=0.45)
        # Weights: reid=0.35/0.75=0.467, face=0.40/0.75=0.533
        # combined = 0.5*0.467 + 0.45*0.533 = 0.233 + 0.240 = ~0.473
        assert result["confidence_level"] == "medium"


class TestMatchScorerCustomWeights:
    """Tests for custom weight configuration."""

    def test_custom_weights_via_constructor(self):
        scorer = MatchScorer(reid_weight=0.5, face_weight=0.3, reasoning_weight=0.2)
        result = scorer.score(
            reid_score=1.0,
            face_score=1.0,
            reasoning_result={"match": True, "confidence": "high"},
        )
        expected = round(1.0 * 0.5 + 1.0 * 0.3 + 0.90 * 0.2, 3)
        assert result["combined_score"] == expected

    def test_custom_threshold(self):
        scorer = MatchScorer(match_threshold=0.8)
        result = scorer.score(reid_score=0.7)
        assert result["is_match"] is False

        result = scorer.score(reid_score=0.85)
        assert result["is_match"] is True
