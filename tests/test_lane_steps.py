"""The pure lane-step arithmetic shared by the edge and the ground (``flightrisk/drone/lane_steps.py``).

Covers the split at step size, the headings and shortest rotation, the
deficit rule (the worked example of design section 3.3, never lengthen,
never re-enter, skip below 20 cm, same-direction runs, ``sensor_down``), the
replay of uncovered segments from ``(lane, flown)`` pairs and the telemetry
wire form of the cuts.
"""
from __future__ import annotations

import itertools

import pytest

from flightrisk.drone import lane_executor, lane_plan
from flightrisk.drone.lane_steps import (
    PLAN_HEADINGS,
    Cut,
    UncoveredSegment,
    apply_cut_deficit,
    decode_cuts,
    encode_cuts,
    lane_targets,
    plan_heading,
    replay_lanes,
    shortest_rotation,
    split_lane_moves,
    split_steps,
    uncovered_segments,
)
from flightrisk.drone.obstacle_sensor import (
    CUT_AUTOPILOT,
    CUT_OBSTACLE,
    CUT_PERSON,
    CUT_SENSOR_DOWN,
    CUT_SENSOR_STALE,
    MAX_CUTS_LISTED,
    SEGMENT_SKIPPED,
)
from flightrisk.drone.safety import RelativePose

#: The demo plan of design section 3.3 (``forward 400, right 100, back 400, right 100, forward 400``).
DEMO = [("forward", 400), ("right", 100), ("back", 400), ("right", 100), ("forward", 400)]


def planned_end(lanes):
    x = y = 0
    for direction, cm in lanes:
        dx, dy = {"forward": (0, 1), "back": (0, -1), "right": (1, 0), "left": (-1, 0)}[direction]
        x, y = x + dx * cm, y + dy * cm
    return x, y


def seg(segment: UncoveredSegment):
    """A segment as ``(lane, (x, y), (end_x, end_y), reason)`` for compact assertions."""
    return segment.lane_index, (segment.x_cm, segment.y_cm), (segment.end_x_cm, segment.end_y_cm), segment.reason


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------


def test_split_lane_moves_is_reexported_unchanged():
    assert lane_plan.split_lane_moves is split_lane_moves
    assert lane_executor.split_lane_moves is split_lane_moves
    assert split_lane_moves(400) == [400]
    assert split_lane_moves(600) == [300, 300]
    assert split_lane_moves(1001) == [334, 334, 333]
    with pytest.raises(ValueError):
        split_lane_moves(19)


@pytest.mark.parametrize("step", [40, 50, 75, 100])
def test_split_steps_every_step_is_between_20_cm_and_the_step(step):
    for total in range(20, 1201):
        steps = split_steps(total, step)
        assert sum(steps) == total
        assert all(20 <= part <= step for part in steps), (total, steps)
        assert max(steps) - min(steps) <= 1


def test_split_steps_at_50_cm():
    assert split_steps(400, 50) == [50] * 8
    assert split_steps(250, 50) == [50] * 5
    assert split_steps(130, 50) == [44, 43, 43]
    assert split_steps(51, 50) == [26, 25]
    assert split_steps(20, 50) == [20]


def test_split_steps_never_makes_a_step_under_the_minimum_move():
    # 21 cm in 20 cm steps cannot be split into flyable steps of at most
    # 20 cm: it becomes one 21 cm step instead of 11 + 10.
    assert split_steps(21, 20) == [21]
    assert split_steps(45, 20) == [23, 22]
    for step, total in itertools.product(range(20, 40), range(20, 400)):
        parts = split_steps(total, step)
        assert sum(parts) == total and min(parts) >= 20, (step, total, parts)
        assert max(parts) < step + 20


def test_split_steps_with_the_field_profile():
    assert split_steps(1200, 500, 20) == [400, 400, 400]


# ---------------------------------------------------------------------------
# Headings
# ---------------------------------------------------------------------------


def test_plan_heading_for_each_direction():
    assert [plan_heading(d) for d in ("forward", "right", "back", "left")] == [0.0, 90.0, 180.0, 270.0]
    assert PLAN_HEADINGS["back"] == 180.0
    with pytest.raises(ValueError):
        plan_heading("up")


def test_plan_heading_faces_the_plan_direction():
    # A body "forward" move at the plan heading of a direction lands where
    # the plan direction at heading 0 lands.
    for direction in ("forward", "right", "back", "left"):
        body = RelativePose(10, 20, plan_heading(direction))
        plan = RelativePose(10, 20, 0.0)
        body.apply_move("forward", 50)
        plan.apply_move(direction, 50)
        assert body.x_cm == pytest.approx(plan.x_cm) and body.y_cm == pytest.approx(plan.y_cm)


@pytest.mark.parametrize("start,target,expected", [
    (0, 0, 0),
    (0, 90, 90),
    (90, 0, -90),
    (0, 270, -90),
    (270, 0, 90),
    (90, 180, 90),
    (180, 90, -90),
    (0, 180, -180),
    (180, 0, -180),
    (120, 90, -30),
    (350, 10, 20),
    (10, 350, -20),
])
def test_shortest_rotation(start, target, expected):
    assert shortest_rotation(start, target) == expected
    assert (start + shortest_rotation(start, target) - target) % 360 == 0


# ---------------------------------------------------------------------------
# The deficit rule
# ---------------------------------------------------------------------------


def test_apply_cut_deficit_shortens_a_lane_that_flies_back_toward_the_grid():
    # A forward lane cut 150 cm short leaves the drone 150 cm behind on y.
    assert apply_cut_deficit("back", 400, (0, -150)) == (250, (0, 0))
    # A right lane cut 120 cm short leaves the drone 120 cm left of the grid.
    assert apply_cut_deficit("left", 300, (-120, 0)) == (180, (0, 0))
    assert apply_cut_deficit("right", 300, (120, 0)) == (180, (0, 0))
    assert apply_cut_deficit("forward", 300, (0, 120)) == (180, (0, 0))


def test_apply_cut_deficit_never_lengthens_a_lane():
    # Another forward lane would have to fly 150 cm further to make up the
    # gap, into the cut: it is flown as planned and the deficit stays.
    assert apply_cut_deficit("forward", 400, (0, -150)) == (400, (0, -150))
    assert apply_cut_deficit("back", 400, (0, 150)) == (400, (0, 150))
    assert apply_cut_deficit("left", 300, (120, 0)) == (300, (120, 0))
    for direction, gap in itertools.product(("forward", "back", "left", "right"), range(-500, 501, 37)):
        for deficit in ((gap, 0), (0, gap), (gap, gap)):
            flown, _ = apply_cut_deficit(direction, 400, deficit)
            assert 0 <= flown <= 400


def test_apply_cut_deficit_leaves_the_other_axis_alone():
    assert apply_cut_deficit("right", 100, (0, -150)) == (100, (0, -150))
    assert apply_cut_deficit("back", 400, (-30, -150)) == (250, (-30, 0))


def test_apply_cut_deficit_skips_a_lane_shortened_below_20_cm():
    assert apply_cut_deficit("back", 400, (0, -381)) == (0, (0, 19))
    assert apply_cut_deficit("back", 400, (0, -380)) == (20, (0, 0))
    # A gap longer than the lane is partly used up by skipping it.
    assert apply_cut_deficit("back", 300, (0, -400)) == (0, (0, -100))


def test_apply_cut_deficit_without_a_deficit():
    for direction in ("forward", "back", "left", "right"):
        assert apply_cut_deficit(direction, 123, (0, 0)) == (123, (0, 0))
    with pytest.raises(ValueError):
        apply_cut_deficit("up", 100, (0, 0))


# ---------------------------------------------------------------------------
# Replay and uncovered segments
# ---------------------------------------------------------------------------


def test_worked_example_of_the_design():
    """Cut at 250 cm on lane 0 of the demo plan (design section 3.3)."""
    replay = replay_lanes(DEMO, [(0, 250)])
    assert replay.targets == (400, 100, 250, 100, 400)
    assert [seg(s) for s in replay.segments] == [
        (0, (0, 250), (0, 400), CUT_OBSTACLE),
        (2, (100, 250), (100, 400), SEGMENT_SKIPPED),
    ]
    # Lane 2 reaches its planned end, so the drone ends on the planned grid.
    assert replay.position == planned_end(DEMO) == (200, 400)
    assert replay.deficit == (0, 0)
    assert all(s.cut == 0 for s in replay.segments)
    assert replay.segments[0].length_cm == 150 and replay.segments[0].direction == "forward"


def test_replay_accepts_assign_lanes_mappings_and_cut_objects():
    lanes = [{"direction": d, "distance_cm": cm} for d, cm in DEMO]
    assert uncovered_segments(lanes, [Cut(0, 250, CUT_OBSTACLE)]) == uncovered_segments(DEMO, [(0, 250)])
    assert lane_targets(lanes, [(0, 250, CUT_PERSON)]) == [400, 100, 250, 100, 400]
    assert uncovered_segments(lanes, [(0, 250, CUT_PERSON)])[0].reason == CUT_PERSON


def test_no_cuts_means_nothing_uncovered():
    replay = replay_lanes(DEMO)
    assert replay.targets == (400, 100, 400, 100, 400)
    assert replay.segments == ()
    assert replay.position == planned_end(DEMO)


def test_a_cut_skips_the_rest_of_a_same_direction_run():
    # The coordinator splits a 600 cm leg into two 300 cm lanes.
    lanes = [("forward", 300), ("forward", 300), ("right", 100), ("back", 300), ("back", 300)]
    replay = replay_lanes(lanes, [(0, 200)])
    assert replay.targets == (300, 0, 100, 0, 200)
    assert [seg(s) for s in replay.segments] == [
        (0, (0, 200), (0, 300), CUT_OBSTACLE),
        (1, (0, 300), (0, 600), CUT_OBSTACLE),
        # 400 cm behind: lane 3 is skipped entirely (its planned span is
        # y 600 down to 300), lane 4 is shortened to reach its planned end.
        (3, (100, 300), (100, 600), SEGMENT_SKIPPED),
        (4, (100, 200), (100, 300), SEGMENT_SKIPPED),
    ]
    assert replay.position == planned_end(lanes) == (100, 0)


def test_a_cut_does_not_lengthen_a_later_lane_in_the_cut_direction():
    lanes = [("forward", 300), ("right", 100), ("forward", 300)]
    replay = replay_lanes(lanes, [(0, 100)])
    # Lane 2 is not lengthened to 500: the drone stays 200 cm behind.
    assert replay.targets == (300, 100, 300)
    assert replay.position == (100, 400)
    assert replay.deficit == (0, -200)
    assert [seg(s) for s in replay.segments] == [(0, (0, 100), (0, 300), CUT_OBSTACLE)]


def test_a_cut_at_the_first_step_leaves_the_whole_lane_uncovered():
    replay = replay_lanes(DEMO, [(0, 0)])
    assert [seg(s) for s in replay.segments][0] == (0, (0, 0), (0, 400), CUT_OBSTACLE)
    # Lane 2 would have to fly back 0 cm: it is skipped entirely.
    assert replay.targets == (400, 100, 0, 100, 400)
    assert [seg(s) for s in replay.segments][1] == (2, (100, 0), (100, 400), SEGMENT_SKIPPED)
    assert replay.position == (200, 400)


def test_a_lane_shortened_below_20_cm_is_skipped():
    lanes = [("forward", 400), ("right", 100), ("back", 400), ("right", 100), ("forward", 400)]
    # Cut 390 cm short of the 400: lane 2 would fly 10 cm, so it is skipped,
    # and lane 4 starts 10 cm ahead and is shortened by those 10 cm.
    replay = replay_lanes(lanes, [(0, 10)])
    assert replay.targets == (400, 100, 0, 100, 390)
    assert replay.position == planned_end(lanes)
    reasons = [(s.lane_index, s.reason) for s in replay.segments]
    assert reasons == [(0, CUT_OBSTACLE), (2, SEGMENT_SKIPPED), (4, SEGMENT_SKIPPED)]


def test_sensor_down_ends_every_later_lane():
    replay = replay_lanes(DEMO, [(1, 50, CUT_SENSOR_DOWN)])
    assert replay.targets == (400, 100, 0, 0, 0)
    assert [seg(s) for s in replay.segments] == [
        (1, (50, 400), (100, 400), CUT_SENSOR_DOWN),
        (2, (100, 400), (100, 0), CUT_SENSOR_DOWN),
        (3, (100, 0), (200, 0), CUT_SENSOR_DOWN),
        (4, (200, 0), (200, 400), CUT_SENSOR_DOWN),
    ]
    # Appended lanes after a sensor_down are not flown either, and a cut
    # recorded for one of them is ignored.
    assert replay_lanes(DEMO + [("left", 100)], [(1, 50, CUT_SENSOR_DOWN), (5, 20)]).targets[-1] == 0


def test_sensor_down_after_a_cut_reports_the_remaining_lanes_where_they_are_planned():
    # Lane 0 is cut at 250 (the drone is 150 cm behind on y) and the sensor
    # goes down 50 cm into lane 1. Lane 1's own remainder is where the drone
    # flies it, at y 250; lanes 2 to 4 are never flown, so they are the
    # planned lanes (design section 4: "the remaining lanes become uncovered
    # segments"), not lanes shifted 150 cm back and out of the zone.
    replay = replay_lanes(DEMO, [(0, 250), (1, 50, CUT_SENSOR_DOWN)])
    assert replay.targets == (400, 100, 0, 0, 0)
    assert [seg(s) for s in replay.segments] == [
        (0, (0, 250), (0, 400), CUT_OBSTACLE),
        (1, (50, 250), (100, 250), CUT_SENSOR_DOWN),
        (2, (100, 400), (100, 0), CUT_SENSOR_DOWN),
        (3, (100, 0), (200, 0), CUT_SENSOR_DOWN),
        (4, (200, 0), (200, 400), CUT_SENSOR_DOWN),
    ]
    assert [s.cut for s in replay.segments] == [0, 1, 1, 1, 1]
    # The drone stays where it stopped; nothing after the cut is flown.
    assert replay.position == (50, 250)
    # Every ended lane lies on its planned span.
    starts = [planned_end(DEMO[:k]) for k in range(len(DEMO) + 1)]
    for segment in replay.segments[2:]:
        assert (segment.x_cm, segment.y_cm) == starts[segment.lane_index]
        assert (segment.end_x_cm, segment.end_y_cm) == starts[segment.lane_index + 1]


def test_two_cuts_attribute_their_shadows():
    lanes = [("forward", 400), ("right", 100), ("back", 400), ("right", 100), ("forward", 400), ("right", 100),
             ("back", 400)]
    replay = replay_lanes(lanes, [(0, 250), (4, 300, CUT_SENSOR_STALE)])
    assert replay.targets == (400, 100, 250, 100, 400, 100, 300)
    assert [(s.lane_index, s.reason, s.cut) for s in replay.segments] == [
        (0, CUT_OBSTACLE, 0),
        (2, SEGMENT_SKIPPED, 0),
        (4, CUT_SENSOR_STALE, 1),
        (6, SEGMENT_SKIPPED, 1),
    ]
    assert replay.position == planned_end(lanes)


def test_sideways_cut_and_left_lanes():
    lanes = [("right", 300), ("forward", 100), ("left", 300)]
    replay = replay_lanes(lanes, [(0, 120, CUT_AUTOPILOT)])
    assert replay.targets == (300, 100, 120)
    assert [seg(s) for s in replay.segments] == [
        (0, (120, 0), (300, 0), CUT_AUTOPILOT),
        (2, (120, 100), (300, 100), SEGMENT_SKIPPED),
    ]
    assert replay.position == (0, 100)


def test_replay_ignores_invalid_and_repeated_cuts():
    assert replay_lanes(DEMO, [(9, 10), (-1, 10)]).segments == ()
    # Only the first cut of a lane counts, and a flown length is clamped to the lane.
    assert [seg(s) for s in replay_lanes(DEMO, [(0, 250), (0, 100)]).segments][0] == (0, (0, 250), (0, 400), "obstacle")
    assert replay_lanes(DEMO, [(0, 900)]).segments == ()
    # A cut of a lane that is skipped (part of a run) is ignored.
    lanes = [("forward", 300), ("forward", 300), ("right", 100)]
    assert len(replay_lanes(lanes, [(0, 100), (1, 50)]).segments) == 2


def test_uncovered_segments_round_trip_through_the_wire_form():
    cuts = [Cut(0, 250, CUT_OBSTACLE), Cut(4, 300, CUT_PERSON)]
    flat, reasons = encode_cuts(cuts)
    assert flat == [0, 250, 4, 300] and reasons == "obstacle,person"
    lanes = DEMO + [("right", 100), ("back", 400)]
    assert uncovered_segments(lanes, decode_cuts(flat, reasons)) == uncovered_segments(lanes, cuts)


def test_segment_as_dict():
    segment = uncovered_segments(DEMO, [(0, 250)])[0]
    assert segment.as_dict() == {
        "lane_index": 0, "direction": "forward", "x_cm": 0, "y_cm": 250, "end_x_cm": 0, "end_y_cm": 400,
        "length_cm": 150, "reason": "obstacle", "cut": 0,
    }


# ---------------------------------------------------------------------------
# Telemetry wire form
# ---------------------------------------------------------------------------


def test_encode_cuts_keeps_the_last_eight_within_the_privacy_screen():
    cuts = [Cut(k, 10 * k, CUT_SENSOR_STALE if k % 2 else CUT_OBSTACLE) for k in range(11)]
    flat, reasons = encode_cuts(cuts)
    assert len(flat) == 2 * MAX_CUTS_LISTED == 16
    assert flat[:2] == [3, 30] and flat[-2:] == [10, 100]
    assert reasons.split(",") == [c.reason for c in cuts[-8:]]
    assert len(reasons) <= 200
    assert encode_cuts([]) == ([], "")


def test_decode_cuts_tolerates_bad_input():
    assert decode_cuts(None) == []
    assert decode_cuts([1, 2, 3], "person") == [Cut(1, 2, CUT_PERSON)]
    assert decode_cuts([1, 2, 3, 4], "nonsense,sensor_down") == [Cut(1, 2, CUT_OBSTACLE), Cut(3, 4, CUT_SENSOR_DOWN)]
    assert decode_cuts([True, 2, "x", 4, 5, 6.4]) == [Cut(5, 6, CUT_OBSTACLE)]
    assert decode_cuts([float("nan"), 1]) == []
