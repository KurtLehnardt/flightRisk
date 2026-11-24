"""The Tello temperature check before a takeoff: read the state packet's temperature and judge it.

A Tello sends a state packet about ten times a second. Its ``templ`` and ``temph`` fields are the lowest
and highest temperature in degrees Celsius; the Tello SDK 1.3, 2.0 and 3.0 all document them. Before
every takeoff the lane executor takes them from the newest state packet of a drone that has
``state_temperature()`` (``TelloController``). Nothing is sent on the command channel for the check:
Tello replies carry no command id, so a query sent before a takeoff could have its reply read as the
takeoff's answer, or the takeoff's ``ok`` read as the temperature. Only a fresh packet counts (at most
``tello._STATE_FRESH_S`` old). The highest value is judged, because it is the one closest to the
overheat point:

* at or above the refusal level (``FLIGHTRISK_TELLO_TEMP_REFUSE_C``, default 95) the takeoff is refused
  with an operator message, unless the refusal is switched off (``off``);
* at or above the warn level (``FLIGHTRISK_TELLO_TEMP_WARN_C``, default 92) the reading is logged as a
  warning and shown on the drone card;
* a reading that is missing (no state packet, a stale one, one without both fields) or out of range is
  ``unknown``. It is logged as a warning and never refuses a takeoff.

Why these defaults (sources in ``docs/fleet-setup.md``, "Takeoff temperature check"): no Ryze or DJI
document publishes a shutdown or no-takeoff temperature. One logged report saw a Tello that sat powered
on reach 95 degrees and switch itself off, and another reports that a Tello EDU stops taking off above
about 100 degrees and shuts down above about 105. The refusal level is the lowest of those reported
shutdown points. Our drone-1 idles at 91 to 93 degrees and still flies, so a warn level of 90 would fire
on every start and be ignored; 92 warns only near the top of that band, 3 degrees below the refusal.

Nothing here talks to a drone; the reading itself is ``TelloController.state_temperature``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

from flightrisk.params import table as _registry
from flightrisk.params.defaults import model_default, model_range



#: Environment variables read at the edge's start (``edge_device``) and checked by the launcher.
ENV_TELLO_TEMP_WARN_C = "FLIGHTRISK_TELLO_TEMP_WARN_C"
ENV_TELLO_TEMP_REFUSE_C = "FLIGHTRISK_TELLO_TEMP_REFUSE_C"
#: The value of ``FLIGHTRISK_TELLO_TEMP_REFUSE_C`` that switches the refusal off (warn only).
REFUSE_OFF = "off"

#: The warn level: near the top of the 91 to 93 degrees our drone-1 reads at rest, 3 below the refusal.
DEFAULT_TEMP_WARN_C = model_default("tello.temp_warn_c", _registry.T)
#: The refusal level: the lowest overheat shutdown temperature reported for a Tello (95).
DEFAULT_TEMP_REFUSE_C = model_default("tello.temp_refuse_c", _registry.T)
#: Allowed warn and refusal levels, in degrees Celsius. Below 40 every Tello that has been on for
#: a minute would warn; above 120 is past every reported shutdown point.
TEMP_LIMIT_RANGE_C = (40, 120)
#: A reading outside this range is not a temperature this board reports, so it is ``unknown``.
TEMP_READING_RANGE_C = model_range("tello_sdk.temp_reading_range_c", _registry.T)

#: Levels of a check.
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_REFUSE = "refuse"
LEVEL_UNKNOWN = "unknown"
LEVELS = (LEVEL_OK, LEVEL_WARN, LEVEL_REFUSE, LEVEL_UNKNOWN)

#: The telemetry field (edge to ground) that carries the temperature, and its keys. ``level``,
#: ``low_c``, ``high_c`` and ``detail`` are the check before the last takeoff (``level`` is None
#: before the first); ``live_low_c`` and ``live_high_c`` are the newest fresh state packet's
#: reading, None while there is none.
TELEMETRY_TEMP = "temp_preflight"
TEMP_KEYS = ("level", "low_c", "high_c", "warn_c", "refuse_c", "detail", "live_low_c", "live_high_c")
#: Longest ``detail`` text in telemetry and in a log event.
MAX_DETAIL_CHARS = 120

#: The structured log event of every check.
EVENT_TEMP_PREFLIGHT = "edge_temp_preflight"

# ``53~56C``, ``53~56``, ``56C``, ``56``, with optional spaces and a degree sign (``°C`` or ``℃``).
# Plain ASCII digits only, so a reply such as ``٥٦`` is not read as a number.
_REPLY_RE = re.compile(r"(-?[0-9]{1,3})(?:\s*~\s*(-?[0-9]{1,3}))?\s*(?:℃|°\s*[cC]?|[cC])?")


@dataclass(frozen=True)
class TempReading:
    """The board's lowest and highest temperature in degrees Celsius."""

    low_c: int
    high_c: int


class TempSample(NamedTuple):
    """A temperature taken from the drone: the reading, or None and why there is none."""

    reading: TempReading | None
    detail: str | None = None


def reading_from(low: Any, high: Any) -> TempReading | None:
    """The reading of two values (``templ`` and ``temph``) put in order, or None when either is not a temperature.

    Each must be a whole number (not a bool) inside ``TEMP_READING_RANGE_C``.
    """
    lo, hi = TEMP_READING_RANGE_C
    for value in (low, high):
        if not _whole(value) or not lo <= value <= hi:
            return None
    return TempReading(min(low, high), max(low, high))


def parse_temp_reply(reply: Any) -> TempReading | None:
    """The temperature in a ``temp?`` reply, or None when the reply is not one.

    The launcher's drone probe asks ``temp?`` before any edge starts; the edge never does (see the
    module docstring). ``53~56C`` is 53 to 56; ``56C``, ``56℃`` and ``56`` are 56 to 56. The two
    values are put in order. A reply that is not text, that has anything else in it (``ok``,
    ``error``, ``unknown command``), or whose values are outside ``TEMP_READING_RANGE_C`` is None.
    """
    if isinstance(reply, (bytes, bytearray)):
        try:
            reply = bytes(reply).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(reply, str):
        return None
    match = _REPLY_RE.fullmatch(reply.strip().strip("\x00").strip())
    if match is None:
        return None
    first = int(match.group(1))
    second = int(match.group(2)) if match.group(2) is not None else first
    return reading_from(first, second)


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

    def level_of(self, high_c: int) -> str:
        """The level of a reading whose highest value is *high_c*: refuse, warn or ok."""
        if self.refuse_c is not None and high_c >= self.refuse_c:
            return LEVEL_REFUSE
        if high_c >= self.warn_c:
            return LEVEL_WARN
        return LEVEL_OK


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


def _range_text(reading: TempReading) -> str:
    if reading.low_c == reading.high_c:
        return f"{reading.high_c} C"
    return f"{reading.low_c} to {reading.high_c} C"


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

    def log_fields(self) -> dict[str, Any]:
        """The fields of the ``edge_temp_preflight`` log event."""
        return {
            "temp_level": self.level,
            "low_c": self.reading.low_c if self.reading else None,
            "high_c": self.reading.high_c if self.reading else None,
            "warn_c": self.limits.warn_c,
            "refuse_c": self.limits.refuse_c,
            "detail": self.detail,
        }

    def operator_text(self) -> str:
        """One plain sentence (or two) for the operator about this check.

        It never names the drone: the ground puts the drone id in front of a refusal
        (``Start refused: drone-1: ...``) and every log event carries ``drone_id``.
        """
        limits = self.limits
        temp = _range_text(self.reading) if self.reading is not None else "unknown"
        if self.level == LEVEL_REFUSE:
            return (
                f"Takeoff refused: {temp} is at or above the refusal level of {limits.refuse_c} C "
                f"({ENV_TELLO_TEMP_REFUSE_C}). Let it cool with the battery out or on a fan, then start again."
            )
        if self.level == LEVEL_WARN:
            return (
                f"Hot: {temp} is at or above the warn level of {limits.warn_c} C. A Tello that sits powered on "
                "keeps heating and can switch itself off; take off soon or let it cool."
            )
        if self.level == LEVEL_UNKNOWN:
            return f"The temperature could not be read before takeoff ({self.detail or 'no reading'}); taking off anyway."
        return f"Temperature {temp} before takeoff."


def judge_reading(reading: TempReading | None, limits: TempLimits, *, detail: str | None = None) -> TempCheck:
    """Judge one reading against *limits*.

    No reading is ``unknown`` with *detail* (why there is none), and never refuses.
    """
    if reading is None:
        return TempCheck(LEVEL_UNKNOWN, limits, detail=_clip(detail or "no reading"))
    return TempCheck(limits.level_of(reading.high_c), limits, reading)


def judge_temperature(reply: Any, limits: TempLimits, *, failure: str | None = None) -> TempCheck:
    """Judge one ``temp?`` reply (the launcher's drone probe) against *limits*.

    *failure* is why there is no reply; a reply that does not parse is ``unknown`` with the reply
    in the detail. Neither ever refuses.
    """
    if failure is not None:
        return judge_reading(None, limits, detail=failure)
    if reply is None:
        return judge_reading(None, limits, detail="no reply to temp?")
    reading = parse_temp_reply(reply)
    if reading is None:
        shown = repr(reply) if isinstance(reply, (str, bytes, bytearray)) else type(reply).__name__
        return judge_reading(None, limits, detail=f"not a temperature: {shown}")
    return judge_reading(reading, limits)


def temp_telemetry(check: TempCheck | None, limits: TempLimits, live: TempReading | None) -> dict[str, Any]:
    """The ``temp_preflight`` telemetry object: every key of ``TEMP_KEYS``, None where not known.

    *check* is the check before the last takeoff (None before the first); *live* is the newest
    fresh state packet's reading (None while there is none).
    """
    reading = check.reading if check is not None else None
    return {
        "level": check.level if check is not None else None,
        "low_c": reading.low_c if reading else None,
        "high_c": reading.high_c if reading else None,
        "warn_c": limits.warn_c,
        "refuse_c": limits.refuse_c,
        "detail": check.detail if check is not None else None,
        "live_low_c": live.low_c if live else None,
        "live_high_c": live.high_c if live else None,
    }


def _clip(text: str) -> str:
    clean = "".join(ch if ch.isprintable() else "?" for ch in str(text))
    return clean[:MAX_DETAIL_CHARS]


def temp_status(value: Any) -> dict[str, Any] | None:
    """An edge's ``temp_preflight`` telemetry as the ground shows it, or None when it is not that shape.

    Keeps only ``TEMP_KEYS``: ``level`` one of ``LEVELS`` or None (no check yet), the temperatures
    and levels finite numbers or None, ``detail`` text of at most ``MAX_DETAIL_CHARS`` characters or
    None.
    """
    if not isinstance(value, Mapping) or "level" not in value:
        return None
    level = value["level"]
    if level is not None and level not in LEVELS:
        return None
    out: dict[str, Any] = {"level": level}
    for key in ("low_c", "high_c", "warn_c", "refuse_c", "live_low_c", "live_high_c"):
        number = value.get(key)
        ok = isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number)
        out[key] = number if ok else None
    detail = value.get("detail")
    out["detail"] = _clip(detail) if isinstance(detail, str) and detail.strip() else None
    return {key: out[key] for key in TEMP_KEYS}


def live_refusal(status: Mapping[str, Any] | None) -> str | None:
    """The ground's start refusal for a drone whose live temperature is at or above its refusal level, or None.

    *status* is ``temp_status()`` of the drone's fresh telemetry. Only a live reading counts: the
    check before the last takeoff may be long out of date, and no live reading never refuses (the
    edge checks again right before its takeoff).
    """
    if not status:
        return None
    high, refuse = status.get("live_high_c"), status.get("refuse_c")
    if high is None or refuse is None or high < refuse:
        return None
    low = status.get("live_low_c")
    temp = f"{low:g} to {high:g} C" if low is not None and low != high else f"{high:g} C"
    return (
        f"too hot: {temp}, at or above the refusal level of {refuse:g} C ({ENV_TELLO_TEMP_REFUSE_C}); "
        "let it cool with the battery out or on a fan, then start again"
    )
