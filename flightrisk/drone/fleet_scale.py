"""Fleet plan scale per environment: room-sized plans and field-sized autopilot plans.

The fleet coordinator (``flightrisk.drone.coordinator.FleetCoordinator``)
plans in centimetres on a grid whose size, lane spacing, altitude slots and
geofence are parameters. Its defaults are the room profile's (a 4 m by 4 m
area, 75 cm lanes, 100 and 130 cm slots). ``FleetScale`` bundles those
parameters per scale so a ground never mixes them up:

* ``ROOM_SCALE``: the room profile's defaults, unchanged.
* ``field_scale(...)``: an outdoor plan for PX4 or ArduPilot drones flown by
  ``--drone mavlink`` edges. The area and slots are given in metres; the
  lane spacing comes from the camera's nadir footprint at the lowest slot
  (``flightrisk.drone.coverage.nadir_swath_m``) with an overlap, so every
  slot covers at least the planned strip; the geofence each edge needs is
  derived from its zone; and the slots are checked against
  ``FIELD_LIMITS`` and a minimum vertical separation.

The plan frame is the same on every scale: each drone takes off at the
near-left corner of its own zone, ``x`` is to the right and ``y`` ahead
(for MAVLink drones, along ``--frame-heading-deg``). Lanes are still sent as
moves of at most 5 m (the coordinator's segmentation); a field edge flies
each as one position target. Merging collinear segments into longer targets
is a listed follow-up (``docs/open-drone-support.md``).

``scale_for(environment, n_drones, ...)`` gives the scale of one
environment (an envelope profile name, ``"room"`` or ``"field"``) for a
camera passed in as values, a ``coverage.CameraModel`` or its horizontal
field of view and frame size, so a caller that resolves each unit's
environment and camera elsewhere never imports a camera catalogue here. The
per-environment defaults stay in this module: ``ROOM_SCALE`` for the room,
and ``field_scale``'s defaults (``PI_CAMERA_MODULE_3`` among them) for the
field.
"""
from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from flightrisk.drone.coordinator import DEFAULT_ALT_SLOTS_CM, DEFAULT_ZONE_GAP_CM
from flightrisk.drone.coverage import CameraModel, nadir_swath_m
from flightrisk.drone.safety import (
    DEFAULT_GEOFENCE_CM,
    FIELD_LIMITS,
    ROOM_LIMITS,
    EnvelopeLimits,
)

__all__ = [
    "MAX_FIELD_AREA_SIDE_M",
    "MIN_SLOT_SEPARATION_M",
    "PI_CAMERA_MODULE_3",
    "ROOM_SCALE",
    "FleetScale",
    "field_scale",
    "scale_for",
]

#: Phase 1 plans areas up to this side length (metres). Larger areas need
#: battery swaps and long-range links that are not planned for yet.
MAX_FIELD_AREA_SIDE_M = 500.0
#: Smallest vertical distance between two altitude slots of one field fleet.
MIN_SLOT_SEPARATION_M = 5.0
#: Extra geofence beyond the farthest point of a drone's zone, in metres.
_FENCE_MARGIN_M = 5.0
#: Narrowest lane spacing a field plan accepts (metres).
_MIN_STRIP_M = 2.0
#: Uncovered stretches narrower than this (lane rounding) are not reported.
_MIN_HOLE_M = 0.1

#: The companion computer's camera pointing straight down. The horizontal
#: field of view is the published one; the mounting, speed and endurance are
#: assumptions about a typical Pixhawk-class build.
PI_CAMERA_MODULE_3 = CameraModel(
    name="Raspberry Pi Camera Module 3 (nadir)",
    width_px=4608,
    height_px=2592,
    hfov_deg=66.0,
    gimbal_pitch_deg=-90.0,
    max_speed_mps=3.0,
    flight_time_s=15 * 60,
    source=(
        "https://www.raspberrypi.com/documentation/accessories/camera.html "
        "(Camera Module 3, standard lens: 66 deg horizontal field of view); nadir mounting, "
        "3 m/s and 15 min are ASSUMPTION"
    ),
)


@dataclass(frozen=True)
class FleetScale:
    """The coordinator parameters of one plan scale, in centimetres."""

    profile: str
    area_cm: tuple[int, int]
    strip_cm: int
    alt_slots_cm: tuple[int, ...]
    geofence_cm: float
    zone_gap_cm: int
    #: Field plans: where each drone takes off, across-track from the search
    #: area's near-left corner (its lanes are inset by half a swath).
    takeoff_x_cm: tuple[float, ...] | None = None
    #: Field plans: the camera's ground swath at the lowest slot, the share
    #: of the search area's width the lanes cover, and what is not covered.
    swath_cm: float | None = None
    coverage_fraction: float | None = None
    coverage_note: str = ""

    def coordinator_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``FleetCoordinator`` (besides the links)."""
        return {
            "area_cm": self.area_cm,
            "strip_cm": self.strip_cm,
            "alt_slots_cm": self.alt_slots_cm,
            "geofence_cm": self.geofence_cm,
            "zone_gap_cm": self.zone_gap_cm,
        }

    def zone_width_cm(self, n_drones: int) -> int:
        """Width of each drone's zone, as the coordinator computes it."""
        return (self.area_cm[0] - self.zone_gap_cm * (n_drones - 1)) // n_drones

    def edge_settings(self, n_drones: int) -> list[dict[str, float]]:
        """What each ``--drone mavlink`` edge must be started with (metres), in zone order."""
        return [
            {
                "alt_slot_m": self.alt_slots_cm[i % len(self.alt_slots_cm)] / 100.0,
                "geofence_m": self.geofence_cm / 100.0,
                "zone_x_m": (
                    self.takeoff_x_cm[i] if self.takeoff_x_cm is not None
                    else i * (self.zone_width_cm(n_drones) + self.zone_gap_cm)
                ) / 100.0,
            }
            for i in range(n_drones)
        ]


#: The indoor plan: the room profile's defaults, which are the coordinator's and the edge's.
ROOM_SCALE = FleetScale(
    profile="room",
    area_cm=(400, 400),
    strip_cm=75,
    alt_slots_cm=tuple(int(s) for s in DEFAULT_ALT_SLOTS_CM),
    geofence_cm=float(DEFAULT_GEOFENCE_CM),
    zone_gap_cm=DEFAULT_ZONE_GAP_CM,
)


def _positive(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number, got {value!r}")
    return float(value)


def field_scale(
    n_drones: int,
    *,
    area_m: tuple[float, float] = (60.0, 60.0),
    alt_slots_m: Sequence[float] = (15.0, 20.0),
    camera: CameraModel = PI_CAMERA_MODULE_3,
    overlap: float = 0.2,
    zone_gap_m: float = 0.0,
    geofence_m: float | None = None,
    limits: EnvelopeLimits = FIELD_LIMITS,
) -> FleetScale:
    """An outdoor fleet plan for *n_drones* PX4 or ArduPilot drones. Raises ``ValueError``.

    The search area is split into one zone per drone across its width. In
    each zone the lanes are inset by half a swath from the zone's edges and
    spaced at most ``swath * (1 - overlap)`` apart, rounding the lane count
    up, so the whole zone is seen at the lowest slot. Each drone takes off
    half a swath inside its zone (``edge_settings``); the coordinator gets a
    lane grid shifted by that inset, with the same zone spacing. The camera's
    long image axis must point across the track (a cropped mode narrows the
    horizontal field of view and the swath). Coverage is computed and
    reported (``coverage_fraction``, ``coverage_note``), never a reason to
    refuse; the gaps between zones (``zone_gap_m``, 0 by default: the
    inset already keeps neighbouring lanes a swath apart) are not covered.

    Each drone needs its own altitude slot, at least
    ``MIN_SLOT_SEPARATION_M`` from the others and within *limits*. The
    geofence, unless given, is the farthest lane point from the takeoff
    point plus 5 m; a smaller one is refused because the edge would refuse
    the plan.
    """
    if isinstance(n_drones, bool) or not isinstance(n_drones, int) or n_drones < 1:
        raise ValueError(f"n_drones must be a positive integer, got {n_drones!r}")
    width_m, depth_m = (_positive("area width", area_m[0]), _positive("area depth", area_m[1]))
    if max(width_m, depth_m) > MAX_FIELD_AREA_SIDE_M:
        raise ValueError(f"area sides must be at most {MAX_FIELD_AREA_SIDE_M:g} m, got {width_m:g} x {depth_m:g} m")
    if not 0 <= overlap < 1:
        raise ValueError(f"overlap must be in [0, 1), got {overlap!r}")
    gap_m = float(zone_gap_m)
    if not math.isfinite(gap_m) or gap_m < 0:
        raise ValueError(f"zone_gap_m must be 0 or more, got {zone_gap_m!r}")
    slots = [_positive("altitude slot", s) for s in alt_slots_m]
    if not slots:
        raise ValueError("alt_slots_m must name at least one slot")
    if len(slots) < n_drones:
        raise ValueError(
            f"{n_drones} drones need {n_drones} altitude slots, got {len(slots)}: drones sharing a slot would "
            "share an altitude where their geofences overlap"
        )
    slots = slots[:n_drones]
    for slot in slots:
        if not limits.min_alt_slot_cm <= slot * 100 <= limits.max_alt_slot_cm:
            raise ValueError(
                f"altitude slot {slot:g} m is outside {limits.min_alt_slot_cm / 100:g}-"
                f"{limits.max_alt_slot_cm / 100:g} m"
            )
    ordered = sorted(slots)
    if any(b - a < MIN_SLOT_SEPARATION_M for a, b in itertools.pairwise(ordered)):
        raise ValueError(f"altitude slots must be at least {MIN_SLOT_SEPARATION_M:g} m apart, got {slots}")
    swath_m = nadir_swath_m(camera, ordered[0])
    half_m = swath_m / 2
    spacing_m = swath_m * (1 - overlap)
    if spacing_m < _MIN_STRIP_M:
        raise ValueError(f"lane spacing {spacing_m:.1f} m is below {_MIN_STRIP_M:g} m; fly higher or use a wider lens")
    zone_w_m = (width_m - gap_m * (n_drones - 1)) / n_drones
    if zone_w_m < limits.min_move_cm / 100:
        raise ValueError(f"area width {width_m:g} m is too narrow for {n_drones} zones with {gap_m:g} m gaps")
    inner_m = zone_w_m - 2 * half_m
    if inner_m <= 0:
        lanes, strip_cm, inset_m = 1, math.floor(spacing_m * 100), zone_w_m / 2  # one centre lane sees it all
        zone_c_cm = strip_cm
    else:
        lanes = math.ceil(inner_m / spacing_m) + 1
        strip_cm = math.floor(inner_m / (lanes - 1) * 100)
        inset_m = half_m
        zone_c_cm = lanes * strip_cm  # the coordinator then lays exactly *lanes* lanes
    pitch_cm = round((zone_w_m + gap_m) * 100)
    gap_c_cm = pitch_cm - zone_c_cm
    takeoff_x = tuple(i * pitch_cm + inset_m * 100 for i in range(n_drones))
    span_x_m = (lanes - 1) * strip_cm / 100
    needed_m = max(span_x_m, depth_m) + _FENCE_MARGIN_M
    fence_m = needed_m if geofence_m is None else _positive("geofence", geofence_m)
    if fence_m < max(span_x_m, depth_m):
        raise ValueError(
            f"geofence {fence_m:g} m is smaller than a drone's lanes ({span_x_m:.1f} m across, {depth_m:g} m deep); "
            "the edges would refuse the plan"
        )
    if fence_m * 100 > limits.max_geofence_cm:
        raise ValueError(f"geofence {fence_m:g} m is above the {limits.max_geofence_cm / 100:g} m limit")
    fraction, note = _coverage(width_m, takeoff_x, lanes, strip_cm / 100, half_m)
    return FleetScale(
        profile=limits.name,
        area_cm=(n_drones * zone_c_cm + (n_drones - 1) * gap_c_cm, round(depth_m * 100)),
        strip_cm=strip_cm,
        alt_slots_cm=tuple(round(s * 100) for s in slots),
        geofence_cm=float(math.ceil(fence_m * 100)),
        zone_gap_cm=gap_c_cm,
        takeoff_x_cm=takeoff_x,
        swath_cm=swath_m * 100,
        coverage_fraction=fraction,
        coverage_note=note,
    )


#: The environments ``scale_for`` accepts: the envelope profile names of the two scales.
_ENVIRONMENTS = (ROOM_LIMITS.name, FIELD_LIMITS.name)


def _camera(
    camera: CameraModel | None, hfov_deg: Any, width_px: Any, height_px: Any
) -> CameraModel | None:
    """The camera given to ``scale_for``: a ``CameraModel``, one built from plain values, or None.

    Plain values describe a camera pointing straight down: the horizontal
    field of view in degrees (above 0 and below 180) across the track, and
    the frame width and height in pixels. All three are needed, and they
    cannot be combined with a ``CameraModel``. The built model takes the
    frame and field of view given; its other fields (speed, endurance) are
    ``PI_CAMERA_MODULE_3``'s and are not used for a scale.
    """
    values = {"hfov_deg": hfov_deg, "width_px": width_px, "height_px": height_px}
    given = sorted(name for name, value in values.items() if value is not None)
    if camera is not None:
        if given:
            raise ValueError(
                f"give the camera either as a CameraModel or as values, not both (got {', '.join(given)})"
            )
        if not isinstance(camera, CameraModel):
            raise ValueError(f"camera must be a coverage.CameraModel, got {type(camera).__name__}")
        return camera
    if not given:
        return None
    if len(given) < len(values):
        missing = sorted(set(values) - set(given))
        raise ValueError(
            f"a camera given as values needs hfov_deg, width_px and height_px; missing {', '.join(missing)}"
        )
    hfov = _positive("hfov_deg", hfov_deg)
    if hfov >= math.degrees(math.pi):
        raise ValueError(f"hfov_deg must be below a half turn, got {hfov_deg!r}")
    for name in ("width_px", "height_px"):
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive whole number of pixels, got {value!r}")
    return replace(
        PI_CAMERA_MODULE_3,
        name=f"nadir camera, {hfov:g} deg across {width_px} x {height_px} px",
        width_px=width_px,
        height_px=height_px,
        hfov_deg=hfov,
        source="given by the caller",
    )


def scale_for(
    environment: str,
    n_drones: int,
    *,
    camera: CameraModel | None = None,
    hfov_deg: float | None = None,
    width_px: int | None = None,
    height_px: int | None = None,
    **field_options: Any,
) -> FleetScale:
    """The fleet plan scale of one environment for *n_drones* drones and the camera given. Raises ``ValueError``.

    ``environment`` is ``"room"`` or ``"field"``; anything else is refused.
    The camera is a ``coverage.CameraModel`` (``camera``) or plain values
    (``hfov_deg``, ``width_px`` and ``height_px`` together, a camera pointing
    straight down); it is validated for either environment.

    * ``"room"`` gives ``ROOM_SCALE``, the room profile's defaults, whatever
      camera is given: the room plan does not depend on the camera. It takes
      no ``field_options``.
    * ``"field"`` gives ``field_scale(n_drones, camera=<that camera>,
      **field_options)``, so the lane spacing follows that camera's swath.
      Without a camera it is ``field_scale``'s default camera.
      ``field_options`` are ``field_scale``'s other keyword arguments
      (``area_m``, ``alt_slots_m``, ``overlap``, ``zone_gap_m``,
      ``geofence_m`` and ``limits``) with its defaults.
    """
    if not isinstance(environment, str) or environment not in _ENVIRONMENTS:
        raise ValueError(f"environment must be one of {', '.join(map(repr, _ENVIRONMENTS))}, got {environment!r}")
    if isinstance(n_drones, bool) or not isinstance(n_drones, int) or n_drones <= 0:
        raise ValueError(f"n_drones must be a positive integer, got {n_drones!r}")
    model = _camera(camera, hfov_deg, width_px, height_px)
    if environment == ROOM_LIMITS.name:
        if field_options:
            raise ValueError(
                f"the room scale is fixed and takes no field options, got {', '.join(sorted(field_options))}"
            )
        return ROOM_SCALE
    if model is None:
        return field_scale(n_drones, **field_options)
    return field_scale(n_drones, camera=model, **field_options)


def _coverage(
    width_m: float, takeoff_x_cm: tuple[float, ...], lanes: int, strip_m: float, half_m: float
) -> tuple[float, str]:
    """Share of the area's width the lanes see (at the lowest slot), and the uncovered stretches."""
    spans = sorted(
        (max(0.0, x / 100 + k * strip_m - half_m), min(width_m, x / 100 + k * strip_m + half_m))
        for x in takeoff_x_cm for k in range(lanes)
    )
    covered, holes, edge = 0.0, [], 0.0
    for lo, hi in spans:
        if lo > edge + 1e-6:
            holes.append((edge, lo))
        if hi > edge:
            covered += hi - max(lo, edge)
            edge = hi
    if edge < width_m - 1e-6:
        holes.append((edge, width_m))
    fraction = min(1.0, covered / width_m)
    holes = [(lo, hi) for lo, hi in holes if hi - lo >= _MIN_HOLE_M]  # centimetre rounding is not a hole
    if not holes:
        return fraction, "the lanes cover the whole area at the lowest slot"
    shown = ", ".join(f"{lo:.1f}-{hi:.1f} m" for lo, hi in holes)
    return fraction, f"{fraction:.0%} of the width is covered; not seen: {shown} across from the near-left corner"
