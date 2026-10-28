"""Follow-up regression tests for the lane executor (landing gate, status, takeoff nack).

Every executor is the real ``LaneExecutor`` on a fake Tello with a fake clock.
"""
from __future__ import annotations

import signal
import threading

import pytest

from flightrisk.edge_device import _install_sigint_land
from tests.lane_executor_support import (
    WAIT_S,
    assign,
    make_v2_executor,
    reply_for,
    send,
    start_at,
    takeoff,
    validate_sent,
    wait_for,
)


@pytest.fixture
def executors():
    created = []

    def _make(drone=None, **kwargs):
        made = make_v2_executor(drone=drone, **kwargs)
        created.append(made)
        return made

    yield _make
    for executor, _, sent, _, _ in created:
        executor.shutdown(timeout=2.0)
        validate_sent(sent)


def _in_thread(target, name: str) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def test_sigint_land_while_the_main_thread_holds_the_land_gate_does_not_deadlock(executors):
    # A second Ctrl+C can land while the main thread is inside land_now, which
    # holds the land gate. The handler's land_now runs on that same thread and
    # must get through; the monitor's link-loss land must not be blocked.
    executor, drone, _, _, _ = executors()
    send(executor, takeoff(cmd_id="t1"))
    assert executor.wait_idle(WAIT_S)
    assert drone.state.is_flying
    previous = _install_sigint_land(executor, executor._log)
    try:
        handler = signal.getsignal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, previous)
    outcome: list[BaseException] = []

    def main_thread_interrupted_inside_land_now():
        with executor._land_gate:
            try:
                handler(signal.SIGINT, None)
            except KeyboardInterrupt as exc:
                outcome.append(exc)

    main = _in_thread(main_thread_interrupted_inside_land_now, "main-sim")
    main.join(WAIT_S)
    stuck = main.is_alive()
    if stuck:  # let the simulated main thread go so the test can finish
        executor._land_gate.release()
        main.join(WAIT_S)
    assert not stuck, "the SIGINT land deadlocked on the land gate"
    assert len(outcome) == 1 and not drone.state.is_flying
    monitor = _in_thread(lambda: executor.land_now("link_loss"), "monitor-sim")
    monitor.join(WAIT_S)
    assert not monitor.is_alive()


def test_a_land_gate_held_by_another_thread_only_delays_the_monitors_land(executors):
    executor, drone, _, _, _ = executors()
    send(executor, takeoff(cmd_id="t1"))
    assert executor.wait_idle(WAIT_S)
    held = threading.Event()
    free = threading.Event()

    def holder():
        with executor._land_gate:
            held.set()
            free.wait(WAIT_S)

    holder_thread = _in_thread(holder, "holder")
    assert held.wait(WAIT_S)
    monitor = _in_thread(lambda: executor.land_now("link_loss"), "monitor-sim")
    assert not wait_for(lambda: not drone.state.is_flying, timeout=0.2)
    free.set()
    holder_thread.join(WAIT_S)
    monitor.join(WAIT_S)
    assert not monitor.is_alive() and not drone.state.is_flying


def _descending_drone(executor, drone):
    """Start a landing whose ``drone.land()`` blocks, so the drone still reports flying."""
    real_land = drone.land
    release = threading.Event()
    entered = threading.Event()

    def slow_land():
        entered.set()
        release.wait(WAIT_S)
        real_land()

    drone.land = slow_land
    lander = _in_thread(lambda: executor.land_now("low_battery"), "lander")
    assert entered.wait(WAIT_S)
    assert drone.state.is_flying

    def finish():
        release.set()
        lander.join(WAIT_S)
        drone.land = real_land

    return finish


def test_takeoff_while_the_flying_drone_is_landing_is_nacked_landing(executors):
    executor, drone, sent, _, _ = executors()
    send(executor, takeoff(cmd_id="t1"))
    assert executor.wait_idle(WAIT_S)
    finish = _descending_drone(executor, drone)
    send(executor, takeoff(cmd_id="t2"))
    nack = reply_for(sent, "t2")
    finish()
    assert (nack["type"], nack["reason"]) == ("nack", "landing")
    assert drone.calls.count(("takeoff",)) == 1


def test_takeoff_of_a_drone_flying_for_good_is_still_busy_already_flying(executors):
    executor, _, sent, _, _ = executors()
    send(executor, takeoff(cmd_id="t1"))
    assert executor.wait_idle(WAIT_S)
    send(executor, takeoff(cmd_id="t2"))
    nack = reply_for(sent, "t2")
    assert (nack["reason"], nack.get("detail")) == ("busy", "already_flying")


class _OwnershipAuthority:
    """Authority that records whether the executor lock was held during ``epoch_seen``."""

    def __init__(self, executor_box: list) -> None:
        self.box = executor_box
        self.held: list[bool] = []

    def check(self, msg):
        return None

    def epoch_seen(self) -> int:
        self.held.append(self.box[0]._lock._is_owned())
        return 7


def test_fleet_status_reads_the_epoch_under_the_executor_lock(executors):
    box: list = []
    authority = _OwnershipAuthority(box)
    executor, _, _, _, _ = executors(authority=authority)
    box.append(executor)
    status = executor.fleet_status()
    assert status["epoch"] == 7 and authority.held == [True]
    assert set(status) == {"plan_id", "state", "plan_valid", "epoch"}


def test_fleet_status_reports_an_unreadable_flight_state_as_unknown(executors):
    executor, _, _, _, _ = executors()

    def unreadable():
        raise OSError("state stream lost")

    executor._is_flying = unreadable
    status = executor.fleet_status()
    assert status["state"] == "unknown" and status["plan_valid"] is False and status["epoch"] == 0
    assert executor.is_active()  # the same unreadable state counts as active there


def test_fleet_status_still_reports_the_real_state(executors):
    executor, _, _, _, _ = executors()
    assert executor.fleet_status()["state"] == "idle"
    send(executor, takeoff(cmd_id="t1"))
    assert executor.wait_idle(WAIT_S)
    assert executor.fleet_status()["state"] == "hovering"


def test_a_copy_of_the_plan_with_other_content_after_a_landing_is_stale_not_mismatched(executors):
    # The same plan_id and order, changed content, after a landing invalidated
    # the plan: refused as stale_plan (the plan may not restart), whatever it holds.
    executor, _, sent, clock, _ = executors()
    send(executor, assign(cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)
    assert wait_for(lambda: executor.lane_index == 3 and not executor.is_busy)
    executor.land_now("operator")
    assert not executor.plan_valid
    send(executor, assign(lanes=assign()["lanes"][:2], cmd_id="a2"))
    assert reply_for(sent, "a2")["reason"] == "stale_plan"
    send(executor, assign(cmd_id="a3"))
    assert reply_for(sent, "a3")["reason"] == "stale_plan"
