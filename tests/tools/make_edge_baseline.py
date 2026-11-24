"""Record the edge start-object baseline (Phase A3, steps S1 and S2).

The fixture ``tests/fixtures/edge_start_baseline.json`` freezes what today's
edge answers at the A3 base commit, so that later A3 pull requests can rewire
the readers to the resolver while ``tests/test_edge_start_baseline.py`` still
compares them with an independent record.

The generator imports the real runtime modules and calls the real functions. It
never reads a value from ``flightrisk/params/table.py`` or from the golden file.
Like the golden generator, it refuses to overwrite an existing output file, so
nothing can silently re-baseline the record.

Run: ``python tests/tools/make_edge_baseline.py [output]``
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import importlib
import inspect
import json
import math
import sys
from pathlib import Path
from typing import Any

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

_SCALARS = (bool, int, float, str, type(None))


def jsonable(value: Any) -> Any:
    """``value`` as plain JSON data; non-finite floats and tuples keep their identity as tagged forms."""
    if isinstance(value, enum.Enum):
        return {"__enum__": f"{type(value).__name__}.{value.name}"}
    if isinstance(value, float) and not math.isfinite(value):
        return {"__float__": repr(value)}
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, (tuple, list)):
        return {"__seq__": [jsonable(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        return {"__set__": sorted((jsonable(item) for item in value), key=json.dumps)}
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if hasattr(value, "__dict__") and not inspect.isroutine(value) and not isinstance(value, type):
        return {name: jsonable(item) for name, item in sorted(vars(value).items())
                if not name.startswith("__") and name not in VOLATILE_ATTRS}
    return {"__repr__": f"{type(value).__name__}"}


def _is_constant_name(name: str) -> bool:
    return name.isupper() or (name.startswith("_") and name[1:].isupper())


def module_constants(module_name: str) -> dict[str, Any]:
    """Every upper-case scalar, tuple or limits constant of one module, and the constructor defaults of its classes."""
    module = importlib.import_module(module_name)
    out: dict[str, Any] = {}
    for name, value in sorted(vars(module).items()):
        if _is_constant_name(name) and (isinstance(value, _SCALARS + (tuple, frozenset, dict)) or dataclasses.is_dataclass(value)):
            out[name] = jsonable(value)
    classes: dict[str, Any] = {}
    for name, cls in sorted(vars(module).items()):
        if not inspect.isclass(cls) or cls.__module__ != module_name:
            continue
        entry: dict[str, Any] = {}
        defaults = {}
        try:
            for param in inspect.signature(cls.__init__).parameters.values():
                if param.default is not inspect.Parameter.empty:
                    defaults[param.name] = jsonable(param.default)
        except (TypeError, ValueError):
            pass
        if defaults:
            entry["init_defaults"] = defaults
        attrs = {attr: jsonable(value) for attr, value in sorted(vars(cls).items())
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
        name: jsonable(value) for name, value in sorted(vars(safety).items())
        if not name.startswith("__") and (name.isupper() or name.endswith("_LIMITS"))
        and isinstance(value, _SCALARS + (tuple, dict)) or dataclasses.is_dataclass(value) and not isinstance(value, type)
    }
    lane_defaults = {
        param.name: jsonable(param.default)
        for param in inspect.signature(LaneExecutor.__init__).parameters.values()
        if param.default is not inspect.Parameter.empty
    }
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
    }


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
    return {"start_objects": start_objects(), "probes": probe_results()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.output.exists():
        print(f"refusing to overwrite {args.output}", file=sys.stderr)
        return 1
    sys.path.insert(0, str(ROOT))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(build(), indent=1, sort_keys=True) + "\n")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
