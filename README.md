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
# Python 3.13 required: 3.15 (choco install python3) has no onnxruntime/insightface wheels
choco install python313 -y
git clone https://github.com/KurtLehnardt/flightRisk.git
cd flightRisk
py -3.13 -m venv venv
# Call the venv python directly; Activate.ps1 is unreliable in non-interactive shells
venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
# Install Ollama from https://ollama.com/download/windows
ollama pull gemma3:1b
```

### Run
```bash
source venv/bin/activate  # Linux/macOS

# Webcam + web dashboard (no drone needed)
python -m flightrisk --webcam --dashboard --target photo_of_child.jpg
# Open http://localhost:5555

# Webcam + OpenCV window
python -m flightrisk --webcam --target photo_of_child.jpg

# Tello drone + dashboard
python -m flightrisk --dashboard --target photo_of_child.jpg
```

## Keyboard Controls (OpenCV window)

`t` takeoff · `l` land · `q` quit

## Mobile Apps

### Android
1. Open `mobile/` in Android Studio (Hedgehog+)
2. Gradle sync, connect phone (USB debugging), Run
3. Settings → LLM Backend → choose Cloud (Claude) or Local (Gemma via MediaPipe)

Local LLM downloads Gemma 2 2B on-device (~1.4GB). Matching weights auto-adjust when using local model.

### iOS
1. Install [xcodegen](https://github.com/yonaskolb/XcodeGen), run `xcodegen` in `ios/`
2. Open `ios/FlightRisk.xcodeproj` in Xcode
3. Settings → LLM Backend → choose Cloud (Claude) or Local (Gemma 4 via MLX Swift)

Requires iOS 17+, iPhone with 6GB+ RAM for local model.

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
