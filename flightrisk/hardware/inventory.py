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
from flightrisk.hardware.ids import DRONE_ID
from flightrisk.params.custom import CustomModel, custom_model
from flightrisk.params.resolver import FACT_NOT_SETTABLE, ParamRefused, spec_for
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
#: The keys a ``link`` may hold (design detail 8.1); every value is text.
LINK_FIELDS = ("kind", "ssid", "host", "mavlink_address")
#: The keys a ``mounts`` entry may hold.
MOUNT_FIELDS = ("mission_pitch_deg",)
#: Parameter ids of every row that is a test-adapter fact for some profile.
FACT_ROW_IDS = frozenset(s.id for s in TABLE if any(e.value == "fact" for e in s.edit.values()))
#: Every fact row id, each dotted prefix and each segment: a refusal never quotes one of these.
FACT_ROW_PARTS = frozenset(
    part for fid in FACT_ROW_IDS for i in range(1, fid.count(".") + 2)
    for part in (".".join(fid.split(".")[:i]), fid.split(".")[i - 1]))
HIDDEN = "(not shown)"


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
        if not isinstance(model, str) or model not in BUILTIN_MODELS:
            model = DEFAULT_MODEL  # an edge-reported model the catalogue does not know is a Tello
        units.append({"unit_id": unit_id, "model": model, "available": True})
    return Inventory(0, {}, tuple(units), {"version": VERSION, "rev": 0, "models": {}, "units": units})


def _shown(value: Any) -> str:
    """``value`` quoted for a refusal, unless it is (part of) a fact row id, which is never named."""
    if isinstance(value, str) and value in FACT_ROW_PARTS:
        return HIDDEN
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return repr(value)
    return HIDDEN


def _check_unit_id(unit_id: Any) -> None:
    if not (isinstance(unit_id, str) and is_identifier(unit_id) and DRONE_ID.match(unit_id)):
        raise InventoryRefused(f"unit id {_shown(unit_id)} is not a valid drone id: letters, digits, '_' and '-', "
                               "starting with a letter or digit, at most 64 characters.")


def _secret_key(key: str) -> bool:
    return any(word in SECRET_WORDS for word in re.split(r"[^a-z0-9]+", key.lower()))


def _walk_secrets(value: Any, token: Optional[str], path: str = "") -> None:
    """INV-2: refuse a key named like a secret and any value equal to the edge token."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key) if str(key) not in FACT_ROW_PARTS else HIDDEN
            where = f"{path}.{name}" if path else name
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
                raise InventoryRefused(FACT_NOT_SETTABLE.format(detail=f"inventory.json unit {unit_id}"))
            _walk_facts(item, profile, unit_id)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_facts(item, profile, unit_id)


MAX_REV = 2**53


def _finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an integer too large for a float
        return False


def _check_pitch(unit_id: str, mount: str, pitch: float, model) -> None:
    """A declared mission pitch lies within the catalogue's pitch range of every camera on the mount."""
    for camera in (c for c in model.cameras if c.mount == mount):
        if camera.mount_kind == "fixed" or camera.pitch_min_deg is None or camera.pitch_max_deg is None:
            fixed = -90 if camera.orientation == "downward" else 0
            if pitch != fixed:
                raise InventoryRefused(f"unit {unit_id}: mount {mount!r} is fixed and looks at {fixed} degrees; "
                                       "its `mission_pitch_deg` must match.")
        elif not camera.pitch_min_deg <= pitch <= camera.pitch_max_deg:
            raise InventoryRefused(f"unit {unit_id}: mount {mount!r} `mission_pitch_deg` must be between "
                                   f"{camera.pitch_min_deg} and {camera.pitch_max_deg} degrees.")


def _unit_model(unit_id: str, model_id: Any, models: Mapping[str, CustomModel]):
    if isinstance(model_id, str) and model_id in BUILTIN_MODELS:
        return BUILTIN_MODELS[model_id], BUILTIN_MODELS[model_id].profile
    if isinstance(model_id, str) and model_id in models:
        return models[model_id].model, models[model_id].base.profile
    raise InventoryRefused(f"unit {unit_id}: model {_shown(model_id)} is neither built in nor defined in `models`.")


def _check_keys(unit_id: str, where: str, value: Any, allowed: Iterable[str]) -> None:
    """A closed object: ``value`` is an object and holds no key outside ``allowed``."""
    if not isinstance(value, Mapping):
        raise InventoryRefused(f"unit {unit_id}: `{where}` must be an object.")
    for key in value:
        if key not in allowed:
            raise InventoryRefused(f"unit {unit_id}: {_shown(key)} is not a field of `{where}`.")


def _check_unit(record: Any, models: Mapping[str, CustomModel]) -> dict:
    """One unit record against the closed schema of design detail 8.1; never stores a fact row."""
    if not isinstance(record, Mapping):
        raise InventoryRefused("a unit record must be an object.")
    unit_id = record.get("unit_id")
    _check_unit_id(unit_id)
    model, profile = _unit_model(unit_id, record.get("model"), models)
    _walk_facts(record, profile, unit_id)
    _check_keys(unit_id, "unit", record, UNIT_FIELDS)
    for name in ("name", "serial"):
        if name in record and not isinstance(record[name], str):
            raise InventoryRefused(f"unit {unit_id}: `{name}` must be text.")
    if "available" in record and not isinstance(record["available"], bool):
        raise InventoryRefused(f"unit {unit_id}: `available` must be true or false.")
    if "link" in record:
        link = record["link"]
        _check_keys(unit_id, "link", link, LINK_FIELDS)
        if link.get("kind") not in LINK_KINDS:
            raise InventoryRefused(f"unit {unit_id}: `link.kind` must be one of {', '.join(LINK_KINDS)}.")
        for key, text in link.items():
            if not isinstance(text, str):
                raise InventoryRefused(f"unit {unit_id}: `link.{key}` must be text.")
    features = record.get("features", {})
    _check_keys(unit_id, "features", features, FEATURES)
    for feature_id, wanted in features.items():
        if not isinstance(wanted, bool):
            raise InventoryRefused(f"unit {unit_id}: feature {feature_id} must be true or false.")
        if FEATURES[feature_id].safety_switch and not wanted:
            raise InventoryRefused(f"unit {unit_id}: {feature_id} is a safety switch and cannot be stored off.")
    mounts = record.get("mounts", {})
    _check_keys(unit_id, "mounts", mounts, {c.mount for c in model.cameras})
    for mount, entry in mounts.items():
        _check_keys(unit_id, f"mounts.{mount}", entry, MOUNT_FIELDS)
        if not _finite_number(entry.get("mission_pitch_deg")):
            raise InventoryRefused(f"unit {unit_id}: mount {mount!r} needs a numeric `mission_pitch_deg`.")
        _check_pitch(unit_id, mount, entry["mission_pitch_deg"], model)
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
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0 or rev > MAX_REV:
        raise InventoryRefused("the inventory `rev` must be a whole number, 0 or more.")
    _walk_secrets(data, edge_token)
    raw_models = data.get("models") or {}
    raw_units = data.get("units") or []
    if not isinstance(raw_models, Mapping) or not isinstance(raw_units, (list, tuple)):
        raise InventoryRefused("`models` must be an object and `units` a list.")
    models: dict[str, CustomModel] = {}
    for model_id, record in raw_models.items():
        if not isinstance(record, Mapping):
            raise InventoryRefused(f"model {_shown(model_id)} must be an object.")
        try:
            models[model_id] = custom_model(model_id, record)
        except ParamRefused as exc:
            raise InventoryRefused(str(exc)) from exc
        except (AttributeError, TypeError, KeyError, OverflowError, ValueError) as exc:  # a hand-edited field of the wrong type
            raise InventoryRefused(f"model {_shown(model_id)} has a field of the wrong type; "
                                   "see the inventory format.") from exc
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
