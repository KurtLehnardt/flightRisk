"""The effective-values report of one unit and its ``unit_rev``.

``build_report`` turns an edge's **start-time** resolution (the result of
``resolver.resolve_unit``), its model and its feature gates into the three
objects of the ``describe_unit`` reply (detail section 12.2): ``unit``,
``gates`` and ``values``, plus ``unit_rev``, the first 12 hex digits of
SHA-256 over their canonical JSON (detail section 12.3, PROTO-5).

Rules:

* **Test-adapter facts are never reported.** A row that is an ``edit: fact``
  row for the unit's profile (the Tello tuning thresholds) is left out of
  ``values`` and out of the hash, so neither its id nor its value ever leaves
  the edge (design 7.5, risk R3).
* **Each row carries its ``edit`` kind** (``ui``, ``file_env`` or
  ``read_only``), so a phone-facing view can show read-only rows as
  read-only and never as settings.
* **Only drone-scope rows** are reported: the ground resolves its own
  ground-scope and mission values.
* **No list of numbers.** A list value is reported as comma-separated text,
  and bounds are the ``lo`` and ``hi`` keys of the row.
* **Canonical JSON.** Keys sorted, compact separators, every float rounded to
  9 significant digits and a whole float written as an integer, so key order
  and float formatting never change ``unit_rev``. Runtime changes (for
  example a later ``set_ceiling``) are not inputs, so they never change it.

Pure: no file, environment or network access. Nothing at runtime calls this
module yet; the edge adopts it in A3.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional

from flightrisk.hardware.catalogue import DroneModel
from flightrisk.params.spec import EffectiveValue, Edit, ParamSpec, Scope
from flightrisk.params.table import TABLE

UNIT_REV_LEN = 12
_TEXT_LEN = 200
_SPECS: Mapping[str, ParamSpec] = MappingProxyType({spec.id: spec for spec in TABLE})


@dataclass(frozen=True)
class UnitReport:
    """The start-time description of one unit. Build it once, at start."""

    unit: Mapping[str, Any]
    gates: Mapping[str, Any]
    values: Mapping[str, Any]
    unit_rev: str

    def wire(self) -> dict[str, Any]:
        """``unit``, ``gates``, ``values`` and ``unit_rev`` as plain JSON-ready objects (copies)."""
        return json.loads(canonical_json({
            "unit": self.unit, "gates": self.gates, "values": self.values, "unit_rev": self.unit_rev,
        }))


def _number(value: float) -> Any:
    rounded = float(f"{value:.9g}")
    return int(rounded) if rounded.is_integer() else rounded


def _plain(value: Any) -> Any:
    """*value* as canonical JSON data: whole floats as integers, enums as their value."""
    if isinstance(value, Enum):
        return _plain(value.value)
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("a reported number must be finite")
        return _number(value)
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    raise TypeError(f"cannot report a value of type {type(value).__name__}")


def canonical_json(obj: Any) -> str:
    """The canonical JSON text of *obj* (sorted keys, compact, normalised numbers)."""
    return json.dumps(_plain(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def unit_rev_of(unit: Mapping[str, Any], gates: Mapping[str, Any], values: Mapping[str, Any]) -> str:
    """First 12 hex digits of SHA-256 over the canonical JSON of the description."""
    text = canonical_json({"unit": unit, "gates": gates, "values": values})
    return hashlib.sha256(text.encode("ascii")).hexdigest()[:UNIT_REV_LEN]


def _scalar(value: Any) -> Any:
    """A reported ``v`` or ``d``: a number, bool, short text or null; a list becomes text."""
    plain = _plain(value)
    if isinstance(plain, list):
        return ",".join(str(item) for item in plain)[:_TEXT_LEN]
    if isinstance(plain, dict):
        raise TypeError("a reported value cannot be an object")
    if isinstance(plain, str):
        return plain[:_TEXT_LEN]
    return plain


def _drone_of(model: Any) -> DroneModel:
    if isinstance(model, DroneModel):
        return model
    inner = getattr(model, "model", None)
    if isinstance(inner, DroneModel):
        return inner
    raise TypeError(f"unsupported drone model {model!r}")


def unit_object(model: Any, *, scale: Optional[str] = None,
                roles: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """The ``unit`` object: model facts and cameras. ``link`` is only the link kind."""
    drone = _drone_of(model)
    roles = roles or {}
    cameras = {
        cam.id: {
            "mount": cam.mount,
            "mount_kind": cam.mount_kind,
            "orient": cam.orientation,
            "pitch_min_deg": _plain(cam.pitch_min_deg),
            "pitch_max_deg": _plain(cam.pitch_max_deg),
            "role": roles.get(cam.id),
            "w_px": cam.stream_w_px,
            "h_px": cam.stream_h_px,
            "hfov_deg": _plain(cam.hfov_deg),
            "sensor": cam.sensor,
            "radiometric": bool(cam.radiometric),
        }
        for cam in drone.cameras
    }
    return {
        "model": drone.id,
        "model_name": drone.name[:_TEXT_LEN],
        "adapter": drone.adapter,
        "scale": scale or drone.scale,
        "positioning": drone.positioning,
        "link": drone.link,
        "cameras": cameras,
    }


def reported_rows(profile: str, resolution: Mapping[str, EffectiveValue]) -> list[str]:
    """Ids of the rows a report includes: drone scope, never a fact for *profile*."""
    rows = []
    for param_id in sorted(resolution):
        spec = _SPECS.get(param_id)
        if spec is None or spec.scope is not Scope.DRONE or spec.is_fact(profile):
            continue
        rows.append(param_id)
    return rows


def values_object(profile: str, resolution: Mapping[str, EffectiveValue]) -> dict[str, Any]:
    """The ``values`` object: ``{id: {"v", "src", "d", "lo", "hi", "edit"}}``, facts left out."""
    values = {}
    for param_id in reported_rows(profile, resolution):
        effective = resolution[param_id]
        edit = _SPECS[param_id].edit.get(profile) or Edit.READ_ONLY
        values[param_id] = {
            "v": _scalar(effective.value),
            "src": _plain(effective.source),
            "d": _scalar(effective.default),
            "lo": _plain(effective.lo),
            "hi": _plain(effective.hi),
            "edit": edit.value,
        }
    return values


def gates_object(gates: Optional[Mapping[str, Mapping[str, Any]]]) -> dict[str, Any]:
    """The ``gates`` object: ``{feature id: {"on", "ok", "why", "src"}}``."""
    out = {}
    for feature_id, gate in sorted((gates or {}).items()):
        why = gate.get("why")
        out[str(feature_id)] = {
            "on": bool(gate.get("on")),
            "ok": bool(gate.get("ok")),
            "why": None if why is None else str(why)[:_TEXT_LEN],
            "src": str(_plain(gate.get("src") or "profile")),
        }
    return out


def build_report(
    model: Any,
    resolution: Mapping[str, EffectiveValue],
    *,
    gates: Optional[Mapping[str, Mapping[str, Any]]] = None,
    roles: Optional[Mapping[str, str]] = None,
    scale: Optional[str] = None,
) -> UnitReport:
    """The start-time report of one unit and its ``unit_rev`` (PROTO-5).

    *model* is a ``DroneModel`` or a custom model from ``custom.py``;
    *resolution* is the start-time result of ``resolver.resolve_unit``;
    *gates* maps a feature id to ``{"on", "ok", "why", "src"}``; *roles* maps
    a camera id to its role.
    """
    drone = _drone_of(model)
    unit = unit_object(model, scale=scale, roles=roles)
    gate_obj = gates_object(gates)
    values = values_object(drone.profile, resolution)
    return UnitReport(
        unit=MappingProxyType(unit),
        gates=MappingProxyType(gate_obj),
        values=MappingProxyType(values),
        unit_rev=unit_rev_of(unit, gate_obj, values),
    )
