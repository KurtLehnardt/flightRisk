"""Integration tests for PersonDetector (YOLO)."""

import numpy as np
import pytest

from amber.vision.detector import PersonDetector

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def detector():
    """Shared detector instance — model loading is slow."""
    return PersonDetector(model_name="yolo11n.pt", confidence=0.4)


class TestPersonDetectorInit:
    def test_initializes_without_error(self, detector):
        assert detector is not None
        assert detector.model is not None


class TestDetect:
    def test_random_noise_returns_list(self, detector, sample_frame):
        result = detector.detect(sample_frame)
        assert isinstance(result, list)

    def test_solid_color_returns_empty(self, detector, solid_blue_frame):
        result = detector.detect(solid_blue_frame)
        assert isinstance(result, list)
        assert len(result) == 0

    def test_detection_dicts_have_required_keys(self, detector):
        """If detections are found, they must have bbox, confidence, crop."""
        # Use a larger frame with some structure to increase chance of detection
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        result = detector.detect(frame)
        # Even if empty, structure is correct
        for det in result:
            assert "bbox" in det, "Detection missing 'bbox' key"
            assert "confidence" in det, "Detection missing 'confidence' key"
            assert "crop" in det, "Detection missing 'crop' key"
            assert isinstance(det["bbox"], list)
            assert len(det["bbox"]) == 4
            assert isinstance(det["confidence"], float)
            assert isinstance(det["crop"], np.ndarray)


class TestAnnotate:
    def test_returns_ndarray_same_shape(self, detector, sample_frame):
        detections = detector.detect(sample_frame)
        annotated = detector.annotate(sample_frame, detections)
        assert isinstance(annotated, np.ndarray)
        assert annotated.shape == sample_frame.shape

    def test_none_match_idx_no_crash(self, detector, sample_frame):
        detections = detector.detect(sample_frame)
        annotated = detector.annotate(sample_frame, detections, match_idx=None)
        assert isinstance(annotated, np.ndarray)

    def test_valid_match_idx_highlights(self, detector):
        """When match_idx is given, that box is drawn green."""
        # Create a fake detection to guarantee we have something to annotate
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        fake_detections = [
            {
                "bbox": [100, 100, 200, 200],
                "confidence": 0.95,
                "crop": frame[100:200, 100:200],
            }
        ]
        annotated = detector.annotate(frame, fake_detections, match_idx=0)
        assert isinstance(annotated, np.ndarray)
        # Check that the pixel at the top-left corner of the bbox is green (BGR: 0, 255, 0)
        # The rectangle is drawn with thickness 3, so check just inside the border
        pixel = annotated[100, 100]
        assert pixel[1] == 255, f"Expected green channel 255, got {pixel[1]}"
        assert pixel[0] == 0, f"Expected blue channel 0, got {pixel[0]}"
        assert pixel[2] == 0, f"Expected red channel 0, got {pixel[2]}"

    def test_empty_detections_returns_copy(self, detector, sample_frame):
        annotated = detector.annotate(sample_frame, [], match_idx=None)
        assert isinstance(annotated, np.ndarray)
        assert annotated.shape == sample_frame.shape
        # Should be a copy, not the same object
        assert annotated is not sample_frame
