"""Ground compute: scoring, tracking, reasoning, and alerting.

Consumes DetectionMessages from the EdgeRunner (locally or over WebSocket)
and produces match decisions.
"""
import numpy as np
from typing import Any

from amber.edge import DetectionMessage, Detection


class GroundStation:
    """Processes detection messages and produces match scores."""

    def __init__(self, scorer=None, tracker=None, target_reid_embedding=None, target_face_embedding=None):
        self._scorer = scorer
        self._tracker = tracker
        self._target_reid = np.array(target_reid_embedding) if target_reid_embedding else None
        self._target_face = np.array(target_face_embedding) if target_face_embedding else None

    def set_target(self, reid_embedding: list[float] | None = None, face_embedding: list[float] | None = None):
        self._target_reid = np.array(reid_embedding) if reid_embedding else None
        self._target_face = np.array(face_embedding) if face_embedding else None

    def process_message(self, msg: DetectionMessage) -> list[dict[str, Any]]:
        """Process a detection message, return scored results."""
        results = []

        for det in msg.detections:
            reid_score = 0.0
            face_score = 0.0

            # Compare ReID embeddings
            if det.reid_embedding is not None and self._target_reid is not None:
                candidate = np.array(det.reid_embedding)
                reid_score = float(np.dot(self._target_reid, candidate) / (
                    np.linalg.norm(self._target_reid) * np.linalg.norm(candidate) + 1e-8
                ))
                reid_score = max(0.0, reid_score)

            # Compare face embeddings
            if det.face_embedding is not None and self._target_face is not None:
                candidate = np.array(det.face_embedding)
                face_score = float(np.dot(self._target_face, candidate) / (
                    np.linalg.norm(self._target_face) * np.linalg.norm(candidate) + 1e-8
                ))
                face_score = max(0.0, face_score)

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
