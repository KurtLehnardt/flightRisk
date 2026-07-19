"""Integration tests for PersonReID (MobileNetV2 embeddings)."""

import numpy as np
import pytest

from amber.vision.reid import PersonReID

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
    def test_extract_embedding_returns_1280d(self, reid, sample_crop):
        embedding = reid._extract_embedding(sample_crop)
        assert isinstance(embedding, np.ndarray)
        assert embedding.shape == (1280,)

    def test_embedding_is_l2_normalized(self, reid, sample_crop):
        embedding = reid._extract_embedding(sample_crop)
        norm = np.linalg.norm(embedding)
        assert abs(norm - 1.0) < 1e-5, f"Expected norm ~1.0, got {norm}"


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
