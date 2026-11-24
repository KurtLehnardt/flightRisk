"""The shortest and longest single move of an envelope profile.

A small module on purpose: the search and path helpers need the move range
without importing the fleet plan checker, which pulls in the coverage
simulator and the fleet protocol. It imports only ``flightrisk.drone.safety``.
"""

from __future__ import annotations

import math
from typing import Any

from flightrisk.drone.safety import EnvelopeLimits


def _int_cm(name: str, value: Any, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number of centimetres, got {value!r}")
    if value != int(value):
        raise ValueError(f"{name} must be a whole number of centimetres, got {value!r}")
    value = int(value)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum} cm, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum} cm, got {value}")
    return value


def _envelope(limits: Any) -> EnvelopeLimits:
    if not isinstance(limits, EnvelopeLimits):
        raise TypeError(f"limits must be a safety.EnvelopeLimits, got {type(limits).__name__}")
    return limits


def _move_range(limits: Any) -> tuple[int, int]:
    """The shortest and longest single move of an envelope profile, as whole centimetres.

    The planner needs the longest move to be at least twice the shortest: a
    leg just longer than one move is split into two moves of at least the
    minimum, and a leg shorter than the minimum is flown as an overshoot pair
    of up to twice the minimum.
    """
    limits = _envelope(limits)
    low = _int_cm(f"{limits.name} min_move_cm", limits.min_move_cm, 1)
    high = _int_cm(f"{limits.name} max_move_cm", limits.max_move_cm, 1)
    if high < 2 * low:
        raise ValueError(
            f"{limits.name} max_move_cm must be at least twice min_move_cm for a plan, got {low}-{high} cm"
        )
    return low, high
