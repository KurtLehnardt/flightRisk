# FlightRisk

AI-powered lost child finder using a DJI Tello drone.

Parent deploys a drone, provides a photo of their child, and the drone autonomously searches using computer vision to locate and identify the child in real-time.

## Architecture

```
DJI Tello (720p WiFi) → MacBook / iPhone / Android → AI Pipeline → Alert
```

**Real-time pipeline (every frame):**
- **YOLO11n** — person detection (40-80+ FPS on Apple Silicon)
- **MobileNetV2 ReID** — appearance matching against reference photo
- **InsightFace ArcFace** — face recognition
- **ByteTrack** — multi-person tracking across frames

**Reasoning layer (periodic, on detections):**
- **Gemma** via Ollama (laptop) or on-device (mobile) — confirms matches with visual reasoning
- Adaptive scoring: when running local LLM, algorithm-based signals (ReID, face) are weighted higher

## Quick Start

### Linux (Ubuntu/Debian)
```bash
git clone https://github.com/KurtLehnardt/flightRisk.git
cd flightRisk
bash scripts/install_linux.sh
```

### macOS
```bash
git clone https://github.com/KurtLehnardt/flightRisk.git
cd flightRisk
bash scripts/install_macos.sh
```

### Windows
```powershell
# Python 3.13: onnxruntime has no 3.15 wheels yet (choco install python3 gives 3.15)
# choco needs an admin PowerShell with Chocolatey installed
choco install python313 -y
git clone https://github.com/KurtLehnardt/flightRisk.git
cd flightRisk
py -3.13 -m venv venv
# Call the venv python directly (avoids the Activate.ps1 execution policy block)
venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
# Optional, only for the local Gemma backend (see "Reasoning backend" below):
# install Ollama from https://ollama.com/download/windows, then
ollama pull gemma3:1b
```

### Run
```bash
source venv/bin/activate  # Linux/macOS

# Tello drone + web dashboard
python -m flightrisk --dashboard --target photo_of_child.jpg
# Windows: venv\Scripts\python.exe -m flightrisk --dashboard --target photo_of_child.jpg
# Open http://localhost:5555

# Tello drone + OpenCV window
python -m flightrisk --target photo_of_child.jpg

# Recorded video file (no drone needed)
python -m flightrisk --dashboard --video test.mp4 --target photo_of_child.jpg
```

## Reasoning backend (Claude or Gemma)

A second opinion confirms ReID/face matches. It can come from **Claude** (Anthropic API, the
default) or **Gemma** (local Ollama). Claude reads its key from the `CLAUDE_API_KEY` environment
variable (`ANTHROPIC_API_KEY` also works). The key is never logged.

```bash
# Windows (persists for new terminals; restart the app afterwards)
setx CLAUDE_API_KEY "sk-ant-..."

python -m flightrisk --dashboard                      # default: claude
python -m flightrisk --dashboard --reasoning gemma    # local Ollama
python -m flightrisk --dashboard --reasoning off      # ReID/face scores only
```

You can also switch while it runs from the **AI Reasoning** selector in the dashboard sidebar. If a
switch fails (for example, no API key), the previous backend stays active and the reason is shown.

| Env var | Default | Meaning |
| --- | --- | --- |
| `FLIGHTRISK_REASONING_PROVIDER` | `claude` | `claude`, `gemma` or `off` |
| `FLIGHTRISK_CLAUDE_MODEL` | `claude-opus-5-5` | any vision-capable Claude model, e.g. `claude-sonnet-5-5` to cut cost |
| `FLIGHTRISK_CLAUDE_EFFORT` | `medium` | `low` to `max`; lower is faster and cheaper |
| `FLIGHTRISK_GEMMA_MODEL` | `gemma4:latest` | Ollama model when using `gemma` |

Each confirmation sends the reference photo and the detected person's crop to the chosen backend
(for Claude, to the Anthropic API). Calls are rate-limited per tracked person.

## Observability (Grafana)

Traces, metrics and logs can be sent to a local Grafana stack with a ready-made dashboard:

```bash
docker compose -f docker-compose.grafana.yml up -d   # Grafana at http://localhost:3000
```

Then start the app with `FLIGHTRISK_OTEL_ENABLED=true OTEL_EXPORTER=otlp-grpc OTEL_ENDPOINT=http://localhost:4317`.
See [observability/README.md](observability/README.md).

## Keyboard Controls (OpenCV window)

`t` takeoff · `l` land · `q` quit

## Mobile Apps

### Android
1. Open `mobile/` in Android Studio (Hedgehog+)
2. Gradle sync, connect phone (USB debugging), Run
3. Settings → LLM Backend → choose Cloud (Claude) or Local (Gemma via MediaPipe)

Local LLM downloads Gemma 2 2B on-device (~1.4GB). Matching weights auto-adjust when using local model.

### iOS
1. Open `ios/FlightRisk.xcodeproj` in Xcode (regenerate it with [xcodegen](https://github.com/yonaskolb/XcodeGen) only after editing `ios/project.yml`)
2. Select a signing team and run
3. Settings → LLM Backend → choose Cloud (Claude) or Local (Gemma 4 via MLX Swift)

Requires iOS 17+, iPhone with 6GB+ RAM for local model.

To bundle the CoreML vision models, two scripts apply, and they produce different files:

- `scripts/download_matching_models.sh` creates a venv, then converts OSNet and InsightFace to `ios/FlightRisk/Models/OSNetReID.mlpackage` and `ios/FlightRisk/Models/InsightFaceR18.mlpackage`. It needs no ONNX input.
- `python scripts/convert_models_to_coreml.py` (pip dependencies are listed in its header) produces `YOLOPersonDetector`, `CLIPVisual`, `SCRFDFaceDetector`, and `ArcFaceMobile` as `.mlmodelc` bundles in `ios/FlightRisk/Models/`. It does nothing on a clean clone, because it reads `yolo11n.onnx`, `clip_visual.onnx`, `scrfd_500m.onnx`, and `arcface_mobilefacenet.onnx` from `mobile/app/src/main/assets/`, which is gitignored and holds only `.gitkeep`. Missing files are skipped and it reports 0/4 converted. You must place those four files there first. `scripts/export_yolo_onnx.py` and `scripts/export_arcface_onnx.py` write `yolo11n.onnx` and `arcface_mobilefacenet.onnx` to `models/`, which you can copy over. No script in this repo produces `clip_visual.onnx` (`export_clip_onnx.py` writes `mobileclip_s2.onnx` and `clip_vit_b32.onnx`) or `scrfd_500m.onnx`, so those two must be obtained separately.

After adding models, regenerate the project with xcodegen in `ios/`. Do not commit the regenerated `ios/FlightRisk.xcodeproj/project.pbxproj`, because it will reference the gitignored model files. Run `git checkout ios/FlightRisk.xcodeproj` before committing.
The app builds and runs without any models; person detection, ReID, and face recognition are skipped with a log message.

## Hardware

- **Drone:** DJI Ryze Tello (TLW004, SDK 1.3)
- **Laptop:** Any with Python 3.11+ (Apple Silicon recommended for MPS acceleration)
- **Android:** SDK 34+, 4GB+ RAM (8GB+ for local LLM)
- **iOS:** iPhone with iOS 17+, 6GB+ RAM for local LLM

## Project Structure

```
flightrisk/          # Python backend
├── drone/           # Tello control + search patterns
├── vision/          # YOLO detection, ReID, face recognition
├── reasoning/       # Gemma LLM reasoning via Ollama
├── dashboard/       # Web dashboard (Flask + WebSocket)
└── main.py          # Entry point
mobile/              # Android app (Kotlin/Jetpack Compose)
ios/                 # iOS app (Swift/SwiftUI, xcodegen)
scripts/             # Install scripts + model export
├── install_linux.sh
├── install_macos.sh
└── download_matching_models.sh
tests/               # pytest suite
```
