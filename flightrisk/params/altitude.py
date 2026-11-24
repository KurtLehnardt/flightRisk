"""The altitude limit of one unit from its resolved values (design section 27 item 53).

The regulatory cap (``regulatory.altitude_cap_cm``, the FAA's 120 m) is on by
default. With it on, the limit is the cap, or the airframe maximum
(``envelope.max_altitude_cm``) when that is known and lower. With it off, the
limit is the airframe maximum, which must be known: a unit with no known
airframe maximum cannot turn the cap off. No default is ever substituted for a
missing row or an unknown maximum.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from flightrisk.params.table import UNSET

CAP_ID = "regulatory.altitude_cap_cm"
CAP_ON_ID = "regulatory.altitude_cap_on"
AIRFRAME_MAX_ID = "envelope.max_altitude_cm"

#: The field rows whose upper bound is the cap. With the cap off it rises to the unit's known airframe
#: maximum, but only for a value from the inventory or the launcher; every other bound is unchanged.
CAP_BOUND_ROWS: frozenset[str] = frozenset({"envelope.ceiling_cm", "envelope.alt_slot_cm"})

#: The id of the acknowledgement the launcher records when the operator turns the cap off.
ACK_ID = "outside_faa_jurisdiction"
_RECORD_KEYS = frozenset({"value", "ack"})
_ACK_KEYS = frozenset({"id", "text_version", "at", "by"})


def _value(values: Mapping[str, Any], param_id: str) -> Any:
    """The resolved value of ``param_id``; a missing row raises ``ValueError``."""
    if param_id not in values:
        raise ValueError(f"{param_id} is missing from the resolved values")
    item = values[param_id]
    return getattr(item, "value", item)


def airframe_max_cm(values: Mapping[str, Any]) -> Optional[int]:
    """The known airframe maximum in centimetres, or ``None`` when it is unknown."""
    value = _value(values, AIRFRAME_MAX_ID)
    if value is None or value == UNSET:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{AIRFRAME_MAX_ID} is {value!r}, not a number")
    return int(value)


def altitude_limit_cm(values: Mapping[str, Any]) -> Optional[int]:
    """The highest altitude in centimetres the unit may fly.

    ``values`` maps a parameter id to its resolved value or ``EffectiveValue``.
    Raises ``ValueError`` for a missing row, and when the cap is off and the
    airframe maximum is unknown.
    """
    cap_on = _value(values, CAP_ON_ID)
    if not isinstance(cap_on, bool):
        raise ValueError(f"{CAP_ON_ID} is {cap_on!r}, not true or false")
    cap = _value(values, CAP_ID)
    if isinstance(cap, bool) or not isinstance(cap, (int, float)):
        raise ValueError(f"{CAP_ID} is {cap!r}, not a number")
    known = airframe_max_cm(values)
    if cap_on:
        return int(cap) if known is None else min(int(cap), known)
    if known is None:
        raise ValueError("the altitude cap cannot be turned off: this unit has no known airframe maximum")
    return known


def _utc(text: Any) -> bool:
    """True when ``text`` is an ISO 8601 time with a UTC offset of zero."""
    if not isinstance(text, str) or not text.strip():
        return False
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return False
    return when.utcoffset() == timedelta(0)


def acknowledgement_problem(record: Any) -> Optional[str]:
    """``None`` for a complete cap-off acknowledgement record, or what is wrong with it.

    The record is the launcher's ``confirmed["regulatory.altitude_cap_on"]``:
    ``{"value": false, "ack": {"id": "outside_faa_jurisdiction", "text_version": str,
    "at": ISO 8601 UTC, "by": role}}``. No other key is accepted.
    """
    if not isinstance(record, Mapping):
        return "the confirmation is not an acknowledgement record with `value` and `ack`"
    extra = sorted(str(k) for k in record if k not in _RECORD_KEYS)
    if extra:
        return f"the confirmation has unknown keys {', '.join(extra)}"
    if record.get("value") is not False:
        return "the confirmation's `value` is not false"
    ack = record.get("ack")
    if not isinstance(ack, Mapping):
        return "the confirmation has no `ack` object"
    extra = sorted(str(k) for k in ack if k not in _ACK_KEYS)
    if extra:
        return f"the acknowledgement has unknown keys {', '.join(extra)}"
    if ack.get("id") != ACK_ID:
        return f"the acknowledgement `id` is not {ACK_ID}"
    for key in ("text_version", "by"):
        value = ack.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"the acknowledgement has no `{key}`"
    if not _utc(ack.get("at")):
        return "the acknowledgement `at` is not an ISO 8601 time in UTC"
    return None
