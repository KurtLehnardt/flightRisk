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


_BAD_LIMITS = [
    {"max_move_cm": -500},
    {"max_move_cm": 0},
    {"min_move_cm": 500, "max_move_cm": 20},
    {"min_move_cm": 30.5},
    {"min_move_cm": 0},
    {"min_move_cm": -20},
    {"max_move_cm": 500.0},
    {"min_move_cm": True},
]


@pytest.mark.parametrize("limits", _BAD_LIMITS)
def test_transit_rejects_invalid_limits(limits):
    with pytest.raises(ValueError):
        transit((0, 0), (0, 1200), **limits)
    with pytest.raises(ValueError):
        transit((0, 0), (0, 0), **limits)


@pytest.mark.parametrize("limits", _BAD_LIMITS)
def test_manual_axis_moves_reject_invalid_limits(limits):
    with pytest.raises(ValueError):
        manual_axis_moves(1200, "forward", "back", exact=True, **limits)
    with pytest.raises(ValueError):
        manual_axis_moves(0, "forward", "back", exact=False, **limits)


def test_transit_refuses_limits_that_cannot_keep_moves_in_range():
    with pytest.raises(ValueError):
        transit((0, 0), (0, 510), min_move_cm=300, max_move_cm=500)


def test_manual_axis_moves_refuse_limits_that_cannot_keep_moves_in_range():
    with pytest.raises(ValueError):
        manual_axis_moves(299, "forward", "back", exact=True, min_move_cm=300, max_move_cm=500)


def test_limit_boundary_is_twice_min_minus_one():
    transit((0, 0), (0, 510), min_move_cm=300, max_move_cm=599)
    manual_axis_moves(299, "forward", "back", exact=True, min_move_cm=300, max_move_cm=599)
    for call in (
        lambda: transit((0, 0), (0, 510), min_move_cm=300, max_move_cm=598),
        lambda: manual_axis_moves(299, "forward", "back", exact=True, min_move_cm=300, max_move_cm=598),
    ):
        with pytest.raises(ValueError):
            call()
