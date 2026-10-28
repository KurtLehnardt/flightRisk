"""Tests for the edge lane executor, its control handling and the edge flight CLI.

Everything runs against ``FakeTelloController`` (no network, no hardware) and
an injected fake clock, so scheduling is deterministic. Flight sequences still
run on the executor's worker thread, so tests wait for them with generous
timeouts; the machine may be heavily loaded.
"""
from __future__ import annotations

import io
import threading
import time
from unittest.mock import patch

import numpy as np
import pytest

from flightrisk.drone.lane_executor import LaneExecutor, parse_lanes, split_lane_moves
from flightrisk.drone.safety import RelativePose, SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController
from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import (
    DroneFrameSource,
    _build_drone,
    _build_parser,
    _handle_control,
    _make_sender,
    run_edge_device,
    start_local_land_key,
)
from flightrisk.observability import StructuredLogger

WAIT_S = 10.0

TELEMETRY_KEYS = {
    "type",
    "drone_id",
    "battery",
    "height_cm",
    "is_flying",
    "rel_x_cm",
    "rel_y_cm",
    "heading_deg",
    "lane_index",
    "lanes_total",
    "link_age_s",
    "takeoff_at_local_s",
    # Optional fields: the active ceiling, margin and altitude slot.
    "ceiling_cm",
    "ceiling_margin_cm",
    "alt_slot_cm",
}

LANES = [
    {"direction": "forward", "distance_cm": 200},
    {"direction": "right", "distance_cm": 100},
    {"direction": "back", "distance_cm": 200},
    {"direction": "right", "distance_cm": 100},
    {"direction": "forward", "distance_cm": 200},
]


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """Monotonic clock that only moves when the test advances it."""

    def __init__(self, start: float = 1000.0):
        self._t = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._t

    def advance(self, dt: float) -> None:
        with self._lock:
            self._t += dt

    def set(self, t: float) -> None:
        with self._lock:
            self._t = t


class GatedFakeTello(FakeTelloController):
    """Fake Tello that holds one chosen move in flight until the test releases it."""

    def __init__(self, gate_on: tuple[str, int], **kwargs):
        super().__init__(**kwargs)
        self.gate_on = gate_on
        self.entered = threading.Event()
        self.release = threading.Event()

    def move(self, direction: str, distance_cm: int) -> bool:
        result = super().move(direction, distance_cm)
        if (direction, distance_cm) == self.gate_on and not self.entered.is_set():
            self.entered.set()
            self.release.wait(WAIT_S)
        return result


class RaisingFakeTello(FakeTelloController):
    """Fake Tello whose horizontal moves fail like a lost UDP reply."""

    def move(self, direction: str, distance_cm: int) -> bool:
        if direction in ("forward", "back", "left", "right"):
            raise RuntimeError("simulated Tello command failure")
        return super().move(direction, distance_cm)


def wait_for(predicate, timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def of_type(sent: list[dict], mtype: str, ack_for: str | None = None) -> list[dict]:
    return [
        m for m in sent
        if m.get("type") == mtype and (ack_for is None or m.get("for") == ack_for)
    ]


def horizontal_moves(drone: FakeTelloController) -> list[tuple]:
    return [c for c in drone.calls if c[0] == "move" and c[1] in ("forward", "back", "left", "right")]


@pytest.fixture
def make_executor():
    """Factory for executors on a fake drone; shuts every executor down afterwards."""
    created: list[LaneExecutor] = []

    def _make(clock=None, drone=None, alt_slot_cm=100, drone_id="drone-1", **kwargs):
        clock = clock if clock is not None else FakeClock()
        drone = drone if drone is not None else FakeTelloController(name=drone_id, battery=90)
        drone.connect()
        envelope = SafetyEnvelope(alt_slot_cm=alt_slot_cm, clock=clock)
        sent: list[dict] = []
        executor = LaneExecutor(
            drone,
            envelope,
            drone_id,
            send=sent.append,
            clock=clock,
            logger=StructuredLogger(component="test-lane-executor"),
            **kwargs,
        )
        created.append(executor)
        return executor, drone, sent, clock

    yield _make
    for executor in created:
        executor.shutdown(timeout=2.0)


def start_lanes(executor: LaneExecutor, clock: FakeClock, lanes=LANES, offset_s: float = 0.0, lead_s: float = 1.0):
    """Assign lanes, sync the clock and schedule a start ``lead_s`` ahead; return t_edge."""
    executor.handle_clock({"type": "clock", "offset_s": offset_s})
    assert executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": executor.drone_id, "lanes": lanes})
    t_ground = clock() - offset_s + lead_s
    assert executor.handle_start_at({"type": "start_at", "t_ground_s": t_ground})
    return t_ground + offset_s


# ---------------------------------------------------------------------------
# R-092: lane execution, dead reckoning, telemetry, start_at ack
# ---------------------------------------------------------------------------


def test_fake_drone_flies_assigned_lanes_in_order(make_executor):
    executor, drone, sent, clock = make_executor()
    t_edge = start_lanes(executor, clock)

    clock.set(t_edge)
    assert executor.wait_idle(WAIT_S)

    assert drone.calls == [
        ("takeoff",),
        ("move", "up", 20),  # 80 cm takeoff height -> 100 cm slot
        ("move", "forward", 200),
        ("move", "right", 100),
        ("move", "back", 200),
        ("move", "right", 100),
        ("move", "forward", 200),
    ]
    assert executor.lane_index == executor.lanes_total == 5
    acks = of_type(sent, "ack", "assign_lanes")
    assert acks == [{"type": "ack", "for": "assign_lanes", "drone_id": "drone-1", "battery": 90, "lanes_total": 5}]
    assert drone.state.is_flying  # hovers after the lanes until the ground lands it


def test_long_lane_split_into_tello_moves(make_executor):
    assert split_lane_moves(600) == [300, 300]
    assert split_lane_moves(1001) == [334, 334, 333]
    assert split_lane_moves(500) == [500]
    with pytest.raises(ValueError):
        split_lane_moves(10)

    executor, drone, _, clock = make_executor()
    t_edge = start_lanes(executor, clock, lanes=[
        {"direction": "back", "distance_cm": 250},
        {"direction": "forward", "distance_cm": 600},
    ])
    clock.set(t_edge)
    assert executor.wait_idle(WAIT_S)
    assert horizontal_moves(drone) == [
        ("move", "back", 250),
        ("move", "forward", 300),
        ("move", "forward", 300),
    ]
    assert executor.envelope.pose_snapshot().y_cm == pytest.approx(350)


def test_dead_reckoning_tracks_moves(make_executor):
    executor, _, _, clock = make_executor()
    t_edge = start_lanes(executor, clock)
    clock.set(t_edge)
    assert executor.wait_idle(WAIT_S)

    # forward 200, right 100, back 200, right 100, forward 200 from the origin.
    pose = executor.envelope.pose_snapshot()
    assert (pose.x_cm, pose.y_cm, pose.heading_deg) == (pytest.approx(200), pytest.approx(200), 0.0)
    telemetry = executor.telemetry()
    assert (telemetry["rel_x_cm"], telemetry["rel_y_cm"]) == (200.0, 200.0)

    # Moves are taken relative to the heading: after a clockwise quarter turn,
    # forward is +x and right is -y.
    turned = RelativePose()
    turned.apply_rotation(90)
    turned.apply_move("forward", 100)
    turned.apply_move("right", 50)
    assert (turned.x_cm, turned.y_cm) == (pytest.approx(100), pytest.approx(-50))
    turned.apply_rotation(-450)
    assert turned.heading_deg == pytest.approx(0.0)


def test_telemetry_fields_present(make_executor):
    executor, drone, sent, clock = make_executor()

    executor.tick()
    telemetry = of_type(sent, "telemetry")
    assert len(telemetry) == 1
    message = telemetry[0]
    assert set(message) == TELEMETRY_KEYS
    assert message["drone_id"] == "drone-1"
    assert message["battery"] == 90
    assert message["is_flying"] is False
    assert message["takeoff_at_local_s"] is None
    assert message["lane_index"] == 0 and message["lanes_total"] == 0

    # Every 0.5 s, not every tick.
    clock.advance(0.2)
    executor.tick()
    assert len(of_type(sent, "telemetry")) == 1
    clock.advance(0.3)
    executor.tick()
    assert len(of_type(sent, "telemetry")) == 2

    # After a scheduled start the takeoff time and lane progress are reported.
    t_edge = start_lanes(executor, clock)
    clock.set(t_edge)
    assert executor.wait_idle(WAIT_S)
    clock.advance(0.5)
    executor.note_ground_message()
    executor.tick()
    latest = of_type(sent, "telemetry")[-1]
    assert latest["is_flying"] is True
    assert latest["height_cm"] == 100
    assert latest["takeoff_at_local_s"] == pytest.approx(t_edge)
    assert latest["lane_index"] == latest["lanes_total"] == 5
    assert latest["link_age_s"] >= 0


@pytest.mark.parametrize("offset_s", [4000.0, -4000.0, 0.0])
def test_start_at_ack_carries_ground_time(make_executor, offset_s):
    clock = FakeClock(start=10_000.0)
    executor, drone, sent, _ = make_executor(clock=clock)
    executor.handle_clock({"type": "clock", "offset_s": offset_s})
    executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": LANES[:1]})

    # offset = edge - ground, so the ground clock currently reads edge - offset.
    ground_now = clock() - offset_s
    t_ground = ground_now + 2.0
    assert executor.handle_start_at({"type": "start_at", "t_ground_s": t_ground})

    # The edge fires at t_ground + offset on its own clock, not before.
    clock.set(t_ground + offset_s - 0.1)
    time.sleep(0.2)
    assert ("takeoff",) not in drone.calls
    assert of_type(sent, "ack", "start_at") == []

    clock.set(t_ground + offset_s)
    assert wait_for(lambda: of_type(sent, "ack", "start_at"))
    ack = of_type(sent, "ack", "start_at")[0]
    assert set(ack) == {"type", "for", "drone_id", "executed_at_ground_s"}
    assert ack["drone_id"] == "drone-1"
    assert ack["executed_at_ground_s"] == pytest.approx(t_ground, abs=1e-6)
    assert drone.calls[0] == ("takeoff",)
    assert executor.wait_idle(WAIT_S)


def test_start_at_without_clock_offset_stays_landed(make_executor):
    executor, drone, sent, clock = make_executor()
    executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": LANES})

    assert executor.handle_start_at({"type": "start_at", "t_ground_s": clock() + 1.0}) is False
    clock.advance(5.0)
    time.sleep(0.1)
    assert drone.calls == []
    assert not drone.state.is_flying
    assert of_type(sent, "ack", "start_at") == []


def test_start_at_refused_when_clock_is_implausible(make_executor):
    executor, drone, _, clock = make_executor(clock=FakeClock(start=10_000.0))
    executor.handle_clock({"type": "clock", "offset_s": 4000.0})
    executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": LANES})
    ground_now = clock() - 4000.0

    # Converting with the wrong sign would put the start 8000 s in the past.
    assert executor.handle_start_at({"type": "start_at", "t_ground_s": ground_now - 8000.0}) is False
    # Far in the future is a clock error too.
    assert executor.handle_start_at({"type": "start_at", "t_ground_s": ground_now + 3600.0}) is False
    # Without lanes there is nothing to fly.
    executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": []})
    assert executor.handle_start_at({"type": "start_at", "t_ground_s": ground_now + 1.0}) is False
    assert drone.calls == []


# ---------------------------------------------------------------------------
# assign_lanes: acks, refusals and re-tasking
# ---------------------------------------------------------------------------


def test_assign_lanes_ack_and_refusals(make_executor):
    executor, _, sent, _ = make_executor()

    # Addressed to another drone, an altitude-changing lane, a bad distance,
    # an out-of-range altitude slot and a path leaving the geofence: no ack.
    refused = [
        {"type": "assign_lanes", "drone_id": "drone-2", "lanes": LANES},
        {"type": "assign_lanes", "drone_id": "drone-1", "lanes": [{"direction": "up", "distance_cm": 100}]},
        {"type": "assign_lanes", "drone_id": "drone-1", "lanes": [{"direction": "forward", "distance_cm": 5}]},
        {"type": "assign_lanes", "drone_id": "drone-1", "lanes": LANES, "alt_cm": 2000},
        {"type": "assign_lanes", "drone_id": "drone-1", "lanes": [{"direction": "left", "distance_cm": 300}] * 2},
        {"type": "assign_lanes", "drone_id": "drone-1", "lanes": "forward"},
    ]
    for msg in refused:
        assert executor.handle_assign_lanes(msg) is False, msg
    assert of_type(sent, "ack") == []

    accepted = {
        "type": "assign_lanes",
        "drone_id": "drone-1",
        "alt_cm": 130,
        "lanes": LANES[:3],
        "zone": {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400},
    }
    assert executor.handle_assign_lanes(accepted) is True
    acks = of_type(sent, "ack", "assign_lanes")
    assert len(acks) == 1 and acks[0]["lanes_total"] == 3 and acks[0]["battery"] == 90
    # The coordinator's slot replaces the local one.
    assert executor.envelope.alt_slot_cm == 130
    assert parse_lanes(LANES[:2]) == [("forward", 200), ("right", 100)]


def test_retask_mid_flight_continues_from_current_lane(make_executor):
    drone = GatedFakeTello(gate_on=("right", 100), name="drone-1", battery=90)
    executor, drone, sent, clock = make_executor(drone=drone)
    old_lanes = LANES[:3]
    t_edge = start_lanes(executor, clock, lanes=old_lanes)
    clock.set(t_edge)

    # Hold the drone in the middle of lane 1.
    assert drone.entered.wait(WAIT_S)
    assert executor.lane_index == 1

    # Coordinator re-task: old lanes unchanged, a transit leg, then the lost
    # drone's remaining lanes.
    new_lanes = old_lanes + [
        {"direction": "right", "distance_cm": 150},
        {"direction": "forward", "distance_cm": 100},
    ]
    assert executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": new_lanes})
    assert executor.lane_index == 1  # progress kept, not reset to lane 0
    assert of_type(sent, "ack", "assign_lanes")[-1]["lanes_total"] == 5

    drone.release.set()
    assert wait_for(lambda: executor.lane_index == 5)
    assert executor.wait_idle(WAIT_S)
    assert horizontal_moves(drone) == [
        ("move", "forward", 200),
        ("move", "right", 100),
        ("move", "back", 200),
        ("move", "right", 150),
        ("move", "forward", 100),
    ]
    pose = executor.envelope.pose_snapshot()
    assert (pose.x_cm, pose.y_cm) == (pytest.approx(250), pytest.approx(100))


def test_retask_after_lanes_complete_flies_only_appended_lanes(make_executor):
    executor, drone, _, clock = make_executor()
    t_edge = start_lanes(executor, clock, lanes=LANES[:2])
    clock.set(t_edge)
    assert executor.wait_idle(WAIT_S)
    assert executor.lane_index == 2 and drone.state.is_flying

    new_lanes = LANES[:2] + [{"direction": "back", "distance_cm": 100}]
    assert executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": new_lanes})
    assert wait_for(lambda: executor.lane_index == 3)
    assert executor.wait_idle(WAIT_S)
    assert horizontal_moves(drone) == [
        ("move", "forward", 200),
        ("move", "right", 100),
        ("move", "back", 100),
    ]


def test_assign_lanes_while_merely_hovering_waits_for_start(make_executor):
    executor, drone, _, _ = make_executor()
    assert executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == [("takeoff",), ("move", "up", 20)]

    executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": LANES})
    time.sleep(0.2)
    assert horizontal_moves(drone) == []


# ---------------------------------------------------------------------------
# Landing paths and fail-safe behaviour
# ---------------------------------------------------------------------------


def test_local_land_key_lands(make_executor):
    executor, drone, _, _ = make_executor()
    drone.takeoff()

    thread = start_local_land_key(executor, io.StringIO("x\nhello\nl\n"))
    thread.join(WAIT_S)

    assert not thread.is_alive()
    assert drone.calls.count(("land",)) == 1
    assert not drone.state.is_flying


def test_emergency_land_all_bypasses_running_lanes(make_executor):
    drone = GatedFakeTello(gate_on=("right", 100), name="drone-1", battery=90)
    executor, drone, _, clock = make_executor(drone=drone)
    t_edge = start_lanes(executor, clock)
    clock.set(t_edge)
    assert drone.entered.wait(WAIT_S)

    # The move is still in flight; the land must not wait for it.
    runner = EdgeRunner(detector=None)
    log = StructuredLogger(component="test-lane-executor")
    _handle_control({"type": "emergency_land_all"}, runner, log, flight=executor)
    assert ("land",) in drone.calls
    assert not drone.state.is_flying

    drone.release.set()
    assert executor.wait_idle(WAIT_S)
    # Nothing was sent after the landing.
    assert drone.calls[-2:] == [("move", "right", 100), ("land",)]


def test_land_cancels_scheduled_start(make_executor):
    executor, drone, sent, clock = make_executor()
    t_edge = start_lanes(executor, clock, lead_s=5.0)
    executor.handle_message({"type": "land"})
    assert executor.wait_idle(WAIT_S)
    clock.set(t_edge + 1.0)
    time.sleep(0.1)
    assert ("takeoff",) not in drone.calls
    assert of_type(sent, "ack", "start_at") == []


def test_executor_exception_lands(make_executor):
    drone = RaisingFakeTello(name="drone-1", battery=90)
    executor, drone, _, clock = make_executor(drone=drone)
    t_edge = start_lanes(executor, clock)
    clock.set(t_edge)
    assert executor.wait_idle(WAIT_S)
    assert drone.calls[-1] == ("land",)
    assert not drone.state.is_flying


def test_control_exception_lands(make_executor):
    executor, drone, _, _ = make_executor()
    drone.takeoff()
    runner = EdgeRunner(detector=None)
    log = StructuredLogger(component="test-lane-executor")

    with patch.object(executor, "handle_ping", side_effect=RuntimeError("boom")):
        _handle_control({"type": "ping", "t1": 1.0}, runner, log, flight=executor)

    assert drone.calls[-1] == ("land",)


def test_ping_answered_with_pong_timestamps(make_executor):
    executor, _, sent, clock = make_executor()
    runner = EdgeRunner(detector=None)
    log = StructuredLogger(component="test-lane-executor")

    _handle_control({"type": "ping", "t1": 12.5}, runner, log, flight=executor, recv_t=clock() - 0.001)
    pongs = of_type(sent, "pong")
    assert len(pongs) == 1
    assert set(pongs[0]) == {"type", "t1", "t2", "t3"}
    assert pongs[0]["t1"] == 12.5
    assert pongs[0]["t2"] == pytest.approx(clock() - 0.001)
    assert pongs[0]["t3"] >= pongs[0]["t2"]


# ---------------------------------------------------------------------------
# run_edge_device in flight mode and the CLI
# ---------------------------------------------------------------------------


class FlightTransport:
    """Fake transport: replays control messages and records JSON sends."""

    def __init__(self, control_queue):
        self._control = list(control_queue)
        self._lock = threading.Lock()
        self.detections = []
        self.json_sent = []
        self.connected = True
        self.disconnected = False

    def send_detections(self, msg, timeout=30):
        self.detections.append(msg)

    def send_json(self, msg):
        self.json_sent.append(msg)

    def recv(self, recv_timeout=None, timeout=30):
        with self._lock:
            if self._control:
                return self._control.pop(0)
        return None

    def disconnect(self, timeout=30):
        self.disconnected = True
        self.connected = False


def test_run_edge_device_flight_mode_handles_control_and_lands_on_exit():
    drone = FakeTelloController(name="drone-1", battery=90)
    drone.connect()
    clock = FakeClock()
    executor = LaneExecutor(
        drone,
        SafetyEnvelope(alt_slot_cm=100, clock=clock),
        "drone-1",
        send=lambda msg: transport.json_sent.append(msg),
        clock=clock,
        logger=StructuredLogger(component="test-lane-executor"),
    )
    transport = FlightTransport([
        {"type": "heartbeat", "seq": 1},
        {"type": "ping", "t1": 5.0},
        {"type": "takeoff"},
    ])
    stop = threading.Event()

    class FramesUntilFlying:
        """Yields frames until the drone has climbed to its slot, then stops."""

        def __init__(self):
            self.count = 0

        def __iter__(self):
            return self

        def __next__(self):
            if self.count and (
                wait_for(lambda: ("move", "up", 20) in drone.calls, timeout=0.05) or stop.is_set()
            ):
                raise StopIteration
            self.count += 1
            return np.zeros((48, 64, 3), np.uint8)

    timer = threading.Timer(WAIT_S, stop.set)
    timer.start()
    try:
        run_edge_device(FramesUntilFlying(), transport, EdgeRunner(detector=None), flight=executor)
    finally:
        timer.cancel()

    assert drone.calls == [("takeoff",), ("move", "up", 20), ("land",)]
    assert of_type(transport.json_sent, "pong")
    assert transport.disconnected is True
    assert transport.detections


def test_drone_frame_source_skips_missing_frames_and_stops():
    class Flaky:
        def __init__(self):
            self.n = 0

        def get_frame(self):
            self.n += 1
            return None if self.n % 2 else np.zeros((4, 4, 3), np.uint8)

    stop = threading.Event()
    source = DroneFrameSource(Flaky(), stop, min_interval_s=0.0)
    assert next(source).shape == (4, 4, 3)
    stop.set()
    with pytest.raises(StopIteration):
        next(source)


def test_cli_builds_tello_and_fake_sources():
    parser = _build_parser()
    log = StructuredLogger(component="test-lane-executor")

    args = parser.parse_args([
        "--source", "tello", "--tello-host", "10.0.0.5", "--drone-id", "drone-2", "--alt-slot", "1.6",
    ])
    assert args.alt_slot == 160.0
    with patch("flightrisk.drone.tello.TelloController") as tello_cls:
        _build_drone(args, log)
    tello_cls.assert_called_once_with(name="drone-2", host="10.0.0.5")

    args = parser.parse_args(["--source", "fake", "--alt-slot", "100"])
    assert args.alt_slot == 100.0 and args.drone_id == "drone-1"
    assert isinstance(_build_drone(args, log), FakeTelloController)

    with pytest.raises(SystemExit):
        parser.parse_args(["--source", "fake", "--alt-slot", "9000"])


def test_make_sender_uses_transport_json_method():
    log = StructuredLogger(component="test-lane-executor")
    transport = FlightTransport([])
    send = _make_sender(transport, log)
    send({"type": "pong"})
    assert transport.json_sent == [{"type": "pong"}]

    # A transport with no way to send drops messages without raising.
    _make_sender(object(), log)({"type": "telemetry"})


def test_no_takeoff_after_shutdown(make_executor):
    executor, drone, _, clock = make_executor()
    executor.handle_clock({"type": "clock", "offset_s": 0.0})
    executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": LANES})
    executor.shutdown(timeout=2.0)

    assert executor.handle_takeoff() is False
    assert executor.handle_start_at({"type": "start_at", "t_ground_s": clock() + 0.5}) is False
    clock.advance(1.0)
    time.sleep(0.1)
    assert drone.calls == []


def test_emergency_callback_lands_once_with_recv_duplicate(make_executor):
    from flightrisk.edge_device import _wire_emergency_callback

    class CallbackTransport(FlightTransport):
        on_emergency_land = None

    executor, drone, _, _ = make_executor()
    drone.takeoff()

    # Hold the first land in progress, as a real Tello land blocks for seconds.
    entered, release = threading.Event(), threading.Event()
    real_land = drone.land

    def slow_land():
        entered.set()
        release.wait(WAIT_S)
        real_land()

    drone.land = slow_land
    transport = CallbackTransport([])
    assert _wire_emergency_callback(transport, executor)
    assert _wire_emergency_callback(FlightTransport([]), executor) is False

    callback_thread = threading.Thread(target=transport.on_emergency_land, args=({"type": "emergency_land_all"},))
    callback_thread.start()
    assert entered.wait(WAIT_S)
    # The same message then arrives through recv() on the control thread.
    _handle_control(
        {"type": "emergency_land_all"}, EdgeRunner(detector=None),
        StructuredLogger(component="test-lane-executor"), flight=executor,
    )
    release.set()
    callback_thread.join(WAIT_S)
    assert drone.calls.count(("land",)) == 1
    assert not drone.state.is_flying
