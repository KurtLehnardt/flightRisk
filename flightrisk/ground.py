"""Ground compute: scoring, tracking, reasoning, and alerting.

Consumes DetectionMessages from the EdgeRunner (locally or over WebSocket)
and produces match decisions.
"""
import logging
import numpy as np
from typing import Any

from flightrisk.edge import DetectionMessage

logger = logging.getLogger(__name__)


class GroundStation:
    """Processes detection messages and produces match scores."""

    def __init__(self, scorer=None, target_reid_embedding=None, target_face_embedding=None):
        self._scorer = scorer
        # Use `is not None` rather than truthiness — target_*_embedding may be a
        # numpy array, whose truthiness is ambiguous for arrays with >1 element
        # and raises ValueError.
        self._target_reid = np.array(target_reid_embedding) if target_reid_embedding is not None else None
        self._target_face = np.array(target_face_embedding) if target_face_embedding is not None else None

    def set_target(self, reid_embedding: list[float] | None = None, face_embedding: list[float] | None = None):
        self._target_reid = np.array(reid_embedding) if reid_embedding is not None else None
        self._target_face = np.array(face_embedding) if face_embedding is not None else None

    def process_message(self, msg: DetectionMessage) -> list[dict[str, Any]]:
        """Process a detection message, return scored results."""
        results = []

        for det in msg.detections:
            reid_score = 0.0
            face_score = 0.0

            # Compare ReID embeddings. A shape mismatch (or any other bad
            # embedding data) between the target and a single detection
            # should not abort scoring for the rest of the batch.
            if det.reid_embedding is not None and self._target_reid is not None:
                try:
                    candidate = np.array(det.reid_embedding)
                    reid_score = float(np.dot(self._target_reid, candidate) / (
                        np.linalg.norm(self._target_reid) * np.linalg.norm(candidate) + 1e-8
                    ))
                    reid_score = max(0.0, reid_score)
                except Exception:
                    logger.warning("reid_similarity_failed", exc_info=True)
                    reid_score = 0.0

            # Compare face embeddings
            if det.face_embedding is not None and self._target_face is not None:
                try:
                    candidate = np.array(det.face_embedding)
                    face_score = float(np.dot(self._target_face, candidate) / (
                        np.linalg.norm(self._target_face) * np.linalg.norm(candidate) + 1e-8
                    ))
                    face_score = max(0.0, face_score)
                except Exception:
                    logger.warning("face_similarity_failed", exc_info=True)
                    face_score = 0.0

            # Score
            score_result = None
            if self._scorer:
                score_result = self._scorer.score(reid_score=reid_score, face_score=face_score)

            results.append({
                "bbox": det.bbox,
                "confidence": det.confidence,
                "reid_score": reid_score,
                "face_score": face_score,
                "score_result": score_result,
                "frame_id": msg.frame_id,
                "timestamp": msg.timestamp,
            })

        return results
