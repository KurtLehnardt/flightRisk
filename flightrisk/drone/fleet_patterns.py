"""Axis-aligned search patterns for a fleet of relative-move drones.

Every pattern here is built only from the four horizontal moves the edge lane
executor accepts (``forward``, ``back``, ``left`` and ``right``, 20-500 cm
each). Nothing rotates, so each drone keeps the fixed-heading dead-reckoning
frame described in ``flightrisk.drone.safety``. The legacy generators in
``flightrisk.drone.search`` turn with ``rotate_degrees`` and are left
unchanged for the single-drone path.

Frame and units
---------------
All coordinates are whole centimetres in the *fleet frame*, the shared grid of
``flightrisk.drone.coordinator``: the origin is the near-left corner of the
search area, ``x`` grows to the right and ``y`` grows ahead along the takeoff
heading every drone shares. The area is ``[0, width] x [0, depth]``. Each
drone's lanes are relative moves from its own takeoff point
(``DronePlan.takeoff_cm``), so the ``rel_x_cm``/``rel_y_cm`` its edge reports
are relative to that point. Unlike today's coordinator, the takeoff point is
not necessarily the near-left corner of the drone's zone: the integration
must add ``takeoff_cm`` (not the zone corner) to place telemetry and
detections on the shared grid.

Coverage model
--------------
``sweep_cm`` is the lateral width one pass covers (the effective sweep width,
for example ``SweepModel.lane_spacing_cm()``). The planner treats the camera
footprint as a square of that width centred on the drone, as the plan's
illustrative model does. ``flightrisk.drone.coverage_sim`` measures the real
coverage of a plan for any footprint, including the forward trapezoid.

Separation rule
---------------
Every multi-drone plan gives each drone a closed, axis-aligned region
(``DronePlan.region``) that contains its whole path: the takeoff point, the
transit to the pattern start, every lane end, and the extra point of any
overshoot pair used for a leg shorter than the 20 cm minimum move. Because
lanes are straight axis-aligned moves and a rectangle is convex, a path whose
waypoints are all inside the region stays inside it between waypoints too.
Regions are pairwise at least ``gap_cm`` apart horizontally:

* parallel split: vertical strips ``[a_i, b_i]`` with ``a_{i+1} - b_i = gap_cm``;
* split expanding square: the half-planes ``x <= lkp_x - s`` and
  ``x >= lkp_x + s`` with ``s = ceil(gap_cm / 2)``, so they are ``2s >= gap_cm``
  apart.

So at every instant, whatever the timing (a slow move, a late start, a drone
that hovers), the commanded positions of two drones are at least
``FleetSearchPlan.separation_cm >= gap_cm`` apart. Separation never depends on
timing, and altitude slots are an extra layer, never the only one. With the
LKP centred between the half-planes and both drones on the same clock, the
split square is also an exact mirror image, so the drones are then
``2 * |x - lkp_x| >= 2s`` apart. Dead-reckoning drift is not part of the rule:
size ``gap_cm`` with ``required_gap_cm`` once the drift fraction has been
measured (hardware rung L3).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from flightrisk.drone.safety import HORIZONTAL_DIRECTIONS, MAX_MOVE_CM, MIN_MOVE_CM
from flightrisk.fleet_protocol import make_assign_lanes

__all__ = [
    "DEFAULT_ALT_SLOTS_CM",
    "DEFAULT_GAP_CM",
    "DEFAULT_MIN_SEPARATION_CM",
    "DEFAULT_PLACEMENT_OFFSET_CM",
    "DEFAULT_PLACEMENT_TOLERANCE_CM",
    "DEFAULT_TAKEOFF_STAGGER_S",
    "MIN_SWEEP_CM",
    "DronePlan",
    "FleetSearchPlan",
    "PatternChoice",
    "PatternKind",
    "Region",
    "check_plan",
    "choose_pattern",
    "expanding_square_lanes",
    "plan_parallel_split",
    "plan_search",
    "plan_split_expanding_square",
    "plan_square_spiral",
    "required_gap_cm",
]

DEFAULT_GAP_CM = 150
"""Default corridor between drone regions; equals the default minimum separation."""

DEFAULT_MIN_SEPARATION_CM = 150
"""Minimum horizontal separation D_min a plan must guarantee (plan section 3.5)."""

DEFAULT_PLACEMENT_OFFSET_CM = 50
"""Split square: how far outside the seam each drone takes off (plan section 3.4)."""

DEFAULT_PLACEMENT_TOLERANCE_CM = 10
"""Operator placement error assumed per drone (plan section 3.1, a guess until L3)."""

DEFAULT_TAKEOFF_STAGGER_S = 2.0
"""Split square: the second drone starts this much later, because both take off near the LKP."""

DEFAULT_ALT_SLOTS_CM = (100, 160)
"""Altitude slots assigned to drones in turn, as in the coordinator."""

_MIN_ALT_SLOT_CM = MIN_MOVE_CM
_MAX_ALT_SLOT_CM = MAX_MOVE_CM

MIN_SWEEP_CM = 2 * MIN_MOVE_CM
"""Smallest sweep width accepted. With at least two minimum moves per sweep, a
pass can always stop half a sweep (20 cm or more) inside an edge, which leaves
room for the overshoot pair a leg shorter than 20 cm needs."""

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
        """The ``zone`` object of an ``assign_lanes`` message."""
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

    def assign_lanes_fields(self) -> dict:
        """The ``alt_cm``, ``lanes`` and ``zone`` fields of this drone's ``assign_lanes``."""
        return {
            "alt_cm": self.alt_cm,
            "lanes": [dict(lane) for lane in self.lanes],
            "zone": self.region.zone_dict(),
        }

    def to_dict(self) -> dict:
        return {
            "drone_id": self.drone_id,
            "takeoff_cm": list(self.takeoff_cm),
            "zone": self.region.zone_dict(),
            "alt_cm": self.alt_cm,
            "lanes": [dict(lane) for lane in self.lanes],
            "transit_lanes": self.transit_lanes,
            "start_offset_s": self.start_offset_s,
        }


@dataclass(frozen=True)
class FleetSearchPlan:
    """A complete fleet search plan.

    ``problems`` lists every reason the plan must not be flown (a path that
    leaves its geofence, a separation below the required minimum). A plan
    with problems is still returned so the operator can see why it was
    refused; ``feasible`` is false for it.
    """

    pattern: PatternKind
    area_cm: tuple[int, int]
    sweep_cm: int
    gap_cm: int
    drones: tuple[DronePlan, ...]
    lkp_cm: Point | None = None
    problems: tuple[str, ...] = ()
    reason: str = ""

    @property
    def feasible(self) -> bool:
        return not self.problems

    @property
    def separation_cm(self) -> float:
        """Guaranteed minimum horizontal distance between any two drones (inf for one drone)."""
        best = math.inf
        for i, a in enumerate(self.drones):
            for b in self.drones[i + 1:]:
                best = min(best, a.region.distance_to(b.region))
        return best

    def drone(self, drone_id: str) -> DronePlan:
        for plan in self.drones:
            if plan.drone_id == drone_id:
                return plan
        raise KeyError(drone_id)

    def placements(self) -> dict[str, tuple[int, int, int]]:
        """Required takeoff pose ``(x_cm, y_cm, heading_deg)`` per drone; heading is always 0."""
        return {plan.drone_id: (plan.takeoff_cm[0], plan.takeoff_cm[1], 0) for plan in self.drones}

    def assign_lanes_message(self, drone_id: str) -> dict[str, Any]:
        """A validated ``assign_lanes`` message for one drone."""
        fields = self.drone(drone_id).assign_lanes_fields()
        return make_assign_lanes(drone_id, fields["alt_cm"], fields["lanes"], fields["zone"])

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern.value,
            "area_cm": list(self.area_cm),
            "sweep_cm": self.sweep_cm,
            "gap_cm": self.gap_cm,
            "lkp_cm": list(self.lkp_cm) if self.lkp_cm is not None else None,
            "separation_cm": self.separation_cm if math.isfinite(self.separation_cm) else None,
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
# Validation helpers
# ----------------------------------------------------------------------


def _int_cm(name: str, value: Any, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number of centimetres, got {value!r}")
    if value != int(value):
        raise ValueError(f"{name} must be a whole number of centimetres, got {value!r}")
    value = int(value)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum} cm, got {value}")
    return value


def _area(area_cm: Any) -> tuple[int, int]:
    if isinstance(area_cm, (int, float)) and not isinstance(area_cm, bool):
        width = depth = area_cm
    else:
        width, depth = area_cm
    return _int_cm("area width", width, MIN_MOVE_CM), _int_cm("area depth", depth, MIN_MOVE_CM)


def _ids(drone_ids: Sequence[str]) -> list[str]:
    ids = [str(d) for d in drone_ids]
    if not ids:
        raise ValueError("at least one drone id is required")
    if len(set(ids)) != len(ids):
        raise ValueError(f"drone ids must be unique, got {ids!r}")
    return ids


def _slots(alt_slots_cm: Sequence[int]) -> list[int]:
    slots = [_int_cm("altitude slot", a) for a in alt_slots_cm]
    if not slots:
        raise ValueError("alt_slots_cm must contain at least one altitude")
    for slot in slots:
        if not _MIN_ALT_SLOT_CM <= slot <= _MAX_ALT_SLOT_CM:
            raise ValueError(f"altitude slot must be within {_MIN_ALT_SLOT_CM}-{_MAX_ALT_SLOT_CM} cm, got {slot}")
    return slots


def _lkp(lkp_cm: Any, width: int, depth: int) -> Point:
    x, y = lkp_cm
    x, y = _int_cm("LKP x", x), _int_cm("LKP y", y)
    if not (0 <= x <= width and 0 <= y <= depth):
        raise ValueError(f"LKP ({x}, {y}) is outside the {width} x {depth} cm area")
    return x, y


def required_gap_cm(
    min_separation_cm: float,
    drift_fraction: float,
    path_length_cm: float,
    placement_tolerance_cm: float = DEFAULT_PLACEMENT_TOLERANCE_CM,
) -> int:
    """Gap that keeps ``min_separation_cm`` despite drift and placement error on both drones.

    Implements ``gap >= D_min + 2 * (k * L + placement tolerance)`` from plan
    sections 3.1 and 3.5, where ``k`` is the dead-reckoning drift fraction and
    ``L`` the longest path flown before re-anchoring. Both ``k`` (3% is the
    plan's provisional guess) and the placement tolerance (10 cm) are
    unmeasured; set them from hardware rung L3 before relying on the result.
    Pass the result as ``min_separation_cm`` to make a plan refuse a smaller
    gap.
    """
    values = (min_separation_cm, drift_fraction, path_length_cm, placement_tolerance_cm)
    if any(not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("min_separation_cm, drift_fraction, path_length_cm and placement_tolerance_cm must not be negative")
    return math.ceil(min_separation_cm + 2 * (drift_fraction * path_length_cm + placement_tolerance_cm))


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
    return lanes


# ----------------------------------------------------------------------
# Plan checks
# ----------------------------------------------------------------------


def check_plan(
    plan: FleetSearchPlan,
    *,
    geofence_cm: float | None = None,
    min_separation_cm: float | None = None,
) -> list[str]:
    """Every reason ``plan`` must not be flown, or an empty list.

    Checks that each lane is a move the executor accepts, that every waypoint
    lies inside the drone's region and the search area, that every waypoint
    lies inside the square geofence of half-width ``geofence_cm`` around the
    drone's takeoff point, that regions are at least ``gap_cm`` apart and
    that the guaranteed separation is at least ``min_separation_cm``.
    """
    problems: list[str] = []
    width, depth = plan.area_cm
    area = Region(0, 0, width, depth)
    for drone in plan.drones:
        lanes_ok = True
        if not drone.lanes:
            # The executor refuses start_at with no lanes (``no_lanes``).
            problems.append(f"{drone.drone_id}: has no lanes to fly; one footprint already covers its area")
        for index, lane in enumerate(drone.lanes):
            distance = lane.get("distance_cm")
            if (
                lane.get("direction") not in HORIZONTAL_DIRECTIONS
                or isinstance(distance, bool)
                or not isinstance(distance, int)
                or not MIN_MOVE_CM <= distance <= MAX_MOVE_CM
            ):
                problems.append(f"{drone.drone_id}: lane {index} {lane!r} is not a 20-500 cm horizontal move")
                lanes_ok = False
        if not _MIN_ALT_SLOT_CM <= drone.alt_cm <= _MAX_ALT_SLOT_CM:
            problems.append(f"{drone.drone_id}: altitude slot {drone.alt_cm} cm is out of range")
        if not lanes_ok:
            continue
        x0, y0 = drone.takeoff_cm
        fence_reported = False
        for point in drone.waypoints():
            if not drone.region.contains(point):
                problems.append(f"{drone.drone_id}: waypoint {point} leaves its region {drone.region.zone_dict()}")
                break
            if not area.contains(point):
                problems.append(f"{drone.drone_id}: waypoint {point} leaves the search area")
                break
            rel = (point[0] - x0, point[1] - y0)
            if (
                geofence_cm is not None
                and not fence_reported
                and (abs(rel[0]) > geofence_cm or abs(rel[1]) > geofence_cm)
            ):
                problems.append(
                    f"{drone.drone_id}: waypoint {rel} (relative to takeoff) leaves the {geofence_cm:g} cm geofence"
                )
                fence_reported = True
    for i, a in enumerate(plan.drones):
        for b in plan.drones[i + 1:]:
            distance = a.region.distance_to(b.region)
            if distance < plan.gap_cm:
                problems.append(f"{a.drone_id} and {b.drone_id}: regions are {distance:g} cm apart, below the gap")
    separation = plan.separation_cm
    if min_separation_cm is not None and len(plan.drones) > 1 and separation < min_separation_cm:
        problems.append(
            f"guaranteed separation {separation:g} cm is below the {min_separation_cm:g} cm minimum"
        )
    return problems


def _finish(plan: FleetSearchPlan, geofence_cm: float | None, min_separation_cm: float | None) -> FleetSearchPlan:
    return replace(plan, problems=tuple(check_plan(plan, geofence_cm=geofence_cm, min_separation_cm=min_separation_cm)))


def _path_bounds(extent: int, sweep: int) -> tuple[int, int]:
    """Lowest and highest path coordinate that still lets a pass reach both edges of ``extent``."""
    half = sweep // 2
    if extent >= 2 * half:
        return half, extent - half
    return extent // 2, extent // 2


# ----------------------------------------------------------------------
# Patterns
# ----------------------------------------------------------------------


def expanding_square_lanes(initial_side_cm: int = 100, growth_cm: int = 100, num_expansions: int = 4) -> list[dict]:
    """Axis-aligned rewrite of ``search.generate_expanding_square``.

    The legacy generator flies ``forward`` and turns 90 degrees clockwise
    after every leg. This version keeps the same leg lengths (two legs of
    each length, growing by ``growth_cm`` after every second leg) but changes
    direction instead of turning: ``forward``, ``right``, ``back``, ``left``.
    The heading never changes, so the lane executor accepts it.
    """
    side = _int_cm("initial_side_cm", initial_side_cm, MIN_MOVE_CM)
    growth = _int_cm("growth_cm", growth_cm)
    if isinstance(num_expansions, bool) or not isinstance(num_expansions, int) or num_expansions < 1:
        raise ValueError(f"num_expansions must be a positive integer, got {num_expansions!r}")
    order = ("forward", "right", "back", "left")
    lanes: list[dict] = []
    for leg in range(4 * num_expansions):
        lanes.extend(_split_distance(order[leg % 4], side + (leg // 2) * growth))
    return lanes


def _lane_positions(lo: float, hi: float, zone_lo: int, zone_hi: int, sweep: int) -> list[int]:
    """Lane x positions whose passes cover ``[lo, hi]``, kept inside ``[zone_lo, zone_hi]``.

    Lanes are evenly spaced, at most ``sweep`` apart, with the first pass
    reaching ``lo`` and the last reaching ``hi``. The count rounds up, so a
    remainder narrower than one sweep still gets a lane (the floor division
    in ``search.generate_parallel_track`` leaves it uncovered).
    """
    half = sweep // 2
    first = math.floor(lo) + half
    last = math.ceil(hi) - half
    if last < first:
        # One pass is wider than the target: centre it.
        first = last = (math.floor(lo) + math.ceil(hi)) // 2
    # A lane cannot leave the zone. When the gap is wider than one sweep the
    # clamped lanes leave part of the gap unsearched, which coverage_sim
    # reports as uncovered.
    first = min(max(first, zone_lo), zone_hi)
    last = min(max(last, zone_lo), zone_hi)
    span = last - first
    if span == 0:
        return [first]
    if span < MIN_MOVE_CM:
        # Two lanes closer than the minimum move: spread them to exactly the
        # minimum, which stays within one sweep, and keep them in the zone.
        left = first - (MIN_MOVE_CM - span) // 2
        left = min(max(left, zone_lo), zone_hi - MIN_MOVE_CM)
        return [left, left + MIN_MOVE_CM]
    gaps = math.ceil(span / sweep)
    return [first + (k * span) // gaps for k in range(gaps + 1)]


def plan_parallel_split(
    drone_ids: Sequence[str],
    area_cm: Any,
    sweep_cm: int,
    *,
    gap_cm: int = DEFAULT_GAP_CM,
    end_inset_cm: int = 0,
    alt_slots_cm: Sequence[int] = DEFAULT_ALT_SLOTS_CM,
    geofence_cm: float | None = None,
    min_separation_cm: float | None = DEFAULT_MIN_SEPARATION_CM,
) -> FleetSearchPlan:
    """Parallel-track (boustrophedon) split: one vertical strip per drone.

    The area is split across its width into equal strips separated by
    ``gap_cm``; drone ``i`` owns strip ``i`` from the left. Each strip is
    covered by lanes along ``y`` (``forward`` and ``back``), because a
    Tello's camera faces along its fixed heading and cannot turn. Each drone
    covers its own strip plus half of every neighbouring gap, so the gaps
    are searched without a drone ever entering one. Lanes run from
    ``end_inset_cm`` to ``depth - end_inset_cm`` (0 means the full depth, as
    in today's coordinator). Every drone flies its lanes left to right and
    takes off at the near end of its first lane, so no transit is needed.

    For two drones the equal split is already balanced in estimated time,
    because the two strips mirror each other.
    """
    ids = _ids(drone_ids)
    width, depth = _area(area_cm)
    sweep = _int_cm("sweep_cm", sweep_cm, MIN_SWEEP_CM)
    gap = _int_cm("gap_cm", gap_cm, 1 if len(ids) > 1 else 0)
    inset = _int_cm("end_inset_cm", end_inset_cm)
    slots = _slots(alt_slots_cm)
    n = len(ids)
    y_lo, y_hi = inset, depth - inset
    if y_hi - y_lo < MIN_MOVE_CM:
        raise ValueError(f"lanes of {y_hi - y_lo} cm are shorter than the {MIN_MOVE_CM} cm minimum move")
    total = width - gap * (n - 1)
    if total < n * MIN_MOVE_CM:
        raise ValueError(f"area width {width} cm is too narrow for {n} strips with {gap} cm gaps")
    base, extra = divmod(total, n)
    half_gap = (gap + 1) // 2
    drones = []
    x = 0
    for i, drone_id in enumerate(ids):
        strip = base + (1 if i < extra else 0)
        zone_lo, zone_hi = x, x + strip
        x = zone_hi + gap
        lo = zone_lo - (half_gap if i > 0 else 0)
        hi = zone_hi + (half_gap if i < n - 1 else 0)
        lanes_x = _lane_positions(lo, hi, zone_lo, zone_hi, sweep)
        points: list[Point] = []
        for k, lane_x in enumerate(lanes_x):
            ends = (y_lo, y_hi) if k % 2 == 0 else (y_hi, y_lo)
            points.extend([(lane_x, ends[0]), (lane_x, ends[1])])
        region = Region(zone_lo, 0, zone_hi, depth)
        drones.append(DronePlan(
            drone_id=drone_id,
            takeoff_cm=points[0],
            region=region,
            alt_cm=slots[i % len(slots)],
            lanes=tuple(_polyline_lanes(points, region)),
        ))
    plan = FleetSearchPlan(
        pattern=PatternKind.PARALLEL_SPLIT,
        area_cm=(width, depth),
        sweep_cm=sweep,
        gap_cm=gap,
        drones=tuple(drones),
    )
    return _finish(plan, geofence_cm, min_separation_cm)


def _half_ring_points(
    s: int, u_hi: int, ly: int, y_lo: int, y_hi: int, sweep: int
) -> list[tuple[int, int]]:
    """``(u, y)`` corners of the U-rings in one half-plane, ``u`` measured from the LKP.

    Ring ``k`` (from 1) has radius ``r_k = sweep // 2 + (k - 1) * sweep``: a
    leg along ``y = lkp_y -/+ r_k`` from the seam ``u = s`` out to
    ``u = r_k``, the outer leg along ``u = r_k``, and a leg back to the seam.
    Odd rings start at the bottom and even rings at the top, so consecutive
    rings are joined by a step along the seam. Every coordinate is clamped
    to the path bounds, which keeps each pass's footprint inside the area
    while still reaching its edge, and the rings stop once a ring reaches
    the bounds on all three open sides.
    """
    half = sweep // 2
    needed = max(u_hi, ly - y_lo, y_hi - ly)
    corners: list[tuple[int, int]] = []
    k = 1
    while True:
        r = half + (k - 1) * sweep
        outer = min(max(r, s), u_hi)
        bottom = max(ly - r, y_lo)
        top = min(ly + r, y_hi)
        if k % 2 == 1:
            corners.extend([(s, bottom), (outer, bottom), (outer, top), (s, top)])
        else:
            corners.extend([(s, top), (outer, top), (outer, bottom), (s, bottom)])
        if r >= needed:
            return corners
        k += 1


def plan_split_expanding_square(
    drone_ids: Sequence[str],
    area_cm: Any,
    lkp_cm: Sequence[int],
    sweep_cm: int,
    *,
    gap_cm: int = DEFAULT_GAP_CM,
    placement_offset_cm: int = DEFAULT_PLACEMENT_OFFSET_CM,
    stagger_s: float = DEFAULT_TAKEOFF_STAGGER_S,
    alt_slots_cm: Sequence[int] = DEFAULT_ALT_SLOTS_CM,
    geofence_cm: float | None = None,
    min_separation_cm: float | None = DEFAULT_MIN_SEPARATION_CM,
) -> FleetSearchPlan:
    """Split expanding square: two mirrored half-plane U-ring patterns around the LKP.

    The first drone searches the half-plane ``x <= lkp_x - s`` and the second
    ``x >= lkp_x + s``, with ``s = ceil(gap_cm / 2)``. Each flies expanding
    U-rings (three legs per ring plus a step along the seam) outward from
    the LKP until its half of the area is covered, so both cover the most
    probable ground first. Each drone takes off ``placement_offset_cm``
    outside its seam level with the LKP, flies back to the start of its
    first ring and then inward to the seam (``transit_lanes`` counts those
    moves). The second drone starts ``stagger_s`` later because the two take
    off only ``2 * (s + placement_offset_cm)`` apart.
    """
    ids = _ids(drone_ids)
    if len(ids) != 2:
        raise ValueError(f"the split expanding square is defined for exactly two drones, got {len(ids)}")
    width, depth = _area(area_cm)
    lx, ly = _lkp(lkp_cm, width, depth)
    sweep = _int_cm("sweep_cm", sweep_cm, MIN_SWEEP_CM)
    gap = _int_cm("gap_cm", gap_cm, 1)
    offset = _int_cm("placement_offset_cm", placement_offset_cm)
    if 0 < offset < MIN_MOVE_CM:
        raise ValueError(f"placement_offset_cm must be 0 or at least {MIN_MOVE_CM} cm, got {offset}")
    if not (math.isfinite(stagger_s) and stagger_s >= 0):
        raise ValueError(f"stagger_s must not be negative, got {stagger_s!r}")
    slots = _slots(alt_slots_cm)
    s = (gap + 1) // 2
    if lx - s - offset < 0 or lx + s + offset > width:
        raise ValueError(
            f"LKP x={lx} cm is too close to a side edge of the {width} cm area for two half-planes "
            f"{gap} cm apart with a {offset} cm placement offset"
        )
    y_lo, y_hi = _path_bounds(depth, sweep)
    half = sweep // 2
    drones = []
    for i, (drone_id, sign) in enumerate(zip(ids, (-1, 1))):
        u_edge = lx if sign < 0 else width - lx
        u_hi = max(s, u_edge - half)
        corners = _half_ring_points(s, u_hi, ly, y_lo, y_hi, sweep)
        takeoff_u = s + offset
        first_u, first_y = corners[0]
        transit = [(takeoff_u, ly), (takeoff_u, first_y), (first_u, first_y)]

        def to_xy(points: Iterable[tuple[int, int]], sign: int = sign) -> list[Point]:
            return [(lx + sign * u, y) for u, y in points]

        region = Region(0, 0, lx - s, depth) if sign < 0 else Region(lx + s, 0, width, depth)
        transit_lanes = _polyline_lanes(to_xy(transit), region)
        pattern_lanes = _polyline_lanes(to_xy(corners), region)
        drones.append(DronePlan(
            drone_id=drone_id,
            takeoff_cm=to_xy([(takeoff_u, ly)])[0],
            region=region,
            alt_cm=slots[i % len(slots)],
            lanes=tuple(transit_lanes + pattern_lanes),
            transit_lanes=len(transit_lanes),
            start_offset_s=float(stagger_s) * i,
        ))
    plan = FleetSearchPlan(
        pattern=PatternKind.SPLIT_EXPANDING_SQUARE,
        area_cm=(width, depth),
        sweep_cm=sweep,
        gap_cm=gap,
        drones=tuple(drones),
        lkp_cm=(lx, ly),
    )
    return _finish(plan, geofence_cm, min_separation_cm)


def plan_square_spiral(
    drone_id: str,
    area_cm: Any,
    lkp_cm: Sequence[int],
    sweep_cm: int,
    *,
    alt_cm: int = DEFAULT_ALT_SLOTS_CM[0],
    geofence_cm: float | None = None,
) -> FleetSearchPlan:
    """Single-drone expanding square as an axis-aligned square spiral, bounded by the area.

    The drone takes off at the LKP and flies legs of ``sweep``, ``sweep``,
    ``2 * sweep``, ``2 * sweep``, ... cycling ``forward``, ``right``,
    ``back``, ``left``, so neighbouring parallel legs are one sweep apart.
    Each corner is clamped to the path bounds, which keeps every pass's
    footprint reaching the area edge, and the spiral stops once its latest
    leg on each of the four sides has reached the bounds.
    """
    ids = _ids([drone_id])
    width, depth = _area(area_cm)
    lx, ly = _lkp(lkp_cm, width, depth)
    sweep = _int_cm("sweep_cm", sweep_cm, MIN_SWEEP_CM)
    slot = _slots([alt_cm])[0]
    x_lo, x_hi = _path_bounds(width, sweep)
    y_lo, y_hi = _path_bounds(depth, sweep)

    def clamp(point: tuple[int, int]) -> Point:
        return min(max(point[0], x_lo), x_hi), min(max(point[1], y_lo), y_hi)

    # Directions in order, the side of the ring each one runs along, and the
    # test that the ideal (unclamped) leg already lies at or beyond the bound.
    order = ((0, 1), (1, 0), (0, -1), (-1, 0))
    reached = [False, False, False, False]
    x, y = lx, ly
    ideal = [(x, y)]
    leg = 0
    while not all(reached):
        dx, dy = order[leg % 4]
        length = (leg // 2 + 1) * sweep
        if leg % 4 == 0:
            reached[0] = reached[0] or x <= x_lo  # forward leg runs along the left side
        elif leg % 4 == 1:
            reached[1] = reached[1] or y >= y_hi  # right leg runs along the top
        elif leg % 4 == 2:
            reached[2] = reached[2] or x >= x_hi  # back leg runs along the right side
        else:
            reached[3] = reached[3] or y <= y_lo  # left leg runs along the bottom
        x, y = x + dx * length, y + dy * length
        ideal.append((x, y))
        leg += 1
    region = Region(0, 0, width, depth)
    points = [clamp(p) for p in ideal]
    plan = FleetSearchPlan(
        pattern=PatternKind.SQUARE_SPIRAL,
        area_cm=(width, depth),
        sweep_cm=sweep,
        gap_cm=0,
        drones=(DronePlan(
            drone_id=ids[0],
            takeoff_cm=points[0],
            region=region,
            alt_cm=slot,
            lanes=tuple(_polyline_lanes(points, region)),
        ),),
        lkp_cm=(lx, ly),
    )
    return _finish(plan, geofence_cm, None)


# ----------------------------------------------------------------------
# Decision
# ----------------------------------------------------------------------


def choose_pattern(
    n_drones: int,
    area_cm: Any,
    lkp_cm: Sequence[int] | None = None,
    *,
    gap_cm: int = DEFAULT_GAP_CM,
    placement_offset_cm: int = DEFAULT_PLACEMENT_OFFSET_CM,
) -> PatternChoice:
    """Pick a pattern by the decision table in plan section 3.4.

    * A last-known point (LKP) and two drones: the split expanding square,
      which minimises the expected time to first detection under a prior
      centred on the LKP. If the LKP is too close to a side edge for the two
      half-planes and their takeoff placements, the parallel split instead.
    * No LKP: the parallel-track split, which minimises makespan.
    * One drone: the square spiral with an LKP, else a single parallel track.
    * More than two drones: the parallel split (the split square is defined
      for two).

    Row two of the table (an LKP with an area larger than the geofence
    allows, flown as a split square out to a radius and then parallel
    tracks) is not implemented: the split square is still chosen and the
    built plan reports the geofence problem, so it is refused.
    """
    if isinstance(n_drones, bool) or not isinstance(n_drones, int) or n_drones < 1:
        raise ValueError(f"n_drones must be a positive integer, got {n_drones!r}")
    width, depth = _area(area_cm)
    if lkp_cm is None:
        return PatternChoice(PatternKind.PARALLEL_SPLIT, "no last-known point: the parallel split minimises makespan")
    lx, _ = _lkp(lkp_cm, width, depth)
    if n_drones == 1:
        return PatternChoice(PatternKind.SQUARE_SPIRAL, "one drone with a last-known point: square spiral from the LKP")
    if n_drones > 2:
        return PatternChoice(
            PatternKind.PARALLEL_SPLIT, "the split expanding square is defined for two drones; parallel split"
        )
    s = (_int_cm("gap_cm", gap_cm, 1) + 1) // 2
    offset = _int_cm("placement_offset_cm", placement_offset_cm)
    if lx - s - offset < 0 or lx + s + offset > width:
        return PatternChoice(
            PatternKind.PARALLEL_SPLIT,
            "the last-known point is too close to a side edge for two half-planes; parallel split",
        )
    return PatternChoice(
        PatternKind.SPLIT_EXPANDING_SQUARE,
        "last-known point: the split expanding square minimises expected time to first detection",
    )


def plan_search(
    drone_ids: Sequence[str],
    area_cm: Any,
    sweep_cm: int,
    lkp_cm: Sequence[int] | None = None,
    *,
    gap_cm: int = DEFAULT_GAP_CM,
    end_inset_cm: int = 0,
    placement_offset_cm: int = DEFAULT_PLACEMENT_OFFSET_CM,
    stagger_s: float = DEFAULT_TAKEOFF_STAGGER_S,
    alt_slots_cm: Sequence[int] = DEFAULT_ALT_SLOTS_CM,
    geofence_cm: float | None = None,
    min_separation_cm: float | None = DEFAULT_MIN_SEPARATION_CM,
) -> FleetSearchPlan:
    """Choose a pattern with ``choose_pattern`` and build it; ``reason`` records the choice."""
    ids = _ids(drone_ids)
    choice = choose_pattern(len(ids), area_cm, lkp_cm, gap_cm=gap_cm, placement_offset_cm=placement_offset_cm)
    if choice.kind is PatternKind.SPLIT_EXPANDING_SQUARE:
        plan = plan_split_expanding_square(
            ids, area_cm, lkp_cm, sweep_cm,
            gap_cm=gap_cm, placement_offset_cm=placement_offset_cm, stagger_s=stagger_s,
            alt_slots_cm=alt_slots_cm, geofence_cm=geofence_cm, min_separation_cm=min_separation_cm,
        )
    elif choice.kind is PatternKind.SQUARE_SPIRAL:
        plan = plan_square_spiral(
            ids[0], area_cm, lkp_cm, sweep_cm, alt_cm=_slots(alt_slots_cm)[0], geofence_cm=geofence_cm
        )
    else:
        plan = plan_parallel_split(
            ids, area_cm, sweep_cm,
            gap_cm=gap_cm, end_inset_cm=end_inset_cm, alt_slots_cm=alt_slots_cm,
            geofence_cm=geofence_cm, min_separation_cm=min_separation_cm,
        )
        if lkp_cm is not None:
            plan = replace(plan, lkp_cm=_lkp(lkp_cm, *plan.area_cm))
    return replace(plan, reason=choice.reason)
