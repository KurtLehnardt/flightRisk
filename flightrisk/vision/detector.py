"""YOLO-based person detector.

Uses Ultralytics YOLO26n/YOLOv8n for real-time person detection.
Runs on Apple Silicon via MPS backend. Filters to person class only.
"""

import logging

import numpy as np
from ultralytics import YOLO

from flightrisk.config import get_config
from flightrisk.obs_log import notice

_log = logging.getLogger(__name__)


class PersonDetector:
    """Detects people in video frames using YOLO."""

    PERSON_CLASS_ID = 0  # COCO class 0 = person

    NMS_IOU_THRESHOLD = 0.5

    def __init__(
        self,
        model_name: str | None = None,
        confidence: float | None = None,
        tiled: bool | None = None,
        tile_size: int | None = None,
        overlap: float | None = None,
        nms_iou: float | None = None,
        imgsz: int | None = None,
    ):
        """Initialize the detector.

        Args:
            model_name: YOLO model to load. Downloads automatically on first run.
                        Use 'yolo11n.pt' (fast) or 'yolo11s.pt' (more accurate).
                        Defaults to `config.vision.detector_model`.
            confidence: Minimum confidence threshold for detections.
                        Defaults to `config.vision.detector_confidence`.
            tiled: Run the model on overlapping tiles and merge the results.
                   Defaults to `config.vision.detector_tiled`.
            tile_size: Tile edge length in pixels.
                       Defaults to `config.vision.detector_tile_size`.
            overlap: Fraction of tile size shared by neighboring tiles, in [0, 1).
                     Defaults to `config.vision.detector_tile_overlap`.
            nms_iou: IoU threshold for non-maximum suppression, in (0, 1],
                     used by each model call and when merging tiles. None
                     keeps today's behaviour: the model's own default for
                     each call and `NMS_IOU_THRESHOLD` when merging tiles.
            imgsz: Model input size in pixels. None keeps the model default.
        """
        cfg = get_config().vision
        if model_name is None:
            model_name = cfg.detector_model
        if confidence is None:
            confidence = cfg.detector_confidence
        if tiled is None:
            tiled = cfg.detector_tiled
        if tile_size is None:
            tile_size = cfg.detector_tile_size
        if overlap is None:
            overlap = cfg.detector_tile_overlap
        if tile_size < 1:
            raise ValueError("tile_size must be positive")
        if not 0.0 <= overlap < 1.0:
            raise ValueError("overlap must be in [0, 1)")
        if nms_iou is not None and not 0.0 < nms_iou <= 1.0:
            raise ValueError("nms_iou must be in (0, 1]")
        if imgsz is not None and imgsz < 32:
            raise ValueError("imgsz must be at least 32")
        self.model_name = model_name
        self.nms_iou = nms_iou
        self.imgsz = imgsz
        self.model = YOLO(model_name)
        self.confidence = confidence
        self.tiled = tiled
        self.tile_size = tile_size
        self.overlap = overlap
        self._device = self._select_device()
        notice(_log, "model_loaded", f"[detector] Loaded {model_name} on {self._device}", model=model_name, device=str(self._device))

    def _select_device(self) -> str:
        """Pick the best available device."""
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        elif torch.cuda.is_available():
            return "cuda"
        return "cpu"

    def _predict_kwargs(self) -> dict:
        """Keyword arguments for one model call; unset options are left to the model."""
        kwargs = {
            "classes": [self.PERSON_CLASS_ID],
            "conf": self.confidence,
            "device": self._device,
            "verbose": False,
        }
        if self.nms_iou is not None:
            kwargs["iou"] = self.nms_iou
        if self.imgsz is not None:
            kwargs["imgsz"] = self.imgsz
        return kwargs

    def detect(self, frame: np.ndarray) -> list[dict]:
        """Detect people in a frame.

        Args:
            frame: BGR numpy array from OpenCV.

        Returns:
            List of detections, each with keys:
                bbox: [x1, y1, x2, y2] pixel coordinates
                confidence: float 0-1
                crop: numpy array of the cropped person image
        """
        if self.tiled:
            return self._detect_tiled(frame)
        results = self.model(frame, **self._predict_kwargs())

        detections = []
        for result in results:
            boxes = result.boxes
            if boxes is None:
                continue

            for i in range(len(boxes)):
                bbox = boxes.xyxy[i].cpu().numpy().astype(int)
                conf = float(boxes.conf[i].cpu())

                x1, y1, x2, y2 = bbox
                crop = frame[y1:y2, x1:x2]

                detections.append({
                    "bbox": bbox.tolist(),
                    "confidence": conf,
                    "crop": crop,
                })

        return detections

    @staticmethod
    def compute_tiles(height: int, width: int, tile_size: int,
                      overlap: float) -> list[tuple[int, int, int, int]]:
        """Return overlapping tile windows (x1, y1, x2, y2) that cover the frame.

        Tiles are `tile_size` square, except where the frame is smaller. The
        last tile on each axis is shifted back so it ends at the frame edge.
        """
        stride = max(1, int(round(tile_size * (1.0 - overlap))))

        def starts(length: int) -> list[int]:
            if length <= tile_size:
                return [0]
            out = list(range(0, length - tile_size, stride))
            out.append(length - tile_size)
            return out

        return [
            (x, y, min(x + tile_size, width), min(y + tile_size, height))
            for y in starts(height)
            for x in starts(width)
        ]

    def _detect_tiled(self, frame: np.ndarray) -> list[dict]:
        """Run the model per tile, offset boxes to frame coordinates, merge with NMS."""
        height, width = frame.shape[:2]
        boxes_xyxy: list[list[int]] = []
        scores: list[float] = []

        for tx1, ty1, tx2, ty2 in self.compute_tiles(
            height, width, self.tile_size, self.overlap
        ):
            tile = frame[ty1:ty2, tx1:tx2]
            results = self.model(tile, **self._predict_kwargs())
            for result in results:
                boxes = result.boxes
                if boxes is None:
                    continue
                for i in range(len(boxes)):
                    bx1, by1, bx2, by2 = boxes.xyxy[i].cpu().numpy().astype(int)
                    boxes_xyxy.append([
                        int(bx1) + tx1, int(by1) + ty1,
                        int(bx2) + tx1, int(by2) + ty1,
                    ])
                    scores.append(float(boxes.conf[i].cpu()))

        if not boxes_xyxy:
            return []

        xywh = [[x1, y1, x2 - x1, y2 - y1] for x1, y1, x2, y2 in boxes_xyxy]
        iou = self.nms_iou if self.nms_iou is not None else self.NMS_IOU_THRESHOLD
        keep = cv2.dnn.NMSBoxes(xywh, scores, 0.0, iou)
        keep_idx = sorted(int(k) for k in np.array(keep).flatten())

        detections = []
        for k in keep_idx:
            x1, y1, x2, y2 = boxes_xyxy[k]
            detections.append({
                "bbox": [x1, y1, x2, y2],
                "confidence": scores[k],
                "crop": frame[y1:y2, x1:x2],
            })
        return detections

    def annotate(self, frame: np.ndarray, detections: list[dict],
                 match_idx: int | None = None) -> np.ndarray:
        """Draw bounding boxes on a frame.

        Args:
            frame: The original frame.
            detections: Output from detect().
            match_idx: Index of the matched person (drawn in green, others in blue).

        Returns:
            Annotated frame copy.
        """
        annotated = frame.copy()
        for i, det in enumerate(detections):
            x1, y1, x2, y2 = det["bbox"]
            is_match = (match_idx is not None and i == match_idx)
            color = (0, 255, 0) if is_match else (255, 180, 0)
            thickness = 3 if is_match else 2
            label = f"MATCH {det['confidence']:.0%}" if is_match else f"{det['confidence']:.0%}"

            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, thickness)
            cv2.putText(
                annotated, label, (x1, y1 - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2,
            )

        return annotated


# Lazy import to keep cv2 at module level
import cv2
