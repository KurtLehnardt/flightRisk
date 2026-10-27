# FlightRisk one-page plan

FlightRisk is open-source (GPL-3.0-or-later) aerial search software for Utah's volunteer search and rescue teams. Target customer (ASSUMPTION: no contact yet): [Salt Lake County Sheriff's Search and Rescue Team](https://saltlakecounty.gov/sheriff/law-enforcement/search--rescue-team). Full budget, licensing and regulatory detail: [docs/roadmap-budget.md](roadmap-budget.md).

## Next milestones

- M1: fly a real aircraft at the altitude the benchmark picks and check the measured `min_person_px` (see [altitude-benchmark.md](altitude-benchmark.md)) holds in the field.
- M2: run the edge process onboard a Jetson instead of a laptop.
- M3: coordinate two or more aircraft with the existing fleet coordinator.
- M4: field trial with a Utah SAR team, using their real search areas.

## People and resources needed

- Today: one developer, solo (ASSUMPTION: no teammates recruited).
- A Part 107 remote pilot for the validation flights.
- One search and rescue volunteer as an advisor, to say what a usable search tool looks like.
- Hardware and fees as itemised in the roadmap appendix.

## Rough timeline

- Weeks 1 to 2: buy the quad, book the pilot, plan the flight from `flightrisk/drone/coverage.py`.
- Weeks 3 to 4: first validation flight, compare recall against the benchmark.
- Weeks 5 to 8: gimbal camera and Jetson integration.
- Weeks 9 to 12: two-aircraft flight and a demo for a SAR team.

## $5K milestone and $10K plan

- **$5,000** stands alone. It funds an open-source quad and a Part 107 pilot for the first real flight, validating the measured `min_person_px` threshold at the planned altitude.
- **$10,000** adds a SIYI A8 mini class gimbal camera, a Jetson Orin Nano Super, and a second airframe.
- Prices are mostly ASSUMPTION; the itemised tables and sources are in the appendix.

## After the hackathon (next 90 days)

- Personal commitment (ASSUMPTION: solo team): keep building on weekends and publish every benchmark result in the repo.
- Days 1 to 30: purchase, pilot, first flight.
- Days 31 to 60: onboard Jetson run.
- Days 61 to 90: ask the SAR team for a feedback session, with no promise of a partnership.

## Honest limits

Dense tree canopy defeats RGB and most thermal imaging, so the fit is open terrain (desert, canyon, meadow, snow), not forest. This plan needs no regulatory approval for the hackathon; real flights need a Part 107 pilot.
