#!/usr/bin/env python3
"""Sweep stock YOLO models and confidence thresholds on the altitude benchmark.

For each model, runs tiled inference once per image at the lowest requested
confidence, then scores every requested confidence from those cached
detections with ``flightrisk.evaluation.detection.evaluate``. Raising the
threshold afterwards gives the same boxes as running at that threshold,
because both the per-tile NMS in Ultralytics and the cross-tile NMS in
``PersonDetector`` are greedy in descending score order: a lower-scored box can
never suppress a higher-scored one. The sweep also scores confidence 0.4 by
default, which reproduces t11's stock tiled numbers as a check.

This sweep runs on the HERIDAL test split. Choosing the best row from it is a
choice made on the test data, and the altitude benchmark doc says so.

Usage from the repository root:

    python scripts/sweep_altitude.py --dataset datasets/heridal/test \\
        --models yolo11n.pt yolo11s.pt yolo11m.pt --conf 0.1 0.25 0.4 \\
        --output results/altitude_sweep.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flightrisk.evaluation.detection import (  # noqa: E402
    DEFAULT_BINS,
    DetectionEvalResult,
    Sample,
    evaluate,
    load_voc_dir,
    min_person_px,
)

DEFAULT_MODELS = ("yolo11n.pt", "yolo11s.pt", "yolo11m.pt")
DEFAULT_CONFS = (0.1, 0.25, 0.4)
IOU_THRESHOLD = 0.3
RECALL_FLOOR = 0.5

DetectorFactory = Callable[[str, float], Any]


class _ReplayDetector:
    """Detector that returns cached detections above a confidence, in sample order."""

    def __init__(self, cached: Sequence[Sequence[dict]], confidence: float):
        self._cached = cached
        self._confidence = confidence
        self._next = 0

    def detect(self, frame) -> list[dict]:
        dets = self._cached[self._next]
        self._next += 1
        return [d for d in dets if d["confidence"] >= self._confidence]


def run_detections(detector: Any, samples: Sequence[Sample]) -> tuple[list[list[dict]], float]:
    """Run detector.detect once per sample and keep only box and score.

    Returns the cached detections and the inference wall time in seconds.
    """
    import cv2

    cached: list[list[dict]] = []
    started = time.perf_counter()
    for s in samples:
        frame = cv2.imread(str(s.image_path))
        if frame is None:
            raise ValueError(f"Could not read image {s.image_path}")
        cached.append([
            {"bbox": list(d["bbox"]), "confidence": float(d["confidence"])}
            for d in detector.detect(frame)
        ])
    return cached, time.perf_counter() - started


def evaluate_confidences(
    detector: Any,
    samples: Sequence[Sample],
    confs: Sequence[float],
    bins: Sequence[tuple[float, float]] = DEFAULT_BINS,
    iou_threshold: float = IOU_THRESHOLD,
) -> tuple[dict[float, DetectionEvalResult], float]:
    """Score several confidence thresholds from one detection pass.

    The detector must already run at or below min(confs).
    """
    cached, seconds = run_detections(detector, samples)
    results = {
        conf: evaluate(_ReplayDetector(cached, conf), samples, bins=bins, iou_threshold=iou_threshold)
        for conf in sorted(confs)
    }
    return results, seconds


def overall(result: DetectionEvalResult) -> dict[str, float | int]:
    """Totals across all height bins, with recall, precision and F1."""
    tp = sum(b.tp for b in result.bins)
    fp = sum(b.fp for b in result.bins)
    fn = sum(b.fn for b in result.bins)
    n_gt = sum(b.n_gt for b in result.bins)
    recall = tp / n_gt if n_gt else 0.0
    precision = tp / (tp + fp) if tp + fp else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"n_gt": n_gt, "tp": tp, "fp": fp, "fn": fn,
            "recall": recall, "precision": precision, "f1": f1}


def run_record(model: str, confidence: float, result: DetectionEvalResult,
               seconds: float | None = None) -> dict[str, Any]:
    """One JSON record: the detection result plus its configuration and totals."""
    record: dict[str, Any] = {"model": model, "confidence": confidence, "tiled": True}
    record.update(result.to_dict())
    record["min_person_px"] = min_person_px(result, RECALL_FLOOR)
    record["overall"] = overall(result)
    if seconds is not None:
        record["inference_seconds"] = round(seconds, 1)
    return record


def rank_key(record: dict[str, Any]) -> tuple[float, float]:
    """Sort key for the best configuration: lowest min_person_px, then highest F1.

    A run with no bin at the recall floor sorts after every run that has one.
    """
    px = record["min_person_px"]
    return (float("inf") if px is None else float(px), -record["overall"]["f1"])


def pick_best(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Return the model and confidence of the best run by rank_key."""
    best = min(runs, key=rank_key)
    return {
        "model": best["model"],
        "confidence": best["confidence"],
        "min_person_px": best["min_person_px"],
        "criterion": "lowest min_person_px (recall >= 0.5), then highest overall F1",
    }


def default_detector_factory(tile_size: int, overlap: float) -> DetectorFactory:
    """Factory that builds a tiled PersonDetector for a model and confidence."""

    def factory(model: str, confidence: float):
        from flightrisk.vision.detector import PersonDetector

        return PersonDetector(model_name=model, confidence=confidence, tiled=True,
                              tile_size=tile_size, overlap=overlap)

    return factory


def run_sweep(
    dataset: str | Path,
    models: Sequence[str],
    confs: Sequence[float],
    output: str | Path,
    detector_factory: DetectorFactory | None = None,
    tile_size: int = 640,
    overlap: float = 0.2,
) -> dict[str, Any]:
    """Evaluate every model at every confidence and write the sweep JSON."""
    samples = load_voc_dir(dataset)
    if not samples:
        raise SystemExit(f"no VOC annotations with images found in {dataset}")
    factory = detector_factory or default_detector_factory(tile_size, overlap)
    runs: list[dict[str, Any]] = []
    for model in models:
        detector = factory(model, min(confs))
        results, seconds = evaluate_confidences(detector, samples, confs)
        for conf, result in results.items():
            record = run_record(Path(model).name, conf, result, seconds)
            runs.append(record)
            o = record["overall"]
            print(f"[sweep] {record['model']} conf={conf:g} recall={o['recall']:.3f} "
                  f"precision={o['precision']:.3f} min_person_px={record['min_person_px']}",
                  flush=True)
    payload = {
        "dataset": "HERIDAL",
        "split": "test",
        "split_note": "The sweep runs on the test split, so the best row is chosen on test data.",
        "n_images": len(samples),
        "n_gt": sum(len(s.boxes) for s in samples),
        "tile_size": tile_size,
        "overlap": overlap,
        "iou_threshold": IOU_THRESHOLD,
        "recall_floor": RECALL_FLOOR,
        "runs": runs,
        "best": pick_best(runs),
    }
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"[sweep] wrote {out}", flush=True)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sweep models and confidences, tiled, on a VOC split")
    parser.add_argument("--dataset", required=True, help="VOC directory (Annotations/ and JPEGImages/)")
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--conf", nargs="+", type=float, default=list(DEFAULT_CONFS))
    parser.add_argument("--output", default="results/altitude_sweep.json")
    parser.add_argument("--tile-size", type=int, default=640)
    parser.add_argument("--overlap", type=float, default=0.2)
    args = parser.parse_args(argv)
    run_sweep(args.dataset, args.models, args.conf, args.output,
              tile_size=args.tile_size, overlap=args.overlap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
