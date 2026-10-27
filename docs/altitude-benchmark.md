# Altitude benchmark

FlightRisk measures how small a person can be in the frame before detection fails, because that pixel height fixes the altitude a search flight must fly.

## Dataset candidates

| Dataset | Exists | License | Size | Registration | Notes |
|---|---|---|---|---|---|
| HERIDAL | Yes | CC-BY-3.0 | 8.3 GB (one zip, 1,647 images) | None, no login | Zenodo record 5662351. UAV images at 40 to 65 m, 4000x3000, single class person, VOC XML. |
| WiSARD | Yes | Check the publisher's terms before use | Large multi-sensor set | Not verified | Wilderness SAR imagery with visible and thermal frames. Not used here. |
| LADD | Yes | Check the publisher's terms before use | Small sample available | Not verified | Lost Aerial Dataset for Detection. Fallback: the runner reads its VOC export. |
| SARD | Yes | Check the publisher's terms before use | Moderate | Not verified | Search and Rescue Dataset of actors in simulated SAR poses. Not used here. |

Pick: HERIDAL (CC-BY-3.0, https://zenodo.org/records/5662351, 8.3 GB, no login). LADD sample is the fallback if bandwidth makes the full download impractical. Document which one was used when results are published.

## Reproduce

    scripts/download_heridal.sh                      # into datasets/heridal/ (gitignored)
    python -m flightrisk.evaluation detect --dataset datasets/heridal --tiled 0 --output results/altitude_single.json
    python -m flightrisk.evaluation detect --dataset datasets/heridal --tiled 1 --output results/altitude_tiled.json

Matching uses IoU 0.3. Recall and precision are binned by ground truth person height in pixels; a false positive is binned by its predicted box height. `min_person_px` is the lower edge of the first bin with recall of at least 0.5.

## Results

<!-- build:t11 start -->
Measured on 2026-10-27 with `scripts/run_altitude_benchmark.sh`. Raw numbers are in `results/altitude_single.json` and `results/altitude_tiled.json`.

**Split used: the HERIDAL test split, 101 images (4000x3000), 337 person boxes.** The train split was not run. The full 8.3 GB zip downloaded at roughly 0.5 MB/s from Zenodo, which would have taken several hours, so the script fetched only the 101 test images and their VOC XML from inside the zip with HTTP range requests (about 0.5 GB). The data is the same HERIDAL record 5662351; LADD was not needed. Dataset files are in the gitignored `datasets/` directory and are not committed.

**Setup.** Detector: `yolo11n.pt` (COCO pretrained, not fine-tuned on aerial imagery), confidence 0.4, device `mps` (Apple M1 Max). Single mode runs one full-frame pass, which ultralytics downscales to 640 px. Tiled mode uses 640 px tiles with 0.2 overlap. Matching uses IoU 0.3.

| bin_px | n_gt | recall_single | precision_single | recall_tiled | precision_tiled |
|---|---|---|---|---|---|
| [0,8) | 0 | 0.000 | 0.000 | 0.000 | 0.000 |
| [8,16) | 0 | 0.000 | 0.000 | 0.000 | 0.000 |
| [16,24) | 0 | 0.000 | 0.000 | 0.000 | 0.000 |
| [24,32) | 7 | 0.000 | 0.000 | 0.000 | 0.000 |
| [32,48) | 108 | 0.000 | 0.000 | 0.046 | 0.556 |
| [48,64) | 111 | 0.000 | 0.000 | 0.081 | 0.818 |
| [64,inf) | 111 | 0.000 | 0.000 | 0.306 | 0.756 |

Bins with `n_gt` of 0 show 0.000 because there is nothing to measure there, not because the detector failed. A bin's precision counts false positives by predicted box height, so it can be nonzero in a bin with no ground truth in the JSON (tiled had 1 false positive in [16,24)).

![Recall by person pixel height, single pass vs tiled](img/altitude_recall.png)

min_person_px (single): none, recall never reached 0.5 in any bin
min_person_px (tiled): none, recall never reached 0.5 in any bin

`min_person_px` is the lower edge of the first bin with recall of at least 0.5. Neither mode qualifies, so both JSON files store `null`. This is the measured result, not a gap in the run.

**What the numbers say.**

- Single pass finds nothing. HERIDAL persons are 32 px and taller in a 4000x3000 frame; after the 640 px downscale they are about 5 to 10 px tall, below what the model detects. It made zero detections on the whole split.
- Tiling does help: it goes from 0 to 48 true positives of 337 (overall recall about 0.14), and recall rises with pixel height to 0.306 at 64 px and taller. Precision is 0.56 to 0.82 where it finds anything.
- Even tiled, the off-the-shelf nano model does not reach 0.5 recall at any height this dataset covers. The last bin is open-ended, so the curve is not resolved above 64 px, and all HERIDAL imagery is from 40 to 65 m. The 0.5 crossing, if it exists for this model, is above 64 px.
- Consequence for the pitch: the benchmark does not yet support a measured altitude. It supports a measured statement that the stock detector needs persons substantially larger than 64 px, or a detector fine-tuned on aerial data, plus tiling.

**Coverage at the planner's reference size.** There is no measured `min_person_px` to feed the coverage planner. The table below is `python -m flightrisk.drone.coverage --min-px 64`, where 64 px is the lower edge of the best bin (recall 0.306, not 0.5). Treat it as an optimistic bound on how high the drone could fly with this detector, not as a measured threshold; the real altitude is lower.

| preset | n | alt_m | lane_m | hours |
|---|---|---|---|---|
| Ground team | 1 | 0.0 | 30.0 | 57.555 |
| Ground team | 2 | 0.0 | 30.0 | 28.778 |
| Ground team | 3 | 0.0 | 30.0 | 19.185 |
| Ground team | 5 | 0.0 | 30.0 | 11.511 |
| Tello | 1 | 39.2 | 55.1 | 2.103 |
| Tello | 2 | 39.2 | 55.1 | 1.052 |
| Tello | 3 | 39.2 | 55.1 | 0.701 |
| Tello | 5 | 39.2 | 55.1 | 0.421 |
| Open quad 4K | 1 | 48.4 | 81.6 | 0.791 |
| Open quad 4K | 2 | 48.4 | 81.6 | 0.395 |
| Open quad 4K | 3 | 48.4 | 81.6 | 0.264 |
| Open quad 4K | 5 | 48.4 | 81.6 | 0.158 |

Hours are for one square mile. Ground team figures come from the ASRC sweep-width source and an assumed 1.5 km/h; drone speed and endurance for the open quad are assumptions, as the planner output notes.

**Reproduce.** `DATASETS_DIR=/path/to/datasets PYTHON=venv/bin/python scripts/run_altitude_benchmark.sh` fetches the test split if absent, runs both modes (several minutes on an M1 Max), and redraws the plot.
<!-- build:t11 end -->
