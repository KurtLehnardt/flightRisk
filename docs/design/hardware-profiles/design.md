# FlightRisk fleet architecture v1

Status: architecture for review, revision 3. Revision 2 answered the round 1 critic of the v1
architecture (C-1, C-2, H-1 to H-11, M-1 to M-12, the nitpicks) and added the user direction that
Tello-coupled tuning values are read-only test-adapter facts; revision 3 answers the final critic
round (B-1 to B-4, N-1 to N-20 and the nits). Section 28 lists each resolution. It implements
the approved product specification `spec-v1.md` (beside this file, unchanged) and folds in issue #197
(hardware catalogue, airframe profiles, inventory, capability-gated features) and every earlier
finding on PR #202 that still applies. Nothing here has been implemented. Base: `origin/main` at
`82ec697`, which contains #211, #205 and #212; every line number below is on `82ec697` (#212 moved the
lost-drone code of `coordinator.py`, `dashboard/app.py` and `dashboard/manual_control.py`). Rows from in-flight branches say so ("incident" is #207, "sensing" is #198).
The parameter tables and every cross-value constraint are in `parameters.md`; the detailed #197
component design (catalogue fields, inventory schema, wire shapes, camera roles, golden file, guard
rules) is `hardware-profiles-detail.md`; the testable checklist is `requirements.md`; the PR plan is
`tasks.md`.

Production first: **MAVLink drones with GPS, flying in the field, are the production platform. The
Tello is the indoor reference and test adapter and never flies in the field.** Tello collision tuning
is tabled (the floor ratio, the H1 calibration, sidestep tuning, and any new Tello-only constants).
FlightRisk is loosely coupled: every rule names a capability, never a model.

## 1. Decisions in one page

1. **Capability model first.** A catalogue describes every fact of spec 4.1 with its source and
   verification status; rules read capabilities; an unknown fact or model is conservative.
2. **The MAVLink/GPS adapter is the first production adapter.** It returns under edge control: into
   its slot, then a GUIDED flight to above its home at the slot, a hold there until the landing is
   cleared, then LAND. The autopilot's own return-to-launch is the fallback when the edge is gone or
   the autopilot's failsafe acts, and B4 constrains every autopilot parameter that decides where and
   how that fallback flies and lands (section 13).
3. **Landing is terminal.** Every landing decision, from any source, enters the edge's state machine;
   from then on the edge refuses every motion command and every automatic return. External control (a
   pilot, a ground control station or the autopilot changed the mode) is a terminal sub-state: no
   automatic land, no retry, one LAND per operator command. On LAND ALL the ground marks every drone
   landing, sends the land, and only then cancels every other sender, asynchronously (section 10.2).
4. **Return to home is the default safety action**, finished by the edge without the ground; landing
   in place is a fallback with a reason from a closed list. RETURN ALL is primary; LAND ALL is the
   emergency control and is never gated, refused or delayed.
5. **The ground never lands a lost drone as a loss reaction and never hands its work away by itself**
   (#212's idle-controller countdown stays, as the spec's stranded event). It declares a
   drone lost only after the drone's own detect and wait times plus a margin; the lost drone's lanes
   wait for a human confirm; its last position, zone, corridor, return line and landing area become
   a no-entry area (section 12). This lands before any default switch.
6. **Without a ground station, a drone returns only inside its own zone and corridor.** The ground
   generates zones and corridors that never overlap within a band, a precomputed landing order for
   shared landing areas, and pushes no-entry areas to the edges (sections 10.9, 11). Each edge reports
   its autopilot's link-loss action and checks the straight line home of the autopilot's own return
   against every assignment it accepts, so no drone whose autopilot returns on link loss flies a task
   whose straight line home leaves its zone and corridor.
7. **Separation grows with uncertainty** (field: 2 × the receiver's 1-sigma accuracy plus speed ×
   telemetry age); two field bands with explicit floors and ceilings; homes at least the base
   separation apart; return slots above the highest known obstacle plus a margin.
8. **Confirmations cannot be bypassed.** Start is refused for any unit without a current
   description; "simulated" comes from the edge's report checked against the autopilot; less-safe
   values need an exact confirmation at Start, for in-flight changes and when a standby takes over.
9. **Roles come from credentials alone.** The operator key never leaves the ground host; phones and
   other laptops pair for per-device supervisor or observer tokens; supervisor tokens need TLS; LAND
   ALL and Land are allowed to the operator, supervisors and observer phones and never fail closed; a
   dashboard observer has them only with an explicit per-token land right, off by default (spec AC-2);
   cleartext observer sessions receive no imagery.
10. **Tello-coupled tuning values are read-only test-adapter facts.** The obstacle depth threshold,
    settle and steadiness tolerances, refusal caps, sensing timings, avoidance counts, temperature levels
    and the dead-reckoning placeholders appear only in the read-only effective-values table with their
    source: never a control, a preflight warning, a calibration request or a confirmation. Operator-facing
    settings are airframe-neutral mission and safety choices (sensing mode, return and land behaviour,
    separation, obstacle tops, the on-find policy) (section 7.5).
11. **Order: core, then the production adapter, then the field airspace and planning, then the return
    machinery, then the switch in four steps (warn only, bounds and confirmations, field defaults,
    indoor defaults), then mobile, tasks and the on-find flows.** 28 PRs (`tasks.md`).

## 2. Scope, environments and assumptions

**In scope:** everything spec v1 requires (sections 2 to 11) plus the #197 core. **Non-goals** are the
spec's (section 1), including the Android direct single-Tello mode, the DJI Mobile SDK adapter,
in-flight gimbal moves and dual detection pipelines.

**Environments** (spec 1, 8.1): `field` (production; GPS drones only) and `indoor` (Dev/test; Tello
family only until another room-scale model has a measured profile). The environment and
`indoor_profile_measured` are read-only facts of a built-in model; a custom model inherits them from
its base and can never set them (M-11).

Assumptions:

- ASSUMPTION A1 (in-flight work): every PR starts after #207 (Tello incident fixes), #198 (sensing
  controls and temperature, without its depth-threshold control, its dashboard depth display and
  telemetry field, its "not calibrated" warning and its messages that name environment variables),
  #208 (geofence, slot and battery consistency; keeps `MIN_START_BATTERY` as an alias), #216 (sidestep
  off by default; row 56 then reads 0) and #213 (mavsdk pinned below 4, Windows support in
  `flightrisk/drone/mavsdk_link.py` and `supervised.py`; it also edits `tests/integration/test_sitl.py`
  and `docs/sitl-demo.md`, which B1 owns) merge. #212 (lost drone flown by hand) merged in `82ec697`
  and is part of the base. #206 (floor ratio) is a draft and tabled. If any changes, A1 updates the
  affected rows and the golden file.
- ASSUMPTION A2: bounds and defaults marked [PROPOSED] are the spec's proposals; [UNCALIBRATED] Tello
  placeholders stay pessimistic and their calibration is tabled.
- ASSUMPTION A3: a launcher and the edges it starts run the same installed version
  (`flightrisk/launcher/commands.py:245-277`).
- ASSUMPTION A4: MAVSDK exposes the receiver's horizontal accuracy (`raw_gps` horizontal uncertainty)
  for ArduPilot and PX4; B2 verifies it in SITL and otherwise reads `GPS_RAW_INT.h_acc` through a
  MAVLink passthrough.
- ASSUMPTION A5: MAVSDK's geofence plugin uploads inclusion and exclusion polygons and circles to both
  autopilots; B3 verifies the read-back in ArduPilot and PX4 SITL.
- ASSUMPTION A6: a ground that does not know an optional key of `detections`, `hello`, `ack` or a
  telemetry object ignores it; each PR that adds one tests it.
- ASSUMPTION A7: the Tello camera is fixed and forward-facing; its 960 by 720 stream is UNVERIFIED
  (`flightrisk/drone/coverage.py:101-106`).
- A8 (resolved for PX4): the PX4 enumerations of section 13 come from the PX4 sources on `main`
  (`src/modules/commander/commander_params.yaml`, `src/modules/navigator/rtl_params.yaml`, section 29);
  the manual Position-mode parameter that revision 2 checked is not in the current reference and
  governs only manual flight, so it is dropped (B-4). B4 still reads the parameter
  metadata of the connected firmware and refuses a takeoff when a checked parameter is missing.
  ArduPilot's current sources name some return parameters with an `_M` suffix (`RTL_ALT_M`,
  `RTL_ALT_FINAL_M`, `RTL_CLIMB_MIN_M`), so B4 reads whichever name the connected firmware has
  (`SYSID_MYGCS` or `MAV_GCS_SYSID` likewise).

## 3. What exists today and is reused

| Existing piece | Where (`82ec697`) | Reused as |
| --- | --- | --- |
| Role protocols, `Capability`, `DroneCapabilities` | `flightrisk/drone/controller.py:67-99`, `:103-117`, `:310-416` (`return_to_home` at `:365`) | The adapter interface (section 20). |
| Backend registry and bootstrap | `flightrisk/drone/registry.py`, `flightrisk/drone/backends.py` | Backends declare their protocol and catalogue models. |
| MAVLink fleet drone: preflight, return-to-launch for failsafe reasons with a land fallback, return altitude at or below the slot | `flightrisk/drone/mavlink_fleet.py:250-308`, `:376-385`, `:694`, `:741`, `:1196-1270` | The production adapter (section 13). |
| Autopilot failsafe checks: fence, battery, RC loss and data-link loss actions, return altitude, system id | `flightrisk/drone/autopilot.py:190-245` (accepted value sets), `:247` (`failsafe_problems`), `:446`, `:461`; `docs/open-drone-support.md:107` (GCS failsafe "recommended but not checked") | Tightened and completed by B4 (section 13). |
| Ground-link watchdog with the field hold; the monitor that latches the first landing reason | `flightrisk/drone/safety.py:319-345`, `:625-639`; `flightrisk/drone/lane_monitor.py:80-130` (latch at `:89-97`) | Inputs of the return state machine; the latch becomes the terminal landing state (section 10.2). |
| Edge authority: landing never fenced or refused | `flightrisk/fleet_authority.py:10-14`, `:93-140`, `:236-270` | LAND ALL keeps its path; queries never feed the watchdog. |
| The executor's existing `return` sequence and `returning` phase (manual resume's way back) | `flightrisk/drone/lane_flight.py:206`, `:355`; `flightrisk/drone/lane_manual.py:848` | Kept; the new return-to-home names use an `rth` prefix and the command is `return_home` (nitpick 3). |
| Ground loss detection: `FLEET_LOSS_TIMEOUT_S = 5.0` | `flightrisk/dashboard/app.py:559-563` | Replaced by a derived per-unit timeout (section 12). |
| Coordinator: lost drones (immediate re-task and one best-effort land), the re-land of a lost drone that flies again, the 20 s lost-drone obstacle | `flightrisk/drone/coordinator.py:2654-2776` (`on_drone_lost`), `:1493` (`_reland_lost_locked`), `:3502-3533` (`_lost_obstacle_locked`, `LOST_OBSTACLE_S` at `:192`) | Replaced by section 12; #212's refusal while the lost drone may still fly is kept as a precondition. |
| Field plan scale, nadir swath | `flightrisk/drone/fleet_scale.py:1-23`, `:78-190`; `coverage.py:326` | Field planning on the dashboard (D3), bird's-eye sweeps. |
| `assign_lanes` with a required `zone`, `on_complete` `hover` or `land`, `hover_timeout_s` only with `hover` | `flightrisk/fleet_protocol/validate.py:135`, `:179-185`; `constants.py:153`, `:165` | Gains `corridor`, `landing`, `no_entry` and `on_complete: return_home` (section 18). |
| List caps: 16 numbers per list, 64 per message | `fleet_protocol/constants.py:259`, `:262` | Corridors and areas encode points as objects (M-7). |
| Dashboard authentication: one API key for every client | `flightrisk/dashboard/app.py:204`, `:219`; 45 Socket.IO handlers, 15 HTTP routes | Replaced by roles from credentials (section 16). |
| LAND ALL path; recovery lands every drone on a confirmed match | `dashboard/app.py:1497`, `:2938`; `dashboard/recovery.py:25`, `:189` | LAND ALL first cancels every sender (section 10.2); recovery replaced by the on-find policy. |
| Orphan countdown that lands, and #212's countdown for a lost drone whose controlling dashboard sends nothing (`FLIGHTRISK_MANUAL_LOST_IDLE_S`, 30 s, at least 5 s) | `dashboard/manual_control.py:58-70`, `:125-134`, `:155-171` | The stranded event (section 10.4, row 12); rows 23 and 157 of `parameters.md`. |
| SITL: `scripts/start_sitl.sh`, `scripts/field_fleet_ground.py`, `tests/integration/test_sitl_fleet.py`; native ArduCopter 4.6.3 on macOS | `docs/sitl-demo.md:81-124`, `docs/open-drone-support.md` | Extended to fleets of 2 to 8, the protocol roster cap (section 21). |
| The ground scores detections against the target; the edge only keeps the embeddings | `flightrisk/edge_device.py:810-813` | Multi-target scoring stays on the ground; no edge wire change (M-4). |
| Everything the #197 detail design reused | `hardware-profiles-detail.md`, section 3 | Unchanged; moved line numbers are in `parameters.md`. |

## 4. Gaps this architecture closes

| Gap (spec appendix A, plus review findings) | Component | PR |
| --- | --- | --- |
| A Tello lands 3 s after the ground link goes silent | Return state machine: short loss holds, long loss returns inside its own zone | C1, C7 |
| MAVLink holds 10 s then returns; no home checks, own-zone rule or failsafe completeness | Home checks, autopilot parameter table, edge-flown return with hold above home | B2, B4, C1 |
| The ground declares a drone lost after 5 s, lands and re-lands it, and re-tasks its lanes at once | Derived loss timeout, no ground landings, confirm before re-task, no-entry area | D2 |
| Corridors and landing order do not exist | Zone and corridor generation, non-overlap check, landing order | D1 |
| The orphan countdown lands; a blocked way back loops into held | Stranded countdown ends in a return; a blocked return lands; no loop | C2, C6, C7 |
| LAND ALL is the only fleet control and senders could undo it | RETURN ALL primary; landing terminal; LAND ALL cancels senders | C1, C2 |
| Every Tello error lands it (`lane_flight.py:358-359`) | Fault classification | C1, C7 |
| Lands at 15 %; no return threshold | Energy model with critical, energy to home, worst-case wait and reserve | C1, C6, C7 |
| A confirmed match lands every drone; one target | Targets, sightings, on-find policy, converge, local search, escort | E1 to E4 |
| Fixed lanes at the default speed | Search-speed rule | D5 |
| Dead-reckoned pose treated as exact; no home marker | Uncertainty models, home markers, Start checks | B2, C1, C2, D1 |
| Every API-key client has the same rights | Roles from credentials | C3 |
| Equal zones, one pattern | Capability-matched tasks | D4 |
| The dashboard cannot plan a field mission (`docs/open-drone-support.md:189`) | Field planning on the dashboard | D3 |

PR #202's #197 findings 4 to 16 are carried by the PRs named in `tasks.md`; findings 1 to 3 are fixed
by #208; finding 15 (Android direct-mode landing at 10 %) is recorded and not changed (spec non-goal).

## 5. Architecture overview

```
 hardware catalogue ── capability facts with source and status (environment and indoor profile
   (flightrisk/hardware)  read-only), return method, positioning and uncertainty model, failsafes,
        │                 cameras, tracking
 profiles + resolver ──── parameters.md rows and constraints, golden file, sources, file safety,
   (flightrisk/params)    exact confirmations, refusal messages
        │
 inventory + features ─── owned units, availability, camera roles, static and runtime gates
        │
 ┌──────┴───────────────────────────── ground station ────────────────────────────────────┐
 │ airspace (bands, separation, homes, zones and corridors, obstacle tops, no-entry areas,   │
 │ landing order) │ lost drones │ mission model (targets, tasks, matcher, sightings,         │
 │ on-find policy, converge, local search, escort, search speed) │ return coordinator       │
 │ (RETURN ALL, ordering, LAND ALL cancellation) │ roles and device tokens │ coordinator     │
 └──────┬──────────────────────────────────────────────────────────────────────────────────┘
        │ fleet protocol v2: assign_lanes (+ corridor, landing, no_entry, on_complete
        │ return_home), return_home, set_no_entry, set_speed, escort_setpoint,
        │ describe_unit query, hello unit_rev, rth telemetry object
 ┌──────┴──────────────────────────────────── edge ───────────────────────────────────────┐
 │ return state machine (flightrisk/drone/rth/): terminal landing, energy, uncertainty,     │
 │ home, faults, progress monitor │ lane executor │ frame metadata                          │
 │ adapters: MAVLink (production: edge-guided return, autopilot fallback, fence upload,    │
 │ parameter table) │ Tello (test: dead-reckoned return) │ fake and SITL                    │
 └──────────────────────────────────────────────────────────────────────────────────────────┘
```

## 6. Capability model and catalogue (spec section 4)

Every fact of spec 4.1 is a `CapabilityFact(value, source, status)`, status in `verified`,
`repository`, `vendor`, `unverified`, `proposed`, shown on the dashboard and both apps (AC-4.1). The
fact groups and the two built-in columns:

| Group | Fields | Generic GPS quad (production) | Tello (Dev/test) |
| --- | --- | --- | --- |
| Positioning | `positioning`, `uncertainty_model` (`receiver` or `dead_reckoning_growth`), `heading_ref`, `sensor_height_cm` | `gps`, `receiver`, `compass` | `dead_reckoning`, growth placeholders [UNCALIBRATED], `power_on_relative`, vision positioning 30 to 1000 cm (vendor) |
| Environment (read-only) | `environment`, `indoor_profile_measured` | `field` | `indoor`, true |
| Home and return | `home_source`, `return_method`, `interruptible_moves` | autopilot home; `edge_guided` with `autopilot_rtl` fallback; interruptible by mode change | pose origin; `edge_dead_reckoned`; a land interrupts a move, a return cannot |
| Failsafes | command-loss action and delay, low and critical battery actions, fence action, checkable before takeoff | the autopilot's, checked before takeoff against section 13 | lands itself about 15 s after the last command (repository) |
| Limits and links | moves, speed, altitude, endurance, range, `commands_without_ground` | autopilot; position hold | SDK 20 to 500 cm; nothing without the edge |
| Cameras and protection | mounts and roles; `protection` | forward RGB by default; ArduCopter path planning or a companion camera; PX4 none | one fixed forward RGB [UNVERIFIED]; stepped sensing, uncalibrated |
| Reporting | `battery_trust`, `fault_visibility` | autopilot estimate; status and failsafe modes | coarse percentage [UNVERIFIED]; error replies only |
| Search and tracking | footprint per camera, altitude and pitch; nominal `detection_rate_hz`; `top_speed`; `tracking` | tracking yes if the camera holds the subject [UNVERIFIED for a fixed forward camera] | tracking no |

Rules: a missing fact takes its most conservative value marked `unverified`; an unknown model gets the
intersection of the built-in bounds of its environment with the safer default of each; Plan refuses a
wrong-environment unit and, indoors, a unit without a measured profile, by name (AC-4.2; A4 refuses a
mixed fleet at Plan already, nitpick 12); a runtime capability missing at takeoff refuses the takeoff
(C1); one lost in flight raises its spec 6.2 event (C1; nitpick 7); the fake drone imitates a catalogue
entry, so changing its declared capabilities changes its return method and separation (AC-4.3).
Camera roles (one role per mount; a gimbal is level only with measured pitch; a gimbal needs a declared
mission pitch) are as in `hardware-profiles-detail.md` section 6.2.

**Simulated units (H-3(c)).** A unit is simulated only when its edge reports it and the report is
verified: a fake or sim adapter; or a MAVLink autopilot whose board identity is a desktop SITL build:
the board type in its `AUTOPILOT_VERSION` reply and the firmware's board name (ArduPilot SITL, PX4
`px4_sitl`), together with the simulator's parameters. Simulation parameters alone never count, because
PX4 SIH and HITL run on real flight controllers with simulation parameters (N-15). B1 records the exact
fingerprint each autopilot's SITL build reports, and a recorded SIH or HITL fixture is classified real. The inventory cannot make a unit
simulated. Only a verified simulated unit may use `--allow-unverified-failsafes` or
`link.require_failsafes = false`, and only it is exempt from confirmations.

## 7. Parameters, profiles and confirmations (spec section 10, #197)

### 7.1 Registry, golden file and change control

The registry (`flightrisk/params/`) holds one row per configurable value (id with unit suffix, unit,
kind, scope `drone`, `ground` or `mission`, default and hard bound per profile, `safer`, `confirm`,
`safety_switch`, `edit`, `applies`, environment names and aliases, flags) and every cross-value
constraint; both are listed in `parameters.md` (rows and constraints), which is the only source the
golden file is generated from. Changes from the previous revision:

- **Profile columns:** `generic_multirotor` (production) and `tello` (test adapter; `tello_edu` shares
  it). Until the PR that changes it, every generic value equals what today's code does for a MAVLink
  unit and every Tello value what it does for a Tello. A value that changes later carries the tag
  "from <PR>" naming exactly the PR of `tasks.md` that changes it; a test checks that every tag names
  an item of section 27 listed under the same PR (H-11).
- **Bounds:** until C5 the resolver enforces the range today's code accepts (`today_accepts` in the
  golden file); C4 only warns where a target bound would refuse; C5 enforces the target bounds.
- **Golden file:** `tests/golden/flight_params.json`, generated once by `scripts/make_param_golden.py`
  (refuses to overwrite) at the A1 base; tests compare `table.py` with it value by value.
- **Change control (M-9, round 2 N6):** `docs/design/hardware-profiles/safety-value-changes.md` is an
  append-only log. Each entry names the value, old and new, the reason, the section 27 item, and the
  SHA-256 of `flightrisk/params/table.py`, of the golden file and of the literal allowlist after the
  change. A test recomputes the three hashes and fails unless they equal the latest entry's, so any
  change to those files without a log entry fails, with no git diff involved. `.github/CODEOWNERS`
  lists these files, the return state machine, `roles.py`, `devices.py` and
  `fleet_protocol/constants.py`, and the critic of every PR checks the log; GitHub cannot make the
  sole owner review their own PR.

### 7.2 Sources and precedence

Lowest to highest: profile, params file, inventory, launcher window, environment, command-line flag;
at runtime the ground (`set_ceiling` and mission settings). Every effective value carries its source.
The environment wins over the window from C5, shown and confirmed when less safe.

### 7.3 Confirmations that nothing bypasses (H-3, round 2 B1)

1. **Less safe** is measured against the built-in base column. A custom model's default is another
   source.
2. **Start needs a current description of every unit.** Start is refused, naming the unit, for any
   unit whose edge has not answered `describe_unit` for its latest `unit_rev`, including older edges
   and edges on a protocol v1 link (H-3(b)). The Android phone-as-edge flies nothing and is not a
   unit, so it is not affected.
3. **The Start confirmation** (spec 9.4) lists each home or mark and heading, the sensing mode, the
   return altitude and threshold, every non-default safety value with its source (never a test-adapter
   fact, section 7.5), and the on-find policy; every less-safe value and every safety feature switched off (including a test-adapter safety switch
   turned off by a developer override, section 7.5) must be confirmed with its exact value per unit (`confirm_params: [{"unit", "id", "value"}]`). It runs on the dashboard however the
   dashboard was started, and also in `scripts/field_fleet_ground.py` as long as that script plans
   field missions (H-3(a)): the script prints the same list and needs `--confirm unit:id=value` for
   each, and its `--allow-unverified-failsafes` and `--link-loss-hold-s` values count as values to
   confirm unless every unit is verified simulated.
4. **Until the confirmation exists (A1 to C4), a less-safe custom-model default is refused** at
   resolve time with "this custom default is less safe than its base; a confirmation for it arrives in
   C5" (H-3(d)). A less-safe value from a file needs `--confirm-less-safe id=value` on a hand-started
   edge or dashboard, as before.
5. **In-flight changes (H-3(e)):** a mission setting changed in flight to a less-safe value (for
   example a lower escort hand-off margin, row 141) needs the same exact confirmation on a laptop, from the
   operator or from a supervisor laptop where section 16.2 allows it, before it takes effect (N-20).
6. **Standby takeover (H-3(f)):** a standby ground that becomes the authority shows its ground-scope
   values that differ from the previous authority's and are less safe; until the operator or a
   supervisor laptop confirms them it sends no new plan, start or re-task. LAND ALL, Land, RETURN ALL,
   return releases and landing clearances are never gated (spec 9.4: returns need no confirmation), so a
   takeover during RETURN ALL never leaves a waiting drone without its release (N-5).
7. **No file stores a safety feature as off** (spec 10.1(3)).
8. **Verified simulated units** need no confirmation (section 6).

### 7.4 Refusal wording (round 2 N8)

All operator texts come from `flightrisk/params/messages.py` in three forms, each in plain words with
numbers: refused input ("`envelope.ceiling_margin_cm` 20 cm from FLIGHTRISK_CEILING_MARGIN_CM is
refused: the Tello allows 30 to 500 cm."), unavailable capability ("drone-1 cannot do bird's-eye
search: the Tello has one fixed forward camera."), and fallback landing ("drone-1 landed in place:
position uncertainty 62 cm is above the 50 cm bound."). A test renders every message.

### 7.5 Tello-coupled values are test-adapter facts (user direction)

Every Tello-coupled tuning value has `edit: fact` in `parameters.md` (32 rows, including #207's refusal cap and fence margin): the obstacle depth
threshold (`sensing.min_safe_depth`), the takeoff settle and steadiness values, the refusal retries and
hold, the late-takeoff limit, the temperature levels, every sensing timing and count, the clear votes,
re-look and sidestep values, the turn and hover-test switches, and the dead-reckoning growth
placeholders. They are shown only in the read-only effective-values table with their source. They are
never a launcher or dashboard control, never stored in a params or inventory file, never a preflight
warning, never a calibration request and never part of a Start confirmation. Their existing environment
variables remain developer overrides for test-adapter bring-up only (logged at start, shown with source
`env`; decision R-5), honoured only on test-adapter units and verified simulated units; a production unit
ignores them with a log line naming the variable (N-10). Two facts are safety switches: the temperature
refusal (`tello.temp_refuse_c` off) and the sensed turn (`sensing.turn` false). Their off value set by an
override is listed in the Start confirmation as a safety feature switched off and must be confirmed
(7.3(3)). The hover-only test (`mode.hover_test`) is a run mode: #207's launcher warning about an
inherited `FLIGHTRISK_HOVER_TEST` stays (N-16). These are the only exceptions to the rule above. The operator-facing settings are airframe-neutral mission and safety choices:
sensing mode, return and land behaviour, separation, obstacle tops and the on-find policy.

A check that uses a fact still reports its result in plain words (for example "drone-1 is too hot to
take off: 62 °C"), because that is a fact about the drone, not a setting; what is never shown is the
fact as something to change, tune or calibrate, or a warning that it is uncalibrated. [PROPOSED] and
[UNCALIBRATED] marks stay in `parameters.md` for developers only.

## 8. Inventory (#197, spec 9.4)

As in `hardware-profiles-detail.md` section 8, with `rev` and compare-and-swap writes, no secrets,
units of built-in or custom models, availability, per-unit feature switches, gimbal mission pitch and
an implicit inventory when the file is missing. Added: each unit has an `environment` from its model;
indoor units have a listed mark and heading (spec 7.2) that the Start confirmation lists; phones with a
supervisor token may add units of built-in models and toggle availability and non-safety features
(section 16); everything else is laptop-only.

## 9. Capability-gated features (#197)

The seven features of `hardware-profiles-detail.md` section 9.2 keep their static requirement,
runtime requirement and "when unavailable" behaviour; the "if lost in flight" column maps each loss to
its spec 6.2 event:

| Feature | If lost in flight |
| --- | --- |
| `forward_obstacle_sensing` | "Obstacle sensor fails mid-task": end and re-match its tasks; return by retracing flown airspace (indoor) or at a slot above the obstacle top (field); land if no route reaches home |
| `onboard_avoidance` | The same event |
| `level_search`, `birdseye_search` | Camera role lost: the task is re-matched; the drone returns at task end |
| `thermal_detection` | Thermal frames stop: the thermal task is re-matched |
| `appearance_match` | Banner on the cards |
| `claude_second_opinion` | Banner "Claude second opinion is off" in the alert panel |

A task type is offered only if an available unit has its feature on (spec 5.2(5)).

## 10. Return to home (spec sections 6 and 7)

### 10.1 Components

| Component | Where | Role |
| --- | --- | --- |
| Return state machine | `flightrisk/drone/rth/machine.py` (pure, clocked) | One per edge. Every safety input, every operator command and every landing decision enters it; it decides the action and the fallback reason and drives a return method. Finishes without the ground. |
| Return methods | `flightrisk/drone/rth/` | `EdgeGuidedReturn` (MAVLink primary: move vertically into the slot, GUIDED flight to above home at the slot, hold there until the landing is cleared, then LAND); `AutopilotFallback` (MAVLink: observes and constrains the autopilot's own return when the edge did not start it or the edge is gone); `DeadReckonedReturn` (Tello, Dev/test). Chosen by the capability `return_method`. |
| Progress monitor | `rth/progress.py` | Runs only during horizontal return legs (B-2): a leg that does not bring the drone `return.progress_min_cm` closer to home within `return.progress_timeout_s` counts as blocked (spec 6.2, "obstacle on the way home"). It is paused in every wait (`rth_waiting`), the hold above home (`rth_above_home`), the vertical move into the slot, the autopilot's climb and loiter phases (`RTL_LOIT_TIME`, `RTL_LAND_DELAY`), person-cue hovers and landing; a drone within `return.progress_min_cm` of home counts as arrived. For an edge-flown return, blocked starts row 13; for an autopilot-started return it only shows the reason and alerts, and the edge sends nothing but operator commands (row 21). |
| Home record | `rth/home.py` | Records home at takeoff, detects a changed or lost home. |
| Energy model | `rth/energy.py` | Section 10.6. |
| Uncertainty model | `rth/uncertainty.py` | Field: k × the receiver's 1-sigma accuracy plus speed × telemetry age; indoor: growth placeholders; a missing value counts as worst case (section 11.2). |
| Fault classifier | `rth/faults.py` | Fault (IMU, motor, positioning, unclassified: land in place) or transient (hover, retry once, return); replaces "every Tello error lands it" (`lane_flight.py:358-359`) from C7. |
| Ground return coordinator | `flightrisk/mission/returns.py` | RETURN ALL and per-drone Return, ordering, landing clearance, and the cancellation of every sender on LAND ALL (section 10.2). |

New names carry an `rth` prefix (`rth_returning`, `rth_waiting`, `rth_above_home`, `rth_landing`) and
the protocol command is `return_home`, because the executor already has a `return` sequence and a
`returning` phase for the manual way back (`lane_flight.py:206`, `:355`).

### 10.2 Landing is terminal (C-1)

- **Every landing decision is an input to the edge state machine**: LAND ALL (`emergency_land_all`),
  per-drone Land, the local land key, Ctrl+C, critical battery, a fault, every fallback reason, the
  link-loss land of today's defaults, a ceiling or geofence breach that lands, an autopilot mode the edge
  sees switch to LAND, and an on-find policy of LAND ALL. Today only the monitor latches the first
  landing reason (`lane_monitor.py:89-97`); the latch moves into the state machine and covers every
  source.
- **`rth_landing` and `landed` are terminal for flight.** While landing, the edge refuses `return_home`,
  `escort_setpoint`, `assign_lanes`, `start_at`, `resume`, `nudge`, manual moves and `set_speed` with the
  existing nack reason `landing` (`fleet_protocol/constants.py`, `NACK_REASONS_V2`), and starts no
  automatic return (task end, low battery, stranded countdown, link loss). A further land is accepted
  and only re-sends the landing, except under external control (next item). Once disarmed on the
  ground the drone is `landed`: it accepts and stores plans (`assign_lanes` is no longer refused) but
  flies only on an operator Start or manual takeoff; nothing automatic ever sends a takeoff (N-19).
- **External control is a terminal sub-state (B-1).** The MAVLink adapter latches external control on
  any mode change it did not make (a safety pilot's RC switch, a ground control station, the
  autopilot's own failsafe) until the drone has landed and disarmed (`mavlink_fleet.py:82-96`). The state
  machine keeps that latch as the sub-state `external_control` (also inside `rth_landing`): no automatic
  land, no land retry (`LAND_RETRY_S`, `lane_executor.py:203`), no move, return or takeoff; every motion
  command is refused with `landing`; each operator land command (LAND ALL, a ground `land`, the local
  land key, Ctrl+C) sends exactly one LAND (spec 6.2: "under external control, one LAND per press").
  The edge never fights the pilot or the autopilot.
- **The MAVLink adapter never turns a decided landing into a return.** `land_for_reason` maps failsafe
  reasons to RTL today (`mavlink_fleet.py:1234-1236`); from C1 the state machine decides between return
  and land, and once land is decided every later call sends LAND only. A queued `wait: false` release
  that arrives after a land is refused with `landing`.
- **LAND ALL marks, sends, then cancels (N-2).** The ground first sets the `landing` mark on every drone
  (a non-blocking flag that every sender checks before it sends), then sends the land to every drone,
  and only then cancels, asynchronously, the return queue and its pending releases, the escort
  controller, local-search re-plans, converge flights, re-task offers, stranded countdowns,
  `on_complete` timers, a scheduled mission-end RETURN ALL and any speed change. Cancellation never
  blocks or delays the send; a sender that loses the race sees the mark and sends nothing. Per-drone
  Land does the same for that drone.
- **Tests:** for each command and automatic action above, a fake-drone test and a SITL scenario send it
  after LAND ALL and assert a `landing` nack, no mode change away from LAND, and a landed drone.
  External control is exempt from the "no mode change away from LAND" assertion and from RTH-20; its
  own fake and SITL tests change the mode as a pilot would during `rth_landing` and assert that no
  second LAND is sent until the next operator land command, which sends exactly one.

### 10.3 States

`flying` (task or manual) → `holding` (short link loss, a blocked leg, a person cue, waiting for a
release) → `rth_returning` (into the slot, then the route) → `rth_waiting` (ordered to wait, at its own
slot, side by side) → `rth_above_home` (holding above home until the landing is cleared) →
`rth_landing` → `landed`; any state → `rth_landing(reason)` → `landed_in_place(reason)`; any flying
state → `external_control` (terminal for every automatic action, 10.2); and
`cannot_be_recalled` (drone link lost; Tello lands by itself). These map to the spec 9.5 display states;
task states (keeping watch, converging, at viewpoint, local search, escorting) come from the mission.

### 10.4 Event table (spec 6.2, one row each)

"Field default" and "Indoor default" are the actions from C6 and C7; before them each row keeps today's
action. Reason codes are those of section 10.5. Every row has a unit test of its default action and of
each fallback reason (AC-6.1); "SITL" rows also have a scenario.

| # | Spec 6.2 event | Detected by | Field default (C6) | Indoor default (C7) | Fallback reason | SITL |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Tasks done | edge `on_complete` from `task.end_action` | hover `retask.window_s`, return (land until C6) | same (land until C7) | per 10.5 | yes |
| 2 | Mission complete | ground | RETURN ALL | same | per 10.5 | yes |
| 3 | RETURN ALL or per-drone Return | `return_home` | stop the task (manual and held too) and return; ground orders conflicts | same, after the current move | not reached: shown with what it will do alone | yes |
| 4 | LAND ALL or per-drone Land | failsafe path | land at once; terminal (10.2) | same | `land_all`, `operator_land` | yes |
| 5 | Ground link lost, short | watchdog after `link.loss_detect_s` | hold; resume when the link returns | same | per 10.5 | yes |
| 6 | Ground link lost, long; ground crash | after `link.loss_wait_s` more | return inside own zone and corridor at its slot, in its landing order | same | `no_cleared_route` | yes |
| 7 | Drone link lost; edge dies or hangs | edge, autopilot | the autopilot's link-loss action (return or land), checked by the edge against the straight line home of each assignment before takeoff and on every re-task (10.9); a hung edge is turned into a dead one by the stall watchdog (13.1) | cannot be recalled | link back: add drift to uncertainty, then return or land | yes |
| 8 | Tello no-command failsafe | adapter fact | n/a | keepalives in flight, holds and returns; stop once landing is decided | `cannot_be_commanded` | no |
| 9 | Low battery | energy model threshold | return | same | per 10.5 | yes |
| 10 | Critical battery | `battery.critical_pct`; field: the autopilot's critical action | land at once, even mid-return | same | `critical_battery` | yes |
| 11 | Battery cannot reach home | energy prediction | return early | same | `battery_cannot_reach_home` | yes |
| 12 | Stranded countdown ends (a held drone with no controller after `stranded.countdown_s`; #212's lost drone whose controlling dashboard sends nothing for `stranded.lost_idle_s`) | ground | `stranded.action` return (land until C6) | same (land until C7) | land, never back to held | yes |
| 13 | Obstacle on the way home | sensor gate, `STOPPED_SHORT`, progress monitor | hover, look again, try another cleared route | same | after `return.blocked_wait_s`: `way_home_blocked_obstacle` | yes |
| 14 | Person cue on the way home | person cue, thermal detection | hover and alert; continue only after a human confirms, a detour or a land | same | after `return.person_cue_wait_s`: `way_home_blocked_person` | no |
| 15 | IMU, motor or positioning fault | fault classifier, autopilot status | land at once; never fight the autopilot | same | `fault_imu`, `fault_motor`, `fault_positioning`, `fault_unclassified` | yes |
| 16 | Command error that is not a fault | fault classifier | hover, retry once, return | same | unclassifiable: `fault_unclassified` | no |
| 17 | Uncertainty above its bound | uncertainty model | n/a (positioning fault) | land; end the task and return earlier when the prediction at home nears the bound | `uncertainty_above_bound` | no |
| 18 | Geofence breach | envelope, autopilot fence | return (autopilot fence action return or land) | moves leaving the fence are refused | land | yes |
| 19 | Ceiling breach | envelope | land at once | same | `ceiling_breach` | no |
| 20 | Obstacle sensor fails mid-task | sensor down | end and re-match tasks; return at a slot above the obstacle top | retrace flown airspace | `no_cleared_route` | no |
| 21 | Autopilot failsafe | autopilot mode (external control, 10.2) | the autopilot wins; FlightRisk sends only operator lands (one per press), shows the reason, re-matches tasks; its return is the highest-priority route | n/a | the autopilot's own | yes |
| 22 | Sighting raised | mission | finder keeps watch, others continue | same | finder returns at its threshold | no |
| 23 | Converge pressed | operator | N drones to separated viewpoints | same, approach beyond the 3 m cue | a drone with no viewpoint keeps its task | yes |
| 24 | Target confirmed | operator | Continue or End search (on-find policy) | same | per drone | no |
| 25 | Not verified pressed | operator | local search if on, else resume | same | per drone | no |
| 26 | Escort battery at hand-off level | energy model | send relief; leave once the relief has the subject in view | n/a | no relief: alert, return at threshold | yes |
| 27 | Escorted subject lost from view | mission | hold, alert, local search | n/a | per drone | yes |
| 28 | Escorted subject crosses a limit | mission | stop at the limit, keep watch, alert | n/a | then row 27 | yes |
| 29 | Return makes no progress (H-2, B-2) | progress monitor, horizontal legs only (10.1) | edge-flown: row 13; autopilot-started: show the reason and alert, send nothing except operator commands (row 21) | row 13 | row 13 | yes |
| 30 | Landing area not cleared | ground clearance, landing order | hold above home until cleared | same | wait bound reached: per 10.8 | yes |

### 10.5 Fallback reasons (closed list)

`critical_battery`, `battery_cannot_reach_home`, `fault_imu`, `fault_motor`, `fault_positioning`,
`fault_unclassified`, `way_home_blocked_obstacle`, `way_home_blocked_person`, `no_known_home`,
`uncertainty_above_bound`, `no_cleared_route`, `cannot_be_commanded`, `operator_land`, `land_all`,
`on_find_land_all` (spec 6.3), plus `ceiling_breach` (spec 6.2, ceiling row) and `no_return_order`
(spec 7.4, "if no order works"). An edge that cannot return (an older edge without `return_home`) is not
landed: it is shown "cannot return (older edge)" and held (M-5).

### 10.6 Energy and the return threshold (H-8)

- **Return threshold** = `battery.critical_pct` + energy to home + worst-case wait + `battery.landing_reserve_pct`,
  and never below `battery.return_floor_pct`.
- **Energy to home** = distance home along the return route × `battery.return_rate_pct_per_100m`
  (field: the autopilot's remaining-capacity estimate when it reports one).
- **Worst-case wait** = (the drone's place in its landing order × `separation.landing_interval_s` +
  the queue wait the ground last granted in `return_home.max_wait_s`, 0 before any) ×
  `battery.hover_rate_pct_per_min` / 60. Both inputs reach the edge in `assign_lanes.landing` and
  `return_home`, so the edge never depends on a number it cannot see.
- **The drone arrives above home with at least critical + reserve**, so a normal return never ends in a
  critical-battery landing short of home. Field: `battery.critical_pct` is the autopilot's critical level,
  read before takeoff, and the reserve is above it.
- Constraints (in `parameters.md`): critical < return floor; return floor ≥ critical + reserve; takeoff
  minimum > return floor; the autopilot's low-battery level ≤ return floor − 5 points, so FlightRisk
  always returns first (H-2). On a field unit K-1 and K-2 are checked again before takeoff with the
  autopilot's critical level in place of `battery.critical_pct` (N-18).

### 10.7 Home (spec 7)

- Recorded once per flight at takeoff (field: the autopilot's home at arming; indoor: the pose origin);
  a home that changes in flight, or an edge restart in flight, lands with `no_known_home`.
- **Before takeoff (spec 7.2, AC-7.1):** field: a 3D fix, at least `gps.min_satellites`, receiver
  accuracy within `home.accuracy_cm`, the autopilot's home within `home.max_offset_cm`, inside the mission
  fence. Indoor: each mark and heading confirmed in the Start confirmation, inside its fence, outside
  no-entry areas, ceiling measured from the takeoff surface (indoor AC-7.1 has requirement RTH-25). Both:
  the return slot between the profile floor and ceiling minus margin and above the obstacle top plus
  margin (section 11.5); no work the unit cannot finish and still return with critical + reserve.
- **Home separation (H-7):** every pair of homes is at least `separation.base_cm` apart, plus both
  receivers' accuracies in the field; Start is refused, naming both drones, for a closer pair. Homes
  closer than `separation.landing_cm` share a landing area, and the plan gives each drone of a shared area
  its place in a landing order.

### 10.8 Returns with the ground: RETURN ALL, ordering, clearance and termination

- **RETURN ALL** sends `return_home` to every flying drone, manual and held ones too, and shows each
  drone's result. `return_home` without `wait` is accepted from any authenticated ground, like LAND
  (M-5); ordering (`wait`, releases) only from the authority. A standby dashboard therefore keeps RETURN
  ALL.
- **Ordering:** every return route is checked against all other drones (section 11). An
  autopilot-started return (row 21, or an autopilot low-battery return) is a fixed route with the
  highest priority; the others yield to it. Conflicts then go lowest battery margin first, then
  shortest route; waiting drones get `return_home {wait: true, max_wait_s}` and hold at their own slot,
  side by side.
- **Landing clearance:** a returning drone holds above home (`rth_above_home`) until the ground clears
  its landing; in a shared landing area one drone at a time. Without the ground, each drone uses its
  precomputed landing order (10.9). Releases and landing clearances are never gated by a confirmation
  (7.3(6)).
- **Termination (M-10):** every wait is bounded by the energy model: the bound is the moment the drone
  would drop below critical + reserve + energy to home. A waiting or holding drone is released before its
  bound; if two bounds cannot both be met, the drone with the earlier bound goes first, and the drone
  that still cannot be scheduled before its bound lands in place with `no_return_order` (ties: the
  higher unit id lands). "Hold for a connected operator" after a blocked return is bounded the same way.
- **Per-drone controls (spec 9.2):** Return enabled whenever the drone flies and can be commanded, else
  it says why; Land always enabled; taking control stops a return; resuming a task after a return caused
  by battery, a fault or uncertainty is refused.

### 10.9 Returns without the ground (spec 7.4, decision 1; H-1)

- `assign_lanes` carries the drone's `zone` (already required, `validate.py:135`), its `corridor`, its
  `landing` record (landing area, place in the order, `separation.landing_interval_s`) and the current
  no-entry areas, and every re-task re-sends all of them; `set_no_entry` pushes later changes to every
  edge (spec 6.1(6)).
- After a long link loss the edge returns only if the whole route home, widened by its uncertainty, lies
  in its zone and corridor at its slot and outside every no-entry area it knows (its own area excepted);
  otherwise it lands after the wait with `no_cleared_route`. In a shared landing area it waits above home
  for its place × the interval before descending (best effort, below).
- Drones outside their own zone by design (re-tasked, converging, local search, escorting) carry a task
  zone and a corridor back to home in their assignment, checked like any zone (section 11.4); an escort
  that loses the ground therefore lands near the subject when its corridor does not reach home, which is
  documented (M-3).
- **Edge death (MAVLink; B-3):** the autopilot flies its own straight line home. The edge reports its
  autopilot's link-loss action (`return`, `land` or `unknown`, read at preflight and after every
  parameter write) as `ap_link_loss` in `describe_unit` and in the `rth` telemetry object (an A2 shape).
  Before takeoff and before it accepts any assignment or re-task (re-flight, converge, local search,
  escort), the edge checks that the straight line from every point of the assignment's zone and
  corridor to home lies inside that zone and corridor. When it does not and the action is return, the
  edge sets the action to land (writes `FS_GCS_ENABLE` 5 or `NAV_DLL_ACT` 3 and reads it back); if that
  fails it refuses the takeoff or nacks the assignment with `straight_line_home`. The ground's Plan and
  re-task offers (D1) use the reported action, so they give such tasks to land-on-loss drones or plan
  zones whose straight line stays inside, and the edge rarely has to refuse. `unknown` counts as
  return. The edge check is C1's (edge files); D1 only plans with the reported action.
- **Landing order without the ground is best effort (N-12).** The autopilot's per-drone landing delay
  (`RTL_LOIT_TIME` on ArduPilot, at most 60 000 ms; `RTL_LAND_DELAY` on PX4) is set to its place × the
  interval, capped at the autopilot's maximum, and an edge that returns alone waits the same above
  home. Both count from each drone's own arrival, so a far drone with place 0 can land together with a
  near one with place 1. Separation in a shared landing area therefore rests on constraint K-15 (homes
  at least the base separation plus both drones' field uncertainties apart), not on the order; the
  order only spaces landings of drones that arrive together.

### 10.10 The Tello's dead-reckoned return (Dev/test only)

The edge flies home itself with keepalives running: first into its slot, then legs of 20 to 500 cm,
direct through operator-cleared areas, otherwise retracing its flown polyline, each leg through the
sensed-step gate; with the sensor down it only retraces (AC-6.4); it descends only over home; a return
starts after the current move. Nothing is tuned; the growth values are pessimistic placeholders.

## 11. Airspace (spec section 8)

`flightrisk/mission/airspace.py` (D1) checks every planned task, transit, viewpoint, escort position
and return route, and generates zones, corridors and landing orders. D1 runs behind today's defaults:
it plans and refuses, but changes no return behaviour.

### 11.1 One environment per airspace

Plan refuses a mixed mission and, indoors, a unit without a measured indoor profile (A4, AC-4.2).

### 11.2 Positioning uncertainty and separation (spec 8.2; M-1)

- **Field uncertainty** = `position.gps_sigma_k` × the receiver's 1-sigma horizontal accuracy (k ≥ 2;
  the EKF's horizontal position variance when it reports one and it is larger) + the drone's ground speed
  × the age of its position. **Indoor:** the dead-reckoning growth model. A missing or stale value
  counts as worst case: the unit gets no new route and its no-entry area grows until a value arrives.
- **Planned separation is space-only and conservative:** two drones in one band never have intersecting
  route tubes (task, transit, viewpoint, escort, return), each widened by half the base separation plus
  its predicted uncertainty, whatever their timing.
- **In flight**, the ground checks every pair at the telemetry rate against `separation.base_cm` +
  uncertainty A + uncertainty B; a violation moves the drone that is not returning out of the
  returning drone's route tube (for an autopilot-started return, out of the tube of its straight line
  home) or holds it when it is already outside that tube; between two drones that are not returning
  the higher unit id holds. The operator is alerted (N-13). The edge never relies on this monitor for its own safety.
- Base separation: indoor 150 cm (today); field 500 cm from D1.

### 11.3 Bands (spec 8.3; H-6)

| Band | Floor | Ceiling | Holds |
| --- | --- | --- | --- |
| Close (field) | `airspace.close_band_floor_cm` (1000) | `airspace.close_band_ceiling_cm` (1500) | close and head-on search, viewpoints, escort, keep-watch, every slot of a unit doing those tasks |
| Sweep (field) | `airspace.sweep_band_floor_cm` (2000) | `airspace.sweep_band_ceiling_cm` (2500) | bird's-eye and thermal sweeps |

Constraints (in `parameters.md`): close floor < close ceiling; sweep floor ≥ close ceiling +
`airspace.band_gap_cm`; sweep floor ≤ sweep ceiling ≤ ceiling − margin; every slot lies in exactly one
band; `escort.altitude_cm`, viewpoint slots and every return slot lie inside the band of the drone's
task. The default field slots (10 and 15 m) and the 15 m escort altitude are in the close band. Drones
in different bands may overlap horizontally except in the column where a sweep-band drone takes off,
lands or descends. Indoors, slots never count as separation, so indoor drones are always separated
horizontally.

### 11.4 Zones, corridors and landing order (H-1)

- D1 gives each drone a **zone** (its task area) and a **corridor** (a tube at its slot from the zone to
  its home, at least base separation + its uncertainty wide) and checks that the zone-and-corridor tubes
  of all drones in one band do not intersect.
- **Straight line home:** for every point of a zone the straight line to home must lie inside the zone
  and corridor; where it does not, the drone's autopilot link-loss action must be land, because an
  autopilot whose edge died flies that straight line. The edge enforces it against every assignment
  it accepts (C1, 10.9); D1's Plan uses each edge's reported `ap_link_loss` to plan zones or choose
  drones so the edge never has to refuse.
- **Landing order:** homes closer than `separation.landing_cm` share a landing area; D1 numbers the
  drones of each shared area (lowest planned battery margin first) and sends each its place and
  `separation.landing_interval_s` in `assign_lanes.landing`. A shared landing area without an order is
  refused. Without the ground the order is best effort (10.9).
- **Re-tasks** (re-flights, converge, local search, escort) carry a task zone and a corridor back to
  home, checked the same way before they are offered; every re-task re-sends zone, corridor, landing
  record and no-entry areas.

### 11.5 Obstacle tops (spec 6.1(5); H-10)

A field mission's search areas and corridors carry `airspace.obstacle_top_cm`, the height of the
highest known obstacle, entered by the operator per area (no default; a field mission without it is
refused at Plan). Plan refuses any slot used to fly or return over an area below its obstacle top +
`airspace.obstacle_margin_cm`, naming the area and the drone. The ceiling and band constraints still
apply. Indoors, the ceiling and the cleared areas play this role.

### 11.6 Fences

Each drone has a fence around its home; the mission has a shared fence; tasks may add more. Every route,
widened by its drone's uncertainty, stays inside every fence. Field: the autopilot's fence is uploaded
before arming (B3) and its action is return or land (B4). Indoor: moves that would leave the fence are
refused.

### 11.7 No-entry areas (spec 6.1(6); H-9)

| Kind | Covers | Created by | Cleared by |
| --- | --- | --- | --- |
| Person cue | the cue's position widened by its uncertainty | a person cue or thermal flag | a human confirm-to-clear, or Converge, Not verified or Escort for its subject |
| Lost drone (section 12) | its moving last position widened by `position.gps_sigma_k` × its last reported 1-sigma accuracy (indoor: its last reported uncertainty) + `noentry.lost_margin_cm` (spec 10.2: accuracy + 5 m in the field, uncertainty + 100 cm indoors), never by speed × age, which would swallow neighbouring zones (N-11); its zone, its corridor, its return line (last position to home, widened the same way) and its landing area; without any position report, the zone, corridor and landing area | the ground declaring it lost | its report of being landed, or a human confirm-to-clear allowed only after the precondition of section 12 |
| Stranded drone | as for a lost drone, while it hovers with no controller | the stranded countdown | as for a lost drone |
| Operator-drawn | the drawn area | the operator | the operator |

- **Confirm-to-enter** lets one named drone enter once (its task, transit, viewpoint, escort or return);
  **confirm-to-clear** removes the area for everyone. Both are operator or supervisor actions on a
  laptop; for a lost or stranded drone both wait for the section 12 precondition.
- **The owning drone is exempt** from its own area, so a lost drone may fly home through it.
- Areas reach the edges in `assign_lanes` and through `set_no_entry` on every change; an edge refuses any
  move or return leg into an area it knows, except its own.

### 11.8 When precision degrades (spec 8.5)

The required separation grows at once; if the corridor still clears every other drone the drone carries
on, otherwise it holds and is re-routed or sent home; past its bound it lands. A field drone that loses
its fix has a positioning fault: its autopilot acts (section 13) and its area is no-entry until it lands.

## 12. Lost drones (C-2, folding in #212)

Today the ground declares a drone lost after `FLEET_LOSS_TIMEOUT_S = 5.0` s
(`dashboard/app.py:559-563`), offers its lanes at once and sends one best-effort land
(`coordinator.py:2654-2776`, `on_drone_lost`), re-lands it if it reports flying again
(`_reland_lost_locked`, `:1493`; #212 exempts drones flown by hand), and keeps its last position as a
20 s point obstacle (`_lost_obstacle_locked`, `:3502-3533`). #212 (merged) also starts the orphan
countdown for a lost drone whose controlling dashboard sends no manual command for
`FLIGHTRISK_MANUAL_LOST_IDLE_S` (30 s, at least 5 s; `dashboard/manual_control.py:58-70`, `:132-134`) and
refuses Resume search for a lost drone (`manual_control.py:789`, `:811`). After the return defaults
switch, a drone holds `link.loss_detect_s` and then `link.loss_wait_s` before it returns, so the
immediate re-task, the land and the re-land would act against a drone that is doing exactly what the
spec asks. D2 replaces them, before any default switch:

1. **The ground loss timeout is derived per unit**: `link.ground_loss_timeout_s` = `link.loss_detect_s`
   + `link.loss_wait_s` + `link.ground_loss_margin_s` (constraint in `parameters.md`). With today's
   defaults a Tello stays at 3 + 0 + 2 = 5 s (unchanged) and a MAVLink drone becomes 3 + 10 + 2 = 15 s
   (today 5 s, which already declares a holding field drone lost); after C7 a Tello becomes 15 s.
2. **The ground never lands or re-lands a lost drone as a loss reaction.** It sends it only what an
   operator sends (Land, Return). If its link returns, the dashboard shows its own state (holding,
   returning, landing) and the operator may Land or Return; Resume task is refused once any of its
   lanes has been confirmed for another drone (N-4). #212's idle-controller countdown is kept as the
   spec's stranded event (row 12), not a loss reaction: a lost drone held by a dashboard that sends
   nothing for `stranded.lost_idle_s` starts the stranded countdown, whose action is `stranded.action`
   (land until C6 and C7, return after).
3. **Its unflown lanes become tasks waiting for a human confirm**; nothing is re-tasked automatically.
4. **The confirm is refused while the lost drone may still be flying** (#212's refusal, kept as a
   precondition): until it reports landed, or until `lost.flying_window_s` after its last report of
   flying. Tello until C7: 20 s (today's `LOST_OBSTACLE_S`: its 15 s auto-land, the descent and a
   margin). From C7 a Tello whose ground link is lost holds and then flies a dead-reckoned return, so
   its window becomes the derived formula below with 20 s as its floor (N-3). Field: derived per unit
   = detect + wait + the edge stall bound and the autopilot's GCS timeout (a hung edge is killed into a
   GCS failsafe, 13.1) + the time home from its last position at its speed + landing time +
   `link.ground_loss_margin_s`. It is never shortened by an endurance estimate (N-1).
5. **Its no-entry area** is the lost-drone area of section 11.7, replacing the 20 s point.
6. #212's other behaviours stay: a held drone that lands with no controller gets the orphan-landed mark,
   a later phone Resume is refused and re-tasks nothing, and Resume search stays refused for a lost
   drone.

## 13. The MAVLink production adapter: autopilot parameters and returns (H-2)

### 13.1 Edge-guided return and the autopilot fallback

An edge-started return moves the drone vertically into its slot first, then flies GUIDED to above its
home at the slot, holds there until the landing is cleared (section 10.8), then sends LAND. The progress
monitor watches it (row 29). The autopilot's own return-to-launch happens only when the autopilot's
failsafe fires or the edge is gone; while the edge lives it treats such a return as the highest-priority
route, watches its progress, and sends only operator lands. The parameters below make that fallback fly
home, at a bounded altitude, and land there in order.

**A hung edge is turned into a dead one (N-1).** `mavsdk_server` is a separate process that keeps
sending the GCS heartbeat (`mavsdk_link.py:167-186`), so a hung edge would never trigger the GCS
failsafe and the drone would hold in GUIDED until its battery failsafe. B4 therefore refuses a field
takeoff unless the server was started tied to the edge (`spawn_tied_to_parent`, `supervised.py:45`;
today a failure to tie it only logs a warning, `mavsdk_link.py:183-185`), and adds a watchdog outside
the control loop that kills the server when the edge's control loop has not ticked for longer than the
autopilot's `FS_GCS_TIMEOUT` (PX4: `COM_DL_LOSS_T`); the heartbeat then stops and the autopilot's
link-loss action runs. A SITL scenario stalls the control loop and asserts the failsafe.

### 13.2 Parameter table (B4)

Checked before every takeoff; a value outside "accepted" refuses the takeoff and names the parameter,
its value and the accepted values. "Today" is what `autopilot.py:190-245` accepts now. Every newly
refused value is listed in section 27 (B4). ArduPilot names follow the connected firmware (A8).

| Purpose | ArduPilot: accepted | ArduPilot today | PX4: accepted | PX4 today | SITL scenario (B4) |
| --- | --- | --- | --- | --- | --- |
| Edge (GCS) link loss | `FS_GCS_ENABLE` 1 (RTL) or 5 (Land); 5 required where the straight line home leaves zone and corridor, checked and set by the edge against each assignment (10.9, B-3); `FS_GCS_TIMEOUT` 2 to 10 s; `SYSID_MYGCS` or `MAV_GCS_SYSID` equal to the edge's MAVSDK system id (245, `docs/open-drone-support.md:107`) | not checked ("recommended") | `NAV_DLL_ACT` 2 (Return) or 3 (Land), 3 required as for ArduPilot; `COM_DL_LOSS_T` 5 to 10 s (PX4's minimum is 5 s, N-18); `COM_DLL_EXCEPT` 0 | `NAV_DLL_ACT` 2 or 3; the rest not checked | kill the edge process; the drone returns or lands as configured, in its landing order |
| Failsafe exceptions | `FS_OPTIONS` bits 0, 1, 2 and 4 clear (continue in Auto on RC or GCS failsafe, continue in Guided on RC failsafe, continue in pilot control on GCS failsafe); bit 3 (continue landing) allowed | not checked | `COM_RCL_EXCEPT` 0 | not checked | set each bit; preflight refuses |
| Position (EKF) fault (B-4) | `FS_EKF_ACTION` 1 or 3 (land); 0 and 2 (AltHold drifts) refused; `FS_EKF_THRESH` > 0 (0 disables the EKF failsafe and is refused); `FS_DR_ENABLE` 1 (Land) or 0 (no dead-reckoning action, so the EKF failsafe lands after `FS_DR_TIMEOUT`); 2 (the default), 3, 4 and 6 refused, because they start a dead-reckoned return straight through other zones | not checked | `COM_POS_FS_ACT` 0 (Descend); 1 (Terminate) refused; `COM_POS_LOW_ACT` 5 (Land), or the low-accuracy action disabled (`COM_POS_LOW_EPH` −1, its default, or `COM_POS_LOW_ACT` 0 or 1); 2 (Hold), 3 (Return) and 4 (Terminate) refused, never 4 | not checked | ArduPilot: `SIM_GPS_DISABLE` 1 in flight with `FS_DR_ENABLE` 1 and again with 0: the drone lands and never returns; preflight refuses `FS_DR_ENABLE` 2 and `FS_EKF_THRESH` 0. PX4: GPS off in flight with `COM_POS_FS_ACT` 0: the drone descends; preflight refuses `COM_POS_FS_ACT` 1 and `COM_POS_LOW_ACT` 4. FlightRisk shows `fault_positioning` |
| Return ends landed at home | `RTL_ALT_FINAL` (`RTL_ALT_FINAL_M`) 0 | not checked | `RTL_LAND_DELAY` ≥ 0 (−1 loiters forever) | not checked | autopilot RTL ends disarmed at home |
| Return goes to home | no rally points (`RALLY_TOTAL` 0) | not checked | `RTL_TYPE` 0 (direct to home or rally point) with no rally points; 1, 2, 3 and 4 (mission landing or closest safe destination) refused | not checked | upload a rally point: preflight refuses |
| No leftover mission landing | value 6 (DO_LAND_START) refused in every failsafe action; the edge clears the autopilot's mission before arming | 6 accepted in the battery and RC-loss actions | mission-landing `RTL_TYPE` values refused; mission cleared | not checked | load a mission with DO_LAND_START: cleared before arming; value 6 refused |
| Return altitude | `RTL_ALT` (`RTL_ALT_M`) 0 or at most the slot; the autopilot returns at the higher of it and its current altitude, so every edge-started return first moves into the slot; `RTL_CLIMB_MIN` (`RTL_CLIMB_MIN_M`) at most the band ceiling minus the slot, so the return never climbs into the sweep band (N-18) | `RTL_ALT` ≤ slot | `RTL_RETURN_ALT` and `RTL_DESCEND_ALT` at most the slot (it returns at the higher of `RTL_RETURN_ALT` and its altitude) | `RTL_RETURN_ALT` ≤ slot | edge killed at 15 m with a 10 m slot: returns within the close band; edge-started return descends into the slot first |
| Landing order without the edge (best effort, 10.9) | `RTL_LOIT_TIME` = min(place × `separation.landing_interval_s`, 60 s) in ms (its range is 0 to 60 000), written and read back before arming | not set | `RTL_LAND_DELAY` = place × interval (s) | not set | two drones sharing a landing area, both edges killed: they land one after the other |
| Fence action | `FENCE_ACTION` 1 (RTL or land) or 2 (land) | 1 to 5 | `GF_ACTION` 3 (Return) or 5 (Land) | 2, 3, 5 | breach the uploaded fence: return or land |
| Critical battery | `BATT_FS_CRT_ACT` 1 (land); FlightRisk's field critical level = the autopilot's critical capacity | 1, 2, 3, 4, 6, 7 | `COM_LOW_BAT_ACT` 2 (land at critical); `BAT_CRIT_THR` read as the field critical level | 2, 3 | drain to critical mid-return: lands in place |
| Low battery | `BATT_FS_LOW_ACT` 1 or 2; the autopilot's low level (`BATT_LOW_MAH` as a share of `BATT_CAPACITY`) at most `battery.return_floor_pct` − 5 points; a voltage-only threshold is refused because it cannot be compared; `BATT_LOW_VOLT` 0 when the capacity threshold is used, because a voltage trigger can fire before FlightRisk's return (N-18); `BATT_CRT_VOLT` kept as a backstop and shown (a critical-voltage landing short of home is the `critical_battery` fallback) | 1, 2, 3, 4, 6, 7; level not compared | `BAT_LOW_THR` at most the return floor − 5 points | not compared | drain slowly: FlightRisk returns before the autopilot's low failsafe |
| RC loss | `FS_THR_ENABLE` 1 (RTL) or 3 (land) | 1, 3, 4, 5, 6, 7 | `NAV_RCL_ACT` 2 or 3 | 2, 3 | RC off in SITL: return or land |
| Edge hang (N-1) | `mavsdk_server` tied to the edge; the stall watchdog kills it after `FS_GCS_TIMEOUT` | not checked (an untied server only logs a warning) | the same, with `COM_DL_LOSS_T` | not checked | stall the edge's control loop: the heartbeat stops and the GCS failsafe acts |
| Kept from today | `FENCE_ENABLE` 1, `FENCE_TYPE` circle and max altitude, fence radius containing the edge's fence, `FENCE_ALT_MAX` above the ceiling and at most 120 m while the regulatory altitude cap is on (item 53 of section 27; with the cap off, at most the unit's known airframe maximum), `BATT_ARM_VOLT`, system id | same | `GF_MAX_HOR_DIST`, `GF_MAX_VER_DIST` (at most 120 m while the cap is on, at most the known airframe maximum with it off), system id | same | existing tests |

Sources: ArduPilot GCS failsafe (https://ardupilot.org/copter/docs/gcs-failsafe.html), RTL mode
(https://ardupilot.org/copter/docs/rtl-mode.html), EKF failsafe
(https://ardupilot.org/copter/docs/common-ekf-inav-failsafe.html), parameter renames
(https://ardupilot.org/copter/docs/common-param-name-changes.html); PX4 safety
(https://docs.px4.io/main/en/config/safety.html) and return mode
(https://docs.px4.io/main/en/flight_modes_mc/return.html); the enumerations and ranges of `FS_DR_ENABLE`,
`FS_DR_TIMEOUT`, `FS_EKF_THRESH`, `FS_EKF_ACTION` and `RTL_LOIT_TIME` from ArduCopter's `Parameters.cpp`,
and of `COM_POS_FS_ACT`, `COM_POS_LOW_ACT`, `COM_POS_LOW_EPH`, `COM_DL_LOSS_T`, `NAV_DLL_ACT`,
`COM_LOW_BAT_ACT` and `RTL_TYPE` from the PX4 parameter files (section 29).

## 14. Missions and tasks (spec section 5)

### 14.1 Records

`flightrisk/mission/model.py` (types in A2, used from D3 and D4): `Mission` (environment, areas with
obstacle tops, targets, units, on-find policy, mission settings rows 129 to 142, tasks, no-entry areas,
airspace plan, log), `Target` (references, status `searching`, `sighted`, `found`, `escorted`, `lost`),
`Task` (type, place, required and preferred capabilities, band, precision tolerance, priority, created
time, progress, state `unassigned` with reasons, `waiting_for_confirm`, `assigned`, `running`, `done`,
`unsearched`, `abandoned`, assignee), `Sighting` (finder, point and uncertainty, per-target scores,
attribution and its state, evidence, Claude opinion as an aid).

### 14.2 Task types and how they fly

| Spec task type | Generator | Flown as |
| --- | --- | --- |
| Wide bird's-eye sweep | `field_scale` lanes in the sweep band from the nadir swath | `assign_lanes` at a sweep-band slot |
| Thermal sweep | the same with the thermal footprint | `assign_lanes`; thermal pipeline is roadmap R1 |
| Head-on area search | today's zone and lane planner | `assign_lanes` in the close band |
| Close search of a flag; re-fly of a segment | flag uncertainty plus margin; today's re-flight segments | `assign_lanes` |
| Keep watch | a hover point that sees the sighting | `assign_lanes` with one point, `on_complete: hover` until released |
| Converge and verify | viewpoint ring (15.3) | one point per drone |
| Moving-target local search | outward search of the r(t) disc (15.4) | `assign_lanes`, re-planned as r grows |
| Escort (field only) | escort controller (15.5) | `escort_setpoint` |

### 14.3 Matching (spec 5.2; M-2)

1. **Hard filters**, never overridden: required capabilities; the feature on; task, routes to it and home
   inside the unit's fences and range and clear of other tubes (section 11); battery for transit, task and
   return to critical + reserve (10.6); predicted uncertainty within the task's tolerance; the unit not
   held, manual, landing or in a fallback; every needed confirmation given.
2. **Ranking by bands, not raw values:** positioning class (RTK, GPS within tolerance, dead reckoning),
   then battery margin in 10-point buckets, then transit time in 30 s buckets, then idle before busy, then
   not already blocked there, then unit id. A centimetre of accuracy never beats a battery bucket.
3. **Task order:** tasks are matched by priority, then creation time, then id.
4. **Running tasks are never reassigned** by a rerun; only a loss, a fallback or an operator action frees
   them.
5. **Every match records its input snapshot** (unit states, uncertainties, batteries, the bucketed
   values) with its explanation, so a decision can be replayed exactly.
6. **No match:** the task is listed unassigned with a reason per unit, shown on the map; the operator may
   free or add a unit, change the task, or **accept the area as unsearched** (task state `unsearched`,
   logged, shown on the map) (spec 5.2; M-12).

### 14.4 Hand-off, re-task and search speed

- A thermal flag becomes a person cue: a close-search task waits for a human confirm (AC-5.2); a
  level-camera unit qualifies only if its uncertainty plus the flag's is within tolerance.
- A lost unit's tasks wait for a confirm (section 12); a landed unit is never launched.
- **Search speed (spec 5.9; M-6):** lane spacing ≤ footprint width × (1 − overlap); speed ≤ (1 − overlap)
  × footprint length along the track × detection rate, capped by the airframe and the step cycle. For a
  level forward camera the footprint is the ground band in which a person appears at least the detection
  profile's minimum height in pixels, between the camera's near edge and that range; its length along the
  track is that band's depth. Before the first measurement the rate is the catalogue's nominal rate for
  the profile (marked unverified) and the speed starts at the lower of that and half the airframe speed.
  A new `set_speed` command (v2, authority only) changes the speed mid-lane; the Tello applies it at its
  next step. The edge reports its achieved rate (`det_hz`) in the detection-profile telemetry object.

## 15. On-find flows (spec 5.5 to 5.8)

### 15.1 Targets, sightings and verification (M-4)

- The ground scores every detection against every target (it scores today; the edge only keeps
  embeddings, `edge_device.py:810-813`), so multi-target needs no edge wire change.
- Each target has its own match threshold. A sighting is attributed to the best target; when the top two
  scores are within `find.ambiguity_margin` it is marked **ambiguous** and shows both. The operator can
  re-attribute until Confirm, after which the attribution is locked. The false-alarm rate grows with the
  number of targets, which the panel states next to the target list.
- On a sighting the finder keeps watch until its return threshold; others continue; the operator answers
  on the laptop (Converge, Confirm found, Not verified, Dismiss). Only a human Confirm marks a target
  found; scores, viewpoint counts and the Claude opinion are labelled aids.

### 15.2 On-find policy

Set at Plan, changeable at any time, logged. On End search: keep watch (default; watchers and escorts stay
until their thresholds or release, others return), RETURN ALL, or LAND ALL. This replaces
`recovery.py:189` for fleets.

### 15.3 Converge and verify

N viewpoints on a ring at no less than `find.approach_distance_cm`, spread in bearing, pairwise separated,
each at its own slot in the close band. **Pre-selection is the N nearest eligible drones by distance**,
as spec 5.6 says, not the 5.2 ranking; the operator can change it; the hard filters of 14.3 still apply.
If the ring, fences or batteries cannot hold N, fewer go with the reason. The verification window lapsing
resumes converged drones, keeps the finder watching and leaves the alert open.

### 15.4 Moving-target local search

r(t) = sighting uncertainty + `find.running_speed_cm_s` × time since last seen, capped; cut to fences and
allowed areas minus other no-entry areas; outward at the 14.4 speed; re-planned as r grows; stops on the
operator, the time limit, the disc covered once, or every drone at its threshold; a new sighting resets it.

### 15.5 Escort, field only (M-3)

- **Setpoints:** the ground computes each escort's setpoint at 1 Hz from the subject's latest geolocated
  position (detections carry `cam` and `pitch_deg`), at `escort.standoff_cm` on its own bearing and
  `escort.altitude_cm` in the close band, pairwise separated; **allowed zones** are the mission fence minus
  every no-entry area other than the subject's own and minus the route tubes of every other drone's
  running task; a setpoint that cannot satisfy them is not sent and the escort holds. The edge holds when
  a setpoint is older than 2 s, and refuses setpoints while landing (10.2).
- **Hand-off before the return threshold:** the hand-off level is the return threshold +
  `escort.handoff_margin_pct`, so the relief is matched before the escort must return; if the relief is not
  in view by the return threshold, the escort returns anyway and the operator is alerted.
- **"The relief has the subject in view"** means the relief's own detections are attributed to the same
  target and its geolocated positions lie within the combined uncertainty of the escort's subject position
  for `escort.handoff_frames` consecutive processed frames; only then does the escort leave.
- **Lost from view** for `escort.lost_from_view_s`: hold, alert, local search; resume only after a human
  confirms a new sighting. **At a limit:** stop there, keep watch, alert.
- **Ends** when the operator ends it, the search ends, or no escort can continue (spec 5.8).
- **Ground crash:** an escort is outside its own zone, so after the long link-loss wait it returns only if
  its task corridor reaches home, otherwise it lands near the subject (documented, section 10.9). An
  escort is offered only to a drone whose autopilot's link-loss action is land, or that its edge switches
  to land when it accepts the escort (10.9), because an escort's straight line home almost never stays
  inside its task zone and corridor (B-3).

## 16. Roles, credentials and devices (spec sections 2 and 9.4; H-4)

### 16.1 Credentials decide the role

- **The role comes from the credential alone**, never from anything the client says about itself.
- **Operator:** `FLIGHTRISK_API_KEY`, accepted only on a loopback connection, so the operator key never
  leaves the ground host. A non-loopback connection that presents it is downgraded to observer-phone rights
  (LAND ALL and Land, with the land right, below) and told to pair; it is never refused outright, so a phone set up the old way
  can still land.
- **Supervisor and observer:** per-device tokens. The operator pairs a device from the dashboard (a one-time
  code or QR code holding the token and the ground's TLS certificate fingerprint) and chooses its role and
  its kind (`laptop` or `phone`). The ground stores only a salted hash of each token with its label, role,
  kind, creation and last-seen time (`~/.flightrisk/devices.json`, beside the other files and outside the
  repository) and can revoke one device at any time. The token never appears in logs, presets or the vault.
  **Pairing is bound to the certificate (N-14):** the QR code is shown only on the ground host's own
  screen; the device pins the fingerprint from it before it sends the code, and the ground refuses a
  pairing over a connection whose certificate does not match, so pairing is never trust-on-first-use.
  F2 and F3 store the token in the Android Keystore or the iOS Keychain and connect only over `wss`
  pinned to the fingerprint (Android trusts only system certificate authorities today,
  `docs/app-store-compliance.md:219`, so F2 adds a pinned trust manager for the ground's certificate).
  A token record also holds its **land right**: always on for the operator, supervisors and observer
  phones; off by default for a dashboard observer, which the operator may turn on per token (R-3).
- **Transport:** supervisor tokens are accepted only over TLS (`wss`, with the paired fingerprint pinned)
  or loopback; a supervisor token over cleartext is refused with "pair over TLS". Observer tokens are
  accepted over cleartext on the local network, but a cleartext observer session receives no imagery
  (reference photos, crops, frames): only state, positions and text (decision R-4).
- **Laptop-only actions** (approving or refusing confirmations, answering match alerts, changing the
  on-find policy, safety settings, homes and less-safe values) need the operator key or a supervisor token
  of kind `laptop` where the table allows it; the kind is part of the token record, not a client claim.

### 16.2 Events by role

"Op" is the operator on the ground host; "Sup laptop", "Sup phone", "Obs phone" and "Obs view" are paired
devices (an observer on a dashboard view uses a laptop observer token). Every Socket.IO event and HTTP
route is in `flightrisk/dashboard/roles.py`; a test enumerates the registered handlers and routes and fails
on any missing entry (deny by default).

| Event class | Op | Sup laptop | Sup phone | Obs phone | Obs view |
| --- | --- | --- | --- | --- | --- |
| LAND ALL, per-drone Land | yes | yes | yes | yes | only with the token's land right (off by default; R-3) |
| RETURN ALL, per-drone Return | yes | yes | yes | no | no |
| Acknowledge an alert (mark seen; not an answer) | yes | yes | yes | no | no |
| Approve or refuse a confirmation (no-entry, Start, less-safe, standby values) | yes | yes | no | no | no |
| Answer a match alert (Confirm, Not verified, Dismiss, Continue, End search, Converge, Escort) | yes | no | no | no | no |
| Change the on-find policy, mission settings, safety settings, homes | yes | no | no | no | no |
| Plan, Start, manual flight, re-task, re-fly | yes | no | no | no | no |
| Resume a held drone nobody controls | yes | yes | yes | no | no |
| Choose a detection profile | yes | yes | yes | no | no |
| Limited inventory edits (add a unit of a built-in model, availability, non-safety features) | yes | yes | yes | no | no |
| Other inventory edits, custom models, device pairing and revocation | yes | no | no | no | no |
| Read everything (state, plan, mission, targets, parameters, inventory) | yes | yes | yes | yes; no imagery over cleartext | yes; no imagery over cleartext |

Contradictions resolved: spec section 2 lets an observer send nothing but LAND ALL and Land, while 9.4
lets "phones" resume a held drone, choose a detection profile and make inventory edits; the specific
observer row wins, so those are supervisor-phone actions. Limited inventory edits are not "settings" in
the sense of section 2 (spec 9.4 grants them explicitly). A supervisor may acknowledge an alert (spec 2),
which only marks it seen; answering it stays with the operator (spec 9.4). The spec holds as written (AC-2, section 2):
observers send LAND ALL and Land from a phone; a dashboard observer has them only when the operator
grants its token the land right (off by default; decision R-3). "Never gated" (spec 6.1(3)) means that
no confirmation, state or role-lookup error ever blocks a land from a session that has the right (16.3).

### 16.3 Land never fails closed

The LAND ALL and per-drone Land handlers check only that the session is authenticated and holds the land
right, which is read once from the credential at connection and cached in the session; a missing role
entry, an error in the role lookup or a revoked supervisor token on an authenticated connection never
blocks them. A test injects a role table that raises and checks that LAND ALL and Land still go out.

## 17. Operator controls and displays (spec section 9)

RETURN ALL first in the top bar, always visible and enabled, one press, Shift+R; LAND ALL always visible
and enabled, danger colour, Shift+L, apart from RETURN ALL, one press, overriding every return. A client
that cannot reach the ground says nothing was sent and points to the local land keys. Per-drone Return and
Land per section 10.8. The map shows homes, uncertainties, return routes and progress, landing order,
landed-in-place drones with reasons, zones, corridors, bands, obstacle tops and no-entry areas with their
kind. Drone cards show home and distance, uncertainty (a number; no calibration label), reserve,
state, countdowns and each value's source. The match alert panel is as spec 9.3, with the ambiguity mark
of 15.1. Every automatic action shows its reason within 2 s and is logged (AC-6.5).

## 18. Fleet protocol and telemetry

Additions are additive; new types are v2 only and are sent only to an edge whose `describe_unit` reply
advertises them. Shapes are defined once in A2.

| Addition | Direction | Content | Older peers |
| --- | --- | --- | --- |
| `return_home` | ground → edge | `wait`, `max_wait_s`; without `wait` accepted from any authenticated ground, with `wait` only from the authority (M-5) | never sent to an edge that does not advertise it; such an edge is shown "cannot return (older edge)" and held, never landed |
| `on_complete: "return_home"` | in `assign_lanes` | with `hover_timeout_s` as the re-task window (≥ 1 s, since the validator requires a positive value, `validate.py:182-185`) | older choices `hover`, `land` |
| `corridor`, `landing`, `no_entry`, `speed_cm_s` | in `assign_lanes` (`zone` already exists and is required, `validate.py:135`) | the corridor as a list of point objects `{"x_cm", "y_cm"}` (objects, so the 16-per-list and 64-per-message number caps of `constants.py:259`, `:262` never bind); `landing` = area id, place, interval; `no_entry` = areas as point-object polygons with kind and id | optional keys |
| `set_no_entry` | ground → edge (v2, authority; also accepted from a standby when it adds an area) | the full current list | |
| `set_speed` | ground → edge (v2, authority) | speed in cm/s | |
| `escort_setpoint` | ground → edge (v2, authority) | target position, altitude, bearing, `valid_s` | field units only |
| `describe_unit` | read-only query | as `hardware-profiles-detail.md` section 12.2; never feeds the watchdog; own cache; one per connection per 2 s; retried by the ground at 2, 4, 8 s and then every 30 s | not a configuration command |
| `unit_rev` | in the v2 edge `hello` | hash of the start-time resolution | ignored by older grounds |
| `rth` | telemetry object | state, reason, home, distance, uncertainty, reserve, threshold, countdown, cannot-recall, landing place | one top-level field: 37 + 1 = 38 of 48 |
| `cam`, `pitch_deg` | in `detections` | camera and pitch per frame | A6 |
| `ap_link_loss` | in `describe_unit` and inside the `rth` telemetry object | the autopilot's link-loss action, `return`, `land` or `unknown` (B-3) | ignored by older grounds; `unknown` counts as return |
| nack reason `straight_line_home` | `NACK_REASONS_V2` | an assignment whose straight line home leaves its zone and corridor while the action cannot be land (B-3) | v2 only |

Note (correction from the A2 review): "optional keys" in the `corridor`, `landing`, `no_entry`,
`speed_cm_s` row does not make them safe for an older edge. An edge from before A2 accepts them on a
v2 link and silently drops them, so they are gated like `on_complete: "return_home"`, and the gate is
enforced on the send path: `shape_for_peer` refuses such an `assign_lanes` unless the edge's
`describe_unit` reply lists the keys in `accepts` (`docs/fleet-protocol-v2.md`, the gates for the
ground and the edge).

Every motion command and `return_home` is refused with `landing` while the drone lands (10.2). A plan is
never refused because the drone is `landed` (N-19). Fleets stay within `MAX_ROSTER = 8`
(`fleet_protocol/constants.py:310`, `fleet_plan_check.py:259`): every SITL scenario uses at most eight
vehicles, and raising the cap is out of scope (N-9). Every new
key passes the privacy screen.

## 19. Mobile contract and apps (spec 9.4)

Phones pair for a supervisor or observer token (16.1) and receive the `session` event with their role
and allowed events. Every phone keeps LAND ALL and per-drone Land pinned; supervisor phones also get
RETURN ALL, per-drone Return, alert acknowledgement, resume-held, detection profile and the limited
inventory edits. New events handled: `session`, `inventory`, `flight_params`, `inventory_ack`,
`return_all_ack`, `targets`, `sightings`, `mission`, and the new `fleet_status` keys (home, distance,
uncertainty, reserve, state, countdowns, reasons, cannot-recall). iOS stays a ground console for a paired supervisor or observer token and never holds the operator key
(N-20); both apps implement the pairing flow of 16.1 (scan the QR code, pin the fingerprint, keep the
token in the Keystore or Keychain) and connect over pinned `wss` (N-14). Android
keeps phone-as-edge unchanged and its direct single-Tello mode is a spec non-goal. AC-6.5's 2 s on the
apps is tested by the ground's emission timing test plus app reducer tests that render the reason from
the fixture event synchronously, and checked once on a device (M-9).

## 20. Adapters and frame seams

1. **MAVLink/GPS (production):** `MavlinkFleetDrone` over MAVSDK, frames from the companion computer's
   capture source (`--source camera` or `url`; it does not declare `STEPPED_SENSING`), edge-guided return
   with the autopilot fallback (section 13), receiver uncertainty, autopilot home, uploaded fence.
2. **Tello (test adapter):** `TelloController`, frames from its video, dead-reckoned return, growth
   placeholders, pose-origin home; no new Tello-only constant.
3. **Fake and SITL:** the fake imitates a catalogue entry; SITL vehicles are MAVLink units verified
   simulated by their edge (section 6).

Frame sources (`CV2FrameSource` `edge_device.py:307`, `DroneFrameSource` `:444`, `ResilientCaptureSource`
`:672`) are wrapped so every frame carries `FrameMeta`; detections carry `cam` and `pitch_deg`. One
detection pipeline per unit.

## 21. Test strategy

### 21.1 Tiers

| Tier | What | Where | Gate |
| --- | --- | --- | --- |
| Unit and fake | every transition, matcher rule, airspace check, role rule, registry and resolver rule; property tests | every PR, CI (Linux), the ground-station laptop, the edge laptop | required |
| SITL | ArduCopter (and PX4 for PX4 paths) fleets of 2 to 8 (the roster cap) with real edges and the real ground; `scripts/field_fleet_ground.py` until D3, then the dashboard | opt-in `sitl` marker; macOS natively or Linux | required for PRs marked SITL; log attached |
| Hardware | indoor Tello runs (edge laptop); later field runs with a safety pilot | the edge laptop; field | listed per PR |

### 21.2 The SITL fleet harness (B1)

`scripts/sitl_fleet.sh --count N --autopilot ardupilot|px4` starts N vehicles with their own system ids
and separated homes; fixtures set the failsafe parameters of section 13, start one real edge per vehicle
(replay frames, no camera) and the ground. B1 merges after A3, so it uses the edge's new command line
(`--model`, `SEAM-3`) from the start (nitpick 11). B1 records the local `AP_FWVersion.h` patch that the
native Mac build needs (`docs/sitl-demo.md:122`, a local patch, not upstream) in the harness's setup
notes (nitpick 10), the SITL fingerprint each autopilot reports (section 6), the PX4 multi-vehicle
command, and the largest fleet up to eight that keeps telemetry at its rate. SITL runs at real time.
#213 merges before B1 (and before A1), because it edits `tests/integration/test_sitl.py` and
`docs/sitl-demo.md`.

### 21.3 Scenario catalogue and testability (M-9)

Each SITL scenario is named in the PR that needs it (`tasks.md`). Rules:

- **Absolute timing bounds** replace comparisons with an old build: after LAND ALL every drone's edge
  sends LAND within 1 s and the autopilot reports LAND mode within 2 s (RTH-20).
- **No automatic takeoff** is a property test: random sequences of every event, command and timeout,
  driven through the state machine and the ground's senders, never produce a takeoff (RTH-21).
- **AC-6.6** uses a geometry that can pass with the placeholders: a 300 cm out-and-back lane in a 400 by
  400 cm room, with the uncertainty bound at its default of 50 cm, where the predicted uncertainty at home
  stays below the bound; ten taped runs on the edge laptop.
- **Indoor AC-7.1** (marks confirmed) has its own requirement (RTH-25).
- **Final-round scenarios:** a pilot's mode change during `rth_landing` sends no second LAND (B-1); a
  shared landing area holds the place-1 drone above home without a blocked report (B-2); an escort
  re-task to a return-on-loss drone is switched to land or nacked, and the edge killed at the far
  corner of a zone lands (B-3); `SIM_GPS_DISABLE` with each accepted `FS_DR_ENABLE`, and PX4 GPS loss
  with `COM_POS_FS_ACT` 0 (B-4); an edge control-loop stall triggers the GCS failsafe (N-1).

### 21.4 The Windows edge laptop

The indoor Dev/test rig: two Tellos on two adapters, the C7 hardware gate (AC-6.6), the Windows
portability run of the suite and the launcher, and optionally a ground against a SITL fleet on the ground-station laptop over
the LAN. It is not the SITL host for large fleets.

## 22. The literal guard test (#197)

As in `hardware-profiles-detail.md` section 14 (rules A to F, name words derived from the registry ids,
allowlist keyed by file, enclosing scope, kind, callee or target, name and value with exact counts). The
scope adds `flightrisk/mission/`, `flightrisk/drone/rth/` and `flightrisk/drone/autopilot_fence.py` once
they exist. Measured on `82ec697` with the 160 ids of `parameters.md`: 524 findings with 514 distinct keys
in 57 of the 76 files in scope (`guard_main.txt`, regenerated with this revision).

The Tello coupling test (`tests/test_tello_coupling.py`, XCP) is the SEAM-6 extension: no code under
`flightrisk/` or `scripts/` outside `flightrisk/drone/tello*.py`, `registry.py` and `backends.py` imports a Tello module, compares against a
Tello adapter or model name, or uses a Tello-only constant, except the permanent exemptions
`flightrisk/main.py`, `flightrisk/drone/mavlink.py`, `scripts/demo_edge.sh`, `scripts/demo_edge.ps1`,
`scripts/demo_edge_pair.ps1` and `tests/test_demo_smoke.py` (owner decision). Today's other violations are a
checked-in baseline that may only shrink; each phase deletes the entries it fixes. The runtime-import guard
becomes a named-adopter allowlist in the same pull request.

## 23. Migration notes for operators

These go into `docs/launcher.md`, `docs/fleet-setup.md` and `docs/open-drone-support.md` with the PR that
causes them:

- **Presets** (`launcher.json`) load unchanged; drones become implicit `tello` units; new keys are written
  on the next save (A5).
- **Ceiling file:** the format is unchanged; from C5 the launcher writes resolved values.
- **Shell exports that win from C5 (Q1):** `FLIGHTRISK_GEOFENCE_CM`, `FLIGHTRISK_CEILING_CM`,
  `FLIGHTRISK_CEILING_MARGIN_CM` exported in a shell profile were ignored by the launcher and win from C5;
  C4 already warns about each, with its value.
- **`FLIGHTRISK_LINK_LOSS_LAND_S`** keeps its name and sets `link.loss_detect_s`; while
  `link.loss_action` is `land` (today's Tello default) its old meaning holds exactly; from C4 the launcher
  and the edge warn that its meaning changes at C7 and name `FLIGHTRISK_LINK_LOSS_WAIT_S` and
  `FLIGHTRISK_LINK_LOSS_ACTION` (M-8).
- **Companion computers:** C4 lists every setting that C5 will refuse, so a field edge can be fixed before
  C5 lands instead of failing to start in the field (M-8).
- **Phones (C3):** a phone that uses `FLIGHTRISK_API_KEY` over the network keeps LAND ALL and Land and
  must be paired for anything else.
- **Tello tuning values (user direction):** the depth threshold, settle tolerances, refusal caps and the
  other Tello-coupled values are no longer controls or warnings anywhere; developers may still set their
  environment variables for bring-up. #198 drops its depth-threshold control and uncalibrated warning
  before it merges, and #216 makes the sidestep off by default.
- **Autopilots (B4):** the parameter table of section 13 with the values to set on each autopilot,
  including `FS_DR_ENABLE` 1 (ArduPilot's default 2 is refused) and `FS_EKF_THRESH` above 0.
- **Dashboard observers (C3):** lose LAND ALL and Land unless the operator grants their token the land
  right; cleartext observer sessions no longer receive imagery.
- **Companion computers (B4):** a field edge whose `mavsdk_server` cannot be tied to it refuses takeoff.

## 24. Risks

- **The default switches change how every drone ends a flight.** They come last, in four steps (C4 warn
  only, C5 bounds and confirmations, C6 field defaults gated by SITL, C7 indoor defaults gated by the edge laptop
  runs), each revertible on its own (M-8).
- **SITL is idealised**; nothing here claims field readiness; the hardware path of
  `docs/open-drone-support.md` still applies.
- **PX4 has never run in SITL here**; B1 brings it up and B4 reads its parameter enumerations (A8).
- **Ground load** (escort setpoints, pairwise separation at telemetry rate for eight drones) is measured
  in the eight-vehicle SITL run.
- **Pairing friction:** phones must pair once (C3); old phones keep landing rights meanwhile.
- **Telemetry headroom:** `rth` is the only new top-level field (38 of 48).

## 25. Decisions

### 25.1 From the approved spec (spec section 12)

1. Without a ground station a drone returns only inside its own zone and corridor at its slot, else lands
   after the wait. 2. Indoor route home: direct through cleared areas, retrace elsewhere. 3. A person cue on
   the way home with no answer: land after the wait. 4. Non-GPS drones indoors only, GPS drones in the
   field, never mixed. 5. Only supervisor phones get Return; every phone keeps Land. 6. Configurable
   on-find policy, optional converge and moving-target search. 7. Verification is the human's visual
   confirmation; Continue or End search; multi-target; escort optional and field only. 8. The search radius
   grows at running speed; search as fast as stays thorough. 9. Indoors: Tello family only until another
   room-scale model has a measured profile. 10. MAVLink/GPS is production; the Tello is the test adapter;
   Tello collision tuning is tabled. 11. RTH is the default for safety actions only (spec 10.1(7)).

### 25.2 Carried from PR #202 (revised where noted)

Q1 the environment wins over the launcher window (C5); Q2 the Tello geofence default is 450 cm (C5); Q3
`tello_edu` is its own entry sharing the Tello column; Q4 Tello driver thresholds and SDK facts are
read-only; Q5 `unit_rev` in the v2 `hello` plus the `describe_unit` query; Q6 the launcher edits existing
fields plus inventory, model, feature switches and gimbal pitch; Q7 custom models narrowing only, with
environment and indoor profile read-only; Q8 the allowlist keyed by file, scope, kind, callee or target,
name and value with counts; Q9 `describe_unit` is a read-only query from any ground with its own cache, a
rate limit and retries; Q10 one inventory on the ground host with `rev` and compare-and-swap; Q11 phones'
inventory rights are supervisor-only (16.2); Q12 `top_down` on a level camera is advisory; Q13 bird's-eye
is a task type matched per unit in the sweep band; Q14 the generic default camera is fixed forward RGB;
D-F18 every Tello height, distance and timing is a registry row, the SDK facts read-only.

### 25.3 Made in this architecture

- D-B1. MAVLink returns are edge-guided (into the slot, to above home, hold, land); the autopilot's
  return-to-launch is the constrained fallback (replaces D-A1).
- D-B2. Confirmations are enforced at Start on the ground for every unit with a current description, in
  `field_fleet_ground.py` while it plans field missions, for in-flight less-safe changes and for a
  standby's values (extends D-A2).
- D-B3. LAND ALL and Land are allowed to the operator, supervisors and observer phones; a dashboard
  observer has them only with an explicit per-token land right, off by default (spec AC-2 as written;
  decision R-3 of the orchestrator, superseding revision 2).
- D-B4. Observer phones only land; resume-held, detection profile and inventory edits are supervisor
  actions (spec section 2 is the more specific rule).
- D-B5. Simulated means verified by the edge against the autopilot; the inventory cannot say it (replaces
  D-A3).
- D-B6. The ground never lands or re-lands a lost drone; it waits for a human confirm, with #212's
  refusal as the precondition.
- D-B7. Constructor defaults keep today's behaviour; resolved defaults carry the spec's (D-A4 kept).
- D-B8. The safety-value change log, verified by hashes, enforces review of table, golden file and
  allowlist (D-A5 made concrete).
- D-B9. The default switch is four PRs (C4 to C7) with separate field and indoor defaults.
- D-B10. The Android direct single-Tello mode is unchanged (spec non-goal; D-A6 kept).
- D-B11. Escort setpoints are computed on the ground at 1 Hz and held by the edge when older than 2 s
  (D-A7 kept).
- D-B12. Tello-coupled tuning values are read-only test-adapter facts, never operator-facing (user
  direction, section 7.5).
- D-B13. Deviation from spec 9.5: the "uncalibrated" label on the drone card's uncertainty is dropped by
  user direction; the uncertainty is shown as a number.
- D-B14. Cleartext observer sessions receive no imagery (decision R-4 of the orchestrator).
- D-B15. Fleets stay within the protocol roster cap of eight; SITL scenarios use at most eight vehicles
  (N-9).
- D-B16. External control is a terminal sub-state of the state machine (B-1); the progress monitor runs
  only on horizontal return legs (B-2); the edge owns the straight-line-home check (B-3).

## 26. Choices decided

No choice remains open. The orchestrator decided R-1 to R-5:

- **R-1.** `FLIGHTRISK_LINK_LOSS_LAND_S` keeps its name and means `link.loss_detect_s`; C4 warns that
  its meaning changes at C7 and names `FLIGHTRISK_LINK_LOSS_WAIT_S` and `FLIGHTRISK_LINK_LOSS_ACTION`.
- **R-2.** The field takeoff minimum becomes 40 % in C6, so constraint K-3 holds for the field profile.
- **R-3.** The spec holds as written: dashboard observers get no LAND ALL or Land by default; the
  operator may grant a per-token land right explicitly (default off). No spec amendment.
- **R-4.** Observer tokens are allowed over plain connections, but imagery (reference photos, crops,
  frames) is withheld from cleartext observer sessions.
- **R-5.** The environment variables of test-adapter facts stay as developer overrides, honoured only
  on test-adapter and verified simulated units; the off value of a safety switch among them is
  confirmed at Start (section 7.5).

## 27. Behaviour changes, by PR (complete list)

Rule: constructor defaults keep today's behaviour; only resolved defaults change. Each PR may edit only
the existing assertions listed for it; a PR whose list says "named in its description" names the exact
lines there before work starts, and any other failing assertion stops the PR. The parity test
(`tests/test_param_parity.py`) fails on any acceptance difference not listed here, and a test checks that
every "from <PR>" tag in `parameters.md` matches an item below naming that PR and that row.

- **A1, A2, B1:** none.
- **A3, A4, A5:** 47. Each adopter of `resolve_unit` refuses at its own start the combinations pinned in
  `ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT` (`tests/test_param_parity.py`) for the names it reads (cross-value
  constraints K-3, K-6, K-8, K-12 and the test-adapter temperature pair), and a production (not simulated)
  MAVLink unit ignores the test-adapter fact overrides pinned in `IGNORED_ON_PRODUCTION_UNIT`, with one log
  line naming each variable (N-10, FACT-3). Environment and flag ranges stay today's (SEAM-10). Assertions:
  any existing test that starts an edge, a ground or a launcher with one of the pinned combinations, named in
  the description. For A5 the assertions are `tests/test_launcher_commands.py:67` and `:119-121`.
  A3's detail: the edge resolves its unit at start with `resolve_unit`, so before it contacts the drone it
  refuses these ranges of input, which a real edge start accepts today. K-3 on the Tello:
  `FLIGHTRISK_MIN_TAKEOFF_BATTERY` from 15 to 25 %, both included, because a takeoff minimum at or under the
  25 % return floor lets a drone take off with its battery already at the level where it must return. Today
  accepts 15 to 100 % and keeps a fraction, so 25.5 and 26 are still accepted; no variable sets the Tello's
  return floor. K-6 on a Tello or a MAVLink unit: `FLIGHTRISK_LINK_LOSS_LAND_S` above 0 and under 1.5 s,
  because a detect time under 1.5 times the 1 s heartbeat interval can trigger on one missed heartbeat plus
  0.5 s of jitter. Today accepts any value above 0, so 1.5 s and more are still accepted. K-8 on the Tello:
  any `FLIGHTRISK_CEILING_CM` minus `FLIGHTRISK_CEILING_MARGIN_CM` under 130 cm, the highest of the Tello's
  altitude slots (100 and 130 cm) or `--alt-slot` when that is higher, that today accepts. Today accepts a
  ceiling minus margin of at least the start's slot (`--alt-slot`, 100 cm by default) and at least the 50 cm
  lowest slot. So with no `--alt-slot`, 100 to 129 cm left under the ceiling is newly refused (for example a
  margin of 871 to 900 cm under a 1000 cm ceiling); with an `--alt-slot` under 130 cm, from that slot (or
  50 cm, if higher) to 129 cm; with an `--alt-slot` of 130 cm or more, nothing. K-12 and the Tello
  temperature pair: none newly refused, because today's start checks already refuse every input the probes
  find. Today's start checks also refuse the other inputs of `ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT` in
  `tests/test_param_parity.py`, so refusing them changes nothing; `tests/test_edge_a3_changes.py` pins the
  split and sweeps each range above at both of its ends. The edge also now reads the params file
  (`~/.flightrisk/params.json`, or `FLIGHTRISK_PARAMS_FILE`) and the inventory (`FLIGHTRISK_INVENTORY_FILE`),
  which today's edge does not read. A file that is present but cannot be read or is not valid (bad JSON, a
  wrong shape or version, or a directory) now stops the start, as do an inventory unit whose model does not
  fit the backend and a safety feature stored off in either file. A file that is absent is treated as empty,
  so a start without one is unchanged. The launcher preflight and `docs/launcher.md` row still let a Tello
  takeoff minimum of 15 to 25 % pass until A4 aligns the check (tasks.md, A4); the edge refuses it at start.
  A production MAVLink unit does not resolve the 9 overrides of the Tello obstacle-sensor facts
  (`FLIGHTRISK_OBSTACLE_*`), but its edge still reads them through today's obstacle reader, so a MAVLink
  camera edge senses with them exactly as today; the start log says so in one line per variable, and the
  change waits for B1 (R6). A production MAVLink unit ignores the 9
  overrides of the Tello flight-guard and temperature facts (the `FLIGHTRISK_TELLO_*` settle, late-takeoff,
  refusal and temperature variables), which its model does not hold, with the same log line naming each
  variable; a MAVLink drone never uses these settings, so its flight does not change. The edge also refuses a start in which an environment variable or flag that today's envelope does not read for that drone would win an envelope row (an altitude slot or a battery level) over a launcher or params-file value, because the drone would otherwise fly neither value; before A3 that start was accepted and the launcher or file value was silently dropped. Until B1 adds the SITL
  board identity check (CAP-6), only the fake adapter counts as simulated, so every MAVLink unit counts as
  production. A test-adapter fact is never classified or logged as less safe, from any layer or value
  (R7, R8). Assertions: `tests/test_param_resolver.py:142` (a fact's off value is applied but no longer
  marked less safe).
- **A3, A4, A5, B4:** 53. The 120 m altitude limit, which comes from FAA rules that may not apply to a
  search-and-rescue mission, becomes a separate regulatory cap that is on by default for every unit. An
  operator may turn it off for a unit at launch only, with an explicit acknowledgement that the mission is
  outside FAA jurisdiction and that the operator is entirely responsible. The acknowledgement is recorded
  with the launch and logged by the edge, and it is never implied or carried over silently. The ground can
  show the cap's state but cannot change it, so no wire shape changes. The airframe maximum altitude is row
  8 (`envelope.max_altitude_cm`). For `generic_multirotor` it becomes unknown unless the inventory states
  it. The 120 m values that stand in for it today stop counting as a known maximum: row 8's 12000 cm for
  `generic_multirotor`, the catalogue's `max_height_m` of 120 for the generic models, MavlinkFleetDrone's
  declared 120 m, the `DroneCapabilities` default of 120 m and the legacy controller's 120 m
  (`flightrisk/drone/mavlink.py`, the dashboard's MavlinkController). Every consumer of the airframe maximum
  must handle an unknown value without crashing and without falling back to a lower or default cap. With the
  cap off, only the bounds that are 120 m because of the cap rise to the unit's known airframe maximum: the
  field ceiling range, the field altitude slot range and the `FENCE_ALT_MAX` check (ArduPilot) and PX4's
  `GF_MAX_VER_DIST`. `flightrisk/drone/coverage.py` `MAX_ALT_M` and the 12000 cm airspace rows stay at 120 m
  for now, and environment and flag ceiling and slot values stay at or below 12000 cm (SEAM-10's today
  ranges). Indoor bounds and every other bound are unchanged. The autopilot fence (B3) still applies, and
  the FlightRisk geofence is horizontal and does not bound height. A unit with no known airframe maximum
  cannot turn the cap off. `regulatory.altitude_cap_on` is read-only to every generic path: the edge refuses
  an off value from a parameter file, the environment, a flag, `--confirm-less-safe` or a ground message,
  and takes it only from the launcher layer's confirmation with a complete acknowledgement. It refuses an
  incomplete acknowledgement. Owners: A3 for the rows listed in tasks.md, the catalogue value, the stale
  note at `scripts/make_param_golden.py:178`, and the edge's enforcement and refusal; A5 for the launcher
  control, the acknowledgement text and its record; A4 for the ground's display of the cap state and of an
  unknown maximum; B4 for the `FENCE_ALT_MAX` bound. The `DroneCapabilities` type change that allows an
  unknown maximum is behaviour-neutral and lands separately. Assertions: named in each PR's description.
- **A4:** 1. A fleet mixing indoor and field units is refused at Plan (nitpick 12).
  48. The dashboard's default MAVLink address becomes `udpin://0.0.0.0:14540` (today `udp://:14540`, the
  listen form MAVSDK 2 and later reject). Assertions: `tests/test_config.py:107`.
  49. Without `inventory.json`, an edge-reported model that the catalogue does not know no longer becomes a
  `tello` unit; Plan refuses that unit, naming the reported model (CAP-2, INV-5). Fleets of known models are
  unchanged. Assertions: `tests/test_inventory.py:265`.
- **X0, XCb, XL:** none.
- **XCa:** none for behaviour. The `DroneCapabilities` type change that allows an unknown maximum for item 53 is behaviour-neutral and lands separately.
- **XCP:** none. Assertions: `tests/test_param_change_control.py:167`
  (`test_no_runtime_module_imports_params_or_hardware` becomes the named-adopter allowlist of
  `tests/param_adopters.py`; the coupling test `tests/test_tello_coupling.py` is added beside it).
- **B2:** 2. Field Start is refused without a 3D fix, with too few satellites, with receiver accuracy above
  `home.accuracy_cm`, with the autopilot's home beyond `home.max_offset_cm`, or outside the mission fence.
  3. A home that changes in flight lands with `no_known_home`. 4. Positioning uncertainty is reported as
  2 × the receiver's accuracy plus speed × age.
- **B3:** 5. The fences are uploaded before arming; a failed upload, a read-back mismatch or another fence
  action refuses the takeoff.
- **B4:** 6. Newly refused autopilot values (section 13.2): ArduPilot `FS_GCS_ENABLE` other than 1 or 5,
  `FS_GCS_TIMEOUT` outside 2 to 10 s, a GCS system id other than the edge's, `FS_OPTIONS` with bit 0, 1, 2
  or 4 set, `FS_EKF_ACTION` 0 or 2, `FS_EKF_THRESH` 0, `FS_DR_ENABLE` 2 (its default), 3, 4 or 6,
  `RTL_CLIMB_MIN` above the band ceiling minus the slot, `BATT_LOW_VOLT` other than 0 with a capacity
  threshold, `RTL_ALT_FINAL` above 0, any rally point, action 6 in any failsafe,
  `FENCE_ACTION` 3, 4 or 5, `BATT_FS_CRT_ACT` other than 1, `BATT_FS_LOW_ACT` 3, 4, 6 or 7, `FS_THR_ENABLE`
  4 to 7, a low-battery level above the return floor − 5 points, a voltage-only low threshold; PX4
  `COM_DL_LOSS_T` outside 5 to 10 s, `COM_DLL_EXCEPT` or `COM_RCL_EXCEPT` other than 0, `COM_POS_FS_ACT` 1,
  `COM_POS_LOW_ACT` 2, 3 or 4 while the low-accuracy check is enabled, `RTL_LAND_DELAY` −1, mission-landing `RTL_TYPE` values, any rally
  point, `GF_ACTION` 2, `COM_LOW_BAT_ACT` 3, `BAT_LOW_THR` above the floor − 5 points, `RTL_DESCEND_ALT`
  above the slot. 7. The edge clears the autopilot's mission and writes the landing delay before arming.
  8. On a MAVLink unit the critical battery is the autopilot's critical action, not the edge's 15 %.
  40. A field takeoff is refused unless `mavsdk_server` is tied to the edge, and a control loop that stalls
  longer than the autopilot's GCS timeout kills the server (N-1). 41. The edge reports `ap_link_loss`.
  50. `--allow-unverified-failsafes` and `link.require_failsafes = false` are refused unless the unit is a
  verified simulated unit (CAP-6); today they skip every failsafe check on any vehicle (MAV-14).
  Assertions: the MAVLink start-path tests that pass the flag with a fake vehicle that is not verified
  simulated, named in the description.
  51. A field takeoff is also refused while another process holds the autopilot's serial device (a leftover
  `mavsdk_server`); the launcher and the edge show the preflight row "serial port free" (extends item 40,
  N-1). Assertions: none expected; any found are named in the description.
  52. The MAVLink adapter turns in place through MAVSDK (`MAV_CMD_CONDITION_YAW` sent with
  `mavlink_passthrough`, no pymavlink), so stepped camera sensing no longer cuts a lane the drone does not
  already face. B4f builds this only if the B1b SITL acceptance scenario shows lanes cut because the drone
  cannot yaw in place. Assertions: tests that pin such a cut for a MAVLink drone, named in the description.
- **D1:** 9. Field base separation becomes 500 cm. 10. Bands are enforced at Plan. 11. A field mission
  needs an obstacle top per area, and slots below it plus the margin are refused. 12. Homes closer than the
  base separation (plus both accuracies in the field) are refused; a shared landing area gets an order.
  13. Zones and corridors are generated and overlapping tubes in a band are refused; Plan gives a zone
  whose straight line home leaves zone and corridor only to a drone that reports a land-on-loss action. 14. Indoor slots no
  longer count as separation.
- **D2:** 15. The ground loss timeout is derived: a field drone is declared lost after 15 s instead of 5 s
  (a Tello stays at 5 s until C7). 16. The ground no longer lands or re-lands a lost drone as a loss reaction. 17. A lost
  drone's lanes wait for a human confirm instead of being offered at once. 18. Lost-drone no-entry areas
  replace the 20 s point, widened by k × accuracy + `noentry.lost_margin_cm`. 42. #212's idle-controller
  countdown becomes the stranded event with `stranded.lost_idle_s` and `stranded.action` (land, as today);
  Resume task is refused once a lane of the lost drone was confirmed for another drone. Assertions: those of the lost-drone and orphan tests (`tests/test_lost_drone_manual_reland.py`
  from #212, `tests/test_orphan_retask.py`, and the lost-drone tests of the coordinator suites), named in
  its description.
- **D3:** none for existing flows (field missions become plannable on the dashboard).
- **C1:** 19. While a drone lands, the edge refuses `return_home`, `escort_setpoint`, `assign_lanes`,
  `start_at`, `resume`, `nudge`, manual moves and `set_speed` with `landing` (today `assign_lanes` and the
  fenced commands already are; the rest are new). 20. The MAVLink adapter never turns a decided landing
  into a return. 43. The edge refuses a takeoff or nacks an assignment with `straight_line_home` when
  the straight line home of its autopilot's return leaves the assignment's zone and corridor and the
  action cannot be set to land; it consumes the landing order of `assign_lanes.landing` for the
  autopilot's landing delay; it refuses legs into known no-entry areas (moved from D1, N-7).
- **C2:** 21. RETURN ALL and per-drone Return exist; LAND ALL moves apart from RETURN ALL. 22. LAND ALL
  marks every drone landing, sends the land, then cancels every other ground sender asynchronously. 23. A returning drone holds above home until the
  ground clears its landing. C2 also reads `task.end_action` and `stranded.action` with today's defaults
  (land), so C6 and C7 change only defaults (N-6).
- **C3:** 24. The operator key works only on loopback; elsewhere it gives observer-phone rights (land only); supervisor and
  observer devices pair for tokens; supervisor tokens need TLS. 44. Dashboard observers lose LAND ALL and
  Land unless their token has the land right (R-3); cleartext observer sessions receive no imagery (R-4).
- **C4:** 25. Warnings only: every value C5 will refuse or ask to confirm, and every deprecated meaning, is
  shown and logged; nothing is refused.
- **C5:** 26. Target bounds enforced. Values accepted today that become refused: `FLIGHTRISK_GEOFENCE_CM`
  below 40 or above 5000; `FLIGHTRISK_FIELD_GEOFENCE_M` and `--geofence-m` below 5 m;
  `FLIGHTRISK_CEILING_MARGIN_CM` above 500; `FLIGHTRISK_FIELD_CEILING_MARGIN_M` and `--ceiling-margin-m`
  above 30 m; `FLIGHTRISK_MIN_TAKEOFF_BATTERY` below 20 or above 95; `FLIGHTRISK_LINK_LOSS_LAND_S` below
  1.5 s or above 10 s (Tello) or 30 s (field); `FLIGHTRISK_FIELD_LINK_LOSS_HOLD_S` and `--link-loss-hold-s`
  below 1.5 s or above 30 s (H-11); `FLIGHTRISK_MANUAL_ORPHAN_LAND_S` below 30 s (refused, not raised) or
  above 600 s; `FLIGHTRISK_FLEET_STRIP_CM` and `--fleet-strip-cm` below 20 or above 1000 cm;
  `FLIGHTRISK_FLEET_AREA_CM` and `--fleet-area-cm` with a side below 20 or above 20 000 cm; `--speed-mps`
  below 0.5 m/s. 27. The environment wins over the launcher window for the three stripped variables.
  28. Start confirmations on the dashboard, in the launcher (409) and in `field_fleet_ground.py`; less-safe
  custom defaults become usable with a confirmation; in-flight less-safe changes and a standby's values
  are confirmed. 29. The Tello geofence default becomes 450 cm for an edge started without a launcher, and
  the rehearsal preset becomes 450 cm. 30. Slots below the profile default need a confirmation (the
  "Layout A (edges on this computer)" preset's 60 and 80 cm after #207). 31. The launcher writes the resolved
  ceiling values. Assertions: `tests/test_safety.py:206` (`(500, 30, 15)` to `(450, 30, 15)`);
  `tests/test_manual_ground.py:415` (the cases `("5", 30.0)` and `("0", 30.0)` become refusals); and the
  launcher tests that start the Layout A or rehearsal presets, named in its description.
- **C6 (field defaults, SITL-gated):** 32. Task end, long link loss, the stranded countdown and low
  battery return (`task.end_action`, `stranded.action`, `link.loss_action`, the energy threshold).
  33. The field takeoff minimum becomes 40 % (R-2). 45. On a MAVLink unit the edge-started returns (long
  link loss, low battery, task end, stranded) switch from the autopilot's RTL (`mavlink_fleet.py:1196-1236`)
  to the edge-guided return (`return.field_method`, N-6). Assertions: the MAVLink start-path
  tests that pin a land on these events, named in its description.
- **C7 (indoor defaults, edge-laptop-gated):** 34. The Tello holds 10 s after the short loss and returns
  (`link.loss_wait_s` 0 to 10, `link.loss_action` land to return); its ground loss timeout becomes 15 s;
  the stranded countdown returns; low battery returns at the threshold (floor 25 %) and only critical
  (15 %) lands; transient errors retry once and return; a failed sensor retraces home. Assertions: the
  Tello start-path tests that pin these lands, named in its description. The tests that construct
  `SafetyEnvelope` with `link_loss_land_s=1.5` (`tests/test_safety.py:193`, `tests/test_edge_uplink.py:206`)
  and the launcher fixture's `FLIGHTRISK_LINK_LOSS_LAND_S=5` (`tests/test_launcher_server.py:110`) stay
  valid: 1.5 and 5 lie inside the 1.5 to 10 s bound (1.5 heartbeat intervals is the smallest value one
  missed 1 s heartbeat plus 0.5 s of jitter cannot trigger). 46. The Tello's `lost.flying_window_s` becomes
  the derived formula with a 20 s floor (N-3).
- **F1 to F3:** 35. Paired supervisor phones get RETURN ALL, Return and the supervisor actions of 16.2.
- **D4:** 36. Plans are capability-matched tasks; an all-Tello fleet gets today's zones and lanes
  (snapshots for 1 to 4 Tellos).
- **D5:** 37. Lane spacing and speed follow the search-speed rule; the field overlap becomes 30 %
  (today 0.2 in `scripts/field_fleet_ground.py:99`).
- **E1:** 38. A confirmed match no longer lands every fleet drone; the on-find policy runs, keep watch by
  default. Assertions: the fleet-recovery tests that expect every drone to land on Confirm, named in its
  description.
- **E2 to E4:** 39. Converge, local search and escort, each started only by an operator press.

## 28. How the review findings are resolved

| Finding | Resolution | Where |
| --- | --- | --- |
| **C-1** landing not terminal | Every landing decision enters the edge state machine; landing is terminal; the edge nacks every motion command and starts no automatic return while landing; the MAVLink adapter never turns a landing into RTL; LAND ALL first cancels every ground sender; fake and SITL tests send each after LAND ALL | 10.2; C1, C2; RTH-26 to RTH-28 |
| **C-2** the ground declares a holding drone lost and re-tasks at once | Derived per-unit loss timeout with a constraint; no land or re-land; lanes wait for a human confirm with #212's refusal as precondition; zone, corridor, return line and landing area become no-entry; in D2, before every switch | 12; D2; LOST-1 to LOST-6 |
| **H-1** no-ground returns only on the edge | D1 generates zones and corridors, checks tube overlap within a band and the straight line home (else autopilot link-loss action land); landing order for shared areas; no-entry areas pushed to edges; zone and corridor re-sent with every re-task; task zones for drones outside their own zone | 10.9, 11.4, 11.7; D1; AIR-7 to AIR-11 |
| **H-2** MAVLink return and failsafe rules incomplete | Parameter table for both autopilots with exact accepted values, today's values and SITL scenarios; edge-guided return into the slot first; autopilot low level below FlightRisk's; autopilot-started return the highest-priority route; hold above home until cleared; progress monitor | 13, 10.1, 10.8; B4; MAV-5 to MAV-14 |
| **H-3** confirmation bypasses | Start refused without a current description; simulated verified by the edge against the autopilot; Start confirmation in `field_fleet_ground.py`; less-safe custom defaults refused until C5; in-flight and standby confirmations | 6, 7.3; C5; R2-B1, CONF-1 to CONF-6 |
| **H-4** self-declared phone roles | Role from the credential alone; operator key loopback only; per-device tokens; TLS for supervisor tokens; the event-by-role table with contradictions resolved; LAND hard-allowed with a test | 16; C3; ROLES-1 to ROLES-9 |
| **H-5** phase order | A, B, D1 to D3, C1 to C7, then F, D4, D5, E; D5 and E1 sequential | `tasks.md` |
| **H-6** bands not computable | Both band intervals as rows; constraints | 11.3; `parameters.md` |
| **H-7** home check always true | Homes at least base separation (+ accuracies) apart; closer than landing separation share a sequenced area | 10.7, 11.4; D1 |
| **H-8** returns end short of home | Threshold = critical + energy + worst-case wait + reserve; constraints with current ids in `parameters.md` | 10.6; `parameters.md` |
| **H-9** no-entry coverage | Moving last position, zone, corridor, return line, landing area; #212 precondition; confirm-to-clear versus confirm-to-enter; owner exempt | 11.7, 12 |
| **H-10** obstacle tops | Per-area `airspace.obstacle_top_cm`; Plan refuses slots below it plus a margin | 11.5 |
| **H-11** `parameters.md` contradictions | Phase tags corrected to `tasks.md`; row 17 bounds 1.5 to 30 s; row 4 safer higher, confirm less safe; a test ties tags to section 27 | `parameters.md`; 7.1 |
| M-1 uncertainty | k × accuracy + speed × age; missing = worst case; space-only tubes; in-flight monitor | 11.2 |
| M-2 matching | Banded ranking, input snapshot, task order, no reassignment of running tasks | 14.3 |
| M-3 escort | Hand-off before the return threshold; "in view" defined; allowed zones; deconfliction; ground-crash behaviour documented | 15.5 |
| M-4 multi-target | Per-target thresholds, ambiguous state, locked attribution, false-alarm note; no edge change | 15.1 |
| M-5 RETURN ALL availability | `return_home` without wait from any ground; older edges held, never landed | 10.8, 18 |
| M-6 search speed | Footprint for a level camera, initial rate, `set_speed` | 14.4 |
| M-7 protocol shapes | `zone` exists; point objects; re-task window ≥ 1 s | 18 |
| M-8 C4 blast radius | Split into C4 to C7; warn-only first; meaning of the old variable kept with warnings; field and indoor defaults separate | 23, 27; `tasks.md` |
| M-9 testability | Absolute timing bounds; property test; app timing test; indoor AC-7.1 requirement; hash-based change log; passable AC-6.6 geometry | 7.1, 19, 21.3 |
| M-10 termination | Energy-bounded waits; deterministic choice of the drone that lands | 10.8 |
| M-11 environment override | Environment and indoor profile read-only for custom models | 2, 6 |
| M-12 spec gaps | Ceiling breach and no-order reasons; every 6.2 row separate; escort end; accept as unsearched; converge pre-selection; observer tension | 10.4, 10.5, 14.3, 15.3, 15.5, 16.2 |
| User direction: Tello-coupled values | 30 rows (32 in revision 3) marked `fact` (read-only, not operator-facing); no requirement surfaces them to the operator; the depth threshold leaves the launcher (#198) | 7.5; `parameters.md` |
| Nitpicks 1 to 12 | Base `fc5f6aa` (now `82ec697`, revision 3); `zone` exists; `rth` names; requirement pointers fixed; SEAM-7 cites A6; spec 10.1(3); CAP-3 moved to C1; row 15 scope note; constraints with current ids; B1 records the native macOS build patch and uses the new command line; mixed-environment refusal in A4 | throughout |

### 28.1 Final round (revision 3)

| Finding | Resolution | Where |
| --- | --- | --- |
| **B-1** terminal landing drops the external-control latch | External control is a terminal sub-state: no automatic land, no retry, one LAND per operator command, every motion command refused with `landing`; exempt from the "no mode change away from LAND" assertion and RTH-20; fake and SITL pilot-mode-change test | 10.2, 10.3, row 21; C1; RTH-35 |
| **B-2** progress monitor has no scope | Runs only on horizontal return legs; paused in waits, above-home holds, slot moves, autopilot climb and loiter, person-cue hovers and landing; within `return.progress_min_cm` of home counts as arrived; an autopilot-started return only shows and alerts | 10.1, rows 21 and 29; C1; RTH-31 |
| **B-3** straight-line rule not implementable in D1 | The edge reports `ap_link_loss` (A2 shape) and checks the straight line home against every assignment it accepts; sets the action to land or refuses or nacks with `straight_line_home`; D1 plans with the reported action; escort only for land-on-loss drones; edge side moved to C1 | 10.9, 11.4, 15.5, 18; A2, C1, D1; AIR-8, RTH-36 |
| **B-4** position-fault row incomplete | ArduPilot `FS_DR_ENABLE` 1 or 0 and `FS_EKF_THRESH` > 0; PX4 `COM_POS_FS_ACT` 0 and `COM_POS_LOW_ACT` 5 or disabled, never 4; revision 2's manual-mode parameter dropped; SITL scenarios; section 27 item 6 | 13.2, 21.3, 27; B4; MAV-6, MAV-15, MAV-16 |
| N-1 edge hang | Field takeoff refused unless `mavsdk_server` is tied to the edge; a stall watchdog kills the server after the autopilot's GCS timeout; the flying window includes the hang bound and is never shortened by endurance | 13.1, 12; B4, D2; MAV-17 |
| N-2 LAND ALL delayed by cancellation | Mark, send, then cancel asynchronously; cancellation never blocks the send | 10.2; C2; RTH-28 |
| N-3 Tello flying window after C7 | Derived from C7 with a 20 s floor; section 27 item 46 | 12; C7; LOST-7 |
| N-4 #212 behaviour | Idle-controller countdown kept as the stranded event with `stranded.lost_idle_s`; Resume task refused once a lane was confirmed for another drone; Resume search stays refused | 12, row 12; D2; LOST-2, LOST-8 |
| N-5 standby takeover gates releases | Releases and landing clearances never gated | 7.3(6), 10.8; C5; CONF-5 |
| N-6 task-end and stranded actions | Rows `task.end_action`, `stranded.action`, `return.field_method`; C2 reads them with today's defaults; C6 switches MAVLink returns to edge-guided (item 45) | 10.4, 27; C2, C6, C7 |
| N-7 ownership and dependencies | Edge-side items moved from D1 to C1; D1 may edit `field_fleet_ground.py`; MAV-6 and MAV-7 split across B4, C1, C2, D4; B2 and B4 first owners of `rth/home.py`, `rth/uncertainty.py`, `rth/methods.py`; C4 and C5 edit `params/*`; #213 before A1 and B1; D4 after F2 and F3 | `tasks.md` |
| N-8 PAR-1, PAR-6, K-3 | Counts fixed; PAR-6 points to `parameters.md` 1.1; K-3 tagged "from C6" | `requirements.md`, `parameters.md` |
| N-9 `MAX_ROSTER` | Fleets and SITL scenarios capped at eight | 18, 21; D-B15 |
| N-10 R-5 too broad | Overrides honoured only on test-adapter and verified simulated units; safety-switch off values confirmed | 7.5, 7.3(3); FACT-3, FACT-4 |
| N-11 lost-drone area swallows zones | k × σ + `noentry.lost_margin_cm`, never speed × age | 11.7; D2; LOST-5 |
| N-12 landing order without the ground | Best effort; separation rests on K-15; `RTL_LOIT_TIME` capped at 60 s | 10.9, 13.2 |
| N-13 separation hold in a return line | The non-returning drone moves out of the returning drone's tube | 11.2; D1; AIR-2 |
| N-14 pairing trust | Pairing bound to the certificate fingerprint; Keystore and Keychain; pinned `wss` on both apps | 16.1, 19; C3, F2, F3; ROLES-10, MOB-9 |
| N-15 simulated fingerprint | Board identity required; SIH and HITL fixtures classified real | 6; B1, A3; CAP-6 |
| N-16 in-flight PRs | #198 drops its dashboard depth display, telemetry field and env-naming messages; rows for #207's refusal cap and fence margin; hover test exempt from FACT-2; row 37 at 92 °C | 2 (A1); `parameters.md`; FACT-2 |
| N-17 R-3 and R-4 | Orchestrator decisions: no dashboard-observer LAND by default with a per-token right; no imagery over cleartext | 16, 26; C3; ROLES-3, ROLES-11 |
| N-18 autopilot details | `BATT_LOW_VOLT` 0, `RTL_CLIMB_MIN` bounded, PX4 `COM_DL_LOSS_T` 5 to 10 s, K-1 and K-2 re-checked with the autopilot's critical level | 13.2, 10.6; B4; MAV-8, MAV-12 |
| N-19 `landed` and plans | Plans accepted while landed; only Start or manual takeoff flies | 10.2, 18; C1; RTH-26 |
| N-20 "operator" wording | Operator or supervisor laptop for confirmations; iOS is a ground console | 7.3, 19; CONF-4, CONF-5 |
| Nits | INV-1 and PROTO-1 pointers; Layout A 60/80 cm; row 145 tag item; spec 9.5 label deviation (D-B13); K-15 uses k × σ; line refs on `82ec697`; PRE-1 lists #216 and #213 | throughout |

## 29. External sources

- Tello SDK 2.0 User Guide: https://xcopter.com/download/Tello%20SDK%202.0%20User%20Guide.pdf
- Tello User Manual v1.2: https://www.adorama.com/col/productManuals/RYTELO.pdf
- Tello EDU specifications: https://core-electronics.com.au/dji-tello-edu.html.md
- 14 CFR 107.51: https://www.ecfr.gov/current/title-14/chapter-I/subchapter-F/part-107/subpart-B/section-107.51
- ArduPilot GCS failsafe: https://ardupilot.org/copter/docs/gcs-failsafe.html
- ArduPilot RTL mode: https://ardupilot.org/copter/docs/rtl-mode.html
- ArduPilot EKF failsafe: https://ardupilot.org/copter/docs/common-ekf-inav-failsafe.html
- ArduPilot parameter name changes: https://ardupilot.org/copter/docs/common-param-name-changes.html
- PX4 safety configuration: https://docs.px4.io/main/en/config/safety.html
- PX4 return mode: https://docs.px4.io/main/en/flight_modes_mc/return.html
- ArduCopter parameter definitions (`FS_DR_ENABLE`, `FS_DR_TIMEOUT`, `FS_EKF_THRESH`, `FS_EKF_ACTION`,
  `RTL_LOIT_TIME`): https://github.com/ArduPilot/ardupilot/blob/master/ArduCopter/Parameters.cpp
- PX4 commander parameters (`COM_POS_FS_ACT`, `COM_POS_LOW_ACT`, `COM_POS_LOW_EPH`, `COM_DL_LOSS_T`,
  `NAV_DLL_ACT`, `COM_LOW_BAT_ACT`):
  https://github.com/PX4/PX4-Autopilot/blob/main/src/modules/commander/commander_params.yaml
- PX4 return parameters (`RTL_TYPE`): https://github.com/PX4/PX4-Autopilot/blob/main/src/modules/navigator/rtl_params.yaml
