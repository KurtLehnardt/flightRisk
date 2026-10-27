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
# Install Ollama from https://ollama.com/download/windows
ollama pull gemma3:1b
```

### Run
```bash
source venv/bin/activate  # Linux/macOS

# Webcam + web dashboard (no drone needed)
python -m flightrisk --webcam --dashboard --target photo_of_child.jpg
# Windows: venv\Scripts\python.exe -m flightrisk --webcam --dashboard --target photo_of_child.jpg
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
1. Open `ios/FlightRisk.xcodeproj` in Xcode (regenerate it with [xcodegen](https://github.com/yonaskolb/XcodeGen) only after editing `ios/project.yml`)
2. Select a signing team and run
3. Settings → LLM Backend → choose Cloud (Claude) or Local (Gemma 4 via MLX Swift)

Requires iOS 17+, iPhone with 6GB+ RAM for local model.

To bundle the CoreML vision models, two scripts apply, and they produce different files:

- `scripts/download_matching_models.sh` creates a venv, then converts OSNet and InsightFace to `ios/FlightRisk/Models/OSNetReID.mlpackage` and `ios/FlightRisk/Models/InsightFaceR18.mlpackage`. It needs no ONNX input.
- `python scripts/convert_models_to_coreml.py` (pip dependencies are listed in its header) produces `YOLOPersonDetector`, `CLIPVisual`, `SCRFDFaceDetector`, and `ArcFaceMobile` as `.mlmodelc` bundles in `ios/FlightRisk/Models/`. It does nothing on a clean clone, because it reads `yolo11n.onnx`, `clip_visual.onnx`, `scrfd_500m.onnx`, and `arcface_mobilefacenet.onnx` from `mobile/app/src/main/assets/`, which is gitignored and holds only `.gitkeep`. Missing files are skipped and it reports 0/4 converted. You must place those four files there first. `scripts/export_yolo_onnx.py` and `scripts/export_arcface_onnx.py` write `yolo11n.onnx` and `arcface_mobilefacenet.onnx` to `models/`, which you can copy over. No script in this repo produces `clip_visual.onnx` (`export_clip_onnx.py` writes `mobileclip_s2.onnx` and `clip_vit_b32.onnx`) or `scrfd_500m.onnx`, so those two must be obtained separately.

After adding models, regenerate the project with xcodegen in `ios/`. Do not commit the regenerated `ios/FlightRisk.xcodeproj/project.pbxproj`, because it will reference the gitignored model files. Run `git checkout ios/FlightRisk.xcodeproj` before committing.
The app builds and runs without any models; person detection, ReID, and face recognition are skipped with a log message.

## Hackathon demo (Nucleus bounty)

Two Tello EDU drones search one floor area together under one ground station. The ground station splits the area into zones, gives neighbouring zones different altitude slots, synchronises every edge's clock, starts both drones at the same instant, merges their detections onto one map and re-tasks a lost drone's lanes to the other. The kickoff commit is `798f21a`, tagged `hackathon/kickoff`; everything built after it is listed in [docs/built-this-week.md](docs/built-this-week.md).

### Hardware

- 2 DJI Tello EDU drones (only an EDU supports the station-mode `ap` command).
- A Mac that runs the ground station and edge 1 (drone 1).
- A Windows laptop that runs edge 2 (drone 2). It installs without the face and LLM packages; see [docs/windows-edge-setup.md](docs/windows-edge-setup.md).
- Station mode: one 2.4 GHz router or phone hotspot that allows client-to-client traffic.
- AP mode: two USB Ethernet adapters (if a laptop has no RJ45 port) and one Cat5e or Cat6 cable between the laptops.
- A clear floor area of about 4 m by 4 m.

### Run it

Pick one network path. Both use the same shared secret, which must be the same on both laptops, and the same launchers. Start the ground station first. Details, the hardware check and the Wi-Fi reset are in [docs/fleet-setup.md](docs/fleet-setup.md).

```bash
export FLIGHTRISK_EDGE_TOKEN="choose-a-long-random-string"

# Path 1, station mode: the drones and both laptops join one router or hotspot.
export FLIGHTRISK_FLEET_NET=station FLIGHTRISK_GROUND_IP=<mac lan ip> FLIGHTRISK_TELLO_HOST=<drone 1 ip>
source scripts/fleet_env.sh && scripts/demo_ground.sh        # second terminal: DRONE_ID=1 ALT_CM=100 scripts/demo_edge.sh

# Path 2, AP mode: each laptop joins one drone's own Wi-Fi, the laptops share a cable (10.42.0.0/24).
export FLIGHTRISK_FLEET_NET=ap
source scripts/fleet_env.sh && scripts/demo_ground.sh        # second terminal: DRONE_ID=1 ALT_CM=100 scripts/demo_edge.sh
```

On the Windows laptop, set `FLIGHTRISK_EDGE_TOKEN` and `FLIGHTRISK_FLEET_NET` (and in station mode `FLIGHTRISK_GROUND_IP` and `FLIGHTRISK_TELLO_HOST` for drone 2), then run:

```powershell
$env:DRONE_ID = "2"; $env:ALT_CM = "160"; .\scripts\demo_edge.ps1
```

`DRONE_ID` 1 and 2 become the fleet ids `drone-1` and `drone-2`, which are the ids the ground station expects (`FLIGHTRISK_FLEET_DRONES`, default `drone-1,drone-2`). Each launcher refuses to start without `FLIGHTRISK_EDGE_TOKEN`. Open the dashboard at `http://127.0.0.1:5555` on the Mac. [docs/demo-script.md](docs/demo-script.md) is the minute-by-minute run sheet, including the fallbacks. Without hardware, `python scripts/demo_fake_fleet.py --n 4 --drive` runs the same ground station against fake edges.

### Offline

The demo needs no internet connection. The detector and appearance matcher run on the laptops, `--no-llm` starts no reasoning model, and station mode needs a local router but not an uplink. Install the packages and model files once while online, before the event.

### Privacy defaults

- The demo matches on appearance only (`FLIGHTRISK_APPEARANCE_ONLY=1`), on the ground station and on the edge: faces are not resolvable at search altitude, the face model is never loaded, and no face embeddings are computed or sent. For flight sources (`tello`, `fake`) the edge is appearance-only by default; face matching on the edge needs `--with-face` or `FLIGHTRISK_APPEARANCE_ONLY=0`.
- Match snapshots are not written to disk (`FLIGHTRISK_SAVE_CAPTURES=0`).
- The dashboard binds `127.0.0.1` only, so Start and LAND ALL are not reachable from the network. Use the browser on the Mac itself. Binding another address with `FLIGHTRISK_DASHBOARD_HOST` needs `FLIGHTRISK_API_KEY`, and the web page does not send that key, so a keyed non-loopback bind locks the web UI out.
- The edge link is the only thing on the LAN, and it needs the shared `FLIGHTRISK_EDGE_TOKEN`. Person crops (images of people in the camera view) do go from each edge to the ground station over that token-authenticated WebSocket link. They are not written to disk by default.
- Socket.IO on the dashboard accepts same-origin pages only.

See [docs/privacy-design.md](docs/privacy-design.md).

### Fine-tuned weights

The fine-tuned aerial person detector (`models/heridal_yolo11s.pt`) is not in git. It lives in the gitignored `models/` directory of the machine that trained it, and no download link is published yet. To reproduce it, run `scripts/finetune_heridal.py` (subcommands `fetch`, `build`, `train` and `evaluate`) as described in [docs/altitude-benchmark.md](docs/altitude-benchmark.md). Without it, the stock `yolo11n.pt` detector is used.

### Documents

- [docs/built-this-week.md](docs/built-this-week.md): what was built after kickoff, with commit evidence
- [docs/pitch.md](docs/pitch.md): the pitch
- [docs/one-page-plan.md](docs/one-page-plan.md): the one-page plan
- [docs/demo-script.md](docs/demo-script.md): the demo run sheet
- [docs/roadmap-budget.md](docs/roadmap-budget.md): roadmap and budget
- [docs/privacy-design.md](docs/privacy-design.md): privacy design
- [docs/fleet-setup.md](docs/fleet-setup.md): two-drone network setup
- [docs/altitude-benchmark.md](docs/altitude-benchmark.md): altitude benchmark and fine-tuning
- [docs/licensing.md](docs/licensing.md): licensing

License: GPL-3.0-or-later.

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
