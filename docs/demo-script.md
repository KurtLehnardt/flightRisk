# FlightRisk demo script

This is the script for judging on Wednesday, October 28 (Nucleus × Startup State Hard Tech bounty). Slots run 8 to 25 minutes, including judge questions, depending on how many teams took the bounty. The core answers the brief's four test-case questions in order and always runs; the extended blocks fill longer slots. The slides are in [docs/pitch.md](pitch.md). Every figure below comes from a merged document, named next to it.

## What existed before kickoff

Kickoff is commit `798f21a`, tagged `hackathon/kickoff`. Before it, the repository already had a single-drone pipeline: person detection, re-identification, tracking, MAVLink control, an edge/ground split and 676 tests. Everything built this week is listed in `docs/built-this-week.md`. Say this out loud at the start of "Show it working".

## Hardware and roles

- Two Tello EDU drones. Drone 1 is controlled from the Mac and drone 2 from the Windows laptop, one edge process per host, so the two video streams never collide on one port (`docs/fleet-setup.md`).
- Mac: ground station and edge-1. It drives the projector with the dashboard and the deck (ASSUMPTION: an HDMI projector is available; organizer question).
- Windows laptop: edge-2.
- Station path: one 2.4 GHz router or phone hotspot that allows client-to-client traffic.
- AP path: one USB-C Gigabit Ethernet adapter for the Mac, one for the Windows laptop if it has no RJ45 port, and one Cat5e or Cat6 patch cable.
- Batteries: see the battery and rehearsal plan below.

## Pre-flight checklist

Finish this before the slot starts (ASSUMPTION: the organizers let us set up in the room before our slot). If the go/no-go check at the end fails, pick the fallback rung before the slot, not during it.

Both paths:

1. Placement and heading (do this before every flight). Mark the 4 m × 4 m area and a takeoff line along its near edge. Put drone-1 at the near-left corner. Put drone-2 250 cm to its right on the same line. Point both noses into the area along the 4 m depth (the +y direction the coordinator assumes). Keep the audience behind the takeoff line. The presenter stands at least 1.5 m beyond the far edge, outside the geofence, never in a flight path. The launchers set the edge geofence to 450 cm; override it with `FLIGHTRISK_GEOFENCE_CM` only if the floor is larger.
2. Floor: a clear 4 m × 4 m area with a ceiling of at least 2.5 m (ASSUMPTION; organizer question). The fleet plan's default area is 400 cm square (`FLIGHTRISK_FLEET_AREA_CM`), and the drones fly at altitude slots of 100 cm and 160 cm.
3. Batteries: a fresh battery in each drone, and each at 30% or more. The edge refuses takeoff below 30% and lands below 15%. Read the level in the hardware check report and again on the fleet panel.
4. The same token on both laptops: `export FLIGHTRISK_EDGE_TOKEN="choose-a-long-random-string"` on the Mac and `$env:FLIGHTRISK_EDGE_TOKEN = "choose-a-long-random-string"` on Windows.
5. Run the hardware check for your path (below) and keep its report on screen until both drones pass.

### Path 1: station mode (`FLIGHTRISK_FLEET_NET=station`)

The drones are already on the router (one-time `ap` step in `docs/fleet-setup.md`). The IPs below are that guide's examples; use the ones discovery prints.

Mac, hardware check report and settings:

```
venv/bin/python scripts/tello_hw_check.py --discover 192.168.1.0/24
venv/bin/python scripts/tello_hw_check.py --drones 192.168.1.50,192.168.1.51
venv/bin/python scripts/tello_hw_check.py --drones 192.168.1.50 --video-check
export FLIGHTRISK_FLEET_NET=station
export FLIGHTRISK_GROUND_IP=192.168.1.20
export FLIGHTRISK_TELLO_HOST=192.168.1.50
source scripts/fleet_env.sh
```

Windows settings:

```
$env:FLIGHTRISK_FLEET_NET = "station"
$env:FLIGHTRISK_GROUND_IP = "192.168.1.20"
$env:FLIGHTRISK_TELLO_HOST = "192.168.1.51"
. .\scripts\fleet_env.ps1
```

### Path 2: AP mode (`FLIGHTRISK_FLEET_NET=ap`)

The static addresses (Mac 10.42.0.1, Windows 10.42.0.2, no gateway) and firewall rules are set once, as in `docs/fleet-setup.md` Path 2 steps 1 to 3. Each laptop joins its own drone's `TELLO-XXXXXX` network.

Cable link check: run `venv/bin/python scripts/check_fleet_link.py --role server` on the Mac, then `venv\Scripts\python.exe scripts\check_fleet_link.py --role client --peer 10.42.0.1` on Windows. Both must print `PASS`. Stop the server with Ctrl-C before launching, because the ground station needs port 9000.

Mac, hardware check report and settings:

```
venv/bin/python scripts/tello_hw_check.py --ap-mode-check
export FLIGHTRISK_FLEET_NET=ap
source scripts/fleet_env.sh
```

Windows, hardware check report and settings:

```
venv\Scripts\python.exe scripts\tello_hw_check.py --ap-mode-check
$env:FLIGHTRISK_FLEET_NET = "ap"
. .\scripts\fleet_env.ps1
```

### Launch (both paths, three commands)

Start the ground station first. Mac, one terminal each:

```
scripts/demo_ground.sh
FLIGHTRISK_GROUND_LOCAL=1 DRONE_ID=1 ALT_CM=100 scripts/demo_edge.sh
```

In AP mode the ground station binds `127.0.0.1,10.42.0.1`. `FLIGHTRISK_GROUND_LOCAL=1` tells the Mac's edge-1 that the ground station is on the same machine, so it connects over loopback (`ws://127.0.0.1:9000`). The expectation is that it keeps running if the cable is pulled; that is UNVERIFIED on hardware, and `docs/fleet-setup.md` step 8 measures it. The inline form above exports the variable to the launcher. If you source `scripts/fleet_env.sh` yourself first, use `export FLIGHTRISK_GROUND_LOCAL=1`, because a plain shell variable is not inherited by the launcher. Station mode ignores the variable.

Windows:

```
$env:DRONE_ID = "2"; $env:ALT_CM = "160"; .\scripts\demo_edge.ps1
```

### Go/no-go

- The dashboard (http://127.0.0.1:5555 on the Mac; it binds to this machine only by default) lists both drones with battery, clock offset and round-trip time.
- If a drone is missing, check that the ground station's fleet list (`--fleet`, env `FLIGHTRISK_FLEET_DRONES`) names the same drone ids the edges use.
- Both batteries read 30% or more, and the floor area is clear.
- Fallbacks are staged: `demo/fleet.mp4` is open in a video player, and `venv/bin/python scripts/demo_fake_fleet.py --n 4 --dry-run` prints its command lines.

## Core (4:00)

Times are from the start of the slot. Each block opens with the brief's question, said out loud word for word.

### 0:00–0:35 · What's the hard tech?

Screen: deck slide 2, the before and after recall plots.

Say: "What's the hard tech? Two things. Finding a person who is a few dozen pixels tall in a frame taken from altitude, and flying a fleet that keeps searching when a drone drops out. We measured the first on HERIDAL, a public search-and-rescue aerial dataset: 337 people in 101 test images. A stock YOLO11n in one pass found none of them. Tiled, it found 14 percent. Fine-tuned this week, a YOLO11s finds 60 percent at 83 percent precision, solidly from 32 pixels tall. It still misses about 4 in 10."

Figures: recall 0.000, 0.142 and 0.602, precision 0.832, 32 px on 108 people (`docs/altitude-benchmark.md`).

### 0:35–2:35 · Show it working

- 0:35 Say: "Show it working. Before kickoff, commit 798f21a, this repository had a single-drone pipeline and 676 tests. This week I added the benchmark, the fine-tune, a coverage planner and the fleet. Every new file is listed in docs/built-this-week.md."
- 0:50 Do: switch to the dashboard. The fleet panel lists drone-1 (Mac) and drone-2 (Windows) with battery, clock offset and round-trip time. Open the Controls tab in the right sidebar and press **Plan search** in the Fleet group. Say: "The coordinator splits the floor into two zones, one per drone, at two altitude slots, 100 and 160 centimetres, so they share neither a zone nor an altitude."
- 1:05 Do: press **Start search** and confirm the prompt for two drones. Say: "Each drone's clock is synced to the ground over the WebSocket, eight ping rounds each, and both get one scheduled start time two seconds out. The skew column shows how far apart they actually started."
- 1:20 Do: let the lanes run. The presenter walks to a spot at least 1.5 m beyond the far edge of the area, outside the geofence and never in a flight path, where a drone camera can see them. Say: "Each laptop stands in for a computer on board. Detections from both drones go onto one map, tagged with the drone that saw them, and pins within 80 centimetres of each other merge."
- 1:55 Do: at the Windows laptop, type `l` and Enter in the edge-2 terminal; drone-2 lands. Say: "Now I take drone-2 out of the search. The ground marks it lost and hands its unflown lanes to drone-1. Watch the map redraw." The `drone_lost` and `retask` banners appear.
- 2:15 Say: "The same code ran four fake drones against one ground station. I killed one, and its lanes moved to the other three 0.7 seconds later. Every drone also carries its own safety envelope: no takeoff below 30 percent battery, land below 15, land by itself after 3 seconds without hearing the ground, and stay inside its geofence." Do: press **LAND ALL** (or Shift+L; Ctrl/Cmd+Shift+L if the cursor is in a text field). Both drones are down by 2:35.

### 2:35–3:20 · Who needs it?

Screen: deck slide 4.

Say: "Who needs it? The user I am building for is the Salt Lake County Sheriff's Search and Rescue Team, about 40 volunteers. That is a target customer; I have not contacted them yet. Summit County runs a mission about every three days. Washington County made 117 rescues in 2024. The honest limit: dense canopy defeats RGB and most thermal imaging, so this is for open terrain: desert, canyon, meadow, snow. And faces are not resolvable at search altitude, so the system matches on appearance only and saves no bystander images by default."

Label and source on the slide: target customer (ASSUMPTION: no contact yet), https://saltlakecounty.gov/sheriff/law-enforcement/search--rescue-team

### 3:20–4:00 · What does $10,000 unlock?

Screen: deck slide 5.

Say: "What does $10,000 unlock? Five thousand dollars stands alone: an open-source quad and a Part 107 pilot for the first real flight that checks our measured threshold, 32 pixels, at the planned altitude. Ten thousand adds a SIYI A8 mini class gimbal camera, a Jetson Orin Nano Super so the edge runs on board, and a second airframe. At 32 pixels the planner flies that quad at 96.8 metres and sweeps a square mile once in about 24 minutes, against about 58 hours for one ground team. I'll keep building for the next 90 days and publish every result. Thank you."

Figures: 0.398 h and 57.555 h per square mile (`docs/altitude-benchmark.md`); drone speed, endurance and walking speed are ASSUMPTION. Budgets $4,765 and $9,514 (`docs/roadmap-budget.md`).

- 4:00 End of the core. Hand over to the extended blocks or to questions.

## Extended (8 to 25 minute slots)

Run the core, then the blocks below in order until the talk time for the slot runs out, and keep the rest for questions. Questions are part of the slot.

| Slot | Talk | Blocks after the core | Q&A |
|---|---|---|---|
| 8 min | 4:00 | none | 4:00 |
| 10 min | 6:00 | E1 | 4:00 |
| 12 min | 8:00 | E1, E2 | 4:00 |
| 16 min | 11:00 | E1 to E4 | 5:00 |
| 25 min | 13:00 | E1 to E6 | 12:00 |

### E1 · Four drones and loss recovery (2:00)

Show that the coordinator scales past two drones. After LAND ALL, run this on the Mac in a fresh terminal, on different ports so the live ground station can stay up:

```
venv/bin/python scripts/demo_fake_fleet.py --n 4 --drive --dashboard-port 5556 --edge-port 9001
```

Open http://127.0.0.1:5556. Each fake edge is a real `python -m flightrisk.edge_device --source fake` process on 127.0.0.1. Points to make (`docs/fleet-scaling.md`): four zones at alternating altitude slots; clock sync, acks and a common scheduled start; loss detected 0.7 s after a kill, and the lost drone's lanes split three ways; survivors keep their lane progress. A drone that stays connected but goes silent is caught by the 5 s silence rule. Honest finding: with the default 500 cm geofence, two survivors refused far re-tasks; the launcher widens the fence for fake drones only, and a refused re-task now comes back as a `nack` instead of failing silently.

### E2 · The benchmark in depth (2:00)

Show the before and after table in `docs/altitude-benchmark.md`. Points to make:

- Test split: HERIDAL, 101 images, 337 people, IoU 0.3 matching, binned by person height in pixels.
- 24 px rests on 7 people (4 found); its 95% interval is 0.25 to 0.84. The [32,48) bin has 108 people at recall 0.611, interval 0.52 to 0.70, which is why 32 px is the solid value.
- Fine-tune: yolo11s on 400 HERIDAL train images (1,054 people), 20 epochs, 51 minutes on an M1 Max. Confidence 0.5 was picked on 50 validation images; the test split was scored once.
- The best stock sweep row (yolo11m, tiled, confidence 0.1, recall 0.442) was chosen by looking at test results, so it is an optimistic bound.
- Precision 0.832 means 41 false positives over 101 frames. Every test image has a person in it, so the test split does not measure false alarms over empty terrain; on the 50 validation images, 22 of them empty, the same threshold gave 23 false positives.

### E3 · From threshold to flight plan (1:30)

Run `venv/bin/python -m flightrisk.drone.coverage --min-px 32` and show the table. At 32 px the open quad flies at 96.8 m with 163.2 m lanes: 0.398 h per square mile with one drone, 0.199 h with two, 0.133 h with three. At 24 px it reaches the planner's 120 m ceiling. The Tello rows say INFEASIBLE: its published limits are 30 m height and 100 m range, and a square mile from a central launch point needs about 1,138 m of range. Dividing by N is an idealisation that ignores takeoff, transit, battery swaps and lost drones (`docs/fleet-scaling.md`).

### E4 · Safety envelope (1:30)

Each edge enforces its own limits, whatever the ground station says: a 300 cm geofence around its takeoff point by default, as set by the demo launchers (the code default is 500 cm; override with `FLIGHTRISK_GEOFENCE_CM`), no takeoff below 30% battery, landing below 15%, and landing on its own after 3.0 s without a ground message (`FLIGHTRISK_LINK_LOSS_LAND_S`). There are three ways to land: the LAND ALL button or Shift+L on the dashboard (Ctrl/Cmd+Shift+L while typing in a text field), `l` and Enter in an edge terminal, and Ctrl-C on an edge, which lands and then exits. The dashboard binds 127.0.0.1 by default, and the edge link needs the shared `FLIGHTRISK_EDGE_TOKEN`.

Confirming a match with a fleet lands every drone in place (LAND ALL); the Tello has no return-to-home, so drones land wherever they are.

### E5 · Privacy (1:00)

Faces are not resolvable at search altitude, so the demo runs appearance-only (`FLIGHTRISK_APPEARANCE_ONLY=1`) on the ground station and on both edges. The face recognizer is never built and no face embeddings are sent. The edges do send person crops (JPEG, one per detection) and ReID appearance embeddings to the ground station over the token-authenticated WebSocket link, so say that plainly if asked. Match snapshots are not written unless `FLIGHTRISK_SAVE_CAPTURES` is turned on, and `--no-llm` starts no reasoning model. Tests use only synthetic images. Beyond that edge-to-ground link, the one place images can leave a device is the mobile app's cloud model option, and only after the user picks it and enters a key (`docs/privacy-design.md`).

### E6 · Licensing and the path to reality (1:00)

FlightRisk is GPL-3.0-or-later. Ultralytics YOLO11 is AGPL-3.0; GPLv3 section 13 permits combining the two, and the AGPL terms still apply to that component. The swap item is an Apache-2.0 detector behind `PersonDetector` or the Ultralytics enterprise license (`docs/licensing.md`). HERIDAL is CC-BY-3.0 and neither the data nor the fine-tuned weights are committed. Then walk the $5,000 and $10,000 tables and milestones M1 to M4 in `docs/roadmap-budget.md`.

## Fallback ladder

Drop one rung per failure. Spend no more than 30 seconds fixing anything on stage; say which rung is running and why.

1. **Two Tellos live** (station or AP path). The core as written.
2. **One Tello live and one fake edge.** Use this when one drone, its laptop or its link fails. On the Mac, in a terminal where `FLIGHTRISK_EDGE_TOKEN` and the fleet settings are exported, first stop the real edge for that drone (Ctrl-C in its terminal, or `l` and Enter to land it, then Ctrl-C), because the ground station refuses a second live edge that claims the same drone id, so the fake edge would be turned away while the real one is still connected. Then start a fake edge in place of the missing drone:

   ```
   export FLIGHTRISK_GROUND_LOCAL=1; source scripts/fleet_env.sh
   venv/bin/python -m flightrisk.edge_device --source fake --drone-id drone-2 --alt-slot 160 --ws-url "$FLIGHTRISK_EDGE_WS"
   ```

   Replace `drone-2` with the id the fleet panel showed for the missing drone. The fake drone flies in process with no hardware, so planning, sync, the scheduled start, the merged map and the re-task all still show. Tell the judges that drone-2 is now a software drone. The `export FLIGHTRISK_GROUND_LOCAL=1` line makes the fake edge use loopback on the AP path, since the ground station is on this Mac; station mode ignores it.
3. **Recorded video of the fleet flight.** Play `demo/fleet.mp4` (2:00 at most, recorded in rehearsal from the shot list below, kept on local disk and never committed) and give the core talk over it.
4. **The N-drone fake-edge demo.** No drones and no network needed. Stop the live ground station and edge-1 with Ctrl-C first, because this launcher starts its own ground station on 127.0.0.1 ports 5555 and 9000:

   ```
   venv/bin/python scripts/demo_fake_fleet.py --n 4 --drive
   ```

   Open http://127.0.0.1:5555 to show four zones, the scheduled start and the re-task after one edge is killed. This rung replaces the SITL rung of the original plan: SITL never completed a flight on this machine, because the only image that runs here never reports position (`docs/sitl-demo.md`).

If the Mac itself fails, present `docs/pitch.md` from any machine; the plots are in `docs/img/`.

## Battery and rehearsal plan

- Ryze publishes a 13 min maximum flight time for the Tello (https://www.ryzerobotics.com/tello/specs). Plan 6-minute flights, so one charged battery covers one rehearsal plus one demo attempt. The core keeps the drones in the air for about 1.5 minutes, from the start at 1:05 to LAND ALL by 2:35.
- Spare batteries: at least 2 per drone (ASSUMPTION), so three per drone with the installed one. Charge every battery the night before (ASSUMPTION: a charger and an outlet are available).
- Battery A in each drone: rehearsal 1 and the recording. Battery B: rehearsal 2. Battery C, untouched until then: the live demo. Battery A or B, recharged, is the spare for a retry. Never launch a battery under 30%; the edge refuses anyway.
- Two rehearsals of the full demo, each timed against 4:00:
  - Rehearsal 1, on the network path the hardware check chose: run the core, record `demo/fleet.mp4` from the shot list, and check on the real drones what the automated tests only cover with fakes: LAND ALL, the local `l` key, and the link-loss landing (stop the ground station while both hover low and confirm both land within about 3 s). Also drop edge-2's Wi-Fi (turn off its laptop or router link, not the ground station) while people are in its camera view and confirm drone-2 lands within about 3 s. Stopping the ground station closes the connection cleanly and cannot reveal a stalled uplink.
  - Rehearsal 2: run the core again with a drill midway, dropping to rung 2 (stop edge-2, start the fake edge) and then running rung 4 once end to end.

## Backup video shot list

`demo/fleet.mp4` must be at most two minutes (120 s). Record it in rehearsal 1: screen capture of the Mac plus a phone video of the floor.

| # | Seconds | Shot | On screen |
|---|---|---|---|
| 1 | 10 | Title and thesis plot | Deck slide 2: the before and after recall plots, "a person a few dozen pixels tall" |
| 2 | 15 | Benchmark before and after | Recall 0.000 to 0.142 to 0.602 at precision 0.832, 32 px solid |
| 3 | 10 | Plan | Dashboard map after **Plan search**: two zones at 100 cm and 160 cm |
| 4 | 20 | Fleet takeoff with skew readout | Phone shot of both drones lifting off, cut with the fleet panel's offset, RTT and skew columns |
| 5 | 20 | Merged map | Lanes progressing, detections from both drones on one map, each tagged with its drone id |
| 6 | 20 | Re-task on loss | `l` and Enter on edge-2, drone-2 lands, `drone_lost` and `retask` banners, drone-1 picks up the lanes |
| 7 | 10 | Four fake drones | `demo_fake_fleet.py --n 4 --drive` output: re-task 0.7 s after the kill |
| 8 | 10 | $5,000 and $10,000 slide | Deck slide 5 with the target customer named |

Total: 115 s.

## Q&A prep

Q: What about forests? Most people get lost under trees.

A: Dense canopy defeats RGB and most thermal imaging, so we do not claim forest. The fit is open terrain: desert, canyon, meadow and snow. Ground teams and dogs stay the tool under canopy.

Q: 60% recall means you miss 4 in 10 people. Is that good enough?

A: It is measured, not hoped: 0.602 recall at 0.832 precision on 337 people in the HERIDAL test split, and recall plateaus near 0.6 at every height. So the coverage hours are the time to sweep an area once, not the time to find everyone. One pass at 0.398 h per square mile (the planner's open quad at 32 px; its speed is ASSUMPTION) leaves time for repeat passes long before one ground team finishes a single sweep at 57.555 h. Whether repeat passes catch the people a first pass misses is not measured yet. We fine-tuned on 400 of the 891 HERIDAL train images that contain a person; training on more is the cheapest next step (ASSUMPTION: that more data raises recall).

Q: Do you do face recognition? What about bystanders' privacy?

A: No face matching in the search demo. Faces are not resolvable at search altitude, so the system is appearance-only by default (`FLIGHTRISK_APPEARANCE_ONLY=1`), on the edge too, and never loads the face model or sends face embeddings. The edges do send person crops and ReID appearance embeddings to the ground station over the token-authenticated link. Match snapshots are off by default, so no bystander imagery is written to disk. The InsightFace face packs are licensed for non-commercial research only, which is another reason not to rely on them. Details are in `docs/privacy-design.md`.

Q: What does the FAA require?

A: The hackathon needs no approval; the Tellos fly indoors. Real flights need a Part 107 remote pilot, which the $5,000 milestone pays for. Operations outside the standard rules need a waiver (https://www.faa.gov/uas/commercial_operators/part_107_waivers), active emergencies have a special governmental interest process (https://www.faa.gov/uas/advanced_operations/emergency_situations), and Part 108 for beyond visual line of sight is still a proposed rule. The planner caps altitude at 120 m. We will verify the current status of all three before any flight (ASSUMPTION).

Q: What is the license, and can this become a company?

A: FlightRisk is GPL-3.0-or-later. Ultralytics YOLO11 is AGPL-3.0; GPLv3 section 13 permits combining them, and the AGPL terms still apply to that component. For a product, we swap in an Apache-2.0 detector behind `PersonDetector` or buy the Ultralytics enterprise license. HERIDAL is CC-BY-3.0, and neither the data nor the fine-tuned weights are in the repository. A company would sell hardware integration and support around free software for county teams (ASSUMPTION; there is no business plan yet).

Q: How many false alarms?

A: 41 false positives over 101 test frames, about 0.4 per 12-megapixel frame. Every test image contains a person, so the test split does not measure false alarms over empty terrain. On 50 validation images, 22 of them empty, the same threshold gave 23 false positives (0.46 per frame). Every pin goes to a human on the dashboard; nothing is dispatched automatically.

Q: Does a Tello prove anything about a real search?

A: It shows the fleet software on real drones: zones, clock sync, a scheduled start, a merged map, re-tasking and landing. It does not prove search coverage. A Tello cannot search a square mile (INFEASIBLE: 30 m height and 100 m range), and the detection threshold comes from HERIDAL, not from our own airframe. The first real flight in the $5,000 milestone is what tests it.

Q: Will you keep going after the hackathon?

A: Yes. It is a solo team today (ASSUMPTION: no teammates recruited). The 90-day plan is to buy and fly in days 1 to 30, run the edge on a Jetson in days 31 to 60, and ask the SAR team for a feedback session, with no promise of a partnership, in days 61 to 90, publishing every benchmark result in the repository (`docs/one-page-plan.md`).

Q: Could this be used to track people who do not want to be found?

A: Any person detector can. We limit what we build: no face recognition at altitude, no saved bystander imagery by default, and GPL code anyone can audit.
