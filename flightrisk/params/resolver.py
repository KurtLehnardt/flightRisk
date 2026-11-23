"""Resolve the effective flight parameters of one unit from its layers.

``resolve`` is a pure function. Callers pass layers they have already read
(the parsed ``params.json`` layer, the inventory entry, the launcher window,
a copy of the environment and the parsed flags); this module never reads
``os.environ`` or a file. Precedence is RES-1, lowest to highest: profile,
file, inventory, launcher, environment, command-line flag. Every value carries
its ``source`` and ``source_detail`` (RES-2). Nothing at runtime calls this
module yet; adoption is A3, A4 and A5.

Bounds (design 7.1, risk R4): until C5 an environment or flag value is read
exactly as today's code reads it: the same parse, accepted spelling, unit
conversion, range and clamp or refuse behaviour. The parse comes from the
name's ``table.TODAY_READS`` entry and the range from ``today_accepts`` of the
golden file, with its ``policy``. A file, inventory or launcher value is
checked against the target bound of the model's profile column.

Decisions:

1. Today's ranges are passed in as ``today_accepts`` (see
   ``today_accepts_from_golden``) because they live in the golden file under
   ``tests/``, which runtime code must not read. An environment or flag value
   is checked against today's range only, never the target bound: a name with
   no recorded today range is open, because today's reader accepts any value
   its parse accepts (``FLIGHTRISK_LINK_LOSS_LAND_S`` above 0,
   ``FLIGHTRISK_FIELD_LINK_LOSS_HOLD_S`` and ``--link-loss-hold-s`` from 0,
   ``--speed-mps`` above 0 to 15 m/s). A3 to A5 add the target bounds to
   these names as a deliberate change recorded in design section 27. Only
   with ``today_accepts=None`` (no golden data), or for a name today's code
   does not read at all (``table.read_today`` is false, for example
   ``FLIGHTRISK_LAND_BATTERY`` and ``FLIGHTRISK_LINK_LOSS_WAIT_S``), is an
   environment or flag value checked against the target bound. On a custom
   or unknown model, which has no today behaviour to keep, an environment or
   flag value must also stay inside the model's own bound, so the effective
   range is today's range intersected with the model's bound. Built-in
   models are unaffected.
2. Where today's code clamps (``policy: clamp``, row 23
   ``FLIGHTRISK_MANUAL_ORPHAN_LAND_S``, "min 30, clamped"), the resolver
   clamps to the same end and logs it (risk R5). C5 turns this into a
   refusal (RES-9).
3. When a row has an alias read only at one scale (for example
   ``FLIGHTRISK_FIELD_CEILING_M`` at field scale), its canonical name is read
   only at the other scales, as ``edge_device`` keeps the indoor ``*_CM``
   variables from ever applying outdoors.
4. A flag ending in ``-m`` or ``-mps`` on a centimetre row is multiplied by
   100 (RES-6). The golden file records ``--speed-mps`` with scale 1; the flag
   is metres per second and the row is centimetres per second, so the
   resolver uses 100.
5. The flags today's edge refuses for a non-MAVLink drone
   (``FIELD_ONLY_FLAGS``) are refused at room scale.
6. Within one layer, when the canonical name and an alias both set a row,
   the canonical name wins and a log line names both.
7. A test-adapter fact set from a file, inventory or launcher layer is
   refused without naming it (FACT-2). Its environment override is honoured
   only on a test-adapter unit or a verified simulated unit and is otherwise
   ignored with one log line naming the variable (design 7.5, N-10). An
   honoured override outside today's range is refused with
   ``FACT_OVERRIDE_REFUSED``, which names neither the fact nor the variable;
   the details go to the log.
8. An environment or flag value for a row that is read-only for the model is
   ignored with a log line, and the profile value stands. A file, inventory
   or launcher value for it is refused.
9. A value for a row that does not apply to the model (no default in its
   profile column) is skipped, and the row is absent from the result.
10. An empty environment value counts as unset, as today's readers treat it.
    An empty flag value is refused where today's argparse type refuses it
    (``TodayRead.empty_refused``).
11. A scaled value keeps fractional centimetres (``1.234`` m is ``123.4``
    cm), as today's readers multiply floats. A name read with ``float()``
    today keeps a fraction on an integer row too; a name with no
    ``TODAY_READS`` entry on an integer row takes only whole numbers.

Safety (step S7):

12. ``less_safe`` compares a value with the built-in base column by
    ``safer``, never with a custom model's default (RES-8). An off value is
    less safe unless the base is off too, so the Tello's default sidestep of 0
    is not. ``non_default`` means "differs from the base default", so a custom
    default that differs is non-default.
13. A value needs confirmation when its row is ``confirm: less_safe`` and the
    value is less safe, or ``confirm: non_default`` and the value is
    non-default (``needs_confirmation``). Without a launcher layer, such a
    value won by ``params.json`` or the inventory is refused unless
    ``confirmations`` (the ``--confirm-less-safe id=value`` flags, see
    ``parse_confirm_flags``) names exactly that id and value. With a launcher
    layer, only the launcher's ``confirmed`` object counts; it is passed as
    the reserved ``confirmed`` key of that layer (RES-11). Less-safe values
    from the launcher, the environment or a flag are accepted, and those from
    the environment or a flag are logged at warning level.
14. A safety switch stored as off in ``params.json`` or the inventory is
    refused (RES-10). A switch that is a test-adapter fact is refused earlier
    by decision 7, without naming it. A unit's inventory ``features`` are
    passed as ``features``; ``forward_obstacle_sensing: false`` is refused.
15. After layering, the A1 constraints of ``constraints.py`` run against the
    resolved values (RES-5); K-3 is not among them until C6.
16. A custom or unknown model (``custom.py``) is passed as ``model``. Its
    narrowed bounds and defaults replace the profile column; its defaults
    carry source ``profile`` with the model's ``source_detail``.
17. ``resolve`` is the layering step: it refuses single values (bounds,
    types, facts, stored switches) and nothing across values. The
    confirmation check (decision 13) and the constraints (decision 15) are
    ``check_resolved``, and ``resolve_unit`` runs both steps. Runtime callers
    (A3 to A5) call ``resolve_unit``. The split keeps each single-value test of
    the layering step free of the cross-value rules, which today's readers do
    not apply when they read one name.

Today's quirks that A1 reproduces (behaviour-neutral; each is a candidate for
design section 27, not a fix here):

- ``--alt-slot`` reads a value above 0 and below 10 as metres (``5`` is
  500 cm, ``1.6`` is 160 cm), and rounds to 0.1 cm
  (``flightrisk/edge_device.py:1541-1557``).
- ``FLIGHTRISK_FAILSAFE_ACTION`` and ``--failsafe-action`` spell the return
  action ``rtl`` and refuse ``return``, the registry's spelling. The
  environment name is stripped and case-folded; the flag is matched exactly,
  so ``--failsafe-action RTL`` is refused (``flightrisk/edge_device.py:1608``
  and ``:1743-1746``).
- Several integer rows take a fraction from the environment or a flag, as
  today reads them with ``float()``: ``FLIGHTRISK_CEILING_CM=150.5``,
  ``FLIGHTRISK_MIN_TAKEOFF_BATTERY=30.5``,
  ``FLIGHTRISK_MANUAL_ORPHAN_LAND_S=45.5`` and ``--alt-slot 100.5``.
- The test-adapter integer facts and the obstacle counts take ASCII digits
  only, so ``+5`` and ``5.0`` are refused, while the decimal facts take a
  sign and an exponent (``flightrisk/edge_device.py:1846-1866``). The
  temperature levels take one to three digits
  (``flightrisk/drone/temp_preflight.py:184``).
- ``FLIGHTRISK_FLEET_AREA_CM`` and ``--fleet-area-cm`` read one number as a
  square and ``WxH`` in either case, whole and positive
  (``flightrisk/dashboard/__main__.py:39-50``).
- The link-loss detect time, the field hold and ``--speed-mps`` have no
  target bound at the environment or flag layer (decision 1).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Union

from flightrisk.hardware.catalogue import BUILTIN_MODELS, SCALES, DroneModel
from flightrisk.params.constraints import FILE_SOURCES, check_constraints, stored_feature_off, stored_switch_off
from flightrisk.params.messages import render_refused_input, render_unavailable_capability
from flightrisk.params.spec import Bound, Confirm, EffectiveValue, Edit, Kind, ParamSpec, Safer, Source, Unit
from flightrisk.params.table import (
    PARSE_AREA,
    PARSE_ASCII_FLOAT,
    PARSE_ASCII_INT,
    PARSE_CHOICE,
    TABLE,
    TODAY_READS,
    read_today,
    TodayRead,
)

log = logging.getLogger(__name__)

#: Layers a caller may pass, lowest to highest (RES-1). The profile layer is the registry column.
CALLER_LAYERS: tuple[Source, ...] = (Source.FILE, Source.INVENTORY, Source.LAUNCHER, Source.ENV, Source.CLI)
#: Full precedence, lowest to highest.
PRECEDENCE: tuple[Source, ...] = (Source.PROFILE,) + CALLER_LAYERS
#: Layers whose values are typed JSON values keyed by parameter id.
_ID_LAYERS = (Source.FILE, Source.INVENTORY, Source.LAUNCHER)
#: Layers whose values are text keyed by an environment name or a flag.
_NAME_LAYERS = (Source.ENV, Source.CLI)
#: Adapters whose units are test adapters (design 7.5).
TEST_ADAPTERS = frozenset({"tello_sdk_udp"})
#: Flags today's edge refuses unless the drone is MAVLink (``edge_device`` ``mavlink_only``).
FIELD_ONLY_FLAGS = frozenset({
    "--ceiling-m", "--ceiling-margin-m", "--geofence-m", "--failsafe-action", "--link-loss-hold-s",
})
#: Unit suffixes of a flag given in metres or metres per second.
_METRE_FLAG_SUFFIXES = ("-m", "-mps")
_METRE_SCALE = 100.0
#: Unit suffixes of an environment name given in metres (``FLIGHTRISK_FIELD_ALT_SLOT_M``).
_METRE_ENV_SUFFIXES = ("_M",)
_CENTIMETRE_UNITS = (Unit.CM, Unit.CM_PER_S, Unit.CM_LIST)
_TRUE_TEXT = ("1", "true", "yes", "on")
_FALSE_TEXT = ("0", "false", "no", "off")

#: Operator text for a refused test-adapter override; it names neither the value nor the variable.
FACT_OVERRIDE_REFUSED = (
    "A test-adapter developer override is outside the range the adapter accepts. Refusing to start."
)
#: Operator text for a stored value that is not a setting; it does not name the value.
FACT_NOT_SETTABLE = "A value in {detail} is not a setting and cannot be stored. Refusing to start."
#: The reserved key of the launcher layer that holds the values the operator confirmed at Start.
LAUNCHER_CONFIRMED = "confirmed"
#: Operator text for a less-safe value from a file that nobody confirmed (RES-11).
UNCONFIRMED_LESS_SAFE = (
    "`{param_id}` {value} {unit} from {detail} is less safe than the {model} default of {base} {unit}. "
    "{how} Refusing to start."
)
_CONFIRM_BY_FLAG = "Start with --confirm-less-safe {param_id}={value} to use it."
_CONFIRM_IN_LAUNCHER = "Confirm it in the launcher at Start to use it."


class ParamRefused(ValueError):
    """An input the resolver refuses; ``message`` is the operator text."""

    def __init__(self, message: str, *, param_id: Optional[str] = None, source: Optional[Source] = None):
        super().__init__(message)
        self.message = message
        self.param_id = param_id
        self.source = source


@dataclass(frozen=True)
class LayerValue:
    """A layer value with where in the layer it came from (for example ``params.json units.drone-1``)."""

    value: Any
    detail: str


@dataclass(frozen=True)
class TodayRange:
    """The range today's code accepts for one name and profile, in the row's unit; ``None`` is open."""

    lo: Optional[float]
    hi: Optional[float]
    policy: str = "refuse"
    #: True when today refuses the end itself (``geofence_cm <= 0`` is refused, so 0 is an open end).
    lo_open: bool = False
    hi_open: bool = False


#: ``{name: {profile: TodayRange or None}}``; a ``None`` range means today's range is not known.
TodayAccepts = Mapping[str, Mapping[str, Optional[TodayRange]]]


def today_accepts_from_golden(golden: Mapping[str, Any]) -> dict[str, dict[str, Optional[TodayRange]]]:
    """Today's accepted ranges per environment name and flag, from the golden file's data."""
    out: dict[str, dict[str, Optional[TodayRange]]] = {}
    for row in golden["rows"]:
        for name, alias in row["env_aliases"].items():
            per: dict[str, Optional[TodayRange]] = {}
            for profile, accepts in (alias.get("today_accepts") or {}).items():
                per[profile] = None if accepts is None else TodayRange(
                    accepts.get("lo"), accepts.get("hi"), accepts.get("policy") or "refuse",
                    bool(accepts.get("lo_open")), bool(accepts.get("hi_open")),
                )
            out[name] = per
    return out


@dataclass(frozen=True)
class _Name:
    """One environment name or flag of a row."""

    spec: ParamSpec
    name: str
    scale: float
    scales: frozenset[str]
    canonical: bool
    #: How today's code reads this name (``table.TODAY_READS``), or None to read it as the row's kind.
    read: Optional[TodayRead] = None


def _flag_scale(spec: ParamSpec, flag: str) -> float:
    if spec.unit in _CENTIMETRE_UNITS and flag.endswith(_METRE_FLAG_SUFFIXES):
        return _METRE_SCALE
    return 1.0


def _build_names() -> tuple[dict[str, _Name], dict[str, _Name]]:
    """Index every environment name and flag of the registry."""
    env: dict[str, _Name] = {}
    cli: dict[str, _Name] = {}
    all_scales = frozenset(SCALES)
    for spec in TABLE:
        alias_scales = {a.applies_to_scale for a in spec.env_aliases if a.applies_to_scale}
        if spec.env:
            env_scale = _METRE_SCALE if spec.unit in _CENTIMETRE_UNITS and spec.env.endswith(_METRE_ENV_SUFFIXES) else 1.0
            env[spec.env] = _Name(spec, spec.env, env_scale, all_scales - alias_scales, True,
                                  TODAY_READS.get(spec.env))
        for alias in spec.env_aliases:
            scales = frozenset({alias.applies_to_scale}) if alias.applies_to_scale else all_scales
            env[alias.name] = _Name(spec, alias.name, alias.scale_factor, scales, False, TODAY_READS.get(alias.name))
        for flag in spec.cli:
            scales = frozenset({"field"}) if flag in FIELD_ONLY_FLAGS else all_scales
            cli[flag] = _Name(spec, flag, _flag_scale(spec, flag), scales, True, TODAY_READS.get(flag))
    return env, cli


_ENV_NAMES, _CLI_NAMES = _build_names()
_BY_ID: dict[str, ParamSpec] = {spec.id: spec for spec in TABLE}


def spec_for(param_id: str) -> Optional[ParamSpec]:
    """The registry row of ``param_id``, or None for an unknown id."""
    return _BY_ID.get(param_id)


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    """True for a finite int or float that converts to a float; an oversized int is not a number."""
    if not _is_numeric(value):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _finite(value: Any, raw: Any) -> Any:
    """Return ``value`` if every number in it is finite; raises ``_WrongType`` otherwise.

    This is the one finiteness check every layer passes after the last conversion step (parse, unit
    scale and rounding), so a metre value that overflows to infinity once scaled is refused.
    """
    items = value if isinstance(value, tuple) else (value,)
    if any(_is_numeric(item) and not _is_number(item) for item in items):
        raise _WrongType(raw)
    return value


def _whole(value: float) -> Union[int, float]:
    return int(value) if float(value).is_integer() else value


class _WrongType(Exception):
    """A value of the wrong type for its row."""


def _off_match(spec: ParamSpec, raw: Any) -> bool:
    """True when ``raw`` is the row's off value, given as text or as a typed value."""
    off = spec.off_value
    if off is None:
        return False
    if isinstance(raw, str) and not isinstance(off, str):
        text = raw.strip().lower()
        if isinstance(off, bool):
            return (text in _TRUE_TEXT) if off else (text in _FALSE_TEXT)
        return text == str(off).lower()
    if isinstance(off, bool) or isinstance(raw, bool):
        return raw is off
    if isinstance(off, str):
        return isinstance(raw, str) and raw.strip().lower() == off.lower()
    return _is_number(raw) and raw == off


def _from_text(spec: ParamSpec, text: str, scale: float) -> Any:
    """Parse an environment or flag value for ``spec``; raises ``_WrongType``."""
    text = text.strip()
    try:
        if spec.kind is Kind.BOOL:
            if text.lower() in _TRUE_TEXT:
                return True
            if text.lower() in _FALSE_TEXT:
                return False
            raise _WrongType(text)
        if spec.kind is Kind.ENUM:
            if text.lower() in spec.choices:
                return text.lower()
            raise _WrongType(text)
        if spec.kind in (Kind.INT_LIST, Kind.INT_PAIR):
            parts = [p for p in text.replace("x", ",").split(",") if p.strip()]
            return _typed_list(spec, [_scaled(float(p), scale, Kind.INT) for p in parts])
        number = float(text)
    except ValueError as exc:
        raise _WrongType(text) from exc
    if not math.isfinite(number):
        raise _WrongType(text)
    if spec.kind is Kind.INT and scale == 1.0 and not number.is_integer():
        raise _WrongType(text)
    return _scaled(number, scale, spec.kind)


#: Today's ASCII number patterns (``flightrisk/edge_device.py:1846-1847``).
_ASCII_INT_RE = re.compile(r"[0-9]+")
_ASCII_FLOAT_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _from_today_text(spec: ParamSpec, raw: str, read: TodayRead, scale: float) -> Any:
    """Parse an environment or flag value exactly as today's reader does (``read``); raises ``_WrongType``."""
    text = raw.strip()
    if read.parse == PARSE_CHOICE:
        key = text.lower() if read.fold else raw
        if key not in read.values:
            raise _WrongType(raw)
        return read.values[key]
    if read.parse == PARSE_AREA:
        try:
            values = [int(part) for part in text.lower().replace(" ", "").split("x")]
        except ValueError as exc:
            raise _WrongType(raw) from exc
        if len(values) == 1:
            values = values * 2
        if len(values) != 2 or min(values) <= 0:
            raise _WrongType(raw)
        return _typed_list(spec, [_scaled(v, scale, Kind.INT) for v in values])
    number: Union[int, float]
    if read.parse == PARSE_ASCII_INT:
        if not _ASCII_INT_RE.fullmatch(text) or (read.max_digits is not None and len(text) > read.max_digits):
            raise _WrongType(raw)
        number = int(text)
    elif read.parse == PARSE_ASCII_FLOAT:
        if not _ASCII_FLOAT_RE.fullmatch(text):
            raise _WrongType(raw)
        number = float(text)
    else:
        try:
            number = float(text)
        except ValueError as exc:
            raise _WrongType(raw) from exc
    if not math.isfinite(number):
        raise _WrongType(raw)
    if read.metres_below is not None and 0 < number < read.metres_below:
        number = number * _METRE_SCALE
    value = number * scale
    if read.round_to is not None:
        value = round(value, read.round_to)
    return float(value) if spec.kind is Kind.FLOAT else _whole(value)


def _scaled(number: float, scale: float, kind: Kind) -> Union[int, float]:
    value = number * scale
    if kind is Kind.FLOAT:
        return float(value)
    return _whole(value)


def _typed_list(spec: ParamSpec, items: list) -> tuple:
    if not items or not all(_is_number(i) for i in items):
        raise _WrongType(items)
    if spec.kind is Kind.INT_PAIR and len(items) != 2:
        raise _WrongType(items)
    return tuple(items)


def coerce_typed(spec: ParamSpec, raw: Any) -> Any:
    """Check a typed (JSON) value against the row's kind; raises ``_WrongType``.

    Used for file, inventory and launcher values, which are never scaled.
    """
    kind = spec.kind
    if kind is Kind.BOOL and isinstance(raw, bool):
        return raw
    if kind is Kind.INT and _is_number(raw) and isinstance(raw, int):
        return raw
    if kind is Kind.FLOAT and _is_number(raw):
        return float(raw)
    if kind is Kind.ENUM and isinstance(raw, str) and raw in spec.choices:
        return raw
    if kind in (Kind.INT_LIST, Kind.INT_PAIR) and isinstance(raw, (list, tuple)):
        if all(isinstance(i, int) and not isinstance(i, bool) for i in raw):
            return _typed_list(spec, list(raw))
    raise _WrongType(raw)


def is_wrong_type(spec: ParamSpec, raw: Any) -> bool:
    """True when a typed value is neither the row's off value nor of the row's kind."""
    if _off_match(spec, raw):
        return False
    try:
        coerce_typed(spec, raw)
    except _WrongType:
        return True
    return False


def _model_of(model: Union[str, DroneModel]) -> DroneModel:
    if isinstance(model, DroneModel):
        return model
    try:
        return BUILTIN_MODELS[model]
    except KeyError:
        raise ValueError(f"unknown drone model {model!r}") from None


def _refusal(spec: ParamSpec, model: DroneModel, unit_id: str, value: Any, detail: str,
             bound: Optional[tuple[Optional[float], Optional[float]]], source: Source) -> ParamRefused:
    """The operator refusal for one value, from ``messages.py``; a fact is never named."""
    profile = model.profile
    if spec.is_fact(profile):
        log.warning("refused test-adapter override %s=%r for %s on %s", detail, value, spec.id, unit_id)
        return ParamRefused(FACT_OVERRIDE_REFUSED, param_id=spec.id, source=source)
    lo, hi = bound if bound else (None, None)
    target = spec.bounds.get(profile)
    if target is not None:
        # An open end of today's range is shown with the target bound's end.
        lo = target.lo if lo is None else lo
        hi = target.hi if hi is None else hi
    if lo is not None or hi is not None:
        text = render_refused_input(
            spec.id, value, spec.unit.value, detail, model.name,
            -math.inf if lo is None else lo, math.inf if hi is None else hi, spec.edit.get(profile, Edit.FILE_ENV),
        )
    else:
        reason = f"takes only {', '.join(spec.choices)}" if spec.choices else "does not take that value"
        text = render_unavailable_capability(unit_id, f"`{spec.id}` {value} from {detail}", model.name, reason)
    return ParamRefused(text, param_id=spec.id, source=source)


def _outside(value: Any, lo: Optional[float], hi: Optional[float]) -> Optional[str]:
    """``"lo"`` or ``"hi"`` when a number (or any item of a pair or list) passes that end."""
    return _outside_range(value, TodayRange(lo, hi))


def _outside_range(value: Any, rng: TodayRange) -> Optional[str]:
    """``"lo"`` or ``"hi"`` when a number (or any item of a pair or list) passes that end of ``rng``."""
    items = value if isinstance(value, tuple) else (value,)
    for item in items:
        if _is_numeric(item) and not _is_number(item):
            # A non-finite or oversized number is never inside a range.
            return "lo" if item < 0 else "hi"
        if not _is_number(item):
            continue
        if rng.lo is not None and (item < rng.lo or (rng.lo_open and item == rng.lo)):
            return "lo"
        if rng.hi is not None and (item > rng.hi or (rng.hi_open and item == rng.hi)):
            return "hi"
    return None


_OPEN = TodayRange(None, None)


def _today_range(today_accepts: Optional[TodayAccepts], name: str, profile: str) -> Optional[TodayRange]:
    if today_accepts is None:
        return None
    return (today_accepts.get(name) or {}).get(profile)


def _check_bound(spec: ParamSpec, model: DroneModel, unit_id: str, value: Any, detail: str, source: Source,
                 name: Optional[str], today_accepts: Optional[TodayAccepts], model_bound: bool = False) -> Any:
    """Return the value to use after the bound check, clamped where today's code clamps; raise if refused.

    ``model_bound`` is true for a custom or unknown model: an environment or flag value must then also
    stay inside the model's own bound, so the effective range is today's range intersected with it.
    """
    profile = model.profile
    target: Optional[Bound] = spec.bounds.get(profile)
    if today_accepts is not None and name and source in _NAME_LAYERS and read_today(name):
        # Decision 1: an environment or flag value of a name today's code reads is checked against today's
        # range only. Such a name with no recorded range is open, as today's reader accepts any value its
        # parse accepts. A name today's code does not read falls through to the target bound.
        today = _today_range(today_accepts, name, profile) or _OPEN
        end = _outside_range(value, today)
        if end is not None:
            if today.policy != "clamp" or isinstance(value, tuple):
                raise _refusal(spec, model, unit_id, value, detail, (today.lo, today.hi), source)
            clamped = today.lo if end == "lo" else today.hi
            clamped = _whole(clamped) if spec.kind is Kind.INT else float(clamped)
            log.warning("%s=%r is outside %s to %s; clamped to %r as today", name, value, today.lo, today.hi, clamped)
            value = clamped
        if model_bound and target is not None and _outside(value, target.lo, target.hi) is not None:
            lo = target.lo if today.lo is None else max(today.lo, target.lo)
            hi = target.hi if today.hi is None else min(today.hi, target.hi)
            raise _refusal(spec, model, unit_id, value, detail, (lo, hi), source)
        return value
    if target is not None and _outside(value, target.lo, target.hi) is not None:
        raise _refusal(spec, model, unit_id, value, detail, (target.lo, target.hi), source)
    return value


def _entries(source: Source, layer: Mapping[str, Any], scale: str, model: DroneModel,
             unit_id: str) -> dict[str, tuple[ParamSpec, Any, str, Optional[_Name]]]:
    """The values one layer sets, per parameter id: ``(spec, raw, detail, name)``."""
    out: dict[str, tuple[ParamSpec, Any, str, Optional[_Name]]] = {}
    if source in _ID_LAYERS:
        for param_id, raw in layer.items():
            spec = _BY_ID.get(param_id)
            if spec is None:
                detail = raw.detail if isinstance(raw, LayerValue) else source.value
                raise ParamRefused(f"`{param_id}` from {detail} is not a known setting. Refusing to start.",
                                   param_id=param_id, source=source)
            if isinstance(raw, LayerValue):
                out[param_id] = (spec, raw.value, raw.detail, None)
            else:
                out[param_id] = (spec, raw, source.value, None)
        return out
    names = _ENV_NAMES if source is Source.ENV else _CLI_NAMES
    for key, raw in layer.items():
        name = names.get(key)
        if name is None:
            continue
        if isinstance(raw, LayerValue):
            raw = raw.value
        empty_refused = name.read is not None and name.read.empty_refused
        if raw is None or (isinstance(raw, str) and not raw.strip() and not empty_refused):
            continue
        if scale not in name.scales:
            if source is Source.CLI and name.spec.defaults.get(model.profile) is not None:
                raise ParamRefused(
                    render_unavailable_capability(unit_id, f"the {key} setting", model.name,
                                                  f"flies at {scale} scale, where it does not apply"),
                    param_id=name.spec.id, source=source,
                )
            continue
        held = out.get(name.spec.id)
        if held is not None:
            kept, dropped = (held[3], name) if held[3].canonical else (name, held[3])
            log.warning("%s and %s both set %s; %s is used", kept.name, dropped.name, name.spec.id, kept.name)
            if kept is held[3]:
                continue
        out[name.spec.id] = (name.spec, raw, key, name)
    return out


def _value_of(spec: ParamSpec, raw: Any, source: Source, name: Optional[_Name]) -> tuple[Any, bool]:
    """The typed value and whether it is the row's off value; raises ``_WrongType``.

    Every layer's value passes ``_finite`` after its last conversion step.
    """
    if _off_match(spec, raw):
        return spec.off_value, True
    value, is_off = _converted(spec, raw, source, name)
    return _finite(value, raw), is_off


def _converted(spec: ParamSpec, raw: Any, source: Source, name: Optional[_Name]) -> tuple[Any, bool]:
    if source in _NAME_LAYERS:
        text = raw if isinstance(raw, str) else str(raw)
        if isinstance(raw, bool) or (raw is not None and not isinstance(raw, (str, int, float))):
            raise _WrongType(raw)
        if name is not None and name.read is not None:
            return _from_today_text(spec, text, name.read, name.scale), False
        return _from_text(spec, text, name.scale if name else 1.0), False
    return coerce_typed(spec, raw), False


_NUMERIC_KINDS = (Kind.INT, Kind.FLOAT, Kind.INT_LIST, Kind.INT_PAIR)


def _new_name_without_bound(spec: ParamSpec, profile: str, name: str) -> bool:
    """True for a number from a name today's code does not read, on a row with no target bound.

    Such a value has neither today's range nor a target bound to check it against, so it is not
    honoured until the PR that brings the bound (``FLIGHTRISK_LINK_LOSS_WAIT_S`` on the Tello, C7).
    """
    return not read_today(name) and spec.kind in _NUMERIC_KINDS and spec.bounds.get(profile) is None


def _is_test_unit(model: DroneModel, simulated: bool) -> bool:
    return model.adapter in TEST_ADAPTERS or bool(simulated)


def is_less_safe(spec: ParamSpec, value: Any, base: Any) -> bool:
    """True when ``value`` is less safe than the built-in base default ``base`` (RES-8)."""
    if _off_match(spec, value):
        return not _off_match(spec, base)
    if not (_is_number(value) and _is_number(base)):
        return False
    if spec.safer is Safer.HIGHER:
        return value < base
    if spec.safer is Safer.LOWER:
        return value > base
    return False


def needs_confirmation(spec: ParamSpec, value: EffectiveValue) -> bool:
    """True when the row's ``confirm`` column asks the operator to confirm this value."""
    if spec.confirm is Confirm.LESS_SAFE:
        return value.less_safe
    if spec.confirm is Confirm.NON_DEFAULT:
        return value.non_default
    return False


def parse_confirm_flags(items: Iterable[str]) -> dict[str, str]:
    """``{id: value}`` from repeated ``--confirm-less-safe id=value`` flag values."""
    out: dict[str, str] = {}
    for item in items:
        param_id, sep, value = item.partition("=")
        if not sep or not param_id.strip() or not value.strip():
            raise ParamRefused(f"--confirm-less-safe {item} is not of the form id=value. Refusing to start.",
                               source=Source.CLI)
        out[param_id.strip()] = value.strip()
    return out


def _confirmed(spec: ParamSpec, raw: Any, value: Any) -> bool:
    """True when a confirmation ``raw`` names exactly ``value`` for the row."""
    if raw is None:
        return False
    if _off_match(spec, raw):
        return _off_match(spec, value)
    try:
        given = _from_text(spec, raw, 1.0) if isinstance(raw, str) else coerce_typed(spec, raw)
    except _WrongType:
        return False
    return isinstance(given, bool) is isinstance(value, bool) and given == value


def _model_and_narrowing(model: Any) -> tuple[DroneModel, Any]:
    """The descriptor of ``model`` and its narrowing (a custom or unknown model), or None."""
    if isinstance(model, (str, DroneModel)):
        return _model_of(model), None
    if callable(getattr(model, "narrow", None)) and isinstance(getattr(model, "model", None), DroneModel):
        return model.model, model
    raise ValueError(f"unsupported drone model {model!r}")


def resolve(
    model: Union[str, DroneModel, Any],
    unit_id: str,
    layers: Mapping[Union[Source, str], Mapping[str, Any]],
    *,
    scale: Optional[str] = None,
    simulated: bool = False,
    confirmations: Optional[Mapping[str, Any]] = None,
    today_accepts: Optional[TodayAccepts] = None,
    features: Optional[Mapping[str, Any]] = None,
) -> dict[str, EffectiveValue]:
    """Resolve every row that applies to ``model`` for ``unit_id``.

    ``model`` is a built-in model id, a ``DroneModel``, or a custom or unknown
    model from ``custom.py``. ``layers`` maps ``file``, ``inventory`` and
    ``launcher`` to ``{id: value}`` (a value may be a ``LayerValue`` naming
    where it came from; the launcher layer may also hold the reserved
    ``confirmed`` object), ``env`` to a copy of the environment and ``cli`` to
    ``{flag: value}``. ``scale`` is ``room`` or ``field`` and defaults to the
    model's scale. ``simulated`` is true only for a verified simulated unit.
    ``confirmations`` maps an id to the value a ``--confirm-less-safe`` flag
    names; ``resolve`` only accepts it, and ``resolve_unit`` checks it.
    ``features`` are the unit's inventory feature switches. Raises
    ``ParamRefused`` for a refused value or a stored safety switch; the
    confirmation and constraint checks are ``check_resolved`` (decision 17).
    """
    drone, narrowing = _model_and_narrowing(model)
    profile = drone.profile
    scale = scale or drone.scale
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}")
    unknown = {Source(k) if not isinstance(k, Source) else k for k in layers} - set(CALLER_LAYERS)
    if unknown:
        raise ValueError(f"layers {sorted(u.value for u in unknown)} are not caller layers")
    if features:
        refused = stored_feature_off(features, f"inventory.json units.{unit_id} features")
        if refused:
            raise ParamRefused(refused, source=Source.INVENTORY)
    test_unit = _is_test_unit(drone, simulated)

    specs: dict[str, ParamSpec] = {}
    bases: dict[str, Any] = {}
    result: dict[str, EffectiveValue] = {}
    for base_spec in TABLE:
        spec = narrowing.narrow(base_spec) if narrowing is not None else base_spec
        default = spec.defaults.get(profile)
        if default is None:
            continue
        specs[spec.id] = spec
        bases[spec.id] = base_spec.defaults.get(profile)
        narrowed = narrowing is not None and spec is not base_spec
        detail = narrowing.source_detail if narrowed else profile
        result[spec.id] = _effective(spec, drone, default, bases[spec.id], Source.PROFILE, detail, False)

    for source in CALLER_LAYERS:
        layer = layers.get(source, layers.get(source.value))
        if source is Source.LAUNCHER and layer:
            layer = {k: v for k, v in layer.items() if k != LAUNCHER_CONFIRMED}
        if not layer:
            continue
        for param_id, (_, raw, detail, name) in _entries(source, layer, scale, drone, unit_id).items():
            if param_id not in result:
                log.debug("%s from %s does not apply to %s; skipped", param_id, detail, drone.id)
                continue
            spec = specs[param_id]
            edit = spec.edit.get(profile)
            if source in _ID_LAYERS and edit in (Edit.FACT, Edit.READ_ONLY):
                if edit is Edit.FACT:
                    raise ParamRefused(FACT_NOT_SETTABLE.format(detail=detail), param_id=param_id, source=source)
                raise _refusal(spec, drone, unit_id, raw, detail, None, source)
            if edit is Edit.READ_ONLY:
                log.warning("%s is ignored: %s is read-only for %s", detail, param_id, drone.id)
                continue
            if name is not None and _new_name_without_bound(spec, profile, name.name):
                log.warning("%s is ignored: no code reads it today and %s has no bound for %s",
                            detail, param_id, drone.id)
                continue
            if edit is Edit.FACT:
                if not test_unit:
                    log.warning("%s is ignored on %s: it is a test-adapter override", detail, unit_id)
                    continue
                log.info("test-adapter override %s=%r on %s", detail, raw, unit_id)
            try:
                value, is_off = _value_of(spec, raw, source, name)
            except _WrongType:
                raise _refusal(spec, drone, unit_id, raw, detail, None, source) from None
            if is_off and spec.safety_switch and source in FILE_SOURCES:
                raise ParamRefused(stored_switch_off(param_id, value, detail), param_id=param_id, source=source)
            if not is_off:
                value = _check_bound(spec, drone, unit_id, value, detail, source,
                                     name.name if name else None, today_accepts, narrowing is not None)
            result[param_id] = _effective(spec, drone, value, bases[param_id], source, detail, is_off)

    return result


def resolve_unit(
    model: Union[str, DroneModel, Any],
    unit_id: str,
    layers: Mapping[Union[Source, str], Mapping[str, Any]],
    *,
    scale: Optional[str] = None,
    simulated: bool = False,
    confirmations: Optional[Mapping[str, Any]] = None,
    today_accepts: Optional[TodayAccepts] = None,
    features: Optional[Mapping[str, Any]] = None,
) -> dict[str, EffectiveValue]:
    """Resolve one unit and run the safety checks a runtime caller needs (decision 17).

    Takes the arguments of ``resolve`` and then calls ``check_resolved`` with
    the launcher layer's ``confirmed`` object, if a launcher layer is given,
    or else with ``confirmations``.
    """
    result = resolve(model, unit_id, layers, scale=scale, simulated=simulated, confirmations=confirmations,
                     today_accepts=today_accepts, features=features)
    launcher = layers.get(Source.LAUNCHER, layers.get(Source.LAUNCHER.value))
    drone, _ = _model_and_narrowing(model)
    if launcher is not None:
        check_resolved(result, drone, launcher_confirmed=launcher.get(LAUNCHER_CONFIRMED) or {}, unit_id=unit_id)
    else:
        check_resolved(result, drone, confirmations=confirmations or {}, unit_id=unit_id)
    return result


def check_resolved(
    result: Mapping[str, EffectiveValue],
    model: DroneModel,
    *,
    confirmations: Optional[Mapping[str, Any]] = None,
    launcher_confirmed: Optional[Mapping[str, Any]] = None,
    unit_id: str = "",
) -> None:
    """Refuse unconfirmed less-safe values from files (RES-11) and failed constraints (RES-5).

    Pass ``launcher_confirmed`` when the unit has a launcher layer, and
    ``confirmations`` (the ``--confirm-less-safe`` flags) otherwise; when
    ``launcher_confirmed`` is given, ``confirmations`` does not count.
    """
    launcher_present = launcher_confirmed is not None
    confirmed = launcher_confirmed if launcher_present else (confirmations or {})
    for param_id, effective in result.items():
        spec = _BY_ID[param_id]
        if not needs_confirmation(spec, effective):
            continue
        if effective.source in FILE_SOURCES:
            if not _confirmed(spec, confirmed.get(param_id), effective.value):
                raise _unconfirmed(spec, model, effective, effective.base_default, launcher_present)
        elif effective.source in _NAME_LAYERS:
            log.warning("less-safe %s=%r from %s on %s", param_id, effective.value, effective.source_detail, unit_id)
    violation = check_constraints(result, model.name, profile=model.profile)
    if violation is not None:
        raise ParamRefused(violation.message)


def _unconfirmed(spec: ParamSpec, model: DroneModel, effective: EffectiveValue, base: Any,
                 launcher_present: bool) -> ParamRefused:
    """The refusal of a less-safe value from a file that was not confirmed exactly (RES-11)."""
    value = "off" if _off_match(spec, effective.value) and isinstance(spec.off_value, str) else effective.value
    shown = str(value).lower() if isinstance(value, bool) else _whole(value) if _is_number(value) else value
    how = (_CONFIRM_IN_LAUNCHER if launcher_present
           else _CONFIRM_BY_FLAG.format(param_id=spec.id, value=shown))
    text = UNCONFIRMED_LESS_SAFE.format(
        param_id=spec.id, value=shown, unit=spec.unit.value, detail=effective.source_detail,
        model=model.name, base=_whole(base) if _is_number(base) else base, how=how,
    )
    return ParamRefused(text, param_id=spec.id, source=effective.source)


def _effective(spec: ParamSpec, model: DroneModel, value: Any, base: Any, source: Source,
               detail: Optional[str], is_off: bool) -> EffectiveValue:
    """An effective value; ``base`` is the built-in base default that less-safe compares with."""
    bound = spec.bounds.get(model.profile)
    default = spec.defaults.get(model.profile)
    return EffectiveValue(
        id=spec.id,
        value=value,
        unit=spec.unit,
        source=source,
        source_detail=detail,
        default=default,
        base_default=base,
        lo=bound.lo if bound else None,
        hi=bound.hi if bound else None,
        model=model.id,
        applies=spec.applies,
        non_default=value != base,
        less_safe=is_less_safe(spec, value, base) if not is_off else not _off_match(spec, base),
    )
