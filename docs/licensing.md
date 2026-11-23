# Licensing

## FlightRisk

FlightRisk is licensed under the **GNU General Public License v3.0 or later** (SPDX: `GPL-3.0-or-later`). See the [`LICENSE`](/LICENSE) file for the full text.

## Third-party components

### Ultralytics YOLO11 (AGPL-3.0)

The `PersonDetector` uses Ultralytics YOLO11 for small-object detection at altitude. Ultralytics YOLO11 is licensed under the **GNU Affero General Public License v3.0** (https://ultralytics.com/license).

GPLv3 Section 13 permits combining GPLv3 code with AGPLv3 code. The AGPL terms still apply to the YOLO component. For production deployment or commercial use, consider the Ultralytics Enterprise license or swap the detector for an Apache-2.0 licensed alternative (YOLOX, RF-DETR, or D-FINE).

### InsightFace

The InsightFace pretrained model packs are provided for **non-commercial research use only** (https://github.com/deepinsight/insightface). In this project, face recognition is disabled by default for the demo (`appearance_only=True`), and faces are not resolvable at search altitude anyway.

### MiDaS (MIT) and gen-efficientnet-pytorch (Apache-2.0)

The MiDaS v2.1 Small depth model used by the obstacle guard is vendored, trimmed to that one model, as `flightrisk/third_party/midas_small/`: `midas_net_small.py` from https://github.com/isl-org/MiDaS (MIT License, Intel ISL) and `efficientnet_lite.py` from https://github.com/rwightman/gen-efficientnet-pytorch (Apache License 2.0, Ross Wightman). Both licenses are compatible with GPLv3. The license texts are `LICENSE-MiDaS` and `LICENSE-gen-efficientnet` in that folder, and each file lists the changes made to it. The weights file, `midas_v21_small_256.pt`, is downloaded from the MiDaS release at install time and is not distributed with FlightRisk.

### Mobile app models: YOLOX-S (Apache-2.0) and open_clip ViT-B-16 laion2b (MIT)

Store builds of the Android and iOS apps use only YOLOX-S (Megvii, Apache-2.0
code and weights) as the person detector and the image tower of open_clip
ViT-B-16 `laion2b_s34b_b88k` (MIT) as the appearance embedder. Neither is in
this repository: `scripts/mobile_models/make_models.py` downloads the pinned
weights, checks their SHA-256, converts them and installs them into the apps'
gitignored model folders before a release build. The inference parts of the
YOLOX model code used for the conversion are vendored in
`scripts/mobile_models/yolox_vendor/` with their `LICENSE` and a `NOTICE` that
lists the changes. The licence texts the apps must show are in
`scripts/mobile_models/NOTICES.md`. The Ultralytics, InsightFace and MobileCLIP
models described above must never be in a store build;
`scripts/mobile_models/licence_guard.py` fails a build that contains them. See
[`docs/mobile-models.md`](mobile-models.md), which also lists the open legal
questions, including the GPL and App Store question.

### Leaflet (BSD-2-Clause)

The Leaflet JavaScript mapping library (https://leafletjs.com/) is vendored as `flightrisk/dashboard/static/leaflet/` under the BSD-2-Clause license. See `flightrisk/dashboard/static/leaflet/LICENSE` for details.
