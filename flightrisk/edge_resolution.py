"""What an edge resolves at start, before it contacts its drone.

This module picks the edge's model and checks it against the backend the edge
runs (hardware-profiles detail 7.3, SEAM-3, EDGE-6). The model comes from the
first of these that names one:

1. ``--model``, else ``FLIGHTRISK_MODEL``;
2. ``--airframe``, else ``FLIGHTRISK_AIRFRAME``;
3. the inventory unit for ``--drone-id`` (``FLIGHTRISK_INVENTORY_FILE``);
4. the launcher layer's ``model`` (``FLIGHTRISK_LAUNCHER_PARAMS``);
5. the backend's default: the first model its registered backend declares
   (``BackendSpec.models``). ``fake`` runs the registry's default backend.

The edge refuses to start when the model's scale, adapter or frame path
differs from its backend's, so a Tello can never fly with outdoor bounds. With
none of the inputs above, the model is the backend's default, which always
matches, so an existing invocation starts exactly as before.

Refusal texts name model ids, backends, scales, adapters and frame paths only.
They never name a parameter value.

``build_layers`` then reads the layers the resolver takes for this unit (RES-1,
EDGE-4, RES-11): the profile (the chosen model), the params file
(``FLIGHTRISK_PARAMS_FILE``, else ``~/.flightrisk/params.json``), the
inventory unit's feature switches (``FLIGHTRISK_INVENTORY_FILE``), the
launcher layer (``FLIGHTRISK_LAUNCHER_PARAMS``, with its ``confirmed``
object), a copy of the environment and the flight flags of the command line,
plus the ``--confirm-less-safe`` confirmations.

``resolve_edge`` resolves them at start, before the edge contacts its drone
(EDGE-1): it calls ``resolve_unit`` (``simulated`` only for the fake adapter,
CAP-6, and today's ranges from ``params/today.py``, so an environment or
flag value of a name today's code reads keeps today's range), runs the static feature gates (``resolve_features``, FEA-7 static
part, FEA-10) and builds the start report (``build_report``). Any refusal
raises ``EdgeStartRefused``, whose text is the operator's exit message. The
resolved values are kept in ``EdgeResolution``; nothing reads them yet, so a
start that is not refused flies exactly as before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from flightrisk.hardware.catalogue import BUILTIN_MODELS, DroneModel
from flightrisk.params.today import TODAY_ACCEPTS

MODEL_ENV_VAR = "FLIGHTRISK_MODEL"
AIRFRAME_ENV_VAR = "FLIGHTRISK_AIRFRAME"
LAUNCHER_PARAMS_ENV_VAR = "FLIGHTRISK_LAUNCHER_PARAMS"
PARAMS_FILE_ENV_VAR = "FLIGHTRISK_PARAMS_FILE"
#: Where the params file is when ``FLIGHTRISK_PARAMS_FILE`` does not say (design detail 10.1).
DEFAULT_PARAMS_FILE = Path("~/.flightrisk/params.json")
#: The keys the launcher layer may hold (design detail 12.1).
LAUNCHER_KEYS = ("unit_id", "model", "values", "features", "confirmed")
CONFIRM_FLAG = "--confirm-less-safe"

#: The edge drone kind (``--drone fake``) that flies an in-process simulation.
#: It names no backend, so it runs the registry's default backend and speaks
#: that backend's adapter.
FAKE_DRONE = "fake"

# Where the chosen model came from, in the order the edge looks.
SOURCE_MODEL_FLAG = "--model"
SOURCE_MODEL_ENV = MODEL_ENV_VAR
SOURCE_AIRFRAME_FLAG = "--airframe"
SOURCE_AIRFRAME_ENV = AIRFRAME_ENV_VAR
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


def _backend_spec(drone: str) -> Any:
    """The registered backend that edge drone kind ``drone`` runs.

    ``fake`` runs the registry's default backend. Any other kind is the name
    of a registered backend that declares the catalogue models it flies.
    Raises ``ValueError`` for any other kind.
    """
    from flightrisk.drone import registry  # Lazy: the registry bootstraps every backend.

    name = registry.DEFAULT_BACKEND if drone == FAKE_DRONE else drone
    if not registry.is_registered(name) or not registry.get(name).models:
        raise ValueError(f"unknown edge drone {drone!r}")
    return registry.get(name)


def backend_default_model(drone: str) -> str:
    """The model edge drone kind ``drone`` flies when nothing names one.

    It is the first model the drone's registered backend declares.
    """
    return _backend_spec(drone).models[0]


def backend_profile(drone: str, frame_path: str) -> BackendProfile:
    """The profile of edge drone kind ``drone`` taking its frames through ``frame_path``.

    The adapter is the registered backend's; the scale is that of the
    backend's default model.
    """
    spec = _backend_spec(drone)
    default = BUILTIN_MODELS[spec.models[0]]
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
    except (ValueError, RecursionError) as exc:
        # A deeply nested value raises RecursionError; it is refused as invalid JSON too (review L1).
        raise ModelRefused(f"{LAUNCHER_PARAMS_ENV_VAR} is not valid JSON ({type(exc).__name__}); "
                           "start the edge from the launcher again or unset it.") from None
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
    except RecursionError:
        raise ModelRefused("the inventory is refused: it is nested too deeply to read.") from None


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
    default_model_id = backend_default_model(drone)  # Raises ValueError for an unknown edge drone.
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
        (_clean(environ.get(AIRFRAME_ENV_VAR)), SOURCE_AIRFRAME_ENV),
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
        model_id, source = default_model_id, SOURCE_BACKEND_DEFAULT

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


@dataclass(frozen=True)
class LauncherLayer:
    """The checked ``FLIGHTRISK_LAUNCHER_PARAMS`` object (design detail 12.1)."""

    unit_id: Optional[str] = None
    model: Optional[str] = None
    values: Mapping[str, Any] = field(default_factory=dict)
    features: Mapping[str, bool] = field(default_factory=dict)
    confirmed: Mapping[str, Any] = field(default_factory=dict)

    def as_resolver_layer(self) -> dict[str, Any]:
        """The layer in the form the resolver takes: ``{id: value}`` plus the reserved ``confirmed`` key."""
        from flightrisk.params.resolver import LAUNCHER_CONFIRMED

        layer = dict(self.values)
        layer[LAUNCHER_CONFIRMED] = dict(self.confirmed)
        return layer


@dataclass(frozen=True)
class EdgeLayers:
    """Everything ``resolve_unit`` takes for one edge unit, read but not resolved.

    ``profile`` is what ``resolve_unit`` takes as its model: the built-in
    model id, or the inventory's custom model with its narrowed bounds.
    ``layers`` maps ``Source.FILE``, ``Source.INVENTORY``, ``Source.ENV`` and
    ``Source.CLI``, and ``Source.LAUNCHER`` only when the launcher set
    ``FLIGHTRISK_LAUNCHER_PARAMS``: with a launcher layer only its
    ``confirmed`` object counts, and without one the ``--confirm-less-safe``
    flags in ``confirmations`` do (RES-11). ``features`` are the inventory
    unit's feature switches; ``launcher`` is the checked launcher layer, or
    None.
    """

    choice: ModelChoice
    profile: Any
    unit_id: str
    layers: Mapping[Any, Mapping[str, Any]]
    confirmations: Mapping[str, str]
    features: Mapping[str, bool]
    launcher: Optional[LauncherLayer]


def _launcher_refused(text: str):
    from flightrisk.params.resolver import ParamRefused
    from flightrisk.params.spec import Source

    return ParamRefused(f"{LAUNCHER_PARAMS_ENV_VAR} {text} Refusing to start.", source=Source.LAUNCHER)


def _known_ids(where: str, values: Any) -> dict[str, Any]:
    """``values`` when it is an object of known parameter ids; raises ``ParamRefused`` otherwise."""
    from flightrisk.params.resolver import spec_for

    if not isinstance(values, dict):
        raise _launcher_refused(f"`{where}` must be an object of parameter values.")
    unknown = sorted(str(k) for k in values if spec_for(k) is None)
    if unknown:
        raise _launcher_refused(f"`{where}` names {', '.join(unknown)}, which is not a known setting.")
    return dict(values)


def parse_launcher_layer(environ: Mapping[str, str]) -> Optional[LauncherLayer]:
    """The launcher layer of ``FLIGHTRISK_LAUNCHER_PARAMS``, or None when the variable is unset or empty (EDGE-4).

    Raises ``ParamRefused`` when the variable is not a JSON object, holds a key
    other than those of ``LAUNCHER_KEYS``, or names a parameter id or feature
    that does not exist. Values are checked by the resolver, which refuses a
    test-adapter fact without naming it (FACT-3).
    """
    from flightrisk.hardware.features import FEATURES

    raw = _clean(environ.get(LAUNCHER_PARAMS_ENV_VAR))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise _launcher_refused(f"is not valid JSON ({type(exc).__name__}); start the edge from the launcher "
                                "again or unset it.") from None
    if not isinstance(data, dict):
        raise _launcher_refused("must be a JSON object; start the edge from the launcher again or unset it.")
    extra = sorted(str(k) for k in data if k not in LAUNCHER_KEYS)
    if extra:
        raise _launcher_refused(f"has unknown keys {', '.join(extra)}.")
    for key in ("unit_id", "model"):
        # A key that is present must hold text: a null unit_id would switch off the unit check (review L2).
        if key in data and (not isinstance(data[key], str) or not data[key].strip()):
            raise _launcher_refused(f"has a `{key}` that is not text.")
    for key in ("values", "confirmed"):
        if key in data and not isinstance(data[key], dict):
            raise _launcher_refused(f"`{key}` must be an object of parameter ids.")
    features = data.get("features", {})
    if not isinstance(features, dict):
        raise _launcher_refused("`features` must be an object of feature switches.")
    unknown = sorted(str(k) for k in features if k not in FEATURES)
    if unknown:
        raise _launcher_refused(f"`features` names {', '.join(unknown)}, which is not a known feature.")
    if any(not isinstance(v, bool) for v in features.values()):
        raise _launcher_refused("`features` values must be true or false.")
    return LauncherLayer(
        unit_id=_clean(data.get("unit_id")),
        model=_clean(data.get("model")),
        values=MappingProxyType(_known_ids("values", data.get("values") or {})),
        features=MappingProxyType(dict(features)),
        confirmed=MappingProxyType(_known_ids("confirmed", data.get("confirmed") or {})),
    )


def params_file_path(environ: Mapping[str, str]) -> Path:
    """``FLIGHTRISK_PARAMS_FILE``, else ``~/.flightrisk/params.json``."""
    named = _clean(environ.get(PARAMS_FILE_ENV_VAR))
    return Path(named).expanduser() if named else DEFAULT_PARAMS_FILE.expanduser()


def _option_of(token: str, options: Sequence[str], allow_abbrev: bool) -> Optional[str]:
    """The option ``token`` names, as argparse matches it: exactly, else by a unique prefix."""
    if token in options:
        return token
    if allow_abbrev:
        matches = [o for o in options if o.startswith(token)]
        if len(matches) == 1:
            return matches[0]
    return None


def command_line_layer(argv: Iterable[str], parser: Any) -> tuple[dict[str, str], list[str]]:
    """The command-line layer ``{flag: text}`` and the ``--confirm-less-safe`` values of ``argv``.

    ``parser`` is the edge's parser, which already accepted ``argv``; it tells
    which options take a value and how an abbreviation expands. Only flags the
    resolver reads are kept, each as the text given (the last one wins, as
    argparse stores it), so a default is never taken for a value.
    """
    from flightrisk.params.table import TABLE

    flags = {flag for spec in TABLE for flag in spec.cli}
    takes_value = {o: a.nargs != 0 for a in parser._actions for o in a.option_strings}
    options = sorted(takes_value)
    allow_abbrev = getattr(parser, "allow_abbrev", True)
    layer: dict[str, str] = {}
    confirms: list[str] = []
    tokens = list(argv)
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if token == "--":
            break
        if not token.startswith("-") or token == "-":
            continue
        name, sep, explicit = token.partition("=")
        option = _option_of(name, options, allow_abbrev and name.startswith("--"))
        if option is None or not takes_value[option]:
            continue
        if sep:
            value = explicit
        elif i < len(tokens):
            value = tokens[i]
            i += 1
        else:
            continue
        if option == CONFIRM_FLAG:
            confirms.append(value)
        elif option in flags:
            layer[option] = value
    return layer, confirms


def build_layers(
    *,
    choice: ModelChoice,
    drone_id: str,
    environ: Mapping[str, str],
    argv: Iterable[str] = (),
    parser: Any = None,
    load_inventory: Optional[Callable[[], Any]] = None,
) -> EdgeLayers:
    """Read every layer of the unit ``drone_id`` flying ``choice``, in the resolver's precedence.

    Raises ``ParamRefused`` when the params file, the launcher layer or a
    ``--confirm-less-safe`` flag is refused, and ``ModelRefused`` when the
    inventory is. ``parser`` defaults to the edge's own parser.
    """
    from flightrisk.params.files import load_params_file
    from flightrisk.params.resolver import parse_confirm_flags
    from flightrisk.params.spec import Source

    if parser is None:
        from flightrisk.edge_device import _build_parser  # Lazy: edge_device is a large import.

        parser = _build_parser()
    params_file = load_params_file(params_file_path(environ)).layer_for(choice.model_id, drone_id)
    inv = (load_inventory or (lambda: _load_inventory(environ)))()
    unit = inv.unit(drone_id) if inv is not None else None
    profile: Any = choice.model_id
    if choice.model_id not in BUILTIN_MODELS:
        profile = inv.models[choice.model_id]
    launcher = parse_launcher_layer(environ)
    cli, confirm_values = command_line_layer(argv, parser)
    layers: dict[Any, Mapping[str, Any]] = {
        Source.FILE: params_file,
        # The inventory's unit schema stores no parameter values: it gives features and mounts.
        Source.INVENTORY: {},
        Source.ENV: dict(environ),
        Source.CLI: cli,
    }
    if launcher is not None:
        layers[Source.LAUNCHER] = launcher.as_resolver_layer()
    return EdgeLayers(
        choice=choice,
        profile=profile,
        unit_id=drone_id,
        layers=MappingProxyType({s: layers[s] for s in _ordered(layers)}),
        confirmations=MappingProxyType(parse_confirm_flags(confirm_values)),
        features=MappingProxyType(dict((unit or {}).get("features") or {})),
        launcher=launcher,
    )


def _ordered(layers: Mapping[Any, Any]) -> list:
    from flightrisk.params.resolver import CALLER_LAYERS

    return [s for s in CALLER_LAYERS if s in layers]


#: The drone kind whose adapter is simulated (CAP-6, fake part). A real Tello and every MAVLink
#: unit count as production until B1 verifies a SITL board identity.
SIMULATED_DRONES = frozenset({FAKE_DRONE})
_REFUSING = "Refusing to start."
_SENSING_MODE = "sensing.mode"
_SENSING_REQUIRED = "required"


class EdgeStartRefused(ValueError):
    """The edge must not start; ``str()`` is the operator's exit message."""


@dataclass(frozen=True)
class EdgeResolution:
    """What the edge resolved at start (EDGE-1): the layers, the values, the feature gates and the report.

    ``values`` is the result of ``resolve_unit``; ``gates`` is that of ``resolve_features``; ``report``
    is the ``UnitReport`` with its ``unit_rev``. The start log (S7) and the consumers rewired from A3.3
    on read them from here.
    """

    layers: EdgeLayers
    simulated: bool
    values: Mapping[str, Any]
    gates: Mapping[str, Mapping[str, Any]]
    report: Any


def _refusing(text: str) -> str:
    text = text.strip()
    return text if text.endswith(_REFUSING) else f"{text} {_REFUSING}"


def check_launcher_unit(layers: EdgeLayers) -> None:
    """Refuse a launcher layer written for another unit, so its values never apply to this one."""
    launcher = layers.launcher
    if launcher is None or launcher.unit_id is None or launcher.unit_id == layers.unit_id:
        return
    from flightrisk.params.messages import render_launcher_for_other_unit

    raise EdgeStartRefused(_refusing(
        render_launcher_for_other_unit(LAUNCHER_PARAMS_ENV_VAR, launcher.unit_id, layers.unit_id)
    ))


def requested_features(layers: EdgeLayers) -> tuple[dict[str, bool], dict[str, str]]:
    """The feature switches set for this unit and where each came from; the launcher wins over the inventory."""
    requested: dict[str, bool] = {}
    sources: dict[str, str] = {}
    for where, switches in ((SOURCE_INVENTORY, layers.features),
                            (SOURCE_LAUNCHER, layers.launcher.features if layers.launcher else {})):
        for feature_id, on in switches.items():
            requested[feature_id] = bool(on)
            sources[feature_id] = where
    return requested, sources


def _capability(feature_id: str) -> str:
    return feature_id.replace("_", " ")


def static_feature_gates(layers: EdgeLayers, values: Mapping[str, Any], *, source: Optional[str]) -> dict:
    """The unit's feature gates at start; raises ``EdgeStartRefused`` when a gate refuses the start.

    A feature switched on that is statically unavailable refuses the start; on a MAVLink unit forward
    obstacle sensing is available only with ``--source camera`` or ``url`` and a level mount (FEA-10).
    Required obstacle sensing refuses the start when forward obstacle sensing is not on and the unit
    can never declare onboard avoidance either (FEA-7, static part); a unit that can declare it is
    checked at takeoff instead.
    """
    from flightrisk.hardware.features import (
        CONFIRM_OFF_REASON,
        FORWARD_OBSTACLE_SENSING,
        ONBOARD_AVOIDANCE,
        FeatureRefused,
        resolve_features,
    )
    from flightrisk.params.messages import render_unavailable_capability, render_unconfirmed_switch_off

    model = layers.choice.model
    requested, sources = requested_features(layers)
    try:
        gates = resolve_features(model, requested, sources, source=source)
    except FeatureRefused as exc:
        if exc.reason == CONFIRM_OFF_REASON.format(feature=exc.feature):
            raise EdgeStartRefused(_refusing(
                render_unconfirmed_switch_off(layers.unit_id, exc.reason, sources.get(exc.feature))
            )) from None
        raise EdgeStartRefused(_refusing(render_unavailable_capability(
            layers.unit_id, _capability(exc.feature), model.name, f"does not have it here ({exc.reason})"
        ))) from None
    mode = values.get(_SENSING_MODE)
    if (mode is not None and mode.value == _SENSING_REQUIRED and not gates[FORWARD_OBSTACLE_SENSING]["on"]
            and not gates[ONBOARD_AVOIDANCE]["ok"]):
        raise EdgeStartRefused(_refusing(render_unavailable_capability(
            layers.unit_id, "required obstacle sensing", model.name,
            "has neither forward obstacle sensing here "
            f"({gates[FORWARD_OBSTACLE_SENSING]['why'] or 'switched off'}) nor onboard avoidance",
        )))
    return gates


def _camera_roles(model: DroneModel) -> dict[str, str]:
    from flightrisk.hardware.features import default_level_tolerance_deg, derive_role

    tolerance = default_level_tolerance_deg(model.profile)
    roles = {cam.id: derive_role(cam, None, tolerance) for cam in model.cameras}
    return {cam_id: role for cam_id, role in roles.items() if role is not None}


def resolve_edge(
    *,
    choice: ModelChoice,
    drone: str,
    drone_id: str,
    source: Optional[str],
    environ: Mapping[str, str],
    argv: Iterable[str] = (),
    parser: Any = None,
    load_inventory: Optional[Callable[[], Any]] = None,
    today_accepts: Mapping[str, Mapping[str, Any]] = TODAY_ACCEPTS,
) -> EdgeResolution:
    """Resolve the unit ``drone_id`` flying ``choice`` on edge drone kind ``drone``, before any contact.

    Builds the layers (``build_layers``), refuses a launcher layer for another unit, resolves every
    value with ``resolve_unit`` (``simulated`` only for the fake adapter), runs the static feature
    gates and builds the start report. Raises ``EdgeStartRefused`` on any refusal; its text comes from
    ``params/messages.py`` through the resolver and the gates, and never names a test-adapter fact.
    """
    from flightrisk.hardware.features import FeatureRefused
    from flightrisk.params.report import build_report
    from flightrisk.params.resolver import ParamRefused, resolve_unit

    if today_accepts is None or not today_accepts:
        # None or an empty map would check environment and flag values against the target bounds,
        # which accepts inputs today's edge refuses (SEAM-10).
        raise ValueError("today_accepts must be today's accepted ranges, not None or an empty map")
    _backend_spec(drone)  # Raises ValueError for an unknown edge drone.
    simulated = drone in SIMULATED_DRONES
    try:
        layers = build_layers(choice=choice, drone_id=drone_id, environ=environ, argv=argv, parser=parser,
                              load_inventory=load_inventory)
        check_launcher_unit(layers)
        values = resolve_unit(
            layers.profile, layers.unit_id, layers.layers, simulated=simulated,
            confirmations=layers.confirmations, today_accepts=today_accepts, features=layers.features,
        )
        gates = static_feature_gates(layers, values, source=source)
        # A built-in model is reported from its descriptor; a custom model carries its own.
        report_model = choice.model if isinstance(layers.profile, str) else layers.profile
        report = build_report(report_model, values, gates=gates, roles=_camera_roles(choice.model))
    except (ParamRefused, ModelRefused, FeatureRefused) as exc:
        raise EdgeStartRefused(_refusing(str(exc))) from None
    return EdgeResolution(
        layers=layers,
        simulated=simulated,
        values=MappingProxyType(dict(values)),
        gates=MappingProxyType(gates),
        report=report,
    )


def _fact_ids(profile: Any) -> frozenset[str]:
    """Ids of the rows that are facts for *profile* (a profile id, or a custom model with an ``id``)."""
    from flightrisk.params.table import TABLE

    name = profile if isinstance(profile, str) else getattr(profile, "id", None)
    if not isinstance(name, str):
        return frozenset()
    return frozenset(spec.id for spec in TABLE if spec.is_fact(name))


def log_edge_start(resolution: EdgeResolution, log: Any, *, drone_id: Optional[str] = None) -> None:
    """Log each non-default value once, with its source (EDGE-3); a less-safe value is a warning.

    A fact row honoured as a developer override is logged at info with source ``env`` (FACT-3) and never
    with safety or setting wording (R8). A fact override ignored on a production unit is logged by the
    resolver, one line naming the variable.
    """
    facts = _fact_ids(resolution.layers.profile)
    for param_id in sorted(resolution.values):
        value = resolution.values[param_id]
        if not getattr(value, "non_default", False):
            continue
        source = getattr(value.source, "value", value.source)
        if param_id in facts:
            log.info("edge_fact_override", drone_id=drone_id, param_id=param_id, value=value.value,
                     source=source, detail=value.source_detail)
        elif value.less_safe:
            log.warning("edge_value_less_safe", drone_id=drone_id, param_id=param_id, value=value.value,
                        default=value.default, source=source, detail=value.source_detail)
        else:
            log.info("edge_value_non_default", drone_id=drone_id, param_id=param_id, value=value.value,
                     default=value.default, source=source, detail=value.source_detail)
