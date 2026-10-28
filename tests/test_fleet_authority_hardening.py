"""Edge authority hardening: races with the executor, logging limits, the plan, the cache and PR-5's contract.

Every test here failed before the fix it covers (review round 2 of PR #104).
The race tests force the interleaving deterministically: a hook runs the
executor's control thread between sampling the executor and taking the
authority's lock.
"""
from __future__ import annotations

import threading

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.fleet_authority import (
    ACCEPT,
    CLOSE,
    IGNORE,
    LAND,
    REFUSE,
    EdgeAuthority,
)
from flightrisk.observability import StructuredLogger
from tests.test_fleet_authority import (
    DRONE,
    FakeHooks,
    accept_plan,
    command,
    connect,
    fence,
    make,
)


class RecordingLog(StructuredLogger):
    """A structured logger that keeps every event instead of printing it."""

    def __init__(self):
        super().__init__(component="test-recording")
        self.events: list[tuple[str, str, dict]] = []

    def _emit(self, level, event, **kwargs):
        self.events.append((level, event, kwargs))

    def count(self, event):
        return sum(1 for _, name, _ in self.events if name == event)


class InterleavingHooks(FakeHooks):
    """``is_active`` takes its sample, then runs ``between`` on another thread before returning it.

    That is the window between sampling the executor and taking the
    authority's lock in which the executor's control thread can run.
    """

    def __init__(self):
        super().__init__()
        self.between = None

    def is_active(self):
        sample = self.active
        step, self.between = self.between, None
        if step is not None:
            thread = threading.Thread(target=step)
            thread.start()
            thread.join()
        return sample


@pytest.mark.parametrize("via", ["hello", "heartbeat"])
def test_takeoff_dispatched_and_finished_while_sampling_blocks_adoption(via):
    hooks = InterleavingHooks()
    auth = EdgeAuthority(DRONE, "win", hooks=hooks)
    connect(auth, "a", "mac", epoch=1)
    takeoff = command(fp.TAKEOFF, 1, "mac", cmd_id="t-1")
    decision = auth.on_message("a", takeoff)

    def control_thread():
        assert auth.begin_dispatch(takeoff, decision.admitted) is takeoff
        hooks.active = True  # the executor launched the takeoff
        auth.begin_dispatch(None)  # and asked for the next message

    if via == "hello":
        hooks.between = control_thread
        connect(auth, "b", "win", epoch=2)
    else:
        hooks.active = True
        connect(auth, "b", "win", epoch=2)  # refused in flight; its heartbeat may adopt once landed
        hooks.active = False
        hooks.between = control_thread
        auth.on_message("b", fp.make_heartbeat(1, fence=fence(2, "win")))
    assert hooks.active, "the interleaving did not run"
    assert auth.epoch_seen == 1 and auth.authority_leader == "mac" and auth.authority_key == "a"
    assert hooks.invalidated == ["epoch_1_adopted"]


def test_takeoff_handed_out_before_sampling_and_finished_before_the_lock_blocks_adoption():
    hooks = InterleavingHooks()
    auth = EdgeAuthority(DRONE, "win", hooks=hooks)
    connect(auth, "a", "mac", epoch=1)
    takeoff = command(fp.TAKEOFF, 1, "mac", cmd_id="t-1")
    decision = auth.on_message("a", takeoff)
    assert auth.begin_dispatch(takeoff, decision.admitted) is takeoff  # handed out, not launched yet

    def control_thread():
        hooks.active = True  # launched after the sample was taken
        auth.begin_dispatch(None)

    hooks.between = control_thread
    connect(auth, "b", "win", epoch=2)
    assert auth.epoch_seen == 1 and auth.authority_leader == "mac"
    # Once the drone is down, the new leader is adopted as before.
    hooks.active = False
    auth.on_message("b", fp.make_heartbeat(2, fence=fence(2, "win")))
    assert auth.epoch_seen == 2 and auth.authority_leader == "win"


def test_supports_is_never_called_under_the_authority_lock():
    auth, hooks = make()
    lock_free: list[bool] = []

    def supports(mtype):
        # An executor hook may take the executor's lock, and the executor may
        # call the authority while holding it: the authority's lock must be free.
        probe = threading.Thread(target=auth.broadcast_keys)
        probe.start()
        probe.join(timeout=1.0)
        lock_free.append(not probe.is_alive())
        return True

    hooks.supports = supports
    connect(auth, "a", "mac", epoch=1)
    assert auth.on_message("a", command(fp.HOLD, 1, "mac")).action == ACCEPT
    assert lock_free == [True]


@pytest.mark.parametrize("source", ["authority", "standby"])
def test_landing_ends_the_plan_so_start_at_naming_it_is_refused(source):
    auth, _ = make()
    connect(auth, "a", "mac", epoch=1)
    connect(auth, "b", "win", role="standby", epoch=0)
    accept_plan(auth, "a", 1, "mac", "p-1")
    assert auth.on_message("a", command(fp.RESUME, 1, "mac", plan_id="p-1")).action == ACCEPT
    assert auth.on_message("a" if source == "authority" else "b", fp.make_land()).action == LAND
    for mtype in (fp.START_AT, fp.RESUME):
        decision = auth.on_message("a", command(mtype, 1, "mac", plan_id="p-1"))
        assert decision.action == REFUSE and decision.replies[0]["reason"] == "no_valid_plan"
    accept_plan(auth, "a", 1, "mac", "p-2")
    assert auth.on_message("a", command(fp.START_AT, 1, "mac", plan_id="p-2")).action == ACCEPT


def test_refusals_from_other_grounds_never_evict_the_authoritys_command_ids():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=1)
    connect(auth, "b", "win", epoch=1)  # a tied leader: never obeyed
    keep = command(fp.TAKEOFF, 1, "mac", cmd_id="keep-1")
    assert auth.on_message("a", keep).action == ACCEPT
    for n in range(fp.CMD_ID_CACHE_SIZE + 10):
        assert auth.on_message("b", command(fp.HOLD, 1, "win", cmd_id=f"flood-{n}")).action == REFUSE
    assert auth.on_message("a", keep).action == IGNORE, "the authority's command would run twice"


def test_a_flood_of_landings_is_logged_once_and_every_one_lands():
    log = RecordingLog()
    auth = EdgeAuthority(DRONE, "win", hooks=FakeHooks(), logger=log)
    connect(auth, "a", "mac", epoch=1)
    for _ in range(200):
        assert auth.on_message("a", fp.make_land()).action == LAND
    assert log.count("edge_landing_command") <= 2


def test_landing_log_reports_how_many_were_not_logged():
    now = [100.0]
    log = RecordingLog()
    auth = EdgeAuthority(DRONE, "win", hooks=FakeHooks(), logger=log, clock=lambda: now[0])
    connect(auth, "a", "mac", epoch=1)
    for _ in range(5):
        auth.on_message("a", fp.make_land())
    now[0] += 1.5
    auth.on_message("a", fp.make_emergency_land_all())
    auth.on_message("a", fp.make_land())
    logged = [fields for _, name, fields in log.events if name == "edge_landing_command"]
    assert [(f["type"], f["not_logged_since"]) for f in logged] == [("land", 0), ("emergency_land_all", 0), ("land", 4)]


def test_repeated_refusals_of_a_connection_are_not_logged_every_time():
    log = RecordingLog()
    auth = EdgeAuthority(DRONE, "win", hooks=FakeHooks(), logger=log)
    for _ in range(45):
        auth.opened("k", "ws://k")
        assert auth.on_message("k", fp.make_heartbeat(0)).action == CLOSE
        auth.closed("k")
    assert log.count("edge_connection_refused") == 3  # the 1st, 20th and 40th


def test_frames_from_a_closed_connection_leave_no_state_and_a_landing_still_lands():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=1)
    auth.closed("a")
    assert auth.on_message("a", fp.make_heartbeat(1, fence=fence(1, "mac"))).action == IGNORE
    decision = auth.on_message("a", fp.make_land(cmd_id="late"))
    assert decision.action == LAND and decision.replies == () and not decision.feed_watchdog
    assert auth.broadcast_keys() == [] and "a" not in auth._conns


def test_authority_contract_for_the_lane_executor():
    """PR-5's ``LaneExecutor(authority=...)`` calls ``check(msg)`` and ``epoch_seen()``."""
    auth, _ = make()
    assert auth.epoch_seen == 0 and auth.epoch_seen() == 0
    connect(auth, "a", "mac", epoch=3)
    assert auth.epoch_seen == 3 and auth.epoch_seen() == 3 and type(auth.epoch_seen()) is int
    assert auth.check(command(fp.TAKEOFF, 3, "mac")) is None
    assert auth.check(command(fp.TAKEOFF, 2, "mac")) == "stale_epoch"
    assert auth.check(command(fp.TAKEOFF, 3, "win")) == "not_authority"
    assert auth.check(fp.make_takeoff()) == "not_authority"
    assert auth.check({"type": "land"}) is None and auth.check({"type": "emergency_land_all"}) is None
    auth.closed("a")
    assert auth.check(command(fp.TAKEOFF, 3, "mac")) == "not_authority"
    assert auth.check(command(fp.HOLD, 3, "mac")) is None
