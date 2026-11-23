# Issue #197 requirements checklist

Every item is testable. "Test" names the kind of check that proves it: a unit test, an integration
test with the fake drone or a fake edge, a fixture test, or a documented manual check (only where no
automated check is possible). Section references point to `design.md`. The PR that delivers each item
is in brackets and matches `tasks.md`. Items marked (F*n*) answer round 1 review finding *n*.

## Prerequisite (PRE)

- [ ] PRE-1 [before PR-1] `fix/geofence-slot-battery-consistency` is merged before PR-1 starts, so
  the golden file pins the Tello geofence at 450 cm (decided Q2), the zone slots at `(100, 130)` and
  one battery minimum (F15). Test: PR-1's golden-file test fails on a base without it.

## Catalogue (CAT)

- [ ] CAT-1 [PR-1] `flightrisk/hardware/catalogue.py` defines `DroneModel` and `CameraSpec` with the
  fields of design sections 6.1 and 6.2 (including `frame_path`, `mount`, `mount_kind` and
  `pitch_measured`), and the built-in models `tello`, `tello_edu` and `generic_multirotor`, with
  adapter ids `tello_sdk_udp`, `mavlink` and the slot `dji_msdk`. Test: unit test that loads every
  built-in model and checks every field is set.
- [ ] CAT-2 [PR-1] Every published fact of every built-in model carries a source URL or the word
  ASSUMPTION. Test: unit test over all models.
- [ ] CAT-3 [PR-1] `flightrisk.hardware` and `flightrisk.params` import neither Flask, Ultralytics,
  torch, OpenCV, MAVSDK nor djitellopy. Test: import in a subprocess and inspect `sys.modules`.
- [ ] CAT-4 [PR-1] `DroneModel.coverage_camera()` for `tello` returns the same numbers as today's
  `coverage.TELLO` preset (`flightrisk/drone/coverage.py:52-66`). Test: unit test.
- [ ] CAT-5 [PR-1] A custom model that widens any bound or changes the scale, the adapter or the frame
  path, or adds a parameter, is refused with a message naming the field. A custom model that narrows
  bounds and sets defaults inside them is accepted. Test: unit tests for each case.
- [ ] CAT-6 [PR-2] A unit whose model has a slot adapter (`dji_msdk`) can be listed but cannot be
  added to a fleet; the message says that no adapter for that protocol is installed. Test: unit test.
- [ ] CAT-7 [PR-1, PR-2] `docs/hardware-catalogue.md` (PR-1), and the fixtures
  `tests/fixtures/mobile_contract/catalogue.json` and `tests/fixtures/mobile_contract/tello_params.json`
  (PR-2), are generated, and a test fails when a committed file differs from the generated one.
- [ ] CAT-8 [PR-2] A dual RGB and thermal sensor is described as two `CameraSpec`s sharing one
  `mount`, and the two always report the same role (F8). Test: unit test with the `site_hex` example
  of design section 8.1.

## Flight parameter registry and golden file (PAR)

- [ ] PAR-1 [PR-1] The registry has exactly the 110 rows of design section 20; ids are unique; every
  id passes the fleet protocol's private-key screen (`flightrisk/fleet_protocol/checks.py:255-266`).
  Test: unit test.
- [ ] PAR-2 [PR-1] For every row and every profile column, the default lies within the bound, and a
  read-only row has `lo == hi`. Test: unit test.
- [ ] PAR-3 [PR-1] (F6) `tests/golden/flight_params.json` holds, for every id and profile, the literal
  `default`, `lo`, `hi`, `safer`, `confirm`, `edit`, `off_value`, `safety_switch` and `origin`, and for
  every environment alias its `today_accepts`. `table.py` equals the golden file value by value. Every
  `origin: today` value equals the constant it replaces in the code at the PR-1 base; every
  `origin: changed` value names a design section 17 item. Test: unit tests.
- [ ] PAR-4 [PR-1] (F18) The `tello` bounds of `move.min_cm` and `move.max_cm`, and the value of every
  `tello_sdk.*` row (rows 87 to 110), equal the constants the Tello driver uses. Test: unit test that
  imports the driver constants.
- [ ] PAR-5 [PR-1] Canonical and alias environment names are unique across rows; every flight
  `FLIGHTRISK_*` variable and flag named in design section 20 maps to exactly one row; a room alias is
  never read for a field model and a field alias never for a room model. Test: unit tests.
- [ ] PAR-6 [PR-1] Every constraint of design section 10.3 holds for every profile's defaults. Test:
  unit test.
- [ ] PAR-7 [PR-1] `docs/flight-parameters.md` is generated from the registry and a test fails when it
  differs.
- [ ] PAR-8 [PR-3] `EnvelopeLimits`, `ROOM_LIMITS`, `FIELD_LIMITS` and `limits_for` are derived from the
  profile columns, and every public constant of `flightrisk/drone/safety.py` keeps its name and value,
  except `DEFAULT_GEOFENCE_CM` (section 17 item 1). Test: unit test; the existing safety tests pass
  with only the edits of section 17.
- [ ] PAR-9 [PR-1] (F6) No test or build step writes the golden file; its generator script refuses to
  overwrite an existing file. Test: a test that runs the generator against a temporary existing file
  and expects a refusal; a grep-based test that no test opens the golden file for writing.
- [ ] PAR-10 [PR-1] (F6) `.github/CODEOWNERS` names the repository owner for
  `flightrisk/params/table.py`, `tests/golden/flight_params.json` and
  `tests/param_literal_allowlist.py`. Test: a test that parses the file and checks the three paths.

## Resolver (RES)

- [ ] RES-1 [PR-1] Precedence is profile, then params file, then inventory, then launcher, then
  environment, then command-line flag, lowest to highest. Test: one unit test per adjacent pair.
- [ ] RES-2 [PR-1] Every effective value carries `source` (`profile`, `file`, `inventory`,
  `launcher`, `env`, `cli`, `ground`, `plan`) and a `source_detail`. Test: unit test.
- [ ] RES-3 [PR-1] A value outside its bound is refused with a message containing the parameter id,
  the value and unit, the source, the model and the bound. No resolved value ever differs from the
  value its winning source gave. Test: unit tests, including a property test over random inputs.
- [ ] RES-4 [PR-1] An `off_value` is accepted outside the bound and is marked less safe. Test: unit
  test.
- [ ] RES-5 [PR-1] A failed cross-value constraint is refused with its operator message. Test: one
  unit test per constraint.
- [ ] RES-6 [PR-1] A metre alias or flag is multiplied by 100 into centimetres. Test: unit test with
  `FLIGHTRISK_FIELD_CEILING_M=30` giving `envelope.ceiling_cm = 3000` with source `env`.
- [ ] RES-7 [PR-1] In `params.json`: an unknown id is refused; a wrong type is refused; a file that
  exists but cannot be read or parsed refuses (no fallback to defaults); a missing file is an empty
  layer; `units.<id>` wins over `models.<model>`, which wins over `defaults`. Test: unit tests.
- [ ] RES-8 [PR-1] (F3) `less_safe` is computed against the built-in base column by `safer`, never
  against a custom model's default; a custom default less safe than its base is marked less safe.
  `confirm: non_default` rows are marked for confirmation whenever they differ from the base default.
  Test: unit tests over all `confirm` rows and a custom model.
- [ ] RES-9 [PR-5] `FLIGHTRISK_MANUAL_ORPHAN_LAND_S=10` is refused at dashboard start with the standard
  message instead of being raised to 30 s. Test: unit test.
- [ ] RES-10 [PR-1] (F3) `params.json` and `inventory.json` refuse a safety switch stored as off
  (`sensing.mode = off`, `sensing.turn = false`, `link.require_failsafes = false`,
  `tello.temp_refuse_c = off`, `forward_obstacle_sensing = false`). Test: one unit test per switch
  and file.
- [ ] RES-11 [PR-1, PR-3] (F3) Without a launcher layer, a less-safe value whose winning source is a
  file is refused unless a `--confirm-less-safe <id>=<value>` flag names exactly that id and value; a
  confirmation for another value does not count; with a launcher layer, the values listed in its
  `confirmed` object are accepted. Test: resolver unit tests (PR-1); edge start tests (PR-3).
- [ ] RES-12 [PR-1] (F19) An empty intersection of bounds across a fleet's models is refused with the
  units named. Test: unit test with two custom models whose bounds do not overlap.

## Camera roles (ROLE)

- [ ] ROLE-1 [PR-2] (F1) Each mount has exactly one role (`level`, `nadir` or `oblique`) derived as in
  design section 6.2. Test: unit tests for fixed forward, fixed downward, and gimbals at 0, -4, -6,
  -59, -60 and -90 degrees with the default tolerance.
- [ ] ROLE-2 [PR-2] (F1) Switching on features that need two roles of one mount (for example
  `forward_obstacle_sensing` and `birdseye_search` on one gimbal) is refused with the mount named.
  Test: unit test.
- [ ] ROLE-3 [PR-2] (F1) A gimbal mount without `pitch_measured` never takes the `level` role. Test:
  unit test.
- [ ] ROLE-4 [PR-3] (F1) On a gimbal mount, every sense gate returns `unavailable` with the cause
  "camera not level", which cuts the lane, when the frame's measured pitch is missing or its absolute
  value exceeds `sensing.level_pitch_tolerance_deg`; a fixed mount is unaffected. Test: lane executor
  tests with a fake gimbal pitch reader at 0, 4, 6 degrees and None.
- [ ] ROLE-5 [PR-2] (F1) A gimbal mount without a declared `mission_pitch_deg` in its unit record has
  no role-gated feature available, with the reason "declare the gimbal's mission pitch". Test: unit
  test.

## Inventory (INV)

- [ ] INV-1 [PR-2] `inventory.json` version 1 is read and written with the schema of design section
  8.1; writes are atomic; a file that exists but cannot be read is refused, never replaced by an empty
  inventory silently. Test: unit tests.
- [ ] INV-2 [PR-2] The loader refuses keys named like `password`, `passphrase`, `secret`, `token` or
  `key`, and any value equal to the edge token. Test: unit tests.
- [ ] INV-3 [PR-2] A `unit_id` must satisfy the launcher's drone id rule
  (`flightrisk/launcher/settings.py:63-65`) and the protocol's `is_identifier`. Test: unit test.
- [ ] INV-4 [PR-2, PR-5, PR-6] Without `inventory.json`, every drone the launcher selects and every id
  in the ground's `--fleet` is a unit of model `tello` (or its reported model), and every built-in
  launcher preset starts exactly as before. Test: unit test of the implicit inventory; launcher
  command snapshot tests for each built-in preset.
- [ ] INV-5 [PR-4, PR-5] Plan uses only available units, and refuses a unit whose reported model
  differs from its inventory model, naming both. Test: integration test with fake edges.
- [ ] INV-6 [PR-2] Units may only reference built-in models or models in the file's `models` section.
  Test: unit test.
- [ ] INV-7 [PR-2] (F17) Every write carries the `rev` it read; a mismatched `rev` is refused with the
  reload message; two concurrent writers with the same `rev` result in exactly one success. Test: unit
  tests including a two-thread race.
- [ ] INV-8 [PR-4, PR-5] (F17) Plan decides each unit's role and pattern from its reported `gates`
  when it has reported, and from the inventory otherwise. Test: coordinator test where the inventory
  asks for `birdseye_search` but the reported gate says it is off.

## Features and gating (FEA)

- [ ] FEA-1 [PR-2] The seven features of design section 9.2 exist, each with a static requirement, an
  optional runtime requirement and a "when unavailable" behaviour; each returns a non-empty reason
  when unavailable. Test: unit test.
- [ ] FEA-2 [PR-2] For a `tello` unit: `forward_obstacle_sensing`, `level_search` and
  `appearance_match` are available; `birdseye_search`, `thermal_detection` and `onboard_avoidance`
  are unavailable with the reasons of design section 9.2. Test: unit test.
- [ ] FEA-3 [PR-2] `birdseye_search` is available exactly when the unit has a mount in the `nadir`
  role. Test: covered by ROLE-1's cases.
- [ ] FEA-4 [PR-2] `thermal_detection` is available exactly when a camera's sensor is `thermal` on a
  mount whose role the unit's search uses. Test: unit test.
- [ ] FEA-5 [PR-2] Switching on a statically unavailable feature from any source is refused with the
  feature's reason; an unavailable feature never switched on is reported off with its reason. Test:
  unit test.
- [ ] FEA-6 [PR-6, PR-5] Switching off `forward_obstacle_sensing` needs the launcher confirmation, and
  the dashboard refuses it from a phone. Test: launcher server test (409, then success with
  `confirm_params`); dashboard event test.
- [ ] FEA-7 [PR-3] (F2) `sensing.mode = required` is accepted when `forward_obstacle_sensing` is
  statically available, or otherwise when `onboard_avoidance` is present at takeoff; with neither at
  takeoff, the takeoff is refused. Today's MAVLink fallback is unchanged: the existing tests of
  `_build_obstacle_wiring` and `_tell_mavlink_drone` (`edge_device.py:1987-1990`, `:1995`,
  `:2060-2085`) pass without edits. Test: edge start and takeoff tests with a fake MAVLink drone whose
  `ONBOARD_AVOIDANCE` appears and disappears.
- [ ] FEA-8 [PR-2, PR-3] Detection profiles carry `requires_sensor` and `requires_view`; a profile that
  requires a thermal sensor is refused on an RGB-only unit with the nack reason `unavailable`;
  `top_down` on a level camera is accepted with a warning in the ack detail. Test: profile switcher
  unit tests.
- [ ] FEA-9 [PR-4] Bird's-eye search is planned only when every planned unit has it on and available;
  otherwise Plan is refused naming the units. When it is planned, the lane spacing comes from the
  nadir swath and the detection profile is `top_down`. Test: coordinator unit tests.
- [ ] FEA-10 [PR-2, PR-3] (F2) On a MAVLink unit, `forward_obstacle_sensing` is available only with
  `--source camera` or `url` and a level mount, and never depends on the drone declaring
  `STEPPED_SENSING` (which `MavlinkFleetDrone` does not, `mavlink_fleet.py:376-385`). Test: unit tests
  for each `--source`.
- [ ] FEA-11 [PR-3, PR-4, PR-5] (F16) Each feature's "when unavailable" behaviour of design section 9.2
  holds: refuse the start, refuse the takeoff, refuse Plan, or degrade with the banner text. Test: one
  test per feature in the PR that owns that behaviour (start and takeoff in PR-3, Plan in PR-4,
  banners in PR-5).

## Seams (SEAM)

- [ ] SEAM-1 [PR-2, PR-3] (F8) `CameraSource` and `FrameMeta` exist (PR-2); `with_meta` wraps the frame
  sources `DroneFrameSource`, `ResilientCaptureSource` and `CV2FrameSource` and the sensor feed, so a
  Tello frame and a MAVLink capture frame both carry `FrameMeta` with the decoder's `FrameStamp` when
  it exists (PR-3). Test: unit tests with `FakeTelloController` and a file capture source.
- [ ] SEAM-2 [PR-2] `BackendSpec` has `adapter` and `models` with defaults, so a registration without
  them still works; `backends.py` maps `tello` to `tello_sdk_udp` and the Tello models, `mavlink` to
  `mavlink` and `generic_multirotor`. Test: registry unit tests.
- [ ] SEAM-3 [PR-3] The edge picks its backend from the unit's model; `--drone` must agree with the
  model's adapter, otherwise the edge refuses to start with both names. Test: edge argument tests.
- [ ] SEAM-4 [PR-3] The `ForwardCameraSensor` is built only when `forward_obstacle_sensing` is
  available and on. Test: obstacle wiring unit test.
- [ ] SEAM-5 [PR-4] `coverage.CameraModel` and `geolocate.CameraModel` instances used for planning and
  geolocation are built from the catalogue; the Tello coverage numbers are unchanged. Test: unit test.
- [ ] SEAM-6 [PR-1 to PR-5] `tests/test_architecture.py` passes, and no module under
  `flightrisk/hardware/` or `flightrisk/params/` imports a concrete backend. Test: extend the
  architecture test.
- [ ] SEAM-7 [PR-3] (F8) Each `detections` message carries optional `cam` and `pitch_deg` from the
  frame it came from; the ground's detections handler accepts a message with and without them, and an
  older handler's validation accepts the extra keys (ASSUMPTION A8). Test: edge message test and
  ground handler test.
- [ ] SEAM-8 [PR-3] (F8) One detection pipeline runs per unit: `select_pipeline` returns exactly one
  profile for the unit's search camera. Test: unit test with a dual-sensor unit.

## Edge (EDGE)

- [ ] EDGE-1 [PR-3] The edge resolves every drone-scope parameter and feature at start and, on any
  refusal, exits with the standard message before it contacts the drone. Test: edge start unit tests
  with a fake drone that records calls.
- [ ] EDGE-2 [PR-3] `SafetyEnvelope`, `FlightGuards`, `TempLimits`, the obstacle and avoidance
  settings and `MavlinkFleetOptions` are built from resolved values. Test: unit tests.
- [ ] EDGE-3 [PR-3] Each non-default value is logged once at start with its source, and less-safe
  values at warning level. Test: log capture test.
- [ ] EDGE-4 [PR-3] The edge reads `FLIGHTRISK_LAUNCHER_PARAMS` as the launcher layer, including its
  `confirmed` object; invalid JSON or an unknown id refuses the start. Test: unit tests.
- [ ] EDGE-5 [PR-3] A Tello edge with no setting resolves every value to the golden file's `tello`
  defaults, and the existing safety, ceiling, flight-guard, obstacle, avoidance, temperature and
  telemetry test suites pass with only the edits of design section 17. Test: the existing suites plus
  a table test of resolved defaults.
- [ ] EDGE-6 [PR-3] An edge refuses to start when its model's scale, adapter or frame path does not
  match its backend. Test: unit test.
- [ ] EDGE-7 [PR-3] Every existing flight environment variable and flag, set to a valid non-default
  value, changes the corresponding resolved value with source `env` or `cli`. Test: table-driven unit
  test over all aliases and flags.

## Protocol (PROTO)

- [ ] PROTO-1 [PR-2] (F5) `describe_unit` is in a new `QUERY_TYPES` set and not in
  `CONFIG_COMMAND_TYPES`; the query and its `ack` have validators; the ack shape is that of design
  section 12.2, with `unit.link` holding only the link kind; every key passes the private-key screen;
  the ack contains no list of numbers; a worst-case ack (a custom model with three cameras and every
  row) is below 16 KB. Test: protocol unit tests.
- [ ] PROTO-2 [PR-2, PR-3] (F12) `unit_rev` is an optional key of the protocol v2 edge `hello`,
  validated as 12 lowercase hex digits (PR-2); the edge sends it in every `hello`, so it is re-sent on
  every reconnect, and the telemetry field count is unchanged (PR-3). Test: validator test;
  reconnect test; `tests/test_telemetry_field_cap.py` passes unchanged.
- [ ] PROTO-3 [PR-2, PR-5] A `hello` with `unit_rev` validates under the current ground rules, and a
  ground never sends `describe_unit` to an edge that has not advertised `unit_rev`. Test: validator
  test (PR-2); coordinator test with a fake older edge (PR-5).
- [ ] PROTO-4 [PR-3] (F5) The edge answers `describe_unit` from any connected ground and in any state;
  the query never feeds the link watchdog in single-ground or multi-ground mode; repeated `cmd_id`s
  are answered from a per-connection cache of 8 entries that never touches the authority's shared
  cache; more than one query per connection per 2 s gets a `nack` with reason `busy`. Test: edge link
  tests for each rule.
- [ ] PROTO-5 [PR-2] (F13) `unit_rev` hashes only the start-time resolution: it changes exactly when
  that content changes, and a later `set_ceiling` does not change it. Test: unit test.
- [ ] PROTO-6 [PR-3] (F5) A standby ground that sends `describe_unit` every 0.2 s while the authority
  falls silent does not delay the link-loss landing: the drone lands within the same tolerance as with
  no queries. Test: multi-ground integration test modelled on `tests/test_edge_uplink.py:205`.
- [ ] PROTO-7 [PR-2] (F5) `flightrisk/obs_comm.py` logs `describe_unit` and its replies as aggregated
  traffic. Test: logging unit test.

## Ground planner (GND)

- [ ] GND-1 [PR-4] The coordinator plans each unit against that unit's geofence, move range and slot
  bounds. Test: unit test with two units of different bounds.
- [ ] GND-2 [PR-4] The start battery gate uses each unit's resolved `battery.min_takeoff_pct`. Test:
  unit test with 40 % set on one edge.
- [ ] GND-3 [PR-4] (F7) The coordinator uses each unit's move range instead of `TELLO_MIN_MOVE_CM` and
  `TELLO_MAX_MOVE_CM`, and default plans for 1, 2, 3 and 4 Tellos are unchanged. Test: unit test with a
  field unit; plan snapshots for 1, 2, 3 and 4 Tellos taken on the PR-4 base and compared after.
- [ ] GND-4 [PR-4] `fleet_patterns` zone slots come from `plan.alt_slots_cm`, `(100, 130)` by
  default. Test: unit test.
- [ ] GND-5 [PR-4] A fleet mixing `room` and `field` units is refused at Plan. Test: unit test.
- [ ] GND-6 [PR-4] (F7) At room scale the slot-separation constraint applies to the distinct values of
  `plan.alt_slots_cm` only, so default three- and four-Tello fleets (100, 130, 100, 130) plan. Test:
  covered by GND-3's snapshots plus a unit test that slots (100, 110) are refused.
- [ ] GND-7 [PR-5] (F13) Plan requires each reporting unit's stored `describe_unit` reply to carry the
  latest `unit_rev` its `hello` advertised, and plans with the intersection of the reported bounds and
  the ground's own registry for the reported model; a reported value outside that intersection refuses
  Plan for the unit with both bounds. Test: unit tests.

## Dashboard (DSH)

- [ ] DSH-1 [PR-5] The ground sends `describe_unit` when a `hello` carries a `unit_rev` it has not
  described, and stores the answer per unit. Test: integration test with a fake edge.
- [ ] DSH-2 [PR-5] Plan is refused, with the message of design section 12.3, while a reporting unit's
  description is missing or stale. Test: unit test.
- [ ] DSH-3 [PR-5] `ceiling_settings.limits` keeps its five keys and holds the intersection of the
  fleet's bounds; each `drones[]` entry may carry `model` and `limits`. Test: payload unit tests; the
  existing mobile fixture tests pass.
- [ ] DSH-4 [PR-5] (F9) Socket.IO `get_inventory` answers `inventory`, `get_flight_params` answers
  `flight_params`; `inventory_add_unit` (new unit of a built-in model only), `inventory_set_available`
  and `inventory_set_feature` (non-safety features only) answer `inventory_ack`, carry `rev`, and are
  refused while a drone flies, a start is scheduled or this ground is a standby. Test: Socket.IO handler
  tests, including refusals for an existing unit id, a custom model, a safety feature in either
  direction and a stale `rev`.
- [ ] DSH-5 [PR-5] The "Fleet hardware" and "Flight parameters" cards show, per unit, the model, camera
  mounts with roles, features with reasons, and every value with unit, source, default and bound,
  including the read-only `tello_sdk.*` rows. Test: rendering test of the payload-to-DOM function
  where one exists; otherwise a documented manual check in a real browser.
- [ ] DSH-6 [PR-5] The dashboard's fleet-wide strip and area defaults come from the registry, and
  `--fleet-strip-cm` and `FLIGHTRISK_FLEET_STRIP_CM` still work within their bounds. Test: argument
  tests.
- [ ] DSH-7 [PR-5] (F15) `flightrisk/dashboard/single_search.py` uses the resolved
  `battery.min_takeoff_pct`. Test: unit test with 40 % set.
- [ ] DSH-8 [PR-5] (F4) The dashboard ceiling card asks for confirmation before saving a ceiling raise
  or a margin decrease relative to the saved file, or a value less safe than the profile default.
  Test: server-side handler test that a save without the confirmation is refused.
- [ ] DSH-9 [PR-5] (F16) A degraded feature (`appearance_match`, `claude_second_opinion`) shows its
  banner text on the cards. Test: payload unit test.

## Launcher (LCH)

- [ ] LCH-1 [PR-6] The launcher edits `inventory.json` (units, links, features, gimbal mission pitch
  and custom models) with compare-and-swap writes, and offers only available units as drone rows.
  Test: server and settings tests.
- [ ] LCH-2 [PR-6] The launcher shows the effective table for each edge it starts, with sources, before
  Start and while the edges run. Test: server test of the payload.
- [ ] LCH-3 [PR-6] A refused value is an `Issue(ERROR)` that blocks Start; an environment value that
  overrides the window is an `Issue(WARNING)`. Test: `validate` unit tests.
- [ ] LCH-4 [PR-6] (F4, F19) Start returns HTTP 409 with `needs_confirmation: "flight_params"` while any
  value of design section 10.2's list is unconfirmed, including an environment or flag value that is
  less safe than the window's value for an `edit: ui` row; the retry succeeds only when
  `confirm_params` holds an entry with the same `unit`, `id` and exact `value` for each; a preset load
  or window reload forgets confirmations; `confirm_sensing` is still accepted. Test: server tests,
  including a confirmation for one unit that does not count for another.
- [ ] LCH-5 [PR-6] Edges and the ground get `FLIGHTRISK_LAUNCHER_PARAMS` (with the `confirmed`
  object); it is in `CONTROLLED_ENV`; the launcher no longer sets `FLIGHTRISK_GEOFENCE_CM`,
  `FLIGHTRISK_CEILING_CM`, `FLIGHTRISK_CEILING_MARGIN_CM` or `--alt-slot`. Test: command snapshot
  tests.
- [ ] LCH-6 [PR-6] The preflight battery check uses the effective takeoff minimum. Test: preflight
  unit test with 40 % set.
- [ ] LCH-7 [PR-6] A `launcher.json` written by the current launcher loads unchanged. Test: fixture test
  with a saved file.
- [ ] LCH-8 [PR-6] (F4) At Start the launcher writes the resolved ceiling, margin and slots into the
  ceiling file, and the Start plan shows their sources; with an inherited `FLIGHTRISK_CEILING_CM`, the
  file holds that value. Test: `ceilingfile` unit tests.
- [ ] LCH-9 [PR-6] (F9) Changing a unit's model to one with any wider bound needs the 409 confirmation
  listing every widened bound. Test: server test.
- [ ] LCH-10 [PR-6] (F19) `docs/launcher.md` and `docs/fleet-setup.md` carry the migration notes of
  design section 16 (presets, ceiling file, shell exports that now win). Test: review.

## Mobile (MOB)

- [ ] MOB-1 [PR-5] `docs/mobile-contract.md` lists the new events and emits; `inventory.json`,
  `flight_params.json`, `inventory_ack.json` and new `client_emits.json` cases are generated by Python
  like the existing fixtures. Test: fixture generator test.
- [ ] MOB-2 [PR-8] The iOS decoders read the new fixtures and the `unknown_keys` variants leniently.
  Test: `GroundProtocolTests`.
- [ ] MOB-3 [PR-8] The iOS app shows the inventory (add a unit of a built-in model, availability,
  non-safety feature switches) and the read-only parameter table, and can emit only the allowed events.
  Test: unit tests of the command encoder against `client_emits.json`.
- [ ] MOB-4 [PR-7] (F14) The Android app has the same screens and emits, and bundles `catalogue.json`
  and `tello_params.json`; `DroneConfig`'s Tello values come from `tello_params.json`; a unit test fails
  when an asset differs from its fixture. Test: Android unit tests.
- [ ] MOB-5 [PR-7] (F14) The Android Tello stack takes its move range, keepalive, command timeout and
  landing battery from `tello_params.json`, lands at `battery.land_pct` (15 %), and `SearchPattern`
  refuses or merges a segment shorter than the minimum move instead of stretching it. Test: Android
  unit tests.
- [ ] MOB-6 [PR-5, PR-7, PR-8] (F9) A phone cannot switch a safety feature in either direction, edit an
  existing unit's model, link or features, remove a unit, or define a model: the ground refuses it and
  `client_emits.json` has a forbidden case for each. Test: fixture and handler tests.
- [ ] MOB-7 [PR-7] Phone-as-edge messages are unchanged. Test: the existing Android edge tests pass
  unchanged.
- [ ] MOB-8 [PR-7] (F11) `tests/test_kotlin_literals.py` reports a planted unit-named numeric literal in
  a temporary Kotlin file, and every finding in `mobile/app/src/main/java/com/flightrisk/app/drone/*.kt`
  and in `DroneConfig` is allowlisted or comes from `tello_params.json`. Test: the test itself.

## Guard test (GRD)

- [ ] GRD-1 [PR-1] (F11) `tests/test_param_literals.py` applies rules A to F of design section 14 to
  the files in scope (including `flightrisk/dashboard/*.py` and `flightrisk/launcher/*.py`), and every
  finding is in `tests/param_literal_allowlist.py` with a category, a reason and its exact count.
  Test: the test itself.
- [ ] GRD-2 [PR-1] An allowlist key whose count no longer matches fails the test. Test: the test
  itself; each migration PR deletes or lowers the entries it migrates.
- [ ] GRD-3 [PR-1] The checker reports each planted literal of design section 14 in a temporary file,
  one per rule. Test: self-tests.
- [ ] GRD-4 [PR-1] Every `deferred` entry names a follow-up. Test: the test itself.
- [ ] GRD-5 [PR-1] (F11) Name words are derived from the registry ids; adding a row with a new word
  makes the checker report an existing literal with that word. Test: self-test with a temporary row.
- [ ] GRD-6 [PR-1] (F11) Allowlist keys are `(file, enclosing scope, kind, callee or target, name,
  value)`; moving a literal to another function or changing its value fails the test. Test: self-tests.

## Documentation (DOC)

- [ ] DOC-1 [PR-1 to PR-6] `docs/flight-parameters.md` and `docs/hardware-catalogue.md` are generated;
  `docs/ceiling-and-altitude.md`, `docs/obstacle-sensing.md`, `docs/open-drone-support.md` and
  `docs/drone-interface.md` (adapter, `CameraSource`, frame metadata, camera roles) are updated by the
  PR that changes the behaviour they describe; `docs/launcher.md` and `docs/fleet-setup.md` describe
  the inventory and the migration notes. Test: review.

## Compatibility (CMP)

- [ ] CMP-1 [PR-1, PR-2] PR-1 and PR-2 change no runtime behaviour: the full test suite passes
  unchanged, and no runtime code path sends `describe_unit`, adds `unit_rev` to a `hello`, or reads
  `params.json` or `inventory.json`. Test: the full suite; a grep-based test for call sites.
- [ ] CMP-2 [PR-3 to PR-6] With no new file, variable or setting, a Tello flight resolves every value
  to the golden file's defaults, and the only behaviour changes are those of design section 17. Test:
  the suites above; a documented manual check: one Tello hover test (`FLIGHTRISK_HOVER_TEST=1`) and one
  lane flight with defaults, before and after PR-3, compared by their logged effective values.
- [ ] CMP-3 [PR-1, PR-3, PR-5, PR-6] (F10) `tests/test_param_parity.py` probes every environment alias
  and flag at both ends of today's range and the new bound, plus a typical and a malformed value, and
  asserts that the resolver accepts exactly what today's code accepts, except the probes listed in
  `PARITY_EXCEPTIONS`, each naming its design section 17 item. Test: the test itself.
- [ ] CMP-4 [PR-3, PR-5, PR-6, PR-7] (F10) Each PR edits only the existing assertions design section 17
  lists for it. Test: review of the diff against the list; a PR that needs another edit stops and
  splits the change out.
