#!/usr/bin/env python3
"""Export CLIP image encoders to ONNX for mobile ReID inference.

Exports two models:
  - Primary:  MobileCLIP-S2     (~20 MB, optimized for mobile)
  - Fallback: OpenAI CLIP ViT-B/32 (~150 MB, proven ONNX export)

For each model the script:
  1. Loads the model via ``open_clip``
  2. Exports the image encoder to ONNX (opset 17)
  3. Runs a parity test: PyTorch vs ONNX embeddings on test images
  4. Parity gate: cosine_similarity >= 0.98 on ALL samples

Mobile preprocessing reference
------------------------------
  Input tensor : float32 [1, 3, 224, 224]  (NCHW, RGB)
  Resize       : resize shortest edge to 256 (bicubic)
  Center crop  : 224 x 224
  Normalize    : mean = [0.48145466, 0.4578275, 0.40821073]
                 std  = [0.26862954, 0.26130258, 0.27577711]
  Output       : float32 [1, D]  where D = 512 for ViT-B models
  Post-process : L2-normalize the output embedding before cosine comparison

  The values above are the standard OpenAI CLIP normalization constants.
  MobileCLIP-S2 uses the same preprocessing when loaded via open_clip.

Requires: pip install open-clip-torch torch onnx onnxruntime opencv-python Pillow
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np


# CLIP image normalization constants (shared by all CLIP variants)
CLIP_MEAN = [0.48145466, 0.4578275, 0.40821073]
CLIP_STD = [0.26862954, 0.26130258, 0.27577711]

# Model registry: (open_clip model name, pretrained tag, output filename)
MODELS = [
    {
        "name": "MobileCLIP-S2",
        "open_clip_model": "MobileCLIP-S2",
        "pretrained": "datacomp_s_s13m_b4k",
        "output_file": "mobileclip_s2.onnx",
        "primary": True,
    },
    {
        "name": "CLIP ViT-B/32",
        "open_clip_model": "ViT-B-32",
        "pretrained": "openai",
        "output_file": "clip_vit_b32.onnx",
        "primary": False,
    },
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export CLIP image encoders to ONNX")
    p.add_argument("--output-dir", default="models", help="Directory for exported ONNX files")
    p.add_argument("--test-images", default=".", help="Directory containing test images")
    p.add_argument("--n-test", type=int, default=20, help="Number of test images for parity")
    p.add_argument(
        "--models", nargs="+", default=["all"],
        choices=["all", "mobileclip", "vitb32"],
        help="Which models to export",
    )
    return p.parse_args()


def export_clip_encoder(
    model_name: str,
    pretrained: str,
    output_path: Path,
    opset: int = 17,
) -> tuple:
    """Export a CLIP image encoder to ONNX.

    Returns:
        (model, preprocess) for use in parity testing.
    """
    try:
        import torch
        import open_clip
    except ImportError:
        print("ERROR: open-clip-torch and torch not installed.")
        print("  pip install open-clip-torch torch")
        sys.exit(1)

    print(f"[clip] Loading {model_name} (pretrained={pretrained}) ...")
    try:
        model, _, preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained,
        )
    except Exception as e:
        print(f"WARNING: Failed to load {model_name}: {e}")
        print("  This model may require specific weights. Skipping.")
        return None, None

    model.eval()

    # Create dummy input matching CLIP preprocessing output
    dummy = torch.randn(1, 3, 224, 224)

    # We only export the visual (image) encoder
    class ImageEncoder(torch.nn.Module):
        def __init__(self, clip_model):
            super().__init__()
            self.visual = clip_model.visual

        def forward(self, x):
            return self.visual(x)

    encoder = ImageEncoder(model)
    encoder.eval()

    os.makedirs(output_path.parent, exist_ok=True)

    print(f"[clip] Exporting image encoder to {output_path} ...")
    try:
        torch.onnx.export(
            encoder,
            dummy,
            str(output_path),
            opset_version=opset,
            input_names=["image"],
            output_names=["embedding"],
            dynamic_axes={
                "image": {0: "batch"},
                "embedding": {0: "batch"},
            },
        )
    except Exception as e:
        print(f"WARNING: ONNX export failed for {model_name}: {e}")
        return None, None

    return model, preprocess


def run_pytorch_embeddings(
    model,
    preprocess,
    images: list[np.ndarray],
) -> list[np.ndarray]:
    """Compute L2-normalized embeddings via PyTorch CLIP model."""
    import torch
    import cv2
    from PIL import Image

    embeddings: list[np.ndarray] = []
    for img in images:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        tensor = preprocess(pil_img).unsqueeze(0)

        with torch.no_grad():
            emb = model.encode_image(tensor).cpu().numpy().flatten()

        # L2 normalize
        norm = np.linalg.norm(emb)
        if norm > 0:
            emb = emb / norm
        embeddings.append(emb)

    return embeddings


def run_onnx_embeddings(
    onnx_path: str,
    images: list[np.ndarray],
) -> list[np.ndarray]:
    """Compute L2-normalized embeddings via ONNX Runtime.

    Applies the standard CLIP preprocessing: resize(256) -> center_crop(224)
    -> normalize(CLIP_MEAN, CLIP_STD).
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

    mean = np.array(CLIP_MEAN, dtype=np.float32).reshape(3, 1, 1)
    std = np.array(CLIP_STD, dtype=np.float32).reshape(3, 1, 1)

    embeddings: list[np.ndarray] = []
    for img in images:
        # Resize shortest edge to 256
        h, w = img.shape[:2]
        if h < w:
            new_h, new_w = 256, int(256 * w / h)
        else:
            new_h, new_w = int(256 * h / w), 256
        resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_CUBIC)

        # Center crop 224x224
        ch, cw = resized.shape[:2]
        top = (ch - 224) // 2
        left = (cw - 224) // 2
        cropped = resized[top:top + 224, left:left + 224]

        # BGR -> RGB, HWC -> CHW, normalize
        rgb = cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        chw = rgb.transpose(2, 0, 1)  # [3, 224, 224]
        chw = (chw - mean) / std
        batch = np.expand_dims(chw, 0)  # [1, 3, 224, 224]

        # Inference
        outputs = sess.run(None, {input_name: batch})
        emb = outputs[0].flatten()

        # L2 normalize
        norm = np.linalg.norm(emb)
        if norm > 0:
            emb = emb / norm
        embeddings.append(emb)

    return embeddings


def main() -> None:
    args = parse_args()

    sys.path.insert(0, str(Path(__file__).parent))
    from parity_test import load_test_images, run_parity_gate, print_parity_report

    print("=" * 60)
    print("  CLIP Image Encoder ONNX Export")
    print("=" * 60)

    # Determine which models to export
    selected = MODELS
    if "all" not in args.models:
        name_map = {"mobileclip": "MobileCLIP-S2", "vitb32": "CLIP ViT-B/32"}
        selected_names = {name_map[m] for m in args.models}
        selected = [m for m in MODELS if m["name"] in selected_names]

    images = load_test_images(args.test_images, n=args.n_test)
    all_results: list[dict] = []

    for spec in selected:
        print(f"\n{'~' * 50}")
        tag = "PRIMARY" if spec["primary"] else "FALLBACK"
        print(f"  [{tag}] {spec['name']}")
        print(f"{'~' * 50}")

        output_path = Path(args.output_dir) / spec["output_file"]

        model, preprocess = export_clip_encoder(
            spec["open_clip_model"],
            spec["pretrained"],
            output_path,
        )

        if model is None:
            print(f"  SKIPPED -- export failed for {spec['name']}")
            all_results.append({
                "name": spec["name"],
                "passed": False,
                "min_score": 0.0,
                "mean_score": 0.0,
                "threshold": 0.98,
                "n_samples": 0,
                "scores": [],
            })
            if spec["primary"]:
                print(
                    "  WARNING: Primary model failed. "
                    "The ViT-B/32 fallback should still work for mobile."
                )
            continue

        # Model info
        size_mb = output_path.stat().st_size / (1024 * 1024)
        print(f"\n  Export complete:")
        print(f"    File       : {output_path}")
        print(f"    Size       : {size_mb:.1f} MB")
        print(f"    Input shape: [1, 3, 224, 224] (NCHW, RGB, float32)")
        print(f"    Normalize  : mean={CLIP_MEAN}, std={CLIP_STD}")
        print(f"    Output     : embedding vector (L2-normalize before use)")

        # Parity test
        print(f"\n  Running parity test on {len(images)} images ...")
        pt_embs = run_pytorch_embeddings(model, preprocess, images)
        ox_embs = run_onnx_embeddings(str(output_path), images)

        result = run_parity_gate(spec["name"], pt_embs, ox_embs, threshold=0.98)
        all_results.append(result)

        if not result["passed"] and spec["primary"]:
            print(
                f"  WARNING: {spec['name']} parity FAILED. "
                "Consider using the ViT-B/32 fallback for mobile."
            )

    print_parity_report(all_results)

    # Overall summary
    passed = all(r["passed"] for r in all_results)
    if passed:
        print("[clip] ALL PARITY GATES PASSED")
    else:
        failed = [r["name"] for r in all_results if not r["passed"]]
        print(f"[clip] PARITY FAILED for: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
