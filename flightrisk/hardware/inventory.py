"""The drone inventory: which drones the user has, as ``inventory.json`` (INV-1 to INV-4, INV-6, INV-7).

The file holds units (a drone each), optional custom models and a revision. It never holds a
secret (INV-2), never a parameter value of a fact row (design 7.5), and a custom model is checked
by ``flightrisk.params.custom`` so its bounds and defaults stay inside the resolver's limits and it
can never set ``environment`` or ``indoor_profile_measured`` (CAP-5).

Without the file the inventory is implicit: every drone is a unit of model ``tello`` (INV-4).
Writes carry the ``rev`` they read; a mismatch is refused (INV-7).
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

from flightrisk.fleet_protocol.checks import is_identifier
from flightrisk.hardware.catalogue import BUILTIN_MODELS
from flightrisk.hardware.features import FEATURES
from flightrisk.launcher.settings import _DRONE_ID
from flightrisk.params.custom import CustomModel, custom_model
from flightrisk.params.resolver import ParamRefused, spec_for
from flightrisk.params.table import TABLE

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt

VERSION = 1
ENV_FILE = "FLIGHTRISK_INVENTORY_FILE"
DEFAULT_MODEL = "tello"
STALE_REV = "the inventory changed since you loaded it; reload and try again"
SECRET_WORDS = frozenset({"password", "passphrase", "secret", "token", "key"})
TOP_FIELDS = ("version", "rev", "models", "units")
UNIT_FIELDS = ("unit_id", "name", "model", "link", "serial", "available", "features", "mounts")
LINK_KINDS = ("wifi_ap", "wifi_station", "radio")
#: Parameter ids of every row that is a test-adapter fact for some profile.
FACT_ROW_IDS = frozenset(s.id for s in TABLE if any(e.value == "fact" for e in s.edit.values()))


class InventoryRefused(ValueError):
    """The inventory file or a write to it is refused; the text is the operator's message."""


@dataclass(frozen=True)
class Inventory:
    """A loaded inventory: its revision, custom models and units (plain validated records)."""

    rev: int = 0
    models: Mapping[str, CustomModel] = field(default_factory=dict)
    units: tuple = ()
    data: Mapping[str, Any] = field(default_factory=dict)

    def unit(self, unit_id: str) -> Optional[Mapping[str, Any]]:
        return next((u for u in self.units if u["unit_id"] == unit_id), None)

    def available_units(self) -> tuple:
        return tuple(u for u in self.units if u.get("available", True))


def inventory_path(path: Union[str, Path, None] = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(ENV_FILE)
    return Path(env) if env else Path.home() / ".flightrisk" / "inventory.json"


def implicit_inventory(unit_ids: Iterable[str], reported_models: Mapping[str, str] = None) -> Inventory:
    """INV-4: without a file, every drone is a ``tello`` unit, or the model its edge reported."""
    reported = reported_models or {}
    units = []
    for unit_id in unit_ids:
        _check_unit_id(unit_id)
        model = reported.get(unit_id, DEFAULT_MODEL)
        units.append({"unit_id": unit_id, "model": model, "available": True})
    return Inventory(0, {}, tuple(units), {"version": VERSION, "rev": 0, "models": {}, "units": units})


def _check_unit_id(unit_id: Any) -> None:
    if not (is_identifier(unit_id) and _DRONE_ID.match(unit_id)):
        raise InventoryRefused(f"unit id {unit_id!r} is not a valid drone id: letters, digits, '_' and '-', "
                               "starting with a letter or digit, at most 64 characters.")


def _secret_key(key: str) -> bool:
    return any(word in SECRET_WORDS for word in re.split(r"[^a-z0-9]+", key.lower()))


def _walk_secrets(value: Any, token: Optional[str], path: str = "") -> None:
    """INV-2: refuse a key named like a secret and any value equal to the edge token."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            where = f"{path}.{key}" if path else str(key)
            if _secret_key(str(key)):
                raise InventoryRefused(f"`{where}` is named like a secret; the inventory never stores one.")
            _walk_secrets(item, token, where)
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _walk_secrets(item, token, f"{path}[{i}]")
    elif token and isinstance(value, str) and value == token:
        raise InventoryRefused(f"`{path}` holds the edge token; the inventory never stores one.")


def _walk_facts(value: Any, profile: str, unit_id: str) -> None:
    """Design 7.5: a unit never stores a parameter value of a row that is a fact for its profile."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            spec = spec_for(key) if isinstance(key, str) else None
            if spec is not None and (spec.is_fact(profile) or key in FACT_ROW_IDS):
                raise InventoryRefused(f"unit {unit_id}: `{key}` is not a setting and cannot be stored.")
            _walk_facts(item, profile, unit_id)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_facts(item, profile, unit_id)


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_unit(record: Any, models: Mapping[str, CustomModel]) -> dict:
    if not isinstance(record, Mapping):
        raise InventoryRefused("a unit record must be an object.")
    unit_id = record.get("unit_id")
    _check_unit_id(unit_id)
    model_id = record.get("model")
    if model_id in BUILTIN_MODELS:
        profile = BUILTIN_MODELS[model_id].profile
    elif model_id in models:
        profile = models[model_id].base.profile
    else:
        raise InventoryRefused(f"unit {unit_id}: model {model_id!r} is neither built in nor defined in `models`.")
    _walk_facts(record, profile, unit_id)
    for name in record:
        if name not in UNIT_FIELDS:
            raise InventoryRefused(f"unit {unit_id}: `{name}` is not a field of a unit.")
    for name in ("name", "serial"):
        if name in record and not isinstance(record[name], str):
            raise InventoryRefused(f"unit {unit_id}: `{name}` must be text.")
    if "available" in record and not isinstance(record["available"], bool):
        raise InventoryRefused(f"unit {unit_id}: `available` must be true or false.")
    link = record.get("link")
    if link is not None and (not isinstance(link, Mapping) or link.get("kind") not in LINK_KINDS):
        raise InventoryRefused(f"unit {unit_id}: `link.kind` must be one of {', '.join(LINK_KINDS)}.")
    for feature_id, wanted in (record.get("features") or {}).items():
        feature = FEATURES.get(feature_id)
        if feature is None:
            raise InventoryRefused(f"unit {unit_id}: unknown feature {feature_id!r}.")
        if not isinstance(wanted, bool):
            raise InventoryRefused(f"unit {unit_id}: feature {feature_id} must be true or false.")
        if feature.safety_switch and not wanted:
            raise InventoryRefused(f"unit {unit_id}: {feature_id} is a safety switch and cannot be stored off.")
    for mount, entry in (record.get("mounts") or {}).items():
        pitch = entry.get("mission_pitch_deg") if isinstance(entry, Mapping) else None
        if not _finite_number(pitch):
            raise InventoryRefused(f"unit {unit_id}: mount {mount!r} needs a numeric `mission_pitch_deg`.")
    return dict(record)


def parse_inventory(data: Any, *, edge_token: Optional[str] = None) -> Inventory:
    """Validate a decoded ``inventory.json``; raises ``InventoryRefused`` with the operator's message."""
    if not isinstance(data, Mapping):
        raise InventoryRefused("the inventory must be a JSON object.")
    for name in data:
        if name not in TOP_FIELDS:
            raise InventoryRefused(f"`{name}` is not a field of the inventory.")
    if data.get("version") != VERSION or isinstance(data.get("version"), bool):
        raise InventoryRefused(f"the inventory version must be {VERSION}.")
    rev = data.get("rev", 0)
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
        raise InventoryRefused("the inventory `rev` must be a whole number, 0 or more.")
    _walk_secrets(data, edge_token)
    raw_models = data.get("models") or {}
    raw_units = data.get("units") or []
    if not isinstance(raw_models, Mapping) or not isinstance(raw_units, (list, tuple)):
        raise InventoryRefused("`models` must be an object and `units` a list.")
    models: dict[str, CustomModel] = {}
    for model_id, record in raw_models.items():
        if not isinstance(record, Mapping):
            raise InventoryRefused(f"model {model_id!r} must be an object.")
        try:
            models[model_id] = custom_model(model_id, record)
        except ParamRefused as exc:
            raise InventoryRefused(str(exc)) from exc
    units, seen = [], set()
    for record in raw_units:
        unit = _check_unit(record, models)
        if unit["unit_id"] in seen:
            raise InventoryRefused(f"unit id {unit['unit_id']!r} appears twice.")
        seen.add(unit["unit_id"])
        units.append(unit)
    clean = {"version": VERSION, "rev": rev, "models": dict(raw_models), "units": units}
    return Inventory(rev, models, tuple(units), clean)


class _FileLock:
    """An exclusive lock on a file beside the inventory, held across processes and threads."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> "_FileLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        if fcntl is not None:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows
            msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self._fd)


class InventoryStore:
    """Reads and writes one ``inventory.json`` with compare-and-swap on ``rev`` (INV-1, INV-7)."""

    def __init__(self, path: Union[str, Path, None] = None, *, edge_token: Optional[str] = None) -> None:
        self.path = inventory_path(path)
        self._token = edge_token

    def load(self) -> Optional[Inventory]:
        """The inventory, or None when the file does not exist. An unreadable file is refused."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise InventoryRefused(f"{self.path} exists but cannot be read ({exc}); fix or move it. "
                                   "It was not replaced.") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise InventoryRefused(f"{self.path} exists but is not valid JSON ({exc}); fix or move it. "
                                   "It was not replaced.") from exc
        return parse_inventory(data, edge_token=self._token)

    def write(self, data: Mapping[str, Any], rev: int) -> Inventory:
        """Write ``data`` (``models`` and ``units``) if ``rev`` is still current; returns the new inventory."""
        with _FileLock(self.path.with_name(self.path.name + ".lock")):
            current = self.load()
            current_rev = current.rev if current is not None else 0
            if rev != current_rev:
                raise InventoryRefused(STALE_REV)
            candidate = {**{k: v for k, v in data.items() if k not in ("version", "rev")},
                         "version": VERSION, "rev": current_rev + 1}
            parsed = parse_inventory(candidate, edge_token=self._token)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(parsed.data, handle, indent=2, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return parsed
