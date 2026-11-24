"""The lane helpers take the unit's move range and default to today's 20 and 500 cm."""

from __future__ import annotations

import pytest

from flightrisk.drone.lane_manual import _axis_moves as manual_axis_moves
from flightrisk.drone.lane_reflight import transit


def _lane(direction: str, distance: int) -> dict:
    return {"direction": direction, "distance_cm": distance}


@pytest.mark.parametrize(
    ("end", "expected"),
    [
        ((0, 1200), [_lane("forward", 400)] * 3),
        ((0, 10), [_lane("forward", 30), _lane("back", 20)]),
        ((-5, 0), [_lane("left", 25), _lane("right", 20)]),
        ((0, -600), [_lane("back", 300)] * 2),
        ((0, 0), []),
    ],
)
def test_transit_defaults_match_todays_moves(end, expected):
    assert transit((0, 0), end) == expected
    assert transit((0, 0), end, min_move_cm=20, max_move_cm=500) == expected


def test_transit_generic_limits_split_long_transit_in_two():
    moves = transit((0, 0), (0, 6000), min_move_cm=20, max_move_cm=5000)
    assert moves == [_lane("forward", 3000), _lane("forward", 3000)]


def test_manual_helper_defaults_match_todays_moves():
    assert manual_axis_moves(1200, "forward", "back", exact=True) == [("forward", 400)] * 3
    assert manual_axis_moves(10, "forward", "back", exact=True) == [("forward", 30), ("back", 20)]
    assert manual_axis_moves(10, "forward", "back", exact=False) == []
    assert manual_axis_moves(-5, "forward", "back", exact=True) == [("back", 25), ("forward", 20)]
    assert manual_axis_moves(-1200, "forward", "back", exact=True) == [("back", 400)] * 3


def test_manual_helper_honours_explicit_limits():
    assert manual_axis_moves(6000, "forward", "back", exact=True, min_move_cm=20, max_move_cm=5000) == [
        ("forward", 3000),
        ("forward", 3000),
    ]
    assert manual_axis_moves(40, "right", "left", exact=True, min_move_cm=50, max_move_cm=500) == [
        ("right", 90),
        ("left", 50),
    ]
    assert manual_axis_moves(40, "right", "left", exact=False, min_move_cm=50, max_move_cm=500) == []
