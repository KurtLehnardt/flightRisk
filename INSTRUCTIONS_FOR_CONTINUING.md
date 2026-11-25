# Instructions for continuing the unfinished drone-agnostic branches

This file is committed, with the same content, on each of the four branches that were unfinished when the
drone-agnostic work was paused on 2026-11-24:

- `drone-agnostic/A4b`
- `drone-agnostic/B1a`
- `drone-agnostic/B1a--c3`
- `docs/fast-path-returns`

It is a hand-off note, not product documentation. **Delete it in the branch's last commit before the branch's PR
merges. It must never reach `main`.**

## The big picture

FlightRisk is becoming drone-agnostic. Real search-and-rescue units are PX4 or ArduPilot airframes over MAVLink,
and the Tello is a development and test adapter only. When the work is done, nothing outside the Tello adapter and
the registry assumes a Tello. A mission flies end to end on a MAVLink unit without loading any Tello module, and
units are matched to tasks by their declared capabilities.

The work was planned as 36 PRs in phases. Issue #252 is the entry point. It holds:

- the full ordered list of remaining work;
- the rules every PR must follow (safety invariants, pinned lists, the hash-chained `safety-value-changes.md` log,
  adopter caps, file ownership, git author and wording);
- the decisions the owner has already made;
- the one open owner decision (the battery return floor).

Read issue #252 before you start. The detailed chunk specs were archived in the owner's shared vault under
`Projects/FlightRisk/drone-agnostic-plans/`. That vault exists only on the owner's machines; this file repeats
what each branch needs.

These four branches sit on the critical path like this:

```
docs/fast-path-returns  (design docs: C1 and C2 before D4, return home at task end)
        |
drone-agnostic/B1a <- drone-agnostic/B1a--c3   (SITL tests pass on real ArduCopter and PX4)
        |
        +--> B1b, B1c (one real edge per vehicle, no-Tello import proof) --> B2, B3, B4 --> C1 --> C2 --> D4
                                                                                       ^
drone-agnostic/A4b  (ground plans each unit with its own values) --> A4c to A4f -------+  (C2a needs A4f)
```

By value for a MAVLink field deployment:

1. B1a matters most. Without it nothing verifies a MAVLink flight in simulation.
2. The docs branch unblocks the return-home work (C1 and C2).
3. A4b is behaviour-neutral groundwork for mixed and field fleets on the dashboard.

None of the four blocks a Tello-only demo.

## Rules that apply to every branch

The full list is in issue #252. The most important ones are:

**Before you start:**

- `main` has moved since the pause. Merge `origin/main` into the branch first (never rebase), then run the
  branch's targeted tests.

**Safety:**

- Never weaken a safety invariant or a refusal to make a test pass. The invariants are LAND ALL, return to home
  as the default, the geofence, the ceiling and altitude slots, the takeoff battery minimum and the autopilot
  failsafe parameter checks.
- Every change is behaviour-neutral unless it implements an item listed in
  `docs/design/hardware-profiles/design.md` section 27.

**Running tests:**

- Run tests from the repo root with the repo venv's Python as `python -m pytest` (flightrisk is not installed in
  the venv).
- If `tests/test_launcher_processes.py` or the ctrl-c teardown test in `tests/test_fake_fleet.py` fails in a full
  run, rerun it alone once before treating it as real. Both are load-sensitive.

**Git and wording:**

- Commit as `Kurt Lehnardt <12109228+KurtLehnardt@users.noreply.github.com>`, with explicit paths.
- Write in plain full sentences. Name machines by role only ("ground-station laptop", "edge laptop").

---

## Branch `drone-agnostic/A4b`

**What it is.** PR A4b of the A4 stack: the ground coordinator plans and gates each unit with that unit's own
resolved values instead of Tello constants. The values are the move range, the takeoff battery minimum, the
geofence, the altitude slot pair and the zone gap. It depends on A4a (#247, merged). The next PR, A4c, is built on
it. It is behaviour-neutral for Tello fleets: the plan snapshots under `tests/fixtures/plan_snapshots/` must not
change.

**Done so far.** Chunks F1, 1, 2, 3, F2 and 4 are merged into the branch (about 2,700 lines over 13 files):

- `flightrisk/ground_params.py` gives the ground one resolution of each unit's values.
- The coordinator uses each unit's move range, battery minimum, fence, slots and gap.
- The dashboard resolves each fleet drone's unit and refuses a refused unit at Plan.
- The map plan's area cap follows the fleet's units.
- The snap tolerance follows the units' minimum move, and the plan records a non-default altitude margin.

Two deviations from the plan were accepted:

- With `geofence_cm=None` the unit fence is not applied. The dashboard always passes one.
- A Tello unit with a constructor gap below 100 cm gets 100 cm. No production caller passes a smaller gap.

**Next steps.**

1. Merge `origin/main`. A trial merge at the pause was clean. The only failure was the expected ledger guard on
   `flightrisk/ground_params.py`, which step 2 fixes.
2. Do chunk A4b-5 (the ledger):
   - Add `flightrisk/ground_params.py` to the `"A4"` section of `tests/param_adopters.py`, within the A4 cap in
     `tests/test_param_change_control.py`.
   - Delete the coupling-baseline entry
     `('flightrisk/drone/coordinator.py', 'FleetCoordinator.plan', 'compare', 'TELLO_MIN_MOVE_CM')` from
     `tests/tello_coupling_baseline.py` if the code no longer makes that comparison. The alias definitions are
     stores, not findings, and `TELLO_MIN_MOVE_CM` and `TELLO_MAX_MOVE_CM` must stay importable from
     `coordinator.py`.
   - Remove the literal allowlist's deferred entries for code that no longer exists. If the allowlist bytes
     change, add a `safety-value-changes.md` entry naming item 47.
   - Grep the diff for any use of a 120 m altitude or `FIELD_LIMITS` that should now be the unit's
     `altitude_limit_cm` (item 53). Record what you find for A4d; do not fix it here.
3. Run these tests:
   - `tests/test_param_parity.py tests/test_param_golden.py tests/test_param_table.py
     tests/test_param_change_control.py tests/test_param_literals.py tests/test_param_today.py
     tests/test_edge_start_baseline.py tests/test_tello_coupling.py tests/test_ground_today_accepts.py
     tests/test_plan_snapshots_tellos.py tests/test_coordinator.py`;
   - then the full suite once.
4. Delete this file, then push and open the PR. Have one critic review it, because it is flight safety. The PR body
   names section 27 item 47 and `GROUND_NAMES` and lists the two accepted deviations above.

**After it merges.** A4c is next: the ground describes units through `describe_unit`, Plan is gated, and mixed
indoor and field fleets are refused. A4c needs protocol v2 leader mode. Agree with the C2a work which PR adds it
first.

---

## Branch `drone-agnostic/B1a`

**What it is.** PR B1a, a native SITL fleet harness for ArduCopter and PX4. It is test-only. Today the repo's SITL
integration tests fail on `main` against real SITL, and the causes are in the tests, not the product:

- ArduCopter's GUIDED mode needs a position before takeoff.
- A fixture holds port 14540.
- The second vehicle has system id 2.
- `test_goto_gps` passes a relative altitude where AMSL is needed.
- The ArduCopter-only tests skip too late.
- Stock SITL streams no telemetry without stream-rate defaults.

B1a fixes these. B1b and B1c, and every later MAVLink gate, build on it.

**Done so far.** Chunks B1a-5, 1, 2 and 4 are merged into the branch:

- The `scripts/start_sitl.sh` hint is fixed, and the docs list MAVLink as supported in simulation.
- `tests/integration/sitl_support.py`, `tests/integration/conftest.py` and their unit tests are added.
- `scripts/sitl_fleet.sh` (`up`, `down`, `status`, `gate`) is added, with checked-in ArduCopter stream-rate
  defaults. Each vehicle runs under a supervisor shell.
- `tests/integration/test_sitl_fleet.py` gives each vehicle its own system id and skips the ArduCopter-only tests
  before connecting.

Chunk B1a-4's own gates passed:

| Firmware | Result |
| --- | --- |
| ArduCopter 4.7.3 | 5 passed |
| ArduCopter 4.6.3 | 5 passed |
| PX4 | 3 passed, 2 skipped |

**Next steps.**

1. Bring in chunk B1a-3 from branch `drone-agnostic/B1a--c3` (see its section below). Merge it with `--no-ff` once
   it is verified.
2. A known teardown issue (it may already be handled by B1a-3):
   - The product's supervised `mavsdk_server` runs as a grandchild of pytest. `sitl_support.child_mavsdk_servers()`
     uses `pgrep -P` and misses it.
   - This is the likely cause of an `OSError 48` in `wait_port_released` at module teardown.
   - Fix it in `tests/integration/sitl_support.py`, which is test-only.
3. Do chunk B1a-6, which changes only `docs/sitl-demo.md`:
   - Add a section, "Native SITL fleet harness (`scripts/sitl_fleet.sh`)", covering the `up`, `down`, `status` and
     `gate` commands, the port, home and system-id map, the run directory and the refusal rules.
   - Add the install notes: the local `AP_FWVersion.h` patch (add the 4.7.3 case), and the PX4 v1.17.0
     `px4_sitl_sih` build with `CONFIG_MODULES_SIMULATION_GZ_MSGS=n`, `GZ_BRIDGE=n` and `GZ_PLUGINS=n`, then
     `make px4_sitl_sih`.
   - Add a dated table of the gate results, with counts only and no machine names.
   - The two-vehicle runs passed with system ids 1 and 2, so correct the system-id sentence near line 124 to say
     that each vehicle has its own system id and the edge is started with it.
   - Keep the strings `start_sitl.sh`, `--instance`, `amd64` and `Real hardware`, because
     `tests/test_sitl_scripts.py` checks them.
   - Done when `tests/test_sitl_scripts.py tests/test_docs_privacy.py tests/test_docs_pitch.py
     tests/test_tello_coupling.py tests/test_param_change_control.py` pass, and the PR changes no file under
     `flightrisk/`, no ledger file and no `safety-value-changes` file.
4. Run the PR gate on a Mac with the native SITL binaries:
   - Run the full suite.
   - Then run, one at a time, `scripts/sitl_fleet.sh gate --autopilot ardupilot --version 4.6.3`, the same with
     `--version 4.7.3`, and `scripts/sitl_fleet.sh gate --autopilot px4`. Each must end with `SITL GATE exit 0`.
   - Run an 8-vehicle `up`, `status` and `down` once.
   - If a gate is interrupted, stop its vehicles with `scripts/sitl_fleet.sh down --base-instance <n>`.
   - Export the venv Python as `PY` first. Otherwise the harness falls back to a `python3` without mavsdk.
5. Delete this file, then push and open the PR. One standard review is enough, because the PR is test-only.

**After it merges.** B1b generates the SITL fixtures by firmware and adds the CAP-6 fingerprints. B1c starts one
real edge per vehicle with `--model generic_multirotor` and proves through `python -X importtime` that no Tello
module loads. That proof is acceptance item 1.

---

## Branch `drone-agnostic/B1a--c3`

**What it is.** Chunk B1a-3 of PR B1a, stacked on `drone-agnostic/B1a` at `bc764f7`. It was written but not yet
verified or reviewed. It is not a PR of its own: it merges into `drone-agnostic/B1a`.

**Done so far.** It has three commits:

- `90c3e77`: make the SITL flight and coverage tests pass on every firmware;
- `c3601ba`: wait for the disarm after landing;
- `43ec79b`: allow for PX4 altitude drift in the landing wait.

**What B1a-3 must do.** It changes only `tests/integration/test_sitl.py` and a new
`tests/integration/test_sitl_coverage.py`.

- **Before connect.** The `sitl_available` fixture calls `sitl_support.wait_ready(address)` before creating the
  controller.
- **Takeoff.** A `_takeoff(ctrl)` helper retries `ctrl.takeoff()` every 2 s for up to 90 s while MAVSDK raises an
  `ActionError` whose result is FAILED or DENIED (matched by class name). It then waits up to 30 s for
  `altitude_msl` to reach 1.5 m above the ground altitude recorded at connect.
- **Landing.** A `_land(ctrl, ground)` helper waits up to 60 s for `altitude_msl` to stay within 0.5 m of the
  ground for 3 s, then up to 5 s for the automatic disarm.
- **`test_goto_gps`** passes the current `ctrl.state.altitude_msl`, not 10.0.
- **Coverage flight.** `TestSITLCoverageFlight` moves, unchanged in behaviour, into `test_sitl_coverage.py`, with
  `wait_ready` before its connect.
- **What stays.** The module fixture keeps `scope="module"` and its Cygwin comment.
  `_import_real_mavlink_controller` and `_sitl_address` stay in `test_sitl.py`.

**Next steps.**

1. Read the three commits against the list above. Confirm that no assertion was weakened and no file outside the
   two allowed ones changed.
2. Run the chunk's gates, one at a time. Each must end with `10 passed` for the two files and `SITL GATE exit 0`,
   and afterwards no `mavsdk_server` from this run may be left running:
   - `scripts/sitl_fleet.sh gate --autopilot ardupilot --version 4.7.3 --count 1 --base-instance 2 --files
     tests/integration/test_sitl.py,tests/integration/test_sitl_coverage.py`;
   - the same with `--version 4.6.3`;
   - the same with `--autopilot px4 --base-instance 2`.
3. If the gates pass, merge it into `drone-agnostic/B1a` with
   `git merge --no-ff drone-agnostic/B1a--c3 -m "Merge chunk B1a-3: ..."`. Delete this branch after the merge.
   This file then comes along with the merge, and the B1a PR deletes it.
4. If a gate fails, fix it on this branch, test-side only. If a traceback ends in a product file under
   `flightrisk/`, stop and report it rather than change the test to pass.

---

## Branch `docs/fast-path-returns`

**What it is.** A docs-only PR to `docs/design/hardware-profiles/{design,requirements,tasks}.md`. It records the
owner's decision that drones return home when the mission is complete, and it reorders the plan so that C1 (edge
returns) and C2 (ground returns) come before D4. It defines:

- **Item 43 without zones.** For an assignment without a `corridor`, the edge skips the straight-line-home check
  and logs `edge_rth_corridor_check_skipped`. Once a flight (takeoff to disarm) has had a corridor, a later
  assignment without one fails the check.
- **The task-end default.** For field units, `task.end_action` becomes `return` in C2. The Tello keeps `land`.
- **The fleet landing order.** Until D1, C2 sends every returning unit a `landing` record that treats the fleet as
  one area, lowest slot first. C1 writes it into the autopilot's landing delay.
- **The task-end fallback.** A unit that cannot take a task-end return lands at task end as today.

It was reviewed twice and checked mechanically against the returns contract version 2.

**Done so far.** Five commits up to `3a6529f`, including one merge of `origin/main`.

**Next steps.**

1. Merge `origin/main`.
2. Sync the docs to the returns contract version 3. That version is not written yet; issue #252 item 3 lists
   what goes into it. At minimum, add the following to the C1 and C2 text and to design.md section 10.8:
   - **Battery threshold.** The return threshold is user-configurable per drone, defaulting to 20 %. The owner
     must first settle the conflict with the rule `return floor >= critical + landing reserve`; see issue #252.
     Do not write a number the parameter table does not allow.
   - **Landing overflow.** ArduPilot caps the landing delay at 60 s, so units whose landing place would need more
     than that land at task end instead of waiting.
   - **The C1 owner's review items.** NEW-2: a battery, link-loss or geofence trigger during an operator-ordered
     return changes the return's cause, battery first, and a later hold cannot stop a battery return. NEW-3: a
     wait ordered during a battery return is nacked with `battery`. NEW-5: define what happens when the landing
     record changes in flight on a re-task. NEW-6: the fake drone's return behaviour and its fault hooks.
3. Run `tests/test_param_golden.py` (it parses the design docs), `tests/test_docs_privacy.py` and
   `tests/test_docs_pitch.py`.
4. Delete this file, then push and open the PR, and merge it with a merge commit. C1 and C2 code should not start
   before this merges and the C1 owner has agreed to contract version 3.
