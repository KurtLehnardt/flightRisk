#!/usr/bin/env bash
# Download and convert OSNet + InsightFace models for FlightRisk.
#
# Creates an isolated Python venv, installs dependencies, and runs both
# conversion scripts to produce ONNX, CoreML, and TFLite model files.
#
# Usage:
#   bash scripts/download_matching_models.sh
#
# Output:
#   models/osnet_x1_0.onnx                              (Person ReID)
#   models/insightface_r18.onnx                          (Face recognition)
#   ios/FlightRisk/Models/OSNetReID.mlpackage             (iOS)
#   ios/FlightRisk/Models/InsightFaceR18.mlpackage        (iOS)
#   mobile/app/src/main/assets/osnet_x1_0.tflite          (Android)
#   mobile/app/src/main/assets/insightface_r18.tflite     (Android)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv-model-convert"

echo "============================================================"
echo "  FlightRisk — Matching Model Download & Conversion"
echo "============================================================"
echo ""
echo "Repo root: $REPO_ROOT"
echo ""

# ── Step 1: Create venv ─────────────────────────────────────────
if [ -d "$VENV_DIR" ]; then
    echo "[venv] Using existing venv: $VENV_DIR"
else
    echo "[venv] Creating Python venv: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "[venv] Python: $(which python3)"
echo "[venv] Version: $(python3 --version)"
echo ""

# ── Step 2: Install dependencies ────────────────────────────────
echo "[deps] Installing dependencies ..."
echo ""

pip install --quiet --upgrade pip

# Core ML tools
pip install --quiet torch torchvision onnx onnxruntime coremltools onnx2torch

# torchreid (OSNet) — try PyPI first, then GitHub
if ! pip install --quiet torchreid 2>/dev/null; then
    echo "[deps] torchreid not on PyPI, installing from GitHub ..."
    pip install --quiet "git+https://github.com/KaiyangZhou/deep-person-reid.git"
fi

# InsightFace
pip install --quiet insightface opencv-python-headless numpy

# TFLite conversion (optional — may fail on some platforms)
echo "[deps] Installing TFLite conversion tools (optional) ..."
pip install --quiet onnx2tf 2>/dev/null || \
pip install --quiet onnx-tf tensorflow 2>/dev/null || \
echo "[deps] WARNING: TFLite conversion tools not available — TFLite outputs will be skipped"

echo ""
echo "[deps] Dependencies installed."
echo ""

# ── Step 3: Run conversions ─────────────────────────────────────
cd "$REPO_ROOT"

echo "------------------------------------------------------------"
echo "  Converting OSNet x1.0 (Person Re-ID)"
echo "------------------------------------------------------------"
echo ""
python3 scripts/convert_osnet.py || {
    echo ""
    echo "WARNING: OSNet conversion failed (see errors above)"
    echo ""
}

echo ""
echo "------------------------------------------------------------"
echo "  Converting InsightFace buffalo_sc (Face Recognition)"
echo "------------------------------------------------------------"
echo ""
python3 scripts/convert_insightface.py || {
    echo ""
    echo "WARNING: InsightFace conversion failed (see errors above)"
    echo ""
}

# ── Step 4: List output files ───────────────────────────────────
echo ""
echo "============================================================"
echo "  Output Files"
echo "============================================================"
echo ""

list_file() {
    local path="$1"
    local label="$2"
    if [ -e "$path" ]; then
        if [ -d "$path" ]; then
            # Directory (e.g. .mlpackage) — sum contents
            local size
            size=$(find "$path" -type f -exec stat -f%z {} + 2>/dev/null | awk '{s+=$1}END{printf "%.1f", s/1048576}' 2>/dev/null || echo "?")
            echo "  OK   $label"
            echo "       $path ($size MB)"
        else
            local size
            size=$(stat -f%z "$path" 2>/dev/null || stat --printf="%s" "$path" 2>/dev/null || echo "0")
            local size_mb
            size_mb=$(echo "scale=1; $size / 1048576" | bc 2>/dev/null || echo "?")
            echo "  OK   $label"
            echo "       $path ($size_mb MB)"
        fi
    else
        echo "  MISS $label"
        echo "       $path"
    fi
}

list_file "$REPO_ROOT/models/osnet_x1_0.onnx"                            "OSNet ONNX (Python)"
list_file "$REPO_ROOT/models/insightface_r18.onnx"                       "InsightFace ONNX (Python)"
list_file "$REPO_ROOT/ios/FlightRisk/Models/OSNetReID.mlpackage"          "OSNet CoreML (iOS)"
list_file "$REPO_ROOT/ios/FlightRisk/Models/InsightFaceR18.mlpackage"     "InsightFace CoreML (iOS)"
list_file "$REPO_ROOT/mobile/app/src/main/assets/osnet_x1_0.tflite"      "OSNet TFLite (Android)"
list_file "$REPO_ROOT/mobile/app/src/main/assets/insightface_r18.tflite"  "InsightFace TFLite (Android)"

echo ""
echo "============================================================"
echo "  Done"
echo "============================================================"
