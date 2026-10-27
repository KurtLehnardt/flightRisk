"""Protocol v2 commands on the edge: ``hold``, ``resume``, ``nudge``, replies and ``cmd_id``s.

This module is the protocol v2 half of the edge lane executor
(``flightrisk.drone.lane_executor``). ``LaneExecutor`` inherits
``FleetCommandMixin``, which adds:

* **Dispatch with at-most-once execution.** Every fenced command that
  carries a ``cmd_id`` goes through a ``CommandCache`` (see
  ``flightrisk.drone.command_cache``): it is recorded as pending on arrival,
  a duplicate is never executed again, and the first reply (ack or nack) is
  final and is resent unchanged to a duplicate. A message without a
  ``cmd_id`` is a v1 command and gets the v1 replies, as before.
* **The authority hook.** ``LaneExecutor(authority=...)`` takes an object
  with two methods (``CommandAuthority``): ``check(msg)`` returns None to
  accept a fenced command or the nack reason (``stale_epoch`` or
  ``not_authority``) to refuse it, and ``epoch_seen()`` returns the epoch
  the edge currently obeys (reported in telemetry and in those nacks).
  Landing never reaches it. The default, ``PermissiveAuthority``, accepts
  everything and remembers the highest epoch it has seen, so the
  single-ground path behaves exactly as before. The edge authority of the
  two-laptop plan (``flightrisk/fleet_authority.py``) plugs in here, and
  calls ``invalidate_plan`` when it adopts a new epoch.
* **hold.** The drone stops after the move in flight and hovers. The
  executor never interrupts a move, and in particular never sends the Tello
  SDK ``stop`` for a hold: interrupting a relative move leaves the distance
  actually flown unknown, which would corrupt the dead-reckoned pose that
  the geofence relies on, and what ``stop`` does mid-move on Tello EDU
  firmware is unverified on hardware (the plan's L2 rung). A Tello hovers
  on its own once a move completes, so holding needs no command at all.
  Every move (lane moves, the altitude-slot climb, and a takeoff that has
  not been sent yet) is claimed under the executor lock just before it is
  sent; once a hold is recorded no further move is claimed. The ack says
  ``detail: finishing_move`` when a move was already claimed. A hold also
  cancels a scheduled ``start_at`` that has not fired (its ``start_at`` is
  answered ``nack busy``), suspends the end-of-lanes hover timeout, and is
  cleared by every landing. A hold on a drone that is on the ground and
  has no start pending is refused with ``not_flying``.
* **resume.** Restarts only the plan named by ``plan_id``, and only while
  it is valid, from the lane and move where the drone stopped (or from the
  start of a plan accepted while holding). Refused with ``landing``,
  ``no_valid_plan``, ``plan_mismatch``, ``not_flying``, ``busy`` (a nudge
  is still flying) or ``geofence`` (the rest of the path, from the current
  dead-reckoned position, would leave the geofence; the drone keeps
  holding). A resume while not holding is acknowledged with
  ``detail: not_holding`` and changes nothing.
* **nudge.** One horizontal move of 20-500 cm, accepted only while the
  drone is hovering idle or holding, never while it flies lanes or another
  sequence runs (``busy``), and only if the geofence allows it
  (``geofence``). It is recorded in the dead-reckoned pose like any lane
  move. It is acknowledged once the drone has flown it; a nudge the
  controller did not execute is answered ``nack busy`` and is not recorded,
  and a controller error lands the drone, as for a lane move. A re-task
  that arrives while a nudge flies is flown after it, once the rest of the
  plan has been checked against the geofence from the nudged position.
"""
from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any, Protocol

from flightrisk.drone.command_cache import DONE, MISMATCH, NEW, PENDING
from flightrisk.drone.controller import command_result
from flightrisk.drone.safety import HORIZONTAL_DIRECTIONS
from flightrisk.fleet_protocol import (
    MAX_EPOCH,
    NUDGE_MAX_CM,
    NUDGE_MIN_CM,
    ProtocolError,
    is_identifier,
    make_ack,
    make_nack,
)

__all__ = [
    "AUTHORITY_REASONS",
    "V2_CONTROL_TYPES",
    "CommandAuthority",
    "FleetCommandMixin",
    "PermissiveAuthority",
]

# Ground -> edge protocol v2 commands the executor handles in addition to
# the v1 ``CONTROL_TYPES``. ``handover`` belongs to the edge authority.
V2_CONTROL_TYPES = frozenset({"hold", "resume", "nudge"})
# Fenced commands the executor dispatches (``FENCED_COMMAND_TYPES`` minus
# ``handover``).
COMMAND_TYPES = frozenset({"takeoff", "assign_lanes", "start_at"}) | V2_CONTROL_TYPES
# The only nack reasons an authority may give.
AUTHORITY_REASONS = frozenset({"stale_epoch", "not_authority"})


class CommandAuthority(Protocol):
    """What the executor needs from the edge authority (plan section 2.3)."""

    def check(self, msg: Mapping[str, Any]) -> str | None:
        """None to accept the fenced command *msg*, or ``stale_epoch``/``not_authority``."""

    def epoch_seen(self) -> int:
        """The epoch of the leader the edge obeys (0 before any)."""


class PermissiveAuthority:
    """Single-ground default: accept every command and track the highest epoch seen."""

    def __init__(self) -> None:
        self._epoch = 0
        self._lock = threading.Lock()

    def check(self, msg: Mapping[str, Any]) -> str | None:
        epoch = msg.get("epoch")
        if isinstance(epoch, int) and not isinstance(epoch, bool) and 0 < epoch <= MAX_EPOCH:
            with self._lock:
                self._epoch = max(self._epoch, epoch)
        return None

    def epoch_seen(self) -> int:
        with self._lock:
            return self._epoch


def _is_nudge_distance(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and NUDGE_MIN_CM <= value <= NUDGE_MAX_CM


class FleetCommandMixin:
    """Protocol v2 command handling for ``LaneExecutor`` (see the module docstring)."""

    # ------------------------------------------------------------------
    # State shared with the ground
    # ------------------------------------------------------------------

    @property
    def epoch_seen(self) -> int:
        """The epoch the edge obeys, from the authority (0 if unknown or invalid)."""
        try:
            epoch = self._authority.epoch_seen()
        except Exception as exc:  # noqa: BLE001 - a broken authority must not stop telemetry
            self._log.warning("edge_authority_error", drone_id=self.drone_id, error=type(exc).__name__)
            return 0
        if isinstance(epoch, int) and not isinstance(epoch, bool) and 0 <= epoch <= MAX_EPOCH:
            return epoch
        return 0

    @property
    def is_held(self) -> bool:
        """True while a ``hold`` is in force."""
        with self._lock:
            return self._held

    @property
    def state(self) -> str:
        """Telemetry ``state``: idle, taking_off, hovering, flying_lanes, holding or landing."""
        flying = self._is_flying()
        with self._lock:
            return self._state_locked(flying)

    def _state_locked(self, flying: bool) -> str:
        """Caller holds ``_lock``."""
        if self._land_reason is not None and (flying or self._land_inflight):
            return "landing"
        phase = self._phase if self._worker_alive() else None
        if not flying:
            return "taking_off" if phase == "taking_off" else "idle"
        if self._held:
            return "holding"
        if phase in ("taking_off", "climbing"):
            return "taking_off"
        if phase == "lanes":
            return "flying_lanes"
        return "hovering"

    # ------------------------------------------------------------------
    # Dispatch, idempotency and authority
    # ------------------------------------------------------------------

    @staticmethod
    def _is_v2(msg: Mapping[str, Any] | None) -> bool:
        """True for a command that carries a valid ``cmd_id`` and so gets v2 replies."""
        return msg is not None and is_identifier(msg.get("cmd_id"))

    def _dispatch_command(self, msg: dict[str, Any]) -> None:
        """Run one fenced command at most once per ``cmd_id`` (see the module docstring)."""
        mtype = msg.get("type")
        cmd_id = msg.get("cmd_id")
        if cmd_id is not None:
            if not is_identifier(cmd_id):
                self._log.warning("edge_command_refused", drone_id=self.drone_id, type=mtype, reason="invalid_cmd_id")
                return
            self._v2_telemetry = True
            status, cached = self._commands.claim(cmd_id, mtype)
            if status == MISMATCH:
                self._log.warning("edge_command_id_reused", drone_id=self.drone_id, type=mtype, cmd_id=cmd_id)
                self._emit_nack(msg, "invalid", detail="cmd_id_reused")
                return
            if status != NEW:
                self._log.info(
                    "edge_command_duplicate", drone_id=self.drone_id, type=mtype, cmd_id=cmd_id, pending=status == PENDING
                )
                if status == DONE and cached is not None:
                    self._emit(cached)
                return
        verdict = self._authority_verdict(msg)
        if verdict is not None:
            self._log.warning(
                "edge_command_refused", drone_id=self.drone_id, type=mtype, cmd_id=cmd_id, reason=verdict
            )
            self._nack(msg, verdict, epoch_seen=self.epoch_seen)
            return
        if mtype == "assign_lanes":
            self.handle_assign_lanes(msg)
        elif mtype == "start_at":
            self.handle_start_at(msg)
        elif mtype == "takeoff":
            self.handle_takeoff(msg)
        elif cmd_id is None and mtype != "hold":
            # resume and nudge move the drone and exist only in v2, which
            # always carries a cmd_id. A hold is executed even without one:
            # stopping is always safe, though it cannot be answered.
            self._log.warning("edge_command_refused", drone_id=self.drone_id, type=mtype, reason="no_cmd_id")
        elif mtype == "hold":
            self.handle_hold(msg)
        elif mtype == "resume":
            self.handle_resume(msg)
        elif mtype == "nudge":
            self.handle_nudge(msg)

    def _authority_verdict(self, msg: Mapping[str, Any]) -> str | None:
        """None to accept *msg*, else a nack reason. Fails closed."""
        try:
            verdict = self._authority.check(msg)
        except Exception as exc:  # noqa: BLE001 - fail closed for anything that moves the drone
            self._log.error("edge_authority_error", drone_id=self.drone_id, error=type(exc).__name__)
            return "not_authority"
        if verdict is None:
            return None
        return verdict if verdict in AUTHORITY_REASONS else "not_authority"

    # ------------------------------------------------------------------
    # Replies
    # ------------------------------------------------------------------

    def _reply(self, msg: Mapping[str, Any], reply: dict[str, Any] | None) -> None:
        """Send the one reply of a v2 command and record it as its final outcome."""
        if not self._commands.finish(msg["cmd_id"], msg.get("type"), reply):
            self._log.debug("edge_reply_suppressed", drone_id=self.drone_id, cmd_id=msg["cmd_id"])
            return
        if reply is not None:
            self._emit(reply)

    def _ack(self, msg: Mapping[str, Any] | None, **fields: Any) -> None:
        """Acknowledge a v2 command (a no-op for a v1 one)."""
        if not self._is_v2(msg):
            return
        try:
            reply = make_ack(msg["type"], self.drone_id, msg["cmd_id"], **fields)
        except ProtocolError as exc:
            self._log.error("edge_reply_invalid", drone_id=self.drone_id, type=msg.get("type"), error=str(exc)[:120])
            reply = None
        self._reply(msg, reply)

    def _nack(self, msg: Mapping[str, Any] | None, reason: str, **fields: Any) -> None:
        """Refuse a v2 command with *reason* (a no-op for a v1 one)."""
        if not self._is_v2(msg):
            return
        self._reply(msg, self._build_nack(msg, reason, **fields))

    def _emit_nack(self, msg: Mapping[str, Any], reason: str, **fields: Any) -> None:
        """Send a nack without recording it (for a reused ``cmd_id``)."""
        reply = self._build_nack(msg, reason, **fields)
        if reply is not None:
            self._emit(reply)

    def _build_nack(self, msg: Mapping[str, Any], reason: str, **fields: Any) -> dict[str, Any] | None:
        try:
            return make_nack(msg["type"], self.drone_id, reason, msg["cmd_id"], **fields)
        except ProtocolError as exc:
            self._log.error("edge_reply_invalid", drone_id=self.drone_id, type=msg.get("type"), error=str(exc)[:120])
            return None

    def _ack_landing(self, msg: Mapping[str, Any]) -> None:
        """Acknowledge a landing that carried a ``cmd_id``. Never cached, never nacked."""
        if not self._is_v2(msg):
            return
        try:
            self._emit(make_ack(msg["type"], self.drone_id, msg["cmd_id"]))
        except ProtocolError as exc:
            self._log.error("edge_reply_invalid", drone_id=self.drone_id, type=msg.get("type"), error=str(exc)[:120])

    def _cancel_reason(self) -> tuple[str, str | None]:
        """Nack reason and detail for a sequence that ended before it executed."""
        with self._lock:
            return self._cancel if self._cancel is not None else ("landing", None)

    # ------------------------------------------------------------------
    # hold / resume / nudge
    # ------------------------------------------------------------------

    def handle_hold(self, msg: dict[str, Any]) -> bool:
        """Stop after the move in flight and hover until ``resume`` (see the module docstring)."""
        refusal = None
        detail = None
        with self._lock:
            flying = self._is_flying()
            if self._land_reason is not None and (flying or self._land_inflight):
                refusal = "landing"
            else:
                cancelled = self._worker_alive() and self._phase == "pre_takeoff"
                if cancelled:
                    # A start or takeoff that has not been sent yet never will be.
                    self._cancel = ("busy", "cancelled_by_hold")
                    self._abort.set()
                if flying or (self._worker_alive() and self._phase == "taking_off"):
                    if not self._held:
                        self._parked = self._resume_kind_locked(cancelled)
                    self._held = True
                    self._hover_since = None
                    if self._moving:
                        detail = "finishing_move"
                    elif cancelled:
                        detail = "start_cancelled"
                elif cancelled:
                    detail = "start_cancelled"
                else:
                    refusal = "not_flying"
        if refusal is not None:
            self._log.warning("edge_hold_refused", drone_id=self.drone_id, reason=refusal, cmd_id=msg.get("cmd_id"))
            self._nack(msg, refusal)
            return False
        self._log.warning(
            "edge_hold",
            drone_id=self.drone_id,
            cmd_id=msg.get("cmd_id"),
            hold_reason=msg.get("reason") if msg.get("reason") in ("alert", "operator", "handover") else None,
            detail=detail,
            lane_index=self.lane_index,
        )
        self._ack(msg, **({"detail": detail} if detail else {}))
        return True

    def _resume_kind_locked(self, cancelled: bool) -> str:
        """What ``resume`` restarts: ``lanes``, ``climb`` or ``hover``. Caller holds ``_lock``."""
        kind = self._active_kind if self._worker_alive() and not cancelled else None
        if kind in ("lanes", "start_at"):
            return "lanes"
        if kind in ("takeoff", "climb"):
            return "climb"
        return "lanes" if self._flying_lanes else "hover"

    def handle_resume(self, msg: dict[str, Any]) -> bool:
        """Continue the held plan named by ``plan_id`` (see the module docstring)."""
        plan_id = msg.get("plan_id")
        refusal = None
        detail = None
        outside = None
        with self._lock:
            flying = self._is_flying()
            current_id = None if self._plan_identity is None else self._plan_identity.plan_id
            if not is_identifier(plan_id):
                refusal = "invalid"
            elif self._land_reason is not None and (flying or self._land_inflight):
                refusal = "landing"
            elif not self._plan_valid:
                refusal = "no_valid_plan"
            elif plan_id != current_id:
                refusal = "plan_mismatch"
            elif not flying:
                refusal = "not_flying"
            elif not self._held:
                detail = "not_holding"
            elif self._worker_alive() and (self._active_kind == "nudge" or self._abort.is_set()):
                # A nudge is flying, or a cancelled start has not exited yet.
                refusal = "busy"
            elif self._worker_alive():
                # The held sequence has not stopped yet: let it carry on.
                self._held = False
                self._parked = None
                detail = "continuing"
            else:
                kind = self._parked
                if kind == "lanes":
                    outside = self._path_outside(self._moves, self._step)
                if outside is not None:
                    refusal = "geofence"
                elif kind in ("lanes", "climb") and not self._launch(kind):
                    refusal = "busy"
                else:
                    self._held = False
                    self._parked = None
                    self._hover_since = None
        if refusal is not None:
            fields = {} if outside is None else {"x_cm": round(outside[0], 1), "y_cm": round(outside[1], 1)}
            self._log.warning(
                "edge_resume_refused", drone_id=self.drone_id, reason=refusal, cmd_id=msg.get("cmd_id"), **fields
            )
            self._nack(msg, refusal)
            return False
        self._log.warning(
            "edge_resume", drone_id=self.drone_id, cmd_id=msg.get("cmd_id"), plan_id=plan_id,
            lane_index=self.lane_index, detail=detail,
        )
        self._ack(msg, plan_id=plan_id, **({"detail": detail} if detail else {}))
        return True

    def handle_nudge(self, msg: dict[str, Any]) -> bool:
        """Fly one horizontal 20-500 cm move while hovering idle or holding (see the module docstring)."""
        direction = msg.get("direction")
        distance = msg.get("distance_cm")
        refusal = None
        with self._lock:
            flying = self._is_flying()
            if direction not in HORIZONTAL_DIRECTIONS or not _is_nudge_distance(distance):
                refusal = "invalid"
            elif self._land_reason is not None and (flying or self._land_inflight):
                refusal = "landing"
            elif not flying:
                refusal = "not_flying"
            elif self._worker_alive():
                refusal = "busy"
            elif not self.envelope.check_move(direction, distance):
                refusal = "geofence"
            elif not self._launch("nudge", cmd=msg, nudge=(direction, distance, self._held)):
                refusal = "busy"
        if refusal is not None:
            self._log.warning("edge_nudge_refused", drone_id=self.drone_id, reason=refusal, cmd_id=msg.get("cmd_id"))
            self._nack(msg, refusal)
            return False
        return True

    def _run_nudge(self, cmd: dict[str, Any], nudge: tuple[str, int, bool]) -> None:
        """Fly an accepted nudge on the worker thread and answer it."""
        direction, distance, allow_held = nudge
        if not self._claim_move(allow_held=allow_held):
            if self._abort.is_set():
                reason, detail = self._cancel_reason()
            else:
                reason, detail = "busy", "held"
            self._nack(cmd, reason, detail=detail)
            return
        try:
            result = command_result(self.drone.move(direction, distance), "move")
        finally:
            self._end_move()
        if not result.ok:
            # Not executed, so the pose is unchanged and the drone hovers
            # (or a landing that started meanwhile refused it).
            self._log.warning(
                "edge_nudge_not_executed", drone_id=self.drone_id, outcome=result.outcome.value, cmd_id=cmd.get("cmd_id")
            )
            if self._abort.is_set():
                reason, detail = self._cancel_reason()
                self._nack(cmd, reason, detail=detail)
            else:
                self._nack(cmd, "busy", detail=result.outcome.value)
            return
        self.envelope.record_move(direction, distance)
        pose = self.envelope.pose_snapshot()
        self._log.info(
            "edge_nudge", drone_id=self.drone_id, direction=direction, distance_cm=distance,
            x_cm=round(pose.x_cm, 1), y_cm=round(pose.y_cm, 1), cmd_id=cmd.get("cmd_id"),
        )
        self._ack(cmd)
