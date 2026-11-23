"""Look again and sidestep at a gate that saw an obstacle, before the lane is cut.

``LaneExecutor`` (``flightrisk.drone.lane_executor``) inherits
``ObstacleAvoidMixin``. When the sense gate of a lane step
(``lane_flight._look``) sees an obstacle (cut reason ``obstacle``, the depth
cue), ``lane_flight._prepare_step`` holds the cut back and calls
``_avoid_blocked``, which tries, in this order:

1. **Look again.** ``obstacle_relook`` times (``FLIGHTRISK_OBSTACLE_RELOOK``,
   2 on the edge) the drone hovers ``obstacle_retry_pause_s`` and runs the
   whole gate again, with its clear votes and its retries, on frames captured
   after that gate started. The step is flown only when every one of these
   gates is clear, so overriding a blocked view takes more clear views than
   clearing a step ever does. A person seen at any of them cuts the lane with
   reason ``person``, a sensor down for ``OBSTACLE_SENSOR_DOWN_S`` cuts it
   with ``sensor_down``, and a sensor that cannot look cuts it with
   ``obstacle`` (the obstacle was seen) without a sidestep.
2. **Sidestep.** Only while the lane is still blocked by the depth cue, only
   for a drone that turns nose first and does not measure its own heading
   (the Tello and the fake drone), at most ``obstacle_sidesteps`` attempts per
   lane (``FLIGHTRISK_OBSTACLE_SIDESTEPS``, 2 on the edge), each
   ``obstacle_sidestep_cm`` sideways (``FLIGHTRISK_OBSTACLE_SIDESTEP_CM``, 50
   on the edge). The side the depth guard calls clearer is tried first, else
   the side with more room. A side is tried only when the point beside the
   drone and the end of the next lane step beside the lane both lie inside
   the geofence and inside the box around the drone's own planned and flown
   path (``_plan_area_locked``). The ground gives every drone a region that
   holds its whole path and keeps regions apart (``fleet_patterns``,
   "Separation rule"), so a sidestep never takes a drone toward another
   drone's lanes; the altitude never changes, so the slot and the ceiling
   are untouched. An attempt:

   a. records the point on the lane where the drone stopped as the point the
      plan continues from (``_resume_target``, the same field a
      ``resume_search`` uses), so a hold, a link-loss hold or a manual
      takeover during the sidestep brings the drone back there before the
      plan goes on (``lane_manual._finish_return`` and the hold point);
   b. turns to face the side and flies the sideways move after a clear gate
      (a person there, or anything else that is not clear, refuses that
      side with nothing moved);
   c. turns back to the lane heading and flies the lane's own next steps on
      the offset line, each after a clear gate and only while it stays
      inside the geofence and the box, for up to
      ``OBSTACLE_SIDESTEP_PASS_CM``; each step flown advances the plan and
      the point on the lane;
   d. flies back to the lane, sensed like the way back of a
      ``resume_search`` (``lane_flight._fly_return_sensed``).

   When at least one step was flown beside the obstacle, the plan goes on
   from the lane with its next gate. Otherwise the other side is tried, if
   an attempt is left, and then the lane is cut.
3. **Cut** as before, where the drone stopped on the lane.

Nothing here ever lands the drone. Every sideways and offset move is checked
against the geofence before it is sent, so the geofence landing of a lane
step can never be reached from a sidestep. A way back to the lane that a
gate does not clear cuts nothing: the drone hovers under manual control
with the point on the lane as its hold point
(``lane_manual._end_blocked_return``), and the operator decides. A landing
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

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from flightrisk.drone.lane_flight import _AGAIN, _CUT, _GO, _STOP
from flightrisk.drone.lane_steps import plan_heading, replay_lanes, shortest_rotation
from flightrisk.drone.obstacle_sensor import (
    AVOID_COUNT_KEYS,
    AVOID_PHASE_KEY,
    AVOID_PHASE_RELOOK,
    AVOID_PHASE_RETURN,
    AVOID_PHASE_SIDESTEP,
    AVOID_RELOOK_CLEARS_KEY,
    AVOID_RELOOKS_KEY,
    AVOID_SIDESTEP_PASSES_KEY,
    AVOID_SIDESTEPS_KEY,
    CUT_OBSTACLE,
    CUT_PERSON,
    CUT_SENSOR_DOWN,
    EVENT_OBSTACLE_RELOOK,
    EVENT_OBSTACLE_SIDESTEP,
    OBSTACLE_CLEAR_VOTES_RANGE,
    OBSTACLE_RELOOK_RANGE,
    OBSTACLE_SIDESTEP_PASS_CM,
    OBSTACLE_SIDESTEPS_RANGE,
    SenseResult,
)
from flightrisk.drone.safety import ROOM_LIMITS, RelativePose

__all__ = [
    "SOURCE_RELOOK",
    "SOURCE_SIDESTEP",
    "SOURCE_SIDESTEP_RETURN",
    "ObstacleAvoidMixin",
]

#: ``source`` of the gate logs of a re-look, a sidestep and its way back to the lane.
SOURCE_RELOOK = "relook"
SOURCE_SIDESTEP = "sidestep"
SOURCE_SIDESTEP_RETURN = "sidestep_return"

# Outcomes of one sidestep attempt (``_try_sidestep``).
_REFUSED = "refused"  # the sideways gate did not clear; nothing moved sideways
_RETURNED = "returned"  # moved sideways, nothing flown beside the obstacle, back on the lane
_PASSED = "passed"  # flew at least one step beside the obstacle, back on the lane
_REPLANNED = "replanned"  # a re-task replaced the step; back on the lane
_STOPPED = "stopped"  # a landing, a hold or a link-loss hold stopped the sequence
_HELD = "held"  # the way back was not cleared: the drone hovers under manual control

# Unit vector (x, y) of each plan direction in the plan frame (``flightrisk.drone.safety``).
_UNIT: dict[str, tuple[int, int]] = {"forward": (0, 1), "back": (0, -1), "right": (1, 0), "left": (-1, 0)}
_DIRECTION_AT: dict[int, str] = {0: "forward", 90: "right", 180: "back", 270: "left"}
# Two points closer than this are the same point.
_SAME_POINT_CM = 1e-6


def _direction_at(heading_deg: float) -> str:
    """The plan direction a heading of a multiple of 90 degrees faces."""
    return _DIRECTION_AT[round(heading_deg) % 360]


def _whole(name: str, value: Any, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{name} must be a whole number from {lo} to {hi}, got {value!r}")
    return value


@dataclass(frozen=True)
class PlanArea:
    """The box around a drone's planned and flown path, in the dead-reckoned pose frame."""

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
        """How far the box reaches from ``(x_cm, y_cm)`` in plan *direction* (0 outside it)."""
        ux, uy = _UNIT[direction]
        if ux:
            return max(0.0, (self.x_max - x_cm) if ux > 0 else (x_cm - self.x_min))
        return max(0.0, (self.y_max - y_cm) if uy > 0 else (y_cm - self.y_min))


class ObstacleAvoidMixin:
    """Re-look and sidestep at a blocked lane gate for ``LaneExecutor`` (see the module docstring)."""

    def _init_avoid(self, clear_votes: Any, relook: Any, sidestep_cm: Any, sidesteps: Any) -> None:
        """Validate and store the avoidance settings. Raises ``ValueError``.

        *sidestep_cm* is 0 (no sidestep) or a move the envelope allows.
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
        self._avoid_counts: dict[str, int] = dict.fromkeys(AVOID_COUNT_KEYS, 0)
        self._avoid_phase: str | None = None
        # (plan generation, lane index) of the lane the sidestep count is for.
        self._sidestep_key: tuple[int, int] | None = None
        self._sidestep_used = 0

    # ------------------------------------------------------------------
    # Settings, telemetry and the plan
    # ------------------------------------------------------------------

    def _avoid_configured(self) -> bool:
        """True when the re-look or the sidestep is switched on (whatever the drone)."""
        return self.obstacle_relook > 0 or (self.obstacle_sidestep_cm > 0 and self.obstacle_sidesteps > 0)

    def _sidestep_enabled(self) -> bool:
        """True when this executor may sidestep: switched on, a sensor, nose first, no measured heading.

        A drone that measures its own heading is an autopilot with its own
        position hold and the field step; its sidestep has not been designed
        or tested, so it only looks again.
        """
        return (
            self.obstacle_sidestep_cm > 0
            and self.obstacle_sidesteps > 0
            and self._obstacle_sensor is not None
            and self._turns_to_face()
            and not callable(getattr(self.drone, "measured_heading_deg", None))
        )

    def _avoid_status(self) -> dict[str, Any] | None:
        """The ``obstacle_avoid`` telemetry object, or None while the re-look and the sidestep are off."""
        if not self._avoid_configured():
            return None
        with self._lock:
            status: dict[str, Any] = dict(self._avoid_counts)
            status[AVOID_PHASE_KEY] = self._avoid_phase
        return status

    def _reset_avoid_plan_locked(self) -> None:
        """Forget the counts of the plan being replaced. Caller holds ``_lock``."""
        self._avoid_counts = dict.fromkeys(AVOID_COUNT_KEYS, 0)
        self._sidestep_key = None
        self._sidestep_used = 0

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
        log = self._log.warning if phase in ("held",) else self._log.info
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
        is returned after *cut* was called under ``_lock`` for a step that was
        still *current*, with the reason it ends on.
        """
        try:
            verdict, why, may_sidestep = self._relook(step, current, first)
            if verdict != _CUT:
                return verdict
            if may_sidestep and self._sidestep_enabled():
                verdict, why = self._sidestep(step, gen, current, why)
                if verdict != _CUT:
                    return verdict
            with self._lock:
                if not current():
                    return _AGAIN
                cut(*why)
            return _CUT
        finally:
            self._avoid_note(None)

    def _relook(
        self, step: tuple[int, int, str, int], current: Callable[[], bool], first: SenseResult,
    ) -> tuple[str, tuple[str, SenseResult], bool]:
        """The re-look: ``(verdict, (reason, result), may_sidestep)``.

        ``_GO`` when every re-look gate cleared; ``_CUT`` with the reason the
        lane would be cut for and whether a sidestep may still be tried;
        ``_AGAIN`` or ``_STOP`` as the gate returns them (a re-task, a hold, a
        landing). Without re-looks it is ``_CUT`` with *first* at once.
        """
        why: tuple[str, SenseResult] = (CUT_OBSTACLE, first)
        rounds = self.obstacle_relook
        if rounds <= 0:
            return _CUT, why, True
        self._avoid_note(AVOID_PHASE_RELOOK, AVOID_RELOOKS_KEY)
        self._avoid_log(EVENT_OBSTACLE_RELOOK, step, "start", rounds=rounds, depth_score=first.depth_score)
        for attempt in range(1, rounds + 1):
            # Hover, then stop here for a landing, a hold, manual control or a
            # link-loss hold: nothing was cut, so the step is gated afresh later.
            if self._abort.wait(self.obstacle_retry_pause_s) or not self._pause_point():
                self._avoid_log(EVENT_OBSTACLE_RELOOK, step, "stopped", attempt=attempt, rounds=rounds)
                return _STOP, why, False
            seen: list[tuple[str, SenseResult]] = []
            verdict = self._look(step, current, lambda reason, result, seen=seen: seen.append((reason, result)),
                                 source=SOURCE_RELOOK)
            if verdict == _GO:
                continue
            if verdict != _CUT:
                self._avoid_log(EVENT_OBSTACLE_RELOOK, step, "stopped", attempt=attempt, rounds=rounds)
                return verdict, why, False
            reason, result = seen[-1]
            self._avoid_log(EVENT_OBSTACLE_RELOOK, step, "blocked", attempt=attempt, rounds=rounds, reason=reason,
                            depth_score=result.depth_score)
            if reason in (CUT_PERSON, CUT_SENSOR_DOWN):
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

    def _plan_area_locked(self, step: tuple[int, int, str, int]) -> PlanArea | None:
        """The box around the planned and flown path of the current plan, in the pose frame, or None. Caller holds ``_lock``.

        The replay of the plan with its cuts (``lane_steps.replay_lanes``)
        gives where each lane is planned and where it is flown, relative to
        the start of the lane list; the drone stands on *step*'s lane, as far
        along it as the plan's progress says, which places that start in the
        pose frame. None when the plan cannot be replayed, which refuses
        every sidestep.
        """
        lane_index, _, direction, _ = step
        try:
            limits = getattr(self.envelope, "limits", ROOM_LIMITS)
            replay = replay_lanes(self._lanes, self._cuts, limits.min_move_cm)
            if not 0 <= lane_index < len(replay.path) - 1:
                return None
            flown = sum(move[3] for move in self._moves[: self._step] if move[0] == lane_index)
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
        return PlanArea(min(xs), max(xs), min(ys), max(ys))

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

    def _sidestep(
        self,
        step: tuple[int, int, str, int],
        gen: int,
        current: Callable[[], bool],
        why: tuple[str, SenseResult],
    ) -> tuple[str, tuple[str, SenseResult]]:
        """Try the allowed sides: ``(verdict, why)``; ``_CUT`` when the lane is still to be cut for *why*."""
        lane_index = step[0]
        with self._lock:
            if not current():
                return _AGAIN, why
            busy = self._resume_target is not None
            area = None if busy else self._plan_area_locked(step)
            key = (gen, lane_index)
            if self._sidestep_key != key:
                self._sidestep_key, self._sidestep_used = key, 0
            left = self.obstacle_sidesteps - self._sidestep_used
        if busy:
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "refused", reason="returning")
            return _CUT, why
        if left <= 0:
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "refused", reason="attempts",
                            attempts=self.obstacle_sidesteps)
            return _CUT, why
        allowed, refused = self._sidestep_sides(step, why[1], area)
        for side, reason in refused:
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "refused", side=side, reason=reason,
                            sidestep_cm=self.obstacle_sidestep_cm)
        for side in allowed:
            with self._lock:
                if not current():
                    return _AGAIN, why
                if self._sidestep_used >= self.obstacle_sidesteps:
                    break
                self._sidestep_used += 1
                attempt = self._sidestep_used
            self._avoid_note(AVOID_PHASE_SIDESTEP, AVOID_SIDESTEPS_KEY)
            outcome, seen = self._try_sidestep(step, gen, side, area, attempt)
            if outcome == _PASSED:
                self._avoid_note(None, AVOID_SIDESTEP_PASSES_KEY)
                return _AGAIN, why
            if outcome in (_STOPPED, _HELD):
                return _STOP, why
            if outcome == _REPLANNED:
                return _AGAIN, why
            if seen is not None and seen[0] in (CUT_PERSON, CUT_SENSOR_DOWN):
                # Never past a person, and a sensor down ends the lanes.
                return _CUT, seen
            if seen is not None and seen[0] != CUT_OBSTACLE:
                break  # the sensor could not look: no further side
        return _CUT, why

    def _try_sidestep(
        self, step: tuple[int, int, str, int], gen: int, side: str, area: PlanArea | None, attempt: int,
    ) -> tuple[str, tuple[str, SenseResult] | None]:
        """One sidestep attempt to *side*: ``(outcome, why)``, *why* the gate result that ended it, if any."""
        lane_direction = step[2]
        heading = plan_heading(lane_direction)
        size = self.obstacle_sidestep_cm
        pose = self.envelope.pose_snapshot()
        line = RelativePose(pose.x_cm, pose.y_cm, heading)
        with self._lock:
            if not self._is_active() or self._abort.is_set():
                return _STOPPED, None
            self._resume_target = line
        self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "start", side=side, sidestep_cm=size, attempt=attempt,
                        attempts=self.obstacle_sidesteps)
        body = "right" if side == _direction_at(heading + 90.0) else "left"
        turn = shortest_rotation(pose.heading_deg, heading)
        path = ([("rotate", turn)] if turn else []) + [(body, size)]
        outcome, blocked = self._fly_return_sensed(path, source=SOURCE_SIDESTEP)
        if outcome == "stopped":
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "stopped", side=side)
            return _STOPPED, None
        moved = self._away_from(line)
        if outcome != "done" and not moved:
            with self._lock:
                if self._resume_target is line:
                    self._resume_target = None
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "refused", side=side,
                            reason=None if blocked is None else blocked[0])
            return _REFUSED, blocked
        seen: tuple[str, SenseResult] | None = blocked
        passed_cm = 0
        replanned = False
        if outcome == "done":
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "moved", side=side, sidestep_cm=size)
            pass_outcome, seen, passed_cm, line, replanned = self._pass_beside(step, gen, line, area)
            if pass_outcome == _STOPPED:
                self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "stopped", side=side, passed_cm=passed_cm)
                return _STOPPED, None
        back = self._back_to_lane(step, line, seen)
        if back != "done":
            return back, None
        if passed_cm > 0:
            self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "passed", side=side, passed_cm=passed_cm)
            return _PASSED, None
        self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "returned", side=side,
                        reason=None if seen is None else seen[0])
        return (_REPLANNED if replanned else _RETURNED), seen

    def _away_from(self, point: RelativePose) -> bool:
        pose = self.envelope.pose_snapshot()
        return abs(pose.x_cm - point.x_cm) > _SAME_POINT_CM or abs(pose.y_cm - point.y_cm) > _SAME_POINT_CM

    def _pass_beside(
        self, step: tuple[int, int, str, int], gen: int, line: RelativePose, area: PlanArea | None,
    ) -> tuple[str, tuple[str, SenseResult] | None, int, RelativePose, bool]:
        """Fly the lane's next steps beside it: ``(outcome, why, passed_cm, line, replanned)``.

        *outcome* is ``_STOPPED`` or ``"done"``; *why* is the gate result that
        ended the pass, if one did; *line* is the point on the lane level
        with the drone, which follows every step flown; *replanned* is True
        when a re-task replaced the step. Each step must still be a step of
        the blocked lane in this plan, stay inside the geofence and *area*,
        face the lane heading and pass a gate.
        """
        lane_index, _, lane_direction, _ = step
        heading = plan_heading(lane_direction)
        fx, fy = _UNIT[lane_direction]
        passed_cm = 0
        why: list[tuple[str, SenseResult]] = []

        def stop(reason: str, result: SenseResult) -> None:
            why.append((reason, result))

        def ended(outcome: str, replanned: bool = False):
            return outcome, (why[-1] if why else None), passed_cm, line, replanned

        while passed_cm < OBSTACLE_SIDESTEP_PASS_CM:
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
            verdict = self._look(beside, still, stop, source=SOURCE_SIDESTEP)
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
            if not self.envelope.check_move("forward", beside[3]):
                return ended("done")
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
        self, step: tuple[int, int, str, int], line: RelativePose, why: tuple[str, SenseResult] | None,
    ) -> str:
        """Fly back to the point *line* on the lane: ``"done"``, ``_STOPPED`` or ``_HELD``.

        Sensed like the way back of a ``resume_search``. A way back that a
        gate does not clear, or that has no path inside the geofence, cuts
        nothing and never lands: the drone hovers under manual control with
        *line* as its hold point. A landing or a hold stops it before its
        next move, with *line* kept as the point the plan continues from.
        """
        if self._away_from(line):
            self._avoid_note(AVOID_PHASE_RETURN)
            with self._lock:
                path = self._return_path_locked(line) if self._resume_target is line else None
                ours = self._resume_target is line
            if not ours:
                return _STOPPED
            if path is None:
                with self._lock:
                    if self._is_active() and not self._abort.is_set():
                        self._hold_short_of_return_locked()
                self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "held", reason="geofence",
                                control_mode=self.control_mode)
                return _HELD
            outcome, blocked = self._fly_return_sensed(path, source=SOURCE_SIDESTEP_RETURN)
            if outcome == "stopped":
                self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "stopped", reason="return")
                return _STOPPED
            if outcome == "blocked" and blocked is not None:
                self._end_blocked_return(line, *blocked)
                self._avoid_log(EVENT_OBSTACLE_SIDESTEP, step, "held", reason=blocked[0],
                                control_mode=self.control_mode)
                return _HELD
        with self._lock:
            if self._resume_target is line:
                self._resume_target = None
        self._avoid_note(AVOID_PHASE_SIDESTEP)
        return "done"
