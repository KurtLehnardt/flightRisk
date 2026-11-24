# Fleet architecture v1: PR plan

28 PRs. **No PR starts until the in-flight work has merged:** #207 (Tello incident fixes), #198 (sensing
controls and temperature, without its depth-threshold control, dashboard depth display and telemetry
field, "not calibrated" warning and messages naming environment variables), #208 (geofence, slot and
battery consistency; keeps `MIN_START_BATTERY` as an alias), #216 (sidestep off by default) and #213
(mavsdk pin and Windows link; it edits `tests/integration/test_sitl.py` and `docs/sitl-demo.md`, which B1
owns, so it merges before A1 and therefore before B1). #212 (lost drone flown by hand) is in the base
`82ec697`. #206 (floor ratio) is a draft and tabled. **A1 goes first.**

Every PR: implementation on the tier named, an opus critic review at xhigh effort capped at two rounds,
then merged unless a blocking defect remains. Every PR states its test plan and its behaviour changes;
the behaviour changes are exactly the items of `design.md` section 27 listed for it, and it edits only the
existing assertions listed there. A PR marked SITL attaches its scenario log.

## Merge order (finding H-5)

```
A1 ─> A2 ─┬─> A3 ─┬─> B1 ─┐
          └─> A4 ─┤       │
                  └─> A5  │      (A5 after A3 and A4)
                          v
                B2 ─> B3 ─> B4 ─> D1 ─> D2 ─> D3 ─> C1 ─> C2 ─> C3 ─> C4 ─> C5 ─> C6 ─┬─> C7
                                                                                       └─> F1 ─┬─> F2 ─┐
                                                                                               └─> F3 ─┤
                                    D4 (fast path: after B4; see Fast path) ───────────────────┴─> D5 ─> E1 ─> E2 ─> E3 ─> E4
```

- Phases in merge order: A (core; A1 and A2 change no behaviour, A3 carries items 47 and 53, A4 items 1, 47,
  48 and 49, A5 item 47 of section 27), B (MAVLink production adapter), D4 (tasks and the matcher, on the fast
  path), D1 to D3 (field airspace, lost drones and field planning, behind today's defaults), C (return
  machinery, roles, then the switch in four steps), F (mobile), D5 (search speed, after D4, F2 and F3), E
  (on-find flows).
- **Development may overlap where files do not:** C1 (edge files) can be developed from B4 on while D1 to
  D3 (ground files) are under review, and merges after D3; C7 (indoor defaults, gated by the edge laptop runs)
  and F1 can proceed side by side after C6 (C7 changes defaults only); F2 and F3 run in parallel. D4 edits
  fixtures the apps read (N-7): it merges before F1 and edits them additively only, and F1 later builds on it.
  D5 depends on D4, F2 and F3, so D5 and E1 to E4 merge after F. D5 and E1 are sequential.
- SITL scenarios before D3 use `scripts/field_fleet_ground.py` as the ground (D1 may edit it for its
  scenarios); from D3 on they use the dashboard. Every scenario uses at most eight vehicles (`MAX_ROSTER`).

### Fast path (owner decision, November 2026)

The fast path to a drone-agnostic, MAVLink-first FlightRisk is: X0, XCP, XCa and XCb, then A3 in parallel with
A4 (A4a to A4f) and XL, then A5 (A5a to A5c) in parallel with B1 (B1a, B1b), then B2, B3 and B4 (B4a to B4f),
then D4 (D4a to D4c), pulled ahead of D1 to D3, C and F. A3 is delivered as the A3.x pull requests that began
with #230. Every other phase in this file is a follow-up after the fast path's acceptance and keeps its order.
D4's edits to `tests/fixtures/mobile_contract/*` are additive only, so the current Android and iOS contract
tests pass unchanged; an edit that cannot be additive moves to F1. Without D1 to D3, D4 plans field fleets
through `scripts/field_fleet_ground.py`. Each A, B and D phase is split into stacked pull requests of about
1,500 changed lines or fewer with one risk area each; `plan/PLAN.md` of the planning pass lists them, except
A3's, which are the A3.x pull requests.
SITL gates run natively on the ground-station laptop (macOS) only (ArduCopter 4.6.3 and 4.7.3, PX4 v1.17.0
SIH); the edge-2 laptop (Windows) runs only the unit suite, and MAVLink phases may merge without a Windows
run.

**Runtime-import guard.** XCP replaces the assertion in `tests/test_param_change_control.py`
(`test_no_runtime_module_imports_params_or_hardware`, today a ban on any run-time import of `flightrisk.params` or
`flightrisk.hardware` outside those packages) with a named-adopter allowlist, `tests/param_adopters.py`, sectioned
by phase and empty at XCP, plus a test that rejects dead entries. The TYPE_CHECKING and leaf-import exemptions
stay. Each adopting PR (A3, A4, A5) adds its own paths, and the guard still fails on any unnamed adopter.

## File ownership (round 2 finding N5)

Each file has one owner at a time; a later PR edits it only after the earlier one merged, which the merge
order guarantees. Shared, sectioned files: `tests/param_literal_allowlist.py` (one section per source file),
`tests/test_param_parity.py` (each PR adds its own exceptions), `tests/integration/test_sitl_scenarios.py`
(one function per scenario), and `docs/design/hardware-profiles/safety-value-changes.md` (append-only).

| File or area | First owner | Later editors |
| --- | --- | --- |
| `flightrisk/params/*`, `flightrisk/hardware/catalogue.py`, `scripts/make_param_golden.py`, `tests/golden/flight_params.json`, `.github/CODEOWNERS`, `docs/flight-parameters.md`, `docs/hardware-catalogue.md`, `safety-value-changes.md` | A1 | `flightrisk/params/legacy.py`: XL; the first phase that adds a profile updates `flightrisk/params/docs.py`; resolver: C4 (warnings), C5 (bounds, precedence, confirmations); table and golden file: B4, A3 (item 53 only: row 8, `envelope.max_altitude_cm` of generic_multirotor), D1, D2, C2, C5, C6, C7, D5, E1 to E4, each with a log entry (N-7); XCa (`catalogue.py`, `AdapterSpec` fields only); XCb (`params/docs.py`); A3 adds `params/today.py` (today's accepted ranges, generated from the golden file by `scripts/make_param_today.py`; every editor of the golden file regenerates it, and `tests/test_param_today.py` fails until they do) and the two edge start refusal templates in `params/messages.py` (a launcher layer for another unit, a safety feature switched off without confirmation), and edits `params/resolver.py` so a test-adapter fact is never classified or logged as less safe, from any layer or value (R7, R8) |
| `flightrisk/hardware/{inventory,features,frames}.py`, `flightrisk/params/report.py`, `flightrisk/mission/model.py` (types), `flightrisk/drone/rth/__init__.py` (protocols), `flightrisk/fleet_protocol/*` (every wire shape), `flightrisk/obs_comm.py`, `flightrisk/drone/controller.py` (`CameraSource`), `flightrisk/drone/registry.py`, `flightrisk/drone/backends.py`, `flightrisk/vision/profiles.py` (fields), `tests/fixtures/mobile_contract/catalogue.json` | A2 | `fleet_protocol/*`: A3 adds `describe_unit` to the edge's `validate_ground_to_edge`, and C1 adds the return commands there; no later PR changes a wire shape; A3: `controller.py` (the `max_altitude_m` annotation and default handling for item 53 only); XCa: `controller.py` (new `Capability` members and their inference only), `registry.py` (the `edge_factory` hook, `for_model`, `edge_kinds`) and `features.py` |
| `flightrisk/edge_device.py`, `flightrisk/edge.py`, `flightrisk/edge_profiles.py`, `flightrisk/edge_uplink.py`, `flightrisk/fleet_authority.py`, `flightrisk/fleet_authority_types.py`, `flightrisk/drone/{safety,obstacle_sensor,obstacle,flight_guards,temp_preflight,tello,tello_bound,fake,lane_flight,lane_executor,lane_monitor,lane_avoid}.py`, `tests/test_telemetry_field_cap.py` | A3 | C1, D5 (`det_hz`, `set_speed`), E4 (escort setpoints) |
| `flightrisk/drone/{mavlink_fleet,autopilot,mavsdk_link}.py`, new `flightrisk/drone/autopilot_fence.py` | A3 (options from params) | B2, B3, B4, C1 |
| `flightrisk/drone/rth/home.py`, `flightrisk/drone/rth/uncertainty.py` | B2 | C1 |
| `flightrisk/drone/rth/methods.py` (the autopilot fallback) | B4 | C1 (edge-guided and dead-reckoned methods) |
| `flightrisk/drone/rth/{machine,progress,dead_reckoned,energy,faults}.py` | C1 | C6, C7 (defaults only) |
| `flightrisk/drone/coordinator.py`, `flightrisk/dashboard/*` (`app.py`, `single_search.py`, `recovery.py`, `manual_control.py`, `ceiling.py`, `alerts.py`, `static/ui/*`, `templates/index.html`, `templates/ceiling_settings.html`), `flightrisk/config.py`, `flightrisk/observability.py` | A4 | D1, D2, D3, C2, C3, C5, F1, D4, D5, E1 to E4 |
| `flightrisk/drone/{fleet_plan_check,fleet_plan_model,fleet_patterns,fleet_scale,plan_edit,plan_paths,coverage,coverage_sim,geolocate,search}.py` | A4 (read through the registry) | D1, D3, D4, D5 |
| `flightrisk/mission/airspace.py` | D1 | D2, D4, E2 to E4 |
| `flightrisk/mission/lost.py` | D2 | |
| `flightrisk/mission/returns.py` | C2 | |
| `flightrisk/dashboard/roles.py`, `flightrisk/dashboard/devices.py` | C3 | F1 |
| `flightrisk/launcher/*` | A5 | C4, C5 |
| `scripts/field_fleet_ground.py` | B1 | D1 (its SITL scenarios), C4, C5 (Start confirmation), plus B2 (Start refusal display) and D4 (the matcher for field fleets) |
| `scripts/sitl_fleet.sh`, `scripts/start_sitl.sh`, `tests/integration/*` | B1 | every SITL PR adds its scenarios |
| `flightrisk/mission/{matching,generators}.py`; `speed.py` | D4; D5 | E1 to E4 |
| `flightrisk/vision/*` target embeddings; `flightrisk/mission/{targets,sightings,onfind}.py` | E1 | |
| `flightrisk/mission/viewpoints.py`; `local_search.py`; `escort.py` | E2; E3; E4 | |
| other `tests/fixtures/mobile_contract/*` | D4 (fast path, additive only) | F1, E1 (new events) |
| `docs/mobile-contract.md` | D4 (fast path, additive only) | F1, E1 (new events) |
| `docs/design/hardware-profiles/*` for the fast-path record | X0 | as today |
| `tests/test_tello_coupling.py`; `tests/tello_coupling_baseline.py` (one section per owning phase); `tests/param_adopters.py` (one section per phase) | XCP | every phase deletes its own baseline entries and adds its own adopters (shared, sectioned files) |
| `flightrisk/drone/{lane_reflight,lane_manual,lane_steps,lane_plan}.py` | XCb | C5 (XR-02, manual floor and band rows) |
| `flightrisk/params/legacy.py`, `flightrisk/drone/mavlink.py` (lines 116-118 only), `flightrisk/drone/fleet.py` | XL | none |
| `flightrisk/drone/backends.py` | A2 | A3 (the three edge registrations), A5 (A5b: one probe-hook entry per adapter) |
| `flightrisk/drone/tello_link.py`, `flightrisk/drone/tello_probe.py` (new) | A5 | none |
| `flightrisk/ground_params.py`, `flightrisk/dashboard/unit_descriptions.py`, `flightrisk/dashboard/unit_cards.py` (new), `flightrisk/recorder.py`, `flightrisk/fleet_ground_link.py` | A4 | F1 (cards events, DSH-4), C2 |
| `flightrisk/hardware/inventory.py` | A2 | A4 (A4c, `implicit_inventory` only, and only with item 49) |
| `scripts/export_mobile_contract_fixtures.py` | D4 (fast path, additive cases only) | F1 |
| `flightrisk/drone/geo.py` | D3 | none |
| `flightrisk/drone/udp_endpoints.py` | X-runtime (follow-up) | none |
| scripts not named above, `Dockerfile`, compose files, requirements files, `installer/`, docs not named in this table, tests not named in this table | X-scripts, X-install, X-docs, X-tests (follow-ups; `plan/PLAN.md` section 5.6) | the phase whose code a test or doc follows, additively |
| `mobile/` | F2 | |
| `ios/` | F3 | |

## Phase A: core profiles, catalogue and inventory (A1, A2 no behaviour change; A3: items 47, 53; A4: 1, 47-49; A5: 47)

### A1: catalogue, registry, golden file, resolver, guard, parity and tag tests
- **Tier:** opus, high. **Depends on:** the in-flight work.
- **Behaviour changes:** none. **Test plan:** unit.
- **Acceptance:** PRE-1, CAP-1, CAP-2, CAT-1 to CAT-5, CAT-7 (docs), PAR-1 to PAR-7, PAR-9, PAR-10, RES-1 to
  RES-8, RES-10, RES-11 (resolver), RES-12, CONF-3, FACT-1, FACT-2 (registry part), TAG-1, GRD-1 to GRD-6,
  R2-B4, R2-N6, R2-N8, R2-N9, SEAM-6, CMP-1, CMP-3 (creation).

### A2: inventory, features, camera roles, mission and return types, every wire shape
- **Tier:** opus, high. **Depends on:** A1.
- **Behaviour changes:** none. **Test plan:** unit (validators, privacy screen, field cap with `rth`).
- **Acceptance:** CAP-5, CAT-6, CAT-7 (fixture), CAT-8, INV-1 to INV-3, INV-6, INV-7, INV-4 (unit), FEA-1 to
  FEA-5, FEA-8 (fields), FEA-10 (static), ROLE-1 to ROLE-3, ROLE-5, SEAM-1 (types), SEAM-2, PROTO-1,
  PROTO-2 (validator), PROTO-3 (validator), PROTO-5, PROTO-7, RTH-11, MAV-19 (the `ap_link_loss` shape and
  the `straight_line_home` nack reason), CMP-1.

### A3: edge adoption
- **Tier:** opus, xhigh. **Depends on:** A2. Parallel with A4.
- **Behaviour changes:** section 27 items 47 (A3 adopters refuse the pinned combinations at their own start) and 53.
  **Test plan:** unit, fake; existing edge suites unchanged except the named item 47 and 53 assertions; PROTO-6.
- **Acceptance:** PAR-8, RES-11 (edge), CAP-4, CAP-6 (fake part), FACT-3, EDGE-1 to EDGE-7, FEA-7, FEA-8
  (edge), FEA-10, ROLE-4, SEAM-1 (wrapping), SEAM-3, SEAM-4, SEAM-7, SEAM-8, PROTO-2 (edge), PROTO-4,
  PROTO-6, CMP-1.

### A4: ground and dashboard adoption
- **Tier:** opus, high. **Depends on:** A2. Parallel with A3.
- **Behaviour changes:** section 27 items 1 (mixed environments refused at Plan), 47, 48 (the dashboard's default
  MAVLink address) and 49 (an unknown edge-reported model is no longer a `tello` unit).
- **Test plan:** unit, fake edges; plan snapshots for 1 to 4 Tellos taken at the A4 base.
- **Acceptance:** INV-4 (ground), INV-5, FEA-11 (banners), GND-1 to GND-5, GND-7, DSH-1 to DSH-3, DSH-5 to
  DSH-7, DSH-9, PROTO-3 (ground), R2-B2 (ground), R2-N7, CMP-1.
- **Follow-up from A3:** Align the launcher preflight's takeoff battery check with the edge's K-3 refusal.

### A5: launcher adoption
- **Tier:** opus, high. **Depends on:** A3 and A4 merged (round 2 B2).
- **Behaviour changes:** section 27 item 47. **Assertions:** `tests/test_launcher_commands.py:67`, `:119-121`.
- **Test plan:** unit; command snapshots for every built-in preset.
- **Acceptance:** LCH-1 to LCH-3, LCH-5 to LCH-7, INV-4 (launcher), FACT-2 (launcher part), R2-B2, CMP-1.

## Phase B: MAVLink production adapter

### B1: SITL fleet harness
- **Tier:** sonnet, high. **Depends on:** A3 (so it uses the new edge command line, nitpick 11); #213 is
  already in its base.
- **Behaviour changes:** none. **Test plan:** SITL with 2 and 8 ArduCopter and 2 PX4 vehicles.
- **Acceptance:** SITL-1 to SITL-3, CAP-6 (board-identity fingerprints; SIH and HITL fixtures classified
  real).

### B2: GPS home checks, home record, receiver uncertainty
- **Tier:** opus, high. **Depends on:** B1.
- **Behaviour changes:** items 2 to 4. **Test plan:** unit, fake; SITL (no fix, far home, home moved).
- **Files first owned:** `flightrisk/drone/rth/home.py`, `flightrisk/drone/rth/uncertainty.py` (N-7).
- **Acceptance:** MAV-1 to MAV-3.

### B3: geofence upload
- **Tier:** opus, high. **Depends on:** B2.
- **Behaviour changes:** item 5. **Test plan:** unit, fake; SITL ArduPilot and PX4.
- **Acceptance:** MAV-4.

### B4: autopilot parameter table, faults and the autopilot fallback
- **Tier:** opus, xhigh. **Depends on:** B3.
- **Behaviour changes:** items 6 to 8, 40, 41, 50, 51 and 52 (item 52 only if the B1b SITL acceptance scenario shows
  lanes cut because the drone cannot yaw in place). **Files first owned:** `flightrisk/drone/rth/methods.py`.
- **Test plan:** one unit test per table row and autopilot; one SITL scenario per row (section 13.2),
  including `SIM_GPS_DISABLE` with `FS_DR_ENABLE` 1 and 0, PX4 GPS loss with `COM_POS_FS_ACT` 0, and an
  edge control-loop stall; B4 reads the parameter metadata of the connected firmware (A8).
- **Acceptance:** MAV-5, MAV-6, MAV-7 (detection and display), MAV-8 to MAV-12, MAV-13 (place 0), MAV-14 to
  MAV-18, MAV-19 (reading).

## Phase D, first part: field airspace, lost drones and field planning (behind today's defaults)

### D1: airspace, zones, corridors, bands, homes, obstacle tops, landing order
- **Tier:** opus, high. **Depends on:** B4.
- **Behaviour changes:** items 9 to 14. **Test plan:** unit; SITL (eight-vehicle separation; a plan for a
  return-on-loss drone avoids a zone whose straight line home leaves it; shared landing area) with
  `field_fleet_ground.py`, which D1 may edit for these scenarios. D1 edits no edge file: the edge's
  straight-line check, its refusal of no-entry legs and its use of the landing order are C1's (N-7).
- **Acceptance:** AIR-1 to AIR-15 (AIR-10 payloads only), GND-6.

### D2: lost drones
- **Tier:** opus, xhigh. **Depends on:** D1.
- **Behaviour changes:** items 15 to 18 and 42. **Assertions:** as named for D2 in section 27.
- **Test plan:** unit; fake (holding field drone not declared lost, link returning, link dropping again,
  hung edge, idle controller); #212's tests.
- **Acceptance:** LOST-1 to LOST-6, LOST-8.

### D3: field missions on the dashboard
- **Tier:** opus, high. **Depends on:** D2.
- **Behaviour changes:** none for existing flows. **Test plan:** dashboard tests; SITL two-vehicle mission
  planned from the dashboard.
- **Acceptance:** TASK-10, SEAM-5.

## Phase C: return machinery, roles, then the switch in four steps

### C1: edge return state machine (today's defaults) with terminal landing
- **Tier:** opus, xhigh. **Depends on:** D3 to merge (development from B4).
- **Behaviour changes:** items 19, 20 and 43.
- **Test plan:** unit (every row), property test, fake (each command after LAND ALL; a pilot mode change
  during `rth_landing`), SITL (edge-guided return, progress monitor on horizontal legs only, a place-1
  drone holding above home, long loss inside and outside the zone with the return action set
  explicitly, escort re-task to a return-on-loss drone, edge killed at the far corner of a zone).
- **Acceptance:** CAP-3, RTH-1 to RTH-10, RTH-18, RTH-21, RTH-23, RTH-24, RTH-26, RTH-27, RTH-29, RTH-31,
  RTH-35, RTH-36, RTH-38 (both methods), MAV-7 (progress watch), MAV-13 (consuming the order), FEA-11
  (start and takeoff).

### C2: ground returns, RETURN ALL, LAND ALL cancellation, landing clearance
- **Tier:** opus, xhigh. **Depends on:** C1.
- **Behaviour changes:** items 21 to 23 (and `task.end_action`, `stranded.action` read with today's
  defaults, no change).
- **Test plan:** unit (a blocking cancellation does not delay LAND ALL), fake; SITL (RETURN ALL with four
  drones and crossing routes, LAND ALL with every sender active, timing bounds); browser check.
- **Acceptance:** RTH-12 to RTH-17, RTH-20, RTH-25, RTH-28, RTH-30, RTH-32, RTH-33, RTH-37, MAV-7 (fixed
  highest-priority route), UI-1 to UI-3.

### C3: roles, device pairing, TLS
- **Tier:** opus, xhigh (security). **Depends on:** C2.
- **Behaviour changes:** items 24 and 44. **Test plan:** unit (one test per table cell class, handler
  enumeration, raising role table, per-token land right, no imagery over cleartext), TLS connection and
  pairing tests with a matching and a mismatched certificate.
- **Acceptance:** ROLES-1 to ROLES-12.

### C4: warn-only release
- **Tier:** sonnet, high. **Depends on:** C3 and A5.
- **Behaviour changes:** item 25 (warnings only). **Test plan:** every C5 refusal case warns under C4.
- **Acceptance:** SW-1, SW-2.

### C5: bounds, precedence and confirmations
- **Tier:** opus, xhigh. **Depends on:** C4.
- **Behaviour changes:** items 26 to 31. **Assertions:** as listed for C5 in section 27.
- **Test plan:** unit, parity exceptions, dashboard start without a launcher, `field_fleet_ground.py`
  confirmations, standby takeover.
- **Acceptance:** RES-9, FEA-6, DSH-8, LCH-4, LCH-8 to LCH-10, CONF-1, CONF-2, CONF-4 (mechanism), CONF-5,
  CONF-6, FACT-2 (Start confirmation part), FACT-4, UI-5, R2-B1, R2-B3, CMP-3 (C5 exceptions), SW-4.

### C6: field return defaults
- **Tier:** opus, xhigh. **Depends on:** C5.
- **Behaviour changes:** items 32, 33 and 45. **Assertions:** as named for C6 in section 27.
- **Test plan:** SITL, the full field return scenario set with the new defaults.
- **Acceptance:** RTH-19 (field), RTH-34 (field rows), RTH-38 (default switch), SW-3, CMP-2 (field), SW-4.

### C7: indoor return defaults
- **Tier:** opus, xhigh. **Depends on:** C6. Gated by the edge laptop runs.
- **Behaviour changes:** items 34 and 46. **Assertions:** as named for C7 in section 27.
- **Test plan:** fake; hardware AC-6.6 (ten taped runs) before merge.
- **Acceptance:** RTH-19 (indoor), RTH-22, RTH-34 (indoor rows), LOST-7, CMP-2 (indoor), SW-4.

## Phase F: mobile

### F1: contract, fixtures and the phone events
- **Tier:** sonnet, high. **Depends on:** C6.
- **Behaviour changes:** item 35 (ground side). **Test plan:** fixture generator, handler tests per role.
- **Acceptance:** DSH-4, MOB-1, MOB-6 (ground), UI-6.

### F2: Android
- **Tier:** sonnet, high. **Depends on:** F1. Parallel with F3.
- **Behaviour changes:** item 35 (Android). **Test plan:** Android unit tests; phone-as-edge unchanged.
- **Acceptance:** MOB-4, MOB-6 (Android), MOB-7, MOB-9 (Android), UI-6.

### F3: iOS
- **Tier:** sonnet, high. **Depends on:** F1. Parallel with F2.
- **Behaviour changes:** item 35 (iOS). **Test plan:** `GroundProtocolTests`.
- **Acceptance:** MOB-2, MOB-3, MOB-6 (iOS), MOB-9 (iOS), UI-6.

## Phase D, second part: tasks and search speed

### D4: missions, tasks and the matcher
- **Tier:** opus, high. **Depends on:** B4. It merges on the fast path, before F1, and edits the fixtures the
  apps read additively only (N-7).
- **Behaviour changes:** item 36. **Test plan:** unit, replay from snapshots, fake; SITL mixed fleet.
- **Acceptance:** TASK-1 to TASK-8, TASK-12, FEA-9, FEA-11 (Plan), INV-8, GND-3.

### D5: search-speed rule
- **Tier:** opus, high. **Depends on:** D4, F2 and F3.
- **Behaviour changes:** item 37. **Test plan:** unit; SITL with a throttled test detector.
- **Acceptance:** TASK-9, TASK-11.

## Phase E: on-find flows (after D5, sequential)

### E1: targets, sightings, verification, on-find policy
- **Tier:** opus, high. **Depends on:** D5.
- **Behaviour changes:** item 38. **Assertions:** as named for E1 in section 27.
- **Test plan:** unit; fake two-target mission.
- **Acceptance:** FIND-1 to FIND-5, FIND-15, FIND-16, FIND-20, CONF-4 (settings), UI-4.

### E2: converge and verify
- **Tier:** opus, high. **Depends on:** E1. **Behaviour changes:** item 39.
- **Test plan:** unit; SITL three drones. **Acceptance:** FIND-6, FIND-7, FIND-17.

### E3: moving-target local search
- **Tier:** sonnet, high. **Depends on:** E2. **Behaviour changes:** item 39.
- **Test plan:** unit; SITL growing disc and reset. **Acceptance:** FIND-8, FIND-9.

### E4: escort (field only)
- **Tier:** opus, xhigh. **Depends on:** E3. **Behaviour changes:** item 39.
- **Test plan:** unit; fake; SITL with a moving test subject, a bystander, a relief hand-off, a limit and a
  loss of view. **Acceptance:** FIND-10 to FIND-14, FIND-18, FIND-19.

## Requirement coverage

Every requirement in `requirements.md` names its PR; DOC-1 and CMP-4 apply to every PR, SITL-4 to every
SITL PR, RTH-34 to C1 to C7. MOB-5 and MOB-8 are dropped (spec non-goal).
