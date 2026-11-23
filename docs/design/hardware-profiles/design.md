# Issue #197: hardware catalogue, inventory, capability-gated features and flight parameters

Status: design for review by a three-agent consensus. Nothing here has been implemented.
Base: `origin/main` at `ebe9496`. In-flight work that merges first: branch
`fix/tello-climb-freshness-guards` (incident fixes), PR #193 (`feature/obstacle-relook-sidestep`),
branch `feature/sensing-controls-temp-preflight`, PR #191 (`feature/claude-reasoning-hardening`) and
PR #192 (`feature/obstacle-metric-eval`). Citations of the form `branch:path:line` or
"`path:line` on the incident branch" refer to those branch tips; plain `path:line` refers to
`origin/main`.

The issue started as "make every Tello height, distance and time configurable, with per-airframe
defaults and hard bounds". The user then widened it: FlightRisk should be tightly coupled to the
DJI Tello but loosely coupled to any drone; users select the drones they own and the code adapts to
that hardware; and features such as thermal detection or a bird's-eye camera are switched on only
where the hardware supports them. This document covers both in one architecture.

## 1. Decisions in one page

1. **Five layers, each a small pure module, each with one owner:**
   - a **hardware catalogue** of drone models (`flightrisk/hardware/catalogue.py`): what a model
     *is* (control protocol, cameras, positioning, envelope facts, link, safety facts);
   - an **airframe profile** per model: the column of defaults and hard bounds that model gives to
     every flight parameter (`flightrisk/params/`);
   - an **inventory** of the units the user owns (`~/.flightrisk/inventory.json`): which models,
     how to reach them, which are available today, and per-unit feature switches;
   - a **feature registry** (`flightrisk/hardware/features.py`): what each feature requires, so the
     UI offers only what the selected hardware supports and says why not otherwise;
   - one **resolver** that turns profile, files, inventory, launcher, environment and command line
     into effective values, each with its source.
2. **The Tello is the reference implementation and first-class model.** Its catalogue entry and
   profile column reproduce today's behaviour exactly; tests pin every Tello default to today's
   constants before any code moves. The generic MAVLink multirotor is the second concrete adapter.
   DJI MSDK is an adapter slot only.
3. **The seams already exist and are extended, not replaced.** The adapter interface is the role
   protocols of `flightrisk/drone/controller.py:1-24,286-392` plus one new optional role for camera
   metadata. The backend registry (`flightrisk/drone/registry.py`, `flightrisk/drone/backends.py`)
   learns which catalogue models a backend drives. Detection-pipeline selection extends the
   detection-profile registry (`flightrisk/vision/profiles.py:80-137`). `tello.py`, `lane_flight.py`
   and `obstacle_camera.py` stay where they are.
4. **Precedence, lowest to highest:** profile default, params file, inventory unit settings,
   launcher window, environment, command-line flag. The three values the dashboard pushes while a
   drone is on the ground (ceiling, margin, slot) keep their runtime path. Every effective value
   carries its source.
5. **Refuse, never clamp.** A value outside its profile's hard bound, a feature switched on for
   hardware that cannot do it, or a failed cross-value constraint is refused with a message naming
   the value, its source and the bound or the missing capability.
6. **The edge is authoritative for what it flies.** Each edge reports its unit description, feature
   states and effective values through one new optional telemetry field (`unit_rev`) and one new
   read-only configuration command (`describe_unit`). Telemetry stays within
   `MAX_FIELDS = 48` (`flightrisk/fleet_protocol/constants.py:307`).
7. **Mobile:** both apps get an inventory screen and feature switches through new, additive ground
   events; phones never switch off a safety feature. Android's direct Tello stack reads the `tello`
   catalogue entry from a generated asset instead of mirroring Python constants.
8. **A guard test** fails on a new unit-named numeric literal in the flight modules that is neither
   in the registry nor in a reviewed allowlist.
9. **Eight PRs**, all starting after the in-flight work merges. PR-1 (catalogue, profiles, resolver,
   guard) goes first; PR-1 and PR-2 (inventory, features, seam types, wire shapes) are pure and
   change no behaviour; then edge, ground planner, dashboard and launcher; the Android and iOS PRs
   come last (`tasks.md`).

## 2. Scope, roadmap and assumptions

**In scope (the first PRs):** the catalogue, the profiles and parameter registry, the inventory,
capability gating, the code seams, and the two concrete adapters (Tello and MAVLink) wired through
them; the dashboard, launcher and both mobile apps updated to show and select them.

**Roadmap, made pluggable here but not built:**

- R1. Thermal detection pipelines: a hot-spot detector, a thermal person detector, and radiometric
  handling where the camera provides it. The slot is a detection profile with
  `requires_sensor = thermal` (`docs/detection-profiles.md:58-65` already sketches a
  `thermal_nadir` registration).
- R2. Better bird's-eye detection: the `top_down` profile with HERIDAL fine-tuned weights exists
  (`flightrisk/vision/profiles.py:15-17`, `:310-318`; `docs/altitude-benchmark.md`);
  further HERIDAL-style training is a follow-up.
- R3. A DJI Mobile SDK adapter. MSDK runs on a phone, so its natural host is the Android app acting
  as a flying edge; it needs the edge fleet protocol on Android. Today other DJI drones are out of
  scope (`docs/open-drone-support.md`, support matrix, "Other DJI").
- R4. Bird's-eye search at room scale (no first-class model has a downward camera today), gimbal
  pitch control per mission, RTK-aware planning, and changing values while a drone flies.
- R5. Camera metadata in phone-as-edge `detections`.

**Out of scope:** choosing defaults for a particular real rescue airframe, calibrating the obstacle
sensor (both from the issue), and vision or reasoning settings, which live in `FlightRiskConfig`
(`flightrisk/config.py:168-248`).

Assumptions (each is labelled where it is used):

- ASSUMPTION A1: the in-flight branches merge as they are at the tips read for this design
  (`origin/fix/tello-climb-freshness-guards` `e7e039a`, `origin/feature/obstacle-relook-sidestep`
  `6f58674`, local `feature/sensing-controls-temp-preflight` `30a6f9e`,
  `origin/feature/claude-reasoning-hardening` `7d48de2`, `origin/feature/obstacle-metric-eval`
  `105c921`). If they change, PR-1 updates the affected rows before it is opened.
- ASSUMPTION A2: hard bounds that do not exist in code today (marked "new" in the tables) are best
  guesses chosen to reject nonsense and clearly unsafe values, not tuned limits.
- ASSUMPTION A3: generic multirotor defaults for values that have no MAVLink equivalent today are
  best guesses; each is marked.
- ASSUMPTION A4: a launcher and the edges it starts are always the same installed version, because
  the launcher starts edges from its own Python (`flightrisk/launcher/commands.py:245-277`).
- ASSUMPTION A5: a fleet that mixes `room` and `field` models is refused at Plan, because plans are
  in one unit scale (`flightrisk/drone/fleet_scale.py:1-23`).
- ASSUMPTION A6: the worst-case telemetry after the in-flight work is 45 fields (section 12.3).
- ASSUMPTION A7: `describe_unit`, being read-only, is obeyed from any connected ground.
- ASSUMPTION A8: the Tello's camera is fixed, forward-facing and has no gimbal; its live stream is
  960 by 720 (`flightrisk/drone/coverage.py:101-106` marks the stream size UNVERIFIED).

## 3. What already exists and is reused

| Existing piece | Where | How the design reuses it |
| --- | --- | --- |
| Role protocols (`Lifecycle`, `TelemetrySource`, `FlightControl`, `Motion`, `Navigation`, `VideoSource`), `Capability`, `DroneCapabilities.flight_scale`, `CommandResult` | `flightrisk/drone/controller.py:1-24`, `:67-99`, `:103-117`, `:286-392` | The adapter interface. One optional role is added (`CameraSource`, section 11.1); nothing is renamed. |
| Backend registry and bootstrap | `flightrisk/drone/registry.py:1-60`, `flightrisk/drone/backends.py` | `BackendSpec` gains two additive fields: the control protocol it speaks and the catalogue models it drives. |
| Edge drone construction | `flightrisk/edge_device.py:1295-1306` (`_drone_kind`), `:2090-2113` (`_build_drone`), `:1703-1723` (MAVLink) | Chooses the backend from the unit's model; `--drone` stays as an override. |
| Detection profiles with a `view` field and fallbacks | `flightrisk/vision/profiles.py:80-137`, `:295-335`; `flightrisk/edge_profiles.py:80` (`ProfileSwitcher`); nack reason `unavailable` (`fleet_protocol/constants.py:115`) | The detection-pipeline seam: profiles gain `requires_sensor` and `requires_view`, and the switcher refuses a profile the unit's camera cannot serve with the existing `unavailable` reason. |
| Forward-camera obstacle sensor | `flightrisk/drone/obstacle_camera.py:1-40`, built in `edge_device.py:1955-2025` | Unchanged; built only when the `forward_obstacle_sensing` feature is available and on. |
| Frame capture stamps | `FrameStamp` and `LatestFrame` in `flightrisk/drone/obstacle_sensor.py:478-640`; `feed_sensor_holder` in `edge_device.py:2028-2044` | Frame metadata reuses `FrameStamp` for time and adds camera id, orientation and sensor type. |
| Camera descriptions for coverage and geolocation | `coverage.CameraModel` (`flightrisk/drone/coverage.py:28-50`, presets `:52-98`), `geolocate.CameraModel` (`flightrisk/drone/geolocate.py:19-25`), `nadir_swath_m` (`coverage.py:326`) | Built from the catalogue's camera descriptors instead of being written separately. |
| Plan scales: head-on room lanes and nadir-swath field lanes | `FleetScale`, `ROOM_SCALE`, `field_scale` (`flightrisk/drone/fleet_scale.py:1-23`, `:78-133`) | The plan-pattern seam for level search versus bird's-eye search. |
| `EnvelopeLimits`, `ROOM_LIMITS`, `FIELD_LIMITS`, `limits_for` | `flightrisk/drone/safety.py:144-218` | Become views of the `tello` and `generic_multirotor` profile columns. |
| `ObstacleSettings` (refuse out-of-range values, log each non-default) | `edge_device.py:1794-1868`; `EVENT_SETTING_NON_DEFAULT` (`obstacle_sensor.py:246`) | The resolver's refusal and logging behaviour. |
| `FlightGuards`, `TempLimits` | incident branch `flightrisk/drone/flight_guards.py:68-153`; sensing branch `flightrisk/drone/temp_preflight.py:36-120` | Typed views built from resolved values. |
| Effective value with a source, and the 409 confirmation | sensing branch `flightrisk/launcher/settings.py:434-560` (`EffectiveObstacle`, `obstacle_env`, `sensing_warning`) and `flightrisk/launcher/server.py` (`CONFIRM_SENSING`) | Generalised to every parameter and feature. |
| Launcher settings, `Issue`s, presets, adapter memory | `flightrisk/launcher/settings.py:76-120`, `:301-378`, `:581-710` | Drone rows are picked from inventory units; issues report refusals and overrides. |
| `set_ceiling` and configuration commands | `flightrisk/fleet_protocol/ceiling.py:1-100`; `fleet_protocol/constants.py:101-111` | Unchanged runtime path; `describe_unit` is added the same way. |
| Ceiling file rules (atomic write, refuse an unreadable file) | `flightrisk/dashboard/ceiling.py:83-183`, `docs/ceiling-and-altitude.md` | The same rules for `params.json` and `inventory.json`. |
| Mobile fixtures generated by Python, lenient decoders, allow-listed emits | `docs/mobile-contract.md:12-52`, `:86-104`, `:132-141` | New events and the catalogue asset follow the same pattern. |
| Android Tello UDP stack | `mobile/app/src/main/java/com/flightrisk/app/drone/TelloConnection.kt`, `DroneManager.kt`, `SearchPattern.kt` | Becomes the Kotlin `tello_sdk_udp` adapter, reading the generated catalogue entry. |
| AST guard test pattern | `tests/test_architecture.py:1-105` | The literal guard test follows it. |

## 4. Findings from the inventory

These are concrete problems the design fixes; they show why one source of truth is needed.

1. **The ground plans against a different geofence than launcher-started edges enforce.** The
   launcher's default is 450 cm (`launcher/settings.py:49`) and it sets `FLIGHTRISK_GEOFENCE_CM`
   for each edge (`commands.py:265`), strips the inherited variable (`commands.py:44`) and never
   sets it for the ground (`commands.py:228-234`). The ground's `fleet_geofence_cm` then uses 500 cm
   (`flightrisk/dashboard/app.py:565-575`, default from `safety.py:107`).
2. **Stale zone slots.** `fleet_patterns.DEFAULT_ALT_SLOTS_CM` is `(100, 160)` "as in the
   coordinator" (`flightrisk/drone/fleet_patterns.py:176-177`), but the coordinator uses `(100, 130)`
   (`flightrisk/drone/coordinator.py:114-120`); 160 cm is above the default ceiling limit of 140 cm.
3. **Three copies of the 30 % takeoff minimum, one configurable.** `safety.py:105` (read from
   `FLIGHTRISK_MIN_TAKEOFF_BATTERY`), `coordinator.py:122` and `launcher/preflight.py:48-49`; the
   preflight warns at 30 whatever the variable says (`preflight.py:417`).
4. **The landing battery threshold cannot be configured** (`safety.py:424`).
5. **The Tello move range leaks into airframe-neutral code:** `coordinator.py:131-134` (used at
   `:935`), the nudge range in the protocol (`fleet_protocol/constants.py:286-288`), and the
   `DroneCapabilities` defaults (`controller.py:106-108`).
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
    edge with a camera source always builds the forward stepped sensor
    (`mavlink_fleet.py:225-236`).
12. **Cameras are described three times and frames carry no camera metadata:**
    `coverage.CameraModel`, `geolocate.CameraModel` (with an assumed 90 degree field of view,
    `geolocate.py:19-25`) and `DetectionProfile.view` ("informational", `vision/profiles.py:133`);
    `VideoSource` yields bare arrays (`controller.py:345-347`).
13. **Android mirrors Python constants and clamps moves.** `DroneConfig` says its defaults "match
    `flightrisk.config.DroneConfig` in Python exactly" (`mobile/app/src/main/java/com/flightrisk/app/config/FlightRiskConfig.kt:186-205`),
    and `SearchPattern.clampDistance` forces every segment into 20 to 500 cm
    (`mobile/app/src/main/java/com/flightrisk/app/drone/SearchPattern.kt:23-29`).
    `docs/mobile-followups.md:58` already asks whether the direct Tello stack is intended.
14. **Fleet-message keys may not contain `feature`, `reid` or `frame`.** The privacy screen refuses
    any key containing a forbidden part, at every depth (`fleet_protocol/constants.py:198-255`,
    `fleet_protocol/checks.py:255-266`). Wire names below avoid them (`gates`, `appearance_match`,
    `view_wait`).

## 5. Architecture overview

```
 catalogue (models)      flightrisk/hardware/catalogue.py   what a model is: protocol, cameras,
        |                                                    positioning, envelope facts, link,
        |                                                    safety facts, scale
        v
 profiles (columns)      flightrisk/params/table.py         per model: default and hard bound of
        |                                                    every flight parameter
        v
 inventory (units)       flightrisk/hardware/inventory.py   the user's units: model, link, serial,
        |                ~/.flightrisk/inventory.json        available, feature switches
        v
 features (gates)        flightrisk/hardware/features.py    what each feature requires; availability
        |                                                    per unit with a reason
        v
 resolver                flightrisk/params/resolve.py        effective value + source for every
        |                                                    parameter and feature of one unit
        v
 consumers   edge: adapter (registry.py/backends.py), SafetyEnvelope, FlightGuards, obstacle wiring,
             detection-pipeline selection;  ground: coordinator, plan checks, dashboard;
             launcher: Start validation;  phones: inventory screen through the ground
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
| `cameras` | Tuple of `CameraSpec` (below). |
| `positioning` | `vps_tof` (optical flow plus time-of-flight, no absolute position), `gps`, or `rtk`. |
| `envelope` | Published facts with sources: maximum speed, maximum height, maximum range, endurance. These feed coverage estimates; the enforced limits are the profile column. |
| `link` | `wifi_ap` (the drone is an access point), `wifi_station`, or `radio` (telemetry radio or companion-computer link). |
| `safety` | Facts the code relies on: what the drone does on command loss and after how long, how it answers commands, and which failsafe is checked before takeoff. |
| `profile` | The profile column id in the parameter registry (usually the model id; `tello_edu` uses `tello`). |
| `extends` | For a custom model: the built-in model it is based on. |
| `sources` | URLs for every published fact, or ASSUMPTION. `coverage.CameraModel` already requires this (`coverage.py:34`). |

`CameraSpec`: `id`; `orientation` (`forward`, `downward`, or `gimbal`); `pitch_min_deg` and
`pitch_max_deg` (equal for a fixed camera; 0 is level, -90 straight down, as in
`geolocate.pixel_to_ground` and `CameraModel.gimbal_pitch_deg`); `stream_w_px`, `stream_h_px`;
`hfov_deg`; `sensor` (`rgb`, `thermal`, or `rgb_thermal`); `radiometric` (thermal only); `fps`.
`DroneModel.coverage_camera(camera_id, pitch_deg)` returns a `coverage.CameraModel` and
`geolocate_camera(...)` returns a `geolocate.CameraModel`, so finding 12's three descriptions come
from one place.

### 6.2 Built-in entries

| Field | `tello` (reference) | `tello_edu` (reference) | `generic_multirotor` (supported) | `custom` |
| --- | --- | --- | --- | --- |
| Adapter | `tello_sdk_udp`: SDK text commands on UDP 8889, state on 8890, video on 11111 (`drone/tello_bound.py:89-91`) | same, SDK 2.0 (answers `sdk?`; a non-EDU Tello does not, `drone/tello.py:93-98`) | `mavlink` through MAVSDK (`drone/mavsdk_link.py`, `--drone mavlink`) | the base model's |
| Cameras | `front`: forward, fixed (ASSUMPTION A8), 960 x 720 stream, 82.6 degree field of view (`coverage.py:52-58`), RGB | same | `main`: declared per unit; default forward, fixed, RGB, 66 degree field of view (the Raspberry Pi Camera Module 3 preset, `fleet_scale.py:58-75`) (Q14) | declared |
| Positioning | `vps_tof`: vision positioning effective at 0.3 to 10 m ([Tello User Manual](https://www.adorama.com/col/productManuals/RYTELO.pdf), p.7) | same | `gps` (3D fix and `gps.min_satellites`, `drone/autopilot.py:61-62`); `rtk` per unit | declared |
| Envelope facts | 8 m/s, 30 m height, 100 m range, 13 min (`coverage.py:52-66`; manual p.4) | same ([Core Electronics](https://core-electronics.com.au/dji-tello-edu.html.md)) | per unit; 120 m regulatory ceiling (`autopilot.py:222`) | declared |
| Link | `wifi_ap` at 192.168.10.1 (`config.py:142`, `TelloWifiChecker.kt:12`) | `wifi_ap`, or `wifi_station` after the SDK 2.0 `ap` command (credentials are never stored) | `radio` or `wifi_station` to the companion computer | declared |
| Safety facts | Lands by itself after about 15 s without a command (`coordinator.py:146-153`); the edge keepalive covers it (`tello.py:252-258`). Answers `takeoff` only once it hovers; late and duplicate replies are possible (`tello.py:93-113`). | same | Autopilot failsafes (link loss, battery, geofence) must be configured; the edge checks them before takeoff (`autopilot.py`), holds `link.loss_hold_s`, then RTL or land (`mavlink_fleet.py:262-266`). | base model's |
| Profile column | `tello` | `tello` (Q3) | `generic_multirotor` | narrowed copy of the base column |

A custom model is defined in `inventory.json` (section 8) with `extends`, its own descriptor fields
and optional narrowed parameter bounds. It may not widen any bound, change the scale or the
adapter, or add a parameter, so it is never less safe than its base. A model whose adapter is a
slot (`dji_msdk`) can be listed but a unit of it cannot be added to a fleet: "no adapter for DJI
Mobile SDK is installed".

## 7. Flight parameters and profiles

### 7.1 `ParamSpec` (one row per value)

| Field | Meaning |
| --- | --- |
| `id` | Dotted lowercase id with the unit as suffix, for example `envelope.ceiling_cm`. Ids pass the fleet privacy key screen (finding 14). |
| `unit` | `cm`, `s`, `pct`, `degC`, `deg`, `deg_per_s`, `cm_per_s`, `count`, `ratio`, `enum`, `bool`, `cm_list`. Distances are centimetres on every model, as today (`safety.py:42-48`); metre environment names are aliases with a scale factor. |
| `kind` | `int`, `float`, `bool`, `enum` (with `choices`), `int_list`. |
| `description`, `group` | One operator sentence; `envelope`, `battery`, `link`, `takeoff`, `tello`, `sensing`, `avoid`, `move`, `plan`, `manual`, `mode`, `gps`. |
| `scope` | `drone` (resolved by the edge, reported) or `ground` (resolved by the ground). |
| `defaults`, `bounds` | Per profile column: default, and inclusive hard bound `(lo, hi)`; `None` when the row does not apply to that model; `lo == hi` makes it read-only for that model. |
| `off_value` | Optional sentinel accepted outside the bound that switches a feature off (`0` for `avoid.sidestep_cm`, `"off"` for `tello.temp_refuse_c`). |
| `safer` | `higher`, `lower` or `none`; used for confirmations, for intersecting bounds in a mixed fleet, and for the fallback profile. |
| `confirm` | A value on the less-safe side of the default (or the `off_value`) needs the operator's confirmation in the launcher. |
| `edit` | `ui`, `file_env`, or `read_only`. |
| `applies` | `next_start`, `on_ground` (changed between flights through `set_ceiling`), or `plan`. Nothing changes in flight in v1. |
| `env`, `env_aliases`, `cli` | Canonical environment name (existing names stay canonical; new rows get `FLIGHTRISK_` plus the upper-case id), aliases with scale factor and the scale they apply to (a room alias is never read for a field model, preserving `safety.py:404-407`), and command-line flags. |
| `constraints` | Cross-value checks with operator messages (section 10.2). |

### 7.2 Profiles

The parameter registry has one column per built-in profile (`tello`, `generic_multirotor`); every
catalogue model names its column. `EnvelopeLimits`, `ROOM_LIMITS`, `FIELD_LIMITS` and `limits_for`
become views of those columns, and the module constants in `safety.py` become re-exports computed
from the `tello` column, so code that imports them (`dashboard/ceiling.py:56-63`,
`launcher/settings.py:30-35`) keeps working and the constants are no longer literals. The full
table of 83 rows is in section 20.

### 7.3 Which profile a unit uses

On the edge, in this order: `--model` or `FLIGHTRISK_MODEL` (with `--airframe` and
`FLIGHTRISK_AIRFRAME` as aliases); the unit's model from the inventory (by `--drone-id`); the
launcher's per-drone model (in `FLIGHTRISK_LAUNCHER_PARAMS`); otherwise the backend's default
(`tello` and `fake` give `tello`, `mavlink` gives `generic_multirotor`). The edge refuses to start
when the model's scale or adapter differs from the backend it is running (`tello.py:568-576`,
`mavlink_fleet.py:376-385`), so a Tello can never fly with outdoor bounds.

On the ground, a unit's model is what its edge reports; before that, the inventory's model; for an
older edge with no inventory entry, `tello`, which is today's fallback (`safety.py:216-218`) and the
narrower distance bounds. For a `ground`-scope parameter that depends on the airframes, the ground
uses the intersection of the fleet's bounds and the most conservative default, by `safer`.

## 8. Inventory

### 8.1 The unit record

`~/.flightrisk/inventory.json` (`FLIGHTRISK_INVENTORY_FILE` overrides), beside `launcher.json`
(`launcher/settings.py:570-574`) and `ceiling.json` (`dashboard/ceiling.py:103-107`), outside the
repository, written atomically like the ceiling file:

```json
{
  "version": 1,
  "models": {
    "site_hex": {"extends": "generic_multirotor", "name": "Site hexacopter",
                 "cameras": [{"id": "gimbal", "orientation": "gimbal", "pitch_min_deg": -90,
                              "pitch_max_deg": 0, "stream_w_px": 1920, "stream_h_px": 1080,
                              "hfov_deg": 82, "sensor": "rgb_thermal", "radiometric": true}],
                 "positioning": "rtk",
                 "param_bounds": {"envelope.ceiling_cm": {"lo": 1000, "hi": 6000}},
                 "param_defaults": {"envelope.ceiling_cm": 4000},
                 "sources": "ASSUMPTION: operator-entered"}
  },
  "units": [
    {"unit_id": "drone-1", "name": "Tello A", "model": "tello",
     "link": {"kind": "wifi_ap", "ssid": "TELLO-5A1B2C", "host": "192.168.10.1"},
     "serial": "0TQDG7REDB1234", "available": true,
     "features": {"forward_obstacle_sensing": true}},
    {"unit_id": "hex-1", "name": "Hex 1", "model": "site_hex",
     "link": {"kind": "radio", "mavlink_address": "serial:///dev/ttyAMA0:921600"},
     "available": false, "features": {"birdseye_search": true, "thermal_detection": true}}
  ]
}
```

- `unit_id` is the drone id used everywhere (the launcher's stricter id rule,
  `launcher/settings.py:63-65`, and the protocol's `is_identifier`).
- **No secrets.** The file never holds a Wi-Fi password, the edge token or any key. The loader
  refuses keys named like `password`, `passphrase`, `secret`, `token` or `key`, and refuses any
  value equal to the edge token, the rule the launcher already applies to settings and presets
  (`launcher/settings.py:330-331`, `:692-696`). An SSID and a serial number are identifiers, not
  secrets.
- Per-session choices stay in the launcher: which local adapter reaches a Tello, the interface
  index, whether the edge runs on this computer, the slot (`launcher/settings.py:88-99`). The
  adapter memory by drone id (`launcher/settings.py:584-590`, `:671-690`) is unchanged.

### 8.2 How the inventory is used

- **No file, no change.** Without `inventory.json` the inventory is implicit: every drone the
  launcher selects, or every id in the ground's `--fleet`, is a unit of model `tello` (or the model
  its edge reports). Every current workflow therefore behaves as today.
- **Fleet planning uses only units in the inventory that are available.** The launcher offers only
  available units as drone rows; the ground's `--fleet` comes from those rows
  (`launcher/settings.py:128-130`). An edge whose reported model differs from its inventory model is
  refused at Plan with both names.
- **Code paths adapt to each unit's capabilities:** per-unit bounds (geofence, move range, slots,
  battery) come from its profile; per-unit features decide which sensor, detection pipeline and plan
  pattern apply (section 9).
- **Who owns it:** the ground host's file is authoritative for the fleet. The launcher edits it
  directly on its own computer; phones edit it through the ground (section 13); an edges-only
  computer keeps its own file for its local units, and the ground learns their models from their
  reports (Q10).

## 9. Capability-gated features

### 9.1 `FeatureSpec`

Each feature declares `id`, `label`, `description`, `requires` (a predicate over the unit's model
and cameras that returns availability and, when unavailable, an operator reason), `scope` (`unit`
or `ground`), `default_on` (when available), `safety` (true when switching it off reduces
protection, which needs confirmation and is never allowed from a phone), and what it selects: a
bound parameter (for example `sensing.mode`), a detection-pipeline requirement, and a plan pattern.

A feature switch is resolved like a parameter (default, inventory unit setting, launcher,
environment, command line) with its source. Switching on a feature that the unit cannot support is
refused with the reason; it is never silently left off. An unavailable feature that was never
switched on is simply off and shown with its reason.

### 9.2 Features in v1

| Feature id | Requires | Selects | Tello | Generic multirotor (default camera) | Notes |
| --- | --- | --- | --- | --- | --- |
| `forward_obstacle_sensing` | a camera that can look level (forward, or a gimbal whose range includes 0 degrees), RGB, and backend capability `STEPPED_SENSING` (`controller.py:81-84`) | `sensing.mode = required`; the `ForwardCameraSensor` (MiDaS) | available, on | available, on | Safety feature: off needs confirmation (the sensing-branch precedent). Finding 11 is resolved by declaring the camera. |
| `onboard_avoidance` | backend capability `ONBOARD_AVOIDANCE` (`controller.py:85-88`) and the autopilot's avoidance configured (`autopilot.py:380-390`) | sensing mode `autopilot` | unavailable: "the Tello has no onboard avoidance" | available when configured | Detected, not switched. |
| `level_search` | a camera that can look level | head-on lanes (`ROOM_SCALE` pattern, `fleet_scale.py:126-133`), detection profile `default` or `level` | available, on | available, on | Today's behaviour. |
| `birdseye_search` | a downward camera, or a gimbal with `pitch_min_deg <= -60` | lawn-mower lanes at altitude with spacing from the nadir swath (`field_scale`, `coverage.nadir_swath_m`); detection profile `top_down`; nadir coverage maths | unavailable: "the Tello has one fixed forward camera" | unavailable with the default camera; available for a unit that declares a downward or gimbal camera | Plan pattern is fleet-wide in v1 (Q13). Detector training is roadmap R2. |
| `thermal_detection` | a camera with sensor `thermal` or `rgb_thermal` | a detection profile with `requires_sensor = thermal`; radiometric handling when `radiometric` | unavailable: "no thermal camera" | per declared camera | Pipelines are roadmap R1; v1 delivers the gate and the slot. |
| `appearance_match` | any RGB camera | ReID scoring on the ground | available, on | available, on | Orientation-agnostic. Wire id avoids "reid" (finding 14). |
| `claude_second_opinion` | none on the unit; ground provider `claude` and cloud consent (`config.py:115-121`) | ground reasoning | ground setting | ground setting | Camera-agnostic; shown per fleet, not per unit. |

The detection profiles gain `requires_sensor` (`rgb` or `thermal`) and `requires_view` (`level`,
`nadir` or `any`; today `view` is informational, `vision/profiles.py:133`). For v1 the `top_down`
profile is advisory on a forward-only camera: still selectable, with the warning "this drone has no
downward camera; top_down is meant for views from above", so nothing that works today disappears
(Q12). A thermal profile is hard-gated, because it cannot run on an RGB stream.

## 10. Sources, precedence and validation

### 10.1 The order

Start-time resolution, lowest to highest:

1. `profile`: the model's profile column (or a feature's default).
2. `file`: `~/.flightrisk/params.json` (`FLIGHTRISK_PARAMS_FILE`), sections `defaults`, then
   `models.<model>`, then `units.<unit_id>`; numeric tuning only.
3. `inventory`: the unit's feature switches in `inventory.json`.
4. `launcher`: values the launcher window sets for the processes it starts.
5. `env`: the process's own `FLIGHTRISK_*` variable (canonical or alias).
6. `cli`: a command-line flag.

Runtime: for `on_ground` parameters (ceiling, margin, slot), an accepted `set_ceiling` replaces the
start value while the drone is on the ground, as today (`docs/ceiling-and-altitude.md`, "What every
edge enforces on its own"); source `ground`. Map-editor inputs (strip, area) are per-plan values;
source `plan`.

Why this order: profile and files are slow-changing site knowledge; the inventory is the owner's
per-unit choice; the window is this mission's choice; the environment is the only explicit,
per-process, non-persistent layer, so a person or script that sets `FLIGHTRISK_HOVER_TEST=1` or
`FLIGHTRISK_OBSTACLE_SENSING=off` did it on purpose, and "existing env names must keep working"
means a variable must not lose silently to a window value saved weeks ago. The sensing branch
already decided this way (`feature/sensing-controls-temp-preflight:flightrisk/launcher/settings.py:496-560`).
A flag already wins over its variable on the edge (`edge_device.py:2210-2221`). Because the
environment wins, the launcher shows every override, warns, and asks for confirmation when it is
less safe.

This reverses one current behaviour: the launcher strips inherited `FLIGHTRISK_GEOFENCE_CM`,
`FLIGHTRISK_CEILING_CM` and `FLIGHTRISK_CEILING_MARGIN_CM` (`commands.py:44-46`), so today the
window wins for those three (Q1).

`params.json` format and rules: `{"version": 1, "defaults": {...}, "models": {"<model>": {...}},
"units": {"<unit_id>": {...}}}`; values in registry units; an unknown id, a wrong type, or a file
that exists but cannot be read refuses Start and the edge start, never falling back to defaults
silently (the ceiling-file rule). Every effective value is
`EffectiveValue(id, value, unit, source, source_detail, default, lo, hi, model, applies,
non_default, less_safe)`.

### 10.2 Refusal and constraints

One message form everywhere, for example: `envelope.ceiling_margin_cm = 20 cm (from
FLIGHTRISK_CEILING_MARGIN_CM) is outside the tello bound 30 to 500 cm: the margin covers the
Tello's height error and its overshoot at the end of a climb.` For a feature: `birdseye_search
(from inventory, unit drone-1) cannot be switched on: the Tello has one fixed forward camera.`

The edge exits before it connects to the drone (`edge_device.py:1810-1813` pattern); the launcher
adds an `Issue(ERROR)` (`launcher/settings.py:301-378`); the dashboard refuses the save, the
inventory edit or the Plan. The clamp of finding 7 becomes a refusal.

Constraints: `battery.land_pct <= battery.min_takeoff_pct` (`safety.py:376-377`); ceiling minus
margin at least the lowest slot (`safety.py:265-270`); every slot at most ceiling minus margin
(`safety.py:274-285`); `tello.temp_warn_c <= tello.temp_refuse_c` unless off;
`link.loss_land_s >= 2 * HEARTBEAT_INTERVAL_S` (`fleet_protocol/constants.py:273-275`);
`avoid.sidestep_cm` is 0 or within the step bound (#193,
`obstacle_sensor.py:116-121` on its branch); `sensing.step_cm` within the move range (`edge_device.py:1833-1838`);
zone slots within the slot bound and at least `plan.min_slot_separation_cm` apart
(`fleet_scale.py:49-50`); `sensing.mode = required` only when `forward_obstacle_sensing` is
available.

## 11. Seams in the code

### 11.1 The drone adapter

The adapter interface is the existing role protocols (`controller.py:286-392`): connect and
disconnect (`Lifecycle`), commands with `CommandResult` (`FlightControl`, `Motion`, `Navigation`),
telemetry (`TelemetrySource.snapshot`), and video (`VideoSource`). One optional role is added:

```python
@dataclass(frozen=True)
class FrameMeta:
    camera_id: str
    orientation: str          # forward | downward | gimbal
    pitch_deg: float          # current pitch; fixed cameras report their fixed value
    sensor: str               # rgb | thermal | rgb_thermal
    stamp: FrameStamp | None  # reuses obstacle_sensor.FrameStamp; None means arrival time
    width_px: int
    height_px: int
    channel_order: str        # as obstacle_sensor's SENSOR_CHANNEL_ORDER

class CameraSource(Protocol):
    def cameras(self) -> tuple[CameraSpec, ...]: ...
    def on_frame_meta(self, cb: Callable[[np.ndarray, FrameMeta], None], camera_id: str | None = None) -> None: ...
```

A backend that does not implement `CameraSource` is wrapped by `frames_with_meta(drone, model)`
(`flightrisk/hardware/frames.py`), which registers on the existing `on_frame` and attaches the
model's primary camera and the decoder's `FrameStamp` (`edge_device.py:2028-2044` shows the stamp
already exists). The Tello controller therefore needs no change for metadata.

`BackendSpec` (`registry.py`) gains `adapter` (a catalogue adapter id) and `models` (the model ids it
can drive), both with defaults, and `backends.py` registers `tello` with `tello_sdk_udp` and
`("tello", "tello_edu")`, `mavlink` with `mavlink` and `("generic_multirotor",)`, and `sim` with the
Tello models. The edge's `_build_drone` (`edge_device.py:2090-2113`) picks the backend from the
unit's model; `--drone` stays as an explicit override that must agree with the model.

### 11.2 Detection-pipeline selection

`select_pipeline(camera: CameraSpec, requested_profile)` in `flightrisk/edge_profiles.py` returns
the profile to run, or a refusal reason. The `ProfileSwitcher` (`edge_profiles.py:80`) calls it
before loading, and refuses with the existing nack reason `unavailable`
(`fleet_protocol/constants.py:115`), so older grounds already understand the refusal. Thermal
pipelines (roadmap R1) are new registered profiles; nothing else branches on profile names
(`vision/profiles.py`, module docstring).

### 11.3 Where today's Tello code sits

| Module | Role behind the seams | Change |
| --- | --- | --- |
| `drone/tello.py` (`TelloController`) and `drone/tello_bound.py` | The `tello_sdk_udp` adapter and its transport | `DroneCapabilities` built from the catalogue (`tello.py:568-576`). Nothing else. |
| `drone/lane_flight.py`, `lane_executor.py`, `lane_monitor.py` | Adapter-neutral executor; branches on `Capability` | Reads resolved parameters instead of module constants (rows 51-52, the guards and the avoidance settings). No flow change. |
| `drone/obstacle_camera.py` (`ForwardCameraSensor`) | The forward-camera obstacle pipeline | None. `edge_device._build_obstacle_wiring` (`edge_device.py:1955-2025`) builds it only when `forward_obstacle_sensing` is available and on. |
| `drone/mavlink_fleet.py` | The `mavlink` adapter | `MavlinkFleetOptions` built from resolved values. |
| `fleet_scale.py`, `coverage.py`, `geolocate.py` | Plan pattern and coverage maths | Camera models built from the catalogue; bird's-eye pattern selected by the feature. |
| Android `TelloConnection.kt`, `DroneManager.kt`, `SearchPattern.kt` | The Kotlin `tello_sdk_udp` adapter on the phone | Read the generated `tello` catalogue entry; `SearchPattern` refuses or merges a segment shorter than the minimum move instead of clamping it (finding 13). |

## 12. Agreement between processes, and the wire

### 12.1 Who resolves

- **Launcher, before Start:** for each edge it starts, resolves exactly the layers that child will
  see (model, local params file, local inventory, window, inherited environment), shows the table,
  turns refusals into errors and overrides into warnings, asks for confirmations, then starts each
  edge with one variable, `FLIGHTRISK_LAUNCHER_PARAMS`, holding compact JSON
  `{"unit_id": "drone-1", "model": "tello", "values": {...}, "features": {...}}` with only what the
  window sets. The variable joins `CONTROLLED_ENV` (`commands.py:27`), so an inherited one is always
  removed and the label `launcher` is truthful. The launcher stops setting the ceiling, margin and
  geofence variables and stops passing `--alt-slot` (`commands.py:257`, `:265-267`). The ground
  process gets the same variable with ground-scope values and the geofence, which removes finding 1
  before any edge reports.
- **Edge, at start:** resolves the same layers plus its flags with the same pure function, refuses
  to start on any refusal, logs each non-default value with its source, builds `SafetyEnvelope`,
  `FlightGuards`, `TempLimits`, the obstacle and avoidance settings, `MavlinkFleetOptions` and the
  feature-selected sensor and pipeline. A hand-started edge has no launcher layer and behaves as
  today.
- **Ground, at Plan:** resolves ground-scope values; uses each unit's reported model, values and
  feature states for that unit's bounds and for the plan pattern; uses the reported
  `battery.min_takeoff_pct` in the start gate (`coordinator.py:2735-2746`).

### 12.2 `describe_unit`

`{"type": "describe_unit", "cmd_id": "...", "drone_id": "drone-2"}`, ground to edge, added to
`CONFIG_COMMAND_TYPES` (`fleet_protocol/constants.py:110-111`); a configuration command needs no
protocol version change. It changes nothing, so it is answered in any state and on any link
version, from any connected ground (ASSUMPTION A7, Q9). The reply is an `ack` with
`for: "describe_unit"`, `unit_rev`, and:

- `unit`: `{"model", "model_name", "adapter", "scale", "positioning", "link", "cameras": {"<id>":
  {"orient", "pitch_min_deg", "pitch_max_deg", "w_px", "h_px", "hfov_deg", "sensor",
  "radiometric"}}}`;
- `gates`: `{"<feature id>": {"on", "ok", "why", "src"}}` (the wire key is `gates` because
  `features` is a forbidden key, finding 14);
- `values`: `{"<param id>": {"v", "src", "d", "lo", "hi"}}`.

Bounds and cameras are objects, not lists, because every list of numbers counts against
`MAX_LISTED_NUMBERS = 64` (`fleet_protocol/constants.py:259-262`, `checks.py:268-280`). About 50
values at about 70 bytes plus the unit is about 4 KB, far below `MAX_MESSAGE_BYTES`
(`constants.py:316`); the nesting depth is 4, below `MAX_DEPTH = 8`; texts are cut to
`MAX_TEXT_LEN = 200` (`constants.py:298`) and never contain the token.

### 12.3 Telemetry

One new optional field, `unit_rev`: the first 12 hex digits of SHA-256 over the canonical JSON of
the `describe_unit` content. The worst-case telemetry is 42 fields after the sensing branch
(`feature/sensing-controls-temp-preflight:flightrisk/fleet_protocol/constants.py:301-307`), plus two
from the incident branch (`move_refusals_total`, `move_refusal_last`, its `fleet_protocol/validate.py`)
and one from #193 (`obstacle_avoid`, its `obstacle_sensor.py`): 45 (ASSUMPTION A6, measured by
`tests/test_telemetry_field_cap.py` in PR-3). With `unit_rev` it is 46 of 48. `MAX_FIELDS` cannot be
raised, because an older ground would reject a newer edge's telemetry; the table cannot travel in
telemetry, which is sent every 0.5 s (`constants.py:276-277`).

The ground sends `describe_unit` when an edge's telemetry first carries `unit_rev` and when it
changes. Plan is refused for a unit that advertises `unit_rev` but has not answered yet ("waiting
for drone-2's description; press Plan again in a moment"), as for the ceiling sync. An older ground
ignores `unit_rev` and never asks; an older edge never advertises it, so the dashboard shows "not
reported (older edge)" and uses the inventory model, else `tello`.

## 13. Dashboard, launcher and mobile

### 13.1 Dashboard and launcher

- **Dashboard:** a "Fleet hardware" card (per unit: model, cameras, positioning, link, each
  feature with available, on, source and reason) and a "Flight parameters" card (per unit and for
  the ground: value, unit, source badge with detail, default, bound, when it applies; non-default
  rows highlighted, less-safe rows marked). Data through new Socket.IO requests `get_inventory` and
  `get_flight_params`, answered by `inventory` and `flight_params`.
- **Launcher:** an inventory editor (units of built-in or custom models; custom models only here),
  drone rows chosen from available units, per-unit feature switches, the effective-values table
  for the edges it starts, and the existing edit fields (ceiling, margin, geofence, slot, sensing
  mode, minimum safe depth). The launcher starts only Tello-family and fake units
  (`launcher/settings.py:41`); MAVLink units show "runs on its companion computer".
- **Confirmations:** Start is refused with HTTP 409 and `needs_confirmation: "flight_params"` while
  any less-safe value or switched-off safety feature is unconfirmed; the retry carries
  `confirm_params: {"<id>": <exact value>}`; a confirmation for another value does not count, and a
  preset load or window reload forgets it. This generalises `CONFIRM_SENSING` (sensing branch
  `launcher/server.py`); `confirm_sensing` stays accepted for one release. The dashboard ceiling
  card confirms only a margin below its default.

### 13.2 Mobile contract and apps

What crosses to the phones, all additive (apps ignore unknown keys at every depth,
`docs/mobile-contract.md:134`):

- New events a phone handles: `inventory` (units, the catalogue summary of their models, per-unit
  feature availability with reasons, `editable` and `locked_reason`), `flight_params` (read-only
  table), and `inventory_ack` (`ok`, `reason`).
- New events a phone may emit (added to the allow-list, `docs/mobile-contract.md:86-104`):
  `get_inventory`, `get_flight_params`, `inventory_set_available {unit_id, available}`,
  `inventory_set_feature {unit_id, feature, on}`, and `inventory_upsert_unit {unit}` for units of
  models the catalogue or the laptop already defines. The ground refuses any of them while a drone
  flies, a start is scheduled, or this ground is a standby, as it does for ceiling changes, and
  refuses `inventory_set_feature` that switches off a safety feature ("switch it off on the laptop,
  which asks for confirmation"). Removing units and defining custom models stay on the laptop.
- `ceiling_settings.limits` keeps its five keys (`dashboard/ceiling.py:680-686`, decoded by iOS
  `Models.swift:529-538` and Android `SettingsModels.kt:148-158`), with the intersection of the
  fleet's bounds; each `ceiling_settings.drones[]` entry may gain `model` and `limits`.
- Fixtures: `inventory.json`, `flight_params.json`, `inventory_ack.json` and new
  `client_emits.json` cases, generated by Python like the others (`docs/mobile-contract.md:12-52`),
  plus `catalogue.json` (the built-in models).

What each app needs:

- **iOS** (operator console only, never an edge, `docs/mobile-contract.md:7-10`): decoders for the
  three events, an inventory screen with availability and feature switches, and the read-only
  parameters table.
- **Android:** the same screens; plus `catalogue.json` bundled as an asset so the direct Tello stack
  takes its move range and safety facts from the `tello` entry instead of `DroneConfig`'s mirrored
  constants (finding 13), with a unit test that fails when the asset and the Python catalogue
  differ. The standalone direct-Tello mode keeps one local unit record (model `tello`, SSID, name)
  in the app's settings, because it works without a ground. Phone-as-edge is unchanged: it flies
  nothing and sends only `detections` without a `drone_id` (`docs/mobile-contract.md:179-194`).

## 14. The guard test

File `tests/test_param_literals.py`, with its allowlist in `tests/param_literal_allowlist.py`.

**Scope:** `flightrisk/drone/*.py`, `flightrisk/edge_device.py`, `flightrisk/edge_profiles.py`,
`flightrisk/launcher/settings.py`, `flightrisk/launcher/commands.py`,
`flightrisk/launcher/preflight.py`, `flightrisk/dashboard/ceiling.py`,
`flightrisk/dashboard/manual_control.py`, `flightrisk/dashboard/safety.py`,
`flightrisk/dashboard/__main__.py`, `flightrisk/fleet_protocol/constants.py`. Excluded:
`flightrisk/params/` and `flightrisk/hardware/` (the tables themselves), and `tests/`.

**Rule.** A finding is an `int` or `float` literal (not a `bool`; a unary minus is folded in; `0`
and `0.0` are exempt as the "off" and origin sentinel) that is:

- (A) the whole value, or a numeric element of a tuple value, bound to a unit-named target: an
  assignment or annotated-assignment target (name or attribute, which covers dataclass fields), a
  function parameter default, or a keyword argument;
- (B) an operand of an ordering comparison (`<`, `<=`, `>`, `>=`) whose other operand is a
  unit-named name or attribute;
- (C) the `default=` of an `add_argument` call whose option name is unit-named, including a numeric
  string such as `"75"` (this catches `dashboard/__main__.py:141` and `:146`).

A name is unit-named when it matches, case-insensitively,
`(?:_|^)(cm|mm|m|km|s|ms|deg|dps|mps|pct|percent|c|hz)$`, or contains one of `height`, `alt`,
`altitude`, `ceiling`, `margin`, `geofence`, `fence`, `slot`, `timeout`, `delay`, `interval`,
`budget`, `settle`, `lag`, `battery`, `distance`, `step`, `separation`, `spacing`, `gap`, `offset`,
`strip`, `width`, `depth`, `speed`, `radius`, `tolerance`, `threshold`, `temp`, `retries`, `retry`,
`votes`, `attempts`, `readings`, `samples`, `sidestep`, `relook`, `clearance`, `fov`, `pitch`; and it
does not match the exemption `(_px|_bytes|_chars|_len)$|^(max_depth|frame_height|frame_width|match_threshold)$`
(pixel sizes, byte sizes, the protocol's nesting depth and vision thresholds are not flight values).

**Allowlist.** A Python dict keyed by source file; each entry is
`(kind, binding name, value) -> (category, reason)` with a count for repeats. Categories: `sdk`,
`protocol`, `regulatory`, `reference`, `internal`, `legacy`, `test_double`, `vision`, `deferred`
(belongs in the registry but is not migrated yet; names its follow-up). Keys use the binding name
and value, never the line number, so moving code does not churn the list, and changing a constant's
value fails the test until the allowlist line changes in the same diff (Q8). Sections are separated
by file, so PRs that migrate different files edit different sections.

**Test cases.** (1) Every finding in scope is allowlisted. (2) Every allowlist entry still matches a
finding (no stale entries; the migration PRs must delete what they migrate). (3) The checker reports
a planted `RETRY_HOLD_S = 2.5`, `foo(timeout_s=3)`, `if height_cm > 30:` and
`add_argument("--gap-cm", default="40")` in a temporary file, as `tests/test_architecture.py:83-101`
does for imports. (4) Every `deferred` entry names a follow-up.

**Measured on `origin/main`** with a prototype of rules A and B: 328 findings in 37 of the 54 files
then in scope (212 assignments, 68 keyword arguments, 43 defaults, 5 comparisons). The in-flight
tips add about 42 (21 incident, 11 relook, 10 sensing). After migration the allowlist holds the
constants of section 21. Known blind spots, covered by review: positional arguments
(`_DefaultSlot(100.0)`, `edge_device.py:1614`), literals inside arithmetic, and names without a unit
word.

**Registry and catalogue self-tests (PR-1 and PR-2):** every default lies within its bound for every
profile; every constraint holds for every profile's defaults; ids are unique and pass the privacy
key screen; environment names are unique across rows; the `tello` move bounds equal the SDK
constant the Tello driver uses; `tello` and `generic_multirotor` defaults equal today's module
constants (pins "unchanged by default"); every catalogue fact has a source or ASSUMPTION; every
feature's `requires` returns a reason when unavailable; the generated docs equal the committed
`docs/flight-parameters.md` and `docs/hardware-catalogue.md`; and the generated
`catalogue.json` fixture equals the committed one.

## 15. Classification and counts

Each inventoried value is in one of five classes:

- **A, airframe parameter:** its sensible default or safe range depends on the aircraft. In the
  registry, a column per profile, editable within the bound.
- **R, profile fact:** airframe-specific but fixed by the aircraft or its SDK (`lo == hi`). In the
  registry so it is shown and so Tello facts stop leaking into neutral code; read-only. The Tello
  SDK's 20 to 500 cm move range is the example: it is the `tello` column of `move.min_cm` and
  `move.max_cm` ([Tello SDK 2.0 User Guide](https://xcopter.com/download/Tello%20SDK%202.0%20User%20Guide.pdf),
  "forward x: x = 20-500"), while the driver keeps its own SDK constant and a test keeps the two
  equal.
- **M, mission or operator parameter:** not airframe-specific; same default for every profile.
- **C, constant:** a protocol or SDK fact, a regulatory limit, reference data, a software internal,
  legacy code, or a test double. Stays in code, allowlisted with its category.
- **Reserved:** a row a planned PR adds (the floor-ratio metric).

Counts: 83 registry rows (56 A, 8 R, 19 M) plus 1 reserved row (section 20), and 89 constant
entries (43 protocol, SDK and regulatory facts; 32 software internals; 7 algorithm or reference
values; 6 legacy or out-of-scope groups; 1 test-double group) (section 21). 173 values inventoried;
64 airframe-specific (A plus R). The catalogue adds 3 built-in models plus custom models, 7
features, and 8 descriptor facts per model.

## 16. Migration of existing and in-flight names

Every existing variable keeps its name and meaning; the registry maps it to a parameter id.

| In-flight work | What it adds | How it folds in |
| --- | --- | --- |
| Incident fixes | `FLIGHTRISK_TELLO_SETTLE_SAMPLES`, `_SETTLE_TOLERANCE_CM`, `_SETTLE_TIMEOUT_S`, `_TAKEOFF_LATE_S`, `_REFUSAL_RETRIES`, `_REFUSAL_HOLD_S`, `FLIGHTRISK_HOVER_TEST` (`flight_guards.py:68-74` on the branch); `MIN_ALT_SLOT_CM = 50` (`safety.py:131` on the branch); `DESCENT_CLEARANCE_CM` (`flight_guards.py:107`) | Rows 25 to 33; the 50 cm floor becomes the `tello` lower bound of `envelope.alt_slot_cm`; `FlightGuards` is built from resolved values; `slot_below_minimum`'s text becomes the bound reason. |
| PR #193 | `FLIGHTRISK_OBSTACLE_CLEAR_VOTES`, `_RELOOK`, `_SIDESTEP_CM`, `_SIDESTEPS`, and `OBSTACLE_SIDESTEP_PASS_CM` (`obstacle_sensor.py:106-148` on the branch) | Rows 54 to 58; `0` is the `off_value` of `avoid.sidestep_cm`; `ObstacleSettings.avoid_kwargs` reads resolved values. The telemetry field `obstacle_avoid` is counted in section 12.3. |
| Sensing branch | Launcher fields `obstacle_sensing` and `obstacle_min_safe_depth` (`launcher/settings.py:146-148` on the branch); `FLIGHTRISK_TELLO_TEMP_WARN_C` and `_REFUSE_C` (`temp_preflight.py:36-47` on the branch) | Rows 40, 43, 37, 38. Saved presets keep their keys. `EffectiveObstacle` and `sensing_warning` become thin wrappers over the resolver and the generic confirmation; `window` and `environment` map to `launcher` and `env`. The launcher's sensing mode is the UI of the `forward_obstacle_sensing` feature. |
| PR #191 | `FLIGHTRISK_CLAUDE_BREAKER_THRESHOLD`, `_BREAKER_COOLOFF`, `_MAX_CALLS_PER_MINUTE` (`config.py:128-130`, `:212-215` on the branch); `_TIMEOUT_S`, `_MAX_RETRIES` (`claude_agent.py:42-49` on the branch) | Not parameters: ground-side service limits with no airframe meaning and no safety bound; they stay in `FlightRiskConfig` (`config.py:1-27`). The `claude_second_opinion` feature only shows whether the ground's provider and consent allow it. |
| PR #192 and the floor-ratio metric | Band fractions (`obstacle.py:307-311` on the branch); `--ratio-threshold` default 1.0 (`scripts/obstacle_replay.py:108` on the branch) | Band fractions are class C. The opt-in metric adds `sensing.metric` and `sensing.floor_ratio_threshold` rows when it lands; the guard test makes that PR add them. |

Stores: `launcher.json` stays version 1 and gains optional per-drone `unit_id` and `model`; older
launchers ignore unknown keys (`launcher/settings.py:168`). `ceiling.json` is unchanged.
`params.json` and `inventory.json` are new and optional.

## 17. Behaviour changes (each deliberate)

1. Launcher-started ground and edges agree on the geofence (finding 1).
2. `fleet_patterns` zone slots become `(100, 130)` (finding 2); only offline coverage tools use them.
3. The preflight battery warning and the coordinator's start gate use the effective takeoff minimum
   (finding 3).
4. `battery.land_pct` becomes configurable through `FLIGHTRISK_LAND_BATTERY` (finding 4).
5. The coordinator and the nudge check use each drone's move range (finding 5); a Tello is unchanged.
6. An orphan-landing time below 30 s is refused instead of raised (finding 7).
7. A hand-started Tello edge refuses a geofence above 5000 cm (finding 9).
8. Inherited `FLIGHTRISK_GEOFENCE_CM`, `FLIGHTRISK_CEILING_CM` and `FLIGHTRISK_CEILING_MARGIN_CM`
   win over the launcher window, with a warning and, when less safe, a confirmation (Q1).
9. If Q2 is decided as recommended, a hand-started Tello edge's default geofence drops from 500 to
   450 cm; the default 400 by 400 cm plan stays inside it.
10. A thermal detection profile is refused on a unit without a thermal camera (new profiles only;
    no existing profile is hard-gated, Q12).
11. Android's `SearchPattern` refuses or merges a segment below the minimum move instead of
    stretching it to 20 cm (finding 13).

## 18. Risks

- **The edge rewiring touches every safety value.** PR-1 pins every `tello` and
  `generic_multirotor` default to today's constants by test before PR-3 moves any code; PR-3's
  acceptance includes the existing safety, ceiling, guard, obstacle and telemetry suites unchanged.
- **Telemetry headroom is 2 fields after this work.** `unit_rev` is the only new field; everything
  else travels in `describe_unit`.
- **Precedence surprises.** Every override is visible before Start and on the dashboard, and
  less-safe overrides need confirmation.
- **Inventory drift between the laptop, an edges-only computer and the phones.** The ground host is
  authoritative, edges report their model, and a mismatch refuses Plan with both names.
- **Scope.** The first two PRs are pure and add no runtime path; the mobile PRs come last and can
  slip without blocking the Python work.
- **Allowlist size (about 260 entries after migration).** Grouped by file and category; stale
  entries fail, so it only shrinks unless a reviewer accepts an addition.

## 19. Open questions for the consensus

Each has options and a recommendation.

- **Q1. Environment versus launcher window.** (A) The environment wins everywhere, the sensing
  branch's rule; the launcher stops stripping the three ceiling and geofence variables and warns
  instead. (B) The window wins everywhere, today's ceiling rule. (C) A per-parameter mix.
  Recommendation: A; one rule, existing variables never lose silently, and every override is shown
  and confirmed.
- **Q2. One Tello geofence default.** (A) 450 cm everywhere; a hand-started edge goes from 500 to
  450, stricter, still fits the default plan. (B) 500 as the profile default and 450 as the launcher
  preset value; nothing changes and two numbers remain. Recommendation: A.
- **Q3. `tello_edu`.** (A) Its own catalogue entry (SDK 2.0, station mode) sharing the `tello`
  parameter column. (B) One `tello` entry with a variant flag. (C) Its own entry and column.
  Recommendation: A; no inventoried flight value differs (both publish 30 m maximum height,
  `coverage.py:61-65`), but the SDK facts do.
- **Q4. Tello driver thresholds (rows 24, 34-36).** (A) Read-only profile facts. (B) Editable within
  narrow bounds. (C) Constants outside the registry. Recommendation: A; they interpret the Tello's
  state stream inside the landing state machine, and a wrong value can confirm a landing that did
  not happen.
- **Q5. How the ground learns each unit.** (A) `unit_rev` in telemetry plus `describe_unit`. (B) The
  table in telemetry. (C) No report; trust the launcher. Recommendation: A; B breaks the field cap,
  C is wrong for edges on another computer.
- **Q6. Launcher edit surface for parameters.** (A) Existing fields plus the inventory, model and
  feature switches. (B) Also battery and link-loss fields. Recommendation: A for v1.
- **Q7. Custom models in v1.** (A) Include them, narrowing only, defined on the laptop. (B) Defer.
  Recommendation: A; pure code in PR-1/PR-2, cannot widen a bound, and the way a new drone is
  described without a code change.
- **Q8. Allowlist key.** (A) Name and value. (B) Name only. Recommendation: A.
- **Q9. Who may send `describe_unit`.** (A) Any connected ground. (B) Only the authority.
  Recommendation: A, so a standby ground can show the table.
- **Q10. Where the inventory lives.** (A) One file on the ground host, edited by the launcher there
  and by phones through the ground; Android also keeps one local unit for its standalone Tello mode.
  (B) Every device keeps its own inventory and they sync. (C) Phones read-only. Recommendation: A;
  one authority, no sync protocol.
- **Q11. What a phone may change.** (A) Availability, units of known models, and non-safety
  feature switches; never switch off a safety feature; never define custom models. (B) Everything
  the launcher can. (C) Nothing. Recommendation: A; it keeps "the laptop stays the mission console"
  (`docs/mobile-contract.md:103`) for anything that reduces protection.
- **Q12. Gating the existing `top_down` profile on a forward-only camera.** (A) Advisory: still
  selectable, with a warning. (B) Hard gate. Recommendation: A, so nothing that works today
  disappears; new thermal profiles are hard-gated.
- **Q13. Mixed patterns in one fleet.** (A) Bird's-eye search only when every planned unit supports
  and enables it; otherwise Plan is refused naming the units. (B) Per-zone patterns.
  Recommendation: A for v1; B is roadmap R4.
- **Q14. Default camera of `generic_multirotor`.** (A) Forward, fixed, RGB, which matches what the
  edge builds today; a unit declares a downward or gimbal camera to get bird's-eye search. (B) No
  default; every MAVLink unit must declare its camera. Recommendation: A, because B breaks every
  existing `--drone mavlink --source camera` command line; finding 11 is then resolved per unit.

## 20. Inventory: registry rows

Legend. Class: A airframe parameter, R profile fact, M mission/operator parameter. Edit: `ui`,
`file_env`, `read_only`. Applies: `start` (next Start), `ground` (on the ground, between flights),
`plan` (at Plan); nothing changes in flight. "n/a" means the row does not apply to that profile.
"new" marks a bound or default that does not exist in code today (ASSUMPTION A2 or A3). "conf"
marks a `confirm` parameter, with the less-safe direction. Line numbers without a branch are on
`origin/main`. Today's value is the `tello` value unless the row says otherwise.

| # | Parameter id | Where today (file:line) | Today | Unit | Class | Tello default | Generic default | Tello bound | Generic bound | Edit | Applies | Sources and notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `envelope.geofence_cm` | `drone/safety.py:107`, `launcher/settings.py:49`, `launcher/settings.py:61`, `safety.py:206-207`, `drone/fleet_plan_check.py:145`, `dashboard/app.py:565-575` | 500 edge, 450 launcher | cm | A | 450 (Q2) | 3000 | 40-5000 (lo new) | 500-50000 | ui | start | `FLIGHTRISK_GEOFENCE_CM` (room); `FLIGHTRISK_FIELD_GEOFENCE_M` x100 and `--geofence-m` (field). conf: higher. Finding 1. |
| 2 | `envelope.ceiling_cm` | `safety.py:130,134,136`, `safety.py:203-204,209`, `launcher/settings.py:50` | 200 | cm | A | 200 | 3000 | 150-1000 | 1000-12000 | ui | ground | `FLIGHTRISK_CEILING_CM`; `FLIGHTRISK_FIELD_CEILING_M` x100, `--ceiling-m`; `set_ceiling`. A measurement, no confirm. |
| 3 | `envelope.ceiling_margin_cm` | `safety.py:131,139`, `safety.py:205,210`, `launcher/settings.py:51` | 60 | cm | A | 60 | 500 | 30-500 (hi new) | 300-3000 (hi new) | ui | ground | `FLIGHTRISK_CEILING_MARGIN_CM`; `FLIGHTRISK_FIELD_CEILING_MARGIN_M` x100, `--ceiling-margin-m`. conf: lower. |
| 4 | `envelope.alt_slot_cm` (per drone) | `safety.py:108,122-123`; incident `safety.py:131`; `safety.py:201-202,208`; `edge_device.py:1614`; `launcher/settings.py:97` | 100 | cm | A | 100 | 1500 | 50-500 (50 from incident) | 1000-12000 | ui | ground | `--alt-slot` (`ALT_CM`); `--alt-slot-m`, `FLIGHTRISK_FIELD_ALT_SLOT_M` x100; launcher per drone; `set_ceiling`, `assign_lanes.alt_cm`. |
| 5 | `plan.alt_slots_cm` | `drone/coordinator.py:114`, `drone/fleet_patterns.py:176`, `launcher/settings.py:54`, `dashboard/ceiling.py:278` | (100, 130); (100, 160) in fleet_patterns | cm_list | A | (100, 130) | (1500, 2000) new | each in row 4 bound | each in row 4 bound | file_env | plan | Zone defaults; per-drone slots on the dashboard card are the UI. Finding 2. |
| 6 | `move.min_cm` | `safety.py:117`, `coordinator.py:133`, `drone/controller.py:106`, `drone/tello.py:571`, `fleet_protocol/constants.py:287`, `safety.py:197` | 20 | cm | R | 20 | 20 | 20-20 | 20-20 | read_only | start | Tello SDK fact. Finding 5. |
| 7 | `move.max_cm` | `safety.py:118`, `coordinator.py:134`, `controller.py:107`, `tello.py:572`, `fleet_protocol/constants.py:288`, `safety.py:198`; legacy `drone/mavlink.py:117` | 500; 5000 field | cm | R | 500 | 5000 | 500-500 | 5000-5000 | read_only | start | Tello SDK fact; legacy backend differs (finding 8). |
| 8 | `envelope.max_altitude_cm` | `controller.py:108`, `tello.py:573`, `mavlink.py:118` | 10 m Tello, 120 m default | cm | R | 1000 | 12000 | fixed | fixed | read_only | start | Declared in `DroneCapabilities`; no consumer found. Both Tello models publish 30 m (`coverage.py:61-65`, finding 10); FlightRisk keeps the stricter 10 m. |
| 9 | `envelope.fence_breach_margin_cm` | `drone/mavlink_fleet.py:283` | 300 | cm | A | n/a | 300 | n/a | 100-1000 new | file_env | start | GPS noise allowance before a breach landing. |
| 10 | `envelope.autopilot_fence_margin_cm` | `drone/autopilot.py:220` | 5 m | cm | A | n/a | 500 | n/a | 200-2000 new | file_env | start | Autopilot fence must contain the edge fence plus this. |
| 11 | `battery.min_takeoff_pct` | `safety.py:105`, `coordinator.py:122`, `launcher/preflight.py:49` | 30 | pct | A | 30 | 30 | 20-95 new | 20-95 new | file_env | start | `FLIGHTRISK_MIN_TAKEOFF_BATTERY`. conf: lower. Finding 3. |
| 12 | `battery.land_pct` | `safety.py:106`, `safety.py:424` | 15 | pct | A | 15 | 15 | 10-50 new | 10-50 new | file_env | start | New `FLIGHTRISK_LAND_BATTERY`. conf: lower. Finding 4. |
| 13 | `battery.warn_pct` | `config.py:140`, `dashboard/safety.py:26` | 20 | pct | M | 20 | 20 | 5-80 new | 5-80 new | file_env | start | Ground display (legacy single-drone path, `observability.py:151-152`). |
| 14 | `battery.critical_pct` | `config.py:141`, `dashboard/safety.py:27` | 10 | pct | M | 10 | 10 | 5-50 new | 5-50 new | file_env | start | Ground display. |
| 15 | `battery.rth_min_pct` | `dashboard/safety.py:21` | 40 | pct | A | n/a | 40 | n/a | 20-90 new | file_env | start | Only drones with return-to-home. conf: lower. |
| 16 | `link.loss_land_s` | `safety.py:98,104` | 3.0 | s | A | 3.0 | 3.0 | 2-10 new | 2-30 new | file_env | start | `FLIGHTRISK_LINK_LOSS_LAND_S`. conf: higher. The Tello lands itself after about 15 s without commands (`coordinator.py:146-153`). |
| 17 | `link.loss_hold_s` | `safety.py:112`, `edge_device.py:227` | 10 (field) | s | A | 0 | 10 | 0-0 | 0-60 new | file_env | start | `FLIGHTRISK_FIELD_LINK_LOSS_HOLD_S`, `--link-loss-hold-s`. conf: higher. |
| 18 | `link.failsafe_action` | `mavlink_fleet.py:264`, `edge_device.py:238` | rtl | enum | A | n/a | rtl | n/a | {rtl, land} | file_env | start | `FLIGHTRISK_FAILSAFE_ACTION`, `--failsafe-action`. |
| 19 | `link.require_failsafes` | `mavlink_fleet.py:266` | true | bool | A | n/a | true | n/a | {true, false} | file_env | start | conf: false. |
| 20 | `link.drone_stale_s` | `mavlink_fleet.py:269` | 3.0 | s | A | n/a | 3.0 | n/a | 1-10 new | file_env | start | Tello's equivalent is the SDK-coupled `_STATE_LOST_S` (constant). |
| 21 | `link.rtl_accept_s` | `mavlink_fleet.py:289` | 5.0 | s | A | n/a | 5.0 | n/a | 2-30 new | file_env | start | |
| 22 | `gps.min_satellites` | `autopilot.py:62`, `mavlink_fleet.py:267` | 8 | count | A | n/a | 8 | n/a | 6-20 new | file_env | start | conf: lower. |
| 23 | `manual.orphan_land_s` | `dashboard/manual_control.py:99-101,121-124` | 120 (min 30, clamped) | s | M | 120 | 120 | 30-600 (hi new) | 30-600 (hi new) | file_env | start | `FLIGHTRISK_MANUAL_ORPHAN_LAND_S`; ground scope; clamp becomes refusal. conf: higher. |
| 24 | `takeoff.reply_timeout_s` | `tello.py:91`, `dashboard/manual_control.py:130`, `mavlink_fleet.py:270` | 20 Tello, 60 MAVLink | s | R (Tello) / A (generic) | 20 | 60 | 20-20 | 20-180 new | read_only (Tello), file_env (generic) | start | Tello value matches djitellopy and is coupled to `_LAND_DEADLINE_S` (`tello.py:79-87`). The ground's manual wait derives from it. |
| 25 | `takeoff.late_s` | incident `flight_guards.py:94-95` | 10.0 | s | A | 10 | n/a | 2-20 | n/a | file_env | start | `FLIGHTRISK_TELLO_TAKEOFF_LATE_S`. conf: higher. |
| 26 | `takeoff.settle_samples` | incident `flight_guards.py:78-79` | 5 | count | A | 5 | n/a | 2-50 | n/a | file_env | start | `FLIGHTRISK_TELLO_SETTLE_SAMPLES`. conf: lower. |
| 27 | `takeoff.settle_tolerance_cm` | incident `flight_guards.py:81-82` | 10 | cm | A | 10 | n/a | 2-50 | n/a | file_env | start | `FLIGHTRISK_TELLO_SETTLE_TOLERANCE_CM`. conf: higher. |
| 28 | `takeoff.settle_timeout_s` | incident `flight_guards.py:86-87` | 5.0 | s | A | 5 | n/a | 1-10 | n/a | file_env | start | `FLIGHTRISK_TELLO_SETTLE_TIMEOUT_S`. |
| 29 | `takeoff.refusal_retries` | incident `flight_guards.py:98-99` | 2 | count | A | 2 | n/a | 0-3 | n/a | file_env | start | `FLIGHTRISK_TELLO_REFUSAL_RETRIES`. |
| 30 | `takeoff.refusal_hold_s` | incident `flight_guards.py:100-101` | 2.5 | s | A | 2.5 | n/a | 0.5-5 | n/a | file_env | start | `FLIGHTRISK_TELLO_REFUSAL_HOLD_S`. |
| 31 | `takeoff.descent_clearance_cm` | incident `flight_guards.py:107` | 50 | cm | R | 50 | n/a | 50-50 | n/a | read_only | start | Equals the lower slot bound: VPS floor 30 cm plus 20 cm. |
| 32 | `takeoff.height_cm` | `tello.py:2350` (fake), `mavlink_fleet.py:217,256` | about 80 Tello; 5 m MAVLink | cm | R (Tello) / A (generic) | 80 | 500 | 80-80 | 200-2000 | read_only (Tello), file_env (generic) | start | Tello hover height is firmware behaviour (`docs/ceiling-and-altitude.md`). |
| 33 | `mode.hover_test` | incident `flight_guards.py:74` | false | bool | M | false | false | {false, true} | {false, true} | file_env | start | `FLIGHTRISK_HOVER_TEST`; shown loudly. |
| 34 | `tello.ground_height_cm` | `tello.py:161` | 10 | cm | R | 10 | n/a | 10-10 | n/a | read_only | start | Land confirmation from the state stream (Q4). |
| 35 | `tello.ground_tof_cm` | `tello.py:166` | 30 | cm | R | 30 | n/a | 30-30 | n/a | read_only | start | (Q4). |
| 36 | `tello.ground_readings` | `tello.py:171` | 3 | count | R | 3 | n/a | 3-3 | n/a | read_only | start | (Q4). |
| 37 | `tello.temp_warn_c` | sensing `temp_preflight.py:42,47` | 90 | degC | A | 90 | n/a | 40-120 | n/a | file_env | start | `FLIGHTRISK_TELLO_TEMP_WARN_C`. |
| 38 | `tello.temp_refuse_c` | sensing `temp_preflight.py:44,47` | 95 or off | degC | A | 95 | n/a | 40-120, off | n/a | file_env | start | `FLIGHTRISK_TELLO_TEMP_REFUSE_C`; off_value `off`. conf: higher or off. |
| 39 | `sensing.turn` | `obstacle_sensor.py:103` | true | bool | M | true | true | {true, false} | {true, false} | file_env | start | `FLIGHTRISK_OBSTACLE_TURN`. conf: false. |
| 40 | `sensing.mode` | `obstacle_sensor.py:101`; sensing `launcher/settings.py:146` | required | enum | M | required | required | {required, off} | {required, off} | ui | start | `FLIGHTRISK_OBSTACLE_SENSING`. conf: off (existing precedent). |
| 41 | `sensing.step_cm` | `obstacle_sensor.py:45,47-48,50`, `edge_device.py:1833-1838` | 50; 500 field | cm | A | 50 | 500 | 20-100 | 20-5000 | file_env | start | `FLIGHTRISK_OBSTACLE_STEP_CM`. |
| 42 | `sensing.settle_s` | `obstacle_sensor.py:56,58` | 0.3 | s | A | 0.3 | 0.3 | 0.1-3.0 | 0.1-3.0 | file_env | start | `FLIGHTRISK_OBSTACLE_SETTLE_S`. conf: lower. |
| 43 | `sensing.min_safe_depth` | `obstacle_sensor.py:79,84`, `drone/obstacle.py:60`; sensing `launcher/settings.py:148` | 0.35 | ratio | A | 0.35 | 0.35 | 0.05-0.9 | 0.05-0.9 | ui | start | `FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH`; uncalibrated. conf: higher. |
| 44 | `sensing.person_fraction` | `obstacle_sensor.py:70,75` | 0.55 | ratio | A | 0.55 | 0.55 | 0.2-0.9 | 0.2-0.9 | file_env | start | `FLIGHTRISK_OBSTACLE_PERSON_FRACTION`; camera field of view. conf: higher. |
| 45 | `sensing.max_decode_lag_s` | `obstacle_sensor.py:93,95` | 0.3 | s | A | 0.3 | n/a | 0.1-2.0 | n/a | file_env | start | `FLIGHTRISK_MAX_DECODE_LAG_S`; Tello decoder only. conf: higher. |
| 46 | `sensing.view_wait_s` | `obstacle_sensor.py:61` | 0.7 | s | A | 0.7 | 0.7 | 0.2-3.0 new | 0.2-3.0 new | file_env | start | Camera latency. |
| 47 | `sensing.sense_timeout_s` | `obstacle_sensor.py:64` | 2.0 | s | A | 2.0 | 2.0 | 0.5-10 new | 0.5-10 new | file_env | start | The "2 s sense budget" of the issue. |
| 48 | `sensing.sense_retries` | `obstacle_sensor.py:66` | 3 | count | M | 3 | 3 | 1-5 new | 1-5 new | file_env | start | |
| 49 | `sensing.sensor_down_s` | `obstacle_sensor.py:68` | 10 | s | M | 10 | 10 | 2-30 new | 2-30 new | file_env | start | conf: higher. |
| 50 | `sensing.person_wait_s` | `obstacle_sensor.py:86` | 1.0 | s | M | 1.0 | 1.0 | 0.2-5 new | 0.2-5 new | file_env | start | |
| 51 | `sensing.retry_pause_s` | `drone/lane_flight.py:155` | 0.5 | s | M | 0.5 | 0.5 | 0.1-3 new | 0.1-3 new | file_env | start | |
| 52 | `move.heading_tolerance_deg` | `lane_flight.py:159`, `mavlink_fleet.py:214` | 10 | deg | A | 10 | 10 | 3-30 new | 3-30 new | file_env | start | Compass accuracy; one concept, two copies. conf: higher. |
| 53 | `sensing.floor_ratio_threshold` (reserved) | #192 `scripts/obstacle_replay.py:108` | 1.0 (offline) | ratio | reserved | - | - | - | - | - | - | Added with `sensing.metric` by the floor-ratio PR. Not counted in the 83. |
| 54 | `avoid.clear_votes` | #193 `obstacle_sensor.py:106-108` | 2 | count | M | 2 | 2 | 1-5 | 1-5 | file_env | start | `FLIGHTRISK_OBSTACLE_CLEAR_VOTES`. conf: lower. |
| 55 | `avoid.relook` | #193 `obstacle_sensor.py:112-114` | 2 | count | M | 2 | 2 | 0-5 | 0-5 | file_env | start | `FLIGHTRISK_OBSTACLE_RELOOK`. |
| 56 | `avoid.sidestep_cm` | #193 `obstacle_sensor.py:119-121` | 50 | cm | A | 50 | 0 new | 20-100, off 0 | 100-2000, off 0 | file_env | start | `FLIGHTRISK_OBSTACLE_SIDESTEP_CM`. Generic default off: untested on MAVLink (ASSUMPTION A3). |
| 57 | `avoid.sidesteps` | #193 `obstacle_sensor.py:124-126` | 2 | count | M | 2 | 2 | 0-4 | 0-4 | file_env | start | `FLIGHTRISK_OBSTACLE_SIDESTEPS`. |
| 58 | `avoid.sidestep_pass_cm` | #193 `obstacle_sensor.py:130` | 150 | cm | A | 150 | 1500 new | 50-400 new | 500-5000 new | file_env | start | Depth cue range (hardware item H1). |
| 59 | `plan.strip_cm` | `drone/plan_edit.py:62-63`, `drone/fleet_scale.py:54,129`, `dashboard/__main__.py:146` | 75 | cm | A | 75 | 1000 new | 40-1000 | 200-10000 | ui (map) | plan | `FLIGHTRISK_FLEET_STRIP_CM`, `--fleet-strip-cm`. Lower bound is `MIN_SWEEP_CM` (`fleet_plan_check.py:186`). Finding 6. |
| 60 | `plan.area_cm` | `dashboard/__main__.py:141`, `fleet_scale.py:128`, `fleet_plan_check.py:151`, `fleet_scale.py:48` | 400 x 400 | cm (w, h) | A | 400 x 400 | per mission | side 40-20000 | side 200-50000 | ui (map) | plan | `FLIGHTRISK_FLEET_AREA_CM`, `--fleet-area-cm`. Tello max side is twice its 100 m range. |
| 61 | `plan.zone_gap_cm` | `coordinator.py:111` | 100 | cm | M | 100 | 500 new | 40-1000 new | 200-5000 new | file_env | plan | |
| 62 | `plan.min_separation_cm` | `fleet_plan_check.py:136` | 150 | cm | A | 150 | 1000 new | 100-1000 new | 500-5000 new | file_env | plan | Airframe size plus position error. conf: lower. |
| 63 | `plan.placement_tolerance_cm` | `fleet_plan_check.py:139,201` | 10 | cm | M | 10 | 300 new | 0-500 | 0-500 | file_env | plan | |
| 64 | `plan.altitude_tolerance_cm` | `fleet_plan_check.py:177` | 10 | cm | A | 10 | 100 new | 5-50 new | 50-500 new | file_env | plan | Height-sensor error. |
| 65 | `plan.placement_offset_cm` | `fleet_patterns.py:170` | 50 | cm | M | 50 | 500 new | 0-500 new | 0-5000 new | file_env | plan | |
| 66 | `plan.takeoff_stagger_s` | `fleet_patterns.py:173`, `fleet_plan_check.py:202` | 2.0 | s | M | 2.0 | 2.0 | 0-60 | 0-60 | file_env | plan | |
| 67 | `plan.min_slot_separation_cm` | `fleet_scale.py:50` | 5 m (field only) | cm | A | 30 new | 500 | 20-200 new | 300-2000 new | file_env | plan | Room plans do not check it today (ASSUMPTION A3). |
| 68 | `plan.dedup_radius_cm` | `coordinator.py:709` | 80 | cm | M | 80 | 500 new | 20-500 new | 100-5000 new | file_env | plan | Merges detections on the shared grid. |
| 69 | `plan.lost_obstacle_s` | `coordinator.py:146` | 20 | s | A | 20 | 60 new | 15-120 new | 15-300 new | file_env | plan | Tello hover then auto-land; a MAVLink drone returns home instead. |
| 70 | `plan.camera_footprint_cm` | `plan_edit.py:70,73` | unset (strip) | cm | A | unset | unset | 20-5000 | 20-5000 | file_env | plan | `FLIGHTRISK_CAMERA_FOOTPRINT_CM`. |
| 71 | `plan.snap_target_cm`, `plan.snap_tolerance_cm` | `drone/plan_paths.py:67,70` | 25, 40 | cm | A | 25, 40 | deferred | deferred | deferred | read_only | plan | Derived from the Tello's 20 cm minimum move; allowlisted as `deferred` in v1 because the snap tolerance is a proven bound. |
| 72 | `plan.field_fence_margin_cm` | `fleet_scale.py:52` | 5 m | cm | A | n/a | 500 | n/a | 200-2000 new | file_env | plan | |
| 73 | `manual.step_cm` | `dashboard/manual_control.py:135` | 30 | cm | A | 30 | 100 new | 20-100 new | 20-1000 new | file_env | start | Must be a move the drone can fly. |
| 74 | `manual.rotate_deg` | `dashboard/manual_control.py:136` | 30 | deg | M | 30 | 30 | 5-90 new | 5-90 new | file_env | start | |
| 75 | `move.speed_cm_s` | `mavlink_fleet.py:258,308` | 3 m/s (max 15) | cm_per_s | A | n/a | 300 | n/a | 50-1500 | file_env | start | |
| 76 | `move.arrival_radius_cm` | `mavlink_fleet.py:272` | 1.0 m | cm | A | n/a | 100 | n/a | 30-500 new | file_env | start | |
| 77 | `move.arrival_alt_cm` | `mavlink_fleet.py:274` | 0.7 m | cm | A | n/a | 70 | n/a | 20-300 new | file_env | start | |
| 78 | `move.timeout_margin_s` | `mavlink_fleet.py:276` | 20 | s | A | n/a | 20 | n/a | 5-120 new | file_env | start | |
| 79 | `move.yaw_rate_dps` | `mavlink_fleet.py:278` | 30 | deg_per_s | A | n/a | 30 | n/a | 5-180 new | file_env | start | |
| 80 | `move.stationary_s` | `mavlink_fleet.py:293` | 3.0 | s | A | n/a | 3.0 | n/a | 1-30 new | file_env | start | Not tuned on a real aircraft. |
| 81 | `move.stationary_radius_cm` | `mavlink_fleet.py:224` | 0.5 m | cm | A | n/a | 50 | n/a | 10-300 new | file_env | start | |
| 82 | `avoid.autopilot_min_margin_cm` | `autopilot.py:386` | 2 m | cm | A | n/a | 200 | n/a | 100-1000 new | file_env | start | Smallest `OA_MARGIN_MAX` that counts as avoidance. conf: lower. |
| 83 | `avoid.autopilot_min_lookahead_cm` | `autopilot.py:388` | 5 m | cm | A | n/a | 500 | n/a | 200-2000 new | file_env | start | Smallest `OA_BR_LOOKAHEAD`. conf: lower. |
| 84 | `envelope.max_geofence_cm` | `safety.py:165,183,206`, `launcher/settings.py:61` | inf room (launcher 5000), 50000 field | cm | R | 5000 | 50000 | fixed | fixed | read_only | start | Becomes the upper bound of row 1 rather than a separate field; listed so `EnvelopeLimits.max_geofence_cm` has a source. Finding 9. |

Row 53 is the reserved row. Rows 6, 7, 8, 31, 34, 35, 36 and 84 are R; rows 24 and 32 are R for
`tello` and A for `generic_multirotor` and are counted as A. That gives 83 counted rows: 56 A,
8 R, 19 M (rows 13, 14, 23, 33, 39, 40, 48, 49, 50, 51, 54, 55, 57, 61, 63, 65, 66, 68, 74).

## 21. Inventory: constants that stay in code

Each entry is allowlisted in the guard test with its category.

| Category | Entries (file:line, value) | Count | Why it stays a constant |
| --- | --- | --- | --- |
| protocol | `fleet_protocol/constants.py:275` `HEARTBEAT_INTERVAL_S` 1.0; `:277` `TELEMETRY_INTERVAL_S` 0.5 (copy `lane_executor.py:187`); `:279` `PONG_BUDGET_S` 0.020; `:282` `HELLO_TIMEOUT_S` 2.0; `:272` `CLOCK_SYNC_MIN_SAMPLES` 4; `coordinator.py:108` `SYNC_ROUNDS` 8; `:287-288` `NUDGE_MIN_CM`/`NUDGE_MAX_CM` 20/500 (wire caps; the edge also checks rows 6-7); `:289` `DEFAULT_HOVER_TIMEOUT_S` 20; `:290` `MAX_HOVER_TIMEOUT_S` 120; `:300` `MAX_LANE_DISTANCE_CM` 100000; `:307` `MAX_FIELDS` 48; `lane_executor.py:199-200` `MAX_START_LEAD_S` 120, `MAX_START_LATE_S` 5; `coordinator.py:125` `TELEMETRY_FRESH_S` 2.0 | 15 | Both ends of a link must agree; a per-drone value would break interoperability. |
| sdk (Tello) | `tello_bound.py:89-91` ports 8889, 8890, 11111; `tello.py:90` `_COMMAND_REPLY_TIMEOUT_S` 7; `:122` `_TIME_BETWEEN_COMMANDS_S` 0.1; `:132` `_STATE_FRESH_S` 0.5; `:140` `_STATE_LOST_S` 3; `:178` `_MOTOR_OFF_S` 2; `:186` `_MOTOR_RUN_GAP_S` 1; `:114` `_TAKEOFF_CONFIRM_S` 6; `:115` `_TAKEOFF_GROUND_AFTER_S` 3; `:206` `_TAKEOFF_LIFT_GUARD_S` 10; `:87` `_LAND_DEADLINE_S` 25; `:211` `_LAND_GROUND_WAIT_S` 6; `:234` `_LAND_STALL_S` 1; `:239` `_LAND_DESCENT_STALL_S` 2; `:240` `_LAND_DESCENT_CM` 5; `:125` `_LAND_ATTEMPTS` 3; `:251` `_CONNECT_ATTEMPTS` 3; `:256` `_KEEPALIVE_INTERVAL_S` 10; `:257` `_KEEPALIVE_TIMEOUT_S` 5; incident `tello.py:169` `_TOF_OUT_OF_RANGE_CM` 6553; sensing `temp_preflight.py:49` `TEMP_READING_RANGE_C` (-40, 150); `coverage.py:105-106` video 960 x 720 | 24 | Facts of the Tello SDK, its firmware or djitellopy that the driver's landing and takeoff state machines are built around. Changing one is a driver change, not a setting. |
| sdk (MAVLink) | `autopilot.py:61` `GPS_FIX_3D` 3; `autopilot.py:224` `DEFAULT_SYSID` 1 | 2 | MAVLink enumeration and factory default. |
| regulatory | `autopilot.py:222` `MAX_FENCE_ALT_M` 120; `coverage.py:23` `MAX_ALT_M` 120 | 2 | 400 ft above ground level ([14 CFR 107.51](https://www.ecfr.gov/current/title-14/chapter-I/subchapter-F/part-107/subpart-B/section-107.51)); a legal ceiling, not a tuning value. |
| internal | `lane_executor.py:193` `_TELEMETRY_JOIN_S` 0.5, `:194` `MONITOR_INTERVAL_S` 0.05, `:196` `LAND_RETRY_S` 2; `lane_flight.py:173` `_SENSE_JOIN_GRACE_S` 0.5, `:188` `_START_SPIN_S` 0.02; `coordinator.py:136` `RETASK_WAIT_S` 2; `tello.py:242` `_STATE_POLL_INTERVAL_S` 2, `:123` `_REPLY_POLL_S` 0.01, `:267` `_STREAM_RESTART_PAUSE_S` 1, `:269` `_STREAM_CHANNEL_WAIT_S` 30, `:270` `_STREAM_CHANNEL_POLL_S` 0.05, `:56` `_FAILURE_LOG_INTERVAL_S` 10; incident `tello.py:137` `_SETTLE_POLL_S` 0.05; `tello_bound.py:108` `MAX_QUEUE_AGE_S` 2; `dashboard/ceiling.py:89` `REPLY_TIMEOUT_S` 5, `:91` `RECONNECT_GAP_S` 5; `manual_control.py:129` `MANUAL_REPLY_TIMEOUT_S` 4, `:131` `_RESUME_REPLY_TIMEOUT_S` 6, `:134` `_RELEASE_GRACE_S` 3, `:138` `_ORPHAN_LAND_RETRY_S` 2; `mavsdk_link.py:71` connect 30, `:73` command 10; `mavlink_fleet.py:212` `_POLL_S` 0.1, `:213` `_TAKEOFF_RETRY_S` 2, `:280` `param_refresh_s` 30; `obstacle.py:61` `check_interval` 0.5; `obstacle_sensor.py:88` `OBSTACLE_PERSON_CUE_DOWN_AFTER` 5; `launcher/preflight.py:47` `PROBE_TIMEOUT_S` 2.5; sensing `temp_preflight.py:52` `TEMP_QUERY_TIMEOUT_S` 2.5; `edge_device.py:276` `_FLEET_RECONNECT_MAX_DELAY_S` 2, `:284` `_FRAME_ERROR_GIVE_UP_S` 10; float slack 1e-6 (`safety.py:583`, `coordinator.py:172`, `plan_paths.py:84`) | 32 | Software timing, polling and log throttles. Not a property of the aircraft or the mission. `LAND_RETRY_S` is safety-relevant and deliberately not tunable. |
| algorithm and reference | `obstacle_sensor.py:77` `OBSTACLE_PERSON_CENTRAL_FRACTION` 1/3; #192 `obstacle.py:307-311` floor and top band fractions; `fleet_plan_check.py:183` `COVERAGE_LINE_SPACING_CM` 10; `fleet_plan_check.py:148` `MIN_COVERAGE` 0.995; `geo.py:15` `EARTH_RADIUS_M`; camera presets `coverage.py:52-98`, `fleet_scale.py:61-75`; `coverage.py:26` `DEFAULT_SWAP_TIME_S` 180 | 7 | Shape of an algorithm, numerical resolution, or published reference data with its own source. |
| legacy and out of scope | `mavlink.py:62-66` legacy backend timings; `mavlink.py:116-118` legacy capabilities; `geo.py:67,99,192` `alt_m=30` helpers; `search.py:64-289` pattern generator signature defaults (the coordinator passes explicit values, `coordinator.py:940`); `config.py:144` `mavlink_cmd_timeout` 30; `edge_device.py:249-250` frame-stride skip (vision performance) | 6 | Legacy single-drone paths or vision performance, not the fleet flight path. |
| test doubles | `fake.py:61-64,143-145`, `template_backend.py:67-69,102,135-136`, `edge_device.py:267` `_FAKE_SPEED_CM_S`, `FLIGHTRISK_FAKE_OBSTACLE_Y_CM` (`obstacle_sensor.py:111`) | 1 group | Simulation only. The fake drone takes its move range from the `tello` profile in PR-2. |

Totals: 89 constant entries, of which 43 are protocol, SDK or regulatory facts.

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

## 23. Decisions (2026-11-22)

The user asked the orchestrator to proceed with its recommended options without waiting for input. Every open question Q1-Q14 is therefore settled on the recommendation given above: Q1 environment wins everywhere; Q2 Tello geofence default 450 cm; Q3 tello_edu as its own catalogue entry sharing the Tello parameter column; Q4 Tello ground-detection thresholds and takeoff reply timeout read-only; Q5 unit_rev telemetry plus a describe_unit command; Q6 the launcher edits existing fields plus inventory, model and features; Q7 custom models in v1, narrowing only; Q8 allowlist keyed by name and value; Q9 any connected ground may send describe_unit; Q10 one inventory file on the ground host, phones edit through the ground, Android keeps a local unit for standalone Tello mode; Q11 phones may change availability, units of known models and non-safety features; Q12 top_down on a forward-only camera is an advisory warning; Q13 bird's-eye only when every unit supports it, otherwise refused; Q14 the generic MAVLink default camera is forward RGB. Any of these can be revisited before the matching PR starts.
