"""Geometry of map-edited plans: anchor conversion, snapping, lanes, geofence, separation and coverage.

The executor tests fly converted lanes through the real ``LaneExecutor`` on an
auto-finishing fake Tello (``tests/lane_executor_support.py``) and compare the
dead-reckoned trace with the drawn path. No network and no drone are involved.
"""

from __future__ import annotations

import itertools
import math

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone import plan_paths as pp
from flightrisk.drone.geo import (
    PlanAnchor,
    frame_to_latlon,
    haversine_m,
    latlon_to_frame,
)
from flightrisk.drone.lane_plan import parse_lanes
from flightrisk.drone.safety import HORIZONTAL_DIRECTIONS, MAX_MOVE_CM, MIN_MOVE_CM
from tests.lane_executor_support import (
    FENCE,
    ZONE,
    horizontal_moves,
    make_v2_executor,
    reply_for,
    send,
    start_at,
    wait_for,
)

UTAH = (40.6894, -111.7310)


# ---------------------------------------------------------------- anchor


@pytest.mark.parametrize("heading", [0.0, 37.5, 90.0, 180.0, 271.0, 359.9])
@pytest.mark.parametrize("point", [(0, 0), (100, 0), (0, 100), (-250, 430), (4000, -3000)])
def test_frame_latlon_round_trip(heading, point):
    anchor = PlanAnchor(*UTAH, heading)
    lat, lon = frame_to_latlon(anchor, *point)
    x, y = latlon_to_frame(anchor, lat, lon)
    assert x == pytest.approx(point[0], abs=1e-6)
    assert y == pytest.approx(point[1], abs=1e-6)


def test_anchor_heading_points_the_forward_axis():
    # Heading 90: forward is east, right is south.
    anchor = PlanAnchor(*UTAH, 90.0)
    lat, lon = frame_to_latlon(anchor, 0, 10_000)  # 100 m forward
    assert lat == pytest.approx(UTAH[0], abs=1e-9) and lon > UTAH[1]
    assert haversine_m(*UTAH, lat, lon) == pytest.approx(100.0, rel=1e-4)
    lat, lon = frame_to_latlon(anchor, 10_000, 0)  # 100 m right
    assert lat < UTAH[0] and lon == pytest.approx(UTAH[1], abs=1e-9)
    # Heading 0: forward is north.
    lat, lon = frame_to_latlon(PlanAnchor(*UTAH, 0.0), 0, 10_000)
    assert lat > UTAH[0] and lon == pytest.approx(UTAH[1], abs=1e-12)


def test_frame_distances_match_the_great_circle():
    anchor = PlanAnchor(*UTAH, 33.0)
    a = frame_to_latlon(anchor, -20_000, 5_000)
    b = frame_to_latlon(anchor, 30_000, -10_000)
    expected = math.hypot(50_000, 15_000) / 100
    assert haversine_m(*a, *b) == pytest.approx(expected, rel=2e-3)


@pytest.mark.parametrize("bad", [
    {"lat": 91, "lon": 0}, {"lat": 0, "lon": 181}, {"lat": float("nan"), "lon": 0}, {"lat": True, "lon": 0},
    {"lat": 1, "lon": 2, "extra": 1}, ["lat", "lon"], {"lat": "40", "lon": 1},
])
def test_bad_anchors_are_refused(bad):
    with pytest.raises(ValueError):
        PlanAnchor.from_dict(bad)


def test_anchor_heading_is_normalised():
    assert PlanAnchor(1, 2, -90).heading_deg == 270.0
    assert PlanAnchor.from_dict({"lat": 1, "lon": 2}).heading_deg == 0.0


# ---------------------------------------------------------------- snapping


def _polyline_distance(point, drawn):
    return min(pp._distance_to_segment(point, a, b) for a, b in itertools.pairwise(drawn))


def _assert_executable(lanes):
    assert parse_lanes(list(lanes)) == [(lane["direction"], lane["distance_cm"]) for lane in lanes]
    for lane in lanes:
        assert lane["direction"] in HORIZONTAL_DIRECTIONS
        assert isinstance(lane["distance_cm"], int) and MIN_MOVE_CM <= lane["distance_cm"] <= MAX_MOVE_CM


DRAWN = [
    [(0, 0), (0, 400), (75, 400), (75, 0)],
    [(0, 0), (300, 300)],
    [(0, 0), (10, 300)],
    [(0, 0), (100, 37), (-50, 200)],
    [(0, 0), (0, 15), (200, 15)],
    [(5, -7), (-123, 456), (789, 10), (0, 0)],
    [(0, 0), (1200, 0), (1200, 3)],
    [(0, 0), (19, 19), (39, 21), (40, 400)],
    [(0, 0), (-260, -170), (40, 90), (41, 91)],
]


@pytest.mark.parametrize("drawn", DRAWN)
def test_snapped_path_hits_every_vertex_and_stays_within_tolerance(drawn):
    converted = pp.path_to_lanes(drawn)
    _assert_executable(converted.lanes)
    assert pp.trace_lanes(drawn[0], converted.lanes) == list(converted.trace)
    # Every drawn vertex is a traced point, in order.
    positions = list(converted.trace)
    cursor = 0
    for vertex in drawn:
        cursor = positions.index(tuple(vertex), cursor)
    # Every point of the snapped path is within the stated tolerance of the drawn path.
    for point in converted.trace:
        assert _polyline_distance(point, drawn) < pp.SNAP_TOLERANCE_CM
    assert converted.max_deviation_cm < pp.SNAP_TOLERANCE_CM
    assert pp.count_moves(drawn) == len(converted.lanes)


def test_axis_aligned_paths_are_unchanged():
    drawn = [(0, 0), (0, 400), (75, 400), (75, 0)]
    converted = pp.path_to_lanes(drawn)
    assert [dict(lane) for lane in converted.lanes] == [
        {"direction": "forward", "distance_cm": 400},
        {"direction": "right", "distance_cm": 75},
        {"direction": "back", "distance_cm": 400},
    ]
    assert converted.max_deviation_cm == 0


def test_a_diagonal_becomes_a_staircase_with_minimum_moves():
    corners = pp.staircase((0, 0), (300, 300))
    assert corners[-1] == (300, 300)
    for (ax, ay), (bx, by) in itertools.pairwise([(0, 0), *corners]):
        assert (ax == bx) != (ay == by)  # one axis per leg
        assert abs(bx - ax) + abs(by - ay) >= MIN_MOVE_CM


def test_a_nearly_straight_segment_becomes_an_l_with_an_overshoot_pair():
    converted = pp.path_to_lanes([(0, 0), (10, 300)])
    assert [dict(lane) for lane in converted.lanes] == [
        {"direction": "forward", "distance_cm": 300},
        {"direction": "right", "distance_cm": 30},
        {"direction": "left", "distance_cm": 20},
    ]


def test_long_legs_are_split_into_equal_moves():
    converted = pp.path_to_lanes([(0, 0), (1201, 0)])
    assert [lane["distance_cm"] for lane in converted.lanes] == [401, 400, 400]


def test_a_short_leg_overshoots_backwards_when_forward_would_leave_the_fence():
    converted = pp.path_to_lanes([(0, 0), (0, 440), (0, 445)], fence_cm=450)
    assert [dict(lane) for lane in converted.lanes][-2:] == [
        {"direction": "back", "distance_cm": 20},
        {"direction": "forward", "distance_cm": 25},
    ]
    assert pp.geofence_violation(converted, 450) is None


def test_too_many_moves_is_refused_before_building():
    long_diagonal = [(0, 0), (20_000, 20_000), (0, 0), (20_000, 20_000)]
    with pytest.raises(ValueError, match="move cap"):
        pp.path_to_lanes(long_diagonal)


def test_a_single_vertex_is_refused():
    with pytest.raises(ValueError):
        pp.path_to_lanes([(0, 0)])


# ---------------------------------------------------------------- checks


def test_geofence_refusal_names_the_vertex():
    converted = pp.path_to_lanes([(0, 0), (0, 300), (200, 300), (200, 500), (0, 0)])
    vertex, point = pp.geofence_violation(converted, 450)
    assert vertex == 3 and point == (200, 500)
    assert pp.geofence_violation(converted, 500) is None


def test_geofence_is_relative_to_the_takeoff_point():
    converted = pp.path_to_lanes([(1000, 1000), (1000, 1440)])
    assert pp.geofence_violation(converted, 450) is None
    converted = pp.path_to_lanes([(1000, 1000), (1000, 1460)])
    assert pp.geofence_violation(converted, 450)[0] == 1


def test_closest_approach_of_parallel_and_crossing_paths():
    a = pp.path_to_lanes([(0, 0), (0, 400)]).trace
    b = pp.path_to_lanes([(150, 0), (150, 400)]).trace
    distance, _, _ = pp.closest_approach(a, b)
    assert distance == 150
    crossing = pp.path_to_lanes([(-100, 200), (300, 200)]).trace
    distance, pa, pb = pp.closest_approach(a, crossing)
    assert distance == 0 and pa == pb == (0.0, 200.0)
    corner = pp.path_to_lanes([(60, 480), (300, 480)]).trace
    distance, pa, pb = pp.closest_approach(a, corner)
    assert distance == pytest.approx(math.hypot(60, 80))
    assert pa == (0.0, 400.0) and pb == (60.0, 480.0)


def test_coverage_of_a_lawnmower_with_square_footprint():
    area = pp.Rect(0, 0, 300, 400)
    full = pp.path_to_lanes([(50, 0), (50, 400), (150, 400), (150, 0), (250, 0), (250, 400)]).trace
    fraction, uncovered, cell = pp.coverage(area, [full], 100)
    assert fraction == 1.0 and uncovered == [] and cell == 10
    half = pp.path_to_lanes([(50, 0), (50, 400), (150, 400), (150, 0)]).trace
    fraction, uncovered, _ = pp.coverage(area, [half], 100)
    assert fraction == pytest.approx(2 / 3, abs=0.01)
    assert uncovered == [{"x_cm": 200, "y_cm": 0, "w_cm": 100, "h_cm": 400}]


def test_coverage_reflects_an_edited_path():
    area = pp.Rect(0, 0, 400, 400)
    generated = pp.path_to_lanes(pp.pattern_vertices("parallel_track", area, 100)).trace
    shortened = pp.path_to_lanes([(0, 0), (0, 400), (100, 400), (100, 200)]).trace
    full, _, _ = pp.coverage(area, [generated], 100)
    partial, uncovered, _ = pp.coverage(area, [shortened], 100)
    assert partial < full
    assert uncovered and all(r["w_cm"] > 0 and r["h_cm"] > 0 for r in uncovered)
    # The uncovered rectangles and the covered fraction agree.
    missing = sum(r["w_cm"] * r["h_cm"] for r in uncovered) / (400 * 400)
    assert missing == pytest.approx(1 - partial, abs=0.01)


def test_coverage_cells_grow_for_large_areas():
    _, _, cell = pp.coverage(pp.Rect(0, 0, 20_000, 10_000), [[(0, 0), (0, 100)]], 75)
    assert cell == 50


# ---------------------------------------------------------------- zones and patterns


def test_split_area_matches_the_coordinator():
    zones = pp.split_area(pp.Rect(0, 0, 400, 400), 2, 100)
    assert zones == [pp.Rect(0, 0, 150, 400), pp.Rect(250, 0, 150, 400)]
    with pytest.raises(ValueError):
        pp.split_area(pp.Rect(0, 0, 100, 400), 3, 100)


@pytest.mark.parametrize("pattern", pp.PATTERNS)
def test_every_pattern_starts_at_the_zone_corner_and_converts(pattern):
    zone = pp.Rect(250, 0, 300, 400)
    vertices = pp.pattern_vertices(pattern, zone, 75)
    assert vertices[0] == (250, 0)
    converted = pp.path_to_lanes(vertices, fence_cm=450)
    _assert_executable(converted.lanes)
    assert pp.geofence_violation(converted, 450) is None
    assert converted.max_deviation_cm < pp.SNAP_TOLERANCE_CM


def test_unknown_pattern_is_refused():
    with pytest.raises(ValueError):
        pp.pattern_vertices("zigzag", pp.Rect(0, 0, 100, 100), 75)


# ---------------------------------------------------------------- executed on the fake drone


@pytest.mark.parametrize("drawn", [
    [(0, 0), (0, 300), (-120, 380), (200, 150)],
    [(0, 0), (37, 290), (40, 300), (-180, -90)],
    [(0, 0), (0, 440), (0, 445), (60, 300)],
    pp.pattern_vertices("sector", pp.Rect(0, 0, 400, 400), 75),
    pp.pattern_vertices("spiral", pp.Rect(0, 0, 400, 400), 75),
])
def test_converted_lanes_flown_by_the_executor_trace_the_drawn_path(drawn):
    converted = pp.path_to_lanes(drawn, fence_cm=450)
    executor, drone, sent, clock, _ = make_v2_executor(geofence_cm=450)
    try:
        msg = fp.make_assign_lanes("drone-1", 100, [dict(lane) for lane in converted.lanes], ZONE, fence=FENCE,
                                   cmd_id="a1", plan_id="p-1", plan_seq=1)
        send(executor, msg)
        assert reply_for(sent, "a1")["type"] == "ack", reply_for(sent, "a1")
        send(executor, start_at(clock() + 0.5, cmd_id="s1"))
        clock.advance(0.5)
        assert wait_for(lambda: len(horizontal_moves(drone)) == len(converted.lanes))
    finally:
        executor.shutdown(timeout=10)
    # Dead-reckon the moves the drone really flew, from the takeoff point.
    x, y = drawn[0]
    flown = [(x, y)]
    for _, direction, distance in horizontal_moves(drone):
        dx, dy = {"right": (1, 0), "left": (-1, 0), "forward": (0, 1), "back": (0, -1)}[direction]
        x, y = x + dx * distance, y + dy * distance
        flown.append((x, y))
    assert flown == list(converted.trace)
    cursor = 0
    for vertex in drawn:
        cursor = flown.index(tuple(vertex), cursor)
    assert max(_polyline_distance(p, drawn) for p in flown) < pp.SNAP_TOLERANCE_CM
    assert flown[-1] == tuple(drawn[-1])
