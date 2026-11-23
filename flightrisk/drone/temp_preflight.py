"""The Tello temperature check before a takeoff: parse the ``temp?`` reply and judge it.

Before every takeoff the lane executor asks a drone that has ``query_temperature()`` (the Tello) for
its temperature with the read-only SDK query ``temp?``. A Tello answers with the lowest and highest
temperature of its main board in degrees Celsius, for example ``53~56C`` (the ``templ`` and ``temph``
fields of its state packet); a single number such as ``56C`` is read as both. The highest value is
judged, because it is the one closest to the overheat point:

* at or above the refusal level (``FLIGHTRISK_TELLO_TEMP_REFUSE_C``, default 95) the takeoff is refused
  with an operator message, unless the refusal is switched off (``off``);
* at or above the warn level (``FLIGHTRISK_TELLO_TEMP_WARN_C``, default 90) the reading is logged as a
  warning and shown on the drone card;
* a reading that is missing (no reply, a timeout, a send that failed) or malformed is ``unknown``. It
  is logged as a warning and never refuses a takeoff.

Why these defaults (sources in ``docs/fleet-setup.md``, "Takeoff temperature check"): the Tello SDK
1.3 documents the ``temp?`` reply as 0 to 90 degrees Celsius, so 90 is the top of the documented
range; no Ryze or DJI document publishes a shutdown temperature. One logged report saw a Tello that
sat powered on reach 95 degrees and switch itself off, and another reports that a Tello EDU stops
taking off above about 100 degrees and shuts down above about 105. The refusal level is the lowest of
those reported shutdown points. A Tello at rest can read 91 to 93 degrees and still fly, so a
refusal at 70 would ground a healthy drone that is merely powered on.

Nothing here talks to a drone; the query itself is ``TelloController.query_temperature``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Environment variables read at the edge's start (``edge_device``).
ENV_TELLO_TEMP_WARN_C = "FLIGHTRISK_TELLO_TEMP_WARN_C"
ENV_TELLO_TEMP_REFUSE_C = "FLIGHTRISK_TELLO_TEMP_REFUSE_C"
#: The value of ``FLIGHTRISK_TELLO_TEMP_REFUSE_C`` that switches the refusal off (warn only).
REFUSE_OFF = "off"

#: The warn level: the top of the ``temp?`` range the Tello SDK 1.3 documents (0 to 90).
DEFAULT_TEMP_WARN_C = 90
#: The refusal level: the lowest overheat shutdown temperature reported for a Tello (95).
DEFAULT_TEMP_REFUSE_C = 95
#: Allowed warn and refusal levels, in degrees Celsius. Below 40 every Tello that has been on for
#: a minute would warn; above 120 is past every reported shutdown point.
TEMP_LIMIT_RANGE_C = (40, 120)
#: A reading outside this range is not a temperature this board reports, so it is ``unknown``.
TEMP_READING_RANGE_C = (-40, 150)

#: How long the edge waits for the reply to ``temp?``.
TEMP_QUERY_TIMEOUT_S = 2.5

#: Levels of a check.
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_REFUSE = "refuse"
LEVEL_UNKNOWN = "unknown"
LEVELS = (LEVEL_OK, LEVEL_WARN, LEVEL_REFUSE, LEVEL_UNKNOWN)

#: The telemetry field (edge to ground) that carries the last check, and its keys.
TELEMETRY_TEMP = "temp_preflight"
TEMP_KEYS = ("level", "low_c", "high_c", "warn_c", "refuse_c", "detail")
#: Longest ``detail`` text in telemetry and in a log event.
MAX_DETAIL_CHARS = 120

#: The structured log event of every check.
EVENT_TEMP_PREFLIGHT = "edge_temp_preflight"

# ``53~56C``, ``53~56``, ``56C``, ``56``, with optional spaces and a degree sign. Plain ASCII
# digits only, so a reply such as ``٥٦`` is not read as a number.
_REPLY_RE = re.compile(r"(-?[0-9]{1,3})(?:\s*~\s*(-?[0-9]{1,3}))?\s*(?:°\s*)?[cC]?")


@dataclass(frozen=True)
class TempReading:
    """One ``temp?`` reply: the board's lowest and highest temperature in degrees Celsius."""

    low_c: int
    high_c: int


def parse_temp_reply(reply: Any) -> TempReading | None:
    """The temperature in a ``temp?`` reply, or None when the reply is not one.

    ``53~56C`` is 53 to 56; ``56C`` and ``56`` are 56 to 56. The two values are put in order. A
    reply that is not text, that has anything else in it (``ok``, ``error``, ``unknown command``),
    or whose values are outside ``TEMP_READING_RANGE_C`` is None.
    """
    if isinstance(reply, (bytes, bytearray)):
        try:
            reply = bytes(reply).decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(reply, str):
        return None
    match = _REPLY_RE.fullmatch(reply.strip().strip("\x00").strip())
    if match is None:
        return None
    first = int(match.group(1))
    second = int(match.group(2)) if match.group(2) is not None else first
    lo, hi = TEMP_READING_RANGE_C
    if not (lo <= first <= hi and lo <= second <= hi):
        return None
    return TempReading(min(first, second), max(first, second))


@dataclass(frozen=True)
class TempLimits:
    """The warn level and the refusal level (None: refusal off), in degrees Celsius."""

    warn_c: int = DEFAULT_TEMP_WARN_C
    refuse_c: int | None = DEFAULT_TEMP_REFUSE_C

    def __post_init__(self) -> None:
        lo, hi = TEMP_LIMIT_RANGE_C
        if not _whole(self.warn_c) or not lo <= self.warn_c <= hi:
            raise ValueError(f"the warn level must be a whole number of degrees from {lo} to {hi}, got {self.warn_c!r}")
        if self.refuse_c is not None:
            if not _whole(self.refuse_c) or not lo <= self.refuse_c <= hi:
                raise ValueError(
                    f"the refusal level must be a whole number of degrees from {lo} to {hi}, or off, got {self.refuse_c!r}"
                )
            if self.refuse_c < self.warn_c:
                raise ValueError(
                    f"the refusal level ({self.refuse_c} C) is below the warn level ({self.warn_c} C); "
                    "set the warn level at or below the refusal level"
                )

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> TempLimits:
        """The levels from ``FLIGHTRISK_TELLO_TEMP_WARN_C`` and ``FLIGHTRISK_TELLO_TEMP_REFUSE_C``.

        Unset or empty means the default. The warn level is a whole number of degrees; the refusal
        level is one too, or ``off``. Raises ``ValueError`` with an operator-facing message for
        anything else, and when the refusal level is below the warn level.
        """
        warn = _env_degrees(environ, ENV_TELLO_TEMP_WARN_C, DEFAULT_TEMP_WARN_C, allow_off=False)
        refuse = _env_degrees(environ, ENV_TELLO_TEMP_REFUSE_C, DEFAULT_TEMP_REFUSE_C, allow_off=True)
        try:
            return cls(warn_c=warn, refuse_c=refuse)
        except ValueError as exc:
            raise ValueError(f"{ENV_TELLO_TEMP_WARN_C}/{ENV_TELLO_TEMP_REFUSE_C}: {exc}") from None


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _env_degrees(environ: Mapping[str, str], name: str, default: int, *, allow_off: bool) -> int | None:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    if allow_off and raw.lower() == REFUSE_OFF:
        return None
    lo, hi = TEMP_LIMIT_RANGE_C
    if not re.fullmatch(r"[0-9]{1,3}", raw) or not lo <= int(raw) <= hi:
        also = f", or {REFUSE_OFF}" if allow_off else ""
        raise ValueError(f"{name}={raw!r} must be a whole number of degrees Celsius from {lo} to {hi}{also}")
    return int(raw)


@dataclass(frozen=True)
class TempCheck:
    """The outcome of one temperature check before a takeoff."""

    level: str
    limits: TempLimits
    reading: TempReading | None = None
    detail: str | None = None

    @property
    def refused(self) -> bool:
        return self.level == LEVEL_REFUSE

    def to_telemetry(self) -> dict[str, Any]:
        """The ``temp_preflight`` telemetry object: every key of ``TEMP_KEYS``, None where not known."""
        return {
            "level": self.level,
            "low_c": self.reading.low_c if self.reading else None,
            "high_c": self.reading.high_c if self.reading else None,
            "warn_c": self.limits.warn_c,
            "refuse_c": self.limits.refuse_c,
            "detail": self.detail,
        }

    def log_fields(self) -> dict[str, Any]:
        """The fields of the ``edge_temp_preflight`` log event."""
        fields = self.to_telemetry()
        fields["temp_level"] = fields.pop("level")
        return fields

    def operator_text(self, drone_id: str | None = None) -> str:
        """One plain sentence (or two) for the operator about this check."""
        name = drone_id or "the drone"
        limits = self.limits
        if self.reading is not None:
            reading = self.reading
            temp = f"{reading.high_c} C" if reading.low_c == reading.high_c else f"{reading.low_c} to {reading.high_c} C"
        else:
            temp = "unknown"
        if self.level == LEVEL_REFUSE:
            return (
                f"Takeoff refused: {name} reports {temp}, at or above the refusal level of {limits.refuse_c} C "
                f"({ENV_TELLO_TEMP_REFUSE_C}). Let it cool with the battery out or on a fan, then start again."
            )
        if self.level == LEVEL_WARN:
            return (
                f"{name} is hot: {temp}, at or above the warn level of {limits.warn_c} C. A Tello that sits "
                "powered on keeps heating and can switch itself off; take off soon or let it cool."
            )
        if self.level == LEVEL_UNKNOWN:
            return f"{name}: the temperature could not be read before takeoff ({self.detail or 'no reading'}); taking off anyway."
        return f"{name} temperature {temp} before takeoff."


def judge_temperature(reply: Any, limits: TempLimits, *, failure: str | None = None) -> TempCheck:
    """Judge one ``temp?`` reply against *limits*.

    *failure* is why there is no reply (a timeout, a send that raised); a reply that does not parse
    is ``unknown`` with the reply in the detail. Neither ever refuses.
    """
    if failure is not None:
        return TempCheck(LEVEL_UNKNOWN, limits, detail=_clip(failure))
    if reply is None:
        return TempCheck(LEVEL_UNKNOWN, limits, detail="no reply to temp?")
    reading = parse_temp_reply(reply)
    if reading is None:
        shown = repr(reply) if isinstance(reply, (str, bytes, bytearray)) else type(reply).__name__
        return TempCheck(LEVEL_UNKNOWN, limits, detail=_clip(f"not a temperature: {shown}"))
    if limits.refuse_c is not None and reading.high_c >= limits.refuse_c:
        return TempCheck(LEVEL_REFUSE, limits, reading)
    if reading.high_c >= limits.warn_c:
        return TempCheck(LEVEL_WARN, limits, reading)
    return TempCheck(LEVEL_OK, limits, reading)


def _clip(text: str) -> str:
    clean = "".join(ch if ch.isprintable() else "?" for ch in str(text))
    return clean[:MAX_DETAIL_CHARS]


def temp_status(value: Any) -> dict[str, Any] | None:
    """An edge's ``temp_preflight`` telemetry as the ground shows it, or None when it is not that shape.

    Keeps only ``TEMP_KEYS``: ``level`` one of ``LEVELS``, the temperatures and levels finite numbers
    or None, ``detail`` text of at most ``MAX_DETAIL_CHARS`` characters or None.
    """
    if not isinstance(value, Mapping) or value.get("level") not in LEVELS:
        return None
    out: dict[str, Any] = {"level": value["level"]}
    for key in ("low_c", "high_c", "warn_c", "refuse_c"):
        number = value.get(key)
        ok = isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number)
        out[key] = number if ok else None
    detail = value.get("detail")
    out["detail"] = _clip(detail) if isinstance(detail, str) and detail.strip() else None
    return out
