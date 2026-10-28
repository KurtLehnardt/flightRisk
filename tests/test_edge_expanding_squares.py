"""An edge flies the coordinator's expanding squares: 50, 100 and 150 cm, closes each, then lands.

The lanes come straight from ``FleetCoordinator``; the edge is a real ``LaneExecutor`` on an in-process
Tello. The geofence is 160 cm, as for the demo.
"""
from __future__ import annotations

import pytest

from flightrisk.drone.coordinator import FleetCoordinator
from tests.lane_executor_support import (
    assign,
    dead_reckoned,
    horizontal_moves,
    make_v2_executor,
    reply_for,
    send,
    start_at,
    validate_sent,
    wait_for,
)
from tests.test_coordinator import FakeClock as CoordClock
from tests.test_coordinator import FakeEdge

WAIT_S = 10.0


def square_lanes():
    clock = CoordClock()
    edges = {"drone-1": FakeEdge("drone-1", clock, 0.0)}
    coord = FleetCoordinator(
        edges, area_cm=(150, 150), strip_cm=50, clock=clock, pattern="expanding-squares", on_complete="land"
    )
    coord.plan()
    (msg,) = edges["drone-1"].of_type("assign_lanes")
    return msg["lanes"], msg["on_complete"]


@pytest.fixture
def executors():
    created = []

    def _make(**kwargs):
        made = make_v2_executor(**kwargs)
        created.append(made)
        return made

    yield _make
    for executor, _drone, sent, _, _ in created:
        executor.shutdown(timeout=2.0)
        validate_sent(sent)


def test_the_edge_flies_three_closed_squares_and_lands(executors):
    lanes, on_complete = square_lanes()
    executor, drone, _sent, clock, _log = executors(geofence_cm=160, alt_slot_cm=100)
    send(executor, assign(lanes=lanes, on_complete=on_complete))
    t_ground = clock() + 0.5
    send(executor, start_at(t_ground))
    clock.set(t_ground)
    assert wait_for(lambda: ("land",) in drone.calls and not executor.is_busy, WAIT_S)
    moves = [(m[1], m[2]) for m in horizontal_moves(drone)]
    assert moves == [
        ("forward", 50), ("right", 50), ("back", 50), ("left", 50),
        ("forward", 100), ("right", 100), ("back", 100), ("left", 100),
        ("forward", 150), ("right", 150), ("back", 150), ("left", 150),
    ]
    assert dead_reckoned(drone) == (0, 0)
    assert drone.calls[-1] == ("land",) and drone.calls.count(("land",)) == 1  # lands once, after the last move
    assert not executor.plan_valid


def test_a_geofence_smaller_than_the_largest_square_refuses_the_start_and_nothing_flies(executors):
    lanes, on_complete = square_lanes()
    executor, drone, sent, clock, _log = executors(geofence_cm=100, alt_slot_cm=100)
    send(executor, assign(lanes=lanes, on_complete=on_complete, cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(1.0)
    # The edge checks the whole path against its fence when the plan is assigned, so the plan is refused
    # (geofence), the start has no valid plan, and the drone never takes off.
    assert wait_for(lambda: reply_for(sent, "s1") is not None, WAIT_S)
    plan_refusal, start_refusal = reply_for(sent, "a1"), reply_for(sent, "s1")
    assert plan_refusal["type"] == "nack" and plan_refusal["reason"] == "geofence"
    assert start_refusal["type"] == "nack" and start_refusal["reason"] == "no_valid_plan"
    assert ("takeoff",) not in drone.calls and horizontal_moves(drone) == []
