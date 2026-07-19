"""Face recognition using InsightFace / ArcFace.

Extracts 512-d face embeddings from detected person crops and compares
them against a reference photo. Complements full-body ReID for higher
confidence matching, especially when clothing changes.

Requires: pip install insightface onnxruntime
"""

import numpy as np
import cv2

try:
    from insightface.app import FaceAnalysis
    HAS_INSIGHTFACE = True
except ImportError:
    HAS_INSIGHTFACE = False


class FaceRecognizer:
    """Face detection + ArcFace embedding extraction and matching."""

    def __init__(self, match_threshold: float = 0.45, det_size: tuple[int, int] = (640, 640)):
        """Initialize InsightFace.

        Args:
            match_threshold: Cosine similarity threshold for face match (0-1).
            det_size: Detection input size. Smaller = faster but less accurate
                      on small faces. (320, 320) for speed, (640, 640) for quality.
        """
        if not HAS_INSIGHTFACE:
            raise RuntimeError("pip install insightface onnxruntime")

        self.match_threshold = match_threshold
        self._target_embedding: np.ndarray | None = None

        # Initialize with buffalo_l (includes detection + recognition)
        self.app = FaceAnalysis(
            name="buffalo_l",
            providers=["CoreMLExecutionProvider", "CPUExecutionProvider"],
        )
        self.app.prepare(ctx_id=0, det_size=det_size)
        print(f"[face] InsightFace (ArcFace) ready, det_size={det_size}")

    def _get_faces(self, image: np.ndarray) -> list:
        """Detect faces and extract embeddings from an image.

        Args:
            image: BGR numpy array.

        Returns:
            List of face objects with .embedding (512-d), .bbox, etc.
        """
        # InsightFace expects BGR (same as OpenCV default)
        faces = self.app.get(image)
        return faces

    def _best_face_embedding(self, image: np.ndarray) -> np.ndarray | None:
        """Get the embedding of the largest face in an image."""
        faces = self._get_faces(image)
        if not faces:
            return None

        # Pick the largest face by bounding box area
        best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        emb = best.embedding
        # L2 normalize
        norm = np.linalg.norm(emb)
        if norm > 0:
            emb = emb / norm
        return emb

    def set_target(self, image: np.ndarray) -> bool:
        """Set the reference face from a photo of the target child.

        Args:
            image: BGR numpy array — a photo containing the child's face.

        Returns:
            True if a face was found and embedding set, False otherwise.
        """
        emb = self._best_face_embedding(image)
        if emb is not None:
            self._target_embedding = emb
            print(f"[face] Target face embedding set (512-d)")
            return True
        print("[face] No face detected in reference photo")
        return False

    def set_target_from_file(self, path: str) -> bool:
        """Load a reference face from disk."""
        image = cv2.imread(path)
        if image is None:
            raise FileNotFoundError(f"Cannot read image: {path}")
        return self.set_target(image)

    def compare(self, crop: np.ndarray) -> float:
        """Compare a detected person crop's face against the target.

        Args:
            crop: BGR numpy array of a detected person.

        Returns:
            Cosine similarity (0-1), or 0.0 if no face found in crop.
        """
        if self._target_embedding is None:
            return 0.0

        emb = self._best_face_embedding(crop)
        if emb is None:
            return 0.0

        similarity = float(np.dot(self._target_embedding, emb))
        return max(0.0, similarity)

    def find_match(self, detections: list[dict]) -> tuple[int | None, float]:
        """Find the best face match among detected persons.

        Args:
            detections: List of dicts with 'crop' key from PersonDetector.

        Returns:
            (index, score) of best face match, or (None, 0.0) if none.
        """
        if self._target_embedding is None or not detections:
            return None, 0.0

        best_idx = None
        best_score = 0.0

        for i, det in enumerate(detections):
            crop = det.get("crop")
            if crop is None or crop.size == 0:
                continue
            score = self.compare(crop)
            if score > best_score:
                best_score = score
                best_idx = i

        if best_score >= self.match_threshold:
            return best_idx, best_score

        return None, best_score

    @property
    def has_target(self) -> bool:
        return self._target_embedding is not None
