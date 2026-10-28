# Fleet scaling: N fake drones against one ground station

This page shows that the fleet code scales past two drones and records how it behaves when one drone drops out. It uses fake edges, not SITL. The only SITL image that runs on the build machine never reports position (see `docs/sitl-demo.md`), so an N-SITL farm is not viable here. Each fake edge is a real `python -m flightrisk.edge_device --source fake` process with its own drone id, altitude slot, WebSocket connection and lane executor over an in-process fake Tello. The ground station is the real dashboard. Everything runs on 127.0.0.1 and no real drone is contacted.

Requirement covered: R-082.

## Run it

```
python scripts/demo_fake_fleet.py --n 4                    # start the ground station and 4 edges, Ctrl-C stops all
python scripts/demo_fake_fleet.py --n 4 --drive            # also plan, start, kill one edge and print the re-task
python scripts/demo_fake_fleet.py --n 4 --attach           # edges only, for a ground station already running
python scripts/demo_fake_fleet.py --n 8 --dry-run          # print the command lines and exit
```

Open `http://127.0.0.1:5555`, press Plan to see N zones, press Start to fly them, then kill one edge process to watch its lanes move to the others. `--drive` does the same through a socket.io client.

What the launcher does:

- Prints every command line before it runs anything, so each process can be started by hand. The token is shown as `<token>`; the value is never printed.
- Generates a random `FLIGHTRISK_EDGE_TOKEN` per run and passes it to the ground station and every edge. With `--attach` it uses the token already in your environment, if any.
- Binds the edge WebSocket to 127.0.0.1 and points every edge at `ws://127.0.0.1:<port>`. The dashboard web port also binds 127.0.0.1 by default.
- Gives drones the coordinator's altitude slots in turn (100 cm, 130 cm, 100 cm, ...), and sizes the area so each zone is three 75 cm lanes wide (`325 * N - 100` cm wide, 400 cm deep). Override with `--area-cm`.
- Starts each child in its own process group. On exit, Ctrl-C or SIGTERM it sends SIGTERM to every group, waits, then SIGKILLs what remains, and prints `All child processes stopped.` Tests cover a grandchild process and a child that ignores SIGTERM.
- Widens the edges' geofence (`FLIGHTRISK_GEOFENCE_CM`) to the area width plus 100 cm unless you set it. See "Finding: geofence refuses far re-tasks" below.

## Observed run, N = 4

Command (area and flags are the launcher defaults for N = 4; the ground station and edges are also started by it):

```
python scripts/demo_fake_fleet.py --n 4 --drive --kill-after-s 6 --watch-s 40
```

Output, 2026-10-27, Apple M1 Max, edges and ground on 127.0.0.1:

```
Loopback only (127.0.0.1); token random per run.
[ground] FLIGHTRISK_EDGE_TOKEN=<token> .../python -m flightrisk.dashboard --source edge --edge-host 127.0.0.1 --edge-port 9000 --port 5555 --fleet drone-1,drone-2,drone-3,drone-4 --fleet-area-cm 1200x400 --fleet-strip-cm 75 --no-llm
Edge geofence widened to 1300 cm (FLIGHTRISK_GEOFENCE_CM) so re-tasked transits are not refused; fake drones only.
Ground station is listening on 127.0.0.1:9000; dashboard at http://127.0.0.1:5555
Started 4 fake edges. Ctrl-C stops everything.
[drive] waiting for 4 edges to report telemetry
[drive] emit plan_fleet
[drive] fleet_plan: 4 zones: drone-1@x=0cm/5 lanes/alt 100cm, drone-2@x=325cm/5 lanes/alt 130cm, drone-3@x=650cm/5 lanes/alt 100cm, drone-4@x=975cm/5 lanes/alt 130cm
[drive] emit start_fleet
[drive] fleet_event {'event': 'start', 'detail': 'start_at in 2 s'}
[drive] killing drone-4 (SIGKILL on its process group)
[drive] +  0.7s after the kill: fleet_event {'event': 'drone_lost', 'drone_id': 'drone-4'}
[drive] +  0.7s after the kill: fleet_event {'event': 'retask', 'drone_id': 'drone-4', 'detail': '4 lanes to drone-3, 4 lanes to drone-2, 4 lanes to drone-1', 'to': {'drone-3': 4, 'drone-2': 4, 'drone-1': 4}}
[drive] re-task observed: True
All child processes stopped.
```

The edges' own logs confirm the survivors accepted the longer lane list with their progress kept (`edge_lanes_assigned`, `lanes_total` 5 before and 9 after, `lane_index` 1, `progress_carried: true`) for drone-1, drone-2 and drone-3. `pgrep -f flightrisk` and `lsof -i :5555 -i :9000` showed nothing afterwards.

What this shows and what it does not:

- It shows four zones side by side, one per drone, at alternating altitude slots; clock sync, lane assignment and acks, a common scheduled start, loss detection 0.7 s after the kill, and the lost drone's lanes split three ways.
- The kill is SIGKILL on a fake edge's process group, so the ground sees a closed socket. A drone that stays connected but goes silent is detected by the 5 s silence rule instead; that path is covered by `tests/test_fleet_dashboard.py`, not by this run.
- The drones are fakes. Fake flight time is distance over 100 cm/s, so this says nothing about real flight time, radio range or Tello video. It does not measure dashboard load with real video.

### Finding: geofence refuses far re-tasks

The first N = 4 runs used the edges' default geofence of 500 cm around takeoff. After drone-4 was killed, drone-3 (the neighbour) accepted its extra lanes, but drone-1 and drone-2 refused them: `edge_assign_lanes_refused ... reason: geofence, geofence_cm: 500.0, x_cm: 638.0` (drone-1) and `x_cm: 725.0, y_cm: 400.0` (drone-2). The `retask` event had already reported 4 lanes sent to each of the three survivors, and the edge logs show only one of them took its lanes, so on a wide area part of the lost zone would have been left unsearched. The refusal itself is the safety envelope working. This was fixed in R-106: an edge now nacks lanes it refuses, and the ground station reports lanes that no survivor accepted as uncovered instead of counting them as sent. The launcher still widens the fence for the demo so a wide area is actually flown.

## Time to cover vs N

From `python -m flightrisk.drone.coverage --min-px 24` and `--min-px 32`, which print hours to search one square mile for each platform preset, for 1, 2, 3 and 5 drones.

`--min-px 24` is the best fine-tuned result, but only 7 persons fall in that bin (optimistic):

| preset | n | alt_m | lane_m | hours |
|---|---|---|---|---|
| Ground team | 1 | 0.0 | 30.0 | 57.555 |
| Ground team | 2 | 0.0 | 30.0 | 28.778 |
| Ground team | 3 | 0.0 | 30.0 | 19.185 |
| Ground team | 5 | 0.0 | 30.0 | 11.511 |
| Tello | 1 | 30.0 | 42.2 | INFEASIBLE (area needs 1138 m of range, platform limit is 100 m) |
| Tello | 2 | 30.0 | 42.2 | INFEASIBLE (area needs 1138 m of range, platform limit is 100 m) |
| Tello | 3 | 30.0 | 42.2 | INFEASIBLE (area needs 1138 m of range, platform limit is 100 m) |
| Tello | 5 | 30.0 | 42.2 | INFEASIBLE (area needs 1138 m of range, platform limit is 100 m) |
| Open quad 4K | 1 | 120.0 | 202.3 | 0.318 |
| Open quad 4K | 2 | 120.0 | 202.3 | 0.159 |
| Open quad 4K | 3 | 120.0 | 202.3 | 0.106 |
| Open quad 4K | 5 | 120.0 | 202.3 | 0.064 |

`--min-px 32` is the solid value, with 108 persons in its bin (conservative):

| preset | n | alt_m | lane_m | hours |
|---|---|---|---|---|
| Ground team | 1 | 0.0 | 30.0 | 57.555 |
| Ground team | 2 | 0.0 | 30.0 | 28.778 |
| Ground team | 3 | 0.0 | 30.0 | 19.185 |
| Ground team | 5 | 0.0 | 30.0 | 11.511 |
| Tello | 1 | 30.0 | 42.2 | INFEASIBLE (area needs 1138 m of range, platform limit is 100 m) |
| Tello | 2 | 30.0 | 42.2 | INFEASIBLE (area needs 1138 m of range, platform limit is 100 m) |
| Tello | 3 | 30.0 | 42.2 | INFEASIBLE (area needs 1138 m of range, platform limit is 100 m) |
| Tello | 5 | 30.0 | 42.2 | INFEASIBLE (area needs 1138 m of range, platform limit is 100 m) |
| Open quad 4K | 1 | 96.8 | 163.2 | 0.398 |
| Open quad 4K | 2 | 96.8 | 163.2 | 0.199 |
| Open quad 4K | 3 | 96.8 | 163.2 | 0.133 |
| Open quad 4K | 5 | 96.8 | 163.2 | 0.08 |

Hours are for one square mile. Tello rows are marked INFEASIBLE instead of given a time: the Tello's published maximum flight height is 30 m and its published maximum flight distance is 100 m (https://www.ryzerobotics.com/tello/specs, checked 2026-10-27), and covering a square mile from a central launch point needs about 1,138 m of range. The Tello's altitude is capped at 30 m, not the altitude the detector threshold alone would allow. Where the values come from: `docs/altitude-benchmark.md` (t22) measured a fine-tuned yolo11s at 0.5 recall from 24 px on 7 persons, and from 32 px on 108 persons with a 95% interval clear of 0.5. It still misses about 4 in 10 persons at every height, so these hours are the time to sweep the area once, not the time to find everyone.

Reading the table: the planner divides the single-drone time by N, so time to cover falls as 1/N. That is an idealisation. It ignores take-off and transit time, battery swaps across drones, the zone gap the coordinator leaves between neighbours, and the loss of a drone mid-flight, which pushes its lanes onto the survivors. Drone speed and endurance for the open quad are assumptions, as the planner output notes. A Tello cannot search a square mile at all; the INFEASIBLE rows say so rather than showing a time.

## Tests

`tests/test_fake_fleet.py`:

- `test_launcher_builds_n_edge_commands`: one ground command plus N edge commands with distinct ids and slots, loopback URLs and the matching `--fleet` list.
- `test_n_edges_plan_and_retask_on_loopback`: four edges over a real token-authenticated transport on 127.0.0.1, four zones planned, lanes acked, fleet started, one edge dropped, its lanes appended to the others and acked.
- Teardown tests for a grandchild process, a SIGTERM-ignoring child and a real Ctrl-C of the launcher.
