"""Search plans built and edited on the dashboard map, and every check they must pass.

A *plan spec* is what the browser sends: a pattern, the search area, the
lane spacing and, for each drone the operator edited, the drawn vertices of
its path (``PlanSpec``). ``build_plan`` turns a spec into one path per drone
(the generated pattern, or the edited path), converts each into executor
moves with ``plan_paths.path_to_lanes``, and runs the checks of
``docs/map-and-plan-editing.md`` section 5:

1. the geofence around each drone's takeoff point, on every traced point;
2. ``fleet_plan_check.check_plan`` (valid 20-500 cm horizontal moves, the
   lane cap, a valid ``assign_lanes`` within the size cap, the altitude slot
   range), with its region gap rule replaced by
3. the path separation check: any two drones' paths must stay ``gap_cm``
   apart everywhere;
4. coverage of the search area with a square footprint of ``strip_cm``,
   reported (never a refusal) as ``kind: "area"`` entries of ``uncovered``.

The ceiling and altitude-slot rules stay in the dashboard's ceiling board,
which supplies each drone's slot (``alt_slot_of``) and refuses Plan and Start
as before.

``BuiltPlan.ok`` is true only when nothing was found. ``BuiltPlan.payload()``
is what the browser shows; ``BuiltPlan.zone_assignments()`` is what the
coordinator sends to the edges, through the existing ``assign_lanes``.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from flightrisk.drone import plan_paths as pp
from flightrisk.drone.coordinator import DEFAULT_ZONE_GAP_CM, ZoneAssignment
from flightrisk.drone.fleet_plan_check import MAX_AREA_SIDE_CM, check_plan
from flightrisk.drone.fleet_plan_model import (
    DronePlan,
    FleetSearchPlan,
    PatternKind,
    Region,
)
from flightrisk.drone.safety import MIN_MOVE_CM
from flightrisk.fleet_protocol import MAX_ROSTER, is_identifier

__all__ = [
    "COVERAGE_MODEL",
    "DEFAULT_STRIP_CM",
    "MAX_STRIP_CM",
    "SPEC_VERSION",
    "BuiltPlan",
    "DronePath",
    "PlanSpec",
    "PlanSpecError",
    "Problem",
    "build_plan",
]

SPEC_VERSION = 1
DEFAULT_STRIP_CM = 75
MAX_STRIP_CM = 1_000
MAX_COORD_CM = 2 * MAX_AREA_SIDE_CM
"""Largest absolute plan-frame coordinate accepted for a vertex or an area corner."""

COVERAGE_MODEL = "square footprint of strip_cm centred under the drone"


class PlanSpecError(ValueError):
    """A plan spec from the browser is malformed; the message says what to fix."""


def _int_field(name: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PlanSpecError(f"{name} must be a number of centimetres, got {value!r}")
    value = round(value)
    if not low <= value <= high:
        raise PlanSpecError(f"{name} must be within {low} to {high} cm, got {value}")
    return value


@dataclass(frozen=True)
class PlanSpec:
    """A plan as the browser describes it (see the module docstring)."""

    pattern: str
    area: pp.Rect
    strip_cm: int | None = None
    paths: Mapping[str, tuple[pp.Point, ...]] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, data: Any) -> PlanSpec:
        """Validate a spec received from the browser. Raises ``PlanSpecError``."""
        if not isinstance(data, Mapping):
            raise PlanSpecError("the plan must be an object")
        unknown = set(data) - {"version", "pattern", "area", "strip_cm", "paths"}
        if unknown:
            raise PlanSpecError(f"unknown plan fields: {', '.join(sorted(map(str, unknown)))}")
        if data.get("version", SPEC_VERSION) != SPEC_VERSION:
            raise PlanSpecError(f"unsupported plan version {data.get('version')!r}")
        pattern = data.get("pattern", "parallel_track")
        if pattern not in pp.PATTERNS:
            raise PlanSpecError(f"pattern must be one of {', '.join(pp.PATTERNS)}, got {pattern!r}")
        area = data.get("area")
        if not isinstance(area, Mapping):
            raise PlanSpecError("area must be an object with x_cm, y_cm, w_cm and h_cm")
        rect = pp.Rect(
            _int_field("area x_cm", area.get("x_cm", 0), -MAX_AREA_SIDE_CM, MAX_AREA_SIDE_CM),
            _int_field("area y_cm", area.get("y_cm", 0), -MAX_AREA_SIDE_CM, MAX_AREA_SIDE_CM),
            _int_field("area w_cm", area.get("w_cm"), MIN_MOVE_CM, MAX_AREA_SIDE_CM),
            _int_field("area h_cm", area.get("h_cm"), MIN_MOVE_CM, MAX_AREA_SIDE_CM),
        )
        strip = data.get("strip_cm")
        strip = None if strip is None else _int_field("strip_cm", strip, MIN_MOVE_CM, MAX_STRIP_CM)
        raw_paths = data.get("paths")
        raw_paths = {} if raw_paths is None else raw_paths
        if not isinstance(raw_paths, Mapping):
            raise PlanSpecError("paths must map drone ids to lists of [x_cm, y_cm] vertices")
        if len(raw_paths) > MAX_ROSTER:
            raise PlanSpecError(f"a plan can have at most {MAX_ROSTER} paths")
        paths: dict[str, tuple[pp.Point, ...]] = {}
        for drone_id, raw in raw_paths.items():
            if not is_identifier(drone_id):
                raise PlanSpecError(f"{drone_id!r} is not a drone id")
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                raise PlanSpecError(f"the path of {drone_id} must be a list of [x_cm, y_cm] vertices")
            if not 2 <= len(raw) <= pp.MAX_VERTICES:
                raise PlanSpecError(f"the path of {drone_id} must have 2 to {pp.MAX_VERTICES} vertices, got {len(raw)}")
            vertices = []
            for index, vertex in enumerate(raw):
                if not isinstance(vertex, Sequence) or isinstance(vertex, (str, bytes)) or len(vertex) != 2:
                    raise PlanSpecError(f"vertex {index} of {drone_id} must be [x_cm, y_cm]")
                vertices.append((
                    _int_field(f"vertex {index} x of {drone_id}", vertex[0], -MAX_COORD_CM, MAX_COORD_CM),
                    _int_field(f"vertex {index} y of {drone_id}", vertex[1], -MAX_COORD_CM, MAX_COORD_CM),
                ))
            paths[drone_id] = tuple(vertices)
        return cls(pattern, rect, strip, paths)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"version": SPEC_VERSION, "pattern": self.pattern, "area": self.area.to_dict()}
        if self.strip_cm is not None:
            out["strip_cm"] = self.strip_cm
        out["paths"] = {drone_id: [list(v) for v in vertices] for drone_id, vertices in self.paths.items()}
        return out


@dataclass(frozen=True)
class Problem:
    """One reason a plan must not be flown, with where to show it on the map."""

    kind: str
    text: str
    drone_id: str | None = None
    other_drone_id: str | None = None
    vertex: int | None = None
    point: tuple[float, float] | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"kind": self.kind, "text": self.text}
        if self.drone_id is not None:
            out["drone_id"] = self.drone_id
        if self.other_drone_id is not None:
            out["other_drone_id"] = self.other_drone_id
        if self.vertex is not None:
            out["vertex"] = self.vertex
        if self.point is not None:
            out["x_cm"], out["y_cm"] = round(self.point[0], 1), round(self.point[1], 1)
        return out


@dataclass
class DronePath:
    """One drone's part of a built plan."""

    drone_id: str
    zone: pp.Rect
    alt_cm: int
    generated: tuple[pp.Point, ...]
    drawn: tuple[pp.Point, ...]
    edited: bool
    converted: pp.ConvertedPath | None = None

    def zone_rect(self) -> pp.Rect:
        """The zone sent to the edge: the split zone grown to contain the whole path."""
        points = [(self.zone.x, self.zone.y), (self.zone.x1, self.zone.y1)]
        if self.converted is not None:
            points.extend(self.converted.trace)
        return pp.Rect.bounding(points)

    def payload(self) -> dict:
        out: dict[str, Any] = {
            "drone_id": self.drone_id,
            "zone": self.zone.to_dict(),
            "alt_cm": self.alt_cm,
            "takeoff_cm": list(self.drawn[0]),
            "edited": self.edited,
            "generated": [list(v) for v in self.generated],
            "drawn": [list(v) for v in self.drawn],
        }
        if self.converted is not None:
            out["path"] = [list(p) for p in self.converted.trace]
            out["lanes"] = [dict(lane) for lane in self.converted.lanes]
            out["moves"] = len(self.converted.lanes)
            out["max_deviation_cm"] = round(self.converted.max_deviation_cm, 1)
        return out


@dataclass
class BuiltPlan:
    """A spec turned into per-drone paths and lanes, with every problem found (see ``build_plan``)."""

    spec: PlanSpec
    mode: str
    drones: list[DronePath]
    problems: list[Problem]
    strip_cm: int
    gap_cm: int
    geofence_cm: float | None
    coverage: float | None = None
    coverage_cell_cm: int | None = None
    uncovered: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and all(d.converted is not None for d in self.drones)

    def problem_text(self) -> str:
        return "; ".join(p.text for p in self.problems)

    def payload(self) -> dict:
        """The ``plan_preview`` the browser draws, also attached to ``fleet_plan`` as ``details``."""
        return {
            "mode": self.mode,
            "ok": self.ok,
            "spec": self.spec.to_dict(),
            "pattern": self.spec.pattern,
            "area": self.spec.area.to_dict(),
            "strip_cm": self.strip_cm,
            "gap_cm": self.gap_cm,
            "geofence_cm": self.geofence_cm,
            "snap_tolerance_cm": pp.SNAP_TOLERANCE_CM,
            "drones": [d.payload() for d in self.drones],
            "problems": [p.to_dict() for p in self.problems],
            "coverage": None if self.coverage is None else {
                "fraction": round(self.coverage, 4),
                "model": COVERAGE_MODEL,
                "cell_cm": self.coverage_cell_cm,
            },
            "uncovered": list(self.uncovered),
        }

    def zone_assignments(self) -> list[ZoneAssignment]:
        """One ``ZoneAssignment`` per drone for ``FleetCoordinator.plan_zones``. Only for an ``ok`` plan."""
        if not self.ok:
            raise ValueError(f"refusing to assign an unchecked plan: {self.problem_text()}")
        out = []
        for drone in self.drones:
            rect = drone.zone_rect()
            assert drone.converted is not None
            out.append(ZoneAssignment(
                drone_id=drone.drone_id,
                x_cm=rect.x,
                y_cm=rect.y,
                w_cm=rect.w,
                h_cm=rect.h,
                alt_cm=drone.alt_cm,
                lanes=tuple(dict(lane) for lane in drone.converted.lanes),
                takeoff_x_cm=drone.converted.takeoff[0],
                takeoff_y_cm=drone.converted.takeoff[1],
            ))
        return out


def _check_plan_problems(drones: list[DronePath], area: pp.Rect, strip_cm: int, geofence_cm: float | None) -> list[str]:
    """``fleet_plan_check.check_plan`` on the converted drones, with the path rules of this module.

    ``check_plan`` works in a frame whose search area starts at the origin
    and whose drones fly inside their regions. Here each drone's region is
    the bounding box of its path and the area is the box around the search
    area and every path, shifted to the origin; its region gap rule is
    switched off (``gap_cm=0``) because the path separation check replaces
    it, and its coverage gate is off (coverage is reported, not gated).
    """
    converted = [d for d in drones if d.converted is not None]
    if not converted:
        return []
    every = [(area.x, area.y), (area.x1, area.y1)] + [p for d in converted for p in d.converted.trace]
    bounds = pp.Rect.bounding(every)
    ox, oy = bounds.x, bounds.y
    plans = []
    for d in converted:
        trace = [(x - ox, y - oy) for x, y in d.converted.trace]
        box = pp.Rect.bounding(trace)
        plans.append(DronePlan(
            drone_id=d.drone_id,
            takeoff_cm=trace[0],
            region=Region(box.x, box.y, box.x1, box.y1),
            alt_cm=d.alt_cm,
            lanes=tuple(dict(lane) for lane in d.converted.lanes),
        ))
    plan = FleetSearchPlan(
        pattern=PatternKind.PARALLEL_SPLIT,
        area_cm=(max(bounds.w, MIN_MOVE_CM), max(bounds.h, MIN_MOVE_CM)),
        sweep_cm=strip_cm,
        gap_cm=0,
        drones=tuple(plans),
        geofence_cm=geofence_cm,
        min_separation_cm=None,
        max_standoff_cm=None,
    )
    return check_plan(plan, min_coverage=None)


def build_plan(
    spec: PlanSpec,
    drone_ids: Sequence[str],
    *,
    strip_cm: int = DEFAULT_STRIP_CM,
    gap_cm: int = DEFAULT_ZONE_GAP_CM,
    geofence_cm: float | None,
    alt_slot_of: Callable[[str, int], int] | None = None,
    mode: str = "fleet",
) -> BuiltPlan:
    """Build and check a plan for ``drone_ids`` (see the module docstring).

    ``strip_cm`` is used when the spec names none. ``alt_slot_of(drone_id,
    index)`` gives each drone's altitude slot (100 cm when omitted).
    ``geofence_cm`` None skips the fence check; the dashboard always passes
    the edges' value. Never raises for a bad plan: every problem is in
    ``BuiltPlan.problems``.
    """
    strip = spec.strip_cm if spec.strip_cm is not None else int(strip_cm)
    ids = [str(d) for d in drone_ids]
    problems: list[Problem] = []
    built = BuiltPlan(spec, mode, [], problems, strip, int(gap_cm), geofence_cm)
    if not ids:
        problems.append(Problem("spec", "no drone to plan for"))
        return built
    for extra in sorted(set(spec.paths) - set(ids)):
        problems.append(Problem("spec", f"the plan has a path for {extra}, which is not in the fleet", drone_id=extra))
    try:
        zones = pp.split_area(spec.area, len(ids), int(gap_cm))
    except ValueError as exc:
        problems.append(Problem("spec", str(exc)))
        return built

    for index, (drone_id, zone) in enumerate(zip(ids, zones)):
        try:
            alt = int(alt_slot_of(drone_id, index)) if alt_slot_of is not None else 100
        except Exception as exc:  # noqa: BLE001 - the ceiling board's refusal is shown as a problem
            problems.append(Problem("altitude", f"{drone_id}: {exc}", drone_id=drone_id))
            alt = 100
        generated = tuple(pp.pattern_vertices(spec.pattern, zone, strip))
        drawn = spec.paths.get(drone_id, generated)
        drone = DronePath(drone_id, zone, alt, generated, tuple(drawn), drawn != generated)
        built.drones.append(drone)
        try:
            drone.converted = pp.path_to_lanes(drone.drawn, fence_cm=geofence_cm)
        except ValueError as exc:
            problems.append(Problem("path", f"{drone_id}: {exc}", drone_id=drone_id))
            continue
        if geofence_cm is not None:
            outside = pp.geofence_violation(drone.converted, geofence_cm)
            if outside is not None:
                vertex, point = outside
                tx, ty = drone.converted.takeoff
                problems.append(Problem(
                    "geofence",
                    f"{drone_id}: the path to vertex {vertex} leaves the {geofence_cm:g} cm geofence around its "
                    f"takeoff point at ({point[0] - tx}, {point[1] - ty}) cm from takeoff",
                    drone_id=drone_id, vertex=vertex, point=point,
                ))

    converted = [d for d in built.drones if d.converted is not None]
    for a, b in itertools.combinations(converted, 2):
        distance, pa, pb = pp.closest_approach(a.converted.trace, b.converted.trace)
        if distance < gap_cm:
            where = ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)
            problems.append(Problem(
                "separation",
                f"{a.drone_id} and {b.drone_id}: the paths come within {distance:.0f} cm of each other near "
                f"({where[0]:.0f}, {where[1]:.0f}) cm, below the {gap_cm} cm gap",
                drone_id=a.drone_id, other_drone_id=b.drone_id, point=where,
            ))
    for text in _check_plan_problems(built.drones, spec.area, strip, geofence_cm):
        if "geofence" in text and any(p.kind == "geofence" for p in problems):
            continue  # already reported with its vertex
        problems.append(Problem("check", text))

    if converted:
        fraction, rects, cell = pp.coverage(spec.area, [d.converted.trace for d in converted], strip)
        built.coverage = fraction
        built.coverage_cell_cm = cell
        built.uncovered = [
            dict(rect, kind="area", reason=f"not seen by any drone camera ({COVERAGE_MODEL})") for rect in rects
        ]
    return built
