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
- SHA-256 previous: 2c084fb72e228cdb48ed6e2da64b4252c8fadb8795afbb8cd299bdf0128d35a9
- SHA-256 table: 368dff8eb8f05ff05ba5ad8b850e07ed9e731a6d9a2419556cef8e80ed3f20f8
- SHA-256 golden: 1e201af91affbe8345b11b4fca93c128541f119b1a138e23800ffbb09788f3f2
- SHA-256 allowlist: fe448eec579b0074675a2081053542143105f710da82ab86ecae76f4a598b1c2
