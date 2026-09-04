"""Input image quality scorer.

Evaluates target photos for blur, brightness, contrast, resolution,
face presence, and aspect ratio using OpenCV-only metrics. Produces
a QualityReport with an overall score, grade, and actionable feedback
so operators know whether a reference photo is usable before starting
a search.
"""

from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class QualityReport:
    """Result of scoring an input image for search suitability."""

    overall_score: float        # 0.0-1.0
    grade: str                  # "good", "fair", "poor"
    dimensions: dict[str, dict] # {name: {score, threshold, passed, raw_value}}
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


class ImageQualityScorer:
    """Scores input images across multiple quality dimensions.

    Uses only OpenCV operations — no extra dependencies required.
    """

    # Dimension weights (must sum to 1.0)
    WEIGHTS: dict[str, float] = {
        "blur": 0.25,
        "brightness": 0.15,
        "contrast": 0.15,
        "resolution": 0.20,
        "face": 0.20,
        "aspect_ratio": 0.05,
    }

    def __init__(self) -> None:
        """Initialize the scorer with OpenCV face detection.

        Uses CascadeClassifier on OpenCV < 5, FaceDetectorYN on OpenCV >= 5.
        Falls back to a stub if neither is available.
        """
        self._face_cascade = None
        self._face_detector_yn = None

        # Try legacy Haar cascade (OpenCV 4.x)
        if hasattr(cv2, "CascadeClassifier"):
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            self._face_cascade = cv2.CascadeClassifier(cascade_path)
        # Try FaceDetectorYN (OpenCV 5.x) -- requires model file
        elif hasattr(cv2, "FaceDetectorYN"):
            try:
                model_path = str(
                    cv2.data.haarcascades + "face_detection_yunet_2023mar.onnx"
                )
                # FaceDetectorYN needs a model file; if absent we skip
                self._face_detector_yn = cv2.FaceDetectorYN.create(
                    model_path, "", (320, 320),
                )
            except Exception:
                pass  # Model file not available; face dim will use stub

    def score(self, image: np.ndarray) -> QualityReport:
        """Score an image across all quality dimensions.

        Args:
            image: BGR numpy array from OpenCV (or grayscale).

        Returns:
            QualityReport with overall score, grade, per-dimension details,
            human-readable issues and suggestions.
        """
        h, w = image.shape[:2]

        # Convert to grayscale once
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image

        dimensions: dict[str, dict] = {}

        # 1. Blur (Laplacian variance)
        variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        blur_score = min(1.0, variance / 500.0)
        dimensions["blur"] = {
            "score": round(blur_score, 3),
            "threshold": 100,
            "passed": variance >= 100,
            "raw_value": round(variance, 2),
        }

        # 2. Brightness (mean pixel value)
        mean_val = float(gray.mean())
        brightness_score = max(0.0, 1.0 - abs(mean_val - 130.0) / 130.0)
        dimensions["brightness"] = {
            "score": round(brightness_score, 3),
            "threshold": "60-200",
            "passed": 60 <= mean_val <= 200,
            "raw_value": round(mean_val, 2),
        }

        # 3. Contrast (standard deviation)
        stdev = float(gray.std())
        contrast_score = min(1.0, stdev / 60.0)
        dimensions["contrast"] = {
            "score": round(contrast_score, 3),
            "threshold": 30,
            "passed": stdev >= 30,
            "raw_value": round(stdev, 2),
        }

        # 4. Resolution (minimum dimension)
        min_dim = min(w, h)
        resolution_score = min(1.0, min_dim / 400.0)
        dimensions["resolution"] = {
            "score": round(resolution_score, 3),
            "threshold": 400,
            "passed": min_dim >= 400,
            "raw_value": min_dim,
        }

        # 5. Face detection
        face_found = False
        if min_dim >= 20:
            if self._face_cascade is not None:
                faces = self._face_cascade.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=5, minSize=(20, 20),
                )
                face_found = len(faces) > 0
            elif self._face_detector_yn is not None:
                try:
                    self._face_detector_yn.setInputSize((w, h))
                    _, faces = self._face_detector_yn.detect(image)
                    face_found = faces is not None and len(faces) > 0
                except Exception:
                    pass
        face_score = 1.0 if face_found else 0.3
        dimensions["face"] = {
            "score": face_score,
            "threshold": "face detected",
            "passed": face_found,
            "raw_value": int(face_found),
        }

        # 6. Aspect ratio
        if h == 0 or w == 0:
            aspect_score = 0.0
            ratio = 0.0
        else:
            ratio = max(w, h) / max(min(w, h), 1)
            if ratio <= 3.0:
                aspect_score = 1.0
            else:
                aspect_score = max(0.0, 1.0 - (ratio - 3.0) / 7.0)
        dimensions["aspect_ratio"] = {
            "score": round(aspect_score, 3),
            "threshold": "1:3 to 3:1",
            "passed": ratio <= 3.0,
            "raw_value": round(ratio, 2),
        }

        # Weighted average
        overall = sum(
            dimensions[dim]["score"] * weight
            for dim, weight in self.WEIGHTS.items()
        )
        overall = round(min(1.0, max(0.0, overall)), 3)

        # Grade
        if overall >= 0.7:
            grade = "good"
        elif overall >= 0.4:
            grade = "fair"
        else:
            grade = "poor"

        # Issues and suggestions
        issues: list[str] = []
        suggestions: list[str] = []

        if not dimensions["blur"]["passed"]:
            issues.append("Image is blurry")
            suggestions.append("Use a sharper photo with less motion blur")

        if mean_val < 60:
            issues.append("Image is too dark")
            suggestions.append("Use a brighter photo or increase exposure")
        elif mean_val > 200:
            issues.append("Image is too bright / washed out")
            suggestions.append("Use a photo with less glare or overexposure")

        if not dimensions["contrast"]["passed"]:
            issues.append("Image has low contrast")
            suggestions.append("Use a photo with more distinct features")

        if not dimensions["resolution"]["passed"]:
            issues.append(f"Resolution too low ({min_dim}px)")
            suggestions.append("Use a higher-resolution photo (at least 400px)")

        if not face_found:
            issues.append("No face detected")
            suggestions.append("Use a photo showing the child's face clearly")

        if not dimensions["aspect_ratio"]["passed"]:
            issues.append("Extreme aspect ratio")
            suggestions.append("Crop the photo closer to a square or 4:3 ratio")

        return QualityReport(
            overall_score=overall,
            grade=grade,
            dimensions=dimensions,
            issues=issues,
            suggestions=suggestions,
        )
