"""Resolve the effective flight parameters of one unit from its layers.

``resolve`` is a pure function. Callers pass layers they have already read
(the parsed ``params.json`` layer, the inventory entry, the launcher window,
a copy of the environment and the parsed flags); this module never reads
``os.environ`` or a file. Precedence is RES-1, lowest to highest: profile,
file, inventory, launcher, environment, command-line flag. Every value carries
its ``source`` and ``source_detail`` (RES-2). Nothing at runtime calls this
module yet; adoption is A3, A4 and A5.

Bounds (design 7.1, risk R4): until C5 an environment or flag value is checked
against the range today's code accepts for that name (``today_accepts`` of the
golden file, with its ``policy``), and a file, inventory or launcher value is
checked against the target bound of the model's profile column.

Decisions:

1. Today's ranges are passed in as ``today_accepts`` (see
   ``today_accepts_from_golden``) because they live in the golden file under
   ``tests/``, which runtime code must not read. A name with no recorded
   today range (the golden file's "no importable range constant" rows, or
   ``today_accepts=None``) is checked against the target bound, the safer
   choice; the parity test reports any such name where that differs from
   today's reader.
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
11. A scaled value keeps fractional centimetres (``1.234`` m is ``123.4``
    cm), as today's readers multiply floats; an unscaled integer row takes
    only whole numbers.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Union

from flightrisk.hardware.catalogue import BUILTIN_MODELS, SCALES, DroneModel
from flightrisk.params.messages import render_refused_input, render_unavailable_capability
from flightrisk.params.spec import Bound, EffectiveValue, Edit, Kind, ParamSpec, Source, Unit
from flightrisk.params.table import TABLE

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
_CENTIMETRE_UNITS = (Unit.CM, Unit.CM_PER_S, Unit.CM_LIST)
_TRUE_TEXT = ("1", "true", "yes", "on")
_FALSE_TEXT = ("0", "false", "no", "off")

#: Operator text for a refused test-adapter override; it names neither the value nor the variable.
FACT_OVERRIDE_REFUSED = (
    "A test-adapter developer override is outside the range the adapter accepts. Refusing to start."
)
#: Operator text for a stored value that is not a setting; it does not name the value.
FACT_NOT_SETTABLE = "A value in {detail} is not a setting and cannot be stored. Refusing to start."


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
                    accepts.get("lo"), accepts.get("hi"), accepts.get("policy") or "refuse"
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
            env[spec.env] = _Name(spec, spec.env, 1.0, all_scales - alias_scales, True)
        for alias in spec.env_aliases:
            scales = frozenset({alias.applies_to_scale}) if alias.applies_to_scale else all_scales
            env[alias.name] = _Name(spec, alias.name, alias.scale_factor, scales, False)
        for flag in spec.cli:
            scales = frozenset({"field"}) if flag in FIELD_ONLY_FLAGS else all_scales
            cli[flag] = _Name(spec, flag, _flag_scale(spec, flag), scales, True)
    return env, cli


_ENV_NAMES, _CLI_NAMES = _build_names()
_BY_ID: dict[str, ParamSpec] = {spec.id: spec for spec in TABLE}


def spec_for(param_id: str) -> Optional[ParamSpec]:
    """The registry row of ``param_id``, or None for an unknown id."""
    return _BY_ID.get(param_id)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


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
    items = value if isinstance(value, tuple) else (value,)
    for item in items:
        if not _is_number(item):
            continue
        if lo is not None and item < lo:
            return "lo"
        if hi is not None and item > hi:
            return "hi"
    return None


def _today_range(today_accepts: Optional[TodayAccepts], name: str, profile: str) -> Optional[TodayRange]:
    if today_accepts is None:
        return None
    return (today_accepts.get(name) or {}).get(profile)


def _check_bound(spec: ParamSpec, model: DroneModel, unit_id: str, value: Any, detail: str, source: Source,
                 name: Optional[str], today_accepts: Optional[TodayAccepts]) -> Any:
    """Return the value to use after the bound check, clamped where today's code clamps; raise if refused."""
    profile = model.profile
    today = _today_range(today_accepts, name, profile) if name and source in _NAME_LAYERS else None
    if today is not None:
        end = _outside(value, today.lo, today.hi)
        if end is None:
            return value
        if today.policy == "clamp" and not isinstance(value, tuple):
            clamped = today.lo if end == "lo" else today.hi
            clamped = _whole(clamped) if spec.kind is Kind.INT else float(clamped)
            log.warning("%s=%r is outside %s to %s; clamped to %r as today", name, value, today.lo, today.hi, clamped)
            return clamped
        raise _refusal(spec, model, unit_id, value, detail, (today.lo, today.hi), source)
    target: Optional[Bound] = spec.bounds.get(profile)
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
        if raw is None or (isinstance(raw, str) and not raw.strip()):
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
    """The typed value and whether it is the row's off value; raises ``_WrongType``."""
    if _off_match(spec, raw):
        return spec.off_value, True
    if source in _NAME_LAYERS:
        text = raw if isinstance(raw, str) else str(raw)
        if isinstance(raw, bool) or (raw is not None and not isinstance(raw, (str, int, float))):
            raise _WrongType(raw)
        return _from_text(spec, text, name.scale if name else 1.0), False
    return coerce_typed(spec, raw), False


def _is_test_unit(model: DroneModel, simulated: bool) -> bool:
    return model.adapter in TEST_ADAPTERS or bool(simulated)


def resolve(
    model: Union[str, DroneModel],
    unit_id: str,
    layers: Mapping[Union[Source, str], Mapping[str, Any]],
    *,
    scale: Optional[str] = None,
    simulated: bool = False,
    confirmations: Optional[Mapping[str, Any]] = None,
    today_accepts: Optional[TodayAccepts] = None,
) -> dict[str, EffectiveValue]:
    """Resolve every row that applies to ``model`` for ``unit_id``.

    ``layers`` maps ``file``, ``inventory`` and ``launcher`` to ``{id: value}``
    (a value may be a ``LayerValue`` naming where it came from), ``env`` to a
    copy of the environment and ``cli`` to ``{flag: value}``. ``scale`` is
    ``room`` or ``field`` and defaults to the model's scale. ``simulated`` is
    true only for a verified simulated unit. ``confirmations`` is accepted so
    callers keep one signature; the less-safe checks that read it come with
    the resolver safety step. Raises ``ParamRefused`` for a refused value.
    """
    drone = _model_of(model)
    profile = drone.profile
    scale = scale or drone.scale
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}")
    unknown = {Source(k) if not isinstance(k, Source) else k for k in layers} - set(CALLER_LAYERS)
    if unknown:
        raise ValueError(f"layers {sorted(u.value for u in unknown)} are not caller layers")
    test_unit = _is_test_unit(drone, simulated)

    result: dict[str, EffectiveValue] = {}
    for spec in TABLE:
        default = spec.defaults.get(profile)
        if default is None:
            continue
        result[spec.id] = _effective(spec, drone, default, default, Source.PROFILE, profile, False)

    for source in CALLER_LAYERS:
        layer = layers.get(source, layers.get(source.value))
        if not layer:
            continue
        for param_id, (spec, raw, detail, name) in _entries(source, layer, scale, drone, unit_id).items():
            if param_id not in result:
                log.debug("%s from %s does not apply to %s; skipped", param_id, detail, drone.id)
                continue
            edit = spec.edit.get(profile)
            if source in _ID_LAYERS and edit in (Edit.FACT, Edit.READ_ONLY):
                if edit is Edit.FACT:
                    raise ParamRefused(FACT_NOT_SETTABLE.format(detail=detail), param_id=param_id, source=source)
                raise _refusal(spec, drone, unit_id, raw, detail, None, source)
            if edit is Edit.READ_ONLY:
                log.warning("%s is ignored: %s is read-only for %s", detail, param_id, drone.id)
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
            if not is_off:
                value = _check_bound(spec, drone, unit_id, value, detail, source,
                                     name.name if name else None, today_accepts)
            result[param_id] = _effective(spec, drone, value, spec.defaults[profile], source, detail, is_off)
    return result


def _effective(spec: ParamSpec, model: DroneModel, value: Any, default: Any, source: Source,
               detail: Optional[str], is_off: bool) -> EffectiveValue:
    bound = spec.bounds.get(model.profile)
    return EffectiveValue(
        id=spec.id,
        value=value,
        unit=spec.unit,
        source=source,
        source_detail=detail,
        default=default,
        base_default=default,
        lo=bound.lo if bound else None,
        hi=bound.hi if bound else None,
        model=model.id,
        applies=spec.applies,
        non_default=source is not Source.PROFILE and value != default,
        less_safe=is_off,
    )
