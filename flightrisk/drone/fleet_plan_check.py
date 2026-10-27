"""Inputs, planning footprints and safety checks for fleet search plans.

``flightrisk.drone.fleet_patterns`` builds the plans and re-exports every
public name of this module, so callers import from ``fleet_patterns`` only.
Nothing here imports ``fleet_patterns``: the plan objects are used through
their attributes.

Separation margin
-----------------
``check_plan`` requires the guaranteed (commanded) separation between any two
drones to be at least ``min_separation_cm + 2 * placement_tolerance_cm``. The
operator places each drone by tape measure with an assumed error of up to
``placement_tolerance_cm`` (plan section 3.1, 10 cm until rung L3 measures
it). If both drones are misplaced toward each other, the true separation is
the commanded one minus twice the tolerance, which is then still at least
``min_separation_cm``. That is why ``DEFAULT_GAP_CM`` is 170 cm: 150 cm of
D_min plus 2 x 10 cm. Dead-reckoning drift is extra; ``required_gap_cm``
sizes the gap for it once the drift fraction has been measured.

Planning footprint
------------------
The patterns rely on a rectangle that the camera is guaranteed to see,
relative to the drone: every ground point at a lateral offset of at most
``lateral_cm / 2`` and an along-track distance (ahead, along ``+y``) between
``near_cm`` and ``far_cm``. ``planning_footprint`` derives it:

* ``SQUARE_FOOTPRINT`` (the string ``"square"``) is the plan's idealised model:
  a square of side ``sweep_cm`` centred on the drone. Only use it for model
  studies, such as reproducing the plan's worked example.
* a ``coverage.SweepModel`` or a ``coverage_sim.ForwardFootprint`` is the real
  forward camera: a trapezoid from the near edge (where the bottom of the
  image meets the ground) to the far edge (the detection range), widening
  with distance. The lateral width is chosen first (``sweep_cm``, or by
  default the width of the footprint at its near edge, which keeps the
  standoff at its minimum); the along-track window then starts where that
  width is fully visible. With a ``SweepModel`` each drone is modelled at its
  own altitude slot (the model's ``height_m`` is replaced by the slot), and
  the window is the one every drone sees.

Lanes along ``y`` use only the lateral width and the window's two ends.
Legs across the heading (the rings of the split expanding square and the
square spiral) are spaced by ``along_cm`` and placed as if the camera were
centred ``ahead_cm`` in front of the drone. Because the rectangle is inside
the true footprint, everything the square-model pattern would cover is also
covered by the real camera.

Coverage gate
-------------
Whatever the footprint, ``check_plan`` simulates which cells of the search
area the real camera ever sees (``coverage_sim.covered_cells`` on 10 cm
cells, with the raster coarsened only beyond the area cap) and refuses a plan
below ``MIN_COVERAGE`` (99.5%, plan section 5.1).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from flightrisk.drone import coverage_sim as cs
from flightrisk.drone.coverage import SweepModel
from flightrisk.drone.safety import (
    DEFAULT_GEOFENCE_CM,
    HORIZONTAL_DIRECTIONS,
    MAX_MOVE_CM,
    MIN_MOVE_CM,
)

__all__ = [
    "DEFAULT_GAP_CM",
    "DEFAULT_MIN_SEPARATION_CM",
    "DEFAULT_PLACEMENT_TOLERANCE_CM",
    "DEFAULT_PLAN_GEOFENCE_CM",
    "MAX_AREA_SIDE_CM",
    "MAX_MOVES_PER_DRONE",
    "MIN_COVERAGE",
    "MIN_SWEEP_CM",
    "SQUARE_FOOTPRINT",
    "PlanningFootprint",
    "check_plan",
    "planning_footprint",
    "required_gap_cm",
    "simulated_coverage",
]

DEFAULT_MIN_SEPARATION_CM = 150
"""Minimum horizontal separation D_min a plan must guarantee (plan section 3.5)."""

DEFAULT_PLACEMENT_TOLERANCE_CM = 10
"""Operator placement error assumed per drone (plan section 3.1, a guess until L3)."""

DEFAULT_GAP_CM = DEFAULT_MIN_SEPARATION_CM + 2 * DEFAULT_PLACEMENT_TOLERANCE_CM
"""Default corridor between drone regions: D_min plus the placement tolerance of both drones (170 cm)."""

DEFAULT_PLAN_GEOFENCE_CM = DEFAULT_GEOFENCE_CM
"""Geofence half-width plans are checked against by default: the edge's own default (``safety``)."""

MIN_COVERAGE = 0.995
"""Smallest simulated fraction of the area a feasible plan must see (plan section 5.1)."""

MAX_AREA_SIDE_CM = 20_000
"""Largest area side accepted: twice the Tello's published 100 m range."""

MAX_MOVES_PER_DRONE = 2_000
"""Most executor moves one drone may be given. At 1.5 s of overhead per move this is
already far beyond one battery; the cap keeps the planner and simulator bounded."""

MIN_SWEEP_CM = 2 * MIN_MOVE_CM
"""Smallest sweep width accepted. With at least two minimum moves per sweep, a
pass can always stop half a sweep (20 cm or more) inside an edge, which leaves
room for the overshoot pair a leg shorter than 20 cm needs."""

SQUARE_FOOTPRINT = "square"
"""Pass as ``footprint`` to plan with the plan's idealised square footprint."""

_MAX_SWEEP_CM = 2 * MAX_AREA_SIDE_CM
_MAX_PLACEMENT_TOLERANCE_CM = 500
_MAX_STAGGER_S = 60.0
_MIN_ALT_SLOT_CM = MIN_MOVE_CM
_MAX_ALT_SLOT_CM = MAX_MOVE_CM
_COVERAGE_CELL_CM = 10.0
_COVERAGE_MAX_CELLS = cs.MAX_RASTER_CELLS
_FROM_PLAN: Any = object()


# ----------------------------------------------------------------------
# Input validation
# ----------------------------------------------------------------------


def _number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return float(value)


def _int_cm(name: str, value: Any, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number of centimetres, got {value!r}")
    if value != int(value):
        raise ValueError(f"{name} must be a whole number of centimetres, got {value!r}")
    value = int(value)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum} cm, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum} cm, got {value}")
    return value


def _pair(name: str, value: Any) -> tuple[Any, Any]:
    try:
        a, b = value
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a pair (x, y), got {value!r}") from None
    return a, b


def _area(area_cm: Any) -> tuple[int, int]:
    if isinstance(area_cm, (int, float)) and not isinstance(area_cm, bool):
        width = depth = area_cm
    else:
        width, depth = _pair("area_cm", area_cm)
    return (
        _int_cm("area width", width, MIN_MOVE_CM, MAX_AREA_SIDE_CM),
        _int_cm("area depth", depth, MIN_MOVE_CM, MAX_AREA_SIDE_CM),
    )


def _ids(drone_ids: Sequence[str]) -> list[str]:
    if isinstance(drone_ids, str):
        raise TypeError(f"drone_ids must be a sequence of ids, got the string {drone_ids!r}")
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


def _lkp(lkp_cm: Any, width: int, depth: int) -> tuple[int, int]:
    x, y = _pair("lkp_cm", lkp_cm)
    x, y = _int_cm("LKP x", x), _int_cm("LKP y", y)
    if not (0 <= x <= width and 0 <= y <= depth):
        raise ValueError(f"LKP ({x}, {y}) is outside the {width} x {depth} cm area")
    return x, y


def _optional_limit(name: str, value: Any) -> float | None:
    """``None`` (the check is explicitly switched off) or a finite, positive, bounded distance."""
    if value is None:
        return None
    value = _number(name, value)
    if not 0 < value <= MAX_AREA_SIDE_CM:
        raise ValueError(f"{name} must be within (0, {MAX_AREA_SIDE_CM}] cm or None, got {value!r}")
    return value


def _tolerance(value: Any) -> float:
    value = _number("placement_tolerance_cm", value)
    if not 0 <= value <= _MAX_PLACEMENT_TOLERANCE_CM:
        raise ValueError(f"placement_tolerance_cm must be within 0-{_MAX_PLACEMENT_TOLERANCE_CM} cm, got {value!r}")
    return value


def _stagger(value: Any) -> float:
    value = _number("stagger_s", value)
    if not 0 <= value <= _MAX_STAGGER_S:
        raise ValueError(f"stagger_s must be within 0-{_MAX_STAGGER_S:g} s, got {value!r}")
    return value


def _safety_inputs(geofence_cm: Any, min_separation_cm: Any, placement_tolerance_cm: Any) -> dict:
    """Validated ``geofence_cm``, ``min_separation_cm`` and ``placement_tolerance_cm`` plan fields."""
    return {
        "geofence_cm": _optional_limit("geofence_cm", geofence_cm),
        "min_separation_cm": _optional_limit("min_separation_cm", min_separation_cm),
        "placement_tolerance_cm": _tolerance(placement_tolerance_cm),
    }


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
    Use the result as ``gap_cm``. To make a plan refuse a smaller gap, pass
    ``min_separation_cm=required_gap_cm(D_min, k, L, placement_tolerance_cm=0)``:
    ``check_plan`` adds the placement tolerance of both drones itself.
    """
    values = (min_separation_cm, drift_fraction, path_length_cm, placement_tolerance_cm)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError(
            "min_separation_cm, drift_fraction, path_length_cm and placement_tolerance_cm must be finite and not negative"
        )
    return math.ceil(min_separation_cm + 2 * (drift_fraction * path_length_cm + placement_tolerance_cm))


# ----------------------------------------------------------------------
# Planning footprint
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PlanningFootprint:
    """The rectangle of ground the planner relies on, relative to the drone (see the module docstring).

    ``kind`` is ``"square"`` or ``"forward"``. Every point with
    ``|dx| <= lateral_cm / 2`` and ``near_cm <= dy <= far_cm`` is visible from
    every drone of the plan. ``along_cm`` is the spacing of legs across the
    heading and ``ahead_cm`` the centre of their band ahead of the drone
    (``ahead_cm - along_cm / 2 >= near_cm`` and ``ahead_cm + along_cm / 2 <=
    far_cm``). ``source`` is the footprint the caller passed.
    """

    kind: str
    lateral_cm: int
    near_cm: float
    far_cm: float
    along_cm: int
    ahead_cm: int
    source: Any = None

    def camera(self, alt_cm: float) -> cs.Footprint:
        """The real footprint of a drone flying at ``alt_cm``, as the coverage check simulates it."""
        if self.kind == SQUARE_FOOTPRINT:
            return cs.RectFootprint.square(self.lateral_cm)
        if isinstance(self.source, SweepModel):
            return cs.ForwardFootprint.from_sweep_model(replace(self.source, height_m=alt_cm / 100))
        return self.source

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "lateral_cm": self.lateral_cm,
            "near_cm": self.near_cm,
            "far_cm": self.far_cm,
            "along_cm": self.along_cm,
            "ahead_cm": self.ahead_cm,
        }


def planning_footprint(footprint: Any, sweep_cm: Any, alts_cm: Sequence[int], depth_cm: int) -> PlanningFootprint:
    """Resolve the ``footprint`` and ``sweep_cm`` arguments of the plan builders.

    ``footprint`` is ``SQUARE_FOOTPRINT``, a ``coverage.SweepModel`` or a
    ``coverage_sim.ForwardFootprint``; ``sweep_cm`` is the lane spacing (an
    upper bound for a camera footprint, and optional there).
    """
    if isinstance(footprint, str):
        if footprint != SQUARE_FOOTPRINT:
            raise ValueError(f"footprint must be {SQUARE_FOOTPRINT!r}, a SweepModel or a ForwardFootprint, got {footprint!r}")
        if sweep_cm is None:
            raise ValueError("the square footprint needs sweep_cm")
        side = _int_cm("sweep_cm", sweep_cm, MIN_SWEEP_CM, _MAX_SWEEP_CM)
        return PlanningFootprint(SQUARE_FOOTPRINT, side, -side / 2, side / 2, side, 0, SQUARE_FOOTPRINT)
    if isinstance(footprint, SweepModel):
        cameras = [cs.ForwardFootprint.from_sweep_model(replace(footprint, height_m=alt / 100)) for alt in alts_cm]
    elif isinstance(footprint, cs.ForwardFootprint):
        cameras = [footprint]
    else:
        raise TypeError(
            f"footprint must be {SQUARE_FOOTPRINT!r}, a coverage.SweepModel or a coverage_sim.ForwardFootprint, "
            f"got {type(footprint).__name__}"
        )
    for camera in cameras:
        for name in ("near_cm", "far_cm", "half_tan", "max_half_width_cm"):
            value = getattr(camera, name)
            if not (isinstance(value, (int, float)) and not math.isnan(value) and value >= 0):
                raise ValueError(f"forward footprint {name} must be a non-negative number, got {value!r}")
        if not math.isfinite(camera.far_cm):
            raise ValueError(f"forward footprint {camera!r} must have a finite far edge")
    half_tan = min(c.half_tan for c in cameras)
    near = max(c.near_cm for c in cameras)
    far = min(c.far_cm for c in cameras)
    reach = min(min(c.max_half_width_cm for c in cameras), far * half_tan)
    if far <= near or reach <= 0:
        raise ValueError(
            f"the camera sees nothing on the ground at altitude slots {list(alts_cm)} cm: "
            f"near edge {near:.0f} cm, far edge {far:.0f} cm"
        )
    widest = math.floor(2 * reach - 1e-6)
    if sweep_cm is None:
        lateral = min(math.floor(2 * near * half_tan - 1e-6), widest)
    else:
        lateral = min(_int_cm("sweep_cm", sweep_cm, MIN_SWEEP_CM, _MAX_SWEEP_CM), widest)
    if lateral < MIN_SWEEP_CM:
        raise ValueError(
            f"the camera footprint is at most {max(lateral, 0)} cm wide where it is fully visible, "
            f"below the {MIN_SWEEP_CM} cm minimum sweep"
        )
    lo = math.ceil(max(near, lateral / (2 * half_tan)) + 1e-6)
    hi = math.floor(far - 1e-6)
    if hi < lo:
        raise ValueError(
            f"a {lateral} cm wide pass is only fully visible beyond the camera's {far:.0f} cm far edge; "
            "use a narrower sweep_cm"
        )
    window = hi - lo
    along = min(window - window % 2, depth_cm + depth_cm % 2)
    return PlanningFootprint("forward", lateral, lo, hi, along, lo + along // 2, footprint)


# ----------------------------------------------------------------------
# Plan checks
# ----------------------------------------------------------------------


def simulated_coverage(plan: Any) -> float:
    """Fraction of the search area's cells the real camera footprints of ``plan`` ever see."""
    width, depth = plan.area_cm
    cell = max(_COVERAGE_CELL_CM, math.sqrt(width * depth / _COVERAGE_MAX_CELLS))
    raster = cs.Raster(width, depth, cell)
    paths = [drone.waypoints() for drone in plan.drones]
    return float(cs.covered_cells(raster, paths, plan.camera_footprints()).mean())


def _lane_problems(drone: Any) -> list[str]:
    problems = []
    if not drone.lanes:
        # The executor refuses start_at with no lanes (``no_lanes``).
        problems.append(f"{drone.drone_id}: has no lanes to fly; one footprint already covers its area")
    if len(drone.lanes) > MAX_MOVES_PER_DRONE:
        problems.append(f"{drone.drone_id}: {len(drone.lanes)} moves exceed the {MAX_MOVES_PER_DRONE} move cap")
    for index, lane in enumerate(drone.lanes):
        distance = lane.get("distance_cm")
        if (
            lane.get("direction") not in HORIZONTAL_DIRECTIONS
            or isinstance(distance, bool)
            or not isinstance(distance, int)
            or not MIN_MOVE_CM <= distance <= MAX_MOVE_CM
        ):
            problems.append(f"{drone.drone_id}: lane {index} {lane!r} is not a 20-500 cm horizontal move")
    return problems


def _path_problems(drone: Any, flight_area: tuple[int, int, int, int], geofence_cm: float | None) -> list[str]:
    problems = []
    fx0, fy0, fx1, fy1 = flight_area
    x0, y0 = drone.takeoff_cm
    fence_reported = False
    for point in drone.waypoints():
        if not drone.region.contains(point):
            problems.append(f"{drone.drone_id}: waypoint {point} leaves its region {drone.region.zone_dict()}")
            break
        if not (fx0 <= point[0] <= fx1 and fy0 <= point[1] <= fy1):
            problems.append(f"{drone.drone_id}: waypoint {point} leaves the flight area {flight_area}")
            break
        rel = (point[0] - x0, point[1] - y0)
        if geofence_cm is not None and not fence_reported and (abs(rel[0]) > geofence_cm or abs(rel[1]) > geofence_cm):
            problems.append(f"{drone.drone_id}: waypoint {rel} (relative to takeoff) leaves the {geofence_cm:g} cm geofence")
            fence_reported = True
    return problems


def check_plan(
    plan: Any,
    *,
    geofence_cm: Any = _FROM_PLAN,
    min_separation_cm: Any = _FROM_PLAN,
    placement_tolerance_cm: Any = _FROM_PLAN,
    min_coverage: float | None = MIN_COVERAGE,
) -> list[str]:
    """Every reason ``plan`` must not be flown, or an empty list.

    Checks that each lane is a move the executor accepts and the move count
    is within ``MAX_MOVES_PER_DRONE``; that every waypoint lies inside the
    drone's region and the plan's flight area (the search area, extended
    ``standoff_cm`` behind its near edge); that every waypoint lies inside the
    square geofence of half-width ``geofence_cm`` around the drone's takeoff
    point; that regions are at least ``gap_cm`` apart; that the guaranteed
    separation is at least ``min_separation_cm + 2 * placement_tolerance_cm``;
    and that the real camera footprint sees at least ``min_coverage`` of the
    area. ``geofence_cm``, ``min_separation_cm`` and ``placement_tolerance_cm``
    default to the values the plan was built with; ``None`` skips a check
    (for model studies only), and non-finite values are refused.
    """
    fence = plan.geofence_cm if geofence_cm is _FROM_PLAN else geofence_cm
    min_sep = plan.min_separation_cm if min_separation_cm is _FROM_PLAN else min_separation_cm
    tolerance = plan.placement_tolerance_cm if placement_tolerance_cm is _FROM_PLAN else placement_tolerance_cm
    limits = _safety_inputs(fence, min_sep, tolerance)
    if min_coverage is not None:
        min_coverage = _number("min_coverage", min_coverage)
        if not 0 <= min_coverage <= 1:
            raise ValueError(f"min_coverage must be within [0, 1], got {min_coverage!r}")
    problems: list[str] = []
    width, depth = plan.area_cm
    flight_area = (0, -plan.standoff_cm, width, depth)
    for drone in plan.drones:
        lane_problems = _lane_problems(drone)
        problems.extend(lane_problems)
        if not _MIN_ALT_SLOT_CM <= drone.alt_cm <= _MAX_ALT_SLOT_CM:
            problems.append(f"{drone.drone_id}: altitude slot {drone.alt_cm} cm is out of range")
        if any("no lanes" not in p for p in lane_problems):
            continue
        problems.extend(_path_problems(drone, flight_area, limits["geofence_cm"]))
    for i, a in enumerate(plan.drones):
        for b in plan.drones[i + 1:]:
            distance = a.region.distance_to(b.region)
            if distance < plan.gap_cm:
                problems.append(f"{a.drone_id} and {b.drone_id}: regions are {distance:g} cm apart, below the gap")
    separation = plan.separation_cm
    if limits["min_separation_cm"] is not None and len(plan.drones) > 1:
        required = limits["min_separation_cm"] + 2 * limits["placement_tolerance_cm"]
        if separation < required:
            problems.append(
                f"guaranteed separation {separation:g} cm is below the {required:g} cm minimum "
                f"({limits['min_separation_cm']:g} cm D_min plus 2 x {limits['placement_tolerance_cm']:g} cm "
                "placement tolerance)"
            )
    if min_coverage is not None:
        coverage = simulated_coverage(plan)
        if coverage < min_coverage:
            problems.append(
                f"the {plan.footprint_kind} camera footprint sees only {coverage:.1%} of the area "
                f"in simulation, below the {min_coverage:.1%} minimum"
            )
    return problems
