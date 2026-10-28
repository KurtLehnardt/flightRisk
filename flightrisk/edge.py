"""Edge compute: runs YOLO + CLIP + InsightFace on video frames.

In local mode, runs in-process on the laptop.
In deployed mode, runs on a Jetson Orin Nano and sends
detection messages over WebSocket to the ground station.
"""
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

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
    # Fleet fields (design section 3). They identify which drone saw the
    # detections and where it was, so the ground can place and deduplicate
    # detections from several drones on one map. All stay None outside fleet
    # mode, and None fields are left out of the wire message.
    drone_id: str | None = None
    rel_x_cm: float | None = None
    rel_y_cm: float | None = None
    heading_deg: float | None = None
    lat: float | None = None
    lon: float | None = None
    # Name of the face model pack (e.g. "buffalo_l") that produced the face
    # embeddings. Optional: omitted from the wire when unknown, and ignored by
    # peers that predate it. Lets the ground detect embeddings from a
    # different model, whose cosine similarity is meaningless.
    face_model: str | None = None


# DetectionMessage fleet fields carried on the wire when set.
FLEET_POSE_FIELDS = ("rel_x_cm", "rel_y_cm", "heading_deg", "lat", "lon")


class EdgeRunner:
    """Produces detection messages from video frames.

    With ``appearance_only`` the runner never computes a face embedding, even
    if a face model is passed, and ``to_dict`` leaves the ``face_embedding``
    field out of every detection. Person crops and ReID (appearance)
    embeddings are still produced.
    """

    def __init__(
        self,
        detector=None,
        reid=None,
        face=None,
        stream_video: bool = False,
        drone_id: str | None = None,
        pose_provider: Callable[[], Mapping[str, Any] | None] | None = None,
        appearance_only: bool = False,
    ):
        self.appearance_only = bool(appearance_only)
        if self.appearance_only and face is not None:
            logger.warning("appearance_only: ignoring the face model passed to EdgeRunner")
            face = None
        self._detector = detector
        self._reid = reid
        self._face = face
        self._frame_id = 0
        # In fleet mode every DetectionMessage is stamped with this drone id
        # and with the pose returned by pose_provider (a mapping with any of
        # rel_x_cm, rel_y_cm, heading_deg, lat and lon) at capture time.
        self.drone_id = drone_id
        self._pose_provider = pose_provider
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
            drone_id=self.drone_id,
        )
        if self._face is not None and not self.appearance_only:
            name = getattr(self._face, "model_name", None)
            msg.face_model = name if isinstance(name, str) else None
        self._stamp_pose(msg)

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

    def _stamp_pose(self, msg: DetectionMessage) -> None:
        """Copy the current pose onto *msg*; a failing provider never drops a frame."""
        if self._pose_provider is None:
            return
        try:
            pose = self._pose_provider()
        except Exception:
            logger.warning("pose_provider_failed", exc_info=True)
            return
        if not pose:
            return
        for key in FLEET_POSE_FIELDS:
            value = pose.get(key)
            if value is not None:
                setattr(msg, key, value)

    def to_dict(self, msg: DetectionMessage) -> dict:
        """Serialize a DetectionMessage to a dict for transport.

        An appearance-only runner leaves ``face_embedding`` out entirely.
        """
        detections = []
        for d in msg.detections:
            item = {
                "bbox": list(d.bbox),
                "confidence": d.confidence,
                "reid_embedding": d.reid_embedding,
                "face_embedding": d.face_embedding,
                "crop": base64.b64encode(d.crop_jpeg).decode() if d.crop_jpeg else None,
            }
            if self.appearance_only:
                del item["face_embedding"]
            detections.append(item)
        data = {
            "type": "detections",
            "timestamp": msg.timestamp,
            "frame_id": msg.frame_id,
            "frame_width": msg.frame_width,
            "frame_height": msg.frame_height,
            "thumbnail": base64.b64encode(msg.thumbnail_jpeg).decode() if msg.thumbnail_jpeg else None,
            "detections": detections,
        }
        if msg.drone_id is not None:
            data["drone_id"] = msg.drone_id
        if msg.face_model is not None and not self.appearance_only:
            data["face_model"] = msg.face_model
        for key in FLEET_POSE_FIELDS:
            value = getattr(msg, key)
            if value is not None:
                data[key] = value
        return data

    @staticmethod
    def from_dict(data: dict) -> DetectionMessage:
        """Deserialize a dict back to a DetectionMessage."""
        msg = DetectionMessage(
            timestamp=data["timestamp"],
            frame_id=data["frame_id"],
            thumbnail_jpeg=base64.b64decode(data["thumbnail"]) if data.get("thumbnail") else None,
            frame_width=data.get("frame_width", 0),
            frame_height=data.get("frame_height", 0),
            drone_id=data.get("drone_id"),
            rel_x_cm=data.get("rel_x_cm"),
            rel_y_cm=data.get("rel_y_cm"),
            heading_deg=data.get("heading_deg"),
            lat=data.get("lat"),
            lon=data.get("lon"),
            face_model=data.get("face_model") if isinstance(data.get("face_model"), str) else None,
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
