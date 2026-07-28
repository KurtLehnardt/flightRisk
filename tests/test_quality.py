"""Tests for amber.vision.quality.ImageQualityScorer."""

import numpy as np
import pytest

from amber.vision.quality import ImageQualityScorer, QualityReport


@pytest.fixture
def scorer():
    return ImageQualityScorer()


class TestBlurDimension:
    """Blur scoring via Laplacian variance."""

    def test_solid_color_scores_low_on_blur(self, scorer):
        # Solid gray image has zero Laplacian variance
        img = np.full((200, 200, 3), 128, dtype=np.uint8)
        report = scorer.score(img)
        assert report.dimensions["blur"]["score"] == 0.0
        assert not report.dimensions["blur"]["passed"]

    def test_random_noise_scores_higher_on_blur(self, scorer):
        rng = np.random.RandomState(42)
        img = rng.randint(0, 256, (200, 200, 3), dtype=np.uint8)
        report = scorer.score(img)
        assert report.dimensions["blur"]["score"] > 0.5


class TestBrightnessDimension:
    """Brightness scoring via mean pixel value."""

    def test_all_black_scores_low_on_brightness(self, scorer):
        img = np.zeros((200, 200, 3), dtype=np.uint8)
        report = scorer.score(img)
        assert report.dimensions["brightness"]["score"] == 0.0
        assert not report.dimensions["brightness"]["passed"]

    def test_all_white_scores_low_on_brightness(self, scorer):
        img = np.full((200, 200, 3), 255, dtype=np.uint8)
        report = scorer.score(img)
        # mean=255, score = max(0, 1 - |255-130|/130) = max(0, 1-0.96) ~ 0.038
        assert report.dimensions["brightness"]["score"] < 0.1
        assert not report.dimensions["brightness"]["passed"]

    def test_mid_gray_scores_high_on_brightness(self, scorer):
        img = np.full((200, 200, 3), 130, dtype=np.uint8)
        report = scorer.score(img)
        assert report.dimensions["brightness"]["score"] == 1.0
        assert report.dimensions["brightness"]["passed"]


class TestContrastDimension:
    """Contrast scoring via standard deviation."""

    def test_uniform_image_scores_low_on_contrast(self, scorer):
        img = np.full((200, 200, 3), 128, dtype=np.uint8)
        report = scorer.score(img)
        assert report.dimensions["contrast"]["score"] == 0.0
        assert not report.dimensions["contrast"]["passed"]

    def test_high_variance_image_scores_high_on_contrast(self, scorer):
        # Create an image with high pixel variance
        img = np.zeros((200, 200, 3), dtype=np.uint8)
        img[:100, :, :] = 0
        img[100:, :, :] = 255
        report = scorer.score(img)
        assert report.dimensions["contrast"]["score"] > 0.8
        assert report.dimensions["contrast"]["passed"]


class TestResolutionDimension:
    """Resolution scoring based on min(width, height)."""

    def test_small_image_scores_low_on_resolution(self, scorer):
        img = np.full((50, 50, 3), 128, dtype=np.uint8)
        report = scorer.score(img)
        assert report.dimensions["resolution"]["score"] == pytest.approx(50 / 400)
        assert not report.dimensions["resolution"]["passed"]

    def test_large_image_scores_high_on_resolution(self, scorer):
        img = np.full((600, 800, 3), 128, dtype=np.uint8)
        report = scorer.score(img)
        assert report.dimensions["resolution"]["score"] == 1.0
        assert report.dimensions["resolution"]["passed"]


class TestAspectRatioDimension:
    """Aspect ratio scoring."""

    def test_square_scores_high_on_aspect_ratio(self, scorer):
        img = np.full((200, 200, 3), 128, dtype=np.uint8)
        report = scorer.score(img)
        assert report.dimensions["aspect_ratio"]["score"] == 1.0
        assert report.dimensions["aspect_ratio"]["passed"]

    def test_extreme_aspect_ratio_scores_low(self, scorer):
        img = np.full((10, 400, 3), 128, dtype=np.uint8)
        report = scorer.score(img)
        # ratio = 400/10 = 40, way beyond 3:1
        assert report.dimensions["aspect_ratio"]["score"] < 0.5
        assert not report.dimensions["aspect_ratio"]["passed"]


class TestOverallScoring:
    """Tests for overall score, grade, and report structure."""

    def test_overall_score_between_0_and_1(self, scorer):
        rng = np.random.RandomState(0)
        img = rng.randint(0, 256, (300, 300, 3), dtype=np.uint8)
        report = scorer.score(img)
        assert 0.0 <= report.overall_score <= 1.0

    def test_grade_good_threshold(self, scorer):
        # Build an image likely to score well: good size, mid-gray with noise
        rng = np.random.RandomState(99)
        img = (rng.normal(130, 40, (500, 500, 3)).clip(0, 255)).astype(np.uint8)
        report = scorer.score(img)
        # Even without face, other dimensions should be high enough for >= 0.4
        assert report.grade in ("good", "fair")

    def test_grade_poor_for_terrible_image(self, scorer):
        # Tiny, solid black image
        img = np.zeros((10, 10, 3), dtype=np.uint8)
        report = scorer.score(img)
        assert report.grade == "poor"
        assert report.overall_score < 0.4

    def test_issues_populated_for_bad_image(self, scorer):
        img = np.zeros((30, 30, 3), dtype=np.uint8)
        report = scorer.score(img)
        assert len(report.issues) > 0
        assert len(report.suggestions) > 0

    def test_tiny_1x1_image_does_not_crash(self, scorer):
        img = np.full((1, 1, 3), 128, dtype=np.uint8)
        report = scorer.score(img)
        assert isinstance(report, QualityReport)
        assert 0.0 <= report.overall_score <= 1.0
