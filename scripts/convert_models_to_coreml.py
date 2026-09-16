#!/usr/bin/env python3
"""Convert ONNX vision models to CoreML for iOS.

Converts the 4 ONNX models from mobile/app/src/main/assets/ to .mlpackage
format via PyTorch (ONNX→PyTorch→CoreML), then compiles to .mlmodelc bundles.

Usage:
    pip install coremltools torch onnx onnx2torch
    python scripts/convert_models_to_coreml.py

Output:
    ios/FlightRisk/Models/YOLOPersonDetector.mlmodelc
    ios/FlightRisk/Models/CLIPVisual.mlmodelc
    ios/FlightRisk/Models/SCRFDFaceDetector.mlmodelc
    ios/FlightRisk/Models/ArcFaceMobile.mlmodelc
"""

import os
import sys
import shutil
import subprocess

try:
    import coremltools as ct
    import torch
    import onnx
    from onnx2torch import convert as onnx_to_torch
except ImportError as e:
    print(f"ERROR: missing dependency: {e}")
    print("Run: pip install coremltools torch onnx onnx2torch")
    sys.exit(1)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ONNX_DIR = os.path.join(REPO_ROOT, "mobile", "app", "src", "main", "assets")
OUTPUT_DIR = os.path.join(REPO_ROOT, "ios", "FlightRisk", "Models")

MODELS = [
    {
        "onnx": "yolo11n.onnx",
        "output": "YOLOPersonDetector",
        "input_shape": (1, 3, 640, 640),
    },
    {
        "onnx": "clip_visual.onnx",
        "output": "CLIPVisual",
        "input_shape": (1, 3, 224, 224),
    },
    {
        "onnx": "scrfd_500m.onnx",
        "output": "SCRFDFaceDetector",
        "input_shape": (1, 3, 640, 640),
    },
    {
        "onnx": "arcface_mobilefacenet.onnx",
        "output": "ArcFaceMobile",
        "input_shape": (1, 3, 112, 112),
    },
]


def convert_model(spec):
    onnx_path = os.path.join(ONNX_DIR, spec["onnx"])
    if not os.path.exists(onnx_path):
        print(f"  SKIP: {onnx_path} not found")
        return False

    print(f"  Converting {spec['onnx']} -> {spec['output']}.mlpackage ...")

    torch_model = onnx_to_torch(onnx_path)
    torch_model.eval()

    example_input = torch.randn(*spec["input_shape"])
    traced = torch.jit.trace(torch_model, example_input)

    model = ct.convert(
        traced,
        inputs=[ct.TensorType(shape=spec["input_shape"])],
        convert_to="mlprogram",
        minimum_deployment_target=ct.target.iOS17,
        compute_precision=ct.precision.FLOAT16,
    )

    mlpackage_path = os.path.join(OUTPUT_DIR, f"{spec['output']}.mlpackage")
    model.save(mlpackage_path)
    print(f"  Saved {mlpackage_path}")

    mlmodelc_path = os.path.join(OUTPUT_DIR, f"{spec['output']}.mlmodelc")
    if os.path.exists(mlmodelc_path):
        shutil.rmtree(mlmodelc_path)

    result = subprocess.run(
        ["xcrun", "coremlcompiler", "compile", mlpackage_path, OUTPUT_DIR],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  WARNING: coremlcompiler failed: {result.stderr}")
        print(f"  The .mlpackage is saved — Xcode will compile it at build time.")
    else:
        print(f"  Compiled to {mlmodelc_path}")
        shutil.rmtree(mlpackage_path)

    return True


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"ONNX source: {ONNX_DIR}")
    print(f"Output dir:  {OUTPUT_DIR}")
    print()

    success = 0
    for spec in MODELS:
        try:
            if convert_model(spec):
                success += 1
        except Exception as e:
            print(f"  ERROR converting {spec['onnx']}: {e}")

    print(f"\n{success}/{len(MODELS)} models converted.")
    if success < len(MODELS):
        print("Missing models will cause graceful degradation (no crashes).")


if __name__ == "__main__":
    main()
