"""Edge compute: runs YOLO + CLIP + InsightFace on video frames.

In local mode, runs in-process on the laptop.
In deployed mode, runs on a Jetson Orin Nano and sends
detection messages over WebSocket to the ground station.
"""
import base64
import time
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np


@dataclass
class Detection:
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2
    confidence: float
    reid_embedding: list[float] | None = None
    face_embedding: list[float] | None = None
    crop_jpeg: bytes | None = None


@dataclass
class DetectionMessage:
    timestamp: float
    frame_id: int
    thumbnail_jpeg: bytes | None = None
    detections: list[Detection] = field(default_factory=list)


class EdgeRunner:
    """Produces detection messages from video frames."""

    def __init__(self, detector=None, reid=None, face=None):
        self._detector = detector
        self._reid = reid
        self._face = face
        self._frame_id = 0

    def process_frame(self, frame: np.ndarray) -> DetectionMessage:
        """Run detection + embedding pipeline on a frame.

        Returns a DetectionMessage with detections, embeddings, and crops.
        """
        self._frame_id += 1
        msg = DetectionMessage(
            timestamp=time.time(),
            frame_id=self._frame_id,
        )

        # Generate thumbnail
        thumb = cv2.resize(frame, (320, 180))
        _, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 60])
        msg.thumbnail_jpeg = buf.tobytes()

        # Run YOLO
        if self._detector is None:
            return msg

        raw_detections = self._detector.detect(frame)

        for det in raw_detections:
            bbox = det["bbox"]  # x1, y1, x2, y2
            x1, y1, x2, y2 = bbox
            crop = frame[y1:y2, x1:x2]

            if crop.size == 0:
                continue

            detection = Detection(
                bbox=tuple(bbox),
                confidence=det["confidence"],
            )

            # CLIP ReID embedding (PersonReID._extract_embedding)
            if self._reid is not None and hasattr(self._reid, '_extract_embedding'):
                try:
                    emb = self._reid._extract_embedding(crop)
                    detection.reid_embedding = emb.tolist() if emb is not None else None
                except Exception:
                    pass

            # Face embedding (FaceRecognizer._best_face_embedding)
            if self._face is not None and hasattr(self._face, '_best_face_embedding'):
                try:
                    emb = self._face._best_face_embedding(crop)
                    detection.face_embedding = emb.tolist() if emb is not None else None
                except Exception:
                    pass

            # Crop JPEG
            _, crop_buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
            detection.crop_jpeg = crop_buf.tobytes()

            msg.detections.append(detection)

        return msg

    def to_dict(self, msg: DetectionMessage) -> dict:
        """Serialize a DetectionMessage to a dict for transport."""
        return {
            "type": "detections",
            "timestamp": msg.timestamp,
            "frame_id": msg.frame_id,
            "thumbnail": base64.b64encode(msg.thumbnail_jpeg).decode() if msg.thumbnail_jpeg else None,
            "detections": [
                {
                    "bbox": list(d.bbox),
                    "confidence": d.confidence,
                    "reid_embedding": d.reid_embedding,
                    "face_embedding": d.face_embedding,
                    "crop": base64.b64encode(d.crop_jpeg).decode() if d.crop_jpeg else None,
                }
                for d in msg.detections
            ],
        }

    @staticmethod
    def from_dict(data: dict) -> DetectionMessage:
        """Deserialize a dict back to a DetectionMessage."""
        msg = DetectionMessage(
            timestamp=data["timestamp"],
            frame_id=data["frame_id"],
            thumbnail_jpeg=base64.b64decode(data["thumbnail"]) if data.get("thumbnail") else None,
        )
        for d in data.get("detections", []):
            det = Detection(
                bbox=tuple(d["bbox"]),
                confidence=d["confidence"],
                reid_embedding=d.get("reid_embedding"),
                face_embedding=d.get("face_embedding"),
                crop_jpeg=base64.b64decode(d["crop"]) if d.get("crop") else None,
            )
            msg.detections.append(det)
        return msg
