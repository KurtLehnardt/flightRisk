"""What an edge resolves at start, before it contacts its drone.

This module picks the edge's model and checks it against the backend the edge
runs (hardware-profiles detail 7.3, SEAM-3, EDGE-6). The model comes from the
first of these that names one:

1. ``--model``, else ``FLIGHTRISK_MODEL``;
2. ``--airframe``;
3. the inventory unit for ``--drone-id`` (``FLIGHTRISK_INVENTORY_FILE``);
4. the launcher layer's ``model`` (``FLIGHTRISK_LAUNCHER_PARAMS``);
5. the backend's default: ``tello`` and ``fake`` give ``tello``, ``mavlink``
   gives ``generic_multirotor``.

The edge refuses to start when the model's scale, adapter or frame path
differs from its backend's, so a Tello can never fly with outdoor bounds. With
none of the inputs above, the model is the backend's default, which always
matches, so an existing invocation starts exactly as before.

Refusal texts name model ids, backends, scales, adapters and frame paths only.
They never name a parameter value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from flightrisk.hardware.catalogue import BUILTIN_MODELS, DroneModel

MODEL_ENV_VAR = "FLIGHTRISK_MODEL"
LAUNCHER_PARAMS_ENV_VAR = "FLIGHTRISK_LAUNCHER_PARAMS"

#: The model each edge drone kind (``--drone``) flies when nothing names one.
BACKEND_DEFAULT_MODELS: Mapping[str, str] = {
    "tello": "tello",
    "fake": "tello",
    "mavlink": "generic_multirotor",
}

#: The registered backend each edge drone kind runs. ``fake`` flies the
#: in-process simulated Tello, so it speaks the Tello backend's adapter.
_BACKEND_NAMES: Mapping[str, str] = {
    "tello": "tello",
    "fake": "tello",
    "mavlink": "mavlink",
}

# Where the chosen model came from, in the order the edge looks.
SOURCE_MODEL_FLAG = "--model"
SOURCE_MODEL_ENV = MODEL_ENV_VAR
SOURCE_AIRFRAME_FLAG = "--airframe"
SOURCE_INVENTORY = "inventory"
SOURCE_LAUNCHER = "launcher"
SOURCE_BACKEND_DEFAULT = "backend default"


class ModelRefused(ValueError):
    """The edge must not start with this model; the text is the operator's message."""


@dataclass(frozen=True)
class BackendProfile:
    """What the edge actually runs: its drone kind, adapter, scale and frame path."""

    drone: str
    adapter: str
    scale: str
    frame_path: str


@dataclass(frozen=True)
class ModelChoice:
    """The model the edge flies and where that choice came from."""

    model_id: str
    source: str
    model: DroneModel


def backend_profile(drone: str, frame_path: str) -> BackendProfile:
    """The profile of edge drone kind ``drone`` taking its frames through ``frame_path``.

    The adapter is the registered backend's; the scale is that of the
    backend's default model.
    """
    from flightrisk.drone import registry  # Lazy: the registry bootstraps every backend.

    spec = registry.get(_BACKEND_NAMES[drone])
    default = BUILTIN_MODELS[BACKEND_DEFAULT_MODELS[drone]]
    return BackendProfile(drone=drone, adapter=spec.adapter, scale=default.scale, frame_path=frame_path)


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def launcher_model(environ: Mapping[str, str]) -> Optional[str]:
    """The ``model`` of the launcher layer, or None when the launcher set none.

    Raises ``ModelRefused`` when the layer is not a JSON object or its model is
    not a string, because the edge cannot know what it flies then.
    """
    raw = _clean(environ.get(LAUNCHER_PARAMS_ENV_VAR))
    if raw is None:
        return None
    try:
        layer = json.loads(raw)
    except ValueError as exc:
        raise ModelRefused(f"{LAUNCHER_PARAMS_ENV_VAR} is not valid JSON ({exc}); "
                           "start the edge from the launcher again or unset it.") from exc
    if not isinstance(layer, dict):
        raise ModelRefused(f"{LAUNCHER_PARAMS_ENV_VAR} must be a JSON object; start the edge from the "
                           "launcher again or unset it.")
    model = layer.get("model")
    if model is None:
        return None
    if not isinstance(model, str) or not model.strip():
        raise ModelRefused(f"{LAUNCHER_PARAMS_ENV_VAR} has a model that is not a model id; start the edge "
                           "from the launcher again or unset it.")
    return model.strip()


def _load_inventory(environ: Mapping[str, str]) -> Any:
    """The inventory, or None when there is none. Raises ``ModelRefused`` when it is refused."""
    from flightrisk.hardware.inventory import ENV_FILE, InventoryRefused, InventoryStore

    try:
        return InventoryStore(environ.get(ENV_FILE) or None).load()
    except InventoryRefused as exc:
        raise ModelRefused(f"the inventory is refused: {exc}") from exc


def choose_model(
    *,
    drone: str,
    drone_id: str,
    model_flag: Optional[str] = None,
    airframe_flag: Optional[str] = None,
    environ: Mapping[str, str],
    load_inventory: Optional[Callable[[], Any]] = None,
) -> ModelChoice:
    """Pick the model an edge flying ``drone`` uses, in the order of detail 7.3.

    ``load_inventory`` returns the inventory or None; by default it reads
    ``FLIGHTRISK_INVENTORY_FILE`` (or ``~/.flightrisk/inventory.json``), and
    only when a step needs it. Raises ``ModelRefused`` when the named model is
    neither built in nor defined in the inventory, or when a layer is refused.
    """
    if drone not in BACKEND_DEFAULT_MODELS:
        raise ValueError(f"unknown edge drone {drone!r}")
    loader = load_inventory or (lambda: _load_inventory(environ))
    loaded: list = []

    def inventory() -> Any:
        if not loaded:
            loaded.append(loader())
        return loaded[0]

    model_id, source = None, None
    for candidate, where in (
        (_clean(model_flag), SOURCE_MODEL_FLAG),
        (_clean(environ.get(MODEL_ENV_VAR)), SOURCE_MODEL_ENV),
        (_clean(airframe_flag), SOURCE_AIRFRAME_FLAG),
    ):
        if candidate is not None:
            model_id, source = candidate, where
            break
    if model_id is None:
        inv = inventory()
        unit = inv.unit(drone_id) if inv is not None else None
        if unit is not None and _clean(unit.get("model")) is not None:
            model_id, source = str(unit["model"]).strip(), SOURCE_INVENTORY
    if model_id is None:
        named = launcher_model(environ)
        if named is not None:
            model_id, source = named, SOURCE_LAUNCHER
    if model_id is None:
        model_id, source = BACKEND_DEFAULT_MODELS[drone], SOURCE_BACKEND_DEFAULT

    model = BUILTIN_MODELS.get(model_id)
    if model is None:
        inv = inventory()
        custom = inv.models.get(model_id) if inv is not None else None
        if custom is None:
            raise ModelRefused(f"model {model_id!r} (from {source}) is neither built in nor defined in the "
                               f"inventory; built-in models are {', '.join(sorted(BUILTIN_MODELS))}.")
        model = custom.model
    return ModelChoice(model_id=model_id, source=source, model=model)


def check_model_against_backend(choice: ModelChoice, backend: BackendProfile) -> None:
    """EDGE-6 and SEAM-3: refuse when the model's scale, adapter or frame path differs from the backend's.

    Raises ``ModelRefused`` naming the model, the backend and every field that
    differs, with both values.
    """
    model = choice.model
    differences = [
        f"{label} {theirs} (the model) is not {ours} (the backend)"
        for label, theirs, ours in (
            ("scale", model.scale, backend.scale),
            ("adapter", model.adapter, backend.adapter),
            ("frame path", model.frame_path, backend.frame_path),
        )
        if theirs != ours
    ]
    if differences:
        raise ModelRefused(
            f"model {choice.model_id} (from {choice.source}) does not match --drone {backend.drone}: "
            + "; ".join(differences)
            + ". Name a model this backend flies, or run the backend that flies this model."
        )


def resolve_model(
    *,
    drone: str,
    drone_id: str,
    frame_path: str,
    model_flag: Optional[str] = None,
    airframe_flag: Optional[str] = None,
    environ: Mapping[str, str],
    load_inventory: Optional[Callable[[], Any]] = None,
) -> ModelChoice:
    """Choose the model and check it against the backend. Raises ``ModelRefused``."""
    choice = choose_model(
        drone=drone, drone_id=drone_id, model_flag=model_flag, airframe_flag=airframe_flag,
        environ=environ, load_inventory=load_inventory,
    )
    check_model_against_backend(choice, backend_profile(drone, frame_path))
    return choice
