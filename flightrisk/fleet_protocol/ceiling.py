"""The ``set_ceiling`` configuration command (ground -> edge) and its replies.

The dashboard sends each edge the room's ceiling height, the safety margin
kept below it and that drone's altitude slot (``docs/ceiling-and-altitude.md``)::

    {"type": "set_ceiling", "cmd_id": "<id>", "drone_id": "drone-2",
     "ceiling_cm": 200, "ceiling_margin_cm": 60, "alt_slot_cm": 80}

Rules:

* **A configuration command, not a flight command.** It never moves a drone
  and never changes who commands it. It is valid on every link version and
  always carries a ``cmd_id``. It is not in ``GROUND_TO_EDGE_TYPES``,
  ``COMMAND_TYPES`` or ``FENCED_COMMAND_TYPES``, so the v1 and v2 rules for
  flight commands are unchanged.
* **Fence.** ``epoch`` and ``leader_id`` are optional and checked as a pair
  when present. A protocol v2 leader ground adds its fence, and an edge
  that follows several grounds obeys the command only from its authority at
  the current epoch (``stale_epoch`` or ``not_authority`` otherwise).
* **Compatibility.** An older single-ground edge logs the unknown type and
  ignores it; an older multi-ground edge's strict decoder drops it. The
  dashboard sends it only to an edge whose telemetry reports ``ceiling_cm``,
  which only edges that understand it do. Older grounds never send it.
* **The edge decides.** The protocol only checks that the three heights are
  positive numbers. The edge applies them only while its drone is on the
  ground, through ``SafetyEnvelope.configure_ceiling``, and refuses anything
  else with a reason from ``CEILING_NACK_REASONS``.
* **Replies.** An ``ack`` or ``nack`` whose ``for`` is ``set_ceiling``, on
  every link version, with ``drone_id`` and ``cmd_id``. An ack carries the
  now active ``ceiling_cm``, ``ceiling_margin_cm`` and ``alt_slot_cm``; a
  nack carries ``reason`` and may carry those three (the values still
  active), ``detail`` and ``epoch_seen``. ``shape_for_peer`` copies the
  command and its replies unchanged, so the ``cmd_id`` survives a v1 link.
* **Landing is unaffected.** Nothing here touches ``land`` or
  ``emergency_land_all``.

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
    _is_member,
    _require,
    _require_drone_id,
    _require_id,
    _require_int_range,
    _require_number,
    _require_text,
    _short,
)
from flightrisk.fleet_protocol.constants import (
    ACK,
    MAX_EPOCH,
    MAX_LANE_DISTANCE_CM,
    NACK,
)

__all__ = [
    "CEILING_FIELDS",
    "CEILING_NACK_REASONS",
    "SET_CEILING",
    "is_ceiling_reply",
    "make_ceiling_ack",
    "make_ceiling_nack",
    "make_set_ceiling",
    "validate_ceiling_reply",
    "validate_set_ceiling",
]

SET_CEILING = "set_ceiling"
# The heights the command sets and the replies report, all in cm.
CEILING_FIELDS = ("ceiling_cm", "ceiling_margin_cm", "alt_slot_cm")
# Why an edge refused a ``set_ceiling``. ``in_flight``: the drone is flying,
# or a takeoff, a scheduled start, lanes or a landing is under way (changes
# apply only on the ground). ``above_ceiling``: the slot is above the new
# ceiling minus margin. ``invalid``: a height out of range or a malformed
# command. ``stale_epoch``/``not_authority``: not from the edge's authority.
CEILING_NACK_REASONS = frozenset({"in_flight", "above_ceiling", "invalid", "stale_epoch", "not_authority"})


def _require_height(msg: Mapping[str, Any], key: str) -> float:
    value = _require_number(msg, key)
    if not 0 < value <= MAX_LANE_DISTANCE_CM:
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be above 0 and at most {MAX_LANE_DISTANCE_CM}, "
            f"got {_short(value)}"
        )
    return value


def validate_set_ceiling(msg: Mapping[str, Any]) -> None:
    """Check a ``set_ceiling`` command (any link version). Raises ``ProtocolError``."""
    _check_structure(msg)
    _require_id(msg, "cmd_id")
    for key in CEILING_FIELDS:
        _require_height(msg, key)
    if "drone_id" in msg:
        _require_drone_id(msg)
    if "epoch" in msg or "leader_id" in msg:
        _require_int_range(msg, "epoch", 1, MAX_EPOCH)
        _require_id(msg, "leader_id")


def is_ceiling_reply(msg: Mapping[str, Any]) -> bool:
    """True for an ``ack`` or ``nack`` answering ``set_ceiling``."""
    return msg.get("type") in (ACK, NACK) and msg.get("for") == SET_CEILING


def validate_ceiling_reply(msg: Mapping[str, Any]) -> None:
    """Check an ``ack`` or ``nack`` for ``set_ceiling`` (any link version). Raises ``ProtocolError``."""
    _require_drone_id(msg)
    _require_id(msg, "cmd_id")
    if "epoch_seen" in msg:
        _require_int_range(msg, "epoch_seen", 0, MAX_EPOCH)
    if "detail" in msg:
        _require_text(msg, "detail")
    if msg.get("type") == ACK:
        if "result" in msg and msg["result"] != "ok":
            raise ProtocolError(f"'ack' field 'result' must be 'ok', got {_short(msg['result'])}")
        for key in CEILING_FIELDS:
            _require_height(msg, key)
        return
    reason = _require(msg, "reason")
    if not _is_member(reason, CEILING_NACK_REASONS):
        raise ProtocolError(
            f"'nack' field 'reason' must be one of {sorted(CEILING_NACK_REASONS)}, got {_short(reason)}"
        )
    for key in CEILING_FIELDS:
        if key in msg:
            _require_height(msg, key)


def make_set_ceiling(
    ceiling_cm: float,
    ceiling_margin_cm: float,
    alt_slot_cm: float,
    *,
    drone_id: str | None = None,
    cmd_id: str | None = None,
    fence: tuple[int, str] | None = None,
) -> dict[str, Any]:
    """Ground -> edge: set the ceiling, its margin and this drone's slot.

    Carries a fresh ``cmd_id`` unless one is given. *fence* is the
    ``(epoch, leader_id)`` of a protocol v2 leader ground, or None.
    """
    msg: dict[str, Any] = {
        "type": SET_CEILING,
        "cmd_id": cmd_id if cmd_id is not None else uuid.uuid4().hex,
        "ceiling_cm": ceiling_cm,
        "ceiling_margin_cm": ceiling_margin_cm,
        "alt_slot_cm": alt_slot_cm,
    }
    if drone_id is not None:
        msg["drone_id"] = drone_id
    if fence is not None:
        msg["epoch"], msg["leader_id"] = fence
    validate_set_ceiling(msg)
    return msg


def make_ceiling_ack(drone_id: str, cmd_id: str, active: Mapping[str, float]) -> dict[str, Any]:
    """Edge -> ground: the ``set_ceiling`` *cmd_id* was applied; *active* holds the three heights now in force."""
    msg: dict[str, Any] = {"type": ACK, "for": SET_CEILING, "drone_id": drone_id, "cmd_id": cmd_id, "result": "ok"}
    msg.update({key: active[key] for key in CEILING_FIELDS})
    validate_ceiling_reply(msg)
    return msg


def make_ceiling_nack(
    drone_id: str,
    cmd_id: str,
    reason: str,
    *,
    active: Mapping[str, float] | None = None,
    detail: str | None = None,
    epoch_seen: int | None = None,
) -> dict[str, Any]:
    """Edge -> ground: the ``set_ceiling`` *cmd_id* was refused for *reason*; *active* is what stays in force."""
    msg: dict[str, Any] = {"type": NACK, "for": SET_CEILING, "drone_id": drone_id, "cmd_id": cmd_id, "reason": reason}
    if active is not None:
        msg.update({key: active[key] for key in CEILING_FIELDS})
    if detail is not None:
        msg["detail"] = detail
    if epoch_seen is not None:
        msg["epoch_seen"] = epoch_seen
    validate_ceiling_reply(msg)
    return msg
