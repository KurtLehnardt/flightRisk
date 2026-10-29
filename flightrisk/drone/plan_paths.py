"""Plan-frame geometry for search plans edited on the dashboard map.

Everything here is in whole centimetres in the *plan frame* of the fleet
coordinator (``flightrisk.drone.coordinator``): ``x`` grows to the right of
the takeoff heading and ``y`` grows forward along it. Nothing here knows about
latitude and longitude; ``flightrisk.drone.geo`` converts for the map.

The module provides:

* ``snap_path`` and ``path_to_lanes``: turn a drawn polyline into the moves the
  edge lane executor can really fly. The executor accepts only ``forward``,
  ``back``, ``left`` and ``right`` moves of 20-500 cm and never rotates
  (``flightrisk.drone.lane_plan.parse_lanes``), so a slanted segment becomes
  an axis-aligned staircase (see ``staircase``). The snapped path passes
  exactly through every drawn vertex and stays less than
  ``SNAP_TOLERANCE_CM`` from the drawn path.
* ``geofence_violation``: the first point of a path outside the square
  geofence around its takeoff point.
* ``closest_approach``: the smallest horizontal distance between two drones'
  paths, timing-independent.
* ``coverage``: the part of a search area that a square camera footprint
  swept along the paths sees, and the rectangles it misses.
* ``split_area`` and ``pattern_vertices``: the zone split of the coordinator
  and the search patterns fitted to a zone, as drawn vertices.

``flightrisk/dashboard/static/ui/plan_geom.js`` mirrors ``staircase``,
``snap_path`` and ``path_to_lanes`` for live display in the browser; the test
vectors in ``tests/fixtures/plan_geometry_vectors.json`` keep the two equal.
The dashboard server's result is the one that counts.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from flightrisk.drone.safety import MAX_MOVE_CM, MIN_MOVE_CM
from flightrisk.drone.search import generate_parallel_track, generate_track_line

__all__ = [
    "MAX_PATH_MOVES",
    "MAX_VERTICES",
    "PATTERNS",
    "SNAP_TARGET_CM",
    "SNAP_TOLERANCE_CM",
    "ConvertedPath",
    "Rect",
    "closest_approach",
    "count_moves",
    "coverage",
    "geofence_violation",
    "path_to_lanes",
    "pattern_vertices",
    "simplify",
    "snap_path",
    "split_area",
    "staircase",
    "trace_lanes",
]

Point = tuple[int, int]

SNAP_TARGET_CM = 25
"""A staircase step's corner is kept this close to the drawn segment when the 20 cm minimum move allows it."""

SNAP_TOLERANCE_CM = 40
"""Guaranteed bound: no point of a snapped path is this far, or farther, from the drawn path."""

MAX_VERTICES = 200
"""Most vertices one drawn path may have."""

MAX_PATH_MOVES = 1024
"""Most moves one path may need: the lane cap of one ``assign_lanes`` (``fleet_protocol.MAX_LANES``)."""

PATTERNS = ("parallel_track", "expanding_square", "track_line", "sector", "spiral")
"""The search patterns the map offers, the same names as ``search.PatternType``."""

_VECTORS = {"forward": (0, 1), "back": (0, -1), "right": (1, 0), "left": (-1, 0)}
_OPPOSITE = {"forward": "back", "back": "forward", "right": "left", "left": "right"}
_FENCE_SLACK_CM = 1e-6


def _direction(dx: int, dy: int) -> str:
    if dx and dy:
        raise ValueError(f"move ({dx}, {dy}) is not along one axis")
    if dx > 0:
        return "right"
    if dx < 0:
        return "left"
    return "forward" if dy > 0 else "back"


def _parts(total: int, count: int) -> list[int]:
    """``count`` whole parts adding up to ``total``, as equal as possible, the larger ones first."""
    base, extra = divmod(total, count)
    return [base + 1] * extra + [base] * (count - extra)


def _point(value: Sequence[float]) -> Point:
    return round(value[0]), round(value[1])


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle with its near-left corner at ``(x, y)``, ``w`` wide and ``h`` deep."""

    x: int
    y: int
    w: int
    h: int

    @property
    def x1(self) -> int:
        return self.x + self.w

    @property
    def y1(self) -> int:
        return self.y + self.h

    @property
    def center(self) -> Point:
        return self.x + self.w // 2, self.y + self.h // 2

    def contains(self, point: Sequence[float]) -> bool:
        return self.x <= point[0] <= self.x1 and self.y <= point[1] <= self.y1

    def to_dict(self) -> dict:
        return {"x_cm": self.x, "y_cm": self.y, "w_cm": self.w, "h_cm": self.h}

    @classmethod
    def bounding(cls, points: Iterable[Sequence[float]]) -> Rect:
        pts = list(points)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, y0 = math.floor(min(xs)), math.floor(min(ys))
        return cls(x0, y0, math.ceil(max(xs)) - x0, math.ceil(max(ys)) - y0)


# ----------------------------------------------------------------------
# Snapping a drawn path to executable moves
# ----------------------------------------------------------------------


def _steps(ax: int, ay: int) -> int:
    """Number of staircase steps for a slanted segment with components ``ax``, ``ay`` (both > 0)."""
    most = max(1, min(ax, ay) // MIN_MOVE_CM)
    wanted = math.ceil(ax * ay / (SNAP_TARGET_CM * math.hypot(ax, ay)) - 1e-9)
    return min(most, max(1, wanted))


def staircase(start: Sequence[int], end: Sequence[int]) -> list[Point]:
    """The corner points of the axis-aligned path from ``start`` to ``end``, ending exactly at ``end``.

    A segment along one axis is returned as its end point. A slanted one
    becomes ``k`` steps; each step moves along the axis of the larger
    component first, then along the other, and the step lengths add up
    exactly. ``k`` is the smallest count that keeps every step corner within
    ``SNAP_TARGET_CM`` of the segment, but never so large that a step on
    either axis is shorter than the 20 cm minimum move; a segment whose
    smaller component is below 20 cm is flown as an L. Either way every
    corner is less than ``SNAP_TOLERANCE_CM`` from the segment.
    """
    x, y = int(start[0]), int(start[1])
    ex, ey = int(end[0]), int(end[1])
    dx, dy = ex - x, ey - y
    if dx == 0 and dy == 0:
        return []
    if dx == 0 or dy == 0:
        return [(ex, ey)]
    ax, ay = abs(dx), abs(dy)
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    k = _steps(ax, ay)
    xs, ys = _parts(ax, k), _parts(ay, k)
    points: list[Point] = []
    for step_x, step_y in zip(xs, ys):
        if ax >= ay:
            x += sx * step_x
            points.append((x, y))
            y += sy * step_y
        else:
            y += sy * step_y
            points.append((x, y))
            x += sx * step_x
        points.append((x, y))
    return points


def simplify(points: Iterable[Sequence[float]]) -> list[Point]:
    """Round to whole centimetres, drop repeated points and interior points on a straight run.

    Only points that continue in the same direction are dropped (a path that
    doubles back keeps its turning point). Used for generated patterns, whose
    vertices are presented for editing; drawn paths are never simplified.
    """
    out: list[Point] = []
    for raw in points:
        point = _point(raw)
        if out and point == out[-1]:
            continue
        if len(out) >= 2:
            (ax, ay), (bx, by) = out[-2], out[-1]
            cross = (bx - ax) * (point[1] - by) - (by - ay) * (point[0] - bx)
            dot = (bx - ax) * (point[0] - bx) + (by - ay) * (point[1] - by)
            if cross == 0 and dot > 0:
                out[-1] = point
                continue
        out.append(point)
    return out


def snap_path(vertices: Sequence[Sequence[int]]) -> tuple[list[Point], list[int]]:
    """The axis-aligned corner points of a drawn path, and for each the drawn segment it belongs to.

    The first point is the first vertex (segment 0). Every later point
    carries the 1-based index of the drawn vertex that ends its segment, so a
    problem found on the snapped path can be shown on the vertex the operator
    dragged.
    """
    if not vertices:
        return [], []
    pts = [_point(v) for v in vertices]
    out = [pts[0]]
    segment = [0]
    for index in range(1, len(pts)):
        for point in staircase(pts[index - 1], pts[index]):
            out.append(point)
            segment.append(index)
    return out, segment


def _leg_count(distance: int) -> int:
    if distance == 0:
        return 0
    return 2 if distance < MIN_MOVE_CM else math.ceil(distance / MAX_MOVE_CM)


def count_moves(vertices: Sequence[Sequence[int]]) -> int:
    """How many moves ``path_to_lanes`` makes for ``vertices``, computed without building them."""
    pts = [_point(v) for v in vertices]
    total = 0
    for (x0, y0), (x1, y1) in itertools.pairwise(pts):
        ax, ay = abs(x1 - x0), abs(y1 - y0)
        if ax == 0 or ay == 0:
            total += _leg_count(ax + ay)
            continue
        k = _steps(ax, ay)
        total += sum(_leg_count(p) for p in _parts(ax, k)) + sum(_leg_count(p) for p in _parts(ay, k))
    return total


def _leg_moves(start: Point, end: Point, inside: Callable[[Point], bool] | None) -> list[tuple[str, int]]:
    """Executor moves for one axis-aligned leg.

    A leg of 20 cm or more is split into equal moves of at most 500 cm. A
    shorter one is flown as an overshoot pair: 20 cm past the end and back,
    or, when that extra point would be outside ``inside``, 20 cm back first
    and then through to the end. The geofence check sees the extra point
    either way.
    """
    dx, dy = end[0] - start[0], end[1] - start[1]
    distance = abs(dx) + abs(dy)
    if distance == 0:
        return []
    direction = _direction(dx, dy)
    if distance >= MIN_MOVE_CM:
        return [(direction, part) for part in _parts(distance, math.ceil(distance / MAX_MOVE_CM))]
    ux, uy = _VECTORS[direction]
    past_end = (end[0] + ux * MIN_MOVE_CM, end[1] + uy * MIN_MOVE_CM)
    before_start = (start[0] - ux * MIN_MOVE_CM, start[1] - uy * MIN_MOVE_CM)
    if inside is not None and not inside(past_end) and inside(before_start):
        return [(_OPPOSITE[direction], MIN_MOVE_CM), (direction, distance + MIN_MOVE_CM)]
    return [(direction, distance + MIN_MOVE_CM), (_OPPOSITE[direction], MIN_MOVE_CM)]


def trace_lanes(start: Sequence[int], lanes: Iterable[dict]) -> list[Point]:
    """Every position a drone reaches flying ``lanes`` from ``start``, ``start`` included."""
    x, y = int(start[0]), int(start[1])
    out = [(x, y)]
    for lane in lanes:
        ux, uy = _VECTORS[lane["direction"]]
        x += ux * int(lane["distance_cm"])
        y += uy * int(lane["distance_cm"])
        out.append((x, y))
    return out


def _distance_to_segment(p: Sequence[float], a: Sequence[float], b: Sequence[float]) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


@dataclass(frozen=True)
class ConvertedPath:
    """A drawn path turned into executor moves.

    ``lanes`` are the ``{"direction", "distance_cm"}`` moves, flown from
    ``trace[0]`` (the takeoff point, the first drawn vertex). ``trace`` holds
    every position after each move, and ``trace_segment[i]`` the drawn
    vertex that ends the segment ``trace[i]`` belongs to (0 for the start).
    ``max_deviation_cm`` is the largest distance from a traced point to its
    drawn segment; the snapped path's farthest points are always traced
    points, because between them the path runs straight.
    """

    drawn: tuple[Point, ...]
    lanes: tuple[dict, ...]
    trace: tuple[Point, ...]
    trace_segment: tuple[int, ...]
    max_deviation_cm: float

    @property
    def takeoff(self) -> Point:
        return self.trace[0]


def path_to_lanes(vertices: Sequence[Sequence[int]], *, fence_cm: float | None = None) -> ConvertedPath:
    """Convert a drawn path into executor moves (see the module docstring).

    ``fence_cm`` is the geofence half-width around the first vertex; it only
    decides which way a short leg's overshoot goes. Raises ``ValueError`` for
    fewer than two vertices, or a path needing more than ``MAX_PATH_MOVES``
    moves (checked before any move is built).
    """
    drawn = tuple(_point(v) for v in vertices)
    if len(drawn) < 2:
        raise ValueError("a path needs at least two vertices")
    moves_needed = count_moves(drawn)
    if moves_needed > MAX_PATH_MOVES:
        raise ValueError(
            f"the path needs {moves_needed} moves, above the {MAX_PATH_MOVES} move cap of one assign_lanes; "
            "use fewer slanted segments or a smaller area"
        )
    takeoff = drawn[0]
    inside = None
    if fence_cm is not None:
        limit = float(fence_cm) + _FENCE_SLACK_CM

        def inside(point: Point) -> bool:
            return abs(point[0] - takeoff[0]) <= limit and abs(point[1] - takeoff[1]) <= limit

    corners, corner_segment = snap_path(drawn)
    lanes: list[dict] = []
    trace = [takeoff]
    trace_segment = [0]
    for (start, end), segment in zip(itertools.pairwise(corners), corner_segment[1:]):
        x, y = start
        for direction, distance in _leg_moves(start, end, inside):
            lanes.append({"direction": direction, "distance_cm": distance})
            ux, uy = _VECTORS[direction]
            x, y = x + ux * distance, y + uy * distance
            trace.append((x, y))
            trace_segment.append(segment)
    deviation = 0.0
    for point, segment in zip(trace, trace_segment):
        if segment == 0:
            continue
        deviation = max(deviation, _distance_to_segment(point, drawn[segment - 1], drawn[segment]))
    return ConvertedPath(drawn, tuple(lanes), tuple(trace), tuple(trace_segment), deviation)


# ----------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------


def geofence_violation(path: ConvertedPath, fence_cm: float) -> tuple[int, Point] | None:
    """The first traced point outside the square geofence around the takeoff point, or None.

    Returns ``(vertex, point)``: the drawn vertex that ends the offending
    segment, and the point (in the plan frame). The check uses the same
    slack as ``SafetyEnvelope.inside_fence``.
    """
    limit = float(fence_cm) + _FENCE_SLACK_CM
    tx, ty = path.takeoff
    for point, segment in zip(path.trace, path.trace_segment):
        if abs(point[0] - tx) > limit or abs(point[1] - ty) > limit:
            return segment, point
    return None


def _segment_boxes(trace: Sequence[Point]) -> np.ndarray:
    pts = np.asarray(trace, dtype=float)
    if len(pts) == 1:
        pts = np.vstack([pts, pts])
    a, b = pts[:-1], pts[1:]
    return np.column_stack([np.minimum(a[:, 0], b[:, 0]), np.minimum(a[:, 1], b[:, 1]),
                            np.maximum(a[:, 0], b[:, 0]), np.maximum(a[:, 1], b[:, 1])])


def _closest_on_boxes(a: np.ndarray, b: np.ndarray) -> tuple[tuple[float, float], tuple[float, float]]:
    """A closest pair of points between two axis-aligned boxes ``[x0, y0, x1, y1]``."""
    def axis(a0: float, a1: float, b0: float, b1: float) -> tuple[float, float]:
        if a1 < b0:
            return a1, b0
        if b1 < a0:
            return a0, b1
        shared = max(a0, b0)
        return shared, shared

    ax, bx = axis(float(a[0]), float(a[2]), float(b[0]), float(b[2]))
    ay, by = axis(float(a[1]), float(a[3]), float(b[1]), float(b[3]))
    return (ax, ay), (bx, by)


def closest_approach(
    trace_a: Sequence[Point], trace_b: Sequence[Point]
) -> tuple[float, tuple[float, float], tuple[float, float]]:
    """Smallest horizontal distance between two traced paths, and where it occurs.

    Both paths are axis-aligned, so every segment is a degenerate box and the
    distance between two segments is the distance between their boxes. The
    result does not depend on timing: it is the closest any point of one
    path ever comes to any point of the other. Returns ``(distance,
    point_on_a, point_on_b)``.
    """
    boxes_a = _segment_boxes(trace_a)
    boxes_b = _segment_boxes(trace_b)
    dx = np.maximum(0.0, np.maximum(boxes_b[None, :, 0] - boxes_a[:, None, 2], boxes_a[:, None, 0] - boxes_b[None, :, 2]))
    dy = np.maximum(0.0, np.maximum(boxes_b[None, :, 1] - boxes_a[:, None, 3], boxes_a[:, None, 1] - boxes_b[None, :, 3]))
    dist = np.hypot(dx, dy)
    i, j = np.unravel_index(int(np.argmin(dist)), dist.shape)
    pa, pb = _closest_on_boxes(boxes_a[i], boxes_b[j])
    return float(dist[i, j]), pa, pb


def coverage(
    area: Rect, traces: Iterable[Sequence[Point]], sweep_cm: float, *, max_cells: int = 400, max_rects: int = 200
) -> tuple[float, list[dict], int]:
    """What a square camera footprint of side ``sweep_cm`` sees of ``area`` along ``traces``.

    The footprint is centred under the drone and keeps its orientation, so
    sweeping it along an axis-aligned segment covers that segment's box
    grown by ``sweep_cm / 2`` on every side. The area is measured on cells of
    ``max(10, ceil(longest side / max_cells))`` cm; a cell counts as seen
    when its centre is. Returns ``(fraction_seen, uncovered, cell_cm)``,
    where ``uncovered`` lists at most ``max_rects`` rectangles (largest first)
    of cells no footprint sees, as ``{"x_cm", "y_cm", "w_cm", "h_cm"}``.
    """
    cell = max(10, math.ceil(max(area.w, area.h) / max_cells))
    nx = max(1, math.ceil(area.w / cell))
    ny = max(1, math.ceil(area.h / cell))
    xs = area.x + np.minimum((np.arange(nx) + 0.5) * cell, area.w)
    ys = area.y + np.minimum((np.arange(ny) + 0.5) * cell, area.h)
    widths = np.minimum(cell, area.w - np.arange(nx) * cell).clip(min=0)
    heights = np.minimum(cell, area.h - np.arange(ny) * cell).clip(min=0)
    seen = np.zeros((ny, nx), dtype=bool)
    half = sweep_cm / 2.0
    for trace in traces:
        for box in _segment_boxes(trace):
            cols = np.nonzero((xs >= box[0] - half) & (xs <= box[2] + half))[0]
            rows = np.nonzero((ys >= box[1] - half) & (ys <= box[3] + half))[0]
            if cols.size and rows.size:
                seen[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1] = True
    weights = np.outer(heights, widths)
    total = float(weights.sum())
    fraction = float((weights * seen).sum() / total) if total > 0 else 1.0

    # Merge unseen cells into rectangles: runs along each row, then identical
    # runs on consecutive rows.
    open_runs: dict[tuple[int, int], int] = {}
    rects: list[tuple[int, int, int, int]] = []
    for row in range(ny + 1):
        runs: set[tuple[int, int]] = set()
        if row < ny:
            col = 0
            unseen = ~seen[row]
            while col < nx:
                if unseen[col]:
                    start = col
                    while col < nx and unseen[col]:
                        col += 1
                    runs.add((start, col))
                else:
                    col += 1
        for run in list(open_runs):
            if run not in runs:
                rects.append((run[0], open_runs.pop(run), run[1], row))
        for run in runs:
            open_runs.setdefault(run, row)
    uncovered = []
    for c0, r0, c1, r1 in rects:
        x0 = area.x + c0 * cell
        y0 = area.y + r0 * cell
        x1 = min(area.x + c1 * cell, area.x1)
        y1 = min(area.y + r1 * cell, area.y1)
        uncovered.append({"x_cm": int(x0), "y_cm": int(y0), "w_cm": int(x1 - x0), "h_cm": int(y1 - y0)})
    uncovered.sort(key=lambda r: (-r["w_cm"] * r["h_cm"], r["y_cm"], r["x_cm"]))
    return fraction, uncovered[:max_rects], cell


# ----------------------------------------------------------------------
# Zones and patterns
# ----------------------------------------------------------------------


def split_area(area: Rect, count: int, gap_cm: int) -> list[Rect]:
    """Split ``area`` into ``count`` side-by-side zones ``gap_cm`` apart, as ``FleetCoordinator.plan`` does."""
    if count < 1:
        raise ValueError("at least one zone is needed")
    zone_w = (area.w - gap_cm * (count - 1)) // count
    if zone_w < MIN_MOVE_CM:
        raise ValueError(f"area width {area.w} cm is too narrow for {count} zones with {gap_cm} cm gaps")
    return [Rect(area.x + i * (zone_w + gap_cm), area.y, zone_w, area.h) for i in range(count)]


def _trace_waypoints(start: Point, waypoints: Iterable) -> list[Point]:
    """Positions after each legacy ``search.Waypoint`` move, flown without turning."""
    x, y = start
    out = [(x, y)]
    for wp in waypoints:
        if wp.direction not in _VECTORS:
            continue
        ux, uy = _VECTORS[wp.direction]
        x += ux * wp.distance_cm
        y += uy * wp.distance_cm
        out.append((x, y))
    return out


def _transit_to(start: Point, target: Point) -> list[Point]:
    """From the takeoff corner to ``target``: forward first, then sideways (``coordinator._transit``)."""
    return [start, (start[0], target[1]), target]


def _polar(center: Point, radius: float, bearing_deg: float) -> tuple[float, float]:
    rad = math.radians(bearing_deg)
    return center[0] + radius * math.sin(rad), center[1] + radius * math.cos(rad)


def pattern_vertices(pattern: str, zone: Rect, strip_cm: int) -> list[Point]:
    """A search pattern fitted to ``zone``, as the drawn vertices of a path from the zone's near-left corner.

    ``parallel_track`` is the coordinator's pattern (``search.generate_parallel_track``
    for the zone). ``expanding_square`` and ``spiral`` start at the zone
    centre, ``sector`` flies out to the zone's inscribed circle and back
    six times, and ``track_line`` sweeps along the zone's centre line. Sector
    and spiral have slanted legs; ``path_to_lanes`` snaps them.
    """
    corner = (zone.x, zone.y)
    strip = max(MIN_MOVE_CM, int(strip_cm))
    if pattern == "parallel_track":
        return simplify(_trace_waypoints(corner, generate_parallel_track(zone.w, zone.h, strip)))
    if pattern == "track_line":
        centre_line = (zone.x + zone.w // 2, zone.y)
        sweep = max(MIN_MOVE_CM, min(strip, zone.w // 2))
        # The legacy generator flies ten forward segments of length // 7, so
        # pass a length that keeps the whole track inside the zone's depth.
        length = max(7 * MIN_MOVE_CM, (zone.h // 10) * 7)
        return simplify([corner] + _trace_waypoints(centre_line, generate_track_line(length, sweep, 3)))
    centre = zone.center
    points: list[Sequence[float]] = list(_transit_to(corner, centre))
    if pattern == "expanding_square":
        x, y = centre
        order = ((0, 1), (1, 0), (0, -1), (-1, 0))
        for leg in range(4 * 50):
            ux, uy = order[leg % 4]
            length = strip * (1 + leg // 2)
            tx, ty = x + ux * length, y + uy * length
            nx = min(max(tx, zone.x), zone.x1)
            ny = min(max(ty, zone.y), zone.y1)
            if abs(nx - x) + abs(ny - y) >= MIN_MOVE_CM:
                x, y = nx, ny
                points.append((x, y))
            if (nx, ny) != (tx, ty):
                # The leg reached the zone edge: the square is as large as the zone allows.
                break
        return simplify(points)
    # Short slanted legs may be flown with a 20 cm overshoot, so the round
    # patterns keep one minimum move away from the zone's edges.
    radius = max(MIN_MOVE_CM, min(zone.w, zone.h) // 2 - MIN_MOVE_CM)
    if pattern == "sector":
        for i in range(6):
            points.append(_polar(centre, radius, 60 * i))
            points.append(centre)
        return simplify(points)
    if pattern == "spiral":
        # An outward spiral of three turns sampled every 45 degrees. Samples
        # closer than two minimum moves to the previous one are skipped, so
        # the tight inner turns do not become a string of overshoot pairs.
        turns, per_turn = 3, 8
        last = centre
        for k in range(1, turns * per_turn + 1):
            point = _polar(centre, radius * k / (turns * per_turn), 360.0 * k / per_turn)
            if k < turns * per_turn and math.hypot(point[0] - last[0], point[1] - last[1]) < 2 * MIN_MOVE_CM:
                continue
            points.append(point)
            last = point
        return simplify(points)
    raise ValueError(f"unknown pattern {pattern!r}; choose one of {', '.join(PATTERNS)}")
