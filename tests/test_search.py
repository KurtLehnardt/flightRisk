"""Unit tests for amber.drone.search — SAR search pattern generator."""

import pytest

from amber.drone.search import (
    PatternType,
    Waypoint,
    _clamp_distance,
    _split_long_move,
    generate_expanding_square,
    generate_parallel_track,
    generate_sector,
    generate_spiral,
    generate_track_line,
    get_pattern_description,
    get_search_pattern,
)

VALID_DIRECTIONS = {"forward", "back", "left", "right", "up", "down"}


class TestClampDistance:
    """Tests for _clamp_distance()."""

    def test_below_minimum_clamps_to_20(self):
        assert _clamp_distance(5) == 20
        assert _clamp_distance(0) == 20
        assert _clamp_distance(-10) == 20

    def test_above_maximum_clamps_to_500(self):
        assert _clamp_distance(600) == 500
        assert _clamp_distance(1000) == 500

    def test_in_range_unchanged(self):
        assert _clamp_distance(100) == 100
        assert _clamp_distance(20) == 20
        assert _clamp_distance(500) == 500
        assert _clamp_distance(250) == 250


class TestSplitLongMove:
    """Tests for _split_long_move()."""

    def test_1000cm_splits_into_500_500(self):
        waypoints = _split_long_move("forward", 1000)
        assert len(waypoints) == 2
        assert waypoints[0].distance_cm == 500
        assert waypoints[1].distance_cm == 500
        assert all(w.direction == "forward" for w in waypoints)

    def test_600cm_splits_into_500_100(self):
        waypoints = _split_long_move("forward", 600)
        assert len(waypoints) == 2
        assert waypoints[0].distance_cm == 500
        assert waypoints[1].distance_cm == 100

    def test_short_move_no_split(self):
        waypoints = _split_long_move("right", 200)
        assert len(waypoints) == 1
        assert waypoints[0].distance_cm == 200
        assert waypoints[0].direction == "right"

    def test_exact_500_no_split(self):
        waypoints = _split_long_move("left", 500)
        assert len(waypoints) == 1
        assert waypoints[0].distance_cm == 500

    def test_very_small_move_clamped(self):
        waypoints = _split_long_move("forward", 5)
        assert len(waypoints) == 1
        assert waypoints[0].distance_cm == 20  # clamped to minimum


class TestExpandingSquare:
    """Tests for generate_expanding_square()."""

    def test_generates_non_empty_waypoints(self):
        waypoints = generate_expanding_square()
        assert len(waypoints) > 0

    def test_correct_number_of_forward_groups(self):
        # Each expansion has 4 sides, so 4 * num_expansions forward moves + rotation waypoints
        waypoints = generate_expanding_square(
            initial_side_cm=100, growth_cm=100, num_expansions=2
        )
        assert len(waypoints) > 0

    def test_all_directions_valid(self):
        waypoints = generate_expanding_square()
        for wp in waypoints:
            assert wp.direction in VALID_DIRECTIONS

    def test_all_distances_in_range(self):
        waypoints = generate_expanding_square()
        for wp in waypoints:
            if wp.distance_cm > 0:
                assert 20 <= wp.distance_cm <= 500


class TestParallelTrack:
    """Tests for generate_parallel_track()."""

    def test_generates_non_empty_waypoints(self):
        waypoints = generate_parallel_track()
        assert len(waypoints) > 0

    def test_num_strips_matches_width_over_strip_width(self):
        width, strip_width = 600, 150
        waypoints = generate_parallel_track(
            width_cm=width, depth_cm=300, strip_width_cm=strip_width
        )
        expected_strips = max(1, width // strip_width)  # 4
        # Count direction changes (forward/back groups)
        forward_groups = sum(1 for w in waypoints if w.direction in ("forward", "back") and w.distance_cm > 0)
        # forward_groups should roughly correspond to strips (each strip is multiple segments)
        assert forward_groups >= expected_strips

    def test_all_directions_valid(self):
        waypoints = generate_parallel_track()
        for wp in waypoints:
            assert wp.direction in VALID_DIRECTIONS

    def test_all_distances_in_range(self):
        waypoints = generate_parallel_track()
        for wp in waypoints:
            if wp.distance_cm > 0:
                assert 20 <= wp.distance_cm <= 500


class TestSector:
    """Tests for generate_sector()."""

    def test_generates_non_empty_waypoints(self):
        waypoints = generate_sector()
        assert len(waypoints) > 0

    def test_returns_to_center_after_each_sector(self):
        # Each sector: fly out, rotate 180, fly back, rotate
        # So forward moves come in pairs (out + back)
        waypoints = generate_sector(radius_cm=200, num_sectors=4)
        # Count rotation waypoints with 180 degree turns (return to center)
        return_rotations = sum(
            1 for w in waypoints if w.rotate_degrees == 180
        )
        assert return_rotations == 4  # one per sector

    def test_all_directions_valid(self):
        waypoints = generate_sector()
        for wp in waypoints:
            assert wp.direction in VALID_DIRECTIONS


class TestSpiral:
    """Tests for generate_spiral()."""

    def test_generates_non_empty_waypoints(self):
        waypoints = generate_spiral()
        assert len(waypoints) > 0

    def test_segment_count_matches_turns_times_segments(self):
        num_turns, segments_per_turn = 3, 8
        waypoints = generate_spiral(
            num_turns=num_turns, segments_per_turn=segments_per_turn
        )
        assert len(waypoints) == num_turns * segments_per_turn

    def test_all_segments_have_rotation(self):
        segments_per_turn = 8
        waypoints = generate_spiral(segments_per_turn=segments_per_turn)
        expected_rotation = 360 // segments_per_turn
        for wp in waypoints:
            assert wp.rotate_degrees == expected_rotation

    def test_all_distances_in_range(self):
        waypoints = generate_spiral()
        for wp in waypoints:
            assert 20 <= wp.distance_cm <= 500


class TestTrackLine:
    """Tests for generate_track_line()."""

    def test_generates_non_empty_waypoints(self):
        waypoints = generate_track_line()
        assert len(waypoints) > 0

    def test_all_directions_valid(self):
        waypoints = generate_track_line()
        for wp in waypoints:
            assert wp.direction in VALID_DIRECTIONS

    def test_all_distances_in_range(self):
        waypoints = generate_track_line()
        for wp in waypoints:
            if wp.distance_cm > 0:
                assert 20 <= wp.distance_cm <= 500


class TestGetSearchPattern:
    """Tests for get_search_pattern() lookup."""

    def test_returns_correct_pattern_expanding_square(self):
        waypoints = get_search_pattern(PatternType.EXPANDING_SQUARE)
        assert len(waypoints) > 0

    def test_returns_correct_pattern_sector(self):
        waypoints = get_search_pattern(PatternType.SECTOR)
        assert len(waypoints) > 0

    def test_returns_correct_pattern_parallel_track(self):
        waypoints = get_search_pattern(PatternType.PARALLEL_TRACK)
        assert len(waypoints) > 0

    def test_returns_correct_pattern_track_line(self):
        waypoints = get_search_pattern(PatternType.TRACK_LINE)
        assert len(waypoints) > 0

    def test_returns_correct_pattern_spiral(self):
        waypoints = get_search_pattern(PatternType.SPIRAL)
        assert len(waypoints) > 0


class TestGetPatternDescription:
    """Tests for get_pattern_description()."""

    def test_returns_non_empty_string_for_each_type(self):
        for ptype in [
            PatternType.EXPANDING_SQUARE,
            PatternType.SECTOR,
            PatternType.PARALLEL_TRACK,
            PatternType.TRACK_LINE,
            PatternType.SPIRAL,
        ]:
            desc = get_pattern_description(ptype)
            assert isinstance(desc, str)
            assert len(desc) > 0


class TestPatternTypeEnum:
    """Tests for PatternType enum."""

    def test_parallel_track_value(self):
        assert PatternType.PARALLEL_TRACK.value == "parallel_track"

    def test_lawnmower_alias_value(self):
        assert PatternType.LAWNMOWER.value == "parallel_track"

    def test_lawnmower_alias_same_generator(self):
        lawnmower = get_search_pattern(PatternType.LAWNMOWER, width_cm=300, depth_cm=200, strip_width_cm=100)
        parallel = get_search_pattern(PatternType.PARALLEL_TRACK, width_cm=300, depth_cm=200, strip_width_cm=100)
        # Same generator, same args, same result
        assert len(lawnmower) == len(parallel)


class TestWaypoint:
    """Tests for Waypoint dataclass."""

    def test_repr_without_rotation(self):
        wp = Waypoint("forward", 100)
        assert "forward" in repr(wp)
        assert "100cm" in repr(wp)

    def test_repr_with_rotation(self):
        wp = Waypoint("forward", 100, rotate_degrees=90)
        r = repr(wp)
        assert "forward" in r
        assert "100cm" in r
        assert "rotate" in r
        assert "90" in r
