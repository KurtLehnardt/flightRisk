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

### Leaflet (BSD-2-Clause)

The Leaflet JavaScript mapping library (https://leafletjs.com/) is vendored as `flightrisk/dashboard/static/leaflet/` under the BSD-2-Clause license. See `flightrisk/dashboard/static/leaflet/LICENSE` for details.
