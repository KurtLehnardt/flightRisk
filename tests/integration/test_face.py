"""Integration tests for FaceRecognizer (InsightFace/ArcFace)."""

import numpy as np
import pytest

try:
    from amber.vision.face import FaceRecognizer, HAS_INSIGHTFACE
except ImportError:
    HAS_INSIGHTFACE = False

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not HAS_INSIGHTFACE, reason="insightface not installed"),
]


@pytest.fixture(scope="module")
def face():
    """Shared FaceRecognizer instance."""
    return FaceRecognizer(match_threshold=0.45)


class TestFaceRecognizerInit:
    def test_initializes(self, face):
        assert face is not None
        assert face.app is not None

    def test_has_target_initially_false(self, face):
        # Reset target for clean test
        face._target_embedding = None
        assert face.has_target is False


class TestCompare:
    def test_no_target_returns_zero(self, face):
        face._target_embedding = None
        crop = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        score = face.compare(crop)
        assert score == 0.0


class TestFindMatch:
    def test_no_target_returns_none(self, face):
        face._target_embedding = None
        idx, score = face.find_match([{"crop": np.zeros((50, 50, 3), dtype=np.uint8)}])
        assert idx is None
        assert score == 0.0


class TestSetTarget:
    def test_random_noise_returns_false(self, face):
        """Random noise should not contain a detectable face."""
        noise = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        result = face.set_target(noise)
        assert result is False


class TestSetTargetFromFile:
    def test_bad_path_raises(self, face):
        with pytest.raises(FileNotFoundError):
            face.set_target_from_file("/nonexistent/image.jpg")
