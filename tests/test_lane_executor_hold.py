"""Protocol v2 hold, resume, nudge and end-of-lanes behaviour of the lane executor.

Every executor here is the real ``LaneExecutor`` on a ``SteppedTello`` (a
``FakeTelloController`` whose moves finish only when the test releases them,
so a move can be held "in flight") with a fake clock. Flight sequences run on
the executor's worker thread, so the tests wait for them with generous
timeouts. Every message the executor sends is checked against the protocol v2
validators.
"""
from __future__ import annotations

import threading

import pytest

from flightrisk.drone.controller import CommandOutcome, CommandResult
from flightrisk.drone.fake import FakeDrone, FakeDroneConfig
from tests.lane_executor_support import (
    LANES,
    WAIT_S,
    SteppedTello,
    assign,
    dead_reckoned,
    fly_to_lane,
    hold,
    horizontal_moves,
    make_v2_executor,
    nudge,
    of_type,
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
    """Factory for v2 executors; shuts every executor down afterwards."""
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


def held_mid_lane(executors, lanes=LANES, **kwargs):
    """Fly to the first lane move, hold while it is in flight, and let it finish."""
    executor, drone, sent, clock, log = executors(drone=stepped(), **kwargs)
    fly_to_lane(executor, drone, clock, sent, lanes=lanes)
    drone.release()  # the climb to the slot
    assert wait_for(lambda: len(drone.move_log) == 2 and drone.waiting)
    send(executor, hold(cmd_id="h1", reason="alert"))
    ack = reply_for(sent, "h1")
    assert ack["type"] == "ack" and ack["detail"] == "finishing_move"
    drone.release()  # the move in flight completes
    assert wait_for(lambda: not executor.is_busy and not drone.busy)
    return executor, drone, sent, clock, log


# ---------------------------------------------------------------------------
# hold
# ---------------------------------------------------------------------------


def test_hold_lets_the_move_in_flight_finish_then_sends_nothing(executors):
    executor, drone, _sent, clock, _ = held_mid_lane(executors)

    assert executor.is_held and executor.state == "holding"
    assert horizontal_moves(drone) == [("move", "forward", 200)]
    # The completed move is in the pose; the lane index is the next lane.
    assert executor.envelope.pose_snapshot().y_cm == pytest.approx(200)
    assert executor.lane_index == 1
    # Nothing else is sent, however long the hold lasts (no stop, no hover).
    for _ in range(20):
        clock.advance(1.0)
        executor.note_ground_message()
        executor.tick()
    assert drone.calls == [("takeoff",), ("move", "up", 20), ("move", "forward", 200)]
    telemetry = executor.telemetry()
    assert telemetry["state"] == "holding" and telemetry["plan_valid"] is True and telemetry["plan_id"] == "p-1"


def test_resume_continues_the_same_plan_at_the_held_lane(executors):
    executor, drone, sent, _, _ = held_mid_lane(executors)
    drone.auto = True
    send(executor, resume("p-1", cmd_id="r1"))
    ack = reply_for(sent, "r1")
    assert ack["type"] == "ack" and ack["plan_id"] == "p-1"
    assert wait_for(lambda: executor.lane_index == 3)
    assert executor.wait_idle(WAIT_S)
    assert horizontal_moves(drone) == [("move", "forward", 200), ("move", "right", 100), ("move", "back", 200)]
    assert dead_reckoned(drone) == (100, 0)
    pose = executor.envelope.pose_snapshot()
    assert (pose.x_cm, pose.y_cm) == (pytest.approx(100), pytest.approx(0))
    assert not executor.is_held and executor.state == "hovering"


def test_hold_with_no_move_in_flight_has_no_detail(executors):
    executor, drone, sent, _, _ = executors()
    send(executor, takeoff(cmd_id="t1"))
    assert executor.wait_idle(WAIT_S)
    assert reply_for(sent, "t1")["type"] == "ack"
    send(executor, hold(cmd_id="h1"))
    ack = reply_for(sent, "h1")
    assert ack["type"] == "ack" and "detail" not in ack
    # A second hold is acknowledged too and changes nothing.
    send(executor, hold(cmd_id="h2"))
    assert reply_for(sent, "h2")["type"] == "ack"
    assert executor.is_held
    # Resume of a drone that only hovered restarts nothing.
    send(executor, assign(cmd_id="a1"))
    send(executor, resume("p-1", cmd_id="r1"))
    assert reply_for(sent, "r1")["type"] == "ack"
    assert not executor.is_held and executor.wait_idle(WAIT_S)
    assert horizontal_moves(drone) == []


def test_hold_refused_on_the_ground_and_while_landing(executors):
    executor, drone, sent, _, _ = executors()
    send(executor, hold(cmd_id="h1"))
    assert reply_for(sent, "h1")["reason"] == "not_flying"
    assert not executor.is_held

    drone.takeoff()
    entered, release = threading.Event(), threading.Event()
    real_land = drone.land

    def slow_land():
        entered.set()
        release.wait(WAIT_S)
        real_land()

    drone.land = slow_land
    lander = threading.Thread(target=executor.land_now, args=("ground_land",))
    lander.start()
    assert entered.wait(WAIT_S)
    send(executor, hold(cmd_id="h2"))
    assert reply_for(sent, "h2")["reason"] == "landing"
    assert executor.state == "landing"
    release.set()
    lander.join(WAIT_S)
    assert not executor.is_held


def test_hold_cancels_a_pending_start(executors):
    executor, drone, sent, clock, _ = executors()
    send(executor, assign(cmd_id="a1"))
    t_ground = clock() + 5.0
    send(executor, start_at(t_ground, cmd_id="s1"))
    send(executor, hold(cmd_id="h1"))
    assert reply_for(sent, "h1")["detail"] == "start_cancelled"
    assert wait_for(lambda: reply_for(sent, "s1") is not None)
    nack = reply_for(sent, "s1")
    assert (nack["type"], nack["reason"], nack["detail"]) == ("nack", "busy", "cancelled_by_hold")
    assert executor.wait_idle(WAIT_S)
    clock.set(t_ground + 1.0)
    assert not wait_for(lambda: drone.calls, timeout=0.2)
    # The plan is still valid: a new start flies it.
    assert executor.plan_valid and not executor.is_held
    send(executor, start_at(clock() + 0.5, cmd_id="s2"))
    clock.advance(0.5)
    assert wait_for(lambda: executor.lane_index == 3)


class GatedTakeoffTello(SteppedTello):
    """Stepped Tello whose takeoff waits until the test releases it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.takeoff_entered = threading.Event()
        self.takeoff_release = threading.Event()

    def takeoff(self, abort=None):
        self.takeoff_entered.set()
        self.takeoff_release.wait(WAIT_S)
        return super().takeoff(abort=abort)


@pytest.mark.parametrize("via", ["takeoff", "start_at"])
def test_hold_during_takeoff_stops_before_the_climb(executors, via):
    drone = GatedTakeoffTello(name="drone-1", battery=90, auto=True)
    executor, drone, sent, clock, _ = executors(drone=drone)
    send(executor, assign(cmd_id="a1"))
    if via == "takeoff":
        send(executor, takeoff(cmd_id="c1"))
    else:
        send(executor, start_at(clock() + 0.5, cmd_id="c1"))
        clock.advance(0.5)
    assert drone.takeoff_entered.wait(WAIT_S)
    assert executor.state == "taking_off"
    send(executor, hold(cmd_id="h1"))
    assert reply_for(sent, "h1")["detail"] == "finishing_move"
    drone.takeoff_release.set()
    assert wait_for(lambda: not executor.is_busy)
    # The takeoff (or start) executed and is acknowledged; nothing moved after it.
    assert reply_for(sent, "c1")["type"] == "ack"
    assert drone.calls == [("takeoff",)]
    assert executor.state == "holding" and executor.telemetry()["height_cm"] == 80
    send(executor, resume("p-1", cmd_id="r1"))
    assert reply_for(sent, "r1")["type"] == "ack"
    assert executor.wait_idle(WAIT_S)
    if via == "takeoff":
        assert drone.calls == [("takeoff",), ("move", "up", 20)]
    else:
        assert horizontal_moves(drone) == [("move", "forward", 200), ("move", "right", 100), ("move", "back", 200)]


def test_landing_clears_a_hold_and_the_plan(executors):
    executor, drone, sent, _, _ = held_mid_lane(executors)
    send(executor, {"type": "land", "cmd_id": "l1"})
    assert ("land",) in drone.calls and not drone.state.is_flying
    assert of_type(sent, "ack", "land")[-1]["cmd_id"] == "l1"
    assert not executor.is_held and not executor.plan_valid
    send(executor, resume("p-1", cmd_id="r1"))
    assert reply_for(sent, "r1")["reason"] == "no_valid_plan"


# ---------------------------------------------------------------------------
# resume refusals
# ---------------------------------------------------------------------------


def test_resume_of_another_plan_is_refused_and_the_drone_keeps_holding(executors):
    executor, drone, sent, clock, _ = held_mid_lane(executors)
    send(executor, resume("p-0", cmd_id="r1"))
    assert reply_for(sent, "r1")["reason"] == "plan_mismatch"
    bad = {"type": "resume", "plan_id": "not an id!", "epoch": 3, "leader_id": "mac", "cmd_id": "r2"}
    send(executor, bad)
    assert reply_for(sent, "r2")["reason"] == "invalid"
    # resume carries no cmd_id: refused without a reply, never executed.
    send(executor, {"type": "resume", "plan_id": "p-1", "epoch": 3, "leader_id": "mac"})
    clock.advance(1.0)
    executor.note_ground_message()
    executor.tick()
    assert executor.is_held and not executor.is_busy
    assert horizontal_moves(drone) == [("move", "forward", 200)]


def test_resume_refused_on_the_ground_and_when_not_holding(executors):
    executor, drone, sent, _, _ = executors()
    send(executor, assign(cmd_id="a1"))
    send(executor, resume("p-1", cmd_id="r1"))
    assert reply_for(sent, "r1")["reason"] == "not_flying"
    send(executor, takeoff(cmd_id="t1"))
    assert executor.wait_idle(WAIT_S)
    send(executor, resume("p-1", cmd_id="r2"))
    ack = reply_for(sent, "r2")
    assert ack["type"] == "ack" and ack["detail"] == "not_holding"
    assert horizontal_moves(drone) == []


def test_resume_refused_while_the_rest_of_the_path_leaves_the_geofence(executors):
    # Geofence 300 cm; one lane forward 250 cm. A nudge forward while holding
    # would take the rest of the lane to y = 350.
    executor, drone, sent, _, _ = held_mid_lane(
        executors, lanes=[{"direction": "forward", "distance_cm": 100}, {"direction": "forward", "distance_cm": 150}],
        geofence_cm=300,
    )
    drone.auto = True
    send(executor, nudge("forward", 100, cmd_id="n1"))
    assert wait_for(lambda: reply_for(sent, "n1") is not None)
    assert reply_for(sent, "n1")["type"] == "ack"
    send(executor, resume("p-1", cmd_id="r1"))
    assert reply_for(sent, "r1")["reason"] == "geofence"
    assert executor.is_held and executor.wait_idle(WAIT_S)
    send(executor, nudge("back", 100, cmd_id="n2"))
    assert wait_for(lambda: reply_for(sent, "n2") is not None)
    send(executor, resume("p-1", cmd_id="r2"))
    assert reply_for(sent, "r2")["type"] == "ack"
    assert executor.wait_idle(WAIT_S)
    assert horizontal_moves(drone)[-1] == ("move", "forward", 150)
    assert executor.envelope.pose_snapshot().y_cm == pytest.approx(250)


# ---------------------------------------------------------------------------
# nudge
# ---------------------------------------------------------------------------


def test_nudge_while_holding_is_flown_recorded_and_keeps_the_hold(executors):
    executor, drone, sent, _, _ = held_mid_lane(executors)
    drone.auto = True
    send(executor, nudge("left", 50, cmd_id="n1"))
    assert wait_for(lambda: reply_for(sent, "n1") is not None)
    assert reply_for(sent, "n1")["type"] == "ack"
    assert executor.is_held and executor.state == "holding"
    assert dead_reckoned(drone) == (-50, 200)
    pose = executor.envelope.pose_snapshot()
    assert (pose.x_cm, pose.y_cm) == (pytest.approx(-50), pytest.approx(200))
    # The lanes go on from the nudged position.
    send(executor, resume("p-1", cmd_id="r1"))
    assert executor.wait_idle(WAIT_S) and wait_for(lambda: executor.lane_index == 3)
    pose = executor.envelope.pose_snapshot()
    assert (pose.x_cm, pose.y_cm) == (pytest.approx(50), pytest.approx(0))


def test_nudge_while_hovering_idle(executors):
    executor, drone, sent, _, _ = executors()
    send(executor, takeoff(cmd_id="t1"))
    assert executor.wait_idle(WAIT_S)
    send(executor, nudge("right", 120, cmd_id="n1"))
    assert wait_for(lambda: reply_for(sent, "n1") is not None)
    assert reply_for(sent, "n1")["type"] == "ack"
    assert horizontal_moves(drone) == [("move", "right", 120)]
    assert executor.envelope.pose_snapshot().x_cm == pytest.approx(120)


def test_nudge_refusals(executors):
    executor, drone, sent, clock, _ = executors(drone=stepped(), geofence_cm=300)
    send(executor, nudge(cmd_id="n1"))
    assert reply_for(sent, "n1")["reason"] == "not_flying"

    fly_to_lane(executor, drone, clock, sent)
    send(executor, nudge(cmd_id="n2"))
    assert reply_for(sent, "n2")["reason"] == "busy"  # flying lanes
    drone.release()
    assert wait_for(lambda: len(drone.move_log) == 2)
    send(executor, hold(cmd_id="h1"))
    drone.release()
    assert wait_for(lambda: not executor.is_busy)

    for index, (direction, distance) in enumerate([("up", 50), ("left", 10), ("left", 501), ("left", 50.5), ("left", True)]):
        msg = {"type": "nudge", "direction": direction, "distance_cm": distance, "epoch": 3, "leader_id": "mac",
               "cmd_id": f"bad{index}"}
        send(executor, msg)
        assert reply_for(sent, f"bad{index}")["reason"] == "invalid"
    send(executor, nudge("forward", 150, cmd_id="n3"))  # y would be 350 > 300
    assert reply_for(sent, "n3")["reason"] == "geofence"
    assert horizontal_moves(drone) == [("move", "forward", 200)]


def test_resume_refused_while_a_nudge_flies(executors):
    executor, drone, sent, _, _ = held_mid_lane(executors)
    drone.expect_nudge()
    send(executor, nudge("left", 50, cmd_id="n1"))
    assert wait_for(lambda: drone.waiting)
    send(executor, resume("p-1", cmd_id="r1"))
    assert reply_for(sent, "r1")["reason"] == "busy"
    drone.release()
    assert wait_for(lambda: reply_for(sent, "n1") is not None)
    assert executor.is_held


class _DropsMoves(FakeDrone):
    def move(self, direction, distance_cm):
        self.calls.append(("move", direction, distance_cm))
        return CommandResult("move", CommandOutcome.DROPPED_BUSY, "another command in flight")


def test_nudge_not_executed_is_not_recorded_and_does_not_land(executors):
    drone = _DropsMoves("drone-1", config=FakeDroneConfig())
    executor, drone, sent, _, _ = executors(drone=drone)
    drone.takeoff()  # the sim drone takes off to 100 cm, its slot
    send(executor, nudge("left", 50, cmd_id="n1"))
    assert wait_for(lambda: reply_for(sent, "n1") is not None)
    nack = reply_for(sent, "n1")
    assert (nack["reason"], nack["detail"]) == ("busy", "dropped_busy")
    assert executor.envelope.pose_snapshot().x_cm == 0.0
    assert drone.snapshot().is_flying and ("land",) not in drone.calls


def test_nudge_controller_error_lands(executors):
    class Raising(SteppedTello):
        def move(self, direction, distance_cm):
            raise RuntimeError("no reply")

    executor, drone, sent, _, _ = executors(drone=Raising(name="drone-1", battery=90, auto=True))
    drone.state.is_flying = True
    drone.state.height = 100
    send(executor, nudge("left", 50, cmd_id="n1"))
    assert wait_for(lambda: reply_for(sent, "n1") is not None)
    assert reply_for(sent, "n1")["reason"] == "landing"
    assert ("land",) in drone.calls and not executor.plan_valid


# ---------------------------------------------------------------------------
# End of lanes: on_complete and hover_timeout_s
# ---------------------------------------------------------------------------


def run_lanes(executor, clock, **assign_kwargs):
    send(executor, assign(cmd_id="a1", **assign_kwargs))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)
    assert wait_for(lambda: executor.lane_index == 3 and not executor.is_busy)


def ticks(executor, clock, seconds: float, step: float = 0.5) -> None:
    for _ in range(round(seconds / step)):
        clock.advance(step)
        executor.note_ground_message()
        executor.tick()


def test_on_complete_land_lands_after_the_last_lane(executors):
    executor, drone, _, clock, log = executors()
    run_lanes(executor, clock, on_complete="land")
    assert wait_for(lambda: ("land",) in drone.calls)
    assert not executor.plan_valid
    assert any(e == "edge_land" and kw["reason"] == "on_complete" for _, e, kw in log.events)


def test_on_complete_hover_lands_after_the_default_20_s(executors):
    executor, drone, _, clock, log = executors()
    run_lanes(executor, clock, on_complete="hover")
    ticks(executor, clock, 19.5)
    assert drone.state.is_flying
    ticks(executor, clock, 1.0)
    assert not drone.state.is_flying and not executor.plan_valid
    assert any(e == "edge_land" and kw["reason"] == "hover_timeout" for _, e, kw in log.events)


def test_hover_timeout_is_configurable_and_suspended_by_a_hold(executors):
    executor, drone, _sent, clock, _ = executors()
    run_lanes(executor, clock, on_complete="hover", hover_timeout_s=5)
    ticks(executor, clock, 3.0)
    send(executor, hold(cmd_id="h1"))
    ticks(executor, clock, 60.0)
    assert drone.state.is_flying and executor.state == "holding"
    send(executor, resume("p-1", cmd_id="r1"))
    assert executor.wait_idle(WAIT_S)
    ticks(executor, clock, 4.0)
    assert drone.state.is_flying  # the countdown started again after the resume
    ticks(executor, clock, 2.0)
    assert not drone.state.is_flying


def test_v1_plan_hovers_after_its_lanes_until_landed(executors):
    executor, drone, _, clock, _ = executors()
    executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": LANES})
    executor.handle_start_at({"type": "start_at", "t_ground_s": clock() + 0.5})
    clock.advance(0.5)
    assert wait_for(lambda: executor.lane_index == 3 and not executor.is_busy)
    ticks(executor, clock, 200.0, step=2.0)
    assert drone.state.is_flying


def test_hold_during_the_last_move_holds_before_the_on_complete_landing(executors):
    executor, drone, sent, clock, _ = executors(drone=stepped())
    fly_to_lane(executor, drone, clock, sent, lanes=LANES[:1], on_complete="land")
    drone.release()  # climb
    assert wait_for(lambda: len(drone.move_log) == 2 and drone.waiting)
    send(executor, hold(cmd_id="h1"))
    drone.release()
    assert wait_for(lambda: not executor.is_busy)
    assert ("land",) not in drone.calls and executor.is_held
    send(executor, resume("p-1", cmd_id="r1"))
    assert wait_for(lambda: ("land",) in drone.calls)


def test_a_retask_that_arrives_during_a_nudge_is_flown_after_it(executors):
    executor, drone, sent, clock, _ = executors(drone=stepped())
    send(executor, assign(lanes=LANES[:1], cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)
    drone.release(2)  # climb and the lane
    assert wait_for(lambda: executor.lane_index == 1 and not executor.is_busy)
    send(executor, nudge("left", 50, cmd_id="n1"))
    assert wait_for(lambda: drone.waiting)
    retask = LANES[:1] + [{"direction": "right", "distance_cm": 100}]
    send(executor, assign(lanes=retask, plan_id="p-2", plan_seq=2, cmd_id="a2"))
    assert reply_for(sent, "a2")["type"] == "ack"
    drone.auto = True
    drone.release()
    assert wait_for(lambda: executor.lane_index == 2 and not executor.is_busy)
    assert horizontal_moves(drone) == [("move", "forward", 200), ("move", "left", 50), ("move", "right", 100)]
    pose = executor.envelope.pose_snapshot()
    assert (pose.x_cm, pose.y_cm) == (pytest.approx(50), pytest.approx(200))
