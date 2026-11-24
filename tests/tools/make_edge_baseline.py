"""Record the edge start-object baseline (Phase A3, steps S1 and S2).

The fixture ``tests/fixtures/edge_start_baseline.json`` freezes what today's
edge answers at the A3 base commit, so that later A3 pull requests can rewire
the readers to the resolver while ``tests/test_edge_start_baseline.py`` still
compares them with an independent record.

The start-object and method sections come only from the real runtime modules:
the generator imports them and calls the real functions, and those sections never
read a value from ``flightrisk/params/table.py`` or from the golden file.

The probe section is not independent in that way. It imports
``tests/test_param_parity.py`` for its probe list and its ``_today`` wrapper,
and that harness loads the resolver and the golden file when it is imported.
The answers recorded are still today's readers' own answers, but which inputs
are probed depends on the harness, so ``tests/test_edge_start_baseline.py`` pins
the harness's sha256 and asks for a new baseline when it changes.

The fixture must be the same on every machine. Names a module binds in a
``try`` block that catches ``ImportError`` (an optional dependency such as
MAVSDK), and module constants computed from them, are left out, because their
value depends on whether that dependency is installed. A callable value is
recorded only by type, and only for the parameter names in
``CALLABLE_DEFAULTS``; any other callable or ``default_factory`` value stops
generation, so a new seam cannot slip into the record unnoticed.

Like the golden generator, it refuses to overwrite an existing output file, so
nothing can silently re-baseline the record.

Run: ``python tests/tools/make_edge_baseline.py [output]``
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import enum
import importlib
import inspect
import json
import math
import sys
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "tests" / "fixtures" / "edge_start_baseline.json"

#: The modules whose constants and constructor defaults the baseline records (the A3 allowlist files).
A3_MODULES = (
    "flightrisk.drone.autopilot",
    "flightrisk.drone.fake",
    "flightrisk.drone.flight_guards",
    "flightrisk.drone.lane_executor",
    "flightrisk.drone.lane_flight",
    "flightrisk.drone.mavlink_fleet",
    "flightrisk.drone.mavsdk_link",
    "flightrisk.drone.obstacle",
    "flightrisk.drone.obstacle_sensor",
    "flightrisk.drone.safety",
    "flightrisk.drone.tello",
    "flightrisk.drone.tello_bound",
    "flightrisk.drone.temp_preflight",
    "flightrisk.edge_device",
    "flightrisk.edge_profiles",
)
#: Argparse destinations of the edge parser whose defaults the baseline records.
PARSER_DESTS = ("speed_mps", "takeoff_alt_m")

#: Attributes that hold a clock reading, a clock or a lock, so they differ on every run.
VOLATILE_ATTRS = frozenset({"_lock", "_clock", "_last_s", "clock"})

#: The only names under which a callable value (a constructor or function default, a field or an attribute)
#: may be recorded. Every one is an injection seam for tests, recorded by type only. Today they are:
#: ``clock`` (FakeDrone, LaneExecutor, MavlinkFleetDrone, LatestFrame, LinkWatchdog, SafetyEnvelope,
#: BoundFrameRead, DroneFrameSource, ProfileSwitcher), ``sleep`` (FakeDrone, LaneExecutor), ``socket_factory`` (BoundFrameRead,
#: BoundTello) and ``loader`` (ProfileSwitcher).
CALLABLE_DEFAULTS = frozenset({"clock", "sleep", "socket_factory", "loader"})
#: The only dataclass fields with a ``default_factory``; each is recorded by the value its factory builds (all
#: four build an empty dict today). A new one stops generation until it is reviewed and listed here.
FACTORY_DEFAULTS = frozenset({
    "flightrisk.drone.autopilot.AutopilotState.stream_age_s",
    "flightrisk.drone.mavlink_fleet._AvoidanceCheck.values",
    "flightrisk.drone.mavlink_fleet._ParamCheck.values",
    "flightrisk.drone.obstacle_sensor.SenseResult.detail",
})

_SCALARS = (bool, int, float, str, type(None))


class BaselineError(Exception):
    """A value the baseline must not record silently."""


def jsonable(value: Any, name: str | None = None) -> Any:
    """``value`` as plain JSON data; non-finite floats and tuples keep their identity as tagged forms.

    ``name`` is the parameter, field, attribute or key the value was found under. A callable value is
    recorded by type only, and only under a name in ``CALLABLE_DEFAULTS``; any other raises ``BaselineError``.
    """
    if value is dataclasses.MISSING or type(value).__name__ == "_HAS_DEFAULT_FACTORY_CLASS":
        raise BaselineError(f"{name}: a default_factory value must be recorded by what it builds (FACTORY_DEFAULTS)")
    if callable(value) and not isinstance(value, enum.Enum):
        if name not in CALLABLE_DEFAULTS:
            raise BaselineError(f"{name}: callable value {value!r} is outside CALLABLE_DEFAULTS")
        return {"__repr__": f"{type(value).__name__}"}
    if isinstance(value, enum.Enum):
        return {"__enum__": f"{type(value).__name__}.{value.name}"}
    if isinstance(value, float) and not math.isfinite(value):
        return {"__float__": repr(value)}
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, (tuple, list)):
        return {"__seq__": [jsonable(item, name) for item in value]}
    if isinstance(value, (set, frozenset)):
        return {"__set__": sorted((jsonable(item, name) for item in value), key=json.dumps)}
    if isinstance(value, dict):
        return {str(key): jsonable(item, str(key)) for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if dataclasses.is_dataclass(value):
        return {field.name: jsonable(getattr(value, field.name), field.name) for field in dataclasses.fields(value)}
    if hasattr(value, "__dict__"):
        return {attr: jsonable(item, attr) for attr, item in sorted(vars(value).items())
                if not attr.startswith("__") and attr not in VOLATILE_ATTRS}
    return {"__repr__": f"{type(value).__name__}"}


def _is_constant_name(name: str) -> bool:
    return name.isupper() or (name.startswith("_") and name[1:].isupper())


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(n, ast.Name) and n.id in ("ImportError", "ModuleNotFoundError", "Exception") for n in names)


def _bound_names(nodes) -> set[str]:
    names = set()
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                names.update((a.asname or a.name).split(".")[0] for a in sub.names)
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                names.add(sub.id)
    return names


def optional_import_names(module: Any) -> set[str]:
    """Module-level names whose value depends on whether an optional import succeeded.

    These are the names bound in a module-level ``try`` that catches ``ImportError``, and every module-level
    assignment that reads one of them (followed to a fixed point), such as ``_REAL_SYSTEM = System``.
    """
    tree = ast.parse(inspect.getsource(module))
    optional: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Try) and any(_catches_import_error(h) for h in node.handlers):
            optional |= _bound_names(node.body + node.handlers + node.orelse)
    assigns = [n for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and n.value]
    changed = True
    while changed:
        changed = False
        for node in assigns:
            reads = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            targets = _bound_names(node.targets if isinstance(node, ast.Assign) else [node.target])
            if reads & optional and not targets <= optional:
                optional |= targets
                changed = True
    return optional


def _init_defaults(cls: type) -> dict[str, Any]:
    """The constructor defaults of ``cls``; a dataclass ``default_factory`` field records what the factory builds.

    A ``default_factory`` field outside ``FACTORY_DEFAULTS`` raises ``BaselineError``.
    """
    factories = {}
    if dataclasses.is_dataclass(cls):
        for field in dataclasses.fields(cls):
            if field.default_factory is not dataclasses.MISSING:
                qualified = f"{cls.__module__}.{cls.__qualname__}.{field.name}"
                if qualified not in FACTORY_DEFAULTS:
                    raise BaselineError(f"{qualified}: default_factory field is outside FACTORY_DEFAULTS")
                factories[field.name] = jsonable(field.default_factory(), field.name)
    defaults = {}
    try:
        params = inspect.signature(cls.__init__).parameters.values()
    except (TypeError, ValueError):
        return defaults
    for param in params:
        if param.name in factories:
            defaults[param.name] = factories[param.name]
        elif param.default is not inspect.Parameter.empty:
            defaults[param.name] = jsonable(param.default, param.name)
    return defaults


def module_constants(module_name: str) -> dict[str, Any]:
    """Every upper-case scalar, tuple or limits constant of one module, and the constructor defaults of its classes.

    Names that depend on an optional import (``optional_import_names``) are left out.
    """
    module = importlib.import_module(module_name)
    optional = optional_import_names(module)
    out: dict[str, Any] = {}
    for name, value in sorted(vars(module).items()):
        if name in optional or not _is_constant_name(name):
            continue
        if isinstance(value, _SCALARS + (tuple, frozenset, dict)) or (
            dataclasses.is_dataclass(value) and not isinstance(value, type)
        ):
            out[name] = jsonable(value, name)
    classes: dict[str, Any] = {}
    for name, cls in sorted(vars(module).items()):
        if not inspect.isclass(cls) or cls.__module__ != module_name:
            continue
        entry: dict[str, Any] = {}
        defaults = _init_defaults(cls)
        if defaults:
            entry["init_defaults"] = defaults
        attrs = {attr: jsonable(value, attr) for attr, value in sorted(vars(cls).items())
                 if _is_constant_name(attr) and isinstance(value, _SCALARS + (tuple,))}
        if attrs:
            entry["constants"] = attrs
        if entry:
            classes[name] = entry
    if classes:
        out["__classes__"] = classes
    return out


def start_objects() -> dict[str, Any]:
    """Every start object built from an empty environment, from the real runtime."""
    from flightrisk import edge_device
    from flightrisk.drone import safety, temp_preflight
    from flightrisk.drone.lane_executor import LaneExecutor
    from flightrisk.drone.mavlink_fleet import MavlinkFleetOptions

    parser = edge_device._build_parser()
    safety_constants = {
        name: jsonable(value, name) for name, value in sorted(vars(safety).items())
        if not name.startswith("__")
        and (name.isupper() or name.endswith("_LIMITS"))
        and (isinstance(value, _SCALARS + (tuple, dict)) or (dataclasses.is_dataclass(value) and not isinstance(value, type)))
    }
    lane_defaults = _init_defaults(LaneExecutor)
    return {
        "safety_envelope_default": jsonable(safety.SafetyEnvelope()),
        "safety_envelope_from_env_empty": jsonable(safety.SafetyEnvelope.from_env(environ={})),
        "safety_envelope_for_field": jsonable(safety.SafetyEnvelope.for_field()),
        "room_limits": jsonable(safety.ROOM_LIMITS),
        "field_limits": jsonable(safety.FIELD_LIMITS),
        "safety_constants": safety_constants,
        "flight_guards_empty_env": jsonable(edge_device._flight_guards({})),
        "temp_limits_from_env_empty": jsonable(temp_preflight.TempLimits.from_env({})),
        "obstacle_settings_tello": jsonable(edge_device.ObstacleSettings("tello", safety.SafetyEnvelope(), {})),
        "obstacle_settings_fake": jsonable(edge_device.ObstacleSettings("fake", safety.SafetyEnvelope(), {})),
        "obstacle_settings_mavlink": jsonable(edge_device.ObstacleSettings("mavlink", safety.SafetyEnvelope.for_field(), {})),
        "mavlink_fleet_options": jsonable(MavlinkFleetOptions()),
        "lane_executor_defaults": lane_defaults,
        "parser_defaults": {dest: jsonable(parser.get_default(dest)) for dest in PARSER_DESTS},
        "module_constants": {name: module_constants(name) for name in A3_MODULES},
        "drone_capabilities": drone_capabilities(),
    }


class _OfflineTello:
    """Stands in for djitellopy's ``Tello`` so building a ``TelloController`` opens no socket."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass


def drone_capabilities() -> dict[str, Any]:
    """The ``capabilities`` each A3 drone class builds inline in its constructor (move limits, altitude)."""
    from flightrisk.drone import tello
    from flightrisk.drone.fake import FakeDrone

    with mock.patch.object(tello, "Tello", _OfflineTello):
        tello_controller = tello.TelloController(host="192.168.10.1")
    return {
        "flightrisk.drone.fake.FakeDrone": jsonable(FakeDrone().capabilities),
        "flightrisk.drone.tello.FakeTelloController": jsonable(tello.FakeTelloController().capabilities),
        "flightrisk.drone.tello.TelloController": jsonable(tello_controller.capabilities),
    }


REFUSED = "REFUSED"
_NAN, _INF = float("nan"), float("inf")
#: The probe grid of ``method_results``: arguments per SafetyEnvelope method, the same for every envelope form.
METHOD_PROBES: dict[str, tuple[tuple, ...]] = {
    "validate_alt_slot": tuple((s,) for s in (-1, 0, 49, 50, 100, 130, 140, 500, 501, 1000, 1500, 2500, 12000, 12001,
                                              _NAN, _INF)),
    "check_alt_slot": tuple((s,) for s in (-1, 49, 50, 100, 130, 140, 141, 500, 1000, 1500, 2500, 2501, 12000)),
    "validate_ceiling": tuple((c, m) for c in (149, 150, 200, 1000, 1001, 3000, 12000, 12001, _NAN)
                              for m in (0, 29, 30, 60, 299, 300, 500, 11000)),
    "check_move": tuple((d, cm) for d in ("forward", "left", "up") for cm in (0, 19, 20, 100, 500, 501, 2999, 3000,
                                                                              5000, 5001, _NAN)),
    "inside_fence": ((0, 0), (500, 0), (500.5, 0), (0, -501), (499, 499), (3000, 3000), (3001, 0), (50000, 0)),
    "climb_allowed": tuple((h, d) for h in (None, 0, 100, 140, 2400, 2500, _NAN) for d in (0, 20, 100, 1000)),
    "above_ceiling": ((None,), (200,), (201,), (3000,), (3001,), (_NAN,)),
    "slot_move": tuple((h,) for h in (None, -5, 0, 50, 81, 100, 119, 120, 600, 1000, 1481, 1500, 1520, 5000, _NAN)),
    "can_takeoff": tuple((b,) for b in (None, 0, 14, 15, 29, 30, 100, _NAN)),
    "should_land": tuple((b,) for b in (None, 0, 14, 15, 29, 30, 100, _NAN)),
}


def method_results() -> dict[str, Any]:
    """Each SafetyEnvelope check's answer, or ``REFUSED`` for a ``ValueError``, per envelope form and probe."""
    from flightrisk.drone import safety

    forms = {
        "default": safety.SafetyEnvelope,
        "from_env_empty": lambda: safety.SafetyEnvelope.from_env(environ={}),
        "for_field": safety.SafetyEnvelope.for_field,
    }
    out: dict[str, Any] = {}
    for form, make in forms.items():
        envelope = make()
        calls = {
            "validate_alt_slot": lambda slot: envelope.validate_alt_slot(slot, envelope.limits),
            "validate_ceiling": lambda ceiling, margin: safety.validate_ceiling(ceiling, margin, envelope.limits),
        }
        results: dict[str, Any] = {}
        for method, probes in METHOD_PROBES.items():
            call = calls.get(method) or getattr(envelope, method)
            answers = {}
            for args in probes:
                try:
                    answers[repr(args)] = jsonable(call(*args))
                except ValueError:
                    answers[repr(args)] = REFUSED
            results[method] = answers
        out[form] = results
    return out


def probe_results() -> dict[str, Any]:
    """Today's reader output, or ``refused``, for every case of ``tests/test_param_parity.py`` (S2)."""
    sys.path.insert(0, str(ROOT))
    from tests.test_param_parity import CASES, _today

    out: dict[str, Any] = {}
    for index, (probe, raw) in enumerate(CASES):
        key = f"{index:04d}|{probe.model}|{probe.name}|{'flag' if probe.flag else 'env'}|{raw!r}"
        out[key] = jsonable(_today(lambda probe=probe, raw=raw: probe.read(raw)))
    return out


def build() -> dict[str, Any]:
    """The whole baseline as JSON data."""
    return {"start_objects": start_objects(), "methods": method_results(), "probes": probe_results()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.output.exists():
        print(f"refusing to overwrite {args.output}", file=sys.stderr)
        return 1
    sys.path.insert(0, str(ROOT))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(build(), indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
