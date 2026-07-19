"""Search pattern generator for autonomous drone search.

Implements standard aerial SAR (Search and Rescue) patterns:
- Expanding Square (SS): Start at last known position, spiral outward
- Sector/Radial (VS): Pie-slice sweeps from center point
- Parallel Track (PS): Lawnmower pattern for large area coverage
- Track Line (TS): Follow a linear path with lateral sweeps
- Contour (OS): Concentric circles at varying altitudes
- Spiral: Continuous outward spiral

The Tello has no GPS, so all distances are relative to launch position
and subject to drift. Good enough for park/field demos.
"""

import math
from dataclasses import dataclass
from enum import Enum


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


def _clamp_distance(cm: int) -> int:
    """Clamp distance to Tello's 20-500cm range."""
    return max(20, min(500, cm))


def _split_long_move(direction: str, total_cm: int) -> list[Waypoint]:
    """Split a long movement into multiple 500cm max segments."""
    waypoints = []
    remaining = total_cm
    while remaining > 0:
        segment = _clamp_distance(min(remaining, 500))
        waypoints.append(Waypoint(direction, segment))
        remaining -= segment
    return waypoints


# --- Standard SAR Patterns ---


def generate_expanding_square(
    initial_side_cm: int = 100,
    growth_cm: int = 100,
    num_expansions: int = 4,
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
            waypoints.extend(_split_long_move("forward", side))
            waypoints.append(Waypoint("forward", 0, rotate_degrees=90))
            if i % 2 == 1:
                side += growth_cm

    return waypoints


def generate_sector(
    radius_cm: int = 300,
    num_sectors: int = 6,
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
        waypoints.extend(_split_long_move("forward", radius_cm))
        # Rotate to next sector angle
        waypoints.append(Waypoint("forward", 0, rotate_degrees=180))
        # Fly back to center
        waypoints.extend(_split_long_move("forward", radius_cm))
        # Rotate to next sector heading
        waypoints.append(Waypoint("forward", 0, rotate_degrees=180 + sector_angle))

    return waypoints


def generate_parallel_track(
    width_cm: int = 400,
    depth_cm: int = 400,
    strip_width_cm: int = 150,
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
    """
    waypoints = []
    num_strips = max(1, width_cm // strip_width_cm)
    going_forward = True

    for i in range(num_strips):
        direction = "forward" if going_forward else "back"
        waypoints.extend(_split_long_move(direction, depth_cm))

        if i < num_strips - 1:
            waypoints.extend(_split_long_move("right", strip_width_cm))

        going_forward = not going_forward

    return waypoints


def generate_track_line(
    length_cm: int = 500,
    sweep_width_cm: int = 100,
    num_sweeps: int = 3,
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
        waypoints.extend(_split_long_move("forward", segment))
        # Sweep left
        waypoints.extend(_split_long_move("left", sweep_width_cm))
        # Forward
        waypoints.extend(_split_long_move("forward", segment))
        # Sweep right (back to center + to right side)
        waypoints.extend(_split_long_move("right", sweep_width_cm * 2))
        # Forward
        waypoints.extend(_split_long_move("forward", segment))
        # Back to center
        waypoints.extend(_split_long_move("left", sweep_width_cm))

    # Final forward segment
    waypoints.extend(_split_long_move("forward", segment))

    return waypoints


def generate_spiral(
    radius_cm: int = 50,
    growth_per_turn_cm: int = 100,
    num_turns: int = 3,
    segments_per_turn: int = 8,
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
        segment_length = _clamp_distance(segment_length)

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
