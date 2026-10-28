"""The coordinator's expanding-squares pattern and its ``on_complete`` option.

``expanding-squares`` flies, from each drone's take-off point, a closed square of ``square_step_cm``, then
one of twice that, and so on for ``square_count`` squares (forward, right, back, left each). The default
pattern, the strips, is unchanged.
"""
from __future__ import annotations

import pytest

from flightrisk.drone import coordinator as coord_mod
from flightrisk.drone.coordinator import FleetCoordinator
from tests.test_coordinator import FakeClock, FakeEdge, walk


def make(n=2, area=(400, 150), **kwargs):
    clock = FakeClock()
    edges = {f"drone-{i + 1}": FakeEdge(f"drone-{i + 1}", clock, 0.0) for i in range(n)}
    coord = FleetCoordinator(edges, area_cm=area, strip_cm=50, clock=clock, **kwargs)
    return coord, edges


def lanes_of(edge):
    (msg,) = edge.of_type("assign_lanes")
    return msg


def test_the_default_pattern_is_the_strip_sweep_and_has_no_on_complete():
    coord, edges = make(area=(400, 400))
    coord.plan()
    msg = lanes_of(edges["drone-1"])
    assert len(msg["lanes"]) > 1 and msg["lanes"][0]["distance_cm"] == 400
    assert "on_complete" not in msg


def test_expanding_squares_are_closed_squares_of_50_100_and_150_cm():
    coord, edges = make(pattern="expanding-squares")
    plan = coord.plan()
    for drone_id in ("drone-1", "drone-2"):
        lanes = lanes_of(edges[drone_id])["lanes"]
        assert [(lane["direction"], lane["distance_cm"]) for lane in lanes] == [
            ("forward", 50), ("right", 50), ("back", 50), ("left", 50),
            ("forward", 100), ("right", 100), ("back", 100), ("left", 100),
            ("forward", 150), ("right", 150), ("back", 150), ("left", 150),
        ]
        points = walk((0, 0), lanes)
        assert points[-1] == (0, 0)  # back at the take-off point
        assert [points[i] for i in (4, 8, 12)] == [(0, 0), (0, 0), (0, 0)]  # each square closes
        assert all(0 <= x <= 150 and 0 <= y <= 150 for x, y in points)  # never leaves 150 cm forward / right
        assert all(20 <= lane["distance_cm"] <= 500 for lane in lanes)
    # The two zones sit side by side, 250 cm apart, each 150 cm wide.
    assert [(z.x_cm, z.w_cm, z.h_cm) for z in plan.zones] == [(0, 150, 150), (250, 150, 150)]


def test_squares_stay_inside_the_zone_the_edge_is_told():
    coord, edges = make(pattern="expanding-squares")
    coord.plan()
    zone = lanes_of(edges["drone-2"])["zone"]
    assert zone == {"x_cm": 250, "y_cm": 0, "w_cm": 150, "h_cm": 150}


def test_a_zone_too_small_for_the_largest_square_is_refused():
    coord, _ = make(pattern="expanding-squares", area=(300, 150))  # zones 100 cm wide
    with pytest.raises(ValueError, match="at least 150 cm wide and deep"):
        coord.plan()
    coord, _ = make(pattern="expanding-squares", area=(400, 100))  # only 100 cm deep
    with pytest.raises(ValueError, match="at least 150 cm wide and deep"):
        coord.plan()


def test_step_and_count_come_from_the_arguments():
    coord, edges = make(pattern="expanding-squares", square_step_cm=40, square_count=2, area=(400, 80))
    coord.plan()
    lanes = lanes_of(edges["drone-1"])["lanes"]
    assert [lane["distance_cm"] for lane in lanes] == [40, 40, 40, 40, 80, 80, 80, 80]


def test_settings_come_from_the_environment_when_not_given(monkeypatch):
    monkeypatch.setenv(coord_mod.PATTERN_ENV, "expanding-squares")
    monkeypatch.setenv(coord_mod.SQUARE_STEP_ENV, "50")
    monkeypatch.setenv(coord_mod.SQUARE_COUNT_ENV, "3")
    monkeypatch.setenv(coord_mod.ON_COMPLETE_ENV, "land")
    coord, edges = make()
    coord.plan()
    msg = lanes_of(edges["drone-1"])
    assert len(msg["lanes"]) == 12 and msg["on_complete"] == "land"


def test_an_argument_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv(coord_mod.PATTERN_ENV, "expanding-squares")
    coord, edges = make(pattern="parallel", area=(400, 400))
    coord.plan()
    assert lanes_of(edges["drone-1"])["lanes"][0]["distance_cm"] == 400


@pytest.mark.parametrize("kwargs", [
    {"pattern": "spiral"},
    {"pattern": "expanding-squares", "square_step_cm": 10},
    {"pattern": "expanding-squares", "square_step_cm": 600},
    {"pattern": "expanding-squares", "square_count": 0},
    {"pattern": "expanding-squares", "square_step_cm": 200, "square_count": 3},
    {"on_complete": "explode"},
])
def test_bad_settings_are_refused_when_the_coordinator_is_built(kwargs):
    with pytest.raises(ValueError):
        make(**kwargs)


def test_a_non_numeric_environment_value_is_refused(monkeypatch):
    monkeypatch.setenv(coord_mod.SQUARE_COUNT_ENV, "three")
    with pytest.raises(ValueError, match="whole numbers"):
        make(pattern="expanding-squares")


def test_on_complete_is_added_only_when_asked_and_survives_a_retask_message():
    coord, edges = make(pattern="expanding-squares", on_complete="land")
    coord.plan()
    assert lanes_of(edges["drone-1"])["on_complete"] == "land"
    coord2, edges2 = make(pattern="expanding-squares", on_complete="hover")
    coord2.plan()
    assert lanes_of(edges2["drone-1"])["on_complete"] == "hover"
    coord3, edges3 = make(pattern="expanding-squares")
    coord3.plan()
    assert "on_complete" not in lanes_of(edges3["drone-1"])
