#!/usr/bin/env python3
"""Export YOLO11n to ONNX for mobile inference, with parity verification.

Uses the Ultralytics export API to produce ``models/yolo11n.onnx``, then
runs both the PyTorch and ONNX models on test images and verifies that
bounding-box coordinates are within 2 px and confidences within 0.01.

Mobile preprocessing reference (must be replicated in ONNX Runtime on Android)
------------------------------------------------------------------------------
  Input tensor : float32 [1, 3, 640, 640]  (NCHW, RGB, 0-1 normalized)
  Letterbox    : resize longest edge to 640, pad shorter edge with gray (114/255)
  Normalization: pixel / 255.0  (NO mean/std subtraction -- YOLO uses raw 0-1)
  Color order  : RGB (convert from BGR if coming from OpenCV)
  Output       : [1, 84, 8400] -- 8400 candidate boxes, 84 = 4 bbox + 80 classes
                 Boxes are in xywh format (center-x, center-y, w, h), pixel coords
                 relative to the 640x640 letterboxed image.
                 Post-processing: transpose to [8400, 84], filter by confidence,
                 NMS, then rescale boxes back to the original image dimensions.

Requires: pip install ultralytics onnx onnxruntime opencv-python numpy
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export YOLO11n to ONNX with parity test")
    p.add_argument("--output-dir", default="models", help="Directory for exported ONNX files")
    p.add_argument("--test-images", default=".", help="Directory containing test images (*.png, *.jpg)")
    p.add_argument("--model", default="yolo11n.pt", help="YOLO model file or name")
    p.add_argument("--imgsz", type=int, default=640, help="Export image size")
    p.add_argument("--n-test", type=int, default=5, help="Number of test images for parity check")
    return p.parse_args()


def export_yolo(model_name: str, output_dir: str, imgsz: int) -> Path:
    """Export YOLO model to ONNX using the Ultralytics API.

    Returns:
        Path to the exported .onnx file.
    """
    try:
        from ultralytics import YOLO
    except ImportError:
        print("ERROR: ultralytics not installed. pip install ultralytics")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)

    print(f"[yolo] Loading {model_name} ...")
    model = YOLO(model_name)

    print(f"[yolo] Exporting to ONNX (imgsz={imgsz}) ...")
    export_path = model.export(format="onnx", imgsz=imgsz, simplify=True)
    export_path = Path(export_path)

    # Move to output dir if not already there
    dest = Path(output_dir) / "yolo11n.onnx"
    if export_path != dest:
        import shutil
        shutil.move(str(export_path), str(dest))
        print(f"[yolo] Moved to {dest}")

    return dest


def run_pytorch_inference(model_name: str, images: list[np.ndarray]) -> list[list[dict]]:
    """Run YOLO PyTorch inference on a list of BGR images.

    Returns:
        List of detection lists, one per image.  Each detection has
        ``bbox`` (xyxy ints) and ``confidence`` (float).
    """
    from ultralytics import YOLO

    model = YOLO(model_name)
    all_dets: list[list[dict]] = []

    for img in images:
        results = model(img, classes=[0], conf=0.4, verbose=False)
        dets: list[dict] = []
        for result in results:
            if result.boxes is None:
                continue
            for i in range(len(result.boxes)):
                bbox = result.boxes.xyxy[i].cpu().numpy().astype(int).tolist()
                conf = float(result.boxes.conf[i].cpu())
                dets.append({"bbox": bbox, "confidence": conf})
        all_dets.append(dets)

    return all_dets


def run_onnx_inference(onnx_path: str, images: list[np.ndarray], imgsz: int = 640) -> list[list[dict]]:
    """Run YOLO ONNX inference on a list of BGR images.

    Replicates the Ultralytics letterbox + NMS pipeline so results are
    directly comparable to PyTorch output.

    Returns:
        List of detection lists matching the PyTorch format.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        print("ERROR: onnxruntime not installed. pip install onnxruntime")
        sys.exit(1)

    try:
        import cv2
    except ImportError:
        print("ERROR: opencv-python not installed. pip install opencv-python")
        sys.exit(1)

    sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    input_name = sess.get_inputs()[0].name

    all_dets: list[list[dict]] = []

    for img in images:
        # Letterbox resize to imgsz x imgsz
        h, w = img.shape[:2]
        scale = min(imgsz / h, imgsz / w)
        new_w, new_h = int(w * scale), int(h * scale)
        resized = cv2.resize(img, (new_w, new_h))

        # Pad to square
        pad_w = imgsz - new_w
        pad_h = imgsz - new_h
        top = pad_h // 2
        left = pad_w // 2
        padded = cv2.copyMakeBorder(
            resized, top, pad_h - top, left, pad_w - left,
            cv2.BORDER_CONSTANT, value=(114, 114, 114),
        )

        # BGR -> RGB, HWC -> CHW, normalize to 0-1
        rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
        blob = rgb.astype(np.float32) / 255.0
        blob = blob.transpose(2, 0, 1)  # CHW
        blob = np.expand_dims(blob, 0)  # NCHW

        # Run inference
        outputs = sess.run(None, {input_name: blob})
        # Output shape: [1, 84, 8400] -- transpose to [8400, 84]
        preds = outputs[0][0].T  # [8400, 84]

        # Extract boxes and scores
        # First 4 columns: cx, cy, w, h (in letterboxed pixel coords)
        # Columns 4-84: class scores
        boxes_xywh = preds[:, :4]
        class_scores = preds[:, 4:]
        person_scores = class_scores[:, 0]  # class 0 = person

        # Filter by confidence
        conf_threshold = 0.4
        mask = person_scores > conf_threshold
        boxes_xywh = boxes_xywh[mask]
        scores = person_scores[mask]

        if len(scores) == 0:
            all_dets.append([])
            continue

        # Convert xywh -> xyxy
        boxes_xyxy = np.zeros_like(boxes_xywh)
        boxes_xyxy[:, 0] = boxes_xywh[:, 0] - boxes_xywh[:, 2] / 2  # x1
        boxes_xyxy[:, 1] = boxes_xywh[:, 1] - boxes_xywh[:, 3] / 2  # y1
        boxes_xyxy[:, 2] = boxes_xywh[:, 0] + boxes_xywh[:, 2] / 2  # x2
        boxes_xyxy[:, 3] = boxes_xywh[:, 1] + boxes_xywh[:, 3] / 2  # y2

        # Simple NMS
        keep = _nms(boxes_xyxy, scores, iou_threshold=0.45)
        boxes_xyxy = boxes_xyxy[keep]
        scores = scores[keep]

        # Rescale boxes from letterboxed coords to original image coords
        boxes_xyxy[:, 0] = (boxes_xyxy[:, 0] - left) / scale
        boxes_xyxy[:, 1] = (boxes_xyxy[:, 1] - top) / scale
        boxes_xyxy[:, 2] = (boxes_xyxy[:, 2] - left) / scale
        boxes_xyxy[:, 3] = (boxes_xyxy[:, 3] - top) / scale

        # Clip to image bounds
        boxes_xyxy[:, 0] = np.clip(boxes_xyxy[:, 0], 0, w)
        boxes_xyxy[:, 1] = np.clip(boxes_xyxy[:, 1], 0, h)
        boxes_xyxy[:, 2] = np.clip(boxes_xyxy[:, 2], 0, w)
        boxes_xyxy[:, 3] = np.clip(boxes_xyxy[:, 3], 0, h)

        dets: list[dict] = []
        for box, score in zip(boxes_xyxy, scores):
            dets.append({
                "bbox": box.astype(int).tolist(),
                "confidence": float(score),
            })
        all_dets.append(dets)

    return all_dets


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float = 0.45) -> list[int]:
    """Greedy non-maximum suppression.

    Args:
        boxes:  [N, 4] xyxy float array.
        scores: [N] float array.

    Returns:
        List of kept indices.
    """
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)

    order = scores.argsort()[::-1]
    keep: list[int] = []

    while len(order) > 0:
        i = order[0]
        keep.append(int(i))

        if len(order) == 1:
            break

        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        w = np.maximum(0, xx2 - xx1)
        h = np.maximum(0, yy2 - yy1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)

        remaining = np.where(iou <= iou_threshold)[0]
        order = order[remaining + 1]

    return keep


def main() -> None:
    args = parse_args()

    # Import parity utilities (same directory)
    sys.path.insert(0, str(Path(__file__).parent))
    from parity_test import load_test_images, compare_detections, print_parity_report

    # --- Export ---
    print("=" * 60)
    print("  YOLO11n ONNX Export")
    print("=" * 60)

    onnx_path = export_yolo(args.model, args.output_dir, args.imgsz)

    # --- Model info ---
    size_mb = onnx_path.stat().st_size / (1024 * 1024)
    print(f"\n[yolo] Export complete:")
    print(f"  File       : {onnx_path}")
    print(f"  Size       : {size_mb:.1f} MB")
    print(f"  Input shape: [1, 3, {args.imgsz}, {args.imgsz}] (NCHW, RGB, float32 0-1)")
    print(f"  Output     : [1, 84, 8400] (xywh + 80 class scores)")

    # --- Parity test ---
    print(f"\n[yolo] Running parity test on {args.n_test} images ...")
    images = load_test_images(args.test_images, n=args.n_test)

    pt_dets = run_pytorch_inference(args.model, images)
    ox_dets = run_onnx_inference(str(onnx_path), images, imgsz=args.imgsz)

    # Aggregate per-image comparisons
    all_results: list[dict] = []
    for i, (pt, ox) in enumerate(zip(pt_dets, ox_dets)):
        r = compare_detections(pt, ox, bbox_tolerance=2.0, conf_tolerance=0.01)
        r["name"] = f"YOLO image {i}"
        all_results.append(r)

    print_parity_report(all_results)

    # Overall summary
    passed = all(r["passed"] for r in all_results)
    if passed:
        print("[yolo] PARITY GATE PASSED -- ONNX export matches PyTorch")
    else:
        print("[yolo] PARITY GATE FAILED -- see report above for mismatches")
        sys.exit(1)


if __name__ == "__main__":
    main()
