"""Stepped obstacle sensing on the edge lane executor (``lane_flight``, ``lane_plan``, ``lane_steps``).

Every executor is the real ``LaneExecutor`` on a fake Tello with a fake
clock and a scripted ``FakeObstacleSensor`` whose answers depend on the
dead-reckoned pose; nothing touches a network, a camera or a drone. The
drone and the sensor write one shared timeline, so every scenario asserts
the two invariants of design section 4:

* no ``move`` is ever sent after a gate that did not return ``clear`` (every
  lane step is sent right after a clear gate, nothing in between);
* the dead-reckoned pose equals the sum of the moves and turns the drone
  executed.

Covers the turn before every lane, the gate before every step, the cut
policy and the deficit rule (the worked example of design section 3.3),
stale, timeout, error and sensor-down handling, holds and resume, re-task,
nudge and manual takeover around cuts, a landing during a gate, the
forward-only and off modes, the telemetry fields, the autopilot's
``STOPPED_SHORT`` and the start refusal when the sensor is missing. Without a
sensor the call sequence is exactly the one the executor always sent.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone.controller import Capability, CommandOutcome, CommandResult
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.lane_steps import Cut, uncovered_segments
from flightrisk.drone.obstacle_sensor import (
    CUT_AUTOPILOT,
    CUT_OBSTACLE,
    CUT_SENSOR_DOWN,
    CUT_SENSOR_STALE,
    EVENT_LANE_CUT,
    EVENT_LANES_ENDED_SENSOR_DOWN,
    EVENT_OBSTACLE_GATE,
    EVENT_OBSTACLE_STOP,
    EVENT_OBSTACLE_TURN,
    EVENT_SENSE_STALE,
    EVENT_SENSE_TIMEOUT,
    EVENT_SENSOR_ERROR,
    EVENT_START_REFUSED,
    OBSTACLE_SENSE_RETRIES,
    REASON_STALE,
    SEGMENT_SKIPPED,
    START_REFUSED_REASON,
    TELEMETRY_CUT_REASONS,
    TELEMETRY_CUTS,
    TELEMETRY_CUTS_TOTAL,
    TELEMETRY_FIELDS,
    TELEMETRY_LATENCY_MS,
    TELEMETRY_SENSING,
    TELEMETRY_SENSOR_OK,
    TELEMETRY_SEQ,
    TELEMETRY_STATE,
    BlockedZone,
    FakeObstacleSensor,
    NullSensor,
    SenseResult,
)
from flightrisk.drone.safety import HORIZONTAL_DIRECTIONS, RelativePose, SafetyEnvelope
from tests.lane_executor_support import (
    FENCE,
    LANES,
    FakeClock,
    QuietLogger,
    SteppedTello,
    assign,
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

#: The demo plan of design section 3.3.
DEMO = [
    {"direction": "forward", "distance_cm": 400},
    {"direction": "right", "distance_cm": 100},
    {"direction": "back", "distance_cm": 400},
    {"direction": "right", "distance_cm": 100},
    {"direction": "forward", "distance_cm": 400},
]
#: Blocked when facing forward on the first lane (x <= 50) at y >= 250.
WALL_AT_250 = BlockedZone(heading_deg=0.0, y_min=250, x_max=50)


def lanes_of(*pairs: tuple[str, int]) -> list[dict[str, Any]]:
    return [{"direction": d, "distance_cm": cm} for d, cm in pairs]


# ---------------------------------------------------------------------------
# Fakes: one timeline for the drone and the sensor
# ---------------------------------------------------------------------------


class GatedTello(SteppedTello):
    """A stepped fake Tello that writes every move, turn and landing to a shared timeline."""

    def __init__(self, timeline: list, clock: FakeClock | None = None, motion_s: float = 0.0, **kwargs):
        super().__init__(**kwargs)
        self.timeline = timeline
        self.clock = clock
        self.motion_s = motion_s
        self.motion_ends: list[float] = []

    def _moved(self) -> None:
        if self.clock is not None and self.motion_s:
            self.clock.advance(self.motion_s)
            self.motion_ends.append(self.clock())

    def move(self, direction, distance_cm):
        if self.state.is_flying:
            self.timeline.append(("move", direction, distance_cm))
        result = super().move(direction, distance_cm)
        self._moved()
        return result

    def rotate(self, degrees):
        self.timeline.append(("rotate", degrees))
        result = super().rotate(degrees)
        self._moved()
        return result

    def land(self):
        self.timeline.append(("land",))
        return super().land()


class TimelineSensor(FakeObstacleSensor):
    """``FakeObstacleSensor`` that writes each result's state to the shared timeline."""

    def __init__(self, timeline: list, pose_provider, blocked=(), **kwargs):
        super().__init__(pose_provider, blocked, **kwargs)
        self.timeline = timeline
        #: Called at the start of every sense call with the call number (from 1).
        self.on_call = None

    def sense(self, after, timeout_s=2.0):
        hook = self.on_call
        if hook is not None:
            hook(len(self.calls) + 1)
        try:
            result = super().sense(after, timeout_s)
        except Exception:
            self.timeline.append(("sense", "error"))
            raise
        self.timeline.append(("sense", result.state))
        return result

    def states(self) -> list[str]:
        return [call.outcome for call in self.calls]


@dataclass
class Rig:
    executor: LaneExecutor
    drone: GatedTello
    sensor: TimelineSensor | None
    sent: list
    clock: FakeClock
    log: QuietLogger
    timeline: list


@pytest.fixture
def rigs():
    created: list[Rig] = []

    def _make(blocked=(), *, auto: bool = True, sensor: Any = "fake", motion_s: float = 0.0,
              geofence_cm: float = 500, drone: Any = None, **kwargs) -> Rig:
        """An executor on a ``GatedTello``; *drone* and *sensor* may be factories.

        *drone* is called with ``(timeline, clock)``, *sensor* (unless it is
        a sensor, None or ``"fake"``) with ``(timeline, envelope, clock)``.
        """
        clock = FakeClock()
        timeline: list = []
        if drone is None:
            drone = GatedTello(timeline, clock=clock, motion_s=motion_s, auto=auto, name="drone-1", battery=90)
        else:
            drone = drone(timeline, clock)
        drone.connect()
        envelope = SafetyEnvelope(alt_slot_cm=100, geofence_cm=geofence_cm, clock=clock)
        if sensor == "fake":
            sensor = TimelineSensor(timeline, envelope.pose_snapshot, blocked)
        elif callable(sensor) and not hasattr(sensor, "sense"):
            sensor = sensor(timeline, envelope, clock)
        kwargs.setdefault("obstacle_retry_pause_s", 0.0)
        sent: list = []
        log = QuietLogger()
        executor = LaneExecutor(drone, envelope, "drone-1", send=sent.append, clock=clock, logger=log,
                                fleet_v2=True, obstacle_sensor=sensor, **kwargs)
        executor.handle_clock({"type": "clock", "offset_s": 0.0})
        rig = Rig(executor, drone, sensor if isinstance(sensor, TimelineSensor) else None, sent, clock, log,
                  timeline)
        created.append(rig)
        return rig

    yield _make
    for rig in created:
        if isinstance(rig.drone, SteppedTello):
            rig.drone.auto = True
            rig.drone.release(100)
        rig.executor.shutdown(timeout=2.0)
        validate_sent(rig.sent)


# ---------------------------------------------------------------------------
# Helpers and the two invariants
# ---------------------------------------------------------------------------


def start(rig: Rig, lanes=LANES, *, plan_id: str = "p-1", plan_seq: int = 1, **assign_kwargs) -> None:
    """Assign *lanes* and fire the start (the drone takes off and flies)."""
    send(rig.executor, assign(lanes=lanes, plan_id=plan_id, plan_seq=plan_seq, cmd_id=f"a-{plan_id}",
                              **assign_kwargs))
    ack = reply_for(rig.sent, f"a-{plan_id}")
    assert ack is not None and ack["type"] == "ack", ack
    t_ground = rig.clock() + 0.5
    send(rig.executor, start_at(t_ground, plan_id=plan_id, cmd_id=f"s-{plan_id}"))
    rig.clock.set(t_ground)


def finished(rig: Rig) -> bool:
    return not rig.executor.is_busy and rig.executor.lane_index >= rig.executor.lanes_total


def fly(rig: Rig, lanes=LANES, **kwargs) -> None:
    start(rig, lanes, **kwargs)
    assert wait_for(lambda: finished(rig)), rig.timeline


def mark_unsensed(rig: Rig) -> None:
    """Moves from here until the next gate are operator moves (nudge, manual steps, the way back)."""
    rig.timeline.append(("mark", "unsensed"))


def assert_every_step_gated(rig: Rig, unsensed_directions: tuple[str, ...] = ()) -> None:
    """No move after a gate that was not clear; every lane step right after a clear gate."""
    events = rig.timeline
    exempt = False
    for k, event in enumerate(events):
        if event == ("mark", "unsensed"):
            exempt = True
            continue
        if event[0] == "sense":
            exempt = False
        if event[0] == "sense" and event[1] != "clear":
            following = events[k + 1] if k + 1 < len(events) else None
            assert following is None or following[0] != "move", (k, events)
        if event[0] != "move" or event[1] not in HORIZONTAL_DIRECTIONS or exempt:
            continue
        if event[1] in unsensed_directions:
            continue
        assert k > 0 and events[k - 1] == ("sense", "clear"), (k, events)


def flown_pose(drone: SteppedTello) -> RelativePose:
    """The pose from every horizontal move and turn the drone executed on this flight."""
    marks = drone.takeoff_marks
    pose = RelativePose()
    for call in drone.calls[marks[-1] if marks else 0:]:
        if call[0] == "move" and call[1] in HORIZONTAL_DIRECTIONS:
            pose.apply_move(call[1], call[2])
        elif call[0] == "rotate":
            pose.apply_rotation(call[1])
    return pose


def assert_pose_is_sum_of_moves(rig: Rig) -> RelativePose:
    pose = rig.executor.envelope.pose_snapshot()
    expected = flown_pose(rig.drone)
    assert pose.x_cm == pytest.approx(expected.x_cm, abs=1e-6)
    assert pose.y_cm == pytest.approx(expected.y_cm, abs=1e-6)
    assert pose.heading_deg % 360 == pytest.approx(expected.heading_deg % 360, abs=1e-6)
    return pose


def assert_invariants(rig: Rig, unsensed_directions: tuple[str, ...] = ()) -> RelativePose:
    assert_every_step_gated(rig, unsensed_directions)
    return assert_pose_is_sum_of_moves(rig)


def lane_moves(rig: Rig) -> list[tuple]:
    return [e for e in rig.timeline if e[0] == "move" and e[1] in HORIZONTAL_DIRECTIONS]


def rotations(rig: Rig) -> list[int]:
    return [e[1] for e in rig.timeline if e[0] == "rotate"]


def at(pose: RelativePose) -> tuple[float, float, float]:
    return round(pose.x_cm, 6), round(pose.y_cm, 6), round(pose.heading_deg % 360, 6)


def answered(rig: Rig, cmd_id: str) -> dict:
    assert wait_for(lambda: reply_for(rig.sent, cmd_id) is not None), cmd_id
    return reply_for(rig.sent, cmd_id)


def events(rig: Rig, name: str) -> list[dict]:
    return [fields for _, event, fields in rig.log.events if event == name]


# ---------------------------------------------------------------------------
# Without a sensor nothing changes
# ---------------------------------------------------------------------------


def test_without_a_sensor_the_call_sequence_is_exactly_as_before():
    executor, drone, sent, clock, log = make_v2_executor(geofence_cm=700)
    lanes = lanes_of(("forward", 600), ("right", 100), ("back", 200))
    send(executor, assign(lanes=lanes, cmd_id="a1"))
    assert executor._moves == [
        (0, 0, "forward", 300), (0, 1, "forward", 300), (1, 0, "right", 100), (2, 0, "back", 200),
    ]
    t_ground = clock() + 0.5
    send(executor, start_at(t_ground, cmd_id="s1"))
    clock.set(t_ground)
    try:
        assert wait_for(lambda: not executor.is_busy and executor.lane_index == 3)
        assert drone.calls == [
            ("takeoff",),
            ("move", "up", 20),
            ("move", "forward", 300),
            ("move", "forward", 300),
            ("move", "right", 100),
            ("move", "back", 200),
        ]
        assert executor.obstacle_sensing is None
        assert executor.obstacle_status() == {}
        assert not [name for name in log.names() if "obstacle" in name or name == EVENT_LANE_CUT]
    finally:
        executor.shutdown(timeout=2.0)
    validate_sent(sent)


def test_the_off_override_flies_as_before_and_says_so(rigs):
    rig = rigs(sensor=NullSensor(), geofence_cm=700)
    fly(rig, lanes_of(("forward", 600), ("right", 100), ("back", 200)))
    assert [c for c in rig.drone.calls if c[0] in ("move", "rotate")] == [
        ("move", "up", 20), ("move", "forward", 300), ("move", "forward", 300), ("move", "right", 100),
        ("move", "back", 200),
    ]
    status = rig.executor.obstacle_status()
    assert status[TELEMETRY_SENSING] == "off" and status[TELEMETRY_SENSOR_OK] is False
    assert status[TELEMETRY_CUTS] == [] and status[TELEMETRY_CUTS_TOTAL] == 0
    assert TELEMETRY_STATE not in status


# ---------------------------------------------------------------------------
# Nose first and a gate before every step
# ---------------------------------------------------------------------------


def test_every_step_is_gated_and_the_drone_turns_to_face_each_lane(rigs):
    rig = rigs()
    fly(rig, LANES)  # forward 200, right 100, back 200
    clear, fwd = ("sense", "clear"), ("move", "forward", 50)
    assert rig.timeline == [
        ("move", "up", 20),
        *[clear, fwd] * 4,
        ("rotate", 90), *[clear, fwd] * 2,
        ("rotate", 90), *[clear, fwd] * 4,
    ]
    # The first gate after a turn already looks the new way.
    assert [call.heading_deg for call in rig.sensor.calls] == [0.0] * 4 + [90.0] * 2 + [180.0] * 4
    pose = assert_invariants(rig)
    assert at(pose) == (100.0, 0.0, 180.0)
    assert len(events(rig, EVENT_OBSTACLE_TURN)) == 2
    assert len(events(rig, EVENT_OBSTACLE_GATE)) == 10
    status = rig.executor.obstacle_status()
    assert status == {
        TELEMETRY_SENSING: "stepped",
        TELEMETRY_SENSOR_OK: True,
        TELEMETRY_STATE: "clear",
        TELEMETRY_LATENCY_MS: 1.0,
        TELEMETRY_SEQ: 0,
        TELEMETRY_CUTS: [],
        TELEMETRY_CUTS_TOTAL: 0,
        TELEMETRY_CUT_REASONS: "",
    }
    assert set(status) == set(TELEMETRY_FIELDS)


def test_each_gate_judges_a_view_newer_than_the_last_move_or_turn(rigs):
    rig = rigs(motion_s=1.0)
    fly(rig, LANES)
    # Every sense call asks for a view newer than the end of the motion
    # (move or turn) right before it.
    motion_end_before: list[float] = []
    ends = iter(rig.drone.motion_ends)
    last = None
    for event in rig.timeline:
        if event[0] in ("move", "rotate"):
            last = next(ends)
        elif event[0] == "sense":
            motion_end_before.append(last)
    assert [call.after for call in rig.sensor.calls] == motion_end_before
    assert all(call.timeout_s == 2.0 for call in rig.sensor.calls)


def test_a_long_hover_still_needs_a_view_newer_than_the_gate(rigs):
    rig = rigs(auto=False)
    start(rig, LANES)
    assert wait_for(lambda: rig.drone.waiting)  # the climb
    rig.drone.release()
    assert wait_for(lambda: rig.drone.waiting and len(lane_moves(rig)) == 1)
    send(rig.executor, hold(cmd_id="h1"))
    assert answered(rig, "h1")["type"] == "ack"
    rig.drone.release()
    assert wait_for(lambda: not rig.executor.is_busy)
    rig.clock.advance(30.0)  # hovering, held, for half a minute
    rig.drone.auto = True
    send(rig.executor, resume(cmd_id="r1"))
    assert answered(rig, "r1")["type"] == "ack"
    assert wait_for(lambda: finished(rig))
    second = rig.sensor.calls[1]
    assert second.after == pytest.approx(rig.clock() - 0.3)


# ---------------------------------------------------------------------------
# The cut policy and the deficit rule
# ---------------------------------------------------------------------------


def test_worked_example_cut_at_250_cm_rejoins_the_planned_grid(rigs):
    rig = rigs([WALL_AT_250])
    fly(rig, DEMO)
    assert [(e[1], e[2]) for e in lane_moves(rig)] == [("forward", 50)] * 5 + [("forward", 50)] * 2 \
        + [("forward", 50)] * 5 + [("forward", 50)] * 2 + [("forward", 50)] * 8
    assert rotations(rig) == [90, 90, -90, -90]
    pose = assert_invariants(rig)
    # Lane 2 is flown as back 250 and ends on its planned end, so the drone
    # ends where the plan ends.
    assert at(pose) == (200.0, 400.0, 0.0)
    assert rig.executor.obstacle_cuts == [Cut(0, 250, CUT_OBSTACLE)]
    segments = rig.executor.obstacle_segments()
    assert segments == uncovered_segments(DEMO, [(0, 250)])
    assert [(s.lane_index, (s.x_cm, s.y_cm), (s.end_x_cm, s.end_y_cm), s.reason) for s in segments] == [
        (0, (0, 250), (0, 400), CUT_OBSTACLE),
        (2, (100, 250), (100, 400), SEGMENT_SKIPPED),
    ]
    status = rig.executor.obstacle_status()
    assert status[TELEMETRY_CUTS] == [0, 250] and status[TELEMETRY_CUTS_TOTAL] == 1
    assert status[TELEMETRY_SEQ] == 1 and status[TELEMETRY_CUT_REASONS] == "obstacle"
    assert status[TELEMETRY_STATE] == "clear"  # later lanes were gated clear
    stop = events(rig, EVENT_OBSTACLE_STOP)
    assert len(stop) == 1
    assert stop[0]["lane_index"] == 0 and stop[0]["flown_cm"] == 250 and stop[0]["unflown_cm"] == 150
    assert (stop[0]["x_cm"], stop[0]["y_cm"], stop[0]["reason"]) == (0.0, 250.0, CUT_OBSTACLE)
    cuts = events(rig, EVENT_LANE_CUT)
    assert [(c["lane_index"], c["flown_cm"], c["reason"]) for c in cuts] == [
        (0, 250, CUT_OBSTACLE), (2, 250, SEGMENT_SKIPPED),
    ]
    # Never landed for the obstacle.
    assert ("land",) not in rig.timeline and rig.drone.state.is_flying


def test_a_cut_skips_the_rest_of_the_same_direction_run(rigs):
    rig = rigs([BlockedZone(heading_deg=0.0, y_min=200, x_max=50)], geofence_cm=700)
    lanes = lanes_of(("forward", 300), ("forward", 300), ("right", 100), ("back", 300), ("back", 300))
    fly(rig, lanes)
    assert rig.executor.obstacle_cuts == [Cut(0, 200, CUT_OBSTACLE)]
    assert len(lane_moves(rig)) == 4 + 2 + 4
    pose = assert_invariants(rig)
    assert at(pose) == (100.0, 0.0, 180.0)  # the planned end
    assert [(s.lane_index, s.reason) for s in rig.executor.obstacle_segments()] == [
        (0, CUT_OBSTACLE), (1, CUT_OBSTACLE), (3, SEGMENT_SKIPPED), (4, SEGMENT_SKIPPED),
    ]


def test_a_later_lane_toward_the_cut_is_never_lengthened(rigs):
    rig = rigs([BlockedZone(heading_deg=0.0, y_min=100, x_max=50)], geofence_cm=700)
    fly(rig, lanes_of(("forward", 300), ("right", 100), ("forward", 300)))
    pose = assert_invariants(rig)
    # Lane 2 is flown as planned (300 cm), not 500 cm to make up the cut.
    assert at(pose) == (100.0, 400.0, 0.0)
    assert sum(e[2] for e in lane_moves(rig)) == 100 + 100 + 300


def test_blocked_on_the_first_step_flies_nothing_of_that_lane(rigs):
    rig = rigs([BlockedZone(heading_deg=0.0, x_max=50)])
    fly(rig, DEMO)
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_OBSTACLE)]
    pose = assert_invariants(rig)
    # Lane 2 would fly back 0 cm: skipped; lane 4 flies its full 400 cm.
    assert at(pose) == (200.0, 400.0, 0.0)
    assert sum(e[2] for e in lane_moves(rig)) == 100 + 100 + 400
    assert rig.sensor.states()[0] == "blocked"


def test_a_person_cut_reports_its_reason(rigs):
    rig = rigs([BlockedZone(heading_deg=90.0, x_min=50, y_min=300, cue="person")])
    fly(rig, DEMO)
    assert rig.executor.obstacle_cuts == [Cut(1, 50, "person")]
    assert rig.executor.obstacle_status()[TELEMETRY_CUT_REASONS] == "person"
    assert_invariants(rig)


# ---------------------------------------------------------------------------
# Fail-safe: stale, timeout, error, sensor down
# ---------------------------------------------------------------------------


def test_a_stale_sensor_is_retried_then_the_lane_is_cut(rigs):
    rig = rigs()
    rig.sensor.queue("stale", "stale", "stale")
    fly(rig, lanes_of(("forward", 100), ("right", 100)))
    assert rig.sensor.states()[:OBSTACLE_SENSE_RETRIES] == ["stale"] * OBSTACLE_SENSE_RETRIES
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE)]
    assert len(events(rig, EVENT_SENSE_STALE)) == 3
    assert [(e[1], e[2]) for e in lane_moves(rig)] == [("forward", 50)] * 2  # lane 1 only
    pose = assert_invariants(rig)
    assert at(pose) == (100.0, 0.0, 90.0)
    assert rig.executor.obstacle_status()[TELEMETRY_CUT_REASONS] == "sensor_stale"
    assert [(s.lane_index, s.reason) for s in rig.executor.obstacle_segments()] == [(0, CUT_SENSOR_STALE)]


def test_a_view_on_the_last_retry_lets_the_step_fly(rigs):
    rig = rigs()
    rig.sensor.queue("stale", "timeout")
    fly(rig, lanes_of(("forward", 100),))
    assert rig.sensor.states() == ["stale", "timeout", "clear", "clear"]
    assert rig.executor.obstacle_cuts == []
    assert len(events(rig, EVENT_SENSE_TIMEOUT)) == 1
    assert at(assert_invariants(rig)) == (0.0, 100.0, 0.0)


def test_the_gate_hovers_between_attempts(rigs):
    rig = rigs(obstacle_retry_pause_s=0.037)
    rig.sensor.queue("stale")
    waits: list[float] = []
    real_wait = rig.executor._abort.wait

    def recording_wait(timeout=None):
        waits.append(timeout)
        return real_wait(timeout)

    rig.executor._abort.wait = recording_wait
    fly(rig, lanes_of(("forward", 50),))
    assert 0.037 in waits
    assert_invariants(rig)


class HangingSensor(TimelineSensor):
    """Hangs on its first call until released (a hung model); answers normally afterwards."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = threading.Event()
        self.hung = threading.Event()

    def sense(self, after, timeout_s=2.0):
        if not self.hung.is_set():
            self.hung.set()
            self.release.wait(10.0)
            return SenseResult.make_clear()  # far too late: the executor has moved on
        return super().sense(after, timeout_s)


def test_a_hung_sensor_is_abandoned_on_its_helper_thread(rigs):
    rig = rigs(sensor=lambda timeline, envelope, clock: HangingSensor(timeline, envelope.pose_snapshot),
               obstacle_sense_timeout_s=0.05)
    sensor = rig.sensor
    try:
        fly(rig, lanes_of(("forward", 50),))
    finally:
        sensor.release.set()
    timeouts = events(rig, EVENT_SENSE_TIMEOUT)
    assert len(timeouts) == 1 and timeouts[0]["latency_ms"] >= 500
    assert rig.executor.obstacle_cuts == []
    assert_invariants(rig)


def test_a_failing_sensor_is_logged_once_and_cuts_the_lane(rigs):
    rig = rigs()
    rig.sensor.queue(RuntimeError("camera gone"), RuntimeError("camera gone"), RuntimeError("camera gone"))
    fly(rig, lanes_of(("forward", 100), ("right", 100)))
    errors = events(rig, EVENT_SENSOR_ERROR)
    assert len(errors) == 1 and "camera gone" in errors[0]["error"]
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE)]
    assert_invariants(rig)


class DownSensor(TimelineSensor):
    """A sensor that never gets a fresh frame; each call takes *call_s* on the fake clock."""

    def __init__(self, timeline, pose_provider, clock: FakeClock, call_s: float):
        super().__init__(timeline, pose_provider)
        self.clock = clock
        self.call_s = call_s

    def sense(self, after, timeout_s=2.0):
        self.clock.advance(self.call_s)
        self.queue("stale")
        return super().sense(after, timeout_s)


def test_a_sensor_down_for_10_s_ends_the_lanes_through_on_complete(rigs):
    rig = rigs(sensor=lambda timeline, envelope, clock: DownSensor(timeline, envelope.pose_snapshot, clock, 4.0))
    sensor = rig.sensor
    start(rig, LANES, on_complete="land")
    assert wait_for(lambda: ("land",) in rig.timeline and not rig.executor.is_busy), rig.timeline
    # Third attempt: down 12 s (>= 10): no sensor_stale cut, the lanes end.
    assert len(sensor.calls) == 3
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_DOWN)]
    assert lane_moves(rig) == []
    assert len(events(rig, EVENT_LANES_ENDED_SENSOR_DOWN)) == 1
    assert events(rig, "edge_land")[0]["reason"] == "on_complete"
    assert [(s.lane_index, s.reason) for s in rig.executor.obstacle_segments()] == [
        (0, CUT_SENSOR_DOWN), (1, CUT_SENSOR_DOWN), (2, CUT_SENSOR_DOWN),
    ]
    status = rig.executor.obstacle_status()
    assert status[TELEMETRY_SENSOR_OK] is False and status[TELEMETRY_STATE] == "unavailable"
    assert status[TELEMETRY_CUT_REASONS] == "sensor_down"
    assert_invariants(rig)


def test_sensor_ok_turns_false_only_after_the_sensor_was_down_for_10_s(rigs):
    rig = rigs()
    rig.sensor.queue("stale", "stale", "stale")
    fly(rig, lanes_of(("forward", 100),))
    # The plan ended with a sensor_stale cut; the sensor has not looked since.
    assert rig.executor.obstacle_status()[TELEMETRY_SENSOR_OK] is True
    rig.clock.advance(10.0)
    assert rig.executor.obstacle_status()[TELEMETRY_SENSOR_OK] is False


# ---------------------------------------------------------------------------
# Holds, re-task, nudge, manual takeover and landing around the gate
# ---------------------------------------------------------------------------


def test_hold_and_resume_between_steps_and_after_a_turn(rigs):
    rig = rigs(auto=False)
    drone = rig.drone
    start(rig, LANES)
    assert wait_for(lambda: drone.waiting)
    drone.release()  # the climb
    assert wait_for(lambda: drone.waiting and len(lane_moves(rig)) == 1)
    send(rig.executor, hold(cmd_id="h1"))
    assert answered(rig, "h1")["detail"] == "finishing_move"
    drone.release()
    assert wait_for(lambda: not rig.executor.is_busy)
    # Stopped after the step in flight, before the next gate.
    assert len(rig.sensor.calls) == 1 and len(lane_moves(rig)) == 1
    send(rig.executor, resume(cmd_id="r1"))
    assert answered(rig, "r1")["type"] == "ack"
    # Steps 2 to 4 of lane 0, the turn, then hold during lane 1's first step.
    for _ in range(3):
        assert wait_for(lambda: drone.waiting)
        drone.release()
    assert wait_for(lambda: drone.waiting and len(lane_moves(rig)) == 5)
    assert rotations(rig) == [90]
    send(rig.executor, hold(cmd_id="h2"))
    assert answered(rig, "h2")["type"] == "ack"
    drone.release()
    assert wait_for(lambda: not rig.executor.is_busy)
    assert at(rig.executor.envelope.pose_snapshot()) == (50.0, 200.0, 90.0)
    gates_before = len(rig.sensor.calls)
    drone.auto = True
    send(rig.executor, resume(cmd_id="r2"))
    assert answered(rig, "r2")["type"] == "ack"
    assert wait_for(lambda: finished(rig))
    # Already facing lane 1: no second turn, but a fresh gate.
    assert rotations(rig) == [90, 90]
    assert rig.sensor.calls[gates_before].heading_deg == 90.0
    assert at(assert_invariants(rig)) == (100.0, 0.0, 180.0)


def test_a_hold_between_attempts_stops_without_a_cut(rigs):
    rig = rigs()
    rig.sensor.queue("stale")
    rig.sensor.on_call = lambda n: send(rig.executor, hold(cmd_id="h1")) if n == 1 else None
    start(rig, LANES)
    assert wait_for(lambda: reply_for(rig.sent, "h1") is not None and not rig.executor.is_busy)
    assert len(rig.sensor.calls) == 1 and rig.executor.obstacle_cuts == []
    assert lane_moves(rig) == []
    send(rig.executor, resume(cmd_id="r1"))
    assert wait_for(lambda: finished(rig))
    assert rig.executor.obstacle_cuts == []
    assert at(assert_invariants(rig)) == (100.0, 0.0, 180.0)


def test_retask_after_a_cut_shortens_the_appended_lane_and_reports_its_cuts(rigs):
    rig = rigs([WALL_AT_250, BlockedZone(heading_deg=180.0, y_max=100, x_min=50)])
    fly(rig, lanes_of(("forward", 400), ("right", 100)))
    assert rig.executor.obstacle_cuts == [Cut(0, 250, CUT_OBSTACLE)]
    assert at(rig.executor.envelope.pose_snapshot()) == (100.0, 250.0, 90.0)
    # The ground appends a lane (a re-task that continues the plan).
    lanes = lanes_of(("forward", 400), ("right", 100), ("back", 400))
    send(rig.executor, assign(lanes=lanes, plan_id="p-2", plan_seq=2, cmd_id="a-p-2"))
    assert answered(rig, "a-p-2")["type"] == "ack"
    assert wait_for(lambda: finished(rig))
    # The appended back lane is shortened to 250 cm by the cut's deficit,
    # then blocked at y 100: the cut carries the lane's index in the
    # current lane list.
    assert rig.executor.obstacle_cuts == [Cut(0, 250, CUT_OBSTACLE), Cut(2, 150, CUT_OBSTACLE)]
    status = rig.executor.obstacle_status()
    assert status[TELEMETRY_CUTS] == [0, 250, 2, 150] and status[TELEMETRY_SEQ] == 2
    assert status[TELEMETRY_CUT_REASONS] == "obstacle,obstacle"
    assert [(s.lane_index, (s.x_cm, s.y_cm), (s.end_x_cm, s.end_y_cm), s.reason)
            for s in rig.executor.obstacle_segments()] == [
        (0, (0, 250), (0, 400), CUT_OBSTACLE),
        (2, (100, 250), (100, 400), SEGMENT_SKIPPED),
        (2, (100, 100), (100, 0), CUT_OBSTACLE),
    ]
    assert at(assert_invariants(rig)) == (100.0, 100.0, 180.0)


def test_a_retask_during_a_gate_is_looked_at_again(rigs):
    rig = rigs([BlockedZone(heading_deg=0.0)])
    new_lanes = lanes_of(("right", 100),)

    def retask(n):
        if n == 1:
            send(rig.executor, assign(lanes=new_lanes, plan_id="p-2", plan_seq=2, cmd_id="a-p-2"))

    rig.sensor.on_call = retask
    start(rig, LANES)
    assert wait_for(lambda: reply_for(rig.sent, "a-p-2") is not None)
    assert wait_for(lambda: finished(rig) and rig.executor.plan_id == "p-2")
    # The blocked answer was for the old plan's step: no cut, the new plan flies.
    assert rig.sensor.states()[0] == "blocked"
    assert rig.executor.obstacle_cuts == []
    assert at(assert_invariants(rig)) == (100.0, 0.0, 90.0)


def test_nudge_after_a_cut_then_retask(rigs):
    rig = rigs([WALL_AT_250])
    fly(rig, lanes_of(("forward", 400),))
    assert at(rig.executor.envelope.pose_snapshot()) == (0.0, 250.0, 0.0)
    mark_unsensed(rig)
    send(rig.executor, nudge("left", 50, cmd_id="n1"))
    assert answered(rig, "n1")["type"] == "ack"
    assert wait_for(lambda: not rig.executor.is_busy)
    assert at(rig.executor.envelope.pose_snapshot()) == (-50.0, 250.0, 0.0)
    send(rig.executor, assign(lanes=lanes_of(("forward", 400), ("right", 100), ("back", 400)),
                              plan_id="p-2", plan_seq=2, cmd_id="a-p-2"))
    assert answered(rig, "a-p-2")["type"] == "ack"
    assert wait_for(lambda: finished(rig))
    # The nudge offsets the rest of the plan (as always); the back lane is
    # still shortened by the cut.
    assert at(assert_invariants(rig)) == (50.0, 0.0, 180.0)


def manual(cmd_id, direction, amount):
    if direction in ("cw", "ccw"):
        return fp.make_manual_move(direction, degrees=amount, cmd_id=cmd_id, fence=FENCE)
    return fp.make_manual_move(direction, distance_cm=amount, cmd_id=cmd_id, fence=FENCE)


def test_manual_takeover_with_a_rotated_heading_resumes_through_the_gate(rigs):
    rig = rigs(auto=False)
    drone = rig.drone
    start(rig, LANES)
    for _ in range(5):  # the climb and lane 0's four steps
        assert wait_for(lambda: drone.waiting)
        drone.release()
    assert wait_for(lambda: drone.waiting and len(lane_moves(rig)) == 5)  # lane 1's first step
    mark_unsensed(rig)
    send(rig.executor, fp.make_manual_hold(cmd_id="m1", fence=FENCE))
    assert answered(rig, "m1")["detail"] == "finishing_move"
    drone.release()
    assert wait_for(lambda: not rig.executor.is_busy)
    drone.auto = True
    hold_point = rig.executor.manual_state.hold_pose
    assert (hold_point.x_cm, hold_point.y_cm, hold_point.heading_deg) == (50.0, 200.0, 90.0)
    for cmd_id, direction, amount in (("m2", "cw", 30), ("m3", "forward", 50)):
        send(rig.executor, manual(cmd_id, direction, amount))
        assert answered(rig, cmd_id)["type"] == "ack"
        assert wait_for(lambda: not rig.executor.is_busy)
    assert rig.executor.envelope.pose_snapshot().heading_deg == 120.0
    gates_before = len(rig.sensor.calls)
    send(rig.executor, fp.make_resume_search(cmd_id="m4", fence=FENCE))
    assert answered(rig, "m4")["type"] == "ack"
    assert wait_for(lambda: finished(rig))
    # The way back restores the hold heading (-30); lane 1 then continues
    # through a gate at heading 90 and lane 2 turns to 180.
    assert rotations(rig) == [90, 30, -30, 90]
    assert rig.sensor.calls[gates_before].heading_deg == 90.0
    pose = assert_invariants(rig)
    assert pose.heading_deg == 180.0 and pose.y_cm == pytest.approx(0.0)


def test_a_landing_during_a_gate_sends_no_move(rigs):
    rig = rigs()
    entered, release = threading.Event(), threading.Event()

    def block(n):
        entered.set()
        release.wait(5.0)

    rig.sensor.on_call = block
    start(rig, LANES)
    assert entered.wait(5.0)
    send(rig.executor, fp.make_land(cmd_id="l1", fence=FENCE))
    assert wait_for(lambda: not rig.drone.state.is_flying)
    release.set()  # the gate now answers clear, after the landing
    assert wait_for(lambda: not rig.executor.is_busy)
    assert lane_moves(rig) == []
    assert rig.timeline[-2:] == [("land",), ("sense", "clear")]
    assert_invariants(rig)


# ---------------------------------------------------------------------------
# Forward-only mode, plan frame, telemetry
# ---------------------------------------------------------------------------


def test_forward_only_mode_never_turns_and_senses_only_forward_steps(rigs):
    rig = rigs(obstacle_turn=False)
    fly(rig, LANES)
    assert rotations(rig) == []
    clear = ("sense", "clear")
    assert rig.timeline == [
        ("move", "up", 20),
        *[clear, ("move", "forward", 50)] * 4,
        *[("move", "right", 50)] * 2,
        *[("move", "back", 50)] * 4,
    ]
    assert at(assert_invariants(rig, unsensed_directions=("right", "back"))) == (100.0, 0.0, 0.0)
    assert rig.executor.obstacle_status()[TELEMETRY_SENSING] == "forward_only"


def test_the_geofence_check_of_a_plan_is_made_in_the_plan_frame(rigs):
    rig = rigs(geofence_cm=300)
    fly(rig, LANES)
    assert at(rig.executor.envelope.pose_snapshot()) == (100.0, 0.0, 180.0)
    # Facing back, a body "right" move would go to x -150 (inside), but the
    # drone turns to face plan "right" first and would reach x 350.
    send(rig.executor, assign(lanes=lanes_of(("right", 250),), plan_id="p-2", plan_seq=2, cmd_id="a-p-2"))
    nack = answered(rig, "a-p-2")
    assert nack["type"] == "nack" and nack["reason"] == "geofence"
    assert (nack["x_cm"], nack["y_cm"], nack["rel_x_cm"]) == (350.0, 0.0, 100.0)


def test_cut_fields_survive_a_landing_and_clear_on_the_next_plan(rigs):
    rig = rigs([WALL_AT_250])
    fly(rig, DEMO)
    send(rig.executor, fp.make_land(cmd_id="l1", fence=FENCE))
    assert wait_for(lambda: rig.executor.state == "idle" and not rig.executor.is_busy)
    status = rig.executor.obstacle_status()
    assert status[TELEMETRY_CUTS] == [0, 250] and status[TELEMETRY_SEQ] == 1
    send(rig.executor, assign(lanes=DEMO, plan_id="p-2", plan_seq=2, cmd_id="a-p-2"))
    assert answered(rig, "a-p-2")["type"] == "ack"
    status = rig.executor.obstacle_status()
    assert status[TELEMETRY_CUTS] == [] and status[TELEMETRY_CUTS_TOTAL] == 0
    assert status[TELEMETRY_CUT_REASONS] == ""
    assert status[TELEMETRY_SEQ] == 1  # never decreases
    assert TELEMETRY_STATE not in status and TELEMETRY_LATENCY_MS not in status
    assert rig.executor.obstacle_segments() == []


def test_telemetry_lists_the_last_eight_cuts(rigs):
    rig = rigs()
    rig.sensor.queue(*[SenseResult.make_blocked()] * 10)
    pairs = [("forward", 50), ("right", 50)] * 5
    fly(rig, lanes_of(*pairs))
    assert len(rig.executor.obstacle_cuts) == 10
    status = rig.executor.obstacle_status()
    assert status[TELEMETRY_CUTS] == [n for lane in range(2, 10) for n in (lane, 0)]
    assert status[TELEMETRY_CUTS_TOTAL] == 10 and status[TELEMETRY_SEQ] == 10
    assert len(status[TELEMETRY_CUTS]) == 16
    assert lane_moves(rig) == []
    assert_invariants(rig)


# ---------------------------------------------------------------------------
# A required sensor that could not be built
# ---------------------------------------------------------------------------


def test_start_and_takeoff_are_refused_when_the_sensor_is_unavailable(rigs):
    rig = rigs(sensor=None, obstacle_sensor_error="MiDaS weights not cached")
    detail = "obstacle sensor unavailable: MiDaS weights not cached"
    send(rig.executor, assign(lanes=LANES, cmd_id="a1"))
    assert answered(rig, "a1")["type"] == "ack"
    send(rig.executor, start_at(rig.clock() + 0.5, cmd_id="s1"))
    nack = answered(rig, "s1")
    assert (nack["type"], nack["reason"], nack["detail"]) == ("nack", "drone_unreachable", detail)
    send(rig.executor, takeoff(cmd_id="t1"))
    nack = answered(rig, "t1")
    assert (nack["type"], nack["reason"], nack["detail"]) == ("nack", "drone_unreachable", detail)
    assert wait_for(lambda: not rig.executor.is_busy)
    assert ("takeoff",) not in rig.drone.calls
    refused = events(rig, EVENT_START_REFUSED)
    assert [r["reason"] for r in refused] == [START_REFUSED_REASON, START_REFUSED_REASON]
    status = rig.executor.obstacle_status()
    assert status[TELEMETRY_SENSING] == "stepped" and status[TELEMETRY_SENSOR_OK] is False
    # Landing is never gated.
    send(rig.executor, fp.make_land(cmd_id="l1", fence=FENCE))
    assert answered(rig, "l1")["type"] == "ack"
    assert ("land",) in rig.drone.calls


def test_without_its_sensor_no_lane_step_is_ever_cleared(rigs):
    rig = rigs(sensor=None, obstacle_sensor_error="no camera")
    # The operator may still take off by hand, but the lanes never fly.
    send(rig.executor, fp.make_manual_hold(cmd_id="m1", fence=FENCE))
    send(rig.executor, fp.make_manual_takeoff(cmd_id="m2", fence=FENCE))
    assert answered(rig, "m2")["type"] == "ack"
    assert wait_for(lambda: not rig.executor.is_busy)
    send(rig.executor, assign(lanes=LANES, cmd_id="a1"))
    assert answered(rig, "a1")["type"] == "ack"
    send(rig.executor, fp.make_resume_search(cmd_id="m3", fence=FENCE))
    assert answered(rig, "m3")["type"] == "ack"
    assert wait_for(lambda: finished(rig))
    assert lane_moves(rig) == []
    # Lane 2 (back 200) would only fly back over lane 0's cut: the deficit
    # rule skips it, so it is never gated.
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE), Cut(1, 0, CUT_SENSOR_STALE)]
    assert len(events(rig, EVENT_SENSOR_ERROR)) == 1


def test_a_sensor_and_a_sensor_error_are_exclusive():
    clock = FakeClock()
    envelope = SafetyEnvelope(alt_slot_cm=100, clock=clock)
    with pytest.raises(ValueError):
        LaneExecutor(SteppedTello(), envelope, "d", send=lambda m: None, obstacle_sensor=NullSensor(),
                     obstacle_sensor_error="x")
    with pytest.raises(ValueError):
        LaneExecutor(SteppedTello(), envelope, "d", send=lambda m: None, obstacle_step_cm=10)


# ---------------------------------------------------------------------------
# An autopilot that avoids by itself
# ---------------------------------------------------------------------------


class AvoidingDrone(GatedTello):
    """A fake autopilot drone with onboard avoidance that stops its first forward move at 250 cm."""

    def __init__(self, timeline, **kwargs):
        super().__init__(timeline, **kwargs)
        self.capabilities = replace(
            self.capabilities, features=self.capabilities.features | {Capability.ONBOARD_AVOIDANCE}
        )
        self.pose = RelativePose()
        self.stopped = False

    def move(self, direction, distance_cm):
        super().move(direction, distance_cm)
        if direction == "forward" and not self.stopped:
            self.stopped = True
            self.pose.apply_move(direction, 250)
            return CommandResult("move", CommandOutcome.STOPPED_SHORT, "held back at 250 cm")
        if direction in HORIZONTAL_DIRECTIONS:
            self.pose.apply_move(direction, distance_cm)
        return True

    def measured_pose(self):
        return RelativePose(self.pose.x_cm, self.pose.y_cm, self.pose.heading_deg)


def test_an_autopilot_stopped_short_cuts_the_lane_at_its_measured_position(rigs):
    rig = rigs(drone=lambda timeline, clock: AvoidingDrone(timeline, auto=True, name="drone-1", battery=90),
               sensor=None)
    drone = rig.drone
    assert rig.executor.obstacle_sensing == "autopilot"
    fly(rig, DEMO)
    assert [c for c in drone.calls if c[0] == "move" and c[1] != "up"] == [
        ("move", "forward", 400), ("move", "right", 100), ("move", "back", 250), ("move", "right", 100),
        ("move", "forward", 400),
    ]
    assert rig.executor.obstacle_cuts == [Cut(0, 250, CUT_AUTOPILOT)]
    pose = rig.executor.envelope.pose_snapshot()
    assert (pose.x_cm, pose.y_cm) == (200.0, 400.0)
    assert (drone.pose.x_cm, drone.pose.y_cm) == (200.0, 400.0)
    status = rig.executor.obstacle_status()
    assert status[TELEMETRY_SENSING] == "autopilot" and status[TELEMETRY_SENSOR_OK] is True
    assert status[TELEMETRY_CUTS] == [0, 250] and status[TELEMETRY_CUT_REASONS] == "autopilot"
    assert events(rig, EVENT_OBSTACLE_STOP)[0]["cue"] == "autopilot"
    assert ("land",) not in rig.timeline


def test_a_stopped_short_outside_a_lane_still_lands():
    """Only a lane move may stop short; a climb that stops short is an error, as before."""

    class ShortClimb(SteppedTello):
        def move(self, direction, distance_cm):
            super().move(direction, distance_cm)
            if direction == "up":
                return CommandResult("move", CommandOutcome.STOPPED_SHORT, "held")
            return True

    executor, drone, sent, clock, log = make_v2_executor(drone=ShortClimb(auto=True, name="drone-1", battery=90))
    try:
        send(executor, assign(lanes=LANES, cmd_id="a1"))
        t_ground = clock() + 0.5
        send(executor, start_at(t_ground, cmd_id="s1"))
        clock.set(t_ground)
        assert wait_for(lambda: ("land",) in drone.calls and not executor.is_busy)
        assert events_named(log, "edge_executor_error")
        assert not [c for c in drone.calls if c[0] == "move" and c[1] in HORIZONTAL_DIRECTIONS]
    finally:
        executor.shutdown(timeout=2.0)


def events_named(log: QuietLogger, name: str) -> list[dict]:
    return [fields for _, event, fields in log.events if event == name]


def test_stale_reason_constant_matches_the_fake_override():
    # The executor treats any unavailable result alike; this pins the fake's spelling.
    assert SenseResult.make_unavailable().reason == REASON_STALE
