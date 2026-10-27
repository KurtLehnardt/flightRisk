"""Tests for the edge safety envelope, link watchdog and Tello safety hooks.

All flight tests use ``FakeTelloController`` or a mocked ``djitellopy.Tello``;
nothing here sends a packet to a real drone. Time is a fake clock wherever the
behaviour depends on it.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, call, patch

import pytest

from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import LinkWatchdog, SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController, TelloController
from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import _handle_control
from flightrisk.observability import StructuredLogger

WAIT_S = 10.0


class FakeClock:
    """Monotonic clock that only moves when the test advances it."""

    def __init__(self, start: float = 500.0):
        self._t = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._t

    def advance(self, dt: float) -> None:
        with self._lock:
            self._t += dt


def wait_for(predicate, timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def make_executor():
    created: list[LaneExecutor] = []

    def _make(battery=90, alt_slot_cm=100, takeoff_height_cm=80, clock=None):
        clock = clock if clock is not None else FakeClock()
        drone = FakeTelloController(name="drone-1", battery=battery, takeoff_height_cm=takeoff_height_cm)
        drone.connect()
        sent: list[dict] = []
        executor = LaneExecutor(
            drone,
            SafetyEnvelope(alt_slot_cm=alt_slot_cm, clock=clock),
            "drone-1",
            send=sent.append,
            clock=clock,
            logger=StructuredLogger(component="test-safety"),
        )
        created.append(executor)
        return executor, drone, sent, clock

    yield _make
    for executor in created:
        executor.shutdown(timeout=2.0)


def lands(drone: FakeTelloController) -> int:
    return drone.calls.count(("land",))


# ---------------------------------------------------------------------------
# R-093: geofence, battery gate, altitude slot
# ---------------------------------------------------------------------------


def test_geofence_refuses_exit(make_executor):
    envelope = SafetyEnvelope(geofence_cm=500)

    assert envelope.check_move("forward", 400)
    envelope.record_move("forward", 400)
    assert not envelope.check_move("forward", 200)  # endpoint y = 600
    assert envelope.check_move("forward", 100)  # endpoint y = 500, on the fence
    assert envelope.check_move("right", 500)
    assert not envelope.check_move("left", 501)  # longer than one Tello move
    assert not envelope.check_move("forward", 10)  # shorter than one Tello move
    assert not envelope.check_move("up", 50)  # lanes never change altitude
    assert not envelope.check_move("sideways", 50)

    # Moves follow the heading: after a quarter turn, forward is +x.
    envelope.pose.apply_rotation(90)
    assert envelope.check_move("forward", 500)
    envelope.record_move("forward", 300)
    assert not envelope.check_move("forward", 300)

    # In flight, a move the geofence refuses ends the flight with a landing.
    executor, drone, sent, clock = make_executor()
    executor.handle_clock({"type": "clock", "offset_s": 0.0})
    assert executor.handle_assign_lanes(
        {"type": "assign_lanes", "drone_id": "drone-1", "lanes": [{"direction": "forward", "distance_cm": 300}]}
    )
    executor.envelope.record_move("forward", 300)  # the pose drifted after the plan was accepted
    assert executor.handle_start_at({"type": "start_at", "t_ground_s": clock() + 0.5})
    clock.advance(0.5)
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == [("takeoff",), ("move", "up", 20), ("land",)]


def test_battery_gate(make_executor):
    envelope = SafetyEnvelope(min_takeoff_battery=30, land_battery=15)
    assert not envelope.can_takeoff(29)
    assert envelope.can_takeoff(30)
    assert not envelope.can_takeoff(None)  # unknown battery never permits a takeoff
    assert envelope.should_land(14)
    assert not envelope.should_land(15)
    assert not envelope.should_land(None)

    # Takeoff is refused below the threshold.
    executor, drone, _, _ = make_executor(battery=25)
    assert executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == []
    assert not drone.state.is_flying

    # Flying with a battery that drops below 15% lands once.
    executor, drone, _, _ = make_executor(battery=60)
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    executor.note_ground_message()
    executor.tick()
    assert lands(drone) == 0
    drone.state.battery = 14
    executor.tick()
    executor.tick()
    assert lands(drone) == 1


def test_altitude_slot_applied(make_executor):
    executor, drone, _, _ = make_executor(alt_slot_cm=160, takeoff_height_cm=80)
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == [("takeoff",), ("move", "up", 80)]
    assert drone.state.height == 160

    # Within one minimum move of the slot: no correction.
    executor, drone, _, _ = make_executor(alt_slot_cm=100, takeoff_height_cm=90)
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == [("takeoff",)]

    # Slot below the takeoff height: descend.
    executor, drone, _, _ = make_executor(alt_slot_cm=50, takeoff_height_cm=80)
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == [("takeoff",), ("move", "down", 30)]

    # Unknown height after takeoff: the slot cannot be reached safely, so land.
    executor, drone, _, _ = make_executor(alt_slot_cm=100, takeoff_height_cm=0)
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == [("takeoff",), ("land",)]

    envelope = SafetyEnvelope(alt_slot_cm=100)
    assert envelope.slot_move(80) == ("up", 20)
    with pytest.raises(ValueError):
        SafetyEnvelope(alt_slot_cm=5)
    with pytest.raises(ValueError):
        envelope.set_alt_slot(900)


def test_envelope_reads_env_overrides():
    env = {
        "FLIGHTRISK_GEOFENCE_CM": "300",
        "FLIGHTRISK_MIN_TAKEOFF_BATTERY": "45",
        "FLIGHTRISK_LINK_LOSS_LAND_S": "1.5",
    }
    envelope = SafetyEnvelope.from_env(alt_slot_cm=160, environ=env)
    assert envelope.geofence_cm == 300
    assert envelope.min_takeoff_battery == 45
    assert envelope.link_watchdog.timeout_s == 1.5
    assert envelope.alt_slot_cm == 160

    defaults = SafetyEnvelope.from_env(environ={})
    assert (defaults.geofence_cm, defaults.min_takeoff_battery, defaults.land_battery) == (500, 30, 15)
    assert defaults.link_watchdog.timeout_s == 3.0
    assert defaults.alt_slot_cm == 100

    with pytest.raises(ValueError):
        SafetyEnvelope.from_env(environ={"FLIGHTRISK_GEOFENCE_CM": "lots"})


# ---------------------------------------------------------------------------
# R-094: link-loss watchdog and keepalive
# ---------------------------------------------------------------------------


def test_link_loss_lands_fake_drone(make_executor):
    executor, drone, _, clock = make_executor()
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    executor.note_ground_message()

    clock.advance(2.9)
    executor.tick()
    assert lands(drone) == 0

    clock.advance(0.2)  # 3.1 s without a ground message
    executor.tick()
    assert lands(drone) == 1
    assert not drone.state.is_flying

    clock.advance(5.0)
    executor.tick()
    assert lands(drone) == 1


def test_heartbeat_resets_watchdog(make_executor):
    executor, drone, _, clock = make_executor()
    runner = EdgeRunner(detector=None)
    log = StructuredLogger(component="test-safety")
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    executor.note_ground_message()

    clock.advance(2.5)
    _handle_control({"type": "heartbeat", "seq": 7}, runner, log, flight=executor)
    clock.advance(2.5)  # 5 s since takeoff, 2.5 s since the heartbeat
    executor.tick()
    assert lands(drone) == 0
    assert executor.telemetry()["link_age_s"] == pytest.approx(2.5)

    clock.advance(0.6)
    executor.tick()
    assert lands(drone) == 1


def test_link_loss_ignored_on_ground(make_executor):
    executor, drone, _, clock = make_executor()
    clock.advance(60.0)
    executor.tick()
    assert drone.calls == []


def test_failed_land_is_retried_while_flying(make_executor):
    executor, drone, _, clock = make_executor()
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    executor.note_ground_message()

    real_land = drone.land
    attempts = []

    def flaky_land():
        attempts.append(clock())
        if len(attempts) == 1:
            raise RuntimeError("no reply from the drone")
        real_land()

    drone.land = flaky_land
    executor.land_now("ground_land")
    assert drone.state.is_flying
    executor.tick()
    assert len(attempts) == 1  # retry waits for the retry interval
    clock.advance(2.0)
    executor.tick()
    assert len(attempts) == 2
    assert not drone.state.is_flying


def test_link_watchdog_feed_and_age():
    clock = FakeClock()
    watchdog = LinkWatchdog(3.0, clock=clock)
    clock.advance(2.0)
    assert watchdog.age_s() == pytest.approx(2.0) and not watchdog.expired()
    watchdog.feed()
    clock.advance(3.0)
    assert watchdog.expired()
    with pytest.raises(ValueError):
        LinkWatchdog(0, clock=clock)


def _mock_tello():
    tello = MagicMock()
    tello.get_battery.return_value = 80
    tello.get_height.return_value = 0
    tello.get_temperature.return_value = 40
    tello.get_flight_time.return_value = 0
    return tello


def test_tello_keepalive_thread_starts_on_connect():
    tello = _mock_tello()
    with patch("flightrisk.drone.tello.Tello", return_value=tello):
        ctrl = TelloController(name="drone-1", host="192.0.2.1")
    try:
        assert ctrl.connect()
        assert ctrl._keepalive_thread is not None and ctrl._keepalive_thread.is_alive()
        assert wait_for(lambda: call("command", timeout=5) in tello.send_control_command.call_args_list)
    finally:
        ctrl._running = False


def test_tello_land_interrupts_inflight_move():
    tello = _mock_tello()
    entered = threading.Event()
    release = threading.Event()

    def slow_move(distance):
        entered.set()
        release.wait(WAIT_S)

    tello.move_forward.side_effect = slow_move
    with patch("flightrisk.drone.tello.Tello", return_value=tello):
        ctrl = TelloController(name="drone-1", host="192.0.2.1")
    ctrl.state.is_flying = True

    mover = threading.Thread(target=ctrl.move, args=("forward", 200), daemon=True)
    mover.start()
    assert entered.wait(WAIT_S)

    lander = threading.Thread(target=ctrl.land, daemon=True)
    lander.start()
    # stop and land go out at once, without waiting for the move's reply.
    assert wait_for(
        lambda: [c.args[0] for c in tello.send_command_without_return.call_args_list] == ["stop", "land"]
    )
    assert tello.land.call_count == 0
    # Nothing new is sent behind the landing.
    assert ctrl.move("back", 100) is False
    assert ctrl.rotate(90) is False

    release.set()
    mover.join(WAIT_S)
    lander.join(WAIT_S)
    assert not lander.is_alive()
    assert tello.land.call_count == 1  # confirming land once the channel is free
    assert ctrl.state.is_flying is False

    # A new takeoff re-arms moves.
    tello.move_forward.side_effect = None
    ctrl.takeoff()
    assert ctrl.move("forward", 50) is True
