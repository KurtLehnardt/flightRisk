# SITL demo: one drone flying the coverage plan at altitude

This page covers the optional SITL track. One ArduPilot SITL copter flies the coverage planner's GPS lawnmower at the planned altitude and speed, points a gimbal over MAVSDK, replays HERIDAL aerial frames as its camera and geolocates detections to latitude and longitude. The same scripts can point at a real quad (see "Real hardware").

Requirements covered: R-050 (this page and `scripts/start_sitl.sh` usage) and R-080 (`scripts/sitl_fly_coverage.py`).

## Status: what was and was not verified

Verified on the build machine (Apple Silicon Mac, Docker 29.5.2, MAVSDK-Python 3.17.2):

- The pinned amd64 image starts under emulation, `start_sitl.sh --instance 0` publishes TCP 5760, and MAVSDK connects to it.
- MAVSDK can read and set parameters, and the vehicle reaches "EKF2 IMU0 is using GPS". With the telemetry stream parameters set to 10 Hz, an arm and takeoff command were accepted.
- The script plans first (altitude 120 m, 12 m/s, 6 waypoints for a 300x300 m area with `OPEN_QUAD_4K` and `--min-px 24`), connects, sets the takeoff altitude and cruise speed, arms and sends takeoff.

NOT achieved: a complete flight with printed waypoint positions. The only ready-made image tried was `radarku/ardupilot-sitl` (the one design section 12 names); it is old (built January 2021, ArduCopter 4.0 era). Against it MAVSDK 3.17 receives no position, GPS info or health updates even after the stream rates are raised, so the script cannot observe the climb. The run ended with `TimeoutError: Did not reach 120 m within 90 s`. No flight result is recorded here because none was observed. Fixing this needs a newer ArduPilot SITL build (compiling ArduPilot under amd64 emulation was judged beyond the time box for this task) or a native `sim_vehicle.py`. The unit tests in `tests/test_sitl_scripts.py` cover the script logic against a fake MAVSDK; the `sitl`-marked test in `tests/integration/test_sitl.py` has not been run against a live SITL.

## Start SITL

```
./scripts/start_sitl.sh                 # instance 0, Docker if available
./scripts/start_sitl.sh --instance 1    # second vehicle on the next port
./scripts/start_sitl.sh --origin 40.6894,-111.7310,1600
./scripts/start_sitl.sh --native        # use a local sim_vehicle.py instead
```

- Instance N listens on TCP `5760 + 10 * N`. The image runs without MAVProxy, so connect with `tcpout://127.0.0.1:<port>`.
- The default origin is the approximate Millcreek Canyon demo point from the design (`UTAH_DEMO_ORIGIN`, 40.6894, -111.7310). Home altitude 1600 m AMSL is an ASSUMPTION.
- The container is named `flightrisk-sitl-N`; stop it with `docker stop flightrisk-sitl-N`.

### Docker image and amd64 emulation

The image `radarku/ardupilot-sitl` is amd64 only, so on an arm64 host it runs under emulation (`--platform linux/amd64`). The script pins it by digest (`sha256:2364ae14...`) because its `:latest` tag has not changed since January 2021. Emulation is slow: expect minutes for the EKF to settle, and plan on one to three instances at most. The image architecture was verified by pulling it; `docker image inspect` reports `amd64`.

## Fly the coverage plan

```
python scripts/sitl_fly_coverage.py --min-px 24 --preset OPEN_QUAD_4K \
    --origin 40.6894,-111.7310 --area 300x300
python scripts/sitl_fly_coverage.py --dry-run          # print the plan only
python scripts/sitl_fly_coverage.py --replay-dir path/to/heridal --detect
```

What it does, in order:

1. Plans with `plan_coverage` (altitude and speed) and `lawnmower_gps` (lanes). Nothing connects before this succeeds.
2. Connects over MAVSDK, sets the takeoff altitude and the cruise speed, arms and takes off.
3. Commands the gimbal to the preset pitch (-90, nadir). The pitch follows the MAVSDK convention: negative looks down. A gimbal failure is logged and the flight continues.
4. Sends one `goto_location` per lane waypoint and prints the position reached, the error against the target and the altitude above the start point. Altitudes sent to the autopilot are AMSL (start altitude plus planned height).
5. At each waypoint, grabs a replay frame (`--replay-dir`), optionally runs the person detector and geolocates each box (bottom centre) with `pixel_to_ground`.
6. Lands.

### Gimbal in SITL

The image's firmware exposes the older `MNT_TYPE` parameter (value 0 by default, so no mount) and does not have `MNT1_TYPE`. The design's assumption that `MNT1_TYPE` exists in SITL is therefore wrong for this image. No gimbal is simulated by default, so gimbal commands are best effort and are expected to be ignored or to fail in SITL. Newer ArduPilot uses `MNT1_TYPE` (see https://ardupilot.org/dev/docs/mavlink-gimbal-mount.html); a newer SITL could simulate a MAVLink gimbal, which was not tested.

### SITL quirks the script works around

- The image streams no telemetry until its `SR0_*` rates are raised, so the script sets them to 10 Hz after connecting.
- The stock `MavlinkController.connect()` waits for health flags that this firmware never reports. The script uses a subclass that waits for a GPS fix instead (and gives up waiting after two minutes with a warning). Pass `--strict-health` to use the stock check, as on real hardware.

## Real hardware

The same scripts can fly an ArduPilot or PX4 quad with a gimbal camera. Nothing here was flown; every item is an ASSUMPTION unless a URL is given. ASSUMPTION: the project owner owns neither a flight controller nor a gimbal today.

What changes:

- Connection: pass `--address` instead of `--instance`, for example `serial:///dev/ttyUSB0:57600` for a telemetry radio or `udp://:14540` for a companion computer (ASSUMPTION: MAVSDK connection URL forms; check the MAVSDK connection guide). Add `--strict-health` so the stock arm-ready check (GPS, calibration) applies instead of the SITL fix wait.
- Gimbal: configure the mount on the flight controller. On ArduPilot set `MNT1_TYPE` for the gimbal protocol and confirm the pitch limits; see https://ardupilot.org/dev/docs/mavlink-gimbal-mount.html. The SIYI A8 mini used for the `OPEN_QUAD_4K` preset is described at https://www.readymaderc.com/products/details/87067-siyi-a8-mini-4k-8mp-ultra-hd-6x-digital-zoom-gimbal-camera. Check the sign: the script sends MAVSDK's convention (negative looks down).
- Camera video: replace `--replay-dir` with the camera's RTSP URL in the controller's `rtsp_url` (ASSUMPTION: the A8 mini serves RTSP on its own address).
- Altitude reference: `goto_location` takes AMSL altitude, so the script adds the start-point altitude. Real GPS altitude is noisy; check the climb on the ground first (ASSUMPTION).
- Speed and endurance: the preset's 12 m/s and 25 minutes are ASSUMPTIONs (see `flightrisk/drone/coverage.py`); set values that match the airframe.
- Safety: test in SITL first, fly with a pilot holding the transmitter and able to take over, configure RTL, a geofence and a low battery failsafe on the flight controller, keep line of sight, and stay under the 120 m ceiling the planner already caps at. Local aviation rules apply (ASSUMPTION: not reviewed here).
- Never run the script against a real vehicle for the first time with `--area` larger than the test field; use `--max-lanes 1` first.

## Fleet edge in SITL (`--drone mavlink`)

The MAVLink fleet edge (`docs/open-drone-support.md`) was run against ArduCopter SITL on 2026-10-29. This is simulation only: no flight controller, radio or aircraft was involved.

### What ran, and what it proved

Machine: Apple Silicon Mac, macOS, ArduCopter 4.6.3 SITL built natively (no Docker, no emulation), MAVSDK-Python 3.17.2.

| Run | Result |
|---|---|
| `tests/integration/test_sitl_fleet.py::test_one_sitl_drone_flies_a_field_plan`: a `field_scale` plan (24 m by 20 m, slot 10 m) from the real `FleetCoordinator`, clock sync and `start_at` to the real `LaneExecutor` and `MavlinkFleetDrone` | Passed. Preflight (GPS, home, arming checks, failsafe parameters) became ready, takeoff, climb to the slot, forward 20 m, right one 10.4 m strip, back 20 m in 40 s; ended 10.42 m right and 0.02 m ahead of the takeoff point at 10.68 m; LAND ALL landed it. |
| `test_two_sitl_drones_fly_a_field_plan`: two SITL instances, two zones, slots 10 m and 15 m | Passed. Both flew their lanes in 42 s and ended within 0.2 m of the planned end points, at 10.59 m and 15.65 m. |
| `test_ground_link_loss_returns_to_launch_in_sitl`: take off, then the ground's heartbeats stop | Passed. 3 s later the edge asked for a return to launch; the copter returned and landed 0.02 m from its takeoff point. |
| The real edge process, `python -m flightrisk.edge_device --drone mavlink --source file ...` with the YOLO detector, and `scripts/field_fleet_ground.py` as the ground over the WebSocket link | Passed. The ground planned, synced and started the edge; all lanes flown in 41 s (telemetry: x 10.42 m, y 0.11 m, 10.70 m high, with GPS position); LAND ALL landed it; Ctrl+C stopped the edge after 1999 frames. |

What SITL found that the simulated-MAVSDK tests could not, all fixed in this change:

- Two MAVSDK links in one process shared MAVSDK's default gRPC port (50051), so one drone's commands reached the other drone. Each link now starts its own `mavsdk_server` on a free port.
- MAVSDK's parameter client mixes up replies when two reads overlap; parameter calls are now serial.
- `telemetry.home()` sends nothing on ArduPilot (it sends `HOME_POSITION` only on request) although the home-position health flag is set; the preflight now relies on the flag.
- ArduCopter refuses a takeoff for about 20 s after its arming checks pass (GUIDED needs the EKF on GPS); the takeoff is now retried until its deadline.
- ArduCopter refuses a speed change on the ground; it is retried once airborne, otherwise the autopilot's cruise speed applies.
- An in-process ground must send heartbeats, or the edge's 3 s link-loss watchdog returns the drone (it did, correctly).

Not proved in SITL: PX4 (no PX4 SITL was run), an RC pilot taking over, the autopilot's own failsafes triggering, a geofence breach, wind or GPS noise, long flights, and anything about real hardware or radios. Those paths are covered only by the simulated-MAVSDK tests.

### Build and run ArduCopter SITL natively (macOS or Linux)

```bash
git clone --branch Copter-4.6.3 --depth 1 --recurse-submodules --shallow-submodules \
    https://github.com/ArduPilot/ardupilot.git
cd ardupilot
python3 -m venv ../sitl-venv && ../sitl-venv/bin/pip install empy==3.3.4 pexpect future pymavlink "setuptools<81"
PATH="$PWD/../sitl-venv/bin:$PATH" ./waf configure --board sitl && PATH="$PWD/../sitl-venv/bin:$PATH" ./waf copter -j4
```

On a Mac with a recent Xcode linker the build stops with `ld: pointer not aligned in '__ZN12AP_FWVersion5fwverE'`; making `PACKED` conditional on non-Apple builds in `libraries/AP_Common/AP_FWVersion.h` fixes it for SITL (a local patch, not upstream). The build takes about two minutes on four cores.

Start instance N (MAVLink on TCP `5760 + 10 * N`). Each instance needs `SYSID_THISMAV 1`, or MAVSDK commands the wrong system id and GUIDED never engages. The stream-rate parameters make the telemetry arrive:

```bash
printf 'SR0_EXTRA1 4\nSR0_EXTRA2 4\nSR0_EXTRA3 2\nSR0_EXT_STAT 2\nSR0_POSITION 4\nSYSID_THISMAV 1\n' > sitl.parm
mkdir -p run0 && cd run0 && ../ardupilot/build/sitl/bin/arducopter --model + --speedup 1 \
    --defaults ../ardupilot/Tools/autotest/default_params/copter.parm,../sitl.parm \
    --home 40.7608,-111.8910,1288,0 -I0
```

SITL accepts one client on the port and exits when it disconnects, so start a fresh instance for every run. The health flags turn true about 20 s after boot and a takeoff is accepted after about 45 s.

### Run the tests and the CLI path

```bash
FLIGHTRISK_SITL_ADDRESS=tcpout://127.0.0.1:5760 \
    python -m pytest tests/integration/test_sitl_fleet.py -m sitl -s -p no:cacheprovider
# two instances (-I0 and -I1):
FLIGHTRISK_SITL_ADDRESS=tcpout://127.0.0.1:5760 FLIGHTRISK_SITL_ADDRESS_2=tcpout://127.0.0.1:5770 \
    python -m pytest tests/integration/test_sitl_fleet.py -m sitl -s -p no:cacheprovider -k two
```

The tests set the failsafe parameters the edge requires on the simulated autopilot. For the CLI path, put them in the SITL defaults instead (`FENCE_ENABLE 1`, `FENCE_ACTION 1`, `BATT_FS_LOW_ACT 2`, `BATT_LOW_VOLT 10.5`, `FS_THR_ENABLE 1`, `RTL_ALT 0`), then:

```bash
export FLIGHTRISK_EDGE_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))")
python scripts/field_fleet_ground.py --drones sitl-1 --area-m 24x20 --slots-m 10 --port 9100 &
python -m flightrisk.edge_device --drone mavlink --mavlink-address tcpout://127.0.0.1:5760 \
    --source file --video clip.mp4 --drone-id sitl-1 --alt-slot-m 10 --geofence-m 29 \
    --frame-heading-deg 0 --ws-url ws://127.0.0.1:9100
```

The sitl tests are excluded from the normal suite by the `sitl` marker and skip without `FLIGHTRISK_SITL_ADDRESS`.
