# FlightRisk - Windows edge install script (drone 2 edge process)
#
# Creates the Python 3.13 virtual environment, installs the Python
# dependencies and downloads the yolo11n detection weights. It deliberately
# skips Ollama and InsightFace model setup, because the edge role does not use
# the on-device language model or face matching.
#
# Usage (regular PowerShell, from the repository root):
#   powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
#
# Requirements:
#   - Python 3.13 installed so that "py -3.13" works (choco install python313 -y)
#   - Internet connection for pip and the weights download

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Green
}

function Invoke-Checked([string]$Description, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

# Step 1: Python 3.13 check
Write-Step "Step 1/4: Checking for Python 3.13"
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "The py launcher was not found. Install Python 3.13 first: choco install python313 -y (administrator PowerShell)."
}
& py -3.13 --version
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.13 was not found. Install it with: choco install python313 -y (onnxruntime has no wheels for newer Python versions yet)."
}

# Step 2: virtual environment
Write-Step "Step 2/4: Creating the virtual environment"
$VenvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Invoke-Checked "Creating the virtual environment" { py -3.13 -m venv venv }
} else {
    Write-Host "venv already exists, reusing it."
}

# Step 3: dependencies. The venv python is called directly, which avoids the
# Activate.ps1 execution policy block.
Write-Step "Step 3/4: Installing Python dependencies"
Invoke-Checked "Upgrading pip" { venv\Scripts\python.exe -m pip install --upgrade pip }
Invoke-Checked "Installing requirements" { venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt }

# Step 4: yolo11n weights
Write-Step "Step 4/4: Downloading yolo11n.pt"
if (-not (Test-Path "yolo11n.pt")) {
    Invoke-Checked "Downloading yolo11n.pt" { venv\Scripts\python.exe -c "from ultralytics import YOLO; YOLO('yolo11n.pt')" }
} else {
    Write-Host "yolo11n.pt already present."
}

Write-Host ""
Write-Host "Ollama and InsightFace setup skipped: the edge role does not need them." -ForegroundColor Yellow
Write-Host "Next: follow docs\windows-edge-setup.md (static IP, firewall, FLIGHTRISK_EDGE_TOKEN, scripts\demo_edge.ps1)."
