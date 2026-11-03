"""Flight sequences of the edge lane executor, run on its worker thread.

``LaneExecutor`` (``flightrisk.drone.lane_executor``) inherits
``FlightSequenceMixin``. One flight sequence runs at a time, on its own
thread; ``_launch`` starts it under the executor lock. The kinds are:

* ``start_at``: wait for the scheduled instant, take off unless flying,
  acknowledge the start in ground time, then fly the lanes;
* ``takeoff``: take off, then climb to the altitude slot and hover;
* ``lanes``: fly the current plan from its current step (a re-task, or a
  ``resume`` after a hold);
* ``climb``: climb to the altitude slot (a ``resume`` of a hold that
  stopped a takeoff before its climb);
* ``nudge``: one operator move (see ``flightrisk.drone.lane_commands``);
* ``manual``, ``manual_takeoff`` and ``return``: a manual step, a manual
  takeoff, and the way back to the hold point followed by the rest of the
  plan (see ``flightrisk.drone.lane_manual``).

Every move, and a takeoff, is *claimed* under the executor lock immediately
before it is sent (``_claim_move``). A landing (the abort flag) or a hold
makes the claim fail, so the sequence stops before its next move and never
interrupts the move already with the drone; a hold then releases the
sequence's slot so ``resume`` can start it again from the same step. No
sequence is launched once a landing has been requested (``_launch``).

Obstacle sensing (design ``.claude/build/obstacle/design.md`` sections 3 and
4). With an obstacle sensor the lanes are split into sensed steps
(``lane_plan``) and ``_fly_plan`` runs, before every step:

1. **Nose first.** The drone turns to face the step's plan direction
   (``forward`` 0, ``right`` 90, ``back`` 180, ``left`` 270 degrees from the
   takeoff heading) by the shortest rotation, recorded in the dead-reckoned
   pose, and flies the step as a body ``forward`` move, which is the only
   direction the camera sees. It turns only when the next step needs another
   heading. With turning disabled (``FLIGHTRISK_OBSTACLE_TURN=0``) plan
   directions are flown as body directions as before and only ``forward``
   steps are sensed.
2. **The sense gate.** ``sensor.sense(after, timeout_s)`` judges a view newer
   than the end of the last move or turn plus the settle time (and newer
   than the gate itself). The call runs on a helper thread that is abandoned
   if it hangs. Only ``clear`` lets the step be claimed and sent; nothing is
   sent after any other result. A drone that measures its own heading in
   the plan frame (``measured_heading_deg()``, an autopilot's compass) must
   also face the commanded heading within ``HEADING_TOLERANCE_DEG``, before
   and after the look: otherwise the gate's result is ``unavailable``
   (reason ``stale``, cause ``heading not reached``), so the camera never
   clears a step it did not face. Such a drone that already has the
   commanded heading in its pose but measures another one (lane 0 when an
   autopilot's plan frame is not its takeoff yaw) is asked once per step to
   turn to it (``rotate(0)``); a turn it reports as not executed while it
   still measures a heading is a turn that did not finish, and cuts the lane
   with ``sensor_stale`` instead of landing. A drone without that
   measurement (the Tello, whose ``cw``/``ccw`` reply arrives once the turn
   is flown) is gated exactly as before.
3. **The cut policy.** ``blocked`` cuts the lane where the drone hovers: the
   rest of the lane and of every following lane in the same direction is
   removed from the plan, the cut ``(lane, flown_cm, reason)`` is recorded
   and reported in telemetry, and later lanes are re-planned with the
   deficit rule (``lane_steps``), which never lengthens a lane. The drone
   never lands for an obstacle and continues with the next lane.
4. **Fail-safe.** A sensor that cannot look (stale frame, timeout, error) is
   asked again up to ``OBSTACLE_SENSE_RETRIES`` times, hovering
   ``obstacle_retry_pause_s`` between attempts, and then cuts the lane with
   reason ``sensor_stale``. Once it has been unable to look for
   ``OBSTACLE_SENSOR_DOWN_S``, the remaining lanes are ended with reason
   ``sensor_down`` and the plan's ``on_complete`` runs. The sensor's down
   timer is looked at again when the drone lands and when a plan that does
   not continue the current one is accepted: one probe ``sense`` call, and a
   view (``clear`` or ``blocked``) clears the timer, so
   ``obstacle_sensor_ok`` describes the sensor as it is now.

The way back of a ``resume_search`` (``lane_manual``) is flown through the
same turns and gates in sensed steps (``_fly_return_sensed``); a gate that
does not clear cuts the lane the search resumes with and ends the way back.

A hold, a manual takeover, a link-loss hold or a landing that arrives during
a gate stops the sequence before its next step exactly as before: the gate
runs before the claim, and the claim is what sends. A re-task during a gate
makes the executor look again for the new plan. A lane move that an
autopilot with onboard avoidance stopped short (``STOPPED_SHORT``) is
recorded as far as the drone's measured position says it got and cuts the
lane with reason ``autopilot``.
"""
from __future__ import annotations

import inspect
import math
import threading
import time
from collections.abc import Callable
from typing import Any

from flightrisk.drone.controller import CommandOutcome, command_result
from flightrisk.drone.lane_steps import (
    Cut,
    plan_heading,
    replay_lanes,
    shortest_rotation,
)
from flightrisk.drone.obstacle_sensor import (
    CUE_AUTOPILOT,
    CUT_AUTOPILOT,
    CUT_SENSOR_DOWN,
    CUT_SENSOR_STALE,
    EVENT_LANE_CUT,
    EVENT_LANES_ENDED_SENSOR_DOWN,
    EVENT_OBSTACLE_GATE,
    EVENT_OBSTACLE_STOP,
    EVENT_OBSTACLE_TURN,
    EVENT_SENSE_STALE,
    EVENT_SENSE_TIMEOUT,
    EVENT_SENSOR_ERROR,
    EVENT_START_REFUSED,
    OBSTACLE_SENSE_RETRIES,
    OBSTACLE_SENSOR_DOWN_S,
    OBSTACLE_SETTLE_S,
    OBSTACLE_STATE_BLOCKED,
    OBSTACLE_STATE_CLEAR,
    OBSTACLE_STATE_UNAVAILABLE,
    REASON_ERROR,
    REASON_STALE,
    REASON_TIMEOUT,
    START_REFUSED_REASON,
    STATE_BLOCKED,
    STATE_CLEAR,
    SenseResult,
)
from flightrisk.drone.safety import ROOM_LIMITS, CeilingError

__all__ = [
    "EVENT_SENSOR_PROBE",
    "HEADING_NOT_REACHED",
    "HEADING_TOLERANCE_DEG",
    "OBSTACLE_RETRY_PAUSE_S",
    "FlightSequenceMixin",
    "MoveNotExecutedError",
]

#: Seconds the drone hovers between two attempts at a gate whose sensor
#: could not look, so a sensor that fails at once still takes about as long
#: as a stale one (``OBSTACLE_SENSE_RETRIES`` attempts, about 3 s in all).
OBSTACLE_RETRY_PAUSE_S = 0.5
#: A drone that measures its heading (``measured_heading_deg()``) may have a
#: step cleared only while that heading is within this many degrees of the
#: commanded heading, so its forward camera looks where the step goes.
HEADING_TOLERANCE_DEG = 10.0
#: ``SenseResult.detail["cause"]`` of a gate that did not judge a view
#: because the drone did not face the commanded heading (an ``unavailable``
#: result with reason ``stale``), and of a turn that did not finish.
HEADING_NOT_REACHED = "heading not reached"
#: Logged once per probe of a sensor whose down timer runs (on a landing, or
#: when a plan that does not continue the current one is accepted).
EVENT_SENSOR_PROBE = "edge_obstacle_sensor_probe"
# How much longer than its own timeout a sense call may run before the
# executor abandons its helper thread and treats the gate as timed out.
_SENSE_JOIN_GRACE_S = 0.5
# Verdicts of a step's preparation (turn and gate) in ``_fly_plan`` and the
# sensed way back of ``resume_search`` (``lane_manual``).
_GO = "go"  # fly the step now
_AGAIN = "again"  # look at the plan again (after a turn, a cut, a re-task)
_STOP = "stop"  # the sequence ends (landing, hold)
_RETRY = "retry"  # ask the sensor again (inside the gate only)
_CUT = "cut"  # the gate or a turn that did not finish cut the lane
_OBSTACLE_STATES = {
    STATE_CLEAR: OBSTACLE_STATE_CLEAR,
    STATE_BLOCKED: OBSTACLE_STATE_BLOCKED,
}

# Below this much remaining time the start wait switches from event waits to
# short sleeps, so takeoff fires close to the scheduled instant.
_START_SPIN_S = 0.02
# Phase of each flight sequence when it is launched.
_FIRST_PHASE = {
    "start_at": "pre_takeoff",
    "takeoff": "pre_takeoff",
    "lanes": "lanes",
    "climb": "climbing",
    "nudge": "nudge",
    "manual": "manual",
    "manual_takeoff": "pre_takeoff",
    "return": "returning",
}


class MoveNotExecutedError(RuntimeError):
    """The drone controller reported a move as not executed (it was not flown)."""


class FlightSequenceMixin:
    """Flight sequences for ``LaneExecutor`` (see the module docstring)."""

    def _launch(
        self,
        kind: str,
        t_edge: float | None = None,
        offset_s: float = 0.0,
        cmd: dict[str, Any] | None = None,
        nudge: tuple[str, int, bool] | None = None,
        payload: Any = None,
    ) -> bool:
        """Start a flight sequence thread. Caller holds ``_lock``.

        Returns False (and starts nothing) once the executor is shut down,
        and while a landing is wanted (``_landing_wanted_locked``): from the
        moment ``land_now`` is called until that landing is over. Every
        caller answers a False with a refusal (``landing``, or ``busy`` once
        shut down), never with an ack.
        """
        if self._closed:
            return False
        # Clear the abort flag before looking for a landing. land_now counts
        # itself in ``_land_requests`` before it sets the flag, so either the
        # check below sees that landing or the flag is set after this clear.
        self._abort.clear()
        if self._landing_wanted_locked():
            self._abort.set()
            self._log.warning("edge_launch_refused", drone_id=self.drone_id, kind=kind, reason="landing")
            return False
        hook = self._after_launch_check
        if hook is not None:
            hook()
        self._cancel = None
        # A new sequence may ask the drone once more to turn to its commanded
        # heading before a step it already asked for (``_face``).
        self._realigned = None
        self._worker = threading.Thread(
            target=self._run_sequence,
            args=(kind, t_edge, offset_s, cmd, nudge, payload),
            daemon=True,
            name=f"{self.drone_id}-flight",
        )
        self._active_thread = self._worker
        self._active_kind = kind
        self._phase = _FIRST_PHASE[kind]
        self._hover_since = None
        self._worker.start()
        return True

    def _is_active(self) -> bool:
        """True if the calling thread runs the current sequence. Caller holds ``_lock``."""
        return self._active_thread is threading.current_thread()

    def _release_active(self) -> None:
        """Mark the calling sequence as finished. Caller holds ``_lock``.

        Under manual control this is where the hold point is recorded when
        the sequence that was running at the hold has stopped.
        """
        if self._is_active():
            self._active_thread = None
            self._active_kind = None
            self._phase = None
            manual = self._manual
            if manual is not None and manual.capture_pending:
                self._capture_hold_point_locked()

    def _set_phase(self, phase: str) -> None:
        with self._lock:
            if self._is_active():
                self._phase = phase

    def _claim_move(self, allow_held: bool = False) -> bool:
        """Claim the right to send one move (or takeoff) now.

        False if a landing aborted the sequence, or if a hold or manual
        control is in force (unless *allow_held*, for a nudge flown while
        holding or a manual step): the sequence then stops, releasing its
        slot so ``resume`` or ``resume_search`` can start it again.
        """
        with self._lock:
            if self._abort.is_set() or not self._is_active():
                return False
            if self._link_holding:
                # The ground link is lost and the drone holds position until
                # the link-loss failsafe; the sequence stops before this move.
                self._log.info("edge_link_hold_stopped", drone_id=self.drone_id, lane_index=self.lane_index)
                self._link_parked = True
                self._release_active()
                return False
            if (self._held or self._manual is not None) and not allow_held:
                self._log.info("edge_hold_stopped", drone_id=self.drone_id, lane_index=self.lane_index)
                self._release_active()
                return False
            self._moving = True
            return True

    def _end_move(self) -> None:
        with self._lock:
            self._moving = False
            if self._lane_step_cm() is not None:
                # A sense gate judges only views newer than this.
                self._last_motion_end_s = self._clock()

    def _run_sequence(
        self,
        kind: str,
        t_edge: float | None,
        offset_s: float,
        cmd: dict[str, Any] | None,
        nudge: tuple[str, int, bool] | None,
        payload: Any = None,
    ) -> None:
        try:
            if kind == "start_at":
                self._run_start(t_edge, offset_s, cmd)
            elif kind == "takeoff":
                if self._takeoff_or_refuse(cmd):
                    self._ack(cmd)
                    self._set_phase("climbing")
                    self._climb_to_slot()
            elif kind == "lanes":
                self._fly_plan()
            elif kind == "climb":
                self._climb_to_slot()
            elif kind == "nudge":
                self._run_nudge(cmd, nudge)
            elif kind == "manual":
                self._run_manual_move(cmd, payload)
            elif kind == "manual_takeoff":
                self._run_manual_takeoff(cmd)
            elif kind == "return":
                self._run_return(cmd, payload)
        except Exception as exc:
            self._log.error("edge_executor_error", drone_id=self.drone_id, kind=kind, error=repr(exc))
            self.land_now("executor_error")
            self._nack(cmd, "landing", detail="executor_error")
        finally:
            with self._lock:
                self._release_active()
                if self._held and not self._worker_alive() and not self._is_flying():
                    # A takeoff that was held and then not executed.
                    self._held, self._parked = False, None
                if kind == "nudge":
                    self._continue_retask_locked()

    def _continue_retask_locked(self) -> None:
        """Fly a re-task that arrived during a nudge, as it would have been without one.

        Caller holds ``_lock``. The rest of the plan is checked against the
        geofence from the nudged position first; if it would leave the
        fence nothing is launched and the drone keeps hovering.
        """
        if not (
            self._flying_lanes
            and self._plan_valid
            and not self._held
            and self._manual is None
            and not self._worker_alive()
            and self._land_reason is None
            and self._step < len(self._moves)
            and self._is_flying()
        ):
            return
        if self._path_outside(self._moves, self._step) is not None:
            self._log.warning("edge_retask_not_flown", drone_id=self.drone_id, reason="geofence")
            return
        if not self._launch("lanes"):
            reason = "shut_down" if self._closed else "landing"
            self._log.warning("edge_retask_not_flown", drone_id=self.drone_id, reason=reason)

    def _on_complete_due_locked(self) -> bool:
        """True if the ``on_complete`` landing the calling sequence decided on is still due. Caller holds ``_lock``.

        Checked by ``land_now`` under the lock, so a hold, or a re-task with
        lanes still to fly or another end-of-lanes action, that arrived
        after the last lane was flown stops the landing.
        """
        return (
            self._is_active()
            and not self._held
            and not self._abort.is_set()
            and self._step >= len(self._moves)
            and self._on_complete == "land"
        )

    def _run_start(self, t_edge: float, offset_s: float, cmd: dict[str, Any] | None) -> None:
        """The start_at sequence: wait, take off (unless flying), acknowledge, fly the lanes."""
        if not self._wait_until(t_edge):
            reason, detail = self._cancel_reason()
            self._nack(cmd, reason, **({"detail": detail} if detail else {}))
            return
        t_exec = self._clock()
        if self._is_flying():
            with self._lock:
                started = not self._abort.is_set() and self._is_active()
                if started:
                    self._phase = "lanes"
            if not started:
                reason, detail = self._cancel_reason()
                self._nack(cmd, reason, **({"detail": detail} if detail else {}))
                return
        elif not self._takeoff_or_refuse(cmd, t_exec):
            return
        # The offset is the one the start was scheduled with, so the
        # reported ground time matches the requested t_ground_s.
        if self._is_v2(cmd):
            self._ack(cmd, executed_at_ground_s=t_exec - offset_s, plan_id=self.plan_id)
        else:
            self._emit({
                "type": "ack",
                "for": "start_at",
                "drone_id": self.drone_id,
                "executed_at_ground_s": t_exec - offset_s,
            })
        self._fly_plan()

    def _takeoff_or_refuse(self, cmd: dict[str, Any] | None, t_exec: float | None = None) -> bool:
        """Take off; on refusal answer *cmd* with a nack. True once airborne."""
        refusal = self._do_takeoff(self._clock() if t_exec is None else t_exec)
        if refusal is None:
            return True
        if refusal == "battery":
            reason, detail = "battery", None
        elif refusal == "drone_unreachable":
            reason, detail = "drone_unreachable", self._unreachable_detail or "drone unreachable"
        elif refusal == "link_loss":
            reason, detail = "busy", "link_loss"
        else:
            reason, detail = self._cancel_reason()
            detail = detail or refusal
        self._nack(cmd, reason, **({"detail": detail} if detail else {}))
        return False

    def _wait_until(self, t_edge: float) -> bool:
        """Wait for the edge clock to reach ``t_edge``. False if aborted first."""
        while True:
            if self._abort.is_set():
                return False
            remaining = t_edge - self._clock()
            if remaining <= 0:
                return True
            if remaining > _START_SPIN_S:
                self._abort.wait(min(remaining - _START_SPIN_S / 2, 0.05))
            else:
                self._sleep(0.0005)

    def _do_takeoff(self, t_exec: float) -> str | None:
        """Take off. Returns None once airborne, else why not.

        The reasons are drone_unreachable, battery, link_loss, aborted and
        not_executed. The drone link is checked first, immediately before
        the takeoff is claimed and sent: a drone the controller cannot reach
        (see ``LaneExecutor._drone_link``) is refused with nothing sent and
        nothing changed, so there is no landing to retry afterwards. So is a
        drone whose required obstacle sensor could not be built
        (``drone_unreachable`` with the sensor detail), except for the
        operator's ``manual_takeoff``: manual steps are never sensed.
        """
        link = self._drone_link()
        if link is not None and not link.ok:
            self._unreachable_detail = link.detail or "drone unreachable"
            self._log.warning(
                "edge_takeoff_refused",
                drone_id=self.drone_id,
                reason="drone_unreachable",
                link_reason=link.reason,
                detail=self._unreachable_detail,
                drone_link_age_s=link.age_s,
            )
            return "drone_unreachable"
        if self._obstacle_sensor_error is not None:
            with self._lock:
                kind = self._active_kind
            if kind != "manual_takeoff":
                self._unreachable_detail = self._obstacle_unavailable_detail()
                self._log.warning(
                    EVENT_START_REFUSED,
                    drone_id=self.drone_id,
                    reason=START_REFUSED_REASON,
                    kind=kind,
                    detail=self._unreachable_detail,
                )
                return "drone_unreachable"
        battery = self._read_battery()
        if not self.envelope.can_takeoff(battery):
            self._log.warning(
                "edge_takeoff_refused",
                drone_id=self.drone_id,
                reason="battery",
                battery=battery,
                min_takeoff_battery=self.envelope.min_takeoff_battery,
            )
            return "battery"
        if self.envelope.link_lost():
            # The ground went silent while the start was pending; the
            # watchdog would land the drone straight away, so do not lift.
            self._log.warning(
                "edge_takeoff_refused",
                drone_id=self.drone_id,
                reason="link_loss",
                link_age_s=round(self.envelope.link_age_s(), 3),
            )
            return "link_loss"
        previous_pose = None
        with self._lock:
            # The takeoff is claimed here: a hold that arrives from now on
            # lets it complete and stops the sequence before its next move.
            if self._abort.is_set() or not self._is_active() or self._land_inflight:
                return "aborted"
            if self._manual is not None and self._active_kind != "manual_takeoff":
                # Under manual control only the operator's takeoff lifts the drone.
                return "aborted"
            if not self._is_flying():
                # No landing was requested since this sequence started (that
                # would have set the abort flag), so any earlier one is
                # finished: the monitor must not "retry" it on the new
                # flight, nor take a takeoff that fails for a landing.
                self._land_reason = None
                self._was_flying = False
                # A new flight starts here, so this spot becomes the takeoff
                # point: the pose and the geofence are relative to it. The
                # pose of the last flight ended wherever that flight landed,
                # and the edge cannot see whether the drone was carried back
                # to its start spot since; keeping it would shift the fence
                # by that distance. Reset before the takeoff is sent, so a
                # takeoff that executes but reports a failure still starts
                # from the right origin.
                previous_pose = self.envelope.reset_pose()
            self._flying_lanes = False
            self._phase = "taking_off"
            self._moving = True
        if previous_pose is not None and (previous_pose.x_cm or previous_pose.y_cm):
            self._log.info(
                "edge_pose_reset",
                drone_id=self.drone_id,
                previous_x_cm=round(previous_pose.x_cm, 1),
                previous_y_cm=round(previous_pose.y_cm, 1),
            )
        try:
            result = command_result(self._drone_takeoff(), "takeoff")
        finally:
            self._end_move()
        if not result.ok:
            # Typically a landing started after the check above: the
            # controller checked the abort flag under its command lock and
            # sent nothing. Any other result that was not executed is
            # treated the same way, so the drone is never recorded as
            # airborne when it did not take off.
            self._log.warning(
                "edge_takeoff_refused",
                drone_id=self.drone_id,
                reason="landing",
                outcome=result.outcome.value,
            )
            return "not_executed"
        with self._lock:
            self._takeoff_at_local_s = t_exec
            # Seen by the monitor's unrequested-landing check from now on.
            self._was_flying = True
        self._log.info("edge_takeoff", drone_id=self.drone_id, battery=battery, at_local_s=t_exec)
        return "aborted" if self._abort.is_set() else None

    def _drone_takeoff(self) -> Any:
        """Call ``drone.takeoff``, passing the abort flag when the controller takes one.

        ``TelloController`` and ``FakeTelloController`` accept ``abort`` and
        check it under their command lock immediately before sending the
        takeoff, so a ``land_now`` that runs between the abort check in
        ``_do_takeoff`` and the takeoff itself cannot be followed by a
        takeoff. They return False when they refuse. The raw return value is
        passed back for ``command_result()`` to normalize.
        """
        takeoff = self.drone.takeoff
        try:
            accepts_abort = "abort" in inspect.signature(takeoff).parameters
        except (TypeError, ValueError):
            accepts_abort = False
        if accepts_abort:
            return takeoff(abort=self._abort)
        return takeoff()

    def _climb_to_slot(self, allow_held: bool = False) -> bool:
        """Move vertically to the altitude slot, measured from the Tello's own height reading.

        Lands if the height is unknown, and lands instead of climbing when
        the slot, or the climb from the measured height, would end above
        the ceiling limit (``SafetyEnvelope.max_alt_slot_cm``). The edge
        enforces that limit itself, whatever the ground sent.
        """
        height = self._read_height()
        try:
            move = self.envelope.slot_move(height)
        except CeilingError as exc:
            self._refuse_climb(height, None, str(exc))
            return False
        except ValueError as exc:
            self._log.error("edge_altitude_unknown", drone_id=self.drone_id, error=str(exc))
            self.land_now("height_unknown")
            return False
        if move is None:
            return True
        direction, distance = move
        if direction == "up" and not self.envelope.climb_allowed(height, distance):
            self._refuse_climb(height, distance, "climb would end above the ceiling limit")
            return False
        self._log.info(
            "edge_alt_slot_climb",
            drone_id=self.drone_id,
            height_cm=height,
            alt_slot_cm=self.envelope.alt_slot_cm,
            direction=direction,
            distance_cm=distance,
        )
        return self._command_move(direction, distance, record=False, allow_held=allow_held)

    def _refuse_climb(self, height: Any, distance: int | None, detail: str) -> None:
        """Do not climb above the ceiling limit: log it and land through the normal landing path."""
        status = self.envelope.ceiling_status()
        self._log.error(
            "slot_refused_above_ceiling",
            drone_id=self.drone_id,
            source="climb",
            height_cm=height,
            distance_cm=distance,
            alt_slot_cm=status["alt_slot_cm"],
            ceiling_cm=status["ceiling_cm"],
            ceiling_margin_cm=status["ceiling_margin_cm"],
            max_alt_slot_cm=self.envelope.max_alt_slot_cm,
            detail=detail,
        )
        self.land_now("ceiling")

    def _command_move(
        self, direction: str, distance: int, record: bool, allow_held: bool = False, lane: bool = False
    ) -> bool:
        """Claim and fly one move; False if the sequence must stop (landing or hold).

        For a *lane* move, a ``STOPPED_SHORT`` result (an autopilot's own
        avoidance held the drone back) is not an error: the part flown is
        recorded and ``_short_stop`` is set for ``_fly_plan`` to cut the lane.
        """
        if not self._claim_move(allow_held=allow_held):
            return False
        start = self.envelope.pose_snapshot() if lane else None
        # Every return value is normalized (None/True: executed; False or a
        # non-OK CommandResult: not executed), so a move the controller did
        # not fly is never recorded in the dead-reckoned pose.
        try:
            result = command_result(self.drone.move(direction, distance), "move")
        finally:
            self._end_move()
        if not result.ok:
            if start is not None and record and result.outcome is CommandOutcome.STOPPED_SHORT:
                flown = self._stopped_short_flown(start, direction, distance)
                if flown > 0:
                    self.envelope.record_move(direction, flown)
                self._short_stop = (flown, result.detail)
                return not self._abort.is_set()
            if self._abort.is_set():
                return False
            raise MoveNotExecutedError(
                f"move {direction} {distance} cm was not executed by the drone controller "
                f"({result.outcome.value})"
            )
        if record:
            self.envelope.record_move(direction, distance)
        return not self._abort.is_set()

    def _fly_plan(self) -> None:
        """Fly the current lane plan from its current step, following re-tasks.

        Stops before the next move when a hold arrives (``resume`` starts it
        again from the same step), and runs the plan's ``on_complete``
        landing after the last lane. With an obstacle sensor every step is
        turned to and gated first (see the module docstring); without one the
        moves are flown exactly as they always were.
        """
        with self._lock:
            if self._abort.is_set():
                return
            self._flying_lanes = True
            if self._is_active():
                self._phase = "lanes"
        climbed_gen: int | None = None
        while not self._abort.is_set():
            land_after = False
            with self._lock:
                if not self._is_active():
                    # A sequence that no longer holds the slot sends nothing
                    # more. Checked here so a skipped on_complete landing
                    # always ends the loop.
                    return
                if self._held:
                    # Hold: stop here, after the move just flown.
                    self._log.info("edge_hold_stopped", drone_id=self.drone_id, lane_index=self.lane_index)
                    self._release_active()
                    return
                gen = self._plan_gen
                step = self._moves[self._step] if self._step < len(self._moves) else None
                if step is None and climbed_gen == gen:
                    land_after = self._on_complete == "land"
                    if not land_after:
                        # Done. Release under the lock so a plan that arrives
                        # from now on starts a new sequence.
                        self._release_active()
            if climbed_gen != gen:
                # New plan (or first plan): make sure the drone is at its slot,
                # which a re-task may have changed.
                if not self._climb_to_slot():
                    return
                climbed_gen = gen
                continue
            if step is None:
                self._log.info("edge_lanes_complete", drone_id=self.drone_id, lanes_total=self.lanes_total)
                if land_after and not self.land_now("on_complete", only_if=self._on_complete_due_locked):
                    # A hold or a re-task arrived after the landing was
                    # decided: the top of the loop stops for the hold, or
                    # flies the new plan.
                    continue
                return
            lane_index, _, direction, distance = step
            body = direction
            if self._lane_step_cm() is not None:
                verdict = self._prepare_step(step, gen)
                if verdict == _STOP:
                    return
                if verdict == _AGAIN:
                    continue
                if self._turns_to_face():
                    # The drone faces the step's plan direction.
                    body = "forward"
            if not self.envelope.check_move(body, distance):
                pose = self.envelope.pose_snapshot()
                extra = {} if body == direction else {"heading_deg": round(pose.heading_deg, 1)}
                self._log.error(
                    "edge_geofence_refused",
                    drone_id=self.drone_id,
                    lane_index=lane_index,
                    direction=direction,
                    distance_cm=distance,
                    x_cm=round(pose.x_cm, 1),
                    y_cm=round(pose.y_cm, 1),
                    **extra,
                )
                self.land_now("geofence")
                return
            self._short_stop = None
            if not self._command_move(body, distance, record=True, lane=True):
                return
            short, self._short_stop = self._short_stop, None
            with self._lock:
                same_plan = self._plan_gen == gen or gen in self._linked_gens
                if (
                    same_plan
                    and not self._abort.is_set()
                    and self._step < len(self._moves)
                    and self._moves[self._step][:2] == step[:2]
                ):
                    if short is None:
                        self._step += 1
                    else:
                        self._cut_lane_locked(
                            step, CUT_AUTOPILOT, flown_in_step=short[0], cue=CUE_AUTOPILOT, detail=short[1]
                        )

    # ------------------------------------------------------------------
    # Obstacle sensing: modes, turns, the sense gate and the cut policy
    # ------------------------------------------------------------------

    def _lane_step_cm(self) -> int | None:
        """The sensed step length when lanes are flown in sensed steps, else None.

        Stepped whenever a sensor was given, or sensing is required and the
        sensor could not be built (every gate then fails, so no step is
        ever cleared). Never for the ``off`` override or without a sensor.
        """
        if self._obstacle_sensor is None and self._obstacle_sensor_error is None:
            return None
        return self.obstacle_step_cm

    def _turns_to_face(self) -> bool:
        """True when the drone turns to face every step (stepped sensing with turning on)."""
        return self._obstacle_turn and self._lane_step_cm() is not None

    def _prepare_step(self, step: tuple[int, int, str, int], gen: int) -> str:
        """Turn to face *step* if needed, then gate it: ``_GO``, ``_AGAIN`` or ``_STOP``.

        A turn returns ``_AGAIN`` so the loop reads the plan again (a hold or
        a re-task may have arrived meanwhile) before the gate; so does a cut.
        With turning disabled only ``forward`` steps are gated; the others
        are flown unsensed.
        """
        direction = step[2]
        current = self._plan_step_check(step, gen)
        cut_target = self._plan_step_target(step)
        if self._turns_to_face():
            verdict = self._face(plan_heading(direction), step, (gen, step), current, cut_target)
            if verdict is not None:
                return _AGAIN if verdict == _CUT else verdict
        elif direction != "forward":
            return _GO
        verdict = self._look(step, current, cut_target)
        return _AGAIN if verdict == _CUT else verdict

    def _plan_step_check(self, step: tuple[int, int, str, int], gen: int) -> Callable[[], bool]:
        """A check, run under ``_lock``, that *step* is still the next move of the plan it was read from."""
        return lambda: self._step_current_locked(step, gen)

    @staticmethod
    def _plan_step_target(step: tuple[int, int, str, int]) -> Callable[[], tuple[int, int, str, int] | None]:
        """The step a cut at a plan step's gate applies to: that step."""
        return lambda: step

    def _face(
        self,
        heading: float,
        step: tuple[int, int, str, int],
        key: Any,
        current: Callable[[], bool],
        cut_target: Callable[[], tuple[int, int, str, int] | None],
    ) -> str | None:
        """Turn to plan *heading* before a sensed step: None once the drone faces it, else ``_AGAIN``, ``_CUT`` or ``_STOP``.

        The turn is the shortest rotation from the commanded (dead-reckoned)
        heading. When no turn is due but the drone measures a heading more
        than ``HEADING_TOLERANCE_DEG`` away from the commanded one, it is
        asked once per step (*key*) and sequence to turn to the commanded
        heading (``rotate(0)``): an autopilot whose plan frame is not its
        takeoff yaw faces lane 0 that way, and one that did not finish an
        earlier turn finishes it. A drone that does not measure its heading
        is never asked.
        """
        delta = shortest_rotation(self.envelope.pose_snapshot().heading_deg, heading)
        if not delta:
            if self._realigned == key or self._heading_off() is None:
                return None
            self._realigned = key
        return self._turn(delta, step, current, cut_target)

    def _turn(
        self,
        degrees: int,
        step: tuple[int, int, str, int],
        current: Callable[[], bool],
        cut_target: Callable[[], tuple[int, int, str, int] | None],
    ) -> str:
        """Turn *degrees* to face *step* (claimed like a move): ``_AGAIN``, ``_CUT`` or ``_STOP``.

        A turn the drone reports as not executed while it still measures a
        heading has not finished: the drone does not face the step, so the
        lane is cut with ``sensor_stale`` (``sensor_down`` once the sensor's
        down timer has run out), never cleared and never landed for. Any
        other failed turn raises as before, and the executor lands.
        """
        before = self.envelope.pose_snapshot().heading_deg
        yaw_before = self._measured_yaw()
        outcome: Any = _STOP
        try:
            outcome = self._rotate_for_step(degrees)
        finally:
            after = self.envelope.pose_snapshot().heading_deg
            if after != before or (degrees == 0 and outcome == _AGAIN):
                # The yaw the drone itself reports, before and after, shows
                # how far the dead-reckoned heading drifts (hardware item H3).
                # A turn of 0 degrees is a drone that measures its heading
                # asked to turn to the commanded one.
                self._log.info(
                    EVENT_OBSTACLE_TURN,
                    drone_id=self.drone_id,
                    lane_index=step[0],
                    direction=step[2],
                    degrees=degrees,
                    from_heading_deg=round(before, 1),
                    to_heading_deg=round(after, 1),
                    yaw_before_deg=yaw_before,
                    yaw_after_deg=self._measured_yaw(),
                )
        if isinstance(outcome, str):
            return outcome
        # A turn that did not finish (``outcome`` is its result).
        off = self._heading_off() or {"cause": HEADING_NOT_REACHED}
        result = SenseResult.make_unavailable(
            REASON_STALE, detail={**off, "turn_degrees": degrees, "turn_detail": str(outcome.detail)[:120]}
        )
        now = self._clock()
        pose = self.envelope.pose_snapshot()
        with self._lock:
            self._note_sense_locked(result, 0.0, now)
            if not current():
                verdict = _AGAIN
            else:
                verdict = _CUT
                target = cut_target()
                if target is not None:
                    down = now - self._sensor_down_since >= OBSTACLE_SENSOR_DOWN_S
                    self._cut_lane_locked(target, CUT_SENSOR_DOWN if down else CUT_SENSOR_STALE, result=result)
        self._log.warning(
            EVENT_SENSE_STALE,
            drone_id=self.drone_id,
            lane_index=step[0],
            attempt=0,
            cause=HEADING_NOT_REACHED,
            turn_degrees=degrees,
            turn_detail=result.detail["turn_detail"] or None,
            measured_heading_deg=result.detail.get("measured_heading_deg"),
            commanded_heading_deg=result.detail.get("commanded_heading_deg"),
            heading_deg=round(pose.heading_deg, 1),
        )
        return verdict

    def _rotate_for_step(self, degrees: int) -> str | Any:
        """Claim and send one turn before a sensed step: ``_AGAIN``, ``_STOP``, or the result of a turn that did not finish.

        A turn the controller executed is recorded in the dead-reckoned pose.
        One it reports ``NOT_EXECUTED`` while the drone still measures a
        heading (an autopilot that did not reach the heading by its
        deadline) is returned for ``_turn`` to cut the lane; every other
        failure raises ``MoveNotExecutedError``, as a failed move does.
        """
        if not self._claim_move():
            return _STOP
        try:
            result = command_result(self.drone.rotate(degrees), "rotate")
        finally:
            self._end_move()
        if result.ok:
            self.envelope.record_rotation(degrees)
            return _STOP if self._abort.is_set() else _AGAIN
        if self._abort.is_set():
            return _STOP
        if result.outcome is CommandOutcome.NOT_EXECUTED and self._measured_heading()[1] is not None:
            return result
        raise MoveNotExecutedError(
            f"rotate {degrees} degrees was not executed by the drone controller ({result.outcome.value})"
        )

    def _measured_yaw(self) -> float | None:
        """The yaw the drone reports (``snapshot().yaw_deg``), or None. Never raises."""
        snapshot = getattr(self.drone, "snapshot", None)
        if not callable(snapshot):
            return None
        try:
            reading = snapshot().yaw_deg
            return None if reading is None or reading.value is None else round(float(reading.value), 1)
        except Exception:  # noqa: BLE001 - a log field only
            return None

    def _measured_heading(self) -> tuple[bool, float | None]:
        """``(measures, heading)``: whether the drone measures its plan-frame heading, and the heading now.

        A drone measures it when it has ``measured_heading_deg()`` (the
        MAVLink fleet drone, from the autopilot's compass in the plan frame).
        Its heading is None while it cannot tell (stale telemetry, before the
        first takeoff, or a probe that raised). ``measured_pose()`` is not
        used for this: it falls back to the commanded heading. Never raises.
        """
        probe = getattr(self.drone, "measured_heading_deg", None)
        if not callable(probe):
            return False, None
        try:
            value = probe()
        except Exception:  # noqa: BLE001 - an unreadable heading is not a heading
            return True, None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return True, None
        return True, float(value) % 360.0

    def _heading_off(self) -> dict[str, Any] | None:
        """Why the drone does not face its commanded heading, as a ``SenseResult`` detail, or None when it does.

        None for a drone that does not measure its heading (the Tello: its
        turn reply arrives once the turn is flown). For one that does, the
        measured heading must be within ``HEADING_TOLERANCE_DEG`` of the
        dead-reckoned (commanded) heading; an unknown heading does not face
        anything.
        """
        measures, measured = self._measured_heading()
        if not measures:
            return None
        commanded = self.envelope.pose_snapshot().heading_deg % 360.0
        if measured is not None:
            error = abs((measured - commanded + 180.0) % 360.0 - 180.0)
            if error <= HEADING_TOLERANCE_DEG:
                return None
        return {
            "cause": HEADING_NOT_REACHED,
            "measured_heading_deg": None if measured is None else round(measured, 1),
            "commanded_heading_deg": round(commanded, 1),
            "tolerance_deg": HEADING_TOLERANCE_DEG,
        }

    def _step_current_locked(self, step: tuple[int, int, str, int], gen: int) -> bool:
        """True if *step* is still the next move of the plan it was read from. Caller holds ``_lock``."""
        return (
            (self._plan_gen == gen or gen in self._linked_gens)
            and self._step < len(self._moves)
            and self._moves[self._step] == step
        )

    def _look(
        self,
        step: tuple[int, int, str, int],
        current: Callable[[], bool],
        cut_target: Callable[[], tuple[int, int, str, int] | None],
        source: str | None = None,
    ) -> str:
        """The sense gate before *step*: ``_GO``, ``_CUT``, ``_AGAIN`` or ``_STOP``.

        Only a ``clear`` view, taken while the drone faced its commanded
        heading (``_sense_facing``), for a step that is still *current*,
        returns ``_GO``. ``blocked`` cuts the lane ``cut_target()`` names
        (``_CUT``). A result that is not a view (``unavailable``, or an
        exception) is retried after a short hover, up to
        ``OBSTACLE_SENSE_RETRIES`` attempts, then cuts with ``sensor_stale``;
        once the sensor has been unable to look for
        ``OBSTACLE_SENSOR_DOWN_S``, the remaining lanes are ended with
        ``sensor_down``. When ``cut_target()`` is None (no lane is left to
        cut) those outcomes still return ``_CUT`` and cut nothing. A step
        that is no longer *current* (a re-task replaced it while the sensor
        looked) returns ``_AGAIN``; so does a hold, manual control or a
        link-loss hold between attempts, and the caller stops for it.
        *source* (for example ``return``) is added to the gate's log.
        """
        attempt = 0
        while True:
            attempt += 1
            started = self._clock()
            # A view newer than the end of the last move or turn plus the
            # settle time, and never older than the gate itself.
            after = max(self._last_motion_end_s, started - OBSTACLE_SETTLE_S)
            result, latency_ms = self._sense_facing(after)
            now = self._clock()
            if self._abort.is_set():
                return _STOP
            pose = self.envelope.pose_snapshot()
            with self._lock:
                self._note_sense_locked(result, latency_ms, started)
                verdict, reason = _RETRY, None
                if not current():
                    # A re-task replaced the step while the sensor looked.
                    verdict = _AGAIN
                elif result.clear:
                    verdict = _GO
                elif result.blocked:
                    reason = result.cut_reason
                elif now - self._sensor_down_since >= OBSTACLE_SENSOR_DOWN_S:
                    reason = CUT_SENSOR_DOWN
                elif attempt >= OBSTACLE_SENSE_RETRIES:
                    reason = CUT_SENSOR_STALE
                if reason is not None:
                    verdict = _CUT
                    target = cut_target()
                    if target is not None:
                        self._cut_lane_locked(target, reason, result=result)
                stopping = (
                    self._held or self._manual is not None or self._link_holding or not self._is_active()
                )
            self._log_gate(step, attempt, result, latency_ms, pose, source)
            if verdict != _RETRY:
                return verdict
            if stopping:
                return _AGAIN
            if self._abort.wait(self.obstacle_retry_pause_s):
                return _STOP

    def _sense_facing(self, after: float) -> tuple[SenseResult, float]:
        """One look, valid only while the drone faces its commanded heading. Never raises.

        A drone that measures its heading and does not face the commanded
        one is not asked to look: the result is ``unavailable`` (reason
        ``stale``, cause ``heading not reached``). The heading is read again
        after a ``clear`` view, which counts only if the drone still faced
        it: the view arrived between the two readings. Drones that do not
        measure their heading get the sensor's answer unchanged.
        """
        off = self._heading_off()
        if off is not None:
            return SenseResult.make_unavailable(REASON_STALE, detail=off), 0.0
        result, latency_ms = self._sense_once(after)
        if result.clear:
            off = self._heading_off()
            if off is not None:
                return SenseResult.make_unavailable(REASON_STALE, latency_ms=latency_ms, detail=off), latency_ms
        return result, latency_ms

    def _sense_once(self, after: float) -> tuple[SenseResult, float]:
        """One sense call on a helper thread: ``(result, latency_ms)``. Never raises.

        An exception, or anything that is not a ``SenseResult``, becomes an
        ``unavailable`` result with reason ``error``. A call still running
        ``_SENSE_JOIN_GRACE_S`` after its own timeout is abandoned (its
        daemon thread is left to finish) and becomes ``timeout``, so a hung
        sensor never holds up the flight thread. Without a sensor (it could
        not be built) every call is ``error``.

        Each call gets a fresh daemon thread rather than one reused helper.
        That is safe: a thread costs well under a millisecond against a gate
        of 0.3 s or more, a call that is still running is never reused, and
        an abandoned call writes its late answer into its own ``box`` that
        no later gate reads, so it can never clear another step.
        """
        sensor = self._obstacle_sensor
        if sensor is None:
            return SenseResult.make_unavailable(REASON_ERROR, detail={"error": self._obstacle_sensor_error}), 0.0
        timeout_s = self.obstacle_sense_timeout_s
        box: dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                box["result"] = sensor.sense(after, timeout_s)
            except Exception as exc:  # noqa: BLE001 - a failing sensor is a gate that cannot look
                box["error"] = exc
            finally:
                done.set()
                self._sense_call_ended()

        began = time.monotonic()
        with self._lock:
            self._sense_inflight += 1
        try:
            threading.Thread(target=run, daemon=True, name=f"{self.drone_id}-sense").start()
        except Exception as exc:  # noqa: BLE001 - no thread, no look
            self._sense_call_ended()
            failed = SenseResult.make_unavailable(REASON_ERROR, detail={"error": f"{type(exc).__name__}: {exc}"[:120]})
            return failed, 0.0
        finished = done.wait(timeout_s + _SENSE_JOIN_GRACE_S)
        measured_ms = (time.monotonic() - began) * 1000.0
        if not finished:
            return SenseResult.make_unavailable(
                REASON_TIMEOUT, latency_ms=measured_ms, detail={"abandoned": True}
            ), measured_ms
        result = box.get("result")
        if "error" in box or not isinstance(result, SenseResult):
            exc = box.get("error")
            error = f"{type(exc).__name__}: {exc}"[:120] if exc is not None else (
                f"sensor returned {type(result).__name__}"
            )
            failed = SenseResult.make_unavailable(REASON_ERROR, latency_ms=measured_ms, detail={"error": error})
            return failed, measured_ms
        latency = result.latency_ms if result.latency_ms and result.latency_ms > 0 else measured_ms
        return result, float(latency)

    # ------------------------------------------------------------------
    # The sensor's down timer: a probe after a landing or a new plan
    # ------------------------------------------------------------------

    def _sense_call_ended(self) -> None:
        """A sense call (a gate's or the probe's) returned: start a probe that waited for it."""
        with self._lock:
            self._sense_inflight = max(0, self._sense_inflight - 1)
            why = self._probe_wanted
            if why is not None and self._sense_inflight == 0:
                self._probe_wanted = None
                self._start_probe_locked(why)

    def _request_sensor_probe_locked(self, why: str) -> None:
        """Ask the sensor once whether it can look again, if its down timer runs. Caller holds ``_lock``.

        Called when the drone lands and when a plan that does not continue
        the current one is accepted. Nothing happens while the sensor works
        (no timer), without a sensor, or while a probe is already pending.
        The probe waits until no other sense call is in flight (a hung call
        keeps it waiting, and the sensor stays reported down); it never
        blocks the caller and never raises.
        """
        if (
            self._obstacle_sensor is None
            or self._sensor_down_since is None
            or self._closed
            or self._probe_running
            or self._probe_wanted is not None
        ):
            return
        if self._sense_inflight:
            self._probe_wanted = why
            return
        self._start_probe_locked(why)

    def _start_probe_locked(self, why: str) -> None:
        """Start the probe's sense call on its own daemon thread. Caller holds ``_lock``."""
        sensor = self._obstacle_sensor
        since = self._sensor_down_since
        if sensor is None or since is None or self._closed:
            return
        self._probe_running = True
        self._sense_inflight += 1
        try:
            threading.Thread(
                target=self._run_probe, args=(sensor, since, why), daemon=True, name=f"{self.drone_id}-sense-probe"
            ).start()
        except Exception as exc:  # noqa: BLE001 - no probe; the timer stays as it is
            self._probe_running = False
            self._sense_inflight = max(0, self._sense_inflight - 1)
            self._log.warning(EVENT_SENSOR_PROBE, drone_id=self.drone_id, why=why, state=None,
                              recovered=False, error=f"{type(exc).__name__}: {exc}"[:120])

    def _run_probe(self, sensor: Any, since: float, why: str) -> None:
        """The probe: one sense call; a view clears the down timer it was started for."""
        result: Any = None
        error = None
        recovered = False
        try:
            try:
                result = sensor.sense(self._clock(), self.obstacle_sense_timeout_s)
            except Exception as exc:  # noqa: BLE001 - the sensor still cannot look
                error = f"{type(exc).__name__}: {exc}"[:120]
            looked = isinstance(result, SenseResult) and not result.unavailable
            with self._lock:
                self._probe_running = False
                # Only the run of failures the probe was started for is
                # cleared; a gate that failed or looked since decides alone.
                recovered = looked and self._sensor_down_since == since
                if recovered:
                    self._sensor_down_since = None
            seen = result if isinstance(result, SenseResult) else None
            self._log.info(
                EVENT_SENSOR_PROBE,
                drone_id=self.drone_id,
                why=why,
                state=None if seen is None else seen.state,
                reason=None if seen is None else (seen.reason or None),
                recovered=recovered,
                error=error,
            )
        finally:
            with self._lock:
                self._probe_running = False
            self._sense_call_ended()

    def _note_sense_locked(self, result: SenseResult, latency_ms: float, started: float) -> None:
        """Record a gate's outcome for telemetry and the sensor's down timer. Caller holds ``_lock``."""
        self._obstacle_state = _OBSTACLE_STATES.get(result.state, OBSTACLE_STATE_UNAVAILABLE)
        self._obstacle_latency_ms = round(float(latency_ms), 1)
        if result.unavailable:
            if self._sensor_down_since is None:
                self._sensor_down_since = started
        else:
            self._sensor_down_since = None

    def _log_gate(
        self,
        step: tuple[int, int, str, int],
        attempt: int,
        result: SenseResult,
        latency_ms: float,
        pose: Any,
        source: str | None = None,
    ) -> None:
        """Log one gate (``edge_obstacle_gate``) and, for a sensor that could not look, why."""
        lane_index = step[0]
        extra = {} if source is None else {"source": source}
        self._log.info(
            EVENT_OBSTACLE_GATE,
            drone_id=self.drone_id,
            lane_index=lane_index,
            chunk_index=step[1],
            attempt=attempt,
            state=result.state,
            cue=result.cue,
            reason=result.reason or None,
            person_cue=result.person_cue,
            depth_score=result.depth_score,
            latency_ms=round(float(latency_ms), 1),
            x_cm=round(pose.x_cm, 1),
            y_cm=round(pose.y_cm, 1),
            heading_deg=round(pose.heading_deg, 1),
            **extra,
        )
        if not result.unavailable:
            self._sensor_error_logged = None
            return
        if result.reason == REASON_ERROR:
            error = str(result.detail.get("error") or "sensor error")
            if error != self._sensor_error_logged:
                # Logged once per change, like the drone-link problem.
                self._sensor_error_logged = error
                self._log.error(EVENT_SENSOR_ERROR, drone_id=self.drone_id, lane_index=lane_index, error=error)
        elif result.reason == REASON_TIMEOUT:
            self._log.warning(
                EVENT_SENSE_TIMEOUT,
                drone_id=self.drone_id,
                lane_index=lane_index,
                attempt=attempt,
                latency_ms=round(float(latency_ms), 1),
            )
        elif result.detail.get("cause") == HEADING_NOT_REACHED:
            self._log.warning(
                EVENT_SENSE_STALE,
                drone_id=self.drone_id,
                lane_index=lane_index,
                attempt=attempt,
                cause=HEADING_NOT_REACHED,
                measured_heading_deg=result.detail.get("measured_heading_deg"),
                commanded_heading_deg=result.detail.get("commanded_heading_deg"),
            )
        else:
            self._log.warning(EVENT_SENSE_STALE, drone_id=self.drone_id, lane_index=lane_index, attempt=attempt)

    # ------------------------------------------------------------------
    # The sensed way back of a resume_search (``lane_manual._run_return``)
    # ------------------------------------------------------------------

    def _fly_return_sensed(self, path: list[tuple[str, int]]) -> str:
        """Fly the way back of a ``resume_search`` in sensed steps: ``done``, ``stopped`` or ``blocked``.

        *path* is ``lane_manual``'s way back: an optional ``("rotate",
        degrees)`` to the hold heading, then moves along that heading's
        forward and right axes. With turning on, every move is flown nose
        first, like a lane: the drone turns to face it by the shortest
        rotation, and each step of at most the sensed step length is gated
        with a fresh view (``_look``) and flown as a body ``forward`` move.
        The hold heading itself is not restored, because the plan then turns
        to face its own next step. With turning off (``forward_only``) the
        drone turns to the hold heading as before and only ``forward`` steps
        are gated, as on a lane.

        A gate that does not clear cuts the lane the search resumes with, as
        the cut policy cuts a lane at its own gate, and the way back ends
        there (``blocked``): the drone hovers where it stopped. A landing, a
        hold, manual control or a link-loss hold stops it before its next
        move (``stopped``), exactly as an unsensed way back stops.
        """
        turning = self._turns_to_face()
        limits = getattr(self.envelope, "limits", ROOM_LIMITS)
        base = self.envelope.pose_snapshot().heading_deg
        lane_index = self.lane_index

        def current() -> bool:
            return True

        index = 0
        for direction, amount in path:
            if direction == "rotate":
                base = (base + amount) % 360.0
                if not turning and not self._command_rotate(amount):
                    return "stopped"
                continue
            face = (base + plan_heading(direction)) % 360.0 if turning else None
            body = "forward" if turning else direction
            for distance in self._split_lane(amount, limits):
                step = (lane_index, index, direction, distance)
                key = ("return", index)
                index += 1
                while True:
                    if face is not None:
                        verdict = self._face(face, step, key, current, self._resume_cut_target_locked)
                        if verdict == _AGAIN:
                            if not self._pause_point():
                                return "stopped"
                            continue
                        if verdict is not None:
                            return "blocked" if verdict == _CUT else "stopped"
                    if body != "forward":
                        break  # forward_only: a back or sideways step is flown unsensed, as on a lane
                    verdict = self._look(step, current, self._resume_cut_target_locked, source="return")
                    if verdict == _GO:
                        break
                    if verdict == _AGAIN:
                        if not self._pause_point():
                            return "stopped"
                        continue
                    return "blocked" if verdict == _CUT else "stopped"
                if not self.envelope.check_move(body, distance):
                    pose = self.envelope.pose_snapshot()
                    self._log.error(
                        "edge_geofence_refused", drone_id=self.drone_id, direction=body, distance_cm=distance,
                        x_cm=round(pose.x_cm, 1), y_cm=round(pose.y_cm, 1), heading_deg=round(pose.heading_deg, 1),
                        source="return",
                    )
                    self.land_now("geofence")
                    return "stopped"
                if not self._command_move(body, distance, record=True):
                    return "stopped"
        return "done"

    def _resume_cut_target_locked(self) -> tuple[int, int, str, int] | None:
        """The lane a blocked way back cuts: the next move of the plan, or None when none is left. Caller holds ``_lock``."""
        return self._moves[self._step] if self._step < len(self._moves) else None

    def _pause_point(self) -> bool:
        """False when the sequence must stop before its next move; nothing is sent either way.

        Asks ``_claim_move``, so a landing, a hold, manual control or a
        link-loss hold stops and releases the sequence exactly as a refused
        move does (a manual hold records its hold point then).
        """
        if not self._claim_move():
            return False
        with self._lock:
            self._moving = False
        return True

    def _stopped_short_flown(self, start: Any, direction: str, distance: int) -> int:
        """How far a move the autopilot stopped short got, from the drone's measured position.

        The measured plan-frame position projected on the move's direction
        from where the move started, within ``0..distance``. A drone that
        reports no position counts as not having moved, which can only make
        later lanes shorter, never longer.
        """
        probe = getattr(self.drone, "measured_pose", None)
        try:
            pose = probe() if callable(probe) else None
        except Exception:  # noqa: BLE001 - fall back to "not moved"
            pose = None
        if pose is None:
            return 0
        ux, uy = start.endpoint(direction, 1.0)
        ux, uy = ux - start.x_cm, uy - start.y_cm
        along = (pose.x_cm - start.x_cm) * ux + (pose.y_cm - start.y_cm) * uy
        return round(max(0.0, min(float(distance), along)))

    def _cut_lane_locked(
        self,
        step: tuple[int, int, str, int],
        reason: str,
        *,
        result: SenseResult | None = None,
        flown_in_step: int = 0,
        cue: str | None = None,
        detail: str | None = None,
    ) -> Cut:
        """Cut the lane of *step*, the next move, where the drone is. Caller holds ``_lock``.

        The rest of the lane and of every following lane in the same
        direction is removed from the plan, the cut is recorded (telemetry
        reports it and ``obstacle_seq`` counts it), and the lanes after it
        are re-planned with the cut rules (``lane_steps.replay_lanes``):
        lanes on the cut's axis that fly back toward the grid are shortened,
        none is ever lengthened, and a ``sensor_down`` cut leaves no lane to
        fly. ``_step`` then points at the first move of the next lane flown.
        *flown_in_step* is how far a move stopped short got.
        """
        lane_index, chunk_index, direction, _ = step
        moves = self._moves
        flown = 0
        k = self._step - 1
        while k >= 0 and moves[k][0] == lane_index:
            flown += moves[k][3]
            k -= 1
        end = self._step
        remaining = 0
        while end < len(moves) and moves[end][0] == lane_index:
            remaining += moves[end][3]
            end += 1
        flown += int(flown_in_step)
        unflown = remaining - int(flown_in_step)
        before: dict[int, int] = {}
        for move in moves[end:]:
            before[move[0]] = before.get(move[0], 0) + move[3]
        cut = Cut(lane_index, int(flown), reason)
        self._cuts.append(cut)
        self._obstacle_seq += 1
        if result is not None:
            # ``obstacle_state`` is the last gate's outcome; a cut that no
            # gate made (an autopilot's STOPPED_SHORT) leaves it alone.
            self._obstacle_state = (
                OBSTACLE_STATE_UNAVAILABLE if reason in (CUT_SENSOR_STALE, CUT_SENSOR_DOWN) else OBSTACLE_STATE_BLOCKED
            )
        limits = getattr(self.envelope, "limits", ROOM_LIMITS)
        replay = replay_lanes(self._lanes, self._cuts, limits.min_move_cm)
        tail: list[tuple[int, int, str, int]] = []
        for later in range(lane_index + 1, len(self._lanes)):
            target = replay.targets[later]
            if target > 0:
                later_direction = self._lanes[later][0]
                tail.extend(
                    (later, index, later_direction, distance)
                    for index, distance in enumerate(self._split_lane(target, limits))
                )
        self._moves = moves[: self._step] + tail

        pose = self.envelope.pose_snapshot()
        self._log.warning(
            EVENT_OBSTACLE_STOP,
            drone_id=self.drone_id,
            lane_index=lane_index,
            chunk_index=chunk_index,
            direction=direction,
            reason=reason,
            cue=cue if cue is not None else (None if result is None else result.cue),
            sensor_reason=None if result is None or not result.reason else result.reason,
            person_cue=None if result is None else result.person_cue,
            depth_score=None if result is None else result.depth_score,
            latency_ms=None if result is None else round(float(result.latency_ms), 1),
            flown_cm=int(flown),
            unflown_cm=int(unflown),
            x_cm=round(pose.x_cm, 1),
            y_cm=round(pose.y_cm, 1),
            heading_deg=round(pose.heading_deg, 1),
            obstacle_seq=self._obstacle_seq,
            detail=detail or None,
            plan_id=None if self._plan_identity is None else self._plan_identity.plan_id,
        )
        self._log.warning(
            EVENT_LANE_CUT,
            drone_id=self.drone_id,
            lane_index=lane_index,
            direction=direction,
            planned_cm=self._lanes[lane_index][1],
            flown_cm=int(flown),
            reason=reason,
        )
        if reason == CUT_SENSOR_DOWN:
            self._log.error(
                EVENT_LANES_ENDED_SENSOR_DOWN,
                drone_id=self.drone_id,
                lane_index=lane_index,
                lanes_ended=len(self._lanes) - lane_index,
                sensor_down_s=OBSTACLE_SENSOR_DOWN_S,
            )
            return cut
        reasons = {}
        for segment in replay.segments:
            reasons.setdefault(segment.lane_index, segment.reason)
        for later in range(lane_index + 1, len(self._lanes)):
            planned = self._lanes[later][1]
            target = replay.targets[later]
            if target < planned and target != before.get(later, 0):
                self._log.warning(
                    EVENT_LANE_CUT,
                    drone_id=self.drone_id,
                    lane_index=later,
                    direction=self._lanes[later][0],
                    planned_cm=planned,
                    flown_cm=target,
                    reason=reasons.get(later, reason),
                )
        return cut
