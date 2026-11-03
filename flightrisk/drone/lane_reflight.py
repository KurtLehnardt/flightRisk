"""Lanes that re-fly the segments one lane cut left unsearched (design section 3.4).

When a drone's lane is cut by an obstacle, the ground offers what the cut
left unsearched once to another drone. The segments of one cut are its own
segments (the rest of the cut lane and of a same-direction run after it,
with the cut's reason) and its ``skipped`` shadows (lanes the deficit rule
shortened because of it), as ``lane_steps.uncovered_segments`` derives them.
This module turns those segments into the lanes the other drone flies:

1. The cut's own segments are flown **reversed**, from the planned far end
   ``E`` of the last one back toward the cut point ``P`` of the first. The
   cutting drone already found the obstacle within about 150 cm beyond
   ``P``, so approaching from ``E`` covers exactly what it could not see.
2. Each shadow follows in plan order, joined by ``transit`` from the current
   end to the shadow's nearer end, and is flown toward its farther end.
3. Segments shorter than ``MIN_MOVE_CM`` (20 cm) cannot be flown as a move;
   they are dropped from the offer and reported as ``too_short``.

The receiver flies these lanes nose-first in sensed steps with the cut
policy of section 3.3, like any lane, so a re-flight that meets the obstacle
again degrades through the normal cut rules.

``transit`` is the ground coordinator's transit between two grid points
(forward or back first, then sideways). It lives here so this module needs
nothing from the coordinator; the coordinator imports it.

Every function is pure. Segments carry ``x_cm, y_cm`` (``P``, the end next
to flown ground) and ``end_x_cm, end_y_cm`` (``E``, the far end), in
whatever frame the caller uses; the lanes and points come back in that frame.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from flightrisk.drone.lane_steps import UncoveredSegment
from flightrisk.drone.obstacle_sensor import SEGMENT_SKIPPED
from flightrisk.drone.safety import MAX_MOVE_CM, MIN_MOVE_CM

__all__ = [
    "ReflightPlan",
    "leg_rest",
    "plan_reflight",
    "reflight_lanes",
    "split_too_short",
    "transit",
]

Point = tuple[int, int]


# ---------------------------------------------------------------------------
# Transit between two grid points
# ---------------------------------------------------------------------------


def _lane(direction: str, distance_cm: int) -> dict:
    return {"direction": direction, "distance_cm": int(distance_cm)}


def _axis_moves(delta_cm: int, positive: str, negative: str) -> list[dict]:
    """Tello-valid moves (20 to 500 cm each) whose net displacement is ``delta_cm``.

    A displacement shorter than the Tello minimum is flown as an overshoot
    followed by a minimum-length return, so the net result stays exact.
    """
    if delta_cm == 0:
        return []
    direction, opposite = (positive, negative) if delta_cm > 0 else (negative, positive)
    distance = abs(delta_cm)
    if distance < MIN_MOVE_CM:
        return [_lane(direction, distance + MIN_MOVE_CM), _lane(opposite, MIN_MOVE_CM)]
    segments = math.ceil(distance / MAX_MOVE_CM)
    base, remainder = divmod(distance, segments)
    return [_lane(direction, base + (1 if i < remainder else 0)) for i in range(segments)]


def transit(start: tuple[int, int], end: tuple[int, int]) -> list[dict]:
    """Moves from ``start`` to ``end``: forward or back first, then sideways.

    Matching the forward position first keeps the drone inside its own zone
    until it crosses the gap sideways at the target's forward position.
    """
    return _axis_moves(end[1] - start[1], "forward", "back") + _axis_moves(end[0] - start[0], "right", "left")


# ---------------------------------------------------------------------------
# Re-flight lanes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReflightPlan:
    """The re-flight of one cut's segments.

    ``lanes`` are the moves to append after the receiver's transit to
    ``points[0]``; ``points`` are the start, then the end of each lane.
    ``segments`` are the segments the lanes re-fly, in flight order, and
    ``legs[k]`` is the index in ``segments`` of the segment lane ``k`` flies,
    or ``None`` for a transit between two segments. ``too_short`` are the
    segments dropped because they are shorter than the minimum move.
    """

    lanes: tuple[dict, ...]
    points: tuple[Point, ...]
    legs: tuple[int | None, ...]
    segments: tuple[UncoveredSegment, ...]
    too_short: tuple[UncoveredSegment, ...]


def split_too_short(segments: Sequence[Any], min_move_cm: float = MIN_MOVE_CM) -> tuple[list[Any], list[Any]]:
    """``(flyable, too_short)``: the segments at least *min_move_cm* long, and the others, in order."""
    flyable = [seg for seg in segments if seg.length_cm >= min_move_cm]
    too_short = [seg for seg in segments if seg.length_cm < min_move_cm]
    return flyable, too_short


def _ends(segment: Any) -> tuple[Point, Point]:
    """``(P, E)`` of a segment: the end next to flown ground, then the far end."""
    return (int(segment.x_cm), int(segment.y_cm)), (int(segment.end_x_cm), int(segment.end_y_cm))


def _direction(start: Point, end: Point) -> str:
    """The plan direction of the straight move from *start* to *end* (one axis only)."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    if dx and dy:
        raise ValueError(f"segment from {start} to {end} is not along one axis")
    if dy > 0:
        return "forward"
    if dy < 0:
        return "back"
    if dx > 0:
        return "right"
    if dx < 0:
        return "left"
    raise ValueError(f"segment from {start} to {end} has no length")


_UNIT = {"forward": (0, 1), "back": (0, -1), "right": (1, 0), "left": (-1, 0)}


def _after(point: Point, move: dict) -> Point:
    """Where flying *move* from *point* ends."""
    ux, uy = _UNIT[move["direction"]]
    return point[0] + ux * move["distance_cm"], point[1] + uy * move["distance_cm"]


def _travel(a: Point, b: Point) -> int:
    """How far a transit from *a* to *b* flies (it moves along one axis, then the other)."""
    return abs(b[0] - a[0]) + abs(b[1] - a[1])


def plan_reflight(segments: Sequence[Any], min_move_cm: float = MIN_MOVE_CM) -> ReflightPlan:
    """The re-flight of one cut's segments (see the module docstring).

    *segments* are ``UncoveredSegment`` objects of one cut: its own segments
    (any reason but ``skipped``) and its ``skipped`` shadows, each group in
    plan order (the order ``uncovered_segments`` returns them in). The own
    segments are flown first, reversed: from the far end of the last back to
    the cut point of the first. Each shadow then follows in plan order, from
    its end nearer to where the re-flight is (by transit length; on a tie, the
    end next to flown ground) to its other end. Consecutive legs that do not
    meet are joined with ``transit``. A shadow that comes first, with no own
    segment before it, starts at its end next to flown ground.
    """
    flyable, too_short = split_too_short(segments, min_move_cm)
    own = sorted((seg for seg in flyable if seg.reason != SEGMENT_SKIPPED), key=lambda seg: seg.lane_index)
    shadows = sorted((seg for seg in flyable if seg.reason == SEGMENT_SKIPPED), key=lambda seg: seg.lane_index)

    # (segment, start, end) in flight order.
    flights: list[tuple[Any, Point, Point]] = []
    for seg in reversed(own):
        p, e = _ends(seg)
        flights.append((seg, e, p))
    for seg in shadows:
        p, e = _ends(seg)
        if flights and _travel(flights[-1][2], e) < _travel(flights[-1][2], p):
            flights.append((seg, e, p))
        else:
            flights.append((seg, p, e))

    lanes: list[dict] = []
    points: list[Point] = []
    legs: list[int | None] = []
    ordered: list[Any] = []
    for seg, start, end in flights:
        if not points:
            points.append(start)
        elif points[-1] != start:
            for move in transit(points[-1], start):
                lanes.append(move)
                legs.append(None)
                points.append(_after(points[-1], move))
        lanes.append(_lane(_direction(start, end), _travel(start, end)))
        legs.append(len(ordered))
        ordered.append(seg)
        points.append(end)
    return ReflightPlan(tuple(lanes), tuple(points), tuple(legs), tuple(ordered), tuple(too_short))


def leg_rest(segment: UncoveredSegment, flown_from: Point, flown_to: Point) -> list[UncoveredSegment]:
    """What of *segment* a move actually flown from *flown_from* to *flown_to* leaves unflown.

    The move covers the part of the segment it runs along: both must lie on
    the same line, so a move beside the segment, or across it, or no move
    at all covers nothing and the whole segment is returned. Otherwise up to
    two pieces are left, one before and one after the stretch flown, each
    with its end next to that stretch as ``x_cm, y_cm`` and the segment's
    other fields kept. The receiver of a re-flight is judged with this
    against its replayed path, so a leg counts as re-flown only where the
    drone really flew it.
    """
    p = (int(segment.x_cm), int(segment.y_cm))
    e = (int(segment.end_x_cm), int(segment.end_y_cm))
    length = _travel(p, e)
    if length <= 0:
        return []
    a = (int(flown_from[0]), int(flown_from[1]))
    b = (int(flown_to[0]), int(flown_to[1]))
    ux, uy = (e[0] - p[0]) // length, (e[1] - p[1]) // length
    if (ux and not (a[1] == b[1] == p[1])) or (uy and not (a[0] == b[0] == p[0])):
        return [segment]

    def along(point: Point) -> int:
        return (point[0] - p[0]) * ux + (point[1] - p[1]) * uy

    lo = max(0, min(along(a), along(b)))
    hi = min(length, max(along(a), along(b)))
    if hi <= lo:
        return [segment]

    def at(t: int) -> Point:
        return p[0] + ux * t, p[1] + uy * t

    rest = []
    for near, far in ((lo, 0), (hi, length)):
        if near != far:
            (x0, y0), (x1, y1) = at(near), at(far)
            rest.append(replace(segment, x_cm=x0, y_cm=y0, end_x_cm=x1, end_y_cm=y1, length_cm=abs(far - near)))
    return rest


def reflight_lanes(segments: Sequence[Any]) -> tuple[list[dict], list[Point]]:
    """``(lanes, points)`` that re-fly one cut's segments: ``plan_reflight`` without the bookkeeping.

    For the worked example of design section 3.3 (lane 0 of ``forward 400,
    right 100, back 400, right 100, forward 400`` cut at 250 cm, which
    leaves the obstacle segment (0, 250) to (0, 400) and the shadow (100,
    250) to (100, 400) on lane 2) the lanes are ``back 150``, ``right 100``
    and ``forward 150``, and the points (0, 400), (0, 250), (100, 250) and
    (100, 400): the receiver transits to (0, 400) first.
    """
    plan = plan_reflight(segments)
    return [dict(lane) for lane in plan.lanes], list(plan.points)
