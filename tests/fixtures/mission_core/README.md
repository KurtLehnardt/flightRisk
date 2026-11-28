# Mission core fixtures

Golden vectors for the phone mission core. Kotlin (`mobile/mission`) and Swift
(`ios/FlightRiskGround`) read the same files, so the two implementations cannot drift.

- Generated files come from `scripts/mission_vectors/*.py`, driven by
  `scripts/make_mission_core_vectors.py --check`, which regenerates in memory and fails on any
  difference.
- Hand-authored spec files name the rule ids they cover.
- Tests reach this directory through the `flightrisk.fixturesDir` system property (Kotlin) and
  the package-relative path in the Swift fixture helpers.
- Every test transport goes through the loopback guard in the fixture helpers. It refuses any
  address other than 127.0.0.1, ::1 and 10.0.2.2.
