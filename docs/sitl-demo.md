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
