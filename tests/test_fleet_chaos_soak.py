"""Chaos soak: minutes of random link faults against the live fleet (marked ``slow``).

Deselected by default. Run it with::

    python -m pytest -m slow tests/test_fleet_chaos_soak.py

``FLIGHTRISK_SOAK_S`` sets the length in seconds (default 180) and
``FLIGHTRISK_SOAK_SEED`` the seed. The plan is a pure function of the seed, so
a failing seed reproduces the same fault timeline; only wall-clock scheduling
varies.

Ground node ``a`` and two fake edges use the protocol the code on main speaks.
The test asserts only what holds on main today: drones land when the heartbeat
watchdog expires, ``emergency_land_all`` reaches every healthy edge, no exception
escapes a read loop or thread, nothing leaks, and landing is never held up.
"""

import os

import pytest

from tests.support.chaos import ChaosRun, plan_episodes
from tests.support.fleet_harness import TwoNodeHarness
from tests.support.leaks import ErrorWatch, snapshot

SOAK_S = float(os.environ.get("FLIGHTRISK_SOAK_S", "180"))
SEED = int(os.environ.get("FLIGHTRISK_SOAK_SEED", "20261027"))

pytestmark = [
    pytest.mark.slow,
    pytest.mark.filterwarnings("ignore:coroutine .EdgeTransport.send. was never awaited"),
]


def test_the_plan_is_reproducible_and_varied():
    ids = ["drone-1", "drone-2"]
    from random import Random
    first = plan_episodes(Random(SEED), 180.0, ids)
    assert first == plan_episodes(Random(SEED), 180.0, ids)
    assert first != plan_episodes(Random(SEED + 1), 180.0, ids)
    kinds = {e.kind for e in first}
    assert {"partition", "latency", "sever", "emergency"} <= kinds
    assert any(e.kind == "partition" and e.duration_s > 3.0 for e in first)  # longer than the watchdog


def test_fleet_survives_random_link_faults():
    before = snapshot()
    with ErrorWatch() as watch:
        harness = TwoNodeHarness(seed=SEED, link_loss_land_s=3.0, heartbeat_interval_s=0.5).start()
        try:
            assert harness.wait_connected(), "the edges never reported"
            plan = plan_episodes(harness.sim.rng("chaos"), SOAK_S, list(harness.edges))
            report = ChaosRun(harness, plan, SOAK_S).run()
        finally:
            harness.stop()
    leaks = snapshot().leaked_since(before)

    print(f"\nsoak {SOAK_S:.0f}s seed {SEED}: {report}")
    assert report.violations == []
    assert watch.errors == []
    assert leaks == {}
    # The run must have exercised what it claims to.
    assert report.watchdog_landings >= 1
    assert report.emergencies_checked >= 1
    assert report.takeoff_orders >= 2
