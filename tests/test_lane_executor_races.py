"""Regression tests for races between landings, holds and new flight sequences.

Each test forces one interleaving deterministically instead of hoping a
thread schedule produces it:

* **The landing gap.** ``land_now`` sets the abort flag before it waits for
  the executor lock. ``LandGate`` (installed as the executor's abort flag)
  stops the landing thread right after that first ``set()``, so a ``resume``,
  ``nudge``, ``start_at``, re-task or the re-task that follows a nudge runs
  while the landing has been requested but not yet recorded. None of them
  may launch a flight sequence or be acknowledged, and the drone must get
  nothing but the land.
* **A hold that arrives after the hover timeout or the on_complete landing
  was decided** must stop that landing, while a hold never stops a battery,
  link-loss, local or ground landing.
* **A nudge cut short by a landing** is refused, never acknowledged.

The executors are real ``LaneExecutor``s on fake Tellos with a fake clock;
every message they send is validated.
"""
from __future__ import annotations

import threading

import pytest

from flightrisk import fleet_protocol as fp
from tests.lane_executor_support import (
    LANES,
    WAIT_S,
    SteppedTello,
    assign,
    dead_reckoned,
    fly_to_lane,
    hold,
    make_v2_executor,
    nudge,
    reply_for,
    resume,
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
    for executor, drone, sent, _, _ in created:
        if isinstance(drone, SteppedTello):
            drone.auto = True
            drone.release(50)
        executor.shutdown(timeout=2.0)
        validate_sent(sent)


def stepped(**kwargs) -> SteppedTello:
    return SteppedTello(name="drone-1", battery=90, **kwargs)


def held_mid_lane(executors):
    """Fly to the first lane move, hold while it is in flight, and let it finish."""
    executor, drone, sent, clock, log = executors(drone=stepped())
    fly_to_lane(executor, drone, clock, sent)
    drone.release()  # the climb to the slot
    assert wait_for(lambda: len(drone.move_log) == 2 and drone.waiting)
    send(executor, hold(cmd_id="h1", reason="alert"))
    drone.release()  # the move in flight completes
    assert wait_for(lambda: not executor.is_busy and not drone.busy)
    assert executor.is_held
    return executor, drone, sent, clock, log


# ---------------------------------------------------------------------------
# The landing gap
# ---------------------------------------------------------------------------


class LandGate(threading.Event):
    """Abort flag that stops one landing thread just after its first ``set()``.

    That is the moment ``land_now`` has asked for the landing but has not
    yet taken the executor lock to record it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.lander: threading.Thread | None = None
        self.entered = threading.Event()
        self.opened = threading.Event()

    def set(self) -> None:
        super().set()
        if threading.current_thread() is self.lander and not self.entered.is_set():
            self.entered.set()
            self.opened.wait(WAIT_S)


class LandingGap:
    """A ``land_now(reason)`` on its own thread (the monitor's), held in the gap."""

    def __init__(self, executor, reason: str = "low_battery") -> None:
        gate = LandGate()
        if executor._abort.is_set():
            threading.Event.set(gate)
        executor._abort = gate
        self.gate = gate
        self.lander = threading.Thread(target=executor.land_now, args=(reason,), name="lander", daemon=True)
        gate.lander = self.lander
        self.lander.start()
        assert gate.entered.wait(WAIT_S)

    def close(self) -> None:
        self.gate.opened.set()
        self.lander.join(WAIT_S)
        assert not self.lander.is_alive()


def test_resume_in_the_landing_gap_is_refused_and_flies_nothing(executors):
    executor, drone, sent, _, _ = held_mid_lane(executors)
    mark = len(drone.calls)
    gap = LandingGap(executor)
    send(executor, resume("p-1", cmd_id="r1"))
    reply = reply_for(sent, "r1")
    gap.close()
    assert executor.wait_idle(WAIT_S)
    assert reply is not None and (reply["type"], reply["reason"]) == ("nack", "landing")
    assert drone.calls[mark:] == [("land",)]
    assert not executor.plan_valid and not executor.is_held


def test_nudge_in_the_landing_gap_is_refused_and_flies_nothing(executors):
    executor, drone, sent, _, _ = held_mid_lane(executors)
    mark = len(drone.calls)
    gap = LandingGap(executor)
    send(executor, nudge("left", 50, cmd_id="n1"))
    reply = reply_for(sent, "n1")
    gap.close()
    assert executor.wait_idle(WAIT_S)
    assert reply is not None and (reply["type"], reply["reason"]) == ("nack", "landing")
    assert drone.calls[mark:] == [("land",)]
    assert executor.envelope.pose_snapshot().x_cm == pytest.approx(0)


@pytest.mark.parametrize("command", ["resume", "nudge"])
def test_a_landing_requested_while_a_command_is_checked_stops_its_launch(executors, command):
    # The command has passed its own landing check under the lock when a
    # landing is requested on another thread; the launch must still refuse.
    executor, drone, sent, _, _ = held_mid_lane(executors)
    mark = len(drone.calls)
    envelope = executor.envelope
    # Called under the lock between the command's checks and its launch: the
    # geofence check of the rest of the plan (resume) or of the nudge.
    name = "pose_snapshot" if command == "resume" else "check_move"
    original = getattr(envelope, name)
    gaps: list[LandingGap] = []

    def request_landing(*args):
        if not gaps:
            gaps.append(LandingGap(executor))
        return original(*args)

    setattr(envelope, name, request_landing)
    try:
        send(executor, resume("p-1", cmd_id="c1") if command == "resume" else nudge("left", 50, cmd_id="c1"))
    finally:
        delattr(envelope, name)
    reply = reply_for(sent, "c1")
    assert gaps
    gaps[0].close()
    assert executor.wait_idle(WAIT_S)
    assert reply is not None and (reply["type"], reply["reason"]) == ("nack", "landing")
    assert drone.calls[mark:] == [("land",)]


def test_hold_in_the_landing_gap_is_refused(executors):
    executor, drone, sent, _, _ = executors()
    send(executor, takeoff(cmd_id="t1"))
    assert executor.wait_idle(WAIT_S)
    gap = LandingGap(executor, reason="local_key")
    send(executor, hold(cmd_id="h1"))
    gap.close()
    assert reply_for(sent, "h1")["reason"] == "landing"
    assert not executor.is_held and not drone.state.is_flying


def test_start_at_in_the_landing_gap_is_refused_and_never_takes_off_again(executors):
    executor, drone, sent, clock, _ = executors()
    send(executor, takeoff(cmd_id="t1"))
    assert executor.wait_idle(WAIT_S)
    send(executor, assign(cmd_id="a1"))
    mark = len(drone.calls)
    gap = LandingGap(executor)
    send(executor, start_at(clock(), cmd_id="s1"))
    gap.close()
    assert executor.wait_idle(WAIT_S)
    assert wait_for(lambda: reply_for(sent, "s1") is not None)
    reply = reply_for(sent, "s1")
    assert (reply["type"], reply["reason"]) == ("nack", "landing")
    clock.advance(1.0)
    assert not wait_for(lambda: len(drone.calls) > mark + 1, timeout=0.2)
    assert drone.calls[mark:] == [("land",)] and not drone.state.is_flying


def test_retask_in_the_landing_gap_is_refused_and_keeps_the_plan(executors):
    executor, drone, sent, clock, _ = executors()
    send(executor, assign(cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)
    assert wait_for(lambda: executor.lane_index == 3 and not executor.is_busy)
    mark = len(drone.calls)
    gap = LandingGap(executor)
    retask = LANES + [{"direction": "left", "distance_cm": 100}]
    send(executor, assign(lanes=retask, plan_id="p-2", plan_seq=2, cmd_id="a2"))
    reply = reply_for(sent, "a2")
    gap.close()
    assert executor.wait_idle(WAIT_S)
    assert reply is not None and (reply["type"], reply["reason"], reply["lanes_total"]) == ("nack", "landing", 4)
    assert executor.plan_id == "p-1" and executor.lanes_total == 3
    assert drone.calls[mark:] == [("land",)]


def test_retask_after_a_nudge_is_not_flown_once_a_landing_is_requested(executors):
    executor, drone, sent, clock, _ = executors(drone=stepped())
    send(executor, assign(lanes=LANES[:1], cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)
    drone.release(2)  # climb and the lane
    assert wait_for(lambda: executor.lane_index == 1 and not executor.is_busy)
    send(executor, nudge("left", 50, cmd_id="n1"))
    assert wait_for(lambda: drone.waiting)
    nudger = executor._worker
    send(executor, assign(lanes=LANES[:1] + [{"direction": "right", "distance_cm": 100}],
                          plan_id="p-2", plan_seq=2, cmd_id="a2"))
    assert reply_for(sent, "a2")["type"] == "ack"
    mark = len(drone.calls)
    gap = LandingGap(executor)
    drone.release()  # the nudge completes while the landing waits for the lock
    nudger.join(WAIT_S)
    assert not nudger.is_alive()
    # The nudge's sequence has ended without launching the re-task.
    assert not executor.is_busy
    gap.close()
    assert reply_for(sent, "n1")["reason"] == "landing"
    assert drone.calls[mark:] == [("land",)]


def test_takeoff_in_the_landing_gap_is_refused(executors):
    executor, drone, sent, _, _ = executors()
    gap = LandingGap(executor, reason="local_key")
    send(executor, takeoff(cmd_id="t1"))
    gap.close()
    assert executor.wait_idle(WAIT_S)
    assert reply_for(sent, "t1")["reason"] == "landing"
    assert ("takeoff",) not in drone.calls


def test_a_requested_landing_is_active_and_reported_as_landing(executors):
    executor, drone, _, _, _ = executors()
    drone.takeoff()
    gap = LandingGap(executor, reason="local_key")
    assert executor.is_active()
    assert executor.state == "landing" and executor.fleet_status()["state"] == "landing"
    gap.close()
    assert not drone.state.is_flying and not executor.is_active()


# ---------------------------------------------------------------------------
# A hold against the hover timeout and the on_complete landing
# ---------------------------------------------------------------------------


def run_lanes(executor, clock, lanes=LANES, **assign_kwargs):
    send(executor, assign(lanes=lanes, cmd_id="a1", **assign_kwargs))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)


def test_hold_after_the_hover_timeout_check_stops_the_landing(executors):
    executor, drone, sent, clock, _ = executors()
    run_lanes(executor, clock, on_complete="hover", hover_timeout_s=2)
    assert wait_for(lambda: executor.lane_index == 3 and not executor.is_busy)
    executor.tick()  # the countdown starts
    clock.advance(2.1)
    executor.note_ground_message()
    should_land = executor.envelope.should_land

    def hold_arrives(battery):
        # The monitor has found the hover timeout due (under the lock) and
        # checks the battery next; the hold is delivered right there.
        send(executor, hold(cmd_id="h1", reason="alert"))
        return should_land(battery)

    executor.envelope.should_land = hold_arrives
    executor.tick()
    executor.envelope.should_land = should_land
    assert reply_for(sent, "h1")["type"] == "ack"
    assert ("land",) not in drone.calls and drone.state.is_flying
    assert executor.is_held and executor.state == "holding" and executor.plan_valid


def test_hold_after_the_on_complete_decision_stops_the_landing(executors):
    executor, drone, sent, clock, log = executors()
    emit = log._emit
    fired = threading.Event()

    def hold_on_lanes_complete(level, event, **kwargs):
        emit(level, event, **kwargs)
        if event == "edge_lanes_complete" and not fired.is_set():
            # The lanes sequence has decided to land; a hold arrives from
            # the control thread before it does.
            fired.set()
            control = threading.Thread(target=send, args=(executor, hold(cmd_id="h1", reason="alert")))
            control.start()
            control.join(WAIT_S)

    log._emit = hold_on_lanes_complete
    run_lanes(executor, clock, lanes=LANES[:1], on_complete="land")
    assert wait_for(lambda: fired.is_set() and not executor.is_busy)
    assert reply_for(sent, "h1")["type"] == "ack"
    assert ("land",) not in drone.calls and drone.state.is_flying
    assert executor.is_held and executor.plan_valid
    # Resume finishes the plan, which ends in its landing.
    send(executor, resume("p-1", cmd_id="r1"))
    assert reply_for(sent, "r1")["type"] == "ack"
    assert wait_for(lambda: ("land",) in drone.calls)
    assert executor.wait_idle(WAIT_S) and not executor.plan_valid


def test_retask_after_the_on_complete_decision_is_flown_instead_of_landing(executors):
    executor, drone, sent, clock, log = executors()
    emit = log._emit
    fired = threading.Event()
    retask = LANES[:1] + [{"direction": "right", "distance_cm": 100}]

    def retask_on_lanes_complete(level, event, **kwargs):
        emit(level, event, **kwargs)
        if event == "edge_lanes_complete" and not fired.is_set():
            fired.set()
            msg = assign(lanes=retask, plan_id="p-2", plan_seq=2, cmd_id="a2", on_complete="land")
            control = threading.Thread(target=send, args=(executor, msg))
            control.start()
            control.join(WAIT_S)

    log._emit = retask_on_lanes_complete
    run_lanes(executor, clock, lanes=LANES[:1], on_complete="land")
    assert wait_for(lambda: ("land",) in drone.calls)
    assert executor.wait_idle(WAIT_S)
    assert reply_for(sent, "a2")["type"] == "ack"
    # The new lane is flown before the new plan's own landing.
    assert drone.calls == [("takeoff",), ("move", "up", 20), ("move", "forward", 200), ("move", "right", 100),
                           ("land",)]


@pytest.mark.parametrize("kind", ["low_battery", "link_loss", "local_key", "ground_land"])
def test_a_hold_never_stops_a_safety_or_operator_landing(executors, kind):
    executor, drone, _sent, clock, _ = held_mid_lane(executors)
    executor.tick()
    if kind == "low_battery":
        drone.state.battery = 10
        executor.note_ground_message()
        executor.tick()
    elif kind == "link_loss":
        clock.advance(3.5)
        executor.tick()
    elif kind == "local_key":
        executor.land_now("local_key")
    else:
        send(executor, fp.make_land(cmd_id="l1"))
    assert ("land",) in drone.calls and not drone.state.is_flying
    assert not executor.is_held and not executor.plan_valid


# ---------------------------------------------------------------------------
# A nudge cut short by a landing
# ---------------------------------------------------------------------------


def test_a_nudge_interrupted_by_a_landing_is_refused(executors):
    executor, drone, sent, _, _ = held_mid_lane(executors)
    drone.expect_nudge()
    send(executor, nudge("left", 50, cmd_id="n1"))
    assert wait_for(lambda: drone.waiting)
    send(executor, fp.make_land(cmd_id="l1"))  # interrupts the nudge move
    assert wait_for(lambda: reply_for(sent, "n1") is not None)
    reply = reply_for(sent, "n1")
    assert (reply["type"], reply["reason"]) == ("nack", "landing")
    assert executor.wait_idle(WAIT_S)
    # The controller reported the move executed, so the pose holds it, as it
    # would for a lane move.
    pose = executor.envelope.pose_snapshot()
    assert (pose.x_cm, pose.y_cm) == dead_reckoned(drone)
