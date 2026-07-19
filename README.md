# Amber Drone

AI-powered lost child finder using a DJI Tello drone.

**Concept:** Parent deploys a drone, provides a photo of their child, and the drone autonomously searches the area using computer vision to locate and identify the child in real-time.

## Architecture

```
DJI Tello (720p WiFi) → MacBook / iPhone → AI Pipeline → Alert
```

**Real-time pipeline (every frame):**
- **YOLO11n** — person detection at 40-80+ FPS on Apple Silicon
- **MobileNetV2 ReID** — appearance matching against reference photo
- **ByteTrack** — multi-person tracking across frames

**Reasoning layer (periodic, on detections):**
- **Gemma 4** via Ollama — confirms matches with visual reasoning

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Install Ollama + Gemma 4 (optional, for LLM reasoning)
brew install ollama
ollama pull gemma4

# Test with webcam (no drone needed) — OpenCV window
python -m amber --webcam --target photo_of_child.jpg

# Test with web dashboard (recommended)
python -m amber --webcam --dashboard --target photo_of_child.jpg
# Open http://localhost:5555

# Run with Tello drone
python -m amber --target photo_of_child.jpg

# Run with Tello drone + web dashboard
python -m amber --dashboard --target photo_of_child.jpg
```

## Keyboard Controls (OpenCV window)

- `t` — takeoff
- `l` — land
- `q` — quit

## Hardware

- **Drone:** DJI Ryze Tello (TLW004, SDK 1.3)
- **Laptop:** MacBook Pro M1 Max
- **Future:** iOS app for iPhone 17+

## Project Structure

```
amber/
├── drone/       # Tello control + search patterns
├── vision/      # YOLO detection + ReID matching
├── reasoning/   # Gemma 4 LLM reasoning
├── dashboard/   # Web dashboard (Flask + WebSocket)
└── main.py      # Entry point
ios/             # Future iOS app (Swift/SwiftUI)
```
