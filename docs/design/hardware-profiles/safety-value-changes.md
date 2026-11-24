# Safety-value change log

This log is append-only. Each entry names the value, the old and new value, the reason, the item of
`design.md` section 27, and the SHA-256 of `flightrisk/params/table.py`, of
`tests/golden/flight_params.json` and of `tests/param_literal_allowlist.py` after the change.
`tests/test_param_change_control.py` recomputes the three hashes and fails unless they equal the
latest entry, which is the last entry in this file.

Each entry also records `SHA-256 previous`: the SHA-256 of this file's UTF-8 text before the entry's
`## ` heading. The test checks every link of that chain, so editing or deleting an older entry
fails. The test also pins the hashes of the A1 baseline entry.

## A1 baseline

- Value: none changed.
- Old and new: not applicable.
- Reason: the registry, the golden file and the literal allowlist are added with today's values.
- Section 27 item: none.
- SHA-256 previous: c8e89f51c02c4d13781fe57e6da6f2b645f71012b10887c9534920084a24e430
- SHA-256 table: f949df64f14cee5c1c025d683cabea1d56a493941db05c214c33f7deac628378
- SHA-256 golden: 7403c28493386027b0c383326d30d2bb2645fa54c154e289276f9246289d2504
- SHA-256 allowlist: 9620d8432ea012b9c02189d4739909c64740f5ed3d680e07af32eb56f9c65743
