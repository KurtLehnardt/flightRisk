# Fleet architecture v1: requirements checklist

Every item is testable. "Test" names the check: a unit test, a fake-drone or simulated-autopilot test
("fake"), an ArduPilot or PX4 SITL scenario ("SITL", design section 21), a fixture test, or a hardware
run. The PR that delivers each item is in brackets and matches `tasks.md`. Section references point to
`design.md` unless they name another file. Finding labels: C-n, H-n, M-n from the round 1 review of the
v1 architecture; B-n and N-n written with a hyphen from the final round (revision 3); (B*n*), (N*n*)
from round 2 of PR #202; (F*n*) from round 1 of PR #202.

Spec acceptance criteria map as follows: AC-1 → CMP-2, RTH-19, RTH-20; AC-2 → ROLES-1 to ROLES-12;
AC-4.1 to AC-4.3 → CAP-1 to CAP-4, GND-5; AC-5.1 to AC-5.3 → TASK-1 to TASK-8; AC-5.4 to AC-5.9 →
FIND-1 to FIND-20, TASK-9 to TASK-12; AC-6.1 to AC-6.6 → RTH-1 to RTH-38, LOST-1 to LOST-8; AC-7.1 to AC-7.3 → MAV-1 to
MAV-19, RTH-13, RTH-25, AIR-13; AC-8.1 and AC-8.2 → AIR-1 to AIR-15, RTH-23; AC-9.1 and AC-9.2 → UI-1 to
UI-6, ROLES-1 to ROLES-12, MOB-9; AC-10 → PAR, RES, CONF, FACT and C5 items.

## Capability model (CAP)

- [ ] CAP-1 [A1] Every built-in model carries every spec 4.1 fact as a `CapabilityFact` with value, source
  and status, shown on the dashboard and both apps (AC-4.1). Test: unit test over all models and facts.
- [ ] CAP-2 [A1] A missing fact takes its most conservative value marked `unverified`; an unknown model
  gets the intersection of its environment's built-in bounds with the safer default of each. Test: unit.
- [ ] CAP-3 [C1] A runtime capability missing at takeoff refuses the takeoff, naming it; one lost in flight
  raises its spec 6.2 event (moved from A3; nitpick 7). Test: fake.
- [ ] CAP-4 [A3] Changing a fake drone's declared capabilities changes its return method and separation with
  no rule change (AC-4.3). Test: fake.
- [ ] CAP-5 [A2] A custom model can never set `environment` or `indoor_profile_measured` (M-11). Test:
  loader test.
- [ ] CAP-6 [A3, B1] A unit is simulated only when its edge reports it and verifies it: a fake or sim
  adapter, or a MAVLink autopilot whose board identity (the `AUTOPILOT_VERSION` board type and the
  firmware's board name) is a desktop SITL build, together with the simulator's parameters; simulation
  parameters alone never count; the inventory cannot mark a unit simulated (H-3(c), N-15). Test: fake;
  SITL; recorded fixtures of a real autopilot, a PX4 SIH and a HITL setup are all classified real.

## MAVLink production adapter (MAV)

- [ ] MAV-1 [B2] Start is refused, naming the drone and the check, for a field drone without a 3D fix, with
  fewer than `gps.min_satellites`, with receiver accuracy above `home.accuracy_cm`, with its autopilot home
  beyond `home.max_offset_cm`, or outside the mission fence (AC-7.1). Test: fake; SITL.
- [ ] MAV-2 [B2] The edge reports its uncertainty as `position.gps_sigma_k` × the receiver's 1-sigma accuracy
  (or the EKF variance when larger) + ground speed × position age, and a missing value as worst case (M-1).
  Test: unit; SITL.
- [ ] MAV-3 [B2] An autopilot home that changes in flight lands the drone with `no_known_home`. Test: fake;
  SITL.
- [ ] MAV-4 [B3] The edge uploads the drone's fence and the mission fence before arming, reads them back and
  refuses the takeoff on a failure or mismatch; fences never change in flight. Test: fake; SITL ArduPilot
  and PX4.
- [ ] MAV-5 [B4] Every row of the parameter table (design section 13.2) is checked before takeoff for both
  autopilots, with the accepted values exactly as listed, and a refusal names the parameter, its value and
  the accepted values (H-2). Test: one unit test per row and autopilot.
- [ ] MAV-6 [B4] A lost GPS fix or an EKF failsafe is a positioning fault: the autopilot acts (ArduPilot
  lands and never starts a dead-reckoned return; PX4 descends), the edge shows `fault_positioning` and sends
  only operator lands. Re-matching its tasks is D4's (TASK-7). Test: SITL (`SIM_GPS_DISABLE` with
  `FS_DR_ENABLE` 1 and with 0; PX4 GPS off with `COM_POS_FS_ACT` 0).
- [ ] MAV-7 [B4, C1, C2] An autopilot-started return is detected and shown with its reason (B4); while
  the edge lives the progress monitor watches its horizontal legs and a blocked one only shows the reason
  and alerts, and the edge sends only operator lands (C1); the ground treats it as a fixed
  highest-priority route (C2) (N-7). Test: fake; SITL (battery failsafe return) in each PR.
- [ ] MAV-8 [B4] GCS failsafe: `FS_GCS_ENABLE` 1 or 5, `FS_GCS_TIMEOUT` 2 to 10 s, the GCS system id equal to
  the edge's; PX4 `NAV_DLL_ACT` 2 or 3, `COM_DL_LOSS_T` 5 to 10 s (PX4's minimum is 5 s, N-18),
  `COM_DLL_EXCEPT` 0. Test: SITL (kill the edge: the drone returns or lands).
- [ ] MAV-9 [B4] `FS_OPTIONS` bits 0, 1, 2 and 4 clear; `COM_RCL_EXCEPT` 0. Test: unit; SITL.
- [ ] MAV-10 [B4] The return ends landed at home (`RTL_ALT_FINAL` 0; `RTL_LAND_DELAY` ≥ 0), goes to home (no
  rally points; PX4 `RTL_TYPE` 0, with 1 to 4 refused), and runs no leftover mission landing (action 6
  refused, mission cleared before arming). Test: SITL for each.
- [ ] MAV-11 [B4] Fence, critical-battery, low-battery and RC-loss actions accept only the values of the
  table; values accepted today and refused now are listed in design section 27 item 6. Test: unit; SITL.
- [ ] MAV-12 [B4] The autopilot's low-battery level is at most `battery.return_floor_pct` − 5 points; a
  voltage-only threshold is refused; `BATT_LOW_VOLT` is 0 when the capacity threshold is used; the field
  critical level is the autopilot's, and K-1 and K-2 are checked again with it (K-4, N-18). Test: unit;
  SITL slow drain (FlightRisk returns first).
- [ ] MAV-13 [B4, C1] The autopilot's landing delay is written as min(the drone's place in its landing
  order × `separation.landing_interval_s`, the autopilot's maximum: 60 s for `RTL_LOIT_TIME`) and read
  back before arming: place 0 in B4; C1 consumes the order D1 sends in `assign_lanes.landing` (N-7, N-12).
  Test: SITL (two drones, shared landing area arriving together, both edges killed, sequenced landings).
- [ ] MAV-14 [B4] `--allow-unverified-failsafes` and `link.require_failsafes = false` are accepted only for
  a verified simulated unit. Test: unit.
- [ ] MAV-15 [B4] ArduPilot position faults (B-4): `FS_DR_ENABLE` 1 or 0 is required and 2 (the default),
  3, 4 and 6 are refused; `FS_EKF_THRESH` above 0 is required. Test: one unit test per value; SITL
  (`SIM_GPS_DISABLE` in flight with each accepted `FS_DR_ENABLE`: the drone lands, never returns).
- [ ] MAV-16 [B4] PX4 position faults (B-4): `COM_POS_FS_ACT` 0 is required (1 refused); `COM_POS_LOW_ACT`
  5, or the low-accuracy action disabled (`COM_POS_LOW_EPH` −1, or `COM_POS_LOW_ACT` 0 or 1), is
  required, and 2, 3 and 4 are refused (never 4). Test: unit; SITL (GPS
  off in flight: the drone descends).
- [ ] MAV-17 [B4] Edge hang (N-1): a field takeoff is refused unless `mavsdk_server` was started tied to the
  edge; a watchdog outside the control loop kills the server when the loop has not ticked for longer than
  the autopilot's `FS_GCS_TIMEOUT` (PX4 `COM_DL_LOSS_T`). Test: unit (untied server refused); SITL (stall
  the control loop: the heartbeat stops and the GCS failsafe acts).
- [ ] MAV-18 [B4] `RTL_CLIMB_MIN` (`RTL_CLIMB_MIN_M`) is at most the band ceiling minus the slot (N-18).
  Test: unit; SITL (the return stays in the close band).
- [ ] MAV-19 [A2, B4] The edge reports its autopilot's link-loss action as `ap_link_loss` (`return`, `land`
  or `unknown`) in `describe_unit` and the `rth` telemetry object: the shape in A2, the reading in B4,
  re-read after every parameter write (B-3). Test: protocol test (A2); fake and SITL (B4).

## SITL harness (SITL)

- [ ] SITL-1 [B1] `scripts/sitl_fleet.sh --count N --autopilot ardupilot|px4` starts N vehicles (2 to 8,
  the protocol roster cap `MAX_ROSTER`, N-9) with their own system ids and separated homes, and stops
  them. Test: runs with N = 2 and N = 8.
- [ ] SITL-2 [B1] Fixtures set the section 13 parameters, start one real edge per vehicle with the A3
  command line (nitpick 11) and the ground, and tear down; `sitl` marker. Test: smoke scenario.
- [ ] SITL-3 [B1] B1 records the largest fleet that keeps telemetry at its rate, the PX4 multi-vehicle
  command, the SITL fingerprint of each autopilot, and the local `AP_FWVersion.h` patch the native Mac
  build needs (`docs/sitl-demo.md:122`, nitpick 10). Test: recorded in `docs/sitl-demo.md`.
- [ ] SITL-4 [B1 to E4] Every PR marked SITL adds its scenarios and attaches the log. Test: review.

## Return to home (RTH)

- [ ] RTH-1 [C1] The state machine implements every state of section 10.3 and every row of the event table
  (10.4); each row has a unit test of its default action and of each fallback reason (AC-6.1, M-12). Test:
  unit.
- [ ] RTH-2 [C1] C1 resolves to today's actions, so it changes no behaviour except items 19, 20 and 43 of
  section 27. Test: existing suites pass with no other edit.
- [ ] RTH-3 [C1] Fault classifier: unclassifiable errors are faults. Test: unit over the known replies.
- [ ] RTH-4 [C1] Return threshold = critical + energy to home + worst-case wait + reserve, never below the
  floor; the worst-case wait comes from the landing order and the last granted queue wait; a drone arrives
  above home with at least critical + reserve (H-8). Test: unit; a fake return with default numbers ends
  landed at home.
- [ ] RTH-5 [C1] The indoor uncertainty model grows from the placeholders and never shrinks in flight.
  Test: unit.
- [ ] RTH-6 [C1] The Tello's dead-reckoned return (section 10.10; AC-6.4). Test: fake.
- [ ] RTH-7 [C1] Blocked return: hover, look again, another route, then after `return.blocked_wait_s` hold
  for an operator (bounded, RTH-32) or land with `way_home_blocked_obstacle`. Test: fake.
- [ ] RTH-8 [C1] Person cue on the way home: land after `return.person_cue_wait_s` without an answer. Test:
  fake.
- [ ] RTH-9 [C1] After a long link loss the edge returns only inside its zone and corridor, outside every
  known no-entry area other than its own, in its landing order, else lands with `no_cleared_route` (AC-6.2).
  Test: fake; SITL.
- [ ] RTH-10 [C1] The `rth` telemetry object is one top-level field (38 of 48 at most). Test: field-cap test.
- [ ] RTH-11 [A2] `return_home`, `on_complete: return_home`, `corridor`, `landing`, `no_entry`,
  `set_no_entry`, `set_speed` and `escort_setpoint` validate; corridor and area points are objects;
  `hover_timeout_s` ≥ 1 s (M-7). Test: protocol tests.
- [ ] RTH-12 [C2] RETURN ALL shows each drone's result. Test: fake; SITL (four drones).
- [ ] RTH-13 [C2] Ordering: autopilot-started returns first, then lowest battery margin, then shortest route;
  waiting drones hold side by side at their own slots; separation never broken (AC-7.3). Test: unit; SITL.
- [ ] RTH-14 [C2] One landing at a time per shared landing area. Test: SITL.
- [ ] RTH-15 [C2] Per-drone controls as spec 9.2. Test: handler tests.
- [ ] RTH-16 [C2] The stranded countdown's return action, a blocked return then lands, no restart (AC-6.3).
  Test: fake.
- [ ] RTH-17 [C2] Homes, uncertainties, routes, progress, landing order and landed-in-place reasons on the
  map (AC-7.2). Test: payload tests; browser check.
- [ ] RTH-18 [C1] An edge restart in flight lands with `no_known_home`. Test: fake.
- [ ] RTH-19 [C6, C7] The resolved defaults switch to return, field in C6 and indoor in C7, and every other
  value keeps today's default (AC-1). Test: the start-path tests named in each PR; SITL (C6); edge laptop (C7).
- [ ] RTH-20 [C2] After LAND ALL every drone's edge sends LAND within 1 s and the autopilot reports LAND mode
  within 2 s, also during returns (AC-6.3; absolute bounds, M-9). A drone under external control is
  exempt: it gets exactly one LAND per operator command (RTH-35). Test: SITL.
- [ ] RTH-21 [C1, C2] Property test: random sequences of events, commands and timeouts through the state
  machine and the ground senders never produce a takeoff (AC-6.5, M-9). Test: property test.
- [ ] RTH-22 [C7] AC-6.6 on the edge laptop: a 300 cm out-and-back lane in a 400 by 400 cm room, link cut past
  the wait, ten taped runs, each landing within its uncertainty bound of the mark. Test: hardware log.
- [ ] RTH-23 [C1] Under fast uncertainty growth an indoor drone holds, then returns, then lands (AC-8.2).
  Test: fake.
- [ ] RTH-24 [C1] Critical battery lands in place at once, even mid-return. Test: fake; SITL.
- [ ] RTH-25 [C2] Indoor Start confirmation lists each mark and heading for the operator to confirm; Start is
  refused until confirmed (indoor AC-7.1; M-9). Test: handler test.
- [ ] RTH-26 [C1] Landing is terminal: every landing decision enters the state machine; while landing the
  edge nacks `return_home`, `escort_setpoint`, `assign_lanes`, `start_at`, `resume`, `nudge`, manual moves
  and `set_speed` with `landing` and starts no automatic return; a further land is accepted. Once landed
  the edge accepts and stores plans and flies only on an operator Start or manual takeoff (C-1, N-19).
  Test: unit; fake (each command after LAND ALL; a plan sent to a landed drone is accepted and not flown).
- [ ] RTH-27 [C1] The MAVLink adapter never sends RTL after a land is decided; a `wait: false` release after
  a land is refused (C-1). Test: fake.
- [ ] RTH-28 [C2] LAND ALL marks every drone `landing`, sends the land, and only then cancels,
  asynchronously, the return queue, escort controller, local-search re-plans, converge, re-task offers,
  stranded countdowns, `on_complete` timers, a scheduled mission-end RETURN ALL and speed changes;
  cancellation never blocks or delays the send, and a sender that loses the race sees the mark and sends
  nothing; per-drone Land the same for one drone (C-1, N-2). Test: unit (a cancellation that blocks does
  not delay the land); SITL (each sender active, then LAND ALL, within RTH-20's bounds).
- [ ] RTH-29 [C1] The MAVLink edge-guided return moves into the slot first, flies GUIDED to above home at the
  slot, holds until cleared, then lands. Test: SITL.
- [ ] RTH-30 [C2] A returning drone holds above home until the ground clears its landing. Test: SITL.
- [ ] RTH-31 [C1] The progress monitor runs only during horizontal return legs: a leg that gains less than
  `return.progress_min_cm` within `return.progress_timeout_s` is blocked; it is paused in waits, the hold
  above home, the move into the slot, the autopilot's climb and loiter phases, person-cue hovers and
  landing; within `return.progress_min_cm` of home counts as arrived; for an autopilot-started return,
  blocked only shows the reason and alerts, and nothing is sent except operator commands (B-2). Test: fake; SITL (an RTL that stops short; a place-1
  drone holding above home in a shared landing area is never reported blocked).
- [ ] RTH-32 [C2] Every wait and hold is bounded by the energy model; if two bounds cannot both be met the
  earlier bound goes first, and the drone that cannot be scheduled lands with `no_return_order` (ties: higher
  unit id) (M-10). Test: unit.
- [ ] RTH-33 [C2] `return_home` without `wait` is accepted from any authenticated ground, so a standby keeps
  RETURN ALL; an edge without `return_home` is shown "cannot return" and held, never landed (M-5). Test:
  multi-ground test; older-edge fake.
- [ ] RTH-34 [C1 to C7] Every spec 6.2 row of section 10.4 has its own test, and every SITL-marked row its
  scenario (M-12). Test: a test that lists the rows and their tests.
- [ ] RTH-35 [C1] External control is a terminal sub-state (B-1): after a mode change the edge did not make,
  no automatic land, no land retry, no move, return or takeoff; every motion command is nacked with
  `landing`; each operator land command sends exactly one LAND. Test: fake and SITL (a simulated pilot
  changes the mode during `rth_landing`: no second LAND until the next operator land, which sends one).
- [ ] RTH-36 [C1] The edge checks, before takeoff and before accepting any assignment or re-task, that the
  straight line from every point of the assignment's zone and corridor to home stays inside them; if not
  and `ap_link_loss` is `return` (or `unknown`), it writes and reads back the land action, else refuses
  the takeoff or nacks with `straight_line_home`; it refuses legs into known no-entry areas other than its
  own (B-3, moved from D1, N-7). Test: unit; fake; SITL (escort re-task to a return-on-loss drone; edge
  killed at the far corner of a zone: the drone lands).
- [ ] RTH-37 [C2] The ground reads `task.end_action` (sent as `on_complete`) and `stranded.action`, with
  today's default land, so C6 and C7 change only defaults (N-6). Test: unit (each value).
- [ ] RTH-38 [C1, C6] `return.field_method`: C1 implements the autopilot RTL and the edge-guided return for
  the returns a MAVLink edge starts; C6 switches the default to edge-guided (section 27 item 45, N-6).
  Test: SITL with each value.

## Lost drones (LOST, C-2)

- [ ] LOST-1 [D2] The ground declares a drone lost only after `link.loss_detect_s` + `link.loss_wait_s` +
  `link.ground_loss_margin_s` for that unit (K-7); `FLEET_LOSS_TIMEOUT_S` is removed. Test: unit; fake field
  drone holding 13 s is not declared lost.
- [ ] LOST-2 [D2] The ground never lands or re-lands a lost drone as a loss reaction; it sends it only
  operator commands. #212's idle-controller countdown stays as the stranded event: a lost drone held by a
  dashboard that sends nothing for `stranded.lost_idle_s` starts the stranded countdown with
  `stranded.action` (N-4). Test: unit (no `land` on loss, none on reconnect while flying; the idle
  countdown still runs).
- [ ] LOST-3 [D2] A lost drone's lanes wait for a human confirm; nothing is re-tasked automatically. Test:
  unit.
- [ ] LOST-4 [D2] The confirm is refused while the lost drone may still be flying: until it reports landed or
  `lost.flying_window_s` after its last flying report, where the field window includes the edge stall
  bound and the autopilot's GCS timeout and is never shortened by an endurance estimate (#212's refusal as
  precondition; N-1). Test: unit, including a link that drops again and a hung edge.
- [ ] LOST-5 [D2] Its no-entry area covers its moving last position widened by `position.gps_sigma_k` × its
  last 1-sigma accuracy (indoor: its last uncertainty) + `noentry.lost_margin_cm`, never by speed × age,
  plus its zone, corridor, return line and landing area (zone, corridor and landing area without a
  position); the owner is exempt (H-9, N-11). Test: unit (a neighbouring zone is not swallowed).
- [ ] LOST-6 [D2] #212's orphan-landed mark and its refusal of a later phone Resume stay. Test: #212's tests
  pass.
- [ ] LOST-7 [C7] The Tello's `lost.flying_window_s` becomes the derived formula with a 20 s floor (N-3).
  Test: unit.
- [ ] LOST-8 [D2] When a lost drone's link returns, Resume task is refused once any of its lanes was
  confirmed for another drone; Resume search stays refused for a lost drone (#212, N-4). Test: handler
  tests.

## Airspace (AIR)

- [ ] AIR-1 [D1] Route tubes of two drones in one band never intersect (space-only, widened by half the base
  separation plus predicted uncertainty); Plan refuses a route that would (AC-8.1). Test: unit; SITL eight
  vehicles.
- [ ] AIR-2 [D1] The in-flight separation monitor checks every pair at the telemetry rate; on a violation
  the drone that is not returning moves out of the returning drone's route tube (for an autopilot-started
  return, out of its straight line home's tube) or holds when already outside it; between two
  non-returning drones the higher unit id holds (M-1, N-13). Test: unit.
- [ ] AIR-3 [D1] Every route widened by uncertainty stays inside every fence and outside every unconfirmed
  no-entry area (AC-8.1). Test: unit.
- [ ] AIR-4 [D1] Person-cue and operator-drawn no-entry areas; confirm-to-enter (one drone, one entry) and
  confirm-to-clear (everyone) on a laptop; Converge, Not verified and Escort count for their subject. Test:
  unit.
- [ ] AIR-5 [D1] Degrading precision grows the separation at once; carry on, hold and re-route or return, or
  land past the bound. Test: unit.
- [ ] AIR-6 [D1] An empty intersection of bounds across a fleet's models refuses Plan, naming the units. Test:
  unit.
- [ ] AIR-7 [D1] Zones and corridors are generated per drone; zone-and-corridor tubes in one band never
  intersect (H-1). Test: unit.
- [ ] AIR-8 [D1] Plan and re-task offers use each edge's reported `ap_link_loss`: a zone whose straight line
  home leaves zone and corridor goes only to a land-on-loss drone, or is re-planned (H-1, B-3); the edge's
  own check is RTH-36. Test: unit; SITL (a plan for a return-on-loss drone avoids such a zone).
- [ ] AIR-9 [D1] Homes closer than `separation.landing_cm` share a landing area with an order sent in
  `assign_lanes.landing`; a shared area without an order is refused (H-1, H-7). Test: unit.
- [ ] AIR-10 [D1] No-entry areas reach the edges in `assign_lanes` and `set_no_entry`; every re-task re-sends
  zone, corridor, landing record and areas (H-1). The edge's refusal of legs into known areas is RTH-36
  (C1). Test: unit (payloads).
- [ ] AIR-11 [D1] Re-tasks, converge, local search and escort carry a task zone and a corridor home, checked
  like any zone; an escort is offered only to a land-on-loss drone (B-3). Test: unit.
- [ ] AIR-12 [D1] Bands: both intervals defined; constraints K-12 and K-13 hold; Plan refuses a slot outside
  its task's band (H-6). Test: unit.
- [ ] AIR-13 [D1] Homes are at least `separation.base_cm` apart plus both accuracies in the field; Start
  names both drones of a closer pair (H-7, K-15). Test: unit; SITL.
- [ ] AIR-14 [D1] A field mission needs `airspace.obstacle_top_cm` per area and corridor; Plan refuses a slot
  below it + `airspace.obstacle_margin_cm`, naming the area and drone (H-10, K-14). Test: unit.
- [ ] AIR-15 [D1] Indoor slots never count as separation; field drones in different bands may overlap
  horizontally except in a sweep-band drone's takeoff, landing or descent column. Test: unit.

## Confirmations (CONF, H-3)

- [ ] CONF-1 [C5] Start is refused, naming the unit, for any unit without a current `describe_unit` reply for
  its latest `unit_rev` (older edges and v1 links included). Test: unit.
- [ ] CONF-2 [C5] `scripts/field_fleet_ground.py` prints the Start list and needs `--confirm unit:id=value`
  for each less-safe value, including its own `--allow-unverified-failsafes` and `--link-loss-hold-s`, unless
  every unit is verified simulated. Test: script tests.
- [ ] CONF-3 [A1] Until C5 a custom-model default less safe than its base is refused at resolve time with the
  message of section 7.3. Test: unit.
- [ ] CONF-4 [C5, E1] A mission setting changed in flight to a less-safe value takes effect only after the
  operator, or a supervisor laptop where section 16.2 allows it, confirms the exact value (N-20). Test:
  handler tests (C5 mechanism, E1 settings).
- [ ] CONF-5 [C5] A standby that becomes the authority sends no new plan, start or re-task until the operator
  or a supervisor laptop confirms its less-safe ground values; LAND ALL, Land, RETURN ALL, return releases
  and landing clearances are never gated (N-5, N-20). Test: multi-ground test (takeover during RETURN ALL:
  every waiting drone still gets its release).
- [ ] CONF-6 [C5] Verified simulated units need no confirmation; the rehearsal preset and the shared launcher
  fixture pass unchanged. Test: launcher tests.

## Test-adapter facts (FACT, user direction)

- [ ] FACT-1 [A1] Every Tello-coupled tuning row of `parameters.md` (32 rows) has `edit: fact`, the note
  "test-adapter fact, read-only, not operator-facing", and `confirm: none`, except the two safety switches
  (rows 38 and 39), whose off value is `less_safe`. Test: registry test.
- [ ] FACT-2 [A1, A5, C5] No fact row is offered to change, tune or calibrate anywhere: not as a launcher or
  dashboard control, not in a params or inventory file schema (the loaders refuse fact ids), not as an
  "uncalibrated" or threshold warning in a preflight message, and not in a Start confirmation. A check
  that uses a fact may still report its result (design section 7.5). Exempt: #207's launcher warning about
  an inherited `FLIGHTRISK_HOVER_TEST`, and the Start listing of a safety switch turned off (FACT-4)
  (N-16). Test: a test that enumerates the launcher and dashboard controls, the file schemas, the preflight
  message catalogue and the Start-confirmation payload builder.
- [ ] FACT-3 [A3] A fact row's existing environment variable still sets it, as a developer override
  logged at start and shown with source `env`, only on a test-adapter unit or a verified simulated unit; a
  production unit ignores it with a log line naming the variable (N-10). Test: unit.
- [ ] FACT-4 [C5] A safety switch among the facts (`tello.temp_refuse_c` off, `sensing.turn` false) turned
  off by an override is listed in the Start confirmation and must be confirmed with its exact value
  (N-10). Test: payload and handler tests.

## Roles (ROLES, H-4)

- [ ] ROLES-1 [C3] The role comes from the credential alone. Test: connection tests with and without a
  `client` claim.
- [ ] ROLES-2 [C3] Every Socket.IO event and HTTP route is in the role table; a missing entry fails a test
  that enumerates handlers and routes. Test: that test.
- [ ] ROLES-3 [C3] The event-by-role table of section 16.2 holds for every role and kind (AC-2): observers
  land only from a phone, or from a dashboard view whose token has the land right (R-3). Test: one allowed
  and one refused test per cell class.
- [ ] ROLES-4 [C3] The operator key is accepted only on loopback; elsewhere it gives observer rights and a
  "pair this device" message. Test: connection tests.
- [ ] ROLES-5 [C3] On connect the ground sends `session` with the role and allowed events. Test: handler test.
- [ ] ROLES-6 [C3] Keys and tokens never appear in logs, presets, launcher-written files or the vault;
  `devices.json` holds only salted hashes. Test: secret scans.
- [ ] ROLES-7 [C3] Pairing creates a per-device token with role and kind; revocation takes effect on the next
  event. Test: handler tests.
- [ ] ROLES-8 [C3] A supervisor token is refused over cleartext from a non-loopback address. Test: connection
  test.
- [ ] ROLES-9 [C3] LAND ALL and per-drone Land are allowed to every authenticated session that holds the
  land right (read from the credential at connection and cached), and never fail closed: with a role table
  that raises, both still go out. Test: that test.
- [ ] ROLES-10 [C3] Pairing is bound to the ground's certificate: the QR code is shown only on the ground
  host, the device pins the fingerprint before it sends the code, and a pairing over a connection whose
  certificate does not match is refused (N-14). Test: pairing tests with a matching and a mismatched
  certificate.
- [ ] ROLES-11 [C3] A cleartext observer session receives no imagery (reference photos, crops, frames),
  only state, positions and text (R-4). Test: emission tests per transport.
- [ ] ROLES-12 [C3] A dashboard observer token has no land right by default; the operator can grant it per
  token, and the change takes effect on the next event (R-3). Test: handler tests.

## Operator UI (UI)

- [ ] UI-1 [C2] RETURN ALL and LAND ALL as section 17 (AC-9.1). Test: DOM tests; browser check.
- [ ] UI-2 [C2] A client that cannot reach the ground says nothing was sent and points to the local land
  keys. Test: UI test.
- [ ] UI-3 [C2] Drone cards show home and distance, uncertainty (a number; never an "uncalibrated" label or a calibration prompt), reserve,
  state, countdowns and sources (spec 9.5). Test: payload tests.
- [ ] UI-4 [E1] The match alert panel as spec 9.3 with the ambiguity mark. Test: payload and handler tests.
- [ ] UI-5 [C5] The Start confirmation lists marks, headings, sensing mode, return altitude and threshold,
  every non-default safety value with its source (never a fact) and the on-find policy (spec 9.4). Test:
  payload test.
- [ ] UI-6 [F1, F2, F3] Dashboard and apps show the same states, homes and reasons; app reducers render a
  reason from the fixture event synchronously; the ground emits it within 2 s (AC-6.5, AC-9.2; M-9). Test:
  fixture, reducer and emission-timing tests; one device check.

## Tasks and matching (TASK)

- [ ] TASK-1 [D4] No task goes to a unit lacking a required capability; a thermal sweep without thermal units
  stays unassigned with a reason per unit (AC-5.1). Test: unit; SITL.
- [ ] TASK-2 [D4] Hard filters are never overridden. Test: unit.
- [ ] TASK-3 [D4] Ranking by bands (positioning class, 10-point battery buckets, 30 s transit buckets, idle,
  not blocked, unit id); tasks matched by priority, creation time, id; running tasks never reassigned; every
  match records its input snapshot (M-2). Test: unit; replay test from a snapshot.
- [ ] TASK-4 [D4] One task per unit; a sensing unit takes appended work only after its own. Test: unit.
- [ ] TASK-5 [D4] The plan explains every choice and refusal per unit. Test: unit.
- [ ] TASK-6 [D4] A thermal flag that may be a person stays "needs confirmation" (AC-5.2). Test: unit.
- [ ] TASK-7 [D4] A lost unit's tasks follow section 12; a unit with a positioning fault or an
  autopilot-started return has its tasks re-matched (MAV-6, MAV-7); re-tasking never sends a takeoff
  (AC-5.3). Test: fake.
- [ ] TASK-8 [D4] An all-Tello indoor fleet gets today's zones and lanes for 1 to 4 Tellos. Test: snapshots.
- [ ] TASK-9 [D5] Speed and spacing follow the rule of 14.4 (AC-5.9). Test: unit; SITL with a throttled test
  detector.
- [ ] TASK-10 [D3] Field missions are planned on the dashboard from GPS-anchored areas with obstacle tops.
  Test: dashboard tests; SITL two-vehicle mission.
- [ ] TASK-11 [D5] The edge reports `det_hz` in the detection-profile telemetry object; `set_speed` changes
  speed mid-lane (the Tello at its next step); the footprint of a level camera and the initial rate follow
  14.4 (M-6). Test: unit; fake.
- [ ] TASK-12 [D4] The operator can accept an area as unsearched; it is logged and shown (M-12). Test:
  handler test.

## On-find flows (FIND)

- [ ] FIND-1 [E1] Per-target references, thresholds and scores; attribution to the best target, ambiguous
  when the top two are within `find.ambiguity_margin`; re-attribution until Confirm, then locked (AC-5.4,
  M-4). Test: unit.
- [ ] FIND-2 [E1] Only a human Confirm marks a target found (AC-5.5). Test: unit.
- [ ] FIND-3 [E1] Continue and End search; the on-find policy runs; changes logged (AC-5.4). Test: unit; fake.
- [ ] FIND-4 [E1] A confirmed match no longer lands every fleet drone. Test: recovery tests.
- [ ] FIND-5 [E1] Finder keeps watch; Not verified starts the local search when on. Test: fake.
- [ ] FIND-6 [E2] Converge viewpoints at no less than the approach distance, separated, never stacked; fewer
  with a reason (AC-5.6). Test: unit; SITL.
- [ ] FIND-7 [E2] Viewpoint evidence and "k of N"; the verification window lapsing resumes drones and leaves
  the alert open. Test: unit.
- [ ] FIND-8 [E3] Local search radius, cut and stop conditions; a new sighting resets it (AC-5.7). Test: unit;
  SITL.
- [ ] FIND-9 [E3] Share of the disc searched; re-planned as r grows. Test: unit.
- [ ] FIND-10 [E4] Escort refused indoors with the reason (AC-5.8). Test: unit.
- [ ] FIND-11 [E4] Stand-off, altitude, separation; allowed zones exclude other drones' running task tubes;
  stop at a limit (M-3). Test: unit; SITL.
- [ ] FIND-12 [E4] Hand-off at return threshold + margin; the escort leaves only when the relief's detections
  are attributed to the same target and geolocate within the combined uncertainty for
  `escort.handoff_frames` frames; otherwise it returns at its threshold with an alert (M-3). Test: SITL with a
  moving test subject and a bystander.
- [ ] FIND-13 [E4] Lost from view: hold, alert, local search; resume only after a confirmed new sighting.
  Test: fake; SITL.
- [ ] FIND-14 [E4] An escort setpoint older than 2 s holds the drone; setpoints are refused while landing.
  Test: fake.
- [ ] FIND-15 [E1] Mission settings change in flight except approach distance and escort stand-off and
  altitude; changes logged (CONF-4 for less-safe ones). Test: unit.
- [ ] FIND-16 [E1] One embedding set per target; the ground scores every detection against every target; no
  edge wire change. Test: unit.
- [ ] FIND-17 [E2] Converge pre-selects the N nearest eligible drones by distance (spec 5.6), changeable,
  under the hard filters. Test: unit.
- [ ] FIND-18 [E4] Escort ends when the operator ends it, the search ends, or no escort can continue. Test:
  unit.
- [ ] FIND-19 [E4] With the ground gone, an escort returns only if its task corridor reaches home, else lands
  after the wait (documented). Test: fake.
- [ ] FIND-20 [E1] The panel states that false alarms grow with the number of targets. Test: payload test.

## The default switch in four steps (SW, M-8)

- [ ] SW-1 [C4] Warn-only release: the launcher, the dashboard, every edge and `scripts/field_fleet_ground.py`
  show and log every value C5 will refuse or ask to confirm, naming the value, its source and the bound or
  reason; nothing is refused. Test: a test that runs every C5 refusal case under C4 and gets a warning.
- [ ] SW-2 [C4] Setting `FLIGHTRISK_LINK_LOSS_LAND_S` logs that its meaning changes at C7 and names
  `FLIGHTRISK_LINK_LOSS_WAIT_S` and `FLIGHTRISK_LINK_LOSS_ACTION`; its value still sets
  `link.loss_detect_s` (decision R-1). Test: unit.
- [ ] SW-3 [C6] The field takeoff minimum becomes 40 % and constraint K-3 holds for the field profile
  (decision R-2). Test: registry test.
- [ ] SW-4 [C5, C6, C7] Each step can be reverted on its own: C6 and C7 change only resolved defaults and
  their golden-file rows; C5 only bounds, precedence and confirmations. Test: review of each diff against
  section 27.

## Change control and earlier round 2 findings (R2, TAG)

- [ ] TAG-1 [A1] Every "from <PR>, section 27 item N" tag in `parameters.md` names an item of `design.md`
  section 27 listed under that PR (H-11). Test: a parser test over both files.
- [ ] R2-B1 [C5] A less-safe custom default needs the Start confirmation; a hand-started dashboard shows it and
  refuses Start until confirmed; a less-safe ground value from a file needs `--confirm-less-safe`. Test:
  dashboard start tests without a launcher.
- [ ] R2-B2 [A4, A5] The dashboard reads `FLIGHTRISK_LAUNCHER_PARAMS` as its launcher layer; A5 merges after
  A3 and A4. Test: dashboard start test with the variable.
- [ ] R2-B3 [C5] The rehearsal preset's geofence is 450 cm; simulated units need no confirmation. Test:
  launcher tests.
- [ ] R2-B4 [A1] Every generic value equals today's MAVLink value or carries a tag. Test: golden-file test.
- [ ] R2-N6 [A1] The safety-value change log's latest entry holds the SHA-256 of the table, the golden file
  and the allowlist, and a test recomputes them (M-9). Test: that test.
- [ ] R2-N7 [A4] `describe_unit` retries at 2, 4, 8 and then every 30 s; Plan names the unit it waits for.
  Test: unit with a silent fake edge.
- [ ] R2-N8 [A1] Every operator text comes from the message catalogue in one of three forms. Test: render
  test.
- [ ] R2-N9 [A1] `MIN_START_BATTERY` stays importable as an alias. Test: import test.


# #197 core requirements (carried from PR #202 round 1, re-tagged by phase)

### Prerequisite (PRE)

- [ ] PRE-1 [before A1] #207, #198 (without its depth-threshold control, dashboard depth display and
  telemetry field, "not calibrated" warning and messages naming environment variables), #208, #216
  (sidestep off by default) and #213 (mavsdk pin, Windows link) are merged before A1 starts; #211, #205 and
  #212 are in the base `82ec697`; #208 keeps `MIN_START_BATTERY` as an alias of the single battery minimum,
  so the golden file pins it (design ASSUMPTION A1, round 2 N9). Test: A1's golden-file test fails on a
  base without them.

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
  of `hardware-profiles-detail.md` section 8.1 (still current for this item).

### Flight parameter registry and golden file (PAR)

- [ ] PAR-1 [A1] The registry has exactly the 160 rows of `parameters.md` (rows 53 and 85 removed); ids
  are unique; every id passes the fleet protocol's private-key screen
  (`flightrisk/fleet_protocol/checks.py:255-266`) (N-8). Test: unit test.
- [ ] PAR-2 [A1] For every row and every profile column, the default lies within the bound, and a
  read-only row has `lo == hi`. Test: unit test.
- [ ] PAR-3 [A1] (F6) `tests/golden/flight_params.json` holds, for every id and profile, the literal
  `default`, `lo`, `hi`, `safer`, `confirm`, `edit`, `off_value`, `safety_switch` and `origin`, and for
  every environment alias its `today_accepts`. `table.py` equals the golden file value by value. Every
  `origin: today` value equals the constant it replaces in the code at the A1 base; every
  `origin: changed` value names a design section 27 item. Test: unit tests.
- [ ] PAR-4 [A1] (F18) The `tello` bounds of `move.min_cm` and `move.max_cm`, and the value of every
  `tello_sdk.*` row (rows 87 to 110), equal the constants the Tello driver uses. Test: unit test that
  imports the driver constants.
- [ ] PAR-5 [A1] Canonical and alias environment names are unique across rows; every flight
  `FLIGHTRISK_*` variable and flag named in design section 27 maps to exactly one row; a room alias is
  never read for a field model and a field alias never for a room model. Test: unit tests.
- [ ] PAR-6 [A1] Every constraint K-1 to K-18 of `parameters.md` section 1.1 holds for every profile's
  defaults, except K-3 for the generic profile, which holds from C6 (N-8). Test: unit test (C6 extends it).
- [ ] PAR-7 [A1] `docs/flight-parameters.md` is generated from the registry and a test fails when it
  differs.
- [ ] PAR-8 [A3] `EnvelopeLimits`, `ROOM_LIMITS`, `FIELD_LIMITS` and `limits_for` are derived from the profile columns, and every public constant of `flightrisk/drone/safety.py` keeps its name and value in phase A (the 450 cm geofence default arrives in C5). Test: unit test; the existing safety tests pass unchanged.
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
- [ ] RES-9 [C5] `FLIGHTRISK_MANUAL_ORPHAN_LAND_S=10` is refused at dashboard start with the standard
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

- [ ] INV-1 [A2] `inventory.json` version 1 is read and written with the schema of
  `hardware-profiles-detail.md` section 8.1; writes are atomic; a file that exists but cannot be read is refused, never replaced by an empty
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
- [ ] INV-8 [D4] (F17) Plan decides each unit's role and pattern from its reported `gates`
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
- [ ] FEA-6 [C5] Switching off `forward_obstacle_sensing` needs the launcher confirmation, and
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
- [ ] FEA-9 [D4] A bird's-eye sweep task is matched only to units with `birdseye_search` on and available, in the sweep band; its lane spacing comes from the nadir swath and its detection profile is `top_down`; head-on tasks run beside it in the close band. Test: matcher and coordinator unit tests.
- [ ] FEA-10 [A2, A3] (F2) On a MAVLink unit, `forward_obstacle_sensing` is available only with
  `--source camera` or `url` and a level mount, and never depends on the drone declaring
  `STEPPED_SENSING` (which `MavlinkFleetDrone` does not, `mavlink_fleet.py:376-385`). Test: unit tests
  for each `--source`.
- [ ] FEA-11 [C1, A4, D4] (F16) Each feature's "when unavailable" behaviour of `hardware-profiles-detail.md` section 9.2
  holds: refuse the start, refuse the takeoff, refuse Plan, or degrade with the banner text. Test: one
  test per feature in the PR that owns that behaviour (start and takeoff in C1, Plan in D4,
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
  older handler's validation accepts the extra keys (ASSUMPTION A6). Test: edge message test and
  ground handler test.
- [ ] SEAM-8 [A3] (F8) One detection pipeline runs per unit: `select_pipeline` returns exactly one
  profile for the unit's search camera. Test: unit test with a dual-sensor unit.
- [ ] SEAM-9 [XCP] No file under `flightrisk/` or `scripts/` outside the Tello adapter files,
  `registry.py` and `backends.py` imports a Tello module, compares against a Tello adapter or model name, or
  uses a Tello-only constant, except the named permanent exemptions; the remaining violations are a baseline
  that may only shrink. Test: `tests/test_tello_coupling.py`.
- [ ] SEAM-10 [A3] Runtime code gets today's accepted environment and flag ranges from
  `flightrisk/params/today.py`, which equals `today_accepts_from_golden` of the golden file exactly, so
  adopting `resolve_unit` keeps today's range checks. Every runtime call of `resolve_unit` passes today's
  accepted ranges from `params/today.py`, never `None`. Test: `tests/test_param_today.py`, which lands with
  A3's `flightrisk/params/today.py` and also runs an AST check over `flightrisk/` that every `resolve_unit` call
  names the `today_accepts` keyword, that its value comes from `flightrisk.params.today`, that it is not a
  literal `None`, and that the call has no `**` arguments; the check follows import aliases and wrappers that
  forward a `None` default.

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
  telemetry test suites pass with only the edits of design section 27. Test: the existing suites plus
  a table test of resolved defaults.
- [ ] EDGE-6 [A3] An edge refuses to start when its model's scale, adapter or frame path does not
  match its backend. Test: unit test.
- [ ] EDGE-7 [A3] Every existing flight environment variable and flag, set to a valid non-default
  value, changes the corresponding resolved value with source `env` or `cli`. Test: table-driven unit
  test over all aliases and flags.

### Protocol (PROTO)

- [ ] PROTO-1 [A2] (F5) `describe_unit` is in a new `QUERY_TYPES` set and not in
  `CONFIG_COMMAND_TYPES`; the query and its `ack` have validators; the ack shape is that of
  `hardware-profiles-detail.md` section 12.2 (with `ap_link_loss` added, MAV-19), with `unit.link` holding only the link kind; every key passes the private-key screen;
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
- [ ] GND-3 [A4, D4] (F7) The coordinator uses each unit's move range instead of `TELLO_MIN_MOVE_CM` and
  `TELLO_MAX_MOVE_CM`, and default plans for 1, 2, 3 and 4 Tellos are unchanged. Test: unit test with a
  field unit; plan snapshots for 1, 2, 3 and 4 Tellos taken on the A4 base and compared after.
- [ ] GND-4 [A4] `fleet_patterns` zone slots equal the coordinator's (fixed by #208); A4 only reads them through the registry. Test: unit test.
- [ ] GND-5 [A4] A mission mixing environments is refused at Plan (moved from D1, nitpick 12), naming each unit from the wrong environment, and indoors every unit without a measured indoor profile (spec AC-4.2). Test: unit test.
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
- [ ] DSH-8 [C5] (F4) The dashboard ceiling card asks for confirmation before saving a ceiling raise
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
- [ ] LCH-4 [C5] (F4, F19) Start returns HTTP 409 with `needs_confirmation: "flight_params"` while any
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
- [ ] LCH-8 [C5] (F4) At Start the launcher writes the resolved ceiling, margin and slots into the
  ceiling file, and the Start plan shows their sources; with an inherited `FLIGHTRISK_CEILING_CM`, the
  file holds that value. Test: `ceilingfile` unit tests.
- [ ] LCH-9 [C5] (F9) Changing a unit's model to one with any wider bound needs the 409 confirmation
  listing every widened bound. Test: server test.
- [ ] LCH-10 [C5] (F19) `docs/launcher.md` and `docs/fleet-setup.md` carry the migration notes of
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
- [ ] MOB-9 [F2, F3] Both apps implement the pairing flow (scan the QR code, pin the fingerprint, then send
  the code), keep the token in the Android Keystore or the iOS Keychain, and connect only over `wss`
  pinned to the fingerprint; Android adds a pinned trust manager for the ground's certificate, since it
  trusts only system authorities today (`docs/app-store-compliance.md:219`) (N-14). Test: Android and iOS
  unit tests (a mismatched certificate is refused; the token is never in plain preferences).
- [ ] MOB-8 [dropped] The Kotlin literal check covered the direct Tello stack, which is a spec non-goal.

### Guard test (GRD)

- [ ] GRD-1 [A1] (F11) `tests/test_param_literals.py` applies rules A to F of design section 22 to
  the files in scope (including `flightrisk/dashboard/*.py` and `flightrisk/launcher/*.py`), and every
  finding is in `tests/param_literal_allowlist.py` with a category, a reason and its exact count.
  Test: the test itself.
- [ ] GRD-2 [A1] An allowlist key whose count no longer matches fails the test. Test: the test
  itself; each migration PR deletes or lowers the entries it migrates.
- [ ] GRD-3 [A1] The checker reports each planted literal of design section 22 in a temporary file,
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

- [ ] CMP-1 [A1 to A5] Phase A changes no runtime behaviour: the full suite passes with only the assertion edits design section 27 lists for A5, and no runtime path sends `return`, `escort_setpoint` or `on_complete: return`, enforces a target bound, or flips a default. Test: the full suite; a grep-based test for call sites.
- [ ] CMP-2 [C6, C7] With no new setting, every resolved value equals the golden file's, except the defaults design section 27 lists for C5, C6 and C7. Test: the suites; SITL before C6 merges; the AC-6.6 hardware runs on the edge laptop before C7 merges.
- [ ] CMP-3 [A1, C5] (F10) `tests/test_param_parity.py` probes every environment alias
  and flag at both ends of today's range and the new bound, plus a typical and a malformed value, and
  asserts that the resolver accepts exactly what today's code accepts, except the probes listed in
  `PARITY_EXCEPTIONS`, each naming its design section 27 item. Test: the test itself.
- [ ] CMP-4 [every PR] (F10) Each PR edits only the existing assertions design section 27
  lists for it. Test: review of the diff against the list; a PR that needs another edit stops and
  splits the change out.