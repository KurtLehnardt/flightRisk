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

### Install as an app (macOS and Windows)

To run FlightRisk without a development checkout, install it per user from the newest release. It opens the
launcher window from `FlightRisk.app` (Mac) or the Start menu (Windows). Details, the Gatekeeper and
SmartScreen steps, update and uninstall are in [docs/installer.md](docs/installer.md).

```bash
# macOS (Apple Silicon, macOS 14 or newer)
bash -c "$(curl -fsSL https://raw.githubusercontent.com/KurtLehnardt/flightRisk/main/installer/macos/install.sh)"
```

```powershell
# Windows (the edge laptop), in PowerShell
irm https://raw.githubusercontent.com/KurtLehnardt/flightRisk/main/installer/windows/install.ps1 | iex
```

The steps below set up a development checkout instead.

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

## Reasoning backend (Gemma or Claude)

A second opinion confirms ReID/face matches. By default it comes from **Gemma** running locally in
Ollama, so nothing leaves the machine. **Claude** (Anthropic API) is an optional cloud backend and is
off unless you explicitly opt in.

Using Claude sends the detected person's crop and the missing child's reference photo to Anthropic.
FlightRisk therefore refuses to start or switch to Claude unless both of these are set:

- `FLIGHTRISK_ALLOW_CLOUD_LLM=1`, the explicit opt-in to cloud reasoning.
- `FLIGHTRISK_CLAUDE_API_KEY`, the API key. `ANTHROPIC_API_KEY` is deliberately ignored, so a key
  exported for other tools never turns on uploads by accident. The key is never logged; the startup
  log only names the variable it came from.

```bash
python -m flightrisk --dashboard                      # default: gemma (local Ollama)
python -m flightrisk --dashboard --reasoning off      # ReID/face scores only

# Opt in to Claude (cloud). Windows: use setx for each variable and restart the terminal.
export FLIGHTRISK_ALLOW_CLOUD_LLM=1
export FLIGHTRISK_CLAUDE_API_KEY="sk-ant-..."
python -m flightrisk --dashboard --reasoning claude
```

You can also switch while it runs from the **AI Reasoning** selector in the dashboard sidebar. Claude
is shown as unavailable, with the reason, until cloud use is allowed and a key is set; the dashboard
cannot opt itself in. If a switch fails (for example, Ollama is not running), the previous backend
stays active and the reason is shown.

| Env var | Default | Meaning |
| --- | --- | --- |
| `FLIGHTRISK_REASONING_PROVIDER` | `gemma` | `gemma`, `claude` or `off` |
| `FLIGHTRISK_ALLOW_CLOUD_LLM` | unset (off) | `1` to allow the Claude cloud backend |
| `FLIGHTRISK_CLAUDE_API_KEY` | unset | Anthropic API key used only when cloud use is allowed |
| `FLIGHTRISK_CLAUDE_MODEL` | `claude-opus-5-5` | any vision-capable Claude model, e.g. `claude-sonnet-5-5` to cut cost |
| `FLIGHTRISK_CLAUDE_EFFORT` | `medium` | `low` to `max`; lower is faster and cheaper |
| `FLIGHTRISK_GEMMA_MODEL` | `gemma4:latest` | Ollama model when using `gemma` |

Each confirmation sends the reference photo and the detected person's crop to the chosen backend
(for Gemma, to the local Ollama server; for Claude, to the Anthropic API). Calls are rate-limited per
tracked person.

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
source scripts/fleet_env.sh && scripts/demo_ground.sh        # second terminal: FLIGHTRISK_GROUND_LOCAL=1 DRONE_ID=1 ALT_CM=100 scripts/demo_edge.sh
```

On the Windows laptop, set `FLIGHTRISK_EDGE_TOKEN` and `FLIGHTRISK_FLEET_NET` (and in station mode `FLIGHTRISK_GROUND_IP` and `FLIGHTRISK_TELLO_HOST` for drone 2), then run:

```powershell
$env:DRONE_ID = "2"; $env:ALT_CM = "130"; .\scripts\demo_edge.ps1
```

`DRONE_ID` 1 and 2 become the fleet ids `drone-1` and `drone-2`, which are the ids the ground station expects (`FLIGHTRISK_FLEET_DRONES`, default `drone-1,drone-2`). Each launcher refuses to start without `FLIGHTRISK_EDGE_TOKEN`. Open the dashboard at `http://127.0.0.1:5555` on the Mac. [docs/demo-script.md](docs/demo-script.md) is the minute-by-minute run sheet, including the fallbacks. Without hardware, `python scripts/demo_fake_fleet.py --n 4 --drive` runs the same ground station against fake edges.

To start the same setups from a window instead of these commands (pick the layout and the number of drones, run the preflight checks, then Start), run `scripts/launcher.command` on the Mac or `scripts\launcher.bat` on Windows; see [docs/launcher.md](docs/launcher.md).

### Offline

The demo needs no internet connection. The detector and appearance matcher run on the laptops, `--no-llm` starts no reasoning model, and station mode needs a local router but not an uplink. Install the packages and model files once while online, before the event.

### Privacy defaults

- The demo matches on appearance only (`FLIGHTRISK_APPEARANCE_ONLY=1`), on the ground station and on the edge: faces are not resolvable at search altitude, the face model is never loaded, and no face embeddings are computed or sent. For flight sources (`tello`, `fake`) the edge is appearance-only by default; face matching on the edge needs `--with-face` or `FLIGHTRISK_APPEARANCE_ONLY=0`.
- Match snapshots are not written to disk (`FLIGHTRISK_SAVE_CAPTURES=0`).
- The dashboard binds `127.0.0.1` only, so Start and LAND ALL are not reachable from the network. Use the browser on the Mac itself. Binding another address with `FLIGHTRISK_DASHBOARD_HOST` needs `FLIGHTRISK_API_KEY`, and the web page does not send that key, so a keyed non-loopback bind locks the web UI out.
- The edge link is the only thing on the LAN, and it needs the shared `FLIGHTRISK_EDGE_TOKEN`. Person crops (JPEG images of people in the camera view, one per detection) and ReID appearance embeddings do go from each edge to the ground station over that token-authenticated WebSocket link. They are not written to disk by default.
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
- [docs/launcher.md](docs/launcher.md): the launcher window that starts the ground station, dashboard and edges
- [docs/installer.md](docs/installer.md): the per-user installers, FlightRisk.app, the disk image and the Windows Setup
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
