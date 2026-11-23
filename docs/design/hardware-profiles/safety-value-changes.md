# Safety-value change log

This log is append-only. Each entry names the value, the old and new value, the reason, the item of
`design.md` section 27, and the SHA-256 of `flightrisk/params/table.py`, of
`tests/golden/flight_params.json` and of `tests/param_literal_allowlist.py` after the change.
`tests/test_param_change_control.py` recomputes the three hashes and fails unless they equal the
latest entry, which is the last entry in this file.

## A1 baseline

- Value: none changed.
- Old and new: not applicable.
- Reason: the registry, the golden file and the literal allowlist are added with today's values.
- Section 27 item: none.
- SHA-256 table: c84bb69104020e30880836a33b4516241458f5741012c46070600111df99ecd8
- SHA-256 golden: 370daf6c2272f04e927336344a64073e1449648a5efca216bdc722922d918c84
- SHA-256 allowlist: 54b9cfd856504c91e9de6fccf41bcd1bc1bfd31c698a1a17fc904b81de907027
