"""Named search plans saved from the dashboard map (docs/map-and-plan-editing.md, section 7).

Each plan is one JSON file, ``<dir>/<name>.json``, where ``<dir>`` is
``FLIGHTRISK_PLANS_DIR`` or ``~/.flightrisk/plans``. A saved plan holds the
plan spec (pattern, area, lane spacing and edited paths), the map anchor and
the map view. It never holds a secret or a drone address, and loading one
sends nothing to any drone: the operator must still press Plan, which checks
it again.

Names are restricted to letters, digits, spaces, dots, dashes and
underscores (no path separator, no leading dot), so a name can never point
outside the plans directory. Files are written atomically and limited in
size, and everything read back is validated again.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flightrisk.drone.geo import PlanAnchor
from flightrisk.drone.plan_edit import PlanSpec, PlanSpecError

__all__ = [
    "MAX_PLANS",
    "MAX_PLAN_BYTES",
    "PLANS_DIR_ENV",
    "PlanStoreError",
    "list_plans",
    "load_plan",
    "plans_dir",
    "save_plan",
]

PLANS_DIR_ENV = "FLIGHTRISK_PLANS_DIR"
MAX_PLAN_BYTES = 256 * 1024
MAX_PLANS = 500
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-][A-Za-z0-9 _.\-]{0,63}$")
_VIEW_MODES = ("grid", "latlon")
_LAYER_RE = re.compile(r"^[a-z0-9-]{1,32}$")


class PlanStoreError(ValueError):
    """A plan cannot be saved or loaded; the message is for the operator."""


def plans_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Where plans are saved: ``FLIGHTRISK_PLANS_DIR``, else ``~/.flightrisk/plans``."""
    env = os.environ if environ is None else environ
    override = (env.get(PLANS_DIR_ENV) or "").strip()
    return Path(override).expanduser() if override else Path.home() / ".flightrisk" / "plans"


def _check_name(name: Any) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name) or name.strip() != name or name.endswith("."):
        raise PlanStoreError(
            "a plan name must be 1 to 64 letters, digits, spaces, dots, dashes or underscores, "
            "not starting with a dot or a space"
        )
    return name


def _path(name: str, base: Path | None) -> Path:
    root = plans_dir() if base is None else Path(base)
    path = root / f"{_check_name(name)}.json"
    # The name pattern already excludes separators; this is a second guard.
    if path.parent != root:
        raise PlanStoreError("invalid plan name")
    return path


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (low <= value <= high):
        raise PlanStoreError(f"view {name} must be a number within {low:g} to {high:g}")
    return float(value)


def _view(raw: Any) -> dict:
    """A validated map view: ``mode``, ``center`` ``[lat, lon]``, ``zoom`` and ``base_layer``, each optional."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise PlanStoreError("the map view must be an object")
    unknown = set(raw) - {"mode", "center", "zoom", "base_layer"}
    if unknown:
        raise PlanStoreError(f"unknown map view fields: {', '.join(sorted(map(str, unknown)))}")
    view: dict[str, Any] = {}
    if raw.get("mode") is not None:
        if raw["mode"] not in _VIEW_MODES:
            raise PlanStoreError("the map view mode must be grid or latlon")
        view["mode"] = raw["mode"]
    if raw.get("center") is not None:
        center = raw["center"]
        if not isinstance(center, (list, tuple)) or len(center) != 2:
            raise PlanStoreError("the map view center must be [lat, lon]")
        view["center"] = [_number(center[0], "latitude", -85, 85), _number(center[1], "longitude", -180, 180)]
    if raw.get("zoom") is not None:
        view["zoom"] = _number(raw["zoom"], "zoom", -4, 22)
    if raw.get("base_layer") is not None:
        layer = raw["base_layer"]
        if not isinstance(layer, str) or not _LAYER_RE.match(layer):
            raise PlanStoreError("the map view base layer must be a layer id")
        view["base_layer"] = layer
    return view


def _document(name: str, spec: Any, anchor: Any, view: Any) -> dict:
    """The validated JSON document of one saved plan."""
    try:
        parsed = PlanSpec.from_payload(spec)
    except PlanSpecError as exc:
        raise PlanStoreError(f"the plan is not valid: {exc}") from None
    doc: dict[str, Any] = {"name": name, "version": 1, "plan": parsed.to_dict()}
    if anchor is not None:
        try:
            doc["anchor"] = PlanAnchor.from_dict(anchor).to_dict()
        except ValueError as exc:
            raise PlanStoreError(f"the anchor is not valid: {exc}") from None
    else:
        doc["anchor"] = None
    doc["view"] = _view(view)
    return doc


def save_plan(name: Any, spec: Any, anchor: Any = None, view: Any = None, *, base: Path | None = None) -> dict:
    """Validate and save a plan under *name*, replacing a plan of the same name. Returns the document."""
    name = _check_name(name)
    doc = _document(name, spec, anchor, view)
    doc["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data = json.dumps(doc, indent=1, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(data) > MAX_PLAN_BYTES:
        raise PlanStoreError(f"the plan is {len(data)} bytes, above the {MAX_PLAN_BYTES} byte limit")
    path = _path(name, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() and len(list(path.parent.glob("*.json"))) >= MAX_PLANS:
        raise PlanStoreError(f"there are already {MAX_PLANS} saved plans; delete some first")
    fd, tmp = tempfile.mkstemp(prefix=".plan-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return doc


def load_plan(name: Any, *, base: Path | None = None) -> dict:
    """Read and validate the plan saved as *name*. Raises ``PlanStoreError``."""
    path = _path(_check_name(name), base)
    try:
        if path.is_symlink() or not path.is_file():
            raise PlanStoreError(f"no saved plan named {name!r}")
        if path.stat().st_size > MAX_PLAN_BYTES:
            raise PlanStoreError(f"the saved plan {name!r} is too large")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if isinstance(exc, PlanStoreError):
            raise
        raise PlanStoreError(f"the saved plan {name!r} cannot be read") from None
    if not isinstance(raw, Mapping):
        raise PlanStoreError(f"the saved plan {name!r} is not a plan")
    doc = _document(name, raw.get("plan"), raw.get("anchor"), raw.get("view"))
    saved_at = raw.get("saved_at")
    if isinstance(saved_at, str) and len(saved_at) <= 32:
        doc["saved_at"] = saved_at
    return doc


def list_plans(*, base: Path | None = None) -> list[dict]:
    """``[{"name", "saved_at"}]`` of every saved plan, newest first; unreadable files are skipped."""
    root = plans_dir() if base is None else Path(base)
    out = []
    try:
        files = sorted(root.glob("*.json"))
    except OSError:
        return []
    for path in files[:MAX_PLANS]:
        name = path.stem
        if not _NAME_RE.match(name):
            continue
        try:
            doc = load_plan(name, base=root)
        except PlanStoreError:
            continue
        out.append({"name": name, "saved_at": doc.get("saved_at")})
    out.sort(key=lambda entry: entry.get("saved_at") or "", reverse=True)
    return out
