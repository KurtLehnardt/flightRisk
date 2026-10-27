"""Data model of a fleet search plan, and turning axis-aligned polylines into executor moves.

``flightrisk.drone.fleet_patterns`` builds these objects and re-exports every
public name here, so callers import from ``fleet_patterns`` only. The frame,
the takeoff/zone contract and the separation rule are documented there.

The ``assign_lanes`` gate fails closed. Plans, drone plans and their lanes
are immutable (lanes are ``FrozenLane`` mappings). ``FleetSearchPlan.feasible``
is true only for a plan whose content still matches what ``check_plan``
passed when it was built (``checked_digest``), so a plan rebuilt with
``dataclasses.replace`` or constructed by hand is not feasible until it is
checked again. ``assign_lanes_message`` runs ``check_plan`` again itself and
refuses on any problem, and also refuses a plan built with a safety check
switched off (``FleetSearchPlan.unchecked``) unless the caller passes
``allow_unchecked=True``.
"""

from __future__ import annotations

import hashlib
import itertools
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from flightrisk.drone import coverage_sim as cs
from flightrisk.drone.fleet_plan_check import (
    DEFAULT_PLACEMENT_TOLERANCE_CM,
    MAX_MOVES_PER_DRONE,
    SQUARE_FOOTPRINT,
    PlanningFootprint,
    check_plan,
)
from flightrisk.drone.safety import MAX_MOVE_CM, MIN_MOVE_CM
from flightrisk.fleet_protocol import make_assign_lanes

__all__ = [
    "DronePlan",
    "FleetSearchPlan",
    "FrozenLane",
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


class FrozenLane(dict):
    """One immutable ``{"direction", "distance_cm"}`` lane.

    A ``dict`` (so the executor's ``parse_lanes`` and JSON accept it) whose
    mutating methods raise ``TypeError``. ``dict(lane)`` is a mutable copy.
    """

    def _refuse(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("plan lanes are immutable; build a new plan instead")

    __setitem__ = __delitem__ = __ior__ = _refuse
    clear = pop = popitem = setdefault = update = _refuse

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(tuple(sorted(self.items())))

    def __reduce__(self) -> tuple:
        return (FrozenLane, (dict(self),))


def _freeze(lane: Any) -> Any:
    """A ``FrozenLane`` copy of a lane mapping; anything else is kept for ``check_plan`` to report."""
    return FrozenLane(lane) if isinstance(lane, Mapping) else lane


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
    ``takeoff_cm``, each already within the Tello's 20-500 cm range; they are
    stored as an immutable tuple of ``FrozenLane``. The first
    ``transit_lanes`` of them fly from the takeoff point to the start of the
    search pattern. ``start_offset_s`` is how long after the fleet start this
    drone should start.
    """

    drone_id: str
    takeoff_cm: Point
    region: Region
    alt_cm: int
    lanes: tuple[dict, ...]
    transit_lanes: int = 0
    start_offset_s: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "lanes", tuple(_freeze(lane) for lane in self.lanes))
        object.__setattr__(self, "takeoff_cm", tuple(self.takeoff_cm))

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

    ``problems`` lists every reason ``check_plan`` found when the plan was
    built (a path that leaves its geofence, a separation below the required
    minimum, coverage below 99.5% with the gate cameras). A plan with
    problems is still returned so the operator can see why it was refused.
    ``checked_digest`` is the ``content_digest`` of what was checked, and
    ``feasible`` is true only while it still matches and there were no
    problems. ``standoff_cm`` is how far behind the area's near edge drones
    fly, and ``max_standoff_cm`` the most that was allowed; ``coverage`` is
    the fraction of the area the gate cameras see.
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
    max_standoff_cm: float | None = None
    checked_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "drones", tuple(self.drones))
        object.__setattr__(self, "problems", tuple(self.problems))

    def content_digest(self) -> str:
        """SHA-256 of everything ``check_plan`` reads (not ``reason``, ``lkp_cm``, ``problems`` or ``coverage``)."""
        footprint = None
        if self.footprint is not None:
            footprint = (self.footprint.to_dict(), repr(self.footprint.source))
        content = (
            self.area_cm, self.sweep_cm, self.gap_cm, self.standoff_cm, self.geofence_cm, self.min_separation_cm,
            self.placement_tolerance_cm, self.max_standoff_cm, footprint,
            tuple(
                (d.drone_id, d.takeoff_cm, d.region, d.alt_cm, d.transit_lanes, d.start_offset_s,
                 tuple(tuple(sorted(lane.items())) if isinstance(lane, Mapping) else repr(lane) for lane in d.lanes))
                for d in self.drones
            ),
        )
        return hashlib.sha256(repr(content).encode("utf-8")).hexdigest()

    @property
    def feasible(self) -> bool:
        """No problems were found, and the plan has not changed since it was checked."""
        return not self.problems and self.checked_digest is not None and self.checked_digest == self.content_digest()

    @property
    def unchecked(self) -> tuple[str, ...]:
        """The safety checks this plan was built without; non-empty means not flyable by default."""
        reasons = []
        if self.footprint is None or self.footprint.kind == SQUARE_FOOTPRINT:
            reasons.append("coverage is modelled with the idealised square footprint, not the real camera")
        if self.geofence_cm is None:
            reasons.append("geofence_cm is None (no geofence check)")
        if self.min_separation_cm is None and len(self.drones) > 1:
            reasons.append("min_separation_cm is None (no minimum separation check)")
        if self.max_standoff_cm is None:
            reasons.append("max_standoff_cm is None (no standoff check)")
        return tuple(reasons)

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
        """The real camera footprint of every drone holding exactly its altitude slot (for estimates).

        The coverage gate uses the smaller ``fleet_plan_check.gate_footprints`` instead.
        """
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

    def assign_lanes_message(
        self, drone_id: str, *, allow_unchecked: bool = False, allow_infeasible: bool = False
    ) -> dict[str, Any]:
        """A validated ``assign_lanes`` message for one drone of a flyable plan.

        Runs ``check_plan`` on the plan as it is now (not the problems
        recorded when it was built) and raises ``ValueError`` on any problem.
        Also raises for a plan in ``unchecked`` (a safety check switched off,
        or the square model) unless ``allow_unchecked`` is true, an explicit
        override for a model study or a test. ``allow_infeasible`` skips every
        check; it is only for previews and tests.
        """
        if not allow_infeasible:
            problems = check_plan(self)
            if problems:
                raise ValueError(f"refusing assign_lanes for {drone_id}: the plan is infeasible: " + "; ".join(problems))
            if self.unchecked and not allow_unchecked:
                raise ValueError(
                    f"refusing assign_lanes for {drone_id}: the plan was built without safety checks ("
                    + "; ".join(self.unchecked)
                    + "); pass allow_unchecked=True only for a model study or a test"
                )
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
            "max_standoff_cm": self.max_standoff_cm,
            "coverage": self.coverage,
            "feasible": self.feasible,
            "unchecked": list(self.unchecked),
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


def _move_count(start: Point, end: Point) -> int:
    """How many moves ``_leg_moves`` makes for one leg (an overshoot pair for a short one)."""
    distance = abs(end[0] - start[0]) + abs(end[1] - start[1])
    if distance == 0:
        return 0
    return 2 if distance < MIN_MOVE_CM else max(1, math.ceil(distance / MAX_MOVE_CM))


def _check_move_cap(count: int) -> None:
    if count > MAX_MOVES_PER_DRONE:
        raise ValueError(
            f"the pattern needs {count} moves for one drone, above the {MAX_MOVES_PER_DRONE} move cap; "
            "use a wider sweep or a smaller area"
        )


def _polyline_lanes(points: Iterable[Point], region: Region) -> list[dict]:
    pts = _simplify(points)
    for point in pts:
        if not region.contains(point):
            raise AssertionError(f"internal error: waypoint {point} outside region {region}")
    # Count before building, so an oversized pattern is refused without allocating it.
    _check_move_cap(sum(_move_count(start, end) for start, end in itertools.pairwise(pts)))
    lanes: list[dict] = []
    for start, end in itertools.pairwise(pts):
        lanes.extend(_leg_moves(start, end, region))
    return lanes
