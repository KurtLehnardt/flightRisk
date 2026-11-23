"""Look again and sidestep at a gate that saw an obstacle, before the lane is cut.

``LaneExecutor`` (``flightrisk.drone.lane_executor``) inherits
``ObstacleAvoidMixin``. When the sense gate of a lane step
(``lane_flight._look``) sees an obstacle (cut reason ``obstacle``, the depth
cue), ``lane_flight._prepare_step`` holds the cut back and calls
``_avoid_blocked``, which tries, in this order:

1. **Look again.** ``obstacle_relook`` times (``FLIGHTRISK_OBSTACLE_RELOOK``,
   2 on the edge) the drone hovers ``obstacle_retry_pause_s`` and runs the
   whole gate again, with its clear votes and its retries, on frames captured
   after that gate started. These gates ask the sensor to wait for a fresh
   person detection even when the depth cue blocks (``person_wait``), so the
   person cue is known rather than ``unknown``. The step is flown only when
   every one of these gates is clear, so overriding a blocked view takes more
   clear views than clearing a step ever does. A person seen at any of them
   cuts the lane with reason ``person``, a sensor down for
   ``OBSTACLE_SENSOR_DOWN_S`` cuts it with ``sensor_down``, and a sensor that
   cannot look cuts it with ``obstacle`` (the obstacle was seen) without a
   sidestep. One lane gets at most ``OBSTACLE_RELOOKS_PER_LANE`` re-look
   rounds; after that a blocked gate on it is not looked at again.
2. **Sidestep.** Only while the lane is still blocked by the depth cue, only
   for a drone that turns nose first, does not measure its own heading and
   does not fly the field envelope (the Tello and the fake drone, never a
   MAVLink drone), and only when the blocked view's person cue is ``clear``
   (a fresh detection that saw no close person) and the sensor does not
   report its person cue down. A lane gate's own blocked view does not wait
   for a person detection, so when its cue is ``unknown`` the drone first
   takes one more look that does (``SOURCE_PERSON_CHECK``, within one sense
   budget): a person there cuts the lane for a person, a ``clear`` person
   cue lets the sidestep go ahead, anything else refuses it. At most ``obstacle_sidesteps`` attempts per
   lane (``FLIGHTRISK_OBSTACLE_SIDESTEPS``, 2 on the edge; a re-task that
   continues the plan keeps the count), each ``obstacle_sidestep_cm``
   sideways (``FLIGHTRISK_OBSTACLE_SIDESTEP_CM``; 0, the edge default,
   switches the sidestep off). The side
   the depth guard calls clearer is tried first, else the side with more
   room. A side is tried only when the point beside the drone and the end of
   the next lane step beside the lane both lie inside the geofence and inside
   the drone's own area (``_plan_area_locked``): the box around its planned
   and flown path, cut down to the ``zone`` rectangle of its plan when the
   plan carries one. The ground gives every drone a region that holds its
   whole path and keeps regions apart (``fleet_patterns``, "Separation
   rule"), so a sidestep stays in the drone's own region. A re-task or a
   re-flight can send another drone's path within ``gap_cm`` of this region,
   and a sidestep then takes up to ``obstacle_sidestep_cm`` of that margin;
   the ground's re-task separation check adds the sidestep to the gap it
   keeps for that reason (``coordinator.SIDESTEP_MARGIN_CM``). The altitude
   never changes, so the slot and the ceiling are untouched. An attempt:

   a. records the point on the lane where the drone stopped as the point the
      plan continues from (``_resume_target``, the same field a
      ``resume_search`` uses), so a hold, a link-loss hold or a manual
      takeover during the sidestep brings the drone back there before the
      plan goes on (``lane_manual._finish_return`` and the hold point);
   b. turns to face the side and flies the sideways move after a clear gate
      (anything that is not clear refuses that side with nothing moved);
   c. turns back to the lane heading and flies the lane's own next steps on
      the offset line, each after a clear gate and only while it stays
      inside the geofence and the area, for up to
      ``OBSTACLE_SIDESTEP_PASS_CM``; each step flown advances the plan and
      the point on the lane, and the stretch flown this way is reported to
      the ground as an offset segment (``obstacle_avoid`` ``offsets``), which
      it offers for a re-flight like a cut segment;
   d. flies back to the lane, sensed like the way back of a
      ``resume_search`` (``lane_flight._fly_return_sensed``).

   Every gate of b, c and d asks the sensor to wait for a person detection
   and clears only on a fresh ``clear`` person cue: an ``unknown`` cue is not
   a view for a sidestep (``lane_flight._look``'s ``need_person``). When at
   least one step was flown beside the obstacle, the plan goes on from the
   lane with its next gate. Otherwise the other side is tried, if an attempt
   is left (the sides are worked out again from where the way back ended),
   and then the lane is cut.
3. **Cut** as before, where the drone stopped on the lane.

**A person or a sensor down seen during a sidestep.** When any gate of the
sideways move, the steps beside the lane or the way back sees a person, or
the sensor has been down for ``OBSTACLE_SENSOR_DOWN_S``, the lane is cut at
once with that reason at the plan's progress, which is the point on the lane
level with the drone, whatever was already flown beside it. The drone then
flies back to that point, sensed, and the plan goes on with the next lane. A
person cut therefore still needs the operator's ``confirm_reflight`` before
another drone re-flies it, and the reason is never lost to a hold, a re-task
that continues the plan or a way back that does not clear. The offset that
sidestep flew is marked with the reason too (``offsets_seen``), because its
re-flight would start at its far end, where the person was: the ground never
offers it without the operator's confirmation. That also covers a way back
that sees a person after the pass reached the end of the lane, when there is
no lane left to cut (logged as ``not_cut``); the drone then holds, and its
``held_reason`` tells the ground a person was seen.

**Time budget.** The re-look and the sidestep of one blocked gate may take
``obstacle_avoid_budget_s`` seconds of wall-clock time
(``FLIGHTRISK_OBSTACLE_AVOID_BUDGET_S``, 45 s by default, 10 to 300). Once it
is spent no further re-look round, sidestep attempt or step beside the lane
is started. A drone beside the lane has its lane cut at the plan's progress
first (``_cut_for_budget``) and then flies back to it, so a way back that
does not clear leaves it held with the lane already cut rather than in
front of the same obstacle again; on the lane, the lane is cut as it would
be without them. Every ``edge_obstacle_relook`` and ``edge_obstacle_sidestep``
log carries ``elapsed_s`` since the blocked gate. The budget belongs to one
blocked gate: after a hold, a link-loss hold or a manual takeover the plan
meets the obstacle at a new gate, with a new budget. What bounds the total is
the per-lane counts, ``obstacle_sidesteps`` attempts and
``OBSTACLE_RELOOKS_PER_LANE`` re-look rounds, which a resumed plan keeps.

**Never land for an obstacle.** Nothing here calls a landing. Every sideways
and offset move is checked against the geofence (shrunk by the lane steps the
drone refused this flight) before it is sent, so the geofence landing of a
lane step can never be reached from a sidestep. A move or turn the drone
itself refuses still lands it through the executor's error path
(``executor_error``): it is never retried here, and a transient refusal is
counted in ``move_refusals`` (``flightrisk.drone.flight_guards``). A way back to the lane that a
gate does not clear cuts nothing (unless it saw a person or a sensor down,
above): the drone hovers under manual control with the point on the lane as
its hold point (``lane_manual._end_blocked_return``), its telemetry reports
``obstacle_avoid`` phase ``held``, the ground announces it to the operator
(fleet event ``sidestep_held``), and the operator decides. If nobody takes
control or presses Resume search, the ground's orphan countdown
(``FLIGHTRISK_MANUAL_ORPHAN_LAND_S``, 120 s by default,
``docs/manual-takeover.md``) lands the drone where it hovers. A landing
(``LAND ALL``, a ground ``land``, the link-loss, battery and ceiling
landings) stops a re-look or a sidestep before its next move, exactly as it
stops a lane. A controller error on one of the extra turns or moves still
lands the drone through the executor's fail-safe, as on any lane move.

Every step is logged (``edge_obstacle_relook`` and ``edge_obstacle_sidestep``
with a ``phase``; the gates as ``edge_obstacle_gate`` with ``source``
``relook``, ``sidestep`` or ``sidestep_return``) and counted in the
``obstacle_avoid`` telemetry field.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from flightrisk.drone.lane_flight import _AGAIN, _CUT, _GO, _STOP
from flightrisk.drone.lane_steps import plan_heading, replay_lanes, shortest_rotation
from flightrisk.drone.obstacle_sensor import (
    AVOID_COUNT_KEYS,
    AVOID_HELD_REASON_KEY,
    AVOID_OFFSETS_KEY,
    AVOID_OFFSETS_SEEN_KEY,
    AVOID_PHASE_HELD,
    AVOID_PHASE_KEY,
    AVOID_PHASE_RELOOK,
    AVOID_PHASE_RETURN,
    AVOID_PHASE_SIDESTEP,
    AVOID_RELOOK_CLEARS_KEY,
    AVOID_RELOOKS_KEY,
    AVOID_SIDESTEP_CM_KEY,
    AVOID_SIDESTEP_PASSES_KEY,
    AVOID_SIDESTEPS_KEY,
    CUT_OBSTACLE,
    CUT_PERSON,
    CUT_SENSOR_DOWN,
    EVENT_OBSTACLE_RELOOK,
    EVENT_OBSTACLE_SIDESTEP,
    MAX_OFFSETS_LISTED,
    OBSTACLE_AVOID_BUDGET_RANGE_S,
    OBSTACLE_AVOID_BUDGET_S,
    OBSTACLE_CLEAR_VOTES_RANGE,
    OBSTACLE_RELOOK_RANGE,
    OBSTACLE_RELOOKS_PER_LANE,
    OBSTACLE_SIDESTEP_PASS_CM,
    OBSTACLE_SIDESTEPS_RANGE,
    PERSON_CUE_CLEAR,
    PERSON_CUE_DOWN_KEY,
    PERSON_CUE_UNKNOWN,
    SenseResult,
)
from flightrisk.drone.safety import FIELD_LIMITS, ROOM_LIMITS, RelativePose

__all__ = [
    "SOURCE_PERSON_CHECK",
    "SOURCE_RELOOK",
    "SOURCE_SIDESTEP",
    "SOURCE_SIDESTEP_RETURN",
    "ObstacleAvoidMixin",
    "PlanArea",
]

#: ``source`` of the gate logs of a re-look, a sidestep and its way back to the lane.
SOURCE_RELOOK = "relook"
SOURCE_SIDESTEP = "sidestep"
SOURCE_SIDESTEP_RETURN = "sidestep_return"
#: ``source`` of the one look that waits for a person detection before a sidestep
#: is decided on a blocked view whose person cue was ``unknown``.
SOURCE_PERSON_CHECK = "person_check"

# Outcomes of one sidestep attempt (``_try_sidestep``).
_REFUSED = "refused"  # the sideways gate did not clear; nothing moved sideways
_RETURNED = "returned"  # moved sideways, nothing flown beside the obstacle, back on the lane
_PASSED = "passed"  # flew at least one step beside the obstacle, back on the lane
_REPLANNED = "replanned"  # a re-task replaced the step; back on the lane
_STOPPED = "stopped"  # a landing, a hold or a link-loss hold stopped the sequence
_HELD = "held"  # the way back was not cleared: the drone hovers under manual control
# ``_sidestep``'s verdict when the lane was already cut there (a person or a
# sensor down seen during the sidestep) and the drone is back on the lane.
_CUT_MADE = "cut_made"
# Gate reasons that end the lane wherever they are seen during a sidestep.
_STOPPING_REASONS = (CUT_PERSON, CUT_SENSOR_DOWN)

# Unit vector (x, y) of each plan direction in the plan frame (``flightrisk.drone.safety``).
_UNIT: dict[str, tuple[int, int]] = {"forward": (0, 1), "back": (0, -1), "right": (1, 0), "left": (-1, 0)}
_DIRECTION_AT: dict[int, str] = {0: "forward", 90: "right", 180: "back", 270: "left"}
# Two points closer than this are the same point.
_SAME_POINT_CM = 1e-6
# ``_zone_area_locked`` when the plan carries no zone: the box alone limits a sidestep.
_NO_ZONE = object()


def _direction_at(heading_deg: float) -> str:
    """The plan direction a heading of a multiple of 90 degrees faces."""
    return _DIRECTION_AT[round(heading_deg) % 360]


def _whole(name: str, value: Any, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{name} must be a whole number from {lo} to {hi}, got {value!r}")
    return value


@dataclass(frozen=True)
class PlanArea:
    """The area a sidestep must stay in, in the dead-reckoned pose frame."""

    x_min: float
    x_max: float
    y_min: float
    y_max: float

    def contains(self, x_cm: float, y_cm: float) -> bool:
        return (
            self.x_min - _SAME_POINT_CM <= x_cm <= self.x_max + _SAME_POINT_CM
            and self.y_min - _SAME_POINT_CM <= y_cm <= self.y_max + _SAME_POINT_CM
        )

    def room(self, x_cm: float, y_cm: float, direction: str) -> float:
        """How far the area reaches from ``(x_cm, y_cm)`` in plan *direction* (0 outside it)."""
        ux, uy = _UNIT[direction]
        if ux:
            return max(0.0, (self.x_max - x_cm) if ux > 0 else (x_cm - self.x_min))
        return max(0.0, (self.y_max - y_cm) if uy > 0 else (y_cm - self.y_min))

    def intersect(self, other: PlanArea) -> PlanArea | None:
        """The part of this area inside *other*, or None when they do not overlap."""
        x_min, x_max = max(self.x_min, other.x_min), min(self.x_max, other.x_max)
        y_min, y_max = max(self.y_min, other.y_min), min(self.y_max, other.y_max)
        if x_min > x_max + _SAME_POINT_CM or y_min > y_max + _SAME_POINT_CM:
            return None
        return PlanArea(x_min, x_max, y_min, y_max)


@dataclass(frozen=True)
class _Pass:
    """What ``_pass_beside`` flew beside the lane.

    *outcome* is ``_STOPPED`` or ``"done"``; *why* is the gate result that
    ended the pass, if one did; *passed_cm* is how far it flew beside the
    lane, starting *from_cm* along the lane; *line* is the point on the lane
    level with the drone, which follows every step flown; *replanned* is True
    when a re-task replaced the step; *budget* is True when the time budget
    ended it.
    """

    outcome: str
    why: tuple[str, SenseResult] | None
    passed_cm: int
    from_cm: int
    line: RelativePose
    replanned: bool
    budget: bool


def _with_person_cue(result: SenseResult, fresh: SenseResult) -> SenseResult:
    """*result* with the person cue of the fresher view *fresh*."""
    return SenseResult(result.state, cue=result.cue, reason=result.reason, latency_ms=result.latency_ms,
                       person_cue=fresh.person_cue, depth_score=result.depth_score, detail=dict(result.detail))


class ObstacleAvoidMixin:
    """Re-look and sidestep at a blocked lane gate for ``LaneExecutor`` (see the module docstring)."""

    def _init_avoid(
        self, clear_votes: Any, relook: Any, sidestep_cm: Any, sidesteps: Any,
        budget_s: Any = OBSTACLE_AVOID_BUDGET_S,
    ) -> None:
        """Validate and store the avoidance settings. Raises ``ValueError``.

        *sidestep_cm* is 0 (no sidestep) or a move the envelope allows.
        *budget_s* is the time budget of one blocked gate, in seconds.
        """
        self.obstacle_clear_votes = _whole("obstacle_clear_votes", clear_votes, *OBSTACLE_CLEAR_VOTES_RANGE)
        self.obstacle_relook = _whole("obstacle_relook", relook, *OBSTACLE_RELOOK_RANGE)
        self.obstacle_sidesteps = _whole("obstacle_sidesteps", sidesteps, *OBSTACLE_SIDESTEPS_RANGE)
        limits = getattr(self.envelope, "limits", ROOM_LIMITS)
        if isinstance(sidestep_cm, bool) or not isinstance(sidestep_cm, int) or not (
            sidestep_cm == 0 or limits.min_move_cm <= sidestep_cm <= limits.max_move_cm
        ):
            raise ValueError(
                f"obstacle_sidestep_cm must be 0 or a whole number from {limits.min_move_cm:g} to "
                f"{limits.max_move_cm:g}, got {sidestep_cm!r}"
            )
        self.obstacle_sidestep_cm = sidestep_cm
        lo, hi = OBSTACLE_AVOID_BUDGET_RANGE_S
        if (
            isinstance(budget_s, bool) or not isinstance(budget_s, (int, float))
            or not math.isfinite(budget_s) or not lo <= budget_s <= hi
        ):
            raise ValueError(f"obstacle_avoid_budget_s must be a number of seconds from {lo:g} to {hi:g}, "
                             f"got {budget_s!r}")
        self.obstacle_avoid_budget_s = float(budget_s)
        self._avoid_counts: dict[str, int] = dict.fromkeys(AVOID_COUNT_KEYS, 0)
        self._avoid_phase: str | None = None
        # The lane the sidestep attempts and the re-look rounds are counted
        # for, by lane index: a re-task that continues the plan keeps the
        # lanes flown so far, and their indexes, so it keeps the counts too.
        self._sidestep_lane: int | None = None
        self._sidestep_used = 0
        self._relook_lane: int | None = None
        self._relook_used = 0
        # Offset segments of the plan: [lane, from_cm, length_cm, seen], one per
        # sidestep that passed; *seen* is ``person`` or ``sensor_down`` when a
        # gate of that sidestep stopped for one, else "".
        self._avoid_offsets: list[list[Any]] = []
        # The manual control a sidestep's blocked way back put the drone under
        # (``AVOID_PHASE_HELD`` while it is still the one in force), and why
        # the way back stopped (``AVOID_HELD_REASON_KEY``).
        self._avoid_held_manual: Any = None
        self._avoid_held_reason: str | None = None
        # Edge clock time when the blocked gate being avoided was seen, for ``elapsed_s``.
        self._avoid_started: float | None = None
        # The gate result of the last way back that cut the lane (``_back_to_lane``).
        self._last_return_block: tuple[str, SenseResult] | None = None

    # ------------------------------------------------------------------
    # Settings, telemetry and the plan
    # ------------------------------------------------------------------

    def _avoid_configured(self) -> bool:
        """True when the re-look or the sidestep is switched on (whatever the drone)."""
        return self.obstacle_relook > 0 or (self.obstacle_sidestep_cm > 0 and self.obstacle_sidesteps > 0)

    def _sidestep_enabled(self) -> bool:
        """True when this executor may sidestep: switched on, a sensor, nose first, no autopilot.

        A drone that measures its own heading, or one flying the field
        envelope, is an autopilot with its own position hold and the field
        step (MAVLink); its sidestep has not been designed or tested, so it
        only looks again.
        """
        limits = getattr(self.envelope, "limits", ROOM_LIMITS)
        return (
            self.obstacle_sidestep_cm > 0
            and self.obstacle_sidesteps > 0
            and self._obstacle_sensor is not None
            and self._turns_to_face()
            and not callable(getattr(self.drone, "measured_heading_deg", None))
            and getattr(limits, "name", None) != FIELD_LIMITS.name
        )

    def _avoid_status(self) -> dict[str, Any] | None:
        """The ``obstacle_avoid`` telemetry object, or None while there is nothing to report.

        Nothing is reported while the re-look and the sidestep are off. An
        edge that may sidestep always reports it, with ``sidestep_cm``, so the
        ground knows to keep its sidestep margin from this drone's path; one
        that only looks again reports it once the plan has something to
        report.
        """
        if not self._avoid_configured():
            return None
        sidestep_cm = self.obstacle_sidestep_cm if self._sidestep_enabled() else 0
        with self._lock:
            counts = dict(self._avoid_counts)
            phase = self._avoid_phase
            held = self._avoid_held_manual
            if phase is None and held is not None and self._manual is held and self._is_flying():
                phase = AVOID_PHASE_HELD
            listed = self._avoid_offsets[-MAX_OFFSETS_LISTED:]
            offsets = [int(number) for segment in listed for number in segment[:3]]
            seen = [str(segment[3]) for segment in listed]
            held_reason = self._avoid_held_reason
        if not sidestep_cm and phase is None and not offsets and not any(counts.values()):
            return None
        status: dict[str, Any] = counts
        status[AVOID_PHASE_KEY] = phase
        status[AVOID_SIDESTEP_CM_KEY] = sidestep_cm
        if offsets:
            status[AVOID_OFFSETS_KEY] = offsets
            if any(seen):
                status[AVOID_OFFSETS_SEEN_KEY] = seen
        if phase == AVOID_PHASE_HELD and held_reason:
            status[AVOID_HELD_REASON_KEY] = held_reason
        return status

    def _reset_avoid_plan_locked(self) -> None:
        """Forget the counts and the offsets of the plan being replaced. Caller holds ``_lock``."""
        self._avoid_counts = dict.fromkeys(AVOID_COUNT_KEYS, 0)
        self._sidestep_lane = None
        self._sidestep_used = 0
        self._relook_lane = None
        self._relook_used = 0
        self._avoid_offsets = []

    def _avoid_note(self, phase: str | None, count: str | None = None) -> None:
        with self._lock:
            self._avoid_phase = phase
            if count is not None:
                self._avoid_counts[count] = self._avoid_counts.get(count, 0) + 1

    def _avoid_wanted_locked(self, reason: str, result: SenseResult) -> bool:
        """True when a lane gate's cut for *reason* is held back to look again or sidestep. Caller holds ``_lock``.

        Only an obstacle (the depth cue): a person is never looked past or
        stepped around, and a sensor that cannot look is handled by the
        gate's own retries.
        """
        return (
            reason == CUT_OBSTACLE
            and result.blocked
            and (self.obstacle_relook > 0 or self._sidestep_enabled())
        )

    def _avoid_log(self, event: str, step: tuple[int, int, str, int], phase: str, **fields: Any) -> None:
        pose = self.envelope.pose_snapshot()
        started = self._avoid_started
        if started is not None:
            fields.setdefault("elapsed_s", round(self._clock() - started, 2))
        log = self._log.warning if phase in ("held", "cut", "not_cut", "budget") else self._log.info
        log(
            event,
            drone_id=self.drone_id,
            lane_index=step[0],
            chunk_index=step[1],
            direction=step[2],
            phase=phase,
            x_cm=round(pose.x_cm, 1),
            y_cm=round(pose.y_cm, 1),
            heading_deg=round(pose.heading_deg, 1),
            **fields,
        )

    def _budget_spent(self, deadline: float, event: str, step: tuple[int, int, str, int]) -> bool:
        """True (and logged) once the blocked gate's time budget is spent."""
        if self._clock() < deadline:
            return False
        self._avoid_log(event, step, "budget", budget_s=self.obstacle_avoid_budget_s)
        return True

    # ------------------------------------------------------------------
    # The blocked gate
    # ------------------------------------------------------------------

    def _avoid_blocked(
        self,
        step: tuple[int, int, str, int],
        gen: int,
        current: Callable[[], bool],
        cut: Callable[[str, SenseResult], None],
        first: SenseResult,
    ) -> str:
        """Look again, sidestep, or cut: ``_GO``, ``_AGAIN``, ``_STOP`` or ``_CUT``.

        *first* is the blocked result whose cut ``_prepare_step`` held back.
        ``_GO`` flies *step* now (every re-look was clear); ``_AGAIN`` reads
        the plan again (a sidestep got past, a re-task, or a hold that the
        top of the plan loop stops for); ``_STOP`` ends the sequence; ``_CUT``
        is returned once the lane is cut: by *cut*, called under ``_lock``
        for a step that was still *current*, or during a sidestep that saw a
        person or a sensor down (``_cut_for_seen``).
        """
        started = self._clock()
        deadline = started + self.obstacle_avoid_budget_s
        self._avoid_started = started
        try:
            verdict, why, may_sidestep = self._relook(step, current, first, deadline)
            if verdict != _CUT:
                return verdict
            if may_sidestep and self._sidestep_enabled():
                verdict, why = self._sidestep(step, gen, current, why, deadline)
                if verdict == _CUT_MADE:
                    return _CUT
                if verdict != _CUT:
                    return verdict
            with self._lock:
                if not current():
                    return _AGAIN
                cut(*why)
            return _CUT
        finally:
            self._avoid_note(None)
            self._avoid_started = None

    def _relook(
        self, step: tuple[int, int, str, int], current: Callable[[], bool], first: SenseResult, deadline: float,
    ) -> tuple[str, tuple[str, SenseResult], bool]:
        """The re-look: ``(verdict, (reason, result), may_sidestep)``.

        ``_GO`` when every re-look gate cleared; ``_CUT`` with the reason the
        lane would be cut for and whether a sidestep may still be tried;
        ``_AGAIN`` or ``_STOP`` as the gate returns them (a re-task, a hold, a
        landing). Without re-looks, or once the lane has used its
        ``OBSTACLE_RELOOKS_PER_LANE`` rounds, it is ``_CUT`` with *first* at
        once and a sidestep may follow. A spent time budget cuts without a
        sidestep.
        """
        why: tuple[str, SenseResult] = (CUT_OBSTACLE, first)
        rounds = self.obstacle_relook
        if rounds <= 0:
            return _CUT, why, True
        lane_index = step[0]
        with self._lock:
            if self._relook_lane != lane_index:
                self._relook_lane, self._relook_used = lane_index, 0
            allowed = self._relook_used < OBSTACLE_RELOOKS_PER_LANE
            if allowed:
                self._relook_used += 1
        if not allowed:
            self._avoid_log(EVENT_OBSTACLE_RELOOK, step, "refused", reason="lane_limit",
                            relooks_per_lane=OBSTACLE_RELOOKS_PER_LANE)
            return _CUT, why, True
        self._avoid_note(AVOID_PHASE_RELOOK, AVOID_RELOOKS_KEY)
        self._avoid_log(EVENT_OBSTACLE_RELOOK, step, "start", rounds=rounds, depth_score=first.depth_score)
        for attempt in range(1, rounds + 1):
            if self._budget_spent(deadline, EVENT_OBSTACLE_RELOOK, step):
                return _CUT, why, False
            # Hover, then stop here for a landing, a hold, manual control or a
            # link-loss hold: nothing was cut, so the step is gated afresh later.
            if self._abort.wait(self.obstacle_retry_pause_s) or not self._pause_point():
                self._avoid_log(EVENT_OBSTACLE_RELOOK, step, "stopped", attempt=attempt, rounds=rounds)
                return _STOP, why, False
            seen: list[tuple[str, SenseResult]] = []
            verdict = self._look(step, current, lambda reason, result, seen=seen: seen.append((reason, result)),
                                 source=SOURCE_RELOOK, person_wait=True)
            if verdict == _GO:
                continue
            if verdict != _CUT:
                self._avoid_log(EVENT_OBSTACLE_RELOOK, step, "stopped", attempt=attempt, rounds=rounds)
                return verdict, why, False
            reason, result = seen[-1]
            self._avoid_log(EVENT_OBSTACLE_RELOOK, step, "blocked", attempt=attempt, rounds=rounds, reason=reason,
                            depth_score=result.depth_score, person_cue=result.person_cue)
            if reason in _STOPPING_REASONS:
                return _CUT, (reason, result), False
            if reason == CUT_OBSTACLE:
                return _CUT, (reason, result), True
            # The sensor could not look: the obstacle it saw still cuts the lane, and nothing sidesteps blind.
            return _CUT, why, False
        self._avoid_note(None, AVOID_RELOOK_CLEARS_KEY)
        self._avoid_log(EVENT_OBSTACLE_RELOOK, step, "cleared", rounds=rounds)
        return _GO, why, False

    # ------------------------------------------------------------------
    # The sidestep
    # ------------------------------------------------------------------

    def _zone_area_locked(self) -> Any:
        """The plan's ``zone`` rectangle in the pose frame, ``_NO_ZONE`` without one, None when unreadable.

        The zone is on the shared grid and the drone's lanes, pose and
        telemetry are relative to its takeoff point, which the zone carries
        as ``takeoff_x_cm``/``takeoff_y_cm`` (else its near-left corner, as
        ``coordinator.ZoneAssignment.origin`` says). Caller holds ``_lock``.
        """
        zone = getattr(self, "_zone", None)
        if zone is None:
            return _NO_ZONE
        if not isinstance(zone, dict):
            return None
        try:
            x, y, w, h = (float(zone[key]) for key in ("x_cm", "y_cm", "w_cm", "h_cm"))
            if zone.get("takeoff_x_cm") is not None and zone.get("takeoff_y_cm") is not None:
                ox, oy = float(zone["takeoff_x_cm"]), float(zone["takeoff_y_cm"])
            else:
                ox, oy = x, y
        except (KeyError, TypeError, ValueError):
            return None
        if not all(math.isfinite(v) for v in (x, y, w, h, ox, oy)) or w < 0 or h < 0:
            return None
        return PlanArea(x - ox, x + w - ox, y - oy, y + h - oy)

    def _plan_area_locked(self, step: tuple[int, int, str, int]) -> PlanArea | None:
        """The area a sidestep of the current plan must stay in, in the pose frame, or None. Caller holds ``_lock``.

        The replay of the plan with its cuts (``lane_steps.replay_lanes``)
        gives where each lane is planned and where it is flown, relative to
        the start of the lane list; the drone stands on *step*'s lane, as far
        along it as the plan's progress says, which places that start in the
        pose frame. The box around those points is cut down to the plan's
        ``zone`` (``_zone_area_locked``) when it carries one. None when the
        plan cannot be replayed, the zone cannot be read or the two do not
        overlap, which refuses every sidestep.
        """
        lane_index, _, direction, _ = step
        try:
            limits = getattr(self.envelope, "limits", ROOM_LIMITS)
            replay = replay_lanes(self._lanes, self._cuts, limits.min_move_cm)
            if not 0 <= lane_index < len(replay.path) - 1:
                return None
            flown = self._lane_flown_locked(lane_index)
            start = replay.path[lane_index]
            ux, uy = _UNIT[direction]
            pose = self.envelope.pose_snapshot()
            origin_x = pose.x_cm - (start[0] + ux * flown)
            origin_y = pose.y_cm - (start[1] + uy * flown)
            points = list(replay.path) + list(replay.planned)
        except (ValueError, KeyError, IndexError, TypeError):
            return None
        xs = [p[0] + origin_x for p in points]
        ys = [p[1] + origin_y for p in points]
        box = PlanArea(min(xs), max(xs), min(ys), max(ys))
        zone = self._zone_area_locked()
        if zone is _NO_ZONE:
            return box
        if zone is None:
            return None
        return box.intersect(zone)

    def _lane_flown_locked(self, lane_index: int) -> int:
        """How far the plan's progress is along lane *lane_index* (its moves before ``_step``). Caller holds ``_lock``."""
        flown = 0
        k = self._step - 1
        while k >= 0 and self._moves[k][0] == lane_index:
            flown += self._moves[k][3]
            k -= 1
        return flown

    def _lane_step_now_locked(self, step: tuple[int, int, str, int], gen: int) -> tuple[int, int, str, int] | None:
        """The plan's next move when it is still on *step*'s lane of the plan *gen* continues, else None.

        Caller holds ``_lock``. That move is where the plan's progress
        stands: a cut of it records the point on the lane level with the
        drone, however far a sidestep got beside it.
        """
        if not (self._plan_gen == gen or gen in self._linked_gens) or self._step >= len(self._moves):
            return None
        now = self._moves[self._step]
        return now if now[0] == step[0] else None

    def _sidestep_sides(
        self, step: tuple[int, int, str, int], result: SenseResult, area: PlanArea | None,
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """The sides to try, best first, and the refused ones with why (``geofence`` or ``plan_area``).

        A side is a plan direction across the lane. It is allowed when the
        point the sideways move ends on and the end of the step beside the
        lane both lie inside the geofence and inside *area*.
        """
        lane_direction, distance = step[2], step[3]
        heading = plan_heading(lane_direction)
        left, right = _direction_at(heading + 270.0), _direction_at(heading + 90.0)
        pose = self.envelope.pose_snapshot()
        detail = result.detail if isinstance(result.detail, dict) else {}
        action = detail.get("depth_action")
        if action == "go_left":
            order = [left, right]
        elif action == "go_right":
            order = [right, left]
        else:
            room = {side: (area.room(pose.x_cm, pose.y_cm, side) if area is not None else 0.0) for side in (right, left)}
            order = [right, left] if room[right] >= room[left] else [left, right]
        fx, fy = _UNIT[lane_direction]
        size = self.obstacle_sidestep_cm
        allowed: list[str] = []
        refused: list[tuple[str, str]] = []
        for side in order:
            ux, uy = _UNIT[side]
            beside = (pose.x_cm + ux * size, pose.y_cm + uy * size)
            ahead = (beside[0] + fx * distance, beside[1] + fy * distance)
            if not (self.envelope.inside_fence(*beside) and self.envelope.inside_fence(*ahead)):
                refused.append((side, "geofence"))
            elif area is None or not (area.contains(*beside) and area.contains(*ahead)):
                refused.append((side, "plan_area"))
            else:
                allowed.append(side)
        return allowed, refused

    def _sidestep_refusal(self, result: SenseResult) -> str | None:
        """Why no sidestep may be tried after the blocked view *result*, or None.

        The view must have a fresh person detection that saw no close person
        (``person_cue`` ``clear``; ``unknown`` refuses), and the sensor must
        not report its person cue down.
        """
        if result.person_cue != PERSON_CUE_CLEAR:
            return f"person_cue_{result.person_cue}"
        status = self._person_cue_status()
        if status is not None and status.get(PERSON_CUE_DOWN_KEY):
            return "person_cue_down"
        return None

    def _sidestep(
        self,
        step: tuple[int, int, str, int],
        gen: int,
        current: Callable[[], bool],
        why: tuple[str, SenseResult],
        deadline: float,
    ) -> tuple[str, tuple[str, SenseResult]]:
        """Try the allowed sides: ``(verdict, why)``.

        ``_CUT`` when the lane is still to be cut for *why*; ``_CUT_MADE``
        when a sidestep already cut it (a person or a sensor down, with that
        reason as *why*) and the drone is back on the lane; ``_AGAIN`` or
        ``_STOP`` otherwise.
        """
        lane_index = step[0]
        if why[1].person_cue == PERSON_CUE_UNKNOWN and not self._person_cue_down():
            # One look that waits for a person detection (bounded by its sense
            # budget), so a lane gate's blocked view, which does not wait for
            # one, can still lead to a sidestep without a re-look.
            verdict, checked = self._person_check(step, current, deadline)
            if verdict in (_AGAIN, _STOP):
                return verdict, why
            if checked is not None and checked[0] in _STOPPING_REASONS:
                return _CUT, checked
            if checked is not None and checked[1].person_cue == PERSON_CUE_CLEAR:
                # The fresh view decides the person rule; a blocked one also gives the clearer side.
                why = checked if checked[1].blocked else (why[0], _with_person_cue(why[1], checked[1]))
        refusal = self._sidestep_refusal(why[1])
        with self._lock:
            if not current():
                return _AGAIN, why
            busy = self._resume_target is not None
            if self._sidestep_lane != lane_index:
                self._sidestep_lane, self._sidestep_used = lane_index, 0
            left = self.obstacle_sidesteps - self._sidestep_used
        if busy:
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "refused", reason="returning")
            return _CUT, why
        if refusal is not None:
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "refused", reason=refusal)
            return _CUT, why
        if left <= 0:
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "refused", reason="attempts",
                            attempts=self.obstacle_sidesteps)
            return _CUT, why
        tried: set[str] = set()
        logged: set[tuple[str, str]] = set()
        while True:
            if self._budget_spent(deadline, EVENT_OBSTACLE_SIDESTEP, step):
                break
            with self._lock:
                if not current():
                    return _AGAIN, why
                if self._sidestep_used >= self.obstacle_sidesteps:
                    break
                # Worked out again before every attempt, from where the drone
                # is now: a way back that skipped a component under the
                # minimum move leaves it a little off the point it left.
                area = self._plan_area_locked(step)
            allowed, refused = self._sidestep_sides(step, why[1], area)
            for side, reason in refused:
                if (side, reason) not in logged:
                    logged.add((side, reason))
                    self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "refused", side=side, reason=reason,
                                    sidestep_cm=self.obstacle_sidestep_cm)
            untried = [side for side in allowed if side not in tried]
            if not untried:
                break
            side = untried[0]
            tried.add(side)
            with self._lock:
                self._sidestep_used += 1
                attempt = self._sidestep_used
            self._avoid_note(AVOID_PHASE_SIDESTEP, AVOID_SIDESTEPS_KEY)
            outcome, seen, cut_made = self._try_sidestep(step, gen, side, area, attempt, deadline, why)
            if cut_made:
                return (_STOP if outcome in (_STOPPED, _HELD) else _CUT_MADE), seen
            if outcome in (_PASSED, _REPLANNED):
                return _AGAIN, why
            if outcome in (_STOPPED, _HELD):
                return _STOP, why
            if seen is not None and seen[0] in _STOPPING_REASONS:
                # Seen, but the plan no longer has this lane (``_cut_for_seen``
                # logged it): the plan that replaced it is gated afresh.
                return _AGAIN, why
            if seen is not None and seen[0] != CUT_OBSTACLE:
                break  # the sensor could not look, or could not rule a person out: no further side
        return _CUT, why

    def _person_cue_down(self) -> bool:
        status = self._person_cue_status()
        return bool(status is not None and status.get(PERSON_CUE_DOWN_KEY))

    def _person_check(
        self, step: tuple[int, int, str, int], current: Callable[[], bool], deadline: float,
    ) -> tuple[str | None, tuple[str, SenseResult] | None]:
        """One gate that waits for a person detection before a sidestep: ``(verdict, (reason, result))``.

        *verdict* is ``_AGAIN`` or ``_STOP`` when the sequence must read the
        plan again or stop (nothing is cut), else None. The pair is the gate's
        last view with the reason a lane would be cut for (``obstacle``,
        ``person``, ``sensor_stale`` or ``sensor_down``), or ``("clear", view)``
        for a clear one; None when no look was taken (the budget is spent).
        """
        if self._budget_spent(deadline, EVENT_OBSTACLE_SIDESTEP, step):
            return None, None
        seen: list[tuple[str, SenseResult]] = []
        verdict = self._look(step, current, lambda reason, result: seen.append((reason, result)),
                             source=SOURCE_PERSON_CHECK, person_wait=True)
        if verdict == _GO:
            last = getattr(self, "_last_look_result", None)
            checked = None if last is None else ("clear", last)
        elif verdict == _CUT and seen:
            checked = seen[-1]
        else:
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "stopped", reason="person_check")
            return verdict, None
        self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "person_check",
                        reason=None if checked is None else checked[0],
                        person_cue=None if checked is None else checked[1].person_cue)
        return None, checked

    def _cut_for_budget(
        self, step: tuple[int, int, str, int], gen: int, why: tuple[str, SenseResult],
    ) -> bool:
        """Cut the blocked lane for *why* at the plan's progress once the time budget ran out mid-sidestep.

        Made before the drone flies back to the lane, so a way back that
        does not clear holds the drone with the lane already cut, and Resume
        search does not bring it back to the same obstacle. True when cut.
        """
        with self._lock:
            if self._abort.is_set():
                return False
            target = self._lane_step_now_locked(step, gen)
            if target is not None:
                self._cut_lane_locked(target, why[0], result=why[1])
        if target is None:
            return False
        self._avoid_log(EVENT_OBSTACLE_SIDESTEP, target, "cut", reason=why[0], detail="budget")
        return True

    def _cut_for_seen(
        self, step: tuple[int, int, str, int], gen: int, seen: tuple[str, SenseResult] | None,
    ) -> bool:
        """Cut the blocked lane at once for a person or a sensor down seen during a sidestep: True when cut.

        The cut is made at the plan's progress (``_lane_step_now_locked``),
        the point on the lane level with the drone, before the drone flies
        back to it, so a hold, a re-task that continues the plan or a way
        back that does not clear can never lose the reason. Nothing is cut
        for any other reason, after a landing (the plan has ended), or when
        the plan no longer has the lane (a re-task replaced it), which is
        logged.
        """
        if seen is None or seen[0] not in _STOPPING_REASONS:
            return False
        reason, result = seen
        target = None
        with self._lock:
            if self._abort.is_set():
                return False
            target = self._lane_step_now_locked(step, gen)
            if target is not None:
                self._cut_lane_locked(target, reason, result=result)
        if target is None:
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "not_cut", reason=reason,
                            detail="the plan no longer has this lane")
            return False
        self._avoid_log(EVENT_OBSTACLE_SIDESTEP, target, "cut", reason=reason)
        return True

    def _try_sidestep(
        self, step: tuple[int, int, str, int], gen: int, side: str, area: PlanArea | None, attempt: int,
        deadline: float, blocked_why: tuple[str, SenseResult],
    ) -> tuple[str, tuple[str, SenseResult] | None, bool]:
        """One sidestep attempt to *side*: ``(outcome, why, cut_made)``.

        *why* is the gate result that ended it, if any; *cut_made* is True
        when the lane was cut during it: for a person or a sensor down seen
        on the way (``_cut_for_seen``), or for *blocked_why*, the blocked
        view being avoided, when the time budget ran out beside the lane
        (``_cut_for_budget``).
        """
        lane_direction = step[2]
        heading = plan_heading(lane_direction)
        size = self.obstacle_sidestep_cm
        pose = self.envelope.pose_snapshot()
        line = RelativePose(pose.x_cm, pose.y_cm, heading)
        with self._lock:
            if not self._is_active() or self._abort.is_set():
                return _STOPPED, None, False
            self._resume_target = line
        self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "start", side=side, sidestep_cm=size, attempt=attempt,
                        attempts=self.obstacle_sidesteps)
        body = "right" if side == _direction_at(heading + 90.0) else "left"
        turn = shortest_rotation(pose.heading_deg, heading)
        path = ([("rotate", turn)] if turn else []) + [(body, size)]
        # The sideways move and its turn are flown like the way back of a
        # resume_search and are never retried (``flight_guards``): a turn is
        # never retried, and a sidestep that the Tello refused (for example
        # "error No valid imu") may have been partly flown beside an
        # obstacle. The executor counts such a refusal in ``move_refusals``
        # and lands (``lane_flight._run_sequence``).
        outcome, blocked = self._fly_return_sensed(path, source=SOURCE_SIDESTEP, person=True)
        if outcome == "stopped":
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "stopped", side=side)
            return _STOPPED, None, False
        cut_made = self._cut_for_seen(step, gen, blocked)
        moved = self._away_from(line)
        if outcome != "done" and not moved:
            with self._lock:
                if self._resume_target is line:
                    self._resume_target = None
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "refused", side=side,
                            reason=None if blocked is None else blocked[0],
                            person_cue=None if blocked is None else blocked[1].person_cue)
            return _REFUSED, blocked, cut_made
        seen: tuple[str, SenseResult] | None = blocked
        passed_cm = 0
        replanned = False
        offset = None
        if outcome == "done":
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "moved", side=side, sidestep_cm=size)
            beside = self._pass_beside(step, gen, line, area, deadline)
            seen, passed_cm, line, replanned = beside.why, beside.passed_cm, beside.line, beside.replanned
            if passed_cm > 0:
                # Counted however the pass ended (a hold or a landing may follow).
                offset = self._note_pass(step[0], gen, beside.from_cm, passed_cm)
                self._note_seen(offset, seen)
            if not cut_made:
                cut_made = self._cut_for_seen(step, gen, seen)
            if beside.budget and not cut_made:
                cut_made = self._cut_for_budget(step, gen, blocked_why)
            if beside.outcome == _STOPPED:
                self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "stopped", side=side, passed_cm=passed_cm)
                return _STOPPED, seen, cut_made
        back, back_cut = self._back_to_lane(step, gen, line, cut_made)
        # A person seen on the way back marks the offset even when the lane had
        # ended and could not be cut for it: the ground then asks the operator.
        self._note_seen(offset, self._last_return_block)
        if back_cut:
            cut_made, seen = True, self._last_return_block
        if back != "done":
            return back, seen, cut_made
        if cut_made:
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "returned", side=side, passed_cm=passed_cm,
                            reason=None if seen is None else seen[0])
            return _RETURNED, seen, True
        if passed_cm > 0:
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "passed", side=side, passed_cm=passed_cm)
            return _PASSED, seen, False
        self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "returned", side=side,
                        reason=None if seen is None else seen[0])
        return (_REPLANNED if replanned else _RETURNED), seen, False

    def _note_pass(self, lane_index: int, gen: int, from_cm: int, length_cm: int) -> list[Any] | None:
        """Count a pass and record the stretch of lane *lane_index* it flew beside the lane (``offsets``).

        Returns the recorded offset, whose last item (the reason a gate of
        the sidestep stopped for, ``_note_seen``) may still be set. Nothing is
        recorded for a plan that a re-task replaced meanwhile: a plan that
        does not continue this one has its own counts and offsets.
        """
        with self._lock:
            if not (self._plan_gen == gen or gen in self._linked_gens):
                return None
            self._avoid_phase = AVOID_PHASE_SIDESTEP
            self._avoid_counts[AVOID_SIDESTEP_PASSES_KEY] = self._avoid_counts.get(AVOID_SIDESTEP_PASSES_KEY, 0) + 1
            offset: list[Any] = [int(lane_index), int(from_cm), int(length_cm), ""]
            self._avoid_offsets.append(offset)
            return offset

    def _note_seen(self, offset: list[Any] | None, seen: tuple[str, SenseResult] | None) -> None:
        """Mark *offset* with a person or a sensor down that a gate of its sidestep stopped for.

        The ground then never offers that offset without the operator's
        confirmation, even when the lane could not be cut for it (the lane
        had ended before the way back saw the person).
        """
        if offset is None or seen is None or seen[0] not in _STOPPING_REASONS:
            return
        with self._lock:
            if not offset[3] or seen[0] == CUT_PERSON:
                offset[3] = seen[0]

    def _away_from(self, point: RelativePose) -> bool:
        pose = self.envelope.pose_snapshot()
        return abs(pose.x_cm - point.x_cm) > _SAME_POINT_CM or abs(pose.y_cm - point.y_cm) > _SAME_POINT_CM

    def _pass_beside(
        self, step: tuple[int, int, str, int], gen: int, line: RelativePose, area: PlanArea | None, deadline: float,
    ) -> _Pass:
        """Fly the lane's next steps beside it (see ``_Pass`` for what it returns).

        Each step must still be a step of the blocked lane in this plan,
        stay inside the geofence and *area*, face the lane heading and pass
        a gate that saw no person, and the time budget must not be spent.
        """
        lane_index, _, lane_direction, _ = step
        heading = plan_heading(lane_direction)
        fx, fy = _UNIT[lane_direction]
        passed_cm = 0
        with self._lock:
            from_cm = self._lane_flown_locked(lane_index)
        why: list[tuple[str, SenseResult]] = []

        def stop(reason: str, result: SenseResult) -> None:
            why.append((reason, result))

        def ended(outcome: str, replanned: bool = False, budget: bool = False) -> _Pass:
            return _Pass(outcome, why[-1] if why else None, passed_cm, from_cm, line, replanned, budget)

        while passed_cm < OBSTACLE_SIDESTEP_PASS_CM:
            if self._budget_spent(deadline, EVENT_OBSTACLE_SIDESTEP, step):
                return ended("done", budget=True)
            with self._lock:
                if not self._is_active() or self._abort.is_set():
                    return ended(_STOPPED)
                if not (self._plan_gen == gen or gen in self._linked_gens):
                    return ended("done", replanned=True)
                beside = self._moves[self._step] if self._step < len(self._moves) else None
            if beside is None or beside[0] != lane_index:
                return ended("done")  # the lane ends here
            pose = self.envelope.pose_snapshot()
            end = (pose.x_cm + fx * beside[3], pose.y_cm + fy * beside[3])
            if not self.envelope.inside_fence(*end) or area is None or not area.contains(*end):
                return ended("done")

            def still(beside: tuple[int, int, str, int] = beside) -> bool:
                return self._step_current_locked(beside, gen)

            verdict = self._face(heading, beside, (SOURCE_SIDESTEP, beside, gen), still, stop)
            if verdict == _AGAIN:
                if not self._pause_point():
                    return ended(_STOPPED)
                continue
            if verdict == _STOP:
                return ended(_STOPPED)
            if verdict is not None:
                return ended("done")
            verdict = self._look(beside, still, stop, source=SOURCE_SIDESTEP, person_wait=True, need_person=True)
            if verdict == _STOP:
                return ended(_STOPPED)
            if verdict == _AGAIN:
                # A re-task replaced the step, or a hold, manual control or a
                # link-loss hold arrived: the latter stop here.
                if not self._pause_point():
                    return ended(_STOPPED)
                with self._lock:
                    replaced = not self._step_current_locked(beside, gen)
                if replaced:
                    return ended("done", replanned=True)
                continue
            if verdict != _GO:
                return ended("done")
            # The envelope's fence is shrunk by the lane steps the drone
            # refused this flight, which it may have partly flown.
            if not self.envelope.check_move("forward", beside[3]):
                return ended("done")
            # Not retried: a refusal beside an obstacle lands, counted in
            # ``move_refusals`` (``lane_flight._run_sequence``).
            if not self._command_move("forward", beside[3], record=True):
                return ended(_STOPPED)
            moved_line = RelativePose(line.x_cm + fx * beside[3], line.y_cm + fy * beside[3], heading)
            with self._lock:
                if self._resume_target is line:
                    self._resume_target = moved_line
                line = moved_line
                counted = (
                    (self._plan_gen == gen or gen in self._linked_gens)
                    and self._step < len(self._moves)
                    and self._moves[self._step][:2] == beside[:2]
                )
                if counted:
                    self._step += 1
            if not counted:
                # A re-task replaced the step while it was flown: the drone
                # is level with the point after it, which the line follows.
                return ended("done", replanned=True)
            passed_cm += beside[3]
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, beside, "beside", passed_cm=passed_cm)
        return ended("done")

    def _back_to_lane(
        self, step: tuple[int, int, str, int], gen: int, line: RelativePose, cut_made: bool,
    ) -> tuple[str, bool]:
        """Fly back to the point *line* on the lane: ``(outcome, cut_here)``.

        *outcome* is ``"done"``, ``_STOPPED`` or ``_HELD``. Sensed like the
        way back of a ``resume_search``, with the sidestep's person rule. A
        way back that a gate does not clear, or that has no path inside the
        geofence, never lands: the drone hovers under manual control with
        *line* as its hold point (``_avoid_held_manual``; its telemetry phase
        is ``held``). When that gate saw a person or a sensor down, the lane
        is also cut for it unless *cut_made* says it already is; *cut_here*
        is True then. The result of a gate that stopped it is in
        ``_last_return_block`` (None otherwise). A
        landing or a hold stops it before its next move, with *line* kept as
        the point the plan continues from.
        """
        self._last_return_block = None
        if self._away_from(line):
            self._avoid_note(AVOID_PHASE_RETURN)
            with self._lock:
                ours = self._resume_target is line
                path = self._return_path_locked(line) if ours else None
            if not ours:
                return _STOPPED, False
            if path is None:
                with self._lock:
                    held = self._is_active() and not self._abort.is_set()
                    if held:
                        self._hold_short_of_return_locked()
                        self._avoid_held_manual = self._manual
                        self._avoid_held_reason = "geofence"
                    lane_index = self.lane_index
                if not held:
                    return _STOPPED, False
                self._log.warning(
                    "edge_resume_search_blocked", drone_id=self.drone_id, reason="geofence", sensor_reason=None,
                    cause=None, target_x_cm=round(line.x_cm, 1), target_y_cm=round(line.y_cm, 1),
                    lane_index=lane_index, control_mode=self.control_mode, **self._pose_fields(),
                )
                self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "held", reason="geofence",
                                control_mode=self.control_mode)
                return _HELD, False
            # Never retried, like the sidestep itself: a refusal lands, counted
            # in ``move_refusals`` (``lane_flight._run_sequence``).
            outcome, blocked = self._fly_return_sensed(path, source=SOURCE_SIDESTEP_RETURN, person=True)
            if outcome == "stopped":
                self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "stopped", reason="return")
                return _STOPPED, False
            if outcome == "blocked" and blocked is not None:
                self._last_return_block = blocked
                cut_here = not cut_made and self._cut_for_seen(step, gen, blocked)
                self._end_blocked_return(line, *blocked)
                with self._lock:
                    if self._manual is not None:
                        self._avoid_held_manual = self._manual
                        self._avoid_held_reason = blocked[0]
                self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "held", reason=blocked[0],
                                person_cue=blocked[1].person_cue, control_mode=self.control_mode)
                return _HELD, cut_here
        with self._lock:
            if self._resume_target is line:
                self._resume_target = None
        self._avoid_note(AVOID_PHASE_SIDESTEP)
        return "done", False
