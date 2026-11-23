# Issue #197 PR decomposition

Eight PRs. **None of them starts until all the in-flight work has merged:** the incident fixes
(branch `fix/tello-climb-freshness-guards`), PR #193, the sensing branch
(`feature/sensing-controls-temp-preflight`), PR #191 and PR #192. Each PR starts from the `main`
that contains them, and PR-1 first re-reads the in-flight constants it pins (design ASSUMPTION A1).

**PR-1 goes first.** PR-1 and PR-2 are pure and change no runtime behaviour. After PR-2 merges,
PR-3, PR-4 and PR-6 are developed in parallel and PR-5 starts against the interfaces PR-2 defines.
The Android and iOS PRs come last and run in parallel with each other.

```
in-flight work merged
        |
      PR-1  core: catalogue, parameter registry, resolver, params file, guard test   (pure)
        |
      PR-2  inventory, features, seam types, wire shapes                              (pure)
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

Merge order: PR-1, PR-2, then PR-3 and PR-4 in either order, then PR-5 (after PR-4) and PR-6
(after PR-3), then PR-7 and PR-8.

Model tiers follow the user's rules: implementation on the tier named; every PR gets an opus
critic review at xhigh effort, capped at two rounds, then merged unless a blocking defect remains.

## Conflict avoidance

Each source file is owned by one PR. The only shared files are
`tests/param_literal_allowlist.py`, which is sectioned by source file so each PR edits only its own
files' sections, and `docs/`, where each PR edits only the pages listed for it.

| File or area | Owner |
| --- | --- |
| `flightrisk/params/*`, `flightrisk/hardware/catalogue.py`, `docs/flight-parameters.md`, `docs/hardware-catalogue.md`, `tests/test_param_literals.py` | PR-1 |
| `flightrisk/hardware/{inventory,features,frames}.py`, `flightrisk/params/report.py`, `flightrisk/fleet_protocol/*`, `flightrisk/drone/controller.py` (new `CameraSource` role only), `flightrisk/drone/registry.py`, `flightrisk/drone/backends.py`, `flightrisk/vision/profiles.py` (new fields only), `tests/fixtures/mobile_contract/catalogue.json` | PR-2 |
| `flightrisk/edge_device.py`, `flightrisk/edge_profiles.py`, `flightrisk/drone/{safety,obstacle_sensor,obstacle,flight_guards,temp_preflight,tello,tello_bound,fake,mavlink_fleet,autopilot,lane_flight,lane_executor,lane_monitor,lane_avoid}.py`, `tests/test_telemetry_field_cap.py` | PR-3 |
| `flightrisk/drone/{coordinator,fleet_plan_check,fleet_plan_model,fleet_patterns,fleet_scale,plan_edit,plan_paths,coverage,coverage_sim,geolocate,search}.py` | PR-4 |
| `flightrisk/dashboard/*` (including `static/` and `templates/`), `flightrisk/config.py`, `flightrisk/observability.py`, the other `tests/fixtures/mobile_contract/*` files, `docs/mobile-contract.md` | PR-5 |
| `flightrisk/launcher/*` | PR-6 |
| `mobile/` | PR-7 |
| `ios/` | PR-8 |

## PR-1: catalogue, parameter registry, resolver, params file and guard test

- **Goal:** the single table of design sections 6, 7, 10 and 20, and the guard, with no runtime
  change.
- **Tier:** opus, high effort (architecture; safety values).
- **Depends on:** the in-flight work. **Goes first.**
- **New files:** `flightrisk/hardware/__init__.py`, `flightrisk/hardware/catalogue.py`
  (`DroneModel`, `CameraSpec`, built-in models, custom-model narrowing, `coverage_camera`,
  `geolocate_camera`); `flightrisk/params/__init__.py`, `spec.py` (`ParamSpec`, units, `safer`,
  `confirm`, `edit`, `applies`), `table.py` (the 83 rows and both profile columns), `resolve.py`
  (layers, aliases with scale and scale scope, constraints, refusal messages, `EffectiveValue`,
  `less_safe`), `files.py` (`params.json` loader), `doc.py` (generators);
  `docs/flight-parameters.md`, `docs/hardware-catalogue.md` (generated);
  `tests/test_hardware_catalogue.py`, `tests/test_params_registry.py`,
  `tests/test_params_resolve.py`, `tests/test_param_literals.py`,
  `tests/param_literal_allowlist.py` (baseline of every current finding, categorised; start from
  `guard_rule.py` and `guard_main.txt` in this directory).
- **Modified files:** none outside tests, except one line in `tests/test_architecture.py` to cover
  the new packages (SEAM-6).
- **Out of scope:** any import of the new packages from runtime code.
- **Acceptance:** CAT-1 to CAT-5, CAT-7 (docs part), PAR-1 to PAR-7, RES-1 to RES-8, GRD-1 to GRD-4,
  SEAM-6, CMP-1.
- **Risk:** a pinned default that does not match today. Mitigation: PAR-3 imports today's constants
  rather than copying numbers.

## PR-2: inventory, features, seam types and wire shapes

- **Goal:** design sections 8, 9, 11.1, 11.2 (types only) and 12.2 to 12.3 (validation only), with
  no runtime change.
- **Tier:** opus, high effort (protocol; no-secrets rule).
- **Depends on:** PR-1 merged.
- **New files:** `flightrisk/hardware/inventory.py` (schema, atomic store, implicit inventory,
  secret refusal), `flightrisk/hardware/features.py` (the seven `FeatureSpec`s and availability),
  `flightrisk/hardware/frames.py` (`FrameMeta`, `frames_with_meta`), `flightrisk/params/report.py`
  (`describe_unit` content builder and parser, `unit_rev`, and the per-unit view the ground uses),
  `flightrisk/fleet_protocol/describe.py` (builders and validators),
  `tests/fixtures/mobile_contract/catalogue.json` and its generator, tests.
- **Modified files (additive only):** `flightrisk/fleet_protocol/constants.py` (`DESCRIBE_UNIT` in
  `CONFIG_COMMAND_TYPES`), `fleet_protocol/validate.py` (the command, its ack, optional
  `unit_rev`), `fleet_protocol/__init__.py` (exports), `flightrisk/drone/controller.py` (the
  `CameraSource` protocol), `flightrisk/drone/registry.py` (`BackendSpec.adapter` and `.models`
  with defaults), `flightrisk/drone/backends.py` (pass them), `flightrisk/vision/profiles.py`
  (`requires_sensor`, `requires_view` with defaults that change nothing).
- **Out of scope:** sending or answering `describe_unit`; reading `inventory.json` at runtime.
- **Acceptance:** CAT-6, CAT-7 (fixture part), INV-1 to INV-3, INV-6, the unit-test part of INV-4,
  FEA-1 to FEA-5, the profile-field part of FEA-8, SEAM-1, SEAM-2, PROTO-1, the validator part of
  PROTO-2, PROTO-3 (validator part), PROTO-5, CMP-1.
- **Risk:** a wire key that trips the privacy screen. Mitigation: PROTO-1 runs every key through
  `checks.py`.

## PR-3: edge adoption (Tello and MAVLink adapters through the seams)

- **Goal:** the edge resolves its unit at start, builds every safety object from resolved values,
  applies feature gating, and reports itself (design sections 7.3, 11, 12.1 edge part, 12.2 edge
  part, 12.3).
- **Tier:** opus, xhigh effort (safety-critical flight path).
- **Depends on:** PR-2 merged. Parallel with PR-4 and PR-6.
- **Files:** `flightrisk/edge_device.py` (resolution at start, launcher layer, model-driven
  backend choice, obstacle wiring gated by the feature, non-default logging),
  `flightrisk/edge_profiles.py` (`select_pipeline`, advisory `top_down`, hard thermal gate),
  `flightrisk/drone/safety.py` (limits and constants derived from the registry,
  `SafetyEnvelope.from_params`), `obstacle_sensor.py` and `obstacle.py` (constants become
  re-exports), `flight_guards.py`, `temp_preflight.py`, `tello.py` (capabilities from the
  catalogue), `tello_bound.py` (decode lag from resolved value), `fake.py` (move range from the
  `tello` column), `mavlink_fleet.py` (`MavlinkFleetOptions.from_params`), `autopilot.py`
  (thresholds from resolved values), `lane_flight.py`, `lane_executor.py`, `lane_monitor.py`,
  `lane_avoid.py` (resolved values; `describe_unit` handling; `unit_rev` in telemetry),
  `tests/test_telemetry_field_cap.py`, new and updated tests; docs `ceiling-and-altitude.md`,
  `obstacle-sensing.md`, `open-drone-support.md`, `drone-interface.md`.
- **Out of scope:** the ground side; the launcher; any change to the Tello landing or takeoff
  state machines.
- **Acceptance:** PAR-8, EDGE-1 to EDGE-7, FEA-7, the edge part of FEA-8, SEAM-3, SEAM-4, PROTO-2
  (field cap), PROTO-4, CMP-2 (including the manual Tello check).
- **Risk:** a regression in a safety value. Mitigation: EDGE-5 and the unchanged existing suites;
  the manual hover and lane check compares logged effective values before and after.

## PR-4: ground planner

- **Goal:** the planner uses per-unit bounds and features (design sections 9.2 plan patterns, 12.1
  ground part, findings 2, 5, 6, 11, 12).
- **Tier:** opus, high effort (cross-cutting planner).
- **Depends on:** PR-2 merged. Parallel with PR-3 and PR-6.
- **Files:** `flightrisk/drone/coordinator.py` (per-unit geofence, move range, slot bounds and
  battery minimum through the PR-2 per-unit view, keeping today's constructor defaults so
  `dashboard/app.py` works unchanged until PR-5), `fleet_plan_check.py`, `fleet_plan_model.py`,
  `fleet_patterns.py`, `fleet_scale.py` (bird's-eye pattern from the nadir swath), `plan_edit.py`,
  `plan_paths.py` (only the `deferred` allowlist entries), `coverage.py` and `geolocate.py`
  (cameras from the catalogue), `coverage_sim.py`, `search.py` (signature defaults from the
  registry), tests; docs `map-and-plan-editing.md`.
- **Out of scope:** dashboard events and UI; sending `describe_unit`.
- **Acceptance:** GND-1 to GND-5, FEA-9, SEAM-5, the planner part of INV-5.
- **Risk:** changing a default Tello plan. Mitigation: GND-3's before-and-after snapshot.

## PR-5: dashboard

- **Goal:** the ground asks each edge to describe itself, serves the inventory and the parameter
  table, gates Plan, and shows the two cards; the mobile fixtures for the new events (design
  sections 12.1 to 12.3 ground part, 13.1 dashboard part, 13.2 contract part).
- **Tier:** opus, high effort (protocol client, locking rules, UI).
- **Depends on:** PR-2 merged to start; merges after PR-4.
- **Files:** new `flightrisk/dashboard/unit_board.py` (`describe_unit` client, per-unit store,
  inventory server, `flight_params`), `dashboard/app.py` (wiring; planner gets per-unit views;
  `fleet_geofence_cm` from the launcher layer and the reports), `dashboard/ceiling.py`
  (intersected `limits`, per-drone `model` and `limits`), `dashboard/__main__.py` (strip and area
  defaults from the registry), `dashboard/manual_control.py` (orphan time refused, not clamped;
  step from the unit), `dashboard/safety.py` and `flightrisk/config.py` and
  `flightrisk/observability.py` (battery warn and critical from the registry),
  `dashboard/static/ui/*` and templates (the two cards), the new and updated
  `tests/fixtures/mobile_contract/*` files and generator, `docs/mobile-contract.md`.
- **Out of scope:** the launcher; the apps.
- **Acceptance:** RES-9, DSH-1 to DSH-6, the dashboard part of FEA-6, the ground part of INV-4 and
  INV-5, PROTO-3 (older edge), MOB-1, the ground part of MOB-6.
- **Risk:** the browser cards are not checked in a real browser. Mitigation: DSH-5's documented
  manual check.

## PR-6: launcher

- **Goal:** the inventory editor, unit-based drone rows, the effective table, generic
  confirmations, the launcher layer, and the precedence decision of Q1 (design sections 10.1, 12.1
  launcher part, 13.1 launcher part).
- **Tier:** opus, high effort (confirmations reduce safety margins).
- **Depends on:** PR-2 merged to start; merges after PR-3, because the edge must read
  `FLIGHTRISK_LAUNCHER_PARAMS` before the launcher stops setting the individual variables.
- **Files:** `flightrisk/launcher/settings.py` (unit rows, model, feature switches, effective
  values through the resolver, `EffectiveObstacle` and `sensing_warning` as wrappers),
  `launcher/commands.py` (`FLIGHTRISK_LAUNCHER_PARAMS`, `CONTROLLED_ENV`, Q1),
  `launcher/server.py` (409 `flight_params`, `confirm_params`, `confirm_sensing` alias, inventory
  API), `launcher/preflight.py` (effective battery minimum), `launcher/ceilingfile.py`,
  `launcher/static/*`, tests; docs `launcher.md`, `fleet-setup.md`.
- **Out of scope:** the dashboard; MAVLink units started by the launcher.
- **Acceptance:** LCH-1 to LCH-7, the launcher part of FEA-6, the launcher part of INV-4.
- **Risk:** an inherited variable now wins over the window (Q1). Mitigation: LCH-3 warnings and
  LCH-4 confirmations.

## PR-7: Android

- **Goal:** the inventory screen and feature switches through the ground, the read-only parameter
  table, the bundled catalogue for the direct Tello stack, and the `SearchPattern` fix (design
  section 13.2).
- **Tier:** sonnet, high effort.
- **Depends on:** PR-5 merged (fixtures). Parallel with PR-8.
- **Files:** `mobile/contract/*` (models and decoders for `inventory`, `flight_params`,
  `inventory_ack`; emit allow-list), `mobile/app/.../ui/` (inventory and parameter screens),
  `mobile/app/src/main/assets/catalogue.json` and its equality test,
  `mobile/app/.../drone/{TelloConnection,DroneManager,SearchPattern}.kt` and
  `config/FlightRiskConfig.kt` (move range and safety facts from the catalogue), the local unit
  record for the standalone Tello mode, tests; `docs/mobile-followups.md`.
- **Out of scope:** phone-as-edge changes; the DJI MSDK adapter (roadmap R3).
- **Acceptance:** MOB-4, MOB-5, MOB-7, the Android part of MOB-6.

## PR-8: iOS

- **Goal:** decoders, the inventory screen with availability and non-safety feature switches, and
  the read-only parameter table (design section 13.2).
- **Tier:** sonnet, high effort.
- **Depends on:** PR-5 merged (fixtures). Parallel with PR-7.
- **Files:** `ios/FlightRiskGround/Sources/GroundProtocol/{Models,Events,Commands,Reducer}.swift`,
  the app's inventory and parameter views, `ios/FlightRiskGround/Tests/GroundProtocolTests/*`;
  `docs/ios-ground-client.md`.
- **Out of scope:** any edge role on iOS.
- **Acceptance:** MOB-2, MOB-3, the iOS part of MOB-6.

## Requirement coverage

Every requirement in `requirements.md` names its PR in brackets. CMP-2's manual Tello check runs
after PR-3 merges, with defaults only, and compares the logged effective values with a run on the
`main` before PR-3.
