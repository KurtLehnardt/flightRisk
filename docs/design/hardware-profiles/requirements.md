# Issue #197 requirements checklist

Every item is testable. "Test" names the kind of check that proves it: a unit test, an
integration test with the fake drone or a fake edge, a fixture test, or a documented manual check
(only where no automated check is possible). Section references point to `design.md`. The PR that
delivers each item is in brackets and matches `tasks.md`.

## Catalogue (CAT)

- [ ] CAT-1 [PR-1] `flightrisk/hardware/catalogue.py` defines `DroneModel` and `CameraSpec` with the
  fields of design section 6.1, and the built-in models `tello`, `tello_edu` and
  `generic_multirotor`, with adapter ids `tello_sdk_udp`, `mavlink` and the slot `dji_msdk`.
  Test: unit test that loads every built-in model and checks every field is set.
- [ ] CAT-2 [PR-1] Every published fact of every built-in model carries a source URL or the word
  ASSUMPTION. Test: unit test over all models.
- [ ] CAT-3 [PR-1] `flightrisk.hardware` and `flightrisk.params` import neither Flask, Ultralytics,
  torch, OpenCV, MAVSDK nor djitellopy. Test: import in a subprocess and inspect `sys.modules`.
- [ ] CAT-4 [PR-1] `DroneModel.coverage_camera()` for `tello` returns the same numbers as today's
  `coverage.TELLO` preset (`flightrisk/drone/coverage.py:52-66`). Test: unit test.
- [ ] CAT-5 [PR-1] A custom model that widens any bound, changes the scale or the adapter, or adds a
  parameter is refused with a message naming the field. A custom model that narrows bounds and sets
  defaults inside them is accepted. Test: unit tests for each case.
- [ ] CAT-6 [PR-2] A unit whose model has a slot adapter (`dji_msdk`) can be listed but cannot be
  added to a fleet; the message says that no adapter for that protocol is installed. Test: unit
  test.
- [ ] CAT-7 [PR-1, PR-2] `docs/hardware-catalogue.md` (PR-1) and
  `tests/fixtures/mobile_contract/catalogue.json` (PR-2) are generated from the catalogue, and a
  test fails when the committed files differ from the generated ones.

## Flight parameter registry (PAR)

- [ ] PAR-1 [PR-1] The registry has exactly the 83 counted rows of design section 20 (the reserved row is not added);
  ids are unique; every id passes the fleet protocol's private-key screen
  (`flightrisk/fleet_protocol/checks.py:255-266`). Test: unit test.
- [ ] PAR-2 [PR-1] For every row and every profile column, the default lies within the bound, and a
  read-only row has `lo == hi`. Test: unit test.
- [ ] PAR-3 [PR-1] Every `tello` default equals today's constant it replaces, and every
  `generic_multirotor` default equals today's `FIELD_LIMITS` or `MavlinkFleetOptions` value, for
  every row that has one (the "Where today" column). Test: table-driven unit test importing the
  current constants.
- [ ] PAR-4 [PR-1] The `tello` bounds of `move.min_cm` and `move.max_cm` equal the Tello SDK range
  constant the Tello driver uses. Test: unit test.
- [ ] PAR-5 [PR-1] Canonical and alias environment names are unique across rows; every flight
  `FLIGHTRISK_*` variable named in design section 20 maps to exactly one row; a room alias is never
  read for a field model and a field alias never for a room model. Test: unit tests.
- [ ] PAR-6 [PR-1] Every constraint of design section 10.2 holds for every profile's defaults.
  Test: unit test.
- [ ] PAR-7 [PR-1] `docs/flight-parameters.md` is generated from the registry and a test fails when
  it differs.
- [ ] PAR-8 [PR-3] `EnvelopeLimits`, `ROOM_LIMITS`, `FIELD_LIMITS` and `limits_for` are derived from
  the profile columns, and every public constant of `flightrisk/drone/safety.py` keeps its name and
  value. Test: unit test comparing values; the existing safety tests pass unchanged.

## Resolver (RES)

- [ ] RES-1 [PR-1] Precedence is profile, then params file, then inventory, then launcher, then
  environment, then command-line flag, lowest to highest. Test: one unit test per adjacent pair.
- [ ] RES-2 [PR-1] Every effective value carries `source` (`profile`, `file`, `inventory`,
  `launcher`, `env`, `cli`, `ground`, `plan`) and a `source_detail` naming the variable, the file
  section, the flag or the command. Test: unit test.
- [ ] RES-3 [PR-1] A value outside its bound is refused with a message containing the parameter id,
  the value and unit, the source, the model and the bound. No resolved value ever differs from the
  value its winning source gave. Test: unit tests, including a property test over random inputs.
- [ ] RES-4 [PR-1] An `off_value` (`0` for `avoid.sidestep_cm`, `off` for `tello.temp_refuse_c`)
  is accepted outside the bound and is marked less safe. Test: unit test.
- [ ] RES-5 [PR-1] A failed cross-value constraint is refused with its operator message. Test: one
  unit test per constraint.
- [ ] RES-6 [PR-1] A metre alias is multiplied by 100 into centimetres. Test: unit test with
  `FLIGHTRISK_FIELD_CEILING_M=30` giving `envelope.ceiling_cm = 3000` with source `env`.
- [ ] RES-7 [PR-1] In `params.json`: an unknown id is refused; a wrong type is refused; a file that
  exists but cannot be read or parsed refuses (no fallback to defaults); a missing file is an empty
  layer; `units.<id>` wins over `models.<model>`, which wins over `defaults`. Test: unit tests.
- [ ] RES-8 [PR-1] `less_safe` is true exactly when a `confirm` row's value is on the less-safe side
  of its default (by `safer`) or is its `off_value`. Test: unit test over all `confirm` rows.
- [ ] RES-9 [PR-5] `FLIGHTRISK_MANUAL_ORPHAN_LAND_S=10` is refused at dashboard start with the
  standard message instead of being raised to 30 s. Test: unit test.

## Inventory (INV)

- [ ] INV-1 [PR-2] `inventory.json` version 1 is read and written with the schema of design
  section 8.1; writes are atomic (temporary file, fsync, rename); a file that exists but cannot be
  read is refused, never replaced by an empty inventory silently. Test: unit tests.
- [ ] INV-2 [PR-2] The loader refuses keys named like `password`, `passphrase`, `secret`, `token`
  or `key`, and any value equal to the edge token. Test: unit tests.
- [ ] INV-3 [PR-2] A `unit_id` must satisfy the launcher's drone id rule
  (`flightrisk/launcher/settings.py:63-65`) and the protocol's `is_identifier`. Test: unit test.
- [ ] INV-4 [PR-2, PR-5, PR-6] Without `inventory.json`, every drone the launcher selects and every
  id in the ground's `--fleet` is a unit of model `tello` (or its reported model), and every
  built-in launcher preset starts exactly as before. Test: unit test of the implicit inventory;
  launcher command-line snapshot tests for each built-in preset.
- [ ] INV-5 [PR-4, PR-5] Plan uses only available units, and refuses a unit whose reported model
  differs from its inventory model, naming both. Test: integration test with fake edges.
- [ ] INV-6 [PR-2] Units may only reference built-in models or models in the file's `models`
  section. Test: unit test.

## Features and gating (FEA)

- [ ] FEA-1 [PR-2] The seven features of design section 9.2 exist; each `requires` returns
  availability and, when unavailable, a non-empty operator reason. Test: unit test.
- [ ] FEA-2 [PR-2] For a `tello` unit: `forward_obstacle_sensing`, `level_search` and
  `appearance_match` are available; `birdseye_search`, `thermal_detection` and `onboard_avoidance`
  are unavailable with the reasons in design section 9.2. Test: unit test.
- [ ] FEA-3 [PR-2] `birdseye_search` is available exactly when the unit has a downward camera or a
  gimbal with `pitch_min_deg <= -60`. Test: boundary unit tests at -59 and -60 degrees.
- [ ] FEA-4 [PR-2] `thermal_detection` is available exactly when a camera's sensor is `thermal` or
  `rgb_thermal`. Test: unit test.
- [ ] FEA-5 [PR-2] Switching on an unavailable feature from any source is refused with the
  feature's reason; an unavailable feature never switched on is reported off with its reason.
  Test: unit test.
- [ ] FEA-6 [PR-6, PR-5] Switching off `forward_obstacle_sensing` needs the launcher confirmation,
  and the dashboard refuses it from a phone. Test: launcher server test (409, then success with
  `confirm_params`); dashboard event test.
- [ ] FEA-7 [PR-3] An edge whose `sensing.mode` is `required` while `forward_obstacle_sensing` is
  unavailable refuses to start with the feature's reason. Test: edge start unit test.
- [ ] FEA-8 [PR-2, PR-3] Detection profiles carry `requires_sensor` and `requires_view`; a profile
  that requires a thermal sensor is refused on an RGB-only unit with the existing nack reason
  `unavailable`; `top_down` on a forward-only camera is accepted with a warning in the ack detail.
  Test: profile switcher unit tests.
- [ ] FEA-9 [PR-4] Bird's-eye search is planned only when every planned unit supports and enables
  it; otherwise Plan is refused naming the units. When it is planned, the lane spacing comes from
  the nadir swath (`coverage.nadir_swath_m`) and the detection profile is `top_down`. Test:
  coordinator unit tests.

## Seams (SEAM)

- [ ] SEAM-1 [PR-2] `CameraSource` and `FrameMeta` exist; `frames_with_meta(drone, model)` wraps a
  backend's `on_frame` and attaches the model's primary camera and the decoder's `FrameStamp`.
  Test: unit test with `FakeTelloController`.
- [ ] SEAM-2 [PR-2] `BackendSpec` has `adapter` and `models` with defaults, so a registration
  without them still works; `backends.py` maps `tello` to `tello_sdk_udp` and the Tello models,
  `mavlink` to `mavlink` and `generic_multirotor`. Test: registry unit tests.
- [ ] SEAM-3 [PR-3] The edge picks its backend from the unit's model; `--drone` must agree with the
  model's adapter, otherwise the edge refuses to start with both names. Test: edge argument tests.
- [ ] SEAM-4 [PR-3] The `ForwardCameraSensor` is built only when `forward_obstacle_sensing` is
  available and on. Test: obstacle wiring unit test.
- [ ] SEAM-5 [PR-4] `coverage.CameraModel` and `geolocate.CameraModel` instances used for planning
  and geolocation are built from the catalogue; the Tello coverage numbers are unchanged. Test:
  unit test comparing with today's preset.
- [ ] SEAM-6 [PR-1 to PR-5] `tests/test_architecture.py` passes, and no module under
  `flightrisk/hardware/` or `flightrisk/params/` imports a concrete backend. Test: extend the
  architecture test.

## Edge (EDGE)

- [ ] EDGE-1 [PR-3] The edge resolves every drone-scope parameter and feature at start and, on any
  refusal, exits with the standard message before it contacts the drone. Test: edge start unit
  tests with a fake drone that records calls.
- [ ] EDGE-2 [PR-3] `SafetyEnvelope`, `FlightGuards`, `TempLimits`, the obstacle and avoidance
  settings and `MavlinkFleetOptions` are built from resolved values. Test: unit tests.
- [ ] EDGE-3 [PR-3] Each non-default value is logged once at start with its source, and less-safe
  values at warning level. Test: log capture test.
- [ ] EDGE-4 [PR-3] The edge reads `FLIGHTRISK_LAUNCHER_PARAMS` as the launcher layer; invalid JSON
  or an unknown id refuses the start. Test: unit tests.
- [ ] EDGE-5 [PR-3] A Tello edge with no setting resolves every value to today's, and the existing
  safety, ceiling, flight-guard, obstacle, avoidance, temperature and telemetry test suites pass
  unchanged. Test: the existing suites plus a table test of resolved defaults.
- [ ] EDGE-6 [PR-3] An edge refuses to start when its model's scale or adapter does not match its
  backend. Test: unit test.
- [ ] EDGE-7 [PR-3] Every existing flight environment variable, set to a valid non-default value,
  changes the corresponding resolved value with source `env`. Test: table-driven unit test over
  all aliases.

## Protocol (PROTO)

- [ ] PROTO-1 [PR-2] `describe_unit` is in `CONFIG_COMMAND_TYPES`; the command and its `ack` have
  validators; the ack shape is that of design section 12.2; every key passes the private-key
  screen; the ack contains no list of numbers; a worst-case ack (a custom model with three cameras
  and every row) is below 16 KB. Test: protocol unit tests.
- [ ] PROTO-2 [PR-2, PR-3] `unit_rev` is an optional telemetry field validated as 12 lowercase hex
  digits; the worst-case telemetry of a Tello edge and of a MAVLink edge is at most 48 fields.
  Test: validator test (PR-2); `tests/test_telemetry_field_cap.py` extended (PR-3).
- [ ] PROTO-3 [PR-2] Telemetry with `unit_rev` validates under the current ground rules, and a
  ground never sends `describe_unit` to an edge that has not advertised `unit_rev`. Test: validator
  test; coordinator test with a fake older edge (PR-5).
- [ ] PROTO-4 [PR-3] The edge answers `describe_unit` while flying, from any connected ground, and
  answers a repeated `cmd_id` with its first answer. Test: lane executor tests.
- [ ] PROTO-5 [PR-2] `unit_rev` changes exactly when the `describe_unit` content changes. Test:
  unit test.

## Ground planner (GND)

- [ ] GND-1 [PR-4] The coordinator plans each unit against that unit's geofence, move range and
  slot bounds. Test: a regression test for finding 1: with a 450 cm edge geofence, no re-task path
  goes beyond 450 cm.
- [ ] GND-2 [PR-4] The start battery gate uses each unit's `battery.min_takeoff_pct`. Test: unit
  test with 40 % set on one edge.
- [ ] GND-3 [PR-4] The coordinator uses each unit's move range instead of `TELLO_MIN_MOVE_CM` and
  `TELLO_MAX_MOVE_CM`; a Tello plan is unchanged. Test: unit test with a field unit; snapshot of a
  default Tello plan before and after.
- [ ] GND-4 [PR-4] `fleet_patterns` zone slots come from `plan.alt_slots_cm`, `(100, 130)` by
  default. Test: unit test.
- [ ] GND-5 [PR-4] A fleet mixing `room` and `field` units is refused at Plan. Test: unit test.

## Dashboard (DSH)

- [ ] DSH-1 [PR-5] The ground sends `describe_unit` when an edge first advertises `unit_rev` and
  whenever it changes, and stores the answer per unit. Test: integration test with a fake edge.
- [ ] DSH-2 [PR-5] Plan is refused, with the message of design section 12.3, while a unit that
  advertises `unit_rev` has not answered. Test: unit test.
- [ ] DSH-3 [PR-5] `ceiling_settings.limits` keeps its five keys and holds the intersection of the
  fleet's bounds; each `drones[]` entry may carry `model` and `limits`. Test: payload unit tests;
  the existing mobile fixture tests pass.
- [ ] DSH-4 [PR-5] Socket.IO `get_inventory` answers `inventory`, `get_flight_params` answers
  `flight_params`; `inventory_set_available`, `inventory_set_feature` and `inventory_upsert_unit`
  answer `inventory_ack`, and are refused while a drone flies, a start is scheduled or this ground
  is a standby. Test: Socket.IO handler tests.
- [ ] DSH-5 [PR-5] The "Fleet hardware" and "Flight parameters" cards show, per unit, the model,
  cameras, features with reasons, and every value with unit, source, default and bound. Test:
  rendering test of the payload-to-DOM function where one exists; otherwise a documented manual
  check in a real browser.
- [ ] DSH-6 [PR-5] The dashboard's fleet-wide strip and area defaults come from the registry, and
  `--fleet-strip-cm` and `FLIGHTRISK_FLEET_STRIP_CM` still work. Test: argument tests.

## Launcher (LCH)

- [ ] LCH-1 [PR-6] The launcher edits `inventory.json` (units and custom models) and offers only
  available units as drone rows. Test: server and settings tests.
- [ ] LCH-2 [PR-6] The launcher shows the effective table for each edge it starts, with sources,
  before Start and while the edges run. Test: server test of the payload.
- [ ] LCH-3 [PR-6] A refused value is an `Issue(ERROR)` that blocks Start; an environment value that
  overrides the window is an `Issue(WARNING)`. Test: `validate` unit tests.
- [ ] LCH-4 [PR-6] Start returns HTTP 409 with `needs_confirmation: "flight_params"` and the list of
  less-safe values while any is unconfirmed; the retry succeeds only when `confirm_params` echoes
  each exact value; a preset load or window reload forgets confirmations; `confirm_sensing` is
  still accepted. Test: server tests.
- [ ] LCH-5 [PR-6] Edges and the ground get `FLIGHTRISK_LAUNCHER_PARAMS`; it is in
  `CONTROLLED_ENV`; the launcher no longer sets `FLIGHTRISK_GEOFENCE_CM`, `FLIGHTRISK_CEILING_CM`,
  `FLIGHTRISK_CEILING_MARGIN_CM` or `--alt-slot` (as decided in Q1). Test: command snapshot tests.
- [ ] LCH-6 [PR-6] The preflight battery check uses the effective takeoff minimum. Test: preflight
  unit test with 40 % set.
- [ ] LCH-7 [PR-6] A `launcher.json` written by the current launcher loads unchanged. Test: fixture
  test with a saved file.

## Mobile (MOB)

- [ ] MOB-1 [PR-5] `docs/mobile-contract.md` lists the new events and emits;
  `inventory.json`, `flight_params.json`, `inventory_ack.json` and new `client_emits.json` cases
  are generated by Python like the existing fixtures. Test: fixture generator test.
- [ ] MOB-2 [PR-8] The iOS decoders read the new fixtures and the `unknown_keys` variants
  leniently. Test: `GroundProtocolTests`.
- [ ] MOB-3 [PR-8] The iOS app shows the inventory with availability and non-safety feature
  switches and the read-only parameter table, and can emit only the allowed events. Test: unit
  tests of the command encoder against `client_emits.json`.
- [ ] MOB-4 [PR-7] The Android app has the same screens and emits, and bundles `catalogue.json`;
  a unit test fails when the bundled asset differs from the fixture. Test: Android unit tests.
- [ ] MOB-5 [PR-7] The Android Tello stack takes its move range from the bundled `tello` entry, and
  `SearchPattern` refuses or merges a segment shorter than the minimum move instead of stretching
  it. Test: Android unit tests.
- [ ] MOB-6 [PR-5, PR-7, PR-8] A phone cannot switch off a safety feature: the ground refuses it
  and `client_emits.json` has a forbidden case for it. Test: fixture and handler tests.
- [ ] MOB-7 [PR-7] Phone-as-edge messages are unchanged. Test: the existing Android edge tests pass
  unchanged.

## Guard test (GRD)

- [ ] GRD-1 [PR-1] `tests/test_param_literals.py` applies the rule of design section 14 to the
  files in scope, and every finding is in `tests/param_literal_allowlist.py` with a category and a
  reason. Test: the test itself.
- [ ] GRD-2 [PR-1] An allowlist entry that no longer matches a finding fails the test. Test: the
  test itself; each migration PR deletes the entries it migrates.
- [ ] GRD-3 [PR-1] The checker reports each planted literal of design section 14 in a temporary
  file. Test: self-tests.
- [ ] GRD-4 [PR-1] Every `deferred` entry names a follow-up. Test: the test itself.

## Documentation (DOC)

- [ ] DOC-1 [PR-1 to PR-6] `docs/flight-parameters.md` and `docs/hardware-catalogue.md` are
  generated; `docs/ceiling-and-altitude.md`, `docs/obstacle-sensing.md`,
  `docs/open-drone-support.md` and `docs/drone-interface.md` (adapter and `CameraSource`) are
  updated by the PR that changes the behaviour they describe; `docs/launcher.md` and
  `docs/fleet-setup.md` describe the inventory. Test: review.

## Compatibility (CMP)

- [ ] CMP-1 [PR-1, PR-2] PR-1 and PR-2 change no runtime behaviour: the full test suite passes
  unchanged, and no runtime code path sends `describe_unit` or reads `params.json` or
  `inventory.json`. Test: the full suite; a grep-based test for call sites.
- [ ] CMP-2 [PR-3 to PR-6] With no new file, variable or setting, a Tello flight resolves every
  value to today's (EDGE-5), and the only behaviour changes are those listed in design section 17.
  Test: the suites above; a documented manual check: one Tello hover test (`FLIGHTRISK_HOVER_TEST=1`)
  and one lane flight with defaults, before and after PR-3, compared by their logged effective
  values.
