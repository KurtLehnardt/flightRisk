# Issue #197 PR decomposition

Eight PRs. **None of them starts until all the in-flight work has merged:** the incident fixes
(`fix/tello-climb-freshness-guards`), #191, #193, #196, #198, #199, #200, #201, the floor-ratio opt-in
(`feature/obstacle-floor-ratio-optin`), the metrics follow-ups (`fix/metrics-report-followups`), and
**`fix/geofence-slot-battery-consistency`, which is a hard prerequisite of PR-1** (it fixes design
findings 1 to 3, so PR-1's golden file pins the Tello geofence at 450 cm, the zone slots at
`(100, 130)` and one battery minimum). Each PR starts from the `main` that contains them, and PR-1
first re-reads the in-flight constants it pins (design ASSUMPTION A1).

**PR-1 goes first.** PR-1 and PR-2 are pure and change no runtime behaviour. After PR-2 merges, PR-3,
PR-4 and PR-6 are developed in parallel and PR-5 starts against the interfaces PR-2 defines. The
Android and iOS PRs come last and run in parallel with each other.

```
in-flight work merged, including fix/geofence-slot-battery-consistency
        |
      PR-1  core: catalogue, registry (110 rows), golden file, resolver, params file, guard, parity
        |
      PR-2  inventory, features and camera roles, seam types, wire shapes (query set, hello key)
        |
   +----+-----------+------------------+
   |                |                  |
 PR-3 edge      PR-4 ground planner   PR-6 launcher (develop now, merge after PR-3)
   |                |
   |              PR-5 dashboard (develop after PR-2, merge after PR-4)
   |                |
   +-------+--------+
           |
     PR-7 Android  ||  PR-8 iOS   (start after PR-5 merges: they need its fixtures)
```

Merge order: PR-1, PR-2, then PR-3 and PR-4 in either order, then PR-5 (after PR-4) and PR-6 (after
PR-3), then PR-7 and PR-8.

Model tiers follow the user's rules: implementation on the tier named; every PR gets an opus critic
review at xhigh effort, capped at two rounds, then merged unless a blocking defect remains.

## Conflict avoidance

Each source file is owned by one PR. The only shared files are `tests/param_literal_allowlist.py`
(sectioned by source file; each PR edits only its own files' sections), `tests/test_param_parity.py`
(PR-1 creates it; PR-3, PR-5 and PR-6 each add only their own `PARITY_EXCEPTIONS` entries), and
`docs/`, where each PR edits only the pages listed for it.

| File or area | Owner |
| --- | --- |
| `flightrisk/params/*` (except `report.py`), `flightrisk/hardware/catalogue.py`, `tests/golden/flight_params.json`, `scripts/make_param_golden.py`, `.github/CODEOWNERS`, `docs/flight-parameters.md`, `docs/hardware-catalogue.md`, `tests/test_param_literals.py`, `tests/param_literal_allowlist.py` (creation), `tests/test_param_parity.py` (creation) | PR-1 |
| `flightrisk/hardware/{inventory,features,frames}.py`, `flightrisk/params/report.py`, `flightrisk/fleet_protocol/*` (constants, validate, fencing `EdgeHello`, new `describe.py`), `flightrisk/obs_comm.py`, `flightrisk/drone/controller.py` (new `CameraSource` role only), `flightrisk/drone/registry.py`, `flightrisk/drone/backends.py`, `flightrisk/vision/profiles.py` (new fields only), `tests/fixtures/mobile_contract/{catalogue,tello_params}.json` | PR-2 |
| `flightrisk/edge_device.py`, `flightrisk/edge.py`, `flightrisk/edge_profiles.py`, `flightrisk/edge_uplink.py`, `flightrisk/fleet_authority.py`, `flightrisk/fleet_authority_types.py`, `flightrisk/drone/{safety,obstacle_sensor,obstacle,flight_guards,temp_preflight,tello,tello_bound,fake,mavlink_fleet,autopilot,lane_flight,lane_executor,lane_monitor,lane_avoid}.py`, `tests/test_telemetry_field_cap.py` | PR-3 |
| `flightrisk/drone/{coordinator,fleet_plan_check,fleet_plan_model,fleet_patterns,fleet_scale,plan_edit,plan_paths,coverage,coverage_sim,geolocate,search}.py` | PR-4 |
| `flightrisk/dashboard/*` (including `single_search.py`, `static/ui/ui.js` and `templates/index.html`, `templates/ceiling_settings.html`), `flightrisk/config.py`, `flightrisk/observability.py`, the other `tests/fixtures/mobile_contract/*` files, `docs/mobile-contract.md` | PR-5 |
| `flightrisk/launcher/*` (including `static/launcher.html`, `static/launcher.js`) | PR-6 |
| `mobile/` | PR-7 |
| `ios/` | PR-8 |

## PR-1: catalogue, parameter registry, golden file, resolver, params file, guard and parity tests

- **Goal:** the single table of design sections 6, 7, 10 and 20, pinned by the golden file, and the
  guard, with no runtime change.
- **Tier:** opus, high effort (architecture; safety values).
- **Depends on:** all in-flight work, including the prerequisite. **Goes first.**
- **New files:** `flightrisk/hardware/__init__.py`, `flightrisk/hardware/catalogue.py` (`DroneModel`,
  `CameraSpec` with mounts, built-in models, custom-model narrowing, `coverage_camera`,
  `geolocate_camera`); `flightrisk/params/__init__.py`, `spec.py` (`ParamSpec`, units, `safer`,
  `confirm`, `safety_switch`, `edit`, `applies`), `table.py` (the 110 rows and both profile columns),
  `resolve.py` (layers, aliases with scale and scale scope, constraints, refusal messages,
  `EffectiveValue`, `less_safe` against the base column, file safety rules, `--confirm-less-safe`
  matching, intersection of bounds), `files.py` (`params.json` loader), `doc.py` (generators);
  `scripts/make_param_golden.py` (one-off, refuses to overwrite) and its output
  `tests/golden/flight_params.json`; `.github/CODEOWNERS`; `docs/flight-parameters.md`,
  `docs/hardware-catalogue.md` (generated); `tests/test_hardware_catalogue.py`,
  `tests/test_params_registry.py`, `tests/test_params_resolve.py`, `tests/test_param_golden.py`,
  `tests/test_param_parity.py`, `tests/test_param_literals.py`, `tests/param_literal_allowlist.py`
  (baseline of every current finding with exact counts, categorised; start from
  `docs/design/hardware-profiles/guard_rule_prototype.py.txt` and
  `docs/design/hardware-profiles/guard_main.txt`).
- **Modified files:** none outside tests, except one line in `tests/test_architecture.py` to cover the
  new packages (SEAM-6).
- **Out of scope:** any import of the new packages from runtime code.
- **Acceptance:** PRE-1, CAT-1 to CAT-5, CAT-7 (docs part), PAR-1 to PAR-7, PAR-9, PAR-10, RES-1 to
  RES-8, RES-10, RES-11 (resolver part), RES-12, GRD-1 to GRD-6, SEAM-6, CMP-1, CMP-3 (creation).
- **Risk:** a golden value that does not match today. Mitigation: the golden file is generated from
  the code at the PR-1 base, and every `changed` value names its section 17 item.

## PR-2: inventory, features and camera roles, seam types and wire shapes

- **Goal:** design sections 6.2, 8, 9, 11.1 (types only), 11.2 (types only) and 12.2 to 12.3
  (validation only), with no runtime change.
- **Tier:** opus, high effort (protocol; no-secrets rule; role logic).
- **Depends on:** PR-1 merged.
- **New files:** `flightrisk/hardware/inventory.py` (schema, `rev` with compare-and-swap under a lock
  file, implicit inventory, secret and safety-switch refusal), `flightrisk/hardware/features.py`
  (the seven `FeatureSpec`s with static and runtime requirements, camera roles, "when unavailable"
  behaviours), `flightrisk/hardware/frames.py` (`FrameMeta`, `with_meta`),
  `flightrisk/params/report.py` (`describe_unit` content builder and parser, `unit_rev` over the
  start-time resolution, the per-unit view the ground uses), `flightrisk/fleet_protocol/describe.py`
  (query builders and validators), the fixtures `tests/fixtures/mobile_contract/catalogue.json` and
  `tests/fixtures/mobile_contract/tello_params.json` and their generator, tests.
- **Modified files (additive only):** `flightrisk/fleet_protocol/constants.py` (`QUERY_TYPES`, not
  `CONFIG_COMMAND_TYPES`), `fleet_protocol/validate.py` (the query, its ack), `fleet_protocol/fencing.py`
  (optional `unit_rev` on `EdgeHello`, never set yet), `fleet_protocol/__init__.py` (exports),
  `flightrisk/obs_comm.py` (queries logged as aggregated traffic), `flightrisk/drone/controller.py`
  (the `CameraSource` protocol), `flightrisk/drone/registry.py` (`BackendSpec.adapter` and `.models`
  with defaults), `flightrisk/drone/backends.py` (pass them), `flightrisk/vision/profiles.py`
  (`requires_sensor`, `requires_view` with defaults that change nothing).
- **Out of scope:** sending or answering `describe_unit`; putting `unit_rev` in a real `hello`; reading
  `inventory.json` at runtime.
- **Acceptance:** CAT-6, CAT-7 (fixtures part), CAT-8, INV-1 to INV-3, INV-6, INV-7, the unit-test part
  of INV-4, FEA-1 to FEA-5, the profile-field part of FEA-8, the static part of FEA-10, ROLE-1, ROLE-2,
  ROLE-3, ROLE-5, the types part of SEAM-1, SEAM-2, PROTO-1, the validator part of PROTO-2, the
  validator part of PROTO-3, PROTO-5, PROTO-7, CMP-1.
- **Risk:** a wire key that trips the privacy screen. Mitigation: PROTO-1 runs every key through
  `checks.py`.

## PR-3: edge adoption (Tello and MAVLink adapters through the seams)

- **Goal:** the edge resolves its unit at start, builds every safety object from resolved values,
  applies static gating at start and runtime gating at takeoff, wraps its frame sources, adds camera
  metadata to detections, advertises `unit_rev` in its `hello`, and answers `describe_unit` as a query
  (design sections 6.2, 7.3, 9, 10.2, 11, 12).
- **Tier:** opus, xhigh effort (safety-critical flight path and link handling).
- **Depends on:** PR-2 merged. Parallel with PR-4 and PR-6.
- **Files:** `flightrisk/edge_device.py` (resolution at start, launcher layer with `confirmed`,
  `--confirm-less-safe`, model-driven backend choice, obstacle wiring gated by the feature with the
  MAVLink fallback unchanged, frame-source wrapping, non-default logging, query handling without
  feeding the watchdog in single-ground mode), `flightrisk/edge.py` (`cam` and `pitch_deg` in
  `detections`), `flightrisk/edge_uplink.py` and `flightrisk/fleet_authority.py`,
  `flightrisk/fleet_authority_types.py` (queries from any ground, `feed_watchdog=False`, per-connection
  cache, rate limit; `unit_rev` in the `hello`), `flightrisk/edge_profiles.py` (`select_pipeline`,
  advisory `top_down`, hard thermal gate, one pipeline per unit), `flightrisk/drone/safety.py` (limits
  and constants derived from the registry, `SafetyEnvelope.from_params`, the 450 cm default),
  `obstacle_sensor.py` and `obstacle.py` (constants become re-exports), `flight_guards.py`,
  `temp_preflight.py`, `tello.py` (capabilities and the `tello_sdk.*` reads), `tello_bound.py`,
  `fake.py`, `mavlink_fleet.py` (`MavlinkFleetOptions.from_params`), `autopilot.py`, `lane_flight.py`,
  `lane_executor.py`, `lane_monitor.py`, `lane_avoid.py` (resolved values; gimbal pitch check in the
  sense gate; runtime gating at takeoff), `tests/test_telemetry_field_cap.py` (unchanged count), new
  and updated tests; docs `ceiling-and-altitude.md`, `obstacle-sensing.md`, `open-drone-support.md`,
  `drone-interface.md`.
- **Existing assertions it may edit:** only `tests/test_safety.py:206` (design section 17).
- **Out of scope:** the ground side; the launcher; any change to the Tello landing or takeoff state
  machines.
- **Acceptance:** PAR-8, RES-11 (edge part), EDGE-1 to EDGE-7, FEA-7, the edge part of FEA-8, FEA-10,
  the start and takeoff part of FEA-11, ROLE-4, SEAM-1 (wrapping), SEAM-3, SEAM-4, SEAM-7, SEAM-8,
  PROTO-2 (edge part), PROTO-4, PROTO-6, CMP-2, CMP-3 (edge exceptions), CMP-4.
- **Risk:** a regression in a safety value or in link-loss timing. Mitigation: EDGE-5, the golden file,
  the parity test, PROTO-6, and the manual hover and lane check of CMP-2.

## PR-4: ground planner

- **Goal:** the planner uses per-unit bounds, roles and reported gates (design sections 9.2 plan
  patterns, 10.3 slot separation, 12.1 ground part; findings 5, 6, 11, 12).
- **Tier:** opus, high effort (cross-cutting planner).
- **Depends on:** PR-2 merged. Parallel with PR-3 and PR-6.
- **Files:** `flightrisk/drone/coordinator.py` (per-unit geofence, move range, slot bounds and battery
  minimum through the PR-2 per-unit view, keeping today's constructor defaults so `dashboard/app.py`
  works unchanged until PR-5; distinct-value slot separation), `fleet_plan_check.py`,
  `fleet_plan_model.py`, `fleet_patterns.py`, `fleet_scale.py` (bird's-eye pattern from the nadir
  swath), `plan_edit.py`, `plan_paths.py` (only its `deferred` allowlist entries), `coverage.py` and
  `geolocate.py` (cameras from the catalogue), `coverage_sim.py`, `search.py` (signature defaults from
  the registry), tests; docs `map-and-plan-editing.md`.
- **Existing assertions it may edit:** none.
- **Out of scope:** dashboard events and UI; sending `describe_unit`; findings 1 to 3 (done by the
  prerequisite).
- **Acceptance:** GND-1 to GND-6, FEA-9, the Plan part of FEA-11, SEAM-5, the planner part of INV-5 and
  INV-8.
- **Risk:** changing a default Tello plan. Mitigation: GND-3's snapshots for 1, 2, 3 and 4 Tellos.

## PR-5: dashboard

- **Goal:** the ground asks each edge to describe itself, serves the inventory and the parameter table
  within the phone rules, gates Plan on the latest description, shows the two cards and the banners,
  and generates the mobile fixtures for the new events (design sections 10.4, 12.1 to 12.3, 13.1,
  13.2).
- **Tier:** opus, high effort (protocol client, locking rules, UI).
- **Depends on:** PR-2 merged to start; merges after PR-4.
- **Files:** new `flightrisk/dashboard/unit_board.py` (`describe_unit` client on `hello` `unit_rev`,
  per-unit store, rev check, bound intersection, inventory server with compare-and-swap and the phone
  rules, `flight_params`), `dashboard/app.py` (wiring; planner gets per-unit views; `fleet_geofence_cm`
  from the resolver), `dashboard/single_search.py` (resolved battery minimum), `dashboard/ceiling.py`
  (intersected `limits`, per-drone `model` and `limits`, confirmation for raises), `dashboard/__main__.py`
  (strip and area defaults from the registry), `dashboard/manual_control.py` (orphan time refused, not
  clamped; step from the unit), `dashboard/safety.py`, `flightrisk/config.py` and
  `flightrisk/observability.py` (battery warn and critical from the registry),
  `dashboard/static/ui/ui.js` and `dashboard/templates/index.html` and
  `dashboard/templates/ceiling_settings.html` (the two cards, banners, ceiling confirmation), the new
  and updated `tests/fixtures/mobile_contract/*` files and generator, `docs/mobile-contract.md`.
- **Existing assertions it may edit:** only `tests/test_manual_ground.py:415`, cases `("5", 30.0)` and
  `("0", 30.0)` (design section 17).
- **Out of scope:** the launcher; the apps; findings 1 to 3.
- **Acceptance:** RES-9, DSH-1 to DSH-9, GND-7, the dashboard part of FEA-6, the banner part of FEA-11,
  the ground part of INV-4, INV-5 and INV-8, the ground part of PROTO-3, MOB-1, the ground part of
  MOB-6, CMP-3 (dashboard exceptions), CMP-4.
- **Risk:** the browser cards are not checked in a real browser. Mitigation: DSH-5's documented manual
  check.

## PR-6: launcher

- **Goal:** the inventory editor, unit-based drone rows, the effective table, the generic
  confirmations keyed by unit and id, the launcher layer with confirmed values, resolved values in the
  ceiling file, and the precedence of Q1 (design sections 10, 12.1 launcher part, 13.1 launcher part,
  16 migration notes).
- **Tier:** opus, high effort (confirmations reduce safety margins).
- **Depends on:** PR-2 merged to start; merges after PR-3, because the edge must read
  `FLIGHTRISK_LAUNCHER_PARAMS` before the launcher stops setting the individual variables.
- **Files:** `flightrisk/launcher/settings.py` (unit rows, model, feature switches, gimbal mission
  pitch, effective values through the resolver, `EffectiveObstacle` and `sensing_warning` as
  wrappers), `launcher/commands.py` (`FLIGHTRISK_LAUNCHER_PARAMS`, `CONTROLLED_ENV`, Q1),
  `launcher/server.py` (409 `flight_params`, `confirm_params` keyed by unit and id, `confirm_sensing`
  alias, model-change confirmation, inventory API with compare-and-swap), `launcher/preflight.py`
  (effective battery minimum), `launcher/ceilingfile.py` (resolved values), `launcher/static/launcher.html`,
  `launcher/static/launcher.js`, tests; docs `launcher.md`, `fleet-setup.md` (with the migration notes).
- **Existing assertions it may edit:** only `tests/test_launcher_commands.py:67` and `:119-121`, and the
  assertion the prerequisite adds to that file for the ground's `FLIGHTRISK_GEOFENCE_CM` (design
  section 17).
- **Out of scope:** the dashboard; MAVLink units started by the launcher.
- **Acceptance:** LCH-1 to LCH-10, the launcher part of FEA-6, the launcher part of INV-4, CMP-3
  (launcher exceptions), CMP-4.
- **Risk:** an inherited variable now wins over the window (Q1). Mitigation: LCH-3 warnings, LCH-4
  confirmations, and the migration notes.

## PR-7: Android

- **Goal:** the inventory screen and feature switches through the ground within the phone rules, the
  read-only parameter table, the generated Tello column driving the direct Tello stack, the
  `SearchPattern` fix, the 15 % landing, and the Kotlin literal check (design sections 13.2, 14).
- **Tier:** sonnet, high effort.
- **Depends on:** PR-5 merged (fixtures). Parallel with PR-8.
- **Files:** `mobile/contract/src/main/kotlin/com/flightrisk/contract/` (`ServerEvents.kt`,
  `SettingsModels.kt`, `ClientCommand.kt` and a new inventory model file: decoders for `inventory`,
  `flight_params`, `inventory_ack`; the emit allow-list), `mobile/app/src/main/java/com/flightrisk/app/ui/ground/`
  (new inventory and parameter cards beside `CeilingCard.kt` and `ProfilesCard.kt`),
  `mobile/app/src/main/assets/catalogue.json` and `mobile/app/src/main/assets/tello_params.json` with
  their equality tests, `mobile/app/src/main/java/com/flightrisk/app/drone/{TelloConnection,DroneManager,SearchPattern}.kt`
  and `mobile/app/src/main/java/com/flightrisk/app/config/FlightRiskConfig.kt` (values from
  `tello_params.json`), the local unit record for the standalone Tello mode, tests;
  `tests/test_kotlin_literals.py` (Python, scans the Kotlin sources); `docs/mobile-followups.md`.
- **Existing assertions it may edit:** Android tests that assert a 10 % auto-landing, if any (design
  section 17 item 2).
- **Out of scope:** phone-as-edge changes; the DJI MSDK adapter (roadmap R3).
- **Acceptance:** MOB-4, MOB-5, MOB-7, MOB-8, the Android part of MOB-6, CMP-4.

## PR-8: iOS

- **Goal:** decoders, the inventory screen within the phone rules, and the read-only parameter table
  (design section 13.2).
- **Tier:** sonnet, high effort.
- **Depends on:** PR-5 merged (fixtures). Parallel with PR-7.
- **Files:** `ios/FlightRiskGround/Sources/GroundProtocol/{Models,Events,Commands,Reducer}.swift`,
  `ios/FlightRiskGround/Tests/GroundProtocolTests/*`, `ios/FlightRisk/State/GroundViewModel.swift`, and
  new inventory and parameter views under `ios/FlightRisk/UI/Ground/` beside `DetectionProfilesView.swift`;
  `docs/ios-ground-client.md`.
- **Existing assertions it may edit:** none.
- **Out of scope:** any edge role on iOS.
- **Acceptance:** MOB-2, MOB-3, the iOS part of MOB-6.

## Requirement coverage

Every requirement in `requirements.md` names its PR in brackets (PRE-1 names the prerequisite).
CMP-2's manual Tello check runs after PR-3 merges, with defaults only, and compares the logged
effective values with a run on the `main` before PR-3.
