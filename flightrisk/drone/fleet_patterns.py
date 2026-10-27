"""Axis-aligned search patterns for a fleet of relative-move drones.

Every pattern here is built only from the four horizontal moves the edge lane
executor accepts (``forward``, ``back``, ``left`` and ``right``, 20-500 cm
each). Nothing rotates, so each drone keeps the fixed-heading dead-reckoning
frame described in ``flightrisk.drone.safety``. The legacy generators in
``flightrisk.drone.search`` turn with ``rotate_degrees`` and are left
unchanged for the single-drone path.

Frame, takeoff and zone
-----------------------
All coordinates are whole centimetres in the *fleet frame*, the shared grid of
``flightrisk.drone.coordinator``: the origin is the near-left corner of the
search area, ``x`` grows to the right and ``y`` grows ahead along the takeoff
heading every drone shares. The search area is ``[0, width] x [0, depth]``.
Each drone's lanes are relative moves from its own takeoff point
(``DronePlan.takeoff_cm``), so the ``rel_x_cm``/``rel_y_cm`` its edge reports
are relative to that point.

Contract for the coordinator integration: the takeoff point is *not* the
corner of the drone's zone. Today's coordinator assumes it is (it adds the
zone corner to telemetry and checks re-task fences around it). The zone of
every ``assign_lanes`` message built here therefore carries the takeoff
point explicitly as ``takeoff_x_cm`` and ``takeoff_y_cm`` next to the zone
rectangle, ``FleetSearchPlan.placements`` lists the same points, and
consumers must use those (never ``x_cm``/``y_cm``) as the drone's origin.

Footprint and coverage
----------------------
Every builder takes a required ``footprint`` keyword: ``SQUARE_FOOTPRINT``
for the plan's idealised square model (only for model studies such as the
plan's worked example), or the real forward camera as a
``coverage.SweepModel`` (for example ``tello_sweep_model(max_range_m=4.0)``),
modelled at each drone's altitude slot, or a ``coverage_sim.ForwardFootprint``,
which describes one altitude and is refused unless every drone flies the
same slot. ``fleet_plan_check`` describes how the planner turns the
footprint into a lateral lane spacing and an along-track window, within the
altitude and placement margins. A forward camera never sees the ground just
ahead of the drone, so to search the strip along the area's near edge the
drones fly up to ``FleetSearchPlan.standoff_cm`` behind it: the *flight
area* is ``[0, width] x [-standoff_cm, depth]`` and must be kept clear, so
the floor needed is ``depth + standoff_cm`` deep. A plan whose standoff
exceeds ``max_standoff_cm`` (by default the geofence half-width) is refused.
Every plan is then measured with its gate cameras and refused below 99.5%
coverage.

Separation rule
---------------
Every multi-drone plan gives each drone a closed, axis-aligned region
(``DronePlan.region``) that contains its whole path: the takeoff point, the
transit to the pattern start, every lane end, and the extra point of any
overshoot pair used for a leg shorter than the 20 cm minimum move. Because
lanes are straight axis-aligned moves and a rectangle is convex, a path whose
waypoints are all inside the region stays inside it between waypoints too.
Regions span the same ``y`` range and are pairwise at least ``gap_cm`` apart
in ``x``:

* parallel split: vertical strips ``[a_i, b_i]`` with ``a_{i+1} - b_i = gap_cm``;
* split expanding square: ``x <= e1`` and ``x >= e2`` with ``e2 - e1 >= gap_cm``
  (see ``plan_split_expanding_square`` for where the seam goes).

So at every instant, whatever the timing (a slow move, a late start, a drone
that hovers), the commanded positions of two drones are at least
``FleetSearchPlan.separation_cm >= gap_cm`` apart. Separation never depends on
timing, and altitude slots are an extra layer, never the only one.
``check_plan`` requires that separation to be at least ``min_separation_cm``
plus twice the placement tolerance, so it still holds if each drone is placed
up to ``placement_tolerance_cm`` toward the other (hence the 170 cm default
gap). Dead-reckoning drift is not part of the rule: size ``gap_cm`` with
``required_gap_cm`` once the drift fraction has been measured (rung L3).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any

from flightrisk.drone.fleet_plan_check import (
    ALTITUDE_DEADBAND_CM,
    ALTITUDE_MARGIN_CM,
    ALTITUDE_TOLERANCE_CM,
    COVERAGE_LINE_SPACING_CM,
    DEFAULT_GAP_CM,
    DEFAULT_MIN_SEPARATION_CM,
    DEFAULT_PLACEMENT_TOLERANCE_CM,
    DEFAULT_PLAN_GEOFENCE_CM,
    MAX_AREA_SIDE_CM,
    MAX_LANES_PER_DRONE,
    MAX_MESSAGE_BYTES,
    MAX_MOVES_PER_DRONE,
    MESSAGE_RESERVE_BYTES,
    MIN_COVERAGE,
    MIN_SWEEP_CM,
    SQUARE_FOOTPRINT,
    STANDOFF_FROM_GEOFENCE,
    PlanningFootprint,
    _area,
    _check,
    _ids,
    _int_cm,
    _lkp,
    _safety_inputs,
    _slots,
    _stagger,
    check_plan,
    gate_footprints,
    planning_footprint,
    required_gap_cm,
    simulated_coverage,
)
from flightrisk.drone.fleet_plan_model import (
    DronePlan,
    FleetSearchPlan,
    FrozenLane,
    PatternChoice,
    PatternKind,
    Point,
    Region,
    _check_move_cap,
    _leg_moves,  # noqa: F401 - re-exported: tests and callers of the single-module layout use it
    _polyline_lanes,
    _simplify,  # noqa: F401 - re-exported: tests and callers of the single-module layout use it
    _split_distance,
)
from flightrisk.drone.safety import MAX_MOVE_CM, MIN_MOVE_CM

__all__ = [
    "ALTITUDE_DEADBAND_CM",
    "ALTITUDE_MARGIN_CM",
    "ALTITUDE_TOLERANCE_CM",
    "COVERAGE_LINE_SPACING_CM",
    "DEFAULT_ALT_SLOTS_CM",
    "DEFAULT_GAP_CM",
    "DEFAULT_MIN_SEPARATION_CM",
    "DEFAULT_PLACEMENT_OFFSET_CM",
    "DEFAULT_PLACEMENT_TOLERANCE_CM",
    "DEFAULT_PLAN_GEOFENCE_CM",
    "DEFAULT_TAKEOFF_STAGGER_S",
    "MAX_AREA_SIDE_CM",
    "MAX_LANES_PER_DRONE",
    "MAX_MESSAGE_BYTES",
    "MAX_MOVES_PER_DRONE",
    "MESSAGE_RESERVE_BYTES",
    "MIN_COVERAGE",
    "MIN_SWEEP_CM",
    "SQUARE_FOOTPRINT",
    "STANDOFF_FROM_GEOFENCE",
    "DronePlan",
    "FleetSearchPlan",
    "FrozenLane",
    "PatternChoice",
    "PatternKind",
    "PlanningFootprint",
    "Region",
    "check_plan",
    "choose_pattern",
    "expanding_square_lanes",
    "gate_footprints",
    "plan_parallel_split",
    "plan_search",
    "plan_split_expanding_square",
    "plan_square_spiral",
    "planning_footprint",
    "required_gap_cm",
    "simulated_coverage",
]

DEFAULT_PLACEMENT_OFFSET_CM = 50
"""Split square: how far outside the seam each drone takes off (plan section 3.4)."""

DEFAULT_TAKEOFF_STAGGER_S = 2.0
"""Split square: the second drone starts this much later, because both take off near the LKP."""

DEFAULT_ALT_SLOTS_CM = (100, 160)
"""Altitude slots assigned to drones in turn, as in the coordinator."""

# ----------------------------------------------------------------------
# Shared builder steps
# ----------------------------------------------------------------------


def _alts(slots: Sequence[int], n: int) -> list[int]:
    return [slots[i % len(slots)] for i in range(n)]


def _finish(plan: FleetSearchPlan) -> FleetSearchPlan:
    problems, coverage = _check(plan)
    return replace(plan, problems=tuple(problems), coverage=coverage, checked_digest=plan.content_digest())


def _path_bounds(extent: int, sweep: int) -> tuple[int, int]:
    """Lowest and highest path coordinate that still lets a pass reach both edges of ``extent``."""
    half = sweep // 2
    if extent >= 2 * half:
        return half, extent - half
    return extent // 2, extent // 2


def _ring_footprint(pf: PlanningFootprint, width: int, depth: int, pattern: str) -> None:
    """Refuse areas and footprints the ring patterns cannot fly (they fall back to the parallel split)."""
    if min(width, depth) < MIN_SWEEP_CM:
        raise ValueError(f"the {pattern} needs an area at least {MIN_SWEEP_CM} cm on each side, got {width} x {depth}")
    if pf.along_cm < MIN_SWEEP_CM:
        raise ValueError(
            f"the camera's along-track window is {pf.along_cm} cm at a {pf.lateral_cm} cm lane spacing, "
            f"below the {MIN_SWEEP_CM} cm the {pattern} needs"
        )


def _floor_y(points: Iterable[Point]) -> int:
    """Lowest ``y`` of any region: 0, or further back if the path flies behind the near edge."""
    return min([0, *(y for _, y in points)])


def _offset(placement_offset_cm: Any) -> int:
    offset = _int_cm("placement_offset_cm", placement_offset_cm, 0, MAX_AREA_SIDE_CM)
    if 0 < offset < MIN_MOVE_CM:
        raise ValueError(f"placement_offset_cm must be 0 or at least {MIN_MOVE_CM} cm, got {offset}")
    return offset


# ----------------------------------------------------------------------
# Patterns
# ----------------------------------------------------------------------


def expanding_square_lanes(initial_side_cm: int = 100, growth_cm: int = 100, num_expansions: int = 4) -> list[dict]:
    """Axis-aligned rewrite of ``search.generate_expanding_square``.

    The legacy generator flies ``forward`` and turns 90 degrees clockwise
    after every leg. This version keeps the same leg lengths (two legs of
    each length, growing by ``growth_cm`` after every second leg) but changes
    direction instead of turning: ``forward``, ``right``, ``back``, ``left``.
    The heading never changes, so the lane executor accepts it. Patterns
    above ``MAX_MOVES_PER_DRONE`` moves are refused before they are built.
    """
    side = _int_cm("initial_side_cm", initial_side_cm, MIN_MOVE_CM, MAX_AREA_SIDE_CM)
    growth = _int_cm("growth_cm", growth_cm, 0, MAX_AREA_SIDE_CM)
    if isinstance(num_expansions, bool) or not isinstance(num_expansions, int) or not 1 <= num_expansions <= 100:
        raise ValueError(f"num_expansions must be an integer within 1-100, got {num_expansions!r}")
    order = ("forward", "right", "back", "left")
    lengths = [side + (leg // 2) * growth for leg in range(4 * num_expansions)]
    # Count the moves before building them, so an oversized pattern is refused without allocating it.
    _check_move_cap(sum(max(1, math.ceil(length / MAX_MOVE_CM)) for length in lengths))
    lanes: list[dict] = []
    for leg, length in enumerate(lengths):
        lanes.extend(_split_distance(order[leg % 4], length))
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
    # clamped lanes leave part of the gap unsearched, which the coverage
    # check reports.
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


def _parallel_lane_ends(pf: PlanningFootprint, depth: int, inset: int) -> tuple[int, int]:
    """Where each lane along ``y`` starts and ends so the passes cover ``[inset, depth - inset]``.

    The square model keeps the plan's convention of lanes from edge to edge.
    A forward camera first sees the ground ``near_cm`` ahead and still sees
    ``far_cm`` ahead, so its lanes start ``near_cm`` before the near edge
    (behind the area) and stop once the far edge of the view has reached the
    far edge of the area.
    """
    if pf.kind == SQUARE_FOOTPRINT:
        start, end = inset, depth - inset
        if end - start < MIN_MOVE_CM:
            raise ValueError(f"lanes of {end - start} cm are shorter than the {MIN_MOVE_CM} cm minimum move")
        return start, end
    if depth - 2 * inset < 0:
        raise ValueError(f"end_inset_cm {inset} leaves nothing of the {depth} cm depth to search")
    start = inset - int(pf.near_cm)
    end = max(start, depth - inset - int(pf.far_cm))
    if 0 < end - start < MIN_MOVE_CM:
        # Too short for one move: fly the minimum move if the area allows it,
        # else hover (the coverage check reports anything left unseen).
        end = start + MIN_MOVE_CM if start + MIN_MOVE_CM <= depth else start
    return start, end


def plan_parallel_split(
    drone_ids: Sequence[str],
    area_cm: Any,
    sweep_cm: int | None,
    *,
    footprint: Any,
    gap_cm: int = DEFAULT_GAP_CM,
    end_inset_cm: int = 0,
    alt_slots_cm: Sequence[int] = DEFAULT_ALT_SLOTS_CM,
    geofence_cm: float | None = DEFAULT_PLAN_GEOFENCE_CM,
    min_separation_cm: float | None = DEFAULT_MIN_SEPARATION_CM,
    placement_tolerance_cm: float = DEFAULT_PLACEMENT_TOLERANCE_CM,
    max_standoff_cm: float | str | None = STANDOFF_FROM_GEOFENCE,
) -> FleetSearchPlan:
    """Parallel-track (boustrophedon) split: one vertical strip per drone.

    The area is split across its width into equal strips separated by
    ``gap_cm``; drone ``i`` owns strip ``i`` from the left. Each strip is
    covered by lanes along ``y`` (``forward`` and ``back``), because a
    Tello's camera faces along its fixed heading and cannot turn. Each drone
    covers its own strip plus half of every neighbouring gap, so the gaps
    are searched without a drone ever entering one (a gap wider than the
    lateral sweep leaves a strip no drone may see, which the coverage check
    refuses). Lanes cover ``end_inset_cm`` to ``depth - end_inset_cm`` (see
    ``_parallel_lane_ends``). Every drone flies its lanes left to right and
    takes off at the start of its first lane, so no transit is needed.

    For two drones the equal split is already balanced in estimated time,
    because the two strips mirror each other.
    """
    ids = _ids(drone_ids)
    width, depth = _area(area_cm)
    n = len(ids)
    gap = _int_cm("gap_cm", gap_cm, 1 if n > 1 else 0, MAX_AREA_SIDE_CM)
    inset = _int_cm("end_inset_cm", end_inset_cm, 0, MAX_AREA_SIDE_CM)
    slots = _slots(alt_slots_cm)
    limits = _safety_inputs(geofence_cm, min_separation_cm, placement_tolerance_cm, max_standoff_cm)
    alts = _alts(slots, n)
    pf = planning_footprint(footprint, sweep_cm, alts, depth, limits["placement_tolerance_cm"])
    sweep = pf.lateral_cm
    y_start, y_end = _parallel_lane_ends(pf, depth, inset)
    total = width - gap * (n - 1)
    if total < n * MIN_MOVE_CM:
        raise ValueError(f"area width {width} cm is too narrow for {n} strips with {gap} cm gaps")
    base, extra = divmod(total, n)
    half_gap = (gap + 1) // 2
    floor_y = min(0, y_start)
    drones = []
    x = 0
    for i, drone_id in enumerate(ids):
        strip = base + (1 if i < extra else 0)
        zone_lo, zone_hi = x, x + strip
        x = zone_hi + gap
        lo = zone_lo - (half_gap if i > 0 else 0)
        hi = zone_hi + (half_gap if i < n - 1 else 0)
        points: list[Point] = []
        for k, lane_x in enumerate(_lane_positions(lo, hi, zone_lo, zone_hi, sweep)):
            ends = (y_start, y_end) if k % 2 == 0 else (y_end, y_start)
            points.extend([(lane_x, ends[0]), (lane_x, ends[1])])
        region = Region(zone_lo, floor_y, zone_hi, depth)
        drones.append(DronePlan(
            drone_id=drone_id,
            takeoff_cm=points[0],
            region=region,
            alt_cm=alts[i],
            lanes=tuple(_polyline_lanes(points, region)),
        ))
    plan = FleetSearchPlan(
        pattern=PatternKind.PARALLEL_SPLIT,
        area_cm=(width, depth),
        sweep_cm=sweep,
        gap_cm=gap,
        drones=tuple(drones),
        footprint=pf,
        standoff_cm=-floor_y,
        **limits,
    )
    return _finish(plan)


def _half_ring_points(
    seam: int, u_hi: int, ly: int, y_lo: int, y_hi: int, step_u: int, step_y: int
) -> list[tuple[int, int]]:
    """``(u, y)`` corners of the U-rings in one half-plane, ``u`` measured from the LKP.

    Ring ``k`` (from 1) has a leg along ``y = lkp_y -/+ ry_k`` with
    ``ry_k = step_y // 2 + (k - 1) * step_y`` from the seam ``u = seam`` out
    to ``u = ru_k = step_u // 2 + (k - 1) * step_u``, the outer leg along
    ``u = ru_k``, and a leg back to the seam. The seam may lie on the far
    side of the LKP (``seam < 0``). Odd rings start at the bottom and even
    rings at the top, so consecutive rings are joined by a step along the
    seam. Every coordinate is clamped to the path bounds, which keeps each
    pass's footprint inside the area while still reaching its edge, and the
    rings stop once a ring reaches the bounds on all three open sides.
    """
    needed_y = max(ly - y_lo, y_hi - ly)
    corners: list[tuple[int, int]] = []
    k = 1
    while True:
        ru = step_u // 2 + (k - 1) * step_u
        ry = step_y // 2 + (k - 1) * step_y
        outer = min(max(ru, seam), u_hi)
        bottom = max(ly - ry, y_lo)
        top = min(ly + ry, y_hi)
        if k % 2 == 1:
            corners.extend([(seam, bottom), (outer, bottom), (outer, top), (seam, top)])
        else:
            corners.extend([(seam, top), (outer, top), (outer, bottom), (seam, bottom)])
        if ru >= u_hi and ry >= needed_y:
            return corners
        k += 1


def _split_square_seams(lx: int, width: int, gap: int, lateral: int, offset: int) -> tuple[int, int] | None:
    """Region edges ``(e1, e2)`` of the split square (drone 1 owns ``x <= e1``), or None if none fits.

    Symmetric when the two seam passes still meet over the LKP
    (``2 * ceil(gap / 2) <= lateral``): ``e1 = lx - s``, ``e2 = lx + s``.
    Otherwise a symmetric split would leave a strip around the LKP that no
    footprint reaches, so the seam moves to one side: the drone on the
    smaller side owns ``lateral // 2`` past the LKP and flies over it from
    the first pass, and the other region starts exactly ``gap`` further on.
    Both drones must still take off ``offset`` inside their own region.
    """
    s = (gap + 1) // 2
    if 2 * s <= lateral:
        options = [(lx - s, lx + s)]
    else:
        h = lateral // 2
        first_covers = (lx + h, lx + h + gap)
        second_covers = (lx - h - gap, lx - h)
        options = [first_covers, second_covers] if lx <= width - lx else [second_covers, first_covers]
    for e1, e2 in options:
        if e1 - offset >= 0 and e2 + offset <= width:
            return e1, e2
    return None


def plan_split_expanding_square(
    drone_ids: Sequence[str],
    area_cm: Any,
    lkp_cm: Sequence[int],
    sweep_cm: int | None,
    *,
    footprint: Any,
    gap_cm: int = DEFAULT_GAP_CM,
    placement_offset_cm: int = DEFAULT_PLACEMENT_OFFSET_CM,
    stagger_s: float = DEFAULT_TAKEOFF_STAGGER_S,
    alt_slots_cm: Sequence[int] = DEFAULT_ALT_SLOTS_CM,
    geofence_cm: float | None = DEFAULT_PLAN_GEOFENCE_CM,
    min_separation_cm: float | None = DEFAULT_MIN_SEPARATION_CM,
    placement_tolerance_cm: float = DEFAULT_PLACEMENT_TOLERANCE_CM,
    max_standoff_cm: float | str | None = STANDOFF_FROM_GEOFENCE,
) -> FleetSearchPlan:
    """Split expanding square: two half-plane U-ring patterns around the LKP.

    Drone 1 owns ``x <= e1`` and drone 2 ``x >= e2`` (``_split_square_seams``).
    When the gap allows, the seam is centred (``e1, e2 = lx -/+ ceil(gap / 2)``)
    and the two patterns are exact mirror images. When it does not (the gap
    is wider than one lateral sweep), a centred seam would leave the LKP
    itself unsearched, so the seam is moved so that one drone's region
    reaches ``lateral // 2`` past the LKP and its first pass sees the LKP;
    the regions are then exactly ``gap_cm`` apart. The strip between them,
    which no drone may enter, starts half a sweep from the LKP, and the part
    of it that neither footprint sees is at least one sweep from the LKP. In
    both cases each drone's whole path stays inside its region, so the
    separation rule of the module docstring holds unchanged.

    Each drone flies expanding U-rings (three legs per ring plus a step along
    the seam) outward from the LKP until its half of the area is covered, so
    both cover the most probable ground first. Legs across the heading are
    spaced by the footprint's ``along_cm`` and the drone flies ``ahead_cm``
    behind the row it is searching (0 for the square model). Each drone takes
    off ``placement_offset_cm`` inside its region from the seam, level with
    the LKP row (clamped to between its first ring row and the highest path
    row, so an LKP near the near edge does not move the takeoff behind the
    rings), flies to the start of its first ring and then along it
    (``transit_lanes`` counts the moves before the ring). The second drone
    starts ``stagger_s`` later because the two take off close together.
    """
    ids = _ids(drone_ids)
    if len(ids) != 2:
        raise ValueError(f"the split expanding square is defined for exactly two drones, got {len(ids)}")
    width, depth = _area(area_cm)
    lx, ly = _lkp(lkp_cm, width, depth)
    gap = _int_cm("gap_cm", gap_cm, 1, MAX_AREA_SIDE_CM)
    offset = _offset(placement_offset_cm)
    stagger = _stagger(stagger_s)
    slots = _slots(alt_slots_cm)
    limits = _safety_inputs(geofence_cm, min_separation_cm, placement_tolerance_cm, max_standoff_cm)
    alts = _alts(slots, 2)
    pf = planning_footprint(footprint, sweep_cm, alts, depth, limits["placement_tolerance_cm"])
    _ring_footprint(pf, width, depth, "split expanding square")
    seams = _split_square_seams(lx, width, gap, pf.lateral_cm, offset)
    if seams is None:
        raise ValueError(
            f"LKP x={lx} cm is too close to a side edge of the {width} cm area for two regions "
            f"{gap} cm apart with a {offset} cm placement offset"
        )
    e1, e2 = seams
    y_lo, y_hi = _path_bounds(depth, pf.along_cm)
    sweep = pf.lateral_cm
    ahead = pf.ahead_cm
    layouts = []
    for drone_id, sign, seam_u in zip(ids, (-1, 1), (lx - e1, e2 - lx)):
        u_edge = lx if sign < 0 else width - lx
        u_hi = max(seam_u, u_edge - sweep // 2)
        corners = _half_ring_points(seam_u, u_hi, ly, y_lo, y_hi, sweep, pf.along_cm)
        takeoff_u = seam_u + offset
        first_u, first_y = corners[0]
        # Level with the LKP row, but never behind the first ring row nor
        # beyond the path bounds: the takeoff alone must not push the
        # standoff past what the rings need, however close the LKP is to an edge.
        takeoff_y = min(max(ly, first_y), y_hi)
        transit = [(takeoff_u, takeoff_y), (takeoff_u, first_y), (first_u, first_y)]

        def to_xy(points: Iterable[tuple[int, int]], sign: int = sign) -> list[Point]:
            # Search rows are ``ahead`` cm in front of the drone.
            return [(lx + sign * u, y - ahead) for u, y in points]

        layouts.append((drone_id, to_xy(transit), to_xy(corners)))
    floor_y = _floor_y(p for _, transit, corners in layouts for p in transit + corners)
    regions = (Region(0, floor_y, e1, depth), Region(e2, floor_y, width, depth))
    drones = []
    for i, ((drone_id, transit, corners), region) in enumerate(zip(layouts, regions)):
        transit_lanes = _polyline_lanes(transit, region)
        pattern_lanes = _polyline_lanes(corners, region)
        drones.append(DronePlan(
            drone_id=drone_id,
            takeoff_cm=transit[0],
            region=region,
            alt_cm=alts[i],
            lanes=tuple(transit_lanes + pattern_lanes),
            transit_lanes=len(transit_lanes),
            start_offset_s=stagger * i,
        ))
    plan = FleetSearchPlan(
        pattern=PatternKind.SPLIT_EXPANDING_SQUARE,
        area_cm=(width, depth),
        sweep_cm=sweep,
        gap_cm=gap,
        drones=tuple(drones),
        lkp_cm=(lx, ly),
        footprint=pf,
        standoff_cm=-floor_y,
        **limits,
    )
    return _finish(plan)


def plan_square_spiral(
    drone_id: str,
    area_cm: Any,
    lkp_cm: Sequence[int],
    sweep_cm: int | None,
    *,
    footprint: Any,
    alt_cm: int = DEFAULT_ALT_SLOTS_CM[0],
    geofence_cm: float | None = DEFAULT_PLAN_GEOFENCE_CM,
    placement_tolerance_cm: float = DEFAULT_PLACEMENT_TOLERANCE_CM,
    max_standoff_cm: float | str | None = STANDOFF_FROM_GEOFENCE,
) -> FleetSearchPlan:
    """Single-drone expanding square as an axis-aligned square spiral, bounded by the area.

    The drone takes off at the LKP (``ahead_cm`` behind it for a forward
    camera, so the LKP is in view) and flies legs ``forward``, ``right``,
    ``back``, ``left`` whose lengths grow by one along-track spacing
    (``forward``/``back``) or one lateral sweep (``right``/``left``) every
    second leg, so neighbouring parallel legs are one spacing apart. Each
    corner is clamped to the path bounds, which keeps every pass's footprint
    reaching the area edge, and the spiral stops once its latest leg on each
    of the four sides has reached the bounds.
    """
    ids = _ids([drone_id])
    width, depth = _area(area_cm)
    lx, ly = _lkp(lkp_cm, width, depth)
    slot = _slots([alt_cm])[0]
    limits = _safety_inputs(geofence_cm, None, placement_tolerance_cm, max_standoff_cm)
    pf = planning_footprint(footprint, sweep_cm, [slot], depth, limits["placement_tolerance_cm"])
    _ring_footprint(pf, width, depth, "square spiral")
    step_x, step_y = pf.lateral_cm, pf.along_cm
    x_lo, x_hi = _path_bounds(width, step_x)
    y_lo, y_hi = _path_bounds(depth, step_y)

    def clamp(point: tuple[int, int]) -> Point:
        return min(max(point[0], x_lo), x_hi), min(max(point[1], y_lo), y_hi) - pf.ahead_cm

    # Directions in order, the side of the ring each one runs along, and the
    # test that the ideal (unclamped) leg already lies at or beyond the bound.
    order = ((0, 1), (1, 0), (0, -1), (-1, 0))
    reached = [False, False, False, False]
    x, y = lx, ly
    ideal = [(x, y)]
    leg = 0
    while not all(reached):
        dx, dy = order[leg % 4]
        length = (leg // 2 + 1) * (step_y if leg % 2 == 0 else step_x)
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
    points = [clamp(p) for p in ideal]
    floor_y = _floor_y(points)
    region = Region(0, floor_y, width, depth)
    plan = FleetSearchPlan(
        pattern=PatternKind.SQUARE_SPIRAL,
        area_cm=(width, depth),
        sweep_cm=step_x,
        gap_cm=0,
        drones=(DronePlan(
            drone_id=ids[0],
            takeoff_cm=points[0],
            region=region,
            alt_cm=slot,
            lanes=tuple(_polyline_lanes(points, region)),
        ),),
        lkp_cm=(lx, ly),
        footprint=pf,
        standoff_cm=-floor_y,
        **limits,
    )
    return _finish(plan)


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
    sweep_cm: int | None = None,
) -> PatternChoice:
    """Pick a pattern by the decision table in plan section 3.4.

    * A last-known point (LKP) and two drones: the split expanding square,
      which minimises the expected time to first detection under a prior
      centred on the LKP. If the LKP is too close to a side edge for the two
      regions and their takeoff placements, or the area is shallower than
      ``MIN_SWEEP_CM``, the parallel split instead. ``sweep_cm`` is the
      lateral sweep used to place the seam (``_split_square_seams``); without
      it only the centred seam is tried.
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
    if min(width, depth) < MIN_SWEEP_CM:
        return PatternChoice(
            PatternKind.PARALLEL_SPLIT, f"the area is narrower than {MIN_SWEEP_CM} cm for rings; parallel split"
        )
    if n_drones == 1:
        return PatternChoice(PatternKind.SQUARE_SPIRAL, "one drone with a last-known point: square spiral from the LKP")
    if n_drones > 2:
        return PatternChoice(
            PatternKind.PARALLEL_SPLIT, "the split expanding square is defined for two drones; parallel split"
        )
    gap = _int_cm("gap_cm", gap_cm, 1, MAX_AREA_SIDE_CM)
    offset = _offset(placement_offset_cm)
    # Without a sweep, 2 * gap is wide enough that only the centred seam is tried.
    lateral = 2 * gap if sweep_cm is None else _int_cm("sweep_cm", sweep_cm, MIN_SWEEP_CM)
    if _split_square_seams(lx, width, gap, lateral, offset) is None:
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
    sweep_cm: int | None,
    lkp_cm: Sequence[int] | None = None,
    *,
    footprint: Any,
    gap_cm: int = DEFAULT_GAP_CM,
    end_inset_cm: int = 0,
    placement_offset_cm: int = DEFAULT_PLACEMENT_OFFSET_CM,
    stagger_s: float = DEFAULT_TAKEOFF_STAGGER_S,
    alt_slots_cm: Sequence[int] = DEFAULT_ALT_SLOTS_CM,
    geofence_cm: float | None = DEFAULT_PLAN_GEOFENCE_CM,
    min_separation_cm: float | None = DEFAULT_MIN_SEPARATION_CM,
    placement_tolerance_cm: float = DEFAULT_PLACEMENT_TOLERANCE_CM,
    max_standoff_cm: float | str | None = STANDOFF_FROM_GEOFENCE,
) -> FleetSearchPlan:
    """Choose a pattern with ``choose_pattern`` and build it; ``reason`` records the choice.

    ``footprint`` is required: the real camera (a ``coverage.SweepModel`` or
    ``coverage_sim.ForwardFootprint``), or ``SQUARE_FOOTPRINT`` for a model
    study. If the chosen ring pattern cannot be built for this area and
    footprint (for example the camera's along-track window is too short),
    the parallel split is built instead and ``reason`` says why.
    """
    ids = _ids(drone_ids)
    width, depth = _area(area_cm)
    slots = _slots(alt_slots_cm)
    # Validate everything up front, so a fallback below can never hide a bad input.
    limits = _safety_inputs(geofence_cm, min_separation_cm, placement_tolerance_cm, max_standoff_cm)
    _offset(placement_offset_cm)
    _stagger(stagger_s)
    _int_cm("end_inset_cm", end_inset_cm, 0, MAX_AREA_SIDE_CM)
    pf = planning_footprint(footprint, sweep_cm, _alts(slots, len(ids)), depth, limits["placement_tolerance_cm"])
    safety = {
        "geofence_cm": geofence_cm,
        "placement_tolerance_cm": placement_tolerance_cm,
        "max_standoff_cm": max_standoff_cm,
    }
    choice = choose_pattern(
        len(ids), area_cm, lkp_cm, gap_cm=gap_cm, placement_offset_cm=placement_offset_cm, sweep_cm=pf.lateral_cm
    )
    reason = choice.reason

    def parallel() -> FleetSearchPlan:
        built = plan_parallel_split(
            ids, area_cm, sweep_cm, footprint=footprint,
            gap_cm=gap_cm, end_inset_cm=end_inset_cm, alt_slots_cm=alt_slots_cm,
            min_separation_cm=min_separation_cm, **safety,
        )
        return built if lkp_cm is None else replace(built, lkp_cm=_lkp(lkp_cm, width, depth))

    try:
        if choice.kind is PatternKind.SPLIT_EXPANDING_SQUARE:
            plan = plan_split_expanding_square(
                ids, area_cm, lkp_cm, sweep_cm, footprint=footprint,
                gap_cm=gap_cm, placement_offset_cm=placement_offset_cm, stagger_s=stagger_s,
                alt_slots_cm=alt_slots_cm, min_separation_cm=min_separation_cm, **safety,
            )
        elif choice.kind is PatternKind.SQUARE_SPIRAL:
            plan = plan_square_spiral(ids[0], area_cm, lkp_cm, sweep_cm, footprint=footprint, alt_cm=slots[0], **safety)
        else:
            plan = parallel()
    except ValueError as exc:
        if choice.kind is PatternKind.PARALLEL_SPLIT:
            raise
        reason = f"{choice.reason}, but it cannot be built here ({exc}); parallel split instead"
        plan = parallel()
    return replace(plan, reason=reason)

