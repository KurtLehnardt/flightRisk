"""YOLO-based person detector.

Uses Ultralytics YOLO26n/YOLOv8n for real-time person detection.
Runs on Apple Silicon via MPS backend. Filters to person class only.
"""

import numpy as np
from ultralytics import YOLO


class PersonDetector:
    """Detects people in video frames using YOLO."""

    PERSON_CLASS_ID = 0  # COCO class 0 = person

    def __init__(self, model_name: str = "yolo11n.pt", confidence: float = 0.4):
        """Initialize the detector.

        Args:
            model_name: YOLO model to load. Downloads automatically on first run.
                        Use 'yolo11n.pt' (fast) or 'yolo11s.pt' (more accurate).
            confidence: Minimum confidence threshold for detections.
        """
        self.model = YOLO(model_name)
        self.confidence = confidence
        self._device = self._select_device()
        print(f"[detector] Loaded {model_name} on {self._device}")

    def _select_device(self) -> str:
        """Pick the best available device."""
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        elif torch.cuda.is_available():
            return "cuda"
        return "cpu"

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
        results = self.model(
            frame,
            classes=[self.PERSON_CLASS_ID],
            conf=self.confidence,
            device=self._device,
            verbose=False,
        )

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
