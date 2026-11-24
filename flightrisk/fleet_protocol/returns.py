"""Ground-to-edge return commands and the optional return keys of ``assign_lanes`` (protocol v2).

Phase A2 defines these wire shapes (design section 18, requirement RTH-11);
no code sends them yet. Four commands::

    {"type": "return_home", "drone_id": "drone-1", "cmd_id": "<id>"}
    {"type": "return_home", "drone_id": "drone-1", "cmd_id": "<id>",
     "epoch": 3, "leader_id": "ground-a", "wait": true, "max_wait_s": 40}
    {"type": "set_no_entry", "drone_id": "drone-1", "cmd_id": "<id>",
     "epoch": 3, "leader_id": "ground-a",
     "areas": [{"id": "cue-1", "kind": "person_cue",
                "points": [{"x_cm": 0, "y_cm": 0}, {"x_cm": 300, "y_cm": 0},
                           {"x_cm": 300, "y_cm": 300}]}]}
    {"type": "set_speed", "drone_id": "drone-1", "cmd_id": "<id>",
     "epoch": 3, "leader_id": "ground-a", "speed_cm_s": 150}
    {"type": "escort_setpoint", "drone_id": "drone-1", "cmd_id": "<id>",
     "epoch": 3, "leader_id": "ground-a", "x_cm": 1200, "y_cm": -400,
     "alt_cm": 1500, "bearing_deg": 90, "valid_s": 2}

Rules:

* **Protocol v2 only.** Each command, and each ``ack`` or ``nack`` whose
  ``for`` names one, is refused on a v1 link, and ``shape_for_peer`` refuses
  to shape one for a v1 peer. A ground sends them only to an edge whose
  ``describe_unit`` reply lists them in ``accepts``. An edge that does not
  handle them refuses them at decode: the strict ``validate_ground_to_edge``
  keeps refusing them until the edge adopts them (C1).
* **Every command carries ``drone_id`` and ``cmd_id``.** ``set_no_entry``,
  ``set_speed``, ``escort_setpoint`` and a ``return_home`` with ``wait``
  also carry the fence (``epoch`` and ``leader_id``), because only the
  authority may send them. A ``return_home`` without ``wait`` is accepted
  from any authenticated ground, like ``land`` (M-5), so its fence is
  optional and checked as a pair when present.
* **``return_home``.** ``wait: true`` tells the drone to hold at its own
  slot for at most ``max_wait_s`` seconds before it returns, and ``wait:
  false`` releases a waiting drone. ``max_wait_s`` is required with ``wait:
  true`` and refused otherwise.
* **Points are objects** (``{"x_cm", "y_cm"}``), so a long corridor or a
  detailed area never meets the per-list and per-message number caps of the
  privacy screen.
* **No-entry areas** are polygons of at least three points with an ``id``,
  a ``kind`` from ``NO_ENTRY_KINDS`` and, optionally, the ``drone_id`` of
  the drone the area belongs to (a lost or stranded drone, which is exempt
  from its own area). ``set_no_entry`` carries the full current list, which
  may be empty.
* **Replies.** The edge answers each command with an ``ack`` (``result:
  "ok"``) or a ``nack`` with a reason from ``NACK_REASONS_V2`` (``landing``
  while the drone lands), each carrying ``drone_id`` and ``cmd_id``.
* **Landing is unaffected.** Nothing here touches ``land`` or
  ``emergency_land_all``.
* **The ``rth`` telemetry object** (``validate_rth_object``,
  ``make_rth_object``) is the edge's return state in its telemetry: one
  top-level field, so it costs one of ``MAX_FIELDS``, with every number as
  a named key (``home`` is a point object). It carries ``ap_link_loss``, the
  autopilot's link-loss action (MAV-19, B-3). It is a protocol v2 field
  (``V2_FIELDS``), so ``shape_for_peer`` strips it, and the
  ``ap_link_loss`` inside it, on a v1 link. Only ``state`` is required;
  every other key is checked when present, and a key this version does
  not define is ignored, like an unknown top-level telemetry key.

This module imports only ``checks`` and ``constants``; ``validate`` calls
into it.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
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
    _require_text,
    _short,
    is_identifier,
)
from flightrisk.fleet_protocol.constants import (
    ACK,
    AP_LINK_LOSS_ACTIONS,
    ESCORT_SETPOINT,
    MAX_AREA_POINTS,
    MAX_CORRIDOR_POINTS,
    MAX_EPOCH,
    MAX_LANDING_INTERVAL_S,
    MAX_LANE_DISTANCE_CM,
    MAX_NO_ENTRY_AREAS,
    MAX_RETURN_WAIT_S,
    MAX_ROSTER,
    MAX_SETPOINT_VALID_S,
    MAX_SPEED_CM_S,
    MIN_RETURN_HOVER_TIMEOUT_S,
    NACK,
    NACK_REASONS_V2,
    NO_ENTRY_KINDS,
    RETURN_COMMAND_TYPES,
    RETURN_HOME,
    RTH_REASONS,
    RTH_STATES,
    SET_NO_ENTRY,
    SET_SPEED,
)

__all__ = [
    "ASSIGN_LANES_RETURN_KEYS",
    "MAX_AREA_POINTS",
    "MAX_CORRIDOR_POINTS",
    "MAX_LANDING_INTERVAL_S",
    "MAX_NO_ENTRY_AREAS",
    "MAX_RETURN_WAIT_S",
    "MAX_SETPOINT_VALID_S",
    "MAX_SPEED_CM_S",
    "MIN_RETURN_HOVER_TIMEOUT_S",
    "RTH_FIELDS",
    "is_return_reply",
    "make_escort_setpoint",
    "make_return_ack",
    "make_return_home",
    "make_return_nack",
    "make_rth_object",
    "make_set_no_entry",
    "make_set_speed",
    "validate_assign_lanes_return_keys",
    "validate_return_command",
    "validate_return_reply",
    "validate_rth_object",
]

# Optional ``assign_lanes`` keys that exist only in protocol v2.
ASSIGN_LANES_RETURN_KEYS = ("corridor", "landing", "no_entry", "speed_cm_s")
_POINT_FIELDS = frozenset({"x_cm", "y_cm"})
_AREA_FIELDS = frozenset({"id", "kind", "points", "drone_id"})
_LANDING_FIELDS = frozenset({"area_id", "place", "interval_s"})
# The keys of the ``rth`` telemetry object, in the order ``make_rth_object``
# writes them. Only ``state`` is required.
RTH_FIELDS = (
    "state",
    "reason",
    "home",
    "distance_cm",
    "uncertainty_cm",
    "reserve_pct",
    "threshold_pct",
    "countdown_s",
    "cannot_recall",
    "landing_area_id",
    "landing_place",
    "ap_link_loss",
)
_RTH_WHAT = "'telemetry' field 'rth'"


def _only(obj: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    for key in obj:
        if key not in allowed:
            raise ProtocolError(f"{what} has the unknown field {_short(key)}; allowed: {sorted(allowed)}")


def _validate_point(point: Any, what: str) -> None:
    if not isinstance(point, Mapping):
        raise ProtocolError(f"{what} must be an object with 'x_cm' and 'y_cm', got {_short(point)}")
    _only(point, _POINT_FIELDS, what)
    for key in ("x_cm", "y_cm"):
        value = point.get(key)
        if not _is_number(value) or abs(value) > MAX_LANE_DISTANCE_CM:
            raise ProtocolError(
                f"{what} field {key!r} must be a finite number of at most {MAX_LANE_DISTANCE_CM} cm "
                f"from home, got {_short(value)}"
            )


def _validate_points(points: Any, what: str, low: int, high: int) -> None:
    if not isinstance(points, list):
        raise ProtocolError(f"{what} must be a list of point objects, got {_short(points)}")
    if not low <= len(points) <= high:
        raise ProtocolError(f"{what} must have {low} to {high} points, got {len(points)}")
    for index, point in enumerate(points):
        _validate_point(point, f"{what} point {index}")


def _validate_areas(areas: Any, what: str) -> None:
    if not isinstance(areas, list):
        raise ProtocolError(f"{what} must be a list of area objects, got {_short(areas)}")
    if len(areas) > MAX_NO_ENTRY_AREAS:
        raise ProtocolError(f"{what} has {len(areas)} areas; the limit is {MAX_NO_ENTRY_AREAS}")
    seen: set[str] = set()
    for index, area in enumerate(areas):
        name = f"{what} area {index}"
        if not isinstance(area, Mapping):
            raise ProtocolError(f"{name} must be an object, got {_short(area)}")
        _only(area, _AREA_FIELDS, name)
        _require_id(area, "id")
        if area["id"] in seen:
            # A later set_no_entry names an area by its id, so ids must be unique.
            raise ProtocolError(f"{name} repeats the area id {_short(area['id'])}")
        seen.add(area["id"])
        _require_choice(area, "kind", NO_ENTRY_KINDS)
        if "drone_id" in area:
            _require_drone_id(area)
        _validate_points(area.get("points"), f"{name} field 'points'", 3, MAX_AREA_POINTS)


def _validate_speed(msg: Mapping[str, Any], mtype: str) -> None:
    speed = _require_number(msg, "speed_cm_s")
    if not 0 < speed <= MAX_SPEED_CM_S:
        raise ProtocolError(
            f"{mtype!r} field 'speed_cm_s' must be above 0 and at most {MAX_SPEED_CM_S}, got {speed!r}"
        )


def _validate_landing(landing: Any) -> None:
    what = "'assign_lanes' field 'landing'"
    if not isinstance(landing, Mapping):
        raise ProtocolError(f"{what} must be an object, got {_short(landing)}")
    _only(landing, _LANDING_FIELDS, what)
    _require_id(landing, "area_id")
    # The drone's place in its landing area's order, from 0. A fleet has at
    # most MAX_ROSTER drones, so at most that many share one area.
    _require_int_range(landing, "place", 0, MAX_ROSTER - 1)
    interval = _require_number(landing, "interval_s")
    if not 0 < interval <= MAX_LANDING_INTERVAL_S:
        raise ProtocolError(
            f"{what} field 'interval_s' must be above 0 and at most {MAX_LANDING_INTERVAL_S}, got {interval!r}"
        )


def validate_assign_lanes_return_keys(msg: Mapping[str, Any], proto: int) -> None:
    """The optional v2 keys of ``assign_lanes``: ``corridor``, ``landing``, ``no_entry``, ``speed_cm_s``.

    Each is refused on a v1 link, where an edge would ignore it. ``corridor``
    is the way home from the zone, at least two point objects; ``landing``
    names the landing area, the drone's place in its order and the order's
    interval; ``no_entry`` is the full list of known no-entry areas.
    """
    present = [key for key in ASSIGN_LANES_RETURN_KEYS if key in msg]
    if not present:
        return
    if proto < 2:
        raise ProtocolError(f"'assign_lanes' field {present[0]!r} exists only in protocol v2")
    if "corridor" in msg:
        _validate_points(msg["corridor"], "'assign_lanes' field 'corridor'", 2, MAX_CORRIDOR_POINTS)
    if "landing" in msg:
        _validate_landing(msg["landing"])
    if "no_entry" in msg:
        _validate_areas(msg["no_entry"], "'assign_lanes' field 'no_entry'")
    if "speed_cm_s" in msg:
        _validate_speed(msg, "assign_lanes")


def _require_v2(mtype: Any, proto: int) -> None:
    if not (_is_int(proto) and proto >= 2):
        raise ProtocolError(f"{_short(mtype)} exists only in protocol v2, not on a v{_short(proto)} link")


def _validate_fence(msg: Mapping[str, Any], *, required: bool) -> None:
    if required or "epoch" in msg or "leader_id" in msg:
        _require_int_range(msg, "epoch", 1, MAX_EPOCH)
        _require_id(msg, "leader_id")


def validate_return_command(msg: Mapping[str, Any], *, proto: int) -> None:
    """Check a ``return_home``, ``set_no_entry``, ``set_speed`` or ``escort_setpoint``."""
    mtype = msg.get("type")
    if mtype not in RETURN_COMMAND_TYPES:
        raise ProtocolError(f"{_short(mtype)} is not a return command")
    _require_v2(mtype, proto)
    _check_structure(msg)
    _require_drone_id(msg)
    _require_id(msg, "cmd_id")
    if mtype == RETURN_HOME:
        ordered = "wait" in msg
        _validate_fence(msg, required=ordered)
        waits = ordered and _require_bool(msg, "wait")
        if waits:
            wait_s = _require_number(msg, "max_wait_s")
            if not 0 < wait_s <= MAX_RETURN_WAIT_S:
                raise ProtocolError(
                    f"'return_home' field 'max_wait_s' must be above 0 and at most {MAX_RETURN_WAIT_S}, "
                    f"got {wait_s!r}"
                )
        elif "max_wait_s" in msg:
            raise ProtocolError("'return_home' field 'max_wait_s' needs 'wait' to be true")
        return
    _validate_fence(msg, required=True)
    if mtype == SET_NO_ENTRY:
        _validate_areas(_require(msg, "areas"), "'set_no_entry' field 'areas'")
    elif mtype == SET_SPEED:
        _validate_speed(msg, mtype)
    else:
        for key in ("x_cm", "y_cm"):
            value = _require_number(msg, key)
            if abs(value) > MAX_LANE_DISTANCE_CM:
                raise ProtocolError(
                    f"'escort_setpoint' field {key!r} must be at most {MAX_LANE_DISTANCE_CM} cm from home, "
                    f"got {value!r}"
                )
        alt = _require_number(msg, "alt_cm")
        if not 0 < alt <= MAX_LANE_DISTANCE_CM:
            raise ProtocolError(f"'escort_setpoint' field 'alt_cm' must be positive, got {alt!r}")
        bearing = _require_number(msg, "bearing_deg")
        if not 0 <= bearing < 360:
            raise ProtocolError(f"'escort_setpoint' field 'bearing_deg' must be in [0, 360), got {bearing!r}")
        valid = _require_number(msg, "valid_s")
        if not 0 < valid <= MAX_SETPOINT_VALID_S:
            raise ProtocolError(
                f"'escort_setpoint' field 'valid_s' must be above 0 and at most {MAX_SETPOINT_VALID_S}, "
                f"got {valid!r}"
            )


def is_return_reply(msg: Mapping[str, Any]) -> bool:
    """True for an ``ack`` or ``nack`` whose ``for`` is a return command."""
    return msg.get("type") in (ACK, NACK) and _is_member(msg.get("for"), RETURN_COMMAND_TYPES)


def validate_return_reply(msg: Mapping[str, Any], *, proto: int) -> None:
    """An ``ack`` or ``nack`` for a return command (protocol v2 only)."""
    mtype = msg.get("type")
    _require_v2(f"{mtype} for {msg.get('for')}", proto)
    _require_drone_id(msg)
    _require_id(msg, "cmd_id")
    if "epoch_seen" in msg:
        _require_int_range(msg, "epoch_seen", 0, MAX_EPOCH)
    if "detail" in msg:
        _require_text(msg, "detail")
    if mtype == ACK:
        if _require(msg, "result") != "ok":
            raise ProtocolError(f"'ack' field 'result' must be 'ok', got {_short(msg['result'])}")
        return
    reason = _require(msg, "reason")
    if not _is_member(reason, NACK_REASONS_V2):
        raise ProtocolError(f"'nack' field 'reason' must be one of {sorted(NACK_REASONS_V2)}, got {_short(reason)}")


def _command(
    mtype: str, drone_id: str, cmd_id: str | None, fence: tuple[int, str] | None, fields: Mapping[str, Any]
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": mtype,
        "drone_id": drone_id,
        "cmd_id": cmd_id if cmd_id is not None else uuid.uuid4().hex,
    }
    if fence is not None:
        # A ``(epoch, leader_id)`` pair, or a ``Fence``.
        epoch, leader_id = (fence.epoch, fence.leader_id) if hasattr(fence, "epoch") else fence
        msg["epoch"], msg["leader_id"] = epoch, leader_id
    msg.update(fields)
    validate_return_command(msg, proto=2)
    return msg


def make_return_home(
    drone_id: str,
    *,
    cmd_id: str | None = None,
    fence: tuple[int, str] | None = None,
    wait: bool | None = None,
    max_wait_s: float | None = None,
) -> dict[str, Any]:
    """Ground -> edge: return home now, or (with *wait*) hold for an order or be released.

    *fence* is the ``(epoch, leader_id)`` of the sending ground; it is
    required with *wait*. *max_wait_s* goes with ``wait=True`` only.
    """
    fields: dict[str, Any] = {}
    if wait is not None:
        fields["wait"] = wait
    if max_wait_s is not None:
        fields["max_wait_s"] = max_wait_s
    return _command(RETURN_HOME, drone_id, cmd_id, fence, fields)


def make_set_no_entry(
    drone_id: str,
    areas: Iterable[Mapping[str, Any]],
    *,
    fence: tuple[int, str],
    cmd_id: str | None = None,
) -> dict[str, Any]:
    """Ground -> edge: the full current list of no-entry areas."""
    copied = [{**dict(area), "points": [dict(p) for p in area.get("points", [])]} for area in areas]
    return _command(SET_NO_ENTRY, drone_id, cmd_id, fence, {"areas": copied})


def make_set_speed(
    drone_id: str, speed_cm_s: float, *, fence: tuple[int, str], cmd_id: str | None = None
) -> dict[str, Any]:
    """Ground -> edge: change the search speed mid-lane."""
    return _command(SET_SPEED, drone_id, cmd_id, fence, {"speed_cm_s": speed_cm_s})


def make_escort_setpoint(
    drone_id: str,
    x_cm: float,
    y_cm: float,
    alt_cm: float,
    bearing_deg: float,
    valid_s: float,
    *,
    fence: tuple[int, str],
    cmd_id: str | None = None,
) -> dict[str, Any]:
    """Ground -> edge: the escort position, altitude and bearing, valid for *valid_s* seconds."""
    fields = {"x_cm": x_cm, "y_cm": y_cm, "alt_cm": alt_cm, "bearing_deg": bearing_deg, "valid_s": valid_s}
    return _command(ESCORT_SETPOINT, drone_id, cmd_id, fence, fields)


def make_return_ack(
    command: Mapping[str, Any], *, epoch_seen: int | None = None, detail: str | None = None
) -> dict[str, Any]:
    """Edge -> ground: accept a return command."""
    msg: dict[str, Any] = {
        "type": ACK,
        "for": command["type"],
        "drone_id": command["drone_id"],
        "cmd_id": command["cmd_id"],
        "result": "ok",
    }
    if epoch_seen is not None:
        msg["epoch_seen"] = epoch_seen
    if detail is not None:
        msg["detail"] = detail
    validate_return_reply(msg, proto=2)
    return msg


def make_return_nack(
    command: Mapping[str, Any],
    reason: str,
    *,
    epoch_seen: int | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Edge -> ground: refuse a return command with a reason from ``NACK_REASONS_V2``."""
    msg: dict[str, Any] = {
        "type": NACK,
        "for": command["type"],
        "drone_id": command["drone_id"],
        "cmd_id": command["cmd_id"],
        "reason": reason,
    }
    if epoch_seen is not None:
        msg["epoch_seen"] = epoch_seen
    if detail is not None:
        msg["detail"] = detail
    validate_return_reply(msg, proto=2)
    return msg


def _rth_optional_number(rth: Mapping[str, Any], key: str, high: float | None = None) -> None:
    value = rth.get(key)
    if value is None:
        return
    if not _is_number(value) or value < 0 or (high is not None and value > high):
        bound = f" and at most {high}" if high is not None else ""
        raise ProtocolError(
            f"{_RTH_WHAT} key {key!r} must be a number of 0 or more{bound}, or null, got {_short(value)}"
        )


def validate_rth_object(rth: Any) -> None:
    """Check the ``rth`` telemetry object; raise ``ProtocolError`` if it is malformed.

    ``state`` (from ``RTH_STATES``) is required. When present: ``reason``
    from ``RTH_REASONS`` or null; ``home`` a point object or null;
    ``distance_cm``, ``uncertainty_cm`` and ``countdown_s`` at least 0 or
    null; ``reserve_pct`` and ``threshold_pct`` from 0 to 100 or null;
    ``cannot_recall`` a boolean; ``landing_area_id`` an identifier or null;
    ``landing_place`` the drone's place in its landing order, 0 to
    ``MAX_ROSTER - 1``, or null; ``ap_link_loss`` from
    ``AP_LINK_LOSS_ACTIONS``. Keys this version does not define are ignored.
    """
    if not isinstance(rth, Mapping):
        raise ProtocolError(f"{_RTH_WHAT} must be an object, got {_short(rth)}")
    state = rth.get("state")
    if not _is_member(state, RTH_STATES):
        raise ProtocolError(f"{_RTH_WHAT} key 'state' must be one of {sorted(RTH_STATES)}, got {_short(state)}")
    reason = rth.get("reason")
    if reason is not None and not _is_member(reason, RTH_REASONS):
        raise ProtocolError(
            f"{_RTH_WHAT} key 'reason' must be one of {sorted(RTH_REASONS)} or null, got {_short(reason)}"
        )
    if rth.get("home") is not None:
        _validate_point(rth["home"], f"{_RTH_WHAT} key 'home'")
    for key in ("distance_cm", "uncertainty_cm", "countdown_s"):
        _rth_optional_number(rth, key)
    for key in ("reserve_pct", "threshold_pct"):
        _rth_optional_number(rth, key, 100)
    if "cannot_recall" in rth and not isinstance(rth["cannot_recall"], bool):
        raise ProtocolError(f"{_RTH_WHAT} key 'cannot_recall' must be true or false, got {_short(rth['cannot_recall'])}")
    area = rth.get("landing_area_id")
    if area is not None and not is_identifier(area):
        raise ProtocolError(f"{_RTH_WHAT} key 'landing_area_id' must be an identifier or null, got {_short(area)}")
    place = rth.get("landing_place")
    if place is not None and not (_is_int(place) and 0 <= place <= MAX_ROSTER - 1):
        raise ProtocolError(
            f"{_RTH_WHAT} key 'landing_place' must be a whole number from 0 to {MAX_ROSTER - 1} or null, "
            f"got {_short(place)}"
        )
    if "ap_link_loss" in rth and not _is_member(rth["ap_link_loss"], AP_LINK_LOSS_ACTIONS):
        raise ProtocolError(
            f"{_RTH_WHAT} key 'ap_link_loss' must be one of {sorted(AP_LINK_LOSS_ACTIONS)}, "
            f"got {_short(rth['ap_link_loss'])}"
        )


def make_rth_object(
    state: str,
    *,
    reason: str | None = None,
    home: tuple[float, float] | Mapping[str, float] | None = None,
    distance_cm: float | None = None,
    uncertainty_cm: float | None = None,
    reserve_pct: float | None = None,
    threshold_pct: float | None = None,
    countdown_s: float | None = None,
    cannot_recall: bool | None = None,
    landing_area_id: str | None = None,
    landing_place: int | None = None,
    ap_link_loss: str | None = None,
) -> dict[str, Any]:
    """The ``rth`` telemetry object, for ``make_telemetry(..., rth=...)``.

    A key left as ``None`` is omitted. *home* is an ``(x_cm, y_cm)`` pair or
    a point object. Nothing in A2 sends it (CMP-1); C1 is the first sender.
    """
    if home is not None and not isinstance(home, Mapping):
        x_cm, y_cm = home
        home = {"x_cm": x_cm, "y_cm": y_cm}
    values = {
        "state": state,
        "reason": reason,
        "home": dict(home) if home is not None else None,
        "distance_cm": distance_cm,
        "uncertainty_cm": uncertainty_cm,
        "reserve_pct": reserve_pct,
        "threshold_pct": threshold_pct,
        "countdown_s": countdown_s,
        "cannot_recall": cannot_recall,
        "landing_area_id": landing_area_id,
        "landing_place": landing_place,
        "ap_link_loss": ap_link_loss,
    }
    rth = {key: values[key] for key in RTH_FIELDS if values[key] is not None}
    validate_rth_object(rth)
    return rth
