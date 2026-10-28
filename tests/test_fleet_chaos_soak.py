"""Chaos soak: minutes of random link faults against the live fleet (marked ``slow``).

Deselected by default. Run it with::

    python -m pytest -m slow tests/test_fleet_chaos_soak.py -s

``FLIGHTRISK_SOAK_S`` sets the length in seconds (default 180, at least 45)
and ``FLIGHTRISK_SOAK_SEED`` the seed. The plan is a pure function of the seed
(tests/test_chaos_plan.py checks it without running anything), so a failing
seed reproduces the same fault timeline; only wall-clock scheduling varies.

Ground node ``a`` and two fake edges use the protocol the code on main speaks,
with the product's settings (3 s watchdog, 1 s heartbeats, 1 to 2 s reconnect
backoff). One edge flies lanes through the real ``LaneExecutor`` and a
``FakeDrone`` with command latency. The test asserts only what holds on main
today (see ``tests/support/chaos.py`` and docs/testing-fleet.md), and it
asserts that the run exercised each fault the plan guarantees, so a passing run
cannot mean that nothing happened.
"""

import os

import pytest

from flightrisk.drone.fake import FakeDroneConfig
from tests.support.chaos import ChaosRun, plan_episodes
from tests.support.fleet_harness import TwoNodeHarness
from tests.support.leaks import ErrorWatch, snapshot
from tests.support.netsim import NetSim

SOAK_S = float(os.environ.get("FLIGHTRISK_SOAK_S", "180"))
SEED = int(os.environ.get("FLIGHTRISK_SOAK_SEED", "20261027"))
SLACK_S = 1.0
LANE_DRONE = "drone-2"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.filterwarnings("ignore:coroutine .EdgeTransport.send. was never awaited"),
]


def test_fleet_survives_random_link_faults():
    before = snapshot()
    with ErrorWatch() as watch:
        harness = TwoNodeHarness(seed=SEED, drone_configs={LANE_DRONE: FakeDroneConfig(latency_s=0.15)})
        report = None
        try:
            harness.start()
            assert harness.wait_connected(), "the edges never reported"
            limit = harness.edges[LANE_DRONE].link_loss_land_s
            plan = plan_episodes(harness.sim.rng("chaos"), SOAK_S, list(harness.edges), limit_s=limit,
                                 slack_s=SLACK_S, lane_drone=LANE_DRONE)
            # The plan that plays is the plan the pure tests check.
            assert plan == plan_episodes(NetSim(seed=SEED).rng("chaos"), SOAK_S, list(harness.edges), limit_s=limit,
                                         slack_s=SLACK_S, lane_drone=LANE_DRONE)
            report = ChaosRun(harness, plan, SOAK_S, slack_s=SLACK_S, lane_drone=LANE_DRONE).run()
        finally:
            harness.stop()
    leaks = snapshot().leaked_since(before)

    print(f"\nsoak {SOAK_S:.0f}s seed {SEED}: {report}")
    assert report.violations == []
    assert watch.errors == []
    assert leaks == {}
    # The run must have exercised what it claims to; the plan guarantees each of these.
    assert report.watchdog_landings >= 1, "no drone was ever landed by its watchdog"
    assert report.max_landing_overrun_s <= SLACK_S
    assert report.episodes_by_kind.get("sever", 0) >= 1
    assert report.emergencies_checked >= 1 and report.emergency_landings == report.emergencies_checked
    assert report.reflights >= 1, "no drone was flying again after a watchdog landing"
    assert report.lane_starts_sent >= 2 and report.lane_moves >= 4, "the lane drone never flew lanes"
    assert report.moves_during_busy >= 1, "the busy fault never met a move"
