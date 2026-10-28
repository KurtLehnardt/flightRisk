"""Lane plans on the edge: parsing, plan identity and ordering, and ``assign_lanes``.

This module holds the plan half of the edge lane executor
(``flightrisk.drone.lane_executor``):

* ``split_lane_moves`` and ``parse_lanes`` turn an ``assign_lanes`` lane list
  into Tello-sized horizontal moves.
* ``PlanIdentity``, ``plan_identity`` and ``classify_plan`` implement the
  plan ordering of protocol v2 (``docs/fleet-protocol-v2.md``, "Plan
  ordering"). Plans are ordered by ``(epoch, plan_seq)``. A plan older than
  the newest one accepted is refused with ``stale_plan``; one with the same
  ``(epoch, plan_seq)`` but another ``plan_id`` (or another content) with
  ``plan_mismatch``; and an exact resend of the current, still valid plan is
  acknowledged without touching the plan or the drone.
* ``parse_on_complete`` reads ``on_complete`` (``hover`` or ``land``) and
  ``hover_timeout_s`` (above 0 and at most 120 s, 20 s when absent).
* ``LanePlanMixin`` is the executor's ``assign_lanes`` handling and its plan
  bookkeeping: the current plan's identity, whether it is still valid, and
  its end-of-lanes action.

Plan validity (plan section 2.8). A plan becomes valid when it is accepted.
It stops being valid at every landing the executor requests or sees (link
loss, battery, local key, emergency, geofence, a ground ``land``, a hover
timeout or the plan's own ``on_complete`` landing) and when the edge
authority calls ``invalidate_plan`` (for example on adopting a new epoch).
An invalid plan is kept, so its identity and lane index can still be
reported, but ``start_at`` and ``resume`` are refused with ``no_valid_plan``
until a new plan (a new ``plan_id``, and a higher ``plan_seq`` or epoch) is
accepted. A late or retried copy of the old plan is refused, so a stale plan
can never fly the drone again.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from flightrisk.drone.safety import (
    HORIZONTAL_DIRECTIONS,
    MAX_MOVE_CM,
    MIN_MOVE_CM,
    SafetyEnvelope,
    is_finite_number,
)
from flightrisk.fleet_protocol import (
    DEFAULT_HOVER_TIMEOUT_S,
    MAX_EPOCH,
    MAX_HOVER_TIMEOUT_S,
    NACK_REASONS,
    ON_COMPLETE_ACTIONS,
    is_identifier,
)

__all__ = [
    "NEW_PLAN",
    "SAME_PLAN",
    "LanePlanMixin",
    "PlanIdentity",
    "classify_plan",
    "parse_lanes",
    "parse_on_complete",
    "plan_identity",
    "split_lane_moves",
]

# Largest plan_seq (and epoch) any JSON implementation represents exactly.
_MAX_SEQ = 2**53 - 1

# Verdicts of ``classify_plan`` besides the two nack reasons.
NEW_PLAN = "new"
SAME_PLAN = "same"


def split_lane_moves(distance_cm: float) -> list[int]:
    """Split one lane into Tello-sized moves of 20-500 cm each, as evenly as possible."""
    total = round(distance_cm)
    if total < MIN_MOVE_CM:
        raise ValueError(f"lane distance {distance_cm!r} cm is below the {MIN_MOVE_CM} cm minimum move")
    count = max(1, math.ceil(total / MAX_MOVE_CM))
    base, extra = divmod(total, count)
    return [base + (1 if i < extra else 0) for i in range(count)]


def parse_lanes(raw: Any) -> list[tuple[str, int]]:
    """Validate an ``assign_lanes`` lane list and return ``(direction, distance_cm)`` pairs.

    Raises ``ValueError`` describing the first problem found. Only
    horizontal directions are accepted, so a lane plan can never change the
    drone's altitude slot.
    """
    if not isinstance(raw, list):
        raise ValueError("lanes must be a list")
    lanes: list[tuple[str, int]] = []
    for index, lane in enumerate(raw):
        if not isinstance(lane, dict):
            raise ValueError(f"lane {index} is not an object")
        direction = lane.get("direction")
        distance = lane.get("distance_cm")
        if direction not in HORIZONTAL_DIRECTIONS:
            raise ValueError(f"lane {index} has unsupported direction {direction!r}")
        if not is_finite_number(distance) or round(distance) < MIN_MOVE_CM:
            raise ValueError(f"lane {index} has invalid distance_cm {distance!r}")
        lanes.append((direction, round(distance)))
    return lanes


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class PlanIdentity:
    """Who a plan is: its ``plan_id`` and its ``(epoch, plan_seq)`` order.

    ``order`` is None for a plan that carries no ``plan_seq`` (a v1 plan).
    Such a plan is accepted as before and is never compared with others, but
    it does not lower the newest order accepted either.
    """

    plan_id: str | None
    order: tuple[int, int] | None


def plan_identity(msg: dict[str, Any], *, v2: bool) -> PlanIdentity:
    """Read the identity of an ``assign_lanes`` message. Raises ``ValueError``.

    A v2 command (one with a ``cmd_id``) must carry an identifier
    ``plan_id`` and an integer ``plan_seq`` of at least 1; on a v1 message
    either is checked only when present. The epoch is the message's fence
    epoch, or 0 when it has none.
    """
    plan_id = msg.get("plan_id")
    if (v2 or "plan_id" in msg) and not is_identifier(plan_id):
        raise ValueError("plan_id must be an identifier")
    if not (v2 or "plan_seq" in msg):
        return PlanIdentity(plan_id, None)
    seq = msg.get("plan_seq")
    if not (_is_int(seq) and 1 <= seq <= _MAX_SEQ):
        raise ValueError("plan_seq must be an integer of at least 1")
    epoch = msg.get("epoch", 0)
    if not (_is_int(epoch) and 0 <= epoch <= MAX_EPOCH):
        raise ValueError("epoch must be a non-negative integer")
    return PlanIdentity(plan_id, (epoch, seq))


def classify_plan(
    incoming: PlanIdentity,
    current: PlanIdentity | None,
    newest: tuple[int, int] | None,
    *,
    current_valid: bool,
    same_content: bool,
) -> str:
    """Decide what to do with an incoming plan.

    Returns ``NEW_PLAN`` (accept it as a new plan), ``SAME_PLAN`` (an exact
    resend of the current valid plan: acknowledge it and change nothing),
    or the nack reason ``stale_plan`` or ``plan_mismatch``. ``newest`` is the
    highest ``(epoch, plan_seq)`` ever accepted, kept across landings.
    """
    if incoming.order is None:
        return NEW_PLAN
    if newest is not None:
        if incoming.order < newest:
            return "stale_plan"
        if incoming.order == newest:
            if current is None or current.order != newest:
                # The newest ordered plan was replaced by an unordered v1
                # plan, so a copy of it is not newer than what is held.
                return "stale_plan"
            if incoming.plan_id != current.plan_id:
                return "plan_mismatch"
            if not current_valid:
                # The plan was invalidated (a landing): it may not restart.
                return "stale_plan"
            return SAME_PLAN if same_content else "plan_mismatch"
    if current is not None and incoming.plan_id is not None and incoming.plan_id == current.plan_id:
        # A newer order needs a new plan_id: start_at and resume name plans
        # by id, so one id must never mean two plans.
        return "plan_mismatch"
    return NEW_PLAN


def parse_on_complete(msg: dict[str, Any]) -> tuple[str | None, float]:
    """Read ``on_complete`` and ``hover_timeout_s``. Raises ``ValueError``.

    Returns ``(action, hover_timeout_s)``. ``action`` is None when the plan
    names none: the drone then hovers after its last lane until the ground
    lands it, which is the v1 behaviour. ``hover`` without a timeout means
    ``DEFAULT_HOVER_TIMEOUT_S`` (20 s).
    """
    action = msg.get("on_complete")
    has_timeout = "hover_timeout_s" in msg
    if action is None and "on_complete" not in msg:
        if has_timeout:
            raise ValueError("hover_timeout_s needs on_complete 'hover'")
        return None, DEFAULT_HOVER_TIMEOUT_S
    if not (isinstance(action, str) and action in ON_COMPLETE_ACTIONS):
        raise ValueError("on_complete must be 'hover' or 'land'")
    if action == "land":
        if has_timeout:
            raise ValueError("hover_timeout_s needs on_complete 'hover'")
        return action, DEFAULT_HOVER_TIMEOUT_S
    if not has_timeout:
        return action, DEFAULT_HOVER_TIMEOUT_S
    timeout = msg.get("hover_timeout_s")
    if not is_finite_number(timeout) or not 0 < timeout <= MAX_HOVER_TIMEOUT_S:
        raise ValueError(f"hover_timeout_s must be above 0 and at most {MAX_HOVER_TIMEOUT_S:g}")
    return action, float(timeout)


class LanePlanMixin:
    """``assign_lanes`` handling and plan bookkeeping for ``LaneExecutor``.

    Uses the executor's state (``_lock``, ``_lanes``, ``_moves``, ``_step``,
    ``_plan_*`` fields and so on); ``LaneExecutor.__init__`` creates it.
    """

    @property
    def plan_id(self) -> str | None:
        """``plan_id`` of the current plan (kept after it is invalidated), or None."""
        with self._lock:
            return None if self._plan_identity is None else self._plan_identity.plan_id

    @property
    def plan_valid(self) -> bool:
        """True while the current plan may be started or resumed (see the module docstring)."""
        with self._lock:
            return self._plan_valid

    def invalidate_plan(self, reason: str) -> bool:
        """Mark the current plan invalid; returns True if it was valid.

        Called for every landing, and by the edge authority when it adopts a
        new epoch. ``start_at`` and ``resume`` are then refused with
        ``no_valid_plan`` until a new plan is accepted.
        """
        with self._lock:
            was_valid = self._plan_valid
            self._plan_valid = False
        if was_valid:
            self._log.warning("edge_plan_invalidated", drone_id=self.drone_id, reason=reason, plan_id=self.plan_id)
        return was_valid

    def handle_assign_lanes(self, msg: dict[str, Any]) -> bool:
        """Validate and store a lane plan, then acknowledge it.

        Refuses a plan addressed to another drone (``other_drone``), with
        malformed lanes, plan identity or end-of-lanes action (``invalid``),
        with an out-of-range altitude (``altitude``), whose dead-reckoned path
        leaves the geofence (``geofence``), or while the battery level cannot
        be read (``battery``), because the ``ack`` must carry it. A plan older
        than the newest accepted is refused with ``stale_plan``, and a plan
        that reuses an order or a ``plan_id`` for different content with
        ``plan_mismatch`` (see ``classify_plan``). A refused plan is logged
        and answered with a ``nack`` carrying the reason and the number of
        lanes received; the current plan is kept unchanged. An exact resend
        of the current valid plan is acknowledged and changes nothing. An
        ``alt_cm`` in the plan replaces the local altitude slot, because the
        coordinator assigns slots across the whole fleet.

        Re-tasking: when the drone is already flying its lanes and the new
        plan starts with the lanes flown so far (including the current one)
        unchanged, progress carries over. The drone keeps its lane index and
        continues into the appended lanes instead of starting again from
        lane 0. Any other plan starts from its first lane at the current
        position. A drone that is holding stores the plan and flies it only
        after ``resume``. A plan that would re-task the drone after a landing
        was requested (``land_now`` called, the landing not yet recorded) is
        refused with ``landing`` and the current plan is kept.
        """
        raw_lanes = msg.get("lanes")
        lanes_received = len(raw_lanes) if isinstance(raw_lanes, list) else 0
        target = msg.get("drone_id")
        if target is not None and str(target) != self.drone_id:
            return self._refuse_lanes(msg, "other_drone", lanes_received, target=target)
        try:
            lanes = parse_lanes(raw_lanes)
            identity = plan_identity(msg, v2=self._is_v2(msg))
            on_complete, hover_timeout_s = parse_on_complete(msg)
        except ValueError as exc:
            return self._refuse_lanes(msg, "invalid", lanes_received, error=str(exc))
        alt_cm = msg.get("alt_cm")
        if alt_cm is not None:
            try:
                alt_cm = SafetyEnvelope.validate_alt_slot(alt_cm)
            except ValueError as exc:
                return self._refuse_lanes(msg, "altitude", lanes_received, error=str(exc))
        battery = self._read_battery()
        if not is_finite_number(battery):
            return self._refuse_lanes(msg, "battery", lanes_received, battery=battery)

        moves = [
            (lane_index, chunk_index, direction, distance)
            for lane_index, (direction, lane_cm) in enumerate(lanes)
            for chunk_index, distance in enumerate(split_lane_moves(lane_cm))
        ]
        content = (tuple(lanes), alt_cm, on_complete, hover_timeout_s)

        outside = None
        carried_step: int | None = None
        accepted = (0, False)
        with self._lock:
            verdict = classify_plan(
                identity,
                self._plan_identity,
                self._plan_newest,
                current_valid=self._plan_valid,
                same_content=content == self._plan_content,
            )
            if verdict == NEW_PLAN:
                if self._flying_lanes and self._is_flying():
                    carried_step = self._carried_step(lanes, moves)
                start_step = carried_step if carried_step is not None else 0
                # Check the path still to be flown against the geofence before
                # accepting anything. A move in progress is not in the pose yet,
                # and it is still part of moves[start_step:].
                outside = self._path_outside(moves, start_step)
                # A re-task is launched before the plan is stored, so a
                # landing that refuses the launch leaves the current plan
                # untouched. The sequence waits for this lock before it
                # reads the plan, so it flies the plan stored below.
                wanted = outside is None and self._retask_wanted_locked()
                retask = wanted and self._launch("lanes")
                if wanted and not retask and not self._closed:
                    verdict = "landing"
                elif outside is None:
                    accepted = self._store_plan(msg, lanes, moves, alt_cm, carried_step, start_step, retask)
                    self._plan_identity = identity
                    if identity.order is not None:
                        self._plan_newest = max(identity.order, self._plan_newest or identity.order)
                    self._plan_valid = True
                    self._plan_content = content
                    self._on_complete = on_complete
                    self._hover_timeout_s = hover_timeout_s
            lanes_total = self._lanes_total
        if verdict == SAME_PLAN:
            self._log.info("edge_lanes_resent", drone_id=self.drone_id, plan_id=identity.plan_id)
            self._ack_lanes(msg, battery, lanes_total)
            return True
        if verdict != NEW_PLAN:
            return self._refuse_lanes(msg, verdict, lanes_received, plan_id=identity.plan_id)
        if outside is not None:
            # The nack is sent outside the lock, like every other message.
            return self._refuse_lanes(
                msg,
                "geofence",
                lanes_received,
                geofence_cm=self.envelope.geofence_cm,
                x_cm=round(outside[0], 1),
                y_cm=round(outside[1], 1),
            )
        lane_index, retask = accepted
        self._log.info(
            "edge_lanes_assigned",
            drone_id=self.drone_id,
            lanes_total=len(lanes),
            lane_index=lane_index,
            progress_carried=carried_step is not None,
            alt_slot_cm=self.envelope.alt_slot_cm,
            retask=retask,
            plan_id=identity.plan_id,
        )
        self._ack_lanes(msg, battery, len(lanes))
        return True

    def _path_outside(
        self, moves: list[tuple[int, int, str, int]], start_step: int
    ) -> tuple[float, float] | None:
        """First dead-reckoned point of ``moves[start_step:]`` outside the geofence, or None."""
        pose = self.envelope.pose_snapshot()
        for _, _, direction, distance in moves[start_step:]:
            pose.apply_move(direction, distance)
            if not self.envelope.inside_fence(pose.x_cm, pose.y_cm):
                return pose.x_cm, pose.y_cm
        return None

    def _store_plan(
        self,
        msg: dict[str, Any],
        lanes: list[tuple[str, int]],
        moves: list[tuple[int, int, str, int]],
        alt_cm: float | None,
        carried_step: int | None,
        start_step: int,
        retask: bool,
    ) -> tuple[int, bool]:
        """Make an accepted plan current. Caller holds ``_lock``.

        Returns the lane index to report and *retask*, whether the caller
        launched a re-task sequence for this plan (``_retask_wanted_locked``).
        """
        if alt_cm is not None and alt_cm != self.envelope.alt_slot_cm:
            self._log.warning(
                "edge_alt_slot_assigned",
                drone_id=self.drone_id,
                local_cm=self.envelope.alt_slot_cm,
                assigned_cm=alt_cm,
            )
            self.envelope.set_alt_slot(alt_cm)
        if carried_step is not None:
            # A move of the old plan that is still in flight counts
            # towards the new plan when it completes.
            self._linked_gens.add(self._plan_gen)
        else:
            self._linked_gens.clear()
        self._lanes = lanes
        self._moves = moves
        self._step = start_step
        self._lanes_total = len(lanes)
        self._plan_gen += 1
        self._zone = msg.get("zone")
        self._hover_since = None
        return self.lane_index, retask

    def _retask_wanted_locked(self) -> bool:
        """True if a new plan must be flown at once (a re-task). Caller holds ``_lock``.

        That is when the drone flies its lanes and nothing else runs. A
        holding drone launches nothing; ``resume`` does.
        """
        return (
            self._flying_lanes
            and self._is_flying()
            and self._land_reason is None
            and not self._held
            and not self._worker_alive()
        )

    def _ack_lanes(self, msg: dict[str, Any], battery: Any, lanes_total: int) -> None:
        """Acknowledge an ``assign_lanes``: the v1 form, or the v2 form for a command with a ``cmd_id``."""
        if self._is_v2(msg):
            self._ack(msg, battery=battery, lanes_total=lanes_total, plan_id=self.plan_id)
            return
        self._emit({
            "type": "ack",
            "for": "assign_lanes",
            "drone_id": self.drone_id,
            "battery": battery,
            "lanes_total": lanes_total,
        })

    def _refuse_lanes(self, msg: dict[str, Any], reason: str, lanes_total: int, **fields: Any) -> bool:
        """Log a refused lane plan and tell the ground with a ``nack``. Returns False.

        A v1 message gets the v1 nack, whose reason must be a v1 reason, so
        the v2-only reasons are reported to a v1 ground as ``invalid``.
        """
        self._log.warning("edge_assign_lanes_refused", drone_id=self.drone_id, reason=reason, **fields)
        if self._is_v2(msg):
            self._nack(msg, reason, lanes_total=lanes_total)
            return False
        self._emit({
            "type": "nack",
            "for": "assign_lanes",
            "drone_id": self.drone_id,
            "reason": reason if reason in NACK_REASONS else "invalid",
            "lanes_total": lanes_total,
        })
        return False

    def _carried_step(
        self, lanes: list[tuple[str, int]], moves: list[tuple[int, int, str, int]]
    ) -> int | None:
        """Step in a new plan that continues the current one, or None. Caller holds ``_lock``.

        Progress carries over only if every lane flown so far, including the
        lane in progress, is unchanged in the new plan.
        """
        if self._step < len(self._moves):
            current = self._moves[self._step][:2]
            lanes_fixed = current[0] + 1
        else:
            current = (self._lanes_total, 0)
            lanes_fixed = self._lanes_total
        if lanes[:lanes_fixed] != self._lanes[:lanes_fixed]:
            return None
        for index, move in enumerate(moves):
            if move[:2] >= current:
                return index
        return len(moves)
