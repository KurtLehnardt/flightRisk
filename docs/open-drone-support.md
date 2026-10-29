# Open-source drones: support matrix, architecture and safety model

FlightRisk should fly the drones that search and rescue teams can build and repair themselves: open-source autopilots such as PX4 and ArduPilot on Pixhawk-class flight controllers, with a companion computer running the FlightRisk edge. This page is the design for that work, the status of each stack, and what phase 1 (this change) built.

Read the "Verification status" section before flying anything. Nothing on this page has flown a real aircraft.

## Support matrix

| Stack | Vehicle | Route into FlightRisk | Status |
|---|---|---|---|
| ArduPilot ArduCopter | Multirotor (Holybro S500 V2 class) | MAVLink through MAVSDK, `--drone mavlink` on the companion computer | Phase 1, built. Unit tests against a simulated MAVSDK, and ArduCopter 4.6.3 SITL: one and two drones flew field plans, link loss returned to launch. Never flown on hardware. |
| PX4 | Multirotor | Same (`--drone mavlink`) | Phase 1, built. Unit tests only, including PX4-specific mode handling; no PX4 SITL run. Never flown on hardware. |
| ArduPilot ArduPlane, PX4 fixed-wing, VTOL in plane mode | Fixed-wing | MAVLink, but a plane cannot hover, stop at the end of a move or take off in place | Not supported. Needs a mission-upload backend (lanes as a MAVLink mission with loiters and a landing sequence). A VTOL in multicopter mode should behave like a multirotor; untested. |
| ArduPilot Rover, PX4 rover, boats | Ground or water | MAVLink `goto_location` with the altitude ignored | Not supported in phase 1: the envelope, slots and ceiling assume flight. Possible later with a "surface" envelope profile. |
| Bitcraze Crazyflie | Nano quad, indoor | `cflib` (MotionCommander or the high-level commander with a Flow, Loco or Lighthouse deck) | Not implemented. Good fit for the `room` profile (centimetre moves, like the Tello). A backend wraps `cflib`; see "Add your drone in 5 steps" in `docs/drone-interface.md`. |
| iNav | Multirotor or plane with GPS | MSP (MultiWii Serial Protocol); iNav also emits some MAVLink telemetry | Not implemented. iNav has position hold, return to home and waypoint missions; a backend would use its MSP waypoint and "GCS navigation" commands. Its MAVLink support is telemetry-oriented, so MAVSDK is not the route. |
| Betaflight | Racing and freestyle quads | MSP | Out of scope for autonomous search: Betaflight has no autonomous navigation (newer releases add GPS position hold and GPS Rescue, but no waypoints or position targets), so it cannot fly lanes. At most it could be flown by hand through `rc_control`, which the fleet does not use. |
| Paparazzi UAV | Multirotor and fixed-wing | PPRZLINK (Ivy bus), not MAVLink | Not implemented. A backend would use the `pprzlink` Python bindings and Paparazzi's flight-plan blocks or guided mode. |
| ROS 2 drones | Any (usually PX4 or ArduPilot underneath) | MAVROS, or PX4's uXRCE-DDS bridge | Not implemented as a separate backend. When the autopilot is PX4 or ArduPilot, run `--drone mavlink` against the same MAVLink endpoint (a MAVLink router can share it with ROS). A native ROS 2 backend would wrap action clients through `rclpy`. |
| Parrot ANAFI (Olympe) | Multirotor | Olympe SDK (open source, BSD-3) | Not implemented. Olympe is open but the drone firmware is closed, so it is "open SDK, closed aircraft". A backend could use Olympe's `moveBy` and `moveTo`. |
| DJI Tello, RoboMaster TT | Indoor quad | `djitellopy` | Supported (the existing backend). Closed firmware with an open, documented SDK. |
| Other DJI (Mini, Mavic, Air, Matrice), Skydio, Autel | Multirotor | Vendor mobile or onboard SDKs | Out of scope: closed firmware, closed or licensed SDKs that run on a phone or a vendor computer, and no open autopilot to configure failsafes on. |

PX4 and ArduPilot through MAVLink cover most open-source drones in use: the Holybro, CUAV, Matek and ArduPilot-compatible boards, the Pixhawk standard, and many commercial airframes built on them.

## Architecture

### Which drone, and where frames come from

The edge used to decide both with `--source`. They are now separate, and every old command line keeps its meaning:

| Command line | Drone flown | Frames |
|---|---|---|
| `--source camera` (default), `--source file`, `--source url` | none | camera, video file, stream URL |
| `--source tello`, `--drone tello` | Tello | Tello video |
| `--source fake`, `--drone fake` | simulated Tello | blank frames |
| `--drone mavlink` (`--source camera` by default, or `file` or `url`) | PX4 or ArduPilot over MAVLink | the companion computer's camera, a file (looped) or a stream URL |

`--drone tello --source camera` and `--drone mavlink --source tello` are refused at startup with the reason, as are the MAVLink options without `--drone mavlink` and `--alt-slot` (centimetres, indoor) with it. `--drone mavlink` also needs `--frame-heading-deg` (the same value on every drone of the fleet) and refuses a `--mavlink-address` whose scheme is not `serial`, `udpin`, `udpout`, `tcpin` or `tcpout` or that starts with `-`.

### What the edge needs from a backend

The edge's `LaneExecutor` is unchanged in what it asks of a drone; a backend provides:

- `connect()`, `disconnect()`, `takeoff(abort=None)`, `land()`, `move(direction, cm)`, `rotate(degrees)`, `state.is_flying`, `read_battery()`, `read_height_cm()`;
- `link_status()` (optional): a `DroneLinkStatus`. The executor refuses a takeoff while it is not `ok` and puts its detail in telemetry. The autopilot backend uses it for the link (`not_connected`, `state_stale`), its preflight (`preflight`) and external control (`external_control`). Landing never looks at it;
- `land_for_reason(reason, retry=False)` (optional): called instead of `land()` with the executor's landing reason, so a backend can map a failsafe landing to its autopilot's return to launch; `retry` marks the monitor's repeat of a landing still under way;
- `measured_pose()` and `geo_position()` (optional): where the drone really is (plan frame, and GPS). Telemetry and detection messages report this pose; the geofence check of planned moves still uses the commanded pose;
- `check_slot(alt_cm)` (optional): a reason a plan's altitude slot is not allowed (the autopilot's return altitude), asked on the ground and in the air;
- `telemetry_fields()` (optional): extra telemetry fields (autopilot, flight mode, whether the cruise speed was taken, external control);
- `capabilities.flight_scale`: `room` or `field`. It declares the drone's scale; today the edge picks the envelope profile from `--drone` (`mavlink` is `field`), and nothing else reads this field yet;
- `fence_breach_margin_cm` (optional): GPS noise allowance before a measured position outside the fence lands the drone.

The fleet protocol is unchanged. Telemetry gains optional fields from drones that report them (`lat`, `lon`, `autopilot`, `flight_mode`, `speed_set`, `external_control`, and `camera_ok` on an edge with its own camera); every ground accepts extra telemetry fields.

### Units and scale

Everything on the wire stays in centimetres, so the protocol, plans and telemetry have one unit for every drone. What changes per drone is the allowed range, through an `EnvelopeLimits` profile in `flightrisk/drone/safety.py`:

| Limit | `room` (Tello, unchanged) | `field` (PX4, ArduPilot) |
|---|---|---|
| Single move | 20 to 500 cm | 20 to 5000 cm |
| Altitude slot | 20 to 500 cm, default 100 | 10 to 120 m, default 15 m |
| Ceiling | 150 to 1000 cm, default 200 | 10 to 120 m, default 30 m |
| Ceiling margin | at least 30 cm, default 60 | at least 3 m, default 5 m |
| Geofence half-width | default 500 cm | default 30 m, at most 500 m |

The outdoor values are set in metres with `--alt-slot-m`, `--ceiling-m`, `--ceiling-margin-m` and `--geofence-m` (or `FLIGHTRISK_FIELD_ALT_SLOT_M`, `FLIGHTRISK_FIELD_CEILING_M`, `FLIGHTRISK_FIELD_CEILING_MARGIN_M`, `FLIGHTRISK_FIELD_GEOFENCE_M`). The indoor `FLIGHTRISK_*_CM` variables are never read for an outdoor drone, so a room setting cannot leak into an aircraft's limits. A room-scale `set_ceiling` sent to a field drone is refused with a `nack`.

The 10 m minimum slot clears people and most low obstacles but is below many trees: choose slots above the local obstacles. Slots of one fleet must be at least 5 m apart.

On the ground, `flightrisk/drone/fleet_scale.py` builds the coordinator's parameters per scale. `field_scale(n, area_m=..., alt_slots_m=...)` takes the area and slots in metres and needs one slot per drone, at least 5 m apart. It splits the area into one zone per drone, insets each zone's lanes by half the camera's straight-down swath at the lowest slot (`coverage.nadir_swath_m`) and rounds the lane count up so the lanes are at most `swath x (1 - overlap)` apart, so the whole zone is seen; each drone takes off half a swath inside its zone. It computes and reports the coverage (`coverage_fraction`, `coverage_note`); coverage is reported, never a reason to refuse. It derives the geofence each edge must be started with (the farthest lane point plus 5 m). `edge_settings(n)` lists each edge's launch point, `--alt-slot-m` and `--geofence-m`. The camera's long image axis must point across the track: a cropped or rotated mode narrows the horizontal field of view and the swath, and the plan would then leave strips unseen.

### From lanes to autopilot setpoints

Each lane move becomes a position target: the drone adds the move to its commanded target in the plan frame, converts it to latitude, longitude and altitude above mean sea level from its takeoff point, sends it with MAVSDK `goto_location` (MAVLink `MAV_CMD_DO_REPOSITION`, supported by PX4 and ArduCopter), and waits until the autopilot's own position estimate is within the arrival radius (1 m, less for short moves).

Why reposition targets and not offboard setpoints: the autopilot holds a reposition target by itself. Offboard mode (PX4 OFFBOARD, ArduPilot GUIDED with `SET_POSITION_TARGET_LOCAL_NED`) needs a continuous setpoint stream from the companion computer; a stalled or crashed edge then trips the autopilot's offboard-loss failsafe in the middle of a lane, and the setpoint thread becomes one more thing that can die. With reposition targets nothing streams: if the edge dies, the drone holds its last target and the autopilot's own failsafes bring it down. The cost is a slow-down at the end of each move. The ground currently sends lanes in segments of at most 5 m, so a long lane is several targets; merging collinear segments is a follow-up.

Watchdogs: every move and takeoff has a deadline (twice the expected time plus 20 s); a target not reached in time raises, and the executor lands. A landing interrupts any move or takeoff that is waiting.

The altitude reference is taken at every send (the autopilot's AMSL altitude minus its height above home), because both autopilots reset home at arming.

Rotation: PX4 turns to the yaw of a reposition, so `rotate()` waits for the heading. ArduCopter ignores that yaw (in GUIDED it faces its direction of travel, `WP_YAW_BEHAVIOR`), so on ArduPilot `rotate()` turns the plan's "forward" for the next moves without waiting for a heading the autopilot will never hold.

The plan frame: the origin is the takeoff point of the current flight; "ahead" is `--frame-heading-deg`, a compass bearing that is required and must be the same on every drone of a fleet (for example 0 for north). Without it each drone's own takeoff heading would rotate its zone into its neighbours'.

## Safety model for real aircraft

A Pixhawk-class aircraft is far more dangerous than a Tello. The edge is one layer; the autopilot's own failsafes are the layer that still works when the companion computer, the edge process or the radio fails. Both are required.

### Required on the autopilot

The edge reads these parameters and refuses to take off until they are set (fail closed: a parameter it cannot read counts as not set). Only values that land or return are accepted: "report only", "warning" and "none" do nothing, "hold" hovers until something else acts, and "terminate" or "disarm" stop the motors in the air. Parameter meanings are from the [ArduCopter](https://ardupilot.org/copter/docs/parameters.html) and [PX4](https://docs.px4.io/main/en/advanced_config/parameter_reference.html) parameter references (cited in `flightrisk/drone/autopilot.py`).

| | ArduPilot (ArduCopter) | PX4 |
|---|---|---|
| Geofence enforced by the autopilot | `FENCE_ENABLE=1`; `FENCE_TYPE` with the circle (2) and max-altitude (1) bits; `FENCE_ACTION` 1 to 5 | `GF_ACTION` 2 (hold), 3 (return) or 5 (land); `GF_MAX_HOR_DIST` above 0 |
| Fence size | `FENCE_RADIUS` at least sqrt(2) x the edge geofence + 5 m; `FENCE_ALT_MAX` above the edge ceiling and at most 120 m | `GF_MAX_HOR_DIST` at least sqrt(2) x the edge geofence + 5 m; `GF_MAX_VER_DIST` above the ceiling and at most 120 m |
| Battery | `BATT_FS_LOW_ACT` and `BATT_FS_CRT_ACT` in 1, 2, 3, 4, 6, 7 (not 0, not 5 terminate); `BATT_LOW_VOLT` or `BATT_LOW_MAH` above 0; `BATT_ARM_VOLT` above 0 | `COM_LOW_BAT_ACT` 2 or 3 |
| RC loss (a safety pilot with a transmitter is required) | `FS_THR_ENABLE` in 1, 3, 4, 5, 6, 7 | `NAV_RCL_ACT` 2 (return) or 3 (land) |
| Data-link loss | (see below) | `NAV_DLL_ACT` 2 or 3 |
| Return altitude, whatever `--failsafe-action` | `RTL_ALT` at or below this drone's slot (0 returns at the current height) | `RTL_RETURN_ALT` at or below the slot (PX4's default is 60 m) |
| System id | `SYSID_THISMAV` (or `MAV_SYSID`) equal to `--mavlink-sysid` (default 1) | `MAV_SYS_ID` equal to `--mavlink-sysid` |

The return-altitude rule applies even with `--failsafe-action land`, because the autopilot's own RC-loss and battery failsafes return by themselves: from a 10 m slot, PX4's default 60 m return would climb through every other slot and far above the ceiling. A plan that moves a flying drone to a slot below the return altitude is refused with an `altitude` nack. `BATT_ARM_VOLT` is required because ArduPilot's remaining percentage counts the charge used since boot, so a half-empty pack reads 100% after a reboot. Recommended but not checked: on ArduPilot, set the GCS-failsafe system id to the companion computer's MAVSDK id (245) and `FS_GCS_ENABLE` to RTL, so the autopilot returns if the edge stops sending heartbeats. A polygon fence may be used in addition, but its size cannot be checked, so the circle is required.

`--allow-unverified-failsafes` skips only the parameter checks. It exists for simulation and must not be used on a real aircraft.

### The edge's preflight (before every takeoff from the ground)

Fresh telemetry from the autopilot (no older than 3 s, checked per stream: a stream that stopped keeps its last values, so position, GPS, health and battery must each be fresh), a known autopilot (PX4 or ArduPilot), a 3D GPS fix with at least 8 satellites, the autopilot's own global-position, home-position and arming checks, a battery reading at or above `FLIGHTRISK_MIN_TAKEOFF_BATTERY` (30% by default), the parameters above, and the existing checks (ground link alive, plan valid). A refused start is logged, reported in telemetry (`drone_link_ok: false` with the reason in `drone_link_detail`), and, for a protocol v2 ground, answered with a `drone_unreachable` nack carrying the reason. Phase 1 reuses `drone_unreachable` because adding a nack reason is a protocol change; a dedicated "not ready" reason is a follow-up.

### What happens when

| Event | What the edge does |
|---|---|
| Ground link silent for `FLIGHTRISK_LINK_LOSS_LAND_S` (3 s) | Holds: no further move is sent and the autopilot holds its position, for `--link-loss-hold-s` (default 10 s for MAVLink drones; a ground-station crash is a common-mode failure that must not send a whole fleet home at once). If the ground comes back the plan resumes. After the hold: `--failsafe-action` (`rtl` by default: the autopilot's return to launch; `land`: land in place). |
| Battery below 15% | `--failsafe-action` |
| Measured position outside the geofence plus 3 m | `--failsafe-action` |
| The autopilot refuses the return, or has not entered it within 5 s | Land in place instead (and every later failsafe landing of that flight lands in place) |
| A planned move would leave the geofence, measured height above the ceiling, unknown height, an executor error | Land in place |
| LAND ALL, a ground `land`, the local `l` key, Ctrl+C | Land in place, always (never gated, never fenced) |
| Any flight mode the edge did not set: an RC pilot switching modes, a ground control station, or the autopilot's own failsafe, including a switch from the edge's return to a landing or from the edge's landing to a return | External control latches for the rest of the flight: the edge sends no move or takeoff, none of its automatic landings, and none of the monitor's landing retries, so it never fights the pilot or the autopilot. Telemetry reports it. A new operator landing command (LAND ALL, a ground land, the local key, Ctrl+C) sends one LAND, once per command. The latch clears once the drone has landed and disarmed. PX4 limitation: the edge itself uses PX4's HOLD, so a pilot switching to HOLD is not told apart; a switch to Position, Altitude, Manual or another mode is. |
| The companion-to-autopilot link drops | Telemetry goes stale; the edge refuses takeoff and cannot land the drone itself. The autopilot holds its last target until its own failsafes act (battery, RC loss, PX4 data-link loss, ArduPilot GCS failsafe if configured as recommended). |
| The edge process dies (crash, SIGKILL, out-of-memory killer) | The edge runs `mavsdk_server` under a small supervisor that kills it within about a second of the edge's death (Linux and macOS), so it stops sending heartbeats and frees the serial port. Then as above: the autopilot holds and its failsafes act (PX4's data-link-loss failsafe fires once the heartbeats stop; ArduPilot's GCS failsafe only if configured as recommended). If the supervisor itself is killed first, the server may keep running until the companion computer reboots. |
| A landing or return already under way | Re-sent by the monitor only while the autopilot has not entered it yet; once entered it is never sent again. |

Defaults are conservative: 3 m/s cruise (at most 15), 5 m takeoff height, 15 m slot (10 m minimum), 30 m ceiling with a 5 m margin, 30 m geofence, a 10 s hold on ground-link loss, failsafe parameters required, MAVLink on loopback only. A refused cruise speed is logged and reported in telemetry (`speed_set: false`); the autopilot's own cruise speed then applies.

### Launch points and landings

Decided for phase 1 (the user has not chosen; this is the reviewer's recommendation, applied): return to launch stays the default link-loss action, with the hold above, the refused-return fallback, and the return-altitude rule. Because returns and landings converge on each drone's launch point:

- Launch points must be about 10 m apart and away from people; no hand launches and no shared pads. The ground script warns when the planned launch points are closer.
- Landings are staggered: at the end of a plan the ground script lands the drones one at a time, lowest slot first, so no drone descends through a lower drone's slot. LAND ALL (an emergency) lands every drone at once, in place.
- A plan that finishes leaves each drone hovering at the end of its last lane until the ground lands it; nothing lands it there by itself except the hover timeout of a v2 plan.

## Verification status

Stated plainly:

- **Unit and integration tests against a simulated MAVSDK** (`tests/support/fake_mavsdk.py`): a kinematic model behind MAVSDK's Python API runs the real `MavsdkAutopilotLink`, `MavlinkFleetDrone`, `LaneExecutor`, ground transport and `FleetCoordinator`. They check the edge's logic: frame math, preflight refusals, failsafe mapping, external control, watchdogs, a two-drone field plan end to end, and the `--drone mavlink` edge over a real WebSocket ground link. They do not check how real PX4 or ArduPilot firmware behaves.
- **ArduCopter 4.6.3 SITL** (native build, `docs/sitl-demo.md`, "Fleet edge in SITL"): one drone flew a field plan from the real coordinator and landed on LAND ALL; two drones in two SITL instances flew their zones at 10 m and 15 m; a silent ground link made the edge return the drone to launch, and it landed 2 cm from its takeoff point; the real edge process (`--drone mavlink`, YOLO detector, WebSocket link) flew a plan from `scripts/field_fleet_ground.py`. SITL also found five problems the simulated tests could not, all fixed (listed there). After the review round's safety fixes it was run again: the field plans (now covering the whole width), the link-loss hold and return, ArduCopter ignoring a reposition's yaw (so `rotate()` no longer waits for it), and a mode switch to LOITER after LAND ALL that the edge did not fight. No PX4 SITL was run. An RC pilot's own switch, the autopilot's own failsafes, a refused return and a geofence breach were not exercised in SITL.
- **Not verified**: any real autopilot, flight controller or companion computer; any real flight; any real radio, mesh or MAVLink router; real GPS noise and wind; camera mounting and the Raspberry Pi camera's footprint at altitude; SITL ran natively on Apple Silicon (no emulation), so emulation timing was not a factor; SITL physics and sensors are idealised.

## Starting a MAVLink edge

On the companion computer (Raspberry Pi 5 with a Hailo accelerator, or a Jetson), with the autopilot on a serial port and a camera attached:

```bash
read -rs FLIGHTRISK_EDGE_TOKEN && export FLIGHTRISK_EDGE_TOKEN   # or source a 0600 env file; never on the command line
python -m flightrisk.edge_device --drone mavlink \
    --mavlink-address serial:///dev/ttyAMA0:921600 --source camera \
    --drone-id drone-1 --alt-slot-m 15 --geofence-m 40 --frame-heading-deg 0 \
    --ws-url ws://<ground>:9000
```

With a MAVLink router sharing the autopilot with a ground control station, point it at the router on loopback, for example `--mavlink-address udpin://127.0.0.1:14540` (the default). MAVLink is not authenticated: listening on another interface lets any host on that network command the aircraft, so do that only on a trusted, isolated network. `--mavlink-sysid` (default 1) must match the autopilot's system id. A stream URL with credentials can be given in `FLIGHTRISK_VIDEO_URL` instead of `--video-url`, which would show it in the process list. `--autopilot px4|ardupilot` skips the detection from parameters.

The dashboard still plans at the indoor scale and validates slots against the indoor ceiling; planning a field fleet from the dashboard is a follow-up. Until then a field plan is made with `field_scale` and the coordinator directly (see `tests/test_field_scale.py` and `docs/sitl-demo.md`).

## Phase plan and follow-ups

Phase 1 (this change): the support matrix and this design; the `field` envelope profile; `--drone` separated from `--source`; the MAVLink fleet edge (`MavlinkFleetDrone` over `MavsdkAutopilotLink`) with preflight, failsafe mapping, external control and watchdogs; `field_scale` for ground plans; a conformance suite and a copyable template backend.

Follow-ups, roughly in order:

1. Dashboard: plan and display field fleets (area and slots in metres, the ceiling board per profile, GPS map), and a protocol v2 nack reason for "not ready".
2. Longer lanes: merge collinear lane segments into one target (fewer stops), and per-scale segment length in the coordinator.
3. Real-hardware path: a tethered or netted first flight, then a single drone in an open field with a safety pilot, then two drones, each after SITL runs of the same plan and settings.
4. Optional offboard-setpoint mode with a setpoint watchdog, for smoother lanes, once the reposition mode has flown.
5. Mission-upload backend for fixed-wing and VTOL.
6. Crazyflie (`cflib`) backend at room scale; iNav (MSP) backend.
7. Multi-drone return deconfliction beyond per-slot return altitudes (staggered landings at a shared home).
8. The dashboard's `MavlinkController` (single-drone path) reads `remaining_percent` as a 0-1 fraction; MAVSDK 2 and later report 0-100, so it shows battery 100 times too high. Not changed here; the fleet edge's link reads it as a percentage.
