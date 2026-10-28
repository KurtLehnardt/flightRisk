"""Message builders. Every builder validates what it produces."""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from flightrisk.fleet_protocol.checks import (
    ProtocolError,
    _is_id,
    _require_number,
    _short,
)
from flightrisk.fleet_protocol.constants import (
    ACK,
    ASSIGN_LANES,
    CLOCK,
    DEFAULT_HOVER_TIMEOUT_S,
    EMERGENCY_LAND_ALL,
    HANDOVER,
    HANDOVER_ACCEPT,
    HANDOVER_OFFER,
    HANDOVER_REJECT,
    HEARTBEAT,
    HOLD,
    LAND,
    NACK,
    NACK_GEOFENCE_FIELDS,
    NODE_STATE,
    NUDGE,
    PEER_HEARTBEAT,
    PING,
    PONG,
    PROTOCOL_VERSION,
    RESUME,
    SET_DETECTION_PROFILE,
    START_AT,
    TAKEOFF,
    TELEMETRY,
)
from flightrisk.fleet_protocol.fencing import (
    EdgeHello,
    Fence,
    GroundHello,
    new_cmd_id,
    next_epoch,
)
from flightrisk.fleet_protocol.validate import (
    _checked,
)

# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------
#
# Every builder validates what it produces, so a malformed message fails
# loudly at the sender instead of being dropped by the receiver. Builders
# that take a ``fence`` produce the v2 form (with a fresh ``cmd_id`` unless
# one is given for a retry) and validate it under v2 rules; without a fence
# they produce exactly the v1 form.


def _fenced(msg: dict[str, Any], fence: Fence | None, cmd_id: str | None, *, command: bool = True) -> int:
    """Add fence and ``cmd_id`` fields in place; return the protocol version to validate with."""
    if fence is None:
        if cmd_id is not None:
            msg["cmd_id"] = cmd_id
        return 1
    if not isinstance(fence, Fence):
        raise ProtocolError(f"fence must be a Fence, got {type(fence).__name__}")
    msg.update(fence.fields())
    if command:
        msg["cmd_id"] = new_cmd_id() if cmd_id is None else cmd_id
    return 2


def make_ping(t1: float | None = None) -> dict[str, Any]:
    """Ground -> edge clock probe; ``t1`` defaults to ``time.monotonic()``."""
    return _checked({"type": PING, "t1": time.monotonic() if t1 is None else t1})


def make_pong(t1: float, t2: float, t3: float) -> dict[str, Any]:
    """Edge -> ground reply carrying the echoed ``t1`` and the edge's ``t2``/``t3``."""
    return _checked({"type": PONG, "t1": t1, "t2": t2, "t3": t3})


def answer_ping(
    ping: Mapping[str, Any],
    t2: float,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Build the pong for *ping*, which the edge received at edge time *t2*.

    ``t3`` is read from *clock* (the edge's monotonic clock) as the last step,
    so the caller should send the returned message straight away.
    """
    if ping.get("type") != PING:
        raise ProtocolError(f"answer_ping expects a 'ping' message, got {ping.get('type')!r}")
    t1 = _require_number(ping, "t1")
    return make_pong(t1, t2, clock())


def make_clock(offset_s: float, *, fence: Fence | None = None) -> dict[str, Any]:
    """Ground -> edge: the median offset (edge clock minus ground clock), unchanged."""
    msg: dict[str, Any] = {"type": CLOCK, "offset_s": offset_s}
    return _checked(msg, _fenced(msg, fence, None, command=False))


def make_assign_lanes(
    drone_id: str,
    alt_cm: float,
    lanes: Iterable[Mapping[str, Any]],
    zone: Mapping[str, Any],
    *,
    fence: Fence | None = None,
    cmd_id: str | None = None,
    plan_id: str | None = None,
    plan_seq: int | None = None,
    on_complete: str | None = None,
    hover_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Ground -> edge lane assignment for one drone.

    The v2 form (with *fence*) needs *plan_id* and *plan_seq*. *plan_seq*
    is an integer from 1 that the leader increases with every new plan it
    issues within its epoch; a retry of the same plan reuses it. The edge
    orders plans by ``(epoch, plan_seq)`` and refuses an older one with
    ``stale_plan``. *on_complete* says what the drone does after its last
    lane: ``"hover"`` for *hover_timeout_s* (default
    ``DEFAULT_HOVER_TIMEOUT_S``) and then land, or ``"land"``.
    """
    msg: dict[str, Any] = {
        "type": ASSIGN_LANES,
        "drone_id": drone_id,
        "alt_cm": alt_cm,
        "lanes": [dict(lane) for lane in lanes],
        "zone": dict(zone),
    }
    if plan_id is not None:
        msg["plan_id"] = plan_id
    if plan_seq is not None:
        msg["plan_seq"] = plan_seq
    if on_complete is not None:
        msg["on_complete"] = on_complete
        if on_complete == "hover":
            msg["hover_timeout_s"] = DEFAULT_HOVER_TIMEOUT_S if hover_timeout_s is None else hover_timeout_s
    if hover_timeout_s is not None and "hover_timeout_s" not in msg:
        msg["hover_timeout_s"] = hover_timeout_s
    return _checked(msg, _fenced(msg, fence, cmd_id))


def make_start_at(
    t_ground_s: float,
    *,
    fence: Fence | None = None,
    cmd_id: str | None = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Ground -> edge: start (take off, then fly lanes) at this ground monotonic time.

    The v2 form (with *fence*) needs the *plan_id* of the plan to start; the
    edge refuses a mismatch with ``plan_mismatch``.
    """
    msg: dict[str, Any] = {"type": START_AT, "t_ground_s": t_ground_s}
    if plan_id is not None:
        msg["plan_id"] = plan_id
    return _checked(msg, _fenced(msg, fence, cmd_id))


def make_takeoff(*, fence: Fence | None = None, cmd_id: str | None = None) -> dict[str, Any]:
    """Ground -> edge: take off to the altitude slot, then hover. Never resent automatically."""
    msg: dict[str, Any] = {"type": TAKEOFF}
    return _checked(msg, _fenced(msg, fence, cmd_id))


def make_land(*, cmd_id: str | None = None, fence: Fence | None = None) -> dict[str, Any]:
    """Ground -> edge: land now. Never fenced; *cmd_id* and *fence* are informational."""
    msg: dict[str, Any] = {"type": LAND}
    if cmd_id is not None:
        if not _is_id(cmd_id):
            raise ProtocolError(f"cmd_id must be an identifier, got {_short(cmd_id)}")
        msg["cmd_id"] = cmd_id
    if fence is not None:
        if not isinstance(fence, Fence):
            raise ProtocolError(f"fence must be a Fence, got {type(fence).__name__}")
        msg.update(fence.fields())
    return msg


def make_emergency_land_all() -> dict[str, Any]:
    """Ground -> every edge: land immediately, ahead of anything else."""
    return {"type": EMERGENCY_LAND_ALL}


def make_heartbeat(seq: int, *, fence: Fence | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": HEARTBEAT, "seq": seq}
    return _checked(msg, _fenced(msg, fence, None, command=False))


@dataclass(frozen=True)
class TelemetryV2:
    """The fields protocol v2 adds to ``telemetry``."""

    epoch: int
    plan_id: str | None
    state: str
    plan_valid: bool

    def fields(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "plan_id": self.plan_id, "state": self.state, "plan_valid": self.plan_valid}


def make_telemetry(
    drone_id: str,
    battery: float | None,
    height_cm: float | None,
    is_flying: bool,
    rel_x_cm: float | None,
    rel_y_cm: float | None,
    heading_deg: float | None,
    lane_index: int | None,
    lanes_total: int | None,
    link_age_s: float | None,
    takeoff_at_local_s: float | None,
    *,
    v2: TelemetryV2 | None = None,
) -> dict[str, Any]:
    """Edge -> ground state report, sent every ``TELEMETRY_INTERVAL_S``."""
    msg: dict[str, Any] = {
        "type": TELEMETRY,
        "drone_id": drone_id,
        "battery": battery,
        "height_cm": height_cm,
        "is_flying": is_flying,
        "rel_x_cm": rel_x_cm,
        "rel_y_cm": rel_y_cm,
        "heading_deg": heading_deg,
        "lane_index": lane_index,
        "lanes_total": lanes_total,
        "link_age_s": link_age_s,
        "takeoff_at_local_s": takeoff_at_local_s,
    }
    if v2 is None:
        return _checked(msg)
    msg.update(v2.fields())
    return _checked(msg, 2)


def make_ack_start_at(drone_id: str, executed_at_ground_s: float, *, cmd_id: str | None = None) -> dict[str, Any]:
    """Edge -> ground: the edge executed ``start_at`` at this time, in ground time."""
    if cmd_id is not None:
        return make_ack(START_AT, drone_id, cmd_id, executed_at_ground_s=executed_at_ground_s)
    return _checked({
        "type": ACK,
        "for": START_AT,
        "drone_id": drone_id,
        "executed_at_ground_s": executed_at_ground_s,
    })


def make_ack_assign_lanes(
    drone_id: str, battery: float, lanes_total: int, *, cmd_id: str | None = None
) -> dict[str, Any]:
    """Edge -> ground: the edge accepted its lane assignment."""
    if cmd_id is not None:
        return make_ack(ASSIGN_LANES, drone_id, cmd_id, battery=battery, lanes_total=lanes_total)
    return _checked({
        "type": ACK,
        "for": ASSIGN_LANES,
        "drone_id": drone_id,
        "battery": battery,
        "lanes_total": lanes_total,
    })


def _geofence_fields(geofence: Mapping[str, float] | None) -> dict[str, float]:
    """The optional geofence fields of an ``assign_lanes`` nack (``NACK_GEOFENCE_FIELDS``)."""
    if not geofence:
        return {}
    unknown = set(geofence) - set(NACK_GEOFENCE_FIELDS)
    if unknown:
        raise ProtocolError(f"unknown geofence nack fields: {sorted(unknown)}")
    return dict(geofence)


def make_nack_assign_lanes(
    drone_id: str, reason: str, lanes_total: int, *, geofence: Mapping[str, float] | None = None
) -> dict[str, Any]:
    """Edge -> ground: the edge refused its lane assignment for ``reason``.

    ``lanes_total`` is the number of lanes in the refused assignment, so the
    ground can tell which of its assignments was refused. *geofence* adds
    the optional fields of ``NACK_GEOFENCE_FIELDS`` to a ``geofence`` nack.
    """
    return _checked({
        "type": NACK,
        "for": ASSIGN_LANES,
        "drone_id": drone_id,
        "reason": reason,
        "lanes_total": lanes_total,
        **_geofence_fields(geofence),
    })


def make_ground_hello(node_id: str, role: str, epoch: int, *, proto: int = PROTOCOL_VERSION) -> dict[str, Any]:
    """Ground -> edge (first message on every connection, and again on any role or epoch change)."""
    return GroundHello(node_id=node_id, role=role, epoch=epoch, proto=proto).to_message()


def make_edge_hello(
    drone_id: str,
    node_id: str,
    epoch_seen: int,
    *,
    plan_id: str | None,
    lane_index: int | None,
    is_flying: bool,
    battery: float | None,
    proto: int = PROTOCOL_VERSION,
) -> dict[str, Any]:
    """Edge -> ground, sent in reply to each ground ``hello`` (never before one)."""
    return EdgeHello(
        drone_id=drone_id,
        node_id=node_id,
        epoch_seen=epoch_seen,
        plan_id=plan_id,
        lane_index=lane_index,
        is_flying=is_flying,
        battery=battery,
        proto=proto,
    ).to_message()


def make_hold(fence: Fence, *, cmd_id: str | None = None, reason: str | None = None) -> dict[str, Any]:
    """Ground -> edge (v2): stop and hover in place, keeping the plan for a later ``resume``."""
    msg: dict[str, Any] = {"type": HOLD}
    if reason is not None:
        msg["reason"] = reason
    return _checked(msg, _fenced(msg, _need_fence(fence), cmd_id))


def make_resume(fence: Fence, *, plan_id: str, cmd_id: str | None = None) -> dict[str, Any]:
    """Ground -> edge (v2): continue the held plan *plan_id*.

    The edge answers a *plan_id* that is not its current plan with nack
    ``plan_mismatch``. A resume is never resent automatically.
    """
    msg: dict[str, Any] = {"type": RESUME, "plan_id": plan_id}
    return _checked(msg, _fenced(msg, _need_fence(fence), cmd_id))


def make_nudge(fence: Fence, direction: str, distance_cm: int, *, cmd_id: str | None = None) -> dict[str, Any]:
    """Ground -> edge (v2): one horizontal move of 20-500 cm while hovering or holding."""
    msg: dict[str, Any] = {"type": NUDGE, "direction": direction, "distance_cm": distance_cm}
    return _checked(msg, _fenced(msg, _need_fence(fence), cmd_id))


def make_handover(
    fence: Fence,
    to_node: str,
    *,
    epoch_next: int | None = None,
    cmd_id: str | None = None,
) -> dict[str, Any]:
    """Ground -> edge (v2): leadership passes to *to_node* at *epoch_next*.

    *epoch_next* defaults to ``next_epoch(fence.epoch)`` and must equal it;
    at ``MAX_EPOCH`` there is none, and this raises ``ProtocolError``.
    """
    fence = _need_fence(fence)
    msg: dict[str, Any] = {
        "type": HANDOVER,
        "to_node": to_node,
        "epoch_next": next_epoch(fence.epoch) if epoch_next is None else epoch_next,
    }
    return _checked(msg, _fenced(msg, fence, cmd_id))


def _need_fence(fence: Any) -> Fence:
    if not isinstance(fence, Fence):
        raise ProtocolError(f"v2 commands need a Fence, got {type(fence).__name__}")
    return fence


def make_ack(
    for_type: str,
    drone_id: str,
    cmd_id: str | None,
    *,
    epoch_seen: int | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Edge -> ground (v2): generic ack for the command *for_type* with *cmd_id*.

    *cmd_id* may be ``None`` only for a ``land``/``emergency_land_all`` that
    carried none. Type-specific fields go in *fields*: ``executed_at_ground_s``
    for ``start_at``; ``battery`` and ``lanes_total`` for ``assign_lanes``.
    """
    msg: dict[str, Any] = {"type": ACK, "for": for_type, "drone_id": drone_id, "result": "ok"}
    if cmd_id is not None:
        msg["cmd_id"] = cmd_id
    if epoch_seen is not None:
        msg["epoch_seen"] = epoch_seen
    msg.update(fields)
    return _checked(msg, 2)


def make_nack(
    for_type: str,
    drone_id: str,
    reason: str,
    cmd_id: str,
    *,
    epoch_seen: int | None = None,
    lanes_total: int | None = None,
    detail: str | None = None,
    geofence: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Edge -> ground (v2): the edge refused the fenced command *for_type* for *reason*.

    Pass *epoch_seen* with ``stale_epoch``/``not_authority`` so a stale
    leader learns the epoch it lost to. ``lanes_total`` is required for an
    ``assign_lanes`` nack. *geofence* adds the optional fields of
    ``NACK_GEOFENCE_FIELDS`` to an ``assign_lanes`` nack. Landing is never
    refused, so it is never nacked.
    """
    msg: dict[str, Any] = {"type": NACK, "for": for_type, "drone_id": drone_id, "cmd_id": cmd_id, "reason": reason}
    if epoch_seen is not None:
        msg["epoch_seen"] = epoch_seen
    if lanes_total is not None:
        msg["lanes_total"] = lanes_total
    if detail is not None:
        msg["detail"] = detail
    msg.update(_geofence_fields(geofence))
    return _checked(msg, 2)


def make_node_state(
    node_id: str,
    role: str,
    epoch: int,
    *,
    roster: Iterable[Mapping[str, Any]] = (),
    mission: Mapping[str, Any] | None = None,
    latch: Mapping[str, Any] | None = None,
    detections: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Ground -> ground: this node's view, sent at 1 Hz and on change. Never carries imagery."""
    return _checked({
        "type": NODE_STATE,
        "node_id": node_id,
        "role": role,
        "epoch": epoch,
        "proto": PROTOCOL_VERSION,
        "roster": [dict(entry) for entry in roster],
        "mission": None if mission is None else dict(mission),
        "latch": None if latch is None else dict(latch),
        "detections": [dict(det) for det in detections],
    }, 2)


def make_handover_offer(epoch_next: int, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Leader -> standby: offer leadership at *epoch_next* with the leader's ``node_state``.

    *epoch_next* must be ``next_epoch(snapshot["epoch"])``.
    """
    return _checked({"type": HANDOVER_OFFER, "epoch_next": epoch_next, "snapshot": dict(snapshot)}, 2)


def make_handover_accept(epoch_next: int) -> dict[str, Any]:
    """Standby -> leader: accepted; the standby has persisted *epoch_next*."""
    return _checked({"type": HANDOVER_ACCEPT, "epoch_next": epoch_next}, 2)


def make_handover_reject(reason: str, *, epoch_next: int | None = None) -> dict[str, Any]:
    """Standby -> leader: refused, with a short human-readable *reason*."""
    msg: dict[str, Any] = {"type": HANDOVER_REJECT, "reason": reason}
    if epoch_next is not None:
        msg["epoch_next"] = epoch_next
    return _checked(msg, 2)


def make_peer_heartbeat(seq: int, epoch: int, *, node_id: str | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": PEER_HEARTBEAT, "seq": seq, "epoch": epoch}
    if node_id is not None:
        msg["node_id"] = node_id
    return _checked(msg, 2)


# ---------------------------------------------------------------------------
# Configuration commands (any link version)
# ---------------------------------------------------------------------------


def make_set_detection_profile(
    profile: str, *, drone_id: str | None = None, cmd_id: str | None = None
) -> dict[str, Any]:
    """Ground -> edge: switch the edge's detection profile (model and detection settings).

    Carries a fresh ``cmd_id`` unless one is given. It is not fenced and is
    valid on every link version; the edge answers with ``make_profile_ack``
    or ``make_profile_nack``.
    """
    msg: dict[str, Any] = {
        "type": SET_DETECTION_PROFILE,
        "profile": profile,
        "cmd_id": cmd_id if cmd_id is not None else new_cmd_id(),
    }
    if drone_id is not None:
        msg["drone_id"] = drone_id
    return _checked(msg)


def make_profile_ack(
    drone_id: str,
    cmd_id: str,
    profile: str,
    *,
    requested: str | None = None,
    fallback: bool = False,
    fallback_reason: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Edge -> ground: *profile* is now active for the ``set_detection_profile`` *cmd_id*.

    *fallback* is true when *profile* is not the one *requested* (for
    example its weights file is missing), with *fallback_reason*.
    """
    msg: dict[str, Any] = {
        "type": ACK,
        "for": SET_DETECTION_PROFILE,
        "drone_id": drone_id,
        "cmd_id": cmd_id,
        "result": "ok",
        "profile": profile,
        "fallback": bool(fallback),
    }
    if requested is not None:
        msg["requested"] = requested
    if fallback:
        msg["fallback_reason"] = fallback_reason
    if detail is not None:
        msg["detail"] = detail
    return _checked(msg)


def make_profile_nack(
    drone_id: str,
    cmd_id: str,
    reason: str,
    *,
    profile: str | None = None,
    requested: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Edge -> ground: the ``set_detection_profile`` *cmd_id* was refused for *reason*.

    *profile* is the profile that stays active; *detail* is a short
    operator-facing explanation (printable, at most 200 characters).
    """
    msg: dict[str, Any] = {
        "type": NACK,
        "for": SET_DETECTION_PROFILE,
        "drone_id": drone_id,
        "cmd_id": cmd_id,
        "reason": reason,
    }
    if profile is not None:
        msg["profile"] = profile
    if requested is not None:
        msg["requested"] = requested
    if detail is not None:
        msg["detail"] = detail
    return _checked(msg)
