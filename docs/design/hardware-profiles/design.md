# FlightRisk fleet architecture v1

Status: architecture for review. It implements the approved product specification
`spec-v1.md` (beside this file, copied unchanged) and folds in issue #197 (hardware catalogue,
airframe profiles, inventory, capability-gated features) and every finding of the two PR #202 critic
rounds that still applies. Nothing here has been implemented. Base: `origin/main` at `b0027a2`. Line
numbers are on `b0027a2` unless a row says "incident" (#207) or "sensing" (#198). The parameter tables
are in `parameters.md`; the detailed #197 component design (catalogue fields, inventory schema, wire
shapes, camera roles, golden file, guard rules) is `hardware-profiles-detail.md`; the testable checklist
is `requirements.md`; the phased PR plan is `tasks.md`.

Production first: **MAVLink drones with GPS, flying in the field, are the production platform. The
Tello is the indoor reference and test adapter and never flies in the field.** Tello collision tuning
is tabled (the floor ratio, the H1 calibration, sidestep tuning, and any new Tello-only constants).
FlightRisk is loosely coupled: every rule names a capability, never a model.

## 1. Decisions in one page

1. **Capability model first.** A catalogue of drone models describes every fact of spec section 4.1
   (positioning, home and return, failsafes, limits and links, cameras and protection, reporting,
   search and tracking), each with its source and verification status. Rules read capabilities;
   an unknown fact takes its most conservative value; an unknown model gets the most conservative
   profile.
2. **The MAVLink/GPS adapter is the first concrete production adapter**, wired only through
   capabilities: its return method is the autopilot's return-to-launch, its positioning uncertainty
   comes from the GPS receiver, and its home is the autopilot's home at arming. The Tello adapter
   implements the same seams for indoor Dev/test with a dead-reckoned return.
3. **Return to home is the default safety action**, carried out by one edge-side return state machine
   per drone that can finish without the ground station. Landing in place is a fallback with a
   reason from a closed list (spec 6.3). RETURN ALL is the primary fleet control; LAND ALL stays the
   emergency control and is never gated, refused or delayed.
4. **Home is the drone's own takeoff point**, recorded once per flight and checked before takeoff.
   Without a ground station a drone returns only along a route inside its own zone and corridor at
   its slot, otherwise it lands after the wait.
5. **Separation grows with positioning uncertainty.** Two drones in one band keep
   base separation + uncertainty A + uncertainty B apart along every route; field bands are 5 m apart;
   indoor slots never count as separation. Indoor and field units never share a mission.
6. **A mission is a set of capability-matched tasks**, not equal zones. A matcher applies hard filters
   (required capabilities, features on, fences, range, battery to finish and return, uncertainty
   within tolerance) and a deterministic ranking, explains every choice and refusal, and never gives a
   task to an unfit unit.
7. **Only a human visual confirmation marks a target found.** Multi-target missions attribute every
   sighting to a target. After Confirm the operator chooses Continue or End search; End search runs
   the on-find policy (keep watch, RETURN ALL or LAND ALL). Converge and verify, the moving-target
   local search and escort (field only) are ground-planned tasks.
8. **The ground station enforces roles** (operator, supervisor, observer) on every event and route,
   deny by default. Every phone keeps LAND ALL and per-drone Land; only supervisor phones get Return.
9. **The #197 core stays:** a parameter registry with defaults and hard bounds per profile
   (`parameters.md`, 140 rows), pinned by a golden file; one resolver with sources; files never weaken
   safety; an inventory of owned units; capability-gated features with camera roles; frame metadata;
   the `describe_unit` query and `unit_rev` in the `hello`; and a literal guard test.
10. **Six phases, 23 PRs** (`tasks.md`): A core with no behaviour change, B MAVLink production
    adapter maturity, C the return-to-home default switch, D the task model and airspace, E the
    on-find flows, F mobile. Every PR states its SITL or fake-drone test plan and its behaviour
    changes.

## 2. Scope, environments and assumptions

**In scope:** everything spec v1 requires (sections 2 to 11) plus the #197 core. **Non-goals** are
the spec's (section 1): storage and protocol design beyond what this document fixes, tuning a real
rescue airframe, obstacle-sensor calibration and the tabled Tello collision tuning, regulation beyond
the spec's ceiling, fence and altitude rules, any automatic takeoff, precision landing and charging,
the DJI Mobile SDK adapter, in-flight gimbal moves and dual detection pipelines (PR #202 roadmap R3 to
R5), and the Android direct single-Tello mode.

**Environments** (spec 1 and 8.1): `field` (production; GPS drones only) and `indoor` (Dev/test; the
Tello family only until another room-scale model has a measured profile). The environment is a
capability fact of each model, not a model test, so a future room-scale drone with a measured profile
joins indoor missions without a rule change.

Assumptions (each is labelled where it is used):

- ASSUMPTION A1 (in-flight work): every PR of this plan starts after these merge: #207 (Tello incident
  fixes), #198 (sensing controls and temperature), #208 (geofence, slot and battery consistency; it
  keeps `MIN_START_BATTERY` as an alias of the single battery minimum), #211 (detection-profile
  telemetry grouping; worst-case telemetry 37 of 48 fields), the lost-drone manual re-land fix, and
  #205 (mobile models). #206 (floor ratio) is a draft and tabled. If any of them changes, PR A1
  updates the affected rows and the golden file before it opens.
- ASSUMPTION A2: bounds and defaults marked [PROPOSED] in `parameters.md` are the spec's proposals,
  not tuned values; [UNCALIBRATED] Tello placeholders stay pessimistic and their calibration is tabled.
- ASSUMPTION A3: a launcher and the edges it starts run the same installed version
  (`flightrisk/launcher/commands.py:245-277`).
- ASSUMPTION A4: MAVSDK exposes the GPS receiver's horizontal accuracy (`raw_gps` horizontal
  uncertainty) for ArduPilot and PX4; PR B1 verifies it in SITL and falls back to the autopilot's
  `GPS_RAW_INT` `h_acc` through a MAVLink passthrough if not.
- ASSUMPTION A5: MAVSDK's geofence plugin uploads inclusion and exclusion polygons and circles to both
  autopilots; PR B2 verifies a read-back in ArduPilot SITL and PX4 SITL.
- ASSUMPTION A6: a ground that does not know an optional key of `detections`, `hello`, `ack` or a
  telemetry object ignores it, as today's grounds ignore extra telemetry fields
  (`docs/open-drone-support.md`, "The fleet protocol is unchanged"); each PR that adds one tests it.
- ASSUMPTION A7: the Tello's camera is fixed and forward-facing; its 960 by 720 stream is UNVERIFIED
  (`flightrisk/drone/coverage.py:101-106`).

## 3. What exists today and is reused

| Existing piece | Where | Reused as |
| --- | --- | --- |
| Role protocols `Lifecycle`, `TelemetrySource`, `FlightControl`, `Motion`, `Navigation` (with `return_to_home`), `VideoSource`; `Capability`; `DroneCapabilities` | `flightrisk/drone/controller.py:67-99`, `:103-117`, `:310-416` (`return_to_home` at `:365`) | The adapter interface (section 17). |
| Backend registry and bootstrap | `flightrisk/drone/registry.py`, `flightrisk/drone/backends.py` | Backends declare their control protocol and the catalogue models they drive. |
| MAVLink fleet drone: preflight (3D fix, satellites, global position, home flag, battery, arming checks, failsafe parameters), return-to-launch for failsafe reasons with a land fallback, return altitude at or below the slot, external-control rules | `flightrisk/drone/mavlink_fleet.py:250-308` (options), `:376-385` (capabilities), `:694` (`check_slot`), `:741` (`preflight_problems`), `:1196-1270` (`land_for_reason`); `flightrisk/drone/autopilot.py:247` (`failsafe_problems`), `:398` (`avoidance_problems`), `:446` (`rtl_altitude_cm`), `:461` (`preflight_problems`) | The production adapter's preflight, home and return base (phase B). |
| Ground-link watchdog with the field hold, and the Tello's immediate landing | `flightrisk/drone/safety.py:319-345`, `:625-639`; `flightrisk/drone/lane_monitor.py:80-130` | The short and long link-loss events of the return state machine (section 10). |
| The lane executor's sensed steps, re-flight transit splitting and avoidance | `flightrisk/drone/lane_flight.py`, `lane_reflight.py`, `lane_avoid.py` | The Tello's dead-reckoned return flies its legs through the same sensed-step machinery. |
| Coordinator: zones, lanes, start gate, re-task, lost drones, LAND ALL | `flightrisk/drone/coordinator.py:489` (`start`), `:2541` (`on_drone_lost`), `:3328-3339` (start gate), `:3793` (`land_all`) | Becomes the executor of matched tasks; zone planning becomes one task generator. |
| Field plan scale from the camera's nadir swath | `flightrisk/drone/fleet_scale.py:1-23`, `:78-190`; `coverage.nadir_swath_m` (`coverage.py:326`) | Lane spacing and the bird's-eye pattern (sections 12.9, 11). |
| `assign_lanes` with `on_complete` `hover` or `land` | `flightrisk/fleet_protocol/constants.py:153`, `:165`; `validate.py:179-184` | Gains `return` (section 15). |
| Configuration commands, failsafe types | `fleet_protocol/constants.py:110-111`, `:125` | `describe_unit` is a separate query set; LAND ALL keeps its failsafe path. |
| Edge authority and watchdog feeding | `flightrisk/fleet_authority.py:93-140`, `:236-270`; `flightrisk/edge_device.py:785`; `flightrisk/edge_uplink.py:198`, `:287-289` | Queries never feed the watchdog (section 15). |
| Dashboard authentication: one API key for every client | `flightrisk/dashboard/app.py:204`, `:219` (`_require_api_key`); 45 Socket.IO handlers and 15 HTTP routes | Replaced by role keys and a per-event role table (section 13). |
| LAND ALL path; recovery lands every drone on a confirmed match; no backend implements `return_to_home` | `dashboard/app.py:1484` (`_land_all`), `:2925`; `dashboard/recovery.py:25`, `:189` | LAND ALL kept as the emergency path; recovery replaced by the on-find policy (section 12.6). |
| Orphan countdown that lands | `dashboard/manual_control.py:110-135` | The stranded countdown, ending in a return (section 10.6). |
| SITL: `scripts/start_sitl.sh` (instances on TCP 5760 + 10 N), `scripts/field_fleet_ground.py`, `tests/integration/test_sitl_fleet.py` (one and two vehicles, opt-in `sitl` marker); ArduCopter 4.6.3 SITL flew one- and two-drone field plans and a link-loss return | `docs/sitl-demo.md`, `docs/open-drone-support.md` ("Verification status") | The SITL fleet harness of section 18 extends them to 2 to 10 vehicles. |
| Everything PR #202 round 1 reused (detection profiles, `FrameStamp`, `ObstacleSettings`, `FlightGuards`, `TempLimits`, the 409 confirmation, launcher presets, the ceiling file, mobile fixtures) | `hardware-profiles-detail.md`, section 3 | Unchanged; the line numbers that moved are updated in `parameters.md`. |

## 4. Gaps this architecture closes

The spec's appendix A lists the gaps between today and the spec; each maps to a component and a
phase here:

| Gap (spec appendix A) | Component | Phase |
| --- | --- | --- |
| A Tello lands 3 s after the ground link goes silent | Return state machine: short loss holds, long loss returns inside its own zone | C |
| MAVLink holds 10 s then returns; no home checks or own-zone rule | Home checks, own-zone rule, shown reasons | B, C |
| The orphan countdown lands; a blocked way back loops into held | Stranded countdown ends in a return; a blocked return lands; no loop | C |
| LAND ALL is the only fleet control | RETURN ALL primary, per-drone Return, roles | C, F |
| Every Tello error lands it (`lane_flight.py:358-359`) | Fault classification: faults and unclassified errors land, others retry once and return | C |
| After its last lane a drone hovers; no `on_complete` | Re-task window, then return (`on_complete: return`) | C |
| Lands at 15 %; no return threshold or energy to home | Energy model, return threshold, landing reserve, critical battery | C |
| No backend implements `return_to_home` | Return methods: autopilot RTL (MAVLink), dead-reckoned (Tello) | B, C |
| A confirmed match lands every drone; no converge, local search or escort | Targets, sightings, on-find policy, converge, local search, escort | E |
| One target | Multi-target references and attribution | E |
| Fixed 75 cm lanes at the default speed | Search-speed rule from footprint, overlap and detection rate | D |
| Dead-reckoned pose treated as exact; no home marker | Uncertainty models, home markers, Start checks | B, C, D |
| A lost drone blocks paths for 20 s | No-entry area until a human confirms | D |
| Sensor down ends the lanes and the drone hovers | Retrace home, else land | C |
| A Tello with its drone link down is only reported | "Cannot be recalled" state | C |
| Every API-key client has the same rights | Role keys and the role table | C |
| Equal zones, one pattern | Capability-matched tasks | D |
| Link-loss time, battery levels and orphan time are env vars or constants | Registry rows with bounds and sources | A |

The #197 findings of PR #202 still apply where they are not fixed yet: findings 1 to 3 are fixed by
#208; findings 4 to 16 (land battery not configurable, Tello move range in neutral code, duplicated
strip and area, the orphan clamp, two legacy MAVLink move ranges, no upper room geofence bound, two
Tello heights, the MAVLink camera orientation, cameras described three times, Android constants,
privacy-screen key names, the Android 10 % landing, and `STEPPED_SENSING` on MAVLink) are carried by
the PRs named in `tasks.md`. Finding 15 (Android lands its direct-mode Tello at 10 %) is recorded but
not changed, because the Android direct single-Tello mode is a non-goal of the spec.

## 5. Architecture overview

```
 hardware catalogue ── capability facts with source and status, environment, return method,
   (flightrisk/hardware)  positioning and uncertainty model, failsafes, cameras, tracking
        │
 profiles + resolver ──── parameters.md rows: default and hard bound per profile, sources,
   (flightrisk/params)    golden file, file safety, exact confirmations
        │
 inventory + features ─── owned units, availability, camera roles, static and runtime gates
        │
 ┌──────┴───────────────────────────── ground station ───────────────────────────────────┐
 │ mission model (flightrisk/mission): targets, tasks, matcher, airspace (separation,      │
 │ bands, fences, no-entry), return ordering, sightings, on-find policy, converge,         │
 │ local search, escort controller, search-speed rule; roles (flightrisk/dashboard/roles)  │
 │ coordinator executes assigned tasks; dashboard and phones display and command           │
 └──────┬──────────────────────────────────────────────────────────────────────────────────┘
        │ fleet protocol v2: assign_lanes (+ zone, corridor, cleared areas, on_complete
        │ return), return, escort_setpoint, describe_unit query, hello unit_rev, rth telemetry
 ┌──────┴────────────────────────────────── edge ─────────────────────────────────────────┐
 │ lane executor (tasks as lanes) │ return state machine (flightrisk/drone/rth/)          │
 │ energy model │ uncertainty model │ home record │ fault classifier │ frame metadata      │
 │ adapters: MAVLink (production: autopilot RTL, fence upload, GPS home) │ Tello (test:     │
 │ dead-reckoned return) │ fake and SITL                                                  │
 └────────────────────────────────────────────────────────────────────────────────────────┘
```

The new pure packages (`flightrisk/hardware`, `flightrisk/params`, `flightrisk/mission`, and the
return state machine core in `flightrisk/drone/rth/`) import no Flask, vision or drone driver, so
they are unit-testable and importable by an edge-only install.

## 6. Capability model and catalogue (spec section 4)

### 6.1 Facts with source and status

Every fact of spec 4.1 is a `CapabilityFact(value, source, status)` with `status` in
`verified` (flown or measured), `repository` (stated by code or docs), `vendor` (vendor document),
`unverified`, or `proposed`. The dashboard and both apps show the status (AC-4.1). The catalogue
model (`DroneModel`, PR #202 section 6 kept) gains these fact groups:

| Group (spec 4.1) | Fields | Generic GPS quad (production) | Tello (Dev/test) |
| --- | --- | --- | --- |
| Positioning | `positioning` (`gps`, `rtk`, `dead_reckoning`); `uncertainty_model` (`receiver` or `dead_reckoning_growth`); `heading_ref` (`compass` or `power_on_relative`); `sensor_height_cm` | `gps`, `receiver`, `compass` (`autopilot.py` 3D fix and 8 satellites) | `dead_reckoning`, growth placeholders [UNCALIBRATED], `power_on_relative`, vision positioning 30 to 1000 cm (vendor) |
| Environment | `environment` (`field` or `indoor`); `indoor_profile_measured` | `field` | `indoor`, measured: true |
| Home and return | `home_source` (`autopilot_arming` or `pose_origin`); `return_method` (`autopilot_rtl`, `edge_flown_slot`, `edge_dead_reckoned`); `interruptible_moves` | autopilot home, `autopilot_rtl`, interruptible by mode change | pose origin reset at takeoff, `edge_dead_reckoned`, a land interrupts a move and a return cannot |
| Failsafes | `command_loss_action` and delay; `low_battery_action`; `fence_action`; `checkable_before_takeoff` | the configured autopilot action, checked before takeoff | lands itself about 15 s after the last command (repository), not configurable |
| Limits and links | move range, speed, altitude, endurance, range; `commands_without_ground` | autopilot; position hold | SDK 20 to 500 cm; nothing without the edge |
| Cameras and protection | camera mounts and roles (PR #202 section 6.2); `protection` (`edge_stepped`, `autopilot_avoidance`, `none`) | forward RGB by default; ArduCopter path planning or a companion camera, PX4 none | one fixed forward RGB [UNVERIFIED]; stepped sensing, uncalibrated |
| Reporting | `battery_trust` (`autopilot_estimate`, `coarse_pct`); `fault_visibility` | autopilot estimate; faults through status and failsafe modes | coarse percentage [UNVERIFIED]; faults only as error replies |
| Search and tracking | footprint per camera and altitude; `detection_rate_hz` (measured at runtime); `top_speed`; `tracking` (absolute positioning, interruptible moves, a camera that holds the subject from the stand-off) | tracking: yes if the camera holds the subject [UNVERIFIED for a fixed forward camera] | tracking: no |

The built-in models are `generic_multirotor` (production, simulation-only status), `tello` and
`tello_edu` (test adapter). The spec's "thermal field drone" is a documented custom-model template in
`docs/hardware-catalogue.md` (a downward or gimbal thermal camera), because no thermal pipeline exists
yet (roadmap R1); defining it in an inventory file makes the thermal tasks matchable.

### 6.2 Rules

- **Unknown facts are conservative.** The resolver fills a missing fact with its most conservative
  value (for example `tracking: false`, `return_method: none`, `protection: none`) and marks it
  `unverified`. An unknown model gets the most conservative profile: the intersection of all
  built-in profile bounds of its environment and the safer default of each (spec 10.1(1)).
- **Environment checks at Plan** (AC-4.2): a unit whose `environment` differs from the mission's is
  refused by name; indoors, a unit without `indoor_profile_measured` is refused by name.
- **Runtime capabilities** (protection, measured gimbal pitch, frames arriving, GPS fix) are checked at
  the takeoff preflight, fail-closed; a runtime capability lost in flight raises its event of spec 6.2
  (section 10.3). AC-4.3: changing a fake drone's declared capabilities changes its return method and
  its separation without a rule change; the fake drone takes its facts from the catalogue entry it is
  told to imitate.
- **Camera roles** (PR #202 round 1, F1, unchanged): each mount has one role at a time (`level`,
  `nadir` or `oblique`); a gimbal is level only with measured pitch; every sense gate on a gimbal
  returns `unavailable` when the measured pitch is not level; a gimbal needs a declared mission pitch.

## 7. Parameters, profiles and confirmations (spec section 10, #197)

### 7.1 Registry, profiles and golden file

The registry (`flightrisk/params/`) holds one row per configurable value: id with unit suffix, unit,
kind, scope (`drone`, `ground`, `mission`), a default and a hard bound per profile column, `safer`,
`confirm`, `safety_switch`, `edit`, `applies`, environment names and aliases with scale factors (a room
alias is never read for a field model), command-line flags, and cross-value constraints. The 140 rows
and the 65 constants that stay in code are in `parameters.md`. `hardware-profiles-detail.md` sections 7 and 10 are
kept, with these changes:

- **Profile columns:** `generic_multirotor` (production) and `tello` (test adapter; `tello_edu` shares
  it). In phase A **every generic value equals what today's code does for a MAVLink unit**, and every
  Tello value equals what it does for a Tello (round 2 finding B4). Values the spec changes are
  marked with the PR that changes them and listed in section 20.
- **Two bound sets until C4.** The golden file records `today_accepts` for every alias. Until PR C4 the
  resolver enforces `today_accepts`, so phases A and B refuse nothing that works today; C4 switches to
  the target bounds of `parameters.md` and section 20 lists every value that becomes refused.
- **Golden file and owners.** PR A1 commits `tests/golden/flight_params.json`, generated once by
  `scripts/make_param_golden.py` (which refuses to overwrite) from the code at the A1 base; tests
  compare the table with it value by value. GitHub cannot require the repository owner to approve
  their own PR, so `.github/CODEOWNERS` alone cannot enforce review in this single-owner repository
  (round 2 finding N6). The enforcement is therefore a test: any diff to `flightrisk/params/table.py`,
  the golden file or the literal allowlist must add an entry to `docs/design/hardware-profiles/
  safety-value-changes.md` naming the value, old and new, the reason and the section 20 item, and the
  critic review of every PR checks that log. `.github/CODEOWNERS` still lists those files, the return
  state machine, the role table and `fleet_protocol/constants.py`, so a second maintainer gets
  requested automatically when one exists.
- **Mission settings** (spec 10.3, rows 129 to 142) have scope `mission`, live in the mission record,
  may change in flight (`applies: in_flight`) except the approach distance and the escort stand-off and
  altitude, and every change is logged with its author.

### 7.2 Sources and precedence

Lowest to highest: profile, params file, inventory, launcher window, environment, command-line flag;
at runtime the ground (`set_ceiling`, mission settings) for `on_ground` and `in_flight` values. Every
effective value carries its source (spec 10.1(4)). The environment wins over the window (#202 Q1),
shown and confirmed when less safe.

### 7.3 Confirmations: one gate at Start that nothing bypasses (round 2 finding B1)

Round 2 found two bypasses: a custom model's less-safe default, and a dashboard started by hand
(no launcher, so no 409). The fix puts the authoritative confirmation where the spec puts it, at Start
on the ground (spec 9.4), fed by what every edge reports:

1. Every effective value, on the ground and on every edge (from `describe_unit`), carries `less_safe`,
   computed against the **built-in base column**, never a custom default. A custom model's default is
   just another source.
2. **Start** (the dashboard's `start_fleet`, however the dashboard was started) shows the Start
   confirmation of spec 9.4: each home or mark and heading, the sensing mode, the return altitude and
   threshold, every non-default safety value with its source, and the on-find policy. Every less-safe
   value and every safety feature switched off must be confirmed with its exact value, per unit:
   `confirm_params: [{"unit": "drone-1", "id": "envelope.ceiling_cm", "value": 260}]` (round 1 F19).
   Start is refused until the confirmation matches what the edges report now (a changed `unit_rev`
   voids it).
3. The launcher's 409 (PR #202 round 1, F4) stays as an earlier, friendlier gate for the processes it
   starts; it never replaces step 2.
4. A hand-started edge still refuses a less-safe value that comes from a file unless
   `--confirm-less-safe id=value` matches; a hand-started dashboard refuses a less-safe ground-scope
   value from a file the same way (`--confirm-less-safe` on the dashboard).
5. No file stores a safety feature as off (spec 10.3(3)).
6. **Simulated units** (adapter `sim`, the fake drone, and SITL vehicles marked `simulated` in the
   inventory) show their less-safe values but need no confirmation, because they cannot hurt anyone;
   this keeps rehearsals and test fixtures from turning into confirmations (round 2 finding B3).

### 7.4 Refusal wording (round 2 finding N8)

All operator texts come from one message catalogue (`flightrisk/params/messages.py`) with three
forms, each in plain words with numbers:

- refused input: "`envelope.ceiling_margin_cm` 20 cm from FLIGHTRISK_CEILING_MARGIN_CM is refused:
  the Tello allows 30 to 500 cm.";
- unavailable capability: "drone-1 cannot do bird's-eye search: the Tello has one fixed forward
  camera.";
- fallback landing (spec 6.3): "drone-1 landed in place: position uncertainty 62 cm is above the 50 cm
  bound."

A test renders every message once and checks that it names the subject, the value or reason, and the
limit.

## 8. Inventory (#197, spec 9.4)

`hardware-profiles-detail.md` section 8 is kept: `~/.flightrisk/inventory.json` with `rev` and compare-and-swap
writes, no secrets, units of built-in or custom models, availability, per-unit feature switches, gimbal
mission pitch, an implicit inventory when the file is missing, and Plan using the reported gates. Added
for the spec:

- each unit has an `environment` (from its model) and a `simulated` flag (section 7.3, step 6);
- **indoor units** have a listed mark and heading (spec 7.2): `"mark": {"label": "tape A",
  "x_cm": 0, "y_cm": 0, "heading_deg": 0}`; the Start confirmation lists them for the operator to
  confirm;
- phones may add units of built-in models and toggle availability and non-safety features (round 1
  F9, spec 9.4 "PR #202's limited inventory edits"); everything else is laptop-only.

## 9. Capability-gated features (#197)

The seven features of `hardware-profiles-detail.md` section 9.2 are kept with their static requirement, runtime
requirement and "when unavailable" behaviour. Round 2 finding N1 adds the column "if lost in flight",
which maps each runtime loss to its spec 6.2 event:

| Feature | If lost in flight |
| --- | --- |
| `forward_obstacle_sensing` | "Obstacle sensor fails mid-task": end and re-match its tasks; return by retracing flown airspace; land if no flown route reaches home (indoor); field drones with avoidance make a normal return |
| `onboard_avoidance` | The same event; a field drone without other protection returns at a slot above known obstacles |
| `level_search`, `birdseye_search` | Camera role lost (gimbal pitch not level or not nadir): the task is re-matched; the drone keeps flying its return normally |
| `thermal_detection` | Thermal frames stop: the thermal task is re-matched; the drone returns at task end |
| `appearance_match` | Banner on the cards; nothing else |
| `claude_second_opinion` | Banner "Claude second opinion is off" in the alert panel (spec 5.6) |

New capability-gated tasks of the spec (thermal sweep, bird's-eye sweep, escort) are gated the same
way: a task type is offered only if an available unit has its feature on (spec 5.2(5)).

## 10. Return to home (spec sections 6 and 7)

### 10.1 Components

| Component | Where | Role |
| --- | --- | --- |
| Return state machine | `flightrisk/drone/rth/machine.py` (pure, clocked) | One per edge. Consumes events, decides the action and the fallback reason, and drives a return method. Finishes without the ground (spec 6.1(7)). |
| Return methods | `flightrisk/drone/rth/methods.py` | `AutopilotReturn` (MAVLink: the autopilot's return-to-launch at the slot, through `land_for_reason`'s existing mode logic, `mavlink_fleet.py:1196-1270`); `EdgeFlownSlotReturn` (MAVLink: the edge flies the route at the slot when the ground orders a waypoint route); `DeadReckonedReturn` (Tello, Dev/test: section 10.5). Chosen by the capability `return_method`. |
| Home record | `flightrisk/drone/rth/home.py` | Records home at takeoff (field: the autopilot's home at arming; indoor: the pose origin), detects a changed or lost home, and answers "home known and valid". |
| Energy model | `flightrisk/drone/rth/energy.py` | Battery needed to reach home = distance home × `battery.return_rate_pct_per_100m` + expected waits × `battery.hover_rate_pct_per_min` + `battery.landing_reserve_pct`. Return threshold = the larger of `battery.return_floor_pct` and that sum. Field: uses the autopilot's remaining-capacity estimate when it reports one. |
| Uncertainty model | `flightrisk/drone/rth/uncertainty.py` | Field: the receiver's horizontal accuracy (ASSUMPTION A4). Indoor: grows by the `position.dr_growth_*` placeholders per metre, turn and minute of hover; never shrinks in flight. Predicts the uncertainty at home along the return route. |
| Fault classifier | `flightrisk/drone/rth/faults.py` | Classifies drone replies and autopilot status into `fault` (IMU, motor, positioning, unclassified: land in place) or `transient` (hover, retry once, return). Replaces "every Tello error lands it" (`lane_flight.py:358-359`). |
| Ground return coordinator | `flightrisk/mission/returns.py` | RETURN ALL and per-drone Return; orders conflicting returns; one landing at a time per landing area; sends releases (section 10.7). |

### 10.2 States

`flying` (a task or manual) → `holding` (short link loss, a blocked leg, a person cue, waiting for a
release) → `returning` (climb or descend into the slot, fly the route) → `waiting_to_return` (ordered
to wait by the ground, side by side at its own slot) → `descending_home` (only over home) →
`landed_at_home`; any state → `landing_in_place(reason)` → `landed_in_place(reason)`; and
`cannot_be_recalled` (the drone link is lost and the drone lands by itself, Tello). These are the
states spec 9.5 shows ("Searching", "Holding", "Keeping watch", "Converging", "At viewpoint",
"Local search", "Escorting", "Returning", "Waiting to return", "Landed at home", "Landed in place with
reason", "Cannot be recalled"); the task states come from the mission model.

### 10.3 Event table (spec 6.2)

Each row is one transition with its owner, its default action, its fallback (a spec 6.3 reason) and
its test. "SITL" means an ArduPilot SITL scenario (section 18); "fake" means the fake drone or the
simulated autopilot link.

| Spec 6.2 event | Detected by | Default action (field; Dev/test) | Fallback reason | Test |
| --- | --- | --- | --- | --- |
| Tasks done; mission complete | edge on `on_complete: return`; ground on mission end | hover `retask.window_s`, then return; mission complete sends RETURN ALL | per 6.3 | fake, SITL |
| RETURN ALL / per-drone Return | ground `return` command | stop the task (manual and held too) and return; ground orders conflicts. Dev/test: after the current move | unreachable: "not reached" plus what it will do alone | fake, SITL (4 drones) |
| LAND ALL / per-drone Land | today's failsafe path (`FAILSAFE_TYPES`, `fleet_protocol/constants.py:125`) | land in place at once; interrupts returns; discards queued commands | none; retried | existing tests; SITL during returns (AC-6.3) |
| Ground link lost, short | edge watchdog after `link.loss_detect_s` | hold; resume when the link returns | 6.3 | fake |
| Ground link lost, long; ground crash | after `link.loss_wait_s` more | return only inside its own zone and corridor at its slot; a standby ground may take over within the wait | "return route not cleared" | fake, SITL (inside and outside the zone) |
| Drone link lost; edge dies | edge (drone link status) | field: the autopilot's link-loss action (return or land, checked before takeoff); Dev/test: "cannot be recalled" | link back in flight: add drift to uncertainty, then return or land | SITL (kill the edge), fake |
| Tello no-command failsafe | adapter fact | keepalives run in flight, holds and returns; stop once a landing is decided | none | fake (Dev/test) |
| Low battery (return threshold) | energy model | return | 6.3 | fake, SITL (`SIM_BATT_*`) |
| Critical battery | `battery.critical_pct`; field: the autopilot's critical action | land in place at once, even mid-return | none | fake, SITL |
| Battery cannot reach home | energy model prediction | return early | "battery cannot reach home" | fake |
| Stranded countdown ends | ground (`stranded.countdown_s`) | return (today: land) | land; never back to held | fake |
| Obstacle on the way home | sensor gate or `STOPPED_SHORT` | hover, look again, try another cleared route | after `return.blocked_wait_s`: hold for a connected operator, else "way home blocked" | fake |
| Person cue on the way home | person cue, thermal detection | hover and alert; continue after a human confirms, a manual detour, or a land | after `return.person_cue_wait_s`: "way home blocked (person, no answer)" | fake |
| IMU, motor or positioning fault | fault classifier; autopilot status | land in place at once; the autopilot's failsafe is never fought | none | fake, SITL (`SIM_GPS_DISABLE`) |
| Command error that is not a fault | fault classifier | hover, retry once, return | land; unclassifiable errors are faults | fake |
| Uncertainty above its bound (Dev/test) | uncertainty model | land; earlier, when the prediction at home nears the bound, end the task and return | "position uncertainty N cm is above the M cm bound" | fake |
| Geofence or ceiling breach | envelope, autopilot fence | fence: return (the autopilot's fence action must be return or land); ceiling: land | land | SITL (fence), fake |
| Obstacle sensor fails mid-task | sensor down | end and re-match its tasks; retrace flown airspace home | "no flown route reaches home" | fake |
| Autopilot failsafe | autopilot mode | the autopilot wins; FlightRisk sends only operator lands, shows the reason, re-matches tasks | the autopilot's own | SITL |
| Sighting, Converge, Confirm, Not verified, escort rows | mission model (section 12) | see section 12 | see section 12 | fake, SITL |

### 10.4 Home (spec 7)

- **Recording:** once per flight at takeoff, never moved. Field: the autopilot's home at arming; a home
  the autopilot changes in flight makes home invalid ("no known home"), alerts the operator and lands
  in place (spec 7.5). Indoor: the pose origin; an edge restart in flight loses it ("no known home").
- **Before takeoff (spec 7.2, AC-7.1):** field: a 3D fix, at least `gps.min_satellites`, receiver
  accuracy within `home.accuracy_cm`, the autopilot's home within `home.max_offset_cm` of the current
  position, inside the mission fence, and at least `separation.landing_cm` from other homes or sharing
  a landing area. Indoor: the operator confirms each mark and heading; each mark is inside its fence,
  separated from other homes and outside no-entry areas; the ceiling is measured from the takeoff
  surface. Both: the return altitude (the drone's own slot) lies between the profile floor and the
  ceiling minus margin, and no unit gets work it cannot finish and still return with its reserve.
  Start names the drone and the check.

### 10.5 The Tello's dead-reckoned return (Dev/test only)

The edge flies home itself, keeping keepalives running. It first moves vertically back into its slot,
then follows a route made of legs of 20 to 500 cm: direct through operator-cleared areas (sent in the
plan as rectangles), otherwise retracing the airspace it already flew (the edge records its flown
polyline). Each leg goes through the existing sensed-step gate (`lane_flight.py`), so a blocked leg or
a person cue raises the events of section 10.3. With the obstacle sensor down it only retraces
(AC-6.4). It descends only over home. A return cannot interrupt a move, so a return starts after the
current move (spec 6.2). Nothing here is tuned; the uncertainty growth is a pessimistic placeholder.

### 10.6 Without the ground station (spec 7.4, decision 1)

The plan gives each drone its zone and its corridor (`assign_lanes` gains `zone` and `corridor`,
section 15). After a long link loss the edge returns only if the whole return route, widened by its
uncertainty, lies inside its zone and corridor at its slot; otherwise it lands after the wait with
"return route not cleared". Field: the autopilot's straight return path is checked the same way
before the edge asks for it. The stranded countdown (a held or manual drone nobody controls) ends in a
return; a blocked return then lands and never re-enters held (spec 6.2, AC-6.3).

### 10.7 RETURN ALL, ordering and landing (spec 7.4, 9.1)

- **RETURN ALL** sends `return` to every flying drone, manual and held ones too. Each drone's result is
  shown: returning, waiting, landing in place with its reason, or "not reached" with what it will do
  alone.
- **Ordering:** the ground checks every return route against all other drones (section 11). Conflicts
  go lowest battery margin first, then shortest route; a drone about to drop below its landing reserve
  goes first. Waiting drones get `return` with `wait: true` and hold at their own slot side by side,
  never stacked; the ground releases them with `wait: false`. If no order works, the drone lands in
  place. A waiting drone that loses the ground applies section 10.6.
- **Landing:** one at a time per landing area; homes closer than `separation.landing_cm` share an
  area.
- **Per-drone controls (spec 9.2):** Return is enabled whenever the drone flies and can be commanded,
  else it says why; Land is always enabled; taking control stops a return; resuming a task after a
  return is refused when battery, a fault or uncertainty caused it.

## 11. Airspace (spec section 8)

`flightrisk/mission/airspace.py` checks every planned task, transit, viewpoint, escort position and
return route:

- **One environment per airspace** (spec 8.1): Plan refuses a mixed mission (section 6.2).
- **Horizontal separation** (spec 8.2): two drones in the same band stay at least
  `separation.base_cm` + uncertainty A + uncertainty B apart, now and along every planned route.
  Indoor base 150 cm (today); field 500 cm (from D1).
- **Bands** (spec 8.3): field close band from `airspace.close_band_floor_cm` (close search, viewpoints,
  escort) and a sweep band at least `airspace.band_gap_cm` above it (bird's-eye and thermal sweeps).
  Drones in different bands may overlap horizontally except in the column where a sweep-band drone
  takes off, lands or descends. Indoors, slots sit under ceiling minus margin and never count as
  separation, so indoor drones are always separated horizontally (this replaces the slot rule of
  round 1 F7, round 2 finding N3: `plan.min_slot_separation_cm` applies only in the field).
- **Fences and no-entry areas** (spec 8.4): each drone has a fence around its home; the mission has a
  shared fence; tasks may add more. Every route is widened by the drone's uncertainty and must stay
  inside every fence and outside every no-entry area. Field: the autopilot's fence must contain
  FlightRisk's and act by return or land (uploaded in B3). Indoor: the fence is soft; moves that would
  leave it are refused.
- **No-entry areas** replace today's 20 s lost-drone timer (`coordinator.py:192`): a person cue, a
  lost drone (radius = its uncertainty + `noentry.lost_margin_cm`) or a stranded drone creates one; a
  drone enters only after a human confirms (spec 6.1(6)), including on its way home.
- **Precision degrades** (spec 8.5): the required separation grows at once; if the corridor still
  clears every other drone the drone carries on, otherwise it holds and is re-routed or sent home, and
  past its bound it lands. A field drone that loses its fix has a positioning fault: its autopilot acts
  and its area is no-entry until it lands.

## 12. Mission and task model (spec section 5)

### 12.1 Records

All in `flightrisk/mission/model.py`, pure and serialisable (the ground keeps them in its mission
store beside today's plan store, `flightrisk/dashboard/plan_store.py`):

- `Mission`: environment, search areas, targets, units, on-find policy, mission settings
  (rows 129 to 142), tasks, no-entry areas, airspace plan, log.
- `Target`: id, label, references (photo and embeddings per target), status (`searching`, `sighted`,
  `found`, `escorted`, `lost`).
- `Task`: id, type, place (area, segment or point with uncertainty), required and preferred
  capabilities, band, precision tolerance, progress, state (`unassigned` with reasons, `assigned`,
  `running`, `done`, `abandoned`), assignee, `needs_confirmation` (for a person cue or a no-entry
  area).
- `Sighting`: id, time, finder, geolocated point and uncertainty, per-target scores, attributed target
  (changeable), evidence per viewpoint, Claude opinion (an aid), state.

### 12.2 Task types and how they become flight

| Spec task type | Generator | Flown as |
| --- | --- | --- |
| Wide bird's-eye sweep | `field_scale` lanes at the sweep band from the nadir swath (`fleet_scale.py`) | `assign_lanes` at the sweep-band slot |
| Thermal sweep | the same with the thermal camera's footprint | `assign_lanes`, thermal pipeline (roadmap R1) |
| Head-on area search | today's zone and lane planner (`coordinator.py`, `plan_paths.py`) | `assign_lanes` in the close band |
| Close search of a flag, re-fly of a segment | flag uncertainty plus margin as a small area; today's re-flight segments (`lane_reflight.py`) | `assign_lanes` |
| Keep watch | a hover point that sees the sighting | `assign_lanes` with one point and `on_complete: hover` until released |
| Converge and verify | viewpoint ring (12.7) | one point per drone |
| Moving-target local search | outward search of the r(t) disc (12.8) | `assign_lanes`, re-planned as r grows |
| Escort (field only) | escort controller (12.9) | `escort_setpoint` commands |

The coordinator stays the executor: it sends lanes, watches acks, nacks and telemetry, re-tasks and
reports. Today's equal-zone plan becomes the head-on area search generator.

### 12.3 Matching (spec 5.2)

`flightrisk/mission/matching.py`:

1. **Hard filters**, never overridden: required capabilities; the feature on; the task and the routes
   to it and home inside the unit's fence and range (widened by uncertainty, section 11); battery for
   transit, task and return plus reserve (energy model); predicted uncertainty within the task's
   tolerance; the unit not held, manual, landing or in a fallback; every needed confirmation given.
2. **Ranking:** better positioning, then battery margin, shorter transit, idle before busy, not already
   blocked there; ties break on unit id.
3. One task per unit at a time; a sensing unit takes appended work only after its own tasks.
4. **Explanations:** for every task, why the unit was chosen and why each other unit was refused,
   shown in the plan and on the map.
5. **No match:** the task is listed unassigned with a reason per unit and shown on the map; never given
   to an unfit unit, never dropped. Matching reruns when a unit frees up (AC-5.1).
6. A task type is offered only if an available unit has its feature on.

### 12.4 Hand-off and re-task (spec 5.3, 5.4)

- A thermal flag becomes a person cue: a close-search task of the flag's uncertainty plus a margin is
  proposed with `needs_confirmation` and waits for a human (AC-5.2). A GPS unit with a level camera
  qualifies only if its uncertainty plus the flag's is within tolerance. The sweep continues and
  findings attach to the flag.
- A lost unit's unfinished tasks are matched again with their progress; its last known area becomes a
  no-entry area until a human confirms; held or manual units never get re-tasked work; a landed unit is
  never launched (AC-5.3, spec 6.1(4)).

### 12.5 Targets, sightings and verification (spec 5.5)

- Each target has its own references; the ground scores every detection against every target and
  attributes the sighting to the best match; the operator can re-attribute (AC-5.4). Vision builds one
  embedding set per target (today one, spec appendix A).
- On a sighting the finder keeps watch until its return threshold; the others continue. The operator
  answers on the laptop: Converge, Confirm found, Not verified, or Dismiss.
- **Only a human Confirm marks a target found** (spec 5.5(3), AC-5.5). Scores, the viewpoint count and
  the Claude second opinion are labelled aids.
- After Confirm: Continue (pre-selected while targets remain) or End search (pre-selected for the last
  target). Confirming the last unfound target ends the search.

### 12.6 On-find policy (spec 5.5(5))

`find.on_find_policy` is set at Plan and changeable at any time by the operator; every change is
logged. On End search: (a) keep watch (default): watchers and escorts stay until their return
thresholds or release, all others return; (b) RETURN ALL; (c) LAND ALL. This replaces
`recovery.py:189`, which lands every fleet drone on a confirmed match today; the legacy single-drone
path keeps its own behaviour.

### 12.7 Converge and verify (spec 5.6)

`flightrisk/mission/viewpoints.py`: N viewpoints on a ring around the last known location at no less
than `find.approach_distance_cm`, spread in bearing, each pair at least the required separation apart
(section 11), each drone at its own slot, never stacked. If the ring, fences or batteries cannot hold N,
fewer go and the operator sees why (AC-5.6). The Converge press is the confirmation to approach. Each
viewpoint reports person seen or not, per-target scores, the crop and "k of N viewpoints match"; the
Claude opinion is shown when on and consented, labelled as an aid, with a banner when it is off. The
verification window lapsing resumes converged drones, keeps the finder watching and leaves the alert
open.

### 12.8 Moving-target local search (spec 5.7)

`flightrisk/mission/local_search.py`: radius r(t) = sighting uncertainty + `find.running_speed_cm_s` ×
time since last seen, capped at `find.local_search_max_radius_cm`. The disc is cut to the mission
fence, the drones' fences and allowed zones, minus no-entry areas other than the subject's own. It uses
the converged drones, the finder or the escorts (changeable), works outward at the 12.10 speed,
re-plans as r grows, and shows the share searched. It stops on the operator, the time limit
(`find.local_search_time_s`), the maximum disc covered once, or every drone at its return threshold. A
new sighting resets centre and clock (AC-5.7).

### 12.9 Escort, field only (spec 5.8)

`flightrisk/mission/escort.py` runs on the ground at 1 Hz: from the subject's latest geolocated
position (detections carry `cam` and `pitch_deg`, section 17; `geolocate.pixel_to_ground`) it computes
each escort's setpoint at `escort.standoff_cm` horizontally on its own bearing, at
`escort.altitude_cm` in the close band, pairwise separated and never stacked, inside the fence and
allowed zones and outside other no-entry areas. It sends `escort_setpoint` (section 15); the edge flies
it with the MAVLink position target and holds if a setpoint is older than 2 s. Escort is refused
indoors with the reason of spec 5.8. If the subject crosses a limit, escorts stop at it and keep watch;
lost from view for `escort.lost_from_view_s`: hold, alert, start a local search. Hand-off: at the return
threshold plus `escort.handoff_margin_pct` a relief is matched; the escort leaves once the relief
reports the subject in view; with no relief the operator is alerted and the escort returns at its
threshold (AC-5.8).

### 12.10 Search speed and thoroughness (spec 5.9)

`flightrisk/mission/speed.py`: lane spacing ≤ footprint width × (1 − `search.required_overlap_pct`);
speed ≤ (1 − overlap) × footprint length along track × measured detection rate, capped by the
airframe's speed and, for stepped sensing, the step cycle (`search.step_cycle_speed_cm_s`, the Tello's
30 cm/s estimate). The edge reports its achieved detection rate in telemetry (`det_hz`, in the
`detection_profile` group of #211); the ground lowers a unit's commanded speed when the rate drops
(AC-5.9). The footprint comes from the camera, altitude and pitch (catalogue, section 6).

## 13. Roles and authentication (spec sections 2 and 9.4)

- **Credentials.** Three keys, each from the environment or `~/.flightrisk-fleet.env` like the edge
  token, never in a committed or synced file: `FLIGHTRISK_API_KEY` (operator, as today,
  `dashboard/app.py:204`), `FLIGHTRISK_SUPERVISOR_KEY` and `FLIGHTRISK_OBSERVER_KEY`. A connection's
  role is the role of the key it presents; with no keys configured, only loopback clients connect and
  they are operators (today's local-development behaviour; a non-loopback bind already needs the API
  key).
- **One role table** (`flightrisk/dashboard/roles.py`) maps every Socket.IO event and HTTP route (45
  and 15 today) to the roles allowed to use it. It is deny by default: a test enumerates every
  registered handler and route and fails if one is missing from the table. The handler checks the role
  before doing anything and answers a refusal with the role and the event.
- **What each role may do** is the spec's table (section 2): the operator everything; the supervisor
  RETURN ALL, LAND ALL, per-drone Return and Land, acknowledging alerts, and approving or refusing
  confirmations; the observer, from a phone, LAND ALL and per-drone Land only. Phones never answer
  confirmations or match alerts, change the on-find policy or safety settings, edit homes, or accept
  less-safe values, because those events are not on any phone's allow-list and the ground refuses them
  from a client that declared itself a phone in its handshake (`client: "ios"` or `"android"`).
- **Handshake:** on connect the ground sends `session` with the role and the list of events the client
  may emit, so the apps and the dashboard enable only what the role allows (AC-2, AC-9.2).

## 14. Operator controls and displays (spec section 9)

- **RETURN ALL** is first in the top bar, always visible, never disabled, one press, no dialog, its own
  shortcut (Shift+R). **LAND ALL** is always visible, never disabled, in the danger colour, Shift+L,
  placed apart from RETURN ALL, one press, no dialog, and overrides every return (AC-9.1). A client
  that cannot reach the ground says that nothing was sent and points to the local land keys or the
  drones' own controls.
- **Per drone:** Return and Land as in section 10.7; Take control and Resume search as today.
- **Map:** each home as a marker in the drone's colour with its id; positions with their uncertainty
  ("± N cm", "uncalibrated" while a placeholder); returns with route and progress; drones landed in
  place where they landed, with the reason; no-entry areas; tasks, assigned and unassigned, with
  reasons.
- **Drone cards** show home and distance, uncertainty, return reserve, the state of section 10.2, the
  countdowns ("Returning in 7 s unless the link returns", "HELD: returning home in 87 s") and each
  value's source (spec 9.5).
- **Match alert panel** (laptop only): attributed target (changeable), score per target, crop and
  viewpoint evidence, "k of N viewpoints match", the Claude opinion labelled as an aid, the verification
  countdown, unfound targets and the on-find policy; buttons Converge N, Confirm found, Not verified,
  Dismiss; after Confirm, Continue, End search and Escort N (field only); local-search and escort
  panels as in spec 9.3.
- Every automatic action is shown with its reason within 2 s on the dashboard and both apps, and is
  logged (AC-6.5).

## 15. Fleet protocol and telemetry

All additions are additive. New ground-to-edge types are protocol v2 only and are sent only to an edge
whose `describe_unit` reply advertises them; older edges are never sent them.

| Addition | Direction | Content | Older peers |
| --- | --- | --- | --- |
| `return` (fenced v2 command) | ground → edge | `wait` (true: hold at the slot as "waiting to return"; false: go now) | never sent to an edge without `return` in its reply; v1 fallback is today's `land` |
| `on_complete: "return"` | ground → edge in `assign_lanes` | plus `hover_timeout_s` as the re-task window | today's choices are `hover` and `land` (`fleet_protocol/constants.py:165`) |
| `zone`, `corridor`, `cleared`, `speed_cm_s` | ground → edge in `assign_lanes` | the own zone rectangle, the corridor polygon (at most 32 points, within `MAX_LISTED_NUMBERS`), operator-cleared rectangles (indoor), the commanded search speed | optional keys |
| `escort_setpoint` (fenced v2 command) | ground → edge | target position (plan frame and latitude and longitude), altitude, bearing, `valid_s` | field units only |
| `describe_unit` (read-only query) | ground → edge | `hardware-profiles-detail.md` section 12.2: answered from any ground, never feeding the watchdog, its own per-connection cache, at most one per connection per 2 s; the reply's `unit` now also carries the spec 4.1 facts, each with `status` | not in `CONFIG_COMMAND_TYPES` |
| `describe_unit` retry (round 2 finding N7) | ground | after a timeout (5 s) or a `busy` nack the ground retries at 2, 4, 8 and then every 30 s while the `unit_rev` is unchanged; Plan says which unit it is waiting for | |
| `unit_rev` in the v2 `hello` | edge → ground | hash of the start-time resolution, re-sent on every reconnect | ignored by older grounds |
| `rth` telemetry object | edge → ground | `state`, `reason`, `home` (plan-frame x and y, two numbers), `dist_cm`, `unc_cm`, `reserve_pct`, `threshold_pct`, `countdown_s`, `cannot_recall` | one top-level field: worst case 37 + 1 = 38 of `MAX_FIELDS = 48` after #211 |
| `det_hz` | edge → ground | achieved detection rate, inside the detection-profile telemetry object of #211 | no new top-level field |
| `detections` keys `cam`, `pitch_deg` | edge → ground | the camera and its pitch for each frame | ASSUMPTION A6 |

The edge's privacy screen rules (no key containing `feature`, `reid`, `frame`, `image` and the other
forbidden parts, `fleet_protocol/constants.py:198-255`) apply to every new key; the names above pass.

## 16. Mobile contract and apps (spec 9.4)

- **Every phone** keeps LAND ALL and per-drone Land, pinned; **supervisor phones** also get RETURN ALL
  (`return_all`) and per-drone Return (`drone_command {"command": "return"}`). Phones may also resume a
  held drone nobody controls, choose a detection profile and make the limited inventory edits. They may
  not plan, start, fly manually, answer confirmations or alerts (Confirm, Continue, End search,
  Converge, Escort), change the on-find policy or safety settings, edit homes or accept less-safe
  values. They never queue commands (spec 9.4).
- **New events a phone handles:** `session` (role and allowed events), `inventory`, `flight_params`,
  `inventory_ack`, `return_all_ack` (each drone's result), `targets` and `sightings` (read-only),
  `mission` (on-find policy, unfound targets, tasks with reasons, no-entry areas), and the new keys of
  `fleet_status` (home, distance, uncertainty, reserve, state, countdowns, reasons, cannot be
  recalled). Apps ignore unknown keys at every depth (`docs/mobile-contract.md`, "Compatibility
  rules").
- **New emits:** `return_all` and `drone_command` `return` (supervisor only), `get_inventory`,
  `get_flight_params`, `inventory_add_unit`, `inventory_set_available`, `inventory_set_feature`.
  Fixtures for every event and every role's allowed and forbidden emits are generated by Python.
- **iOS** stays an operator console. **Android** keeps phone-as-edge unchanged; its direct
  single-Tello mode is a non-goal of the spec and is not changed (finding 15 recorded, not fixed).
- Apps show the same states, homes and reasons as the dashboard within 2 s (AC-6.5, AC-9.2).

## 17. Adapters and frame seams (production first)

`hardware-profiles-detail.md` section 11 is kept, re-ordered so the production adapter comes first:

1. **MAVLink/GPS (production)**: `MavlinkFleetDrone` over MAVSDK (`mavlink_fleet.py`), frames from the
   companion computer's capture source (`--source camera` or `url`; it does not declare
   `STEPPED_SENSING`, `mavlink_fleet.py:376-385`), return method `autopilot_rtl`, positioning
   uncertainty from the receiver, home from the autopilot, fence uploaded before arming (B3).
2. **Tello (test adapter)**: `TelloController` (`tello.py`), frames from the drone's video, return
   method `edge_dead_reckoned`, positioning uncertainty from growth placeholders, home at the pose
   origin. No new Tello-only constant is added beyond the read-only SDK facts already registered.
3. **Fake and SITL**: the fake drone imitates a catalogue entry's capabilities (AC-4.3); SITL vehicles
   are MAVLink units marked `simulated` in the inventory.

Frame sources (`CV2FrameSource` `edge_device.py:307`, `DroneFrameSource` `:444`,
`ResilientCaptureSource` `:672`) are wrapped so every frame carries `FrameMeta` (camera, mount, sensor,
pitch, `FrameStamp`), and detections carry `cam` and `pitch_deg` to the ground, which escort and
geolocation need. One detection pipeline runs per unit.

## 18. Test strategy: SITL first for production behaviour

### 18.1 Tiers

| Tier | What | Where it runs | Gate |
| --- | --- | --- | --- |
| 1. Unit and fake | Every state-machine transition, matcher rule, airspace check, role rule, registry and resolver rule; the simulated MAVSDK link (`tests/test_mavlink_fleet_edge.py` style) and the fake drone | every PR, CI (Linux), the Mac, the ZBook (the suite is portable since #189) | required |
| 2. SITL fleet | ArduCopter SITL (and PX4 SITL for the PX4-specific paths) fleets of 2 to 10 vehicles with real edge processes and the real ground | opt-in `sitl` marker (`pytest.ini` excludes it by default); the Mac natively (ArduCopter 4.6.3 built natively, as verified in `docs/open-drone-support.md`) or a Linux machine | required for every PR marked "SITL" in `tasks.md`; its log is attached to the PR |
| 3. Hardware | Indoor Tello runs (Dev/test) and, later, field runs with a safety pilot | the ZBook for Tellos; field later | listed per PR; AC-6.6 |

### 18.2 The SITL fleet harness (PR B1)

- `scripts/sitl_fleet.sh --count N --autopilot ardupilot|px4 [--origin LAT,LON,ALT]` starts N vehicles,
  extending `scripts/start_sitl.sh` (instance N on TCP 5760 + 10 N; `--native` uses a local
  `sim_vehicle.py`). Each instance gets its own system id and a home offset on a grid so homes are
  separated (spec 7.2). PX4 instances use PX4's multi-vehicle SITL with MAVSDK ports 14540 + N
  (ASSUMPTION: verified in B1, since no PX4 SITL has run yet).
- `tests/integration/sitl_fleet.py` (pytest fixtures): starts the vehicles, sets the failsafe
  parameters the edge requires (as `tests/integration/test_sitl_fleet.py` does today), starts one real
  edge process per vehicle (`python -m flightrisk.edge_device --drone mavlink --source file` with replay
  frames, so no camera is needed) and the real ground (`scripts/field_fleet_ground.py` until D3, then
  the dashboard), and tears everything down.
- **Scenario catalogue** (`tests/integration/test_sitl_scenarios.py`), each a spec acceptance test:
  RETURN ALL with four drones whose return routes conflict (ordering, no stacking, landing one at a
  time); LAND ALL during returns (AC-6.3); long link loss inside and outside the own zone (AC-6.2);
  kill an edge (autopilot link-loss action); battery drain to the return threshold and to critical
  (SITL battery simulation parameters); GPS loss (`SIM_GPS_DISABLE`, a positioning fault); autopilot
  fence breach with the uploaded fence; Start refused for a missing fix or a far autopilot home
  (AC-7.1); capability matching with a mixed simulated fleet (a custom thermal model left unassigned
  without a thermal unit, AC-5.1); converge with three drones (AC-5.6); escort of a simulated subject
  that moves at running speed (a test detector emits geolocated detections along a path) with a relief
  hand-off (AC-5.8); local search growth (AC-5.7); and a ten-vehicle separation stress run (AC-8.1,
  AC-7.3).
- **Scale and timing:** SITL runs at real time (speed-up 1), because edge and ground timers are wall
  clock. Detectors are off or replaced by the test detector so ten edges fit on one machine
  (ASSUMPTION: ten ArduCopter instances plus ten edges fit on the Mac; B1 measures CPU and records the
  largest fleet that keeps telemetry at its rate).

### 18.3 The Windows ZBook's role

The ZBook (4 cores, no GPU, `docs/fleet-setup.md`) is the indoor Dev/test rig: two Tellos on two Wi-Fi
adapters (layout B), the hardware runs of AC-6.6 (a Tello whose ground link is cut returns and lands
within its uncertainty bound of the taped mark, ten runs) and of each Dev/test row of the event table,
and the Windows portability check of the full test suite and the launcher. It may also run the ground
station against a SITL fleet hosted on the Mac over the LAN, which exercises the field ground on
Windows; it is not the SITL host for large fleets.

## 19. The literal guard test (#197)

`hardware-profiles-detail.md` section 14 is kept (rules A to F, name words derived from the registry ids, allowlist
keyed by file, enclosing scope, kind, callee or target, name and value with exact counts, the Kotlin
check moved out with the Android direct mode). The scope adds `flightrisk/mission/` and
`flightrisk/drone/rth/` once they exist. Re-measured on `b0027a2` with the ids of `parameters.md`: 519
findings with 509 distinct keys in 57 of the 76 files in scope (`guard_main.txt` lists them;
`guard_rule_prototype.py.txt` is the prototype).

## 20. Behaviour changes, by phase (complete list)

Rule for every phase: **constructor defaults keep today's behaviour; only resolved defaults change.**
`SafetyEnvelope`, `LaneExecutor`, `ManualControl` and the coordinator keep today's constructor
defaults, and the new defaults arrive through the resolver at edge start, dashboard start and Plan. Tests
that build these objects directly therefore keep testing today's mechanisms (a land on link loss is
still reachable with `link.loss_action = land`), and only tests that go through a start path or a plan
default change. Each PR may edit only the existing assertions listed for it below; if any other
existing assertion fails, the PR stops and the change is split out for its own review. A parity test
(`tests/test_param_parity.py`, PR A1) probes every environment alias and flag at both ends of today's
range and the target bound and fails on any difference that is not listed here.

**Phase A: no behaviour change.** A5 changes how the launcher hands values to its processes
(`FLIGHTRISK_LAUNCHER_PARAMS` for edges and the ground, round 2 finding B2) but not the values; it
keeps today's rule that the window wins for the three variables the launcher strips
(`commands.py:44-46`) until C4. Assertions A5 may edit: `tests/test_launcher_commands.py:67`
(`--alt-slot` no longer passed) and `:119-121` (the three variables travel in the launcher layer).

**Phase B (field only; every change below concerns simulated MAVLink units today):**

1. B2: Start is refused for a field drone whose receiver accuracy exceeds `home.accuracy_cm`, whose
   autopilot home is farther than `home.max_offset_cm`, whose home is outside the mission fence, or
   whose home is too close to another without sharing a landing area (spec 7.2).
2. B2: an autopilot home that changes in flight lands the drone in place with "no known home".
3. B3: the edge uploads the drone's fence and the mission fence to the autopilot before arming; a failed
   upload or read-back, or a fence action other than return or land, refuses the takeoff.
4. B4: on a MAVLink unit the critical battery is the autopilot's critical action, checked before
   takeoff, instead of the edge landing at 15 %; the autopilot's link-loss action must be return or
   land; a lost GPS fix is a positioning fault that lands in place.

**Phase C:**

5. C2 adds RETURN ALL and per-drone Return (new controls; nothing existing changes) and moves LAND ALL
   apart from RETURN ALL in the top bar.
6. C3 enforces roles: with only `FLIGHTRISK_API_KEY` configured every keyed client is an operator, as
   today; a client that declares itself a phone is refused every event outside the phone allow-list
   (the apps never send those today).
7. C4, **the return-to-home default switch** (spec 10.1(7)): task end hovers `retask.window_s` then
   returns (today: hovers until landed); a long ground-link loss returns inside the own zone, else lands
   after the wait (today: a Tello lands 3 s after the link goes silent; `link.loss_wait_s` goes from 0 to
   10 s for the Tello); the stranded countdown ends in a return (today: lands); low battery returns at
   the return threshold and only critical battery lands in place (today: a Tello lands at 15 %); a
   command error that is not a fault retries once and returns (today: every Tello error lands it); the
   obstacle sensor failing mid-task retraces home (today: hovers).
8. C4: the Tello geofence default becomes 450 cm for an edge started without a launcher (#202 Q2); the
   launcher's "Rehearsal with fake drones" preset changes from 500 to 450 cm
   (`launcher/settings.py:562`, round 2 finding B3).
9. C4: the target bounds of `parameters.md` replace today's accepted ranges. Values accepted today that
   become refused: `FLIGHTRISK_GEOFENCE_CM` below 40 or above 5000; `FLIGHTRISK_FIELD_GEOFENCE_M` and
   `--geofence-m` below 5 m; `FLIGHTRISK_CEILING_MARGIN_CM` above 500; `FLIGHTRISK_FIELD_CEILING_MARGIN_M`
   and `--ceiling-margin-m` above 30 m; `FLIGHTRISK_MIN_TAKEOFF_BATTERY` below 20 or above 95;
   `FLIGHTRISK_LINK_LOSS_LAND_S` below 1.5 s or above 10 s (Tello) or 30 s (field);
   `FLIGHTRISK_FIELD_LINK_LOSS_HOLD_S` and `--link-loss-hold-s` below 1.5 s or above 30 s;
   `FLIGHTRISK_MANUAL_ORPHAN_LAND_S` below 30 s (refused instead of raised to 30 s) or above 600 s;
   `FLIGHTRISK_FLEET_STRIP_CM` and `--fleet-strip-cm` below 20 or above 1000 cm;
   `FLIGHTRISK_FLEET_AREA_CM` and `--fleet-area-cm` with a side below 20 or above 20 000 cm;
   `--speed-mps` below 0.5 m/s.
10. C4: inherited `FLIGHTRISK_GEOFENCE_CM`, `FLIGHTRISK_CEILING_CM` and `FLIGHTRISK_CEILING_MARGIN_CM`
    win over the launcher window, shown as warnings and confirmed when less safe (#202 Q1); the launcher
    writes the resolved ceiling values into the ceiling file.
11. C4: Start on the dashboard shows the Start confirmation of spec 9.4 and is refused until every
    less-safe value and every safety feature switched off is confirmed with its exact value (section
    7.3); simulated units need none.

Assertions C4 may edit: `tests/test_safety.py:206` (`(500, 30, 15)` becomes `(450, 30, 15)`);
`tests/test_manual_ground.py:415` (the cases `("5", 30.0)` and `("0", 30.0)` become refusals); and the
start-path assertions of the edge and dashboard start tests that pin a land on link loss, a land at the
orphan countdown or a land on every error when the defaults are resolved, which C4 lists in its PR
description before it starts. The tests that construct `SafetyEnvelope` with `link_loss_land_s=1.5`
(`tests/test_safety.py:193`, `tests/test_edge_uplink.py:206`) stay valid because the bound starts at
1.5 s (round 2 finding N2: 1.5 heartbeat intervals is the smallest wait that one missed 1 s heartbeat
plus 0.5 s of jitter cannot trigger). The shared launcher test fixture's `FLIGHTRISK_LINK_LOSS_LAND_S=5`
(`tests/test_launcher_server.py:110`) stays valid: 5 s lies inside the new 1.5 to 10 s bound, and the
fixture's fake drones are simulated units that need no confirmation (round 2 finding B3).

**Phase D:**

12. D1: the field base separation becomes 500 cm (generic `separation.base_cm`; today 150, applied only
    to map plans); a lost or stranded drone's area is no-entry until a human confirms (today: a 20 s
    timer, `coordinator.py:192`); indoor slots no longer count as separation.
13. D2: plans are capability-matched tasks; for an all-Tello indoor fleet the head-on generator produces
    exactly today's zones and lanes (snapshots for 1, 2, 3 and 4 Tellos, round 1 F7).
14. D3: field missions are planned on the dashboard; lane spacing and speed follow the search-speed rule;
    the field overlap default becomes 30 % (today 0.2 in `scripts/field_fleet_ground.py:99`).

**Phase E:**

15. E1: a confirmed match no longer lands every fleet drone (`recovery.py:189`); End search runs the
    on-find policy, keep watch by default. E1 edits the fleet-recovery assertions that expect every
    drone to land on Confirm, listed in its PR description.
16. E2 to E4 add converge, local search and escort, each started only by an operator press.

**Phase F:**

17. Supervisor phones gain RETURN ALL and per-drone Return; phones gain the limited inventory edits and
    the read-only mission, target and parameter views.

## 21. Risks

- **The default switch (C4) changes how every drone ends a flight.** It lands alone, after the return
  machinery has shipped behind today's defaults (C1, C2) and run in SITL, with its own critic review and
  the AC-6.6 hardware runs on the ZBook before merge.
- **SITL is idealised** (`docs/open-drone-support.md`, "Not verified"): no real GPS noise, wind or
  radios. Nothing here claims field readiness; the hardware path of `docs/open-drone-support.md`
  (tethered, then one drone with a safety pilot, then two) still applies.
- **PX4 has never run in SITL here.** B1 brings it up; until then PX4-specific paths are tested against
  the simulated MAVSDK only.
- **Ground load.** Escort setpoints at 1 Hz and ten-drone separation checks run on the ground; D1 and E4
  measure them in the ten-vehicle SITL run.
- **Roles misconfiguration** could lock a supervisor out or let a phone do too much. Deny by default,
  the `session` handshake and a role test per event bound the risk.
- **Telemetry headroom:** `rth` is the only new top-level field (38 of 48 after #211).

## 22. Decisions

### 22.1 From the approved spec (spec section 12, 2026-11-22)

1. No ground station: a drone returns only inside its own zone and corridor at its slot, else lands
   after the wait.
2. Indoor route home: direct through operator-cleared areas, retrace elsewhere.
3. A person cue on the way home with no answer: land after the person-cue wait.
4. Environments: non-GPS drones indoors only, GPS drones in the field, never mixed.
5. Phones: only supervisor phones get Return; every phone keeps Land.
6. On a find: a configurable on-find policy (keep watch, RETURN ALL, LAND ALL), optional converge and
   verify, optional moving-target search.
7. Verification is the human's visual confirmation; hints are aids; after Confirm, Continue or End
   search; multi-target missions; escort optional and field only.
8. Search bound: the radius grows at a running speed; drones search as fast as stays thorough.
9. Indoors: Tello family only until another room-scale drone has a measured profile.
10. Production versus test: MAVLink/GPS is production; the Tello is the indoor reference and test
    adapter; Tello collision tuning is tabled.
11. RTH is the default for safety actions, overriding #197's "Tello flights unchanged by default" for
    those actions only (spec 10.1(7)).

### 22.2 Carried from PR #202 (decided 2026-11-22, revised where noted)

Q1 the environment wins over the launcher window (applied in C4); Q2 the Tello geofence default is
450 cm (C4); Q3 `tello_edu` is its own catalogue entry sharing the Tello column; Q4 Tello driver
thresholds and SDK facts are read-only; Q5 `unit_rev` in the v2 `hello` plus the `describe_unit` query;
Q6 the launcher edits existing fields plus inventory, model, feature switches and gimbal pitch; Q7
custom models in v1, narrowing only; Q8 the allowlist keyed by file, scope, kind, callee or target,
name and value with counts; Q9 `describe_unit` is a read-only query from any ground, never feeding the
watchdog, with its own cache, a rate limit and the retry of section 15; Q10 one inventory on the ground
host with `rev` and compare-and-swap; Q11 phones add units of built-in models and toggle availability
and non-safety features only; Q12 `top_down` on a level camera is advisory; Q13 bird's-eye search only
when every planned unit supports it (now a task type: a bird's-eye sweep is matched only to units that
have it, and a mixed fleet runs different task types side by side in different bands, which supersedes
the "fleet-wide pattern" limit); Q14 the generic default camera is fixed forward RGB. D-F18: every
Tello height, distance and timing is a registry row, the SDK facts read-only and shown (rows 87 to 110,
marked test adapter).

### 22.3 Made in this architecture

- D-A1. The production adapter's return method is the autopilot's return-to-launch; the edge-flown
  slot route is used only when the ground orders a waypoint route around a conflict.
- D-A2. Confirmations are enforced at Start on the ground for every unit, from what the edges report;
  the launcher's 409 is an earlier gate only (round 2 B1).
- D-A3. Simulated units need no confirmation (round 2 B3).
- D-A4. Constructor defaults keep today's behaviour; resolved defaults carry the spec's (section 20).
- D-A5. Code-owner review is enforced by a safety-value change log checked by a test and by the
  critic, because the repository has one owner (round 2 N6).
- D-A6. The Android direct single-Tello mode is left unchanged (spec non-goal); its 10 % landing is
  recorded, not fixed (round 1 F14).
- D-A7. Escort setpoints are computed on the ground at 1 Hz and flown by the edge as MAVLink position
  targets with a 2 s validity.

### 22.4 Remaining questions

None. The spec answers every product question, and every design choice above has a stated
recommendation that this document adopts. If the consensus prefers another option for D-A1 to D-A7,
the PR that implements it is named in `tasks.md`.

## 23. How the PR #202 critic findings are resolved

| Finding | Status | Where |
| --- | --- | --- |
| Round 1 F1 to F19 | Kept as resolved in round 1, except F14 (Android direct mode is a spec non-goal, D-A6), F7's slot rule (superseded by spec 8.3, section 11) and the floor-ratio rows (tabled) | sections 6 to 9, 15, 19; `parameters.md` |
| **B1** custom-model defaults or a hand-started dashboard bypass the less-safe confirm | Fixed: less safe is measured against the base column; the authoritative confirmation is the Start confirmation on the ground, fed by every edge's report, whatever started the dashboard; a hand-started dashboard and edge refuse file-sourced less-safe values without `--confirm-less-safe` | section 7.3 |
| **B2** the launcher PR must merge after the edge and dashboard PRs, and the ground must read its launcher layer | Fixed: A5 (launcher) merges after A3 (edge) and A4 (dashboard); A4 makes the dashboard read `FLIGHTRISK_LAUNCHER_PARAMS` as its launcher layer | `tasks.md` A4, A5 |
| **B3** the rehearsal preset's 500 cm against the 450 cm base, and the shared fixture's `LINK_LOSS 5` turning tests into 409s | Fixed: the rehearsal preset becomes 450 cm in C4; simulated units need no confirmation; 5 s is inside the new bound | sections 7.3, 20 items 8 and C4 assertions |
| **B4** generic defaults must equal today's effective MAVLink values or be listed | Fixed: every generic value in phase A equals today's (sidestep 50, pass 150, zone gap 0, separation 150, placement tolerance 10, altitude tolerance 10, offset 50, dedup 80, lost obstacle 20, manual step 30, field slots 10 and 15 m, critical 15 %); later values are listed in section 20 | `parameters.md`, section 20 |
| N1 mid-flight gating column | Added | section 9 |
| N2 the 1.5 s reasoning | Stated | sections 20 (C4 assertions), `parameters.md` row 16 |
| N3 the slot rule | Replaced by spec 8.3 (indoor slots never separate; bands in the field) | section 11 |
| N4 safer and confirm for every row | Added as columns for all 140 rows | `parameters.md` |
| N5 owners | Every file has one owning PR, including `fleet_authority*.py`, `edge_uplink.py`, `obs_comm.py`, `single_search.py`, `recovery.py`, `roles.py` and the new packages | `tasks.md` |
| N6 CODEOWNERS | CODEOWNERS kept, enforcement by the safety-value change log test and the critic | section 7.1, D-A5 |
| N7 `describe_unit` retry | Added | section 15 |
| N8 refusal wording | One message catalogue, three forms, tested | section 7.4 |
| N9 the `MIN_START_BATTERY` alias #208 keeps | The registry maps it to `battery.min_takeoff_pct` and keeps it importable (re-export), so `single_search.py` and #208's alias keep working | `parameters.md` row 11, A4 |
| N10 nits | Fixed in the text | throughout |

## 24. External sources

- Tello SDK 2.0 User Guide, move commands 20 to 500 cm:
  https://xcopter.com/download/Tello%20SDK%202.0%20User%20Guide.pdf
- Tello User Manual v1.2: p.4 flight time and range; p.5 app-signal failsafe; p.7 vision positioning
  0.3 to 10 m: https://www.adorama.com/col/productManuals/RYTELO.pdf
- Tello EDU specifications: https://core-electronics.com.au/dji-tello-edu.html.md
- 14 CFR 107.51, 400 ft limit: https://www.ecfr.gov/current/title-14/chapter-I/subchapter-F/part-107/subpart-B/section-107.51
- ArduPilot MAVLink gimbal and mount (cited by `docs/sitl-demo.md`):
  https://ardupilot.org/dev/docs/mavlink-gimbal-mount.html
