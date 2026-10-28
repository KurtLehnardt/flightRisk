"""Manual takeover on the edge lane executor (``flightrisk/drone/lane_manual.py``).

Every executor is the real ``LaneExecutor`` on a fake Tello (``SteppedTello``)
with a fake clock; nothing touches a network or a drone. Covers the hold
point (pose and lane progress, recorded after a move in flight), the limits
on manual steps and the manual takeoff, the dead-man, landing (never gated)
and ``resume_search`` (back to the hold point at the altitude slot, then the
rest of the lanes).
"""
from __future__ import annotations

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone.controller import DroneLinkStatus
from tests.lane_executor_support import (
    LANES,
    SteppedTello,
    fly_to_lane,
    make_v2_executor,
    reply_for,
    send,
    validate_sent,
    wait_for,
)

FENCE = fp.Fence(3, "mac")


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


class LinkTello(SteppedTello):
    """A stepped fake Tello that reports its drone link like the real controller."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.link_ok = True

    def link_status(self):
        if self.link_ok:
            return DroneLinkStatus(True, 0.1, None, None)
        return DroneLinkStatus(False, 12.0, "state_stale", "Tello unreachable (no state for 12 s)")


def hold(cmd_id):
    return fp.make_manual_hold(cmd_id=cmd_id, fence=FENCE)


def move(cmd_id, direction, amount=30):
    if direction in ("cw", "ccw"):
        return fp.make_manual_move(direction, degrees=amount, cmd_id=cmd_id, fence=FENCE)
    return fp.make_manual_move(direction, distance_cm=amount, cmd_id=cmd_id, fence=FENCE)


def resume(cmd_id):
    return fp.make_resume_search(cmd_id=cmd_id, fence=FENCE)


def takeoff(cmd_id):
    return fp.make_manual_takeoff(cmd_id=cmd_id, fence=FENCE)


def answered(sent, cmd_id):
    assert wait_for(lambda: reply_for(sent, cmd_id) is not None), cmd_id
    return reply_for(sent, cmd_id)


def step(executor, sent, msg):
    """Send one manual command and wait until it is answered and its flight sequence has ended."""
    send(executor, msg)
    reply = answered(sent, msg["cmd_id"])
    assert wait_for(lambda: not executor.is_busy)
    return reply


def held_mid_lane(executors, drone=None, **kwargs):
    """Fly to the first lane move (forward 200), take manual control while it flies, let it finish."""
    drone = drone if drone is not None else SteppedTello(name="drone-1", battery=90)
    executor, drone, sent, clock, log = executors(drone=drone, **kwargs)
    fly_to_lane(executor, drone, clock, sent)
    drone.release()  # the climb to the 100 cm slot
    assert wait_for(lambda: len(drone.move_log) == 2 and drone.waiting)
    send(executor, hold("m1"))
    ack = answered(sent, "m1")
    assert ack["type"] == "ack" and ack["detail"] == "finishing_move" and ack["control_mode"] == "manual"
    drone.release()  # the lane move in flight completes; the search stops after it
    assert wait_for(lambda: not executor.is_busy and not drone.busy)
    drone.auto = True
    return executor, drone, sent, clock, log


def calls_after(drone, mark):
    return [c for c in drone.calls[mark:] if c[0] in ("move", "rotate", "takeoff", "land")]


# ---------------------------------------------------------------------------
# Hold point
# ---------------------------------------------------------------------------


def test_hold_records_pose_and_progress_after_the_move_in_flight(executors):
    executor, drone, _sent, _, log = held_mid_lane(executors)
    state = executor.manual_state
    assert (state.hold_pose.x_cm, state.hold_pose.y_cm, state.hold_pose.heading_deg) == (0.0, 200.0, 0.0)
    assert state.hold_step == 1 and state.hold_lane_index == 1
    assert executor.control_mode == "manual" and executor.state == "holding"
    assert "edge_manual_hold_point" in log.names()
    # The search does not advance on its own.
    assert len(drone.move_log) == 2


def test_hold_on_the_ground_only_marks_the_executor_paused(executors):
    executor, drone, sent, _, _ = executors()
    send(executor, hold("m1"))
    ack = answered(sent, "m1")
    assert ack["type"] == "ack" and ack["detail"] == "on_ground"
    assert executor.control_mode == "manual" and executor.manual_state.hold_pose is None
    assert drone.calls == [] or all(c[0] not in ("takeoff", "move") for c in drone.calls)


def test_hold_cancels_a_scheduled_start(executors):
    executor, drone, sent, clock, _ = executors()
    send(executor, fp.make_assign_lanes("drone-1", 100, LANES, {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400},
                                        fence=FENCE, plan_id="p-1", plan_seq=1))
    send(executor, fp.make_start_at(clock() + 5.0, fence=FENCE, plan_id="p-1", cmd_id="s1"))
    send(executor, hold("m1"))
    assert answered(sent, "m1")["detail"] == "start_cancelled"
    assert answered(sent, "s1")["type"] == "nack"
    clock.advance(6.0)
    assert executor.wait_idle(5.0) and not drone.state.is_flying


# ---------------------------------------------------------------------------
# Manual steps and their limits
# ---------------------------------------------------------------------------


def test_manual_steps_are_flown_and_dead_reckoned(executors):
    executor, drone, sent, _, log = held_mid_lane(executors)
    step(executor, sent, move("a", "right", 50))
    assert answered(sent, "a")["type"] == "ack"
    step(executor, sent, move("b", "cw", 30))
    ack = answered(sent, "b")
    assert ack["type"] == "ack" and ack["heading_deg"] == 30.0 and ack["x_cm"] == 50.0
    assert ("rotate", 30) in drone.calls
    events = [f for _, e, f in log.events if e == "edge_manual_move"]
    assert [f["outcome"] for f in events] == ["ok", "ok"]


def test_geofence_refuses_a_manual_step(executors):
    executor, drone, sent, _, _ = held_mid_lane(executors, geofence_cm=450)
    step(executor, sent, move("a", "forward", 100))  # to (0, 300)
    step(executor, sent, move("b", "forward", 100))  # to (0, 400)
    assert answered(sent, "b")["type"] == "ack"
    before = len(drone.calls)
    step(executor, sent, move("c", "forward", 100))  # (0, 500) is outside 450
    nack = answered(sent, "c")
    assert nack["type"] == "nack" and nack["reason"] == "geofence"
    assert nack["geofence_cm"] == 450 and nack["y_cm"] == 500.0 and "450 cm geofence" in nack["detail"]
    assert len(drone.calls) == before  # nothing sent to the drone
    assert drone.state.is_flying  # a refused step never lands


def test_ceiling_and_floor_refuse_vertical_steps(executors):
    executor, drone, sent, _, _ = held_mid_lane(executors)
    assert drone.state.height == 100
    step(executor, sent, move("up", "up", 50))  # 150 > 140 = ceiling 200 - margin 60
    nack = answered(sent, "up")
    assert nack["reason"] == "ceiling" and nack["max_alt_slot_cm"] == 140 and "ceiling limit 140" in nack["detail"]
    step(executor, sent, move("up2", "up", 40))  # exactly at the limit is allowed
    assert answered(sent, "up2")["type"] == "ack" and drone.state.height == 140
    step(executor, sent, move("down", "down", 100))  # 40 is the floor: 140 - 100 = 40 is allowed
    assert answered(sent, "down")["type"] == "ack"
    step(executor, sent, move("down2", "down", 20))
    nack = answered(sent, "down2")
    assert nack["reason"] == "floor" and nack["floor_cm"] == fp.MANUAL_FLOOR_CM


def test_unknown_height_refuses_vertical_steps(executors):
    executor, drone, sent, _, _ = held_mid_lane(executors)
    drone.state.height = None
    step(executor, sent, move("u", "up", 20))
    assert answered(sent, "u")["reason"] == "ceiling"
    step(executor, sent, move("d", "down", 20))
    assert answered(sent, "d")["reason"] == "floor"


def test_a_dead_drone_link_refuses_steps_but_never_landing(executors):
    executor, drone, sent, _, _ = held_mid_lane(executors, drone=LinkTello(name="drone-1", battery=90))
    drone.link_ok = False
    step(executor, sent, move("a", "left", 30))
    nack = answered(sent, "a")
    assert nack["reason"] == "drone_unreachable" and "unreachable" in nack["detail"]
    send(executor, fp.make_manual_land(cmd_id="l1"))
    assert answered(sent, "l1")["type"] == "ack"
    assert ("land",) in drone.calls and not drone.state.is_flying


def test_steps_need_manual_control(executors):
    executor, _, sent, _, _ = executors()
    step(executor, sent, move("a", "left", 30))
    assert answered(sent, "a")["reason"] == "not_manual"


def test_a_manual_cmd_id_runs_at_most_once(executors):
    executor, drone, sent, _, _ = held_mid_lane(executors)
    msg = move("a", "left", 30)
    send(executor, msg)
    first = answered(sent, "a")
    before = len(drone.calls)
    send(executor, msg)
    assert len(drone.calls) == before
    assert [m for m in sent if m.get("cmd_id") == "a"] == [first, first]


# ---------------------------------------------------------------------------
# Manual takeoff and land
# ---------------------------------------------------------------------------


def test_manual_takeoff_uses_the_takeoff_preflight_once_and_climbs_to_the_slot(executors):
    executor, drone, sent, _, _log = executors(drone=LinkTello(name="drone-1", battery=90, auto=True))
    send(executor, hold("m1"))
    step(executor, sent, takeoff("t1"))
    ack = answered(sent, "t1")
    assert ack["type"] == "ack" and ack["height_cm"] == 100
    assert [c for c in drone.calls if c[0] == "takeoff"] == [("takeoff",)]  # never a second takeoff
    assert ("move", "up", 20) in drone.calls  # 80 cm takeoff height -> 100 cm slot
    assert executor.control_mode == "manual" and drone.state.is_flying
    # Still paused: nothing flies on its own after a manual takeoff.
    assert executor.state == "holding"


def test_manual_takeoff_is_refused_by_the_same_preflight(executors):
    executor, drone, sent, _, _ = executors(drone=LinkTello(name="drone-1", battery=90, auto=True))
    send(executor, hold("m1"))
    drone.link_ok = False
    step(executor, sent, takeoff("t1"))
    assert answered(sent, "t1")["reason"] == "drone_unreachable"
    drone.link_ok = True
    drone.state.battery = 10
    step(executor, sent, takeoff("t2"))
    assert answered(sent, "t2")["reason"] == "battery"
    assert not any(c[0] == "takeoff" for c in drone.calls)


def test_autonomous_takeoff_and_start_are_refused_under_manual_control(executors):
    executor, drone, sent, _, _ = executors()
    send(executor, hold("m1"))
    send(executor, fp.make_takeoff(fence=FENCE, cmd_id="t1"))
    nack = answered(sent, "t1")
    assert nack["type"] == "nack" and nack["reason"] == "busy" and nack["detail"] == "manual_control"
    assert not drone.state.is_flying


def test_manual_land_keeps_the_executor_paused_until_resume_search(executors):
    executor, drone, sent, _, log = held_mid_lane(executors)
    send(executor, fp.make_manual_land(cmd_id="l1"))
    assert answered(sent, "l1")["type"] == "ack"
    assert ("edge_land" in log.names()) and any(
        f.get("reason") == "manual_land" for _, e, f in log.events if e == "edge_land")
    assert not drone.state.is_flying and executor.control_mode in ("manual", "held")
    # Landing ended the plan: resume_search only releases.
    send(executor, resume("r1"))
    ack = answered(sent, "r1")
    assert ack["type"] == "ack" and ack["detail"] == "no_plan" and executor.control_mode == "autonomous"


# ---------------------------------------------------------------------------
# Dead-man and link loss
# ---------------------------------------------------------------------------


def test_dead_man_holds_without_landing_while_heartbeats_keep_the_link(executors):
    executor, drone, sent, clock, log = held_mid_lane(executors)
    for _ in range(5):
        clock.advance(1.0)
        send(executor, {"type": "heartbeat", "seq": 1})
        executor.tick()
    assert executor.control_mode == "held"
    assert "edge_manual_deadman_hold" in log.names()
    assert drone.state.is_flying and ("land",) not in drone.calls
    telemetry = executor.telemetry()
    assert telemetry["control_mode"] == "held" and telemetry["is_flying"] is True
    # The next manual command makes it manual again.
    step(executor, sent, move("a", "left", 30))
    answered(sent, "a")
    assert executor.control_mode == "manual"


def test_link_loss_still_lands_a_manually_controlled_drone(executors):
    executor, drone, _sent, clock, log = held_mid_lane(executors)
    clock.advance(executor.envelope.link_watchdog.timeout_s + 0.5)
    executor.tick()
    assert ("land",) in drone.calls
    assert any(f.get("reason") == "link_loss" for _, e, f in log.events if e == "edge_land")


# ---------------------------------------------------------------------------
# resume_search
# ---------------------------------------------------------------------------


def test_resume_search_returns_to_the_hold_point_at_the_slot_and_continues_the_lanes(executors):
    executor, drone, sent, _, log = held_mid_lane(executors)
    step(executor, sent, move("a", "right", 50))
    step(executor, sent, move("b", "cw", 30))
    step(executor, sent, move("c", "up", 20))
    assert answered(sent, "c")["type"] == "ack" and drone.state.height == 120
    mark = len(drone.calls)
    send(executor, resume("r1"))
    ack = answered(sent, "r1")
    assert ack["type"] == "ack" and ack["detail"] == "returning"
    assert (ack["target_x_cm"], ack["target_y_cm"]) == (0.0, 200.0)
    assert executor.control_mode == "autonomous"
    assert wait_for(lambda: executor.lane_index == 3 and not executor.is_busy)
    assert calls_after(drone, mark) == [
        ("move", "down", 20),      # back to the 100 cm altitude slot first
        ("rotate", -30),           # the heading at the hold
        ("move", "left", 50),      # the position at the hold
        ("move", "right", 100),    # then lane 1 and lane 2, from the recorded progress
        ("move", "back", 200),
    ]
    pose = executor.envelope.pose_snapshot()
    assert (round(pose.x_cm), round(pose.y_cm), round(pose.heading_deg)) == (100, 0, 0)
    assert "edge_resume_search_returned" in log.names()


def test_resume_search_from_outside_the_fence_uses_the_nearest_allowed_point(executors):
    executor, _drone, sent, _, _ = held_mid_lane(executors, geofence_cm=450)
    with executor._lock:
        executor._manual.hold_pose.x_cm = -600.0  # as if the fence had shrunk since the hold
    send(executor, resume("r1"))
    ack = answered(sent, "r1")
    assert ack["detail"] == "returning_nearest" and ack["target_x_cm"] == -450.0


def test_resume_search_refuses_a_plan_that_would_leave_the_fence(executors):
    executor, _drone, sent, _, _ = held_mid_lane(executors, geofence_cm=450)
    with executor._lock:
        executor._manual.hold_pose.y_cm = 440.0  # the remaining "right 100, back 200" stays inside
        executor._manual.hold_pose.x_cm = 400.0  # but right 100 from x=400 reaches 500
    send(executor, resume("r1"))
    nack = answered(sent, "r1")
    assert nack["type"] == "nack" and nack["reason"] == "geofence"
    assert executor.control_mode == "manual"  # still under manual control, hovering


def test_a_new_manual_hold_interrupts_the_return_and_keeps_the_original_hold_point(executors):
    executor, drone, sent, _, _ = held_mid_lane(executors)
    step(executor, sent, move("a", "right", 50))
    answered(sent, "a")
    drone.auto = False
    send(executor, resume("r1"))
    answered(sent, "r1")
    assert wait_for(lambda: drone.waiting)  # the first return move (left 50) is in flight
    send(executor, hold("m2"))
    drone.release()
    assert wait_for(lambda: not executor.is_busy)
    state = executor.manual_state
    assert (state.hold_pose.x_cm, state.hold_pose.y_cm) == (0.0, 200.0)
    assert state.hold_step == 1


def test_a_retask_while_held_is_stored_and_flown_only_after_resume_search(executors):
    executor, _drone, sent, _, _ = held_mid_lane(executors)
    lanes = LANES + [{"direction": "left", "distance_cm": 100}]
    send(executor, fp.make_assign_lanes("drone-1", 100, lanes, {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400},
                                        fence=FENCE, plan_id="p-2", plan_seq=2, cmd_id="a2"))
    assert answered(sent, "a2")["type"] == "ack"
    assert not executor.is_busy and executor.control_mode == "manual"
    send(executor, resume("r1"))
    answered(sent, "r1")
    assert wait_for(lambda: executor.lane_index == 4 and not executor.is_busy)


# ---------------------------------------------------------------------------
# Routing on the edge
# ---------------------------------------------------------------------------


def test_edge_device_and_uplink_route_manual_commands_to_the_executor(executors):
    from flightrisk.edge_device import _handle_control
    from flightrisk.edge_uplink import LaneExecutorHooks
    from tests.lane_executor_support import QuietLogger

    executor, _, sent, _, _ = executors()
    _handle_control(hold("m1"), runner=None, log=QuietLogger(), flight=executor, recv_t=executor.now())
    assert answered(sent, "m1")["type"] == "ack" and executor.control_mode == "manual"
    hooks = LaneExecutorHooks(executor)
    assert all(hooks.supports(mtype) for mtype in fp.MANUAL_COMMAND_TYPES)
