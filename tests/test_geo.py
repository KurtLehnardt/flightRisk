"""Tests for flightrisk.drone.geo."""

import math

import pytest

from flightrisk.drone.geo import (
    UTAH_DEMO_ORIGIN,
    UTAH_LAT_RANGE,
    UTAH_LON_RANGE,
    haversine_m,
    lawnmower_gps,
    offset_latlon,
    waypoints_to_gps,
)
from flightrisk.drone.search import PatternType, Waypoint, get_search_pattern

LAT, LON = UTAH_DEMO_ORIGIN


def test_offset_north_100m_roundtrip():
    lat2, lon2 = offset_latlon(LAT, LON, 100.0, 0.0)
    assert haversine_m(LAT, LON, lat2, lon2) == pytest.approx(100.0, abs=0.5)
    assert lon2 == pytest.approx(LON)


def test_haversine_known_pair():
    # One degree of latitude on the equator is about 111.195 km.
    assert haversine_m(0, 0, 1, 0) == pytest.approx(111_195, abs=100)
    assert haversine_m(LAT, LON, LAT, LON) == 0.0


def test_forward_with_heading_90_moves_east():
    (wp,) = waypoints_to_gps([Waypoint("forward", 1000)], LAT, LON, heading_deg=90.0)
    assert wp.lon > LON
    assert wp.lat == pytest.approx(LAT, abs=1e-7)
    assert haversine_m(LAT, LON, wp.lat, wp.lon) == pytest.approx(10.0, abs=0.1)


def test_rotate_changes_subsequent_direction():
    wps = waypoints_to_gps([Waypoint("forward", 1000, rotate_degrees=90), Waypoint("forward", 1000)], LAT, LON)
    assert wps[0].lat > LAT and wps[0].lon == pytest.approx(LON)
    assert wps[1].lon > wps[0].lon
    assert wps[1].lat == pytest.approx(wps[0].lat, abs=1e-7)


def test_up_changes_alt():
    wps = waypoints_to_gps([Waypoint("up", 500), Waypoint("down", 200)], LAT, LON, alt_m=30.0)
    assert wps[0].alt_m == pytest.approx(35.0)
    assert wps[1].alt_m == pytest.approx(33.0)
    assert (wps[1].lat, wps[1].lon) == (LAT, LON)


def test_all_patterns_convert():
    for pattern in (
        PatternType.EXPANDING_SQUARE,
        PatternType.SECTOR,
        PatternType.PARALLEL_TRACK,
        PatternType.TRACK_LINE,
        PatternType.SPIRAL,
    ):
        body = get_search_pattern(pattern)
        gps = waypoints_to_gps(body, LAT, LON)
        assert len(gps) == len(body) > 0


def test_unknown_direction_rejected():
    with pytest.raises(ValueError):
        waypoints_to_gps([Waypoint("sideways", 100)], LAT, LON)


def test_lawnmower_lane_count_and_bounds():
    width, depth, spacing = 100.0, 200.0, 30.0
    pts = lawnmower_gps(LAT, LON, width, depth, spacing, heading_deg=0.0, alt_m=60.0)
    lanes = math.ceil(width / spacing) + 1
    assert len(pts) == 2 * lanes
    assert all(p.alt_m == 60.0 for p in pts)
    for p in pts:
        assert haversine_m(LAT, LON, p.lat, p.lon) <= math.hypot(width + spacing, depth) + 1.0
    # Heading north: lanes step east and each lane runs depth_m north.
    assert pts[0].lat == pytest.approx(LAT)
    assert haversine_m(pts[0].lat, pts[0].lon, pts[1].lat, pts[1].lon) == pytest.approx(depth, abs=0.5)
    assert pts[2].lat > pts[3].lat  # second lane flown in the opposite direction


def test_demo_origin_in_utah():
    assert UTAH_LAT_RANGE[0] <= LAT <= UTAH_LAT_RANGE[1]
    assert UTAH_LON_RANGE[0] <= LON <= UTAH_LON_RANGE[1]


def test_demo_origin_is_millcreek_canyon_design_value():
    assert UTAH_DEMO_ORIGIN == (40.6894, -111.7310)
