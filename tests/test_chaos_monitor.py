"""The chaos monitor must fail when the fleet is broken (marked ``slow``, run with the soak).

A soak that passes proves little unless it would have failed. Each test here
plays a short hand-made plan against a fleet with one safety feature removed
and requires the monitor to report exactly that; a control run with nothing
removed must be clean.
"""

import pytest

from flightrisk.drone.safety import SafetyEnvelope
from tests.support.chaos import ChaosRun, Episode
from tests.support.fleet_harness import TwoNodeHarness
from tests.support.leaks import ErrorWatch
from tests.support.netsim import BOTH, DOWN

pytestmark = [
    pytest.mark.slow,
    pytest.mark.filterwarnings("ignore:coroutine .EdgeTransport.send. was never awaited"),
]

# Longer than the 3 s watchdog plus the 1 s slack.
LONG_DOWN = Episode(3.0, "partition", "drone-1", DOWN, 7.0, (("hold", True),))
EMERGENCY = Episode(2.0, "emergency", "all", BOTH, 1.6)
# The control plan puts the partition first and the emergency well after it. That 8 s clearance is kept as a
# conservative separation so that the control test reads clearly. The old race (a takeoff ordered right after an
# emergency landing was re-landed 2 s later, because the stale landing reason was not cleared) was fixed by
# PR #105 and is guarded by tests/test_lane_executor_model.py. This file alone would not catch a regression of
# that fix unless the plan below, which places the takeoff right after the emergency, is run too.
PARTITION_THEN_EMERGENCY = [
    Episode(2.0, "partition", "drone-1", DOWN, 7.0, (("hold", True),)),
    Episode(11.0, "emergency", "all", BOTH, 1.6),
]
# The pattern that used to fail: emergency at 2 s, partition at 5 s, so a takeoff follows the emergency landing
# and the partition begins soon after.
EMERGENCY_THEN_PARTITION = [
    Episode(2.0, "emergency", "all", BOTH, 1.6),
    Episode(5.0, "partition", "drone-1", DOWN, 7.0, (("hold", True),)),
]


def _play(plan, duration_s, *, mutate=None):
    with ErrorWatch() as watch:
        harness = TwoNodeHarness(seed=11)
        try:
            harness.start()
            assert harness.wait_connected()
            if mutate is not None:
                mutate(harness)
            report = ChaosRun(harness, plan, duration_s).run()
        finally:
            harness.stop()
    assert watch.errors == []
    return report


def test_a_fleet_with_working_safety_is_clean_under_the_control_plan():
    report = _play(PARTITION_THEN_EMERGENCY, 15.0)
    assert report.violations == []
    assert report.watchdog_landings >= 1 and report.reflights >= 1
    assert report.emergencies_checked >= 1 and report.emergency_landings == report.emergencies_checked


def test_a_takeoff_right_after_an_emergency_landing_is_not_re_landed_inside_a_later_partition():
    """Regression guard for the stale landing reason fixed in PR #105: the old failing pattern is clean now."""
    report = _play(EMERGENCY_THEN_PARTITION, 15.0)
    assert report.violations == []
    assert report.watchdog_landings >= 1 and report.reflights >= 1
    assert report.emergencies_checked >= 1 and report.emergency_landings == report.emergencies_checked


def test_a_disabled_watchdog_is_reported_when_a_drone_keeps_flying_through_a_long_outage(monkeypatch):
    monkeypatch.setattr(SafetyEnvelope, "link_lost", lambda self: False)
    report = _play([LONG_DOWN], 12.0)
    assert any("still flying" in v and "watchdog expired" in v for v in report.violations), report
    assert report.watchdog_landings == 0


def test_an_edge_that_gets_the_emergency_and_never_lands_is_reported():
    def never_land_on_emergency(harness):
        for edge in harness.edges.values():
            real = edge.flight.land_now
            edge.flight.land_now = lambda reason, real=real: None if reason == "emergency_land_all" else real(reason)

    report = _play([EMERGENCY], 8.0, mutate=never_land_on_emergency)
    assert any("never told to land" in v for v in report.violations), report
    assert report.emergency_landings == 0 and report.emergencies_checked >= 1
