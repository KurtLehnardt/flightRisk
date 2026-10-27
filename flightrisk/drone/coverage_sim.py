"""Coverage and timing model for fleet search plans.

Everything this module returns is model output, not a measurement. It uses
the fleet frame and units of ``flightrisk.drone.fleet_patterns``
(centimetres, origin at the near-left corner of the search area, ``x`` to the
right, ``y`` ahead along the shared takeoff heading) and seconds.

Timing model (``TimingModel``)
------------------------------
A drone sits at its takeoff point until ``start_offset_s + takeoff_s``. Every
move then costs ``move_overhead_s`` of hovering in place (stop, settle and
the command round trip), plus ``turn_extra_s`` when its direction differs
from the previous move's, followed by straight flight at ``speed_cm_s``.
Lanes longer than 500 cm are split exactly as the lane executor splits them.
Landing takes ``land_s`` at the last position. A drone's total time is
``start_offset_s + takeoff_s + sum(overhead + distance / speed) + land_s``;
the makespan is the largest total. The overhead of 1.5 s per move is a
placeholder until hardware rung L2 measures it.

Detection model
---------------
The search area is rasterised into square cells (10 cm by default). A cell is
found at the first instant its centre lies inside the camera footprint of an
airborne drone. ``RectFootprint.square(sweep)`` is the plan's idealised square
footprint centred on the drone; ``ForwardFootprint`` is the trapezoid ahead of
a forward-facing camera from ``coverage.SweepModel`` (both are defined in
``flightrisk.drone.footprints`` and re-exported here). The camera always faces
``+y`` because fleet drones never rotate. First-detection times are exact for
piecewise-linear flight, not sampled.

Given a prior ``p`` over cells, the expected time to first detection is the
plan's ``E[T_det] = sum(p_c * t_first(c)) / POD``. A cell the plan never sees
has ``t_first = inf``, so whenever any prior mass is left unsearched
``expected_time_to_detection_s`` is infinite: an incomplete plan never reports
a finite time that hides the area it skipped. ``prob_covered`` (the prior mass
the plan sees) is always reported next to it, and
``conditional_time_to_detection_s`` is the same sum restricted to the covered
cells and divided by ``prob_covered``, which is only the expected time given
that the person is inside the searched part. POD is 1 until rung L1
calibrates it.

``covered_cells`` answers only "is this cell ever seen" for axis-aligned
polylines, in time linear in the number of legs times the raster side.
``covered_fraction`` is what the planner's coverage gate uses: the fraction of
the area the footprints ever see, measured exactly along horizontal lines and
integrated over ``y`` with every polygon edge as a breakpoint, so a strip
narrower than a raster cell is not missed. ``first_detection_times`` only
evaluates the cells each segment's footprint can reach, and refuses work above
``MAX_DETECTION_WORK`` cell evaluations instead of stalling.

Separation
----------
``min_separation_cm`` computes the exact minimum horizontal distance between
two piecewise-linear tracks, walking both timelines once (linear in their
length). ``min_separation_under_jitter`` repeats it with
every move's duration scaled by a random factor within ``1 +/- jitter``
(plan section 3.5 uses 30%), plus the all-fast and all-slow extremes.
"""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from flightrisk.drone.footprints import (
    MAX_SCAN_INTERVALS,
    Footprint,
    ForwardFootprint,
    RectFootprint,
    _per_track,
    covered_fraction,
)
from flightrisk.drone.lane_executor import split_lane_moves

__all__ = [
    "DEFAULT_TIMING",
    "MAX_DETECTION_WORK",
    "MAX_RASTER_CELLS",
    "MAX_SCAN_INTERVALS",
    "DroneEstimate",
    "Footprint",
    "ForwardFootprint",
    "Raster",
    "RectFootprint",
    "SearchEstimate",
    "Segment",
    "TimingModel",
    "covered_cells",
    "covered_fraction",
    "estimate_plan",
    "first_detection_times",
    "gaussian_prior",
    "min_separation_cm",
    "min_separation_under_jitter",
    "plan_tracks",
    "timeline",
    "uniform_prior",
]

_VECTORS = {"forward": (0.0, 1.0), "back": (0.0, -1.0), "right": (1.0, 0.0), "left": (-1.0, 0.0)}

MAX_RASTER_CELLS = 4_000_000
"""Largest raster accepted: a 200 m x 200 m area at 10 cm cells. Larger rasters
are refused with an error rather than left to stall the caller."""

MAX_DETECTION_WORK = 600_000_000
"""Most (segment, cell) evaluations ``first_detection_times`` performs before
refusing with an error that asks for a larger ``cell_cm``."""


@dataclass(frozen=True)
class TimingModel:
    """Speeds and fixed costs of a relative-move drone (defaults: plan section 3.4)."""

    speed_cm_s: float = 100.0
    move_overhead_s: float = 1.5
    turn_extra_s: float = 0.0
    takeoff_s: float = 0.0
    land_s: float = 0.0

    def __post_init__(self) -> None:
        if not (math.isfinite(self.speed_cm_s) and self.speed_cm_s > 0):
            raise ValueError(f"speed_cm_s must be positive, got {self.speed_cm_s!r}")
        for name in ("move_overhead_s", "turn_extra_s", "takeoff_s", "land_s"):
            value = getattr(self, name)
            if not (math.isfinite(value) and value >= 0):
                raise ValueError(f"{name} must not be negative, got {value!r}")


DEFAULT_TIMING = TimingModel()
"""1 m/s and 1.5 s per move: the plan's placeholder values until rung L2 measures them."""


@dataclass(frozen=True)
class Segment:
    """Straight flight (or a hover when both ends match) from ``t0`` to ``t1``."""

    t0: float
    t1: float
    x0: float
    y0: float
    x1: float
    y1: float
    airborne: bool = True
    move_index: int | None = None


def _moves(lanes: Iterable[dict]) -> list[tuple[int, str, int]]:
    """``(lane_index, direction, distance_cm)`` moves, split like the lane executor splits lanes."""
    return [
        (index, lane["direction"], distance)
        for index, lane in enumerate(lanes)
        for distance in split_lane_moves(lane["distance_cm"])
    ]


def timeline(
    start_cm: Sequence[float],
    lanes: Sequence[dict],
    timing: TimingModel = DEFAULT_TIMING,
    start_offset_s: float = 0.0,
    scales: Sequence[float] | None = None,
) -> list[Segment]:
    """The segments one drone flies: waiting on the ground, then each move, then landing.

    ``scales`` optionally stretches each move's overhead and flight time by
    its own factor (one per executor move), to model timing uncertainty.
    """
    moves = _moves(lanes)
    if scales is not None and len(scales) != len(moves):
        raise ValueError(f"expected {len(moves)} scale factors, got {len(scales)}")
    x, y = float(start_cm[0]), float(start_cm[1])
    t = 0.0
    segments: list[Segment] = []
    wait = start_offset_s + timing.takeoff_s
    if wait > 0:
        segments.append(Segment(0.0, wait, x, y, x, y, airborne=False))
        t = wait
    previous = None
    for k, (_, direction, distance) in enumerate(moves):
        scale = 1.0 if scales is None else float(scales[k])
        hover = timing.move_overhead_s + (timing.turn_extra_s if previous not in (None, direction) else 0.0)
        hover *= scale
        if hover > 0:
            segments.append(Segment(t, t + hover, x, y, x, y, move_index=k))
            t += hover
        dx, dy = _VECTORS[direction]
        nx, ny = x + dx * distance, y + dy * distance
        duration = distance / timing.speed_cm_s * scale
        segments.append(Segment(t, t + duration, x, y, nx, ny, move_index=k))
        t += duration
        x, y = nx, ny
        previous = direction
    if timing.land_s > 0:
        segments.append(Segment(t, t + timing.land_s, x, y, x, y, airborne=False))
    if not segments:
        segments.append(Segment(0.0, 0.0, x, y, x, y))
    return segments


def plan_tracks(
    plan: Any,
    timing: TimingModel = DEFAULT_TIMING,
    *,
    include_transit: bool = True,
    scales: dict[str, Sequence[float]] | None = None,
) -> dict[str, list[Segment]]:
    """Timelines for every drone of a ``FleetSearchPlan``, keyed by drone id.

    With ``include_transit=False`` each drone starts at its pattern start
    with no start offset, which is how the plan's illustrative numbers were
    computed.
    """
    tracks = {}
    for drone in plan.drones:
        if not include_transit:
            drone = drone.without_transit()
        tracks[drone.drone_id] = timeline(
            drone.takeoff_cm,
            drone.lanes,
            timing,
            drone.start_offset_s,
            None if scales is None else scales.get(drone.drone_id),
        )
    return tracks


# ----------------------------------------------------------------------
# Raster and priors
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Raster:
    """Square cells covering ``[0, width_cm] x [0, depth_cm]``, addressed by their centres."""

    width_cm: float
    depth_cm: float
    cell_cm: float = 10.0

    def __post_init__(self) -> None:
        values = (self.width_cm, self.depth_cm, self.cell_cm)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise ValueError("raster dimensions and cell size must be finite numbers")
        if self.width_cm <= 0 or self.depth_cm <= 0 or self.cell_cm <= 0:
            raise ValueError("raster dimensions and cell size must be positive")
        cells = self.shape[0] * self.shape[1]
        if cells > MAX_RASTER_CELLS:
            raise ValueError(
                f"a {self.width_cm:g} x {self.depth_cm:g} cm raster of {self.cell_cm:g} cm cells has {cells} cells, "
                f"above the {MAX_RASTER_CELLS} limit; use a larger cell_cm"
            )

    @property
    def shape(self) -> tuple[int, int]:
        """``(nx, ny)``: cells across the width and along the depth."""
        nx = max(1, math.ceil(self.width_cm / self.cell_cm - 1e-9))
        ny = max(1, math.ceil(self.depth_cm / self.cell_cm - 1e-9))
        return nx, ny

    def axes(self) -> tuple[np.ndarray, np.ndarray]:
        """Cell-centre coordinates along ``x`` and along ``y``."""
        nx, ny = self.shape
        xs = np.minimum((np.arange(nx) + 0.5) * self.cell_cm, self.width_cm)
        ys = np.minimum((np.arange(ny) + 0.5) * self.cell_cm, self.depth_cm)
        return xs, ys

    def centres(self) -> tuple[np.ndarray, np.ndarray]:
        """Every cell centre, flattened with ``x`` as the slow index (cell ``i * ny + j``)."""
        xs, ys = self.axes()
        cx, cy = np.meshgrid(xs, ys, indexing="ij")
        return cx.ravel(), cy.ravel()


def uniform_prior(raster: Raster) -> np.ndarray:
    cx, _ = raster.centres()
    return np.full(cx.shape, 1.0 / cx.size)


def gaussian_prior(raster: Raster, centre_cm: Sequence[float], sigma_cm: float) -> np.ndarray:
    """Isotropic Gaussian centred on the LKP, truncated to the area and normalised."""
    if sigma_cm <= 0:
        raise ValueError("sigma_cm must be positive")
    cx, cy = raster.centres()
    weights = np.exp(-((cx - centre_cm[0]) ** 2 + (cy - centre_cm[1]) ** 2) / (2 * sigma_cm**2))
    return weights / weights.sum()


# ----------------------------------------------------------------------
# First detection
# ----------------------------------------------------------------------


def _entry_times(seg: Segment, cx: np.ndarray, cy: np.ndarray, footprint: Footprint) -> np.ndarray:
    """Earliest time in ``seg`` each cell is visible, or inf."""
    length_x = seg.x1 - seg.x0
    length_y = seg.y1 - seg.y0
    inf = np.full(cx.shape, np.inf)
    if length_x == 0 and length_y == 0:
        lo, hi = footprint.along_interval(np.abs(cx - seg.x0))
        dy = cy - seg.y0
        return np.where((dy >= lo) & (dy <= hi), seg.t0, inf)
    duration = seg.t1 - seg.t0
    if length_x == 0:
        # Moving along y at fixed x: visible while drone y lies in [cy - hi, cy - lo].
        lo, hi = footprint.along_interval(np.abs(cx - seg.x0))
        a, b = cy - hi, cy - lo
        start, end, length = seg.y0, seg.y1, length_y
    else:
        # Moving along x at fixed y: visible while drone x lies in [cx - h, cx + h].
        h = footprint.lateral_halfwidth(cy - seg.y0)
        a = np.where(h >= 0, cx - h, np.inf)
        b = np.where(h >= 0, cx + h, -np.inf)
        start, end, length = seg.x0, seg.x1, length_x
    with np.errstate(invalid="ignore"):
        if length > 0:
            entry = np.maximum(start, a)
            ok = (a <= b) & (entry <= np.minimum(end, b))
            frac = (entry - start) / length
        else:
            entry = np.minimum(start, b)
            ok = (a <= b) & (entry >= np.maximum(end, a))
            frac = (start - entry) / -length
    return np.where(ok, seg.t0 + frac * duration, inf)


def first_detection_times(
    raster: Raster,
    tracks: Iterable[Sequence[Segment]],
    footprint: Footprint | Sequence[Footprint],
) -> np.ndarray:
    """Per cell, the first time any airborne drone's footprint contains its centre (inf if never).

    ``footprint`` is one footprint for every track, or a list with one per
    track (drones at different altitudes see different ground). Each segment
    is evaluated only on the cells its footprint can reach (its bounding box
    grown by the footprint's extent); more than ``MAX_DETECTION_WORK`` cell
    evaluations in all raise ``ValueError``.
    """
    tracks = list(tracks)
    footprints = _per_track(footprint, len(tracks))
    xs, ys = raster.axes()
    times = np.full((xs.size, ys.size), np.inf)
    jobs = []
    work = 0
    for track, fp in zip(tracks, footprints):
        lo, hi = fp.along_extent()
        reach = fp.lateral_reach()
        if lo > hi or reach < 0:
            continue
        for seg in track:
            if not seg.airborne:
                continue
            i0 = np.searchsorted(xs, min(seg.x0, seg.x1) - reach, side="left")
            i1 = np.searchsorted(xs, max(seg.x0, seg.x1) + reach, side="right")
            j0 = np.searchsorted(ys, min(seg.y0, seg.y1) + lo, side="left")
            j1 = np.searchsorted(ys, max(seg.y0, seg.y1) + hi, side="right")
            if i1 > i0 and j1 > j0:
                jobs.append((seg, fp, int(i0), int(i1), int(j0), int(j1)))
                work += (int(i1) - int(i0)) * (int(j1) - int(j0))
    if work > MAX_DETECTION_WORK:
        raise ValueError(
            f"first-detection times need {work} cell evaluations, above the {MAX_DETECTION_WORK} limit; "
            "use a larger cell_cm"
        )
    for seg, fp, i0, i1, j0, j1 in jobs:
        cx, cy = np.meshgrid(xs[i0:i1], ys[j0:j1], indexing="ij")
        block = times[i0:i1, j0:j1]
        np.minimum(block, _entry_times(seg, cx, cy, fp), out=block)
    return times.ravel()


def covered_cells(
    raster: Raster,
    paths: Iterable[Sequence[Sequence[float]]],
    footprint: Footprint | Sequence[Footprint],
) -> np.ndarray:
    """Per cell, whether any drone flying these axis-aligned polylines ever sees its centre.

    ``paths`` are waypoint lists (takeoff first); consecutive waypoints must
    share ``x`` or ``y``. The result equals ``np.isfinite`` of
    ``first_detection_times`` for the same flight, without the timing: a leg
    along ``y`` at ``x = X`` sees, in each column, the closed interval of
    ``y`` from its low end plus the footprint's nearest visible distance to
    its high end plus the farthest; a leg along ``x`` sees, in each row, an
    interval of ``x``. Intervals are accumulated in two difference arrays, so
    the cost is the number of legs times the raster side plus one pass over
    the cells.
    """
    paths = [list(path) for path in paths]
    footprints = _per_track(footprint, len(paths))
    xs, ys = raster.axes()
    nx, ny = xs.size, ys.size
    columns = np.zeros((nx, ny + 1), dtype=np.int32)
    rows = np.zeros((ny, nx + 1), dtype=np.int32)
    col_index = np.arange(nx)
    row_index = np.arange(ny)
    for path, fp in zip(paths, footprints):
        if not path:
            continue
        legs = list(itertools.pairwise(path)) or [(path[0], path[0])]
        for (x0, y0), (x1, y1) in legs:
            if x0 == x1:
                lo, hi = fp.along_interval(np.abs(xs - x0))
                with np.errstate(invalid="ignore"):
                    first = np.searchsorted(ys, min(y0, y1) + lo, side="left")
                    last = np.searchsorted(ys, max(y0, y1) + hi, side="right")
                ok = last > first
                np.add.at(columns, (col_index[ok], first[ok]), 1)
                np.add.at(columns, (col_index[ok], last[ok]), -1)
            elif y0 == y1:
                half = fp.lateral_halfwidth(ys - y0)
                visible = half >= 0
                first = np.searchsorted(xs, min(x0, x1) - half, side="left")
                last = np.searchsorted(xs, max(x0, x1) + half, side="right")
                ok = visible & (last > first)
                np.add.at(rows, (row_index[ok], first[ok]), 1)
                np.add.at(rows, (row_index[ok], last[ok]), -1)
            else:
                raise ValueError(f"leg ({x0}, {y0}) -> ({x1}, {y1}) is not axis-aligned")
    seen = np.cumsum(columns, axis=1)[:, :ny] > 0
    seen |= (np.cumsum(rows, axis=1)[:, :nx] > 0).T
    return seen.ravel()


# ----------------------------------------------------------------------
# Estimates
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class DroneEstimate:
    drone_id: str
    moves: int
    distance_cm: float
    transit_s: float
    total_s: float
    start_offset_s: float


@dataclass(frozen=True)
class SearchEstimate:
    """Model output for one plan; see the module docstring for the definitions."""

    drones: tuple[DroneEstimate, ...]
    makespan_s: float
    coverage: float
    prob_covered: float
    expected_time_to_detection_s: float
    time_to_coverage_s: dict
    conditional_time_to_detection_s: float = math.inf

    def drone(self, drone_id: str) -> DroneEstimate:
        for estimate in self.drones:
            if estimate.drone_id == drone_id:
                return estimate
        raise KeyError(drone_id)


def _track_end(track: Sequence[Segment]) -> float:
    return track[-1].t1 if track else 0.0


def estimate_plan(
    plan: Any,
    *,
    timing: TimingModel = DEFAULT_TIMING,
    footprint: Footprint | None = None,
    prior: Any = "uniform",
    sigma_cm: float | None = None,
    cell_cm: float = 10.0,
    pod: float = 1.0,
    include_transit: bool = True,
    coverage_levels: Sequence[float] = (0.5, 0.9, 0.99),
) -> SearchEstimate:
    """Makespan, per-drone times, coverage and expected time to first detection for a plan.

    ``footprint`` defaults to the plan's own camera footprints (one per drone,
    at its altitude slot; ``FleetSearchPlan.camera_footprints``), or to the
    square of the plan's sweep width for a plan without them. ``prior`` is
    ``"uniform"``, ``"lkp"`` (a Gaussian of ``sigma_cm`` on the plan's LKP)
    or an array over the raster cells. ``expected_time_to_detection_s`` is
    infinite unless the plan sees every cell with prior mass; see the module
    docstring.
    """
    if not (isinstance(pod, (int, float)) and math.isfinite(pod) and 0 < pod <= 1):
        raise ValueError("pod must be within (0, 1]")
    width, depth = plan.area_cm
    raster = Raster(width, depth, cell_cm)
    if footprint is None:
        cameras = getattr(plan, "camera_footprints", None)
        footprint = cameras() if callable(cameras) else RectFootprint.square(plan.sweep_cm)
    tracks = plan_tracks(plan, timing, include_transit=include_transit)
    drones = []
    for drone in plan.drones:
        flown = drone if include_transit else drone.without_transit()
        moves = _moves(flown.lanes)
        transit_moves = len(_moves(flown.lanes[: flown.transit_lanes]))
        track = tracks[drone.drone_id]
        takeoff_end = flown.start_offset_s + timing.takeoff_s
        transit_end = takeoff_end
        for seg in track:
            if seg.move_index is not None and seg.move_index < transit_moves:
                transit_end = seg.t1
        drones.append(DroneEstimate(
            drone_id=drone.drone_id,
            moves=len(moves),
            distance_cm=float(sum(distance for _, _, distance in moves)),
            transit_s=transit_end - takeoff_end,
            total_s=_track_end(track),
            start_offset_s=flown.start_offset_s,
        ))
    times = first_detection_times(raster, tracks.values(), footprint)
    if isinstance(prior, str):
        if prior == "uniform":
            weights = uniform_prior(raster)
        elif prior == "lkp":
            if plan.lkp_cm is None or sigma_cm is None:
                raise ValueError("the 'lkp' prior needs a plan with an LKP and sigma_cm")
            weights = gaussian_prior(raster, plan.lkp_cm, sigma_cm)
        else:
            raise ValueError(f"unknown prior {prior!r}")
    else:
        weights = np.asarray(prior, dtype=float)
        if weights.shape != times.shape:
            raise ValueError("prior must have one weight per raster cell")
        weights = weights / weights.sum()
    if not (np.all(np.isfinite(weights)) and np.all(weights >= 0) and weights.sum() > 0):
        raise ValueError("prior weights must be finite, non-negative and not all zero")
    covered = np.isfinite(times)
    prob_covered = float(weights[covered].sum())
    if prob_covered > 0:
        conditional = float((weights[covered] * times[covered]).sum() / prob_covered / pod)
    else:
        conditional = math.inf
    # The plan's formula sums p_c * t_first(c) over every cell; an unseen cell
    # with prior mass has t_first = inf, so the expectation is infinite.
    expected = conditional if not np.any(weights[~covered] > 0) else math.inf
    finite = np.sort(times[covered])
    levels = {}
    for level in coverage_levels:
        needed = math.ceil(level * times.size - 1e-9)
        levels[level] = float(finite[needed - 1]) if 0 < needed <= finite.size else math.inf
    return SearchEstimate(
        drones=tuple(drones),
        makespan_s=max((d.total_s for d in drones), default=0.0),
        coverage=float(covered.mean()),
        prob_covered=prob_covered,
        expected_time_to_detection_s=expected,
        time_to_coverage_s=levels,
        conditional_time_to_detection_s=conditional,
    )


# ----------------------------------------------------------------------
# Separation
# ----------------------------------------------------------------------


class _Walker:
    """Position along one timeline at non-decreasing times, advancing a segment pointer."""

    def __init__(self, track: Sequence[Segment]) -> None:
        self._track = track
        self._index = 0

    def at(self, t: float) -> tuple[float, float]:
        track = self._track
        if t <= track[0].t0:
            return track[0].x0, track[0].y0
        while self._index < len(track) and track[self._index].t1 < t:
            self._index += 1
        if self._index == len(track):
            return track[-1].x1, track[-1].y1
        return _interpolate(track[self._index], t)


def _interpolate(seg: Segment, t: float) -> tuple[float, float]:
    if seg.t1 == seg.t0:
        return seg.x1, seg.y1
    # Clamped, so a gap between two segments holds the drone where the next one starts.
    f = min(1.0, max(0.0, (t - seg.t0) / (seg.t1 - seg.t0)))
    return seg.x0 + f * (seg.x1 - seg.x0), seg.y0 + f * (seg.y1 - seg.y0)


def _position(track: Sequence[Segment], t: float) -> tuple[float, float]:
    """Position at time ``t``; before the first segment and after the last the drone stays put."""
    return _Walker(track).at(t)


def _segment_origin_distance(ax: float, ay: float, bx: float, by: float) -> float:
    """Distance from the origin to the segment from ``(ax, ay)`` to ``(bx, by)``."""
    vx, vy = bx - ax, by - ay
    length2 = vx * vx + vy * vy
    if length2 == 0:
        return math.hypot(ax, ay)
    f = max(0.0, min(1.0, -(ax * vx + ay * vy) / length2))
    return math.hypot(ax + f * vx, ay + f * vy)


def min_separation_cm(track_a: Sequence[Segment], track_b: Sequence[Segment]) -> float:
    """Exact minimum horizontal distance between two drones flying these timelines.

    Between consecutive breakpoints of either timeline both drones move
    linearly, so their offset moves along a straight segment and its
    smallest length is found in closed form. The breakpoints are visited in
    order with one pointer per timeline, so the cost is linear in the number
    of segments (after sorting the breakpoints).
    """
    times = sorted({0.0} | {s.t0 for s in track_a} | {s.t1 for s in track_a}
                   | {s.t0 for s in track_b} | {s.t1 for s in track_b})
    walk_a, walk_b = _Walker(track_a), _Walker(track_b)
    offsets = []
    for t in times:
        (xa, ya), (xb, yb) = walk_a.at(t), walk_b.at(t)
        offsets.append((xa - xb, ya - yb))
    best = math.hypot(*offsets[0])
    for (ax, ay), (bx, by) in itertools.pairwise(offsets):
        best = min(best, _segment_origin_distance(ax, ay, bx, by))
    return best


def min_separation_under_jitter(
    plan: Any,
    timing: TimingModel = DEFAULT_TIMING,
    *,
    jitter: float = 0.3,
    trials: int = 50,
    seed: int = 0,
) -> float:
    """Smallest pairwise separation over random per-move timing errors within ``1 +/- jitter``.

    The nominal timing and the all-fast and all-slow combinations of every
    drone (for up to eight drones) are always included. Returns inf for a
    single drone.
    """
    if isinstance(jitter, bool) or not isinstance(jitter, (int, float)) or not 0 <= jitter < 1:
        raise ValueError(f"jitter must be within [0, 1), got {jitter!r}")
    if isinstance(trials, bool) or not isinstance(trials, int) or trials < 0:
        raise ValueError(f"trials must be a whole number, at least 0, got {trials!r}")
    counts = {d.drone_id: len(_moves(d.lanes)) for d in plan.drones}
    ids = list(counts)
    if len(ids) < 2:
        return math.inf
    rng = random.Random(seed)
    extremes = (1 - jitter, 1 + jitter)
    scale_sets: list[dict[str, list[float]]] = [{drone_id: [1.0] * counts[drone_id] for drone_id in ids}]
    scale_sets += [
        {drone_id: [extremes[(i >> k) & 1 if k < 8 else 0]] * counts[drone_id] for k, drone_id in enumerate(ids)}
        for i in range(2 ** min(len(ids), 8))
    ]
    for _ in range(trials):
        scale_sets.append({
            drone_id: [rng.uniform(1 - jitter, 1 + jitter) for _ in range(counts[drone_id])] for drone_id in ids
        })
    best = math.inf
    for scales in scale_sets:
        tracks = plan_tracks(plan, timing, scales=scales)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                best = min(best, min_separation_cm(tracks[a], tracks[b]))
    return best
