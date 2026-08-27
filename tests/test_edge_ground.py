"""Tests for EdgeRunner / GroundStation compute split."""

import base64
import time
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from amber.edge import Detection, DetectionMessage, EdgeRunner
from amber.ground import GroundStation
from amber.vision.scorer import MatchScorer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_frame(width=640, height=480):
    """Create a synthetic BGR frame."""
    return np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)


def _make_mock_detector(detections: list[dict]):
    """Return a mock detector whose .detect() returns *detections*."""
    mock = MagicMock()
    mock.detect.return_value = detections
    return mock


def _make_mock_reid(embedding: np.ndarray | None = None):
    """Return a mock PersonReID with a controllable _extract_embedding."""
    mock = MagicMock()
    if embedding is not None:
        mock._extract_embedding.return_value = embedding
    else:
        mock._extract_embedding.side_effect = Exception("no embedding")
    return mock


def _make_mock_face(embedding: np.ndarray | None = None):
    """Return a mock FaceRecognizer with a controllable _best_face_embedding."""
    mock = MagicMock()
    if embedding is not None:
        mock._best_face_embedding.return_value = embedding
    else:
        mock._best_face_embedding.return_value = None
    return mock


def _normalized(vec: list[float]) -> np.ndarray:
    """Return an L2-normalized copy of *vec*."""
    a = np.array(vec, dtype=np.float64)
    return a / (np.linalg.norm(a) + 1e-12)


# ===========================================================================
# EdgeRunner tests
# ===========================================================================

class TestEdgeRunnerNoDetector:
    """EdgeRunner with no detector should return an empty message."""

    def test_returns_empty_detections(self):
        runner = EdgeRunner()
        msg = runner.process_frame(_make_frame())
        assert isinstance(msg, DetectionMessage)
        assert msg.detections == []
        assert msg.frame_id == 1

    def test_frame_id_increments(self):
        runner = EdgeRunner()
        m1 = runner.process_frame(_make_frame())
        m2 = runner.process_frame(_make_frame())
        assert m1.frame_id == 1
        assert m2.frame_id == 2

    def test_generates_thumbnail(self):
        runner = EdgeRunner()
        msg = runner.process_frame(_make_frame())
        assert msg.thumbnail_jpeg is not None
        # Thumbnail should be decodable JPEG
        arr = np.frombuffer(msg.thumbnail_jpeg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        assert img is not None
        assert img.shape[1] == 320  # width
        assert img.shape[0] == 180  # height


class TestEdgeRunnerWithDetector:
    """EdgeRunner with a mock detector."""

    def test_produces_detections(self):
        frame = _make_frame()
        dets = [{"bbox": [10, 20, 110, 220], "confidence": 0.85, "crop": frame[20:220, 10:110]}]
        runner = EdgeRunner(detector=_make_mock_detector(dets))
        msg = runner.process_frame(frame)

        assert len(msg.detections) == 1
        d = msg.detections[0]
        assert d.bbox == (10, 20, 110, 220)
        assert d.confidence == 0.85
        assert d.crop_jpeg is not None

    def test_skips_zero_size_crop(self):
        """Detection whose bbox yields an empty crop is skipped."""
        frame = _make_frame(width=100, height=100)
        # bbox outside frame bounds -> empty crop
        dets = [{"bbox": [200, 200, 300, 300], "confidence": 0.9, "crop": np.array([])}]

        mock_det = MagicMock()
        mock_det.detect.return_value = dets
        runner = EdgeRunner(detector=mock_det)
        msg = runner.process_frame(frame)
        # The frame slicing frame[200:300, 200:300] on a 100x100 frame gives empty
        assert len(msg.detections) == 0

    def test_reid_embedding_attached(self):
        frame = _make_frame()
        dets = [{"bbox": [10, 20, 110, 220], "confidence": 0.8, "crop": frame[20:220, 10:110]}]
        emb = _normalized([1.0, 0.0, 0.0, 0.0])
        runner = EdgeRunner(
            detector=_make_mock_detector(dets),
            reid=_make_mock_reid(emb),
        )
        msg = runner.process_frame(frame)
        assert len(msg.detections) == 1
        assert msg.detections[0].reid_embedding is not None
        assert len(msg.detections[0].reid_embedding) == 4

    def test_face_embedding_attached(self):
        frame = _make_frame()
        dets = [{"bbox": [10, 20, 110, 220], "confidence": 0.8, "crop": frame[20:220, 10:110]}]
        emb = _normalized([0.0, 1.0, 0.0, 0.0])
        runner = EdgeRunner(
            detector=_make_mock_detector(dets),
            face=_make_mock_face(emb),
        )
        msg = runner.process_frame(frame)
        assert len(msg.detections) == 1
        assert msg.detections[0].face_embedding is not None

    def test_reid_exception_is_swallowed(self):
        frame = _make_frame()
        dets = [{"bbox": [10, 20, 110, 220], "confidence": 0.8, "crop": frame[20:220, 10:110]}]
        runner = EdgeRunner(
            detector=_make_mock_detector(dets),
            reid=_make_mock_reid(None),  # raises Exception
        )
        msg = runner.process_frame(frame)
        assert len(msg.detections) == 1
        assert msg.detections[0].reid_embedding is None

    def test_face_returns_none_handled(self):
        frame = _make_frame()
        dets = [{"bbox": [10, 20, 110, 220], "confidence": 0.8, "crop": frame[20:220, 10:110]}]
        runner = EdgeRunner(
            detector=_make_mock_detector(dets),
            face=_make_mock_face(None),
        )
        msg = runner.process_frame(frame)
        assert len(msg.detections) == 1
        assert msg.detections[0].face_embedding is None

    def test_multiple_detections(self):
        frame = _make_frame()
        dets = [
            {"bbox": [10, 20, 110, 220], "confidence": 0.8, "crop": frame[20:220, 10:110]},
            {"bbox": [200, 100, 400, 400], "confidence": 0.6, "crop": frame[100:400, 200:400]},
        ]
        runner = EdgeRunner(detector=_make_mock_detector(dets))
        msg = runner.process_frame(frame)
        assert len(msg.detections) == 2


class TestEdgeRunnerSerialization:
    """Serialization round-trip tests."""

    def test_round_trip_preserves_fields(self):
        runner = EdgeRunner()
        frame = _make_frame()

        # Build a message manually with known data
        detection = Detection(
            bbox=(10, 20, 110, 220),
            confidence=0.85,
            reid_embedding=[0.1, 0.2, 0.3],
            face_embedding=[0.4, 0.5, 0.6],
            crop_jpeg=b"\xff\xd8fake_jpeg",
        )
        msg = DetectionMessage(
            timestamp=1234567890.123,
            frame_id=42,
            thumbnail_jpeg=b"\xff\xd8fake_thumb",
            detections=[detection],
        )

        data = runner.to_dict(msg)
        restored = EdgeRunner.from_dict(data)

        assert restored.timestamp == msg.timestamp
        assert restored.frame_id == msg.frame_id
        assert restored.thumbnail_jpeg == msg.thumbnail_jpeg
        assert len(restored.detections) == 1

        rd = restored.detections[0]
        assert rd.bbox == detection.bbox
        assert rd.confidence == detection.confidence
        assert rd.reid_embedding == detection.reid_embedding
        assert rd.face_embedding == detection.face_embedding
        assert rd.crop_jpeg == detection.crop_jpeg

    def test_round_trip_no_thumbnail(self):
        msg = DetectionMessage(timestamp=1.0, frame_id=1, thumbnail_jpeg=None)
        runner = EdgeRunner()
        data = runner.to_dict(msg)
        restored = EdgeRunner.from_dict(data)
        assert restored.thumbnail_jpeg is None

    def test_round_trip_no_detections(self):
        msg = DetectionMessage(
            timestamp=1.0,
            frame_id=1,
            thumbnail_jpeg=b"\xff\xd8thumb",
        )
        runner = EdgeRunner()
        data = runner.to_dict(msg)
        restored = EdgeRunner.from_dict(data)
        assert restored.detections == []

    def test_round_trip_none_embeddings(self):
        detection = Detection(
            bbox=(0, 0, 50, 50),
            confidence=0.5,
            reid_embedding=None,
            face_embedding=None,
            crop_jpeg=None,
        )
        msg = DetectionMessage(
            timestamp=1.0,
            frame_id=1,
            detections=[detection],
        )
        runner = EdgeRunner()
        data = runner.to_dict(msg)
        restored = EdgeRunner.from_dict(data)

        rd = restored.detections[0]
        assert rd.reid_embedding is None
        assert rd.face_embedding is None
        assert rd.crop_jpeg is None

    def test_to_dict_type_field(self):
        msg = DetectionMessage(timestamp=1.0, frame_id=1, thumbnail_jpeg=b"x")
        runner = EdgeRunner()
        data = runner.to_dict(msg)
        assert data["type"] == "detections"


class TestEdgeRunnerEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_frame(self):
        """A 1x1 frame should not crash."""
        runner = EdgeRunner()
        tiny = np.zeros((1, 1, 3), dtype=np.uint8)
        msg = runner.process_frame(tiny)
        assert msg.frame_id == 1
        assert msg.thumbnail_jpeg is not None


# ===========================================================================
# GroundStation tests
# ===========================================================================

class TestGroundStationNoTarget:
    """GroundStation with no target embeddings."""

    def test_returns_zero_scores(self):
        gs = GroundStation()
        msg = DetectionMessage(
            timestamp=time.time(),
            frame_id=1,
            detections=[
                Detection(bbox=(10, 20, 110, 220), confidence=0.8,
                          reid_embedding=[0.1, 0.2, 0.3],
                          face_embedding=[0.4, 0.5, 0.6]),
            ],
        )
        results = gs.process_message(msg)
        assert len(results) == 1
        assert results[0]["reid_score"] == 0.0
        assert results[0]["face_score"] == 0.0

    def test_empty_detections(self):
        gs = GroundStation()
        msg = DetectionMessage(timestamp=time.time(), frame_id=1)
        results = gs.process_message(msg)
        assert results == []


class TestGroundStationScoring:
    """GroundStation cosine similarity scoring."""

    def test_identical_embeddings_score_one(self):
        emb = _normalized([1.0, 0.0, 0.0, 0.0]).tolist()
        gs = GroundStation(target_reid_embedding=emb, target_face_embedding=emb)
        msg = DetectionMessage(
            timestamp=time.time(),
            frame_id=1,
            detections=[
                Detection(bbox=(0, 0, 50, 50), confidence=0.9,
                          reid_embedding=emb, face_embedding=emb),
            ],
        )
        results = gs.process_message(msg)
        assert len(results) == 1
        assert results[0]["reid_score"] == pytest.approx(1.0, abs=1e-6)
        assert results[0]["face_score"] == pytest.approx(1.0, abs=1e-6)

    def test_orthogonal_embeddings_score_zero(self):
        target = _normalized([1.0, 0.0, 0.0, 0.0]).tolist()
        candidate = _normalized([0.0, 1.0, 0.0, 0.0]).tolist()
        gs = GroundStation(target_reid_embedding=target)
        msg = DetectionMessage(
            timestamp=time.time(),
            frame_id=1,
            detections=[
                Detection(bbox=(0, 0, 50, 50), confidence=0.9,
                          reid_embedding=candidate),
            ],
        )
        results = gs.process_message(msg)
        assert results[0]["reid_score"] == pytest.approx(0.0, abs=1e-6)

    def test_known_cosine_similarity(self):
        """Verify cosine similarity for a known pair."""
        a = _normalized([3.0, 4.0]).tolist()
        b = _normalized([4.0, 3.0]).tolist()
        # cos(a, b) = (3*4 + 4*3) / (5 * 5) = 24/25 = 0.96
        gs = GroundStation(target_reid_embedding=a)
        msg = DetectionMessage(
            timestamp=time.time(),
            frame_id=1,
            detections=[
                Detection(bbox=(0, 0, 50, 50), confidence=0.9,
                          reid_embedding=b),
            ],
        )
        results = gs.process_message(msg)
        assert results[0]["reid_score"] == pytest.approx(0.96, abs=0.01)

    def test_negative_similarity_clamped_to_zero(self):
        target = _normalized([1.0, 0.0]).tolist()
        candidate = _normalized([-1.0, 0.0]).tolist()
        gs = GroundStation(target_reid_embedding=target)
        msg = DetectionMessage(
            timestamp=time.time(),
            frame_id=1,
            detections=[
                Detection(bbox=(0, 0, 50, 50), confidence=0.9,
                          reid_embedding=candidate),
            ],
        )
        results = gs.process_message(msg)
        assert results[0]["reid_score"] == 0.0


class TestGroundStationSetTarget:
    """set_target updates target embeddings."""

    def test_set_target_changes_scores(self):
        gs = GroundStation()
        emb = _normalized([1.0, 0.0, 0.0]).tolist()

        # Before setting target, score is 0
        msg = DetectionMessage(
            timestamp=time.time(),
            frame_id=1,
            detections=[
                Detection(bbox=(0, 0, 50, 50), confidence=0.9,
                          reid_embedding=emb),
            ],
        )
        results = gs.process_message(msg)
        assert results[0]["reid_score"] == 0.0

        # After setting target
        gs.set_target(reid_embedding=emb)
        results = gs.process_message(msg)
        assert results[0]["reid_score"] == pytest.approx(1.0, abs=1e-6)

    def test_set_target_clears_with_none(self):
        emb = _normalized([1.0, 0.0]).tolist()
        gs = GroundStation(target_reid_embedding=emb)
        gs.set_target(reid_embedding=None)
        msg = DetectionMessage(
            timestamp=time.time(),
            frame_id=1,
            detections=[
                Detection(bbox=(0, 0, 50, 50), confidence=0.9,
                          reid_embedding=emb),
            ],
        )
        results = gs.process_message(msg)
        assert results[0]["reid_score"] == 0.0


class TestGroundStationWithScorer:
    """Integration with MatchScorer."""

    def test_scorer_produces_combined_score(self):
        scorer = MatchScorer()
        target = _normalized([1.0, 0.0, 0.0, 0.0]).tolist()
        gs = GroundStation(
            scorer=scorer,
            target_reid_embedding=target,
            target_face_embedding=target,
        )
        msg = DetectionMessage(
            timestamp=time.time(),
            frame_id=1,
            detections=[
                Detection(bbox=(0, 0, 50, 50), confidence=0.9,
                          reid_embedding=target, face_embedding=target),
            ],
        )
        results = gs.process_message(msg)
        assert len(results) == 1
        sr = results[0]["score_result"]
        assert sr is not None
        assert "combined_score" in sr
        assert sr["combined_score"] > 0.5
        assert sr["is_match"] is True

    def test_scorer_no_match_when_orthogonal(self):
        scorer = MatchScorer()
        target = _normalized([1.0, 0.0, 0.0, 0.0]).tolist()
        candidate = _normalized([0.0, 1.0, 0.0, 0.0]).tolist()
        gs = GroundStation(
            scorer=scorer,
            target_reid_embedding=target,
            target_face_embedding=target,
        )
        msg = DetectionMessage(
            timestamp=time.time(),
            frame_id=1,
            detections=[
                Detection(bbox=(0, 0, 50, 50), confidence=0.9,
                          reid_embedding=candidate, face_embedding=candidate),
            ],
        )
        results = gs.process_message(msg)
        sr = results[0]["score_result"]
        assert sr["combined_score"] == 0.0
        assert sr["is_match"] is False


class TestGroundStationNoneEmbeddings:
    """Handles None embeddings gracefully."""

    def test_none_reid_embedding(self):
        target = _normalized([1.0, 0.0]).tolist()
        gs = GroundStation(target_reid_embedding=target)
        msg = DetectionMessage(
            timestamp=time.time(),
            frame_id=1,
            detections=[
                Detection(bbox=(0, 0, 50, 50), confidence=0.9,
                          reid_embedding=None, face_embedding=None),
            ],
        )
        results = gs.process_message(msg)
        assert results[0]["reid_score"] == 0.0
        assert results[0]["face_score"] == 0.0

    def test_none_face_with_valid_reid(self):
        emb = _normalized([1.0, 0.0]).tolist()
        gs = GroundStation(target_reid_embedding=emb)
        msg = DetectionMessage(
            timestamp=time.time(),
            frame_id=1,
            detections=[
                Detection(bbox=(0, 0, 50, 50), confidence=0.9,
                          reid_embedding=emb, face_embedding=None),
            ],
        )
        results = gs.process_message(msg)
        assert results[0]["reid_score"] == pytest.approx(1.0, abs=1e-6)
        assert results[0]["face_score"] == 0.0


# ===========================================================================
# End-to-end round-trip tests
# ===========================================================================

class TestRoundTrip:
    """EdgeRunner produces -> serialize -> deserialize -> GroundStation scores."""

    def test_full_pipeline(self):
        frame = _make_frame()
        reid_emb = _normalized([1.0, 0.0, 0.0, 0.0])
        face_emb = _normalized([0.0, 1.0, 0.0, 0.0])

        dets = [{"bbox": [10, 20, 110, 220], "confidence": 0.85, "crop": frame[20:220, 10:110]}]
        runner = EdgeRunner(
            detector=_make_mock_detector(dets),
            reid=_make_mock_reid(reid_emb),
            face=_make_mock_face(face_emb),
        )

        # Edge: produce message
        msg = runner.process_frame(frame)
        assert len(msg.detections) == 1

        # Serialize and deserialize (simulates network transport)
        data = runner.to_dict(msg)
        restored = EdgeRunner.from_dict(data)

        # Ground: process with matching target
        target_reid = reid_emb.tolist()
        target_face = face_emb.tolist()
        gs = GroundStation(
            target_reid_embedding=target_reid,
            target_face_embedding=target_face,
        )
        results = gs.process_message(restored)

        assert len(results) == 1
        assert results[0]["reid_score"] == pytest.approx(1.0, abs=1e-6)
        assert results[0]["face_score"] == pytest.approx(1.0, abs=1e-6)
        assert results[0]["frame_id"] == 1
        assert results[0]["bbox"] == (10, 20, 110, 220)

    def test_full_pipeline_with_scorer(self):
        frame = _make_frame()
        reid_emb = _normalized([1.0, 0.0, 0.0, 0.0])
        face_emb = _normalized([0.0, 1.0, 0.0, 0.0])

        dets = [{"bbox": [10, 20, 110, 220], "confidence": 0.85, "crop": frame[20:220, 10:110]}]
        runner = EdgeRunner(
            detector=_make_mock_detector(dets),
            reid=_make_mock_reid(reid_emb),
            face=_make_mock_face(face_emb),
        )

        msg = runner.process_frame(frame)
        data = runner.to_dict(msg)
        restored = EdgeRunner.from_dict(data)

        scorer = MatchScorer()
        gs = GroundStation(
            scorer=scorer,
            target_reid_embedding=reid_emb.tolist(),
            target_face_embedding=face_emb.tolist(),
        )
        results = gs.process_message(restored)

        assert len(results) == 1
        sr = results[0]["score_result"]
        assert sr is not None
        assert sr["is_match"] is True
        assert sr["combined_score"] > 0.8

    def test_pipeline_no_match(self):
        """Orthogonal embeddings through the full pipeline produce no match."""
        frame = _make_frame()
        reid_emb = _normalized([1.0, 0.0, 0.0, 0.0])
        face_emb = _normalized([0.0, 1.0, 0.0, 0.0])

        dets = [{"bbox": [10, 20, 110, 220], "confidence": 0.85, "crop": frame[20:220, 10:110]}]
        runner = EdgeRunner(
            detector=_make_mock_detector(dets),
            reid=_make_mock_reid(reid_emb),
            face=_make_mock_face(face_emb),
        )

        msg = runner.process_frame(frame)
        data = runner.to_dict(msg)
        restored = EdgeRunner.from_dict(data)

        # Target is orthogonal to detection embeddings
        target_reid = _normalized([0.0, 0.0, 1.0, 0.0]).tolist()
        target_face = _normalized([0.0, 0.0, 0.0, 1.0]).tolist()
        scorer = MatchScorer()
        gs = GroundStation(
            scorer=scorer,
            target_reid_embedding=target_reid,
            target_face_embedding=target_face,
        )
        results = gs.process_message(restored)

        assert len(results) == 1
        assert results[0]["reid_score"] == pytest.approx(0.0, abs=1e-6)
        assert results[0]["face_score"] == pytest.approx(0.0, abs=1e-6)
        sr = results[0]["score_result"]
        assert sr["is_match"] is False
