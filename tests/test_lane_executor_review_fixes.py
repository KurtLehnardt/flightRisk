"""Regression tests for the retroactive review findings on the lane executor (PR #108).

* A: an exception inside ``land_now`` after the in-flight flag is set must
  not strand the flag.
* B: the announce counter is released after ``_lock`` is released, never
  while holding it (lock-order inversion with the re-entrant gate).
* C: ``fleet_status`` reads the authority epoch before taking ``_lock``.
* D: the SIGINT handler lands even when its log call raises.
* The ``unknown`` flight state never reaches the wire.
"""
from __future__ import annotations

import signal
import threading

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.edge_device import _install_sigint_land
from flightrisk.fleet_authority import EdgeAuthority
from flightrisk.observability import StructuredLogger
from tests.lane_executor_support import (
    WAIT_S,
    QuietLogger,
    assign,
    make_v2_executor,
    reply_for,
    send,
    takeoff,
    wait_for,
)


class RaisingLogger(QuietLogger):
    """Raises *exc* from one named log event, once (or always when ``times`` is None)."""

    def __init__(self, event: str, exc: BaseException, times: int | None = 1) -> None:
        super().__init__()
        self.event, self.exc, self.times = event, exc, times

    def _emit(self, level, event, **kwargs):
        super()._emit(level, event, **kwargs)
        if event == self.event and (self.times is None or self.times > 0):
            if self.times is not None:
                self.times -= 1
            raise self.exc


@pytest.fixture
def make():
    created = []

    def _make(**kwargs):
        made = make_v2_executor(**kwargs)
        created.append(made[0])
        return made

    yield _make
    for executor in created:
        executor.shutdown(timeout=2.0)


# --- A -----------------------------------------------------------------------

@pytest.mark.parametrize("exc_type", [RuntimeError, KeyboardInterrupt])
def test_exception_in_land_now_log_does_not_strand_the_landing_flag(make, exc_type):
    executor, drone, sent, _, _ = make()
    send(executor, takeoff(cmd_id="t1"))
    assert executor.wait_idle(WAIT_S)
    assert drone.state.is_flying
    executor._log = RaisingLogger("edge_land", exc_type("boom"))

    with pytest.raises(exc_type):
        executor.land_now("operator")

    # The flag is not stranded: a later landing still reaches the drone.
    assert executor._land_inflight is False
    assert executor.land_now("shutdown") is True
    assert drone.calls.count(("land",)) == 1
    assert not drone.state.is_flying
    # Everything that was refused while the flag was stranded works again.
    assert wait_for(lambda: not executor.is_active())
    assert executor.state == "idle"
    send(executor, assign(cmd_id="a1"))
    assert reply_for(sent, "a1")["type"] == "ack"
    send(executor, takeoff(cmd_id="t2"))
    assert wait_for(lambda: reply_for(sent, "t2") is not None)
    assert reply_for(sent, "t2")["type"] == "ack"
    assert executor.wait_idle(WAIT_S)
    assert drone.state.is_flying


def test_exception_in_the_announce_step_does_not_leak_the_request_counter(make):
    executor, _, _, _, _ = make()
    send(executor, takeoff(cmd_id="t1"))
    assert executor.wait_idle(WAIT_S)
    # A KeyboardInterrupt from the abort flag, raised after the counter went up.
    real = executor._abort
    state = {"armed": True}

    class Flag:
        def __getattr__(self, name):
            return getattr(real, name)

        def set(self):
            if state["armed"]:
                state["armed"] = False
                raise KeyboardInterrupt
            real.set()

    executor._abort = Flag()
    with pytest.raises(KeyboardInterrupt):
        executor.land_now("operator")
    assert executor._land_requests == 0
    assert executor._land_inflight is False


# --- B -----------------------------------------------------------------------

class ProbeGate:
    """Wraps the land gate; the land_now thread waits before its second acquisition."""

    def __init__(self, inner, thread_name: str, hold_second: threading.Event, other_has_gate: threading.Event) -> None:
        self.inner, self.thread_name = inner, thread_name
        self.hold_second, self.other_has_gate = hold_second, other_has_gate
        self.enters = 0

    def __enter__(self):
        if threading.current_thread().name == self.thread_name:
            self.enters += 1
            if self.enters == 2:
                self.hold_second.set()
                self.other_has_gate.wait(2.0)
        self.inner.acquire()
        return self

    def __exit__(self, *exc):
        self.inner.release()


def test_announce_counter_is_released_without_holding_the_executor_lock():
    # Not the ``make`` fixture: a deadlocked executor could not be shut down.
    executor, _, _, _, _ = make_v2_executor()
    hold_second, other_has_gate = threading.Event(), threading.Event()
    executor._land_gate = ProbeGate(executor._land_gate, "lander", hold_second, other_has_gate)
    other_done = threading.Event()

    def lander():
        executor.land_now("operator")

    def other():
        # A gate-holding caller that then needs the executor lock, as a
        # re-entrant land_now (SIGINT handler) does.
        assert hold_second.wait(2.0)
        with executor._land_gate.inner:
            other_has_gate.set()
            with executor._lock:
                pass
        other_done.set()

    a = threading.Thread(target=lander, name="lander", daemon=True)
    b = threading.Thread(target=other, name="other", daemon=True)
    a.start()
    b.start()
    a.join(5.0)
    b.join(5.0)
    deadlocked = a.is_alive() or b.is_alive()
    if not deadlocked:
        executor.shutdown(timeout=2.0)
    assert not deadlocked, "lock-order deadlock between the land gate and the executor lock"
    assert other_done.is_set()
    assert executor._land_requests == 0


# --- C -----------------------------------------------------------------------

def test_fleet_status_does_not_call_the_authority_under_the_executor_lock(make):
    seen = []

    class Authority:
        def check(self, msg):
            return None

        def epoch_seen(self):
            result = []

            def probe_lock():
                got = executor._lock.acquire(blocking=False)
                result.append(got)
                if got:
                    executor._lock.release()

            probe = threading.Thread(target=probe_lock)
            probe.start()
            probe.join(2.0)
            seen.append(bool(result and result[0]))  # True: the lock was free
            return 3

    executor, _, _, _, _ = make(authority=Authority())
    seen.clear()
    assert executor.fleet_status()["epoch"] == 3
    assert seen == [True]


# --- D -----------------------------------------------------------------------

class RaisingStructuredLog(StructuredLogger):
    def __init__(self) -> None:
        super().__init__(component="test-sigint")
        self.events = []

    def warning(self, event, **kwargs):
        self.events.append(event)
        if event == "edge_sigint_land":
            raise RuntimeError("reentrant call")

    def error(self, event, **kwargs):
        self.events.append(event)


def test_sigint_handler_lands_even_when_its_log_call_raises():
    lands = []

    class Flight:
        def land_now(self, reason):
            lands.append(reason)

    log = RaisingStructuredLog()
    previous = _install_sigint_land(Flight(), log)
    handler = signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)
    finally:
        signal.signal(signal.SIGINT, previous if previous is not None else signal.SIG_DFL)
    assert lands == ["sigint"]


# --- the unknown state never reaches the wire ---------------------------------

def test_unknown_flight_state_never_reaches_the_wire():
    class Hooks:
        def is_active(self):
            return False

        def supports(self, mtype):
            return True

        def snapshot(self):
            return {"is_flying": False, "lane_index": 0, "battery": 50}

        def fleet_status(self):
            return {"plan_id": None, "state": "unknown", "plan_valid": False, "epoch": 0}

        def invalidate_plan(self, reason):
            pass

    auth = EdgeAuthority("drone-1", "win", hooks=Hooks(), legacy=False)
    out = auth.outbound({"type": "telemetry", "drone_id": "drone-1", "is_flying": False})
    assert out["state"] in fp.EDGE_STATES and out["state"] != "unknown"
