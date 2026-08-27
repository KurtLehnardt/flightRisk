"""IoU-based detection tracker for temporal vote accumulation.

Matches detections across frames by bounding box overlap (IoU),
maintains a rolling window of match scores per tracked person,
and keeps the best crop (highest detection confidence) for each track.
"""

from dataclasses import dataclass, field

import numpy as np


@dataclass
class TrackedDetection:
    """A detection with accumulated tracking state."""

    track_id: int
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2
    confidence: float
    crop: np.ndarray | None = None
    reid_scores: list[float] = field(default_factory=list)
    face_scores: list[float] = field(default_factory=list)
    frames_seen: int = 0
    best_crop: np.ndarray | None = None
    best_crop_confidence: float = 0.0

    @property
    def avg_reid_score(self) -> float:
        """Average ReID score over the rolling window."""
        return sum(self.reid_scores) / len(self.reid_scores) if self.reid_scores else 0.0

    @property
    def avg_face_score(self) -> float:
        """Average face score over the rolling window."""
        return sum(self.face_scores) / len(self.face_scores) if self.face_scores else 0.0


@dataclass
class _Track:
    """Internal track state."""

    track_id: int
    bbox: tuple[int, int, int, int]
    confidence: float
    crop: np.ndarray | None
    reid_scores: list[float]
    face_scores: list[float]
    frames_seen: int
    age: int  # frames since last match
    best_crop: np.ndarray | None
    best_crop_confidence: float


def _compute_iou(box1: tuple, box2: tuple) -> float:
    """Compute Intersection over Union between two bounding boxes.

    Args:
        box1: (x1, y1, x2, y2) coordinates.
        box2: (x1, y1, x2, y2) coordinates.

    Returns:
        IoU value in [0, 1].
    """
    x1 = max(box1[0], box2[0])
    y1 = max(box1[1], box2[1])
    x2 = min(box1[2], box2[2])
    y2 = min(box1[3], box2[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
    area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
    union = area1 + area2 - intersection
    return intersection / union if union > 0 else 0.0


class DetectionTracker:
    """Track detections across frames using IoU matching."""

    def __init__(
        self,
        iou_threshold: float = 0.3,
        max_age: int = 15,
        vote_window: int = 8,
    ):
        """Initialize the tracker.

        Args:
            iou_threshold: Minimum IoU to match a detection to an existing track.
            max_age: Number of frames a track survives without a match before removal.
            vote_window: Number of recent scores to keep for averaging.
        """
        self.iou_threshold = iou_threshold
        self.max_age = max_age
        self.vote_window = vote_window
        self._tracks: dict[int, _Track] = {}
        self._next_id: int = 0

    def update(self, detections: list[dict]) -> list[TrackedDetection]:
        """Match new detections to existing tracks and return tracked detections.

        Args:
            detections: List of dicts from PersonDetector with keys:
                'bbox' (x1,y1,x2,y2), 'confidence', 'crop' (np.ndarray)

        Returns:
            List of TrackedDetection with stable track_ids and score histories.
        """
        if not detections and not self._tracks:
            return []

        matched_track_ids: set[int] = set()
        matched_det_indices: set[int] = set()

        # Build IoU matrix and do greedy matching
        if self._tracks and detections:
            track_ids = list(self._tracks.keys())
            # Compute all IoU pairs
            iou_pairs: list[tuple[float, int, int]] = []
            for det_idx, det in enumerate(detections):
                det_bbox = tuple(det["bbox"])
                for tid in track_ids:
                    track = self._tracks[tid]
                    iou = _compute_iou(det_bbox, track.bbox)
                    if iou >= self.iou_threshold:
                        iou_pairs.append((iou, det_idx, tid))

            # Sort by IoU descending for greedy matching
            iou_pairs.sort(key=lambda x: x[0], reverse=True)

            for iou_val, det_idx, tid in iou_pairs:
                if det_idx in matched_det_indices or tid in matched_track_ids:
                    continue
                # Match this detection to this track
                det = detections[det_idx]
                track = self._tracks[tid]
                track.bbox = tuple(det["bbox"])
                track.confidence = det["confidence"]
                track.crop = det.get("crop")
                track.frames_seen += 1
                track.age = 0

                # Update best crop if this detection has higher confidence
                if det["confidence"] > track.best_crop_confidence:
                    track.best_crop = det.get("crop")
                    track.best_crop_confidence = det["confidence"]

                matched_track_ids.add(tid)
                matched_det_indices.add(det_idx)

        # Create new tracks for unmatched detections
        for det_idx, det in enumerate(detections):
            if det_idx in matched_det_indices:
                continue
            crop = det.get("crop")
            new_track = _Track(
                track_id=self._next_id,
                bbox=tuple(det["bbox"]),
                confidence=det["confidence"],
                crop=crop,
                reid_scores=[],
                face_scores=[],
                frames_seen=1,
                age=0,
                best_crop=crop,
                best_crop_confidence=det["confidence"],
            )
            self._tracks[self._next_id] = new_track
            matched_track_ids.add(self._next_id)
            self._next_id += 1

        # Age unmatched tracks and remove expired ones
        expired: list[int] = []
        for tid, track in self._tracks.items():
            if tid not in matched_track_ids:
                track.age += 1
                if track.age > self.max_age:
                    expired.append(tid)
        for tid in expired:
            del self._tracks[tid]

        # Return TrackedDetection for all active tracks
        return self._build_tracked_detections()

    def add_scores(
        self, track_id: int, reid_score: float = 0.0, face_score: float = 0.0
    ):
        """Add match scores to a track's history.

        Appends scores to the rolling window and trims to vote_window size.

        Args:
            track_id: The track to update.
            reid_score: ReID similarity score for this frame.
            face_score: Face recognition score for this frame.
        """
        track = self._tracks.get(track_id)
        if track is None:
            return

        if reid_score > 0.0:
            track.reid_scores.append(reid_score)
            if len(track.reid_scores) > self.vote_window:
                track.reid_scores = track.reid_scores[-self.vote_window :]

        if face_score > 0.0:
            track.face_scores.append(face_score)
            if len(track.face_scores) > self.vote_window:
                track.face_scores = track.face_scores[-self.vote_window :]

    def get_track(self, track_id: int) -> TrackedDetection | None:
        """Get current state of a specific track.

        Args:
            track_id: The track ID to look up.

        Returns:
            TrackedDetection if the track exists, None otherwise.
        """
        track = self._tracks.get(track_id)
        if track is None:
            return None
        return self._track_to_detection(track)

    def clear(self):
        """Remove all tracks."""
        self._tracks.clear()
        self._next_id = 0

    @property
    def active_tracks(self) -> list[TrackedDetection]:
        """Return all active (non-expired) tracks."""
        return self._build_tracked_detections()

    def _build_tracked_detections(self) -> list[TrackedDetection]:
        """Convert internal tracks to TrackedDetection list."""
        return [self._track_to_detection(t) for t in self._tracks.values()]

    @staticmethod
    def _track_to_detection(track: _Track) -> TrackedDetection:
        """Convert a _Track to a TrackedDetection."""
        return TrackedDetection(
            track_id=track.track_id,
            bbox=track.bbox,
            confidence=track.confidence,
            crop=track.crop,
            reid_scores=list(track.reid_scores),
            face_scores=list(track.face_scores),
            frames_seen=track.frames_seen,
            best_crop=track.best_crop,
            best_crop_confidence=track.best_crop_confidence,
        )
