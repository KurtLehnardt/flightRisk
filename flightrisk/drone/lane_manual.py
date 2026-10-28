"""Manual takeover on the edge: ``manual_hold``, ``manual_move``, ``manual_takeoff`` and ``resume_search``.

``LaneExecutor`` (``flightrisk.drone.lane_executor``) inherits
``ManualControlMixin``. The protocol side is
``flightrisk/fleet_protocol/manual.py`` and the operator's view is
``docs/manual-takeover.md``. In short:

* **manual_hold** takes the drone out of its search. The executor stops
  advancing but keeps its plan: a running lane sequence stops after the move
  already with the drone (a move is never interrupted) and the drone hovers.
  The pose (position and heading) and the plan progress (the next move) at
  the moment the search stopped are recorded as the *hold point*; when a
  move was in flight they are recorded once it has completed. A drone on the
  ground is only marked paused (its pending scheduled start is cancelled).
  While the drone is under manual control no lane, re-task, scheduled start,
  autonomous takeoff, v2 ``resume`` or ``nudge`` moves it, and the
  end-of-lanes hover timeout does not run. Landing works as always.
* **manual_move** is one bounded step: a 20 to 100 cm move forward, back,
  left, right, up or down, or a 5 to 45 degree rotation. It is checked
  against every limit an autonomous flight has, plus a floor: the geofence
  (a square of half-width ``geofence_cm`` around the takeoff point, from the
  dead-reckoned pose), the ceiling limit for climbs (the ceiling minus its
  margin, ``SafetyEnvelope.max_alt_slot_cm``, from the measured height; an
  unknown height refuses the climb), ``MANUAL_FLOOR_CM`` for descents, and
  the drone link (a drone the controller cannot reach is not moved). A
  refused move is answered with a nack naming the limit; a move that is
  flown is recorded in the dead-reckoned pose like a lane move, and is
  acknowledged once the drone has flown it. As for lanes, a controller error
  lands the drone.
* **manual_takeoff** takes off through the same preflight as an autonomous
  takeoff (drone link, battery, ground link; ``lane_flight._do_takeoff``),
  then climbs to the drone's altitude slot under the same ceiling checks,
  and hovers under manual control.
* **The manual land** is the ordinary ``land`` (``reason: manual``): never
  fenced, never refused, the existing landing path. The drone stays under
  manual control on the ground, so nothing flies it until ``resume_search``
  or a new plan. Like every landing it ends the plan.
* **resume_search** ends manual control. Without a valid plan with moves
  left the drone is just released (``detail: no_plan``); on the ground with
  a valid plan it is released to wait for its start (``detail: on_ground``).
  In the air it flies back to the hold point and continues: first to its
  altitude slot (so the way back is flown in its own altitude band), then to
  the recorded heading, then to the recorded position (a move along the
  recorded forward axis and one along its right axis, in whichever order
  stays inside the geofence), then the rest of the plan from the recorded
  progress. A hold point outside the geofence is replaced by the nearest
  point inside it (``detail: returning_nearest``); a way back or a remaining
  plan that would leave the geofence is refused (``nack geofence``) and the
  drone stays under manual control. The return is autonomous flight: a new
  ``manual_hold`` stops it before its next move and keeps the original hold
  point.
* **Dead-man.** With no manual command for ``MANUAL_DEADMAN_S`` (1.5 s) the
  drone is reported ``held``: it hovers in place, because every manual step
  ends in a hover and nothing else moves it. The next manual command makes
  it ``manual`` again. The dead-man never lands; the ground-link watchdog
  (``FLIGHTRISK_LINK_LOSS_LAND_S``) and the battery and ceiling landings are
  unchanged and still land a manually controlled drone.
* **A new plan on the ground ends manual control** (an ``assign_lanes``
  accepted while the drone is on the ground). In the air a new plan is only
  stored, and ``resume_search`` flies it from the hold point.

Telemetry carries ``control_mode`` (``autonomous``, ``manual`` or ``held``)
on every link version; its presence tells the ground that this edge takes
the manual commands.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from flightrisk.drone.controller import command_result
from flightrisk.drone.lane_flight import MoveNotExecutedError
from flightrisk.drone.lane_plan import split_lane_moves
from flightrisk.drone.safety import (
    HORIZONTAL_DIRECTIONS,
    MIN_MOVE_CM,
    RelativePose,
    format_cm,
    is_finite_number,
)
from flightrisk.fleet_protocol import (
    MANUAL_DEADMAN_S,
    MANUAL_FLOOR_CM,
    MANUAL_HOLD,
    MANUAL_MOVE,
    MANUAL_ROTATIONS,
    MANUAL_TAKEOFF,
    MAX_TEXT_LEN,
    RESUME_SEARCH,
    ProtocolError,
    make_manual_ack,
    make_manual_nack,
    validate_manual_command,
)

__all__ = ["MANUAL_CONTROL_TYPES", "ManualControlMixin", "ManualState"]

# Ground -> edge manual commands the executor handles.
MANUAL_CONTROL_TYPES = frozenset({MANUAL_HOLD, MANUAL_MOVE, MANUAL_TAKEOFF, RESUME_SEARCH})


@dataclass
class ManualState:
    """Manual control of one drone; guarded by the executor lock."""

    since_s: float
    last_cmd_s: float
    # True once no manual command arrived for the dead-man time.
    deadman: bool = False
    # Where the search stopped: the pose (position and heading) and the plan
    # progress. None while the drone was on the ground when taken over.
    hold_pose: RelativePose | None = None
    hold_step: int | None = None
    hold_lane_index: int | None = None
    hold_plan_gen: int | None = None
    # A flight sequence was still running at the hold: the hold point is
    # recorded when it stops (after the move it had with the drone).
    capture_pending: bool = False


def _round(value: float) -> float:
    return round(float(value), 1)


def _axis_moves(component_cm: float, positive: str, negative: str, *, exact: bool) -> list[tuple[str, int]]:
    """Tello-sized moves along one axis whose net displacement is *component_cm*.

    A component shorter than the 20 cm minimum is flown as an overshoot and a
    20 cm return when *exact*, and skipped otherwise (at most 20 cm short).
    """
    distance = round(abs(component_cm))
    if distance == 0:
        return []
    direction, opposite = (positive, negative) if component_cm > 0 else (negative, positive)
    if distance < MIN_MOVE_CM:
        return [(direction, distance + MIN_MOVE_CM), (opposite, MIN_MOVE_CM)] if exact else []
    return [(direction, part) for part in split_lane_moves(distance)]


class ManualControlMixin:
    """Manual takeover for ``LaneExecutor`` (see the module docstring)."""

    manual_deadman_s: float = MANUAL_DEADMAN_S

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def control_mode(self) -> str:
        """``autonomous``, ``manual`` or ``held`` (see the module docstring)."""
        with self._lock:
            return self._control_mode_locked()

    def _control_mode_locked(self) -> str:
        manual = self._manual
        if manual is None:
            return "autonomous"
        return "held" if manual.deadman else "manual"

    @property
    def manual_state(self) -> ManualState | None:
        """A copy of the manual-control state, or None while autonomous."""
        with self._lock:
            manual = self._manual
            if manual is None:
                return None
            pose = manual.hold_pose
            copy = ManualState(**manual.__dict__)
            copy.hold_pose = None if pose is None else RelativePose(pose.x_cm, pose.y_cm, pose.heading_deg)
            return copy

    def _manual_note_command_locked(self) -> bool:
        """Record operator activity for the dead-man; True if it ends a dead-man hold. Caller holds ``_lock``."""
        manual = self._manual
        if manual is None:
            return False
        manual.last_cmd_s = self._clock()
        was, manual.deadman = manual.deadman, False
        return was

    def _release_manual_locked(self) -> None:
        """End manual control; the drone is autonomous again. Caller holds ``_lock``."""
        self._manual = None
        self._held = False
        self._parked = None
        self._hover_since = None

    def _capture_hold_point_locked(self) -> None:
        """Record the hold point: the pose and the plan progress where the search stopped. Caller holds ``_lock``.

        During an interrupted return the hold point stays the one the return
        was flying to, so the search still resumes where it first stopped.
        """
        manual = self._manual
        if manual is None:
            return
        manual.capture_pending = False
        if not self._is_flying():
            return
        target = self._resume_target if self._resume_target is not None else self.envelope.pose_snapshot()
        self._resume_target = None
        manual.hold_pose = RelativePose(target.x_cm, target.y_cm, target.heading_deg)
        manual.hold_step = self._step
        manual.hold_lane_index = self._moves[self._step][0] if self._step < len(self._moves) else self._lanes_total
        manual.hold_plan_gen = self._plan_gen
        self._log.info(
            "edge_manual_hold_point",
            drone_id=self.drone_id,
            x_cm=_round(target.x_cm),
            y_cm=_round(target.y_cm),
            heading_deg=_round(target.heading_deg),
            lane_index=manual.hold_lane_index,
            step=manual.hold_step,
            plan_id=None if self._plan_identity is None else self._plan_identity.plan_id,
        )

    def _resume_point_locked(self) -> RelativePose | None:
        """Where the plan will continue from while under manual control or returning, else None. Caller holds ``_lock``."""
        if self._resume_target is not None:
            target = self._resume_target
            return RelativePose(target.x_cm, target.y_cm, target.heading_deg)
        manual = self._manual
        if manual is None or manual.capture_pending:
            return None
        if manual.hold_pose is None:
            # Taken over on the ground: the plan starts at the takeoff point.
            return RelativePose()
        pose = manual.hold_pose
        return RelativePose(pose.x_cm, pose.y_cm, pose.heading_deg)

    def _manual_deadman_due_locked(self, now: float) -> bool:
        """Trip the dead-man when no manual command came for ``manual_deadman_s``. Caller holds ``_lock``."""
        manual = self._manual
        if manual is None or manual.deadman or now - manual.last_cmd_s < self.manual_deadman_s:
            return False
        manual.deadman = True
        if self._is_flying():
            # Nothing but a manual step moves a drone under manual control;
            # keep it explicitly held so no sequence is resumed by accident.
            self._held = True
            self._hover_since = None
        return True

    # ------------------------------------------------------------------
    # Replies
    # ------------------------------------------------------------------

    def _pose_fields(self) -> dict[str, float]:
        pose = self.envelope.pose_snapshot()
        return {"x_cm": _round(pose.x_cm), "y_cm": _round(pose.y_cm), "heading_deg": _round(pose.heading_deg)}

    def _manual_ack(self, msg: dict[str, Any], **fields: Any) -> None:
        if not self._is_v2(msg):
            return
        try:
            reply = make_manual_ack(msg["type"], self.drone_id, msg["cmd_id"], control_mode=self.control_mode, **fields)
        except ProtocolError as exc:
            self._log.error("edge_reply_invalid", drone_id=self.drone_id, type=msg.get("type"), error=str(exc)[:120])
            reply = None
        self._reply(msg, reply)

    def _manual_nack(self, msg: dict[str, Any], reason: str, **fields: Any) -> None:
        if not self._is_v2(msg):
            return
        detail = fields.pop("detail", None)
        if detail is not None:
            detail = "".join(ch for ch in str(detail) if ch.isprintable())[:MAX_TEXT_LEN] or None
        try:
            reply = make_manual_nack(
                msg["type"], self.drone_id, msg["cmd_id"], reason,
                control_mode=self.control_mode, detail=detail, **fields,
            )
        except ProtocolError as exc:
            self._log.error("edge_reply_invalid", drone_id=self.drone_id, type=msg.get("type"), error=str(exc)[:120])
            reply = None
        self._reply(msg, reply)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch_manual(self, msg: dict[str, Any]) -> None:
        """Run one manual command (already through the cmd_id cache and the authority)."""
        try:
            validate_manual_command(msg)
        except ProtocolError as exc:
            self._log.warning("edge_manual_refused", drone_id=self.drone_id, type=msg.get("type"),
                              reason="invalid", error=str(exc)[:120])
            self._manual_nack(msg, "invalid", detail=str(exc))
            return
        mtype = msg.get("type")
        if mtype == MANUAL_HOLD:
            self.handle_manual_hold(msg)
        elif mtype == MANUAL_MOVE:
            self.handle_manual_move(msg)
        elif mtype == MANUAL_TAKEOFF:
            self.handle_manual_takeoff(msg)
        elif mtype == RESUME_SEARCH:
            self.handle_resume_search(msg)

    # ------------------------------------------------------------------
    # manual_hold
    # ------------------------------------------------------------------

    def handle_manual_hold(self, msg: dict[str, Any]) -> bool:
        """Take manual control: pause the search and hover in place (see the module docstring)."""
        refusal = None
        detail = None
        entered = False
        airborne = False
        with self._lock:
            flying = self._is_flying()
            if self._landing_wanted_locked():
                refusal = "landing"
            else:
                cancelled = self._worker_alive() and self._phase == "pre_takeoff" and self._active_kind != "manual_takeoff"
                if cancelled:
                    # A scheduled start or takeoff that has not been sent never will be.
                    self._cancel = ("busy", "cancelled_by_manual_hold")
                    self._abort.set()
                airborne = flying or (self._worker_alive() and self._phase == "taking_off")
                if self._manual is None:
                    entered = True
                    now = self._clock()
                    self._manual = ManualState(since_s=now, last_cmd_s=now)
                    if airborne:
                        if not self._held:
                            self._parked = self._resume_kind_locked(cancelled)
                        self._held = True
                        self._hover_since = None
                        if self._worker_alive() and not cancelled:
                            # The running sequence stops after its move in
                            # flight; the hold point is recorded then.
                            self._manual.capture_pending = True
                        else:
                            self._capture_hold_point_locked()
                    if self._moving:
                        detail = "finishing_move"
                    elif cancelled:
                        detail = "start_cancelled"
                    elif not airborne:
                        detail = "on_ground"
                else:
                    detail = "already_manual"
                self._manual_note_command_locked()
                lane_index = self.lane_index
        if refusal is not None:
            self._log.warning("edge_manual_hold_refused", drone_id=self.drone_id, reason=refusal, cmd_id=msg.get("cmd_id"))
            self._manual_nack(msg, refusal)
            return False
        pose = self._pose_fields()
        self._log.warning(
            "edge_manual_hold",
            drone_id=self.drone_id,
            cmd_id=msg.get("cmd_id"),
            entered=entered,
            airborne=airborne,
            detail=detail,
            lane_index=lane_index,
            plan_id=self.plan_id,
            **pose,
        )
        self._manual_ack(msg, detail=detail, **pose)
        return True

    # ------------------------------------------------------------------
    # manual_move
    # ------------------------------------------------------------------

    def _manual_limit(self, direction: str, amount: int, height: Any) -> tuple[str, dict[str, Any]] | None:
        """The limit a manual step would break, as ``(reason, fields)``, or None. Caller holds ``_lock``."""
        envelope = self.envelope
        if direction in HORIZONTAL_DIRECTIONS:
            if envelope.check_move(direction, amount):
                return None
            x_cm, y_cm = envelope.move_endpoint(direction, amount)
            fence = envelope.geofence_cm
            return "geofence", {
                "geofence_cm": fence,
                "x_cm": _round(x_cm),
                "y_cm": _round(y_cm),
                "detail": (
                    f"{direction} {amount} cm would reach ({x_cm:.0f}, {y_cm:.0f}) cm from the takeoff point, "
                    f"outside the {format_cm(fence)} cm geofence"
                ),
            }
        if direction == "up":
            limit = envelope.max_alt_slot_cm
            if not is_finite_number(height):
                return "ceiling", {"max_alt_slot_cm": limit, "detail": "height unknown, so climbing is refused"}
            if envelope.climb_allowed(height, amount):
                return None
            return "ceiling", {
                "height_cm": height,
                "max_alt_slot_cm": limit,
                "detail": (
                    f"up {amount} cm from {format_cm(height)} cm would end above the ceiling limit "
                    f"{format_cm(limit)} cm (ceiling {format_cm(envelope.ceiling_cm)} cm minus margin "
                    f"{format_cm(envelope.ceiling_margin_cm)} cm)"
                ),
            }
        if direction == "down":
            if not is_finite_number(height):
                return "floor", {"floor_cm": MANUAL_FLOOR_CM, "detail": "height unknown, so descending is refused"}
            if height - amount >= MANUAL_FLOOR_CM:
                return None
            return "floor", {
                "height_cm": height,
                "floor_cm": MANUAL_FLOOR_CM,
                "detail": f"down {amount} cm from {format_cm(height)} cm would end below the {MANUAL_FLOOR_CM} cm floor; use Land",
            }
        return None  # a rotation changes no position or height

    def handle_manual_move(self, msg: dict[str, Any]) -> bool:
        """Fly one bounded manual step after checking every limit (see the module docstring)."""
        direction = msg.get("direction")
        rotation = direction in MANUAL_ROTATIONS
        amount = int(msg.get("degrees") if rotation else msg.get("distance_cm"))
        # Read before the executor lock: the controller takes its own locks.
        link = self._drone_link()
        height = self._read_height() if direction in ("up", "down") else None
        refusal = None
        fields: dict[str, Any] = {}
        with self._lock:
            self._manual_note_command_locked()
            if self._manual is None:
                refusal = "not_manual"
                fields["detail"] = "take control of this drone first"
            elif self._landing_wanted_locked():
                refusal = "landing"
            elif not self._is_flying():
                refusal = "not_flying"
                fields["detail"] = "the drone is on the ground; use Takeoff"
            elif link is not None and not link.ok:
                refusal = "drone_unreachable"
                fields["detail"] = link.detail or "drone unreachable"
            elif self._worker_alive():
                refusal = "busy"
                fields["detail"] = "the previous step is still being flown"
            else:
                limit = self._manual_limit(direction, amount, height)
                if limit is not None:
                    refusal, fields = limit
                elif not self._launch("manual", cmd=msg, payload=(direction, amount)):
                    refusal = "busy" if self._closed else "landing"
        if refusal is not None:
            self._log.warning(
                "edge_manual_move",
                drone_id=self.drone_id,
                cmd_id=msg.get("cmd_id"),
                direction=direction,
                amount=amount,
                outcome="refused",
                reason=refusal,
                detail=fields.get("detail"),
            )
            self._manual_nack(msg, refusal, **fields)
            return False
        return True

    def _run_manual_move(self, cmd: dict[str, Any], payload: tuple[str, int]) -> None:
        """Fly an accepted manual step on the worker thread and answer it."""
        direction, amount = payload
        rotation = direction in MANUAL_ROTATIONS
        if not self._claim_move(allow_held=True):
            reason, detail = self._cancel_reason() if self._abort.is_set() else ("busy", "held")
            self._manual_nack(cmd, reason, detail=detail)
            return
        try:
            if rotation:
                degrees = amount if direction == "cw" else -amount
                result = command_result(self.drone.rotate(degrees), "rotate")
            else:
                result = command_result(self.drone.move(direction, amount), "move")
        finally:
            self._end_move()
        if not result.ok:
            # Not flown: the pose is unchanged and the drone hovers.
            outcome = result.outcome.value
            self._log.warning("edge_manual_move", drone_id=self.drone_id, cmd_id=cmd.get("cmd_id"),
                              direction=direction, amount=amount, outcome="not_executed", detail=outcome)
            if self._abort.is_set():
                reason, detail = self._cancel_reason()
                self._manual_nack(cmd, reason, detail=detail)
            else:
                self._manual_nack(cmd, "busy", detail=outcome)
            return
        # The controller reported the step executed: record it like a lane
        # move (a vertical step changes no position; height is measured).
        if rotation:
            self.envelope.record_rotation(amount if direction == "cw" else -amount)
        elif direction in HORIZONTAL_DIRECTIONS:
            self.envelope.record_move(direction, amount)
        if self._abort.is_set():
            reason, detail = self._cancel_reason()
            self._log.warning("edge_manual_move", drone_id=self.drone_id, cmd_id=cmd.get("cmd_id"),
                              direction=direction, amount=amount, outcome="interrupted", reason=reason)
            self._manual_nack(cmd, reason, detail=detail)
            return
        pose = self._pose_fields()
        self._log.info("edge_manual_move", drone_id=self.drone_id, cmd_id=cmd.get("cmd_id"),
                       direction=direction, amount=amount, outcome="ok", **pose)
        self._manual_ack(cmd, **pose)

    # ------------------------------------------------------------------
    # manual_takeoff
    # ------------------------------------------------------------------

    def handle_manual_takeoff(self, msg: dict[str, Any]) -> bool:
        """Take off under manual control: the autonomous preflight, then the climb to the slot."""
        link = self._drone_link()
        refusal = None
        detail = None
        with self._lock:
            self._manual_note_command_locked()
            if self._manual is None:
                refusal, detail = "not_manual", "take control of this drone first"
            elif self._landing_wanted_locked():
                refusal = "landing"
            elif self._is_flying():
                refusal, detail = "busy", "already_flying"
            elif self._worker_alive():
                refusal = "busy"
            elif link is not None and not link.ok:
                # Checked again right before the takeoff (``_do_takeoff``).
                refusal, detail = "drone_unreachable", link.detail or "drone unreachable"
            elif not self._launch("manual_takeoff", cmd=msg):
                refusal = "busy" if self._closed else "landing"
        if refusal is not None:
            self._log.warning("edge_manual_takeoff_refused", drone_id=self.drone_id, reason=refusal,
                              detail=detail, cmd_id=msg.get("cmd_id"))
            self._manual_nack(msg, refusal, detail=detail)
            return False
        return True

    def _run_manual_takeoff(self, cmd: dict[str, Any]) -> None:
        if not self._takeoff_or_refuse(cmd):
            return
        with self._lock:
            if self._manual is not None:
                # A new flight from a new takeoff point: an earlier hold point
                # no longer means anything (the plan ended at the landing).
                self._manual.hold_pose = None
                self._manual.capture_pending = False
            self._held = True
            self._parked = "hover"
            self._hover_since = None
            if self._is_active():
                self._phase = "climbing"
        if not self._climb_to_slot(allow_held=True):
            reason, detail = self._cancel_reason()
            self._manual_nack(cmd, reason, detail=detail or "climb to the altitude slot not completed")
            return
        height = self._read_height()
        self._log.warning("edge_manual_takeoff", drone_id=self.drone_id, cmd_id=cmd.get("cmd_id"),
                          height_cm=height, alt_slot_cm=self.envelope.alt_slot_cm)
        self._manual_ack(cmd, height_cm=height if is_finite_number(height) else None, **self._pose_fields())

    # ------------------------------------------------------------------
    # resume_search
    # ------------------------------------------------------------------

    def _return_path_locked(self, target: RelativePose) -> list[tuple[str, int]] | None:
        """Steps from the current pose back to *target*, inside the geofence, or None. Caller holds ``_lock``.

        A rotation to the target heading, then a move along that heading's
        forward axis and one along its right axis (each split into Tello
        moves), in whichever order keeps every point inside the geofence.
        Exact paths (short components flown as an overshoot and a return) are
        tried first, then paths that skip components under 20 cm.
        """
        pose = self.envelope.pose_snapshot()
        steps: list[tuple[str, int]] = []
        turn = round(((target.heading_deg - pose.heading_deg + 180.0) % 360.0) - 180.0)
        if turn:
            steps.append(("rotate", turn))
        heading = (pose.heading_deg + turn) % 360.0
        rad = math.radians(heading)
        dx, dy = target.x_cm - pose.x_cm, target.y_cm - pose.y_cm
        right = dx * math.cos(rad) - dy * math.sin(rad)
        forward = dx * math.sin(rad) + dy * math.cos(rad)
        for exact in (True, False):
            ahead = _axis_moves(forward, "forward", "back", exact=exact)
            side = _axis_moves(right, "right", "left", exact=exact)
            for order in (ahead + side, side + ahead):
                sim = RelativePose(pose.x_cm, pose.y_cm, heading)
                inside = True
                for direction, distance in order:
                    sim.apply_move(direction, distance)
                    if not self.envelope.inside_fence(sim.x_cm, sim.y_cm):
                        inside = False
                        break
                if inside:
                    return steps + order
        return None

    def handle_resume_search(self, msg: dict[str, Any]) -> bool:
        """End manual control and continue the search from the hold point (see the module docstring)."""
        refusal = None
        detail = None
        fields: dict[str, Any] = {}
        released = False
        with self._lock:
            flying = self._is_flying()
            manual = self._manual
            if manual is None:
                detail = "not_manual"
            elif self._landing_wanted_locked():
                refusal = "landing"
            elif self._worker_alive():
                refusal, detail = "busy", "a manual step or takeoff is still being flown"
            elif not (self._plan_valid and self._step < len(self._moves)):
                self._release_manual_locked()
                released, detail = True, "no_plan"
            elif not flying:
                self._release_manual_locked()
                released, detail = True, "on_ground"
            else:
                point = self._resume_point_locked() or self.envelope.pose_snapshot()
                fence = self.envelope.geofence_cm
                target = RelativePose(
                    max(-fence, min(fence, point.x_cm)), max(-fence, min(fence, point.y_cm)), point.heading_deg
                )
                clamped = (target.x_cm, target.y_cm) != (point.x_cm, point.y_cm)
                path = self._return_path_locked(target)
                fields = {"target_x_cm": _round(target.x_cm), "target_y_cm": _round(target.y_cm)}
                if path is None:
                    refusal = "geofence"
                    detail = "the way back to the resume point would leave the geofence; fly closer and try again"
                elif self._path_outside(self._moves, self._step, target) is not None:
                    refusal = "geofence"
                    detail = "the rest of the plan from the resume point would leave the geofence"
                elif not self._launch("return", cmd=msg, payload=(target, path)):
                    refusal = "busy" if self._closed else "landing"
                else:
                    self._release_manual_locked()
                    self._resume_target = target
                    released = True
                    detail = "returning_nearest" if clamped else "returning"
            lane_index = self.lane_index
        if refusal is not None:
            self._log.warning("edge_resume_search_refused", drone_id=self.drone_id, reason=refusal, detail=detail,
                              cmd_id=msg.get("cmd_id"), **fields)
            self._manual_nack(msg, refusal, detail=detail, **fields)
            return False
        self._log.warning(
            "edge_resume_search",
            drone_id=self.drone_id,
            cmd_id=msg.get("cmd_id"),
            released=released,
            detail=detail,
            lane_index=lane_index,
            plan_id=self.plan_id,
            **fields,
        )
        if released:
            self._log.warning("edge_manual_released", drone_id=self.drone_id, reason="resume_search", detail=detail)
        self._manual_ack(msg, detail=detail, **fields)
        return True

    def _command_rotate(self, degrees: int) -> bool:
        """Claim and fly one rotation; False if the sequence must stop (landing or hold)."""
        if not self._claim_move():
            return False
        try:
            result = command_result(self.drone.rotate(degrees), "rotate")
        finally:
            self._end_move()
        if not result.ok:
            if self._abort.is_set():
                return False
            raise MoveNotExecutedError(
                f"rotate {degrees} degrees was not executed by the drone controller ({result.outcome.value})"
            )
        self.envelope.record_rotation(degrees)
        return not self._abort.is_set()

    def _run_return(self, cmd: dict[str, Any], payload: tuple[RelativePose, list[tuple[str, int]]]) -> None:
        """Fly back to the hold point, then continue the plan (the ``resume_search`` sequence)."""
        target, path = payload
        # Altitude first: the way back is flown in the drone's own slot.
        if not self._climb_to_slot():
            return
        for direction, amount in path:
            if direction == "rotate":
                if not self._command_rotate(amount):
                    return
                continue
            if not self.envelope.check_move(direction, amount):
                pose = self.envelope.pose_snapshot()
                self._log.error("edge_geofence_refused", drone_id=self.drone_id, direction=direction,
                                distance_cm=amount, x_cm=_round(pose.x_cm), y_cm=_round(pose.y_cm), source="return")
                self.land_now("geofence")
                return
            if not self._command_move(direction, amount, record=True):
                return
        with self._lock:
            if not self._is_active():
                return
            self._resume_target = None
        pose = self._pose_fields()
        self._log.info("edge_resume_search_returned", drone_id=self.drone_id, target_x_cm=_round(target.x_cm),
                       target_y_cm=_round(target.y_cm), lane_index=self.lane_index, **pose)
        self._fly_plan()
