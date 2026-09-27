#!/usr/bin/env python3
"""Download and convert InsightFace buffalo_sc recognition model.

Exports the InsightFace ArcFace MobileFaceNet recognition model from the
buffalo_sc model pack to ONNX, CoreML, and TFLite for cross-platform
face recognition in the FlightRisk pipeline.

The buffalo_sc pack ships the recognition model as ONNX natively
(w600k_mbf.onnx), so the ONNX step is a download + copy. CoreML and
TFLite conversions are derived from the ONNX.

Preprocessing reference
-----------------------
  Face detection : detect face, extract 5-point landmarks
  Alignment      : affine warp to 112x112 using standard ArcFace template
  Input tensor   : float32 [1, 3, 112, 112] (NCHW, RGB)
  Normalize      : (pixel - 127.5) / 127.5   (maps 0-255 to -1..1)
                   NOTE: do NOT divide by 255 first
  Output         : float32 [1, 512] embedding
  Post-process   : L2-normalize before cosine comparison

  ArcFace alignment template (5 landmarks for 112x112)::
      dst = np.array([
          [38.2946, 51.6963],   # left eye
          [73.5318, 51.5014],   # right eye
          [56.0252, 71.7366],   # nose tip
          [41.5493, 92.3655],   # left mouth corner
          [70.7299, 92.2041],   # right mouth corner
      ], dtype=np.float32)

Usage:
    pip install insightface onnxruntime coremltools onnx2tf
    python scripts/convert_insightface.py

Output:
    models/insightface_r18.onnx
    ios/FlightRisk/Models/InsightFaceR18.mlpackage
    mobile/app/src/main/assets/insightface_r18.tflite
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

ONNX_FILENAME = "insightface_r18.onnx"
COREML_FILENAME = "InsightFaceR18"
TFLITE_FILENAME = "insightface_r18.tflite"

INPUT_SHAPE = (1, 3, 112, 112)  # batch, channels, height, width
OUTPUT_DIM = 512


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Convert InsightFace buffalo_sc to ONNX/CoreML/TFLite")
    p.add_argument("--output-dir", default=str(MODELS_DIR), help="Directory for ONNX output")
    p.add_argument("--model-pack", default="buffalo_sc", help="InsightFace model pack name")
    p.add_argument("--skip-coreml", action="store_true", help="Skip CoreML conversion")
    p.add_argument("--skip-tflite", action="store_true", help="Skip TFLite conversion")
    p.add_argument("--verify", action="store_true", default=True, help="Run ONNX inference verification")
    return p.parse_args()


def locate_recognition_onnx(model_pack: str) -> Path | None:
    """Find the recognition ONNX inside an InsightFace model pack.

    InsightFace downloads model packs to ``~/.insightface/models/<pack>/``.
    The recognition model is typically named ``w600k_mbf.onnx`` (MobileFaceNet,
    trained on WebFace600K).

    Returns:
        Path to the recognition ONNX, or None if not found.
    """
    base = Path.home() / ".insightface" / "models" / model_pack
    if not base.exists():
        return None

    # Common recognition model filenames in InsightFace packs
    candidates = [
        "w600k_mbf.onnx",       # buffalo_sc MobileFaceNet (primary)
        "w600k_r50.onnx",       # buffalo_l ResNet-50
        "glintr100.onnx",       # buffalo_l ArcFace-R100
    ]
    for name in candidates:
        p = base / name
        if p.exists():
            return p

    # Fallback: any .onnx that is not a detection model
    for f in sorted(base.glob("*.onnx")):
        if not f.name.startswith("det_"):
            return f

    return None


def download_model_pack(model_pack: str) -> bool:
    """Trigger InsightFace model pack download via FaceAnalysis initialization.

    Returns:
        True if the pack was downloaded or already exists, False on failure.
    """
    try:
        from insightface.app import FaceAnalysis
    except ImportError:
        print("[insightface] ERROR: insightface not installed.")
        print("  pip install insightface onnxruntime")
        return False

    print(f"[insightface] Initializing FaceAnalysis({model_pack}) to ensure download ...")
    try:
        app = FaceAnalysis(
            name=model_pack,
            providers=["CPUExecutionProvider"],
        )
        app.prepare(ctx_id=-1, det_size=(320, 320))
        print(f"[insightface] Model pack {model_pack} ready")
        return True
    except Exception as e:
        print(f"[insightface] ERROR: Failed to initialize: {e}")
        print(f"  Expected location: ~/.insightface/models/{model_pack}/")
        return False


def export_to_onnx(model_pack: str, output_dir: str) -> Path | None:
    """Copy the recognition ONNX from the InsightFace model pack.

    The InsightFace buffalo_sc pack ships the recognition model as ONNX
    natively, so this is a download + copy rather than a conversion.

    Returns:
        Path to the copied ONNX file, or None on failure.
    """
    os.makedirs(output_dir, exist_ok=True)
    onnx_path = Path(output_dir) / ONNX_FILENAME

    # Ensure model pack is downloaded
    if not download_model_pack(model_pack):
        return None

    # Locate the recognition ONNX
    src = locate_recognition_onnx(model_pack)
    if src is None:
        print(f"[insightface] ERROR: Recognition ONNX not found in {model_pack} pack")
        pack_dir = Path.home() / ".insightface" / "models" / model_pack
        if pack_dir.exists():
            print(f"  Contents of {pack_dir}:")
            for f in sorted(pack_dir.iterdir()):
                print(f"    {f.name} ({f.stat().st_size / 1024:.0f} KB)")
        return None

    shutil.copy2(str(src), str(onnx_path))
    size_mb = onnx_path.stat().st_size / (1024 * 1024)
    print(f"[insightface] Copied {src.name} -> {onnx_path} ({size_mb:.1f} MB)")
    return onnx_path


def verify_onnx(onnx_path: Path) -> bool:
    """Run a quick inference through the ONNX model to verify output shape."""
    try:
        import onnxruntime as ort
    except ImportError:
        print("[insightface] WARNING: onnxruntime not installed, skipping verification")
        return True

    print(f"[insightface] Verifying ONNX inference ...")
    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])

    inp = sess.get_inputs()[0]
    out = sess.get_outputs()[0]
    print(f"  Input : {inp.name} shape={inp.shape} dtype={inp.type}")
    print(f"  Output: {out.name} shape={out.shape} dtype={out.type}")

    # Create a dummy aligned face: (pixel - 127.5) / 127.5
    dummy = np.random.randint(0, 256, size=(112, 112, 3)).astype(np.float32)
    normalized = (dummy - 127.5) / 127.5
    chw = normalized.transpose(2, 0, 1)  # [3, 112, 112]
    batch = np.expand_dims(chw, 0)       # [1, 3, 112, 112]

    result = sess.run(None, {inp.name: batch})
    embedding = result[0]
    print(f"  Inference output shape: {embedding.shape}")

    if embedding.shape[-1] != OUTPUT_DIM:
        print(f"  ERROR: Expected {OUTPUT_DIM}-d embedding, got {embedding.shape[-1]}-d")
        return False

    if np.allclose(embedding, 0.0):
        print("  WARNING: All-zero embedding -- model may not be loaded correctly")
        return False

    # Check embedding norm (should be non-trivial)
    norm = np.linalg.norm(embedding)
    print(f"  Embedding L2 norm: {norm:.4f}")
    print(f"  Verification passed: {OUTPUT_DIM}-d embedding produced")
    return True


def convert_to_coreml(onnx_path: Path) -> Path | None:
    """Convert ONNX to CoreML .mlpackage.

    Uses onnx2torch -> coremltools (preferred) or direct coremltools ONNX
    conversion as fallback.

    Returns:
        Path to the .mlpackage, or None on failure.
    """
    try:
        import coremltools as ct
    except ImportError:
        print("[insightface] ERROR: coremltools not installed. pip install coremltools")
        return None

    os.makedirs(str(IOS_MODELS_DIR), exist_ok=True)
    mlpackage_path = IOS_MODELS_DIR / f"{COREML_FILENAME}.mlpackage"

    # Strategy 1: ONNX -> PyTorch -> CoreML
    try:
        import torch
        from onnx2torch import convert as onnx_to_torch

        print(f"[insightface] Converting ONNX -> PyTorch -> CoreML ...")
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
        print(f"[insightface] CoreML saved: {mlpackage_path}")

        _try_compile_coreml(mlpackage_path)
        return mlpackage_path

    except ImportError:
        print("[insightface] onnx2torch not available, trying direct ONNX -> CoreML ...")
    except Exception as e:
        print(f"[insightface] onnx2torch path failed: {e}")
        print("[insightface] Falling back to direct ONNX -> CoreML ...")

    # Strategy 2: Direct ONNX -> CoreML
    try:
        model = ct.converters.onnx.convert(
            model=str(onnx_path),
            minimum_deployment_target=ct.target.iOS17,
            convert_to="mlprogram",
            compute_precision=ct.precision.FLOAT16,
        )
        model.save(str(mlpackage_path))
        print(f"[insightface] CoreML saved (direct): {mlpackage_path}")

        _try_compile_coreml(mlpackage_path)
        return mlpackage_path
    except Exception as e:
        print(f"[insightface] ERROR: CoreML conversion failed: {e}")
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
        print(f"[insightface] NOTE: coremlcompiler not available -- Xcode will compile at build time")
    else:
        print(f"[insightface] Compiled to {mlmodelc_path}")


def convert_to_tflite(onnx_path: Path) -> Path | None:
    """Convert ONNX to TFLite via TF SavedModel intermediate.

    Tries onnx2tf first (recommended), then onnx-tf as fallback.

    Returns:
        Path to the .tflite file, or None on failure.
    """
    os.makedirs(str(ANDROID_ASSETS_DIR), exist_ok=True)
    tflite_path = ANDROID_ASSETS_DIR / TFLITE_FILENAME
    savedmodel_dir = MODELS_DIR / "insightface_savedmodel"

    # Strategy 1: onnx2tf
    try:
        import onnx2tf

        print(f"[insightface] Converting ONNX -> TFLite via onnx2tf ...")
        onnx2tf.convert(
            input_onnx_file_path=str(onnx_path),
            output_folder_path=str(savedmodel_dir),
            non_verbose=True,
        )

        generated_tflite = list(savedmodel_dir.glob("*.tflite"))
        if generated_tflite:
            shutil.copy2(str(generated_tflite[0]), str(tflite_path))
            print(f"[insightface] TFLite saved: {tflite_path}")
            _cleanup_savedmodel(savedmodel_dir)
            return tflite_path

        return _savedmodel_to_tflite(savedmodel_dir, tflite_path)

    except ImportError:
        pass
    except Exception as e:
        print(f"[insightface] onnx2tf failed: {e}")

    # Strategy 2: onnx-tf
    try:
        from onnx_tf.backend import prepare
        import onnx
        import tensorflow as tf

        print(f"[insightface] Converting ONNX -> TF SavedModel via onnx-tf ...")
        onnx_model = onnx.load(str(onnx_path))
        tf_rep = prepare(onnx_model)
        tf_rep.export_graph(str(savedmodel_dir))
        print(f"[insightface] SavedModel exported to {savedmodel_dir}")

        return _savedmodel_to_tflite(savedmodel_dir, tflite_path)

    except ImportError:
        print("[insightface] ERROR: Neither onnx2tf nor onnx-tf installed.")
        print("  pip install onnx2tf  (recommended)")
        print("  pip install onnx-tf  (alternative)")
        return None
    except Exception as e:
        print(f"[insightface] ERROR: TFLite conversion failed: {e}")
        _cleanup_savedmodel(savedmodel_dir)
        return None


def _savedmodel_to_tflite(savedmodel_dir: Path, tflite_path: Path) -> Path | None:
    """Convert a TF SavedModel to TFLite."""
    try:
        import tensorflow as tf

        print(f"[insightface] Converting SavedModel -> TFLite ...")
        converter = tf.lite.TFLiteConverter.from_saved_model(str(savedmodel_dir))
        converter.optimizations = [tf.lite.Optimize.DEFAULT]
        converter.target_spec.supported_types = [tf.float16]
        tflite_model = converter.convert()

        tflite_path.parent.mkdir(parents=True, exist_ok=True)
        tflite_path.write_bytes(tflite_model)
        size_mb = tflite_path.stat().st_size / (1024 * 1024)
        print(f"[insightface] TFLite saved: {tflite_path} ({size_mb:.1f} MB)")

        _cleanup_savedmodel(savedmodel_dir)
        return tflite_path

    except ImportError:
        print("[insightface] ERROR: TensorFlow not installed for TFLite conversion")
        return None
    except Exception as e:
        print(f"[insightface] ERROR: SavedModel -> TFLite conversion failed: {e}")
        _cleanup_savedmodel(savedmodel_dir)
        return None


def _cleanup_savedmodel(savedmodel_dir: Path) -> None:
    """Remove temporary SavedModel directory."""
    if savedmodel_dir.exists():
        shutil.rmtree(savedmodel_dir, ignore_errors=True)


def print_summary(onnx_path: Path | None, coreml_path: Path | None, tflite_path: Path | None) -> None:
    """Print conversion summary with file sizes."""
    print("\n" + "=" * 60)
    print("  InsightFace buffalo_sc Conversion Summary")
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
                size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            else:
                size = path.stat().st_size
            size_mb = size / (1024 * 1024)
            print(f"  {label:20s}: {path} ({size_mb:.1f} MB)")
        else:
            print(f"  {label:20s}: {path} (NOT FOUND)")

    print()
    print("  Input shape  : [1, 3, 112, 112] (NCHW, RGB, float32)")
    print("  Normalize    : (pixel - 127.5) / 127.5  (maps 0-255 to -1..1)")
    print("  Output       : [1, 512] embedding (L2-normalize before use)")
    print("  Alignment    : 5-point landmark affine warp to 112x112")
    print()


def main() -> None:
    args = parse_args()

    print("=" * 60)
    print("  InsightFace buffalo_sc Model Conversion")
    print("=" * 60)

    # Step 1: Download and copy the ONNX
    onnx_path = export_to_onnx(args.model_pack, args.output_dir)
    if onnx_path is None:
        print("[insightface] ONNX export failed -- cannot proceed with conversions")
        sys.exit(1)

    # Step 2: Verify ONNX
    if args.verify:
        if not verify_onnx(onnx_path):
            print("[insightface] ONNX verification failed -- aborting further conversions")
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
