#!/usr/bin/env python3
"""Download and convert OSNet x1.0 for person re-identification.

Exports OSNet x1.0 (pretrained on ImageNet) to ONNX, CoreML, and TFLite
for cross-platform person re-identification in the FlightRisk pipeline.

OSNet (Omni-Scale Network) produces 512-d feature embeddings for person
re-identification. The x1.0 variant balances accuracy and speed for
real-time mobile inference.

Preprocessing reference
-----------------------
  Input tensor : float32 [1, 3, 256, 128] (NCHW, RGB)
  Normalize    : ImageNet stats
                   mean = [0.485, 0.456, 0.406]
                   std  = [0.229, 0.224, 0.225]
                 i.e. pixel/255.0, then (channel - mean) / std
  Output       : float32 [1, 512] embedding
  Post-process : L2-normalize before cosine comparison

Usage:
    pip install torchreid coremltools onnxruntime onnx2tf
    python scripts/convert_osnet.py

    # Or install torchreid from GitHub:
    pip install git+https://github.com/KaiyangZhou/deep-person-reid.git

Output:
    models/osnet_x1_0.onnx
    ios/FlightRisk/Models/OSNetReID.mlpackage
    mobile/app/src/main/assets/osnet_x1_0.tflite
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / "models"
IOS_MODELS_DIR = REPO_ROOT / "ios" / "FlightRisk" / "Models"
ANDROID_ASSETS_DIR = REPO_ROOT / "mobile" / "app" / "src" / "main" / "assets"

ONNX_FILENAME = "osnet_x1_0.onnx"
COREML_FILENAME = "OSNetReID"
TFLITE_FILENAME = "osnet_x1_0.tflite"

INPUT_SHAPE = (1, 3, 256, 128)  # batch, channels, height, width
OUTPUT_DIM = 512


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Convert OSNet x1.0 to ONNX/CoreML/TFLite")
    p.add_argument("--output-dir", default=str(MODELS_DIR), help="Directory for ONNX output")
    p.add_argument("--skip-coreml", action="store_true", help="Skip CoreML conversion")
    p.add_argument("--skip-tflite", action="store_true", help="Skip TFLite conversion")
    p.add_argument("--verify", action="store_true", default=True, help="Run ONNX inference verification")
    return p.parse_args()


def load_osnet_model():
    """Load pretrained OSNet x1.0 using torchreid.

    Tries the torchreid.models API first (more control), then falls back
    to FeatureExtractor.

    Returns:
        (torch.nn.Module, torch device)
    """
    try:
        import torch
    except ImportError:
        print("ERROR: PyTorch not installed. pip install torch")
        sys.exit(1)

    device = torch.device("cpu")

    # Strategy 1: build_model + pretrained weights
    try:
        import torchreid
        model = torchreid.models.build_model(
            name="osnet_x1_0",
            num_classes=1000,
            pretrained=True,
        )
        model = model.to(device)
        model.eval()
        print("[osnet] Loaded OSNet x1.0 via torchreid.models.build_model")
        return model, device
    except ImportError:
        pass
    except Exception as e:
        print(f"[osnet] build_model failed: {e}, trying FeatureExtractor ...")

    # Strategy 2: FeatureExtractor (downloads weights automatically)
    try:
        from torchreid.utils import FeatureExtractor
        extractor = FeatureExtractor(
            model_name="osnet_x1_0",
            model_path="auto",
            device="cpu",
        )
        model = extractor.model
        model.eval()
        print("[osnet] Loaded OSNet x1.0 via FeatureExtractor")
        return model, device
    except ImportError:
        print("ERROR: torchreid not installed.")
        print("  pip install torchreid")
        print("  or: pip install git+https://github.com/KaiyangZhou/deep-person-reid.git")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to load OSNet x1.0: {e}")
        sys.exit(1)


def export_to_onnx(model, device, output_dir: str) -> Path:
    """Export OSNet to ONNX format.

    Args:
        model: The loaded PyTorch model.
        device: torch device.
        output_dir: Directory to save the ONNX file.

    Returns:
        Path to the exported ONNX file.
    """
    import torch

    os.makedirs(output_dir, exist_ok=True)
    onnx_path = Path(output_dir) / ONNX_FILENAME

    dummy_input = torch.randn(*INPUT_SHAPE, device=device)

    # Some OSNet builds return a tuple; wrap to extract the embedding
    with torch.no_grad():
        test_out = model(dummy_input)
    if isinstance(test_out, (tuple, list)):
        print(f"[osnet] Model returns tuple of length {len(test_out)}, wrapping to extract embedding ...")

        class OSNetWrapper(torch.nn.Module):
            def __init__(self, base_model):
                super().__init__()
                self.base = base_model

            def forward(self, x):
                out = self.base(x)
                if isinstance(out, (tuple, list)):
                    return out[0]
                return out

        model = OSNetWrapper(model)
        model.eval()

    print(f"[osnet] Exporting to ONNX: {onnx_path}")
    torch.onnx.export(
        model,
        dummy_input,
        str(onnx_path),
        input_names=["input"],
        output_names=["embedding"],
        dynamic_axes={"input": {0: "batch"}, "embedding": {0: "batch"}},
        opset_version=13,
        do_constant_folding=True,
    )

    size_mb = onnx_path.stat().st_size / (1024 * 1024)
    print(f"[osnet] ONNX exported: {onnx_path} ({size_mb:.1f} MB)")
    return onnx_path


def verify_onnx(onnx_path: Path) -> bool:
    """Run a quick inference through the ONNX model to verify output shape."""
    try:
        import onnxruntime as ort
    except ImportError:
        print("[osnet] WARNING: onnxruntime not installed, skipping ONNX verification")
        return True

    print(f"[osnet] Verifying ONNX inference ...")
    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])

    inp = sess.get_inputs()[0]
    out = sess.get_outputs()[0]
    print(f"  Input : {inp.name} shape={inp.shape} dtype={inp.type}")
    print(f"  Output: {out.name} shape={out.shape} dtype={out.type}")

    dummy = np.random.randn(*INPUT_SHAPE).astype(np.float32)
    result = sess.run(None, {inp.name: dummy})
    embedding = result[0]
    print(f"  Inference output shape: {embedding.shape}")

    if embedding.shape[-1] != OUTPUT_DIM:
        print(f"  ERROR: Expected {OUTPUT_DIM}-d embedding, got {embedding.shape[-1]}-d")
        return False

    # Check that outputs are not all zeros (model is producing features)
    if np.allclose(embedding, 0.0):
        print("  WARNING: All-zero embedding -- model may not be loaded correctly")
        return False

    print(f"  Verification passed: {OUTPUT_DIM}-d embedding produced")
    return True


def convert_to_coreml(onnx_path: Path) -> Path | None:
    """Convert ONNX to CoreML .mlpackage.

    Uses onnx2torch to load into PyTorch, then coremltools to convert.
    Falls back to direct coremltools ONNX conversion if onnx2torch fails.

    Returns:
        Path to the .mlpackage, or None on failure.
    """
    try:
        import coremltools as ct
    except ImportError:
        print("[osnet] ERROR: coremltools not installed. pip install coremltools")
        return None

    os.makedirs(str(IOS_MODELS_DIR), exist_ok=True)
    mlpackage_path = IOS_MODELS_DIR / f"{COREML_FILENAME}.mlpackage"

    # Strategy 1: ONNX -> PyTorch -> CoreML (preferred, better optimization)
    try:
        import torch
        from onnx2torch import convert as onnx_to_torch

        print(f"[osnet] Converting ONNX -> PyTorch -> CoreML ...")
        torch_model = onnx_to_torch(str(onnx_path))
        torch_model.eval()

        example_input = torch.randn(*INPUT_SHAPE)
        traced = torch.jit.trace(torch_model, example_input)

        model = ct.convert(
            traced,
            inputs=[ct.TensorType(name="input", shape=INPUT_SHAPE)],
            convert_to="mlprogram",
            minimum_deployment_target=ct.target.iOS17,
            compute_precision=ct.precision.FLOAT16,
        )
        model.save(str(mlpackage_path))
        print(f"[osnet] CoreML saved: {mlpackage_path}")

        _try_compile_coreml(mlpackage_path)
        return mlpackage_path

    except ImportError:
        print("[osnet] onnx2torch not available, trying direct ONNX -> CoreML ...")
    except Exception as e:
        print(f"[osnet] onnx2torch path failed: {e}")
        print("[osnet] Falling back to direct ONNX -> CoreML ...")

    # Strategy 2: Direct ONNX -> CoreML
    try:
        model = ct.converters.onnx.convert(
            model=str(onnx_path),
            minimum_deployment_target=ct.target.iOS17,
            convert_to="mlprogram",
            compute_precision=ct.precision.FLOAT16,
        )
        model.save(str(mlpackage_path))
        print(f"[osnet] CoreML saved (direct): {mlpackage_path}")

        _try_compile_coreml(mlpackage_path)
        return mlpackage_path
    except Exception as e:
        print(f"[osnet] ERROR: CoreML conversion failed: {e}")
        return None


def _try_compile_coreml(mlpackage_path: Path) -> None:
    """Try to compile .mlpackage to .mlmodelc using xcrun (macOS only)."""
    mlmodelc_path = mlpackage_path.with_suffix(".mlmodelc")
    if mlmodelc_path.exists():
        shutil.rmtree(mlmodelc_path)

    result = subprocess.run(
        ["xcrun", "coremlcompiler", "compile", str(mlpackage_path), str(mlpackage_path.parent)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[osnet] NOTE: coremlcompiler not available -- Xcode will compile at build time")
    else:
        print(f"[osnet] Compiled to {mlmodelc_path}")


def convert_to_tflite(onnx_path: Path) -> Path | None:
    """Convert ONNX to TFLite via TensorFlow SavedModel intermediate.

    Tries multiple conversion paths in order:
      1. onnx2tf (recommended, handles most ONNX ops well)
      2. onnx-tf (older but widely available)

    Returns:
        Path to the .tflite file, or None on failure.
    """
    os.makedirs(str(ANDROID_ASSETS_DIR), exist_ok=True)
    tflite_path = ANDROID_ASSETS_DIR / TFLITE_FILENAME
    savedmodel_dir = MODELS_DIR / "osnet_savedmodel"

    # Strategy 1: onnx2tf (preferred)
    try:
        import onnx2tf

        print(f"[osnet] Converting ONNX -> TFLite via onnx2tf ...")
        onnx2tf.convert(
            input_onnx_file_path=str(onnx_path),
            output_folder_path=str(savedmodel_dir),
            non_verbose=True,
        )

        # onnx2tf generates TFLite files in the output folder
        generated_tflite = list(savedmodel_dir.glob("*.tflite"))
        if generated_tflite:
            shutil.copy2(str(generated_tflite[0]), str(tflite_path))
            print(f"[osnet] TFLite saved: {tflite_path}")
            _cleanup_savedmodel(savedmodel_dir)
            return tflite_path

        # If no TFLite directly, try converting the SavedModel
        return _savedmodel_to_tflite(savedmodel_dir, tflite_path)

    except ImportError:
        pass
    except Exception as e:
        print(f"[osnet] onnx2tf failed: {e}")

    # Strategy 2: onnx-tf
    try:
        from onnx_tf.backend import prepare
        import onnx
        import tensorflow as tf

        print(f"[osnet] Converting ONNX -> TF SavedModel via onnx-tf ...")
        onnx_model = onnx.load(str(onnx_path))
        tf_rep = prepare(onnx_model)
        tf_rep.export_graph(str(savedmodel_dir))
        print(f"[osnet] SavedModel exported to {savedmodel_dir}")

        return _savedmodel_to_tflite(savedmodel_dir, tflite_path)

    except ImportError:
        print("[osnet] ERROR: Neither onnx2tf nor onnx-tf installed.")
        print("  pip install onnx2tf  (recommended)")
        print("  pip install onnx-tf  (alternative)")
        return None
    except Exception as e:
        print(f"[osnet] ERROR: TFLite conversion failed: {e}")
        _cleanup_savedmodel(savedmodel_dir)
        return None


def _savedmodel_to_tflite(savedmodel_dir: Path, tflite_path: Path) -> Path | None:
    """Convert a TF SavedModel to TFLite."""
    try:
        import tensorflow as tf

        print(f"[osnet] Converting SavedModel -> TFLite ...")
        converter = tf.lite.TFLiteConverter.from_saved_model(str(savedmodel_dir))
        converter.optimizations = [tf.lite.Optimize.DEFAULT]
        converter.target_spec.supported_types = [tf.float16]
        tflite_model = converter.convert()

        tflite_path.parent.mkdir(parents=True, exist_ok=True)
        tflite_path.write_bytes(tflite_model)
        size_mb = tflite_path.stat().st_size / (1024 * 1024)
        print(f"[osnet] TFLite saved: {tflite_path} ({size_mb:.1f} MB)")

        _cleanup_savedmodel(savedmodel_dir)
        return tflite_path

    except ImportError:
        print("[osnet] ERROR: TensorFlow not installed for TFLite conversion")
        return None
    except Exception as e:
        print(f"[osnet] ERROR: SavedModel -> TFLite conversion failed: {e}")
        _cleanup_savedmodel(savedmodel_dir)
        return None


def _cleanup_savedmodel(savedmodel_dir: Path) -> None:
    """Remove temporary SavedModel directory."""
    if savedmodel_dir.exists():
        shutil.rmtree(savedmodel_dir, ignore_errors=True)


def print_summary(onnx_path: Path | None, coreml_path: Path | None, tflite_path: Path | None) -> None:
    """Print conversion summary with file sizes."""
    print("\n" + "=" * 60)
    print("  OSNet x1.0 Conversion Summary")
    print("=" * 60)

    outputs = [
        ("ONNX (Python)", onnx_path),
        ("CoreML (iOS)", coreml_path),
        ("TFLite (Android)", tflite_path),
    ]

    for label, path in outputs:
        if path is None:
            print(f"  {label:20s}: SKIPPED / FAILED")
        elif path.exists():
            if path.is_dir():
                # .mlpackage is a directory
                size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            else:
                size = path.stat().st_size
            size_mb = size / (1024 * 1024)
            print(f"  {label:20s}: {path} ({size_mb:.1f} MB)")
        else:
            print(f"  {label:20s}: {path} (NOT FOUND)")

    print()
    print("  Input shape  : [1, 3, 256, 128] (NCHW, RGB, float32)")
    print("  Normalize    : ImageNet (mean=[0.485,0.456,0.406], std=[0.229,0.224,0.225])")
    print("  Output       : [1, 512] embedding (L2-normalize before use)")
    print()


def main() -> None:
    args = parse_args()

    print("=" * 60)
    print("  OSNet x1.0 Person Re-ID Model Conversion")
    print("=" * 60)

    # Step 1: Load model and export to ONNX
    model, device = load_osnet_model()
    onnx_path = export_to_onnx(model, device, args.output_dir)

    # Step 2: Verify ONNX
    if args.verify:
        if not verify_onnx(onnx_path):
            print("[osnet] ONNX verification failed -- aborting further conversions")
            sys.exit(1)

    # Step 3: Convert to CoreML
    coreml_path = None
    if not args.skip_coreml:
        coreml_path = convert_to_coreml(onnx_path)

    # Step 4: Convert to TFLite
    tflite_path = None
    if not args.skip_tflite:
        tflite_path = convert_to_tflite(onnx_path)

    # Summary
    print_summary(onnx_path, coreml_path, tflite_path)


if __name__ == "__main__":
    main()
