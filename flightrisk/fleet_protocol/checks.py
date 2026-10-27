"""``ProtocolError`` and the field, structure and privacy checks the validators share."""
from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping
from typing import Any

from flightrisk.fleet_protocol.constants import (
    MAX_DEPTH,
    MAX_FIELDS,
    MAX_ID_LEN,
    MAX_LISTED_NUMBERS,
    MAX_MESSAGE_BYTES,
    MAX_NODES,
    MAX_NUMBERS_PER_LIST,
    MAX_TEXT_LEN,
    MIN_PROTOCOL_VERSION,
    PRIVACY_FORBIDDEN_KEYS,
    PRIVACY_FORBIDDEN_KEY_PARTS,
    _BASE64_RE,
    _DATA_URI_RE,
    _ID_RE,
    _KEY_SEPARATORS_RE,
    _MAX_ABS_NUMBER,
    _MAX_PROTO_FIELD,
    _MAX_SAFE_INT,
)


class ProtocolError(ValueError):
    """A fleet control message does not match the protocol schema."""


# ---------------------------------------------------------------------------
# Field checks
# ---------------------------------------------------------------------------


def _short(value: Any) -> str:
    """``repr`` capped in length, so a huge hostile value cannot flood the log."""
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _is_number(value: Any) -> bool:
    """True for a finite int or float of sane magnitude (bool is rejected even though it is an int)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -_MAX_ABS_NUMBER <= value <= _MAX_ABS_NUMBER
    if isinstance(value, float):
        return math.isfinite(value) and abs(value) <= _MAX_ABS_NUMBER
    return False


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_member(value: Any, choices: frozenset[str]) -> bool:
    """``value in choices`` that is False, not a TypeError, for unhashable values."""
    return isinstance(value, str) and value in choices


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def _require(msg: Mapping[str, Any], key: str) -> Any:
    if key not in msg:
        raise ProtocolError(f"{_short(msg.get('type'))} message is missing {key!r}")
    return msg[key]


def _require_number(msg: Mapping[str, Any], key: str) -> float:
    value = _require(msg, key)
    if not _is_number(value):
        raise ProtocolError(f"{msg.get('type')!r} field {key!r} must be a finite number, got {_short(value)}")
    return value


def _require_optional_number(msg: Mapping[str, Any], key: str) -> None:
    value = _require(msg, key)
    if value is not None and not _is_number(value):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be a finite number or null, got {_short(value)}"
        )


def _require_optional_int(msg: Mapping[str, Any], key: str) -> None:
    value = _require(msg, key)
    if value is not None and not (_is_int(value) and 0 <= value <= _MAX_SAFE_INT):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be a non-negative integer or null, got {_short(value)}"
        )


def _require_int_range(msg: Mapping[str, Any], key: str, low: int, high: int) -> int:
    value = _require(msg, key)
    if not (_is_int(value) and low <= value <= high):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be an integer from {low} to {high}, got {_short(value)}"
        )
    return value


def _require_bool(msg: Mapping[str, Any], key: str) -> bool:
    value = _require(msg, key)
    if not isinstance(value, bool):
        raise ProtocolError(f"{msg.get('type')!r} field {key!r} must be a boolean, got {_short(value)}")
    return value


def _require_drone_id(msg: Mapping[str, Any], key: str = "drone_id") -> str:
    value = _require(msg, key)
    if not isinstance(value, str) or not value or len(value) > MAX_ID_LEN:
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be a non-empty string of at most "
            f"{MAX_ID_LEN} characters, got {_short(value)}"
        )
    return value


def _require_id(msg: Mapping[str, Any], key: str, *, nullable: bool = False) -> str | None:
    value = _require(msg, key)
    if value is None and nullable:
        return None
    if not _is_id(value):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be an identifier of 1 to {MAX_ID_LEN} characters "
            f"(letters, digits, '.', '_', ':', '-'){' or null' if nullable else ''}, got {_short(value)}"
        )
    return value


def _require_choice(msg: Mapping[str, Any], key: str, choices: frozenset[str]) -> str:
    value = _require(msg, key)
    if not _is_member(value, choices):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be one of {sorted(choices)}, got {_short(value)}"
        )
    return value


def _require_text(msg: Mapping[str, Any], key: str) -> str:
    value = _require(msg, key)
    if not (isinstance(value, str) and 0 < len(value) <= MAX_TEXT_LEN and value.isprintable()):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be printable text of 1 to {MAX_TEXT_LEN} "
            f"characters, got {_short(value)}"
        )
    return value


def _require_list(msg: Mapping[str, Any], key: str, max_len: int) -> list[Any]:
    value = _require(msg, key)
    if not isinstance(value, list):
        raise ProtocolError(f"{msg.get('type')!r} field {key!r} must be a list, got {_short(value)}")
    if len(value) > max_len:
        raise ProtocolError(f"{msg.get('type')!r} field {key!r} has {len(value)} items; the limit is {max_len}")
    return value


def _require_object(msg: Mapping[str, Any], key: str, *, nullable: bool = False) -> Mapping[str, Any] | None:
    value = _require(msg, key)
    if value is None and nullable:
        return None
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{msg.get('type')!r} field {key!r} must be an object, got {_short(value)}")
    return value


def _is_private_key(key: str) -> bool:
    """True when *key* names imagery or biometric data (see ``PRIVACY_FORBIDDEN_KEYS``)."""
    folded = unicodedata.normalize("NFKC", key).casefold()
    compact = _KEY_SEPARATORS_RE.sub("", folded)
    if folded in PRIVACY_FORBIDDEN_KEYS or compact in PRIVACY_FORBIDDEN_KEYS:
        return True
    return any(part in compact for part in PRIVACY_FORBIDDEN_KEY_PARTS)


def _looks_like_encoded_media(text: str) -> bool:
    """True for a data URI or a long base64-looking string (an encoded image or vector)."""
    if _DATA_URI_RE.search(text):
        return True
    return len(text) > MAX_ID_LEN and _BASE64_RE.fullmatch(text) is not None


def _is_plain_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_structure(msg: Mapping[str, Any], *, max_text: int = MAX_MESSAGE_BYTES) -> None:
    """Caps on shape (fields, depth, size) plus the privacy screen.

    Walks the message iteratively, so a deeply nested value raises
    ``ProtocolError`` instead of ``RecursionError``. *max_text* caps every
    string, keys included (the peer link passes ``MAX_TEXT_LEN``).
    """
    mtype = _short(msg.get("type"))
    if len(msg) > MAX_FIELDS:
        raise ProtocolError(f"{mtype} message has {len(msg)} fields; the limit is {MAX_FIELDS}")
    stack: list[tuple[Any, int]] = [(msg, 1)]
    nodes = 0
    listed_numbers = 0

    def check_text(text: str, what: str) -> None:
        if len(text) > max_text:
            raise ProtocolError(f"{mtype} message has a {what} longer than {max_text} characters")
        if _looks_like_encoded_media(text):
            raise ProtocolError(
                f"{mtype} message has a {what} that looks like encoded media (a data URI or "
                "base64); fleet control messages never carry imagery or biometric data"
            )

    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            raise ProtocolError(f"{mtype} message has more than {MAX_NODES} values")
        if isinstance(value, Mapping):
            if depth > MAX_DEPTH:
                raise ProtocolError(f"{mtype} message nests deeper than {MAX_DEPTH} levels")
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ProtocolError(f"{mtype} message has a non-string key {_short(key)}")
                check_text(key, "key")
                if _is_private_key(key):
                    raise ProtocolError(
                        f"{mtype} message has the field {_short(key)}; fleet control "
                        "messages never carry imagery or biometric data"
                    )
                stack.append((item, depth + 1))
        elif isinstance(value, (list, tuple)):
            if depth > MAX_DEPTH:
                raise ProtocolError(f"{mtype} message nests deeper than {MAX_DEPTH} levels")
            numbers = sum(1 for item in value if _is_plain_number(item))
            listed_numbers += numbers
            if numbers > MAX_NUMBERS_PER_LIST or listed_numbers > MAX_LISTED_NUMBERS:
                raise ProtocolError(
                    f"{mtype} message has a list of {numbers} numbers ({listed_numbers} in all); the limits "
                    f"are {MAX_NUMBERS_PER_LIST} per list and {MAX_LISTED_NUMBERS} per message, because "
                    "fleet control messages never carry embeddings"
                )
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            check_text(value, "string")
        elif isinstance(value, (bytes, bytearray, memoryview)):
            raise ProtocolError(f"{mtype} message has a binary value; fleet control messages carry only JSON")


def _require_only(obj: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    """Peer-link allowlist: refuse any field of *obj* that is not in *allowed*."""
    for key in obj:
        if key not in allowed:
            raise ProtocolError(
                f"{what} has the field {_short(key)}, which is not in its schema; peer messages "
                "carry only the fields the protocol defines"
            )


def _validate_proto_field(msg: Mapping[str, Any]) -> int:
    return _require_int_range(msg, "proto", MIN_PROTOCOL_VERSION, _MAX_PROTO_FIELD)
