#!/usr/bin/env bash
# FlightRisk — Linux install script (Ubuntu/Debian)
#
# Installs all dependencies, sets up the Python environment, downloads
# AI models, and runs the test suite. Designed for headless Ubuntu on
# AWS EC2 or any Debian-based system.
#
# Usage:
#   bash scripts/install_linux.sh
#
# Requirements:
#   - Ubuntu 22.04+ or Debian 12+
#   - At least 8GB RAM (for Gemma model inference)
#   - At least 15GB free disk (models + venv)
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

# ── Step 1: System packages ─────────────────────────────────────
step "Step 1/6: Installing system packages"

if ! command -v apt-get &>/dev/null; then
    fail "This script requires apt-get (Ubuntu/Debian). For other distros, install Python 3.11+, git, and curl manually."
fi

sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-venv python3-pip python3-dev \
    git curl wget \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxrender1 libxext6 \
    ffmpeg \
    build-essential

PYTHON=$(command -v python3)
info "Python: $PYTHON ($(python3 --version))"

# ── Step 2: Python virtual environment ──────────────────────────
step "Step 2/6: Setting up Python virtual environment"

VENV_DIR="$REPO_ROOT/venv"

if [ -d "$VENV_DIR" ]; then
    info "Using existing venv: $VENV_DIR"
else
    info "Creating venv: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
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

# ── Step 4: Ollama + Gemma (local LLM) ──────────────────────────
step "Step 4/6: Installing Ollama + Gemma local model"

if command -v ollama &>/dev/null; then
    info "Ollama already installed: $(ollama --version)"
else
    info "Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
fi

# Start Ollama service if not running
if ! pgrep -x ollama &>/dev/null; then
    info "Starting Ollama service..."
    ollama serve &>/dev/null &
    sleep 3
fi

# Pull Gemma model (best fit for available RAM)
TOTAL_RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
info "Total RAM: ${TOTAL_RAM_MB}MB"

if [ "$TOTAL_RAM_MB" -ge 16000 ]; then
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

echo "  Python:       $(python3 --version)"
echo "  Venv:         $VENV_DIR"
echo "  Ollama:       $(ollama --version 2>/dev/null || echo 'not found')"
echo "  LLM Model:    $MODEL"
echo "  ONNX Models:  $(ls "$REPO_ROOT/models/"*.onnx 2>/dev/null | wc -l | tr -d ' ') files"
echo ""
echo "  To start the dashboard:"
echo "    source venv/bin/activate"
echo "    python -m flightrisk --webcam --dashboard"
echo "    # Open http://localhost:5555"
echo ""
