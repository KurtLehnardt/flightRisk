"""Custom models, unknown models and fleets over the built-in profile columns.

A custom model is defined in ``inventory.json`` ``models`` with ``extends``
(detail section 6, 8.1). It may narrow a bound and set a default inside its
bounds. It may not widen a bound, change the scale, the adapter, the frame
path or the profile, add a parameter, or set ``environment`` or
``indoor_profile_measured`` (CAT-5, K-18, M-11). Until C5 a custom default
that is less safe than its base is refused (CONF-3, design 7.3(4)).

Decisions:

1. A custom model extends a built-in model only, never another custom model.
2. Descriptor fields other than the fixed ones are kept as given for the A2
   loader; the cameras of the descriptor stay the base's until A2 parses
   them. Unknown fields are refused, naming the field.
3. A bound may be narrowed only on a row that has a numeric bound for the
   base profile. A test-adapter fact or a read-only row in ``param_bounds``
   or ``param_defaults`` is refused; a fact is never named.
4. An unknown model (CAP-2) narrows to the intersection of the bounds of the
   built-in profiles of its scale, with the safer default of each by
   ``safer``, and keeps only the rows every such profile has. Its facts are
   empty, so each takes its conservative value marked ``unverified``.
5. A fleet intersects the bounds of its ``mission`` and ``ground`` rows, the
   values the fleet shares; a ``drone`` row is per unit and is not
   intersected. The fleet default is the safer default of the units.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Union

from flightrisk.hardware.catalogue import BUILTIN_MODELS, DroneModel
from flightrisk.params.resolver import (
    FACT_NOT_SETTABLE,
    ParamRefused,
    _model_and_narrowing,
    coerce_typed,
    is_less_safe,
    is_wrong_type,
    spec_for,
)
from flightrisk.params.spec import Bound, Edit, ParamSpec, Safer, Scope, Source
from flightrisk.params.table import TABLE

#: Operator text of CONF-3 (design 7.3(4)), exactly.
CUSTOM_LESS_SAFE = "this custom default is less safe than its base; a confirmation for it arrives in C5"
#: Fields a custom model may never set (K-18, M-11).
FORBIDDEN_FIELDS = ("environment", "indoor_profile_measured")
#: Descriptor fields that must equal the base's (CAT-5).
FIXED_FIELDS = ("scale", "adapter", "frame_path", "profile")
#: Descriptor fields a custom model may set, beside ``extends`` and the parameter fields.
DESCRIPTOR_FIELDS = ("name", "maker", "cameras", "positioning", "link", "sources", "mounts")
PARAM_FIELDS = ("param_bounds", "param_defaults")


@dataclass(frozen=True)
class CustomModel:
    """A model narrowed from a built-in base: its descriptor, bounds and defaults by parameter id."""

    model: DroneModel
    base: DroneModel
    bounds: Mapping[str, Bound]
    defaults: Mapping[str, Any]
    source_detail: str
    #: Rows the model keeps; ``None`` keeps every row of the base profile.
    rows: Optional[frozenset[str]] = None

    def narrow(self, spec: ParamSpec) -> ParamSpec:
        """The row with this model's bound and default in the profile column."""
        profile = self.base.profile
        if self.rows is not None and spec.id not in self.rows:
            return dataclasses.replace(spec, defaults={**spec.defaults, profile: None})
        if spec.id not in self.bounds and spec.id not in self.defaults:
            return spec
        bounds = dict(spec.bounds)
        defaults = dict(spec.defaults)
        if spec.id in self.bounds:
            bounds[profile] = self.bounds[spec.id]
        if spec.id in self.defaults:
            defaults[profile] = self.defaults[spec.id]
        return dataclasses.replace(spec, bounds=bounds, defaults=defaults)


def _refuse(model_id: str, field: str, reason: str) -> ParamRefused:
    return ParamRefused(f"Custom model {model_id} is refused: {field} {reason}. Refusing to start.",
                        source=Source.INVENTORY)


def _num(value: Any) -> str:
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def _row(model_id: str, field: str, param_id: str, base: DroneModel) -> ParamSpec:
    spec = spec_for(param_id)
    profile = base.profile
    if spec is None or spec.defaults.get(profile) is None:
        raise _refuse(model_id, f"`{field}.{param_id}`", f"adds a parameter the {base.name} does not have")
    edit = spec.edit.get(profile)
    if edit is Edit.FACT:
        raise ParamRefused(FACT_NOT_SETTABLE.format(detail=f"inventory.json models.{model_id} {field}"),
                           source=Source.INVENTORY)
    if edit is Edit.READ_ONLY:
        raise _refuse(model_id, f"`{field}.{param_id}`", f"is read-only for the {base.name}")
    return spec


def custom_model(model_id: str, record: Mapping[str, Any]) -> CustomModel:
    """Check one ``inventory.json`` model record and return the custom model; raises ``ParamRefused``."""
    if model_id in BUILTIN_MODELS:
        raise _refuse(model_id, "its id", "is the id of a built-in model")
    base_id = record.get("extends")
    base = BUILTIN_MODELS.get(base_id) if isinstance(base_id, str) else None
    if base is None:
        raise _refuse(model_id, "`extends`", "must name a built-in model")
    for field in record:
        if field in FORBIDDEN_FIELDS:
            raise _refuse(model_id, f"`{field}`", "is a fact of the built-in base and cannot be set")
        if field in FIXED_FIELDS:
            if record[field] != getattr(base, field):
                raise _refuse(model_id, f"`{field}`", f"cannot change from the {base.name}'s {getattr(base, field)}")
        elif field not in ("extends",) + DESCRIPTOR_FIELDS + PARAM_FIELDS:
            raise _refuse(model_id, f"`{field}`", "is not a field of a custom model")

    profile = base.profile
    bounds: dict[str, Bound] = {}
    for param_id, given in (record.get("param_bounds") or {}).items():
        spec = _row(model_id, "param_bounds", param_id, base)
        base_bound = spec.bounds.get(profile)
        lo = given.get("lo") if isinstance(given, Mapping) else None
        hi = given.get("hi") if isinstance(given, Mapping) else None
        field = f"`param_bounds.{param_id}`"
        if base_bound is None or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lo, hi)):
            raise _refuse(model_id, field, "is not a numeric bound the base has")
        if lo > hi:
            raise _refuse(model_id, field, f"{_num(lo)} to {_num(hi)} is empty")
        if lo < base_bound.lo or hi > base_bound.hi:
            raise _refuse(model_id, field, f"{_num(lo)} to {_num(hi)} {spec.unit.value} widens the {base.name}'s "
                                           f"{_num(base_bound.lo)} to {_num(base_bound.hi)} {spec.unit.value}")
        bounds[param_id] = Bound(lo, hi)

    defaults: dict[str, Any] = {}
    for param_id, value in (record.get("param_defaults") or {}).items():
        spec = _row(model_id, "param_defaults", param_id, base)
        field = f"`param_defaults.{param_id}`"
        if is_wrong_type(spec, value):
            raise _refuse(model_id, field, f"{value!r} is not a {spec.kind.value}")
        if value != spec.off_value:
            value = coerce_typed(spec, value)
        bound = bounds.get(param_id) or spec.bounds.get(profile)
        if bound is not None and value != spec.off_value and _outside(value, bound):
            raise _refuse(model_id, field, f"{_num(value)} is outside {_num(bound.lo)} to {_num(bound.hi)}")
        defaults[param_id] = value

    narrowed = dataclasses.replace(base, id=model_id, name=str(record.get("name") or model_id), extends=base.id)
    model = CustomModel(narrowed, base, bounds, defaults, f"inventory.json models.{model_id}")
    _refuse_less_safe_defaults(model)
    return model


def _outside(value: Any, bound: Bound) -> bool:
    items = value if isinstance(value, tuple) else (value,)
    return any(isinstance(i, (int, float)) and not isinstance(i, bool) and not bound.lo <= i <= bound.hi
               for i in items)


def _refuse_less_safe_defaults(model: CustomModel) -> None:
    """CONF-3: until C5 a custom default less safe than its base column is refused."""
    profile = model.base.profile
    for param_id, value in model.defaults.items():
        spec = spec_for(param_id)
        if is_less_safe(spec, value, spec.defaults.get(profile)):
            raise ParamRefused(f"`{param_id}` {_num(value)} in {model.source_detail}: {CUSTOM_LESS_SAFE}.",
                               param_id=param_id, source=Source.INVENTORY)


def _safer_of(spec: ParamSpec, values: list) -> Any:
    numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(numbers) != len(values) or spec.safer is Safer.NONE:
        return values[0]
    return max(numbers) if spec.safer is Safer.HIGHER else min(numbers)


def unknown_model(model_id: str, scale: str) -> CustomModel:
    """CAP-2: an unknown model at ``scale`` narrows to its scale's built-in bounds and safer defaults."""
    builtins = sorted((m for m in BUILTIN_MODELS.values() if m.scale == scale and m.support != "slot"),
                      key=lambda m: m.id)
    if not builtins:
        raise ValueError(f"no built-in model flies at scale {scale!r}")
    base = builtins[0]
    profiles = sorted({m.profile for m in builtins})
    bounds: dict[str, Bound] = {}
    defaults: dict[str, Any] = {}
    rows = set()
    for spec in TABLE:
        values = [spec.defaults.get(p) for p in profiles]
        if any(v is None for v in values):
            continue
        rows.add(spec.id)
        limits = [spec.bounds.get(p) for p in profiles]
        if all(b is not None for b in limits):
            bound = Bound(max(b.lo for b in limits), min(b.hi for b in limits))
            if bound.lo > bound.hi:
                raise ValueError(f"the built-in bounds of {spec.id} at {scale} scale do not overlap")
            bounds[spec.id] = bound
        defaults[spec.id] = _safer_of(spec, values)
    descriptor = dataclasses.replace(base, id=model_id, name=model_id, extends=base.id, facts={})
    return CustomModel(descriptor, base, bounds, defaults, f"unknown model {model_id}", frozenset(rows))


@dataclass(frozen=True)
class FleetBound:
    """The bound every unit of a fleet allows for one shared row, and the safer default."""

    bound: Bound
    default: Any


def fleet_bounds(units: Mapping[str, Union[str, DroneModel, CustomModel]]) -> dict[str, FleetBound]:
    """RES-12: intersect the shared rows' bounds across a fleet's models; refuse an empty one, naming units."""
    narrowed: dict[str, tuple[DroneModel, Any]] = {u: _model_and_narrowing(m) for u, m in units.items()}
    out: dict[str, FleetBound] = {}
    for base_spec in TABLE:
        if base_spec.scope not in (Scope.MISSION, Scope.GROUND):
            continue
        per_unit = []
        for unit_id, (drone, narrowing) in narrowed.items():
            spec = narrowing.narrow(base_spec) if narrowing is not None else base_spec
            per_unit.append((unit_id, drone, spec.bounds.get(drone.profile), spec.defaults.get(drone.profile)))
        if not per_unit or any(b is None or d is None for _, _, b, d in per_unit):
            continue
        lo = max(b.lo for _, _, b, _ in per_unit)
        hi = min(b.hi for _, _, b, _ in per_unit)
        if lo > hi:
            parts = [f"{u} ({d.name}) allows {_num(b.lo)} to {_num(b.hi)} {base_spec.unit.value}"
                     for u, d, b, _ in per_unit]
            raise ParamRefused(f"`{base_spec.id}` has no value every unit allows: {'; '.join(parts)}. "
                               "Refusing to start.", param_id=base_spec.id)
        out[base_spec.id] = FleetBound(Bound(lo, hi), _safer_of(base_spec, [d for _, _, _, d in per_unit]))
    return out
