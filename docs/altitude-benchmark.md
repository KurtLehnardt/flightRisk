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

<!-- build:t22 start -->
## Fine-tuned detector and model sweep

Measured on 2026-10-27 with `scripts/sweep_altitude.py` and `scripts/finetune_heridal.py`. Raw numbers are in `results/altitude_sweep.json` and `results/altitude_finetuned.json`.

Every row below uses t11's HERIDAL test split (101 images, 4000x3000, 337 persons), t11's tiling (640 px tiles, 0.2 overlap), IoU 0.3 matching and the same bins, on `mps` (Apple M1 Max). A cell is recall, with true positives over ground truth persons in that bin. No test person is shorter than 24 px, so the empty bins below 24 px are left out.

**Before and after.**

| configuration | recall [24,32) | recall [32,48) | recall [48,64) | recall [64,inf) | overall recall | overall precision | min_person_px |
|---|---|---|---|---|---|---|---|
| Stock yolo11n, single pass, conf 0.4 (t11) | 0.000 (0/7) | 0.000 (0/108) | 0.000 (0/111) | 0.000 (0/111) | 0.000 (0/337) | 0.000 | none |
| Stock yolo11n, tiled, conf 0.4 (t11) | 0.000 (0/7) | 0.046 (5/108) | 0.081 (9/111) | 0.306 (34/111) | 0.142 (48/337) | 0.696 | none |
| Best stock sweep: yolo11m, tiled, conf 0.1 | 0.143 (1/7) | 0.343 (37/108) | 0.387 (43/111) | 0.613 (68/111) | 0.442 (149/337) | 0.596 | 64 |
| Fine-tuned yolo11s, tiled, conf 0.5 | 0.571 (4/7) | 0.611 (66/108) | 0.550 (61/111) | 0.649 (72/111) | 0.602 (203/337) | 0.832 | 24 |

![Recall by person pixel height: fine-tuned yolo11s against the stock models](img/altitude_recall_finetuned.png)

**Measured `min_person_px` of the best configuration: 24 px.** The best configuration is yolo11s fine-tuned on HERIDAL train tiles, run tiled at confidence 0.5. The definition is t11's: the lower edge of the first bin with recall of at least 0.5 (`min_person_px` in `flightrisk/evaluation/detection.py`).

How much weight each number carries:

- The 24 px value rests on 7 persons, of whom 4 were found. The 95% Wilson interval for that bin's recall is 0.25 to 0.84, so these data cannot rule out recall below 0.5 at 24 to 32 px.
- 32 px is the conservative reading. The [32,48) bin has 108 persons and recall 0.611, with a 95% interval of 0.52 to 0.70, clear of 0.5.
- Recall plateaus instead of rising with height. The [48,64) bin is 0.550 (95% interval 0.46 to 0.64) and [64,inf) is 0.649. About 4 in 10 persons are still missed at every height.
- Precision is 0.832: 41 false positives over 101 frames, about 0.4 per 12 MP frame. Every test image contains a person, so the test split does not measure false alarms over empty terrain. On the 50 validation images, 22 of which have no person, the same threshold gave 23 false positives (0.46 per frame).
- HERIDAL is one camera system at 40 to 65 m. The pixel threshold carries over to other cameras and terrain only approximately.

**Stock model sweep.** The sweep ran on the test split, so its best row was chosen by looking at test results. Treat that row as an optimistic bound for stock COCO weights, not a held-out result. Each model ran once at the lowest confidence, and higher thresholds were scored by dropping lower-scored boxes. Both NMS stages are greedy by score, so this gives the same boxes as separate runs. The yolo11n row at confidence 0.4 reproduces t11's tiled result exactly in every bin, which confirms it.

| model | conf | recall [24,32) | recall [32,48) | recall [48,64) | recall [64,inf) | overall recall | overall precision | min_person_px |
|---|---|---|---|---|---|---|---|---|
| yolo11n | 0.1 | 0.143 (1/7) | 0.269 (29/108) | 0.333 (37/111) | 0.550 (61/111) | 0.380 | 0.318 | 64 |
| yolo11n | 0.25 | 0.000 (0/7) | 0.157 (17/108) | 0.189 (21/111) | 0.414 (46/111) | 0.249 | 0.571 | none |
| yolo11n | 0.4 | 0.000 (0/7) | 0.046 (5/108) | 0.081 (9/111) | 0.306 (34/111) | 0.142 | 0.696 | none |
| yolo11s | 0.1 | 0.143 (1/7) | 0.231 (25/108) | 0.297 (33/111) | 0.532 (59/111) | 0.350 | 0.480 | 64 |
| yolo11s | 0.25 | 0.143 (1/7) | 0.130 (14/108) | 0.153 (17/111) | 0.450 (50/111) | 0.243 | 0.678 | none |
| yolo11s | 0.4 | 0.000 (0/7) | 0.083 (9/108) | 0.081 (9/111) | 0.387 (43/111) | 0.181 | 0.782 | none |
| yolo11m | 0.1 | 0.143 (1/7) | 0.343 (37/108) | 0.387 (43/111) | 0.613 (68/111) | 0.442 | 0.596 | 64 |
| yolo11m | 0.25 | 0.143 (1/7) | 0.157 (17/108) | 0.225 (25/111) | 0.495 (55/111) | 0.291 | 0.784 | none |
| yolo11m | 0.4 | 0.143 (1/7) | 0.102 (11/108) | 0.171 (19/111) | 0.414 (46/111) | 0.228 | 0.895 | none |

The sweep's rule picks the lowest `min_person_px`, then the highest overall F1. It chose yolo11m at confidence 0.1. On stock weights, a lower threshold buys recall with precision, and no stock row reaches 0.5 recall below 64 px.

**Fine-tuning setup.**

- Train data: 400 images from the official HERIDAL train list, 1,054 persons. They were drawn at random (seed 0) from the 891 of 1,391 train images that contain at least one person. The person images alone yield far more empty tiles than training needs, so the slow download went to images with labels. The script fetched them from the Zenodo zip with HTTP range requests (about 2 GB) instead of the 8.3 GB archive.
- Validation data: 50 images drawn at random (seed 0) from the official val list of 155, including 22 with no person, 73 persons in all. They were never trained on. Ultralytics used their tiles to keep the best checkpoint, and the evaluate step used the full images to pick the confidence threshold.
- Test data: the 101-image test split. The fetch excludes every test name from the train and val subsets. No test image was trained on or used to pick a checkpoint or threshold, and the fine-tuned model was scored on the test split once.
- Tiles: the same windows as `PersonDetector.compute_tiles` at inference (640 px, 0.2 overlap). A box is labelled in a tile when at least half its area lies inside, clipped to the tile edge. Every tile with a person is kept: 1,343 train tiles with 1,763 labels. Tiles clear of every box are subsampled to one per person tile (1,343 of 17,636). Tiles that cut through a person without labelling it are dropped.
- Training: `yolo11s.pt` (COCO) fine-tuned for 20 epochs, imgsz 640, batch 16, on `mps`, with Ultralytics 8.4.101 defaults otherwise (AdamW chosen automatically, mosaic off for the last 10 epochs). It took 51 minutes on the M1 Max. At the last epoch, Ultralytics reported mAP50 0.824 and mAP50-95 0.506 on the val tiles. The weights are `models/heridal_yolo11s.pt`, which is gitignored and not committed.
- Confidence: the evaluate step ran the 50 validation images tiled at 0.1, 0.25, 0.4 and 0.5, a list fixed before any test run, and kept the highest overall F1. It chose 0.5 (validation recall 0.740, precision 0.701, F1 0.720). F1 was still rising at 0.5, the top of the list. Higher thresholds were not tried, so the test split was scored once.

**Coverage at the measured value.** This is `python -m flightrisk.drone.coverage --min-px 24`:

| preset | n | alt_m | lane_m | hours |
|---|---|---|---|---|
| Ground team | 1 | 0.0 | 30.0 | 57.555 |
| Ground team | 2 | 0.0 | 30.0 | 28.778 |
| Ground team | 3 | 0.0 | 30.0 | 19.185 |
| Ground team | 5 | 0.0 | 30.0 | 11.511 |
| Tello | 1 | 104.5 | 146.9 | 0.772 |
| Tello | 2 | 104.5 | 146.9 | 0.386 |
| Tello | 3 | 104.5 | 146.9 | 0.257 |
| Tello | 5 | 104.5 | 146.9 | 0.154 |
| Open quad 4K | 1 | 120.0 | 202.3 | 0.318 |
| Open quad 4K | 2 | 120.0 | 202.3 | 0.159 |
| Open quad 4K | 3 | 120.0 | 202.3 | 0.106 |
| Open quad 4K | 5 | 120.0 | 202.3 | 0.064 |

Hours are for one square mile. At 24 px, the open quad reaches the planner's 120 m regulatory ceiling; its camera alone would allow a higher altitude. At the conservative 32 px (`--min-px 32`), one Tello flies at 78.4 m and takes 0.989 h, and one open quad flies at 96.8 m and takes 0.398 h. Ground team figures and the open quad's speed and endurance carry the assumptions the planner prints.

**What changed for the pitch.** t11 had no measured altitude. A yolo11s fine-tuned on 400 HERIDAL train images, run tiled, reaches 0.5 recall from 24 px on 7 persons, and from 32 px with a 95% interval clear of 0.5 on 108 persons, at precision 0.83. It still misses about 4 in 10 persons at every height. The coverage hours are therefore the time to sweep the area once at that recall, not the time to find everyone.

**Reproduce.** Run these from the repository root with the venv active. Data goes to the gitignored `datasets/` directory and weights to the gitignored `models/` directory, and neither is committed.

    D=datasets/heridal
    # Test split (101 images), fetched the way t11 did.
    DATASETS_DIR=datasets PYTHON=python scripts/run_altitude_benchmark.sh
    # Stock sweep on the test split (about 8 minutes on an M1 Max).
    python scripts/sweep_altitude.py --dataset $D/test --models yolo11n.pt models/yolo11s.pt models/yolo11m.pt \
        --conf 0.1 0.25 0.4 --output results/altitude_sweep.json
    # Validation and train subsets, fetched from the Zenodo zip with range requests.
    python scripts/finetune_heridal.py fetch --split val --limit 50 --out $D/val
    python scripts/finetune_heridal.py fetch --split train --limit 400 --positives-only --out $D/train
    # Tiles, fine-tuning on MPS, then the evaluation JSON and the plot.
    python scripts/finetune_heridal.py build --train $D/train --val $D/val --out $D/yolo_tiles
    python scripts/finetune_heridal.py train --data $D/yolo_tiles/data.yaml --model models/yolo11s.pt --epochs 20
    python scripts/finetune_heridal.py evaluate --weights models/heridal_yolo11s.pt --val $D/val --test $D/test
    python -m flightrisk.drone.coverage --min-px 24

Ultralytics downloads `yolo11s.pt` and `yolo11m.pt` into `models/` on first use. Training on MPS is not bit-for-bit deterministic, so a rerun gives close but not identical numbers.

**Attribution.** HERIDAL dataset in keras-retinanet PASCAL VOC format, by Pasi Pyrrö, Zenodo, 2021, https://doi.org/10.5281/zenodo.5662351, licensed CC-BY-3.0. That record is a modified version of the HERIDAL database (http://ipsar.fesb.unist.hr/HERIDAL%20database.html) by D. Božić-Štulić, Ž. Marušić and S. Gotovac, described in "Deep Learning Approach in Aerial Imagery for Supporting Land Search and Rescue Missions", International Journal of Computer Vision 127, 2019. FlightRisk changed the data only on local disk: it cut images into 640 px tiles and converted the VOC boxes to YOLO labels for training. No HERIDAL images or tiles, and no weights trained on them, are committed to this repository.
<!-- build:t22 end -->
