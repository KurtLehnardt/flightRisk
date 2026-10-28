"""Hold, resume, nudge and stale plans with the real Tello driver on the wire-level fake.

These run a real ``LaneExecutor`` over a real ``TelloController`` whose
djitellopy client talks to ``FakeTelloWire`` (``tests/conftest.py``), so the
assertions are about the exact Tello SDK commands that reach the UDP socket.
A command the wire is told to ``hold`` stays unanswered until the test
releases it, which is how a move is kept "in flight". The battery and height
readers are stubbed (no state stream), so a land is confirmed by its reply.

The point of these tests: a ``hold`` never puts ``stop`` (or anything else)
on the wire, the move already on the wire completes, nothing follows it until
``resume``, and after a landing the old plan cannot put a ``takeoff`` on the
wire again.
"""
from __future__ import annotations

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from tests.conftest import TELLO_FLIGHT_WORDS
from tests.lane_executor_support import (
    WAIT_S,
    FakeClock,
    QuietLogger,
    assign,
    hold,
    nudge,
    reply_for,
    resume,
    send,
    start_at,
    validate_sent,
    wait_for,
)

LANES = [{"direction": "forward", "distance_cm": 100}, {"direction": "right", "distance_cm": 100}]


@pytest.fixture
def wired(tello_on_wire):
    """An executor on a TelloController over the fake wire; lands and shuts down afterwards."""
    made = []

    def _make(hold_commands=None):
        ctrl, wire = tello_on_wire(hold=set(hold_commands or ()))
        ctrl.read_battery = lambda: 90
        ctrl.read_height_cm = lambda: 100 if ctrl.state.is_flying else 0
        clock = FakeClock()
        sent: list[dict] = []
        executor = LaneExecutor(
            ctrl, SafetyEnvelope(alt_slot_cm=100, clock=clock), "drone-1", send=sent.append,
            clock=clock, logger=QuietLogger(), fleet_v2=True,
        )
        executor.handle_clock({"type": "clock", "offset_s": 0.0})
        made.append((executor, sent))
        return executor, ctrl, wire, sent, clock

    yield _make
    for executor, sent in made:
        executor.shutdown(timeout=WAIT_S)
        validate_sent(sent)


def flight_words(wire) -> list[str]:
    return [c for c in wire.sent if c.split()[0] in TELLO_FLIGHT_WORDS | {"stop", "land"}]


def start(executor, clock, plan_id="p-1", cmd_id="s1"):
    send(executor, start_at(clock() + 0.5, plan_id=plan_id, cmd_id=cmd_id))
    clock.advance(0.5)


def test_hold_on_the_wire_sends_no_stop_and_nothing_after_the_move(wired):
    executor, _ctrl, wire, sent, clock = wired(hold_commands={"forward 100"})
    send(executor, assign(lanes=LANES, cmd_id="a1"))
    start(executor, clock)
    assert wire.entered.wait(WAIT_S)  # "forward 100" is on the wire, unanswered
    send(executor, hold(cmd_id="h1", reason="alert"))
    assert reply_for(sent, "h1")["detail"] == "finishing_move"
    assert "stop" not in wire.sent

    wire.release("forward 100")
    assert wait_for(lambda: not executor.is_busy)
    assert flight_words(wire) == ["takeoff", "forward 100"]
    assert executor.state == "holding" and executor.envelope.pose_snapshot().y_cm == pytest.approx(100)

    send(executor, resume("p-1", cmd_id="r1"))
    assert reply_for(sent, "r1")["type"] == "ack"
    assert wait_for(lambda: executor.lane_index == 2 and not executor.is_busy)
    assert flight_words(wire) == ["takeoff", "forward 100", "right 100"]
    pose = executor.envelope.pose_snapshot()
    assert (pose.x_cm, pose.y_cm) == (pytest.approx(100), pytest.approx(100))


def test_nudge_while_holding_on_the_wire(wired):
    executor, _ctrl, wire, sent, clock = wired(hold_commands={"forward 100"})
    send(executor, assign(lanes=LANES, cmd_id="a1"))
    start(executor, clock)
    assert wire.entered.wait(WAIT_S)
    send(executor, hold(cmd_id="h1"))
    wire.release("forward 100")
    assert wait_for(lambda: not executor.is_busy)
    send(executor, nudge("left", 50, cmd_id="n1"))
    assert wait_for(lambda: reply_for(sent, "n1") is not None)
    assert reply_for(sent, "n1")["type"] == "ack"
    assert flight_words(wire) == ["takeoff", "forward 100", "left 50"]
    assert executor.is_held and executor.envelope.pose_snapshot().x_cm == pytest.approx(-50)


def test_land_while_holding_then_the_old_plan_never_takes_off_again(wired):
    executor, ctrl, wire, sent, clock = wired(hold_commands={"forward 100"})
    send(executor, assign(lanes=LANES, cmd_id="a1"))
    start(executor, clock)
    assert wire.entered.wait(WAIT_S)
    send(executor, hold(cmd_id="h1"))
    wire.release("forward 100")
    assert wait_for(lambda: not executor.is_busy)

    send(executor, fp.make_land(cmd_id="l1"))
    assert not ctrl.state.is_flying and "land" in wire.sent
    assert not executor.is_held and not executor.plan_valid
    landed = list(wire.sent)

    # A new leader (or a retry) that still has the old plan cannot restart it.
    send(executor, assign(lanes=LANES, cmd_id="a1-late"))
    assert reply_for(sent, "a1-late")["reason"] == "stale_plan"
    start(executor, clock, cmd_id="s2")
    assert reply_for(sent, "s2")["reason"] == "no_valid_plan"
    send(executor, resume("p-1", cmd_id="r1"))
    assert reply_for(sent, "r1")["reason"] == "no_valid_plan"
    clock.advance(1.0)
    assert wire.sent == landed

    # A new plan flies.
    send(executor, assign(lanes=LANES[:1], plan_id="p-2", plan_seq=2, cmd_id="a2"))
    wire.hold.clear()
    start(executor, clock, plan_id="p-2", cmd_id="s3")
    assert wait_for(lambda: reply_for(sent, "s3") is not None and not executor.is_busy)
    assert flight_words(wire)[len(flight_words(wire)) - 2:] == ["takeoff", "forward 100"]
