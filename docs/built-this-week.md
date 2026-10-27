# Built this week

This page is the evidence of what FlightRisk already was before the Hard Tech Week kickoff and what was built during the week. Git backs every claim: the kickoff is commit `798f21a`, tagged `hackathon/kickoff` (Monday 2026-10-26, 16:00 MDT), and the path lists below were computed from `git diff --name-status 798f21a..HEAD`.

Check it yourself:

    git show hackathon/kickoff --stat
    git log --oneline 798f21a..HEAD
    git diff --name-status 798f21a..HEAD

`tests/test_docs_built_this_week.py` checks the lists against git: every "(new)" path is absent at `798f21a` and present now, and every "(changed)" path exists at `798f21a`.

## Before kickoff

At `798f21a` the repository already contained a single-drone search system:

- Person detection (YOLO), re-identification and tracking pipeline.
- DJI Tello control and MAVLink control.
- A web dashboard.
- An edge/ground split with a transport between the two.
- Android and iOS mobile apps.
- 676 tests passing, 9 deselected (the `sitl` marker).

It had no fleet protocol, no coverage planner, no altitude benchmark and no license file.

## Built this week

Tests: 676 passed, 9 deselected before kickoff; 1006 passed, 10 deselected after the week's work, including the three tests for this page (`venv/bin/python -m pytest -q -p no:cacheprovider`). The tenth deselected test is an extra `sitl`-marked test.

Each path below is tagged "(new)" when it did not exist at `798f21a` and "(changed)" when it did and was modified. The 103 paths of the diff are listed, plus this page and its test, which are committed after the log below was taken.

- Altitude benchmark and fine-tuning: a HERIDAL benchmark runner, a tiled detector and a fine-tuned `yolo11s`. Headline numbers from `docs/altitude-benchmark.md`: the stock `yolo11n` found 0 of 337 people in a single pass and 48 of 337 (overall recall 0.142) tiled, and never reached 0.5 recall in any bin. The fine-tuned `yolo11s`, run tiled at confidence 0.5, reaches overall recall 0.602 at precision 0.832 and a measured `min_person_px` of 24 px. That 24 px rests on 7 people (4 found); the conservative reading is 32 px, with recall 0.611 on 108 people. About 4 in 10 people are still missed at every height. Test split: 101 HERIDAL images, 337 people.
- Coverage planner: lawnmower planner with Tello, open quad and ground-team presets, per-preset altitude and range limits, pixel-to-ground geolocation, a MAVSDK gimbal and a SITL flight script.
- Fleet protocol, coordinator and safety: fleet control messages and clock sync over the edge/ground transport, a `FleetCoordinator` that splits zones and re-tasks a lost drone's lanes, and an edge `SafetyEnvelope` (geofence, altitude slot, battery gate, link-loss watchdog) with emergency landing.
- Dashboard fleet UI: offline Leaflet map, fleet panel, plan and start controls, LAND ALL button, loopback bind by default.
- Two-network fleet setup and Windows edge: fleet setup guide with environment scripts and a link check, Windows edge installer with minimal dependencies.
- N-drone fake-fleet demo: demo launchers and a fake-edge fleet runner that need no drone.
- Privacy fixes: appearance-only matching by default, no import-time directory creation, a privacy design page and a corrected privacy policy.
- License: GPL-3.0-or-later with a third-party licensing page.
- Pitch materials: pitch, demo script, one-page plan, roadmap and budget, and README section.

### Altitude benchmark and fine-tuning

- `datasets/README.md` (new)
- `docs/altitude-benchmark.md` (new)
- `docs/img/altitude_recall.png` (new)
- `docs/img/altitude_recall_finetuned.png` (new)
- `flightrisk/evaluation/cli.py` (changed)
- `flightrisk/evaluation/detection.py` (new)
- `flightrisk/vision/detector.py` (changed)
- `results/altitude_finetuned.json` (new)
- `results/altitude_single.json` (new)
- `results/altitude_sweep.json` (new)
- `results/altitude_tiled.json` (new)
- `scripts/download_heridal.sh` (new)
- `scripts/finetune_heridal.py` (new)
- `scripts/run_altitude_benchmark.sh` (new)
- `scripts/sweep_altitude.py` (new)
- `tests/fixtures/aerial_tiny/img_a.png` (new)
- `tests/fixtures/aerial_tiny/img_a.xml` (new)
- `tests/fixtures/aerial_tiny/img_b.png` (new)
- `tests/fixtures/aerial_tiny/img_b.xml` (new)
- `tests/fixtures/aerial_tiny/img_c.png` (new)
- `tests/fixtures/aerial_tiny/img_c.xml` (new)
- `tests/test_benchmark_doc.py` (new)
- `tests/test_detection_eval.py` (new)
- `tests/test_finetune_script.py` (new)
- `tests/test_tiled_detector.py` (new)

### Coverage planner and SITL track

- `docs/sitl-demo.md` (new)
- `flightrisk/drone/coverage.py` (new)
- `flightrisk/drone/geo.py` (new)
- `flightrisk/drone/geolocate.py` (new)
- `flightrisk/drone/lane_executor.py` (new)
- `flightrisk/drone/mavlink.py` (changed)
- `scripts/sitl_fly_coverage.py` (new)
- `scripts/start_sitl.sh` (changed)
- `tests/integration/test_sitl.py` (changed)
- `tests/test_coverage.py` (new)
- `tests/test_geo.py` (new)
- `tests/test_geolocate.py` (new)
- `tests/test_lane_executor.py` (new)
- `tests/test_mavlink_gimbal.py` (new)
- `tests/test_sitl_scripts.py` (new)

### Fleet protocol, coordinator and safety

- `docs/fleet-scaling.md` (new)
- `flightrisk/config.py` (changed)
- `flightrisk/drone/coordinator.py` (new)
- `flightrisk/drone/safety.py` (new)
- `flightrisk/drone/tello.py` (changed)
- `flightrisk/edge.py` (changed)
- `flightrisk/edge_device.py` (changed)
- `flightrisk/fleet_protocol/__init__.py` (new)
- `flightrisk/ground.py` (changed)
- `flightrisk/transport.py` (changed)
- `scripts/tello_hw_check.py` (new)
- `tests/conftest.py` (changed)
- `tests/test_coordinator.py` (new)
- `tests/test_fleet_protocol.py` (new)
- `tests/test_hw_check.py` (new)
- `tests/test_retask_refusal.py` (new)
- `tests/test_safety.py` (new)

### Dashboard fleet UI

- `flightrisk/dashboard/__main__.py` (changed)
- `flightrisk/dashboard/alerts.py` (changed)
- `flightrisk/dashboard/app.py` (changed)
- `flightrisk/dashboard/pipeline.py` (changed)
- `flightrisk/dashboard/state.py` (changed)
- `flightrisk/dashboard/static/leaflet/LICENSE` (new)
- `flightrisk/dashboard/static/leaflet/leaflet.css` (new)
- `flightrisk/dashboard/static/leaflet/leaflet.js` (new)
- `flightrisk/dashboard/templates/index.html` (changed)
- `tests/e2e/test_fleet_controls.py` (new)
- `tests/e2e/test_map_ui.py` (new)
- `tests/test_dashboard_bind.py` (new)
- `tests/test_fleet_dashboard.py` (new)

### Two-network fleet setup and Windows edge

- `docs/fleet-setup.md` (new)
- `docs/windows-edge-setup.md` (new)
- `requirements-edge.txt` (new)
- `scripts/check_fleet_link.py` (new)
- `scripts/demo_edge.ps1` (new)
- `scripts/demo_edge.sh` (new)
- `scripts/demo_ground.sh` (new)
- `scripts/fleet_env.ps1` (new)
- `scripts/fleet_env.sh` (new)
- `scripts/install_windows.ps1` (new)
- `tests/test_edge_minimal_deps.py` (new)
- `tests/test_fleet_setup_docs.py` (new)
- `tests/test_windows_setup.py` (new)

### N-drone fake-fleet demo

- `demo/README.md` (new)
- `scripts/demo_fake_fleet.py` (new)
- `tests/test_demo_smoke.py` (new)
- `tests/test_fake_fleet.py` (new)

### Privacy fixes

- `docs/privacy-design.md` (new)
- `docs/privacy-policy.md` (changed)
- `tests/test_appearance_only.py` (new)
- `tests/test_docs_privacy.py` (new)
- `tests/test_privacy.py` (new)

### License

- `LICENSE` (new)
- `docs/licensing.md` (new)
- `tests/test_license.py` (new)

### Pitch materials and documentation

- `.gitignore` (changed)
- `README.md` (changed)
- `docs/built-this-week.md` (new)
- `docs/demo-script.md` (new)
- `docs/one-page-plan.md` (new)
- `docs/pitch.md` (new)
- `docs/roadmap-budget.md` (new)
- `tests/test_docs_built_this_week.py` (new)
- `tests/test_docs_pitch.py` (new)
- `tests/test_docs_roadmap.py` (new)

## Commits since kickoff

Output of `git log --oneline 798f21a..HEAD` on the branch that holds this page (84 commits; the commit that adds this page is not in it):

```
d952744 feat(t99): integration demo launchers [R-032,R-033,R-034] — gate PASS
c380362 test(t99): launcher, README and two-fake-edge smoke tests [R-032,R-033,R-034]
e025332 docs(t99): README Hackathon demo (Nucleus bounty) section [R-033]
6138293 feat(t99): demo launchers for ground and edge [R-032]
16a696e feat(t15): pitch demo script [R-040,R-041,R-062,R-063,R-064,R-083,R-100] — gate PASS
6c4fb89 docs(t15): pitch deck and demo script for the Nucleus bounty [R-040,R-041,R-062,R-063,R-064,R-083,R-100]
218ad98 feat(t26): nack passthrough [R-108] — gate PASS
08433d3 feat(t27): coverage realism [R-109] — gate PASS
2285dfd docs(t27): regenerate coverage tables at 24 and 32 px, drop stale bind line [R-109]
e470991 feat(t27): per-preset altitude and range limits in the coverage planner [R-109]
f6e3a85 feat(t26): deliver edge nack to ground on_control [R-108]
839ba0a feat(t25): retask refusal secure bind [R-106,R-107] — gate PASS
768bc7d feat(t22): finetune heridal [R-085] — gate PASS
346668b docs(t22): fine-tuned yolo11s reaches 0.5 recall from 24 px on the HERIDAL test split [R-085]
308eeca fix(t22): fetch only train or val subsets and align usage with the measured run [R-085]
7a286d3 feat(t25): dashboard binds 127.0.0.1 by default and refuses an unauthenticated non-loopback bind [R-107]
189d0d5 feat(t25): show uncovered re-task lanes in the fleet panel, add refusal tests [R-106]
a8569cc feat(t25): coordinator offers re-tasked lanes only inside geofences and reports refusals [R-106]
e9f089b feat(t25): edge answers a refused lane assignment with a nack [R-106]
29e55c2 feat(t20): sitl fleet n [R-082] — gate PASS
078e57f feat(t20): fake-edge fleet launcher, tests and scaling doc [R-082]
11c8e80 feat(t19): sitl single drone altitude [R-050,R-080] — gate PASS
cdebb7a test(t19): drop added skip, resolve live mavlink module [R-080]
0f7b151 feat(t19): SITL coverage flight script, start_sitl instances, demo doc, Millcreek origin [R-050,R-080]
bb460d2 feat(t22): fetch a HERIDAL train subset, build tiled YOLO data and fine-tune on MPS [R-085]
6f5ad36 feat(t24): fleet plan start controls [R-105] — gate PASS
c956af2 feat(t24): fleet Plan search and Start search controls [R-105]
7334e37 feat(t22): sweep stock YOLO models and confidences tiled on the HERIDAL test split [R-085]
b5b3eec feat(t09): dashboard fleet wiring [R-001,R-019,R-020,R-095,R-097] — gate PASS
0568ce5 test(t09): cover appearance-only, --no-llm, status fields, LAND ALL and fleet events [R-001,R-019,R-020,R-095,R-097]
a30c1b2 feat(t09): wire the dashboard to the fleet with appearance-only and --no-llm defaults [R-001,R-019,R-020,R-095,R-097]
5c479cc feat(t23): windows edge minimal deps [R-104] — gate PASS
c14096b feat(t11): benchmark run results [R-072] — gate PASS
ebb974f feat(t23): install Windows edge from requirements-edge.txt [R-104]
fc21fe9 feat(t07): edge tello executor safety [R-090,R-091,R-092,R-093,R-094,R-096] — gate PASS
6d3fdee feat(t11): run altitude benchmark on HERIDAL test split and publish results [R-072]
99ddb04 fix(t07): refuse a scheduled takeoff when the ground link is already lost [R-094]
549f642 feat(t07): land from the transport emergency callback and skip duplicate concurrent lands [R-090,R-094]
5de8b0d fix(t07): refuse takeoff after executor shutdown and stop control intake before landing on exit [R-092,R-096]
87dcb09 feat(t06): fleet protocol clock sync [R-090,R-091] — gate PASS
17ac69f test(t07): cover geofence, battery gate, altitude slot, link-loss watchdog and Tello land bypass [R-093,R-094]
c1401b4 test(t07): cover lane execution, start_at ack offsets, re-task progress and land paths [R-090,R-092,R-096]
e082a72 fix(t06): queue emergency_land_all before starting its land thread [R-090]
f406814 test(t06): move fleet protocol test imports to module top [R-090,R-091]
f70bbc6 feat(t07): fly Tello and fake sources from the edge with a control thread, land key and SIGINT land [R-090,R-092,R-096]
786cb12 fix(t06): keep the edge reader from ever waiting on a send [R-090,R-091]
af3818f feat(t07): carry lane progress across mid-flight re-tasks and add executor fail-safe [R-092,R-094]
d355cce test(t06): cover fleet control, emergency landing and clock sync over loopback [R-090,R-091]
6334497 feat(t08): fleet coordinator [R-081,R-095] — gate PASS
7ab9208 feat(t07): add LaneExecutor for scheduled starts, lane flight, telemetry and auto-land [R-092,R-093,R-094]
4b8cea8 feat(t06): carry fleet control and clock sync over the edge/ground transport [R-090,R-091]
f9f87fc fix(t08): stop sending start_at after the first failed send [R-081]
39f85de test(t08): check sync and land_all with replies from another thread [R-081,R-095]
320ef30 test(t08): cover FleetCoordinator with fake edge links [R-081,R-095]
c84b319 feat(t07): make Tello land interrupt in-flight commands and add an in-process fake Tello [R-092,R-093]
f026a99 feat(t06): carry drone id and pose on detections messages [R-090]
a1c0db3 feat(t06): add fleet control protocol schemas and clock math [R-090,R-091]
7c574bf feat(t07): add SafetyEnvelope with geofence, altitude slot, battery gates and link watchdog [R-093,R-094]
d2e4da8 feat(t08): add FleetCoordinator over abstract edge links [R-081,R-095]
184999a feat(t10): map ui fleet panel [R-021,R-022,R-023,R-024,R-095,R-097] — gate PASS
46e05b2 feat(t18): geolocate gimbal replay [R-077,R-078,R-079] — gate PASS
a4928c1 feat(t16): privacy policy design docs [R-006,R-083] — gate PASS
9756b91 feat(t13): windows edge setup [R-103] — gate PASS
7f94f4b feat(t12): fleet setup docs env [R-098] — gate PASS
3124568 feat(t01): tello hw check hygiene [R-031,R-102] — gate PASS
6ad1008 feat(t03): altitude benchmark runner [R-070,R-071] — gate PASS
a1916bb feat(t14): one page plan roadmap [R-042,R-061,R-065,R-083] — gate PASS
0935b81 feat(t02): coverage planner geo [R-010,R-011,R-074,R-075,R-076] — gate PASS
064f583 feat(t21): license gpl3 [R-101] — gate PASS
9030654 feat(t05): privacy minimum [R-003,R-005] — gate PASS
c009287 feat(t10): offline Leaflet map, fleet panel, LAND ALL button and Shift+L hotkey [R-021,R-022,R-023,R-024,R-095,R-097]
9841cab docs(t16): fix privacy policy contradiction and add privacy design [R-006,R-083]
3d1e423 feat(t04): tiled inference config [R-002,R-073] — gate PASS
2f422bf feat(t18): pixel-to-ground geolocation, MAVSDK gimbal pitch, aerial frame replay [R-077,R-078,R-079]
4fd4f61 docs(t21): add GPL-3.0-or-later LICENSE and licensing.md [R-101]
2410b00 feat(t01): Tello hardware check script and datasets/demo hygiene [R-031,R-102]
5fa28ab docs(t12): fleet setup guide, fleet_env scripts and link check [R-098]
52c27c8 test(t13): fix script negative assertions [R-103]
331746f docs(t14): one-page plan and roadmap/budget appendix [R-042,R-061,R-065,R-083]
7f78860 feat(t02): coverage planner, presets and geo helpers [R-010,R-011,R-074,R-075,R-076]
dcacf6e feat(t05): gate match snapshots, drop import-time mkdir, isolate captures in tests [R-003,R-005]
6e6bb86 feat(t03): altitude benchmark runner, HERIDAL download script, fixtures, doc template [R-070,R-071]
51a5377 feat(t13): Windows edge install script, setup doc and static test [R-103]
696251f feat(t04): tiled inference with NMS merge and config flags [R-002,R-073]
```

## Attempted but not finished

- SITL never completed a flight on this machine. MAVSDK connected, set parameters and sent arm and takeoff, but the only ready-made image tried (an old ArduCopter 4.0 build) gave no position updates, and the run ended with a timeout. No flight result is recorded. Details are in `docs/sitl-demo.md`.
- The live two-Tello flight is the demo itself. It has not yet been flown in rehearsal. What has run is the fleet code against fake edges and in-process fake Tellos.

## Datasets and weights

- The HERIDAL data is not in git. `datasets/*` is gitignored (only `datasets/README.md` is tracked). `scripts/download_heridal.sh` and `scripts/finetune_heridal.py fetch` pull it from Zenodo record 5662351 (CC-BY-3.0).
- The fine-tuned weights `models/heridal_yolo11s.pt` are not in git either: `models/` and `*.pt` are gitignored, and no download link is published. Reproduce them with the commands in the "Reproduce" section of `docs/altitude-benchmark.md`; the fine-tune took 51 minutes on an Apple M1 Max, and a rerun gives close but not identical numbers.
- The raw benchmark results are committed in `results/`.
