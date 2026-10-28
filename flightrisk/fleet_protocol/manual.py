"""Manual takeover commands (ground -> edge) and their replies.

The dashboard lets the operator take one drone out of its search pattern and
fly it by hand (``docs/manual-takeover.md``). These commands carry that::

    {"type": "manual_hold", "cmd_id": "<id>", "drone_id": "drone-2"}
    {"type": "manual_move", "cmd_id": "<id>", "direction": "forward", "distance_cm": 30}
    {"type": "manual_move", "cmd_id": "<id>", "direction": "cw", "degrees": 30}
    {"type": "manual_takeoff", "cmd_id": "<id>"}
    {"type": "resume_search", "cmd_id": "<id>"}
    {"type": "land", "cmd_id": "<id>", "reason": "manual"}      (the manual land)

Rules:

* **Flight commands, fenced like the others.** Each one can move the drone
  or change who flies it, so each always carries a ``cmd_id`` (the edge runs
  a command at most once per id and answers every one), and ``epoch`` and
  ``leader_id`` as a pair. The fence is required on a protocol v2 link and
  on an edge that requires fences (multi-ground mode), exactly like
  ``takeoff``; on a v1 link (today's single dashboard) it is optional and
  checked when present, again like ``takeoff``. The multi-ground edge
  authority treats them like the other fenced commands (``stale_epoch``,
  ``not_authority``, ``cmd_id`` cache).
* **Not in the v1 or v2 command sets.** They are kept out of
  ``GROUND_TO_EDGE_TYPES``, ``FENCED_COMMAND_TYPES`` and ``COMMAND_TYPES``,
  so every existing rule for those sets is unchanged; the validators,
  ``shape_for_peer`` and the edge authority name ``MANUAL_COMMAND_TYPES``
  explicitly. They are valid on every link version, so the v1 dashboard can
  use them, and ``shape_for_peer`` copies them unchanged (the ``cmd_id``
  survives a v1 link).
* **Landing is never fenced and never refused.** The manual land is the
  ordinary ``land`` message with ``reason: "manual"``. Every edge, old or
  new, executes it through the existing landing path, and it is never
  nacked. There is no separate ``manual_land`` type on the wire, so an edge
  that does not know manual takeover still lands.
* **Replies.** An ``ack`` or ``nack`` whose ``for`` is one of the manual
  commands, valid on every link version, with ``drone_id`` and ``cmd_id``.
  A nack's ``reason`` is one of ``MANUAL_NACK_REASONS``. Both may carry
  ``detail`` (operator text), ``epoch_seen``, ``control_mode`` and the
  numeric fields of ``MANUAL_REPLY_NUMBERS`` (where the drone is, or which
  limit refused a move).
* **Compatibility.** An edge that knows these commands reports
  ``control_mode`` in its telemetry; the dashboard sends them only to such
  an edge and otherwise refuses locally with reason ``unsupported``. An
  older single-ground edge logs an unknown type and ignores it, and an older
  multi-ground edge's strict decoder drops it, so neither can be moved by
  one. Older grounds never send them.

This module imports only ``checks`` and ``constants``; ``validate`` and
``fencing`` call into it.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from flightrisk.fleet_protocol.checks import (
    ProtocolError,
    _check_structure,
    _is_int,
    _is_member,
    _require,
    _require_choice,
    _require_drone_id,
    _require_id,
    _require_int_range,
    _require_optional_number,
    _require_text,
    _short,
)
from flightrisk.fleet_protocol.constants import (
    ACK,
    LAND,
    MAX_EPOCH,
    NACK,
    NACK_REASONS_V2,
)

__all__ = [
    "CONTROL_MODES",
    "MANUAL_COMMAND_TYPES",
    "MANUAL_DEADMAN_S",
    "MANUAL_DIRECTIONS",
    "MANUAL_FLOOR_CM",
    "MANUAL_HOLD",
    "MANUAL_LAND_REASON",
    "MANUAL_MOVE",
    "MANUAL_NACK_REASONS",
    "MANUAL_REPLY_NUMBERS",
    "MANUAL_ROTATE_MAX_DEG",
    "MANUAL_ROTATE_MIN_DEG",
    "MANUAL_ROTATIONS",
    "MANUAL_STEP_MAX_CM",
    "MANUAL_STEP_MIN_CM",
    "MANUAL_TAKEOFF",
    "MANUAL_TRANSLATIONS",
    "RESUME_SEARCH",
    "is_manual_land",
    "is_manual_reply",
    "make_manual_ack",
    "make_manual_hold",
    "make_manual_land",
    "make_manual_move",
    "make_manual_nack",
    "make_manual_takeoff",
    "make_resume_search",
    "validate_manual_command",
    "validate_manual_reply",
]

MANUAL_HOLD = "manual_hold"
MANUAL_MOVE = "manual_move"
MANUAL_TAKEOFF = "manual_takeoff"
RESUME_SEARCH = "resume_search"
MANUAL_COMMAND_TYPES = frozenset({MANUAL_HOLD, MANUAL_MOVE, MANUAL_TAKEOFF, RESUME_SEARCH})
# The ``reason`` a manual land carries. It only changes the edge's log line;
# the landing itself is the ordinary one.
MANUAL_LAND_REASON = "manual"

# A manual move is one bounded step: a relative move of 20 to 100 cm (the
# Tello SDK minimum is 20 cm) or a rotation of 5 to 45 degrees. The drone
# flies it and then hovers on its own, so no manual command can leave the
# drone moving.
MANUAL_TRANSLATIONS = frozenset({"forward", "back", "left", "right", "up", "down"})
MANUAL_ROTATIONS = frozenset({"cw", "ccw"})
MANUAL_DIRECTIONS = MANUAL_TRANSLATIONS | MANUAL_ROTATIONS
MANUAL_STEP_MIN_CM = 20
MANUAL_STEP_MAX_CM = 100
MANUAL_ROTATE_MIN_DEG = 5
MANUAL_ROTATE_MAX_DEG = 45
# A manual descent may not end below this height (cm above the takeoff
# surface, as the Tello measures it).
MANUAL_FLOOR_CM = 40
# With no manual command for this long, an edge under manual control
# reports itself held (hovering, waiting for the operator).
MANUAL_DEADMAN_S = 1.5

# ``control_mode`` in telemetry and replies: ``autonomous`` (flying or ready
# to fly its plan), ``manual`` (under manual control, commanded within the
# last ``MANUAL_DEADMAN_S``), ``held`` (under manual control, no command for
# longer: it hovers in place).
CONTROL_MODES = frozenset({"autonomous", "manual", "held"})

# Why an edge refused a manual command: the v2 reasons plus ``not_manual``
# (the drone is not under manual control), ``ceiling`` (a climb would end
# above the ceiling limit, the ceiling minus its margin), ``floor`` (a
# descent would end below ``MANUAL_FLOOR_CM``) and ``unsupported`` (the
# edge, or this ground, cannot do it).
MANUAL_NACK_REASONS = NACK_REASONS_V2 | frozenset({"not_manual", "ceiling", "floor", "unsupported"})
# Optional numbers in a reply, all in cm or degrees, relative to the drone's
# takeoff point: where the drone is, the resume target, and the limit that
# refused a move.
MANUAL_REPLY_NUMBERS = (
    "x_cm",
    "y_cm",
    "heading_deg",
    "height_cm",
    "target_x_cm",
    "target_y_cm",
    "geofence_cm",
    "max_alt_slot_cm",
    "floor_cm",
)


def _validate_fence_pair(msg: Mapping[str, Any], *, required: bool) -> None:
    if required or "epoch" in msg or "leader_id" in msg:
        _require_int_range(msg, "epoch", 1, MAX_EPOCH)
        _require_id(msg, "leader_id")


def validate_manual_command(msg: Mapping[str, Any], *, proto: int = 1, require_fence: bool = False) -> None:
    """Check a manual command. Raises ``ProtocolError``.

    ``cmd_id`` is required on every link version. The fence pair is
    required when *proto* is 2 or more or *require_fence* is set, and
    checked whenever present.
    """
    _check_structure(msg)
    mtype = msg.get("type")
    if not _is_member(mtype, MANUAL_COMMAND_TYPES):
        raise ProtocolError(f"not a manual command: {_short(mtype)}")
    _require_id(msg, "cmd_id")
    _validate_fence_pair(msg, required=require_fence or (_is_int(proto) and proto >= 2))
    if "drone_id" in msg:
        _require_drone_id(msg)
    if mtype != MANUAL_MOVE:
        return
    direction = _require_choice(msg, "direction", MANUAL_DIRECTIONS)
    if direction in MANUAL_ROTATIONS:
        _require_int_range(msg, "degrees", MANUAL_ROTATE_MIN_DEG, MANUAL_ROTATE_MAX_DEG)
        if "distance_cm" in msg:
            raise ProtocolError("'manual_move' rotation must not carry 'distance_cm'")
    else:
        _require_int_range(msg, "distance_cm", MANUAL_STEP_MIN_CM, MANUAL_STEP_MAX_CM)
        if "degrees" in msg:
            raise ProtocolError("'manual_move' translation must not carry 'degrees'")


def is_manual_reply(msg: Mapping[str, Any]) -> bool:
    """True for an ``ack`` or ``nack`` answering a manual command."""
    return msg.get("type") in (ACK, NACK) and _is_member(msg.get("for"), MANUAL_COMMAND_TYPES)


def is_manual_land(msg: Mapping[str, Any]) -> bool:
    """True for a ``land`` the operator sent from the manual controls."""
    return msg.get("type") == LAND and msg.get("reason") == MANUAL_LAND_REASON


def validate_manual_reply(msg: Mapping[str, Any]) -> None:
    """Check an ``ack`` or ``nack`` for a manual command (any link version). Raises ``ProtocolError``."""
    _require_drone_id(msg)
    _require_id(msg, "cmd_id")
    if "epoch_seen" in msg:
        _require_int_range(msg, "epoch_seen", 0, MAX_EPOCH)
    if "detail" in msg:
        _require_text(msg, "detail")
    if "control_mode" in msg:
        _require_choice(msg, "control_mode", CONTROL_MODES)
    if "plan_id" in msg:
        _require_id(msg, "plan_id", nullable=True)
    for key in MANUAL_REPLY_NUMBERS:
        if key in msg:
            _require_optional_number(msg, key)
    if msg.get("type") == ACK:
        if "result" in msg and msg["result"] != "ok":
            raise ProtocolError(f"'ack' field 'result' must be 'ok', got {_short(msg['result'])}")
        return
    reason = _require(msg, "reason")
    if not _is_member(reason, MANUAL_NACK_REASONS):
        raise ProtocolError(
            f"'nack' field 'reason' must be one of {sorted(MANUAL_NACK_REASONS)}, got {_short(reason)}"
        )


def _command(mtype: str, *, drone_id: str | None, cmd_id: str | None, fence: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": mtype, "cmd_id": cmd_id if cmd_id is not None else uuid.uuid4().hex}
    if drone_id is not None:
        msg["drone_id"] = drone_id
    if fence is not None:
        # A ``Fence`` (``fencing.Fence``) or an ``(epoch, leader_id)`` pair.
        fields = fence.fields() if hasattr(fence, "fields") else {"epoch": fence[0], "leader_id": fence[1]}
        msg.update(fields)
    return msg


def make_manual_hold(*, drone_id: str | None = None, cmd_id: str | None = None, fence: Any = None) -> dict[str, Any]:
    """Ground -> edge: take manual control; pause the search and hover in place."""
    msg = _command(MANUAL_HOLD, drone_id=drone_id, cmd_id=cmd_id, fence=fence)
    validate_manual_command(msg, require_fence=fence is not None)
    return msg


def make_manual_move(
    direction: str,
    *,
    distance_cm: int | None = None,
    degrees: int | None = None,
    drone_id: str | None = None,
    cmd_id: str | None = None,
    fence: Any = None,
) -> dict[str, Any]:
    """Ground -> edge: one bounded manual step (a move of *distance_cm* or a rotation of *degrees*)."""
    msg = _command(MANUAL_MOVE, drone_id=drone_id, cmd_id=cmd_id, fence=fence)
    msg["direction"] = direction
    if direction in MANUAL_ROTATIONS:
        msg["degrees"] = degrees
    else:
        msg["distance_cm"] = distance_cm
    validate_manual_command(msg, require_fence=fence is not None)
    return msg


def make_manual_takeoff(*, drone_id: str | None = None, cmd_id: str | None = None, fence: Any = None) -> dict[str, Any]:
    """Ground -> edge: take off under manual control (same preflight as any takeoff) and climb to the slot."""
    msg = _command(MANUAL_TAKEOFF, drone_id=drone_id, cmd_id=cmd_id, fence=fence)
    validate_manual_command(msg, require_fence=fence is not None)
    return msg


def make_resume_search(*, drone_id: str | None = None, cmd_id: str | None = None, fence: Any = None) -> dict[str, Any]:
    """Ground -> edge: end manual control and continue the search from where it was paused."""
    msg = _command(RESUME_SEARCH, drone_id=drone_id, cmd_id=cmd_id, fence=fence)
    validate_manual_command(msg, require_fence=fence is not None)
    return msg


def make_manual_land(*, cmd_id: str | None = None) -> dict[str, Any]:
    """Ground -> edge: the manual land. An ordinary, never-fenced ``land`` with ``reason: manual``."""
    return {"type": LAND, "cmd_id": cmd_id if cmd_id is not None else uuid.uuid4().hex, "reason": MANUAL_LAND_REASON}


def make_manual_ack(for_type: str, drone_id: str, cmd_id: str, **fields: Any) -> dict[str, Any]:
    """Edge -> ground: the manual command *cmd_id* was accepted (or carried out)."""
    msg: dict[str, Any] = {"type": ACK, "for": for_type, "drone_id": drone_id, "cmd_id": cmd_id, "result": "ok"}
    msg.update({key: value for key, value in fields.items() if value is not None})
    validate_manual_reply(msg)
    return msg


def make_manual_nack(for_type: str, drone_id: str, cmd_id: str, reason: str, **fields: Any) -> dict[str, Any]:
    """Edge -> ground: the manual command *cmd_id* was refused for *reason*."""
    msg: dict[str, Any] = {"type": NACK, "for": for_type, "drone_id": drone_id, "cmd_id": cmd_id, "reason": reason}
    msg.update({key: value for key, value in fields.items() if value is not None})
    validate_manual_reply(msg)
    return msg
