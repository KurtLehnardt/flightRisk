"""Tests for flat-terrain pixel-to-ground geolocation (hand-computed cases)."""

from __future__ import annotations

import math

import pytest

from flightrisk.drone.geolocate import CameraModel, pixel_to_ground

LAT, LON = 40.0, -111.0
M_PER_DEG_LAT = 111_320.0
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(LAT))
CAM = CameraModel(width=640, height=480, hfov_deg=90.0)  # focal length 320 px


def _offsets_m(result):
    lat, lon = result
    return (lat - LAT) * M_PER_DEG_LAT, (lon - LON) * M_PER_DEG_LON  # north, east


def test_nadir_center_is_below_drone():
    result = pixel_to_ground(LAT, LON, 100.0, 37.0, -90.0, CAM, 320, 240)
    north, east = _offsets_m(result)
    assert north == pytest.approx(0.0, abs=1e-3)
    assert east == pytest.approx(0.0, abs=1e-3)


def test_45deg_100m_is_100m_ahead():
    # Heading north: centre pixel lands 100 m north.
    north, east = _offsets_m(pixel_to_ground(LAT, LON, 100.0, 0.0, -45.0, CAM, 320, 240))
    assert north == pytest.approx(100.0, abs=1e-3)
    assert east == pytest.approx(0.0, abs=1e-3)


def test_45deg_with_yaw_east_and_south():
    north, east = _offsets_m(pixel_to_ground(LAT, LON, 100.0, 90.0, -45.0, CAM, 320, 240))
    assert north == pytest.approx(0.0, abs=1e-3)
    assert east == pytest.approx(100.0, abs=1e-3)
    north, east = _offsets_m(pixel_to_ground(LAT, LON, 100.0, 180.0, -45.0, CAM, 320, 240))
    assert north == pytest.approx(-100.0, abs=1e-3)
    assert east == pytest.approx(0.0, abs=1e-3)


def test_nadir_right_edge_is_right_of_heading():
    # Right edge is 320 px from centre at focal 320 px, so 1x altitude to the right.
    north, east = _offsets_m(pixel_to_ground(LAT, LON, 50.0, 0.0, -90.0, CAM, 640, 240))
    assert north == pytest.approx(0.0, abs=1e-3)
    assert east == pytest.approx(50.0, abs=1e-3)
    # Heading east: the right of the drone is south.
    north, east = _offsets_m(pixel_to_ground(LAT, LON, 50.0, 90.0, -90.0, CAM, 640, 240))
    assert north == pytest.approx(-50.0, abs=1e-3)
    assert east == pytest.approx(0.0, abs=1e-3)


def test_pixel_below_center_pulls_toward_drone():
    # Pitch -45, bottom edge (240 px below centre) tilts the ray to -45 - 36.87 deg.
    north, _ = _offsets_m(pixel_to_ground(LAT, LON, 100.0, 0.0, -45.0, CAM, 320, 480))
    # forward = cos45 - 0.75*sin45, down = sin45 + 0.75*cos45
    expected = 100.0 * (math.cos(math.pi / 4) - 0.75 * math.sin(math.pi / 4)) / (
        math.sin(math.pi / 4) + 0.75 * math.cos(math.pi / 4)
    )
    assert north == pytest.approx(expected, abs=1e-3)
    assert north == pytest.approx(100.0 * (0.25 / 1.75), abs=1e-3)


def test_above_horizon_none():
    assert pixel_to_ground(LAT, LON, 100.0, 0.0, 10.0, CAM, 320, 240) is None
    assert pixel_to_ground(LAT, LON, 100.0, 0.0, 0.0, CAM, 320, 240) is None
    # Level camera, but a pixel below centre still points down.
    assert pixel_to_ground(LAT, LON, 100.0, 0.0, 0.0, CAM, 320, 400) is not None
    # Pitched down, but the top pixel row rises above the horizon.
    assert pixel_to_ground(LAT, LON, 100.0, 0.0, -10.0, CAM, 320, 0) is None


def test_non_positive_altitude_none():
    assert pixel_to_ground(LAT, LON, 0.0, 0.0, -90.0, CAM, 320, 240) is None
