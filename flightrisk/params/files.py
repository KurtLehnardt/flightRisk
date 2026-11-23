"""Load ``params.json``, the parameter file layer of the resolver (RES-7).

The file is ``{"version": 1, "defaults": {...}, "models": {model: {...}},
"units": {unit: {...}}}``, each section mapping a parameter id to a typed
value. For one unit, ``units.<unit>`` wins over ``models.<model>``, which
wins over ``defaults``. A missing file is an empty layer. A file that exists
but cannot be read or parsed is refused, with no fallback to the defaults.
An unknown id, a value of the wrong type and a test-adapter fact are refused
(FACT-2, file schema part); a fact is refused without being named. An id
that is read-only for every model is refused here; one read-only for only
some models is refused by the resolver when it reaches such a unit. Bounds
are checked by the resolver, which knows the unit's model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Union

from flightrisk.params.resolver import FACT_NOT_SETTABLE, LayerValue, ParamRefused, is_wrong_type, spec_for
from flightrisk.params.spec import Edit

SCHEMA_VERSION = 1
SECTIONS = ("defaults", "models", "units")
DEFAULT_NAME = "params.json"


@dataclass(frozen=True)
class ParamsFile:
    """A checked ``params.json``; ``layer_for`` gives one unit's file layer."""

    name: str = DEFAULT_NAME
    defaults: Mapping[str, Any] = field(default_factory=dict)
    models: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    units: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def layer_for(self, model_id: str, unit_id: str) -> dict[str, LayerValue]:
        """The file layer of one unit, each value naming the section it came from."""
        layer: dict[str, LayerValue] = {}
        for section, values in (
            ("defaults", self.defaults),
            (f"models.{model_id}", self.models.get(model_id, {})),
            (f"units.{unit_id}", self.units.get(unit_id, {})),
        ):
            for param_id, value in values.items():
                layer[param_id] = LayerValue(value, f"{self.name} {section}")
        return layer


def _refuse(text: str) -> ParamRefused:
    return ParamRefused(f"{text} Refusing to start.")


def _check_section(name: str, where: str, values: Any) -> Mapping[str, Any]:
    if not isinstance(values, dict):
        raise _refuse(f"{name} {where} must be an object of parameter values.")
    for param_id, value in values.items():
        spec = spec_for(param_id)
        if spec is None:
            raise _refuse(f"{name} {where} names `{param_id}`, which is not a known setting.")
        edits = set(spec.edit.values())
        if Edit.FACT in edits:
            raise ParamRefused(FACT_NOT_SETTABLE.format(detail=f"{name} {where}"), param_id=param_id)
        if edits == {Edit.READ_ONLY}:
            raise _refuse(f"{name} {where} sets `{param_id}`, which is read-only.")
        if is_wrong_type(spec, value):
            raise _refuse(f"{name} {where} sets `{param_id}` to {value!r}, which is not a {spec.kind.value}.")
    return MappingProxyType(dict(values))


def _check_nested(name: str, section: str, values: Any) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(values, dict):
        raise _refuse(f"{name} {section} must be an object.")
    return MappingProxyType({
        str(key): _check_section(name, f"{section}.{key}", inner) for key, inner in values.items()
    })


def parse_params(data: Any, name: str = DEFAULT_NAME) -> ParamsFile:
    """Check parsed ``params.json`` data; raises ``ParamRefused``."""
    if not isinstance(data, dict):
        raise _refuse(f"{name} must be a JSON object.")
    version = data.get("version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise _refuse(f"{name} must have \"version\": {SCHEMA_VERSION}.")
    extra = sorted(set(data) - set(SECTIONS) - {"version"})
    if extra:
        raise _refuse(f"{name} has unknown sections {', '.join(extra)}.")
    return ParamsFile(
        name=name,
        defaults=_check_section(name, "defaults", data.get("defaults", {})),
        models=_check_nested(name, "models", data.get("models", {})),
        units=_check_nested(name, "units", data.get("units", {})),
    )


def load_params_file(path: Union[str, Path]) -> ParamsFile:
    """Read and check ``params.json``; a missing file is an empty layer."""
    path = Path(path)
    if not (path.is_symlink() or path.exists()):
        return ParamsFile(name=path.name)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _refuse(f"{path.name} exists but cannot be read ({type(exc).__name__}).") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _refuse(f"{path.name} exists but cannot be parsed ({exc.msg}, line {exc.lineno}).") from None
    except (ValueError, RecursionError) as exc:
        # Python's integer digit limit raises a plain ValueError; deep nesting raises RecursionError.
        raise _refuse(f"{path.name} exists but cannot be parsed ({type(exc).__name__}).") from None
    return parse_params(data, name=path.name)
