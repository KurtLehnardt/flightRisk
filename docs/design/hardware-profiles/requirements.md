# Fleet architecture v1: requirements checklist

Every item is testable. "Test" names the check that proves it: a unit test, a fake-drone or simulated
autopilot test ("fake"), an ArduPilot or PX4 SITL scenario ("SITL", design section 18), a fixture
test, or a hardware run. The PR that delivers each item is in brackets and matches `tasks.md`. Items
marked (B*n*) or (N*n*) answer a round 2 critic finding of PR #202; (F*n*) a round 1 finding. Section
references point to `design.md` unless they name another file.

The spec acceptance criteria map to these items as follows: AC-1 → CMP-2, RTH-1 to RTH-20; AC-2 →
ROLES-1 to ROLES-6; AC-4.1 to AC-4.3 → CAP-1 to CAP-4, GND-5; AC-5.1 to AC-5.3 → TASK-1 to TASK-8;
AC-5.4 to AC-5.9 → FIND-1 to FIND-16, TASK-9 to TASK-11; AC-6.1 to AC-6.6 → RTH-1 to RTH-24; AC-7.1 to
AC-7.3 → MAV-1 to MAV-4, RTH-14 to RTH-18; AC-8.1 and AC-8.2 → AIR-1 to AIR-6; AC-9.1 and AC-9.2 →
UI-1 to UI-6, ROLES-1 to ROLES-6; AC-10 → PAR, RES and C4 items.

## Capability model (CAP)

- [ ] CAP-1 [A1] Every built-in model carries every spec 4.1 fact as a `CapabilityFact` with a value, a
  source and a status (`verified`, `repository`, `vendor`, `unverified`, `proposed`), and the
  dashboard and both apps can show them (AC-4.1). Test: unit test over all models and facts.
- [ ] CAP-2 [A1] A missing fact takes its most conservative value, marked `unverified`; an unknown model
  gets the intersection of the built-in bounds of its environment with the safer default of each.
  Test: unit tests.
- [ ] CAP-3 [A3] A runtime capability missing at takeoff refuses the takeoff with the capability named,
  and one lost in flight raises its spec 6.2 event (design section 9). Test: fake.
- [ ] CAP-4 [A3] Changing a fake drone's declared capabilities (return method, positioning) changes its
  return method and its separation with no rule change (AC-4.3). Test: fake.

## MAVLink production adapter (MAV)

- [ ] MAV-1 [B2] Start is refused, naming the drone and the check, for a field drone without a 3D fix,
  with fewer than `gps.min_satellites`, with receiver accuracy above `home.accuracy_cm`, with its
  autopilot home farther than `home.max_offset_cm`, with its home outside the mission fence, or too
  close to another home without sharing a landing area (AC-7.1). Test: fake; SITL (no fix, far home).
- [ ] MAV-2 [B2] The edge reports the receiver's horizontal accuracy as its positioning uncertainty in
  the `rth` telemetry object (ASSUMPTION A4 verified in SITL). Test: SITL.
- [ ] MAV-3 [B2] An autopilot home that changes in flight lands the drone in place with "no known home"
  and alerts the operator (spec 7.1, 7.5). Test: fake; SITL (set home in flight).
- [ ] MAV-4 [B3] The edge uploads the drone's fence and the mission fence before arming, reads them back,
  and refuses the takeoff on a failed upload, a mismatched read-back or a fence action other than
  return or land; fences never change in flight (ASSUMPTION A5). Test: fake; SITL ArduPilot and PX4.
- [ ] MAV-5 [B4] The autopilot's link-loss action must be return or land, and its critical-battery
  action is checked before takeoff; on a MAVLink unit the critical battery is the autopilot's action,
  not the edge's 15 %. Test: fake; SITL.
- [ ] MAV-6 [B4] A lost GPS fix or an EKF failsafe is a positioning fault: the autopilot acts, FlightRisk
  sends only operator lands, shows the reason and re-matches the drone's tasks. Test: SITL
  (`SIM_GPS_DISABLE`).
- [ ] MAV-7 [B4] `AutopilotReturn` implements the return method seam with the autopilot's
  return-to-launch at the drone's slot (the existing `land_for_reason` mode logic), including the land
  fallback when the autopilot refuses or does not enter the return. Test: fake; SITL.

## SITL harness (SITL)

- [ ] SITL-1 [B1] `scripts/sitl_fleet.sh --count N --autopilot ardupilot|px4` starts N vehicles with
  their own system ids and separated homes, and stops them; N from 2 to 10. Test: run with N = 2 and
  N = 10 on the Mac; the log is attached to B1.
- [ ] SITL-2 [B1] `tests/integration/sitl_fleet.py` fixtures set the required failsafe parameters,
  start one real edge per vehicle (replay frames, no camera) and the real ground, and tear everything
  down; the tests carry the `sitl` marker and are skipped without SITL. Test: the fixtures' own smoke
  test in SITL.
- [ ] SITL-3 [B1] B1 records the largest fleet that keeps telemetry at its rate on the Mac and on a
  Linux machine, and the PX4 multi-vehicle command that worked. Test: recorded in
  `docs/sitl-demo.md`.
- [ ] SITL-4 [B1 to E4] Every PR marked "SITL" in `tasks.md` adds its scenario to
  `tests/integration/test_sitl_scenarios.py` and attaches the run log. Test: review.

## Return to home (RTH)

- [ ] RTH-1 [C1] The return state machine (`flightrisk/drone/rth/machine.py`) implements every state of
  design section 10.2 and every transition of the event table (section 10.3), pure and clocked, with
  one unit test per default action and per fallback reason (AC-6.1). Test: unit tests.
- [ ] RTH-2 [C1] Phase C1 keeps today's actions as the resolved defaults (a Tello lands on a long link
  loss, the orphan countdown lands, every Tello error lands), so C1 changes no behaviour. Test: the
  existing suites pass unchanged.
- [ ] RTH-3 [C1] The fault classifier maps drone replies and autopilot status to fault or transient;
  unclassifiable errors are faults. Test: unit tests over the known replies (`error Motor stop`, `error
  No valid imu`, `error Not joystick`, unknown text).
- [ ] RTH-4 [C1] The energy model computes the return threshold as the larger of
  `battery.return_floor_pct` and energy to home plus `battery.landing_reserve_pct`, and predicts the
  battery at home; a prediction below the reserve returns early. Test: unit tests.
- [ ] RTH-5 [C1] The indoor uncertainty model grows with distance, turns and hover time from the
  placeholders, never shrinks in flight, and predicts the uncertainty at home. Test: unit tests.
- [ ] RTH-6 [C1] The Tello's dead-reckoned return climbs or descends into its slot first, flies direct
  through cleared areas and otherwise retraces its flown polyline in legs of 20 to 500 cm through the
  sensed-step gate, descends only over home, keeps keepalives running, and with the sensor down only
  retraces (AC-6.4). Test: fake.
- [ ] RTH-7 [C1] A blocked leg hovers, looks again, tries another cleared route, then after
  `return.blocked_wait_s` holds for a connected operator or lands with "way home blocked". Test: fake.
- [ ] RTH-8 [C1] A person cue on the way home hovers and alerts, continues only after a human confirms, a
  manual detour or a land, and lands after `return.person_cue_wait_s` without an answer. Test: fake.
- [ ] RTH-9 [C1] After a long link loss the edge returns only if the whole route, widened by its
  uncertainty, lies inside its `zone` and `corridor` at its slot; otherwise it lands with "return route
  not cleared" (AC-6.2). Test: fake; SITL (inside and outside the zone).
- [ ] RTH-10 [C1] The `rth` telemetry object carries state, reason, home, distance, uncertainty,
  reserve, threshold, countdown and "cannot be recalled", as one top-level field. Test: telemetry field
  cap test (38 of 48 at most).
- [ ] RTH-11 [C1] `return` (with `wait`), `on_complete: return` and the `zone`, `corridor`, `cleared`
  and `speed_cm_s` keys of `assign_lanes` validate, and are never sent to an edge whose reply does not
  advertise them. Test: protocol tests.
- [ ] RTH-12 [C2] RETURN ALL sends `return` to every flying drone, manual and held ones too, and shows
  each drone's result: returning, waiting, landing in place with its reason, or "not reached" with what
  it will do alone. Test: fake; SITL (four drones).
- [ ] RTH-13 [C2] Conflicting returns go lowest battery margin first, then shortest route; a drone about
  to drop below its landing reserve goes first; waiting drones hold side by side at their own slots,
  never stacked; if no order works the drone lands in place; separation is never broken (AC-7.3).
  Test: unit tests; SITL (four drones with crossing routes).
- [ ] RTH-14 [C2] One landing at a time per landing area; homes closer than `separation.landing_cm`
  share an area. Test: unit test; SITL.
- [ ] RTH-15 [C2] Per-drone Return is enabled whenever the drone flies and can be commanded, else says
  why; Land is always enabled; taking control stops a return; resuming a task after a return caused by
  battery, a fault or uncertainty is refused. Test: dashboard handler tests.
- [ ] RTH-16 [C2] The stranded countdown ends in a return when its resolved action is return; a blocked
  return then lands and the countdown never restarts (AC-6.3). Test: fake.
- [ ] RTH-17 [C2] Homes show from takeoff to landing as markers in each drone's colour; positions show
  their uncertainty; returns show route and progress; a drone landed in place shows where and why
  (AC-7.2). Test: payload tests; a documented browser check.
- [ ] RTH-18 [C2] An edge restart in flight lands with "no known home" (AC-7.2). Test: fake.
- [ ] RTH-19 [C4] The resolved defaults switch to return (task end after `retask.window_s`, long link
  loss, stranded countdown, low battery, sensor failure; transient errors retry once and return), and
  every other value keeps today's default (AC-1, spec 10.1(7)). Test: the start-path tests named in C4's
  description; SITL.
- [ ] RTH-20 [C4] LAND ALL during returns lands every drone no slower than today (AC-6.3). Test: SITL
  (timed against `main` before C4).
- [ ] RTH-21 [C1 to C4] No automatic action ever sends a takeoff; each shows its reason within 2 s on the
  dashboard and is logged (AC-6.5). Test: a test that scans every automatic action path for takeoff
  sends; event timing test.
- [ ] RTH-22 [C4] AC-6.6 (hardware, Dev/test): a Tello whose ground link is cut past the wait returns
  and lands within its uncertainty bound of its taped mark in ten runs on the ZBook. Test: hardware log
  attached to C4.
- [ ] RTH-23 [C1] Under fast uncertainty growth an indoor drone holds, then returns, then lands, in that
  order (AC-8.2). Test: fake.
- [ ] RTH-24 [C4] Critical battery lands in place at once, even mid-return. Test: fake; SITL.

## Roles (ROLES)

- [ ] ROLES-1 [C3] A connection's role is the role of the key it presents (`FLIGHTRISK_API_KEY`
  operator, `FLIGHTRISK_SUPERVISOR_KEY`, `FLIGHTRISK_OBSERVER_KEY`); with no keys only loopback
  clients connect, as operators. Test: connection tests.
- [ ] ROLES-2 [C3] Every Socket.IO event and HTTP route is in the role table; a missing entry fails a
  test that enumerates the registered handlers and routes (deny by default). Test: that test.
- [ ] ROLES-3 [C3] Observers can send only LAND ALL and per-drone Land, from a phone; supervisors cannot
  plan, start, fly, answer alerts or change settings; non-supervisor phones cannot send Return (AC-2).
  Test: one refusal test per role and event class.
- [ ] ROLES-4 [C3] A client that declares itself a phone is refused confirmations, match-alert answers,
  on-find policy changes, safety settings, home edits and less-safe acceptances, whatever its key.
  Test: handler tests.
- [ ] ROLES-5 [C3] On connect the ground sends `session` with the role and the allowed events. Test:
  handler test.
- [ ] ROLES-6 [C3] Keys never appear in logs, files the launcher writes, presets or the vault. Test:
  the launcher's existing secret checks extended to the two new keys.

## Operator UI (UI)

- [ ] UI-1 [C2] RETURN ALL is first in the top bar, always visible, never disabled, one press, no
  dialog, Shift+R; LAND ALL is always visible, never disabled, in the danger colour, Shift+L, not
  adjacent to RETURN ALL, one press, no dialog (AC-9.1). Test: DOM tests of the template; a documented
  browser check.
- [ ] UI-2 [C2] A client that cannot reach the ground says that nothing was sent and points to the local
  land keys. Test: UI test with a dropped connection.
- [ ] UI-3 [C2] Drone cards show home and distance, uncertainty ("uncalibrated" while a placeholder),
  return reserve, state, countdowns and each value's source (spec 9.5). Test: payload tests.
- [ ] UI-4 [E1] The match alert panel shows the attributed target (changeable), per-target scores,
  evidence, "k of N viewpoints match", the Claude opinion as an aid, the verification countdown, the
  unfound targets and the on-find policy, with the buttons of spec 9.3. Test: payload and handler tests.
- [ ] UI-5 [C4] The Start confirmation lists each home or mark, heading, sensing mode, return altitude
  and threshold, every non-default safety value with its source, and the on-find policy (spec 9.4).
  Test: payload test.
- [ ] UI-6 [F1, F2, F3] Dashboard and apps show the same states, homes and reasons (AC-9.2). Test:
  fixtures generated from the dashboard payloads.

## Airspace (AIR)

- [ ] AIR-1 [D1] Two drones in one band stay at least `separation.base_cm` + both uncertainties apart
  along every planned task, transit, viewpoint, escort and return route; Plan refuses a route that
  breaks it (AC-8.1). Test: unit tests; SITL ten-vehicle stress run.
- [ ] AIR-2 [D1] Field bands: the close band from `airspace.close_band_floor_cm`, the sweep band at
  least `airspace.band_gap_cm` above it. Test: unit tests.
- [ ] AIR-3 [D1] Every route widened by its drone's uncertainty stays inside every fence and outside
  every unconfirmed no-entry area (AC-8.1). Test: unit tests.
- [ ] AIR-4 [D1] A person cue, a lost drone (radius = uncertainty + `noentry.lost_margin_cm`) or a
  stranded drone creates a no-entry area that a drone enters only after a human confirms, including on
  its way home; the 20 s lost-drone timer is removed. Test: unit tests; fake.
- [ ] AIR-5 [D1] When a drone's precision degrades, its required separation grows at once; it carries
  on, holds and is re-routed or sent home, or lands past its bound. Test: unit tests.
- [ ] AIR-6 [D1] An empty intersection of bounds across a fleet's models refuses Plan, naming the units
  (round 1 F19). Test: unit test.

## Tasks and matching (TASK)

- [ ] TASK-1 [D2] No task goes to a unit lacking a required capability; a field fleet without thermal
  leaves a thermal sweep unassigned with a reason per unit, shown on the map (AC-5.1). Test: unit
  tests; SITL with a mixed simulated fleet.
- [ ] TASK-2 [D2] The hard filters of design section 12.3 are never overridden by a setting or a
  confirmation. Test: unit tests.
- [ ] TASK-3 [D2] Ranking is better positioning, battery margin, shorter transit, idle before busy, not
  already blocked there, then unit id. Test: unit tests.
- [ ] TASK-4 [D2] One task per unit; a sensing unit takes appended work only after its own tasks. Test:
  unit test.
- [ ] TASK-5 [D2] The plan explains every choice and every refusal per unit. Test: unit test of the
  explanation payload.
- [ ] TASK-6 [D2] A thermal flag that may be a person stays "needs confirmation" until a human confirms;
  a level-camera unit qualifies only if its uncertainty plus the flag's is within tolerance (AC-5.2).
  Test: unit tests.
- [ ] TASK-7 [D2] A lost unit's tasks are re-matched with their progress; its area is no-entry until a
  human confirms; re-tasking never sends a takeoff (AC-5.3). Test: fake.
- [ ] TASK-8 [D2] For an all-Tello indoor fleet the head-on generator produces today's zones and lanes
  for 1, 2, 3 and 4 Tellos. Test: the snapshots of GND-3.
- [ ] TASK-9 [D3] No search exceeds (1 − overlap) × footprint length × measured detection rate (capped
  by the airframe and the step cycle) or a lane spacing of footprint width × (1 − overlap); a lower
  detection rate slows the drone (AC-5.9). Test: unit tests; SITL with a throttled test detector.
- [ ] TASK-10 [D3] Field missions are planned on the dashboard from a GPS-anchored area, with tasks per
  capability on the map. Test: dashboard tests; SITL two-vehicle mission planned from the dashboard.
- [ ] TASK-11 [D3] The edge reports its achieved detection rate inside the detection-profile telemetry
  object. Test: telemetry test.

## On-find flows (FIND)

- [ ] FIND-1 [E1] Each target has its own references and status; every sighting is attributed to the
  best-matching target with a score for each and can be re-attributed (AC-5.4). Test: unit tests.
- [ ] FIND-2 [E1] Only a human Confirm marks a target found; the Claude opinion and viewpoint count are
  labelled aids (AC-5.5). Test: unit tests.
- [ ] FIND-3 [E1] Continue keeps searching for unfound targets; confirming the last one or End search
  runs exactly the on-find policy in force; policy changes are logged (AC-5.4). Test: unit tests;
  fake.
- [ ] FIND-4 [E1] A confirmed match no longer lands every fleet drone; the legacy single-drone path is
  unchanged. Test: recovery tests.
- [ ] FIND-5 [E1] On a sighting the finder keeps watch until its return threshold and the others
  continue; Not verified starts the local search when it is on, otherwise drones resume. Test: fake.
- [ ] FIND-6 [E2] Converge viewpoints are at least `find.approach_distance_cm` from the subject,
  pairwise separated and never stacked; when the ring cannot hold N, fewer go with a reason (AC-5.6).
  Test: unit tests; SITL with three drones.
- [ ] FIND-7 [E2] Each viewpoint reports its evidence and the panel shows "k of N viewpoints match"; the
  verification window lapsing resumes converged drones, keeps the finder watching and leaves the alert
  open. Test: unit tests.
- [ ] FIND-8 [E3] The local search radius is uncertainty + running speed × elapsed time, capped, cut to
  fences and zones minus other no-entry areas; it stops on each stop condition, and a new sighting
  resets it (AC-5.7). Test: unit tests; SITL.
- [ ] FIND-9 [E3] The local search shows the share of the disc searched and re-plans as the radius
  grows. Test: unit tests.
- [ ] FIND-10 [E4] Escort is refused indoors with the reason of spec 5.8 (AC-5.8). Test: unit test.
- [ ] FIND-11 [E4] Field escorts keep the stand-off, altitude and separation, never stack or cross a
  limit; at a limit they stop and keep watch. Test: unit tests; SITL with a moving test subject.
- [ ] FIND-12 [E4] A relief has the subject in view before an escort leaves; without relief the operator
  is alerted and the escort returns at its threshold. Test: SITL.
- [ ] FIND-13 [E4] Losing the subject for `escort.lost_from_view_s` holds the escorts, alerts and starts
  a local search; escort resumes only after a human confirms a new sighting. Test: fake; SITL.
- [ ] FIND-14 [E4] An escort setpoint older than 2 s makes the edge hold. Test: fake.
- [ ] FIND-15 [E1] Mission settings (rows 129 to 142) change in flight except the approach distance and
  the escort stand-off and altitude, and every change is logged with its author. Test: unit tests.
- [ ] FIND-16 [E1] Vision keeps one embedding set per target and scores every detection against every
  target. Test: unit tests.

## Round 2 findings (R2)

- [ ] R2-B1 [C4] A less-safe custom-model default needs the Start confirmation; a dashboard started by
  hand shows the Start confirmation and refuses Start until every less-safe value reported by any edge
  or set on the ground is confirmed with its exact value; a hand-started dashboard refuses a less-safe
  ground value from a file without `--confirm-less-safe`. Test: dashboard start tests without a
  launcher.
- [ ] R2-B2 [A4, A5] The dashboard reads `FLIGHTRISK_LAUNCHER_PARAMS` as its launcher layer, and A5
  merges after A3 and A4. Test: dashboard start test with the variable; merge order in `tasks.md`.
- [ ] R2-B3 [C4] The rehearsal preset's geofence is 450 cm; simulated units need no confirmation; the
  shared launcher fixture (`tests/test_launcher_server.py:110`) passes unchanged. Test: launcher
  tests.
- [ ] R2-B4 [A1] Every `generic_multirotor` value in the golden file equals today's effective value for
  a MAVLink unit, or carries `origin: changed` with its section 20 item. Test: the golden-file test
  imports today's MAVLink and field constants.
- [ ] R2-N6 [A1] A diff to `flightrisk/params/table.py`, the golden file or the literal allowlist fails a
  test unless `docs/design/hardware-profiles/safety-value-changes.md` gains an entry naming the value,
  old and new, the reason and the section 20 item. Test: that test.
- [ ] R2-N7 [A4] The ground retries `describe_unit` after a timeout or a `busy` nack at 2, 4, 8 and then
  every 30 s while `unit_rev` is unchanged, and Plan names the unit it waits for. Test: unit test with a
  silent fake edge.
- [ ] R2-N8 [A1] Every operator text comes from the message catalogue in one of three forms (refused,
  unavailable, landed in place), each naming the subject, the value or reason and the limit. Test: a
  test that renders every message.
- [ ] R2-N9 [A1] `MIN_START_BATTERY` stays importable as an alias of `battery.min_takeoff_pct`. Test:
  import test.


# #197 core requirements (carried from PR #202 round 1, re-tagged by phase)

### Prerequisite (PRE)

- [ ] PRE-1 [before A1] #207, #198, #208, #211, the lost-drone manual re-land fix and #205 are merged before A1 starts; #208 keeps `MIN_START_BATTERY` as an alias of the single battery minimum, so the golden file pins it (design ASSUMPTION A1, round 2 N9). Test: A1's golden-file test fails on a base without them.

### Catalogue (CAT)

- [ ] CAT-1 [A1] `flightrisk/hardware/catalogue.py` defines `DroneModel` and `CameraSpec` with the
  fields of `hardware-profiles-detail.md` sections 6.1 and 6.2 (including `frame_path`, `mount`, `mount_kind` and
  `pitch_measured`), and the built-in models `tello`, `tello_edu` and `generic_multirotor`, with
  adapter ids `tello_sdk_udp`, `mavlink` and the slot `dji_msdk`. Test: unit test that loads every
  built-in model and checks every field is set.
- [ ] CAT-2 [A1] Every published fact of every built-in model carries a source URL or the word
  ASSUMPTION. Test: unit test over all models.
- [ ] CAT-3 [A1] `flightrisk.hardware` and `flightrisk.params` import neither Flask, Ultralytics,
  torch, OpenCV, MAVSDK nor djitellopy. Test: import in a subprocess and inspect `sys.modules`.
- [ ] CAT-4 [A1] `DroneModel.coverage_camera()` for `tello` returns the same numbers as today's
  `coverage.TELLO` preset (`flightrisk/drone/coverage.py:52-66`). Test: unit test.
- [ ] CAT-5 [A1] A custom model that widens any bound or changes the scale, the adapter or the frame
  path, or adds a parameter, is refused with a message naming the field. A custom model that narrows
  bounds and sets defaults inside them is accepted. Test: unit tests for each case.
- [ ] CAT-6 [A2] A unit whose model has a slot adapter (`dji_msdk`) can be listed but cannot be
  added to a fleet; the message says that no adapter for that protocol is installed. Test: unit test.
- [ ] CAT-7 [A1, A2] `docs/hardware-catalogue.md` (A1), and the fixtures
  `tests/fixtures/mobile_contract/catalogue.json` and `tests/fixtures/mobile_contract/tello_params.json`
  (A2), are generated, and a test fails when a committed file differs from the generated one.
- [ ] CAT-8 [A2] A dual RGB and thermal sensor is described as two `CameraSpec`s sharing one
  `mount`, and the two always report the same role (F8). Test: unit test with the `site_hex` example
  of `hardware-profiles-detail.md` section 8.1.

### Flight parameter registry and golden file (PAR)

- [ ] PAR-1 [A1] The registry has exactly the 140 rows of `parameters.md`; ids are unique; every
  id passes the fleet protocol's private-key screen (`flightrisk/fleet_protocol/checks.py:255-266`).
  Test: unit test.
- [ ] PAR-2 [A1] For every row and every profile column, the default lies within the bound, and a
  read-only row has `lo == hi`. Test: unit test.
- [ ] PAR-3 [A1] (F6) `tests/golden/flight_params.json` holds, for every id and profile, the literal
  `default`, `lo`, `hi`, `safer`, `confirm`, `edit`, `off_value`, `safety_switch` and `origin`, and for
  every environment alias its `today_accepts`. `table.py` equals the golden file value by value. Every
  `origin: today` value equals the constant it replaces in the code at the A1 base; every
  `origin: changed` value names a design section 20 item. Test: unit tests.
- [ ] PAR-4 [A1] (F18) The `tello` bounds of `move.min_cm` and `move.max_cm`, and the value of every
  `tello_sdk.*` row (rows 87 to 110), equal the constants the Tello driver uses. Test: unit test that
  imports the driver constants.
- [ ] PAR-5 [A1] Canonical and alias environment names are unique across rows; every flight
  `FLIGHTRISK_*` variable and flag named in design section 20 maps to exactly one row; a room alias is
  never read for a field model and a field alias never for a room model. Test: unit tests.
- [ ] PAR-6 [A1] Every constraint of `hardware-profiles-detail.md` section 10.3 holds for every profile's defaults. Test:
  unit test.
- [ ] PAR-7 [A1] `docs/flight-parameters.md` is generated from the registry and a test fails when it
  differs.
- [ ] PAR-8 [A3] `EnvelopeLimits`, `ROOM_LIMITS`, `FIELD_LIMITS` and `limits_for` are derived from the profile columns, and every public constant of `flightrisk/drone/safety.py` keeps its name and value in phase A (the 450 cm geofence default arrives in C4). Test: unit test; the existing safety tests pass unchanged.
- [ ] PAR-9 [A1] (F6) No test or build step writes the golden file; its generator script refuses to
  overwrite an existing file. Test: a test that runs the generator against a temporary existing file
  and expects a refusal; a grep-based test that no test opens the golden file for writing.
- [ ] PAR-10 [A1] (F6) `.github/CODEOWNERS` names the repository owner for
  `flightrisk/params/table.py`, `tests/golden/flight_params.json` and
  `tests/param_literal_allowlist.py`. Test: a test that parses the file and checks the three paths.

### Resolver (RES)

- [ ] RES-1 [A1] Precedence is profile, then params file, then inventory, then launcher, then
  environment, then command-line flag, lowest to highest. Test: one unit test per adjacent pair.
- [ ] RES-2 [A1] Every effective value carries `source` (`profile`, `file`, `inventory`,
  `launcher`, `env`, `cli`, `ground`, `plan`) and a `source_detail`. Test: unit test.
- [ ] RES-3 [A1] A value outside its bound is refused with a message containing the parameter id,
  the value and unit, the source, the model and the bound. No resolved value ever differs from the
  value its winning source gave. Test: unit tests, including a property test over random inputs.
- [ ] RES-4 [A1] An `off_value` is accepted outside the bound and is marked less safe. Test: unit
  test.
- [ ] RES-5 [A1] A failed cross-value constraint is refused with its operator message. Test: one
  unit test per constraint.
- [ ] RES-6 [A1] A metre alias or flag is multiplied by 100 into centimetres. Test: unit test with
  `FLIGHTRISK_FIELD_CEILING_M=30` giving `envelope.ceiling_cm = 3000` with source `env`.
- [ ] RES-7 [A1] In `params.json`: an unknown id is refused; a wrong type is refused; a file that
  exists but cannot be read or parsed refuses (no fallback to defaults); a missing file is an empty
  layer; `units.<id>` wins over `models.<model>`, which wins over `defaults`. Test: unit tests.
- [ ] RES-8 [A1] (F3) `less_safe` is computed against the built-in base column by `safer`, never
  against a custom model's default; a custom default less safe than its base is marked less safe.
  `confirm: non_default` rows are marked for confirmation whenever they differ from the base default.
  Test: unit tests over all `confirm` rows and a custom model.
- [ ] RES-9 [C4] `FLIGHTRISK_MANUAL_ORPHAN_LAND_S=10` is refused at dashboard start with the standard
  message instead of being raised to 30 s. Test: unit test.
- [ ] RES-10 [A1] (F3) `params.json` and `inventory.json` refuse a safety switch stored as off
  (`sensing.mode = off`, `sensing.turn = false`, `link.require_failsafes = false`,
  `tello.temp_refuse_c = off`, `forward_obstacle_sensing = false`). Test: one unit test per switch
  and file.
- [ ] RES-11 [A1, A3] (F3) Without a launcher layer, a less-safe value whose winning source is a
  file is refused unless a `--confirm-less-safe <id>=<value>` flag names exactly that id and value; a
  confirmation for another value does not count; with a launcher layer, the values listed in its
  `confirmed` object are accepted. Test: resolver unit tests (A1); edge start tests (A3).
- [ ] RES-12 [A1] (F19) An empty intersection of bounds across a fleet's models is refused with the
  units named. Test: unit test with two custom models whose bounds do not overlap.

### Camera roles (ROLE)

- [ ] ROLE-1 [A2] (F1) Each mount has exactly one role (`level`, `nadir` or `oblique`) derived as in
  `hardware-profiles-detail.md` section 6.2. Test: unit tests for fixed forward, fixed downward, and gimbals at 0, -4, -6,
  -59, -60 and -90 degrees with the default tolerance.
- [ ] ROLE-2 [A2] (F1) Switching on features that need two roles of one mount (for example
  `forward_obstacle_sensing` and `birdseye_search` on one gimbal) is refused with the mount named.
  Test: unit test.
- [ ] ROLE-3 [A2] (F1) A gimbal mount without `pitch_measured` never takes the `level` role. Test:
  unit test.
- [ ] ROLE-4 [A3] (F1) On a gimbal mount, every sense gate returns `unavailable` with the cause
  "camera not level", which cuts the lane, when the frame's measured pitch is missing or its absolute
  value exceeds `sensing.level_pitch_tolerance_deg`; a fixed mount is unaffected. Test: lane executor
  tests with a fake gimbal pitch reader at 0, 4, 6 degrees and None.
- [ ] ROLE-5 [A2] (F1) A gimbal mount without a declared `mission_pitch_deg` in its unit record has
  no role-gated feature available, with the reason "declare the gimbal's mission pitch". Test: unit
  test.

### Inventory (INV)

- [ ] INV-1 [A2] `inventory.json` version 1 is read and written with the schema of design section
  8.1; writes are atomic; a file that exists but cannot be read is refused, never replaced by an empty
  inventory silently. Test: unit tests.
- [ ] INV-2 [A2] The loader refuses keys named like `password`, `passphrase`, `secret`, `token` or
  `key`, and any value equal to the edge token. Test: unit tests.
- [ ] INV-3 [A2] A `unit_id` must satisfy the launcher's drone id rule
  (`flightrisk/launcher/settings.py:63-65`) and the protocol's `is_identifier`. Test: unit test.
- [ ] INV-4 [A2, A4, A5] Without `inventory.json`, every drone the launcher selects and every id
  in the ground's `--fleet` is a unit of model `tello` (or its reported model), and every built-in
  launcher preset starts exactly as before. Test: unit test of the implicit inventory; launcher
  command snapshot tests for each built-in preset.
- [ ] INV-5 [A4] Plan uses only available units, and refuses a unit whose reported model
  differs from its inventory model, naming both. Test: integration test with fake edges.
- [ ] INV-6 [A2] Units may only reference built-in models or models in the file's `models` section.
  Test: unit test.
- [ ] INV-7 [A2] (F17) Every write carries the `rev` it read; a mismatched `rev` is refused with the
  reload message; two concurrent writers with the same `rev` result in exactly one success. Test: unit
  tests including a two-thread race.
- [ ] INV-8 [D2] (F17) Plan decides each unit's role and pattern from its reported `gates`
  when it has reported, and from the inventory otherwise. Test: coordinator test where the inventory
  asks for `birdseye_search` but the reported gate says it is off.

### Features and gating (FEA)

- [ ] FEA-1 [A2] The seven features of `hardware-profiles-detail.md` section 9.2 exist, each with a static requirement, an
  optional runtime requirement and a "when unavailable" behaviour; each returns a non-empty reason
  when unavailable. Test: unit test.
- [ ] FEA-2 [A2] For a `tello` unit: `forward_obstacle_sensing`, `level_search` and
  `appearance_match` are available; `birdseye_search`, `thermal_detection` and `onboard_avoidance`
  are unavailable with the reasons of `hardware-profiles-detail.md` section 9.2. Test: unit test.
- [ ] FEA-3 [A2] `birdseye_search` is available exactly when the unit has a mount in the `nadir`
  role. Test: covered by ROLE-1's cases.
- [ ] FEA-4 [A2] `thermal_detection` is available exactly when a camera's sensor is `thermal` on a
  mount whose role the unit's search uses. Test: unit test.
- [ ] FEA-5 [A2] Switching on a statically unavailable feature from any source is refused with the
  feature's reason; an unavailable feature never switched on is reported off with its reason. Test:
  unit test.
- [ ] FEA-6 [C4] Switching off `forward_obstacle_sensing` needs the launcher confirmation, and
  the dashboard refuses it from a phone. Test: launcher server test (409, then success with
  `confirm_params`); dashboard event test.
- [ ] FEA-7 [A3] (F2) `sensing.mode = required` is accepted when `forward_obstacle_sensing` is
  statically available, or otherwise when `onboard_avoidance` is present at takeoff; with neither at
  takeoff, the takeoff is refused. Today's MAVLink fallback is unchanged: the existing tests of
  `_build_obstacle_wiring` and `_tell_mavlink_drone` (`edge_device.py:1987-1990`, `:1995`,
  `:2060-2085`) pass without edits. Test: edge start and takeoff tests with a fake MAVLink drone whose
  `ONBOARD_AVOIDANCE` appears and disappears.
- [ ] FEA-8 [A2, A3] Detection profiles carry `requires_sensor` and `requires_view`; a profile that
  requires a thermal sensor is refused on an RGB-only unit with the nack reason `unavailable`;
  `top_down` on a level camera is accepted with a warning in the ack detail. Test: profile switcher
  unit tests.
- [ ] FEA-9 [D2] A bird's-eye sweep task is matched only to units with `birdseye_search` on and available, in the sweep band; its lane spacing comes from the nadir swath and its detection profile is `top_down`; head-on tasks run beside it in the close band. Test: matcher and coordinator unit tests.
- [ ] FEA-10 [A2, A3] (F2) On a MAVLink unit, `forward_obstacle_sensing` is available only with
  `--source camera` or `url` and a level mount, and never depends on the drone declaring
  `STEPPED_SENSING` (which `MavlinkFleetDrone` does not, `mavlink_fleet.py:376-385`). Test: unit tests
  for each `--source`.
- [ ] FEA-11 [A3, A4, D2] (F16) Each feature's "when unavailable" behaviour of `hardware-profiles-detail.md` section 9.2
  holds: refuse the start, refuse the takeoff, refuse Plan, or degrade with the banner text. Test: one
  test per feature in the PR that owns that behaviour (start and takeoff in A3, Plan in D2,
  banners in A4).

### Seams (SEAM)

- [ ] SEAM-1 [A2, A3] (F8) `CameraSource` and `FrameMeta` exist (A2); `with_meta` wraps the frame
  sources `DroneFrameSource`, `ResilientCaptureSource` and `CV2FrameSource` and the sensor feed, so a
  Tello frame and a MAVLink capture frame both carry `FrameMeta` with the decoder's `FrameStamp` when
  it exists (A3). Test: unit tests with `FakeTelloController` and a file capture source.
- [ ] SEAM-2 [A2] `BackendSpec` has `adapter` and `models` with defaults, so a registration without
  them still works; `backends.py` maps `tello` to `tello_sdk_udp` and the Tello models, `mavlink` to
  `mavlink` and `generic_multirotor`. Test: registry unit tests.
- [ ] SEAM-3 [A3] The edge picks its backend from the unit's model; `--drone` must agree with the
  model's adapter, otherwise the edge refuses to start with both names. Test: edge argument tests.
- [ ] SEAM-4 [A3] The `ForwardCameraSensor` is built only when `forward_obstacle_sensing` is
  available and on. Test: obstacle wiring unit test.
- [ ] SEAM-5 [D3] `coverage.CameraModel` and `geolocate.CameraModel` instances used for planning and
  geolocation are built from the catalogue; the Tello coverage numbers are unchanged. Test: unit test.
- [ ] SEAM-6 [A1 to A4] `tests/test_architecture.py` passes, and no module under
  `flightrisk/hardware/` or `flightrisk/params/` imports a concrete backend. Test: extend the
  architecture test.
- [ ] SEAM-7 [A3] (F8) Each `detections` message carries optional `cam` and `pitch_deg` from the
  frame it came from; the ground's detections handler accepts a message with and without them, and an
  older handler's validation accepts the extra keys (ASSUMPTION A8). Test: edge message test and
  ground handler test.
- [ ] SEAM-8 [A3] (F8) One detection pipeline runs per unit: `select_pipeline` returns exactly one
  profile for the unit's search camera. Test: unit test with a dual-sensor unit.

### Edge (EDGE)

- [ ] EDGE-1 [A3] The edge resolves every drone-scope parameter and feature at start and, on any
  refusal, exits with the standard message before it contacts the drone. Test: edge start unit tests
  with a fake drone that records calls.
- [ ] EDGE-2 [A3] `SafetyEnvelope`, `FlightGuards`, `TempLimits`, the obstacle and avoidance
  settings and `MavlinkFleetOptions` are built from resolved values. Test: unit tests.
- [ ] EDGE-3 [A3] Each non-default value is logged once at start with its source, and less-safe
  values at warning level. Test: log capture test.
- [ ] EDGE-4 [A3] The edge reads `FLIGHTRISK_LAUNCHER_PARAMS` as the launcher layer, including its
  `confirmed` object; invalid JSON or an unknown id refuses the start. Test: unit tests.
- [ ] EDGE-5 [A3] A Tello edge with no setting resolves every value to the golden file's `tello`
  defaults, and the existing safety, ceiling, flight-guard, obstacle, avoidance, temperature and
  telemetry test suites pass with only the edits of design section 20. Test: the existing suites plus
  a table test of resolved defaults.
- [ ] EDGE-6 [A3] An edge refuses to start when its model's scale, adapter or frame path does not
  match its backend. Test: unit test.
- [ ] EDGE-7 [A3] Every existing flight environment variable and flag, set to a valid non-default
  value, changes the corresponding resolved value with source `env` or `cli`. Test: table-driven unit
  test over all aliases and flags.

### Protocol (PROTO)

- [ ] PROTO-1 [A2] (F5) `describe_unit` is in a new `QUERY_TYPES` set and not in
  `CONFIG_COMMAND_TYPES`; the query and its `ack` have validators; the ack shape is that of design
  section 12.2, with `unit.link` holding only the link kind; every key passes the private-key screen;
  the ack contains no list of numbers; a worst-case ack (a custom model with three cameras and every
  row) is below 16 KB. Test: protocol unit tests.
- [ ] PROTO-2 [A2, A3] (F12) `unit_rev` is an optional key of the protocol v2 edge `hello`,
  validated as 12 lowercase hex digits (A2); the edge sends it in every `hello`, so it is re-sent on
  every reconnect, and the telemetry field count is unchanged (A3). Test: validator test;
  reconnect test; `tests/test_telemetry_field_cap.py` passes unchanged.
- [ ] PROTO-3 [A2, A4] A `hello` with `unit_rev` validates under the current ground rules, and a
  ground never sends `describe_unit` to an edge that has not advertised `unit_rev`. Test: validator
  test (A2); coordinator test with a fake older edge (A4).
- [ ] PROTO-4 [A3] (F5) The edge answers `describe_unit` from any connected ground and in any state;
  the query never feeds the link watchdog in single-ground or multi-ground mode; repeated `cmd_id`s
  are answered from a per-connection cache of 8 entries that never touches the authority's shared
  cache; more than one query per connection per 2 s gets a `nack` with reason `busy`. Test: edge link
  tests for each rule.
- [ ] PROTO-5 [A2] (F13) `unit_rev` hashes only the start-time resolution: it changes exactly when
  that content changes, and a later `set_ceiling` does not change it. Test: unit test.
- [ ] PROTO-6 [A3] (F5) A standby ground that sends `describe_unit` every 0.2 s while the authority
  falls silent does not delay the link-loss landing: the drone lands within the same tolerance as with
  no queries. Test: multi-ground integration test modelled on `tests/test_edge_uplink.py:205`.
- [ ] PROTO-7 [A2] (F5) `flightrisk/obs_comm.py` logs `describe_unit` and its replies as aggregated
  traffic. Test: logging unit test.

### Ground planner (GND)

- [ ] GND-1 [A4] The coordinator plans each unit against that unit's geofence, move range and slot
  bounds. Test: unit test with two units of different bounds.
- [ ] GND-2 [A4] The start battery gate uses each unit's resolved `battery.min_takeoff_pct`. Test:
  unit test with 40 % set on one edge.
- [ ] GND-3 [A4, D2] (F7) The coordinator uses each unit's move range instead of `TELLO_MIN_MOVE_CM` and
  `TELLO_MAX_MOVE_CM`, and default plans for 1, 2, 3 and 4 Tellos are unchanged. Test: unit test with a
  field unit; plan snapshots for 1, 2, 3 and 4 Tellos taken on the A4 base and compared after.
- [ ] GND-4 [A4] `fleet_patterns` zone slots equal the coordinator's (fixed by #208); A4 only reads them through the registry. Test: unit test.
- [ ] GND-5 [D1] A mission mixing environments is refused at Plan, naming each unit from the wrong environment, and indoors every unit without a measured indoor profile (spec AC-4.2). Test: unit test.
- [ ] GND-6 [D1] Indoor slots never count as separation: two indoor drones are separated horizontally whatever their slots; field drones in different bands may overlap horizontally except in a sweep-band drone's takeoff, landing or descent column (spec 8.3; replaces round 1 F7). Test: airspace unit tests; the snapshots of GND-3 still pass.
- [ ] GND-7 [A4] (F13) Plan requires each reporting unit's stored `describe_unit` reply to carry the
  latest `unit_rev` its `hello` advertised, and plans with the intersection of the reported bounds and
  the ground's own registry for the reported model; a reported value outside that intersection refuses
  Plan for the unit with both bounds. Test: unit tests.

### Dashboard (DSH)

- [ ] DSH-1 [A4] The ground sends `describe_unit` when a `hello` carries a `unit_rev` it has not
  described, and stores the answer per unit. Test: integration test with a fake edge.
- [ ] DSH-2 [A4] Plan is refused, with the message of `hardware-profiles-detail.md` section 12.3, while a reporting unit's
  description is missing or stale. Test: unit test.
- [ ] DSH-3 [A4] `ceiling_settings.limits` keeps its five keys and holds the intersection of the
  fleet's bounds; each `drones[]` entry may carry `model` and `limits`. Test: payload unit tests; the
  existing mobile fixture tests pass.
- [ ] DSH-4 [F1] Socket.IO `get_inventory` answers `inventory`, `get_flight_params` answers `flight_params`; `inventory_add_unit` (new unit of a built-in model only), `inventory_set_available` and `inventory_set_feature` (non-safety features only) answer `inventory_ack`, carry `rev`, and are refused while a drone flies, a start is scheduled or this ground is a standby. Test: handler tests, including refusals for an existing unit id, a custom model, a safety feature in either direction and a stale `rev`.
- [ ] DSH-5 [A4] The "Fleet hardware" and "Flight parameters" cards show, per unit, the model, camera
  mounts with roles, features with reasons, and every value with unit, source, default and bound,
  including the read-only `tello_sdk.*` rows. Test: rendering test of the payload-to-DOM function
  where one exists; otherwise a documented manual check in a real browser.
- [ ] DSH-6 [A4] The dashboard's fleet-wide strip and area defaults come from the registry, and
  `--fleet-strip-cm` and `FLIGHTRISK_FLEET_STRIP_CM` still work within their bounds. Test: argument
  tests.
- [ ] DSH-7 [A4] (F15) `flightrisk/dashboard/single_search.py` uses the resolved
  `battery.min_takeoff_pct`. Test: unit test with 40 % set.
- [ ] DSH-8 [C4] (F4) The dashboard ceiling card asks for confirmation before saving a ceiling raise
  or a margin decrease relative to the saved file, or a value less safe than the profile default.
  Test: server-side handler test that a save without the confirmation is refused.
- [ ] DSH-9 [A4] (F16) A degraded feature (`appearance_match`, `claude_second_opinion`) shows its
  banner text on the cards. Test: payload unit test.

### Launcher (LCH)

- [ ] LCH-1 [A5] The launcher edits `inventory.json` (units, links, features, gimbal mission pitch
  and custom models) with compare-and-swap writes, and offers only available units as drone rows.
  Test: server and settings tests.
- [ ] LCH-2 [A5] The launcher shows the effective table for each edge it starts, with sources, before
  Start and while the edges run. Test: server test of the payload.
- [ ] LCH-3 [A5] A refused value is an `Issue(ERROR)` that blocks Start; an environment value that
  overrides the window is an `Issue(WARNING)`. Test: `validate` unit tests.
- [ ] LCH-4 [C4] (F4, F19) Start returns HTTP 409 with `needs_confirmation: "flight_params"` while any
  value of `hardware-profiles-detail.md` section 10.2's list is unconfirmed, including an environment or flag value that is
  less safe than the window's value for an `edit: ui` row; the retry succeeds only when
  `confirm_params` holds an entry with the same `unit`, `id` and exact `value` for each; a preset load
  or window reload forgets confirmations; `confirm_sensing` is still accepted. Test: server tests,
  including a confirmation for one unit that does not count for another.
- [ ] LCH-5 [A5] Edges and the ground get `FLIGHTRISK_LAUNCHER_PARAMS` (with the `confirmed`
  object); it is in `CONTROLLED_ENV`; the launcher no longer sets `FLIGHTRISK_GEOFENCE_CM`,
  `FLIGHTRISK_CEILING_CM`, `FLIGHTRISK_CEILING_MARGIN_CM` or `--alt-slot`. Test: command snapshot
  tests.
- [ ] LCH-6 [A5] The preflight battery check uses the effective takeoff minimum. Test: preflight
  unit test with 40 % set.
- [ ] LCH-7 [A5] A `launcher.json` written by the current launcher loads unchanged. Test: fixture test
  with a saved file.
- [ ] LCH-8 [C4] (F4) At Start the launcher writes the resolved ceiling, margin and slots into the
  ceiling file, and the Start plan shows their sources; with an inherited `FLIGHTRISK_CEILING_CM`, the
  file holds that value. Test: `ceilingfile` unit tests.
- [ ] LCH-9 [C4] (F9) Changing a unit's model to one with any wider bound needs the 409 confirmation
  listing every widened bound. Test: server test.
- [ ] LCH-10 [C4] (F19) `docs/launcher.md` and `docs/fleet-setup.md` carry the migration notes of
  `hardware-profiles-detail.md` section 16 (presets, ceiling file, shell exports that now win). Test: review.

### Mobile (MOB)

- [ ] MOB-1 [F1] `docs/mobile-contract.md` lists the new events and emits; `inventory.json`,
  `flight_params.json`, `inventory_ack.json` and new `client_emits.json` cases are generated by Python
  like the existing fixtures. Test: fixture generator test.
- [ ] MOB-2 [F3] The iOS decoders read the new fixtures and the `unknown_keys` variants leniently.
  Test: `GroundProtocolTests`.
- [ ] MOB-3 [F3] The iOS app shows the inventory (add a unit of a built-in model, availability,
  non-safety feature switches) and the read-only parameter table, and can emit only the allowed events.
  Test: unit tests of the command encoder against `client_emits.json`.
- [ ] MOB-4 [F2] The Android app shows the inventory with the phone limits, the read-only parameter, mission and target views, and the supervisor's Return controls when its role allows them. Test: Android unit tests against the fixtures.
- [ ] MOB-5 [dropped] The Android direct Tello stack is not changed: the direct single-Tello mode is a spec non-goal (design D-A6).
- [ ] MOB-6 [F1, F2, F3] (F9) A phone cannot switch a safety feature in either direction, edit an
  existing unit's model, link or features, remove a unit, or define a model: the ground refuses it and
  `client_emits.json` has a forbidden case for each. Test: fixture and handler tests.
- [ ] MOB-7 [F2] Phone-as-edge messages are unchanged. Test: the existing Android edge tests pass
  unchanged.
- [ ] MOB-8 [dropped] The Kotlin literal check covered the direct Tello stack, which is a spec non-goal.

### Guard test (GRD)

- [ ] GRD-1 [A1] (F11) `tests/test_param_literals.py` applies rules A to F of design section 19 to
  the files in scope (including `flightrisk/dashboard/*.py` and `flightrisk/launcher/*.py`), and every
  finding is in `tests/param_literal_allowlist.py` with a category, a reason and its exact count.
  Test: the test itself.
- [ ] GRD-2 [A1] An allowlist key whose count no longer matches fails the test. Test: the test
  itself; each migration PR deletes or lowers the entries it migrates.
- [ ] GRD-3 [A1] The checker reports each planted literal of design section 19 in a temporary file,
  one per rule. Test: self-tests.
- [ ] GRD-4 [A1] Every `deferred` entry names a follow-up. Test: the test itself.
- [ ] GRD-5 [A1] (F11) Name words are derived from the registry ids; adding a row with a new word
  makes the checker report an existing literal with that word. Test: self-test with a temporary row.
- [ ] GRD-6 [A1] (F11) Allowlist keys are `(file, enclosing scope, kind, callee or target, name,
  value)`; moving a literal to another function or changing its value fails the test. Test: self-tests.

### Documentation (DOC)

- [ ] DOC-1 [every PR] `docs/flight-parameters.md` and `docs/hardware-catalogue.md` are generated;
  `docs/ceiling-and-altitude.md`, `docs/obstacle-sensing.md`, `docs/open-drone-support.md` and
  `docs/drone-interface.md` (adapter, `CameraSource`, frame metadata, camera roles) are updated by the
  PR that changes the behaviour they describe; `docs/launcher.md` and `docs/fleet-setup.md` describe
  the inventory and the migration notes. Test: review.

### Compatibility (CMP)

- [ ] CMP-1 [A1 to A5] Phase A changes no runtime behaviour: the full suite passes with only the assertion edits design section 20 lists for A5, and no runtime path sends `return`, `escort_setpoint` or `on_complete: return`, enforces a target bound, or flips a default. Test: the full suite; a grep-based test for call sites.
- [ ] CMP-2 [C4] With no new setting, every resolved value equals the golden file's, except the defaults design section 20 lists for C4. Test: the suites; the AC-6.6 hardware runs on the ZBook before C4 merges.
- [ ] CMP-3 [A1, C4] (F10) `tests/test_param_parity.py` probes every environment alias
  and flag at both ends of today's range and the new bound, plus a typical and a malformed value, and
  asserts that the resolver accepts exactly what today's code accepts, except the probes listed in
  `PARITY_EXCEPTIONS`, each naming its design section 20 item. Test: the test itself.
- [ ] CMP-4 [every PR] (F10) Each PR edits only the existing assertions design section 20
  lists for it. Test: review of the diff against the list; a PR that needs another edit stops and
  splits the change out.