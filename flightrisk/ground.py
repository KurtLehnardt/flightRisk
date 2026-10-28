"""Ground compute: scoring, tracking, reasoning, and alerting.

Consumes DetectionMessages from the EdgeRunner (locally or over WebSocket)
and produces match decisions.
"""
import logging
import numpy as np
from typing import Any

from flightrisk.edge import DetectionMessage
from flightrisk.vision.profiles import weight_scale_for

logger = logging.getLogger(__name__)


class GroundStation:
    """Processes detection messages and produces match scores."""

    def __init__(self, scorer=None, target_reid_embedding=None, target_face_embedding=None,
                 face_model: str | None = None):
        self._scorer = scorer
        # Face model pack of the ground's own recogniser (the one the target
        # embedding came from). Edge messages naming a different model are
        # not scored on face: embeddings from different models are not
        # comparable.
        self._face_model = face_model
        self._warned_face_models: set = set()
        # Use `is not None` rather than truthiness — target_*_embedding may be a
        # numpy array, whose truthiness is ambiguous for arrays with >1 element
        # and raises ValueError.
        self._target_reid = np.array(target_reid_embedding) if target_reid_embedding is not None else None
        self._target_face = np.array(target_face_embedding) if target_face_embedding is not None else None

    def set_target(self, reid_embedding: list[float] | None = None, face_embedding: list[float] | None = None):
        self._target_reid = np.array(reid_embedding) if reid_embedding is not None else None
        self._target_face = np.array(face_embedding) if face_embedding is not None else None

    def _face_model_mismatch(self, msg) -> bool:
        """True (and one ERROR per drone/model pair) if the edge used another face model."""
        edge_model = getattr(msg, "face_model", None)
        if not edge_model or not self._face_model or edge_model == self._face_model:
            return False
        key = (getattr(msg, "drone_id", None), edge_model)
        if key not in self._warned_face_models:
            self._warned_face_models.add(key)
            logger.error(
                "face_model_mismatch drone=%s edge_model=%s ground_model=%s: face scores from "
                "this edge are ignored. Set the same FLIGHTRISK_FACE_MODEL on the ground and "
                "every edge.", key[0], edge_model, self._face_model)
        return True

    def process_message(self, msg: DetectionMessage) -> list[dict[str, Any]]:
        """Process a detection message, return scored results."""
        results = []
        # A detection profile can weight face or ReID matching differently
        # (flightrisk/vision/profiles.py). None, the default and an unknown
        # profile all leave the scorer's weights unchanged.
        weight_scale = weight_scale_for(getattr(msg, "detection_profile", None))

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
            if det.face_embedding is not None and self._target_face is not None \
                    and not self._face_model_mismatch(msg):
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
                if weight_scale is None:
                    score_result = self._scorer.score(reid_score=reid_score, face_score=face_score)
                else:
                    score_result = self._scorer.score(
                        reid_score=reid_score, face_score=face_score, weight_scale=weight_scale,
                    )

            results.append({
                "bbox": det.bbox,
                "confidence": det.confidence,
                "reid_score": reid_score,
                "face_score": face_score,
                "score_result": score_result,
                "frame_id": msg.frame_id,
                "timestamp": msg.timestamp,
                # Fleet fields: which drone saw it and where that drone was.
                # None outside fleet mode.
                "drone_id": getattr(msg, "drone_id", None),
                "rel_x_cm": getattr(msg, "rel_x_cm", None),
                "rel_y_cm": getattr(msg, "rel_y_cm", None),
                "heading_deg": getattr(msg, "heading_deg", None),
                "lat": getattr(msg, "lat", None),
                "lon": getattr(msg, "lon", None),
                "detection_profile": getattr(msg, "detection_profile", None),
            })

        return results
