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

<!-- results: filled in by the benchmark run (task t11) -->

| bin_px | n_gt | recall_single | precision_single | recall_tiled | precision_tiled |
|---|---|---|---|---|---|
| [0,8) | pending | pending | pending | pending | pending |
| [8,16) | pending | pending | pending | pending | pending |
| [16,24) | pending | pending | pending | pending | pending |
| [24,32) | pending | pending | pending | pending | pending |
| [32,48) | pending | pending | pending | pending | pending |
| [48,64) | pending | pending | pending | pending | pending |
| [64,inf) | pending | pending | pending | pending | pending |

min_person_px (single): pending
min_person_px (tiled): pending

Split used: pending.
