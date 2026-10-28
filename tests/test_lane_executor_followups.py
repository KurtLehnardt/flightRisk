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
    executor, drone, sent, _, _ = executors()
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
