# Model Export Scripts

Export YOLO, CLIP, and ArcFace models from PyTorch/InsightFace to ONNX format
for mobile inference (ONNX Runtime on Android). Each script includes a parity
gate that verifies the ONNX output matches the original Python model.

## Requirements

```bash
pip install ultralytics open-clip-torch insightface onnx onnxruntime opencv-python Pillow torch numpy
```

## Scripts

### `parity_test.py` -- Shared utilities

Provides `load_test_images`, `cosine_similarity`, `run_parity_gate`,
`compare_detections`, and `print_parity_report`. Imported by all export scripts.

```bash
# Verify it loads
python scripts/parity_test.py
```

### `export_yolo_onnx.py` -- YOLO11n person detector

Exports `yolo11n.pt` to ONNX using the Ultralytics API. Parity gate checks
bounding-box coordinates (within 2 px) and confidences (within 0.01).

```bash
python scripts/export_yolo_onnx.py --output-dir models/ --test-images . --n-test 5
```

**Output:** `models/yolo11n.onnx` (~11 MB)

### `export_clip_onnx.py` -- CLIP image encoder (ReID)

Exports two CLIP image encoders for person re-identification:
- **Primary:** MobileCLIP-S2 (~20 MB, mobile-optimized)
- **Fallback:** OpenAI CLIP ViT-B/32 (~150 MB, proven ONNX export)

Parity gate: cosine similarity >= 0.98 on all test embeddings.

```bash
python scripts/export_clip_onnx.py --output-dir models/ --test-images . --n-test 20
```

**Output:** `models/mobileclip_s2.onnx`, `models/clip_vit_b32.onnx`

### `export_arcface_onnx.py` -- ArcFace face recognition

Copies the ArcFace MobileFaceNet ONNX from the InsightFace `buffalo_sc` model
pack (InsightFace ships models as ONNX natively). Parity gate compares against
the Python InsightFace API.

Also documents the dlib `face_recognition_model_v1` conversion path (not
recommended -- ArcFace is smaller and more accurate).

```bash
python scripts/export_arcface_onnx.py --output-dir models/ --test-images . --n-test 20
```

**Output:** `models/arcface_mobilefacenet.onnx` (~4 MB)

## Expected outputs in `models/`

| File                        | Size    | Input Shape           | Output Shape   |
|-----------------------------|---------|-----------------------|----------------|
| `yolo11n.onnx`              | ~11 MB  | [1, 3, 640, 640]      | [1, 84, 8400]  |
| `mobileclip_s2.onnx`        | ~20 MB  | [1, 3, 224, 224]      | [1, 512]       |
| `clip_vit_b32.onnx`         | ~150 MB | [1, 3, 224, 224]      | [1, 512]       |
| `arcface_mobilefacenet.onnx` | ~4 MB  | [1, 3, 112, 112]      | [1, 512]       |

## Parity gate thresholds

- **YOLO:** Bounding boxes within 2 px, confidence within 0.01. Both models
  must find the same number of detections per image.
- **CLIP:** Cosine similarity >= 0.98 between PyTorch and ONNX embeddings on
  every test image. Embeddings are L2-normalized before comparison.
- **ArcFace:** Cosine similarity >= 0.98 between InsightFace Python API and
  direct ONNX Runtime embeddings. Only images with detected faces are tested.

A parity gate failure means the ONNX model's output diverges meaningfully from
the Python original, which would cause the mobile app to behave differently.
Investigate the preprocessing pipeline (normalization, resize, color order)
before shipping a model that fails parity.

## Mobile preprocessing cheat sheet

### YOLO11n
- Letterbox resize longest edge to 640, pad with gray (114, 114, 114)
- `pixel / 255.0` (no mean/std subtraction)
- BGR -> RGB, HWC -> CHW
- Post-process: transpose output, filter by confidence, NMS, rescale boxes

### CLIP (MobileCLIP-S2 / ViT-B/32)
- Resize shortest edge to 256 (bicubic)
- Center crop 224x224
- `pixel / 255.0`, then `(pixel - mean) / std`
- mean = `[0.48145466, 0.4578275, 0.40821073]`
- std = `[0.26862954, 0.26130258, 0.27577711]`
- BGR -> RGB, HWC -> CHW
- L2-normalize the output embedding

### ArcFace (MobileFaceNet)
- Detect face + 5-point landmarks
- Affine warp to 112x112 using standard ArcFace alignment template
- `(pixel - 127.5) / 127.5` (maps 0-255 to -1..1)
- BGR -> RGB, HWC -> CHW
- L2-normalize the 512-d output embedding

## Obstacle Sensing Scripts

### `obstacle_record.py` -- Record the frames the obstacle sensor sees

Connects to one Tello, turns its video on (it never takes off) and records the
frames the edge's obstacle sensor would judge, through the same feeds the edge
uses: each new decoded frame once, converted from the decoder's RGB to the
sensor's BGR, stamped with its capture time, and refused when the decoder marks
it stale (a decoder backlog, or a corrupt frame after lost video). Each frame is
a lossless PNG, so a replay gives the depth model exactly the input it had in
flight. With `--tello-local-ip` (the two-drone layout) the decoder stamps
capture times; without it a frame counts from its arrival.

```bash
venv/bin/python scripts/obstacle_record.py --out rec/plant_150cm --seconds 5 --label "plant 150 cm"
venv/bin/python scripts/obstacle_record.py --out rec/wall_300cm --tello-local-ip 192.168.10.2
```

It writes `frame_000001.png` and onwards, `frames.csv` (`index`, `file`,
`captured_at_s`, `arrived_at_s`, `capture_to_arrival_ms`, `wall_time`) and
`meta.json` (the label, the channel order, the settle time and the frame,
stale and decoder counts). The output directory must not already hold a
recording.

### `obstacle_replay.py` -- Replay recordings and calibrate depth threshold

Replays a drone recording and runs the obstacle sensor on every nth frame to
calibrate the MiDaS depth threshold (`FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH`)
against known distances. Prints per-frame cue values, decisions and latency to
stdout; optionally writes per-frame results to CSV for analysis. Every frame
goes through the same channel conversion as the edge's frame feeds
(`to_sensor_frame`), so a frame replayed from a recording reaches MiDaS as the
same tensor it was in flight.

```bash
# Replay a recording made by obstacle_record.py (the input to calibrate with)
python scripts/obstacle_replay.py --frames rec/plant_150cm --out rec/plant_150cm/replay.csv

# Replay a video, process every 5th frame (lossy: numbers can differ a little from flight)
python scripts/obstacle_replay.py --video recording.mp4 --stride 5

# Test with a custom depth threshold
python scripts/obstacle_replay.py --frames rec/plant_150cm --min-safe-depth 0.40
```

**Arguments:**
- `--frames <dir>` or `--video <mp4>` (one is required): A recording directory written by `obstacle_record.py`, or a video file.
- `--stride n` (default 1): Process every nth frame.
- `--out csv` (optional): Write per-frame data to CSV file.
- `--min-safe-depth x` (default 0.35): Depth threshold for obstacle detection.

**Output columns (CSV):**
- `frame`: Original frame number in the recording or video.
- `processed`: Frame count among processed (stride-ed) frames.
- `action`: Decision from depth cue (`clear`, `reverse`, `go_left`, `go_right`).
- `center_depth`: Depth statistic at frame center (normalized inverse depth).
- `center_p90`: 90th percentile of center region.
- `left_depth`, `right_depth`: Left and right region depths.
- `confidence`: Model confidence in the decision.
- `center_stat`: Statistic used (`mean` or `p90`).
- `latency_ms`: Inference time in milliseconds.
- `safe`: Boolean, whether the path is clear.
- `captured_at_s`: The frame's capture time from the recording (empty for a video).

### `bench_obstacle.py` -- Benchmark MiDaS latency under YOLO load

Measures depth model inference latency (p50, p95) while YOLO11n runs in a
background thread to simulate realistic edge load. Can read frames from a video
file or generate synthetic frames.

```bash
# Benchmark with synthetic frames, no YOLO load
python scripts/bench_obstacle.py --frames 100

# Benchmark with video and YOLO background load
python scripts/bench_obstacle.py --video recording.mp4 --frames 100 --with-yolo

# Test with custom depth threshold
python scripts/bench_obstacle.py --frames 100 --min-safe-depth 0.40
```

**Arguments:**
- `--video <mp4>` (optional): Path to video file. If not given, generates synthetic frames.
- `--frames n` (default 100): Number of frames to process.
- `--with-yolo`: Run YOLO11n on a background thread to simulate load.
- `--min-safe-depth x` (default 0.35): Depth threshold (for consistency with replay).

**Output:**
Prints latency percentiles (p50, p95) and summary statistics to stderr:
- Mean, min, and max latency across all frames.
- P50 and P95 percentiles for typical and worst-case inference times.

Use this to verify that MiDaS inference stays below the design budget (design
section 2.3: 150 to 300 ms under two-drone YOLO load on the ZBook) and to
measure actual performance on your hardware before flight.
