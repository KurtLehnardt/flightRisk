"""Search pattern generator for autonomous drone search.

Implements standard aerial SAR (Search and Rescue) patterns:
- Expanding Square (SS): Start at last known position, spiral outward
- Sector/Radial (VS): Pie-slice sweeps from center point
- Parallel Track (PS): Lawnmower pattern for large area coverage
- Track Line (TS): Follow a linear path with lateral sweeps
- Contour (OS): Concentric circles at varying altitudes
- Spiral: Continuous outward spiral

All distances are relative to the launch position and subject to drift.
Every move is within the move range of one unit's ``safety.EnvelopeLimits``
profile, given as the keyword-only ``limits`` of each generator:
``ROOM_LIMITS`` by default, whose 20-500 cm range is the room profile's
default. A leg longer than the longest move is split into several moves, and
a leg shorter than the shortest move is lengthened to it. The multi-drone
splitters use one profile for every zone; a fleet whose units have different
limits generates each zone with that unit's own ``limits``.
"""

import math
from dataclasses import dataclass
from enum import Enum

from flightrisk.drone.fleet_plan_check import _move_range
from flightrisk.drone.safety import ROOM_LIMITS, EnvelopeLimits


class PatternType(Enum):
    EXPANDING_SQUARE = "expanding_square"
    SECTOR = "sector"
    PARALLEL_TRACK = "parallel_track"
    TRACK_LINE = "track_line"
    SPIRAL = "spiral"
    # Aliases for compatibility
    LAWNMOWER = "parallel_track"


@dataclass
class Waypoint:
    """A relative movement command."""
    direction: str  # forward, back, left, right, up, down
    distance_cm: int
    rotate_degrees: int = 0  # applied after movement

    def __repr__(self):
        parts = [f"{self.direction} {self.distance_cm}cm"]
        if self.rotate_degrees:
            parts.append(f"rotate {self.rotate_degrees}deg")
        return " -> ".join(parts)


def _clamp_distance(cm: int, *, limits: EnvelopeLimits = ROOM_LIMITS) -> int:
    """Clamp a distance to the move range of ``limits`` (20-500 cm for the room profile)."""
    shortest, longest = _move_range(limits)
    return max(shortest, min(longest, cm))


def _split_long_move(direction: str, total_cm: int, *, limits: EnvelopeLimits = ROOM_LIMITS) -> list[Waypoint]:
    """Split a movement into segments of at most the longest move of ``limits`` (500 cm for the room profile).

    The segments are as long as possible, so a leg up to the longest move
    stays one segment. A remainder shorter than the shortest move is
    lengthened to it.
    """
    shortest, longest = _move_range(limits)
    waypoints = []
    remaining = total_cm
    while remaining > 0:
        segment = max(shortest, min(remaining, longest))
        waypoints.append(Waypoint(direction, segment))
        remaining -= segment
    return waypoints


# --- Standard SAR Patterns ---


def generate_expanding_square(
    initial_side_cm: int = 100,
    growth_cm: int = 100,
    num_expansions: int = 4,
    *,
    limits: EnvelopeLimits = ROOM_LIMITS,
) -> list[Waypoint]:
    """Expanding Square (SS) — standard SAR pattern.

    Best when the subject's last known position is reliable. The drone
    starts at the suspected location and flies outward in progressively
    larger square-shaped loops.

    Used for: High-confidence LKP, small initial search area.
    """
    waypoints = []
    side = initial_side_cm

    for _ in range(num_expansions):
        # Each expansion is a full square: forward, right turn, forward, right turn, ...
        # But each pair of sides is the same length, growing after every 2 sides
        for i in range(4):
            waypoints.extend(_split_long_move("forward", side, limits=limits))
            waypoints.append(Waypoint("forward", 0, rotate_degrees=90))
            if i % 2 == 1:
                side += growth_cm

    return waypoints


def generate_sector(
    radius_cm: int = 300,
    num_sectors: int = 6,
    *,
    limits: EnvelopeLimits = ROOM_LIMITS,
) -> list[Waypoint]:
    """Sector / Radial Search (VS) — standard SAR pattern.

    Flies a series of triangular "pie slices" from the center point.
    Each sector: fly out to radius, rotate, fly back to center.
    Covers a circular area with high confidence in target location.

    Used for: Circular area around LKP, directional probability.
    """
    waypoints = []
    sector_angle = 360 // num_sectors

    for i in range(num_sectors):
        # Fly outward
        waypoints.extend(_split_long_move("forward", radius_cm, limits=limits))
        # Rotate to next sector angle
        waypoints.append(Waypoint("forward", 0, rotate_degrees=180))
        # Fly back to center
        waypoints.extend(_split_long_move("forward", radius_cm, limits=limits))
        # Rotate to next sector heading
        waypoints.append(Waypoint("forward", 0, rotate_degrees=180 + sector_angle))

    return waypoints


def generate_parallel_track(
    width_cm: int = 400,
    depth_cm: int = 400,
    strip_width_cm: int = 150,
    *,
    limits: EnvelopeLimits = ROOM_LIMITS,
) -> list[Waypoint]:
    """Parallel Track / Lawnmower (PS) — standard SAR pattern.

    The drone flies back and forth in straight, evenly spaced lines
    to achieve total coverage of a rectangular area. Best for large,
    relatively flat areas where the exact location is unknown.

    Used for: Large area, unknown location, flat terrain.

    Args:
        width_cm: Total width to cover (left-right).
        depth_cm: How far forward each strip goes.
        strip_width_cm: Spacing between strips (based on camera FOV at altitude).
        limits: The unit's envelope profile; every move is within its move range.
    """
    waypoints = []
    num_strips = max(1, width_cm // strip_width_cm)
    going_forward = True

    for i in range(num_strips):
        direction = "forward" if going_forward else "back"
        waypoints.extend(_split_long_move(direction, depth_cm, limits=limits))

        if i < num_strips - 1:
            waypoints.extend(_split_long_move("right", strip_width_cm, limits=limits))

        going_forward = not going_forward

    return waypoints


def generate_track_line(
    length_cm: int = 500,
    sweep_width_cm: int = 100,
    num_sweeps: int = 3,
    *,
    limits: EnvelopeLimits = ROOM_LIMITS,
) -> list[Waypoint]:
    """Track Line (TS) — standard SAR pattern.

    Flown directly along a missing person's intended route or trail.
    The drone follows the line, sweeping left and right to capture
    visuals along the track.

    Used for: Following a known path, trail, or road.
    """
    waypoints = []
    segment = length_cm // (num_sweeps * 2 + 1)

    for i in range(num_sweeps):
        # Forward along track
        waypoints.extend(_split_long_move("forward", segment, limits=limits))
        # Sweep left
        waypoints.extend(_split_long_move("left", sweep_width_cm, limits=limits))
        # Forward
        waypoints.extend(_split_long_move("forward", segment, limits=limits))
        # Sweep right (back to center + to right side)
        waypoints.extend(_split_long_move("right", sweep_width_cm * 2, limits=limits))
        # Forward
        waypoints.extend(_split_long_move("forward", segment, limits=limits))
        # Back to center
        waypoints.extend(_split_long_move("left", sweep_width_cm, limits=limits))

    # Final forward segment
    waypoints.extend(_split_long_move("forward", segment, limits=limits))

    return waypoints


def generate_spiral(
    radius_cm: int = 50,
    growth_per_turn_cm: int = 100,
    num_turns: int = 3,
    segments_per_turn: int = 8,
    *,
    limits: EnvelopeLimits = ROOM_LIMITS,
) -> list[Waypoint]:
    """Spiral search pattern.

    Approximates a continuous outward spiral with straight segments
    and rotations. Good for open areas with a central reference point.
    """
    waypoints = []
    rotation_per_segment = 360 // segments_per_turn

    for turn in range(num_turns):
        current_radius = radius_cm + (turn * growth_per_turn_cm)
        segment_length = int(
            2 * math.pi * current_radius / segments_per_turn
        )
        segment_length = _clamp_distance(segment_length, limits=limits)

        for _ in range(segments_per_turn):
            waypoints.append(
                Waypoint("forward", segment_length, rotation_per_segment)
            )

    return waypoints


# --- Pattern lookup ---

_GENERATORS = {
    PatternType.EXPANDING_SQUARE: generate_expanding_square,
    PatternType.SECTOR: generate_sector,
    PatternType.PARALLEL_TRACK: generate_parallel_track,
    PatternType.TRACK_LINE: generate_track_line,
    PatternType.SPIRAL: generate_spiral,
    PatternType.LAWNMOWER: generate_parallel_track,  # alias
}


def get_search_pattern(
    pattern: PatternType = PatternType.EXPANDING_SQUARE,
    **kwargs,
) -> list[Waypoint]:
    """Get a search pattern by type."""
    generator = _GENERATORS.get(pattern, generate_expanding_square)
    return generator(**kwargs)


def get_pattern_description(pattern: PatternType) -> str:
    """Get a human-readable description of a search pattern."""
    descriptions = {
        PatternType.EXPANDING_SQUARE: (
            "Expanding Square — starts at last known position, spirals outward "
            "in progressively larger square loops. Best for high-confidence LKP."
        ),
        PatternType.SECTOR: (
            "Sector/Radial — flies pie-slice sweeps from center point. "
            "Covers circular area, good when direction of travel is unknown."
        ),
        PatternType.PARALLEL_TRACK: (
            "Parallel Track — lawnmower pattern with evenly spaced lines "
            "for total area coverage. Best for large, flat search areas."
        ),
        PatternType.TRACK_LINE: (
            "Track Line — follows a known path or trail with lateral sweeps. "
            "Best when the missing person was following a route."
        ),
        PatternType.SPIRAL: (
            "Spiral — continuous outward spiral from center. "
            "Good for open areas with central reference point."
        ),
    }
    return descriptions.get(pattern, "Unknown pattern")


# --- Multi-drone zone splitting ---


def split_parallel_track_zones(
    num_zones: int,
    width_cm: int = 400,
    depth_cm: int = 400,
    strip_width_cm: int = 150,
    *,
    limits: EnvelopeLimits = ROOM_LIMITS,
) -> list[list[Waypoint]]:
    """Split parallel track into N zones for multi-drone.

    ``limits`` is the envelope profile of every zone's unit, so every zone's
    pattern has the same moves. A fleet whose units have different limits
    calls ``generate_parallel_track`` for each zone with that unit's limits.
    """
    if num_zones <= 1:
        return [generate_parallel_track(width_cm, depth_cm, strip_width_cm, limits=limits)]
    zone_width = width_cm // num_zones
    zones = []
    for i in range(num_zones):
        offset = _split_long_move("right", zone_width * i, limits=limits) if i > 0 else []
        pattern = generate_parallel_track(zone_width, depth_cm, strip_width_cm, limits=limits)
        zones.append(offset + pattern)
    return zones


def split_sector_zones(
    num_zones: int,
    radius_cm: int = 300,
    num_sectors: int = 6,
    *,
    limits: EnvelopeLimits = ROOM_LIMITS,
) -> list[list[Waypoint]]:
    """Split sector search across drones, every zone flown with the envelope profile ``limits``."""
    if num_zones <= 1:
        return [generate_sector(radius_cm, num_sectors, limits=limits)]
    sectors_per_zone = max(1, num_sectors // num_zones)
    zones = []
    for i in range(num_zones):
        count = min(sectors_per_zone, num_sectors - i * sectors_per_zone)
        if count <= 0:
            break
        rotation = int((360 / num_sectors) * (i * sectors_per_zone))
        prefix = [Waypoint("forward", 0, rotate_degrees=rotation)] if rotation > 0 else []
        zones.append(prefix + generate_sector(radius_cm, count, limits=limits))
    return zones
