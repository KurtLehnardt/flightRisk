"""Schema checks for the ground-to-ground ``node_state`` message (peer link, v2 only)."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from flightrisk.fleet_protocol.checks import (
    ProtocolError,
    _is_id,
    _is_int,
    _is_number,
    _require,
    _require_bool,
    _require_choice,
    _require_id,
    _require_int_range,
    _require_list,
    _require_number,
    _require_object,
    _require_only,
    _short,
    _validate_proto_field,
)
from flightrisk.fleet_protocol.constants import (
    _DETECTION_FIELDS,
    _LATCH_FIELDS,
    _MAX_SAFE_INT,
    _MISSION_FIELDS,
    _NODE_STATE_FIELDS,
    _ROSTER_FIELDS,
    GROUND_ROLES,
    LATCH_PHASES,
    MAX_DETECTIONS,
    MAX_EPOCH,
    MAX_NODE_STATE_BYTES,
    MAX_ROSTER,
    MISSION_STATES,
)


def _validate_number_pair(value: Any, name: str, *, positive: bool = False, size: int = 2) -> None:
    if not (isinstance(value, list) and len(value) == size and all(_is_number(v) for v in value)):
        raise ProtocolError(f"{name} must be a list of {size} finite numbers, got {_short(value)}")
    if positive and any(v <= 0 for v in value):
        raise ProtocolError(f"{name} must be positive, got {_short(value)}")


def _plain(value: Any) -> Any:
    """JSON ``default`` hook: serialise a non-dict mapping as an object."""
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{type(value).__name__} is not JSON")


def _compact_size(msg: Mapping[str, Any]) -> int:
    """Bytes of *msg* as compact JSON (as ``encode`` sends it)."""
    try:
        text = json.dumps(msg, separators=(",", ":"), allow_nan=False, default=_plain)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolError(f"'node_state' cannot be encoded as JSON: {_short(str(exc))}") from exc
    return len(text.encode("utf-8"))


def _validate_node_state(msg: Mapping[str, Any]) -> None:
    """Allowlisted fields, drone ids that are identifiers, and at most ``MAX_NODE_STATE_BYTES``."""
    _require_only(msg, _NODE_STATE_FIELDS, "'node_state'")
    _require_id(msg, "node_id")
    _require_choice(msg, "role", GROUND_ROLES)
    _require_int_range(msg, "epoch", 0, MAX_EPOCH)
    _validate_proto_field(msg)
    for index, entry in enumerate(_require_list(msg, "roster", MAX_ROSTER)):
        if not isinstance(entry, Mapping):
            raise ProtocolError(f"'node_state' roster entry {index} must be an object")
        _require_only(entry, _ROSTER_FIELDS, f"'node_state' roster entry {index}")
        _require_id(entry, "drone_id")
        _require_id(entry, "node_id")
        _require_bool(entry, "connected")
        _require_bool(entry, "is_flying")
        _require_int_range(entry, "epoch_seen", 0, MAX_EPOCH)
    mission = _require_object(msg, "mission", nullable=True)
    if mission is not None:
        _require_only(mission, _MISSION_FIELDS, "'node_state' mission")
        _require_id(mission, "plan_id", nullable=True)
        _require_id(mission, "pattern")
        _require_choice(mission, "state", MISSION_STATES)
        _validate_number_pair(_require(mission, "area_cm"), "'node_state' mission 'area_cm'", positive=True)
        lkp = _require(mission, "lkp_cm")
        if lkp is not None:
            _validate_number_pair(lkp, "'node_state' mission 'lkp_cm'")
        placements = _require_object(mission, "placements")
        if len(placements) > MAX_ROSTER:
            raise ProtocolError(f"'node_state' mission 'placements' has more than {MAX_ROSTER} drones")
        for drone_id, placement in placements.items():
            if not _is_id(drone_id):
                raise ProtocolError(
                    f"'node_state' placement key {_short(drone_id)} is not a drone id (an identifier)"
                )
            _validate_number_pair(placement, f"'node_state' placement {drone_id!r}", size=3)
    latch = _require_object(msg, "latch", nullable=True)
    if latch is not None:
        _require_only(latch, _LATCH_FIELDS, "'node_state' latch")
        _require_choice(latch, "phase", LATCH_PHASES)
        alert_id = _require(latch, "alert_id")
        if alert_id is not None and not (_is_int(alert_id) and 0 <= alert_id <= _MAX_SAFE_INT) \
                and not _is_id(alert_id):
            raise ProtocolError(f"'node_state' latch 'alert_id' must be an id or null, got {_short(alert_id)}")
    for index, det in enumerate(_require_list(msg, "detections", MAX_DETECTIONS)):
        if not isinstance(det, Mapping):
            raise ProtocolError(f"'node_state' detection {index} must be an object")
        _require_only(det, _DETECTION_FIELDS, f"'node_state' detection {index}")
        _require_int_range(det, "id", 0, _MAX_SAFE_INT)
        _require_number(det, "x_cm")
        _require_number(det, "y_cm")
        _require_number(det, "first_seen_s")
        drone_ids = _require(det, "drone_ids")
        if not (isinstance(drone_ids, list) and len(drone_ids) <= MAX_ROSTER):
            raise ProtocolError(f"'node_state' detection {index} 'drone_ids' must be a list of drone ids")
        for drone_id in drone_ids:
            if not _is_id(drone_id):
                raise ProtocolError(
                    f"'node_state' detection {index} has a bad drone id {_short(drone_id)} (not an identifier)"
                )
    size = _compact_size(msg)
    if size > MAX_NODE_STATE_BYTES:
        raise ProtocolError(
            f"'node_state' is {size} bytes as compact JSON; the limit is {MAX_NODE_STATE_BYTES}"
        )
