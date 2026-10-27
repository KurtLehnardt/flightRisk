"""JSON wire encoding: ``encode``, ``decode`` and the fail-safe landing parse."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from flightrisk.fleet_protocol.checks import (
    ProtocolError,
    _is_int,
    _short,
)
from flightrisk.fleet_protocol.constants import (
    DIRECTION_EDGE_TO_GROUND,
    DIRECTION_GROUND_TO_EDGE,
    EMERGENCY_LAND_ALL,
    FAILSAFE_MAX_BYTES,
    FAILSAFE_TYPES,
    LAND,
    LEGACY_EDGE_TO_GROUND_TYPES,
    LEGACY_GROUND_TO_EDGE_TYPES,
    LEGACY_MAX_BYTES,
    MAX_MESSAGE_BYTES,
)
from flightrisk.fleet_protocol.validate import (
    validate_for_direction,
)

# ---------------------------------------------------------------------------
# Wire encoding
# ---------------------------------------------------------------------------


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"control message repeats the key {_short(key)}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise ProtocolError(f"control message contains the non-finite number {name}")


def _parse_finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ProtocolError(f"control message contains the out-of-range number {_short(text)}")
    return value


# The legacy non-fleet types that may travel in each direction. They are
# allowed up to ``LEGACY_MAX_BYTES``; fleet messages up to ``MAX_MESSAGE_BYTES``.
_LEGACY_TYPES_BY_DIRECTION = {
    DIRECTION_GROUND_TO_EDGE: LEGACY_GROUND_TO_EDGE_TYPES,
    DIRECTION_EDGE_TO_GROUND: LEGACY_EDGE_TO_GROUND_TYPES,
}


def _is_legacy(msg: Any, direction: str) -> bool:
    """True when *msg* is an object whose ``type`` is a legacy type of *direction*."""
    legacy = _LEGACY_TYPES_BY_DIRECTION.get(direction) if isinstance(direction, str) else None
    if not legacy or not isinstance(msg, Mapping):
        return False
    mtype = msg.get("type")
    return isinstance(mtype, str) and mtype in legacy


def encode(msg: Mapping[str, Any], *, direction: str, proto: int) -> str:
    """Validate *msg* for *direction* and *proto*, then serialise it as compact JSON.

    Raises ``ProtocolError`` for an invalid message or one larger than
    ``MAX_MESSAGE_BYTES`` encoded (``LEGACY_MAX_BYTES`` for a legacy
    ``detections``, ``set_target`` or ``stream_video`` message in its own
    direction).
    """
    validate_for_direction(msg, direction, proto=proto)
    try:
        text = json.dumps(msg, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"control message cannot be encoded as JSON: {exc}") from exc
    limit = LEGACY_MAX_BYTES if _is_legacy(msg, direction) else MAX_MESSAGE_BYTES
    if len(text.encode("utf-8")) > limit:
        raise ProtocolError(f"encoded control message exceeds {limit} bytes")
    return text


def _lenient_int(text: str) -> int | float:
    try:
        return int(text)
    except ValueError:
        # More digits than Python converts by default. The value is never
        # used by the fail-safe parse, which only reads ``type``.
        return math.inf


def _top_level_types(source: str | bytes) -> list[Any] | None:
    """Every value of the top-level ``type`` key, parsed as leniently as ``json`` allows.

    Repeated keys are kept (each occurrence is returned), ``NaN``,
    ``Infinity``, out-of-range numbers, huge integers and control characters
    in strings are accepted. Returns ``None`` when *source* is not a JSON
    object. Never raises.
    """
    outermost: list[list[tuple[str, Any]]] = []

    def keep_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        # Objects close innermost first, so the last call is the top-level
        # object whenever the top-level value is an object.
        outermost[:] = [pairs]
        return dict(pairs)

    try:
        value = json.loads(source, object_pairs_hook=keep_pairs, parse_int=_lenient_int, strict=False)
    except (ValueError, RecursionError, TypeError, OverflowError):
        return None
    if not isinstance(value, dict) or not outermost:
        return None
    return [item for key, item in outermost[0] if key == "type"]


def _may_name_a_landing(raw: str | bytes | bytearray) -> bool:
    """False only when no JSON string in *raw* can decode to ``land`` or ``emergency_land_all``.

    A cheap prefilter for the lenient parse, which costs tens of
    milliseconds on a hostile 1 MiB frame. Both landing types contain the
    four characters ``land``. A JSON string can only decode to a value
    containing them if the source has them literally, or spells some of
    them with a backslash escape. ``json.loads`` reads bytes as UTF-8
    unless they are UTF-16 or UTF-32, and ASCII text in those encodings
    always contains NUL bytes, so bytes with a NUL are always parsed.
    Replacing invalid UTF-8 only ever adds U+FFFD, never a letter.
    """
    if isinstance(raw, str):
        return "land" in raw or "\\" in raw
    return b"land" in raw or b"\\" in raw or b"\x00" in raw


def failsafe_type_of(raw: Any, *, max_bytes: int = FAILSAFE_MAX_BYTES) -> str | None:
    """The landing a received ground-to-edge message asks for, read leniently.

    This is the fail-safe fallback ``decode`` uses after its strict parser
    rejects an input. Returns ``"emergency_land_all"`` or ``"land"`` when
    *raw* (``str``, ``bytes`` or ``bytearray`` of at most *max_bytes* bytes,
    measured as UTF-8 for ``str``) is a JSON object whose top-level ``type``
    key, or any occurrence of that key when it is repeated, is a landing type
    (``emergency_land_all`` wins over ``land``). Otherwise returns ``None``.
    Never raises.

    The parse accepts everything ``json.loads`` accepts (which is what the
    pre-v2 edge used, including bytes in UTF-16 or UTF-32 and a UTF-8 BOM),
    and additionally: repeated keys, ``NaN``/``Infinity``, numbers out of
    float range, integers with more digits than Python converts, control
    characters inside strings, invalid UTF-8 (replaced with U+FFFD) and a
    leading BOM in text. It does not accept input that is not a single JSON
    object, nesting deeper than the parser's recursion limit, or input over
    *max_bytes*. Treating ``{"type": "heartbeat", "type": "land"}`` as a
    landing is deliberate: landing is the safe reading of an ambiguous
    message.
    """
    if not (_is_int(max_bytes) and max_bytes > 0):
        return None
    sources: list[str | bytes] = []
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > max_bytes:
            return None
        data = bytes(raw)
        # json.loads detects UTF-8/16/32 and a UTF-8 BOM itself; the second
        # attempt replaces bytes that are not valid UTF-8.
        sources = [data, data.decode("utf-8", "replace").removeprefix("﻿")]
    elif isinstance(raw, str):
        if len(raw) > max_bytes or len(raw.encode("utf-8", "surrogatepass")) > max_bytes:
            return None
        sources = [raw.removeprefix("﻿")]
    else:
        return None
    if not _may_name_a_landing(raw):
        return None
    for source in sources:
        types = _top_level_types(source)
        if types is None:
            continue
        found = {value for value in types if isinstance(value, str) and value in FAILSAFE_TYPES}
        if EMERGENCY_LAND_ALL in found:
            return EMERGENCY_LAND_ALL
        if LAND in found:
            return LAND
        return None
    return None


def _wire_size(raw: Any, limit: int) -> int:
    """The size of *raw* in bytes (UTF-8 for ``str``), or more than *limit* if it is larger."""
    if isinstance(raw, (bytes, bytearray)):
        return len(raw)
    if isinstance(raw, str):
        if len(raw) > limit:
            # Never fewer bytes than characters: skip encoding a huge string.
            return len(raw)
        return len(raw.encode("utf-8", "surrogatepass"))
    raise ProtocolError(f"control message must be text or bytes, got {type(raw).__name__}")


def _parse_strict(raw: Any, max_bytes: int) -> Any:
    """Parse *raw* as strict JSON, or raise ``ProtocolError``."""
    if _wire_size(raw, max_bytes) > max_bytes:
        raise ProtocolError(f"control message exceeds {max_bytes} bytes")
    if isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("control message is not valid UTF-8") from exc
    else:
        text = raw
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_finite_float,
        )
    except ProtocolError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ProtocolError(f"control message is not valid JSON: {_short(str(exc))}") from exc


def decode(
    raw: str | bytes | bytearray,
    *,
    direction: str,
    proto: int,
    max_bytes: int = MAX_MESSAGE_BYTES,
    require_fence: bool = False,
) -> dict[str, Any]:
    """Parse and strictly validate one received control message.

    Rejects, with ``ProtocolError`` and nothing else: input over *max_bytes*,
    invalid UTF-8 or JSON, repeated keys, ``NaN``/``Infinity``, nesting too
    deep for the parser, and anything ``validate_for_direction`` rejects.

    *max_bytes* applies to fleet messages. A legacy message of the direction
    (``detections`` from an edge, ``set_target`` or ``stream_video`` from a
    ground) may be up to ``max(max_bytes, LEGACY_MAX_BYTES)`` (1 MiB, the
    WebSocket frame limit), as before v2; it is parsed with the same strict
    rules and passed through by type, so its own handler checks it.

    Fail-safe exception (ground-to-edge direction only): when the strict
    parse rejects *raw* but ``failsafe_type_of`` reads it as a landing
    (bounded by ``max(max_bytes, FAILSAFE_MAX_BYTES)``), the result is
    ``{"type": "land"}`` or ``{"type": "emergency_land_all"}`` and nothing
    else, so an edge never refuses a landing that the pre-v2 edge executed.
    A landing that parses strictly is returned whole whatever *proto* and
    *require_fence* are, including an invalid *proto* such as ``None``.
    """
    if not (_is_int(max_bytes) and max_bytes > 0):
        raise ProtocolError(f"max_bytes must be a positive integer, got {_short(max_bytes)}")
    has_legacy = isinstance(direction, str) and direction in _LEGACY_TYPES_BY_DIRECTION
    legacy_limit = max(max_bytes, LEGACY_MAX_BYTES) if has_legacy else max_bytes
    try:
        msg = _parse_strict(raw, legacy_limit)
        if legacy_limit > max_bytes and not _is_legacy(msg, direction) and _wire_size(raw, max_bytes) > max_bytes:
            raise ProtocolError(f"control message exceeds {max_bytes} bytes")
    except ProtocolError:
        if direction == DIRECTION_GROUND_TO_EDGE:
            landing = failsafe_type_of(raw, max_bytes=max(max_bytes, FAILSAFE_MAX_BYTES))
            if landing is not None:
                return {"type": landing}
        raise
    try:
        validate_for_direction(msg, direction, proto=proto, require_fence=require_fence)
    except ProtocolError:
        # The validators accept a landing before looking at *proto*; this is
        # a second guard, so no validation failure can refuse a landing that
        # parsed strictly.
        if direction == DIRECTION_GROUND_TO_EDGE and isinstance(msg, dict) and _is_landing(msg.get("type")):
            return msg
        raise
    return msg


def _is_landing(mtype: Any) -> bool:
    return isinstance(mtype, str) and mtype in FAILSAFE_TYPES
