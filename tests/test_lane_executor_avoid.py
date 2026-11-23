"""Clear votes, the re-look and the sidestep at a blocked lane gate (``lane_flight._look``, ``lane_avoid``).

Every executor is the real ``LaneExecutor`` on the fake Tello and the scripted
sensor of ``tests/test_lane_executor_obstacle.py``, whose shared timeline
checks the two invariants of the obstacle design after every scenario: no
move is ever sent after a gate that was not clear (each move comes right
after a clear view), and the dead-reckoned pose is the sum of the moves and
turns the drone flew. On top of that, every scenario here checks that a
blocked reading never lands the drone.

The plan is the design's worked example (``DEMO``): forward 400, right 100,
back 400, right 100, forward 400, inside a 500 cm geofence, so the drone's
own path spans x 0..200 and y 0..400.
"""
# The tests take the ``rigs`` fixture imported from the obstacle tests as an argument.
# ruff: noqa: F811
from __future__ import annotations

import pytest

from flightrisk import edge_device
from flightrisk import fleet_protocol as fp
from flightrisk.drone.lane_avoid import (
    SOURCE_RELOOK,
    SOURCE_SIDESTEP,
    SOURCE_SIDESTEP_RETURN,
    PlanArea,
)
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.lane_steps import Cut
from flightrisk.drone.obstacle_sensor import (
    CUE_PERSON,
    CUT_OBSTACLE,
    CUT_PERSON,
    ENV_OBSTACLE_AVOID_BUDGET_S,
    ENV_OBSTACLE_CLEAR_VOTES,
    ENV_OBSTACLE_RELOOK,
    ENV_OBSTACLE_SIDESTEP_CM,
    ENV_OBSTACLE_SIDESTEPS,
    EVENT_OBSTACLE_GATE,
    EVENT_OBSTACLE_RELOOK,
    EVENT_OBSTACLE_SIDESTEP,
    MAX_OFFSETS_LISTED,
    OBSTACLE_SETTLE_S,
    TELEMETRY_AVOID,
    TELEMETRY_PERSON_CUE,
    BlockedZone,
    SenseResult,
)
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.edge_device import ObstacleSettings
from tests.lane_executor_support import (
    FENCE,
    LANES,
    hold,
    nudge,
    reply_for,
    resume,
    send,
    wait_for,
)
from tests.test_lane_executor_obstacle import (  # noqa: F401 - ``rigs`` is a fixture
    DEMO,
    WALL_AT_250,
    TimelineSensor,
    answered,
    assert_invariants,
    at,
    events,
    finished,
    fly,
    lane_moves,
    lanes_of,
    mark_unsensed,
    rigs,
    start,
)

#: A thin obstacle on lane 0's line (x 0), seen facing forward from y 150 to 250.
POLE = BlockedZone(heading_deg=0.0, x_min=-10, x_max=10, y_min=150, y_max=250)
#: A wall across lane 0 and the line beside it (x up to 60) from y 250.
WIDE_WALL = BlockedZone(heading_deg=0.0, x_max=60, y_min=250)
#: Re-look twice, sidestep 50 cm, two sidesteps per lane: the edge's defaults.
AVOID = {"obstacle_relook": 2, "obstacle_sidestep_cm": 50, "obstacle_sidesteps": 2}
SIDESTEP_ONLY = {"obstacle_relook": 0, "obstacle_sidestep_cm": 50, "obstacle_sidesteps": 2}


#: ``obstacle_avoid`` before anything happened: the edge leaves the field out until then.
NOTHING_YET = {"relooks": 0, "relook_clears": 0, "sidesteps": 0, "sidestep_passes": 0, "phase": None}


def avoid(rig) -> dict:
    return rig.executor.obstacle_status().get(TELEMETRY_AVOID) or dict(NOTHING_YET)


def counts(rig) -> tuple[int, int, int, int]:
    status = avoid(rig)
    return status["relooks"], status["relook_clears"], status["sidesteps"], status["sidestep_passes"]


def phases(rig, event: str) -> list[str]:
    return [fields["phase"] for fields in events(rig, event)]


def gate_sources(rig) -> list:
    return [fields.get("source") for fields in events(rig, EVENT_OBSTACLE_GATE)]


def assert_never_landed(rig) -> None:
    assert ("land",) not in rig.timeline
    assert rig.drone.state.is_flying
    assert rig.executor._land_reason is None
    assert "edge_land" not in rig.log.names()


def assert_back_on_the_plan(rig) -> None:
    assert rig.executor._resume_target is None
    assert avoid(rig)["phase"] is None


# ---------------------------------------------------------------------------
# Clear votes
# ---------------------------------------------------------------------------


def test_with_two_votes_every_step_follows_two_clear_views(rigs):
    rig = rigs(obstacle_clear_votes=2)
    fly(rig, LANES)
    clear, fwd = ("sense", "clear"), ("move", "forward", 50)
    assert rig.timeline == [
        ("move", "up", 20),
        *[clear, clear, fwd] * 4,
        ("rotate", 90), *[clear, clear, fwd] * 2,
        ("rotate", 90), *[clear, clear, fwd] * 4,
    ]
    assert at(assert_invariants(rig)) == (100.0, 0.0, 180.0)


def test_with_two_votes_a_single_clear_view_no_longer_flies_a_step(rigs):
    rig = rigs(obstacle_clear_votes=2)
    rig.sensor.queue(SenseResult.make_clear(), SenseResult.make_blocked())
    fly(rig, LANES)
    # The first view was clear, the second blocked: lane 0 is cut before any of it is flown.
    assert rig.timeline[1:4] == [("sense", "clear"), ("sense", "blocked"), ("rotate", 90)]
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_OBSTACLE)]
    assert_invariants(rig)
    assert_never_landed(rig)


def test_with_one_vote_a_single_clear_view_flies_the_step_as_before(rigs):
    rig = rigs(obstacle_clear_votes=1)
    rig.sensor.queue(SenseResult.make_clear(), SenseResult.make_blocked())
    fly(rig, LANES)
    assert rig.timeline[1:3] == [("sense", "clear"), ("move", "forward", 50)]
    assert rig.executor.obstacle_cuts == [Cut(0, 50, CUT_OBSTACLE)]


def test_a_person_at_any_vote_cuts_the_lane_for_a_person(rigs):
    rig = rigs(obstacle_clear_votes=2, **AVOID)
    rig.sensor.queue(SenseResult.make_clear(), SenseResult.make_blocked(CUE_PERSON))
    fly(rig, LANES)
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_PERSON)]
    # A person is never looked past or stepped around.
    assert counts(rig) == (0, 0, 0, 0)
    assert_invariants(rig)
    assert_never_landed(rig)


def test_a_view_that_is_not_a_view_starts_the_votes_again(rigs):
    rig = rigs(obstacle_clear_votes=2)
    rig.sensor.queue(SenseResult.make_clear(), "stale", SenseResult.make_clear(), SenseResult.make_clear())
    fly(rig, lanes_of(("forward", 50),))
    assert rig.timeline == [
        ("move", "up", 20),
        ("sense", "clear"), ("sense", "unavailable"), ("sense", "clear"), ("sense", "clear"),
        ("move", "forward", 50),
    ]
    assert rig.executor.obstacle_cuts == []


class ClockedSensor(TimelineSensor):
    """A scripted sensor that takes 0.1 s of the fake clock per look and records when each look ended."""

    def __init__(self, timeline, pose_provider, clock, blocked=()):
        super().__init__(timeline, pose_provider, blocked)
        self.clock = clock
        self.ended: list[float] = []

    def sense(self, after, timeout_s=2.0):
        self.clock.advance(0.1)
        result = super().sense(after, timeout_s)
        self.ended.append(self.clock())
        return result


def test_each_vote_judges_a_frame_captured_after_the_previous_vote(rigs):
    rig = rigs(sensor=lambda timeline, envelope, clock: ClockedSensor(timeline, envelope.pose_snapshot, clock),
               motion_s=1.0, obstacle_clear_votes=3)
    sensor = rig.executor._obstacle_sensor
    fly(rig, LANES)
    calls = sensor.calls
    assert len(calls) == 3 * 10
    for gate in range(10):
        for vote in (1, 2):
            k = gate * 3 + vote
            # The frame this vote may judge is captured at or after ``after`` plus the settle time,
            # so at least the settle time after the previous vote ended: never the previous vote's frame.
            assert calls[k].after >= sensor.ended[k - 1]
            assert calls[k].after + OBSTACLE_SETTLE_S > sensor.ended[k - 1]
            assert calls[k].after > calls[k - 1].after


# ---------------------------------------------------------------------------
# The re-look
# ---------------------------------------------------------------------------


def test_a_relook_that_clears_flies_the_step(rigs):
    rig = rigs(obstacle_relook=2)
    rig.sensor.queue(SenseResult.make_blocked())
    fly(rig, LANES)
    assert rig.timeline[1:5] == [
        ("sense", "blocked"), ("sense", "clear"), ("sense", "clear"), ("move", "forward", 50),
    ]
    assert rig.executor.obstacle_cuts == []
    assert at(assert_invariants(rig)) == (100.0, 0.0, 180.0)
    assert counts(rig) == (1, 1, 0, 0)
    assert phases(rig, EVENT_OBSTACLE_RELOOK) == ["start", "cleared"]
    assert gate_sources(rig).count(SOURCE_RELOOK) == 2
    assert_back_on_the_plan(rig)
    assert_never_landed(rig)


def test_a_relook_needs_every_look_clear(rigs):
    rig = rigs(obstacle_relook=2)
    rig.sensor.queue(SenseResult.make_blocked(), SenseResult.make_clear(), SenseResult.make_blocked())
    fly(rig, LANES)
    # One clear look among the re-looks does not fly the step.
    assert rig.timeline[1:5] == [("sense", "blocked"), ("sense", "clear"), ("sense", "blocked"), ("rotate", 90)]
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_OBSTACLE)]
    assert phases(rig, EVENT_OBSTACLE_RELOOK) == ["start", "blocked"]
    assert_invariants(rig)
    assert_never_landed(rig)


def test_a_relook_still_blocked_cuts_the_lane_as_before(rigs):
    rig = rigs([WALL_AT_250], obstacle_relook=2)
    fly(rig, DEMO)
    # The same cut and the same flight as without the re-look (the worked example).
    assert rig.executor.obstacle_cuts == [Cut(0, 250, CUT_OBSTACLE)]
    assert at(assert_invariants(rig)) == (200.0, 400.0, 0.0)
    assert sum(e[2] for e in lane_moves(rig)) == 250 + 100 + 250 + 100 + 400
    states = rig.sensor.states()
    first = states.index("blocked")
    # The first re-look that is blocked ends the re-look: no later one could make every one clear.
    assert states[first:first + 3] == ["blocked", "blocked", "clear"]
    assert counts(rig) == (1, 0, 0, 0)
    assert phases(rig, EVENT_OBSTACLE_RELOOK) == ["start", "blocked"]
    assert_back_on_the_plan(rig)
    assert_never_landed(rig)


def test_a_person_seen_at_a_relook_cuts_the_lane_for_a_person(rigs):
    rig = rigs(**AVOID)
    rig.sensor.queue(SenseResult.make_blocked(), SenseResult.make_blocked(CUE_PERSON))
    fly(rig, LANES)
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_PERSON)]
    assert counts(rig) == (1, 0, 0, 0)  # and no sidestep toward it
    assert_invariants(rig)
    assert_never_landed(rig)


def test_a_sensor_that_cannot_look_at_a_relook_cuts_for_the_obstacle_without_a_sidestep(rigs):
    rig = rigs(**AVOID)
    rig.sensor.queue(SenseResult.make_blocked(), "stale", "stale", "stale")
    fly(rig, LANES)
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_OBSTACLE)]
    assert counts(rig) == (1, 0, 0, 0)
    assert_invariants(rig)
    assert_never_landed(rig)


# ---------------------------------------------------------------------------
# The sidestep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("votes", [1, 2])
def test_a_sidestep_gets_past_a_thin_obstacle_and_rejoins_the_lane(rigs, votes):
    rig = rigs([POLE], obstacle_clear_votes=votes, **AVOID)
    fly(rig, DEMO)
    assert rig.executor.obstacle_cuts == []
    pose = assert_invariants(rig)
    assert at(pose) == (200.0, 400.0, 0.0)  # the plan's end: every step of every lane flown
    assert counts(rig) == (1, 0, 1, 1)
    # Left of lane 0 is outside the drone's own path (x 0..200), so it steps right.
    sidestep = events(rig, EVENT_OBSTACLE_SIDESTEP)
    assert [(e["phase"], e.get("side"), e.get("reason")) for e in sidestep[:3]] == [
        ("refused", "left", "plan_area"), ("start", "right", None), ("moved", "right", None),
    ]
    assert [e["phase"] for e in sidestep[3:]] == ["beside", "beside", "beside", "passed"]
    assert sidestep[-1]["passed_cm"] == 150
    # Every sideways and beside move came after clear views of their own.
    sources = gate_sources(rig)
    assert sources.count(SOURCE_SIDESTEP) == 4 * votes and sources.count(SOURCE_SIDESTEP_RETURN) == votes
    # The flown path: to x 50 at y 150, along x 50 to y 300, back to x 0, then the plan.
    x = y = 0.0
    heading = 0.0
    points = []
    for call in rig.drone.calls:
        if call[0] == "rotate":
            heading = (heading + call[1]) % 360
        elif call[0] == "move" and call[1] == "forward":
            dx, dy = {0.0: (0, 1), 90.0: (1, 0), 180.0: (0, -1), 270.0: (-1, 0)}[heading]
            x, y = x + dx * call[2], y + dy * call[2]
            points.append((x, y))
    assert points[:8] == [(0, 50), (0, 100), (0, 150), (50, 150), (50, 200), (50, 250), (50, 300), (0, 300)]
    assert all(not (abs(px) <= 10 and 150 < py <= 250) for px, py in points)  # never through the pole
    assert_back_on_the_plan(rig)
    assert_never_landed(rig)


def test_a_wide_wall_is_not_passed_and_the_lane_is_cut_where_the_drone_stopped(rigs):
    rig = rigs([WIDE_WALL], **AVOID)
    fly(rig, DEMO)
    assert rig.executor.obstacle_cuts == [Cut(0, 250, CUT_OBSTACLE)]
    assert at(assert_invariants(rig)) == (200.0, 400.0, 0.0)  # as in the worked example
    assert counts(rig) == (1, 0, 1, 0)
    assert [e["phase"] for e in events(rig, EVENT_OBSTACLE_SIDESTEP)] == ["refused", "start", "moved", "returned"]
    stop = events(rig, "edge_obstacle_stop")
    assert (stop[0]["x_cm"], stop[0]["y_cm"]) == (0.0, 250.0)  # cut back on the lane, where it stopped
    assert_back_on_the_plan(rig)
    assert_never_landed(rig)


def test_sidesteps_per_lane_are_bounded(rigs):
    lane_wall = BlockedZone(heading_deg=0.0, x_max=10, y_min=150)
    beside_wall = BlockedZone(heading_deg=0.0, x_min=40, x_max=60, y_min=250)
    rig = rigs([lane_wall, beside_wall], **SIDESTEP_ONLY)
    fly(rig, DEMO)
    # The first sidestep passes 100 cm beside the lane; the second finds the wall beside it too.
    assert rig.executor.obstacle_cuts == [Cut(0, 250, CUT_OBSTACLE)]
    assert counts(rig) == (0, 0, 2, 1)
    assert at(assert_invariants(rig)) == (200.0, 400.0, 0.0)
    assert_back_on_the_plan(rig)
    assert_never_landed(rig)


def test_no_sidestep_is_tried_once_the_lane_has_used_its_attempts(rigs):
    lane_wall = BlockedZone(heading_deg=0.0, x_max=10, y_min=150)
    beside_wall = BlockedZone(heading_deg=0.0, x_min=40, x_max=60, y_min=250)
    rig = rigs([lane_wall, beside_wall], obstacle_relook=0, obstacle_sidestep_cm=50, obstacle_sidesteps=1)
    fly(rig, DEMO)
    assert rig.executor.obstacle_cuts == [Cut(0, 250, CUT_OBSTACLE)]
    assert counts(rig) == (0, 0, 1, 1)
    refused = [e for e in events(rig, EVENT_OBSTACLE_SIDESTEP) if e["phase"] == "refused"]
    assert refused[-1]["reason"] == "attempts"
    assert_never_landed(rig)


def test_a_sidestep_outside_the_drones_own_path_is_refused(rigs):
    # A single lane: the drone's own path has no width, so either side would leave it.
    rig = rigs([POLE], **AVOID)
    fly(rig, lanes_of(("forward", 400),))
    assert rig.executor.obstacle_cuts == [Cut(0, 150, CUT_OBSTACLE)]
    assert [(e["phase"], e["reason"]) for e in events(rig, EVENT_OBSTACLE_SIDESTEP)] == [
        ("refused", "plan_area"), ("refused", "plan_area"),
    ]
    assert counts(rig) == (1, 0, 0, 0)
    assert at(assert_invariants(rig)) == (0.0, 150.0, 0.0)
    assert_never_landed(rig)


def test_a_sidestep_the_geofence_refuses_is_not_flown(rigs):
    lanes = lanes_of(("forward", 100), ("right", 100), ("back", 100))
    block = BlockedZone(heading_deg=180.0, x_min=140, y_min=60)
    rig = rigs([block], geofence_cm=150, obstacle_relook=0, obstacle_sidestep_cm=110, obstacle_sidesteps=2)
    # Hold at lane 2's first gate, then the operator nudges the drone toward the fence.
    rig.sensor.on_call = lambda n: send(rig.executor, hold(cmd_id="h1")) if n == 5 else None
    start(rig, lanes)
    assert wait_for(lambda: reply_for(rig.sent, "h1") is not None and not rig.executor.is_busy)
    mark_unsensed(rig)
    send(rig.executor, nudge("left", 50, cmd_id="n1"))
    assert answered(rig, "n1")["type"] == "ack"
    assert wait_for(lambda: not rig.executor.is_busy)
    assert at(rig.executor.envelope.pose_snapshot()) == (150.0, 100.0, 180.0)
    moves_before = len(lane_moves(rig))
    send(rig.executor, resume(cmd_id="r1"))
    assert wait_for(lambda: finished(rig))
    # One side would leave the geofence, the other the drone's own path: nothing moves, the lane is cut.
    assert [(e["phase"], e["reason"]) for e in events(rig, EVENT_OBSTACLE_SIDESTEP)] == [
        ("refused", "plan_area"), ("refused", "geofence"),
    ]
    assert len(lane_moves(rig)) == moves_before
    assert rig.executor.obstacle_cuts == [Cut(2, 0, CUT_OBSTACLE)]
    assert at(rig.executor.envelope.pose_snapshot()) == (150.0, 100.0, 180.0)
    assert_never_landed(rig)


def test_the_sides_are_refused_by_the_geofence_first():
    executor = LaneExecutor(
        _drone(), SafetyEnvelope(alt_slot_cm=100, geofence_cm=100), "drone-1", send=lambda m: None,
        obstacle_sensor=TimelineSensor([], lambda: None), **SIDESTEP_ONLY,
    )
    executor.envelope.record_move("right", 80)
    step = (0, 0, "forward", 50)
    area = PlanArea(-200.0, 200.0, -200.0, 200.0)
    allowed, refused = executor._sidestep_sides(step, SenseResult.make_blocked(), area)
    # x 130 is outside the 100 cm fence; x 30 is inside both.
    assert allowed == ["left"] and refused == [("right", "geofence")]
    allowed, refused = executor._sidestep_sides(
        step, SenseResult.make_blocked(detail={"depth_action": "go_right"}), PlanArea(0.0, 90.0, 0.0, 200.0),
    )
    assert allowed == ["left"] and refused == [("right", "geofence")]
    allowed, refused = executor._sidestep_sides(step, SenseResult.make_blocked(), None)
    assert allowed == [] and sorted(refused) == [("left", "plan_area"), ("right", "geofence")]


def _drone():
    from flightrisk.drone.tello import FakeTelloController

    drone = FakeTelloController(name="drone-1")
    drone.connect()
    return drone


def test_a_first_block_by_a_person_is_never_looked_past_or_stepped_around(rigs):
    rig = rigs([BlockedZone(heading_deg=0.0, x_max=50, y_min=250, cue="person")], **AVOID)
    fly(rig, DEMO)
    assert rig.executor.obstacle_cuts[0] == Cut(0, 250, CUT_PERSON)
    assert counts(rig) == (0, 0, 0, 0)
    assert events(rig, EVENT_OBSTACLE_RELOOK) == [] and events(rig, EVENT_OBSTACLE_SIDESTEP) == []
    assert_invariants(rig)
    assert_never_landed(rig)


def test_a_sidestep_never_moves_toward_a_person(rigs):
    person_beside = BlockedZone(heading_deg=90.0, x_max=10, y_min=140, y_max=160, cue="person")
    rig = rigs([POLE, person_beside], **SIDESTEP_ONLY)
    fly(rig, DEMO)
    # The sideways gate sees the person: nothing moves toward it, and the lane is cut for a person,
    # so the ground offers it to another drone only once the operator confirms.
    assert rig.executor.obstacle_cuts[0] == Cut(0, 150, CUT_PERSON)
    refused = [e for e in events(rig, EVENT_OBSTACLE_SIDESTEP) if e["phase"] == "refused"]
    assert refused[-1]["reason"] == CUT_PERSON
    assert counts(rig)[2:] == (1, 0)
    assert "moved" not in phases(rig, EVENT_OBSTACLE_SIDESTEP)
    assert_invariants(rig)
    assert_never_landed(rig)


def test_forward_only_mode_looks_again_but_never_sidesteps(rigs):
    rig = rigs([WALL_AT_250], obstacle_turn=False, **AVOID)
    fly(rig, DEMO)
    assert rig.executor.obstacle_cuts == [Cut(0, 250, CUT_OBSTACLE)]
    assert counts(rig) == (1, 0, 0, 0)
    assert_never_landed(rig)


def test_a_drone_that_measures_its_heading_never_sidesteps(rigs):
    rig = rigs(**AVOID)
    assert rig.executor._sidestep_enabled()
    rig.drone.measured_heading_deg = lambda: 0.0
    assert not rig.executor._sidestep_enabled()


# ---------------------------------------------------------------------------
# A way back to the lane that is not cleared, landings, holds and link loss
# ---------------------------------------------------------------------------


def test_a_way_back_that_is_not_cleared_holds_under_manual_control_and_cuts_nothing(rigs):
    blocks_the_way_back = BlockedZone(heading_deg=270.0, x_min=40, x_max=60, y_min=290, y_max=310)
    rig = rigs([POLE, blocks_the_way_back], **SIDESTEP_ONLY)
    start(rig, DEMO)
    assert wait_for(lambda: not rig.executor.is_busy and rig.executor.control_mode != "autonomous")
    assert rig.executor.control_mode in ("manual", "held")
    hold_point = rig.executor.manual_state.hold_pose
    # The hold point is on the lane, level with the drone: the plan continues from there.
    assert (hold_point.x_cm, hold_point.y_cm, hold_point.heading_deg) == (0.0, 300.0, 0.0)
    assert rig.executor.manual_state.hold_step == 6
    assert at(rig.executor.envelope.pose_snapshot())[:2] == (50.0, 300.0)
    assert rig.executor.obstacle_cuts == []
    assert phases(rig, EVENT_OBSTACLE_SIDESTEP)[-1] == "held"
    # The telemetry tells the ground, which announces it to the operator (``sidestep_held``).
    assert avoid(rig)["phase"] == "held"
    blocked = events(rig, "edge_resume_search_blocked")
    assert [b["reason"] for b in blocked] == [CUT_OBSTACLE]
    assert_invariants(rig)
    assert_never_landed(rig)
    # Resume search flies back to the point on the lane; the phase is no longer held.
    rig.sensor.blocked = [POLE]
    send(rig.executor, fp.make_resume_search(cmd_id="m2", fence=FENCE))
    assert answered(rig, "m2")["type"] == "ack"
    assert wait_for(lambda: finished(rig))
    assert avoid(rig)["phase"] is None
    assert rig.executor.obstacle_cuts == []
    assert at(assert_invariants(rig, unsensed_directions=())) == (200.0, 400.0, 0.0)


def test_land_all_during_a_relook_lands_and_sends_no_move(rigs):
    rig = rigs(**AVOID)
    rig.sensor.queue(SenseResult.make_blocked())
    rig.sensor.on_call = lambda n: send(rig.executor, fp.make_emergency_land_all()) if n == 2 else None
    start(rig, LANES)
    assert wait_for(lambda: not rig.drone.state.is_flying and not rig.executor.is_busy)
    assert lane_moves(rig) == []
    assert rig.timeline.count(("land",)) == 1
    assert rig.executor._land_reason == "emergency_land_all"
    assert rig.executor.obstacle_cuts == []
    assert rig.executor._resume_target is None
    assert_invariants(rig)


def test_land_all_during_a_sidestep_lands_beside_the_lane_and_sends_no_more_moves(rigs):
    rig = rigs([POLE], **SIDESTEP_ONLY)
    # Calls 1-3: lane 0 from y 0 to 150; 4: blocked; 5: the sideways gate; 6: the first gate beside the lane.
    rig.sensor.on_call = lambda n: send(rig.executor, fp.make_emergency_land_all()) if n == 6 else None
    start(rig, DEMO)
    assert wait_for(lambda: not rig.drone.state.is_flying and not rig.executor.is_busy)
    assert rig.timeline[-2:] == [("land",), ("sense", "clear")]
    assert at(rig.executor.envelope.pose_snapshot())[:2] == (50.0, 150.0)
    assert rig.executor._land_reason == "emergency_land_all"
    assert rig.executor._resume_target is None
    assert rig.executor.obstacle_cuts == []
    assert_invariants(rig)


def test_link_loss_during_a_relook_parks_the_drone_and_the_link_coming_back_gates_the_step_again(rigs):
    rig = rigs(**AVOID)
    rig.sensor.queue(SenseResult.make_blocked())
    rig.sensor.on_call = lambda n: rig.executor._begin_link_hold() if n == 2 else None
    start(rig, LANES)
    assert wait_for(lambda: not rig.executor.is_busy and rig.executor._link_parked)
    assert lane_moves(rig) == [] and rig.executor.obstacle_cuts == []
    rig.executor._end_link_hold()
    assert wait_for(lambda: finished(rig))
    assert rig.executor.obstacle_cuts == []
    assert at(assert_invariants(rig)) == (100.0, 0.0, 180.0)
    assert_back_on_the_plan(rig)
    assert_never_landed(rig)


def test_link_loss_during_a_sidestep_flies_back_to_the_lane_before_the_plan_goes_on(rigs):
    rig = rigs([POLE], **SIDESTEP_ONLY)
    rig.sensor.on_call = lambda n: rig.executor._begin_link_hold() if n == 6 else None
    start(rig, DEMO)
    assert wait_for(lambda: not rig.executor.is_busy and rig.executor._link_parked)
    # Parked beside the lane; the point on the lane is where the plan continues.
    assert at(rig.executor.envelope.pose_snapshot())[:2] == (50.0, 150.0)
    target = rig.executor._resume_target
    assert (target.x_cm, target.y_cm, target.heading_deg) == (0.0, 150.0, 0.0)
    rig.sensor.on_call = None
    rig.executor._end_link_hold()
    assert wait_for(lambda: finished(rig))
    assert rig.executor.obstacle_cuts == []
    assert at(assert_invariants(rig)) == (200.0, 400.0, 0.0)
    assert counts(rig)[2:] == (2, 1)  # the second attempt got past
    assert_back_on_the_plan(rig)
    assert_never_landed(rig)


def test_a_manual_hold_during_a_sidestep_keeps_the_point_on_the_lane_as_the_hold_point(rigs):
    rig = rigs([POLE], **SIDESTEP_ONLY)
    rig.sensor.on_call = (
        lambda n: send(rig.executor, fp.make_manual_hold(cmd_id="m1", fence=FENCE)) if n == 6 else None
    )
    start(rig, DEMO)
    assert wait_for(lambda: reply_for(rig.sent, "m1") is not None and not rig.executor.is_busy)
    hold_point = rig.executor.manual_state.hold_pose
    assert (hold_point.x_cm, hold_point.y_cm, hold_point.heading_deg) == (0.0, 150.0, 0.0)
    assert rig.executor.manual_state.hold_step == 3
    rig.sensor.on_call = None
    send(rig.executor, fp.make_resume_search(cmd_id="m2", fence=FENCE))
    assert answered(rig, "m2")["type"] == "ack"
    assert wait_for(lambda: finished(rig))
    assert rig.executor.obstacle_cuts == []
    assert at(assert_invariants(rig)) == (200.0, 400.0, 0.0)
    assert_never_landed(rig)


@pytest.mark.parametrize("settings", [
    {},
    AVOID,
    dict(AVOID, obstacle_clear_votes=2),
    SIDESTEP_ONLY,
])
def test_a_blocked_reading_never_lands_the_drone(rigs, settings):
    rig = rigs([BlockedZone(heading_deg=None)], **settings)
    fly(rig, DEMO)
    # Everything is blocked: every lane is cut where it starts and the drone hovers where it took off.
    assert lane_moves(rig) == []
    assert {cut.reason for cut in rig.executor.obstacle_cuts} == {CUT_OBSTACLE}
    assert at(assert_invariants(rig))[:2] == (0.0, 0.0)
    assert_never_landed(rig)


# ---------------------------------------------------------------------------
# Telemetry, settings and validation
# ---------------------------------------------------------------------------


def test_the_avoid_field_reports_the_sidestep_and_what_the_relook_did(rigs):
    assert TELEMETRY_AVOID not in rigs().executor.obstacle_status()
    assert TELEMETRY_AVOID not in rigs(obstacle_clear_votes=3).executor.obstacle_status()
    # An edge that may sidestep says so from the start, so the ground keeps its sidestep margin.
    rig = rigs(**AVOID)
    assert rig.executor.obstacle_status()[TELEMETRY_AVOID] == dict(NOTHING_YET, sidestep_cm=50)
    assert TELEMETRY_AVOID in rig.executor.telemetry()
    # One that only looks again leaves the field out until the plan has something to report.
    relook_only = rigs(obstacle_relook=2)
    assert TELEMETRY_AVOID not in relook_only.executor.obstacle_status()
    relook_only.sensor.queue(SenseResult.make_blocked())
    fly(relook_only, LANES)
    assert relook_only.executor.obstacle_status()[TELEMETRY_AVOID] == {
        "relooks": 1, "relook_clears": 1, "sidesteps": 0, "sidestep_passes": 0, "phase": None, "sidestep_cm": 0,
    }


def test_the_counts_restart_with_a_plan_that_does_not_continue_the_current_one(rigs):
    rig = rigs(**AVOID)
    rig.sensor.queue(SenseResult.make_blocked())
    fly(rig, LANES)
    assert counts(rig) == (1, 1, 0, 0)
    send(rig.executor, fp.make_assign_lanes("drone-1", 100, lanes_of(("left", 50),), {"x_cm": 0, "y_cm": 0,
                                            "w_cm": 400, "h_cm": 400}, fence=FENCE, plan_id="p-2", plan_seq=2,
                                            cmd_id="a-p-2"))
    assert answered(rig, "a-p-2")["type"] == "ack"
    assert counts(rig) == (0, 0, 0, 0)


def test_the_worst_case_telemetry_with_the_avoid_field_stays_under_the_cap():
    from tests.test_obstacle_h6_fixes import _CueSensor, _envelope
    from tests.test_telemetry_field_cap import _CUE, _GpsTello, _runner

    drone = _GpsTello(name="drone-1")
    drone.connect()
    ex = LaneExecutor(drone, _envelope(), "drone-1", send=lambda m: None, obstacle_sensor=_CueSensor(_CUE),
                      **AVOID)
    ex._v2_telemetry = True
    ex._obstacle_state, ex._obstacle_latency_ms = "clear", 210.0
    # The worst case: every count, a phase and the most offsets listed, next to eight cuts.
    ex._avoid_counts = {"relooks": 9, "relook_clears": 3, "sidesteps": 4, "sidestep_passes": 2}
    ex._avoid_phase = "sidestep"
    ex._avoid_offsets = [[lane, 150, 150, "person" if lane == 5 else ""] for lane in range(6)]
    ex._cuts = [Cut(lane, 250, CUT_OBSTACLE) for lane in range(9)]
    sent = []
    edge_device._with_profile_status(sent.append, _runner(True))(ex.telemetry())
    msg = sent[0]
    assert {TELEMETRY_AVOID, TELEMETRY_PERSON_CUE, "lat"} <= set(msg)
    assert len(msg[TELEMETRY_AVOID]["offsets"]) == 3 * MAX_OFFSETS_LISTED
    assert msg[TELEMETRY_AVOID]["offsets_seen"] == ["", "", "", "person"]
    assert len(msg) <= fp.MAX_FIELDS - 5, len(msg)
    # PR #198's ``temp_preflight`` is never in ``msg``: a MAVLink drone sends no temperature (a Tello does,
    # but has no GPS, two fields fewer). Fields another open branch adds once merged: the incident branch's
    # ``move_refusals_total`` and ``move_refusal_last``. Together they still leave headroom.
    pending = 2
    assert len(msg) + pending <= fp.MAX_FIELDS - 3, len(msg) + pending
    assert fp.validate_message(fp.shape_for_peer(msg, 2), proto=2) == "telemetry"


def _settings(**env):
    return ObstacleSettings("tello", SafetyEnvelope.from_env(alt_slot_cm=100, environ={}), env)


def test_the_edge_defaults_are_two_votes_two_relooks_and_two_50_cm_sidesteps():
    settings = _settings()
    assert settings.avoid_kwargs() == {
        "obstacle_clear_votes": 2, "obstacle_relook": 2, "obstacle_sidestep_cm": 50, "obstacle_sidesteps": 2,
        "obstacle_avoid_budget_s": 45.0,
    }
    assert settings.non_default == []


def test_the_edge_reads_and_reports_the_avoid_settings():
    settings = _settings(**{
        ENV_OBSTACLE_CLEAR_VOTES: "1", ENV_OBSTACLE_RELOOK: "0", ENV_OBSTACLE_SIDESTEP_CM: "0",
        ENV_OBSTACLE_SIDESTEPS: "4", ENV_OBSTACLE_AVOID_BUDGET_S: "90",
    })
    assert settings.avoid_kwargs() == {
        "obstacle_clear_votes": 1, "obstacle_relook": 0, "obstacle_sidestep_cm": 0, "obstacle_sidesteps": 4,
        "obstacle_avoid_budget_s": 90.0,
    }
    assert {name for name, _, _ in settings.non_default} == {
        ENV_OBSTACLE_CLEAR_VOTES, ENV_OBSTACLE_RELOOK, ENV_OBSTACLE_SIDESTEP_CM, ENV_OBSTACLE_SIDESTEPS,
        ENV_OBSTACLE_AVOID_BUDGET_S,
    }
    assert _settings(**{ENV_OBSTACLE_SIDESTEP_CM: "100"}).sidestep_cm == 100
    assert _settings(**{ENV_OBSTACLE_SIDESTEP_CM: "20"}).sidestep_cm == 20


@pytest.mark.parametrize(("name", "raw"), [
    (ENV_OBSTACLE_CLEAR_VOTES, "0"),
    (ENV_OBSTACLE_CLEAR_VOTES, "6"),
    (ENV_OBSTACLE_CLEAR_VOTES, "2.0"),
    (ENV_OBSTACLE_RELOOK, "6"),
    (ENV_OBSTACLE_RELOOK, "-1"),
    (ENV_OBSTACLE_SIDESTEP_CM, "10"),
    (ENV_OBSTACLE_SIDESTEP_CM, "101"),
    (ENV_OBSTACLE_SIDESTEP_CM, "fifty"),
    (ENV_OBSTACLE_SIDESTEPS, "5"),
    (ENV_OBSTACLE_AVOID_BUDGET_S, "9"),
    (ENV_OBSTACLE_AVOID_BUDGET_S, "301"),
    (ENV_OBSTACLE_AVOID_BUDGET_S, "nan"),
])
def test_a_bad_avoid_setting_refuses_to_start(name, raw):
    with pytest.raises(SystemExit, match=name):
        _settings(**{name: raw})


def test_the_executor_defaults_fly_as_before_and_bad_values_are_refused():
    envelope = SafetyEnvelope(alt_slot_cm=100, geofence_cm=500)
    executor = LaneExecutor(_drone(), envelope, "drone-1", send=lambda m: None)
    assert (executor.obstacle_clear_votes, executor.obstacle_relook, executor.obstacle_sidestep_cm,
            executor.obstacle_sidesteps) == (1, 0, 0, 0)
    for bad in ({"obstacle_clear_votes": 0}, {"obstacle_clear_votes": 6}, {"obstacle_relook": 6},
                {"obstacle_sidestep_cm": 10}, {"obstacle_sidesteps": 5}, {"obstacle_relook": True},
                {"obstacle_sidestep_cm": 50.0}, {"obstacle_avoid_budget_s": 9.9},
                {"obstacle_avoid_budget_s": 300.5}, {"obstacle_avoid_budget_s": float("inf")},
                {"obstacle_avoid_budget_s": True}, {"obstacle_settle_s": 0.05}):
        with pytest.raises(ValueError):
            LaneExecutor(_drone(), envelope, "drone-1", send=lambda m: None, **bad)
