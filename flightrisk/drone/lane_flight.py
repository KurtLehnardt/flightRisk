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
* ``nudge``: one operator move (see ``flightrisk.drone.lane_commands``).

Every move, and a takeoff, is *claimed* under the executor lock immediately
before it is sent (``_claim_move``). A landing (the abort flag) or a hold
makes the claim fail, so the sequence stops before its next move and never
interrupts the move already with the drone; a hold then releases the
sequence's slot so ``resume`` can start it again from the same step. No
sequence is launched once a landing has been requested (``_launch``).
"""
from __future__ import annotations

import inspect
import threading
from typing import Any

from flightrisk.drone.controller import command_result
from flightrisk.drone.safety import CeilingError

__all__ = ["FlightSequenceMixin", "MoveNotExecutedError"]

# Below this much remaining time the start wait switches from event waits to
# short sleeps, so takeoff fires close to the scheduled instant.
_START_SPIN_S = 0.02
# Phase of each flight sequence when it is launched.
_FIRST_PHASE = {"start_at": "pre_takeoff", "takeoff": "pre_takeoff", "lanes": "lanes", "climb": "climbing", "nudge": "nudge"}


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
        self._worker = threading.Thread(
            target=self._run_sequence,
            args=(kind, t_edge, offset_s, cmd, nudge),
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
        """Mark the calling sequence as finished. Caller holds ``_lock``."""
        if self._is_active():
            self._active_thread = None
            self._active_kind = None
            self._phase = None

    def _set_phase(self, phase: str) -> None:
        with self._lock:
            if self._is_active():
                self._phase = phase

    def _claim_move(self, allow_held: bool = False) -> bool:
        """Claim the right to send one move (or takeoff) now.

        False if a landing aborted the sequence, or if a hold is in force
        (unless *allow_held*, for a nudge flown while holding): the sequence
        then stops, releasing its slot so ``resume`` can start it again.
        """
        with self._lock:
            if self._abort.is_set() or not self._is_active():
                return False
            if self._held and not allow_held:
                self._log.info("edge_hold_stopped", drone_id=self.drone_id, lane_index=self.lane_index)
                self._release_active()
                return False
            self._moving = True
            return True

    def _end_move(self) -> None:
        with self._lock:
            self._moving = False

    def _run_sequence(
        self,
        kind: str,
        t_edge: float | None,
        offset_s: float,
        cmd: dict[str, Any] | None,
        nudge: tuple[str, int, bool] | None,
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
        nothing changed, so there is no landing to retry afterwards.
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

    def _climb_to_slot(self) -> bool:
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
        return self._command_move(direction, distance, record=False)

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

    def _command_move(self, direction: str, distance: int, record: bool) -> bool:
        """Claim and fly one move; False if the sequence must stop (landing or hold)."""
        if not self._claim_move():
            return False
        # Every return value is normalized (None/True: executed; False or a
        # non-OK CommandResult: not executed), so a move the controller did
        # not fly is never recorded in the dead-reckoned pose.
        try:
            result = command_result(self.drone.move(direction, distance), "move")
        finally:
            self._end_move()
        if not result.ok:
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
        landing after the last lane.
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
            if not self.envelope.check_move(direction, distance):
                pose = self.envelope.pose_snapshot()
                self._log.error(
                    "edge_geofence_refused",
                    drone_id=self.drone_id,
                    lane_index=lane_index,
                    direction=direction,
                    distance_cm=distance,
                    x_cm=round(pose.x_cm, 1),
                    y_cm=round(pose.y_cm, 1),
                )
                self.land_now("geofence")
                return
            if not self._command_move(direction, distance, record=True):
                return
            with self._lock:
                same_plan = self._plan_gen == gen or gen in self._linked_gens
                if (
                    same_plan
                    and not self._abort.is_set()
                    and self._step < len(self._moves)
                    and self._moves[self._step][:2] == step[:2]
                ):
                    self._step += 1
