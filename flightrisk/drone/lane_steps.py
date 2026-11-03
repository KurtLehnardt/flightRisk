"""Pure lane-step arithmetic shared by the edge lane executor and the ground.

The obstacle sensing design (``.claude/build/obstacle/design.md``, sections
3.1 to 3.4 and 6) flies every lane in short sensed steps, cuts a lane where
the sensor sees an obstacle, and keeps the rest of the plan on its planned
grid with a deficit rule. The edge (``flightrisk.drone.lane_executor``) and
the ground coordinator must agree exactly on what that rule does, so both
call the functions here and nothing else:

* ``split_lane_moves`` splits a lane into Tello-sized moves (20 to 500 cm),
  and ``split_steps`` into sensed steps of at most ``step_cm``.
* ``plan_heading`` is the heading that faces a plan direction (``forward``
  0, ``right`` 90, ``back`` 180, ``left`` 270 degrees clockwise from the
  takeoff heading), and ``shortest_rotation`` the turn that reaches it.
* ``apply_cut_deficit`` is the deficit rule for one lane: a lane is
  shortened when flying it closes the gap a cut left on its axis, it is
  never lengthened, and a lane shortened below the 20 cm minimum move is
  skipped entirely.
* ``replay_lanes`` replays a whole plan from its lanes and its cuts, in the
  order they happened: what each lane is flown as (``lane_targets``), where
  the drone ends, and which parts of the plan nobody looked at
  (``uncovered_segments``).
* ``encode_cuts`` and ``decode_cuts`` are the telemetry wire form of the cuts:
  a flat list ``[lane, flown_cm, ...]`` of at most 16 numbers plus a comma
  separated string of their reasons.

The frame is the plan frame of ``flightrisk.drone.safety``: ``x_cm`` to the
right of the takeoff heading, ``y_cm`` ahead along it. Positions of segments
are relative to where the drone started its first lane (the origin of its
lane list); the ground adds the zone origin.

The cut rules (design section 3.3) as the replay applies them:

* A cut ``(lane, flown_cm, reason)`` stops lane ``lane`` after ``flown_cm``
  of the length it was being flown as. The rest of that lane, and every
  immediately following lane in the same direction (a run the coordinator
  produces when it splits a long displacement), is not flown. That unflown
  stretch is uncovered with the cut's reason.
* The drone is then behind its planned position on the lane's axis. A later
  lane on that axis that flies back toward the planned grid is shortened by
  the gap (``apply_cut_deficit``); its first part is uncovered with reason
  ``skipped``. A later lane that would widen the gap is flown at its planned
  length, so the drone never flies past a lane's planned end and never back
  into a cut.
* A cut with reason ``sensor_down`` ends the plan: every later lane, appended
  ones included, is uncovered with that reason and flown as nothing.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from flightrisk.drone.obstacle_sensor import (
    CUT_OBSTACLE,
    CUT_REASONS,
    CUT_SENSOR_DOWN,
    MAX_CUT_REASONS_CHARS,
    MAX_CUTS_LISTED,
    SEGMENT_SKIPPED,
)
from flightrisk.drone.safety import HORIZONTAL_DIRECTIONS, MAX_MOVE_CM, MIN_MOVE_CM

__all__ = [
    "PLAN_HEADINGS",
    "Cut",
    "LaneReplay",
    "UncoveredSegment",
    "apply_cut_deficit",
    "decode_cuts",
    "encode_cuts",
    "lane_targets",
    "plan_heading",
    "replay_lanes",
    "shortest_rotation",
    "split_lane_moves",
    "split_steps",
    "uncovered_segments",
]

#: Heading (degrees clockwise from the takeoff heading) that faces each plan direction.
PLAN_HEADINGS: dict[str, float] = {"forward": 0.0, "right": 90.0, "back": 180.0, "left": 270.0}

# Unit vector (x, y) of each plan direction in the plan frame.
_UNIT: dict[str, tuple[int, int]] = {"forward": (0, 1), "back": (0, -1), "right": (1, 0), "left": (-1, 0)}


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------


def split_lane_moves(
    distance_cm: float, min_move_cm: float = MIN_MOVE_CM, max_move_cm: float = MAX_MOVE_CM
) -> list[int]:
    """Split one lane into Tello-sized moves of 20-500 cm each, as evenly as possible.

    *min_move_cm* and *max_move_cm* replace the Tello range for a drone with
    another envelope profile (``SafetyEnvelope.limits``).
    """
    total = round(distance_cm)
    if total < min_move_cm:
        raise ValueError(f"lane distance {distance_cm!r} cm is below the {min_move_cm:g} cm minimum move")
    count = max(1, math.ceil(total / max_move_cm))
    base, extra = divmod(total, count)
    return [base + (1 if i < extra else 0) for i in range(count)]


def split_steps(distance_cm: float, step_cm: float, min_move_cm: float = MIN_MOVE_CM) -> list[int]:
    """Split one lane into sensed steps of at most *step_cm*, as evenly as possible.

    This is ``split_lane_moves`` with *step_cm* as the longest move, so every
    step is between *min_move_cm* and *step_cm* long. A step shorter than the
    minimum move cannot be flown, so when *step_cm* is under twice the
    minimum and the lane cannot be cut into steps of the allowed range (21 cm
    in 20 cm steps), it is cut into the fewest steps of at least the
    minimum instead, each of them at most one minimum move longer than
    *step_cm*. With the default 50 cm step that never happens.
    """
    total = round(distance_cm)
    steps = split_lane_moves(total, min_move_cm, step_cm)
    if min(steps) >= min_move_cm:
        return steps
    count = max(1, int(total // min_move_cm))
    return split_lane_moves(total, min_move_cm, math.ceil(total / count))


# ---------------------------------------------------------------------------
# Headings
# ---------------------------------------------------------------------------


def plan_heading(direction: str) -> float:
    """The heading that faces plan *direction* (``forward`` 0, ``right`` 90, ``back`` 180, ``left`` 270)."""
    try:
        return PLAN_HEADINGS[direction]
    except KeyError:
        raise ValueError(f"not a horizontal direction: {direction!r}") from None


def shortest_rotation(from_deg: float, to_deg: float) -> int:
    """Whole degrees to turn from heading *from_deg* to *to_deg*: positive clockwise, within -180..179.

    An exact reversal is a counter-clockwise half turn (-180), the same rule
    the manual-takeover return uses.
    """
    return round(((float(to_deg) - float(from_deg) + 180.0) % 360.0) - 180.0)


# ---------------------------------------------------------------------------
# The deficit rule
# ---------------------------------------------------------------------------


def _axis(direction: str) -> tuple[int, int]:
    """(axis, sign) of a plan direction: axis 0 is x, 1 is y; sign is +1 or -1 along it."""
    if direction not in _UNIT:
        raise ValueError(f"not a horizontal direction: {direction!r}")
    ux, uy = _UNIT[direction]
    return (0, ux) if ux else (1, uy)


def apply_cut_deficit(
    direction: str,
    planned_cm: int,
    deficit: tuple[int, int],
    min_move_cm: float = MIN_MOVE_CM,
) -> tuple[int, tuple[int, int]]:
    """The deficit rule for one lane: ``(flown_cm, deficit)`` after flying it.

    *deficit* is ``(x_cm, y_cm)``, the drone's actual minus its planned
    position on each plan axis, left by earlier cuts. A cut of ``r`` cm on a
    ``forward`` lane leaves ``(0, -r)``.

    When flying this lane closes the gap on its axis (a ``back`` lane after a
    ``forward`` cut) it is flown ``planned_cm`` minus the gap and the gap is
    used up, so the lane ends on its planned end. When flying it would widen
    the gap, or there is none, it is flown at ``planned_cm`` and the deficit
    is unchanged: a lane is never lengthened beyond its plan. A lane
    shortened below *min_move_cm* is skipped entirely (``flown_cm`` 0) and
    the deficit keeps what the skip did not use.
    """
    axis, sign = _axis(direction)
    planned = round(planned_cm)
    dx, dy = round(deficit[0]), round(deficit[1])
    gap = dx if axis == 0 else dy
    ahead = sign * gap  # how far the drone already is along this lane's direction
    if ahead <= 0:
        return planned, (dx, dy)
    flown = planned - ahead
    if flown < min_move_cm:
        flown = 0
    gap += sign * (flown - planned)
    return flown, ((gap, dy) if axis == 0 else (dx, gap))


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cut:
    """One lane cut: lane ``lane_index`` stopped after ``flown_cm``, for ``reason``.

    ``lane_index`` indexes the drone's current lane list (appended lanes
    included); ``flown_cm`` is measured along the lane as it was being flown.
    """

    lane_index: int
    flown_cm: int
    reason: str = CUT_OBSTACLE


@dataclass(frozen=True)
class UncoveredSegment:
    """A stretch of a planned lane that the drone did not look at.

    ``x_cm, y_cm`` is the end next to what was flown (for a cut, the point
    where the drone stopped) and ``end_x_cm, end_y_cm`` the far end, both in
    the plan frame relative to the start of the lane list. ``reason`` is a
    cut reason (``obstacle``, ``person``, ``sensor_stale``, ``sensor_down``,
    ``autopilot``) or ``skipped`` for a lane shortened by the deficit rule.
    ``cut`` is the index, in the cuts given to the replay, of the cut that
    produced or caused this segment.
    """

    lane_index: int
    direction: str
    x_cm: int
    y_cm: int
    end_x_cm: int
    end_y_cm: int
    length_cm: int
    reason: str
    cut: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane_index": self.lane_index,
            "direction": self.direction,
            "x_cm": self.x_cm,
            "y_cm": self.y_cm,
            "end_x_cm": self.end_x_cm,
            "end_y_cm": self.end_y_cm,
            "length_cm": self.length_cm,
            "reason": self.reason,
            "cut": self.cut,
        }


@dataclass(frozen=True)
class LaneReplay:
    """The result of ``replay_lanes``.

    ``targets[i]`` is what lane ``i`` is flown as (0 when it is not flown at
    all); a cut lane's target is the length it was being flown as when it
    was cut. ``position`` is where the drone ends and ``deficit`` its actual
    minus planned position, both after the last lane.
    """

    targets: tuple[int, ...]
    segments: tuple[UncoveredSegment, ...]
    position: tuple[int, int]
    deficit: tuple[int, int]


def _lane_pair(lane: Any) -> tuple[str, int]:
    """``(direction, distance_cm)`` of a lane given as a pair or as an ``assign_lanes`` mapping."""
    if isinstance(lane, Mapping):
        direction, distance = lane.get("direction"), lane.get("distance_cm")
    else:
        direction, distance = lane[0], lane[1]
    if direction not in HORIZONTAL_DIRECTIONS:
        raise ValueError(f"not a horizontal direction: {direction!r}")
    return str(direction), round(float(distance))


def _as_cut(cut: Any) -> Cut:
    if isinstance(cut, Cut):
        return cut
    lane_index, flown = int(cut[0]), round(float(cut[1]))
    reason = str(cut[2]) if len(cut) > 2 else CUT_OBSTACLE
    return Cut(lane_index, flown, reason)


def replay_lanes(
    lanes: Sequence[Any], cuts: Iterable[Any] = (), min_move_cm: float = MIN_MOVE_CM
) -> LaneReplay:
    """Replay a lane plan with its cuts (see the module docstring for the rules).

    *lanes* are ``(direction, distance_cm)`` pairs or ``assign_lanes`` lane
    mappings. *cuts* are ``Cut`` objects or ``(lane_index, flown_cm[,
    reason])`` tuples in the order they happened; a missing reason is
    ``obstacle``. Only the first cut of a lane counts, and a cut of a lane
    that was not flown (skipped, or after a ``sensor_down``) or outside the
    lane list is ignored, so a replay of a truncated or repeated list never
    raises. Every lane not cut is assumed flown to its target.
    """
    plan = [_lane_pair(lane) for lane in lanes]
    first_cut: dict[int, tuple[int, Cut]] = {}
    for index, raw in enumerate(cuts):
        cut = _as_cut(raw)
        if 0 <= cut.lane_index < len(plan) and cut.lane_index not in first_cut:
            first_cut[cut.lane_index] = (index, cut)

    x = y = 0
    deficit = (0, 0)
    # Index of the last cut that changed the deficit on each axis, so a
    # shortened lane can name the cut it is the shadow of.
    axis_cut: list[int] = [-1, -1]
    targets: list[int] = []
    segments: list[UncoveredSegment] = []
    # An active same-direction run after a cut: (direction, cursor, reason, cut index).
    run: tuple[str, tuple[int, int], str, int] | None = None
    # After a sensor_down cut: (cursor, cut index).
    ended: tuple[tuple[int, int], int] | None = None

    def segment(lane_index: int, direction: str, start: tuple[int, int], end: tuple[int, int],
                reason: str, cut_index: int) -> None:
        length = abs(end[0] - start[0]) + abs(end[1] - start[1])
        if length > 0:
            segments.append(UncoveredSegment(
                lane_index, direction, start[0], start[1], end[0], end[1], length, reason, cut_index,
            ))

    for lane_index, (direction, planned) in enumerate(plan):
        ux, uy = _UNIT[direction]
        axis, sign = _axis(direction)
        if ended is not None:
            cursor, cut_index = ended
            end = (cursor[0] + ux * planned, cursor[1] + uy * planned)
            segment(lane_index, direction, cursor, end, CUT_SENSOR_DOWN, cut_index)
            ended = (end, cut_index)
            targets.append(0)
            continue
        if run is not None:
            run_direction, cursor, reason, cut_index = run
            if direction == run_direction:
                end = (cursor[0] + ux * planned, cursor[1] + uy * planned)
                segment(lane_index, direction, cursor, end, reason, cut_index)
                run = (run_direction, end, reason, cut_index)
                gap = deficit[axis] - sign * planned
                deficit = (gap, deficit[1]) if axis == 0 else (deficit[0], gap)
                targets.append(0)
                continue
            run = None
        ahead = sign * deficit[axis]
        target, deficit = apply_cut_deficit(direction, planned, deficit, min_move_cm)
        if target < planned:
            # The planned lane starts `ahead` cm behind the drone; the part of
            # it the shortened lane does not fly is uncovered.
            skipped = planned - target
            start = (x + ux * (skipped - ahead), y + uy * (skipped - ahead))
            planned_start = (x - ux * ahead, y - uy * ahead)
            segment(lane_index, direction, start, planned_start, SEGMENT_SKIPPED, axis_cut[axis])
        targets.append(target)
        entry = first_cut.get(lane_index)
        if entry is None or target <= 0:
            x, y = x + ux * target, y + uy * target
            continue
        cut_index, cut = entry
        flown = max(0, min(cut.flown_cm, target))
        stop = (x + ux * flown, y + uy * flown)
        end = (x + ux * target, y + uy * target)
        segment(lane_index, direction, stop, end, cut.reason, cut_index)
        gap = deficit[axis] + sign * (flown - target)
        deficit = (gap, deficit[1]) if axis == 0 else (deficit[0], gap)
        axis_cut[axis] = cut_index
        x, y = stop
        if cut.reason == CUT_SENSOR_DOWN:
            ended = (end, cut_index)
        else:
            run = (direction, end, cut.reason, cut_index)
    return LaneReplay(tuple(targets), tuple(segments), (x, y), deficit)


def lane_targets(lanes: Sequence[Any], cuts: Iterable[Any] = (), min_move_cm: float = MIN_MOVE_CM) -> list[int]:
    """What each lane is flown as under the cut rules (0 for a lane that is not flown)."""
    return list(replay_lanes(lanes, cuts, min_move_cm).targets)


def uncovered_segments(lanes: Sequence[Any], cuts: Iterable[Any] = (), min_move_cm: float = MIN_MOVE_CM
                       ) -> list[UncoveredSegment]:
    """The parts of the plan nobody looked at, in lane order (see ``replay_lanes``).

    The edge and the ground both derive the segments with this function from
    the same lanes and cuts, so they always agree.
    """
    return list(replay_lanes(lanes, cuts, min_move_cm).segments)


# ---------------------------------------------------------------------------
# Telemetry wire form
# ---------------------------------------------------------------------------


def encode_cuts(cuts: Iterable[Any], limit: int = MAX_CUTS_LISTED) -> tuple[list[int], str]:
    """The last *limit* cuts as telemetry: ``([lane, flown_cm, ...], "reason,reason,...")``.

    The list holds at most ``2 * limit`` numbers (16 for the default eight
    cuts, the privacy screen's list limit) and the reasons string at most
    ``MAX_CUT_REASONS_CHARS`` characters.
    """
    listed = [_as_cut(cut) for cut in cuts][-limit:] if limit > 0 else []
    flat: list[int] = []
    for cut in listed:
        flat.extend((cut.lane_index, cut.flown_cm))
    reasons = ",".join(cut.reason for cut in listed)[:MAX_CUT_REASONS_CHARS]
    return flat, reasons


def decode_cuts(flat: Sequence[Any] | None, reasons: str | None = None) -> list[Cut]:
    """Read ``obstacle_cuts`` and ``obstacle_cut_reasons`` back into cuts.

    A missing or unknown reason reads as ``obstacle``; an odd trailing number
    or a pair that is not two whole numbers is dropped.
    """
    if not flat:
        return []
    names = [name.strip() for name in reasons.split(",")] if isinstance(reasons, str) and reasons else []
    cuts: list[Cut] = []
    for k in range(len(flat) // 2):
        lane, flown = flat[2 * k], flat[2 * k + 1]
        if isinstance(lane, bool) or isinstance(flown, bool):
            continue
        if not (isinstance(lane, (int, float)) and isinstance(flown, (int, float))):
            continue
        if not (math.isfinite(lane) and math.isfinite(flown)):
            continue
        reason = names[k] if k < len(names) and names[k] in CUT_REASONS else CUT_OBSTACLE
        cuts.append(Cut(int(lane), round(flown), reason))
    return cuts
