"""Data model of a fleet search plan, and turning axis-aligned polylines into executor moves.

``flightrisk.drone.fleet_patterns`` builds these objects and re-exports every
public name here, so callers import from ``fleet_patterns`` only. The frame,
the takeoff/zone contract and the separation rule are documented there.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from flightrisk.drone import coverage_sim as cs
from flightrisk.drone.fleet_plan_check import (
    DEFAULT_PLACEMENT_TOLERANCE_CM,
    MAX_MOVES_PER_DRONE,
    SQUARE_FOOTPRINT,
    PlanningFootprint,
)
from flightrisk.drone.safety import MAX_MOVE_CM, MIN_MOVE_CM
from flightrisk.fleet_protocol import make_assign_lanes

__all__ = [
    "DronePlan",
    "FleetSearchPlan",
    "PatternChoice",
    "PatternKind",
    "Point",
    "Region",
]

_DIRECTION_FOR = {
    (1, 0): "right",
    (-1, 0): "left",
    (0, 1): "forward",
    (0, -1): "back",
}
_VECTOR_FOR = {name: vector for vector, name in _DIRECTION_FOR.items()}
_OPPOSITE = {"right": "left", "left": "right", "forward": "back", "back": "forward"}

Point = tuple[int, int]


class PatternKind(str, Enum):
    PARALLEL_SPLIT = "parallel_split"
    SPLIT_EXPANDING_SQUARE = "split_expanding_square"
    SQUARE_SPIRAL = "square_spiral"


@dataclass(frozen=True)
class Region:
    """A closed axis-aligned rectangle ``[x0, x1] x [y0, y1]`` in the fleet frame."""

    x0: int
    y0: int
    x1: int
    y1: int

    def __post_init__(self) -> None:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError(f"empty region {self!r}")

    def contains(self, point: Sequence[float]) -> bool:
        x, y = point
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def distance_to(self, other: Region) -> float:
        """Smallest horizontal distance between a point of this region and one of ``other``."""
        dx = max(0, other.x0 - self.x1, self.x0 - other.x1)
        dy = max(0, other.y0 - self.y1, self.y0 - other.y1)
        return math.hypot(dx, dy)

    def zone_dict(self) -> dict:
        """The rectangle as ``x_cm``/``y_cm`` (near-left corner), ``w_cm`` and ``h_cm``."""
        return {"x_cm": self.x0, "y_cm": self.y0, "w_cm": self.x1 - self.x0, "h_cm": self.y1 - self.y0}


@dataclass(frozen=True)
class DronePlan:
    """One drone's part of a fleet plan.

    ``lanes`` are ``{"direction", "distance_cm"}`` relative moves from
    ``takeoff_cm``, each already within the Tello's 20-500 cm range. The
    first ``transit_lanes`` of them fly from the takeoff point to the start
    of the search pattern. ``start_offset_s`` is how long after the fleet
    start this drone should start.
    """

    drone_id: str
    takeoff_cm: Point
    region: Region
    alt_cm: int
    lanes: tuple[dict, ...]
    transit_lanes: int = 0
    start_offset_s: float = 0.0

    def waypoints(self) -> list[Point]:
        """Fleet-frame positions: the takeoff point, then the end of every lane."""
        x, y = self.takeoff_cm
        points = [(x, y)]
        for lane in self.lanes:
            dx, dy = _VECTOR_FOR[lane["direction"]]
            x += dx * lane["distance_cm"]
            y += dy * lane["distance_cm"]
            points.append((x, y))
        return points

    def relative_waypoints(self) -> list[Point]:
        """Positions relative to the takeoff point, as the edge dead-reckons them."""
        x0, y0 = self.takeoff_cm
        return [(x - x0, y - y0) for x, y in self.waypoints()]

    @property
    def path_length_cm(self) -> int:
        return sum(lane["distance_cm"] for lane in self.lanes)

    @property
    def pattern_start_cm(self) -> Point:
        return self.waypoints()[self.transit_lanes]

    def without_transit(self) -> DronePlan:
        """The same plan taking off at the pattern start, with no transit and no start offset."""
        return replace(
            self,
            takeoff_cm=self.pattern_start_cm,
            lanes=self.lanes[self.transit_lanes:],
            transit_lanes=0,
            start_offset_s=0.0,
        )

    def zone_dict(self) -> dict:
        """The ``zone`` of this drone's ``assign_lanes``: its region plus its takeoff point.

        ``x_cm``/``y_cm``/``w_cm``/``h_cm`` are the region that contains the
        whole path. ``takeoff_x_cm``/``takeoff_y_cm`` are the fleet-frame
        origin of the drone's relative lanes and telemetry; they are generally
        not the zone corner.
        """
        zone = self.region.zone_dict()
        zone["takeoff_x_cm"], zone["takeoff_y_cm"] = self.takeoff_cm
        return zone

    def assign_lanes_fields(self) -> dict:
        """The ``alt_cm``, ``lanes`` and ``zone`` fields of this drone's ``assign_lanes``."""
        return {"alt_cm": self.alt_cm, "lanes": [dict(lane) for lane in self.lanes], "zone": self.zone_dict()}

    def to_dict(self) -> dict:
        return {
            "drone_id": self.drone_id,
            "takeoff_cm": list(self.takeoff_cm),
            "zone": self.zone_dict(),
            "alt_cm": self.alt_cm,
            "lanes": [dict(lane) for lane in self.lanes],
            "transit_lanes": self.transit_lanes,
            "start_offset_s": self.start_offset_s,
        }


@dataclass(frozen=True)
class FleetSearchPlan:
    """A complete fleet search plan.

    ``problems`` lists every reason the plan must not be flown (a path that
    leaves its geofence, a separation below the required minimum, coverage
    below 99.5% with the real footprint). A plan with problems is still
    returned so the operator can see why it was refused; ``feasible`` is
    false for it and ``assign_lanes_message`` refuses to build its messages.
    ``standoff_cm`` is how far behind the area's near edge drones may fly;
    ``coverage`` is the simulated fraction of the area the camera sees.
    """

    pattern: PatternKind
    area_cm: tuple[int, int]
    sweep_cm: int
    gap_cm: int
    drones: tuple[DronePlan, ...]
    lkp_cm: Point | None = None
    problems: tuple[str, ...] = ()
    reason: str = ""
    footprint: PlanningFootprint | None = None
    standoff_cm: int = 0
    geofence_cm: float | None = None
    min_separation_cm: float | None = None
    placement_tolerance_cm: float = DEFAULT_PLACEMENT_TOLERANCE_CM
    coverage: float | None = None

    @property
    def feasible(self) -> bool:
        return not self.problems

    @property
    def footprint_kind(self) -> str:
        return self.footprint.kind if self.footprint is not None else SQUARE_FOOTPRINT

    @property
    def separation_cm(self) -> float:
        """Guaranteed minimum horizontal distance between any two drones (inf for one drone)."""
        best = math.inf
        for i, a in enumerate(self.drones):
            for b in self.drones[i + 1:]:
                best = min(best, a.region.distance_to(b.region))
        return best

    @property
    def flight_area(self) -> Region:
        """Where any drone may fly: the search area extended ``standoff_cm`` behind its near edge."""
        width, depth = self.area_cm
        return Region(0, -self.standoff_cm, width, depth)

    def camera_footprints(self) -> list[cs.Footprint]:
        """The real camera footprint of every drone, at its altitude slot."""
        if self.footprint is None:
            return [cs.RectFootprint.square(self.sweep_cm)] * len(self.drones)
        return [self.footprint.camera(drone.alt_cm) for drone in self.drones]

    def drone(self, drone_id: str) -> DronePlan:
        for plan in self.drones:
            if plan.drone_id == drone_id:
                return plan
        raise KeyError(drone_id)

    def placements(self) -> dict[str, tuple[int, int, int]]:
        """Required takeoff pose ``(x_cm, y_cm, heading_deg)`` per drone; heading is always 0."""
        return {plan.drone_id: (plan.takeoff_cm[0], plan.takeoff_cm[1], 0) for plan in self.drones}

    def assign_lanes_message(self, drone_id: str, *, allow_infeasible: bool = False) -> dict[str, Any]:
        """A validated ``assign_lanes`` message for one drone of a feasible plan.

        Raises ``ValueError`` for a plan with problems unless
        ``allow_infeasible`` is true, which is only for previews and tests.
        """
        if self.problems and not allow_infeasible:
            raise ValueError(f"refusing assign_lanes for {drone_id}: the plan is infeasible: " + "; ".join(self.problems))
        fields = self.drone(drone_id).assign_lanes_fields()
        return make_assign_lanes(drone_id, fields["alt_cm"], fields["lanes"], fields["zone"])

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern.value,
            "area_cm": list(self.area_cm),
            "sweep_cm": self.sweep_cm,
            "gap_cm": self.gap_cm,
            "footprint": self.footprint.to_dict() if self.footprint is not None else {"kind": SQUARE_FOOTPRINT},
            "standoff_cm": self.standoff_cm,
            "flight_area": self.flight_area.zone_dict(),
            "lkp_cm": list(self.lkp_cm) if self.lkp_cm is not None else None,
            "separation_cm": self.separation_cm if math.isfinite(self.separation_cm) else None,
            "geofence_cm": self.geofence_cm,
            "min_separation_cm": self.min_separation_cm,
            "placement_tolerance_cm": self.placement_tolerance_cm,
            "coverage": self.coverage,
            "feasible": self.feasible,
            "problems": list(self.problems),
            "reason": self.reason,
            "drones": [plan.to_dict() for plan in self.drones],
        }


@dataclass(frozen=True)
class PatternChoice:
    kind: PatternKind
    reason: str


# ----------------------------------------------------------------------
# Turning axis-aligned polylines into executor moves
# ----------------------------------------------------------------------


def _split_distance(direction: str, distance: int) -> list[dict]:
    """Moves of 20-500 cm in one direction whose lengths add up to ``distance`` (>= 20)."""
    count = max(1, math.ceil(distance / MAX_MOVE_CM))
    base, extra = divmod(distance, count)
    return [{"direction": direction, "distance_cm": base + (1 if i < extra else 0)} for i in range(count)]


def _simplify(points: Iterable[Point]) -> list[Point]:
    """Drop repeated points and merge consecutive legs that continue in the same direction."""
    out: list[Point] = []
    for point in points:
        point = (int(point[0]), int(point[1]))
        if out and point == out[-1]:
            continue
        if len(out) >= 2:
            (ax, ay), (bx, by) = out[-2], out[-1]
            same_line = (ax == bx == point[0]) or (ay == by == point[1])
            same_way = (bx - ax) * (point[0] - bx) + (by - ay) * (point[1] - by) > 0
            if same_line and same_way:
                out[-1] = point
                continue
        out.append(point)
    return out


def _leg_moves(start: Point, end: Point, region: Region) -> list[dict]:
    """Executor moves for one axis-aligned leg, never leaving ``region``.

    A leg shorter than the 20 cm minimum move is flown as an overshoot pair:
    either past the end and back, or first away from the start and then
    through to the end, whichever keeps the extra point inside the region.
    """
    dx, dy = end[0] - start[0], end[1] - start[1]
    if dx and dy:
        raise ValueError(f"leg {start} -> {end} is not axis-aligned")
    distance = abs(dx) + abs(dy)
    if distance == 0:
        return []
    unit = ((dx > 0) - (dx < 0), (dy > 0) - (dy < 0))
    direction = _DIRECTION_FOR[unit]
    if distance >= MIN_MOVE_CM:
        return _split_distance(direction, distance)
    past_end = (end[0] + unit[0] * MIN_MOVE_CM, end[1] + unit[1] * MIN_MOVE_CM)
    if region.contains(past_end):
        return [
            {"direction": direction, "distance_cm": distance + MIN_MOVE_CM},
            {"direction": _OPPOSITE[direction], "distance_cm": MIN_MOVE_CM},
        ]
    before_start = (start[0] - unit[0] * MIN_MOVE_CM, start[1] - unit[1] * MIN_MOVE_CM)
    if region.contains(before_start):
        return [
            {"direction": _OPPOSITE[direction], "distance_cm": MIN_MOVE_CM},
            {"direction": direction, "distance_cm": distance + MIN_MOVE_CM},
        ]
    raise ValueError(f"a {distance} cm leg from {start} cannot be flown inside region {region}")


def _polyline_lanes(points: Iterable[Point], region: Region) -> list[dict]:
    pts = _simplify(points)
    for point in pts:
        if not region.contains(point):
            raise AssertionError(f"internal error: waypoint {point} outside region {region}")
    lanes: list[dict] = []
    for start, end in itertools.pairwise(pts):
        lanes.extend(_leg_moves(start, end, region))
    if len(lanes) > MAX_MOVES_PER_DRONE:
        raise ValueError(
            f"the pattern needs {len(lanes)} moves for one drone, above the {MAX_MOVES_PER_DRONE} move cap; "
            "use a wider sweep or a smaller area"
        )
    return lanes
