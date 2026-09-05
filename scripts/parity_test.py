"""Shared parity-testing utilities for ONNX model exports.

Every export script imports from here so there is one definition of
'does the ONNX output match the PyTorch output?' across YOLO, CLIP,
and ArcFace.

Usage from an export script::

    from parity_test import (
        load_test_images,
        cosine_similarity,
        run_parity_gate,
        compare_detections,
        print_parity_report,
    )
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Sequence

import numpy as np


# ---------------------------------------------------------------------------
# Test-image loading
# ---------------------------------------------------------------------------

def load_test_images(
    directory: str | Path = ".",
    n: int = 20,
    extensions: Sequence[str] = ("*.png", "*.jpg", "*.jpeg"),
) -> list[np.ndarray]:
    """Load up to *n* images from *directory* as BGR numpy arrays.

    Falls back to synthetic 640x480 images if fewer than *n* real images
    are found.

    Args:
        directory: Folder to search for images.
        n:         Maximum number of images to return.
        extensions: Glob patterns for image files.

    Returns:
        List of BGR ``np.ndarray`` images, length <= *n*.
    """
    try:
        import cv2
    except ImportError:
        print("[parity] WARNING: opencv-python not installed, using synthetic images")
        return _synthetic_images(n)

    paths: list[str] = []
    for ext in extensions:
        paths.extend(glob.glob(os.path.join(str(directory), ext)))
    paths = sorted(set(paths))[:n]

    images: list[np.ndarray] = []
    for p in paths:
        img = cv2.imread(p)
        if img is not None:
            images.append(img)

    if len(images) < n:
        remaining = n - len(images)
        print(
            f"[parity] Found {len(images)} real images, "
            f"generating {remaining} synthetic test images"
        )
        images.extend(_synthetic_images(remaining))

    return images[:n]


def _synthetic_images(n: int) -> list[np.ndarray]:
    """Generate simple synthetic BGR images for parity testing."""
    rng = np.random.RandomState(42)
    return [rng.randint(0, 255, (480, 640, 3), dtype=np.uint8) for _ in range(n)]


# ---------------------------------------------------------------------------
# Embedding / similarity helpers
# ---------------------------------------------------------------------------

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two 1-D vectors.

    Returns:
        float in [-1, 1].  1.0 = identical direction.
    """
    a = a.flatten().astype(np.float64)
    b = b.flatten().astype(np.float64)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def run_parity_gate(
    name: str,
    pytorch_embeddings: list[np.ndarray],
    onnx_embeddings: list[np.ndarray],
    threshold: float = 0.98,
) -> dict:
    """Compare embedding lists element-wise via cosine similarity.

    Args:
        name:                Human-readable model name for reporting.
        pytorch_embeddings:  Embeddings from the PyTorch model.
        onnx_embeddings:     Embeddings from the ONNX model.
        threshold:           Minimum cosine-similarity for a PASS.

    Returns:
        dict with keys ``name``, ``passed``, ``scores``, ``min_score``,
        ``mean_score``, ``threshold``.
    """
    assert len(pytorch_embeddings) == len(onnx_embeddings), (
        f"Length mismatch: {len(pytorch_embeddings)} vs {len(onnx_embeddings)}"
    )

    scores = [
        cosine_similarity(pt, ox)
        for pt, ox in zip(pytorch_embeddings, onnx_embeddings)
    ]

    return {
        "name": name,
        "passed": all(s >= threshold for s in scores),
        "scores": scores,
        "min_score": min(scores) if scores else 0.0,
        "mean_score": float(np.mean(scores)) if scores else 0.0,
        "threshold": threshold,
        "n_samples": len(scores),
    }


# ---------------------------------------------------------------------------
# Detection comparison (YOLO-style bounding boxes + confidences)
# ---------------------------------------------------------------------------

def compare_detections(
    pytorch_dets: list[dict],
    onnx_dets: list[dict],
    bbox_tolerance: float = 2.0,
    conf_tolerance: float = 0.01,
) -> dict:
    """Compare two sets of person detections for parity.

    Each detection dict must have keys ``bbox`` (list of 4 ints/floats,
    xyxy) and ``confidence`` (float).

    The comparison matches detections greedily by closest bbox center,
    then checks that coordinates are within *bbox_tolerance* pixels and
    confidences within *conf_tolerance*.

    Returns:
        dict with ``passed``, ``n_pytorch``, ``n_onnx``,
        ``matched_count``, ``bbox_diffs``, ``conf_diffs``.
    """
    result: dict = {
        "name": "detection_parity",
        "passed": True,
        "n_pytorch": len(pytorch_dets),
        "n_onnx": len(onnx_dets),
        "matched_count": 0,
        "bbox_diffs": [],
        "conf_diffs": [],
    }

    if len(pytorch_dets) != len(onnx_dets):
        result["passed"] = False
        return result

    if not pytorch_dets:
        return result

    # Greedy match by bbox center distance
    used: set[int] = set()
    for pt_det in pytorch_dets:
        pt_bbox = np.array(pt_det["bbox"], dtype=np.float64)
        pt_center = np.array([(pt_bbox[0] + pt_bbox[2]) / 2,
                              (pt_bbox[1] + pt_bbox[3]) / 2])

        best_j = -1
        best_dist = float("inf")
        for j, ox_det in enumerate(onnx_dets):
            if j in used:
                continue
            ox_bbox = np.array(ox_det["bbox"], dtype=np.float64)
            ox_center = np.array([(ox_bbox[0] + ox_bbox[2]) / 2,
                                  (ox_bbox[1] + ox_bbox[3]) / 2])
            dist = float(np.linalg.norm(pt_center - ox_center))
            if dist < best_dist:
                best_dist = dist
                best_j = j

        if best_j < 0:
            result["passed"] = False
            continue

        used.add(best_j)
        ox_det = onnx_dets[best_j]
        ox_bbox = np.array(ox_det["bbox"], dtype=np.float64)

        bbox_diff = float(np.max(np.abs(pt_bbox - ox_bbox)))
        conf_diff = abs(pt_det["confidence"] - ox_det["confidence"])

        result["bbox_diffs"].append(bbox_diff)
        result["conf_diffs"].append(conf_diff)

        if bbox_diff > bbox_tolerance or conf_diff > conf_tolerance:
            result["passed"] = False

        result["matched_count"] += 1

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_parity_report(results: dict | list[dict]) -> None:
    """Print a human-readable parity test report.

    Args:
        results: A single result dict (from ``run_parity_gate`` or
                 ``compare_detections``) or a list of them.
    """
    if isinstance(results, dict):
        results = [results]

    print("\n" + "=" * 60)
    print("  PARITY TEST REPORT")
    print("=" * 60)

    all_passed = True
    for r in results:
        name = r.get("name", "unknown")
        passed = r.get("passed", False)
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False

        print(f"\n  [{status}] {name}")

        # Embedding-style report
        if "min_score" in r:
            print(f"    Threshold : {r['threshold']:.4f}")
            print(f"    Min score : {r['min_score']:.6f}")
            print(f"    Mean score: {r['mean_score']:.6f}")
            print(f"    Samples   : {r['n_samples']}")

        # Detection-style report
        if "n_pytorch" in r:
            print(f"    PyTorch detections: {r['n_pytorch']}")
            print(f"    ONNX detections   : {r['n_onnx']}")
            print(f"    Matched           : {r['matched_count']}")
            if r["bbox_diffs"]:
                print(f"    Max bbox diff (px): {max(r['bbox_diffs']):.2f}")
            if r["conf_diffs"]:
                print(f"    Max conf diff     : {max(r['conf_diffs']):.4f}")

    print("\n" + "-" * 60)
    overall = "ALL PASSED" if all_passed else "SOME FAILED"
    print(f"  Overall: {overall}")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# CLI entry point (run standalone to verify the module loads)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("[parity_test] Module loaded successfully.")
    print(
        f"  cosine_similarity([1,0], [1,0]) = "
        f"{cosine_similarity(np.array([1, 0]), np.array([1, 0]))}"
    )
    print(
        f"  cosine_similarity([1,0], [0,1]) = "
        f"{cosine_similarity(np.array([1, 0]), np.array([0, 1]))}"
    )

    imgs = load_test_images(".", n=3)
    print(f"  Loaded {len(imgs)} test images (shapes: {[i.shape for i in imgs]})")
