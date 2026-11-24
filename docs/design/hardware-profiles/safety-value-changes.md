# Safety-value change log

This log is append-only. Each entry names the value, the old and new value, the reason, the item of
`design.md` section 27, and the SHA-256 of `flightrisk/params/table.py`, of
`tests/golden/flight_params.json` and of `tests/param_literal_allowlist.py` after the change.
`tests/test_param_change_control.py` recomputes the three hashes and fails unless they equal the
latest entry, which is the last entry in this file.

Each entry also records `SHA-256 previous`: the SHA-256 of this file's UTF-8 text before the entry's
`## ` heading. The test checks every link of that chain, so editing or deleting an older entry
fails. The test also pins the hashes of the A1 baseline entry. The chain catches an edit to an old entry
unless every later hash is also recomputed; CODEOWNERS review of this file is the control for that case.

## A1 baseline

- Value: none changed.
- Old and new: not applicable.
- Reason: the registry, the golden file and the literal allowlist are added with today's values.
- Section 27 item: none.
- SHA-256 previous: c69c2a1f30b24c0e4d06525c75ffdc744d20e6df4bf39744ec46aaacf35f650c
- SHA-256 table: f949df64f14cee5c1c025d683cabea1d56a493941db05c214c33f7deac628378
- SHA-256 golden: 7403c28493386027b0c383326d30d2bb2645fa54c154e289276f9246289d2504
- SHA-256 allowlist: 49966ba5ce72cc90ec17e4890d171c9a67078dec3c69ea91c391f5569b39e750

## A2 return-command wire caps

- Value: none changed.
- Old and new: not applicable.
- Reason: A2 adds the return-command wire caps to the protocol constants and allowlists them.
- Section 27 item: none.
- SHA-256 previous: ef1d4ca66102f52f0090d56ea1bd9fdb6b3d2dc7782739409dc61f68d30953e9
- SHA-256 table: f949df64f14cee5c1c025d683cabea1d56a493941db05c214c33f7deac628378
- SHA-256 golden: 7403c28493386027b0c383326d30d2bb2645fa54c154e289276f9246289d2504
- SHA-256 allowlist: 448848fa34bc7ea6dc130b3b0c423bb48ce68a5303e665f68a00278e173cc79e

## PR #227: ArduCopter 4.7 fence altitude frame

- Value: `_AP_FENCE_ALT_FRAME_ABOVE_HOME` in `flightrisk/drone/autopilot.py`, the only accepted `FENCE_ALT_MAX_TP`.
- Old and new: not checked before; now 1 (above home). Any other value is refused, and so is an unreadable
  `FENCE_ALT_MAX_TP` unless the vehicle reports only the Copter 4.6 name `RTL_ALT`.
- Reason: ArduCopter 4.7 added `FENCE_ALT_MAX_TP` (0 above sea level, 1 above home, 2 above the EKF origin,
  3 above terrain; tag Copter-4.7.3, `libraries/AC_Fence/AC_Fence.cpp` lines 168-173). The check reads
  `FENCE_ALT_MAX` as metres above home, so any other frame could let the autopilot fence sit above the
  ceiling or the 120 m cap. The allowlist gains this ArduPilot enumeration value.
- Section 27 item: none listed; it is of the kind in item 6 (B4, newly refused autopilot values).
- SHA-256 previous: fdb6b42784beff334a555dd0cd69f6c17b594fc66449c3e5026c12586867efa5
- SHA-256 table: f949df64f14cee5c1c025d683cabea1d56a493941db05c214c33f7deac628378
- SHA-256 golden: 7403c28493386027b0c383326d30d2bb2645fa54c154e289276f9246289d2504
- SHA-256 allowlist: 7b4539efd7be798d98e11aa03af4a962acab92f084b5e7d020cbe47b9a8904db

## PR-A3.3: safety, envelope, flight guard and temperature defaults from the registry

- Value: none changed.
- Old and new: not applicable.
- Reason: PR-A3.3 moves 27 + 11 deferred allowlist entries to registry lookups with no value change: 27 in
  `flightrisk/drone/safety.py` and 11 in `flightrisk/drone/flight_guards.py` and
  `flightrisk/drone/temp_preflight.py`. Each constant now reads its registry row and keeps its value and
  type, so the allowlist only loses those 38 entries.
- Section 27 item: 47 (A3). A production unit ignores the Tello flight-guard and temperature overrides,
  with a log line naming each variable.
- SHA-256 previous: 79c17994057de59fb0abd4a575c978dad863b81ec39efdb6f9d0432d66acb710
- SHA-256 table: f949df64f14cee5c1c025d683cabea1d56a493941db05c214c33f7deac628378
- SHA-256 golden: 7403c28493386027b0c383326d30d2bb2645fa54c154e289276f9246289d2504
- SHA-256 allowlist: fe448eec579b0074675a2081053542143105f710da82ab86ecae76f4a598b1c2

## XL: legacy MAVLink limits from one params constant

- Value: none changed. The legacy `MavlinkController` declares `LEGACY_MAVLINK_MIN_MOVE_CM`,
  `LEGACY_MAVLINK_MAX_MOVE_CM` and `LEGACY_MAVLINK_MAX_ALTITUDE_M` from `flightrisk/params/legacy.py`.
- Old and new: unchanged, 10 cm, 50000 cm and 120 m.
- Reason: the three `DroneCapabilities` literals of the legacy `MavlinkController` moved into
  `flightrisk/params/legacy.py`, so the allowlist drops their three legacy entries. The 120 m is the legacy
  controller's own declaration, not row 8 and not the generic_multirotor airframe limit that section 27 item 53
  removes.
- Section 27 item: none.
- SHA-256 previous: 2c084fb72e228cdb48ed6e2da64b4252c8fadb8795afbb8cd299bdf0128d35a9
- SHA-256 table: f949df64f14cee5c1c025d683cabea1d56a493941db05c214c33f7deac628378
- SHA-256 golden: 7403c28493386027b0c383326d30d2bb2645fa54c154e289276f9246289d2504
- SHA-256 allowlist: dd1af99c5e3615ed79b51d9efbed782d2f3abf41cd3855f08831cdde67c68621

## A3 item 53 part 1: the regulatory altitude cap rows and an unknown airframe maximum

- Value: row 8 `envelope.max_altitude_cm` for `generic_multirotor`; new rows 163
  `regulatory.altitude_cap_cm` and 164 `regulatory.altitude_cap_on`.
- Old and new: row 8 generic default 12000 cm becomes unset (unknown), its bound 12000-12000 becomes
  1000-60000 cm (a new safety value: the range an inventory may state for the airframe maximum), and its
  edit read_only becomes file_env. Row 163 is 12000 cm, read-only. Row 164 is a safety switch, default
  true; its off value is refused from a file or the inventory, has no environment name or flag, and from
  the launcher counts only when the launcher's `confirmed` object names it. Tello values are unchanged.
- Reason: the 120 m came from the FAA rules (14 CFR 107.51), which may not apply to a search-and-rescue
  mission. The cap becomes a separate regulatory row that is on by default, and the 120 m stops counting
  as a known airframe maximum. With the cap off the limit is the known airframe maximum, and a unit with
  no known maximum cannot turn the cap off (`flightrisk/params/altitude.py`).
- Section 27 item: 53 (A3).
- SHA-256 previous: 232c2d910939388ccb34883b2e83f3605c60aedafed6a69da687e85872a5bab6
- SHA-256 table: 368dff8eb8f05ff05ba5ad8b850e07ed9e731a6d9a2419556cef8e80ed3f20f8
- SHA-256 golden: 1e201af91affbe8345b11b4fca93c128541f119b1a138e23800ffbb09788f3f2
- SHA-256 allowlist: dd1af99c5e3615ed79b51d9efbed782d2f3abf41cd3855f08831cdde67c68621

## PR-A3.5: Tello adapter and fake constants from the registry

- Value: none changed.
- Old and new: not applicable.
- Reason: PR-A3.5 moves 29 + 3 + 2 deferred allowlist entries to registry lookups with no value change: 29 in
  `flightrisk/drone/tello.py`, 3 in `flightrisk/drone/tello_bound.py` (the command, state and video ports) and 2
  in `flightrisk/drone/fake.py` (`move.min_cm` and `move.max_cm`). Each constant keeps its name, value and type.
  One of the 29 is the `max_altitude_m=10` of `TelloController.__init__`, now derived from row 8
  (`envelope.max_altitude_cm`, 1000 cm in the Tello column) as whole metres, an int. It is FlightRisk's stricter
  10 m ceiling, not an SDK limit. Two `tello.py` entries are reclassified from deferred to sdk under section
  3.3, because they have no registry row: `_SETTLE_MIN_GAP_S` and `_SETTLE_BURST_GAP_S`. `_LAND_STALL_PACKETS` is
  FlightRisk's own land-resend threshold, not an SDK value, so it stays deferred for phase C1 (the return and
  landing state machine that owns terminal landing).
  The five module copies of the registry lookup are replaced by `model_default` in
  `flightrisk/params/defaults.py`.
- Section 27 item: none.
- SHA-256 previous: bc54b498c0348beb1e6391edbfad011c4fd54373ea69935773d8d262e61dc523
- SHA-256 table: 368dff8eb8f05ff05ba5ad8b850e07ed9e731a6d9a2419556cef8e80ed3f20f8
- SHA-256 golden: 1e201af91affbe8345b11b4fca93c128541f119b1a138e23800ffbb09788f3f2
- SHA-256 allowlist: 2d1f684e22577a885abe706fbcada993b9609871ac50cec3a5ff1ff329becaf4

## PR-A3.4: obstacle sensing, avoidance and lane constants from the registry

- Value: no registry value changed. One input is handled differently: a launcher `sensing.mode` of off
  (`FLIGHTRISK_LAUNCHER_PARAMS`) now refuses the start unless the launcher's `confirmed` object names it, and
  is applied with a warning when it does (design item 47).
- Old and new: launcher `sensing.mode` off without confirmation: old, the start was accepted and the launcher
  value ignored, so the drone sensed as usual; new, the start is refused. The environment variable
  `FLIGHTRISK_OBSTACLE_SENSING` keeps today's reader. No registry value has an old and a new value.
- Reason: PR-A3.4 moves 18 + 4 deferred allowlist entries to registry lookups with no value change: 18 in
  `flightrisk/drone/obstacle_sensor.py` and `flightrisk/drone/obstacle.py` (S13), and 4 in
  `flightrisk/drone/lane_flight.py` and `flightrisk/drone/lane_executor.py` (S15). Each constant now reads its
  registry row and keeps its value and type. Two entries are reclassified: `BlockedZone.heading_tol_deg` is
  internal (the zone geometry of the fake sensor, with no registry row), and the `LaneExecutor` default
  `obstacle_clear_votes=1` is a test double (the edge always passes its own clear votes). Three deferred
  entries stay, with their reasons re-tagged to C1: `OBSTACLE_AVOID_BUDGET_S`, `OBSTACLE_RELOOKS_PER_LANE`
  and `obstacle_takeoff_probe`, which have no registry row.
- Section 27 item: 47 (A3). A production MAVLink unit leaves the nine `FLIGHTRISK_OBSTACLE_*` fact overrides
  out of the resolution, with one log line naming each variable. The line says the edge reads it itself,
  because the edge's obstacle settings still honour these overrides (plan risk R6 leaves that to B1), and
  that they apply only when the unit senses obstacles with a camera. Switching obstacle sensing off is less
  safe, so a launcher off needs the launcher's confirmation, as a less-safe value from a file does (RES-11).
  A custom MAVLink model whose camera does not look level now gets no camera sensor (SEAM-4), so its takeoff
  needs the autopilot's own avoidance; this is the safe direction.
- SHA-256 previous: d50e8fdce6df26b154797d20568ad6e10c1b1aea6702f596227e44fc0a561153
- SHA-256 table: 368dff8eb8f05ff05ba5ad8b850e07ed9e731a6d9a2419556cef8e80ed3f20f8
- SHA-256 golden: 1e201af91affbe8345b11b4fca93c128541f119b1a138e23800ffbb09788f3f2
- SHA-256 allowlist: e9e1ca37715dda8b95da03ddefaeaccbddd481d3a56d74fcbd21c42ebe060887
