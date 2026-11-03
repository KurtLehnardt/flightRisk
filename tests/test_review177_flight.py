"""Fixes for the flight-path findings of the opus reviews of PR #177: B1, N2, N5, the sense-thread nit, R2-B2 and R2-N1.

* **B1.** A drone that measures its heading in the plan frame (the MAVLink
  fleet drone's ``measured_heading_deg()``) has a step cleared only while it
  faces the commanded heading within ``HEADING_TOLERANCE_DEG``; otherwise the
  gate is ``unavailable`` (reason ``stale``, cause ``heading not reached``)
  and the normal retry and ``sensor_stale`` path applies. That covers lane 0
  when an autopilot's plan frame is not its takeoff yaw (the drone is asked
  once to turn to the commanded heading first). On ArduPilot ``rotate`` turns
  the vehicle in place with the link's ``yaw_to`` and waits for the measured
  heading; a heading not reached by the deadline is ``NOT_EXECUTED`` and the
  executor cuts the lane instead of clearing or landing. A link without
  ``yaw_to`` cannot turn an ArduCopter, so its ``rotate`` still only turns
  the plan, and the gate then cuts every step the vehicle does not face. The
  Tello measures no plan-frame heading and is unchanged.
* **N5.** A landing, and a plan that does not continue the current one,
  probe a sensor whose down timer runs with one ``sense`` call; a view
  clears the timer, so ``obstacle_sensor_ok`` describes the sensor now.
* **N2.** The way back of ``resume_search`` is flown nose first in sensed
  steps.
* **R2-B2** (round 2). A gate on that way back that does not clear cuts
  nothing: the drone is put back under manual control with its hold point
  kept, and the lanes are only ever flown from the hold point (the critic's
  probe: held at (0, 100), 100 cm right by hand, the way back blocked, then
  a second hold, a nudge or a manual step, and resume again). A way back
  that a v2 hold or a link-loss hold stopped is flown again before the plan.
* **R2-N1** (round 2). A plan that does not continue the current one gets
  its own sensor-down time, while a sensor that is really down stays
  reported down; and a gate waits for the new plan's probe (which may hold a
  camera's one inference worker) instead of counting a busy worker.

Fakes only: the fake Tello, the scripted sensor, a fake clock, and the
simulated MAVSDK of ``tests.support.fake_mavsdk``. Nothing here has flown.
"""
from __future__ import annotations

import math
import threading
import time

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone.autopilot import ARDUPILOT, PX4
from flightrisk.drone.controller import CommandOutcome, CommandResult, command_result
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.lane_flight import (
    EVENT_SENSOR_PROBE,
    HEADING_NOT_REACHED,
    HEADING_TOLERANCE_DEG,
)
from flightrisk.drone.lane_steps import Cut, replay_lanes
from flightrisk.drone.mavlink_fleet import (
    EVENT_STEPPED_CANNOT_YAW,
    NO_AVOIDANCE_CANNOT_YAW_REMEDY,
    SENSING_NONE,
    STEPPED_CANNOT_YAW_DETAIL,
    MavlinkFleetDrone,
    MavlinkFleetOptions,
)
from flightrisk.drone.mavsdk_link import MavsdkAutopilotLink
from flightrisk.drone.obstacle_sensor import (
    CUT_OBSTACLE,
    CUT_SENSOR_DOWN,
    CUT_SENSOR_STALE,
    EVENT_OBSTACLE_GATE,
    EVENT_SENSE_STALE,
    OBSTACLE_SENSE_RETRIES,
    REASON_TIMEOUT,
    SENSING_STEPPED,
    TELEMETRY_CUT_REASONS,
    TELEMETRY_SENSOR_OK,
    BlockedZone,
    FakeObstacleSensor,
    SenseResult,
)
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController, TelloController
from tests import test_lane_executor_obstacle as obstacle_tests
from tests.lane_executor_support import (
    FENCE,
    LANES,
    QuietLogger,
    assign,
    hold,
    nudge,
    reply_for,
    resume,
    send,
    wait_for,
)
from tests.support.fake_mavsdk import SimVehicle, install
from tests.test_lane_executor_obstacle import (
    GatedTello,
    TimelineSensor,
    answered,
    assert_invariants,
    at,
    events,
    finished,
    fly,
    lane_moves,
    lanes_of,
    manual,
    mark_unsensed,
    rotations,
    start,
)

#: The stepped-sensing executor rig of ``test_lane_executor_obstacle`` (a fixture).
rigs = obstacle_tests.rigs

# ---------------------------------------------------------------------------
# B1 on the executor: a drone that measures its heading
# ---------------------------------------------------------------------------


class HeadingTello(GatedTello):
    """A stepped fake Tello that measures its heading in the plan frame, like an autopilot's compass.

    ``actual`` is where the vehicle points and ``commanded`` the sum of the
    turns it executed. A turn points the vehicle at the commanded heading
    (so ``rotate(0)`` turns it back there) unless ``turns`` is False, an
    autopilot that ignores the yaw. For ``lag_reads`` heading reads after a
    turn of more than 0 degrees the old heading is still shown. ``script``
    values are returned first, one per read. The first ``fail_turns`` turns
    are reported ``NOT_EXECUTED`` (heading not reached) and change nothing.
    """

    def __init__(self, timeline, *, start_heading=0.0, turns=True, lag_reads=0, fail_turns=0, script=(), **kwargs):
        super().__init__(timeline, **kwargs)
        self.actual = float(start_heading)
        self.commanded = 0.0
        self.turns = turns
        self.lag_reads = lag_reads
        self.fail_turns = fail_turns
        self.script = list(script)
        self.reads: list = []
        self._shown = None
        self._lag = 0

    def rotate(self, degrees):
        if self.fail_turns > 0:
            self.fail_turns -= 1
            self.timeline.append(("rotate_not_executed", degrees))
            return CommandResult("rotate", CommandOutcome.NOT_EXECUTED, "heading not reached")
        result = super().rotate(degrees)
        self.commanded = (self.commanded + degrees) % 360.0
        if self.turns:
            if degrees:
                self._shown, self._lag = self.actual, self.lag_reads
            self.actual = self.commanded
        return result

    def measured_heading_deg(self):
        if self.script:
            value = self.script.pop(0)
        elif self._lag > 0:
            self._lag -= 1
            value = self._shown
        else:
            value = self.actual
        self.reads.append(value)
        return value


def heading_tello(**kwargs):
    return lambda timeline, clock: HeadingTello(timeline, clock=clock, auto=True, name="drone-1", battery=90, **kwargs)


def stale_events(rig):
    return events(rig, EVENT_SENSE_STALE)


def test_a_turn_the_vehicle_did_not_make_never_clears_the_step(rigs):
    """B1: ArduCopter without a yaw command: the turn is reported done, the camera still looks the old way."""
    rig = rigs(drone=heading_tello(turns=False))
    fly(rig, lanes_of(("forward", 100), ("right", 100)))
    # Lane 1: after the turn the drone still measures 0 degrees. It is asked
    # once to turn to 90 (rotate 0); no view is judged and no step is flown.
    assert rotations(rig) == [90, 0]
    assert [(e[1], e[2]) for e in lane_moves(rig)] == [("forward", 50)] * 2  # lane 0 only
    assert len(rig.sensor.calls) == 2
    assert rig.executor.obstacle_cuts == [Cut(1, 0, CUT_SENSOR_STALE)]
    stale = stale_events(rig)
    assert len(stale) == OBSTACLE_SENSE_RETRIES
    assert all(e["cause"] == HEADING_NOT_REACHED for e in stale)
    assert (stale[0]["measured_heading_deg"], stale[0]["commanded_heading_deg"]) == (0.0, 90.0)
    gates = events(rig, EVENT_OBSTACLE_GATE)
    assert [g["state"] for g in gates[2:]] == ["unavailable"] * OBSTACLE_SENSE_RETRIES
    assert ("land",) not in rig.timeline
    assert_invariants(rig)


def test_a_lagging_heading_holds_the_step_until_the_drone_faces_it(rigs):
    rig = rigs(drone=heading_tello(lag_reads=3))
    fly(rig, lanes_of(("forward", 50), ("right", 50)))
    # Lane 1: the compass shows 0 for three reads after the turn (the
    # re-turn check and two gate attempts); the third attempt faces 90.
    assert rotations(rig) == [90, 0]
    assert len(stale_events(rig)) == 2
    assert rig.executor.obstacle_cuts == []
    assert rig.timeline[-4:] == [("rotate", 90), ("rotate", 0), ("sense", "clear"), ("move", "forward", 50)]
    assert [call.heading_deg for call in rig.sensor.calls] == [0.0, 90.0]
    assert_invariants(rig)


def test_lane_0_is_turned_to_the_frame_heading_first(rigs):
    """B1: an autopilot placed 40 degrees off its plan frame faces lane 0 before the first gate."""
    rig = rigs(drone=heading_tello(start_heading=40.0))
    fly(rig, lanes_of(("forward", 100),))
    assert rig.drone.reads[0] == 40.0
    assert rig.timeline[:4] == [("move", "up", 20), ("rotate", 0), ("sense", "clear"), ("move", "forward", 50)]
    assert rotations(rig) == [0]
    assert rig.executor.obstacle_cuts == []
    assert_invariants(rig)


def test_a_drone_that_cannot_turn_flies_no_lane_and_never_looks(rigs):
    rig = rigs(drone=heading_tello(start_heading=40.0, turns=False))
    fly(rig, lanes_of(("forward", 100), ("right", 100)))
    assert lane_moves(rig) == [] and rig.sensor.calls == []
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE), Cut(1, 0, CUT_SENSOR_STALE)]
    assert rotations(rig) == [0, 90, 0]
    assert ("land",) not in rig.timeline
    assert_invariants(rig)


def test_a_clear_view_taken_while_the_heading_swung_away_does_not_count(rigs):
    # Reads: the re-turn check 0, before the look 0, after the look 25 (off).
    rig = rigs(drone=heading_tello(script=[0.0, 0.0, 25.0]))
    fly(rig, lanes_of(("forward", 50),))
    assert rig.sensor.states() == ["clear", "clear"]
    assert [g["state"] for g in events(rig, EVENT_OBSTACLE_GATE)] == ["unavailable", "clear"]
    assert stale_events(rig)[0]["measured_heading_deg"] == 25.0
    assert len(lane_moves(rig)) == 1
    assert_invariants(rig)


def test_an_unknown_heading_never_clears_a_step(rigs):
    rig = rigs(drone=heading_tello(script=[None] * (1 + OBSTACLE_SENSE_RETRIES)))
    fly(rig, lanes_of(("forward", 50),))
    assert rig.sensor.calls == [] and lane_moves(rig) == []
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE)]
    assert all(e["measured_heading_deg"] is None for e in stale_events(rig))
    assert_invariants(rig)


def test_a_turn_that_did_not_finish_cuts_the_lane_and_never_lands(rigs):
    """B1: an autopilot's turn that misses its deadline is not facing the step: cut, never cleared."""
    rig = rigs(drone=heading_tello(fail_turns=1))
    fly(rig, lanes_of(("forward", 50), ("right", 50), ("back", 50)))
    assert ("rotate_not_executed", 90) in rig.timeline
    assert rig.executor.obstacle_cuts == [Cut(1, 0, CUT_SENSOR_STALE)]
    # Lane 2 turns from the heading the drone still has (0) to 180 and flies.
    assert rotations(rig) == [-180]
    assert [(e[1], e[2]) for e in lane_moves(rig)] == [("forward", 50)] * 2
    turn = [e for e in stale_events(rig) if e.get("turn_degrees") is not None]
    assert turn and turn[0]["cause"] == HEADING_NOT_REACHED and turn[0]["turn_degrees"] == 90
    assert ("land",) not in rig.timeline and not events(rig, "edge_executor_error")
    assert_invariants(rig)


class FailingTurnTello(GatedTello):
    """A fake Tello whose turns are reported not executed; it measures no heading."""

    def rotate(self, degrees):
        self.timeline.append(("rotate_not_executed", degrees))
        return CommandResult("rotate", CommandOutcome.NOT_EXECUTED, "dropped")


def test_a_failed_turn_on_a_drone_without_a_heading_still_lands(rigs):
    rig = rigs(drone=lambda timeline, clock: FailingTurnTello(timeline, clock=clock, auto=True, name="drone-1",
                                                               battery=90))
    start(rig, lanes_of(("forward", 50), ("right", 50)))
    assert wait_for(lambda: ("land",) in rig.timeline and not rig.executor.is_busy), rig.timeline
    assert events(rig, "edge_executor_error")
    assert rig.executor.obstacle_cuts == []
    assert [(e[1], e[2]) for e in lane_moves(rig)] == [("forward", 50)]


def test_turning_switched_off_never_turns_and_still_holds_an_unfaced_step(rigs):
    """FLIGHTRISK_OBSTACLE_TURN=0: no turn at all, not even to the commanded heading."""
    rig = rigs(drone=heading_tello(start_heading=40.0), obstacle_turn=False)
    fly(rig, lanes_of(("forward", 50), ("right", 50)))
    assert rotations(rig) == []
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE)]
    assert [(e[1], e[2]) for e in lane_moves(rig)] == [("right", 50)]  # flown unsensed, as before
    assert_invariants(rig, unsensed_directions=("right",))


def test_the_tello_measures_no_plan_frame_heading():
    """Its yaw is the IMU's, relative to power-on and drifting, and its turn reply arrives after the turn."""
    assert not hasattr(TelloController, "measured_heading_deg")
    assert not hasattr(FakeTelloController, "measured_heading_deg")
    assert HEADING_TOLERANCE_DEG == 10.0


# ---------------------------------------------------------------------------
# B1 on the MAVLink drone (simulated MAVSDK)
# ---------------------------------------------------------------------------


class YawingLink(MavsdkAutopilotLink):
    """The MAVSDK link plus the optional ``yaw_to`` (``MAV_CMD_CONDITION_YAW``), played on the simulated vehicle.

    The simulated MAVSDK has no yaw command, so ``yaw_to`` sets the vehicle's
    yaw goal directly (it then turns as PX4 turns to a reposition's yaw).
    With ``turns=False`` it is accepted and ignored, like an autopilot that
    never reaches the heading. Every call is recorded in ``vehicle.commands``.
    """

    def __init__(self, address, vehicle, *, turns=True, **kwargs):
        super().__init__(address, **kwargs)
        self.vehicle = vehicle
        self.turns = turns

    def yaw_to(self, yaw_deg):
        with self.vehicle.lock:
            self.vehicle.commands.append(("yaw_to", round(float(yaw_deg), 3)))
            if self.turns:
                self.vehicle.goal_yaw = float(yaw_deg) % 360.0


@pytest.fixture
def make_mav(monkeypatch):
    built = []

    def _make(vehicle=None, *, yawing=True, turns=True, connect=True, event_log=None, **options):
        vehicle = vehicle or SimVehicle(ARDUPILOT, time_scale=20.0)
        install(monkeypatch, vehicle)
        address = "tcpout://127.0.0.1:1"
        link = YawingLink(address, vehicle, turns=turns) if yawing else MavsdkAutopilotLink(address)
        drone = MavlinkFleetDrone("drone-1", link, MavlinkFleetOptions(**options), event_log=event_log)
        drone.bind_slot_source(lambda: 1500.0)
        built.append(drone)
        if connect:
            assert drone.connect() is True
        return drone, vehicle

    yield _make
    for drone in built:
        drone.disconnect()


def airborne(drone, vehicle):
    assert wait_for(lambda: drone.link_status().ok), drone.link_status()
    assert command_result(drone.takeoff(), "takeoff").ok
    assert vehicle.in_air


def angle(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


def test_ardupilot_rotate_turns_in_place_and_waits_for_the_heading(make_mav):
    drone, vehicle = make_mav(frame_heading_deg=0.0)
    airborne(drone, vehicle)
    assert angle(drone.measured_heading_deg(), 0.0) <= 1.0
    result = command_result(drone.rotate(90), "rotate")
    assert result.ok, result
    assert ("yaw_to", 90.0) in vehicle.commands
    assert angle(vehicle.heading, 90.0) <= HEADING_TOLERANCE_DEG
    assert angle(drone.measured_heading_deg(), 90.0) <= HEADING_TOLERANCE_DEG
    assert command_result(drone.move("forward", 500), "move").ok
    assert vehicle.east == pytest.approx(5.0, abs=0.6) and vehicle.north == pytest.approx(0.0, abs=0.6)


def test_rotate_0_turns_an_ardupilot_to_its_plan_frame_heading(make_mav):
    """B1 lane 0: the vehicle took off facing north, the plan frame faces east."""
    drone, vehicle = make_mav(frame_heading_deg=90.0)
    airborne(drone, vehicle)
    assert angle(drone.measured_heading_deg(), 270.0) <= 1.0  # north is 270 degrees in an east frame
    assert command_result(drone.rotate(0), "rotate").ok
    assert ("yaw_to", 90.0) in vehicle.commands
    assert angle(drone.measured_heading_deg(), 0.0) <= HEADING_TOLERANCE_DEG
    assert command_result(drone.rotate(90), "rotate").ok
    assert ("yaw_to", 180.0) in vehicle.commands  # compass heading: frame 90 plus 90
    assert angle(drone.measured_heading_deg(), 90.0) <= HEADING_TOLERANCE_DEG


def test_ardupilot_heading_not_reached_is_not_executed_and_keeps_the_commanded_heading(make_mav):
    drone, vehicle = make_mav(turns=False, frame_heading_deg=0.0, yaw_rate_dps=1000.0, move_timeout_margin_s=0.5)
    airborne(drone, vehicle)
    started = time.monotonic()
    result = command_result(drone.rotate(90), "rotate")
    assert result.outcome is CommandOutcome.NOT_EXECUTED and "heading not reached" in result.detail
    assert time.monotonic() - started < 10.0
    assert ("yaw_to", 90.0) in vehicle.commands
    # The commanded heading did not change: "forward" is still north.
    assert command_result(drone.move("forward", 500), "move").ok
    assert vehicle.north == pytest.approx(5.0, abs=0.6) and vehicle.east == pytest.approx(0.0, abs=0.6)


def test_the_ardupilot_link_without_yaw_to_keeps_turning_only_the_plan(make_mav):
    """No yaw command (the MAVSDK link today): rotate only turns the plan, as before."""
    drone, vehicle = make_mav(yawing=False, frame_heading_deg=0.0)
    airborne(drone, vehicle)
    assert command_result(drone.rotate(90), "rotate").ok
    assert not [c for c in vehicle.commands if c[0] == "yaw_to"]
    # The executor's gate catches it: the measured heading is still 0.
    assert angle(drone.measured_heading_deg(), 0.0) <= 1.0


def test_measured_heading_is_none_without_a_frame_or_with_stale_telemetry(make_mav):
    drone, vehicle = make_mav(frame_heading_deg=0.0, stale_after_s=0.5)
    assert wait_for(lambda: drone.link_status().ok), drone.link_status()
    assert drone.measured_heading_deg() is None  # no plan frame before the first takeoff
    airborne(drone, vehicle)
    assert drone.measured_heading_deg() is not None
    vehicle.cut_link()
    try:
        assert wait_for(lambda: drone.measured_heading_deg() is None)
    finally:
        vehicle.restore_link()


def preflight_detail(drone, needle):
    """The preflight detail once it mentions *needle* (the parameters are read in the background)."""
    status = wait_for(lambda: needle in (drone.link_status().detail or "")) and drone.link_status()
    assert status and status.reason == "preflight", drone.link_status()
    return status.detail


def test_an_ardupilot_that_cannot_yaw_is_not_told_to_use_a_camera(make_mav):
    """B1 follow-up: a camera source would cut every lane this ArduCopter does not already face."""
    drone, _ = make_mav(yawing=False)
    drone.set_obstacle_sensing(SENSING_NONE)
    detail = preflight_detail(drone, "no obstacle avoidance")
    assert NO_AVOIDANCE_CANNOT_YAW_REMEDY in detail and "camera source" not in detail
    assert "OA_TYPE" in detail and "FLIGHTRISK_OBSTACLE_SENSING=off" in detail
    result = drone.takeoff()
    assert result.outcome is CommandOutcome.REJECTED and NO_AVOIDANCE_CANNOT_YAW_REMEDY in result.detail


@pytest.mark.parametrize("autopilot,yawing", [(ARDUPILOT, True), (PX4, False)])
def test_a_drone_that_can_face_its_moves_keeps_the_camera_option(make_mav, autopilot, yawing):
    drone, _ = make_mav(SimVehicle(autopilot, time_scale=20.0), yawing=yawing)
    drone.set_obstacle_sensing(SENSING_NONE)
    detail = preflight_detail(drone, "no obstacle avoidance")
    assert "(configure the autopilot's avoidance, use a camera source for stepped sensing, " \
           "or set FLIGHTRISK_OBSTACLE_SENSING=off)" in detail
    assert NO_AVOIDANCE_CANNOT_YAW_REMEDY not in detail


def cannot_yaw_events(log):
    return [fields for _, event, fields in log.events if event == EVENT_STEPPED_CANNOT_YAW]


def test_stepped_sensing_on_an_ardupilot_that_cannot_yaw_warns_once_and_still_flies(make_mav):
    log = QuietLogger()
    drone, vehicle = make_mav(yawing=False, event_log=log)
    assert wait_for(lambda: drone.link_status().ok), drone.link_status()
    drone.set_obstacle_sensing(SENSING_STEPPED)
    warnings = cannot_yaw_events(log)
    assert len(warnings) == 1
    assert warnings[0]["detail"] == STEPPED_CANNOT_YAW_DETAIL and warnings[0]["autopilot"] == ARDUPILOT
    assert [level for level, event, _ in log.events if event == EVENT_STEPPED_CANNOT_YAW] == ["warning"]
    drone.refresh_params()  # later parameter reads do not repeat it
    assert len(cannot_yaw_events(log)) == 1
    # Not refused: the edge flies, and the gate cuts the lanes it does not face.
    assert drone.link_status().ok, drone.link_status()
    airborne(drone, vehicle)


def test_the_cannot_yaw_warning_waits_for_the_autopilot_type(make_mav):
    log = QuietLogger()
    drone, _ = make_mav(yawing=False, connect=False, event_log=log)
    drone.set_obstacle_sensing(SENSING_STEPPED)  # before connect: the autopilot type is not known yet
    assert cannot_yaw_events(log) == []
    assert drone.connect() is True
    assert wait_for(lambda: cannot_yaw_events(log)), log.names()
    assert len(cannot_yaw_events(log)) == 1


@pytest.mark.parametrize("autopilot,yawing,mode", [
    (ARDUPILOT, True, SENSING_STEPPED),
    (PX4, False, SENSING_STEPPED),
    (ARDUPILOT, False, "forward_only"),
    (ARDUPILOT, False, "off"),
])
def test_no_cannot_yaw_warning_when_the_drone_can_face_its_moves_or_never_turns(make_mav, autopilot, yawing, mode):
    log = QuietLogger()
    drone, _ = make_mav(SimVehicle(autopilot, time_scale=20.0), yawing=yawing, event_log=log)
    assert wait_for(lambda: drone.link_status().ok), drone.link_status()
    drone.set_obstacle_sensing(mode)
    drone.refresh_params()
    assert cannot_yaw_events(log) == []


class FacingRecorder(FakeObstacleSensor):
    """The scripted sensor, recording the commanded and the measured heading at every look."""

    def __init__(self, pose_provider, drone):
        super().__init__(pose_provider)
        self.drone = drone
        self.seen: list[tuple[float, float | None]] = []

    def sense(self, after, timeout_s=2.0):
        result = super().sense(after, timeout_s)
        self.seen.append((self.calls[-1].heading_deg, self.drone.measured_heading_deg()))
        return result


class MavEdge:
    """A MavlinkFleetDrone with a yaw-capable link under a sensed LaneExecutor (field steps of 500 cm)."""

    def __init__(self, monkeypatch, vehicle, *, yawing=True, turns=True, **options):
        self.vehicle = vehicle
        install(monkeypatch, vehicle)
        address = "tcpout://127.0.0.1:1"
        link = YawingLink(address, vehicle, turns=turns) if yawing else MavsdkAutopilotLink(address)
        self.drone = MavlinkFleetDrone("drone-1", link, MavlinkFleetOptions(frame_heading_deg=0.0, **options))
        self.envelope = SafetyEnvelope.for_field(geofence_cm=3000, link_loss_land_s=30.0, link_loss_hold_s=0.0)
        self.drone.bind_envelope(self.envelope)
        self.sensor = FacingRecorder(self.envelope.pose_snapshot, self.drone)
        self.sent: list[dict] = []
        self.log = QuietLogger()
        self.executor = LaneExecutor(self.drone, self.envelope, "drone-1", send=self.sent.append, logger=self.log,
                                     telemetry_interval_s=0.1, obstacle_sensor=self.sensor, obstacle_step_cm=500,
                                     obstacle_retry_pause_s=0.02)
        assert self.drone.connect()
        self.drone.set_obstacle_sensing(SENSING_STEPPED)
        self.executor.start()

    def handle(self, msg):
        self.executor.note_ground_message()
        self.executor.handle_message(msg, recv_t=self.executor.now())

    def fly(self, lanes):
        assert wait_for(lambda: self.drone.link_status().ok), self.drone.link_status()
        self.handle({"type": "assign_lanes", "drone_id": "drone-1", "alt_cm": 1500, "lanes": lanes})
        self.handle({"type": "clock", "offset_s": 0.0})
        self.handle({"type": "start_at", "t_ground_s": self.executor.now() + 0.05})
        assert wait_for(lambda: self.executor.lane_index == len(lanes) and not self.executor.is_busy, timeout=30.0), (
            self.log.names())

    def close(self):
        self.executor.shutdown(timeout=5.0)
        self.drone.disconnect()


@pytest.fixture
def make_mav_edge(monkeypatch):
    edges = []

    def _make(vehicle, **kwargs):
        edge = MavEdge(monkeypatch, vehicle, **kwargs)
        edges.append(edge)
        return edge

    yield _make
    for edge in edges:
        edge.close()


def test_every_mavlink_gate_looks_the_way_the_step_goes(make_mav_edge):
    """B1 end to end: placed 40 degrees off the frame, the ArduCopter faces lane 0 and every later lane."""
    edge = make_mav_edge(SimVehicle(ARDUPILOT, time_scale=20.0, heading_deg=40.0))
    edge.fly([
        {"direction": "forward", "distance_cm": 1000},
        {"direction": "right", "distance_cm": 500},
        {"direction": "back", "distance_cm": 1000},
    ])
    assert edge.executor.obstacle_cuts == []
    assert len(edge.sensor.seen) == 5
    for commanded, measured in edge.sensor.seen:
        assert measured is not None and angle(measured, commanded) <= HEADING_TOLERANCE_DEG, edge.sensor.seen
    assert [c for c, _ in edge.sensor.seen] == [0.0, 0.0, 90.0, 180.0, 180.0]
    assert edge.vehicle.north == pytest.approx(0.0, abs=1.0) and edge.vehicle.east == pytest.approx(5.0, abs=1.0)
    assert ("yaw_to", 0.0) in edge.vehicle.commands  # lane 0: turned to the frame heading first


def test_the_review_scenario_an_ardupilot_never_steps_blind_after_a_turn(make_mav_edge):
    """B1 as reviewed: the MAVSDK link cannot turn an ArduCopter, so the right lane is cut, never flown blind."""
    edge = make_mav_edge(SimVehicle(ARDUPILOT, time_scale=20.0), yawing=False)
    edge.fly([
        {"direction": "forward", "distance_cm": 500},
        {"direction": "right", "distance_cm": 500},
    ])
    assert edge.executor.obstacle_cuts == [Cut(1, 0, CUT_SENSOR_STALE)]
    assert len(edge.sensor.calls) == 1  # lane 0 only: the forward-looking camera never judged lane 1
    assert edge.vehicle.east == pytest.approx(0.0, abs=1.0) and edge.vehicle.north == pytest.approx(5.0, abs=1.0)
    stale = [fields for _, event, fields in edge.log.events if event == EVENT_SENSE_STALE]
    assert stale and all(e["cause"] == HEADING_NOT_REACHED for e in stale)


def test_a_mavlink_turn_that_never_finishes_cuts_the_lane_and_moves_nothing_that_way(make_mav_edge):
    edge = make_mav_edge(SimVehicle(ARDUPILOT, time_scale=20.0), turns=False, yaw_rate_dps=1000.0,
                         move_timeout_margin_s=0.5)
    edge.fly([
        {"direction": "forward", "distance_cm": 500},
        {"direction": "right", "distance_cm": 500},
        {"direction": "back", "distance_cm": 500},
    ])
    assert edge.executor.obstacle_cuts == [Cut(1, 0, CUT_SENSOR_STALE), Cut(2, 0, CUT_SENSOR_STALE)]
    assert edge.vehicle.north == pytest.approx(5.0, abs=1.0) and edge.vehicle.east == pytest.approx(0.0, abs=1.0)
    assert len(edge.sensor.calls) == 1  # lane 0's only step
    assert edge.drone.state.is_flying
    assert "edge_executor_error" not in edge.log.names()


# ---------------------------------------------------------------------------
# N5: the sensor's down timer after a landing and a new plan
# ---------------------------------------------------------------------------


class FlakySensor(TimelineSensor):
    """A sensor that cannot look for its first *down_calls* calls; each call takes *call_s* on the fake clock."""

    def __init__(self, timeline, pose_provider, clock, call_s, down_calls):
        super().__init__(timeline, pose_provider)
        self.clock = clock
        self.call_s = call_s
        self.down_calls = down_calls

    def sense(self, after, timeout_s=2.0):
        self.clock.advance(self.call_s)
        if len(self.calls) < self.down_calls:
            self.queue("stale")
        return super().sense(after, timeout_s)


def flaky(down_calls, call_s=4.0):
    return lambda timeline, envelope, clock: FlakySensor(timeline, envelope.pose_snapshot, clock, call_s, down_calls)


def probes(rig):
    return events(rig, EVENT_SENSOR_PROBE)


def test_a_landing_probes_a_down_sensor_and_a_working_one_is_ok_again(rigs):
    rig = rigs(sensor=flaky(down_calls=3))
    start(rig, LANES, on_complete="land")
    assert wait_for(lambda: ("land",) in rig.timeline and not rig.executor.is_busy), rig.timeline
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_DOWN)]
    assert wait_for(lambda: probes(rig)), rig.log.names()
    probe = probes(rig)[0]
    assert (probe["why"], probe["state"], probe["recovered"]) == ("landing", "clear", True)
    assert len(rig.sensor.calls) == 4  # three gates and the probe
    status = rig.executor.obstacle_status()
    assert status[TELEMETRY_SENSOR_OK] is True
    assert status[TELEMETRY_CUT_REASONS] == "sensor_down"  # the cut is still reported


def test_a_sensor_still_down_stays_down_until_a_new_plan_finds_it_working(rigs):
    rig = rigs(sensor=flaky(down_calls=4))  # three gates and the landing's probe fail
    start(rig, LANES, on_complete="land")
    assert wait_for(lambda: probes(rig) and not rig.executor.is_busy), rig.log.names()
    assert probes(rig)[0]["recovered"] is False
    assert rig.executor.obstacle_status()[TELEMETRY_SENSOR_OK] is False
    assert not rig.drone.state.is_flying
    # A new plan on the ground probes again; the sensor works now.
    send(rig.executor, assign(lanes=LANES, plan_id="p-2", plan_seq=2, cmd_id="a-2"))
    assert reply_for(rig.sent, "a-2")["type"] == "ack"
    assert wait_for(lambda: len(probes(rig)) == 2), rig.log.names()
    assert (probes(rig)[1]["why"], probes(rig)[1]["recovered"]) == ("new_plan", True)
    assert rig.executor.obstacle_status()[TELEMETRY_SENSOR_OK] is True


def test_a_working_sensor_is_never_probed(rigs):
    rig = rigs()
    start(rig, LANES, on_complete="land")
    assert wait_for(lambda: ("land",) in rig.timeline and not rig.executor.is_busy), rig.timeline
    send(rig.executor, assign(lanes=LANES, plan_id="p-2", plan_seq=2, cmd_id="a-2"))
    assert probes(rig) == []
    assert len(rig.sensor.calls) == len(events(rig, EVENT_OBSTACLE_GATE)) == 10


def test_the_probe_waits_for_a_sense_call_in_flight(rigs):
    rig = rigs()
    executor = rig.executor
    with executor._lock:
        executor._sensor_down_since = rig.clock()
        executor._sense_inflight = 1  # a gate's call, or an abandoned hung one
        executor._request_sensor_probe_locked("landing")
        assert executor._probe_wanted == "landing"
    assert rig.sensor.calls == [] and probes(rig) == []
    executor._sense_call_ended()
    assert wait_for(lambda: probes(rig)), rig.log.names()
    assert probes(rig)[0]["recovered"] is True
    assert executor.obstacle_status()[TELEMETRY_SENSOR_OK] is True


# ---------------------------------------------------------------------------
# R2-N1: a new plan's own sensor-down time, and its first gate and the probe
# ---------------------------------------------------------------------------


def after_assign(rig) -> list:
    """The timeline after the ``("mark", "assign")`` entry a test wrote before sending a new plan."""
    timeline = rig.timeline
    return timeline[len(timeline) - timeline[::-1].index(("mark", "assign")):]


def test_a_new_plan_gets_its_own_sensor_down_time_and_a_down_sensor_stays_reported_down(rigs):
    """R2-N1: failures before a new plan never end it; the sensor itself stays reported down."""
    rig = rigs(sensor=flaky(down_calls=1000, call_s=3.0))  # never looks; each call takes 3 s
    start(rig, lanes_of(("forward", 100), ("right", 100)))  # on_complete none: the drone hovers
    assert wait_for(lambda: finished(rig)), rig.timeline
    # Three attempts (9 s) cut lane 0 as stale; at 12 s the sensor is down and lane 1 ends.
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE), Cut(1, 0, CUT_SENSOR_DOWN)]
    assert rig.executor.obstacle_status()[TELEMETRY_SENSOR_OK] is False
    gates_before = len(events(rig, EVENT_OBSTACLE_GATE))
    rig.timeline.append(("mark", "assign"))
    send(rig.executor, assign(lanes=lanes_of(("right", 100), ("forward", 100)), plan_id="p-2", plan_seq=2,
                              cmd_id="a-2"))
    assert reply_for(rig.sent, "a-2")["type"] == "ack"
    assert wait_for(lambda: finished(rig) and probes(rig)), rig.timeline
    probe = probes(rig)[0]
    assert (probe["why"], probe["recovered"]) == ("new_plan", False)
    # The new plan gets its own 10 s: its first lane is retried and cut as
    # stale, not ended at once with sensor_down for the old plan's failures.
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE), Cut(1, 0, CUT_SENSOR_DOWN)]
    attempts = [g["attempt"] for g in events(rig, EVENT_OBSTACLE_GATE)[gates_before:]]
    assert attempts == [1, 2, 3, 1]
    # A sensor that is really down is still reported down.
    assert rig.executor.obstacle_status()[TELEMETRY_SENSOR_OK] is False
    assert not any(e[0] == "move" and e[1] in ("forward", "back", "left", "right") for e in after_assign(rig))


class OneWorkerSensor(TimelineSensor):
    """Like the camera sensor: one inference worker, and a call made while it is busy times out at once.

    The first *down_calls* calls cannot look and take 4 s on the fake clock.
    While ``hold_next`` is set the next call keeps the worker until
    ``release`` is set.
    """

    def __init__(self, timeline, pose_provider, clock, down_calls):
        super().__init__(timeline, pose_provider)
        self.clock = clock
        self.down_calls = down_calls
        self.busy = threading.Lock()
        self.hold_next = threading.Event()
        self.holding = threading.Event()
        self.release = threading.Event()
        self.n = 0

    def sense(self, after, timeout_s=2.0):
        if not self.busy.acquire(blocking=False):
            self.timeline.append(("sense", "busy"))
            return SenseResult.make_unavailable(REASON_TIMEOUT, detail={"worker_busy": True})
        try:
            self.n += 1
            if self.n <= self.down_calls:
                self.clock.advance(4.0)
                self.queue("stale")
            if self.hold_next.is_set():
                self.hold_next.clear()
                self.holding.set()
                self.release.wait(20.0)
            return super().sense(after, timeout_s)
        finally:
            self.busy.release()


def test_a_gate_waits_for_the_new_plan_probe_instead_of_counting_a_busy_worker(rigs):
    """R2-N1, the critic's probe: the probe holds the camera's one worker while the new plan's first gate looks."""
    holder = {}

    def make(timeline, envelope, clock):
        holder["sensor"] = OneWorkerSensor(timeline, envelope.pose_snapshot, clock, down_calls=3)
        return holder["sensor"]

    # A generous sense timeout, so the gate's wait for the probe never runs out under load.
    rig = rigs(sensor=make, geofence_cm=700, obstacle_sense_timeout_s=30.0)
    sensor = holder["sensor"]
    start(rig, lanes_of(("forward", 100),))  # on_complete none: the drone hovers
    assert wait_for(lambda: finished(rig)), rig.timeline
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_DOWN)]
    assert rig.executor.obstacle_status()[TELEMETRY_SENSOR_OK] is False
    # The sensor works again. A new plan arrives while the drone hovers, and
    # its probe holds the worker while the plan turns to its first lane.
    sensor.hold_next.set()
    rig.timeline.append(("mark", "assign"))
    send(rig.executor, assign(lanes=lanes_of(("right", 100), ("forward", 100)), plan_id="p-2", plan_seq=2,
                              cmd_id="a-2"))
    assert reply_for(rig.sent, "a-2")["type"] == "ack"
    assert sensor.holding.wait(10.0)
    assert wait_for(lambda: ("rotate", 90) in after_assign(rig)), rig.timeline
    time.sleep(0.1)  # the first gate is now waiting for the probe (or about to)
    sensor.release.set()
    assert wait_for(lambda: finished(rig) and not rig.executor.is_busy and probes(rig)), rig.timeline
    assert ("sense", "busy") not in rig.timeline
    probe = probes(rig)[0]
    assert (probe["why"], probe["state"], probe["recovered"]) == ("new_plan", "clear", True)
    assert rig.executor.obstacle_cuts == []
    assert rig.executor.obstacle_status()[TELEMETRY_SENSOR_OK] is True
    # The probe's view clears nothing: every step of the new plan has a gate of its own.
    clear = ("sense", "clear")
    assert after_assign(rig) == [
        ("rotate", 90), clear, clear, ("move", "forward", 50), clear, ("move", "forward", 50),
        ("rotate", -90), clear, ("move", "forward", 50), clear, ("move", "forward", 50),
    ]
    assert_invariants(rig)


def test_a_gate_whose_probe_never_ends_cannot_look(rigs):
    """A probe whose call hangs past the gate's own budget: the gate is a timeout and calls nothing."""
    rig = rigs(obstacle_sense_timeout_s=0.05)
    executor = rig.executor
    with executor._lock:
        executor._probe_running = True  # a probe call that never returns
        executor._probe_idle.clear()
    began = time.monotonic()
    result, latency_ms = executor._sense_once(0.0)
    assert time.monotonic() - began >= 0.5  # the timeout plus the join grace
    assert (result.state, result.reason, result.detail) == ("unavailable", REASON_TIMEOUT, {"probe_in_flight": True})
    assert latency_ms >= 500.0
    assert rig.sensor.calls == [] and executor._sense_inflight == 0
    with executor._lock:
        executor._probe_running = False
        executor._probe_idle.set()
    result, _ = executor._sense_once(0.0)
    assert result.clear and len(rig.sensor.calls) == 1


# ---------------------------------------------------------------------------
# N2: the way back of resume_search is sensed
# ---------------------------------------------------------------------------


def take_over_at_gate(rig, gate: int, *moves) -> None:
    """Take manual control while gate number *gate* looks, then fly the manual *moves* (unsensed)."""

    def hook(n):
        if n == gate:
            send(rig.executor, fp.make_manual_hold(cmd_id="m-hold", fence=FENCE))

    rig.sensor.on_call = hook
    start(rig, LANES)
    assert answered(rig, "m-hold")["type"] == "ack"
    assert wait_for(lambda: not rig.executor.is_busy)
    rig.sensor.on_call = None
    mark_unsensed(rig)
    for k, (direction, amount) in enumerate(moves):
        send(rig.executor, manual(f"m-{k}", direction, amount))
        assert answered(rig, f"m-{k}")["type"] == "ack"
        assert wait_for(lambda: not rig.executor.is_busy)


def after_mark(rig) -> list:
    timeline = rig.timeline
    return timeline[len(timeline) - timeline[::-1].index(("mark", "unsensed")):]


def test_the_way_back_is_flown_nose_first_in_gated_steps(rigs):
    rig = rigs()
    # Held while lane 0's third gate looks (at y = 100), then 100 cm right by hand.
    take_over_at_gate(rig, 3, ("right", 100))
    hold_point = rig.executor.manual_state.hold_pose
    assert (hold_point.x_cm, hold_point.y_cm, hold_point.heading_deg) == (0.0, 100.0, 0.0)
    send(rig.executor, fp.make_resume_search(cmd_id="m-resume", fence=FENCE))
    assert answered(rig, "m-resume")["detail"] == "returning"
    assert wait_for(lambda: finished(rig)), rig.timeline
    clear = ("sense", "clear")
    # Way back: face left (270) and fly 100 cm in two gated steps; lane 0
    # then turns back to 0 for its last two steps.
    assert after_mark(rig)[:11] == [
        ("move", "right", 100),
        ("rotate", -90), clear, ("move", "forward", 50), clear, ("move", "forward", 50),
        ("rotate", 90), clear, ("move", "forward", 50), clear, ("move", "forward", 50),
    ]
    assert rig.executor.obstacle_cuts == []
    pose = assert_invariants(rig)
    assert (round(pose.x_cm, 6), round(pose.y_cm, 6)) == (100.0, 0.0)


# ---------------------------------------------------------------------------
# R2-B2: a way back that stops short of the hold point keeps the hold point
# ---------------------------------------------------------------------------


def resume_into_a_blocked_way_back(rig, cmd_id: str = "m-resume") -> None:
    """Send ``resume_search`` and wait until a gate on the way back has stopped it."""
    send(rig.executor, fp.make_resume_search(cmd_id=cmd_id, fence=FENCE))
    assert answered(rig, cmd_id)["type"] == "ack"
    assert wait_for(lambda: events(rig, "edge_resume_search_blocked") and not rig.executor.is_busy), rig.timeline


def assert_under_manual_control_at_hold_point(rig, hold_step: int) -> None:
    """R2-B2: back under manual control, hovering, with the hold point (0, 100) and the plan progress kept."""
    executor = rig.executor
    assert executor.control_mode == "manual"
    state = executor.manual_state
    assert at(state.hold_pose) == (0.0, 100.0, 0.0)
    assert state.hold_step == hold_step and not state.capture_pending
    assert executor._resume_target is None  # moved into the hold point, as for an interrupted return
    assert rig.drone.state.is_flying and ("land",) not in rig.timeline


def lane_pairs(lanes) -> list[tuple[str, int]]:
    return [(lane["direction"], lane["distance_cm"]) for lane in lanes]


def test_a_blocked_way_back_keeps_the_hold_point_under_manual_control(rigs):
    rig = rigs(blocked=[BlockedZone(heading_deg=270.0)])  # anything seen facing left
    take_over_at_gate(rig, 3, ("right", 50))
    hold_step = rig.executor.manual_state.hold_step
    resume_into_a_blocked_way_back(rig)
    assert rig.timeline[-2:] == [("rotate", -90), ("sense", "blocked")]
    # Nothing is cut: the drone is not where the search continues, so no cut
    # could record where it stopped. The plan's progress is unchanged.
    assert rig.executor.obstacle_cuts == []
    assert rig.executor.obstacle_segments() == []
    blocked = events(rig, "edge_resume_search_blocked")[0]
    assert (blocked["reason"], blocked["sensor_reason"], blocked["lane_index"]) == (CUT_OBSTACLE, None, 0)
    assert (blocked["target_x_cm"], blocked["target_y_cm"], blocked["control_mode"]) == (0.0, 100.0, "manual")
    assert_under_manual_control_at_hold_point(rig, hold_step)
    assert not events(rig, "edge_resume_search_returned")
    pose = assert_invariants(rig)
    assert (pose.x_cm, pose.y_cm, pose.heading_deg) == (50.0, 100.0, 270.0)


def test_a_blocked_way_back_on_the_last_lane_runs_on_complete_only_once_the_lane_is_flown(rigs):
    rig = rigs(blocked=[BlockedZone(heading_deg=270.0)])

    def hook(n):
        if n == 3:
            send(rig.executor, fp.make_manual_hold(cmd_id="m-hold", fence=FENCE))

    rig.sensor.on_call = hook
    start(rig, lanes_of(("forward", 200),), on_complete="land")
    assert answered(rig, "m-hold")["type"] == "ack"
    assert wait_for(lambda: not rig.executor.is_busy)
    rig.sensor.on_call = None
    hold_step = rig.executor.manual_state.hold_step
    mark_unsensed(rig)
    send(rig.executor, manual("m-0", "right", 50))
    assert answered(rig, "m-0")["type"] == "ack"
    assert wait_for(lambda: not rig.executor.is_busy)
    resume_into_a_blocked_way_back(rig)
    # The lane is not flown yet, so nothing is cut and nothing lands.
    assert rig.executor.obstacle_cuts == []
    assert not events(rig, "edge_land")
    assert_under_manual_control_at_hold_point(rig, hold_step)
    # The way is clear again: resume_search flies back, finishes the lane,
    # and only then does the plan's on_complete land the drone.
    rig.sensor.blocked = []
    send(rig.executor, fp.make_resume_search(cmd_id="m-resume-2", fence=FENCE))
    assert answered(rig, "m-resume-2")["detail"] == "returning"
    assert wait_for(lambda: ("land",) in rig.timeline and not rig.executor.is_busy), rig.timeline
    assert rig.executor.obstacle_cuts == []
    assert events(rig, "edge_land")[0]["reason"] == "on_complete"
    returned = events(rig, "edge_resume_search_returned")
    assert len(returned) == 1 and (returned[0]["x_cm"], returned[0]["y_cm"]) == (0.0, 100.0)
    pose = assert_invariants(rig)
    assert (round(pose.x_cm, 6), round(pose.y_cm, 6)) == (0.0, 200.0)


def test_a_way_back_whose_sensor_cannot_look_stops_under_manual_control(rigs):
    rig = rigs()
    take_over_at_gate(rig, 3, ("right", 50))
    hold_step = rig.executor.manual_state.hold_step
    rig.sensor.queue(*["stale"] * OBSTACLE_SENSE_RETRIES)
    resume_into_a_blocked_way_back(rig)
    assert rig.executor.obstacle_cuts == []
    blocked = events(rig, "edge_resume_search_blocked")[0]
    assert (blocked["reason"], blocked["sensor_reason"]) == (CUT_SENSOR_STALE, "stale")
    gates = [g for g in events(rig, EVENT_OBSTACLE_GATE) if g.get("source") == "return"]
    assert [g["state"] for g in gates] == ["unavailable"] * OBSTACLE_SENSE_RETRIES
    assert_under_manual_control_at_hold_point(rig, hold_step)
    assert_invariants(rig)


@pytest.mark.parametrize("then", ["hold", "nudge", "steer"])
def test_after_a_blocked_way_back_the_lanes_are_flown_only_from_the_hold_point(rigs, then):
    """R2-B2, the critic's probe: hold at (0, 100), 100 cm right by hand, the way back blocked, then resume again.

    A second ``manual_hold`` keeps the hold point, a ``nudge`` and a v2
    ``resume`` are refused under manual control, and a manual step around
    the obstacle is allowed. Whatever the operator did, the next
    ``resume_search`` flies back to (0, 100) first, so the drone ends where
    the edge and ground replays say: (100, 0).
    """
    rig = rigs(blocked=[BlockedZone(heading_deg=270.0)])
    take_over_at_gate(rig, 3, ("right", 100))
    hold_step = rig.executor.manual_state.hold_step
    resume_into_a_blocked_way_back(rig, "r-1")
    assert at(rig.executor.envelope.pose_snapshot()) == (100.0, 100.0, 270.0)
    assert_under_manual_control_at_hold_point(rig, hold_step)
    rig.sensor.blocked = []
    if then == "hold":
        send(rig.executor, fp.make_manual_hold(cmd_id="h-2", fence=FENCE))
        assert answered(rig, "h-2")["detail"] == "already_manual"
    elif then == "nudge":
        send(rig.executor, nudge("back", 20, cmd_id="n-1"))
        assert (answered(rig, "n-1")["type"], answered(rig, "n-1")["reason"]) == ("nack", "busy")
        send(rig.executor, resume(cmd_id="v2-resume"))
        refused = answered(rig, "v2-resume")
        assert (refused["type"], refused["reason"], refused["detail"]) == ("nack", "busy", "manual_control")
    else:
        mark_unsensed(rig)
        send(rig.executor, manual("m-steer", "back", 30))  # facing left: 30 cm towards +x
        assert answered(rig, "m-steer")["type"] == "ack"
        assert wait_for(lambda: not rig.executor.is_busy)
        assert at(rig.executor.envelope.pose_snapshot()) == (130.0, 100.0, 270.0)
    assert not rig.executor.is_busy
    assert_under_manual_control_at_hold_point(rig, hold_step)
    send(rig.executor, fp.make_resume_search(cmd_id="r-2", fence=FENCE))
    reply = answered(rig, "r-2")
    assert (reply["detail"], reply["target_x_cm"], reply["target_y_cm"]) == ("returning", 0.0, 100.0)
    assert wait_for(lambda: finished(rig)), rig.timeline
    returned = events(rig, "edge_resume_search_returned")
    assert len(returned) == 1 and (returned[0]["x_cm"], returned[0]["y_cm"]) == (0.0, 100.0)
    assert rig.executor.obstacle_cuts == []
    replay = replay_lanes(lane_pairs(LANES), rig.executor.obstacle_cuts)
    pose = assert_invariants(rig)
    assert (round(pose.x_cm), round(pose.y_cm)) == tuple(replay.position) == (100, 0)


def stop_way_back_at_its_first_gate(rig, how: str) -> int:
    """Hold at (0, 100), fly 100 cm right, resume, and stop the way back with a v2 hold or a link-loss hold.

    Returns the hold point's plan step.
    """
    take_over_at_gate(rig, 3, ("right", 100))
    hold_step = rig.executor.manual_state.hold_step
    first = len(rig.sensor.calls) + 1

    def hook(n):
        if n != first:
            return
        if how == "hold":
            send(rig.executor, hold(cmd_id="h-1"))
        else:
            with rig.executor._lock:
                rig.executor._link_holding = True  # what the monitor sets when the ground goes silent

    rig.sensor.on_call = hook
    send(rig.executor, fp.make_resume_search(cmd_id="r-1", fence=FENCE))
    assert answered(rig, "r-1")["type"] == "ack"
    assert wait_for(lambda: len(rig.sensor.calls) >= first and not rig.executor.is_busy), rig.timeline
    rig.sensor.on_call = None
    # Stopped before its first step: turned to face left, not moved.
    assert at(rig.executor.envelope.pose_snapshot()) == (100.0, 100.0, 270.0)
    assert rig.executor.control_mode == "autonomous"
    return hold_step


def continue_stopped_way_back(rig, how: str) -> None:
    if how == "hold":
        send(rig.executor, resume(cmd_id="v2-resume"))
        assert answered(rig, "v2-resume")["type"] == "ack"
    else:
        rig.executor._end_link_hold()


@pytest.mark.parametrize("how", ["hold", "link"])
def test_a_way_back_stopped_by_a_hold_is_flown_again_before_the_plan(rigs, how):
    """A v2 hold or a link-loss hold stops the way back; resume (or the link) flies the rest of it first."""
    rig = rigs()
    stop_way_back_at_its_first_gate(rig, how)
    continue_stopped_way_back(rig, how)
    assert wait_for(lambda: finished(rig)), rig.timeline
    returned = events(rig, "edge_resume_search_returned")
    assert len(returned) == 1 and (returned[0]["x_cm"], returned[0]["y_cm"]) == (0.0, 100.0)
    assert rig.executor.obstacle_cuts == []
    replay = replay_lanes(lane_pairs(LANES), rig.executor.obstacle_cuts)
    pose = assert_invariants(rig)
    assert (round(pose.x_cm), round(pose.y_cm)) == tuple(replay.position) == (100, 0)


def test_a_stopped_way_back_with_no_way_back_left_waits_under_manual_control(rigs):
    rig = rigs()
    hold_step = stop_way_back_at_its_first_gate(rig, "hold")
    rig.executor._return_path_locked = lambda target: None  # no way back inside the geofence
    moves_before = len(lane_moves(rig))
    continue_stopped_way_back(rig, "hold")
    assert wait_for(lambda: events(rig, "edge_resume_search_blocked") and not rig.executor.is_busy), rig.timeline
    blocked = events(rig, "edge_resume_search_blocked")[0]
    assert (blocked["reason"], blocked["target_x_cm"], blocked["target_y_cm"]) == ("geofence", 0.0, 100.0)
    assert len(lane_moves(rig)) == moves_before
    assert_under_manual_control_at_hold_point(rig, hold_step)
    # A way back exists again: resume_search flies it and then the plan.
    del rig.executor._return_path_locked
    send(rig.executor, fp.make_resume_search(cmd_id="r-2", fence=FENCE))
    assert answered(rig, "r-2")["detail"] == "returning"
    assert wait_for(lambda: finished(rig)), rig.timeline
    pose = assert_invariants(rig)
    assert (round(pose.x_cm), round(pose.y_cm)) == (100, 0)


def test_forward_only_way_back_gates_its_forward_steps_only(rigs):
    rig = rigs(obstacle_turn=False)
    take_over_at_gate(rig, 3, ("back", 50), ("right", 50))
    send(rig.executor, fp.make_resume_search(cmd_id="m-resume", fence=FENCE))
    assert answered(rig, "m-resume")["type"] == "ack"
    assert wait_for(lambda: finished(rig)), rig.timeline
    assert rotations(rig) == []
    # Way back: forward 50 after a clear gate, left 50 unsensed; then lane 0 goes on.
    assert after_mark(rig)[:6] == [
        ("move", "back", 50), ("move", "right", 50),
        ("sense", "clear"), ("move", "forward", 50), ("move", "left", 50), ("sense", "clear"),
    ]
    assert [g.get("source") for g in events(rig, EVENT_OBSTACLE_GATE)].count("return") == 1
    pose = assert_invariants(rig, unsensed_directions=("left", "right", "back"))
    assert math.isclose(pose.y_cm, 0.0, abs_tol=1e-6)
