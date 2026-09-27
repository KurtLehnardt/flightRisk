"""Tests for flightrisk.vision.signal_registry.SignalRegistry."""

import numpy as np
import pytest

from flightrisk.config import reset_config
from flightrisk.vision.scorer import MatchScorer
from flightrisk.vision.signal_registry import SignalRegistry


@pytest.fixture(autouse=True)
def _reset_config():
    """Ensure each test gets a fresh config singleton."""
    reset_config()
    yield
    reset_config()


class TestSignalRegistryInit:
    def test_creates_pure_cv_signals(self):
        """clothing_color and height_ratio should always be available."""
        registry = SignalRegistry()
        signals = registry.active_signals
        assert "clothing_color" in signals
        assert "height_ratio" in signals

    def test_onnx_signals_unavailable_without_models(self):
        """osnet_reid and insightface_face should be absent when models
        don't exist at the default paths."""
        registry = SignalRegistry()
        signals = registry.active_signals
        # Models don't exist in the test env
        assert "osnet_reid" not in signals
        assert "insightface_face" not in signals


class TestSignalRegistryRegister:
    def test_register_all_adds_to_scorer(self):
        registry = SignalRegistry()
        scorer = MatchScorer()
        registry.register_all(scorer)

        # The scorer should now have clothing_color and height_ratio registered
        assert "clothing_color" in scorer._signals
        assert "height_ratio" in scorer._signals

    def test_registered_signals_have_correct_weights(self):
        registry = SignalRegistry()
        scorer = MatchScorer()
        registry.register_all(scorer)

        assert scorer._signals["clothing_color"]["weight"] == 0.15
        assert scorer._signals["height_ratio"]["weight"] == 0.05


class TestSignalRegistryTargets:
    def test_set_target_all_providers(self):
        registry = SignalRegistry()
        img = np.zeros((200, 100, 3), dtype=np.uint8)
        results = registry.set_target(img)

        for name in registry.active_signals:
            assert name in results

    def test_clear_targets_resets_all(self):
        registry = SignalRegistry()
        img = np.zeros((200, 100, 3), dtype=np.uint8)
        registry.set_target(img)
        registry.clear_targets()

        for name in registry.active_signals:
            provider = registry.get_provider(name)
            assert not getattr(provider, "has_target", True)


class TestSignalRegistryScoring:
    def test_score_detection_returns_dict(self):
        registry = SignalRegistry()
        target = np.zeros((200, 100, 3), dtype=np.uint8)
        registry.set_target(target)
        crop = np.zeros((200, 100, 3), dtype=np.uint8)
        scores = registry.score_detection(crop)
        assert isinstance(scores, dict)

    def test_score_detection_no_target_returns_empty(self):
        registry = SignalRegistry()
        crop = np.zeros((200, 100, 3), dtype=np.uint8)
        scores = registry.score_detection(crop)
        assert scores == {}

    def test_scores_work_with_scorer(self):
        """Extra signal scores should integrate with MatchScorer."""
        registry = SignalRegistry()
        scorer = MatchScorer()
        registry.register_all(scorer)

        target = np.zeros((200, 100, 3), dtype=np.uint8)
        registry.set_target(target)

        crop = np.zeros((200, 100, 3), dtype=np.uint8)
        extra = registry.score_detection(crop)

        result = scorer.score(reid_score=0.7, face_score=0.8, **extra)
        assert result["combined_score"] > 0
        assert result["signals_used"] >= 2

    def test_get_provider_returns_none_for_unknown(self):
        registry = SignalRegistry()
        assert registry.get_provider("nonexistent") is None

    def test_get_provider_returns_instance(self):
        registry = SignalRegistry()
        provider = registry.get_provider("clothing_color")
        assert provider is not None
