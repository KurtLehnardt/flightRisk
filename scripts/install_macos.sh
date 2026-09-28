#!/usr/bin/env bash
# FlightRisk — macOS install script
#
# Installs all dependencies via Homebrew, sets up the Python environment,
# downloads AI models (including on-device Gemma), and runs the test suite.
#
# Usage:
#   bash scripts/install_macos.sh
#
# Requirements:
#   - macOS 13+ (Ventura or later)
#   - Apple Silicon (M1/M2/M3/M4) recommended for MPS acceleration
#   - At least 8GB RAM
#   - At least 15GB free disk
#   - Internet connection

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }
step()  { echo -e "\n${GREEN}══════════════════════════════════════════${NC}"; echo -e "${GREEN}  $*${NC}"; echo -e "${GREEN}══════════════════════════════════════════${NC}\n"; }

cd "$REPO_ROOT"

# ── Step 1: Homebrew + system packages ──────────────────────────
step "Step 1/6: Installing system packages"

if ! command -v brew &>/dev/null; then
    info "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add to path for Apple Silicon
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
fi

info "Homebrew: $(brew --version | head -1)"

brew install python@3.13 git curl ffmpeg 2>/dev/null || true

PYTHON=$(brew --prefix python@3.13)/bin/python3.13
if [ ! -x "$PYTHON" ]; then
    PYTHON=$(command -v python3)
fi
info "Python: $PYTHON ($($PYTHON --version))"

# ── Step 2: Python virtual environment ──────────────────────────
step "Step 2/6: Setting up Python virtual environment"

VENV_DIR="$REPO_ROOT/venv"

if [ -d "$VENV_DIR" ]; then
    info "Using existing venv: $VENV_DIR"
else
    info "Creating venv: $VENV_DIR"
    $PYTHON -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip setuptools wheel
info "venv activated: $(which python3)"

# ── Step 3: Python dependencies ─────────────────────────────────
step "Step 3/6: Installing Python dependencies"

pip install --quiet -r requirements.txt
pip install --quiet -r requirements-dev.txt

info "Python dependencies installed"

# Check MPS availability (Apple Silicon GPU)
python3 -c "import torch; print(f'MPS available: {torch.backends.mps.is_available()}')" 2>/dev/null || \
    warn "Could not check MPS availability"

# ── Step 4: Ollama + Gemma (local LLM) ──────────────────────────
step "Step 4/6: Installing Ollama + Gemma local model"

if command -v ollama &>/dev/null; then
    info "Ollama already installed: $(ollama --version)"
else
    info "Installing Ollama via Homebrew..."
    brew install ollama
fi

# Start Ollama service if not running
if ! pgrep -x ollama &>/dev/null; then
    info "Starting Ollama service..."
    ollama serve &>/dev/null &
    sleep 3
fi

# Pull Gemma model — macOS with Apple Silicon can handle larger models
TOTAL_RAM_MB=$(sysctl -n hw.memsize | awk '{printf "%d", $1/1048576}')
info "Total RAM: ${TOTAL_RAM_MB}MB"

ARCH=$(uname -m)
info "Architecture: $ARCH"

if [ "$TOTAL_RAM_MB" -ge 32000 ]; then
    MODEL="gemma3:12b"
    info "32GB+ RAM detected — pulling gemma3:12b"
elif [ "$TOTAL_RAM_MB" -ge 16000 ]; then
    MODEL="gemma3:4b"
    info "16GB+ RAM detected — pulling gemma3:4b"
elif [ "$TOTAL_RAM_MB" -ge 8000 ]; then
    MODEL="gemma3:1b"
    info "8GB+ RAM detected — pulling gemma3:1b"
else
    MODEL="gemma3:1b"
    warn "Less than 8GB RAM — pulling gemma3:1b (may be slow)"
fi

if ollama list 2>/dev/null | grep -q "$MODEL"; then
    info "Model $MODEL already pulled"
else
    info "Pulling $MODEL (this may take a few minutes)..."
    ollama pull "$MODEL"
fi

info "Ollama ready with model: $MODEL"

# ── Step 5: Download matching models ────────────────────────────
step "Step 5/6: Downloading matching models (OSNet + InsightFace)"

if [ -f "$REPO_ROOT/models/osnet_x1_0.onnx" ] && [ -f "$REPO_ROOT/models/insightface_r18.onnx" ]; then
    info "Matching models already downloaded"
else
    bash "$REPO_ROOT/scripts/download_matching_models.sh" || {
        warn "Matching model download had issues (see above). Continuing..."
    }
fi

# ── Step 6: Run tests ──────────────────────────────────────────
step "Step 6/6: Running test suite"

cd "$REPO_ROOT"
python3 -m pytest tests/ -v --tb=short 2>&1 | tail -30
TEST_EXIT=${PIPESTATUS[0]}

echo ""
echo "============================================================"
if [ "$TEST_EXIT" -eq 0 ]; then
    info "All tests passed"
else
    warn "Some tests failed (exit code $TEST_EXIT)"
fi

# ── Summary ─────────────────────────────────────────────────────
echo ""
step "Installation Complete"

echo "  macOS:        $(sw_vers -productVersion)"
echo "  Architecture: $ARCH"
echo "  Python:       $(python3 --version)"
echo "  Venv:         $VENV_DIR"
echo "  MPS (GPU):    $(python3 -c 'import torch; print(torch.backends.mps.is_available())' 2>/dev/null || echo 'unknown')"
echo "  Ollama:       $(ollama --version 2>/dev/null || echo 'not found')"
echo "  LLM Model:    $MODEL"
echo "  ONNX Models:  $(ls "$REPO_ROOT/models/"*.onnx 2>/dev/null | wc -l | tr -d ' ') files"
echo ""
echo "  To start the dashboard:"
echo "    source venv/bin/activate"
echo "    python -m flightrisk --webcam --dashboard"
echo "    # Open http://localhost:5555"
echo ""
