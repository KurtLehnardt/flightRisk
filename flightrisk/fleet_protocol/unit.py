"""The ``describe_unit`` query, its reply and the gates that use them (protocol v2).

Phase A2 defines these wire shapes (design section 18, detail sections 12.2
and 12.3, requirements PROTO-1 to PROTO-3); no code sends them yet. The
query::

    {"type": "describe_unit", "drone_id": "drone-1", "cmd_id": "<id>"}

The reply is an ``ack`` with ``for: "describe_unit"``::

    {"type": "ack", "for": "describe_unit", "drone_id": "drone-1",
     "cmd_id": "<id>", "result": "ok", "unit_rev": "0123456789ab",
     "unit": {"model": "tello", "model_name": "...", "adapter": "...",
              "scale": "room", "positioning": "vps_tof", "link": "wifi_ap",
              "cameras": {"front": {"mount": "nose", "mount_kind": "fixed",
                                    "orient": "forward", "pitch_min_deg": 0,
                                    "pitch_max_deg": 0, "role": null,
                                    "w_px": 960, "h_px": 720, "hfov_deg": 82.6,
                                    "sensor": "rgb", "radiometric": false}}},
     "gates": {"<feature id>": {"on": true, "ok": true, "why": null, "src": "profile"}},
     "values": {"<param id>": {"v": 120, "src": "profile", "d": 120,
                               "lo": 50, "hi": 300, "edit": "ui"}},
     "ap_link_loss": "return", "accepts": ["return_home"]}

Rules:

* **A read-only query, not a command.** ``describe_unit`` is in
  ``QUERY_TYPES``, not in ``CONFIG_COMMAND_TYPES`` or ``COMMAND_TYPES``. It
  is protocol v2 only, carries ``drone_id`` and ``cmd_id``, and needs no
  fence, because any ground may ask. The edge checks it with
  ``validate_describe_unit`` before its authority and fence checks, so the
  strict ``validate_ground_to_edge`` still refuses it until the edge adopts
  it (A3).
* **The reply** carries ``unit``, ``gates``, ``values`` and ``unit_rev``,
  optionally ``ap_link_loss`` (from ``AP_LINK_LOSS_ACTIONS``) and
  ``accepts``. ``unit.link`` is only the link kind (``LINK_KINDS``), never an
  SSID, host or address; ``unit`` and each camera allow only their own keys.
  Bounds and cameras are objects, never lists of numbers, and a value that
  is a list is sent as comma-separated text. The whole reply is at most
  ``MAX_DESCRIBE_UNIT_BYTES``. A value row never has ``edit: "fact"``:
  test-adapter facts are never described.
* **``accepts`` (spec correction, risk R1).** Design section 18 says new
  commands go only to an edge whose ``describe_unit`` reply advertises
  them, but detail 12.2 gave the reply no key for that. ``accepts`` is that
  key: a list of names from ``ACCEPT_NAMES``. A ground ignores a name it
  does not know, and ``escort_setpoint`` is listed only by a field unit.
  ``may_send`` is the ground's gate.
* **A refused query** gets a ``nack`` with a reason from
  ``DESCRIBE_UNIT_NACK_REASONS`` (``busy`` when queries come too fast).
* **``unit_rev``** is 12 lowercase hex digits. It is optional in the
  protocol v2 edge ``hello``; ``may_describe`` answers whether a ground may
  ask an edge for its description (never without ``unit_rev``, PROTO-3).
* **``nack_reason_for_peer``.** An older ground drops a nack with the new
  reason ``straight_line_home`` as malformed, so an edge sends it only to a
  ground that has sent it ``describe_unit``, and ``invalid`` otherwise.

This module imports only ``checks`` and ``constants``; ``validate`` calls
into it.
"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from flightrisk.fleet_protocol.checks import (
    ProtocolError,
    _check_structure,
    _is_id,
    _is_int,
    _is_member,
    _is_number,
    _require,
    _require_drone_id,
    _require_id,
    _require_int_range,
    _require_object,
    _require_text,
    _short,
)
from flightrisk.fleet_protocol.constants import (
    ACK,
    ASSIGN_LANES,
    AP_LINK_LOSS_ACTIONS,
    DESCRIBE_UNIT,
    ESCORT_SETPOINT,
    HELLO,
    MAX_EPOCH,
    MAX_TEXT_LEN,
    NACK,
    RETURN_COMMAND_TYPES,
)

#: The names an edge may list in ``accepts``: the return commands and the
#: optional ``assign_lanes`` keys an older edge would refuse.
ACCEPT_NAMES = frozenset(RETURN_COMMAND_TYPES) | frozenset({
    "assign_lanes.on_complete.return_home",
    "assign_lanes.corridor",
    "assign_lanes.landing",
    "assign_lanes.no_entry",
    "assign_lanes.speed_cm_s",
})
#: Link kinds ``unit.link`` may name.
LINK_KINDS = frozenset({"wifi_ap", "wifi_station", "radio"})
UNIT_SCALES = frozenset({"room", "field"})
#: The ``edit`` kinds a described value may carry. ``fact`` is never sent.
DESCRIBED_EDIT_KINDS = frozenset({"ui", "file_env", "read_only"})
DESCRIBE_UNIT_NACK_REASONS = frozenset({"busy", "invalid"})
#: Nack reasons an older ground does not know; see ``nack_reason_for_peer``.
NEW_NACK_REASONS = frozenset({"straight_line_home"})
MAX_DESCRIBE_UNIT_BYTES = 16 * 1024
MAX_CAMERAS = 8
MAX_GATES = 64
MAX_VALUES = 256
UNIT_REV_RE = re.compile(r"[0-9a-f]{12}")

_UNIT_KEYS = frozenset({"model", "model_name", "adapter", "scale", "positioning", "link", "cameras"})
_CAMERA_KEYS = frozenset({
    "mount", "mount_kind", "orient", "pitch_min_deg", "pitch_max_deg", "role",
    "w_px", "h_px", "hfov_deg", "sensor", "radiometric",
})
_GATE_KEYS = frozenset({"on", "ok", "why", "src"})
_VALUE_KEYS = frozenset({"v", "src", "d", "lo", "hi", "edit"})


def is_unit_rev(value: Any) -> bool:
    """True for 12 lowercase hex digits."""
    return isinstance(value, str) and UNIT_REV_RE.fullmatch(value) is not None


def _require_v2(what: str, proto: int) -> None:
    if not (_is_int(proto) and proto >= 2):
        raise ProtocolError(f"{what} is protocol v2 only, got v{_short(proto)}")


def _only(obj: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    for key in obj:
        if key not in allowed:
            raise ProtocolError(f"{what} has the field {_short(key)}, which is not in its schema")


def _text(value: Any, what: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not (isinstance(value, str) and 0 < len(value) <= MAX_TEXT_LEN and value.isprintable()):
        raise ProtocolError(f"{what} must be printable text of 1 to {MAX_TEXT_LEN} characters, got {_short(value)}")


def _number(value: Any, what: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not _is_number(value):
        raise ProtocolError(f"{what} must be a number, got {_short(value)}")


def _keyed(obj: Any, what: str, limit: int) -> Mapping[str, Any]:
    if not isinstance(obj, Mapping):
        raise ProtocolError(f"{what} must be an object, got {_short(obj)}")
    if len(obj) > limit:
        raise ProtocolError(f"{what} has {len(obj)} entries; the limit is {limit}")
    for key, item in obj.items():
        if not _is_id(key):
            raise ProtocolError(f"{what} key {_short(key)} is not an identifier")
        if not isinstance(item, Mapping):
            raise ProtocolError(f"{what}.{key} must be an object, got {_short(item)}")
    return obj


def _validate_camera(cam: Mapping[str, Any], what: str) -> None:
    _only(cam, _CAMERA_KEYS, what)
    for key in ("mount", "mount_kind", "sensor"):
        _text(cam.get(key), f"{what}.{key}")
    _text(cam.get("orient"), f"{what}.orient", nullable=True)
    _text(cam.get("role"), f"{what}.role", nullable=True)
    for key in ("pitch_min_deg", "pitch_max_deg"):
        _number(cam.get(key), f"{what}.{key}", nullable=True)
    for key in ("w_px", "h_px"):
        value = cam.get(key)
        if not (_is_int(value) and 0 < value <= 100_000):
            raise ProtocolError(f"{what}.{key} must be a positive integer, got {_short(value)}")
    hfov = cam.get("hfov_deg")
    if not (_is_number(hfov) and 0 < hfov <= 360):
        raise ProtocolError(f"{what}.hfov_deg must be a number above 0 and at most 360, got {_short(hfov)}")
    if not isinstance(cam.get("radiometric"), bool):
        raise ProtocolError(f"{what}.radiometric must be true or false, got {_short(cam.get('radiometric'))}")


def _validate_unit(unit: Mapping[str, Any]) -> str:
    _only(unit, _UNIT_KEYS, "'describe_unit' reply 'unit'")
    for key in ("model", "adapter"):
        if not _is_id(unit.get(key)):
            raise ProtocolError(f"'describe_unit' reply 'unit.{key}' must be an identifier, got {_short(unit.get(key))}")
    _text(unit.get("model_name"), "'describe_unit' reply 'unit.model_name'")
    _text(unit.get("positioning"), "'describe_unit' reply 'unit.positioning'")
    scale = unit.get("scale")
    if not _is_member(scale, UNIT_SCALES):
        raise ProtocolError(f"'describe_unit' reply 'unit.scale' must be one of {sorted(UNIT_SCALES)}, got {_short(scale)}")
    link = unit.get("link")
    if not _is_member(link, LINK_KINDS):
        raise ProtocolError(
            f"'describe_unit' reply 'unit.link' must be a link kind {sorted(LINK_KINDS)}, never an address, "
            f"got {_short(link)}"
        )
    cameras = _keyed(unit.get("cameras"), "'describe_unit' reply 'unit.cameras'", MAX_CAMERAS)
    for cam_id, cam in cameras.items():
        _validate_camera(cam, f"'describe_unit' reply camera {cam_id!r}")
    return scale


def _validate_scalar(value: Any, what: str) -> None:
    if value is None or isinstance(value, bool) or _is_number(value):
        return
    if isinstance(value, str) and len(value) <= MAX_TEXT_LEN and value.isprintable():
        return
    raise ProtocolError(f"{what} must be a number, true, false, short text or null, got {_short(value)}")


def _validate_values(values: Mapping[str, Any]) -> None:
    for param_id, row in _keyed(values, "'describe_unit' reply 'values'", MAX_VALUES).items():
        what = f"'describe_unit' value {param_id!r}"
        _only(row, _VALUE_KEYS, what)
        _validate_scalar(row.get("v"), f"{what} 'v'")
        _validate_scalar(row.get("d"), f"{what} 'd'")
        _number(row.get("lo"), f"{what} 'lo'", nullable=True)
        _number(row.get("hi"), f"{what} 'hi'", nullable=True)
        if not _is_id(row.get("src")):
            raise ProtocolError(f"{what} 'src' must be an identifier, got {_short(row.get('src'))}")
        if not _is_member(row.get("edit"), DESCRIBED_EDIT_KINDS):
            raise ProtocolError(
                f"{what} 'edit' must be one of {sorted(DESCRIBED_EDIT_KINDS)}, got {_short(row.get('edit'))}"
            )


def _validate_gates(gates: Mapping[str, Any]) -> None:
    for feature_id, gate in _keyed(gates, "'describe_unit' reply 'gates'", MAX_GATES).items():
        what = f"'describe_unit' gate {feature_id!r}"
        _only(gate, _GATE_KEYS, what)
        for key in ("on", "ok"):
            if not isinstance(gate.get(key), bool):
                raise ProtocolError(f"{what} '{key}' must be true or false, got {_short(gate.get(key))}")
        _text(gate.get("why"), f"{what} 'why'", nullable=True)
        if not _is_id(gate.get("src")):
            raise ProtocolError(f"{what} 'src' must be an identifier, got {_short(gate.get('src'))}")


def _validate_accepts(accepts: Any, scale: str) -> None:
    if not isinstance(accepts, list) or len(accepts) > 32:
        raise ProtocolError(f"'describe_unit' reply 'accepts' must be a list of at most 32 names, got {_short(accepts)}")
    seen = set()
    for name in accepts:
        if not (isinstance(name, str) and 0 < len(name) <= 64 and name.isprintable()) or name in seen:
            raise ProtocolError(f"'describe_unit' reply 'accepts' has a bad or repeated name {_short(name)}")
        seen.add(name)
    if ESCORT_SETPOINT in seen and scale != "field":
        raise ProtocolError("'describe_unit' reply lists 'escort_setpoint', which only a field unit accepts")


def encoded_size(msg: Mapping[str, Any]) -> int:
    """Bytes of *msg* as compact UTF-8 JSON."""
    return len(json.dumps(msg, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))


def validate_describe_unit(msg: Mapping[str, Any], *, proto: int) -> None:
    """Check a ``describe_unit`` query. Raises ``ProtocolError``."""
    if msg.get("type") != DESCRIBE_UNIT:
        raise ProtocolError(f"expected a 'describe_unit' query, got {_short(msg.get('type'))}")
    _require_v2("'describe_unit'", proto)
    _check_structure(msg)
    _require_drone_id(msg)
    _require_id(msg, "cmd_id")


def is_describe_unit_reply(msg: Mapping[str, Any]) -> bool:
    """True for an ``ack`` or ``nack`` answering ``describe_unit``."""
    return msg.get("type") in (ACK, NACK) and msg.get("for") == DESCRIBE_UNIT


def validate_describe_unit_reply(msg: Mapping[str, Any], *, proto: int) -> None:
    """Check an ``ack`` or ``nack`` for ``describe_unit`` (protocol v2 only). Raises ``ProtocolError``."""
    _require_v2("a 'describe_unit' reply", proto)
    _check_structure(msg)
    _require_drone_id(msg)
    _require_id(msg, "cmd_id")
    if "epoch_seen" in msg:
        _require_int_range(msg, "epoch_seen", 0, MAX_EPOCH)
    if "detail" in msg:
        _require_text(msg, "detail")
    if msg.get("type") == NACK:
        reason = _require(msg, "reason")
        if not _is_member(reason, DESCRIBE_UNIT_NACK_REASONS):
            raise ProtocolError(
                f"'nack' field 'reason' must be one of {sorted(DESCRIBE_UNIT_NACK_REASONS)}, got {_short(reason)}"
            )
        return
    if _require(msg, "result") != "ok":
        raise ProtocolError(f"'ack' field 'result' must be 'ok', got {_short(msg.get('result'))}")
    if not is_unit_rev(_require(msg, "unit_rev")):
        raise ProtocolError(f"'unit_rev' must be 12 lowercase hex digits, got {_short(msg.get('unit_rev'))}")
    scale = _validate_unit(_require_object(msg, "unit"))
    _validate_gates(_require_object(msg, "gates"))
    _validate_values(_require_object(msg, "values"))
    if "ap_link_loss" in msg and not _is_member(msg["ap_link_loss"], AP_LINK_LOSS_ACTIONS):
        raise ProtocolError(
            f"'ap_link_loss' must be one of {sorted(AP_LINK_LOSS_ACTIONS)}, got {_short(msg['ap_link_loss'])}"
        )
    if "accepts" in msg:
        _validate_accepts(msg["accepts"], scale)
    size = encoded_size(msg)
    if size > MAX_DESCRIBE_UNIT_BYTES:
        raise ProtocolError(f"'describe_unit' reply is {size} bytes; the limit is {MAX_DESCRIBE_UNIT_BYTES}")


def validate_edge_hello_unit_rev(msg: Mapping[str, Any]) -> None:
    """Check the optional ``unit_rev`` of an edge ``hello`` (PROTO-2). Raises ``ProtocolError``."""
    if "unit_rev" in msg and not is_unit_rev(msg["unit_rev"]):
        raise ProtocolError(f"'hello' field 'unit_rev' must be 12 lowercase hex digits, got {_short(msg['unit_rev'])}")


def make_describe_unit(drone_id: str, *, cmd_id: str | None = None) -> dict[str, Any]:
    """Ground -> edge: ask the edge for its description."""
    msg = {"type": DESCRIBE_UNIT, "drone_id": drone_id, "cmd_id": cmd_id or uuid.uuid4().hex}
    validate_describe_unit(msg, proto=2)
    return msg


def make_describe_unit_ack(
    drone_id: str,
    cmd_id: str,
    *,
    unit_rev: str,
    unit: Mapping[str, Any],
    gates: Mapping[str, Any],
    values: Mapping[str, Any],
    ap_link_loss: str | None = None,
    accepts: Iterable[str] = (),
) -> dict[str, Any]:
    """Edge -> ground: the unit's description (detail section 12.2), checked before it is returned."""
    msg: dict[str, Any] = {
        "type": ACK, "for": DESCRIBE_UNIT, "drone_id": drone_id, "cmd_id": cmd_id, "result": "ok",
        "unit_rev": unit_rev, "unit": dict(unit), "gates": dict(gates), "values": dict(values),
    }
    if ap_link_loss is not None:
        msg["ap_link_loss"] = ap_link_loss
    names = sorted(set(accepts))
    if names:
        msg["accepts"] = names
    validate_describe_unit_reply(msg, proto=2)
    return msg


def make_describe_unit_nack(drone_id: str, cmd_id: str, reason: str, *, detail: str | None = None) -> dict[str, Any]:
    """Edge -> ground: the query was refused (``busy`` when queries come too fast)."""
    msg: dict[str, Any] = {"type": NACK, "for": DESCRIBE_UNIT, "drone_id": drone_id, "cmd_id": cmd_id, "reason": reason}
    if detail is not None:
        msg["detail"] = detail
    validate_describe_unit_reply(msg, proto=2)
    return msg


def may_describe(edge_hello: Any) -> bool:
    """True when a ground may send ``describe_unit`` to the edge that sent *edge_hello* (PROTO-3).

    Only a protocol v2 edge ``hello`` with a valid ``unit_rev`` qualifies, so
    an older edge, or one on a v1 link, is never asked.
    """
    return (
        isinstance(edge_hello, Mapping)
        and edge_hello.get("type") == HELLO
        and _is_int(edge_hello.get("proto"))
        and edge_hello["proto"] >= 2
        and is_unit_rev(edge_hello.get("unit_rev"))
    )


def required_accepts(msg: Mapping[str, Any]) -> frozenset[str]:
    """The ``ACCEPT_NAMES`` a message needs the edge to have advertised before it is sent."""
    mtype = msg.get("type")
    if mtype in RETURN_COMMAND_TYPES:
        return frozenset({mtype})
    if mtype != ASSIGN_LANES:
        return frozenset()
    names = {f"assign_lanes.{key}" for key in ("corridor", "landing", "no_entry", "speed_cm_s") if key in msg}
    if msg.get("on_complete") == "return_home":
        names.add("assign_lanes.on_complete.return_home")
    return frozenset(names)


def may_send(peer_description: Any, what: str | Mapping[str, Any]) -> bool:
    """The ground's gate: True when *what* may go to the edge described by *peer_description*.

    *what* is a name from ``ACCEPT_NAMES``, another message type, or a whole
    message. *peer_description* is the edge's validated ``describe_unit``
    ack, or None when it has none. Anything that needs a name from
    ``ACCEPT_NAMES`` is allowed only when the ack's ``accepts`` lists it;
    everything else (today's messages) is always allowed. ``describe_unit``
    itself is gated by ``may_describe`` on the edge's ``hello``.
    """
    if what == DESCRIBE_UNIT or (isinstance(what, Mapping) and what.get("type") == DESCRIBE_UNIT):
        raise ValueError("describe_unit is gated by may_describe(edge_hello), not by a description")
    if isinstance(what, Mapping):
        needed = required_accepts(what)
    else:
        needed = frozenset({what}) & ACCEPT_NAMES
    if not needed:
        return True
    if not isinstance(peer_description, Mapping):
        return False
    accepts = peer_description.get("accepts")
    if not isinstance(accepts, list):
        return False
    return needed <= {name for name in accepts if isinstance(name, str)}


def nack_reason_for_peer(reason: str, peer_query: Any) -> str:
    """The nack reason an edge may send a ground (risk R2).

    A reason in ``NEW_NACK_REASONS`` goes only to a ground that has sent this
    edge a ``describe_unit`` query (*peer_query*, the last one it sent, or
    None), which proves the ground understands it; any other ground gets
    ``invalid``, with the same ``detail`` text. Other reasons pass unchanged.

    A ``describe_unit`` query carries no sender identity, so the caller must
    pass the last query received on the same connection as the nack's
    destination, never the last one the edge received from any ground. After
    a failover to an older standby, a query from the former leader proves
    nothing about the new ground, which would drop the nack as malformed.
    """
    if reason not in NEW_NACK_REASONS:
        return reason
    if isinstance(peer_query, Mapping) and peer_query.get("type") == DESCRIBE_UNIT:
        return reason
    return "invalid"
