"""Edge compute: runs YOLO + CLIP + InsightFace on video frames.

In local mode, runs in-process on the laptop.
In deployed mode, runs on a Jetson Orin Nano and sends
detection messages over WebSocket to the ground station.
"""
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np

logger = logging.getLogger(__name__)


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
    # Source frame dimensions, so the client can scale/normalize detection
    # bboxes for rendering (esp. in detections-only mode over a canvas).
    frame_width: int = 0
    frame_height: int = 0


class EdgeRunner:
    """Produces detection messages from video frames."""

    def __init__(self, detector=None, reid=None, face=None, stream_video: bool = False):
        self._detector = detector
        self._reid = reid
        self._face = face
        self._frame_id = 0
        # When False (the default), only detection metadata + per-detection
        # crops/embeddings are shipped -- no full-frame thumbnail -- to keep
        # bandwidth and battery use down. Flip on for live video streaming.
        self.stream_video = stream_video

    def set_stream_video(self, enabled: bool) -> None:
        """Enable or disable full-frame thumbnail streaming at runtime."""
        self.stream_video = enabled

    def process_frame(self, frame: np.ndarray) -> DetectionMessage:
        """Run detection + embedding pipeline on a frame.

        Returns a DetectionMessage with detections, embeddings, and crops.
        """
        self._frame_id += 1
        msg = DetectionMessage(
            timestamp=time.time(),
            frame_id=self._frame_id,
            frame_width=frame.shape[1],
            frame_height=frame.shape[0],
        )

        # Generate thumbnail only when streaming video is enabled. When off
        # (the default), thumbnail_jpeg stays None -- crops and embeddings
        # below are still produced regardless.
        if self.stream_video:
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

            # CLIP ReID embedding, via PersonReID's public extract_embedding().
            # PersonReID's compare()/find_match() only return a similarity
            # score against an already-set target; extract_embedding() gives
            # the raw feature vector, which the edge needs to ship to the
            # ground station for later matching.
            if self._reid is not None and hasattr(self._reid, 'extract_embedding'):
                try:
                    emb = self._reid.extract_embedding(crop)
                    detection.reid_embedding = emb.tolist() if emb is not None else None
                except Exception:
                    logger.warning("reid_embedding_failed", exc_info=True)

            # Face embedding, via FaceRecognizer's public extract_embedding()
            # (wraps _best_face_embedding internally).
            if self._face is not None and hasattr(self._face, 'extract_embedding'):
                try:
                    emb = self._face.extract_embedding(crop)
                    detection.face_embedding = emb.tolist() if emb is not None else None
                except Exception:
                    logger.warning("face_embedding_failed", exc_info=True)

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
            "frame_width": msg.frame_width,
            "frame_height": msg.frame_height,
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
            frame_width=data.get("frame_width", 0),
            frame_height=data.get("frame_height", 0),
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
