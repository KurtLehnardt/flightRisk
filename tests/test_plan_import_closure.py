"""Importing the search and path helpers must not pull in the flight stack.

Each check runs in a fresh interpreter, because the test process has already
imported most of the package.
"""

from __future__ import annotations

import json
import subprocess
import sys

_PROBE = (
    "import sys, json, {module}; "
    "print(json.dumps(sorted(m for m in sys.modules "
    "if m.split('.')[0] in ('flightrisk', 'numpy'))))"
)

# What importing flightrisk.drone.search may load: the package, the drone
# package, search itself, and the two small modules it needs for the move range.
# The safety module reads its envelope values from the flight parameter
# registry (PR #243), so the registry table and its row types load too; they
# hold data only, not the flight stack.
SEARCH_ALLOWED = {
    "flightrisk",
    "flightrisk.drone",
    "flightrisk.drone.search",
    "flightrisk.drone.move_range",
    "flightrisk.drone.safety",
    "flightrisk.params",
    "flightrisk.params.spec",
    "flightrisk.params.table",
}
FORBIDDEN = {
    "flightrisk.drone.lane_executor",
    "flightrisk.drone.coverage_sim",
    "flightrisk.drone.fleet_plan_check",
}


def _loaded(module: str) -> set[str]:
    out = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module)],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(json.loads(out.stdout))


def test_search_loads_only_the_small_closure():
    loaded = _loaded("flightrisk.drone.search")
    assert loaded <= SEARCH_ALLOWED, sorted(loaded - SEARCH_ALLOWED)
    assert "numpy" not in loaded


def test_plan_paths_skips_the_flight_stack():
    loaded = _loaded("flightrisk.drone.plan_paths")
    assert not loaded & FORBIDDEN, sorted(loaded & FORBIDDEN)
    assert "flightrisk.drone.search" in loaded
