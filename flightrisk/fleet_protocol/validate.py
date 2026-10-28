"""Per-type schema checks, ``validate_message`` and the strict directional validators."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flightrisk.fleet_protocol.checks import (
    ProtocolError,
    _check_structure,
    _is_int,
    _is_member,
    _is_number,
    _require,
    _require_bool,
    _require_choice,
    _require_drone_id,
    _require_id,
    _require_int_range,
    _require_number,
    _require_object,
    _require_only,
    _require_optional_int,
    _require_optional_number,
    _require_text,
    _short,
    _validate_proto_field,
    is_profile_name,
)
from flightrisk.fleet_protocol.constants import (
    _KNOWN_TYPES,
    _MAX_SAFE_INT,
    _PEER_MESSAGE_FIELDS,
    _ZONE_FIELDS,
    ACK,
    ASSIGN_LANES,
    CLOCK,
    COMMAND_TYPES,
    CONFIG_COMMAND_TYPES,
    DIRECTION_EDGE_TO_GROUND,
    DIRECTION_GROUND_TO_EDGE,
    DIRECTION_PEER,
    EDGE_ROLE,
    EDGE_STATES,
    EDGE_TO_GROUND_TYPES_V2,
    FAILSAFE_TYPES,
    FENCED_COMMAND_TYPES,
    FENCED_CONTROL_TYPES,
    GROUND_ROLES,
    GROUND_TO_EDGE_TYPES,
    GROUND_TO_EDGE_TYPES_V2,
    HANDOVER,
    HANDOVER_ACCEPT,
    HANDOVER_OFFER,
    HANDOVER_REJECT,
    HEARTBEAT,
    HELLO,
    HOLD,
    HOLD_REASONS,
    LANE_DIRECTIONS,
    LEGACY_EDGE_TO_GROUND_TYPES,
    LEGACY_GROUND_TO_EDGE_TYPES,
    MAX_EPOCH,
    MAX_HOVER_TIMEOUT_S,
    MAX_ID_LEN,
    MAX_LANE_DISTANCE_CM,
    MAX_LANES,
    MAX_TEXT_LEN,
    MIN_PROTOCOL_VERSION,
    NACK,
    NACK_GEOFENCE_FIELDS,
    NACK_REASONS,
    NACK_REASONS_V2,
    NODE_STATE,
    NUDGE,
    NUDGE_DIRECTIONS,
    NUDGE_MAX_CM,
    NUDGE_MIN_CM,
    ON_COMPLETE_ACTIONS,
    PEER_HEARTBEAT,
    PEER_TYPES,
    PING,
    PONG,
    PROFILE_FALLBACK_REASONS,
    PROFILE_NACK_REASONS,
    PROTOCOL_VERSION,
    RESUME,
    SET_DETECTION_PROFILE,
    START_AT,
    TAKEOFF,
    TELEMETRY,
    V2_ONLY_COMMAND_TYPES,
)
from flightrisk.fleet_protocol.peer import (
    _validate_node_state,
)


def _validate_lanes(msg: Mapping[str, Any]) -> None:
    lanes = _require(msg, "lanes")
    if not isinstance(lanes, list):
        raise ProtocolError(f"'assign_lanes' field 'lanes' must be a list, got {_short(lanes)}")
    if len(lanes) > MAX_LANES:
        raise ProtocolError(f"'assign_lanes' has {len(lanes)} lanes; the limit is {MAX_LANES}")
    for index, lane in enumerate(lanes):
        if not isinstance(lane, Mapping):
            raise ProtocolError(f"'assign_lanes' lane {index} must be an object, got {_short(lane)}")
        direction = lane.get("direction")
        if not _is_member(direction, LANE_DIRECTIONS):
            raise ProtocolError(
                f"'assign_lanes' lane {index} direction must be one of "
                f"{sorted(LANE_DIRECTIONS)}, got {_short(direction)}"
            )
        distance = lane.get("distance_cm")
        if not _is_number(distance) or not 0 < distance <= MAX_LANE_DISTANCE_CM:
            raise ProtocolError(
                f"'assign_lanes' lane {index} distance_cm must be a positive number of at most "
                f"{MAX_LANE_DISTANCE_CM}, got {_short(distance)}"
            )


def _validate_zone(msg: Mapping[str, Any]) -> None:
    zone = _require(msg, "zone")
    if not isinstance(zone, Mapping):
        raise ProtocolError(f"'assign_lanes' field 'zone' must be an object, got {_short(zone)}")
    for key in _ZONE_FIELDS:
        value = zone.get(key)
        if not _is_number(value):
            raise ProtocolError(f"'assign_lanes' zone field {key!r} must be a finite number, got {_short(value)}")
    if zone["w_cm"] < 0 or zone["h_cm"] < 0:
        raise ProtocolError("'assign_lanes' zone width and height must not be negative")


def _validate_fence(msg: Mapping[str, Any], mtype: str, proto: int) -> None:
    """Fencing fields: required in v2 (and on v2-only commands), checked whenever present.

    ``epoch`` and ``leader_id`` travel as a pair: a message with one and not
    the other is malformed under every protocol version. A fenced epoch is at
    least 1, because a leader's first epoch is ``stored + 1`` with nothing
    stored meaning 0.
    """
    required = proto >= 2 or mtype in V2_ONLY_COMMAND_TYPES
    if required or "epoch" in msg or "leader_id" in msg:
        _require_int_range(msg, "epoch", 1, MAX_EPOCH)
        _require_id(msg, "leader_id")
    if mtype in FENCED_COMMAND_TYPES and (required or "cmd_id" in msg):
        _require_id(msg, "cmd_id")


def _validate_plan_id(msg: Mapping[str, Any], proto: int) -> None:
    if proto >= 2 or "plan_id" in msg:
        _require_id(msg, "plan_id")


def _validate_assign_lanes(msg: Mapping[str, Any], proto: int) -> None:
    _require_drone_id(msg)
    alt = _require_number(msg, "alt_cm")
    if alt <= 0:
        raise ProtocolError(f"'assign_lanes' field 'alt_cm' must be positive, got {alt!r}")
    _validate_lanes(msg)
    _validate_zone(msg)
    _validate_plan_id(msg, proto)
    if proto >= 2 or "plan_seq" in msg:
        # Orders the plans one leader issues within its epoch, so an edge can
        # refuse an older plan that arrives late (nack ``stale_plan``).
        _require_int_range(msg, "plan_seq", 1, _MAX_SAFE_INT)
    on_complete = msg.get("on_complete")
    if "on_complete" in msg:
        _require_choice(msg, "on_complete", ON_COMPLETE_ACTIONS)
    if "hover_timeout_s" in msg:
        if on_complete != "hover":
            raise ProtocolError("'assign_lanes' field 'hover_timeout_s' needs 'on_complete' to be 'hover'")
        timeout = _require_number(msg, "hover_timeout_s")
        if not 0 < timeout <= MAX_HOVER_TIMEOUT_S:
            raise ProtocolError(
                f"'assign_lanes' field 'hover_timeout_s' must be above 0 and at most "
                f"{MAX_HOVER_TIMEOUT_S}, got {timeout!r}"
            )


def _validate_telemetry(msg: Mapping[str, Any], proto: int) -> None:
    _require_drone_id(msg)
    _require_bool(msg, "is_flying")
    for key in ("battery", "height_cm", "rel_x_cm", "rel_y_cm", "heading_deg",
                "link_age_s", "takeoff_at_local_s"):
        _require_optional_number(msg, key)
    for key in ("lane_index", "lanes_total"):
        _require_optional_int(msg, key)
    if proto >= 2 or "epoch" in msg:
        _require_int_range(msg, "epoch", 0, MAX_EPOCH)
    if proto >= 2 or "plan_id" in msg:
        _require_id(msg, "plan_id", nullable=True)
    if proto >= 2 or "state" in msg:
        _require_choice(msg, "state", EDGE_STATES)
    if proto >= 2 or "plan_valid" in msg:
        _require_bool(msg, "plan_valid")


def _validate_reply_common(msg: Mapping[str, Any], target: str, proto: int) -> None:
    """Fields shared by v2 ``ack`` and ``nack``."""
    _require_drone_id(msg)
    if (proto >= 2 and target not in FAILSAFE_TYPES) or "cmd_id" in msg:
        _require_id(msg, "cmd_id")
    if "epoch_seen" in msg:
        _require_int_range(msg, "epoch_seen", 0, MAX_EPOCH)
    if "plan_id" in msg:
        _require_id(msg, "plan_id", nullable=True)
    if "detail" in msg:
        _require_text(msg, "detail")


def _require_profile_name(msg: Mapping[str, Any], key: str, *, nullable: bool = False) -> str | None:
    value = _require(msg, key)
    if value is None and nullable:
        return None
    if not is_profile_name(value):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be a profile name (lowercase letters, digits and "
            f"underscores, starting with a letter, at most 32 characters){' or null' if nullable else ''}, "
            f"got {_short(value)}"
        )
    return value


def _validate_set_detection_profile(msg: Mapping[str, Any]) -> None:
    """A configuration command: ``profile`` and ``cmd_id`` on every link version, no fence.

    ``drone_id`` is optional (the ground addresses the edge's connection);
    a fence, when a ground adds one, is checked as a pair like on any other
    message. The profile only has to be a well-formed name: whether the edge
    knows it is the edge's answer (``nack{reason: unknown_profile}``).
    """
    _require_profile_name(msg, "profile")
    _require_id(msg, "cmd_id")
    if "drone_id" in msg:
        _require_drone_id(msg)
    if "epoch" in msg or "leader_id" in msg:
        _require_int_range(msg, "epoch", 1, MAX_EPOCH)
        _require_id(msg, "leader_id")


def _validate_profile_reply(msg: Mapping[str, Any], *, ack: bool) -> None:
    """An ``ack`` or ``nack`` for a configuration command, under any protocol version.

    Both carry ``drone_id`` and ``cmd_id`` and may carry ``requested`` (the
    profile asked for), ``epoch_seen`` and ``detail``. An ack carries the
    ``profile`` now active and ``fallback`` (true when it is not the one
    requested, with ``fallback_reason``). A nack carries a reason from
    ``PROFILE_NACK_REASONS`` and may carry the ``profile`` still active.
    """
    _require_drone_id(msg)
    _require_id(msg, "cmd_id")
    if "epoch_seen" in msg:
        _require_int_range(msg, "epoch_seen", 0, MAX_EPOCH)
    if "detail" in msg:
        _require_text(msg, "detail")
    if "requested" in msg:
        _require_profile_name(msg, "requested")
    if ack:
        if "result" in msg and msg["result"] != "ok":
            raise ProtocolError(f"'ack' field 'result' must be 'ok', got {_short(msg['result'])}")
        _require_profile_name(msg, "profile")
        if _require_bool(msg, "fallback"):
            _require_choice(msg, "fallback_reason", PROFILE_FALLBACK_REASONS)
        elif "fallback_reason" in msg:
            raise ProtocolError("'ack' field 'fallback_reason' needs 'fallback' to be true")
        return
    reason = _require(msg, "reason")
    if not _is_member(reason, PROFILE_NACK_REASONS):
        raise ProtocolError(
            f"'nack' field 'reason' must be one of {sorted(PROFILE_NACK_REASONS)}, got {_short(reason)}"
        )
    if "profile" in msg:
        _require_profile_name(msg, "profile", nullable=True)


def _validate_ack(msg: Mapping[str, Any], proto: int) -> None:
    target = _require(msg, "for")
    if _is_member(target, CONFIG_COMMAND_TYPES):
        _validate_profile_reply(msg, ack=True)
        return
    if not isinstance(target, str) or not target or len(target) > MAX_ID_LEN:
        raise ProtocolError(f"'ack' field 'for' must be a non-empty string, got {_short(target)}")
    if proto >= 2 and target not in COMMAND_TYPES:
        raise ProtocolError(f"'ack' field 'for' must be one of {sorted(COMMAND_TYPES)}, got {_short(target)}")
    _validate_reply_common(msg, target, proto)
    if proto >= 2 or "result" in msg:
        result = _require(msg, "result")
        if result != "ok":
            raise ProtocolError(f"'ack' field 'result' must be 'ok', got {_short(result)}")
    if target == START_AT:
        _require_number(msg, "executed_at_ground_s")
    elif target == ASSIGN_LANES:
        _require_number(msg, "battery")
        lanes_total = _require(msg, "lanes_total")
        if not (_is_int(lanes_total) and 0 <= lanes_total <= _MAX_SAFE_INT):
            raise ProtocolError(
                f"'ack' for 'assign_lanes' field 'lanes_total' must be a non-negative integer, "
                f"got {_short(lanes_total)}"
            )


def _validate_nack(msg: Mapping[str, Any], proto: int) -> None:
    target = _require(msg, "for")
    if _is_member(target, CONFIG_COMMAND_TYPES):
        # Configuration commands are refused the same way on every link
        # version (they are newer than v1's lane-only nack).
        _validate_profile_reply(msg, ack=False)
        return
    if proto < 2:
        # v1: only lane assignments are refused, for the v1 reasons.
        if target != ASSIGN_LANES:
            raise ProtocolError(f"'nack' field 'for' must be {ASSIGN_LANES!r}, got {_short(target)}")
        reasons = NACK_REASONS
    else:
        # A landing is never refused, so it is never nacked.
        if not _is_member(target, FENCED_COMMAND_TYPES):
            raise ProtocolError(
                f"'nack' field 'for' must be one of {sorted(FENCED_COMMAND_TYPES)}, got {_short(target)}"
            )
        reasons = NACK_REASONS_V2
    _validate_reply_common(msg, target, proto)
    reason = _require(msg, "reason")
    if not _is_member(reason, reasons):
        raise ProtocolError(f"'nack' field 'reason' must be one of {sorted(reasons)}, got {_short(reason)}")
    if target == ASSIGN_LANES:
        lanes_total = _require(msg, "lanes_total")
        if not (_is_int(lanes_total) and 0 <= lanes_total <= _MAX_SAFE_INT):
            raise ProtocolError(
                f"'nack' field 'lanes_total' must be a non-negative integer, got {_short(lanes_total)}"
            )
        for key in NACK_GEOFENCE_FIELDS:
            if key in msg:
                _require_number(msg, key)


def _is_ground_hello(msg: Mapping[str, Any]) -> bool:
    return _is_member(msg.get("role"), GROUND_ROLES)


def _validate_hello(msg: Mapping[str, Any]) -> None:
    _validate_proto_field(msg)
    if _is_ground_hello(msg):
        if "drone_id" in msg:
            raise ProtocolError("a ground 'hello' must not carry 'drone_id'")
        node_id = _require_id(msg, "node_id")
        low = 1 if msg["role"] == "leader" else 0
        _require_int_range(msg, "epoch", low, MAX_EPOCH)
        if "leader_id" in msg:
            leader_id = _require_id(msg, "leader_id")
            if msg["role"] == "leader" and leader_id != node_id:
                raise ProtocolError("a leader 'hello' must name itself as 'leader_id'")
        return
    role = msg.get("role", EDGE_ROLE)
    if role != EDGE_ROLE:
        raise ProtocolError(
            f"'hello' field 'role' must be one of {sorted(GROUND_ROLES | {EDGE_ROLE})}, got {_short(role)}"
        )
    _require_drone_id(msg)
    _require_id(msg, "node_id")
    _require_int_range(msg, "epoch_seen", 0, MAX_EPOCH)
    _require_id(msg, "plan_id", nullable=True)
    _require_optional_int(msg, "lane_index")
    _require_bool(msg, "is_flying")
    battery = _require(msg, "battery")
    if battery is not None and not (_is_number(battery) and 0 <= battery <= 100):
        raise ProtocolError(f"'hello' field 'battery' must be a percentage or null, got {_short(battery)}")


def validate_message(msg: Any, *, proto: int = 1) -> str:
    """Check a fleet control message against the protocol schema.

    Returns the message type when the message is a valid fleet control
    message. Raises ``ProtocolError`` when the message is not an object, has
    no string ``type``, or is a known fleet type whose fields do not match the
    schema. Message types this protocol does not define (for example the
    legacy ``detections``, ``set_target`` and ``stream_video`` messages) are
    returned unchecked, so callers decide for themselves how to treat them;
    use ``validate_ground_to_edge``/``validate_edge_to_ground`` to reject them.

    *proto* is the protocol version agreed for the link (1 when no ``hello``
    was exchanged). With ``proto >= 2``, fenced types must carry ``epoch``
    and ``leader_id`` (and ``cmd_id`` for commands), lane plans and starts
    must carry ``plan_id``, telemetry must carry the v2 state fields, and
    acks and nacks follow the generic v2 rules. With ``proto == 1``, v2
    fields are optional but checked when present. ``land`` and
    ``emergency_land_all`` are always accepted, whatever else they carry and
    whatever *proto* is (even an invalid one, such as ``None`` before a
    ``hello`` has been exchanged).

    Never raises anything but ``ProtocolError`` for any *msg*.
    """
    mtype = _message_type(msg)
    if mtype in FAILSAFE_TYPES:
        # Fail-safe: a landing is accepted whatever its optional fields say,
        # and before *proto* is looked at, so a bad link version can never
        # refuse one.
        return mtype
    if not (_is_int(proto) and proto >= MIN_PROTOCOL_VERSION):
        raise ProtocolError(f"protocol version must be an integer >= {MIN_PROTOCOL_VERSION}, got {_short(proto)}")
    if mtype not in _KNOWN_TYPES:
        return mtype

    if mtype in PEER_TYPES:
        _check_structure(msg, max_text=MAX_TEXT_LEN)
        if mtype in _PEER_MESSAGE_FIELDS:
            _require_only(msg, _PEER_MESSAGE_FIELDS[mtype], repr(mtype))
    else:
        _check_structure(msg)
    if mtype in FENCED_COMMAND_TYPES or mtype in FENCED_CONTROL_TYPES:
        _validate_fence(msg, mtype, proto)

    if mtype == PING:
        _require_number(msg, "t1")
    elif mtype == PONG:
        for key in ("t1", "t2", "t3"):
            _require_number(msg, key)
    elif mtype == CLOCK:
        _require_number(msg, "offset_s")
    elif mtype == ASSIGN_LANES:
        _validate_assign_lanes(msg, proto)
    elif mtype == START_AT:
        _require_number(msg, "t_ground_s")
        _validate_plan_id(msg, proto)
    elif mtype == HEARTBEAT:
        _require_int_range(msg, "seq", 0, _MAX_SAFE_INT)
    elif mtype == TAKEOFF:
        pass
    elif mtype == SET_DETECTION_PROFILE:
        _validate_set_detection_profile(msg)
    elif mtype == HOLD:
        if "reason" in msg:
            _require_choice(msg, "reason", HOLD_REASONS)
    elif mtype == RESUME:
        # A v2-only command, so required on every link version: the edge
        # resumes only the plan the ground means, and answers a mismatch with
        # nack ``plan_mismatch``.
        _require_id(msg, "plan_id")
    elif mtype == NUDGE:
        _require_choice(msg, "direction", NUDGE_DIRECTIONS)
        _require_int_range(msg, "distance_cm", NUDGE_MIN_CM, NUDGE_MAX_CM)
    elif mtype == HANDOVER:
        _require_id(msg, "to_node")
        epoch_next = _require_int_range(msg, "epoch_next", 2, MAX_EPOCH)
        if epoch_next != msg["epoch"] + 1:
            # Same rule as handover_offer: leadership passes to exactly the
            # next epoch, so no epochs are skipped or burned.
            raise ProtocolError("'handover' field 'epoch_next' must be 'epoch' + 1")
        if msg["to_node"] == msg["leader_id"]:
            raise ProtocolError("'handover' must name another node as 'to_node'")
    elif mtype == HELLO:
        _validate_hello(msg)
    elif mtype == TELEMETRY:
        _validate_telemetry(msg, proto)
    elif mtype == ACK:
        _validate_ack(msg, proto)
    elif mtype == NACK:
        _validate_nack(msg, proto)
    elif mtype == NODE_STATE:
        _validate_node_state(msg)
    elif mtype == HANDOVER_OFFER:
        epoch_next = _require_int_range(msg, "epoch_next", 2, MAX_EPOCH)
        snapshot = _require_object(msg, "snapshot")
        if snapshot.get("type", NODE_STATE) != NODE_STATE:
            raise ProtocolError("'handover_offer' field 'snapshot' must be a 'node_state'")
        _validate_node_state(snapshot)
        if snapshot["role"] != "leader" or snapshot["epoch"] + 1 != epoch_next:
            raise ProtocolError("'handover_offer' must come from the leader and offer the next epoch")
    elif mtype == HANDOVER_ACCEPT:
        _require_int_range(msg, "epoch_next", 2, MAX_EPOCH)
    elif mtype == HANDOVER_REJECT:
        _require_text(msg, "reason")
        if "epoch_next" in msg:
            _require_int_range(msg, "epoch_next", 2, MAX_EPOCH)
    elif mtype == PEER_HEARTBEAT:
        _require_int_range(msg, "seq", 0, _MAX_SAFE_INT)
        _require_int_range(msg, "epoch", 0, MAX_EPOCH)
        if "node_id" in msg:
            _require_id(msg, "node_id")
    return mtype


def _message_type(msg: Any) -> str:
    if not isinstance(msg, Mapping):
        raise ProtocolError(f"control message must be an object, got {type(msg).__name__}")
    mtype = msg.get("type")
    if not isinstance(mtype, str) or not mtype:
        raise ProtocolError(f"control message has no string 'type': {_short(mtype)}")
    return mtype


def validate_ground_to_edge(msg: Any, *, proto: int, require_fence: bool = False) -> str:
    """Strict check for a message an edge received from a ground.

    Like ``validate_message`` with *proto*, but also rejects any type that
    is not a ground-to-edge type of that version: unknown commands, edge or
    peer messages, an edge-form ``hello``, and v2-only commands on a v1 link.
    The legacy ``set_target`` and ``stream_video`` messages pass by type only.
    Configuration commands (``set_detection_profile``) are accepted on every
    link version and checked against their own schema; they are never
    fenced, because they cannot move the drone.

    With *require_fence*, motion and control (``FENCED_COMMAND_TYPES`` and
    ``FENCED_CONTROL_TYPES``) must carry a valid fence, and commands a
    ``cmd_id``, whatever the link version. An edge in a v2-only or
    multi-ground deployment sets it, so a ground that never sends ``hello``,
    starts with a bare ``heartbeat`` or announces ``proto: 1`` cannot move
    the drone or feed its watchdog with unfenced v1 messages. They are then
    also checked under the v2 rules, so a fenced ``assign_lanes`` must carry
    ``plan_id`` and ``plan_seq`` and a fenced ``start_at`` must carry
    ``plan_id``, whatever the link version.

    Landing is never fenced and never refused: ``land`` and
    ``emergency_land_all`` are accepted before *proto* or *require_fence*
    is looked at, so even an invalid *proto* (such as ``None`` before a
    ``hello``) cannot refuse one.
    """
    mtype = _message_type(msg)
    if mtype in FAILSAFE_TYPES:
        return mtype
    if mtype in LEGACY_GROUND_TO_EDGE_TYPES:
        return mtype
    if mtype in CONFIG_COMMAND_TYPES:
        # Allowed on every link version: it never moves the drone, and a v1
        # single-ground edge takes it too.
        return validate_message(msg, proto=proto)
    allowed = GROUND_TO_EDGE_TYPES_V2 if _is_int(proto) and proto >= 2 else GROUND_TO_EDGE_TYPES | {HELLO}
    if mtype not in allowed:
        raise ProtocolError(f"{_short(mtype)} is not a ground-to-edge message in protocol v{_short(proto)}")
    validate_message(msg, proto=proto)
    if mtype == HELLO and not _is_ground_hello(msg):
        raise ProtocolError("a ground-to-edge 'hello' must have role 'leader' or 'standby'")
    if require_fence and (mtype in FENCED_COMMAND_TYPES or mtype in FENCED_CONTROL_TYPES):
        # The full v2 rules, not only the fence: a fenced assign_lanes
        # without plan_id or plan_seq, or a fenced start_at without plan_id,
        # is refused too.
        try:
            validate_message(msg, proto=PROTOCOL_VERSION)
        except ProtocolError as exc:
            raise ProtocolError(
                f"unfenced or incomplete {mtype!r} refused: this edge requires the v2 form "
                f"on every link ({exc})"
            ) from exc
    return mtype


def validate_edge_to_ground(msg: Any, *, proto: int) -> str:
    """Strict check for a message a ground received from an edge.

    Rejects unknown types and ground-only types. The legacy ``detections``
    message passes by type only.
    """
    mtype = _message_type(msg)
    if mtype in LEGACY_EDGE_TO_GROUND_TYPES:
        return mtype
    if mtype not in EDGE_TO_GROUND_TYPES_V2:
        raise ProtocolError(f"{_short(mtype)} is not an edge-to-ground message")
    validate_message(msg, proto=proto)
    if mtype == HELLO and _is_ground_hello(msg):
        raise ProtocolError("an edge-to-ground 'hello' must have role 'edge'")
    return mtype


def validate_peer_message(msg: Any) -> str:
    """Strict check for a ground-to-ground message on the peer link (v2 only)."""
    mtype = _message_type(msg)
    if mtype not in PEER_TYPES:
        raise ProtocolError(f"{_short(mtype)} is not a ground-to-ground message")
    return validate_message(msg, proto=PROTOCOL_VERSION)


def validate_for_direction(msg: Any, direction: str, *, proto: int, require_fence: bool = False) -> str:
    """Dispatch to the strict validator for *direction* (``DIRECTION_*``).

    *require_fence* applies to the ground-to-edge direction only (see
    ``validate_ground_to_edge``).
    """
    if direction == DIRECTION_GROUND_TO_EDGE:
        return validate_ground_to_edge(msg, proto=proto, require_fence=require_fence)
    if direction == DIRECTION_EDGE_TO_GROUND:
        return validate_edge_to_ground(msg, proto=proto)
    if direction == DIRECTION_PEER:
        return validate_peer_message(msg)
    raise ProtocolError(f"unknown message direction {_short(direction)}")


def _checked(msg: dict[str, Any], proto: int = 1) -> dict[str, Any]:
    validate_message(msg, proto=proto)
    return msg
