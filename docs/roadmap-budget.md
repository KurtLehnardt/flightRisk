# Roadmap and budget appendix

This appendix backs [docs/one-page-plan.md](one-page-plan.md). Prices marked ASSUMPTION are planning estimates that must be checked before purchase. Only the SIYI and Jetson prices come from a cited source.

## $5K milestone

The $5,000 milestone stands on its own. It funds an open-source quad and a Part 107 pilot for the first real flight that validates the measured `min_person_px` threshold at the planned altitude (see [altitude-benchmark.md](altitude-benchmark.md)). It does not need the gimbal or the Jetson.

| Item | Qty | Unit USD | Total USD | Source |
|---|---|---|---|---|
| Open-source quad kit with Pixhawk-class flight controller, GPS, telemetry radio | 1 | 1100 | 1100 | ASSUMPTION |
| Flight batteries | 4 | 90 | 360 | ASSUMPTION |
| Fixed-mount 4K camera | 1 | 350 | 350 | ASSUMPTION |
| Part 107 remote pilot, field days | 3 | 400 | 1200 | ASSUMPTION |
| FAA aircraft registration | 1 | 5 | 5 | ASSUMPTION |
| Liability insurance for validation flights | 1 | 500 | 500 | ASSUMPTION |
| Ground targets and markers | 1 | 250 | 250 | ASSUMPTION |
| Spares and crash repair | 1 | 500 | 500 | ASSUMPTION |
| Contingency | 1 | 500 | 500 | ASSUMPTION |
| Total | | | 4765 | |

## $10K plan

The $10,000 plan adds a SIYI A8 mini class gimbal camera, a Jetson Orin Nano Super so the edge process runs onboard, and a second airframe for fleet flights. It replaces the fixed-mount camera.

| Item | Qty | Unit USD | Total USD | Source |
|---|---|---|---|---|
| Open-source quad kit with Pixhawk-class flight controller, GPS, telemetry radio | 2 | 1100 | 2200 | ASSUMPTION |
| Flight batteries | 8 | 90 | 720 | ASSUMPTION |
| SIYI A8 mini class gimbal camera (about $230 to $285) | 1 | 285 | 285 | https://www.readymaderc.com/products/details/87067-siyi-a8-mini-4k-8mp-ultra-hd-6x-digital-zoom-gimbal-camera |
| Jetson Orin Nano Super Developer Kit | 1 | 249 | 249 | https://www.phoronix.com/news/NVIDIA-Jetson-Orin-Nano-Super |
| Jetson storage, power and mounting | 1 | 200 | 200 | ASSUMPTION |
| Flight-controller to companion computer harness | 1 | 100 | 100 | ASSUMPTION |
| Part 107 remote pilot, field days | 6 | 400 | 2400 | ASSUMPTION |
| FAA aircraft registration | 2 | 5 | 10 | ASSUMPTION |
| Liability insurance | 1 | 900 | 900 | ASSUMPTION |
| Ground targets and markers | 1 | 250 | 250 | ASSUMPTION |
| Spares and crash repair | 1 | 800 | 800 | ASSUMPTION |
| Travel to two Utah field sites | 1 | 400 | 400 | ASSUMPTION |
| Contingency | 1 | 1000 | 1000 | ASSUMPTION |
| Total | | | 9514 | |

## Milestones

- M1, validate the threshold: run `flightrisk/evaluation/detection.py` on the field footage and plan the flight with `flightrisk/drone/coverage.py`. Funded by the $5K milestone.
- M2, onboard compute: run `flightrisk/edge_device.py` and `flightrisk/edge.py` on the Jetson with the gimbal camera.
- M3, fleet: coordinate two aircraft through `flightrisk/drone/fleet.py` and `flightrisk/drone/mavlink.py`.
- M4, field trial: show the ground dashboard in `flightrisk/dashboard/` to a Utah SAR team on a real search area.

## Licensing and swap table

FlightRisk is GPL-3.0-or-later (https://www.gnu.org/licenses/gpl-3.0.txt). Ultralytics YOLO11 is AGPL-3.0 (https://ultralytics.com/license). GPLv3 section 13 (§13) permits combining GPLv3 code with AGPLv3 code, but the AGPL terms still apply to that component (https://www.gnu.org/licenses/gpl-3.0.html#section13).

| Component | License | Note | Swap item |
|---|---|---|---|
| FlightRisk | GPL-3.0-or-later | Project license. | None. |
| Ultralytics YOLO11 | AGPL-3.0 | Terms still apply to this component. | Detector swap: an Apache-2.0 detector (YOLOX, RF-DETR or D-FINE; ASSUMPTION, verify each license) behind `PersonDetector` in `flightrisk/vision/detector.py`, or the Ultralytics enterprise license. |
| InsightFace pretrained packs | Non-commercial research only (https://github.com/deepinsight/insightface) | Face recognition is off by default and irrelevant at altitude. | Leave disabled. |
| HERIDAL dataset | CC-BY-3.0 (https://zenodo.org/records/5662351) | Never committed; cited in the benchmark doc. | LADD sample as fallback. |
| Leaflet | BSD-2-Clause | Vendored. | None. |
| djitellopy, MAVSDK | MIT, BSD-3 (ASSUMPTION, verify) | Runtime dependencies. | None. |

## Regulatory note

- Real flights are small-UAS flights and need a Part 107 remote pilot; waivers cover operations outside the standard rules: https://www.faa.gov/uas/commercial_operators/part_107_waivers
- Emergency situations have a special governmental interest process: https://www.faa.gov/uas/advanced_operations/emergency_situations
- Part 108 (beyond visual line of sight) is a proposed rule: https://www.dlapiper.com/insights/publications/2025/10/faa-proposed-part-108-bvlos-rule
- ASSUMPTION: verify the current status of all three before any flight. The hackathon needs no regulatory approval.

## Kubernetes

We recommend no Kubernetes. For field control it is the wrong tool: the ground station is one offline laptop at a trailhead, drones are not schedulable nodes, and link loss must trigger landing, not rescheduling. For a simulated-drone farm, `docker compose` with N services covers 3 to 6 instances on one laptop. Revisit only if we ever run 50+ SITL instances in CI.

## Honest limits

- Dense tree canopy defeats RGB and most thermal imaging; the fit is open terrain (desert, canyon, meadow, snow), which is most of Utah's SAR ground.
- The `min_person_px` threshold comes from a public aerial dataset (HERIDAL), not from our own airframe. The first real flight in the $5K milestone is what tests it.
- Open-quad flight time and speed are ASSUMPTION until measured.
- Faces are not resolvable at search altitude, so the system is appearance-only by design.
- The customer is a target (ASSUMPTION: no contact yet), not a partner.
- This is a solo-team plan (ASSUMPTION) and not a business plan or financial model.
