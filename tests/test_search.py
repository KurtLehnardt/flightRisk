"""Tests for amber.drone.search SAR patterns."""

import pytest

from amber.drone.search import (
    PatternType,
    Waypoint,
    _clamp_distance,
    _split_long_move,
    generate_expanding_square,
    generate_sector,
    generate_parallel_track,
    generate_track_line,
    generate_spiral,
    get_search_pattern,
    get_pattern_description,
)

VALID_DIRECTIONS = {"forward", "back", "left", "right", "up", "down"}


class TestClampDistance:
    """Tests for _clamp_distance helper."""

    def test_below_minimum_clamps_to_20(self):
        assert _clamp_distance(5) == 20
        assert _clamp_distance(0) == 20
        assert _clamp_distance(-10) == 20

    def test_above_maximum_clamps_to_500(self):
        assert _clamp_distance(600) == 500
        assert _clamp_distance(1000) == 500

    def test_in_range_unchanged(self):
        assert _clamp_distance(20) == 20
        assert _clamp_distance(250) == 250
        assert _clamp_distance(500) == 500


class TestSplitLongMove:
    """Tests for _split_long_move helper."""

    def test_1000cm_splits_into_two_500(self):
        waypoints = _split_long_move("forward", 1000)
        assert len(waypoints) == 2
        assert all(wp.distance_cm == 500 for wp in waypoints)
        assert all(wp.direction == "forward" for wp in waypoints)

    def test_600cm_splits_into_500_plus_100(self):
        waypoints = _split_long_move("forward", 600)
        assert len(waypoints) == 2
        assert waypoints[0].distance_cm == 500
        assert waypoints[1].distance_cm == 100

    def test_300cm_single_segment(self):
        waypoints = _split_long_move("forward", 300)
        assert len(waypoints) == 1
        assert waypoints[0].distance_cm == 300

    def test_small_distance_clamped_to_20(self):
        waypoints = _split_long_move("forward", 5)
        assert len(waypoints) == 1
        assert waypoints[0].distance_cm == 20


class TestExpandingSquare:
    """Tests for expanding square pattern."""

    def test_generates_non_empty_waypoints(self):
        waypoints = generate_expanding_square()
        assert len(waypoints) > 0

    def test_all_directions_valid(self):
        waypoints = generate_expanding_square()
        for wp in waypoints:
            assert wp.direction in VALID_DIRECTIONS

    def test_all_distances_within_range(self):
        waypoints = generate_expanding_square()
        for wp in waypoints:
            if wp.distance_cm > 0:
                assert 20 <= wp.distance_cm <= 500

    def test_produces_rotations(self):
        waypoints = generate_expanding_square()
        rotations = [wp for wp in waypoints if wp.rotate_degrees != 0]
        assert len(rotations) > 0

    def test_rotations_are_90_degrees(self):
        waypoints = generate_expanding_square()
        rotations = [wp for wp in waypoints if wp.rotate_degrees != 0]
        assert all(wp.rotate_degrees == 90 for wp in rotations)


class TestSector:
    """Tests for sector/radial pattern."""

    def test_generates_non_empty_waypoints(self):
        waypoints = generate_sector()
        assert len(waypoints) > 0

    def test_all_directions_valid(self):
        waypoints = generate_sector()
        for wp in waypoints:
            assert wp.direction in VALID_DIRECTIONS

    def test_all_distances_within_range(self):
        waypoints = generate_sector()
        for wp in waypoints:
            if wp.distance_cm > 0:
                assert 20 <= wp.distance_cm <= 500


class TestParallelTrack:
    """Tests for parallel track / lawnmower pattern."""

    def test_generates_non_empty_waypoints(self):
        waypoints = generate_parallel_track()
        assert len(waypoints) > 0

    def test_all_directions_valid(self):
        waypoints = generate_parallel_track()
        for wp in waypoints:
            assert wp.direction in VALID_DIRECTIONS

    def test_all_distances_within_range(self):
        waypoints = generate_parallel_track()
        for wp in waypoints:
            if wp.distance_cm > 0:
                assert 20 <= wp.distance_cm <= 500

    def test_num_strips_correct(self):
        # width=400, strip_width=100 => 4 strips
        waypoints = generate_parallel_track(width_cm=400, depth_cm=200, strip_width_cm=100)
        # Count forward/back moves (these are the strip traversals)
        strip_moves = [wp for wp in waypoints if wp.direction in ("forward", "back") and wp.distance_cm > 0]
        # Each strip is one forward or back move, so should have 4 strip traversals
        assert len(strip_moves) == 4

    def test_alternates_forward_and_back(self):
        waypoints = generate_parallel_track(width_cm=400, depth_cm=200, strip_width_cm=100)
        # Extract only strip direction moves (not the lateral right moves)
        strip_directions = [wp.direction for wp in waypoints if wp.direction in ("forward", "back")]
        # Should alternate: forward, back, forward, back
        for i, d in enumerate(strip_directions):
            expected = "forward" if i % 2 == 0 else "back"
            assert d == expected


class TestTrackLine:
    """Tests for track line pattern."""

    def test_generates_non_empty_waypoints(self):
        waypoints = generate_track_line()
        assert len(waypoints) > 0

    def test_all_directions_valid(self):
        waypoints = generate_track_line()
        for wp in waypoints:
            assert wp.direction in VALID_DIRECTIONS

    def test_all_distances_within_range(self):
        waypoints = generate_track_line()
        for wp in waypoints:
            if wp.distance_cm > 0:
                assert 20 <= wp.distance_cm <= 500


class TestSpiral:
    """Tests for spiral pattern."""

    def test_generates_non_empty_waypoints(self):
        waypoints = generate_spiral()
        assert len(waypoints) > 0

    def test_all_directions_valid(self):
        waypoints = generate_spiral()
        for wp in waypoints:
            assert wp.direction in VALID_DIRECTIONS

    def test_all_distances_within_range(self):
        waypoints = generate_spiral()
        for wp in waypoints:
            if wp.distance_cm > 0:
                assert 20 <= wp.distance_cm <= 500

    def test_segment_count_equals_turns_times_segments(self):
        turns = 3
        segments_per_turn = 8
        waypoints = generate_spiral(num_turns=turns, segments_per_turn=segments_per_turn)
        assert len(waypoints) == turns * segments_per_turn

    def test_all_segments_have_rotation(self):
        waypoints = generate_spiral(segments_per_turn=8)
        for wp in waypoints:
            assert wp.rotate_degrees == 45  # 360 / 8


class TestGetSearchPattern:
    """Tests for pattern dispatch function."""

    def test_dispatches_expanding_square(self):
        waypoints = get_search_pattern(PatternType.EXPANDING_SQUARE)
        assert len(waypoints) > 0

    def test_dispatches_sector(self):
        waypoints = get_search_pattern(PatternType.SECTOR)
        assert len(waypoints) > 0

    def test_dispatches_parallel_track(self):
        waypoints = get_search_pattern(PatternType.PARALLEL_TRACK)
        assert len(waypoints) > 0

    def test_dispatches_track_line(self):
        waypoints = get_search_pattern(PatternType.TRACK_LINE)
        assert len(waypoints) > 0

    def test_dispatches_spiral(self):
        waypoints = get_search_pattern(PatternType.SPIRAL)
        assert len(waypoints) > 0

    def test_lawnmower_alias_works(self):
        lawnmower = get_search_pattern(PatternType.LAWNMOWER)
        parallel = get_search_pattern(PatternType.PARALLEL_TRACK)
        # Same generator, same default params => same result
        assert len(lawnmower) == len(parallel)
        for lw, pt in zip(lawnmower, parallel):
            assert lw.direction == pt.direction
            assert lw.distance_cm == pt.distance_cm


class TestGetPatternDescription:
    """Tests for pattern description lookup."""

    def test_all_patterns_have_non_empty_descriptions(self):
        for pattern in PatternType:
            desc = get_pattern_description(pattern)
            assert isinstance(desc, str)
            assert len(desc) > 0

    def test_expanding_square_description_contains_keyword(self):
        desc = get_pattern_description(PatternType.EXPANDING_SQUARE)
        assert "square" in desc.lower() or "spiral" in desc.lower()

    def test_parallel_track_description_contains_keyword(self):
        desc = get_pattern_description(PatternType.PARALLEL_TRACK)
        assert "lawnmower" in desc.lower() or "parallel" in desc.lower()
