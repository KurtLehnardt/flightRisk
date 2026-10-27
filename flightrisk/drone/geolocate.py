"""Pixel-to-ground geolocation on flat terrain.

Casts a ray from the camera through a pixel, using the drone pose and gimbal
pitch, and intersects it with a horizontal ground plane at the drone's
takeoff altitude. DEM terrain is out of scope.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["CameraModel", "pixel_to_ground"]

_METERS_PER_DEG_LAT = 111_320.0
_MIN_DOWNWARD = 1e-6  # rays flatter than this never reach the ground


@dataclass(frozen=True)
class CameraModel:
    """Pinhole camera with square pixels, described by image size and horizontal FOV."""

    width: int
    height: int
    hfov_deg: float = 90.0


def pixel_to_ground(
    lat: float,
    lon: float,
    alt_agl_m: float,
    heading_deg: float,
    gimbal_pitch_deg: float,
    camera: CameraModel,
    px: float,
    py: float,
) -> tuple[float, float] | None:
    """Return the (lat, lon) of the ground point seen at pixel (px, py).

    Heading is degrees clockwise from north. Gimbal pitch follows the MAVSDK
    convention: 0 is level with the horizon and -90 looks straight down. The
    camera has no roll. Returns None when the ray does not reach the ground
    (at or above the horizon) or the altitude is not positive.
    """
    if alt_agl_m <= 0:
        return None

    focal_px = (camera.width / 2.0) / math.tan(math.radians(camera.hfov_deg) / 2.0)
    right_c = (px - camera.width / 2.0) / focal_px
    down_c = (py - camera.height / 2.0) / focal_px

    # Tilt the camera axes down by theta, in the heading-aligned frame.
    theta = math.radians(-gimbal_pitch_deg)
    forward = math.cos(theta) - down_c * math.sin(theta)
    right = right_c
    down = math.sin(theta) + down_c * math.cos(theta)
    if down <= _MIN_DOWNWARD:
        return None

    scale = alt_agl_m / down
    ahead_m = forward * scale
    right_m = right * scale

    heading = math.radians(heading_deg)
    north_m = ahead_m * math.cos(heading) - right_m * math.sin(heading)
    east_m = ahead_m * math.sin(heading) + right_m * math.cos(heading)

    out_lat = lat + north_m / _METERS_PER_DEG_LAT
    out_lon = lon + east_m / (_METERS_PER_DEG_LAT * math.cos(math.radians(lat)))
    return out_lat, out_lon
