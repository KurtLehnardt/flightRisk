"""The ledger of modules allowed to import ``flightrisk.params`` or ``flightrisk.hardware`` at run time.

Each adopting PR adds its own paths to its phase section in the ledger chunk of that PR, and no other
PR edits that section. ``tests/test_param_change_control.py`` fails on any runtime import of either
package from a path named in no section, so an unnamed adopter cannot land. It also fails when a named
path no longer exists or no longer imports either package, so the ledger holds no dead entries.
Paths are posix paths under ``flightrisk/``. Every section is empty until its phase adopts the packages.
"""

from __future__ import annotations

PARAM_ADOPTERS: dict[str, frozenset[str]] = {
    "X-core": frozenset(),
    "X-legacy": frozenset({"flightrisk/drone/mavlink.py"}),
    "A3": frozenset({
        "flightrisk/edge_resolution.py",
        "flightrisk/drone/safety.py",
        "flightrisk/drone/flight_guards.py",
        "flightrisk/drone/temp_preflight.py",
        "flightrisk/drone/obstacle_sensor.py",
        "flightrisk/drone/lane_flight.py",
    }),
    "A4": frozenset(),
    "A5": frozenset(),
    "B1": frozenset(),
    "B2": frozenset(),
    "B3": frozenset(),
    "B4": frozenset(),
    "D4": frozenset(),
}
