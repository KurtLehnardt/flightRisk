"""Tests for flightrisk.vision.signals — configurable matching signal providers."""

import numpy as np
import pytest

from flightrisk.vision.signals import (
    ClothingColorSignal,
    HeightRatioSignal,
    InsightFaceR18Signal,
    OSNetReIDSignal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _solid_color_image(color_bgr: tuple[int, int, int], h: int = 200, w: int = 100) -> np.ndarray:
    """Create a solid-color BGR image."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = color_bgr
    return img


def _random_person_crop(h: int = 200, w: int = 100) -> np.ndarray:
    """Create a random-noise BGR image simulating a person crop."""
    rng = np.random.RandomState(42)
    return rng.randint(0, 256, (h, w, 3), dtype=np.uint8)


# ---------------------------------------------------------------------------
# 1. ClothingColorSignal
# ---------------------------------------------------------------------------

class TestClothingColorSignal:
    def test_available_is_always_true(self):
        signal = ClothingColorSignal()
        assert signal.available is True

    def test_has_target_false_initially(self):
        signal = ClothingColorSignal()
        assert signal.has_target is False

    def test_set_target_sets_has_target(self):
        signal = ClothingColorSignal()
        img = _solid_color_image((255, 0, 0))
        signal.set_target(img)
        assert signal.has_target is True

    def test_clear_target_resets(self):
        signal = ClothingColorSignal()
        signal.set_target(_solid_color_image((255, 0, 0)))
        signal.clear_target()
        assert signal.has_target is False

    def test_compare_no_target_returns_zero(self):
        signal = ClothingColorSignal()
        assert signal.compare(_solid_color_image((255, 0, 0))) == 0.0

    def test_compare_none_crop_returns_zero(self):
        signal = ClothingColorSignal()
        signal.set_target(_solid_color_image((255, 0, 0)))
        assert signal.compare(None) == 0.0

    def test_compare_empty_crop_returns_zero(self):
        signal = ClothingColorSignal()
        signal.set_target(_solid_color_image((255, 0, 0)))
        assert signal.compare(np.array([])) == 0.0

    def test_identical_image_high_score(self):
        signal = ClothingColorSignal()
        img = _solid_color_image((0, 128, 255))
        signal.set_target(img)
        score = signal.compare(img)
        assert score >= 0.9, f"Expected >= 0.9 for identical image, got {score}"

    def test_different_color_lower_score(self):
        signal = ClothingColorSignal()
        signal.set_target(_solid_color_image((255, 0, 0)))  # blue
        score = signal.compare(_solid_color_image((0, 255, 0)))  # green
        # Different colors should score lower than identical
        assert score < 0.9

    def test_score_in_range_0_1(self):
        signal = ClothingColorSignal()
        signal.set_target(_random_person_crop())
        score = signal.compare(_random_person_crop(h=300, w=120))
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# 2. HeightRatioSignal
# ---------------------------------------------------------------------------

class TestHeightRatioSignal:
    def test_available_is_always_true(self):
        signal = HeightRatioSignal()
        assert signal.available is True

    def test_has_target_false_initially(self):
        signal = HeightRatioSignal()
        assert signal.has_target is False

    def test_set_target_from_image(self):
        signal = HeightRatioSignal()
        img = np.zeros((200, 100, 3), dtype=np.uint8)
        signal.set_target(img)
        assert signal.has_target is True
        assert signal._target_ratio == pytest.approx(2.0)

    def test_set_target_from_bbox(self):
        signal = HeightRatioSignal()
        signal.set_target_from_bbox((10, 20, 110, 220))
        assert signal.has_target is True
        assert signal._target_ratio == pytest.approx(2.0)

    def test_clear_target(self):
        signal = HeightRatioSignal()
        signal.set_target(np.zeros((200, 100, 3), dtype=np.uint8))
        signal.clear_target()
        assert signal.has_target is False

    def test_compare_no_target_returns_zero(self):
        signal = HeightRatioSignal()
        assert signal.compare(np.zeros((200, 100, 3), dtype=np.uint8)) == 0.0

    def test_compare_none_crop_returns_zero(self):
        signal = HeightRatioSignal()
        signal.set_target(np.zeros((200, 100, 3), dtype=np.uint8))
        assert signal.compare(None) == 0.0

    def test_identical_ratio_perfect_score(self):
        signal = HeightRatioSignal()
        signal.set_target(np.zeros((200, 100, 3), dtype=np.uint8))
        score = signal.compare(np.zeros((200, 100, 3), dtype=np.uint8))
        assert score == pytest.approx(1.0)

    def test_different_ratio_lower_score(self):
        signal = HeightRatioSignal()
        signal.set_target(np.zeros((200, 100, 3), dtype=np.uint8))  # ratio 2.0
        score = signal.compare(np.zeros((100, 100, 3), dtype=np.uint8))  # ratio 1.0
        # 1 - abs(2.0 - 1.0) / max(2.0, 1.0) = 1 - 1.0/2.0 = 0.5
        assert score == pytest.approx(0.5)

    def test_score_clamped_to_0_1(self):
        signal = HeightRatioSignal()
        signal.set_target(np.zeros((200, 100, 3), dtype=np.uint8))
        # Various crop shapes
        for h, w in [(50, 100), (400, 100), (200, 50), (200, 200)]:
            score = signal.compare(np.zeros((h, w, 3), dtype=np.uint8))
            assert 0.0 <= score <= 1.0

    def test_compare_bbox(self):
        signal = HeightRatioSignal()
        signal.set_target_from_bbox((0, 0, 100, 200))  # ratio 2.0
        score = signal.compare_bbox((10, 10, 110, 210))  # also ratio 2.0
        assert score == pytest.approx(1.0)

    def test_zero_width_returns_zero(self):
        signal = HeightRatioSignal()
        signal.set_target(np.zeros((200, 100, 3), dtype=np.uint8))
        # 0-width crop
        score = signal.compare(np.zeros((200, 0, 3), dtype=np.uint8))
        assert score == 0.0


# ---------------------------------------------------------------------------
# 3. OSNetReIDSignal — model-missing fallback
# ---------------------------------------------------------------------------

class TestOSNetReIDSignalNoModel:
    """Tests OSNet signal when model file is absent (graceful degradation)."""

    def test_unavailable_when_model_missing(self):
        signal = OSNetReIDSignal(model_path="/nonexistent/osnet.onnx")
        assert signal.available is False

    def test_has_target_false_initially(self):
        signal = OSNetReIDSignal(model_path="/nonexistent/osnet.onnx")
        assert signal.has_target is False

    def test_compare_returns_zero_when_unavailable(self):
        signal = OSNetReIDSignal(model_path="/nonexistent/osnet.onnx")
        crop = _random_person_crop()
        assert signal.compare(crop) == 0.0

    def test_set_target_returns_false_when_unavailable(self):
        signal = OSNetReIDSignal(model_path="/nonexistent/osnet.onnx")
        result = signal.set_target(_random_person_crop())
        assert result is False

    def test_clear_target(self):
        signal = OSNetReIDSignal(model_path="/nonexistent/osnet.onnx")
        signal.clear_target()  # Should not raise
        assert signal.has_target is False


# ---------------------------------------------------------------------------
# 4. InsightFaceR18Signal — model-missing fallback
# ---------------------------------------------------------------------------

class TestInsightFaceR18SignalNoModel:
    """Tests InsightFace R18 signal when model file is absent."""

    def test_unavailable_when_model_missing(self):
        signal = InsightFaceR18Signal(model_path="/nonexistent/r18.onnx")
        assert signal.available is False

    def test_has_target_false_initially(self):
        signal = InsightFaceR18Signal(model_path="/nonexistent/r18.onnx")
        assert signal.has_target is False

    def test_compare_returns_zero_when_unavailable(self):
        signal = InsightFaceR18Signal(model_path="/nonexistent/r18.onnx")
        crop = _random_person_crop()
        assert signal.compare(crop) == 0.0

    def test_set_target_returns_false_when_unavailable(self):
        signal = InsightFaceR18Signal(model_path="/nonexistent/r18.onnx")
        result = signal.set_target(_random_person_crop())
        assert result is False

    def test_clear_target(self):
        signal = InsightFaceR18Signal(model_path="/nonexistent/r18.onnx")
        signal.clear_target()  # Should not raise
        assert signal.has_target is False
