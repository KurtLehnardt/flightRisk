"""Altitude benchmark: person detection recall and precision by pixel height.

Loads Pascal VOC annotated aerial images (HERIDAL, LADD exports), runs a
detector over them, and reports recall and precision binned by the ground
truth person height in pixels. The lower edge of the first bin that reaches a
recall floor is the minimum person size the detector can find, which fixes the
altitude a search flight must fly.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

import cv2

Box = tuple[float, float, float, float]

# Seven bins from the design: [lo, hi) in pixels of person height.
DEFAULT_BINS: list[tuple[float, float]] = [
    (0, 8), (8, 16), (16, 24), (24, 32), (32, 48), (48, 64), (64, float("inf")),
]

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp")


@dataclass
class Sample:
    """One annotated image: its path and ground truth person boxes (xyxy)."""

    image_path: Path
    width: int
    height: int
    boxes: list[Box] = field(default_factory=list)


@dataclass
class BinResult:
    """Counts and rates for one person-height bin."""

    lo: float
    hi: float
    n_gt: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def recall(self) -> float:
        return self.tp / self.n_gt if self.n_gt else 0.0

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 0.0


@dataclass
class DetectionEvalResult:
    """Per-bin results of one detector run over a dataset."""

    bins: list[BinResult]
    n_images: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_images": self.n_images,
            "bins": [
                {
                    "lo": b.lo,
                    "hi": None if b.hi == float("inf") else b.hi,
                    "n_gt": b.n_gt,
                    "tp": b.tp,
                    "fp": b.fp,
                    "fn": b.fn,
                    "recall": b.recall,
                    "precision": b.precision,
                }
                for b in self.bins
            ],
        }


def load_voc_xml(xml_path: Path, image_dir: Path | None = None) -> Sample | None:
    """Parse one VOC XML file into a Sample, or None when the image is missing."""
    root = ET.parse(xml_path).getroot()
    base = image_dir or xml_path.parent
    filename = root.findtext("filename")
    candidates = [base / filename] if filename else []
    candidates += [base / (xml_path.stem + ext) for ext in IMAGE_EXTENSIONS]
    image_path = next((c for c in candidates if c.is_file()), None)
    if image_path is None:
        return None
    size = root.find("size")
    width = int(float(size.findtext("width", "0"))) if size is not None else 0
    height = int(float(size.findtext("height", "0"))) if size is not None else 0
    boxes: list[Box] = []
    for obj in root.findall("object"):
        bb = obj.find("bndbox")
        if bb is None:
            continue
        boxes.append(tuple(float(bb.findtext(k, "0")) for k in ("xmin", "ymin", "xmax", "ymax")))
    return Sample(image_path=image_path, width=width, height=height, boxes=boxes)


def load_voc_dir(dir: str | Path) -> list[Sample]:
    """Load every VOC XML under dir (recursively) with its matching image.

    Annotations that sit in an ``Annotations`` folder are paired with images in
    a sibling ``JPEGImages`` folder when present.
    """
    root = Path(dir)
    samples: list[Sample] = []
    for xml_path in sorted(root.rglob("*.xml")):
        image_dir = None
        if xml_path.parent.name == "Annotations":
            sibling = xml_path.parent.parent / "JPEGImages"
            if sibling.is_dir():
                image_dir = sibling
        sample = load_voc_xml(xml_path, image_dir)
        if sample is not None:
            samples.append(sample)
    return samples


def box_iou(a: Sequence[float], b: Sequence[float]) -> float:
    """Intersection over union of two xyxy boxes."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def match_detections(
    gt_boxes: Sequence[Sequence[float]],
    pred_boxes: Sequence[Sequence[float]],
    iou: float = 0.3,
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Greedily match predictions to ground truth by descending IoU.

    Returns (matches, unmatched_gt, unmatched_pred) where matches is a list of
    (gt_index, pred_index) pairs. Each box is used at most once.
    """
    pairs = []
    for gi, g in enumerate(gt_boxes):
        for pi, p in enumerate(pred_boxes):
            score = box_iou(g, p)
            if score >= iou:
                pairs.append((score, gi, pi))
    pairs.sort(key=lambda t: -t[0])
    used_g: set[int] = set()
    used_p: set[int] = set()
    matches: list[tuple[int, int]] = []
    for _, gi, pi in pairs:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        matches.append((gi, pi))
    unmatched_gt = [i for i in range(len(gt_boxes)) if i not in used_g]
    unmatched_pred = [i for i in range(len(pred_boxes)) if i not in used_p]
    return matches, unmatched_gt, unmatched_pred


def _bin_index(height: float, bins: Sequence[tuple[float, float]]) -> int | None:
    for i, (lo, hi) in enumerate(bins):
        if lo <= height < hi:
            return i
    return None


def bin_by_height(
    samples: Sequence[Sample], bins: Sequence[tuple[float, float]] = DEFAULT_BINS
) -> list[int]:
    """Count ground truth boxes per height bin."""
    counts = [0] * len(bins)
    for s in samples:
        for b in s.boxes:
            idx = _bin_index(b[3] - b[1], bins)
            if idx is not None:
                counts[idx] += 1
    return counts


def evaluate(
    detector: Any,
    samples: Sequence[Sample],
    bins: Sequence[tuple[float, float]] = DEFAULT_BINS,
    iou_threshold: float = 0.3,
) -> DetectionEvalResult:
    """Run detector.detect(frame) over samples and score per height bin.

    A true positive and a false negative are binned by the ground truth box
    height. A false positive has no ground truth, so it is binned by the
    predicted box height.
    """
    results = [BinResult(lo=lo, hi=hi) for lo, hi in bins]
    for s in samples:
        frame = cv2.imread(str(s.image_path))
        if frame is None:
            raise ValueError(f"Could not read image {s.image_path}")
        preds = [list(d["bbox"]) for d in detector.detect(frame)]
        matches, miss_gt, extra_pred = match_detections(s.boxes, preds, iou=iou_threshold)
        for gi, _ in matches:
            idx = _bin_index(s.boxes[gi][3] - s.boxes[gi][1], bins)
            if idx is not None:
                results[idx].n_gt += 1
                results[idx].tp += 1
        for gi in miss_gt:
            idx = _bin_index(s.boxes[gi][3] - s.boxes[gi][1], bins)
            if idx is not None:
                results[idx].n_gt += 1
                results[idx].fn += 1
        for pi in extra_pred:
            idx = _bin_index(preds[pi][3] - preds[pi][1], bins)
            if idx is not None:
                results[idx].fp += 1
    return DetectionEvalResult(bins=results, n_images=len(samples))


def min_person_px(result: DetectionEvalResult, recall_floor: float = 0.5) -> float | None:
    """Lower edge of the first bin with recall at or above the floor.

    Bins with no ground truth are skipped. Returns None when no bin qualifies.
    """
    for b in result.bins:
        if b.n_gt > 0 and b.recall >= recall_floor:
            return b.lo
    return None
