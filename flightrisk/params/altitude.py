"""The altitude limit of one unit from its resolved values (design section 27 item 53).

The regulatory cap (``regulatory.altitude_cap_cm``, the FAA's 120 m) is on by
default. With it on, the limit is the cap, or the airframe maximum
(``envelope.max_altitude_cm``) when that is known and lower. With it off, the
limit is the airframe maximum, which must be known: a unit with no known
airframe maximum cannot turn the cap off. No default is ever substituted for a
missing row or an unknown maximum.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from flightrisk.params.table import UNSET

CAP_ID = "regulatory.altitude_cap_cm"
CAP_ON_ID = "regulatory.altitude_cap_on"
AIRFRAME_MAX_ID = "envelope.max_altitude_cm"


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
