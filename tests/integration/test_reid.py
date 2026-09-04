"""Integration tests for PersonReID (MobileNetV2 embeddings)."""

import numpy as np
import pytest

from flightrisk.vision.reid import PersonReID

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def reid():
    """Shared ReID instance — model loading is slow."""
    return PersonReID(match_threshold=0.55)


class TestPersonReIDInit:
    def test_initializes_without_error(self, reid):
        assert reid is not None
        assert reid.model is not None


class TestEmbedding:
    def test_extract_embedding_returns_512d(self, reid, sample_crop):
        embedding = reid._extract_embedding(sample_crop)
        assert isinstance(embedding, np.ndarray)
        assert embedding.shape == (512,)

    def test_embedding_is_l2_normalized(self, reid, sample_crop):
        embedding = reid._extract_embedding(sample_crop)
        norm = np.linalg.norm(embedding)
        assert abs(norm - 1.0) < 1e-5, f"Expected norm ~1.0, got {norm}"


class TestPublicExtractEmbedding:
    """Tests for the public extract_embedding() wrapper (used by EdgeRunner
    instead of reaching into the private _extract_embedding)."""

    def test_matches_private_extract_embedding(self, reid, sample_crop):
        public = reid.extract_embedding(sample_crop)
        private = reid._extract_embedding(sample_crop)
        assert isinstance(public, np.ndarray)
        assert public.shape == private.shape
        assert np.allclose(public, private)

    def test_empty_crop_returns_none(self, reid):
        empty = np.zeros((0, 0, 3), dtype=np.uint8)
        assert reid.extract_embedding(empty) is None

    def test_none_crop_returns_none(self, reid):
        assert reid.extract_embedding(None) is None


class TestCompare:
    def test_no_target_returns_zero(self, reid, sample_crop):
        # Fresh reid or one with no target set
        r = PersonReID(match_threshold=0.55)
        score = r.compare(sample_crop)
        assert score == 0.0


class TestFindMatch:
    def test_no_target_returns_none(self):
        r = PersonReID(match_threshold=0.55)
        idx, score = r.find_match([{"crop": np.zeros((50, 50, 3), dtype=np.uint8)}])
        assert idx is None
        assert score == 0.0

    def test_empty_list_returns_none(self, reid):
        idx, score = reid.find_match([])
        assert idx is None
        assert score == 0.0


class TestSetTargetAndCompare:
    def test_same_image_high_score(self, reid):
        """set_target with an image, then compare the same image -> score close to 1.0."""
        image = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        reid.set_target(image)
        score = reid.compare(image)
        assert score > 0.95, f"Self-comparison should be ~1.0, got {score}"

    def test_different_images_lower_score(self, reid):
        """Two structurally different images should have similarity < self-match."""
        # Random noise looks similar to MobileNetV2, so use structurally different images
        img1 = np.zeros((224, 224, 3), dtype=np.uint8)  # solid black
        img1[50:150, 50:150] = [255, 0, 0]  # red square in center

        img2 = np.full((224, 224, 3), 255, dtype=np.uint8)  # solid white
        img2[0:50, 0:50] = [0, 255, 0]  # green square in corner

        reid.set_target(img1)
        self_score = reid.compare(img1)
        other_score = reid.compare(img2)
        assert other_score < self_score, (
            f"Different image score ({other_score}) should be less than "
            f"self-comparison score ({self_score})"
        )


class TestSetTargetFromFile:
    def test_nonexistent_file_raises(self, reid):
        with pytest.raises(FileNotFoundError):
            reid.set_target_from_file("/nonexistent/path/to/image.jpg")


class TestClearTarget:
    """clear_target() was accidentally defined twice; verify the single
    remaining definition still works correctly."""

    def test_clear_target_resets_to_none(self, reid, sample_crop):
        reid.set_target(sample_crop)
        assert reid._target_embedding is not None
        reid.clear_target()
        assert reid._target_embedding is None

    def test_compare_after_clear_returns_zero(self, reid, sample_crop):
        reid.set_target(sample_crop)
        reid.clear_target()
        assert reid.compare(sample_crop) == 0.0
