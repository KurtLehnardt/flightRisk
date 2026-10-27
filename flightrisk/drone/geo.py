"""Geodesy helpers: local offsets, distances and body-frame to GPS conversion.

Headings are compass degrees (0 = north, 90 = east, clockwise positive). The
flat-earth offset used here is accurate to well under a metre over the few
kilometres a single search sortie covers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from flightrisk.drone.search import PatternType, Waypoint, get_search_pattern

EARTH_RADIUS_M = 6_371_008.8

# Demo origin near Salt Lake City, Utah. ASSUMPTION: a placeholder start point
# for demos and simulation, not a real search area.
UTAH_DEMO_ORIGIN = (40.7608, -111.8910)

# Rough bounding box of Utah, used to sanity check demo coordinates.
UTAH_LAT_RANGE = (36.998, 42.001)
UTAH_LON_RANGE = (-114.053, -109.041)


@dataclass(frozen=True)
class GpsWaypoint:
    """An absolute position with altitude above the takeoff point in metres."""

    lat: float
    lon: float
    alt_m: float


def offset_latlon(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    """Return the latitude and longitude reached by moving north and east in metres."""
    dlat = math.degrees(north_m / EARTH_RADIUS_M)
    dlon = math.degrees(east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres between two points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _move(lat: float, lon: float, heading_deg: float, distance_m: float) -> tuple[float, float]:
    """Move distance_m along a compass heading."""
    h = math.radians(heading_deg)
    return offset_latlon(lat, lon, distance_m * math.cos(h), distance_m * math.sin(h))


# Bearing offset of each horizontal body-frame direction relative to heading.
_DIRECTION_OFFSET_DEG = {"forward": 0.0, "right": 90.0, "back": 180.0, "left": -90.0}


def waypoints_to_gps(
    waypoints: list[Waypoint],
    origin_lat: float,
    origin_lon: float,
    heading_deg: float = 0.0,
    alt_m: float = 30.0,
) -> list[GpsWaypoint]:
    """Convert body-frame waypoints to GPS positions, one per input waypoint.

    Each output is the position after the waypoint's movement. A waypoint's
    rotate_degrees is applied after the movement and changes the heading used
    by all later waypoints. The up and down directions change altitude only.
    """
    lat, lon, alt, heading = origin_lat, origin_lon, alt_m, heading_deg
    out: list[GpsWaypoint] = []
    for wp in waypoints:
        dist_m = wp.distance_cm / 100.0
        if wp.direction == "up":
            alt += dist_m
        elif wp.direction == "down":
            alt -= dist_m
        elif wp.direction in _DIRECTION_OFFSET_DEG:
            lat, lon = _move(lat, lon, heading + _DIRECTION_OFFSET_DEG[wp.direction], dist_m)
        else:
            raise ValueError(f"Unknown waypoint direction: {wp.direction!r}")
        heading = (heading + wp.rotate_degrees) % 360.0
        out.append(GpsWaypoint(lat, lon, alt))
    return out


def lawnmower_gps(
    origin_lat: float,
    origin_lon: float,
    width_m: float,
    depth_m: float,
    lane_spacing_m: float,
    heading_deg: float = 0.0,
    alt_m: float = 30.0,
) -> list[GpsWaypoint]:
    """Return a lawnmower pattern as GPS waypoints.

    Lanes run along heading_deg for depth_m and are stepped to the right of the
    heading by lane_spacing_m. There are ceil(width_m / lane_spacing_m) + 1
    lanes, so the last lane lies on or just past the far edge of the area.
    Each lane contributes a start and an end waypoint, and alternate lanes are
    flown in opposite directions.
    """
    if lane_spacing_m <= 0:
        raise ValueError("lane_spacing_m must be positive")
    lanes = math.ceil(width_m / lane_spacing_m) + 1
    out: list[GpsWaypoint] = []
    for i in range(lanes):
        lane_lat, lane_lon = _move(origin_lat, origin_lon, heading_deg + 90.0, i * lane_spacing_m)
        end_lat, end_lon = _move(lane_lat, lane_lon, heading_deg, depth_m)
        ends = [(lane_lat, lane_lon), (end_lat, end_lon)]
        if i % 2:
            ends.reverse()
        out.extend(GpsWaypoint(la, lo, alt_m) for la, lo in ends)
    return out


def pattern_to_gps(
    pattern: PatternType,
    origin_lat: float,
    origin_lon: float,
    heading_deg: float = 0.0,
    alt_m: float = 30.0,
    **kwargs,
) -> list[GpsWaypoint]:
    """Generate a named search pattern and convert it to GPS waypoints."""
    return waypoints_to_gps(get_search_pattern(pattern, **kwargs), origin_lat, origin_lon, heading_deg, alt_m)
