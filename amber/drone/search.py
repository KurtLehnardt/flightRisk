"""Search pattern generator for autonomous drone search.

Generates waypoint sequences for common search patterns.
The Tello has no GPS, so distances are relative to launch position
and subject to drift — good enough for a park/field demo.
"""

from dataclasses import dataclass
from enum import Enum


class PatternType(Enum):
    LAWNMOWER = "lawnmower"
    EXPANDING_SQUARE = "expanding_square"
    SPIRAL = "spiral"


@dataclass
class Waypoint:
    """A relative movement command."""
    direction: str  # forward, back, left, right
    distance_cm: int
    rotate_degrees: int = 0  # applied after movement

    def __repr__(self):
        parts = [f"{self.direction} {self.distance_cm}cm"]
        if self.rotate_degrees:
            parts.append(f"rotate {self.rotate_degrees}°")
        return " → ".join(parts)


def generate_lawnmower(
    width_cm: int = 400,
    depth_cm: int = 400,
    strip_width_cm: int = 150,
) -> list[Waypoint]:
    """Generate a lawnmower (boustrophedon) search pattern.

    The drone flies forward, turns, moves sideways one strip width,
    turns again, flies back. Repeats until the area is covered.

    Args:
        width_cm: Total width to cover (left-right).
        depth_cm: How far forward each strip goes.
        strip_width_cm: Spacing between strips (based on camera FOV at altitude).
    """
    waypoints = []
    num_strips = max(1, width_cm // strip_width_cm)
    going_forward = True

    for i in range(num_strips):
        if going_forward:
            waypoints.append(Waypoint("forward", min(depth_cm, 500)))
            remaining = depth_cm - 500
            while remaining > 0:
                waypoints.append(Waypoint("forward", min(remaining, 500)))
                remaining -= 500
        else:
            waypoints.append(Waypoint("back", min(depth_cm, 500)))
            remaining = depth_cm - 500
            while remaining > 0:
                waypoints.append(Waypoint("back", min(remaining, 500)))
                remaining -= 500

        if i < num_strips - 1:
            waypoints.append(Waypoint("right", min(strip_width_cm, 500)))

        going_forward = not going_forward

    return waypoints


def generate_expanding_square(
    initial_side_cm: int = 100,
    growth_cm: int = 100,
    num_expansions: int = 4,
) -> list[Waypoint]:
    """Generate an expanding square search pattern.

    Starts at center, spirals outward in a square pattern with
    increasing side lengths. Good for "last known position" searches.
    """
    waypoints = []
    directions = ["forward", "right", "back", "left"]
    side = initial_side_cm

    for expansion in range(num_expansions):
        for i, direction in enumerate(directions):
            dist = min(side, 500)
            waypoints.append(Waypoint(direction, dist))
            remaining = side - 500
            while remaining > 0:
                waypoints.append(Waypoint(direction, min(remaining, 500)))
                remaining -= 500

            if i < 3 or expansion < num_expansions - 1:
                pass  # continue pattern
        side += growth_cm

    return waypoints


def generate_spiral(
    radius_cm: int = 50,
    growth_per_turn_cm: int = 100,
    num_turns: int = 3,
    segments_per_turn: int = 8,
) -> list[Waypoint]:
    """Generate a spiral search pattern using forward + rotate segments.

    Approximates a spiral with straight segments and rotations.
    """
    waypoints = []
    rotation_per_segment = 360 // segments_per_turn

    for turn in range(num_turns):
        current_radius = radius_cm + (turn * growth_per_turn_cm)
        segment_length = int(
            2 * 3.14159 * current_radius / segments_per_turn
        )
        segment_length = max(20, min(500, segment_length))

        for _ in range(segments_per_turn):
            waypoints.append(
                Waypoint("forward", segment_length, rotation_per_segment)
            )

    return waypoints


def get_search_pattern(
    pattern: PatternType = PatternType.EXPANDING_SQUARE,
    **kwargs,
) -> list[Waypoint]:
    """Get a search pattern by type."""
    generators = {
        PatternType.LAWNMOWER: generate_lawnmower,
        PatternType.EXPANDING_SQUARE: generate_expanding_square,
        PatternType.SPIRAL: generate_spiral,
    }
    return generators[pattern](**kwargs)
