# Fleet architecture v1: phased PR plan

Six phases, 23 PRs. **No PR starts until the in-flight work has merged:** #207 (Tello incident
fixes), #198 (sensing controls and temperature), #208 (geofence, slot and battery consistency; keeps
`MIN_START_BATTERY` as an alias), #211 (detection-profile telemetry grouping), the lost-drone manual
re-land fix, and #205 (mobile models). #206 (floor ratio) is a draft and tabled. **A1 goes first.**

Every PR: implementation on the tier named, an opus critic review at xhigh effort capped at two
rounds, then merged unless a blocking defect remains. Every PR states its test plan ("unit", "fake",
"SITL", "hardware") and its behaviour changes; the behaviour changes are exactly those of `design.md`
section 20 for that PR, and the existing assertions it may edit are only those listed there. A PR
marked SITL attaches its SITL scenario log.

## Order and parallelism

```
A1 ─> A2 ─┬─> A3 ─┬──────────────────────────────> A5 (after A3 and A4)
          │       │
          ├─> A4 ─┤
          │       │
          └─> B1 ─┴─> B2 ─> B3 ─> B4 ─> C1 ─┐
                          (A3 first)        ├─> C4 ─> D1 ─> D2 ─┬─> D3
              A4 ─────────────────────> C2 ─> C3 ─┘              └─> E1 ─> E2 ─> E3 ─> E4
                                                                    E1, C4 ─> F1 ─┬─> F2
                                                                                  └─> F3
```

Parallel pairs: A3 with A4; B1 with A3 and A4; C1 with C2; D3 with E1; F2 with F3. Everything else is
sequential because it shares a file with its predecessor.

## File ownership (round 2 finding N5)

Each file has one owner per phase. A later PR edits a file only after its owner has merged; the
sequential order above guarantees it. Shared files are sectioned: `tests/param_literal_allowlist.py`
(one section per source file), `tests/test_param_parity.py` (each PR adds its own `PARITY_EXCEPTIONS`
entries), `tests/integration/test_sitl_scenarios.py` (one test function per PR), and
`docs/design/hardware-profiles/safety-value-changes.md` (append-only log).

| File or area | First owner | Later editors (in order) |
| --- | --- | --- |
| `flightrisk/params/*` (spec, table, resolve, files, messages, doc), `flightrisk/hardware/catalogue.py`, `scripts/make_param_golden.py`, `tests/golden/flight_params.json`, `.github/CODEOWNERS`, `docs/flight-parameters.md`, `docs/hardware-catalogue.md`, `docs/design/hardware-profiles/safety-value-changes.md` | A1 | table and golden: B2, B4, C1, C4, D1, D3, E1 to E4 (each with a log entry) |
| `flightrisk/hardware/{inventory,features,frames}.py`, `flightrisk/params/report.py`, `flightrisk/mission/model.py` (types), `flightrisk/drone/rth/__init__.py` and `rth/methods.py` (the return-method protocol), `flightrisk/fleet_protocol/*` (every wire shape of this plan), `flightrisk/obs_comm.py`, `flightrisk/drone/controller.py` (`CameraSource`), `flightrisk/drone/registry.py`, `flightrisk/drone/backends.py`, `flightrisk/vision/profiles.py` (fields), `tests/fixtures/mobile_contract/catalogue.json` | A2 | `fleet_protocol/*`: none (A2 defines every shape once) |
| `flightrisk/edge_device.py`, `flightrisk/edge.py`, `flightrisk/edge_profiles.py`, `flightrisk/edge_uplink.py`, `flightrisk/fleet_authority.py`, `flightrisk/fleet_authority_types.py`, `flightrisk/drone/{safety,obstacle_sensor,obstacle,flight_guards,temp_preflight,tello,tello_bound,fake,lane_flight,lane_executor,lane_monitor,lane_avoid}.py`, `tests/test_telemetry_field_cap.py` | A3 | C1 (edge return wiring), D3 (`det_hz`), E4 (escort setpoints) |
| `flightrisk/drone/{mavlink_fleet,autopilot,mavsdk_link}.py` | A3 (options from params) | B2, B3, B4 |
| `flightrisk/drone/coordinator.py`, `flightrisk/dashboard/*` (including `app.py`, `single_search.py`, `recovery.py`, `manual_control.py`, `ceiling.py`, `alerts.py`, `static/ui/*`, `templates/*`), `flightrisk/config.py`, `flightrisk/observability.py` | A4 | C2, C3 (`roles.py`, handler checks), C4 (Start confirmation), D1 to D3, E1 to E4, F1 |
| `flightrisk/drone/{fleet_plan_check,fleet_plan_model,fleet_patterns,fleet_scale,plan_edit,plan_paths,coverage,coverage_sim,geolocate,search}.py` | A4 (read through the registry) | D1, D3 |
| `flightrisk/launcher/*` | A5 | C4 |
| `scripts/sitl_fleet.sh`, `scripts/start_sitl.sh`, `scripts/field_fleet_ground.py`, `tests/integration/*` | B1 | every SITL PR adds its scenario |
| `flightrisk/drone/rth/autopilot_return.py`, `flightrisk/drone/autopilot_fence.py` | B4, B3 | |
| `flightrisk/drone/rth/{machine,dead_reckoned,energy,uncertainty,faults,home}.py` | C1 | C4 (defaults only) |
| `flightrisk/mission/returns.py` | C2 | D1 |
| `flightrisk/dashboard/roles.py` | C3 | F1 (phone events) |
| `flightrisk/mission/{airspace}.py`; `{matching,generators}.py`; `speed.py` | D1; D2; D3 | E2 to E4 |
| `flightrisk/vision/*` target embeddings; `flightrisk/mission/{targets,sightings,onfind}.py` | E1 | |
| `flightrisk/mission/viewpoints.py`; `local_search.py`; `escort.py` | E2; E3; E4 | |
| other `tests/fixtures/mobile_contract/*`, `docs/mobile-contract.md` | F1 | |
| `mobile/` | F2 | |
| `ios/` | F3 | |

## Phase A: core profiles, catalogue and inventory (no behaviour change)

### A1: catalogue, parameter registry, golden file, resolver, guard and parity tests

- **Goal:** design sections 6, 7 and 19 and `parameters.md`, as pure code with tests; nothing at runtime
  imports it yet.
- **Tier:** opus, high.
- **Depends on:** the in-flight work.
- **Files:** see the ownership table; `tests/test_hardware_catalogue.py`, `tests/test_params_registry.py`,
  `tests/test_params_resolve.py`, `tests/test_param_golden.py`, `tests/test_param_parity.py`,
  `tests/test_param_messages.py`, `tests/test_param_literals.py`, `tests/param_literal_allowlist.py`
  (from `docs/design/hardware-profiles/guard_rule_prototype.py.txt` and `guard_main.txt`), one line in
  `tests/test_architecture.py`.
- **Behaviour changes:** none.
- **Test plan:** unit.
- **Acceptance:** PRE-1, CAP-1, CAP-2, CAT-1 to CAT-5, CAT-7 (docs), PAR-1 to PAR-7, PAR-9, PAR-10,
  RES-1 to RES-8, RES-10, RES-11 (resolver), RES-12, GRD-1 to GRD-6, R2-B4, R2-N6, R2-N8, R2-N9,
  SEAM-6, CMP-1, CMP-3 (creation).

### A2: inventory, features, camera roles, mission types and every wire shape

- **Goal:** the pure records and the complete protocol additions of design section 15, validated but
  never sent.
- **Tier:** opus, high.
- **Depends on:** A1.
- **Behaviour changes:** none.
- **Test plan:** unit (validators, privacy screen on every new key, field-count cap with `rth`).
- **Acceptance:** CAT-6, CAT-7 (fixture), CAT-8, INV-1 to INV-3, INV-6, INV-7, the unit part of INV-4,
  FEA-1 to FEA-5, the profile-field part of FEA-8, the static part of FEA-10, ROLE-1 to ROLE-3, ROLE-5,
  the types part of SEAM-1, SEAM-2, PROTO-1, PROTO-2 (validator), PROTO-3 (validator), PROTO-5, PROTO-7,
  RTH-11 (validators), CMP-1.

### A3: edge adoption

- **Goal:** the edge resolves its unit at start with today's values and today's accepted ranges, builds
  every safety object from resolved values, wraps its frame sources, adds camera metadata to
  detections, advertises `unit_rev` in its `hello`, and answers `describe_unit` as a query that never
  feeds the watchdog.
- **Tier:** opus, xhigh.
- **Depends on:** A2. Parallel with A4.
- **Behaviour changes:** none.
- **Test plan:** unit, fake; the existing edge, safety, obstacle, avoidance and telemetry suites
  unchanged; PROTO-6 multi-ground test.
- **Acceptance:** PAR-8, RES-11 (edge), CAP-3, CAP-4, EDGE-1 to EDGE-7, FEA-7, FEA-8 (edge), FEA-10,
  FEA-11 (start and takeoff), ROLE-4, SEAM-1 (wrapping), SEAM-3, SEAM-4, SEAM-7, SEAM-8, PROTO-2 (edge),
  PROTO-4, PROTO-6, CMP-1.

### A4: ground and dashboard adoption

- **Goal:** the ground asks each edge to describe itself (with the retry of N7), stores per-unit views,
  plans each unit against its own bounds with today's values, reads `FLIGHTRISK_LAUNCHER_PARAMS` as its
  launcher layer (round 2 B2), and shows the read-only "Fleet hardware" and "Flight parameters" cards.
- **Tier:** opus, high.
- **Depends on:** A2. Parallel with A3.
- **Behaviour changes:** none.
- **Test plan:** unit, fake edges; plan snapshots for 1, 2, 3 and 4 Tellos taken at the A4 base.
- **Acceptance:** INV-4 (ground), INV-5, FEA-11 (banners), GND-1 to GND-4, GND-7, DSH-1 to DSH-3,
  DSH-5 to DSH-7, DSH-9, PROTO-3 (ground), R2-B2 (ground), R2-N7, CMP-1.

### A5: launcher adoption

- **Goal:** the inventory editor, unit-based drone rows, the effective-values table, and the launcher
  layer for edges and the ground, keeping today's precedence for the three stripped variables.
- **Tier:** opus, high.
- **Depends on:** A3 and A4 merged (round 2 B2: the edge and the ground must read the layer before the
  launcher stops setting the individual variables).
- **Behaviour changes:** none; the values travel in `FLIGHTRISK_LAUNCHER_PARAMS`.
- **Existing assertions it may edit:** `tests/test_launcher_commands.py:67`, `:119-121`.
- **Test plan:** unit; launcher command snapshots for every built-in preset.
- **Acceptance:** LCH-1 to LCH-3, LCH-5 to LCH-7, INV-4 (launcher), R2-B2, CMP-1.

## Phase B: MAVLink production adapter maturity

### B1: SITL fleet harness

- **Goal:** design section 18.2: `scripts/sitl_fleet.sh` for 2 to 10 ArduPilot or PX4 vehicles, the
  pytest fixtures, and the scenario file with a smoke scenario.
- **Tier:** sonnet, high.
- **Depends on:** A2. Parallel with A3 and A4.
- **Behaviour changes:** none (test infrastructure).
- **Test plan:** SITL smoke runs with 2 and 10 ArduCopter vehicles and 2 PX4 vehicles on the Mac;
  record the largest fleet that keeps telemetry at its rate.
- **Acceptance:** SITL-1 to SITL-3.

### B2: GPS home checks, home record and receiver uncertainty

- **Goal:** design section 10.4 for the field: the Start checks, the home record and the invalid-home
  landing, and the receiver's accuracy as positioning uncertainty.
- **Tier:** opus, high.
- **Depends on:** A3, B1.
- **Behaviour changes:** section 20 items 1 and 2 (field, simulated units today).
- **Test plan:** unit, fake; SITL (no fix, far autopilot home, home moved in flight).
- **Acceptance:** MAV-1 to MAV-3.

### B3: geofence upload

- **Goal:** upload the drone's fence and the mission fence to the autopilot before arming, verify the
  read-back and the fence action, never change fences in flight.
- **Tier:** opus, high.
- **Depends on:** B2.
- **Behaviour changes:** section 20 item 3.
- **Test plan:** unit, fake; SITL ArduPilot and PX4 (upload, read-back, breach returns).
- **Acceptance:** MAV-4.

### B4: failsafes, faults and the autopilot return method

- **Goal:** the autopilot's link-loss and critical-battery actions checked, positioning faults, and
  `AutopilotReturn` behind the return-method seam.
- **Tier:** opus, xhigh.
- **Depends on:** B3.
- **Behaviour changes:** section 20 item 4.
- **Test plan:** unit, fake; SITL (GPS loss, battery drain to critical, edge killed, refused return).
- **Acceptance:** MAV-5 to MAV-7.

## Phase C: the return-to-home default switch

### C1: edge return state machine (today's defaults)

- **Goal:** design sections 10.1 to 10.6 on the edge: the state machine, the energy and uncertainty
  models, the fault classifier, the home record for the Tello, the dead-reckoned return, the own-zone
  rule, and `rth` telemetry, all resolving to today's actions.
- **Tier:** opus, xhigh.
- **Depends on:** B4. Parallel with C2.
- **Behaviour changes:** none (RTH-2).
- **Test plan:** unit (every transition), fake (every Dev/test row), SITL (long link loss inside and
  outside the zone with the return action set explicitly).
- **Acceptance:** RTH-1 to RTH-11, RTH-18, RTH-21, RTH-23.

### C2: ground returns, RETURN ALL and the operator controls

- **Goal:** design sections 10.7 and 14: RETURN ALL, per-drone Return, ordering, landing areas, the
  stranded countdown's return action, home markers, routes, states and countdowns, and the new top bar.
- **Tier:** opus, high.
- **Depends on:** A4. Parallel with C1.
- **Behaviour changes:** section 20 item 5.
- **Test plan:** unit, fake; SITL (RETURN ALL with four drones and crossing routes, landing one per
  area); a documented browser check of the top bar.
- **Acceptance:** RTH-12 to RTH-17, UI-1 to UI-3.

### C3: roles enforced on the ground

- **Goal:** design section 13: role keys, the role table, deny by default, the `session` handshake.
- **Tier:** opus, xhigh (security).
- **Depends on:** C2.
- **Behaviour changes:** section 20 item 6.
- **Test plan:** unit (one refusal per role and event class, the handler-enumeration test).
- **Acceptance:** ROLES-1 to ROLES-6.

### C4: the switch

- **Goal:** design section 20 items 7 to 11: the resolved RTH defaults, the 450 cm Tello geofence and
  rehearsal preset, the target bounds, the environment precedence, the resolved ceiling file, and the
  Start confirmation on the ground with the launcher's 409.
- **Tier:** opus, xhigh.
- **Depends on:** C1, C3, A5.
- **Behaviour changes:** section 20 items 7 to 11.
- **Existing assertions it may edit:** those listed for C4 in section 20, plus the start-path assertions
  it lists in its description before work starts.
- **Test plan:** unit, fake, parity test exceptions for every item; SITL (the full return scenario set
  with the new defaults, LAND ALL during returns timed against the pre-C4 `main`); hardware AC-6.6 on
  the ZBook (ten taped runs) before merge.
- **Acceptance:** RES-9, FEA-6, DSH-8, LCH-4, LCH-8 to LCH-10, RTH-19, RTH-20, RTH-22, RTH-24, UI-5,
  R2-B1, R2-B3, CMP-2, CMP-3 (C4 exceptions).

## Phase D: the task model, capability matching and the field airspace

### D1: airspace

- **Goal:** design section 11: separation by uncertainty, bands, fences and no-entry areas (replacing
  the 20 s timer), precision degrade, the environment split at Plan.
- **Tier:** opus, high.
- **Depends on:** C4.
- **Behaviour changes:** section 20 item 12.
- **Test plan:** unit; SITL ten-vehicle separation stress run.
- **Acceptance:** AIR-1 to AIR-6, GND-5, GND-6.

### D2: missions, tasks and the matcher

- **Goal:** design sections 12.1 to 12.4: the mission and task records in use, the generators, the
  matcher with explanations, hand-off and re-task.
- **Tier:** opus, high.
- **Depends on:** D1.
- **Behaviour changes:** section 20 item 13 (none for an all-Tello fleet).
- **Test plan:** unit; fake; snapshots for 1 to 4 Tellos; SITL mixed simulated fleet with a custom
  thermal model.
- **Acceptance:** TASK-1 to TASK-8, FEA-9, FEA-11 (Plan), INV-8, GND-3.

### D3: field missions on the dashboard and the search-speed rule

- **Goal:** design section 12.10 and the dashboard's field planning (GPS-anchored areas, tasks on the
  map), `det_hz` from the edge.
- **Tier:** opus, high.
- **Depends on:** D2. Parallel with E1.
- **Behaviour changes:** section 20 item 14.
- **Test plan:** unit; SITL two-vehicle mission planned from the dashboard, throttled test detector.
- **Acceptance:** TASK-9 to TASK-11, SEAM-5.

## Phase E: on-find flows

### E1: targets, sightings, verification and the on-find policy

- **Goal:** design sections 12.5 and 12.6 and the alert panel of section 14.
- **Tier:** opus, high.
- **Depends on:** D2. Parallel with D3.
- **Behaviour changes:** section 20 item 15.
- **Test plan:** unit; fake two-target mission.
- **Acceptance:** FIND-1 to FIND-5, FIND-15, FIND-16, UI-4.

### E2: converge and verify

- **Tier:** opus, high. **Depends on:** E1.
- **Behaviour changes:** section 20 item 16 (operator-started only).
- **Test plan:** unit; SITL with three drones converging on a test sighting.
- **Acceptance:** FIND-6, FIND-7.

### E3: moving-target local search

- **Tier:** sonnet, high. **Depends on:** E2.
- **Behaviour changes:** section 20 item 16.
- **Test plan:** unit; SITL with a growing disc and a new sighting that resets it.
- **Acceptance:** FIND-8, FIND-9.

### E4: escort (field only)

- **Tier:** opus, xhigh (drones following a person). **Depends on:** E3 and B4.
- **Behaviour changes:** section 20 item 16.
- **Test plan:** unit; fake (stale setpoints hold); SITL with a test subject moving at running speed,
  a relief hand-off, a limit crossing and a loss of view.
- **Acceptance:** FIND-10 to FIND-14.

## Phase F: mobile

### F1: contract and fixtures

- **Goal:** design section 16 on the ground side: the phone events per role, the fixtures and
  `docs/mobile-contract.md`.
- **Tier:** sonnet, high. **Depends on:** C4 and E1.
- **Behaviour changes:** section 20 item 17 (ground side).
- **Test plan:** fixture generator tests; handler tests per role.
- **Acceptance:** DSH-4, MOB-1, MOB-6 (ground), UI-6.

### F2: Android

- **Tier:** sonnet, high. **Depends on:** F1. Parallel with F3.
- **Behaviour changes:** section 20 item 17 (Android).
- **Test plan:** Android unit tests against the fixtures; phone-as-edge tests unchanged.
- **Acceptance:** MOB-4, MOB-6 (Android), MOB-7, UI-6.

### F3: iOS

- **Tier:** sonnet, high. **Depends on:** F1. Parallel with F2.
- **Behaviour changes:** section 20 item 17 (iOS).
- **Test plan:** `GroundProtocolTests` against the fixtures.
- **Acceptance:** MOB-2, MOB-3, MOB-6 (iOS), UI-6.

## Requirement coverage

Every requirement in `requirements.md` names its PR in brackets; DOC-1 and CMP-4 apply to every PR,
SITL-4 to every SITL PR, and RTH-21 to every phase C PR. MOB-5 and MOB-8 are dropped (spec non-goal).
