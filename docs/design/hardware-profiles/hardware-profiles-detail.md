# Hardware profiles: detailed component design (PR #202 round 1)

This is the detailed #197 component design as revised after the round 1 critic review (head
`04acc47`). `design.md` (fleet architecture v1) and `spec-v1.md` supersede it wherever they differ:
in particular the phase plan and PR numbers (`tasks.md` replaces PR-1 to PR-8), the behaviour-change
list (`design.md` section 20 replaces section 17 here), the slot rule (spec 8.3 replaces 10.3's
distinct-slot rule), the Android direct Tello mode (a spec non-goal), and the tables (moved to
`parameters.md`). Its line numbers are on `a96c6b1`; `parameters.md` carries the `b0027a2` ones.


Status: design, revised after the round 1 critic review (findings F1 to F19). Nothing here has been
implemented. Base: `origin/main` at `a96c6b1`, which contains #180, #188, #189, #190, #192, #194,
#195 and #203. Citations of the form "`path:line` on branch X" refer to the in-flight branch tips
listed in ASSUMPTION A1; plain `path:line` refers to `a96c6b1`.

The issue started as "make every Tello height, distance and time configurable, with per-airframe
defaults and hard bounds". The user then widened it: FlightRisk should be tightly coupled to the
DJI Tello but loosely coupled to any drone; users select the drones they own and the code adapts to
that hardware; and features such as thermal detection or a bird's-eye camera are switched on only
where the hardware supports them. The user also asked that every Tello height, distance and timing
be configurable and shown; section 23 records how that is met. This document covers all of it in
one architecture.

## 1. Decisions in one page

1. **Five layers, each a small pure module, each with one owner:**
   - a **hardware catalogue** of drone models (`flightrisk/hardware/catalogue.py`): what a model
     *is* (control protocol, camera mounts and sensors, positioning, envelope facts, link, safety
     facts);
   - an **airframe profile** per model: the column of defaults and hard bounds that model gives to
     every flight parameter (`flightrisk/params/`), pinned by a committed golden file;
   - an **inventory** of the units the user owns (`~/.flightrisk/inventory.json`, with a revision
     number and compare-and-swap writes): models, how to reach them, which are available today,
     per-unit feature switches, and a declared mission pitch for any gimbal;
   - a **feature registry** (`flightrisk/hardware/features.py`) with two gating phases: static
     catalogue and inventory facts when values are resolved, and runtime capabilities at the
     takeoff preflight, fail-closed;
   - one **resolver** that turns profile, files, inventory, launcher, environment and command line
     into effective values, each with its source.
2. **The Tello is the reference implementation and first-class model.** Its catalogue entry and
   profile column reproduce today's behaviour; a golden file of literal values, generated once from
   today's code, pins every Tello and MAVLink value before any code moves. Every Tello height,
   distance and timing, including the 24 SDK and driver facts, is a registry row and shown with its
   source; the SDK facts are read-only. The generic MAVLink multirotor is the second concrete
   adapter. DJI MSDK is an adapter slot only.
3. **The seams already exist and are extended, not replaced.** The adapter interface is the role
   protocols of `flightrisk/drone/controller.py:286-392` plus one optional camera role. Frame
   sources (not drones) are wrapped so every frame carries camera metadata, which reaches the ground
   in each detection. Detection-pipeline selection extends the detection-profile registry. One
   detection pipeline runs per unit in v1. `tello.py`, `lane_flight.py` and `obstacle_camera.py`
   stay where they are.
4. **Each camera mount has one role at a time:** level (obstacle sensing and level search) or nadir
   (bird's-eye search). A combination that needs one mount both level and down is refused. A gimbal
   counts as level only with measured pitch, and every sense gate on a gimbal returns `unavailable`
   (the lane is cut) when the measured pitch leaves the level tolerance.
5. **Precedence, lowest to highest:** profile default, params file, inventory, launcher window,
   environment, command-line flag. Files can never silently weaken safety: "less safe" is measured
   against the built-in base column, a safety switch can never be stored as off in a file, and a
   less-safe value from a file needs an exact confirmation. For window-editable values, an
   environment or flag value that is less safe than the window's needs confirmation too.
6. **Refuse, never clamp.** A value outside its profile's hard bound, a feature switched on for
   hardware that cannot do it, an empty intersection of bounds, or a failed cross-value constraint
   is refused with a message naming the value, its source and the bound or the missing capability.
7. **The edge is authoritative for what it flies.** Each edge advertises `unit_rev` in its protocol
   v2 `hello` (no telemetry field) and answers the read-only query `describe_unit`. Queries are a
   separate message set from configuration commands: answered from any ground, never feeding the
   link watchdog, with their own per-connection command-id cache and a rate limit.
8. **Mobile:** phones may create units of built-in models and toggle availability and non-safety
   features, nothing else. Android's direct Tello stack reads a generated Tello parameter column
   instead of mirrored constants.
9. **A guard test** fails on a new unit-named numeric literal, environment default or safety default
   in the flight, launcher and dashboard modules that is neither in the registry nor in a reviewed
   allowlist; table, golden file and allowlist have code owners.
10. **Eight PRs**, all starting after the in-flight work merges, with
    `fix/geofence-slot-battery-consistency` as a hard prerequisite of PR-1. PR-1 (catalogue,
    profiles, resolver, golden file, guard) goes first; PR-1 and PR-2 (inventory, features, seam
    types, wire shapes) are pure and change no behaviour (`tasks.md`).

## 2. Scope, roadmap and assumptions

**In scope (the first PRs):** the catalogue, the profiles and parameter registry, the inventory,
capability gating, the code seams, and the two concrete adapters (Tello and MAVLink) wired through
them; the dashboard, launcher and both mobile apps updated to show and select them.

**Roadmap, made pluggable here but not built:**

- R1. Thermal detection pipelines: a hot-spot detector, a thermal person detector, and radiometric
  handling where the camera provides it. The slot is a detection profile with
  `requires_sensor = thermal` (`docs/detection-profiles.md:58-65` sketches a `thermal_nadir`
  registration).
- R2. Better bird's-eye detection: the `top_down` profile with HERIDAL fine-tuned weights exists
  (`flightrisk/vision/profiles.py:15-17`, `:310-318`; `docs/altitude-benchmark.md`); further
  HERIDAL-style training is a follow-up.
- R3. A DJI Mobile SDK adapter. MSDK runs on a phone, so its natural host is the Android app acting
  as a flying edge; it needs the edge fleet protocol on Android. Today other DJI drones are out of
  scope (`docs/open-drone-support.md`, support matrix, "Other DJI").
- R4. Gimbal pitch control per mission (switching one camera between level and nadir roles in a
  flight), bird's-eye search at room scale, per-zone patterns, RTK-aware planning, and changing
  values while a drone flies.
- R5. Two detection pipelines at once on one unit (for example RGB and thermal on a dual sensor).

**Out of scope:** choosing defaults for a particular real rescue airframe, calibrating the obstacle
sensor (both from the issue), and vision or reasoning settings, which live in `FlightRiskConfig`
(`flightrisk/config.py:168-248`).

Assumptions (each is labelled where it is used):

- ASSUMPTION A1: all of this work starts after these merge, at or after the tips read for this
  revision: the incident fixes `fix/tello-climb-freshness-guards` (`e7e039a`); #193
  `feature/obstacle-relook-sidestep` (`6f58674`); #198 `feature/sensing-controls-temp-preflight`
  (`30a6f9e`); #191 `feature/claude-reasoning-hardening` (`b61f05c`); #196 and #199 (iOS and
  Android demo modes); #200 `fix/flight-ops-followups` (`c5fa76d`, adds
  `flightrisk/dashboard/single_search.py`); #201 `fix/windows-followups` (`787d53c`); the
  floor-ratio opt-in `feature/obstacle-floor-ratio-optin` (`86bd096`); the metrics follow-ups
  `fix/metrics-report-followups` (`7e935a8`); and `fix/geofence-slot-battery-consistency`
  (`da61e28`), which fixes findings 1 to 3 and is a hard prerequisite of PR-1. If any of them
  changes, PR-1 updates the affected rows and the golden file before it is opened.
- ASSUMPTION A2: hard bounds that do not exist in code today (marked "new" in the tables) are best
  guesses chosen to reject nonsense and clearly unsafe values, not tuned limits.
- ASSUMPTION A3: generic multirotor defaults for values that have no MAVLink equivalent today are
  best guesses; each is marked.
- ASSUMPTION A4: a launcher and the edges it starts are always the same installed version, because
  the launcher starts edges from its own Python (`flightrisk/launcher/commands.py:245-277`).
- ASSUMPTION A5: a fleet that mixes `room` and `field` models is refused at Plan, because plans are
  in one unit scale (`flightrisk/drone/fleet_scale.py:1-23`).
- ASSUMPTION A6: the worst-case telemetry after the in-flight work is 45 fields (the sensing branch's
  42 plus two from the incident branch and one from #193; the floor-ratio branch nests its fields).
  This design adds no telemetry field; `tests/test_telemetry_field_cap.py` measures it in PR-3.
- ASSUMPTION A7: the Tello's camera is fixed, forward-facing and has no gimbal; its live stream is
  960 by 720 (`flightrisk/drone/coverage.py:101-106` marks the stream size UNVERIFIED).
- ASSUMPTION A8: a ground that does not know the optional `cam` and `pitch_deg` keys of an edge's
  `detections` message ignores them, as every ground accepts extra telemetry fields
  (`docs/open-drone-support.md`, "The fleet protocol is unchanged"). PR-3 proves it with a test
  against the ground's detections handler.

## 3. What already exists and is reused

| Existing piece | Where | How the design reuses it |
| --- | --- | --- |
| Role protocols (`Lifecycle`, `TelemetrySource`, `FlightControl`, `Motion`, `Navigation`, `VideoSource`), `Capability`, `DroneCapabilities.flight_scale`, `CommandResult` | `flightrisk/drone/controller.py:1-24`, `:67-99`, `:103-117`, `:286-392` | The adapter interface. One optional role is added (`CameraSource`, section 11.1); nothing is renamed. |
| Backend registry and bootstrap | `flightrisk/drone/registry.py:1-60`, `flightrisk/drone/backends.py` | `BackendSpec` gains two additive fields: the control protocol it speaks and the catalogue models it drives. |
| Edge drone construction | `flightrisk/edge_device.py:1295-1306` (`_drone_kind`), `:2090-2113` (`_build_drone`), `:1703-1723` (MAVLink) | Chooses the backend from the unit's model; `--drone` stays as an override that must agree. |
| Frame sources | `CV2FrameSource` (`edge_device.py:292`), `DroneFrameSource` (`:429`), `ResilientCaptureSource` (`:657`); `FrameStamp` and `LatestFrame` (`flightrisk/drone/obstacle_sensor.py:478-640`); `feed_sensor_holder` (`edge_device.py:2028-2044`) | Wrapped so every frame, from the Tello's video or a MAVLink capture source, carries `FrameMeta`; time comes from the existing `FrameStamp`. |
| Detections message | `EdgeRunner.to_dict` (`flightrisk/edge.py:276`) | Gains optional `cam` and `pitch_deg`. |
| Detection profiles with a `view` field and fallbacks | `flightrisk/vision/profiles.py:80-137`, `:295-335`; `flightrisk/edge_profiles.py:80` (`ProfileSwitcher`); nack reason `unavailable` (`fleet_protocol/constants.py:115`) | The detection-pipeline seam: profiles gain `requires_sensor` and `requires_view`; the switcher refuses a profile the unit's active camera cannot serve with the existing `unavailable` reason. |
| Forward-camera obstacle sensor and the MAVLink fallback | `flightrisk/drone/obstacle_camera.py:1-40`; `edge_device.py:1955-2025` (`_build_obstacle_wiring`; `off` at `:1987-1990`; a MAVLink edge without `--source camera` or `url` builds no sensor, `:1995`); `_tell_mavlink_drone` (`edge_device.py:2060-2085`) | Unchanged behaviour. The feature gate decides whether the forward sensor is built; without it, the MAVLink drone's preflight still demands onboard avoidance at takeoff. |
| Edge authority and watchdog feeding | `flightrisk/fleet_authority.py:93-140`, `:236-270`; single-ground feeding in `edge_device.py:755-775`; multi-ground feeding in `flightrisk/edge_uplink.py:198`, `:287-289` | Queries are handled before the authority checks, never feed the watchdog, and use their own cache. |
| Edge `hello` (protocol v2) | `EdgeHello` (`flightrisk/fleet_protocol/fencing.py:177-200`) | Gains optional `unit_rev`; it is sent on every connection, so it is re-sent on reconnect. |
| Camera descriptions for coverage and geolocation | `coverage.CameraModel` (`flightrisk/drone/coverage.py:28-50`, presets `:52-98`), `geolocate.CameraModel` (`flightrisk/drone/geolocate.py:19-25`), `nadir_swath_m` (`coverage.py:326`) | Built from the catalogue's camera descriptors. |
| Plan scales: head-on room lanes and nadir-swath field lanes | `FleetScale`, `ROOM_SCALE`, `field_scale` (`flightrisk/drone/fleet_scale.py:1-23`, `:78-133`) | The plan-pattern seam for level search versus bird's-eye search. |
| `EnvelopeLimits`, `ROOM_LIMITS`, `FIELD_LIMITS`, `limits_for` | `flightrisk/drone/safety.py:144-218` | Become views of the `tello` and `generic_multirotor` profile columns. |
| `ObstacleSettings` (refuse out-of-range values, log each non-default) | `edge_device.py:1794-1868`; `EVENT_SETTING_NON_DEFAULT` (`obstacle_sensor.py:246`) | The resolver's refusal and logging behaviour. |
| `FlightGuards`, `TempLimits` | `flightrisk/drone/flight_guards.py:68-153` on the incident branch; `flightrisk/drone/temp_preflight.py:36-120` on #198 | Typed views built from resolved values. |
| Effective value with a source, and the 409 confirmation | `flightrisk/launcher/settings.py:434-560` on #198 (`EffectiveObstacle`, `obstacle_env`, `sensing_warning`) and `flightrisk/launcher/server.py` on #198 (`CONFIRM_SENSING`) | Generalised to every parameter and feature. |
| Launcher settings, `Issue`s, presets, adapter memory | `flightrisk/launcher/settings.py:76-120`, `:301-378`, `:581-710` | Drone rows are picked from inventory units; issues report refusals and overrides. |
| Launcher writes the ceiling file at Start | `flightrisk/launcher/ceilingfile.py:38-46` (`desired_settings`) | Writes the resolved values instead of the window values (section 10.4). |
| `set_ceiling` and configuration commands | `flightrisk/fleet_protocol/ceiling.py:1-100`; `fleet_protocol/constants.py:101-111` | Unchanged runtime path. `describe_unit` is not a configuration command (section 12.2). |
| Ceiling file rules (atomic write, refuse an unreadable file) | `flightrisk/dashboard/ceiling.py:83-183`, `docs/ceiling-and-altitude.md` | The same rules for `params.json` and `inventory.json`. |
| Mobile fixtures generated by Python, lenient decoders, allow-listed emits | `docs/mobile-contract.md:12-52`, `:86-104`, `:132-141` | New events, the catalogue and the Tello column follow the same pattern. |
| Android Tello UDP stack | `mobile/app/src/main/java/com/flightrisk/app/drone/TelloConnection.kt`, `DroneManager.kt`, `SearchPattern.kt`; `config/FlightRiskConfig.kt:186-205` | Becomes the Kotlin `tello_sdk_udp` adapter, driven by the generated Tello column. |
| AST guard test pattern | `tests/test_architecture.py:1-105` | The literal guard test follows it. |

## 4. Findings from the inventory

These are concrete problems the design addresses; they show why one source of truth is needed.
Findings 1 to 3 are fixed before PR-1 by `fix/geofence-slot-battery-consistency` (`da61e28`): the
launcher passes `FLIGHTRISK_GEOFENCE_CM` to the ground, `fleet_patterns` takes the coordinator's
slots, and `min_takeoff_battery_from_env` in `safety.py` becomes the single battery minimum. PR-4,
PR-5 and PR-6 therefore do not touch them.

1. **The ground planned against a different geofence than launcher-started edges enforce** (450 cm
   from `launcher/settings.py:49` and `commands.py:265`, while the ground used 500 cm,
   `dashboard/app.py:565-575`). Fixed by the prerequisite.
2. **Stale zone slots** `(100, 160)` in `fleet_patterns.py:176-177`. Fixed by the prerequisite.
3. **Three copies of the 30 % takeoff minimum** (`safety.py:105`, `coordinator.py:122`,
   `launcher/preflight.py:48-49`). Fixed by the prerequisite.
4. **The landing battery threshold cannot be configured** (`safety.py:424`).
5. **The Tello move range leaks into airframe-neutral code:** `coordinator.py:131-134` (used at
   `:935`), the nudge range (`fleet_protocol/constants.py:286-288`), and the `DroneCapabilities`
   defaults (`controller.py:106-108`).
6. **Lane spacing and area defaults are written several times:** 75 cm at `drone/plan_edit.py:62`,
   `drone/fleet_scale.py:129` and `dashboard/__main__.py:146`; 400 cm at `fleet_scale.py:128` and
   `dashboard/__main__.py:141`.
7. **A clamp:** `manual_orphan_land_s` raises a value below 30 s to 30 s
   (`dashboard/manual_control.py:121-124`).
8. **The legacy MAVLink backend declares another move range** (10 to 50 000 cm,
   `drone/mavlink.py:116-117`) than the fleet MAVLink path (20 to 5000 cm,
   `mavlink_fleet.py:376-379`).
9. **No upper bound on a hand-started room geofence** (`safety.py:183`, `inf`), while the launcher
   caps it at 5000 cm (`launcher/settings.py:61`).
10. **Two Tello maximum heights:** 10 m in the backend's capabilities (`drone/tello.py:573`) and the
    published 30 m in the coverage preset (`coverage.py:61-65`).
11. **The MAVLink camera has no declared orientation, and two parts assume different ones.** Field
    coverage plans lanes from a camera "pointing straight down" (`fleet_scale.py:58-67`), while an
    edge with a camera source always builds the forward stepped sensor (`mavlink_fleet.py:225-236`).
12. **Cameras are described three times and frames carry no camera metadata:**
    `coverage.CameraModel`, `geolocate.CameraModel` (with an assumed 90 degree field of view,
    `geolocate.py:19-25`) and `DetectionProfile.view` ("informational", `vision/profiles.py:133`);
    `VideoSource` yields bare arrays (`controller.py:345-347`).
13. **Android mirrors Python constants and clamps moves.** `DroneConfig` says its defaults "match
    `flightrisk.config.DroneConfig` in Python exactly" (`FlightRiskConfig.kt:186-205`), and
    `SearchPattern.clampDistance` forces every segment into 20 to 500 cm (`SearchPattern.kt:23-29`).
14. **Fleet-message keys may not contain `feature`, `reid` or `frame`.** The privacy screen refuses
    any key containing a forbidden part, at every depth (`fleet_protocol/constants.py:198-255`,
    `fleet_protocol/checks.py:255-266`). Wire names below avoid them (`gates`, `appearance_match`,
    `view_wait`).
15. **Android lands the Tello at another battery level than the edge.** The Android direct Tello
    stack lands at its critical threshold of 10 % (`DroneManager.kt:452-458`,
    `FlightRiskConfig.kt:194`); the Python edge lands at 15 % (`safety.py:106`).
16. **The MAVLink fleet drone does not declare `STEPPED_SENSING`** (`mavlink_fleet.py:376-385`); a
    MAVLink edge senses obstacles only from a capture source (`--source camera` or `url`,
    `edge_device.py:1995`), never from the drone. A gate that asked the backend for
    `STEPPED_SENSING` would wrongly switch MAVLink sensing off.

## 5. Architecture overview

```
 catalogue (models)      flightrisk/hardware/catalogue.py   what a model is: protocol, camera mounts
        |                                                    and sensors, positioning, envelope facts,
        |                                                    link, safety facts, scale
        v
 profiles (columns)      flightrisk/params/table.py         per model: default and hard bound of
        |                tests/golden/flight_params.json     every flight parameter, pinned
        v
 inventory (units)       flightrisk/hardware/inventory.py   the user's units: model, link kind and
        |                ~/.flightrisk/inventory.json        address, serial, available, feature
        |                                                    switches, gimbal mission pitch; rev + CAS
        v
 features (gates)        flightrisk/hardware/features.py    static gate at resolve time (camera roles,
        |                                                    frame path); runtime gate at takeoff
        v
 resolver                flightrisk/params/resolve.py        effective value + source for every
        |                                                    parameter and feature of one unit
        v
 consumers   edge: adapter (registry.py/backends.py), SafetyEnvelope, FlightGuards, obstacle wiring,
             frame-source wrapping, detection-pipeline selection, takeoff preflight;
             ground: coordinator, plan checks, dashboard; launcher: Start validation;
             phones: inventory screen through the ground
```

All five modules are pure Python with no Flask, vision or drone imports, so an edge-only install
and the launcher can import them, as `dashboard/ceiling.py` is written to be importable without
Flask (`launcher/ceilingfile.py:9-11`).

## 6. Hardware catalogue

### 6.1 The model descriptor

`DroneModel` (frozen dataclass):

| Field | Content |
| --- | --- |
| `id`, `name`, `maker` | For example `tello`, "Ryze Tello", "Ryze Tech / DJI". |
| `support` | `reference` (first-class, flown), `supported` (built, tested in simulation), `slot` (declared adapter, no implementation). |
| `scale` | `room` or `field`, the values of `DroneCapabilities.flight_scale` (`controller.py:113-117`). |
| `adapter` | The control protocol: `tello_sdk_udp`, `mavlink`, or `dji_msdk` (slot). The backend registry maps it to an implementation. |
| `frame_path` | Where the edge gets this model's frames: `drone_video` (the backend's `on_frame`; Tello) or `capture_source` (`--source camera` or `url` on the companion computer; MAVLink). |
| `cameras` | Tuple of `CameraSpec` (below). |
| `positioning` | `vps_tof` (optical flow plus time-of-flight, no absolute position), `gps`, or `rtk`. |
| `envelope` | Published facts with sources: maximum speed, maximum height, maximum range, endurance. These feed coverage estimates; the enforced limits are the profile column. |
| `link` | `wifi_ap` (the drone is an access point), `wifi_station`, or `radio`. |
| `safety` | Facts the code relies on: what the drone does on command loss and after how long, how it answers commands, and which failsafe is checked before takeoff. |
| `profile` | The profile column id in the parameter registry (usually the model id; `tello_edu` uses `tello`). |
| `extends` | For a custom model: the built-in model it is based on. |
| `sources` | URLs for every published fact, or ASSUMPTION. `coverage.CameraModel` already requires this (`coverage.py:34`). |

`CameraSpec`:

| Field | Content |
| --- | --- |
| `id` | For example `front`. |
| `mount` | The physical mount id. A dual RGB and thermal sensor is two `CameraSpec`s that share one mount; they always point the same way and share one role. |
| `mount_kind` | `fixed` or `gimbal`. |
| `orientation` | For a fixed mount: `forward` or `downward`. |
| `pitch_min_deg`, `pitch_max_deg` | For a gimbal: its range (0 is level, -90 straight down, as in `geolocate.pixel_to_ground` and `CameraModel.gimbal_pitch_deg`). |
| `pitch_measured` | True when the adapter reports the gimbal's measured pitch in each frame's metadata. Without it a gimbal can never take the level role. |
| `stream_w_px`, `stream_h_px`, `hfov_deg`, `fps` | Stream geometry. |
| `sensor` | `rgb` or `thermal`. |
| `radiometric` | Thermal only. |

`DroneModel.coverage_camera(camera_id, pitch_deg)` returns a `coverage.CameraModel` and
`geolocate_camera(...)` returns a `geolocate.CameraModel`, so finding 12's three descriptions come
from one place.

### 6.2 Camera roles

Each mount has exactly one **role** for a flight, decided before takeoff:

- `level`: a fixed forward mount, or a gimbal whose declared mission pitch is within
  `sensing.level_pitch_tolerance_deg` of 0 **and** whose pitch is measured. Serves
  `forward_obstacle_sensing` and `level_search`.
- `nadir`: a fixed downward mount, or a gimbal whose declared mission pitch is -60 degrees or lower.
  Serves `birdseye_search`.
- `oblique`: any other gimbal pitch. Serves only orientation-agnostic features.

Until roadmap R4, a gimbal mount must have a declared fixed `mission_pitch_deg` in the unit record
(section 8.1), and the operator sets the gimbal to it before takeoff; the design never moves a
gimbal. A unit with one mount cannot have features switched on that need two roles, for example
`forward_obstacle_sensing` (level) and `birdseye_search` (nadir) on one gimbal; that combination is
refused with "camera mount `gimbal` cannot look level for obstacle sensing and down for bird's-eye
search at once". At runtime, every sense gate on a gimbal checks the frame's measured pitch
(`FrameMeta.pitch_deg`, section 11.1): when its absolute value exceeds
`sensing.level_pitch_tolerance_deg`, or the pitch is missing, the gate returns `unavailable` with the
cause "camera not level", which cuts the lane exactly as a stale view does today. A gimbal pointing
down can therefore never let MiDaS judge a nadir view as the path ahead.

### 6.3 Built-in entries

| Field | `tello` (reference) | `tello_edu` (reference) | `generic_multirotor` (supported) | `custom` |
| --- | --- | --- | --- | --- |
| Adapter | `tello_sdk_udp`: SDK text commands on UDP 8889, state on 8890, video on 11111 (`drone/tello_bound.py:89-91`) | same, SDK 2.0 (answers `sdk?`; a non-EDU Tello does not, `drone/tello.py:93-98`) | `mavlink` through MAVSDK (`drone/mavsdk_link.py`, `--drone mavlink`) | the base model's |
| Frame path | `drone_video` (the backend declares `STEPPED_SENSING`, `tello.py:568-576`) | same | `capture_source` (only with `--source camera` or `url`; finding 16) | the base model's |
| Cameras | `front`: fixed forward mount (ASSUMPTION A7), 960 x 720 stream, 82.6 degree field of view (`coverage.py:52-58`), RGB | same | `main`: declared per unit; default fixed forward, RGB, 66 degree field of view (the Raspberry Pi Camera Module 3 preset, `fleet_scale.py:58-75`) (Q14) | declared |
| Positioning | `vps_tof`: vision positioning effective at 0.3 to 10 m ([Tello User Manual](https://www.adorama.com/col/productManuals/RYTELO.pdf), p.7) | same | `gps` (3D fix and `gps.min_satellites`, `drone/autopilot.py:61-62`); `rtk` per unit | declared |
| Envelope facts | 8 m/s, 30 m height, 100 m range, 13 min (`coverage.py:52-66`; manual p.4) | same ([Core Electronics](https://core-electronics.com.au/dji-tello-edu.html.md)) | per unit; 120 m regulatory ceiling (`autopilot.py:222`) | declared |
| Link | `wifi_ap` at 192.168.10.1 (`config.py:142`, `TelloWifiChecker.kt:12`) | `wifi_ap`, or `wifi_station` after the SDK 2.0 `ap` command (credentials are never stored) | `radio` or `wifi_station` to the companion computer | declared |
| Safety facts | Lands by itself after about 15 s without a command (`coordinator.py:146-153`); the edge keepalive covers it (`tello.py:252-258`). Answers `takeoff` only once it hovers; late and duplicate replies are possible (`tello.py:93-113`). | same | Autopilot failsafes (link loss, battery, geofence) must be configured; the edge checks them before takeoff (`autopilot.py`), holds `link.loss_hold_s`, then RTL or land (`mavlink_fleet.py:262-266`). | base model's |
| Profile column | `tello` | `tello` (Q3) | `generic_multirotor` | narrowed copy of the base column |

A custom model is defined in `inventory.json` (section 8) with `extends`, its own descriptor fields
and optional narrowed parameter bounds and defaults. It may not widen any bound, change the scale,
the adapter or the frame path, or add a parameter. Its defaults may lie anywhere inside its bounds,
but "less safe" is always measured against the built-in base column (section 10.2), so a custom
default cannot quietly weaken safety. A model whose adapter is a slot (`dji_msdk`) can be listed,
but a unit of it cannot be added to a fleet: "no adapter for DJI Mobile SDK is installed".

## 7. Flight parameters and profiles

### 7.1 `ParamSpec` (one row per value)

| Field | Meaning |
| --- | --- |
| `id` | Dotted lowercase id with the unit as suffix, for example `envelope.ceiling_cm`. Ids pass the fleet privacy key screen (finding 14). |
| `unit` | `cm`, `s`, `pct`, `degC`, `deg`, `deg_per_s`, `cm_per_s`, `count`, `ratio`, `enum`, `bool`, `cm_list`, `port`, `px_pair`, `degC_pair`. Distances are centimetres on every model, as today (`safety.py:42-48`); metre environment names and flags are aliases with a scale factor. |
| `kind` | `int`, `float`, `bool`, `enum` (with `choices`), `int_list`, `int_pair`. |
| `description`, `group` | One operator sentence; `envelope`, `battery`, `link`, `takeoff`, `tello`, `tello_sdk`, `sensing`, `avoid`, `move`, `plan`, `manual`, `mode`, `gps`. |
| `scope` | `drone` (resolved by the edge, reported) or `ground` (resolved by the ground). |
| `defaults`, `bounds` | Per profile column: default, and inclusive hard bound `(lo, hi)`; `None` when the row does not apply to that model; `lo == hi` makes it read-only for that model. |
| `off_value` | Optional sentinel accepted outside the bound that switches a behaviour off (`0` for `avoid.sidestep_cm`, `"off"` for `tello.temp_refuse_c`). |
| `safety_switch` | True when the `off_value` or `false` removes a protection (`sensing.mode = off`, `sensing.turn = false`, `link.require_failsafes = false`, `tello.temp_refuse_c = off`, and the `forward_obstacle_sensing` feature off). Such a value can never be stored in a file (section 10.2). |
| `safer` | `higher`, `lower` or `none`. |
| `confirm` | `less_safe` (a value on the less-safe side of the base default, or the `off_value`, needs confirmation), `non_default` (any change from the base default needs confirmation, for opt-in experimental behaviour), or `none`. |
| `edit` | `ui`, `file_env`, or `read_only`. |
| `applies` | `next_start`, `on_ground` (changed between flights through `set_ceiling`), or `plan`. Nothing changes in flight in v1. |
| `env`, `env_aliases`, `cli` | Canonical environment name (existing names stay canonical; new rows get `FLIGHTRISK_` plus the upper-case id), aliases with scale factor and the scale they apply to (a room alias is never read for a field model, preserving `safety.py:404-407`), and command-line flags with scale factor. |
| `constraints` | Cross-value checks with operator messages (section 10.3). |

### 7.2 Profiles and the golden file

The parameter registry has one column per built-in profile (`tello`, `generic_multirotor`); every
catalogue model names its column. `EnvelopeLimits`, `ROOM_LIMITS`, `FIELD_LIMITS` and `limits_for`
become views of those columns, and the module constants in `safety.py` become re-exports computed
from the `tello` column, so code that imports them (`dashboard/ceiling.py:56-63`,
`launcher/settings.py:30-35`) keeps working and the constants are no longer literals. The full table
of 110 rows is in section 20.

**Golden file.** PR-1 commits `tests/golden/flight_params.json`: for every parameter id and every
profile, the literal `default`, `lo`, `hi`, `safer`, `confirm`, `edit`, `off_value` and
`safety_switch`, plus `origin` (`today` when the value equals today's code, `new` for a new bound
or row, or `changed` with the number of the section 17 item that changes it) and, for each
environment alias, `today_accepts` (the range today's code accepts). It is generated once, by a
one-off script run by hand while PR-1 is written, from the code at the PR-1 base (after the
prerequisite and all in-flight work). No test or build step ever writes it; the script refuses to
overwrite an existing file. Tests compare `table.py` with the golden file value by value, so
changing a safety value needs two deliberate edits in one diff plus, where it applies, an allowlist
edit. A new `.github/CODEOWNERS` (none exists today) makes the repository owner a required
reviewer of `flightrisk/params/table.py`, `tests/golden/flight_params.json` and
`tests/param_literal_allowlist.py`.

### 7.3 Which profile a unit uses

On the edge, in this order: `--model` or `FLIGHTRISK_MODEL` (with `--airframe` and
`FLIGHTRISK_AIRFRAME` as aliases); the unit's model from the inventory (by `--drone-id`); the
launcher's per-drone model (in `FLIGHTRISK_LAUNCHER_PARAMS`); otherwise the backend's default
(`tello` and `fake` give `tello`, `mavlink` gives `generic_multirotor`). The edge refuses to start
when the model's scale, adapter or frame path differs from the backend it is running
(`tello.py:568-576`, `mavlink_fleet.py:376-385`), so a Tello can never fly with outdoor bounds.

On the ground, a unit's model is what its edge reports; before that, the inventory's model; for an
older edge with no inventory entry, `tello`, which is today's fallback (`safety.py:216-218`) and has
the narrower distance bounds. The ground never trusts a reported bound wider than its own registry:
for each reported value it uses the intersection of the reported bound and its own bound for the
reported model (section 12.3). For a `ground`-scope parameter that depends on the airframes, the
ground uses the intersection of the fleet's bounds and the most conservative default, by `safer`;
an empty intersection refuses Plan, naming the units whose bounds do not overlap.

## 8. Inventory

### 8.1 The unit record

`~/.flightrisk/inventory.json` (`FLIGHTRISK_INVENTORY_FILE` overrides), beside `launcher.json`
(`launcher/settings.py:570-574`) and `ceiling.json` (`dashboard/ceiling.py:103-107`), outside the
repository:

```json
{
  "version": 1,
  "rev": 7,
  "models": {
    "site_hex": {"extends": "generic_multirotor", "name": "Site hexacopter",
                 "cameras": [
                   {"id": "rgb", "mount": "gimbal", "mount_kind": "gimbal", "pitch_min_deg": -90,
                    "pitch_max_deg": 0, "pitch_measured": true, "stream_w_px": 1920,
                    "stream_h_px": 1080, "hfov_deg": 82, "sensor": "rgb"},
                   {"id": "ir", "mount": "gimbal", "mount_kind": "gimbal", "pitch_min_deg": -90,
                    "pitch_max_deg": 0, "pitch_measured": true, "stream_w_px": 640,
                    "stream_h_px": 512, "hfov_deg": 50, "sensor": "thermal", "radiometric": true}],
                 "positioning": "rtk",
                 "param_bounds": {"envelope.ceiling_cm": {"lo": 1000, "hi": 6000}},
                 "param_defaults": {"envelope.ceiling_cm": 4000},
                 "sources": "ASSUMPTION: operator-entered"}
  },
  "units": [
    {"unit_id": "drone-1", "name": "Tello A", "model": "tello",
     "link": {"kind": "wifi_ap", "ssid": "TELLO-5A1B2C", "host": "192.168.10.1"},
     "serial": "0TQDG7REDB1234", "available": true,
     "features": {"birdseye_search": false}},
    {"unit_id": "hex-1", "name": "Hex 1", "model": "site_hex",
     "link": {"kind": "radio", "mavlink_address": "serial:///dev/ttyAMA0:921600"},
     "mounts": {"gimbal": {"mission_pitch_deg": -90}},
     "available": false, "features": {"birdseye_search": true}}
  ]
}
```

- `unit_id` is the drone id used everywhere (the launcher's stricter id rule,
  `launcher/settings.py:63-65`, and the protocol's `is_identifier`).
- **No secrets.** The file never holds a Wi-Fi password, the edge token or any key. The loader
  refuses keys named like `password`, `passphrase`, `secret`, `token` or `key`, and refuses any value
  equal to the edge token, the rule the launcher already applies to settings and presets
  (`launcher/settings.py:330-331`, `:692-696`). An SSID and a serial number are identifiers, not
  secrets.
- **No weakened safety.** A unit's `features` may switch a feature on, or switch off a feature that
  is not a safety switch. Storing `forward_obstacle_sensing: false` is refused (section 10.2).
- **Revision and compare-and-swap.** Every write carries the `rev` it read. The store takes an
  exclusive lock file beside the inventory, compares `rev`, writes `rev + 1` atomically (temporary
  file, fsync, rename, as the ceiling file does), and releases the lock. A mismatch is refused with
  "the inventory changed since you loaded it; reload and try again". This is how the launcher and
  the dashboard (on a phone's behalf) can both write without losing each other's changes.
- Per-session choices stay in the launcher: which local adapter reaches a Tello, the interface index,
  whether the edge runs on this computer, the slot (`launcher/settings.py:88-99`). The adapter
  memory by drone id (`launcher/settings.py:584-590`, `:671-690`) is unchanged.

### 8.2 How the inventory is used

- **No file, no change.** Without `inventory.json` the inventory is implicit: every drone the
  launcher selects, or every id in the ground's `--fleet`, is a unit of model `tello` (or the model
  its edge reports). Every current workflow therefore behaves as today.
- **Fleet planning uses only units in the inventory that are available.** The launcher offers only
  available units as drone rows; the ground's `--fleet` comes from those rows
  (`launcher/settings.py:128-130`). An edge whose reported model differs from its inventory model is
  refused at Plan with both names.
- **Plan uses the reported gates.** The inventory holds what the owner asked for; the edge's
  `describe_unit` reply holds what the unit actually runs (`gates`, section 12.2). Plan decides the
  pattern and each unit's role from the reported gates; before a report, from the inventory.
- **Who edits what:** the ground host's file is authoritative for the fleet. The launcher edits it
  directly on its own computer (units, models, links, features, custom models). Phones, through the
  ground, may only add units of built-in models and toggle availability and non-safety features
  (section 13.2). Changing a unit's model, link or features, defining custom models and removing
  units are laptop-only; a change of model that widens any bound needs the launcher confirmation of
  section 13.1. An edges-only computer keeps its own file for its local units, and the ground learns
  their models from their reports (Q10).

## 9. Capability-gated features

### 9.1 `FeatureSpec` and the two gating phases

Each feature declares `id`, `label`, `description`, a **static** requirement, an optional
**runtime** requirement, `scope` (`unit` or `ground`), `default_on` (when available),
`safety_switch` (switching it off removes a protection: confirmation needed, never stored in a file,
never allowed from a phone), what it selects (a bound parameter such as `sensing.mode`, a
detection-pipeline requirement, a plan pattern and a camera role), and what happens when it is
unavailable.

- **Static phase, at resolve time** (launcher before Start, edge at start, ground at Plan): uses
  only catalogue and inventory facts: the model's frame path, the camera mounts, sensors and declared
  mission pitch, and the backend's declared capabilities.
- **Runtime phase, at the takeoff preflight on the edge**, fail-closed: capabilities that can appear
  or disappear while the edge runs: the autopilot's `ONBOARD_AVOIDANCE` (which can appear after the
  first parameter read and drop later, `edge_device.py:2063-2068`), the gimbal's measured pitch, and
  frames actually arriving. A runtime requirement that does not hold at takeoff refuses the takeoff
  with the feature's reason.

The obstacle rule is: **`sensing.mode = required` needs `forward_obstacle_sensing` (static) or
`onboard_avoidance` (runtime, checked fail-closed at takeoff).** This is today's MAVLink behaviour
and it is kept exactly: with `off` the edge builds a `NullSensor` (`edge_device.py:1987-1990`); a
MAVLink edge without `--source camera` or `url` builds no camera sensor (`:1995`), and
`_tell_mavlink_drone` then sets the drone's sensing mode to `autopilot` or `none`, whose preflight
refuses a takeoff until `avoidance_status()` is empty (`edge_device.py:2060-2085`). A feature
switched on that is statically unavailable is refused at resolve time with its reason; it is never
silently left off. A feature that was never switched on and is unavailable is simply off and shown
with its reason.

### 9.2 Features in v1

| Feature id | Static requirement | Runtime requirement | Selects | When unavailable | Tello | Generic multirotor (default camera) |
| --- | --- | --- | --- | --- | --- | --- |
| `forward_obstacle_sensing` (safety switch) | a mount in the `level` role with an RGB sensor, and a frame path: `drone_video` with the backend's `STEPPED_SENSING` (Tello), or `capture_source` with `--source camera` or `url` (MAVLink) | gimbal pitch measured and within tolerance at every gate, else the gate returns `unavailable` and cuts the lane | `sensing.mode = required` served by the `ForwardCameraSensor` (MiDaS) | if `sensing.mode = required` and `onboard_avoidance` is not declared at takeoff: **refuse the takeoff** (today's MAVLink fallback); if switched on but statically unavailable: **refuse the start** | available, on | available, on, only with `--source camera` or `url` |
| `onboard_avoidance` | backend may declare `ONBOARD_AVOIDANCE` (`controller.py:85-88`) | `ONBOARD_AVOIDANCE` declared and `avoidance_status()` empty at takeoff (`autopilot.py:380-390`) | sensing mode `autopilot` | **refuse the takeoff** when it is the only protection and is missing; otherwise not used | unavailable: "the Tello has no onboard avoidance" | available when configured |
| `level_search` | a mount in the `level` role | none | head-on lanes (`ROOM_SCALE` pattern, `fleet_scale.py:126-133`), detection profile `default` or `level` | **refuse Plan** for a unit that has neither `level_search` nor `birdseye_search`, naming it | available, on | available, on |
| `birdseye_search` | a mount in the `nadir` role | none | lawn-mower lanes at altitude with spacing from the nadir swath (`field_scale`, `coverage.nadir_swath_m`); detection profile `top_down`; nadir coverage maths | switched on but unavailable: **refuse the start and Plan**; Plan also refused unless every planned unit has it on and available (Q13) | unavailable: "the Tello has one fixed forward camera" | unavailable with the default camera; available for a unit that declares a downward mount or a gimbal with mission pitch -60 degrees or lower |
| `thermal_detection` | a camera with sensor `thermal` on a mount whose role the unit's search uses | frames arriving from that camera | a detection profile with `requires_sensor = thermal`; radiometric handling when `radiometric` | switched on but unavailable: **refuse the start**; profiles that need it refuse with `unavailable` | unavailable: "no thermal camera" | per declared camera |
| `appearance_match` | any RGB camera | none | ReID scoring on the ground | **degrade with a banner**: "detections from hex-1 are not scored for appearance (no RGB camera)" | available, on | available, on |
| `claude_second_opinion` | none on the unit; ground provider `claude` and cloud consent (`config.py:115-121`) | the ground's uplink check | ground reasoning | **degrade with a banner**: alerts are not sent to Claude, with the reason | ground setting | ground setting |

One detection pipeline runs per unit in v1: the unit's search role picks one camera (for a dual
sensor, the RGB or the thermal spec of the mount) and one detection profile. Running RGB and thermal
pipelines together is roadmap R5.

The detection profiles gain `requires_sensor` (`rgb` or `thermal`) and `requires_view` (`level`,
`nadir` or `any`; today `view` is informational, `vision/profiles.py:133`). The `top_down` profile is
advisory on a level camera: still selectable, with the warning "this drone has no downward camera;
top_down is meant for views from above", so nothing that works today disappears (Q12). A thermal
profile is hard-gated, because it cannot run on an RGB stream.

## 10. Sources, precedence and validation

### 10.1 The order

Start-time resolution, lowest to highest:

1. `profile`: the model's profile column (or a feature's default).
2. `file`: `~/.flightrisk/params.json` (`FLIGHTRISK_PARAMS_FILE`), sections `defaults`, then
   `models.<model>`, then `units.<unit_id>`; numeric tuning only.
3. `inventory`: the unit's feature switches and mount pitch in `inventory.json`.
4. `launcher`: values the launcher window sets for the processes it starts.
5. `env`: the process's own `FLIGHTRISK_*` variable (canonical or alias).
6. `cli`: a command-line flag.

Runtime: for `on_ground` parameters (ceiling, margin, slot), an accepted `set_ceiling` replaces the
start value while the drone is on the ground, as today (`docs/ceiling-and-altitude.md`, "What every
edge enforces on its own"); source `ground` (section 10.4). Map-editor inputs (strip, area) are
per-plan values; source `plan`.

Why this order: profile and files are slow-changing site knowledge; the inventory is the owner's
per-unit choice; the window is this mission's choice; the environment is the only explicit,
per-process, non-persistent layer, so a person or script that sets `FLIGHTRISK_HOVER_TEST=1` or
`FLIGHTRISK_OBSTACLE_SENSING=off` did it on purpose, and "existing env names must keep working"
means a variable must not lose silently to a window value saved weeks ago. #198 already decided this
way (`flightrisk/launcher/settings.py:496-560` on that branch). A flag already wins over its variable
on the edge (`edge_device.py:2210-2221`). Because the environment wins, the launcher shows every
override, warns, and asks for confirmation when it is less safe (section 10.2).

This reverses one current behaviour: the launcher strips inherited `FLIGHTRISK_GEOFENCE_CM`,
`FLIGHTRISK_CEILING_CM` and `FLIGHTRISK_CEILING_MARGIN_CM` (`commands.py:44-46`), so today the window
wins for those three (Q1; migration note in section 16).

`params.json` format and rules: `{"version": 1, "defaults": {...}, "models": {"<model>": {...}},
"units": {"<unit_id>": {...}}}`; values in registry units; an unknown id, a wrong type, or a file
that exists but cannot be read refuses Start and the edge start, never falling back to defaults
silently (the ceiling-file rule). Every effective value is
`EffectiveValue(id, value, unit, source, source_detail, default, base_default, lo, hi, model,
applies, non_default, less_safe)`.

### 10.2 Files never silently weaken safety; confirmations

- **"Less safe" is measured against the built-in base column**, by `safer`: for a custom model, the
  base model's default, never the custom default. A custom model whose default is less safe than its
  base shows that default as less safe and needs confirmation like any other source.
- **A safety switch can never be stored as off in a file.** `params.json` and `inventory.json`
  refuse `sensing.mode = off`, `sensing.turn = false`, `link.require_failsafes = false`,
  `tello.temp_refuse_c = off` and `forward_obstacle_sensing = false`. Off is a per-session choice
  only: the launcher window with its confirmation, or an environment variable or flag of one
  process.
- **A less-safe value from a file needs an exact confirmation.** The launcher confirms it at Start
  (below) and passes the confirmed values to the edge in `FLIGHTRISK_LAUNCHER_PARAMS`
  (`"confirmed"`). An edge started without a launcher layer refuses a less-safe value that comes
  from a file unless a `--confirm-less-safe <id>=<value>` flag (repeatable) names exactly that id
  and value. Less-safe values from the edge's own environment or flags are accepted, because they
  are explicit for that process, and are logged at warning level.
- **Launcher confirmations.** Start is refused with HTTP 409 and
  `needs_confirmation: "flight_params"` while any of these is unconfirmed: a value of a `confirm:
  less_safe` row that is less safe than its base default, or is its `off_value`; a value of a
  `confirm: non_default` row that differs from its base default; a safety feature switched off; and,
  for `edit: ui` rows, an environment or flag value that is less safe than the window's value, even
  when it is not less safe than the default. The retry carries
  `confirm_params: [{"unit": "drone-1", "id": "envelope.ceiling_cm", "value": 260}, ...]` (`unit`
  is null for ground-scope rows); a confirmation counts only for that unit, id and exact value; a
  preset load or window reload forgets confirmations. This generalises `CONFIRM_SENSING` (#198's
  `launcher/server.py`); `confirm_sensing` stays accepted for one release.

### 10.3 Refusal and constraints

One message form everywhere, for example: `envelope.ceiling_margin_cm = 20 cm (from
FLIGHTRISK_CEILING_MARGIN_CM) is outside the tello bound 30 to 500 cm: the margin covers the
Tello's height error and its overshoot at the end of a climb.` For a feature: `birdseye_search
(from inventory, unit drone-1) cannot be switched on: the Tello has one fixed forward camera.`

The edge exits before it connects to the drone (`edge_device.py:1810-1813` pattern); the launcher
adds an `Issue(ERROR)` (`launcher/settings.py:301-378`); the dashboard refuses the save, the
inventory edit or the Plan. The clamp of finding 7 becomes a refusal. Hard bounds are applied by the
resolver to every configured input; the `SafetyEnvelope` and `FlightGuards` constructors keep only
their structural checks, so tests that build these objects directly are unaffected.

Constraints: `battery.land_pct <= battery.min_takeoff_pct` (`safety.py:376-377`); ceiling minus
margin at least the lowest slot (`safety.py:265-270`); every slot at most ceiling minus margin
(`safety.py:274-285`); `tello.temp_warn_c <= tello.temp_refuse_c` unless off;
`link.loss_land_s >= 1.5 * HEARTBEAT_INTERVAL_S` (`fleet_protocol/constants.py:273-275`), so one
missed heartbeat cannot land a drone; `avoid.sidestep_cm` is 0 or within the step bound
(`obstacle_sensor.py:116-121` on #193); `sensing.step_cm` within the move range
(`edge_device.py:1833-1838`); every `plan.alt_slots_cm` element within the slot bound; **the
distinct values** of `plan.alt_slots_cm` at least `plan.min_slot_separation_cm` apart (repeated
values are allowed because zones are horizontally separated, so the default three- and four-Tello
fleets fly 100, 130, 100 and 130 cm; the field scale keeps its own check, `fleet_scale.py:49-50`);
`sensing.mode = required` only with the obstacle rule of section 9.1; and no mount asked for two
roles (section 6.2).

### 10.4 The ceiling: one value from Start to the air

The three `on_ground` values travel like this, and the dashboard's ceiling file is the single
authority once the ground runs:

1. At Start the launcher resolves ceiling, margin and per-drone slots (environment may win, Q1) and
   writes **the resolved effective values** into the ceiling file (`launcher/ceilingfile.py:38-46`
   writes the window's values today and changes to the resolved ones). The launcher's existing
   "what Start will write" plan (`ceilingfile.py:53-80`) shows them with their sources.
2. Each edge starts with the same resolved values (through the launcher layer, or its own
   environment when started by hand).
3. The dashboard sends `set_ceiling` with the ceiling file's values to every edge on the ground
   whose reported values differ (`docs/ceiling-and-altitude.md`, "The `set_ceiling` command"). With
   step 1 they normally do not differ, so an environment value that won at Start is not silently
   undone by the first `set_ceiling`. An edge started by hand with other values is brought to the
   ceiling file's values, as today, and its card shows that its start value (with its source) was
   replaced by the dashboard's.
4. Later changes on the dashboard's ceiling card are source `ground`. The card asks for confirmation
   for a ceiling raise or a margin decrease relative to the saved file, and for any value less safe
   than the profile default (`envelope.ceiling_cm` has `safer: lower`).

## 11. Seams in the code

### 11.1 The drone adapter and frame metadata

The adapter interface is the existing role protocols (`controller.py:286-392`): connect and
disconnect (`Lifecycle`), commands with `CommandResult` (`FlightControl`, `Motion`, `Navigation`),
telemetry (`TelemetrySource.snapshot`), and video (`VideoSource`). One optional role is added:

```python
@dataclass(frozen=True)
class FrameMeta:
    camera_id: str
    mount: str
    sensor: str               # rgb | thermal
    pitch_deg: float | None   # measured for a gimbal with pitch_measured; the fixed value for a fixed mount; None if unknown
    stamp: FrameStamp | None  # reuses obstacle_sensor.FrameStamp; None means arrival time
    width_px: int
    height_px: int
    channel_order: str        # as obstacle_sensor's SENSOR_CHANNEL_ORDER

class CameraSource(Protocol):
    def cameras(self) -> tuple[CameraSpec, ...]: ...
    def camera_pitch_deg(self, camera_id: str) -> float | None: ...
```

**Frame sources are wrapped, not drones.** `with_meta(source, camera, pitch_reader)` in
`flightrisk/hardware/frames.py` wraps each frame source the edge iterates: `DroneFrameSource`
(`edge_device.py:429`) for the Tello's video, and `ResilientCaptureSource` (`:657`) and
`CV2FrameSource` (`:292`) for a MAVLink capture source. The sensor feed
(`feed_sensor_holder`, `edge_device.py:2028-2044`) tags its frames the same way. So MAVLink frames
are tagged too, and the Tello controller needs no change. The pitch reader is the backend's
`camera_pitch_deg` when it implements `CameraSource`, otherwise the fixed mount's value, otherwise
None (which a gimbal gate treats as "not level").

**Metadata reaches the ground.** Each `detections` message (`EdgeRunner.to_dict`,
`flightrisk/edge.py:276`) gains optional `cam` (camera id) and `pitch_deg` (the frame's pitch), so
the ground can geolocate with the right camera and show which camera saw a person. Older grounds
ignore the keys (ASSUMPTION A8, tested in PR-3).

`BackendSpec` (`registry.py`) gains `adapter` (a catalogue adapter id) and `models` (the model ids it
can drive), both with defaults; `backends.py` registers `tello` with `tello_sdk_udp` and
`("tello", "tello_edu")`, `mavlink` with `mavlink` and `("generic_multirotor",)`, and `sim` with the
Tello models. The edge's `_build_drone` (`edge_device.py:2090-2113`) picks the backend from the
unit's model; `--drone` stays as an override that must agree.

### 11.2 Detection-pipeline selection

`select_pipeline(camera: CameraSpec, role, requested_profile)` in `flightrisk/edge_profiles.py`
returns the one profile to run on the unit's search camera, or a refusal reason. The
`ProfileSwitcher` (`edge_profiles.py:80`) calls it before loading and refuses with the existing nack
reason `unavailable` (`fleet_protocol/constants.py:115`), so older grounds already understand the
refusal. Thermal pipelines (roadmap R1) are new registered profiles.

### 11.3 Where today's Tello code sits

| Module | Role behind the seams | Change |
| --- | --- | --- |
| `drone/tello.py` (`TelloController`) and `drone/tello_bound.py` | The `tello_sdk_udp` adapter and its transport | `DroneCapabilities` built from the catalogue (`tello.py:568-576`); the 24 SDK constants become reads of the read-only `tello_sdk.*` rows with the same values. No logic change. |
| `drone/lane_flight.py`, `lane_executor.py`, `lane_monitor.py`, `lane_avoid.py` | Adapter-neutral executor; branches on `Capability` | Reads resolved parameters; the sense gate adds the gimbal pitch check (a no-op for a fixed mount); `describe_unit` is answered by the edge's link code, not the executor. |
| `drone/obstacle_camera.py` (`ForwardCameraSensor`) | The forward-camera obstacle pipeline | None. `_build_obstacle_wiring` (`edge_device.py:1955-2025`) builds it only when `forward_obstacle_sensing` is available and on. |
| `drone/mavlink_fleet.py` | The `mavlink` adapter | `MavlinkFleetOptions` built from resolved values. |
| `fleet_scale.py`, `coverage.py`, `geolocate.py` | Plan pattern and coverage maths | Camera models built from the catalogue; bird's-eye pattern selected by the reported gates. |
| Android `TelloConnection.kt`, `DroneManager.kt`, `SearchPattern.kt`, `config/FlightRiskConfig.kt` | The Kotlin `tello_sdk_udp` adapter on the phone | Read the generated Tello column (section 13.2); `SearchPattern` refuses or merges a segment shorter than the minimum move instead of clamping it (finding 13). |

## 12. Agreement between processes, and the wire

### 12.1 Who resolves

- **Launcher, before Start:** for each edge it starts, resolves exactly the layers that child will
  see (model, local params file, local inventory, window, inherited environment), shows the table,
  turns refusals into errors and overrides into warnings, asks for confirmations, writes the
  resolved ceiling values (section 10.4), then starts each edge with one variable,
  `FLIGHTRISK_LAUNCHER_PARAMS`, holding compact JSON `{"unit_id": "drone-1", "model": "tello",
  "values": {...}, "features": {...}, "confirmed": {...}}` with only what the window sets and what the
  operator confirmed. The variable joins `CONTROLLED_ENV` (`commands.py:27`), so an inherited one is
  always removed and the label `launcher` is truthful. The launcher stops setting the ceiling, margin
  and geofence variables and stops passing `--alt-slot` (`commands.py:257`, `:265-267`). The ground
  process gets the same variable with ground-scope values and the geofence.
- **Edge, at start:** resolves the same layers plus its flags with the same pure function, refuses
  to start on any refusal, logs each non-default value with its source, builds `SafetyEnvelope`,
  `FlightGuards`, `TempLimits`, the obstacle and avoidance settings, `MavlinkFleetOptions`, the
  feature-selected sensor, the frame-source wrapping and the pipeline, and computes `unit_rev`.
- **Edge, at takeoff:** checks the runtime requirements of section 9.1, fail-closed.
- **Ground, at Plan:** resolves ground-scope values; uses each unit's reported model, values and
  gates, intersected with its own registry, for that unit's bounds, role and pattern; uses the
  reported `battery.min_takeoff_pct` in the start gate.

### 12.2 `describe_unit`: a read-only query, not a configuration command

Queries are a new message set, `QUERY_TYPES = frozenset({"describe_unit"})` in
`fleet_protocol/constants.py`, separate from `CONFIG_COMMAND_TYPES` (`constants.py:110-111`). A
query changes nothing on the edge, and the rules differ from commands:

- **Any ground may ask.** In multi-ground mode the `EdgeAuthority` (`fleet_authority.py:236-270`)
  handles a query before any authority or fence check and returns it as handled with
  `feed_watchdog=False`. In single-ground mode `_apply_control` (`edge_device.py:755-775`), which
  today feeds the watchdog for every message (`:768-770`), skips the feed for query types.
- **A query never feeds the link watchdog**, from any ground, so a standby that keeps asking cannot
  delay a link-loss landing (requirement PROTO-6 tests it).
- **Its own command-id cache:** a small per-connection cache (the last 8 query `cmd_id`s and their
  answers) kept on the connection, never the authority's shared cache (`fleet_authority.py:130`), so
  queries cannot evict the record of a flight command.
- **Rate limit:** at most one answered query per connection per 2 s; a faster query gets a `nack`
  with reason `busy` and changes nothing.
- **Logging:** `flightrisk/obs_comm.py` logs queries and their replies as aggregated traffic, like
  telemetry, so a chatty ground cannot flood the log.

The reply is an `ack` with `for: "describe_unit"`, `unit_rev`, and:

- `unit`: `{"model", "model_name", "adapter", "scale", "positioning", "link", "cameras": {"<id>":
  {"mount", "mount_kind", "orient", "pitch_min_deg", "pitch_max_deg", "role", "w_px", "h_px",
  "hfov_deg", "sensor", "radiometric"}}}`, where `link` is only the kind (`wifi_ap`,
  `wifi_station` or `radio`), never an SSID, host or address;
- `gates`: `{"<feature id>": {"on", "ok", "why", "src"}}` (the wire key is `gates` because
  `features` is a forbidden key, finding 14);
- `values`: `{"<param id>": {"v", "src", "d", "lo", "hi"}}`.

Bounds and cameras are objects, not lists, because every list of numbers counts against
`MAX_LISTED_NUMBERS = 64` (`fleet_protocol/constants.py:259-262`, `checks.py:268-280`). About 75
drone-scope values at about 70 bytes plus the unit is about 6 KB, far below `MAX_MESSAGE_BYTES`
(`constants.py:316`); the nesting depth is 4, below `MAX_DEPTH = 8`; texts are cut to
`MAX_TEXT_LEN = 200` (`constants.py:298`) and never contain the token.

### 12.3 `unit_rev` in the `hello`

`unit_rev` is the first 12 hex digits of SHA-256 over the canonical JSON of the **start-time**
resolution only (model, cameras, gates and values as resolved at start; runtime `set_ceiling`
changes are not hashed, they already travel in telemetry as `ceiling_cm`, `ceiling_margin_cm` and
`alt_slot_cm`). It is an optional key of the protocol v2 edge `hello` (`EdgeHello`,
`fleet_protocol/fencing.py:177-200`). The edge sends a `hello` on every connection, so the value is
re-sent on every reconnect. No telemetry field is added (ASSUMPTION A6).

- The ground sends `describe_unit` when a `hello` carries a `unit_rev` it has not described.
- **Plan requires the latest description:** for every unit whose `hello` advertised a `unit_rev`,
  the stored reply's `unit_rev` must equal the latest advertised one; otherwise Plan is refused with
  "waiting for drone-2's description; press Plan again in a moment", as for the ceiling sync.
- **The ground intersects reported bounds with its own registry** for the reported model, so an edge
  can never make the ground plan beyond the ground's own bounds; a reported value outside the
  intersection refuses Plan for that unit with both bounds.
- **Compatibility:** an older ground ignores the unknown `hello` key and never asks. An edge on a
  protocol v1 link, or an older edge, sends no `unit_rev`: the dashboard shows "not reported" and
  plans with the inventory model's bounds (else `tello`), which is today's behaviour; the edge still
  enforces its own values and refuses what it cannot fly.

## 13. Dashboard, launcher and mobile

### 13.1 Dashboard and launcher

- **Dashboard:** a "Fleet hardware" card (per unit: model, camera mounts with role and sensors,
  positioning, link kind, each feature with available, on, source and reason, and the banners of
  section 9.2) and a "Flight parameters" card (per unit and for the ground: value, unit, source badge
  with detail, default, bound, when it applies; non-default rows highlighted, less-safe rows marked;
  the read-only `tello_sdk.*` rows in a collapsed group). Data through new Socket.IO requests
  `get_inventory` and `get_flight_params`, answered by `inventory` and `flight_params`.
- **Launcher:** an inventory editor (units of built-in or custom models; custom models only here),
  drone rows chosen from available units, per-unit feature switches and gimbal mission pitch, the
  effective-values table for the edges it starts, the existing edit fields (ceiling, margin,
  geofence, slot, sensing mode, minimum safe depth), and the confirmations of section 10.2. Changing
  a unit's model to one with any wider bound needs the same 409 confirmation, listing every widened
  bound. The launcher starts only Tello-family and fake units (`launcher/settings.py:41`); MAVLink
  units show "runs on its companion computer".

### 13.2 Mobile contract and apps

What crosses to the phones, all additive (apps ignore unknown keys at every depth,
`docs/mobile-contract.md:134`):

- New events a phone handles: `inventory` (units, the catalogue summary of their models, per-unit
  feature availability with reasons, `rev`, `editable` and `locked_reason`), `flight_params`
  (read-only table) and `inventory_ack` (`ok`, `reason`, `rev`).
- New events a phone may emit (added to the allow-list, `docs/mobile-contract.md:86-104`):
  `get_inventory`, `get_flight_params`, `inventory_add_unit {rev, unit}` (a new `unit_id` of a
  **built-in** model only; refused for an existing id or a custom model),
  `inventory_set_available {rev, unit_id, available}` and
  `inventory_set_feature {rev, unit_id, feature, on}` for non-safety features only. The ground
  refuses any of them while a drone flies, a start is scheduled, or this ground is a standby, as it
  does for ceiling changes; refuses a stale `rev`; and refuses switching a safety feature from a
  phone in either direction ("change it on the laptop"). Editing a unit's model, link or features
  other than these, removing units and defining custom models are laptop-only (Q11).
- `ceiling_settings.limits` keeps its five keys (`dashboard/ceiling.py:680-686`, decoded by iOS
  `Models.swift:529-538` and Android `SettingsModels.kt:148-158`), with the intersection of the
  fleet's bounds; each `ceiling_settings.drones[]` entry may gain `model` and `limits`.
- Fixtures: `inventory.json`, `flight_params.json`, `inventory_ack.json` and new
  `client_emits.json` cases, generated by Python like the others (`docs/mobile-contract.md:12-52`),
  plus `catalogue.json` (the built-in models) and `tello_params.json` (the `tello` column: every row
  with its default, bound and unit).

What each app needs:

- **iOS** (operator console only, never an edge, `docs/mobile-contract.md:7-10`): decoders for the
  three events, an inventory screen (add a unit of a built-in model, availability, non-safety feature
  switches), and the read-only parameters table.
- **Android:** the same screens; plus `catalogue.json` and `tello_params.json` bundled as assets.
  `DroneConfig`'s Tello values (`FlightRiskConfig.kt:186-205`) are generated from `tello_params.json`
  instead of being mirrored by hand, and the direct Tello stack takes its move range, keepalive,
  command timeout and landing battery from it, with a unit test that fails when an asset differs
  from its fixture. This resolves finding 15: the standalone mode lands at `battery.land_pct` (15 %),
  like the edge, instead of at the 10 % critical alert level (section 17). The standalone
  direct-Tello mode keeps one local unit record (model `tello`, SSID, name) in the app's settings,
  because it works without a ground. Phone-as-edge is unchanged: it flies nothing and sends only
  `detections` without a `drone_id` (`docs/mobile-contract.md:179-194`).

## 14. The guard test

File `tests/test_param_literals.py`, with its allowlist in `tests/param_literal_allowlist.py`. The
rule is prototyped in `guard_rule_prototype.py.txt` beside this document.

**Scope:** `flightrisk/drone/*.py`, `flightrisk/edge_device.py`, `flightrisk/edge_profiles.py`,
`flightrisk/launcher/*.py`, `flightrisk/dashboard/*.py`, `flightrisk/fleet_protocol/constants.py`.
Excluded: `flightrisk/params/` and `flightrisk/hardware/` (the tables themselves), and `tests/`.

**Name words** are derived from the registry, not hand-listed: every token of length three or more
of every parameter id (split on `.` and `_`), minus unit tokens, plus a fixed list of time and count
words (`timeout`, `delay`, `interval`, `budget`, `lag`, `retries`, `retry`, `attempts`, `readings`,
`samples`, `votes`, `tolerance`, `threshold`, `radius`, `width`, `depth`, `speed`, `distance`, `gap`,
`offset`, `height`, `alt`, `altitude`, `fov`, `pitch`). A name is unit-named when it ends in a unit
suffix (`_cm`, `_mm`, `_m`, `_km`, `_s`, `_ms`, `_deg`, `_dps`, `_mps`, `_pct`, `_percent`, `_c`,
`_hz`, `_cm_s`) or contains a name word, and is not exempt
(`(_px|_bytes|_chars|_len)$` or exactly `max_depth`, `frame_height`, `frame_width`,
`match_threshold`). A new registry row therefore widens the guard automatically.

**Rules.** A finding is:

- (A) an `int` or `float` literal (not a `bool`; a unary minus is folded in; `0` and `0.0` exempt as
  the "off" and origin sentinel) that is the whole value, or a numeric element of a tuple value,
  bound to a unit-named target: an assignment or annotated-assignment target, a function parameter
  default, or a keyword argument;
- (B) such a literal as an operand of an ordering comparison whose other operand is a unit-named
  name or attribute;
- (C) the `default=` of an `add_argument` call whose option name is unit-named, including a numeric
  string such as `"75"`;
- (D) the default of an environment read, `<mapping>.get(NAME, default)` or
  `os.getenv(NAME, default)`, where `NAME` is a `FLIGHTRISK_*` string or an `ENV_*`/`*_ENV` constant
  whose stem is unit-named, and the default is numeric or a numeric string (this catches
  `dashboard/__main__.py:141` and `:146`);
- (E) the `default=` of a `dataclasses.field()` call bound to a unit-named target;
- (F) a `bool` or `str` literal that is a definition-time default of a safety-named target
  (`failsafe`, `require`, `sensing`, `hover`, `avoid`, `refuse`, `obstacle`, `geofence`, `ceiling`,
  `land`): a function parameter default, a class-body field, or a module-level `DEFAULT_*` name;
  never a state flag assigned inside a function, an enum member, an `ENV_`/`EVENT_` name, or a text.

**Allowlist.** Keyed by `(file, enclosing scope, kind, callee or target, name, value)` with the
exact count of findings for that key; each key maps to `(category, reason)`. Categories: `sdk`,
`protocol`, `regulatory`, `reference`, `internal`, `legacy`, `test_double`, `vision`, `map`,
`process`, `bound` (today's accepted range of an environment reader; the parity test pins it and C5
replaces it with the registry bound), `deferred` (belongs in the registry but is not migrated yet;
names its follow-up). An entry whose name or value duplicates a registry row's value must be
`deferred`; one that is a row's accepted range may be `bound`; a reviewed exceptions list in the
checker holds the coincidental matches. Keys
use names and values, never line numbers, so moving code does not churn the list; a changed value or
a changed count fails until the allowlist changes in the same diff (Q8). Sections are separated by
file, so PRs that migrate different files edit different sections. `tests/param_literal_allowlist.py`
has a code owner (section 7.2).

**Test cases.** (1) Every finding in scope is allowlisted with its exact count. (2) Every allowlist
key still matches its count of findings (no stale entries; the migration PRs must delete what they
migrate). (3) The checker reports a planted literal for each rule in a temporary file:
`RETRY_HOLD_S = 2.5`, `foo(timeout_s=3)`, `if height_cm > 30:`,
`add_argument("--gap-cm", default="40")`, `os.environ.get("FLIGHTRISK_GAP_CM", "40")`,
`gap_cm: int = field(default=40)` and `def f(require_failsafes=False)`, as
`tests/test_architecture.py:83-101` does for imports. (4) Every `deferred` entry names a follow-up.
(5) Adding a registry row whose id contains a new word makes the checker report an existing literal
with that word (the derivation works).

**Measured on `a96c6b1`** with the prototype and the ids of section 20: see the figures recorded in
section 15. Known blind spots, covered by review: positional arguments (`_DefaultSlot(100.0)`,
`edge_device.py:1614`), literals inside arithmetic, and names without a unit or name word.

**Kotlin.** PR-7 adds `tests/test_kotlin_literals.py`, a regular-expression check over
`mobile/app/src/main/java/com/flightrisk/app/drone/*.kt` and the `DroneConfig` class of
`mobile/app/src/main/java/com/flightrisk/app/config/FlightRiskConfig.kt`: a `const val`, a data-class
default or a named argument whose camelCase name contains a name word and whose value is a numeric
literal is a finding unless allowlisted. It runs in the Python test suite, needs no Gradle, and its
limits (no type information, comments are stripped first) are documented in the test.

**Registry and catalogue self-tests (PR-1 and PR-2):** `table.py` equals the golden file value by
value; every default lies within its bound for every profile; every constraint holds for every
profile's defaults; ids are unique and pass the privacy key screen; environment names are unique
across rows; the `tello` move bounds and every `tello_sdk.*` row equal the constants the Tello driver
uses; every catalogue fact has a source or ASSUMPTION; every feature returns a reason when
unavailable; the generated docs equal the committed `docs/flight-parameters.md` and
`docs/hardware-catalogue.md`; and the generated `catalogue.json` and `tello_params.json` fixtures
equal the committed ones.

## 15. Classification and counts

Each inventoried value is in one of four classes:

- **A, airframe parameter:** its sensible default or safe range depends on the aircraft. In the
  registry, a column per profile, editable within the bound.
- **R, profile fact:** airframe-specific but fixed by the aircraft, its SDK or its driver (`lo ==
  hi`). In the registry so it is shown with its source and so Tello facts stop leaking into neutral
  code; read-only. The Tello SDK's 20 to 500 cm move range is the example: it is the `tello` column of
  `move.min_cm` and `move.max_cm` ([Tello SDK 2.0 User Guide](https://xcopter.com/download/Tello%20SDK%202.0%20User%20Guide.pdf),
  "forward x: x = 20-500"). Since the user asked that every Tello height, distance and timing be
  shown, the 24 Tello SDK and driver facts that were constants in the first draft are now R rows 87
  to 110 (section 23, decision D-F18).
- **M, mission or operator parameter:** not airframe-specific; same default for every profile.
- **C, constant:** a protocol fact, a MAVLink enumeration, a regulatory limit, reference data, a
  software internal, legacy code, or a test double. Stays in code, allowlisted with its category.

Counts:

- **110 registry rows:** 58 A, 32 R, 20 M (section 20). Airframe-specific (A plus R): 90.
- **65 constant entries** (section 21): 19 protocol, MAVLink and regulatory facts (15 protocol,
  2 MAVLink, 2 regulatory), 32 software internals, 7 algorithm or reference values, 6 legacy or
  out-of-scope groups, 1 test-double group.
- **175 values inventoried** in all.
- The catalogue: 3 built-in models plus custom models; 7 features; 9 descriptor fields per camera.
- **Guard on `a96c6b1`** (rules A to F, the scope of section 14, name words from the 110 ids):
  486 findings with 476 distinct allowlist keys, in 55 of the 74 files in scope (316 assignments,
  73 keyword arguments, 62 parameter defaults, 25 comparisons, 4 environment defaults, 2 argparse
  defaults, 4 safety defaults). The first draft's narrower rule found 328 in 37 of 54 files.
  `guard_main.txt` beside this document lists every finding.

## 16. Migration of existing and in-flight names

Every existing variable keeps its name and meaning; the registry maps it to a parameter id.

| In-flight work | What it adds | How it folds in |
| --- | --- | --- |
| `fix/geofence-slot-battery-consistency` (prerequisite) | `min_takeoff_battery_from_env` in `safety.py`; the ground gets `FLIGHTRISK_GEOFENCE_CM`; `fleet_patterns` takes the coordinator's slots | Becomes the resolver's battery read in PR-3; the golden file pins 450 for the Tello geofence (section 17 item 1) and `(100, 130)` for the zone slots. |
| Incident fixes | `FLIGHTRISK_TELLO_SETTLE_SAMPLES`, `_SETTLE_TOLERANCE_CM`, `_SETTLE_TIMEOUT_S`, `_TAKEOFF_LATE_S`, `_REFUSAL_RETRIES`, `_REFUSAL_HOLD_S`, `FLIGHTRISK_HOVER_TEST` (`flight_guards.py:68-74` on the branch); `MIN_ALT_SLOT_CM = 50` (`safety.py:131` on the branch); `DESCENT_CLEARANCE_CM` (`flight_guards.py:107`); `_TOF_OUT_OF_RANGE_CM` (`tello.py:169`) | Rows 25 to 33 and 108; the 50 cm floor becomes the `tello` lower bound of `envelope.alt_slot_cm`; `FlightGuards` is built from resolved values. |
| #193 | `FLIGHTRISK_OBSTACLE_CLEAR_VOTES`, `_RELOOK`, `_SIDESTEP_CM`, `_SIDESTEPS`, and `OBSTACLE_SIDESTEP_PASS_CM` (`obstacle_sensor.py:106-148` on the branch) | Rows 54 to 58; `0` is the `off_value` of `avoid.sidestep_cm`. |
| #198 | Launcher fields `obstacle_sensing` and `obstacle_min_safe_depth` (`launcher/settings.py:146-148` on the branch); `FLIGHTRISK_TELLO_TEMP_WARN_C` and `_REFUSE_C` (`temp_preflight.py:36-47` on the branch) | Rows 40, 43, 37, 38. Saved presets keep their keys. `EffectiveObstacle` and `sensing_warning` become wrappers over the resolver and the generic confirmation; `window` and `environment` map to `launcher` and `env`. The launcher's sensing mode is the UI of the `forward_obstacle_sensing` feature. |
| Floor-ratio opt-in | `FLIGHTRISK_OBSTACLE_METRIC` (`score` or `floor_ratio`) and `FLIGHTRISK_OBSTACLE_FLOOR_RATIO_MAX` (0.80, 0.5 to 1.5) (`obstacle_sensor.py:132-145` on the branch) | Rows 53 and 85. Switching the metric away from `score` is `confirm: non_default`. |
| #200 | `flightrisk/dashboard/single_search.py`, whose start gate imports `MIN_START_BATTERY` from the coordinator (the prerequisite removes that constant; whichever merges second resolves it) | PR-5 owns `single_search.py` and makes it use the resolved `battery.min_takeoff_pct`. |
| #191 | `FLIGHTRISK_CLAUDE_BREAKER_THRESHOLD`, `_BREAKER_COOLOFF`, `_MAX_CALLS_PER_MINUTE` and the client timeouts | Not parameters: ground-side service limits with no airframe meaning; they stay in `FlightRiskConfig` (`config.py:1-27`). The `claude_second_opinion` feature only shows whether the ground's provider and consent allow it. |
| #192 (merged) and the metrics follow-ups | Band fractions (`obstacle.py`) and the offline report | Class C (algorithm shape). |
| #196, #199, #201 | Demo modes and Windows fixes | No flight values. |

**Migration notes for operators** (go into `docs/launcher.md` and `docs/fleet-setup.md` with PR-6):

- **Presets** (`~/.flightrisk/launcher.json`): version 1 is unchanged. Old presets load as before;
  their drones become implicit `tello` units, and new optional per-drone `unit_id` and `model` keys
  are added only when the preset is saved again. Older launchers ignore those keys
  (`launcher/settings.py:168`).
- **Ceiling file** (`~/.flightrisk/ceiling.json`): the format is unchanged. From PR-6 the launcher
  writes the resolved values at Start (section 10.4), so a value from the environment now reaches
  the file; the launcher's Start plan shows it.
- **Shell exports that now win (Q1):** a `FLIGHTRISK_GEOFENCE_CM`, `FLIGHTRISK_CEILING_CM` or
  `FLIGHTRISK_CEILING_MARGIN_CM` exported in a shell profile was ignored by the launcher until now and
  wins from PR-6. The launcher shows each such variable as a warning with its value before Start, and
  asks for confirmation when it is less safe than the window's value. To return to the window's
  value, unset the variable and restart the launcher.
- **New files** (`params.json`, `inventory.json`) are optional; without them nothing changes.

## 17. Behaviour changes and tightened bounds (complete list)

Every change below is deliberate. Anything refused today stays refused, and anything accepted today
stays accepted, except what this list names; the parity test (below) enforces it.

**Defaults that change:**

1. **Tello geofence default 500 to 450 cm** for an edge started without a launcher (Q2); the default
   400 by 400 cm plan stays inside it. Launcher-started edges already use 450.
2. **Android standalone Tello mode lands at 15 % instead of 10 %** (finding 15).

**Values accepted today that are refused (new hard bounds):**

3. `FLIGHTRISK_GEOFENCE_CM` below 40 or above 5000 (today any positive value, `safety.py:366-371`
   with `max_geofence_cm = inf`).
4. `FLIGHTRISK_FIELD_GEOFENCE_M` and `--geofence-m` below 5 m (today any positive value up to
   500 m).
5. `FLIGHTRISK_CEILING_MARGIN_CM` above 500 (today only the ceiling-minus-margin check limits it).
6. `FLIGHTRISK_FIELD_CEILING_MARGIN_M` and `--ceiling-margin-m` above 30 m.
7. `FLIGHTRISK_MIN_TAKEOFF_BATTERY` below 20 or above 95 (today 0 to 100).
8. `FLIGHTRISK_LINK_LOSS_LAND_S` below 1.5 s, or above 10 s for a Tello and 30 s for a MAVLink
   drone (today any positive value).
9. `FLIGHTRISK_FIELD_LINK_LOSS_HOLD_S` and `--link-loss-hold-s` above 60 s (today any value of 0 or
   more).
10. `FLIGHTRISK_MANUAL_ORPHAN_LAND_S` below 30 s is refused instead of raised to 30 s (finding 7),
    and above 600 s is refused.
11. `FLIGHTRISK_FLEET_STRIP_CM` and `--fleet-strip-cm` below 20 or above 1000 cm (today the
    coordinator accepts any positive value, `coordinator.py:731-732`; the map editor already uses 20 to
    1000, `plan_edit.py:152`), and `FLIGHTRISK_FLEET_AREA_CM` and `--fleet-area-cm` with a side below
    20 or above 20 000 cm (today any positive side, `dashboard/__main__.py:39-50`), are refused when
    the dashboard starts.
12. `--speed-mps` below 0.5 m/s (today any positive value up to 15).

**New refusals that cannot affect a working setup today** (new files, new features, new cameras):

13. A safety switch stored as off in `params.json` or `inventory.json`; a less-safe value from a file
    on a hand-started edge without `--confirm-less-safe`.
14. A thermal detection profile on a unit without a thermal camera; a feature switched on that is
    unavailable; a mount asked for two roles; a gimbal sense gate whose measured pitch is not level.
15. A unit whose reported model differs from its inventory model, a stale `describe_unit` reply, an
    empty intersection of bounds, and a mixed `room` and `field` fleet, at Plan.

**Other behaviour changes:**

16. The coordinator and the nudge check use each unit's move range (finding 5); a Tello is unchanged.
17. `battery.land_pct` becomes configurable through `FLIGHTRISK_LAND_BATTERY` (finding 4).
18. Inherited `FLIGHTRISK_GEOFENCE_CM`, `FLIGHTRISK_CEILING_CM` and `FLIGHTRISK_CEILING_MARGIN_CM`
    win over the launcher window, with a warning and, when less safe, a confirmation (Q1); the
    launcher writes the resolved ceiling values into the ceiling file.
19. Android's `SearchPattern` refuses or merges a segment below the minimum move instead of
    stretching it to 20 cm (finding 13).
20. `top_down` on a level camera now shows an advisory warning (no refusal).
21. `describe_unit` and its replies appear in the aggregated `drone_comm` log events.

**Existing assertions the PRs may edit, and nothing else.** If any other existing assertion fails,
the PR stops and the change is split out for its own review.

- PR-3: `tests/test_safety.py:206`, `(500, 30, 15)` becomes `(450, 30, 15)` (item 1). Symbolic
  assertions that follow `DEFAULT_GEOFENCE_CM` (`tests/test_fleet_plan_check.py:354`,
  `tests/test_field_scale.py:59`, `:100`, `tests/test_fleet_plan_gate.py:202`) keep passing without
  an edit. The tests that set `FLIGHTRISK_LINK_LOSS_LAND_S=1.5` (`tests/test_safety.py:193`,
  `tests/test_edge_uplink.py:206`) stay valid because the bound starts at 1.5 s.
- PR-5: `tests/test_manual_ground.py:415`, the cases `("5", 30.0)` and `("0", 30.0)` become
  refusals (item 10).
- PR-6: `tests/test_launcher_commands.py:67` (`--alt-slot` is no longer passed) and `:119-121` (the
  three variables are no longer set; the values appear in `FLIGHTRISK_LAUNCHER_PARAMS` instead), and
  the assertion the prerequisite fix adds to the same file for the ground's
  `FLIGHTRISK_GEOFENCE_CM`.
- PR-7: Android tests that assert a 10 % auto-landing, if any (item 2).

**Parity test** (`tests/test_param_parity.py`, added by PR-1 and extended by PR-3, PR-5 and PR-6):
for every environment alias and flag, the golden file's `today_accepts` records the range today's
code accepts. The test probes, for each alias, the values just below and at each end of both the
old and the new range, a typical value and a malformed value, and asserts that the resolver accepts
exactly what today's code accepts, except the probes listed in a `PARITY_EXCEPTIONS` table whose
every entry names its item number above. A difference without an entry fails.

## 18. Risks

- **The edge rewiring touches every safety value.** The golden file pins every value before PR-3
  moves any code; the parity test pins acceptance; PR-3's acceptance includes the existing safety,
  ceiling, guard, obstacle and telemetry suites with only the edits listed in section 17.
- **A standby ground or a buggy client floods queries.** Queries never feed the watchdog, are rate
  limited per connection, use their own cache and are aggregated in the log; PROTO-6 tests that a
  standby's queries do not delay a link-loss landing.
- **A gimbal pointed down while forward sensing is on.** Roles are exclusive per mount, a gimbal is
  level only with measured pitch, and every gate cuts the lane when the pitch is not level.
- **Precedence surprises.** Every override is visible before Start and on the dashboard; less-safe
  overrides, including an environment value less safe than the window's, need confirmation.
- **Inventory drift and lost updates.** The ground host is authoritative, writes are
  compare-and-swap on `rev`, edges report their model, and a mismatch refuses Plan.
- **Scope.** The first two PRs are pure; the mobile PRs come last and can slip without blocking the
  Python work.
- **Allowlist size.** Grouped by file and category, with exact counts; stale entries fail, so it only
  shrinks unless a reviewer accepts an addition.

## 19. Open questions

All fourteen were decided on 2026-11-22 under the user's standing instruction to proceed with the
recommended options (section 23). They are kept here with their options so a later change has the
context. Q5, Q9 and Q11 were revised after the round 1 review.

- **Q1. Environment versus launcher window.** (A) The environment wins everywhere; the launcher stops
  stripping the three ceiling and geofence variables and warns instead. (B) The window wins
  everywhere. (C) A per-parameter mix. Recommendation: A.
- **Q2. One Tello geofence default.** (A) 450 cm everywhere. (B) 500 as the profile default and 450
  as the launcher preset value. Recommendation: A.
- **Q3. `tello_edu`.** (A) Its own catalogue entry sharing the `tello` column. (B) One entry with a
  variant flag. (C) Its own entry and column. Recommendation: A; no flight value differs (both
  publish 30 m maximum height, `coverage.py:61-65`), but the SDK facts do.
- **Q4. Tello driver thresholds (rows 24, 34-36, 87-110).** (A) Read-only profile facts, shown with
  their source. (B) Editable within narrow bounds. (C) Constants outside the registry.
  Recommendation: A; they drive the landing and takeoff state machines.
- **Q5. How the ground learns each unit.** (A) `unit_rev` in the protocol v2 `hello` plus the
  `describe_unit` query. (B) `unit_rev` as a telemetry field. (C) The table in telemetry. (D) No
  report. Recommendation: A; it costs no telemetry field and is re-sent on reconnect.
- **Q6. Launcher edit surface for parameters.** (A) Existing fields plus the inventory, model,
  feature switches and gimbal mission pitch. (B) Also battery and link-loss fields. Recommendation: A.
- **Q7. Custom models in v1.** (A) Include them, narrowing only, defined on the laptop. (B) Defer.
  Recommendation: A.
- **Q8. Allowlist key.** (A) File, scope, kind, callee or target, name and value with exact counts.
  (B) Name only. Recommendation: A.
- **Q9. Who may send `describe_unit`, and how.** (A) Any connected ground, as a read-only query
  outside `CONFIG_COMMAND_TYPES`: never feeding the watchdog, its own per-connection cache, rate
  limited. (B) Only the authority, as a configuration command. Recommendation: A.
- **Q10. Where the inventory lives.** (A) One file on the ground host with a revision and
  compare-and-swap writes, edited by the launcher there and, within limits, by phones through the
  ground; Android also keeps one local unit for its standalone Tello mode. (B) Per-device
  inventories that sync. (C) Phones read-only. Recommendation: A.
- **Q11. What a phone may change.** (A) Add units of built-in models, toggle availability, and toggle
  non-safety features; nothing else (model, link and feature edits of existing units, removals and
  custom models are laptop-only). (B) Also edit existing units. (C) Nothing. Recommendation: A.
- **Q12. `top_down` on a level camera.** (A) Advisory. (B) Hard gate. Recommendation: A.
- **Q13. Mixed patterns in one fleet.** (A) Bird's-eye only when every planned unit supports and
  enables it. (B) Per-zone patterns. Recommendation: A for v1; B is roadmap R4.
- **Q14. Default camera of `generic_multirotor`.** (A) Fixed forward RGB, matching what the edge
  builds today. (B) No default. Recommendation: A.

## 20. and 21. Tables

Moved to `parameters.md`, which is the single source for every row and constant. Rows 53 and 85
(floor ratio) are removed because the floor ratio is tabled.

## 22. External sources

- Tello SDK 2.0 User Guide, move commands 20-500 cm:
  https://xcopter.com/download/Tello%20SDK%202.0%20User%20Guide.pdf
- Tello User Manual v1.2 (2018.04): p.4 maximum flight time about 13 minutes and range 100 m; p.5
  failsafe landing after 50 s of lost app signal; p.7 Vision Positioning System effective at 0.3 to
  10 m, best at 0.3 to 6 m: https://www.adorama.com/col/productManuals/RYTELO.pdf
- Tello EDU specifications (maximum flight height 30 m, 8 m/s, 13 minutes, 82.6 degree field of
  view): https://core-electronics.com.au/dji-tello-edu.html.md
- 14 CFR 107.51, 400 ft operating limit:
  https://www.ecfr.gov/current/title-14/chapter-I/subchapter-F/part-107/subpart-B/section-107.51

## 23. Decisions (2026-11-22, revised after round 1)

The user asked the orchestrator to proceed with its recommended options without waiting for input.
Every open question is therefore settled on the recommendation of section 19: Q1 the environment wins
everywhere; Q2 the Tello geofence default is 450 cm; Q3 `tello_edu` is its own catalogue entry sharing
the Tello parameter column; Q4 the Tello ground-detection thresholds, the takeoff reply timeout and the
SDK and driver facts are read-only; Q5 `unit_rev` travels in the protocol v2 `hello` and the ground
asks with the `describe_unit` query (no telemetry field); Q6 the launcher edits the existing fields
plus inventory, model, feature switches and gimbal mission pitch; Q7 custom models are in v1, narrowing
only; Q8 the allowlist is keyed by file, scope, kind, callee or target, name and value with exact
counts; Q9 any connected ground may send `describe_unit`, which is a read-only query outside
`CONFIG_COMMAND_TYPES`, never feeds the link watchdog, has its own per-connection command-id cache and
is rate limited; Q10 one inventory file on the ground host with a revision and compare-and-swap writes,
phones edit within limits through the ground, and Android keeps a local unit for standalone Tello mode;
Q11 phones may add units of built-in models and toggle availability and non-safety features, and
nothing else (editing a unit's model, link or features, removing units and defining custom models are
laptop-only, with a confirmation when bounds widen); Q12 `top_down` on a level camera is an advisory
warning; Q13 bird's-eye search only when every planned unit supports and enables it; Q14 the generic
MAVLink default camera is fixed forward RGB.

**D-F18. Every Tello height, distance and timing is configurable or shown.** The user asked that every
such Tello value be configurable and shown. Under the same standing instruction the recommended option
is accepted: the 24 Tello SDK and driver constants listed as constants in the first draft become
read-only registry rows 87 to 110, shown in the effective-values tables with their source and never
editable, because they are facts of the Tello SDK or of the landing and takeoff state machines built
around it. Every other Tello value is an editable row within its hard bound.

Any of these can be revisited before the matching PR starts.
