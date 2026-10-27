"""Integration tests for FaceRecognizer (InsightFace/ArcFace)."""

import numpy as np
import pytest

try:
    from flightrisk.vision.face import FaceRecognizer, HAS_INSIGHTFACE
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


class TestPublicExtractEmbedding:
    """Tests for the public extract_embedding() wrapper (used by EdgeRunner
    instead of reaching into the private _best_face_embedding)."""

    def test_no_face_returns_none(self, face):
        noise = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        assert face.extract_embedding(noise) is None

    def test_matches_private_best_face_embedding(self, face):
        crop = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        public = face.extract_embedding(crop)
        private = face._best_face_embedding(crop)
        # Both None (no face in random noise) or both equal arrays.
        if public is None or private is None:
            assert public is None and private is None
        else:
            assert np.allclose(public, private)


@pytest.fixture(scope="module")
def sample_photo():
    """insightface's bundled public sample image (six adults)."""
    insightface = pytest.importorskip("insightface")
    return insightface.data.get_image("t1")


class TestEnrolmentReport:
    def test_group_photo_enrols_and_warns_about_multiple_faces(self, face, sample_photo):
        assert face.set_target(sample_photo) is True
        rep = face.target_report
        assert rep.ok and rep.n_faces >= 2
        assert any("faces found" in w for w in rep.warnings)
        assert np.isclose(np.linalg.norm(face.target_embedding), 1.0, atol=1e-4)

    def test_tiny_face_is_rejected_with_message_and_clears_old_target(self, face, sample_photo):
        import cv2

        assert face.set_target(sample_photo)
        tiny = cv2.resize(sample_photo, None, fx=0.1, fy=0.1, interpolation=cv2.INTER_AREA)
        assert face.set_target(tiny) is False
        assert face.target_report.reason in ("face_too_small", "no_face")
        assert face.target_report.message
        assert not face.has_target  # stale target must not linger

    def test_no_face_report(self, face):
        noise = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        assert face.set_target(noise) is False
        assert face.target_report.reason == "no_face"


class TestTrackedMatching:
    def test_same_person_scores_high_and_other_low(self, face, sample_photo):
        import cv2

        det, _ = face._detect(sample_photo, 1024, thresh=0.5)
        boxes = sorted(det[:, :4].astype(int).tolist())
        assert len(boxes) >= 2

        def head(b, px=60.0):
            x0, y0, x1, y1 = b
            w, h = x1 - x0, y1 - y0
            c = sample_photo[max(0, y0 - h // 2): y1 + h, max(0, x0 - w): x1 + w]
            return cv2.resize(c, None, fx=px / w, fy=px / w, interpolation=cv2.INTER_AREA)

        x0, y0, x1, y1 = boxes[0]
        ref = sample_photo[max(0, y0 - 40): y1 + 80, max(0, x0 - 80): x1 + 80]
        assert face.set_target(ref)
        same, other = head(boxes[0]), head(boxes[1])
        for _ in range(4):
            _, s_same = face.find_match([{"crop": same, "track_id": 1}])
            _, s_other = face.find_match([{"crop": other, "track_id": 2}])
        assert s_same > 0.45 > s_other
