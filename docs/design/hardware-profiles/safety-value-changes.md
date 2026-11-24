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

## PR-A3.5: Tello adapter and fake constants from the registry

- Value: none changed.
- Old and new: not applicable.
- Reason: PR-A3.5 moves 29 + 3 + 2 deferred allowlist entries to registry lookups with no value change: 29 in
  `flightrisk/drone/tello.py`, 3 in `flightrisk/drone/tello_bound.py` (the command, state and video ports) and 2
  in `flightrisk/drone/fake.py` (`move.min_cm` and `move.max_cm`). Each constant keeps its name, value and type.
  One of the 29 is the `max_altitude_m=10` of `TelloController.__init__`, now derived from row 8
  (`envelope.max_altitude_cm`, 1000 cm in the Tello column) as whole metres, an int. It is FlightRisk's stricter
  10 m ceiling, not an SDK limit. Three `tello.py` entries are reclassified from deferred to sdk under section
  3.3, because they have no registry row: `_LAND_STALL_PACKETS`, `_SETTLE_MIN_GAP_S` and `_SETTLE_BURST_GAP_S`.
  The five module copies of the registry lookup are replaced by `model_default` in
  `flightrisk/params/defaults.py`.
- Section 27 item: none.
- SHA-256 previous: 232c2d910939388ccb34883b2e83f3605c60aedafed6a69da687e85872a5bab6
- SHA-256 table: f949df64f14cee5c1c025d683cabea1d56a493941db05c214c33f7deac628378
- SHA-256 golden: 7403c28493386027b0c383326d30d2bb2645fa54c154e289276f9246289d2504
- SHA-256 allowlist: 7bec38cc71d5a88bc7439d34a3294c1d9ba186aabc40cb0db388e36e8f542a42
