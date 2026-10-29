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

# Demo origin in Millcreek Canyon, Utah, as specified in the design. The
# coordinates are approximate: a placeholder start point for demos and
# simulation, not a surveyed search area.
UTAH_DEMO_ORIGIN = (40.6894, -111.7310)

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


@dataclass(frozen=True)
class PlanAnchor:
    """Where the plan frame's origin is on the Earth, and which way its forward axis points.

    The plan frame is the centimetre grid of the fleet coordinator and of the
    edge's dead reckoning: ``x`` grows to the right of the takeoff heading and
    ``y`` grows forward along it. ``heading_deg`` is the compass bearing of
    that forward (``+y``) axis. A Tello has no GPS, so the operator sets the
    anchor; nothing safety related ever depends on it.
    """

    lat: float
    lon: float
    heading_deg: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (("lat", self.lat), ("lon", self.lon), ("heading_deg", self.heading_deg)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"anchor {name} must be a finite number, got {value!r}")
        if not -85.0 <= self.lat <= 85.0:
            raise ValueError(f"anchor latitude must be within -85 to 85 degrees, got {self.lat!r}")
        if not -180.0 <= self.lon <= 180.0:
            raise ValueError(f"anchor longitude must be within -180 to 180 degrees, got {self.lon!r}")
        object.__setattr__(self, "heading_deg", float(self.heading_deg) % 360.0)

    def to_dict(self) -> dict:
        return {"lat": self.lat, "lon": self.lon, "heading_deg": self.heading_deg}

    @classmethod
    def from_dict(cls, data: object) -> PlanAnchor:
        """Build an anchor from ``{"lat", "lon", "heading_deg"}``; raises ``ValueError`` for anything else."""
        if not isinstance(data, dict):
            # ValueError like every other bad anchor, so callers handle one exception type.
            raise ValueError("the anchor must be an object with lat, lon and heading_deg")  # noqa: TRY004
        unknown = set(data) - {"lat", "lon", "heading_deg"}
        if unknown:
            raise ValueError(f"unknown anchor fields: {', '.join(sorted(map(str, unknown)))}")
        return cls(data.get("lat"), data.get("lon"), data.get("heading_deg", 0.0))


def frame_to_latlon(anchor: PlanAnchor, x_cm: float, y_cm: float) -> tuple[float, float]:
    """Latitude and longitude of the plan-frame point ``(x_cm, y_cm)``.

    The forward axis points along ``anchor.heading_deg`` and the right axis
    90 degrees clockwise from it. The flat-earth offset of ``offset_latlon``
    is used, so the result is accurate to well under a metre over a few
    kilometres.
    """
    h = math.radians(anchor.heading_deg)
    east_m = (x_cm * math.cos(h) + y_cm * math.sin(h)) / 100.0
    north_m = (y_cm * math.cos(h) - x_cm * math.sin(h)) / 100.0
    return offset_latlon(anchor.lat, anchor.lon, north_m, east_m)


def latlon_to_frame(anchor: PlanAnchor, lat: float, lon: float) -> tuple[float, float]:
    """The plan-frame point ``(x_cm, y_cm)`` at ``lat``/``lon``; the exact inverse of ``frame_to_latlon``."""
    north_m = math.radians(lat - anchor.lat) * EARTH_RADIUS_M
    east_m = math.radians(lon - anchor.lon) * EARTH_RADIUS_M * math.cos(math.radians(anchor.lat))
    h = math.radians(anchor.heading_deg)
    x_m = east_m * math.cos(h) - north_m * math.sin(h)
    y_m = east_m * math.sin(h) + north_m * math.cos(h)
    return x_m * 100.0, y_m * 100.0


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
