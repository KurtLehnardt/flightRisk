"""Safety rules of the re-look and the sidestep (``lane_avoid``) found by the first review of the feature.

Every executor is the real ``LaneExecutor`` on the fake Tello and the scripted
sensor of ``tests/test_lane_executor_obstacle.py``, as in
``tests/test_lane_executor_avoid.py``. The scenarios cover:

* a person or a sensor down seen anywhere during a sidestep cuts the lane with
  that reason at the point on the lane level with the drone, after which the
  drone flies back to that point and goes on with the next lane;
* a sidestep moves only on views with a fresh ``clear`` person cue, and the
  gates of the re-look and the sidestep ask the sensor to wait for one;
* a point the plan was to continue from never outlives a landing, a takeoff
  or a new plan on the ground;
* the sidestep's area is cut down to the plan's zone, it is never flown on the
  field envelope, and a time budget and a per-lane re-look limit bound it;
* the counts and offsets the ground gets, and interruptions of every kind.
"""
# The tests take the ``rigs`` fixture imported from the obstacle tests as an argument.
# ruff: noqa: F811
from __future__ import annotations

import math

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone import fleet_patterns
from flightrisk.drone.fleet_plan_check import SQUARE_FOOTPRINT
from flightrisk.drone.lane_avoid import (
    SOURCE_RELOOK,
    SOURCE_SIDESTEP,
    SOURCE_SIDESTEP_RETURN,
    PlanArea,
)
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.lane_steps import Cut
from flightrisk.drone.obstacle_sensor import (
    CUT_OBSTACLE,
    CUT_PERSON,
    CUT_SENSOR_DOWN,
    EVENT_OBSTACLE_GATE,
    EVENT_OBSTACLE_RELOOK,
    EVENT_OBSTACLE_SIDESTEP,
    OBSTACLE_RELOOKS_PER_LANE,
    PERSON_CUE_CLEAR,
    PERSON_CUE_UNKNOWN,
    TELEMETRY_AVOID,
    TELEMETRY_CUT_REASONS,
    BlockedZone,
    SenseResult,
)
from flightrisk.drone.safety import FIELD_LIMITS, SafetyEnvelope
from flightrisk.edge_device import ObstacleWiring
from tests.lane_executor_support import (
    FENCE,
    LANES,
    hold,
    reply_for,
    resume,
    send,
    wait_for,
)
from tests.test_lane_executor_avoid import (
    AVOID,
    POLE,
    SIDESTEP_ONLY,
    assert_back_on_the_plan,
    assert_never_landed,
    avoid,
    counts,
    phases,
)
from tests.test_lane_executor_obstacle import (  # noqa: F401 - ``rigs`` is a fixture
    DEMO,
    TimelineSensor,
    answered,
    assert_invariants,
    at,
    events,
    finished,
    fly,
    lane_moves,
    lanes_of,
    rigs,
    start,
)

#: A person standing 50 cm right of lane 0, seen facing forward from the line beside the lane at y 250.
PERSON_BESIDE = BlockedZone(heading_deg=0.0, x_min=40, x_max=60, y_min=250, y_max=260, cue="person")
#: A shorter pole on lane 0: blocked facing forward from y 150 to 240.
SHORT_POLE = BlockedZone(heading_deg=0.0, x_min=-10, x_max=10, y_min=150, y_max=240)


def flown_points(rig) -> list[tuple[float, float]]:
    """Every point a forward move of this flight ended on, in the plan frame."""
    x = y = 0.0
    heading = 0.0
    points = []
    for call in rig.drone.calls:
        if call[0] == "takeoff":
            x = y = heading = 0.0
            points = []
        elif call[0] == "rotate":
            heading = (heading + call[1]) % 360
        elif call[0] == "move" and call[1] == "forward":
            rad = math.radians(heading)
            x, y = round(x + math.sin(rad) * call[2], 6), round(y + math.cos(rad) * call[2], 6)
            points.append((x, y))
    return points


def sidestep_trail(rig) -> list[tuple]:
    return [(e["phase"], e.get("side"), e.get("passed_cm"), e.get("reason"))
            for e in events(rig, EVENT_OBSTACLE_SIDESTEP)]


class ScriptedSensor(TimelineSensor):
    """A timeline sensor whose answer a test may replace by pose, and which records ``person_wait``.

    ``rule(pose_tuple, call_number)`` returns a ``SenseResult`` to answer with,
    or None for the zones. ``advance_s`` moves the fake clock by that much on
    every call (a slow sensor), so time budgets can be reached.
    """

    def __init__(self, timeline, envelope, clock, blocked=(), rule=None, advance_s=0.0, status=None):
        super().__init__(timeline, envelope.pose_snapshot, blocked)
        self.envelope = envelope
        self.clock = clock
        self.rule = rule
        self.advance_s = advance_s
        self.person_waits: list[bool] = []
        self.status_fields = status or {}

    def sense(self, after, timeout_s=2.0, person_wait=False):
        self.person_waits.append(person_wait)
        if self.advance_s:
            self.clock.advance(self.advance_s)
        if self.rule is not None:
            pose = self.envelope.pose_snapshot()
            answer = self.rule((round(pose.x_cm, 3), round(pose.y_cm, 3), round(pose.heading_deg % 360, 3)),
                               len(self.calls) + 1)
            if answer is not None:
                self.queue(answer)
        return super().sense(after, timeout_s)

    def status(self):
        return {**super().status(), **self.status_fields}


def scripted(blocked=(), **kwargs):
    return lambda timeline, envelope, clock: ScriptedSensor(timeline, envelope, clock, blocked, **kwargs)


# ---------------------------------------------------------------------------
# A person or a sensor down seen during a sidestep (review blocker 1)
# ---------------------------------------------------------------------------


def test_a_person_seen_beside_the_lane_after_a_pass_cuts_the_lane_for_a_person(rigs):
    # The review's probe: a pole on lane 0 and a person at (50, 250), seen only from the line beside the lane.
    rig = rigs([SHORT_POLE, PERSON_BESIDE], **SIDESTEP_ONLY)
    fly(rig, DEMO)
    # The lane is cut for a person where the drone stood level with it on the lane, after 100 cm beside it.
    assert rig.executor.obstacle_cuts == [Cut(0, 250, CUT_PERSON)]
    assert rig.executor.obstacle_status()[TELEMETRY_CUT_REASONS] == CUT_PERSON
    assert sidestep_trail(rig) == [
        ("refused", "left", None, "plan_area"), ("start", "right", None, None), ("moved", "right", None, None),
        ("beside", None, 50, None), ("beside", None, 100, None), ("cut", None, None, CUT_PERSON),
        ("returned", "right", 100, CUT_PERSON),
    ]
    points = flown_points(rig)
    # Back to the lane at (0, 250) first, then the next lane; never along lane 0 past the person.
    assert points[:7] == [(0, 50), (0, 100), (0, 150), (50, 150), (50, 200), (50, 250), (0, 250)]
    assert not any(px == 0 and py > 250 for px, py in points)
    stop = events(rig, "edge_obstacle_stop")[0]
    assert (stop["reason"], stop["flown_cm"]) == (CUT_PERSON, 250)
    # The pass still counts, and the stretch beside the lane is reported for a re-flight.
    assert counts(rig) == (0, 0, 1, 1)
    assert avoid(rig)["offsets"] == [0, 150, 100]
    assert_back_on_the_plan(rig)
    assert_invariants(rig)
    assert_never_landed(rig)


def test_a_person_seen_on_the_sideways_move_cuts_at_once_with_nothing_moved(rigs):
    person_right = BlockedZone(heading_deg=90.0, x_max=10, y_min=140, y_max=160, cue="person")
    rig = rigs([POLE, person_right], **SIDESTEP_ONLY)
    fly(rig, DEMO)
    assert rig.executor.obstacle_cuts[0] == Cut(0, 150, CUT_PERSON)
    assert "moved" not in phases(rig, EVENT_OBSTACLE_SIDESTEP)
    assert ("cut", None, None, CUT_PERSON) in sidestep_trail(rig)
    assert_invariants(rig)
    assert_never_landed(rig)


def test_a_person_seen_on_the_way_back_cuts_the_lane_and_holds_under_manual_control(rigs):
    # The way back from (50, 300) faces 270 degrees; a person is seen there.
    person_on_the_way_back = BlockedZone(heading_deg=270.0, x_min=40, x_max=60, y_min=290, y_max=310, cue="person")
    rig = rigs([POLE, person_on_the_way_back], **SIDESTEP_ONLY)
    start(rig, DEMO)
    assert wait_for(lambda: not rig.executor.is_busy and rig.executor.control_mode != "autonomous")
    # Cut for the person at the point on the lane, and held: the reason is never lost.
    assert rig.executor.obstacle_cuts == [Cut(0, 300, CUT_PERSON)]
    hold_point = rig.executor.manual_state.hold_pose
    assert (hold_point.x_cm, hold_point.y_cm) == (0.0, 300.0)
    assert phases(rig, EVENT_OBSTACLE_SIDESTEP)[-2:] == ["cut", "held"]
    assert avoid(rig)["phase"] == "held"
    assert [e["reason"] for e in events(rig, "edge_resume_search_blocked")] == [CUT_PERSON]
    assert_invariants(rig)
    assert_never_landed(rig)


def test_a_sensor_down_beside_the_lane_cuts_the_lanes_for_sensor_down_after_flying_back(rigs):
    def rule(pose, n):
        x, y, heading = pose
        # The view beside the lane, facing forward from y 200, cannot be judged; each try takes 4 s.
        if x == 50 and y >= 200 and heading == 0:
            rig.clock.advance(4.0)
            return "stale"
        return None

    rig = rigs(sensor=scripted([POLE], rule=rule), **SIDESTEP_ONLY)
    fly(rig, DEMO)
    # 12 s of views that could not be judged: past OBSTACLE_SENSOR_DOWN_S, so every lane is ended.
    assert rig.executor.obstacle_cuts == [Cut(0, 200, CUT_SENSOR_DOWN)]
    assert ("cut", None, None, CUT_SENSOR_DOWN) in sidestep_trail(rig)
    # It flew back to the lane before anything else, and nothing was flown after.
    assert at(assert_invariants(rig))[:2] == (0.0, 200.0)
    assert flown_points(rig)[-1] == (0, 200)
    assert_back_on_the_plan(rig)
    assert_never_landed(rig)


# ---------------------------------------------------------------------------
# The person cue must be known (review blocker 2)
# ---------------------------------------------------------------------------


def test_an_unknown_person_cue_on_the_blocked_view_refuses_the_sidestep(rigs):
    rig = rigs(**SIDESTEP_ONLY)
    rig.sensor.queue(SenseResult.make_blocked(person_cue=PERSON_CUE_UNKNOWN))
    fly(rig, LANES)
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_OBSTACLE)]
    assert sidestep_trail(rig) == [("refused", None, None, "person_cue_unknown")]
    assert counts(rig) == (0, 0, 0, 0)
    assert "moved" not in phases(rig, EVENT_OBSTACLE_SIDESTEP)
    assert_invariants(rig)
    assert_never_landed(rig)


def test_an_unknown_person_cue_on_the_relook_refuses_the_sidestep(rigs):
    rig = rigs(**AVOID)
    rig.sensor.queue(SenseResult.make_blocked(person_cue=PERSON_CUE_CLEAR),
                     SenseResult.make_blocked(person_cue=PERSON_CUE_UNKNOWN))
    fly(rig, LANES)
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_OBSTACLE)]
    assert sidestep_trail(rig) == [("refused", None, None, "person_cue_unknown")]
    assert counts(rig) == (1, 0, 0, 0)
    assert_never_landed(rig)


def test_a_sideways_gate_that_cannot_rule_a_person_out_refuses_the_sidestep(rigs):
    def rule(pose, n):
        # Facing right from the lane: a clear depth view, but no fresh person detection.
        return SenseResult.make_clear(person_cue=PERSON_CUE_UNKNOWN) if pose[2] == 90 and pose[0] == 0 else None

    rig = rigs([POLE], sensor=scripted([POLE], rule=rule), **SIDESTEP_ONLY)
    fly(rig, DEMO)
    # Nothing moved sideways, and no other side is tried after a gate that could not rule a person out.
    assert rig.executor.obstacle_cuts[0] == Cut(0, 150, CUT_OBSTACLE)
    assert "moved" not in phases(rig, EVENT_OBSTACLE_SIDESTEP)
    refused = [e for e in events(rig, EVENT_OBSTACLE_SIDESTEP) if e["phase"] == "refused" and e.get("side") == "right"]
    assert refused[-1]["reason"] == "sensor_stale" and refused[-1]["person_cue"] == PERSON_CUE_UNKNOWN
    # The next point flown after the cut is lane 1's first step, flown facing right from the lane.
    assert flown_points(rig)[2:4] == [(0, 150), (50, 150)] and rig.timeline.count(("rotate", 90)) >= 1
    assert_invariants(rig)
    assert_never_landed(rig)


def test_a_beside_gate_that_cannot_rule_a_person_out_flies_no_step_beside_the_lane(rigs):
    def rule(pose, n):
        return SenseResult.make_clear(person_cue=PERSON_CUE_UNKNOWN) if pose == (50, 150, 0) else None

    rig = rigs(sensor=scripted([POLE], rule=rule), **SIDESTEP_ONLY)
    fly(rig, DEMO)
    assert rig.executor.obstacle_cuts[0] == Cut(0, 150, CUT_OBSTACLE)
    assert (50, 200) not in flown_points(rig)
    assert counts(rig)[3] == 0
    assert_invariants(rig)
    assert_never_landed(rig)


def test_a_person_cue_reported_down_refuses_every_sidestep(rigs):
    sensor = scripted([POLE], status={"person_cue_down": True, "person_detection_age_s": 9.0})
    rig = rigs(sensor=sensor, **SIDESTEP_ONLY)
    fly(rig, DEMO)
    assert rig.executor.obstacle_cuts[0] == Cut(0, 150, CUT_OBSTACLE)
    assert sidestep_trail(rig) == [("refused", None, None, "person_cue_down")]
    assert_never_landed(rig)


def test_the_relook_and_sidestep_gates_ask_the_sensor_to_wait_for_a_person(rigs):
    rig = rigs(sensor=scripted([POLE]), obstacle_relook=1, obstacle_sidestep_cm=50, obstacle_sidesteps=1)
    fly(rig, DEMO)
    sensor = rig.executor._obstacle_sensor
    sources = [e.get("source") for e in events(rig, EVENT_OBSTACLE_GATE)]
    assert len(sources) == len(sensor.person_waits)
    waits = dict.fromkeys(sources)
    for source, wait in zip(sources, sensor.person_waits):
        assert waits[source] in (None, wait), source
        waits[source] = wait
    assert waits == {None: False, SOURCE_RELOOK: True, SOURCE_SIDESTEP: True, SOURCE_SIDESTEP_RETURN: True}


# ---------------------------------------------------------------------------
# The point the plan continues from never outlives the flight (review blocker 3)
# ---------------------------------------------------------------------------


def test_an_unplanned_landing_during_a_held_sidestep_forgets_the_point_on_the_lane(rigs):
    # The review's probe: hold mid-sidestep, the Tello lands on its own, then a new plan and takeoff.
    rig = rigs([POLE], **SIDESTEP_ONLY)
    rig.sensor.on_call = lambda n: send(rig.executor, hold(cmd_id="h1")) if n == 6 else None
    start(rig, DEMO)
    assert wait_for(lambda: reply_for(rig.sent, "h1") is not None and not rig.executor.is_busy)
    target = rig.executor._resume_target
    assert (target.x_cm, target.y_cm) == (0.0, 150.0)
    rig.drone.state.is_flying = False
    rig.drone.state.height = 0
    rig.executor.tick()
    assert "edge_unplanned_landing" in rig.log.names()
    assert rig.executor._resume_target is None
    rig.sensor.on_call = None
    rig.sensor.blocked = []
    mark = len(rig.drone.calls)
    start(rig, lanes_of(("right", 100)), plan_id="p-2", plan_seq=2)
    assert wait_for(lambda: finished(rig))
    # The new flight flies its own lane from its takeoff point, never to the old point on the lane.
    assert rig.drone.calls[mark:] == [
        ("takeoff",), ("move", "up", 20), ("rotate", 90), ("move", "forward", 50), ("move", "forward", 50),
    ]
    assert at(rig.executor.envelope.pose_snapshot())[:2] == (100.0, 0.0)


def test_a_takeoff_forgets_a_point_left_from_an_earlier_flight(rigs):
    rig = rigs()
    send(rig.executor, fp.make_assign_lanes("drone-1", 100, LANES, {"x_cm": 0, "y_cm": 0, "w_cm": 400,
                                            "h_cm": 400}, fence=FENCE, plan_id="p-1", plan_seq=1, cmd_id="a1"))
    assert answered(rig, "a1")["type"] == "ack"
    from flightrisk.drone.safety import RelativePose

    # Left over, as by a landing that raced the monitor: the takeoff resets the frame and forgets it.
    rig.executor._resume_target = RelativePose(-120.0, 80.0, 0.0)
    t_ground = rig.clock() + 0.5
    send(rig.executor, fp.make_start_at(t_ground, fence=FENCE, plan_id="p-1", cmd_id="s1"))
    rig.clock.set(t_ground)
    assert wait_for(lambda: finished(rig))
    assert rig.executor._resume_target is None
    assert "edge_resume_search_returned" not in rig.log.names()
    assert at(assert_invariants(rig)) == (100.0, 0.0, 180.0)


def test_a_new_plan_on_the_ground_forgets_a_point_left_from_an_earlier_flight(rigs):
    from flightrisk.drone.safety import RelativePose

    rig = rigs()
    rig.executor._resume_target = RelativePose(-120.0, 80.0, 0.0)
    send(rig.executor, fp.make_assign_lanes("drone-1", 100, LANES, {"x_cm": 0, "y_cm": 0, "w_cm": 400,
                                            "h_cm": 400}, fence=FENCE, plan_id="p-1", plan_seq=1, cmd_id="a1"))
    assert answered(rig, "a1")["type"] == "ack"
    assert rig.executor._resume_target is None


# ---------------------------------------------------------------------------
# The area, the drones that may sidestep, the time budget and the re-look limit
# ---------------------------------------------------------------------------


def fly_in_zone(rig, lanes, zone) -> None:
    send(rig.executor, fp.make_assign_lanes("drone-1", 100, lanes, zone, fence=FENCE, plan_id="p-1", plan_seq=1,
                                            cmd_id="a-p-1"))
    assert answered(rig, "a-p-1")["type"] == "ack"
    t_ground = rig.clock() + 0.5
    send(rig.executor, fp.make_start_at(t_ground, fence=FENCE, plan_id="p-1", cmd_id="s-p-1"))
    rig.clock.set(t_ground)
    assert wait_for(lambda: finished(rig)), rig.timeline


@pytest.mark.parametrize(("zone", "allowed"), [
    # The zone's near-left corner is the takeoff point: x 0..220 in the drone's frame.
    ({"x_cm": 1000, "y_cm": 500, "w_cm": 220, "h_cm": 400}, True),
    # A takeoff point 180 cm into the zone: x -180..40, so the right side (x 50) is outside it.
    ({"x_cm": 1000, "y_cm": 500, "w_cm": 220, "h_cm": 400, "takeoff_x_cm": 1180, "takeoff_y_cm": 500}, False),
    # A zone narrower than the path: the box around the path is cut down to it.
    ({"x_cm": 0, "y_cm": 0, "w_cm": 20, "h_cm": 400}, False),
])
def test_the_sidestep_stays_inside_the_plans_zone(rigs, zone, allowed):
    rig = rigs([POLE], geofence_cm=1000, **SIDESTEP_ONLY)
    fly_in_zone(rig, DEMO, zone)
    if allowed:
        assert rig.executor.obstacle_cuts == []
        assert (50, 200) in flown_points(rig)
    else:
        assert rig.executor.obstacle_cuts[0] == Cut(0, 150, CUT_OBSTACLE)
        assert ("refused", "right", None, "plan_area") in sidestep_trail(rig)
        assert "moved" not in phases(rig, EVENT_OBSTACLE_SIDESTEP)
    assert_never_landed(rig)


def test_a_fleet_plan_drone_never_sidesteps_toward_its_neighbour(rigs):
    plan = fleet_patterns.plan_parallel_split(["a", "b"], (1000, 400), 100, footprint=SQUARE_FOOTPRINT,
                                              geofence_cm=1000)
    mine, theirs = plan.drones
    fields = mine.assign_lanes_fields()
    zone = fields["zone"]
    ox, oy = zone["takeoff_x_cm"], zone["takeoff_y_cm"]
    last = mine.relative_waypoints()[-2]  # the start of its last lane, nearest the neighbour
    # A pole on the last lane: the side toward drone b is outside drone a's zone, so it steps away from b.
    pole = BlockedZone(heading_deg=0.0, x_min=last[0] - 10, x_max=last[0] + 10, y_min=150, y_max=250)
    rig = rigs([pole], geofence_cm=1000, **SIDESTEP_ONLY)
    fly_in_zone(rig, fields["lanes"], zone)
    assert rig.executor.obstacle_cuts == []
    assert ("refused", "right", None, "plan_area") in sidestep_trail(rig)
    assert ("start", "left", None, None) in sidestep_trail(rig)
    zone_x = (zone["x_cm"] - ox, zone["x_cm"] + zone["w_cm"] - ox)
    their_path = [(x - ox, y - oy) for x, y in theirs.waypoints()]
    for x, y in flown_points(rig):
        assert zone_x[0] <= x <= zone_x[1]
        # Never closer to the other drone's whole path than the plan's gap.
        nearest = min(math.dist((x, y), (px, max(min(y, 400), 0))) for px, _ in their_path)
        assert nearest >= plan.gap_cm
    assert_invariants(rig)
    assert_never_landed(rig)


def test_an_appended_lane_outside_the_zone_is_never_sidestepped(rigs):
    # A re-flight appended to this drone runs outside its zone (y above 400): the box would allow a
    # sidestep there, the zone does not.
    appended = list(DEMO) + lanes_of(("forward", 100))
    pole = BlockedZone(heading_deg=0.0, x_min=190, x_max=210, y_min=450, y_max=460)
    rig = rigs([pole], geofence_cm=600, **SIDESTEP_ONLY)

    def retask(n):
        if n == 25:  # a gate on lane 4, which the drone is flying
            send(rig.executor, fp.make_assign_lanes("drone-1", 100, appended, {"x_cm": 0, "y_cm": 0, "w_cm": 400,
                                                    "h_cm": 400}, fence=FENCE, plan_id="p-2", plan_seq=2,
                                                    cmd_id="a-p-2"))

    rig.sensor.on_call = retask
    start(rig, DEMO)
    assert wait_for(lambda: reply_for(rig.sent, "a-p-2") is not None)
    assert wait_for(lambda: finished(rig))
    assert rig.executor.obstacle_cuts == [Cut(5, 50, CUT_OBSTACLE)]
    assert [(e["phase"], e["reason"]) for e in events(rig, EVENT_OBSTACLE_SIDESTEP)] == [
        ("refused", "plan_area"), ("refused", "plan_area"),
    ]
    assert at(assert_invariants(rig)) == (200.0, 450.0, 0.0)
    assert_never_landed(rig)


def test_a_drone_on_the_field_envelope_never_sidesteps():
    from flightrisk.drone.tello import FakeTelloController

    drone = FakeTelloController(name="drone-1")
    drone.connect()
    envelope = SafetyEnvelope(alt_slot_cm=1500, ceiling_cm=3000, ceiling_margin_cm=500, geofence_cm=3000,
                              limits=FIELD_LIMITS)
    executor = LaneExecutor(drone, envelope, "drone-1", send=lambda m: None,
                            obstacle_sensor=TimelineSensor([], envelope.pose_snapshot), obstacle_step_cm=500,
                            obstacle_relook=2, obstacle_sidestep_cm=100, obstacle_sidesteps=2)
    try:
        assert executor._avoid_configured()
        assert not executor._sidestep_enabled()
    finally:
        executor.shutdown(timeout=2.0)


def test_the_time_budget_ends_the_relook_and_cuts_as_before(rigs):
    calls = []

    def rule(pose, n):
        calls.append(n)
        return SenseResult.make_blocked(person_cue=PERSON_CUE_CLEAR) if n == 1 else SenseResult.make_clear(
            person_cue=PERSON_CUE_CLEAR)

    # Every look takes 4 s; five re-looks would take 20 s, the budget is 10 s.
    rig = rigs(sensor=scripted(rule=rule, advance_s=4.0), obstacle_relook=5, obstacle_sidestep_cm=50,
               obstacle_sidesteps=2, obstacle_avoid_budget_s=10)
    fly(rig, LANES)
    assert rig.executor.obstacle_cuts[0] == Cut(0, 0, CUT_OBSTACLE)
    relook = events(rig, EVENT_OBSTACLE_RELOOK)
    assert [e["phase"] for e in relook] == ["start", "budget"]
    assert relook[-1]["elapsed_s"] >= 10.0 and relook[-1]["budget_s"] == 10.0
    # No sidestep after the budget, and nothing flown toward the obstacle.
    assert events(rig, EVENT_OBSTACLE_SIDESTEP) == []
    assert all(not (px == 0 and py > 0) for px, py in flown_points(rig))
    assert_never_landed(rig)


def test_the_time_budget_ends_a_pass_beside_the_lane_and_the_drone_flies_back(rigs):
    rig = rigs(sensor=scripted([POLE], advance_s=4.0), obstacle_avoid_budget_s=10, **SIDESTEP_ONLY)
    fly(rig, DEMO)
    trail = sidestep_trail(rig)
    assert ("budget", None, None, None) in trail
    passed = [e for e in events(rig, EVENT_OBSTACLE_SIDESTEP) if e["phase"] == "passed"]
    # The budget stopped the pass short of its 150 cm; every log carries the time since the blocked gate.
    assert passed and passed[0]["passed_cm"] < 150
    assert all("elapsed_s" in e for e in events(rig, EVENT_OBSTACLE_SIDESTEP))
    pose = assert_invariants(rig)
    assert at(pose)[:2] == (200.0, 400.0)
    assert_back_on_the_plan(rig)
    assert_never_landed(rig)


def test_a_lane_gets_a_limited_number_of_relook_rounds(rigs):
    rig = rigs(obstacle_relook=2)
    blocked, clear = SenseResult.make_blocked(), SenseResult.make_clear()
    rig.sensor.queue(*([blocked, clear, clear] * OBSTACLE_RELOOKS_PER_LANE), blocked)
    fly(rig, LANES)
    # Three blocked gates were looked at again and flown; the fourth on the same lane is cut at once.
    assert rig.executor.obstacle_cuts == [Cut(0, 50 * OBSTACLE_RELOOKS_PER_LANE, CUT_OBSTACLE)]
    assert phases(rig, EVENT_OBSTACLE_RELOOK) == ["start", "cleared"] * OBSTACLE_RELOOKS_PER_LANE + ["refused"]
    assert events(rig, EVENT_OBSTACLE_RELOOK)[-1]["reason"] == "lane_limit"
    assert_never_landed(rig)


# ---------------------------------------------------------------------------
# Counts, offsets, re-tasks and interruptions
# ---------------------------------------------------------------------------


def test_a_pass_reports_its_stretch_beside_the_lane_as_an_offset(rigs):
    rig = rigs([POLE], **AVOID)
    fly(rig, DEMO)
    status = rig.executor.obstacle_status()[TELEMETRY_AVOID]
    assert status["offsets"] == [0, 150, 150]
    assert status["sidestep_passes"] == 1
    msg = rig.executor.telemetry()
    assert fp.validate_message(msg, proto=2) == "telemetry"


def test_a_pass_that_a_hold_interrupts_still_counts(rigs):
    rig = rigs([POLE], **SIDESTEP_ONLY)
    # Calls 1-3: lane 0; 4: blocked; 5: the sideways gate; 6: the first gate beside the lane; 7: the second.
    rig.sensor.on_call = lambda n: send(rig.executor, hold(cmd_id="h1")) if n == 7 else None
    start(rig, DEMO)
    assert wait_for(lambda: reply_for(rig.sent, "h1") is not None and not rig.executor.is_busy)
    assert counts(rig)[2:] == (1, 1)
    assert avoid(rig)["offsets"] == [0, 150, 50]
    rig.sensor.on_call = None
    send(rig.executor, resume(cmd_id="r1"))
    assert wait_for(lambda: finished(rig))
    assert at(assert_invariants(rig)) == (200.0, 400.0, 0.0)
    assert_back_on_the_plan(rig)
    assert_never_landed(rig)


def test_a_protocol_hold_on_the_way_back_resumes_from_the_lane(rigs):
    rig = rigs([POLE], **SIDESTEP_ONLY)
    # Call 9 is the gate of the way back from (50, 300).
    rig.sensor.on_call = lambda n: send(rig.executor, hold(cmd_id="h1")) if n == 9 else None
    start(rig, DEMO)
    assert wait_for(lambda: reply_for(rig.sent, "h1") is not None and not rig.executor.is_busy)
    assert at(rig.executor.envelope.pose_snapshot())[:2] == (50.0, 300.0)
    target = rig.executor._resume_target
    assert (target.x_cm, target.y_cm) == (0.0, 300.0)
    rig.sensor.on_call = None
    send(rig.executor, resume(cmd_id="r1"))
    assert wait_for(lambda: finished(rig))
    assert rig.executor.obstacle_cuts == []
    assert flown_points(rig)[7] == (0, 300)  # back on the lane before the plan went on
    assert at(assert_invariants(rig)) == (200.0, 400.0, 0.0)
    assert_back_on_the_plan(rig)
    assert_never_landed(rig)


def test_land_all_on_the_way_back_lands_beside_the_lane_and_forgets_the_point(rigs):
    rig = rigs([POLE], **SIDESTEP_ONLY)
    rig.sensor.on_call = lambda n: send(rig.executor, fp.make_emergency_land_all()) if n == 9 else None
    start(rig, DEMO)
    assert wait_for(lambda: not rig.drone.state.is_flying and not rig.executor.is_busy)
    assert at(rig.executor.envelope.pose_snapshot())[:2] == (50.0, 300.0)
    assert rig.executor._land_reason == "emergency_land_all"
    assert rig.executor._resume_target is None
    assert rig.timeline.count(("land",)) == 1
    assert_invariants(rig)


def test_link_loss_on_the_way_back_flies_back_to_the_lane_when_the_link_returns(rigs):
    rig = rigs([POLE], **SIDESTEP_ONLY)
    rig.sensor.on_call = lambda n: rig.executor._begin_link_hold() if n == 9 else None
    start(rig, DEMO)
    assert wait_for(lambda: not rig.executor.is_busy and rig.executor._link_parked)
    assert at(rig.executor.envelope.pose_snapshot())[:2] == (50.0, 300.0)
    rig.sensor.on_call = None
    rig.executor._end_link_hold()
    assert wait_for(lambda: finished(rig))
    assert flown_points(rig)[7] == (0, 300)
    assert at(assert_invariants(rig)) == (200.0, 400.0, 0.0)
    assert_back_on_the_plan(rig)
    assert_never_landed(rig)


def test_a_retask_to_a_new_plan_during_a_pass_flies_back_and_then_the_new_plan(rigs):
    rig = rigs([POLE], **SIDESTEP_ONLY)

    def retask(n):
        if n == 7:  # the second gate beside the lane
            send(rig.executor, fp.make_assign_lanes("drone-1", 100, lanes_of(("right", 100)), {
                "x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}, fence=FENCE, plan_id="p-2", plan_seq=2,
                cmd_id="a-p-2"))

    rig.sensor.on_call = retask
    start(rig, DEMO)
    assert answered(rig, "a-p-2")["type"] == "ack"
    assert wait_for(lambda: finished(rig))
    points = flown_points(rig)
    # Back on the old lane at (0, 200), then the new plan's lane from there.
    assert points[-3:] == [(0, 200), (50, 200), (100, 200)]
    # The new plan has its own counts: the old plan's pass is not reported in it.
    assert counts(rig) == (0, 0, 0, 0) and "offsets" not in avoid(rig)
    assert_back_on_the_plan(rig)
    assert_invariants(rig)
    assert_never_landed(rig)


def test_a_retask_that_continues_the_plan_keeps_the_lanes_sidestep_attempts(rigs):
    second_pole = BlockedZone(heading_deg=0.0, x_min=-10, x_max=10, y_min=350, y_max=400)
    rig = rigs([POLE, second_pole], obstacle_relook=0, obstacle_sidestep_cm=50, obstacle_sidesteps=1)
    continued = list(DEMO) + lanes_of(("left", 100))

    def retask(n):
        if n == 10:  # the gate at (0, 300), back on lane 0 after the first sidestep
            send(rig.executor, fp.make_assign_lanes("drone-1", 100, continued, {"x_cm": 0, "y_cm": 0, "w_cm": 400,
                                                    "h_cm": 400}, fence=FENCE, plan_id="p-2", plan_seq=2,
                                                    cmd_id="a-p-2"))

    rig.sensor.on_call = retask
    start(rig, DEMO)
    assert answered(rig, "a-p-2")["type"] == "ack"
    assert wait_for(lambda: finished(rig))
    # The re-task continued the plan, so lane 0 had used its one attempt: the second pole cuts it.
    assert rig.executor.obstacle_cuts == [Cut(0, 350, CUT_OBSTACLE)]
    refused = [e for e in events(rig, EVENT_OBSTACLE_SIDESTEP) if e["phase"] == "refused"]
    assert refused[-1]["reason"] == "attempts"
    assert counts(rig)[2:] == (1, 1)
    assert_invariants(rig)
    assert_never_landed(rig)


# ---------------------------------------------------------------------------
# The gate's freshness floor uses the configured settle time (review item 9)
# ---------------------------------------------------------------------------


def test_a_gate_never_asks_for_a_frame_older_than_itself_with_a_short_settle(rigs):
    starts = []

    def rule(pose, n):
        starts.append(rig.clock() - 1.0)  # the clock before this call's 1 s
        return "stale" if n == 1 else None

    rig = rigs(sensor=scripted(rule=rule, advance_s=1.0), obstacle_settle_s=0.1)
    fly(rig, lanes_of(("forward", 50)))
    sensor = rig.executor._obstacle_sensor
    # The retry started 1 s after the move ended. The sensor judges a frame captured at or after ``after``
    # plus its 0.1 s settle, which must be the retry's own start; the fixed 0.3 s let it be 0.2 s older.
    retry = sensor.calls[1]
    assert retry.after + 0.1 == pytest.approx(starts[1])


def test_the_edge_passes_its_settle_time_to_the_executor():
    sensor = TimelineSensor([], lambda: None)
    assert "obstacle_settle_s" not in ObstacleWiring(sensor=sensor).executor_kwargs()
    kwargs = ObstacleWiring(sensor=sensor, settle_s=0.5).executor_kwargs()
    assert kwargs["obstacle_settle_s"] == 0.5
    assert kwargs["obstacle_sense_timeout_s"] == pytest.approx(2.2)


def test_planarea_intersection():
    box = PlanArea(0.0, 200.0, 0.0, 400.0)
    assert box.intersect(PlanArea(-50.0, 100.0, 100.0, 500.0)) == PlanArea(0.0, 100.0, 100.0, 400.0)
    assert box.intersect(PlanArea(300.0, 400.0, 0.0, 400.0)) is None


def test_a_person_cut_is_offered_only_after_the_operators_confirmation(rigs):
    # The ground's side of blocker 1: the person cut the sidestep made reaches it as an ordinary person cut.
    from flightrisk.drone.obstacle_sensor import CONFIRM_REFLIGHT_CUT_REASONS

    rig = rigs([SHORT_POLE, PERSON_BESIDE], **SIDESTEP_ONLY)
    fly(rig, DEMO)
    status = rig.executor.obstacle_status()
    assert status["obstacle_cut_reasons"].split(",")[0] in CONFIRM_REFLIGHT_CUT_REASONS
    assert status["obstacle_cuts"][:2] == [0, 250]
