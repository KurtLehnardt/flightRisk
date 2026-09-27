"""Configurable matching signal providers.

Each signal provider extracts a specific feature from person crops and
compares them against a target reference. Signals are registered with
:class:`~flightrisk.vision.scorer.MatchScorer` via ``register_signal()``
and passed as keyword arguments to ``score()``.

Signal providers follow a common pattern:
    - ``set_target(image)`` to set the reference photo
    - ``compare(crop)`` to compare a detection crop against the target
    - ``available`` property to check if the provider is ready

Pure-CV signals (clothing_color, height_ratio) are always available.
ONNX model signals (osnet_reid, insightface_face) gracefully degrade
to unavailable when their model files are missing.
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. HSV Color Histogram — clothing_color
# ---------------------------------------------------------------------------

class ClothingColorSignal:
    """HSV color histogram comparison for clothing appearance matching.

    Splits person crops into upper body (top 40%) and lower body (bottom
    40%) regions, computes normalized HSV histograms, and compares them
    using correlation.
    """

    def __init__(self, h_bins: int = 30, s_bins: int = 32) -> None:
        self.h_bins = h_bins
        self.s_bins = s_bins
        self._target_upper_hist: np.ndarray | None = None
        self._target_lower_hist: np.ndarray | None = None

    @property
    def available(self) -> bool:
        """Always available -- pure OpenCV, no model needed."""
        return True

    @property
    def has_target(self) -> bool:
        return self._target_upper_hist is not None

    def _compute_histograms(
        self, image: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Compute normalized HSV histograms for upper and lower body.

        Args:
            image: BGR numpy array of a person crop.

        Returns:
            (upper_hist, lower_hist) -- each a 1-D normalized histogram.
        """
        h, w = image.shape[:2]
        upper = image[: int(h * 0.4), :]
        lower = image[int(h * 0.6) :, :]

        hsv_upper = cv2.cvtColor(upper, cv2.COLOR_BGR2HSV)
        hsv_lower = cv2.cvtColor(lower, cv2.COLOR_BGR2HSV)

        upper_hist = cv2.calcHist(
            [hsv_upper], [0, 1], None, [self.h_bins, self.s_bins],
            [0, 180, 0, 256],
        )
        lower_hist = cv2.calcHist(
            [hsv_lower], [0, 1], None, [self.h_bins, self.s_bins],
            [0, 180, 0, 256],
        )

        cv2.normalize(upper_hist, upper_hist)
        cv2.normalize(lower_hist, lower_hist)
        return upper_hist, lower_hist

    def set_target(self, image: np.ndarray) -> None:
        """Set the reference image for color comparison.

        Args:
            image: BGR numpy array -- a photo of the target person.
        """
        self._target_upper_hist, self._target_lower_hist = (
            self._compute_histograms(image)
        )
        logger.info("clothing_color target set")

    def clear_target(self) -> None:
        self._target_upper_hist = None
        self._target_lower_hist = None

    def compare(self, crop: np.ndarray) -> float:
        """Compare a detection crop's color histogram against the target.

        Args:
            crop: BGR numpy array of a detected person.

        Returns:
            Average correlation score (0-1). Higher = more similar.
        """
        if self._target_upper_hist is None or crop is None or crop.size == 0:
            return 0.0

        try:
            det_upper, det_lower = self._compute_histograms(crop)
            upper_score = cv2.compareHist(
                self._target_upper_hist, det_upper, cv2.HISTCMP_CORREL
            )
            lower_score = cv2.compareHist(
                self._target_lower_hist, det_lower, cv2.HISTCMP_CORREL
            )
            # Correlation ranges [-1, 1]; clamp to [0, 1]
            avg = (max(0.0, upper_score) + max(0.0, lower_score)) / 2.0
            return float(min(1.0, avg))
        except Exception:
            logger.warning("clothing_color_compare_failed", exc_info=True)
            return 0.0


# ---------------------------------------------------------------------------
# 2. Bbox Height Ratio — height_ratio
# ---------------------------------------------------------------------------

class HeightRatioSignal:
    """Bounding box aspect ratio comparison.

    Compares the target photo's person bbox aspect ratio (height/width)
    against each detection's aspect ratio. This is a lightweight proxy
    for body proportions and is most useful as a weak signal to
    corroborate stronger ones.
    """

    def __init__(self) -> None:
        self._target_ratio: float | None = None

    @property
    def available(self) -> bool:
        """Always available -- pure math, no model needed."""
        return True

    @property
    def has_target(self) -> bool:
        return self._target_ratio is not None

    def set_target(self, image: np.ndarray) -> None:
        """Set the target ratio from a reference image.

        Args:
            image: BGR numpy array -- the full person crop from the
                   reference photo.
        """
        h, w = image.shape[:2]
        if w > 0:
            self._target_ratio = h / w
        else:
            self._target_ratio = None
        logger.info("height_ratio target set (ratio=%.2f)", self._target_ratio or 0)

    def clear_target(self) -> None:
        self._target_ratio = None

    def compare(self, crop: np.ndarray) -> float:
        """Compare a detection crop's aspect ratio against the target.

        Args:
            crop: BGR numpy array of a detected person.

        Returns:
            Score in [0, 1]. 1.0 = identical ratio.
        """
        if self._target_ratio is None or crop is None or crop.size == 0:
            return 0.0

        h, w = crop.shape[:2]
        if w == 0:
            return 0.0
        det_ratio = h / w
        max_ratio = max(self._target_ratio, det_ratio)
        if max_ratio == 0:
            return 0.0
        score = 1.0 - abs(self._target_ratio - det_ratio) / max_ratio
        return float(max(0.0, min(1.0, score)))


# ---------------------------------------------------------------------------
# 3. OSNet Person ReID — osnet_reid
# ---------------------------------------------------------------------------

_DEFAULT_OSNET_PATH = "models/osnet_x1_0.onnx"

# ImageNet normalization constants
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


class OSNetReIDSignal:
    """OSNet x1.0 person re-identification via ONNX inference.

    Uses a 256x128 input and produces 512-d person appearance
    embeddings. Compared via cosine similarity.

    If the ONNX model file is missing, the signal marks itself as
    unavailable and all comparisons return 0.0.
    """

    def __init__(self, model_path: str | None = None) -> None:
        self._model_path = model_path or _DEFAULT_OSNET_PATH
        self._session = None
        self._input_name: str | None = None
        self._target_embedding: np.ndarray | None = None
        self._available = False
        self._load_model()

    def _load_model(self) -> None:
        path = Path(self._model_path)
        if not path.exists():
            logger.warning(
                "osnet_reid model not found at %s -- signal unavailable",
                self._model_path,
            )
            return

        try:
            import onnxruntime as ort

            self._session = ort.InferenceSession(
                str(path),
                providers=["CoreMLExecutionProvider", "CPUExecutionProvider"],
            )
            self._input_name = self._session.get_inputs()[0].name
            self._available = True
            logger.info("osnet_reid loaded from %s", self._model_path)
        except Exception:
            logger.warning("osnet_reid failed to load", exc_info=True)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def has_target(self) -> bool:
        return self._target_embedding is not None

    def _preprocess(self, image: np.ndarray) -> np.ndarray:
        """Resize and normalize a person crop for OSNet input.

        Args:
            image: BGR numpy array.

        Returns:
            Float32 array of shape (1, 3, 256, 128).
        """
        resized = cv2.resize(image, (128, 256))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        normalized = (rgb - _IMAGENET_MEAN) / _IMAGENET_STD
        # HWC -> CHW -> NCHW
        chw = np.transpose(normalized, (2, 0, 1))
        return np.expand_dims(chw, axis=0).astype(np.float32)

    def _extract_embedding(self, image: np.ndarray) -> np.ndarray | None:
        """Run inference to get a 512-d embedding.

        Args:
            image: BGR numpy array of a person crop.

        Returns:
            L2-normalized 512-d feature vector, or None on failure.
        """
        if not self._available or self._session is None:
            return None
        try:
            blob = self._preprocess(image)
            outputs = self._session.run(None, {self._input_name: blob})
            embedding = outputs[0].flatten()
            norm = np.linalg.norm(embedding)
            if norm > 0:
                embedding = embedding / norm
            return embedding
        except Exception:
            logger.warning("osnet_reid_embedding_failed", exc_info=True)
            return None

    def set_target(self, image: np.ndarray) -> bool:
        """Set the reference person image.

        Args:
            image: BGR numpy array of the target person.

        Returns:
            True if embedding was extracted successfully.
        """
        emb = self._extract_embedding(image)
        if emb is not None:
            self._target_embedding = emb
            logger.info("osnet_reid target set (%d-d)", len(emb))
            return True
        logger.warning("osnet_reid failed to set target")
        return False

    def clear_target(self) -> None:
        self._target_embedding = None

    def compare(self, crop: np.ndarray) -> float:
        """Compare a detection crop against the target.

        Args:
            crop: BGR numpy array of a detected person.

        Returns:
            Cosine similarity (0-1).
        """
        if self._target_embedding is None or crop is None or crop.size == 0:
            return 0.0
        emb = self._extract_embedding(crop)
        if emb is None:
            return 0.0
        similarity = float(np.dot(self._target_embedding, emb))
        return max(0.0, similarity)


# ---------------------------------------------------------------------------
# 4. InsightFace R18 — insightface_face
# ---------------------------------------------------------------------------

_DEFAULT_INSIGHTFACE_R18_PATH = "models/insightface_r18.onnx"


class InsightFaceR18Signal:
    """InsightFace buffalo_sc (R18) face recognition via ONNX inference.

    Uses a 112x112 aligned face crop and produces 512-d face embeddings.
    Compared via cosine similarity.

    This is a lighter-weight alternative to the full InsightFace
    ``FaceAnalysis`` pipeline used by :class:`FaceRecognizer` -- it runs
    the recognition model directly on pre-cropped face regions without
    the InsightFace dependency's detection step.

    If the ONNX model file is missing, the signal marks itself as
    unavailable and all comparisons return 0.0.
    """

    def __init__(self, model_path: str | None = None) -> None:
        self._model_path = model_path or _DEFAULT_INSIGHTFACE_R18_PATH
        self._session = None
        self._input_name: str | None = None
        self._target_embedding: np.ndarray | None = None
        self._available = False
        self._load_model()

    def _load_model(self) -> None:
        path = Path(self._model_path)
        if not path.exists():
            logger.warning(
                "insightface_r18 model not found at %s -- signal unavailable",
                self._model_path,
            )
            return

        try:
            import onnxruntime as ort

            self._session = ort.InferenceSession(
                str(path),
                providers=["CoreMLExecutionProvider", "CPUExecutionProvider"],
            )
            self._input_name = self._session.get_inputs()[0].name
            self._available = True
            logger.info("insightface_r18 loaded from %s", self._model_path)
        except Exception:
            logger.warning("insightface_r18 failed to load", exc_info=True)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def has_target(self) -> bool:
        return self._target_embedding is not None

    def _preprocess(self, image: np.ndarray) -> np.ndarray:
        """Align and normalize a face crop for InsightFace R18.

        Args:
            image: BGR numpy array (ideally an aligned face crop).

        Returns:
            Float32 array of shape (1, 3, 112, 112).
        """
        resized = cv2.resize(image, (112, 112))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32)
        # Normalize to [-1, 1] (InsightFace convention)
        normalized = (rgb - 127.5) / 127.5
        # HWC -> CHW -> NCHW
        chw = np.transpose(normalized, (2, 0, 1))
        return np.expand_dims(chw, axis=0).astype(np.float32)

    def _extract_embedding(self, image: np.ndarray) -> np.ndarray | None:
        """Run inference to get a 512-d face embedding.

        Args:
            image: BGR numpy array of a face crop.

        Returns:
            L2-normalized 512-d embedding, or None on failure.
        """
        if not self._available or self._session is None:
            return None
        try:
            blob = self._preprocess(image)
            outputs = self._session.run(None, {self._input_name: blob})
            embedding = outputs[0].flatten()
            norm = np.linalg.norm(embedding)
            if norm > 0:
                embedding = embedding / norm
            return embedding
        except Exception:
            logger.warning("insightface_r18_embedding_failed", exc_info=True)
            return None

    def set_target(self, image: np.ndarray) -> bool:
        """Set the reference face image.

        Args:
            image: BGR numpy array containing the target face.

        Returns:
            True if embedding was extracted successfully.
        """
        emb = self._extract_embedding(image)
        if emb is not None:
            self._target_embedding = emb
            logger.info("insightface_r18 target set (%d-d)", len(emb))
            return True
        logger.warning("insightface_r18 failed to set target")
        return False

    def clear_target(self) -> None:
        self._target_embedding = None

    def compare(self, crop: np.ndarray) -> float:
        """Compare a face crop against the target.

        Args:
            crop: BGR numpy array of a detected face.

        Returns:
            Cosine similarity (0-1).
        """
        if self._target_embedding is None or crop is None or crop.size == 0:
            return 0.0
        emb = self._extract_embedding(crop)
        if emb is None:
            return 0.0
        similarity = float(np.dot(self._target_embedding, emb))
        return max(0.0, similarity)
