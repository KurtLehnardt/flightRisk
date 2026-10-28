"""The chaos plan is pure, so it is tested without running anything (the soak itself is marked ``slow``)."""

from random import Random

import pytest

from tests.support.chaos import (
    _CLEARANCE,
    BUSY_S,
    DOWN_AT,
    MIN_SOAK_S,
    check_flight_log,
    guaranteed_episodes,
    plan_episodes,
)
from tests.support.netsim import BOTH, DOWN, NetSim

IDS = ["drone-1", "drone-2"]
LIMIT, SLACK = 3.0, 1.0


def plan(seed: int, duration: float = 60.0):
    return plan_episodes(NetSim(seed=seed).rng("chaos"), duration, IDS, limit_s=LIMIT, slack_s=SLACK, lane_drone="drone-2")


def test_the_plan_is_reproducible_and_varies_with_the_seed():
    assert plan(20261027, 180.0) == plan(20261027, 180.0)
    assert plan(20261027, 180.0) != plan(20261028, 180.0)


def test_the_soak_plays_the_plan_these_tests_check():
    """The soak draws from ``harness.sim.rng('chaos')``, which is ``NetSim(seed).rng('chaos')``."""
    assert plan(7) == plan_episodes(NetSim(seed=7).rng("chaos"), 60.0, IDS, limit_s=LIMIT, slack_s=SLACK,
                                    lane_drone="drone-2")
    assert plan(7) != plan_episodes(Random(7), 60.0, IDS, limit_s=LIMIT, slack_s=SLACK, lane_drone="drone-2")


@pytest.mark.parametrize("duration", [MIN_SOAK_S, 60.0, 180.0])
@pytest.mark.parametrize("seed", range(100))
def test_every_plan_holds_each_fault_the_soak_asserts_on(seed, duration):
    episodes = plan(seed, duration)
    kinds = [e.kind for e in episodes]
    assert kinds.count("emergency") >= 1 and kinds.count("sever") >= 1
    busy = [e for e in episodes if e.kind == "busy" and e.target == "drone-2" and e.duration_s >= BUSY_S]
    assert busy
    long_down = [e for e in episodes if e.kind == "partition" and e.direction in (DOWN, BOTH)
                 and e.duration_s >= LIMIT + SLACK + 1.0]
    assert long_down
    fixed = guaranteed_episodes(NetSim(seed=seed).rng("chaos"), duration, IDS, limit_s=LIMIT, slack_s=SLACK,
                                lane_drone="drone-2")
    assert max(e.end_s for e in fixed) <= duration - 4.0, "the guaranteed episodes end well before the run does"
    assert all(e in episodes for e in fixed)


@pytest.mark.parametrize("seed", range(50))
def test_nothing_else_overlaps_a_guaranteed_episode(seed):
    episodes = plan(seed)
    fixed = guaranteed_episodes(NetSim(seed=seed).rng("chaos"), 60.0, IDS, limit_s=LIMIT, slack_s=SLACK,
                                lane_drone="drone-2")
    assert len(fixed) == 4 and all(e in episodes for e in fixed)
    for guaranteed in fixed:
        before, after = _CLEARANCE[guaranteed.kind]
        others = [e for e in episodes if e not in fixed and e.start_s < guaranteed.end_s + after
                  and e.end_s > guaranteed.start_s - before]
        assert others == [], f"{others} overlaps {guaranteed}"


@pytest.mark.parametrize("duration", [MIN_SOAK_S, 60.0, 180.0])
@pytest.mark.parametrize("seed", range(100))
def test_no_emergency_comes_shortly_before_the_guaranteed_partition(seed, duration):
    """The gap is checked against a literal 8 s, not against the constant the generator uses."""
    episodes = plan(seed, duration)
    (down,) = [e for e in episodes if e.kind == "partition" and e.direction in (DOWN, BOTH)
               and e.duration_s >= LIMIT + SLACK + 1.0 and e.start_s == pytest.approx(DOWN_AT * duration, abs=0.01)]
    for e in episodes:
        if e.kind == "emergency" and e.start_s <= down.start_s:
            assert down.start_s - e.end_s >= 8.0, f"{e} is less than 8 s before {down}"


@pytest.mark.parametrize("seed", range(40))
def test_the_clearance_around_each_guaranteed_episode_holds_by_a_brute_force_scan(seed):
    """Sample the timeline every 0.1 s and look for random episodes inside the required clear space."""
    duration = 60.0
    episodes = plan(seed, duration)
    fixed = guaranteed_episodes(NetSim(seed=seed).rng("chaos"), duration, IDS, limit_s=LIMIT, slack_s=SLACK,
                                lane_drone="drone-2")
    literal = {"busy": (3.0, 1.0), "partition": (3.0, 1.0), "sever": (3.0, 3.0), "emergency": (3.0, 3.0)}
    others = [e for e in episodes if e not in fixed]
    for guaranteed in fixed:
        before, after = literal[guaranteed.kind]
        t = guaranteed.start_s - before
        while t < guaranteed.end_s + after:
            for e in others:
                assert not (e.start_s <= t < max(e.end_s, e.start_s + 1e-9)), f"{e} is active at {t:.1f} s, inside the clear space of {guaranteed}"
            t += 0.1


def test_random_episodes_still_cover_a_fair_share_of_a_60_s_run():
    """The clearance windows must not thin the random chaos out: at least 20 percent of the run on average."""
    shares = []
    for seed in range(100):
        episodes = plan(seed, 60.0)
        fixed = guaranteed_episodes(NetSim(seed=seed).rng("chaos"), 60.0, IDS, limit_s=LIMIT, slack_s=SLACK,
                                    lane_drone="drone-2")
        spans = sorted((e.start_s, min(e.end_s, 60.0)) for e in episodes if e not in fixed and e.duration_s > 0)
        covered, reached = 0.0, 0.0
        for start, end in spans:
            start = max(start, reached)
            if end > start:
                covered += end - start
                reached = end
        shares.append(covered / 60.0)
    assert sum(shares) / len(shares) >= 0.20, sum(shares) / len(shares)


def test_a_run_too_short_for_the_guaranteed_episodes_is_refused():
    with pytest.raises(ValueError, match="at least"):
        plan(1, MIN_SOAK_S - 1)


def test_the_guaranteed_partition_outlasts_the_watchdog_and_the_slack():
    for seed in range(20):
        (down,) = [e for e in plan(seed) if e.kind == "partition" and e.duration_s >= LIMIT + SLACK + 1.0
                   and e.start_s == pytest.approx(0.3 * 60.0, abs=0.01)]
        assert down.duration_s >= LIMIT + SLACK + 1.0


# --- the flight-log invariants, on hand-made logs ---

def test_a_clean_flight_log_passes():
    log = [("connect",), ("takeoff",), ("move", "up", 60), ("move", "forward", 100), ("land",), ("land",),
           ("takeoff",), ("move", "up", 60), ("land",)]
    assert check_flight_log(log) == []


def test_motion_after_a_landing_is_a_violation():
    problems = check_flight_log([("takeoff",), ("land",), ("move", "forward", 100)])
    assert len(problems) == 1 and "after the landing" in problems[0]
    assert check_flight_log([("takeoff",), ("land",), ("rotate", 90)])
    assert check_flight_log([("takeoff",), ("land",), ("rc_control", 0, 10, 0, 0)])


def test_two_takeoffs_without_a_landing_are_a_violation():
    problems = check_flight_log([("takeoff",), ("move", "up", 60), ("takeoff",)])
    assert len(problems) == 1 and "already in progress" in problems[0]
