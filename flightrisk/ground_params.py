"""What the ground station resolves for each unit, once, from the unit's model (RES adoption, ground half).

The ground plans and gates every unit with that unit's own values (GND-1, GND-2, GND-7). This module is
the only place on the ground that resolves them, and the only A4 caller of ``resolve_unit``:

* ``resolve_ground_unit`` resolves one unit of a built-in model. Its layers are the environment, holding
  only the names ground code reads today (``GROUND_ENV_NAMES``), and the dashboard's flags, holding only
  ``GROUND_FLAGS``. A name only an edge reads, such as an edge's link-loss time, is left out, so the
  ground never refuses or changes a unit for a value it does not use (section 27, item 47, ground part).
  It passes ``TODAY_ACCEPTS``, so an environment or flag value keeps today's accepted range (SEAM-10),
  and a ground resolution is never simulated (CAP-6).
* ``units_for`` builds the units of a fleet through the implicit inventory (INV-4) and returns the units
  it resolved and the refusal text of each unit it did not.

A refusal raises ``GroundUnitRefused``. Its ``text`` is the resolver's text, which names the variable,
and ``str()`` names the unit as well; the ground shows it at Plan, its start for a unit (item 47).

Nothing here reads the airframe's height limit or ``DroneCapabilities``' altitude: height bounds on the
ground come from the ceiling and slot rows only (item 53). ``values`` holds every effective value the
resolver returns, for the dashboard; no code here reads that row.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from flightrisk.drone.move_range import _move_range
from flightrisk.drone.safety import (
    FIELD_LIMITS,
    ROOM_LIMITS,
    EnvelopeLimits,
    format_cm,
    is_finite_number,
)
from flightrisk.hardware.catalogue import BUILTIN_MODELS, DroneModel
from flightrisk.hardware.inventory import InventoryRefused, implicit_inventory
from flightrisk.params.resolver import ParamRefused, resolve_unit
from flightrisk.params.spec import EffectiveValue, Source
from flightrisk.params.today import TODAY_ACCEPTS

#: The registry's environment names that ground code reads today, each with its reader. Only these reach
#: a ground resolution; ``tests/test_ground_params.py`` lists them with the reader's line.
GROUND_ENV_NAMES = frozenset({
    # flightrisk/dashboard/app.py fleet_geofence_cm, the fence the ground plans re-tasked lanes against.
    "FLIGHTRISK_GEOFENCE_CM",
    # flightrisk/drone/safety.py min_takeoff_battery_from_env, called by the coordinator and the single search.
    "FLIGHTRISK_MIN_TAKEOFF_BATTERY",
    # flightrisk/dashboard/manual_control.py manual_orphan_land_s and manual_lost_idle_s.
    "FLIGHTRISK_MANUAL_ORPHAN_LAND_S",
    "FLIGHTRISK_MANUAL_LOST_IDLE_S",
    # flightrisk/dashboard/__main__.py, the defaults of --fleet-area-cm and --fleet-strip-cm.
    "FLIGHTRISK_FLEET_AREA_CM",
    "FLIGHTRISK_FLEET_STRIP_CM",
    # flightrisk/drone/plan_edit.py camera_footprint_cm, which only the dashboard's map plan calls.
    "FLIGHTRISK_CAMERA_FOOTPRINT_CM",
})
#: The registry's flags that ground code reads today: the dashboard's fleet area and lane spacing.
GROUND_FLAGS = frozenset({"--fleet-area-cm", "--fleet-strip-cm"})
#: Every name a ground resolution reads.
GROUND_NAMES = GROUND_ENV_NAMES | GROUND_FLAGS

_MIN_MOVE = "move.min_cm"
_MAX_MOVE = "move.max_cm"
_SLOT = "envelope.alt_slot_cm"
_CEILING = "envelope.ceiling_cm"
_MARGIN = "envelope.ceiling_margin_cm"
_GEOFENCE = "envelope.geofence_cm"
_MAX_GEOFENCE = "envelope.max_geofence_cm"
_MIN_TAKEOFF_BATTERY = "battery.min_takeoff_pct"


class GroundUnitRefused(ValueError):
    """The ground must not plan or fly this unit.

    ``text`` is the refusal as given (for a resolver refusal, the resolver's text, which names the
    variable); ``str()`` puts the unit in front of it, so the message names both.
    """

    def __init__(self, unit_id: str, text: str):
        super().__init__(f"unit {unit_id}: {text}")
        self.unit_id = unit_id
        self.text = text


@dataclass(frozen=True)
class GroundCamera:
    """A model's main camera as plain values, for ``fleet_scale.scale_for`` (its first catalogue camera).

    The frame size is the still-photo size when the camera has one, else the stream size, as in the
    catalogue's own coverage camera (``DroneModel.coverage_camera``).
    """

    id: str
    hfov_deg: float
    width_px: int
    height_px: int


@dataclass(frozen=True)
class GroundUnit:
    """One unit's resolved values on the ground.

    ``limits`` holds the ranges and defaults of the unit's model profile from the resolved move, slot,
    ceiling and geofence rows, as the edge's envelope profile does; a unit of a built-in model gets the
    shared ``ROOM_LIMITS`` or ``FIELD_LIMITS`` object. The values the unit actually uses, including one
    set through a ground name, are in ``values``. ``min_move_cm`` and ``max_move_cm`` are whole
    centimetres, with the longest at least twice the shortest, as the planner needs.
    ``max_range_m`` is the catalogue's (``None`` when the model is not range-limited) and ``camera`` the
    catalogue's main camera (``None`` for a model without one).
    """

    unit_id: str
    model_id: str
    #: The catalogue's ``environment`` fact (``indoor`` or ``field``).
    environment: str
    indoor_profile_measured: bool
    limits: EnvelopeLimits
    min_move_cm: int
    max_move_cm: int
    min_takeoff_battery_pct: float
    max_range_m: float | None
    camera: GroundCamera | None
    #: The resolver's effective values, by parameter id (read-only).
    values: Mapping[str, EffectiveValue]


def _layer(given: Mapping[str, Any] | None, names: frozenset[str]) -> dict[str, Any]:
    """The entries of ``given`` whose name is in ``names``."""
    return {name: value for name, value in (given or {}).items() if name in names}


def _row(unit_id: str, values: Mapping[str, EffectiveValue], param_id: str) -> EffectiveValue:
    value = values.get(param_id)
    if value is None:
        raise GroundUnitRefused(unit_id, f"its model gives no `{param_id}` value, so the ground cannot plan it.")
    return value


def _limits(unit_id: str, model: DroneModel, values: Mapping[str, EffectiveValue]) -> EnvelopeLimits:
    """The envelope profile of the unit's model, from the resolved rows; never the height limit row."""
    slot = _row(unit_id, values, _SLOT)
    ceiling = _row(unit_id, values, _CEILING)
    margin = _row(unit_id, values, _MARGIN)
    if model.scale == ROOM_LIMITS.name:
        # The room envelope has never capped the geofence itself (safety.ROOM_LIMITS); the edge checks
        # the geofence row's bound at its start.
        max_geofence_cm = ROOM_LIMITS.max_geofence_cm
    else:
        max_geofence_cm = _row(unit_id, values, _MAX_GEOFENCE).value
    limits = EnvelopeLimits(
        name=model.scale,
        min_move_cm=_row(unit_id, values, _MIN_MOVE).value,
        max_move_cm=_row(unit_id, values, _MAX_MOVE).value,
        min_alt_slot_cm=slot.lo,
        max_alt_slot_cm=slot.hi,
        min_ceiling_cm=ceiling.lo,
        max_ceiling_cm=ceiling.hi,
        min_ceiling_margin_cm=margin.lo,
        max_geofence_cm=max_geofence_cm,
        default_geofence_cm=_row(unit_id, values, _GEOFENCE).default,
        default_alt_slot_cm=slot.default,
        default_ceiling_cm=ceiling.default,
        default_ceiling_margin_cm=margin.default,
    )
    # The shared profile object where it is equal, because a check such as safety.slot_below_minimum
    # recognises the room profile by identity.
    return next((shared for shared in (ROOM_LIMITS, FIELD_LIMITS) if shared == limits), limits)


def _move_limits(unit_id: str, limits: EnvelopeLimits) -> tuple[int, int]:
    """The unit's shortest and longest single move as whole centimetres; refuses a range a plan cannot use.

    The rule is the planner's (``move_range._move_range``): the longest move must be at least twice the
    shortest, so a leg just longer than one move splits into two moves of at least the shortest.
    """
    try:
        return _move_range(limits)
    except ValueError as exc:
        low, high = limits.min_move_cm, limits.max_move_cm
        if is_finite_number(low) and is_finite_number(high) and high < 2 * low:
            text = (f"its longest single move ({format_cm(high)} cm) is less than twice its shortest single "
                    f"move ({format_cm(low)} cm). A plan splits a long leg into moves no shorter than the "
                    "shortest, so it needs the longest to be at least twice the shortest.")
        else:
            text = f"its move range cannot be planned: {exc}."
        raise GroundUnitRefused(unit_id, text) from None


def _camera(model: DroneModel) -> GroundCamera | None:
    if not model.cameras:
        return None
    cam = model.cameras[0]
    return GroundCamera(
        id=cam.id,
        hfov_deg=cam.hfov_deg,
        width_px=cam.photo_w_px or cam.stream_w_px,
        height_px=cam.photo_h_px or cam.stream_h_px,
    )


def resolve_ground_unit(
    unit_id: str,
    model_id: str,
    *,
    environ: Mapping[str, str],
    flags: Mapping[str, Any] | None = None,
) -> GroundUnit:
    """Resolve the unit ``unit_id`` of the built-in model ``model_id`` for the ground.

    ``environ`` is the ground's environment and ``flags`` maps the dashboard's flags (``--fleet-area-cm``)
    to their values; only ``GROUND_ENV_NAMES`` and ``GROUND_FLAGS`` of them are read. Raises
    ``GroundUnitRefused`` when the resolver refuses a value or a combination of values, or when the
    unit's move range cannot be planned.
    """
    model = BUILTIN_MODELS.get(model_id) if isinstance(model_id, str) else None
    if model is None:
        raise GroundUnitRefused(unit_id, f"its model {model_id!r} is not a built-in model.")
    layers = {
        Source.ENV: _layer(environ, GROUND_ENV_NAMES),
        Source.CLI: _layer(flags, GROUND_FLAGS),
    }
    try:
        values = resolve_unit(model_id, unit_id, layers, simulated=False, today_accepts=TODAY_ACCEPTS)
    except ParamRefused as exc:
        raise GroundUnitRefused(unit_id, exc.message) from None
    limits = _limits(unit_id, model, values)
    min_move_cm, max_move_cm = _move_limits(unit_id, limits)
    max_range = model.envelope.get("max_range_m")
    return GroundUnit(
        unit_id=unit_id,
        model_id=model_id,
        environment=model.fact("environment").value,
        indoor_profile_measured=bool(model.fact("indoor_profile_measured").value),
        limits=limits,
        min_move_cm=min_move_cm,
        max_move_cm=max_move_cm,
        min_takeoff_battery_pct=float(_row(unit_id, values, _MIN_TAKEOFF_BATTERY).value),
        max_range_m=None if max_range is None else max_range.value,
        camera=_camera(model),
        values=MappingProxyType(dict(values)),
    )


def units_for(
    unit_ids: Iterable[str],
    reported_models: Mapping[str, str] | None,
    *,
    environ: Mapping[str, str],
    flags: Mapping[str, Any] | None = None,
) -> tuple[dict[str, GroundUnit], dict[str, str]]:
    """The ground units of ``unit_ids``, each of the model the implicit inventory gives it (INV-4).

    ``reported_models`` maps a unit id to the model its edge reported, if any. Returns ``(units, refused)``:
    ``units`` maps each resolved unit id to its ``GroundUnit`` and ``refused`` maps each other unit id to
    its refusal text, which names the unit. One unit's refusal never stops the others.
    """
    units: dict[str, GroundUnit] = {}
    refused: dict[str, str] = {}
    for unit_id in unit_ids:
        try:
            record = implicit_inventory([unit_id], reported_models).unit(unit_id)
            units[unit_id] = resolve_ground_unit(unit_id, record["model"], environ=environ, flags=flags)
        except InventoryRefused as exc:
            refused[unit_id] = str(GroundUnitRefused(unit_id, str(exc)))
        except GroundUnitRefused as exc:
            refused[unit_id] = str(exc)
    return units, refused
