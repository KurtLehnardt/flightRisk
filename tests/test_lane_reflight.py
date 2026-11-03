"""The lanes that re-fly one lane cut's segments (design section 3.4), as pure functions.

``lane_reflight`` turns the segments a cut left unsearched (from
``lane_steps.uncovered_segments``) into the lanes another drone appends:
the cut's own segments reversed, then each ``skipped`` shadow from its
nearer end, joined by the coordinator's transit, with segments under the
20 cm minimum move dropped as ``too_short``. No drone or clock is involved.
"""
from __future__ import annotations

import pytest

from flightrisk.drone import coordinator as coordinator_module
from flightrisk.drone import lane_reflight
from flightrisk.drone.lane_reflight import (
    plan_reflight,
    reflight_lanes,
    split_too_short,
    transit,
)
from flightrisk.drone.lane_steps import Cut, UncoveredSegment, uncovered_segments
from flightrisk.drone.obstacle_sensor import CUT_OBSTACLE, CUT_PERSON, SEGMENT_SKIPPED
from flightrisk.drone.safety import MIN_MOVE_CM

# The demo plan of design section 3.3.
DEMO = [("forward", 400), ("right", 100), ("back", 400), ("right", 100), ("forward", 400)]


def lane(direction, cm):
    return {"direction": direction, "distance_cm": cm}


def seg(lane_index, direction, p, e, reason=CUT_OBSTACLE, cut=0):
    """A segment from P (next to flown ground) to E (the far end)."""
    length = abs(e[0] - p[0]) + abs(e[1] - p[1])
    return UncoveredSegment(lane_index, direction, p[0], p[1], e[0], e[1], length, reason, cut)


def walk(start, lanes):
    """Every point reached flying *lanes* from *start*."""
    unit = {"forward": (0, 1), "back": (0, -1), "right": (1, 0), "left": (-1, 0)}
    points = [start]
    for move in lanes:
        dx, dy = unit[move["direction"]]
        x, y = points[-1]
        points.append((x + dx * move["distance_cm"], y + dy * move["distance_cm"]))
    return points


# ---------------------------------------------------------------------------
# The worked example
# ---------------------------------------------------------------------------


def test_worked_example_of_the_design():
    segments = uncovered_segments(DEMO, [Cut(0, 250, CUT_OBSTACLE)])
    lanes, points = reflight_lanes(segments)
    assert lanes == [lane("back", 150), lane("right", 100), lane("forward", 150)]
    assert points == [(0, 400), (0, 250), (100, 250), (100, 400)]


def test_the_points_follow_the_lanes():
    segments = uncovered_segments(DEMO, [Cut(0, 250, CUT_OBSTACLE)])
    lanes, points = reflight_lanes(segments)
    assert walk(points[0], lanes) == points


def test_the_plan_keeps_which_segment_each_lane_re_flies():
    segments = uncovered_segments(DEMO, [Cut(0, 250, CUT_OBSTACLE)])
    plan = plan_reflight(segments)
    # The reversed obstacle segment, a transit between the two, then the shadow.
    assert plan.legs == (0, None, 1)
    assert [s.lane_index for s in plan.segments] == [0, 2]
    assert [s.reason for s in plan.segments] == [CUT_OBSTACLE, SEGMENT_SKIPPED]
    assert plan.too_short == ()


# ---------------------------------------------------------------------------
# The cut's own segments are flown reversed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("direction, p, e, flown", [
    ("forward", (0, 250), (0, 400), "back"),
    ("back", (100, 300), (100, 0), "forward"),
    ("right", (120, 50), (300, 50), "left"),
    ("left", (300, 50), (120, 50), "right"),
])
def test_the_obstacle_segment_is_flown_from_its_far_end_toward_the_cut(direction, p, e, flown):
    lanes, points = reflight_lanes([seg(0, direction, p, e)])
    assert lanes == [lane(flown, abs(e[0] - p[0]) + abs(e[1] - p[1]))]
    assert points == [e, p]


def test_a_same_direction_run_is_flown_back_from_the_end_of_the_run():
    # forward 500 then forward 400, cut 300 cm into the first: the run's two segments.
    lanes_plan = [("forward", 500), ("forward", 400), ("right", 100), ("back", 900)]
    segments = uncovered_segments(lanes_plan, [Cut(0, 300, CUT_PERSON)])
    plan = plan_reflight(segments)
    assert list(plan.lanes) == [lane("back", 400), lane("back", 200), lane("right", 100), lane("forward", 600)]
    assert list(plan.points) == [(0, 900), (0, 500), (0, 300), (100, 300), (100, 900)]
    # The second lane of the run is flown first, then the cut lane, then the shadow.
    assert [s.lane_index for s in plan.segments] == [1, 0, 3]
    assert plan.legs == (0, 1, None, 2)


def test_own_segments_are_ordered_by_lane_whatever_order_they_come_in():
    first = seg(0, "forward", (0, 300), (0, 500))
    second = seg(1, "forward", (0, 500), (0, 900))
    assert reflight_lanes([second, first]) == reflight_lanes([first, second])


# ---------------------------------------------------------------------------
# Shadows: plan order, nearer end, transits
# ---------------------------------------------------------------------------


def test_each_shadow_starts_at_its_end_nearer_to_where_the_re_flight_is():
    own = seg(0, "forward", (0, 250), (0, 400))
    # The re-flight is at (0, 250) after the reversed obstacle segment; this
    # shadow's far end (100, 300) is nearer than its end next to flown ground.
    shadow = seg(2, "back", (100, 400), (100, 300), SEGMENT_SKIPPED)
    lanes, points = reflight_lanes([own, shadow])
    # From (0, 250): (100, 300) is 150 cm of transit away, (100, 400) is 250 cm.
    assert lanes == [lane("back", 150), lane("forward", 50), lane("right", 100), lane("forward", 100)]
    assert points == [(0, 400), (0, 250), (0, 300), (100, 300), (100, 400)]


def test_on_a_tie_the_shadow_starts_at_its_end_next_to_flown_ground():
    own = seg(0, "forward", (0, 250), (0, 400))
    # Both ends of this shadow are 150 cm of transit from (0, 250).
    shadow = seg(2, "right", (50, 150), (50, 350), SEGMENT_SKIPPED)
    _, points = reflight_lanes([own, shadow])
    assert points[-1] == (50, 350)


def test_shadows_follow_in_plan_order_joined_by_transits():
    own = seg(0, "forward", (0, 300), (0, 400))
    later = seg(4, "back", (200, 300), (200, 400), SEGMENT_SKIPPED)
    earlier = seg(2, "back", (100, 300), (100, 400), SEGMENT_SKIPPED)
    plan = plan_reflight([own, later, earlier])
    assert [s.lane_index for s in plan.segments] == [0, 2, 4]
    assert list(plan.lanes) == [
        lane("back", 100),                         # (0, 400) -> (0, 300)
        lane("right", 100),                        # transit -> (100, 300)
        lane("forward", 100),                      # shadow of lane 2 -> (100, 400)
        lane("right", 100),                        # transit -> (200, 400)
        lane("back", 100),                         # shadow of lane 4, from its nearer end -> (200, 300)
    ]
    assert plan.legs == (0, None, 1, None, 2)
    assert walk(plan.points[0], plan.lanes) == list(plan.points)


def test_a_transit_between_segments_moves_forward_or_back_first():
    own = seg(0, "right", (0, 100), (200, 100))
    shadow = seg(3, "forward", (300, 300), (300, 500), SEGMENT_SKIPPED)
    lanes, points = reflight_lanes([own, shadow])
    # Reversed: (200, 100) -> (0, 100); then forward 200, then right 300, to (300, 300).
    assert lanes == [lane("left", 200), lane("forward", 200), lane("right", 300), lane("forward", 200)]
    assert points[-1] == (300, 500)


def test_shadows_without_an_own_segment_start_at_the_end_next_to_flown_ground():
    shadow = seg(2, "back", (100, 250), (100, 400), SEGMENT_SKIPPED)
    lanes, points = reflight_lanes([shadow])
    assert lanes == [lane("forward", 150)]
    assert points == [(100, 250), (100, 400)]


# ---------------------------------------------------------------------------
# Too short
# ---------------------------------------------------------------------------


def test_segments_under_twenty_cm_are_dropped_as_too_short():
    own = seg(0, "forward", (0, 390), (0, 400))           # 10 cm
    shadow = seg(2, "back", (100, 250), (100, 400), SEGMENT_SKIPPED)
    plan = plan_reflight([own, shadow])
    assert plan.too_short == (own,)
    assert list(plan.segments) == [shadow]
    assert list(plan.lanes) == [lane("forward", 150)]
    assert list(plan.points) == [(100, 250), (100, 400)]


def test_the_demo_cut_ten_cm_in_leaves_a_too_short_shadow():
    segments = uncovered_segments(DEMO, [Cut(0, 10, CUT_OBSTACLE)])
    plan = plan_reflight(segments)
    assert [(s.lane_index, s.length_cm) for s in plan.too_short] == [(4, 10)]
    assert [(s.lane_index, s.length_cm) for s in plan.segments] == [(0, 390), (2, 400)]


def test_exactly_the_minimum_move_is_kept():
    kept = seg(0, "forward", (0, 380), (0, 400))
    dropped = seg(1, "forward", (0, 400), (0, 419))
    flyable, too_short = split_too_short([kept, dropped])
    assert MIN_MOVE_CM == 20
    assert flyable == [kept] and too_short == [dropped]


def test_nothing_long_enough_gives_no_lanes():
    plan = plan_reflight([
        seg(0, "forward", (0, 390), (0, 400)),
        seg(2, "back", (100, 390), (100, 400), SEGMENT_SKIPPED),
    ])
    assert plan.lanes == () and plan.points == () and plan.segments == ()
    assert len(plan.too_short) == 2
    assert reflight_lanes([]) == ([], [])


def test_a_segment_off_one_axis_is_refused():
    bad = UncoveredSegment(0, "forward", 0, 0, 50, 50, 100, CUT_OBSTACLE, 0)
    with pytest.raises(ValueError):
        plan_reflight([bad])


# ---------------------------------------------------------------------------
# The transit is the coordinator's
# ---------------------------------------------------------------------------


def test_the_coordinator_transits_with_this_function():
    assert coordinator_module._transit is lane_reflight.transit


@pytest.mark.parametrize("start, end, moves", [
    ((0, 0), (0, 0), []),
    ((0, 0), (0, 400), [lane("forward", 400)]),
    ((550, 0), (0, 400), [lane("forward", 400), lane("left", 275), lane("left", 275)]),
    ((0, 0), (10, -5), [lane("back", 25), lane("forward", 20), lane("right", 30), lane("left", 20)]),
    ((0, 0), (0, 1001), [lane("forward", 334), lane("forward", 334), lane("forward", 333)]),
])
def test_transit_moves_forward_or_back_first_in_tello_sized_moves(start, end, moves):
    assert transit(start, end) == moves
    assert walk(start, moves)[-1] == end
