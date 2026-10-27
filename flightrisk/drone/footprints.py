"""Camera footprints of the fleet planner and the exact area they sweep.

``flightrisk.drone.coverage_sim`` re-exports every public name here; import
from there. Offsets are ``dx = point_x - drone_x`` (right) and ``dy =
point_y - drone_y`` (ahead, the camera's heading) in centimetres.

* ``RectFootprint`` is an axis-aligned rectangle; ``RectFootprint.square`` is
  the plan's idealised square footprint centred on the drone.
* ``ForwardFootprint`` is the trapezoid ahead of a level forward-facing
  camera (``coverage.SweepModel``). ``ForwardFootprint.eroded`` describes the
  ground that stays visible whatever a placement error of up to a tolerance.

``covered_fraction`` measures the fraction of a rectangle that a set of
axis-aligned flights ever sees. The planner's coverage gate uses it.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

__all__ = [
    "MAX_SCAN_INTERVALS",
    "Footprint",
    "ForwardFootprint",
    "RectFootprint",
    "covered_fraction",
]

MAX_SCAN_INTERVALS = 30_000_000
"""Most covered intervals ``covered_fraction`` collects before refusing."""

_MAX_FOOTPRINT_CM = 1e7
_MAX_HALF_TAN = 1e3  # tan of an 89.9 degree half-angle is about 573


class Footprint(Protocol):
    """Where a cell is visible, in offsets ``dx = cell_x - drone_x``, ``dy = cell_y - drone_y``.

    The visible set must be symmetric in ``dx`` and its half-width must not
    shrink as ``dy`` grows within the visible range (true of both footprints
    here), which ``max_lateral_halfwidth`` relies on.
    """

    def lateral_halfwidth(self, dy: np.ndarray) -> np.ndarray:
        """Half-width of the visible span of ``dx`` at each ``dy``; negative where nothing is visible."""

    def along_interval(self, adx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """``(lo, hi)`` of the visible ``dy`` at each ``|dx|``; ``lo > hi`` where nothing is visible."""

    def max_lateral_halfwidth(self, dy_lo: np.ndarray, dy_hi: np.ndarray) -> np.ndarray:
        """Largest visible half-width over ``dy`` in ``[dy_lo, dy_hi]``; negative where nothing is visible."""

    def along_extent(self) -> tuple[float, float]:
        """Smallest and largest ``dy`` that can be visible (a superset is allowed)."""

    def lateral_reach(self) -> float:
        """Largest visible ``|dx|``."""

    def along_breakpoints(self) -> tuple[float, ...]:
        """Every ``dy`` at which ``lateral_halfwidth`` jumps or changes slope."""


def _footprint_number(name: str, value: Any, *, allow_inf: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        raise ValueError(f"footprint {name} must be a number, got {value!r}")
    if value < 0 or (value > _MAX_FOOTPRINT_CM and not (allow_inf and value == math.inf)):
        raise ValueError(f"footprint {name} must be within 0-{_MAX_FOOTPRINT_CM:g} cm, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class RectFootprint:
    """A rectangle ``2 * half_x_cm`` wide and ``2 * half_y_cm`` deep, centred ``ahead_cm`` ahead."""

    half_x_cm: float
    half_y_cm: float
    ahead_cm: float = 0.0

    def __post_init__(self) -> None:
        _footprint_number("half_x_cm", self.half_x_cm)
        _footprint_number("half_y_cm", self.half_y_cm)
        ahead = self.ahead_cm
        if isinstance(ahead, bool) or not isinstance(ahead, (int, float)) or not abs(ahead) <= _MAX_FOOTPRINT_CM:
            raise ValueError(f"footprint ahead_cm must be within +/-{_MAX_FOOTPRINT_CM:g} cm, got {ahead!r}")

    @classmethod
    def square(cls, side_cm: float) -> RectFootprint:
        """The plan's idealised footprint: a square of the sweep width centred on the drone."""
        return cls(side_cm / 2, side_cm / 2)

    def lateral_halfwidth(self, dy: np.ndarray) -> np.ndarray:
        inside = np.abs(dy - self.ahead_cm) <= self.half_y_cm
        return np.where(inside, self.half_x_cm, -1.0)

    def along_interval(self, adx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        inside = adx <= self.half_x_cm
        lo = np.where(inside, self.ahead_cm - self.half_y_cm, np.inf)
        hi = np.where(inside, self.ahead_cm + self.half_y_cm, -np.inf)
        return lo, hi

    def max_lateral_halfwidth(self, dy_lo: np.ndarray, dy_hi: np.ndarray) -> np.ndarray:
        lo, hi = self.along_extent()
        return np.where((dy_hi >= lo) & (dy_lo <= hi), self.half_x_cm, -1.0)

    def along_extent(self) -> tuple[float, float]:
        return self.ahead_cm - self.half_y_cm, self.ahead_cm + self.half_y_cm

    def lateral_reach(self) -> float:
        return self.half_x_cm

    def along_breakpoints(self) -> tuple[float, ...]:
        return self.along_extent()


@dataclass(frozen=True)
class ForwardFootprint:
    """Trapezoid ahead of a forward-facing camera.

    A ground point at offset ``(dx, dy)`` is visible when ``near_cm <= dy <=
    far_cm`` and ``|dx| <= min(dy * half_tan - inset_cm, max_half_width_cm)``.
    ``max_half_width_cm`` caps the lateral reach, for a calibrated sweep
    width. ``inset_cm`` is 0 for a camera; ``eroded`` sets it to describe the
    ground that stays visible whatever a placement error of up to ``t`` cm.
    Every field must be a finite, non-negative number (``max_half_width_cm``
    may be infinite) and ``half_tan`` must be positive. A footprint whose far
    edge is not beyond its near edge sees nothing, which is allowed.
    """

    near_cm: float
    far_cm: float
    half_tan: float
    max_half_width_cm: float = math.inf
    inset_cm: float = 0.0

    def __post_init__(self) -> None:
        _footprint_number("near_cm", self.near_cm)
        _footprint_number("far_cm", self.far_cm)
        _footprint_number("max_half_width_cm", self.max_half_width_cm, allow_inf=True)
        _footprint_number("inset_cm", self.inset_cm)
        ht = self.half_tan
        if isinstance(ht, bool) or not isinstance(ht, (int, float)) or not 0 < ht <= _MAX_HALF_TAN:
            raise ValueError(f"footprint half_tan must be a number within (0, {_MAX_HALF_TAN:g}], got {ht!r}")

    @classmethod
    def from_sweep_model(cls, model: Any) -> ForwardFootprint:
        """Footprint of a ``coverage.SweepModel``; its sweep width caps the lateral reach."""
        return cls(
            near_cm=model.near_m * 100,
            far_cm=model.far_m * 100,
            half_tan=model.half_tan * model.efficiency,
            max_half_width_cm=model.sweep_width_m * 100 / 2,
        )

    def eroded(self, tolerance_cm: float) -> ForwardFootprint:
        """The points that stay visible when the drone is anywhere within ``tolerance_cm`` in x and y.

        ``(dx, dy)`` is kept when the whole square ``(dx, dy) + [-t, t]^2``
        is visible; because the visible half-width grows with ``dy``, that is
        ``|dx| + t <= min((dy - t) * half_tan - inset_cm, max_half_width_cm)``
        with ``near_cm + t <= dy <= far_cm - t``.
        """
        t = _footprint_number("tolerance_cm", tolerance_cm)
        return ForwardFootprint(
            near_cm=self.near_cm + t,
            far_cm=max(self.far_cm - t, 0.0),
            half_tan=self.half_tan,
            max_half_width_cm=max(self.max_half_width_cm - t, 0.0),
            inset_cm=self.inset_cm + t * (self.half_tan + 1),
        )

    def lateral_halfwidth(self, dy: np.ndarray) -> np.ndarray:
        dy = np.asarray(dy, dtype=float)
        half = np.minimum(dy * self.half_tan - self.inset_cm, self.max_half_width_cm)
        inside = (dy >= self.near_cm) & (dy <= self.far_cm) & (half >= 0)
        return np.where(inside, half, -1.0)

    def along_interval(self, adx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        visible = adx <= self.lateral_reach()
        lo = np.where(visible, np.maximum(self.near_cm, (adx + self.inset_cm) / self.half_tan), np.inf)
        hi = np.where(visible, self.far_cm, -np.inf)
        return lo, hi

    def max_lateral_halfwidth(self, dy_lo: np.ndarray, dy_hi: np.ndarray) -> np.ndarray:
        # The half-width grows with dy, so the largest one is at the top of the range.
        top = np.minimum(dy_hi, self.far_cm)
        return np.where(np.asarray(dy_lo) <= self.far_cm, self.lateral_halfwidth(top), -1.0)

    def along_extent(self) -> tuple[float, float]:
        return self.near_cm, self.far_cm

    def lateral_reach(self) -> float:
        if self.far_cm < self.near_cm:
            return -1.0
        return min(self.far_cm * self.half_tan - self.inset_cm, self.max_half_width_cm)

    def along_breakpoints(self) -> tuple[float, ...]:
        points = {self.near_cm, self.far_cm, self.inset_cm / self.half_tan}
        if math.isfinite(self.max_half_width_cm):
            points.add((self.max_half_width_cm + self.inset_cm) / self.half_tan)
        return tuple(sorted(p for p in points if self.near_cm <= p <= self.far_cm))


def _per_track(footprint: Footprint | Sequence[Footprint], count: int) -> list[Footprint]:
    if isinstance(footprint, (list, tuple)):
        if len(footprint) != count:
            raise ValueError(f"expected {count} footprints, one per track, got {len(footprint)}")
        return list(footprint)
    return [footprint] * count


def _union_lengths(line: np.ndarray, start: np.ndarray, end: np.ndarray, lines: int, width: float) -> np.ndarray:
    """Per line, the total length of the union of its ``[start, end]`` intervals (all within ``[0, width]``)."""
    # Shift each line's intervals into its own slot of a single axis, so one
    # sort and one running maximum give every line's union at once.
    pitch = width + 1.0
    s = line * pitch + start
    e = line * pitch + end
    order = np.argsort(s, kind="stable")
    s, e, line = s[order], e[order], line[order]
    reach = np.maximum.accumulate(e)
    before = np.concatenate(([-np.inf], reach[:-1]))
    gained = np.maximum(0.0, e - np.maximum(s, before))
    return np.bincount(line, weights=gained, minlength=lines)


def covered_fraction(
    width_cm: float,
    depth_cm: float,
    paths: Iterable[Sequence[Sequence[float]]],
    footprint: Footprint | Sequence[Footprint],
    *,
    line_spacing_cm: float = 10.0,
) -> float:
    """Fraction of ``[0, width_cm] x [0, depth_cm]`` that drones flying these polylines ever see.

    ``paths`` are axis-aligned waypoint lists as for ``covered_cells``. The
    area seen is the union of every leg's swept footprint. Along a
    horizontal line that union is a set of ``x`` intervals whose length is
    computed exactly (so a gap between two passes is measured however narrow
    it is). As a function of ``y`` the length is piecewise linear: it can
    only jump or bend at the ``y`` of a polygon vertex (a leg end plus one of
    the footprint's ``along_breakpoints``) or where two slanted sides cross.
    The length is evaluated just above and just below every vertex ``y`` and
    on a grid of at most ``line_spacing_cm`` (and at most 1/100 of the
    depth), and integrated with the trapezoid rule on each piece. That is
    exact except at crossings of two slanted sides, where the error is at
    most of the order of the grid spacing squared. More than
    ``MAX_SCAN_INTERVALS`` intervals raise ``ValueError``.
    """
    width, depth = float(width_cm), float(depth_cm)
    paths = [[(float(x), float(y)) for x, y in path] for path in paths]
    footprints = _per_track(footprint, len(paths))
    legs = []  # (footprint, along_y, x_lo, x_hi, y_lo, y_hi)
    knots = []
    for path, fp in zip(paths, footprints):
        if not path:
            continue
        breaks = np.asarray(fp.along_breakpoints(), dtype=float)
        for (x0, y0), (x1, y1) in itertools.pairwise(path if len(path) > 1 else path * 2):
            if x0 != x1 and y0 != y1:
                raise ValueError(f"leg ({x0}, {y0}) -> ({x1}, {y1}) is not axis-aligned")
            legs.append((fp, x0 == x1, min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)))
            knots.append(breaks + min(y0, y1))
            knots.append(breaks + max(y0, y1))
    step = min(float(line_spacing_cm), depth / 100)
    grid = np.linspace(0.0, depth, max(1, math.ceil(depth / step)) + 1)
    knots = np.concatenate([grid, *knots]) if knots else grid
    knots = np.unique(knots[(knots >= 0) & (knots <= depth)])
    # Two evaluation lines per piece, just inside each end.
    gaps = np.diff(knots)
    eps = np.minimum(1e-4, gaps / 4)
    ys = np.column_stack((knots[:-1] + eps, knots[1:] - eps)).ravel()
    pieces: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    total = 0
    for fp, along_y, x_lo, x_hi, y_lo, y_hi in legs:
        lo, hi = fp.along_extent()
        a = np.searchsorted(ys, y_lo + lo, side="left")
        b = np.searchsorted(ys, y_hi + hi, side="right")
        if b <= a:
            continue
        total += b - a
        if total > MAX_SCAN_INTERVALS:
            raise ValueError(f"the coverage scan needs more than {MAX_SCAN_INTERVALS} intervals; the plan is too large")
        y = ys[a:b]
        half = fp.max_lateral_halfwidth(y - y_hi, y - y_lo) if along_y else fp.lateral_halfwidth(y - y_lo)
        keep = half >= 0
        start = np.clip(x_lo - half[keep], 0.0, width)
        end = np.clip(x_hi + half[keep], 0.0, width)
        pieces.append((np.arange(a, b)[keep], start, end))
    if pieces:
        line = np.concatenate([p[0] for p in pieces])
        lengths = _union_lengths(line, np.concatenate([p[1] for p in pieces]),
                                 np.concatenate([p[2] for p in pieces]), ys.size, width)
    else:
        lengths = np.zeros(ys.size)
    lengths = lengths.reshape(-1, 2)
    area = float(((lengths[:, 0] + lengths[:, 1]) / 2 * gaps).sum())
    return min(1.0, area / (width * depth))
