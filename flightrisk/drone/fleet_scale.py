"""Fleet plan scale per drone type: room-sized Tello plans and field-sized autopilot plans.

The fleet coordinator (``flightrisk.drone.coordinator.FleetCoordinator``)
plans in centimetres on a grid whose size, lane spacing, altitude slots and
geofence are parameters. Its defaults are the indoor Tello's (a 4 m by 4 m
area, 75 cm lanes, 100 and 130 cm slots). ``FleetScale`` bundles those
parameters per scale so a ground never mixes them up:

* ``ROOM_SCALE``: the Tello defaults, unchanged.
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
"""
from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from flightrisk.drone.coordinator import DEFAULT_ALT_SLOTS_CM, DEFAULT_ZONE_GAP_CM
from flightrisk.drone.coverage import CameraModel, nadir_swath_m
from flightrisk.drone.safety import DEFAULT_GEOFENCE_CM, FIELD_LIMITS, EnvelopeLimits

__all__ = [
    "MAX_FIELD_AREA_SIDE_M",
    "MIN_SLOT_SEPARATION_M",
    "PI_CAMERA_MODULE_3",
    "ROOM_SCALE",
    "FleetScale",
    "field_scale",
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
                "zone_x_m": i * (self.zone_width_cm(n_drones) + self.zone_gap_cm) / 100.0,
            }
            for i in range(n_drones)
        ]


#: The indoor Tello plan: the coordinator's and the edge's defaults.
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
    zone_gap_m: float = 5.0,
    geofence_m: float | None = None,
    limits: EnvelopeLimits = FIELD_LIMITS,
) -> FleetScale:
    """An outdoor fleet plan for *n_drones* PX4 or ArduPilot drones. Raises ``ValueError``.

    Lane spacing is the camera's nadir swath at the lowest slot times
    ``1 - overlap``. The geofence, unless given, is the farthest point of a
    zone from its takeoff corner plus 5 m; a smaller one is refused because
    the edge would refuse the plan. Slots must be within *limits* and at
    least ``MIN_SLOT_SEPARATION_M`` apart.
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
    for slot in slots:
        if not limits.min_alt_slot_cm <= slot * 100 <= limits.max_alt_slot_cm:
            raise ValueError(
                f"altitude slot {slot:g} m is outside {limits.min_alt_slot_cm / 100:g}-"
                f"{limits.max_alt_slot_cm / 100:g} m"
            )
    ordered = sorted(slots)
    if any(b - a < MIN_SLOT_SEPARATION_M for a, b in itertools.pairwise(ordered)):
        raise ValueError(f"altitude slots must be at least {MIN_SLOT_SEPARATION_M:g} m apart, got {slots}")
    strip_m = nadir_swath_m(camera, ordered[0]) * (1 - overlap)
    if strip_m < _MIN_STRIP_M:
        raise ValueError(f"lane spacing {strip_m:.1f} m is below {_MIN_STRIP_M:g} m; fly higher or use a wider lens")
    zone_w_m = (width_m - gap_m * (n_drones - 1)) / n_drones
    if zone_w_m < limits.min_move_cm / 100:
        raise ValueError(f"area width {width_m:g} m is too narrow for {n_drones} zones with {gap_m:g} m gaps")
    needed_m = max(zone_w_m, depth_m) + _FENCE_MARGIN_M
    fence_m = needed_m if geofence_m is None else _positive("geofence", geofence_m)
    if fence_m < max(zone_w_m, depth_m):
        raise ValueError(
            f"geofence {fence_m:g} m is smaller than a zone ({zone_w_m:.1f} m wide, {depth_m:g} m deep); "
            "the edges would refuse the plan"
        )
    if fence_m * 100 > limits.max_geofence_cm:
        raise ValueError(f"geofence {fence_m:g} m is above the {limits.max_geofence_cm / 100:g} m limit")
    return FleetScale(
        profile=limits.name,
        area_cm=(round(width_m * 100), round(depth_m * 100)),
        strip_cm=math.floor(strip_m * 100),
        alt_slots_cm=tuple(round(s * 100) for s in slots),
        geofence_cm=float(math.ceil(fence_m * 100)),
        zone_gap_cm=round(gap_m * 100),
    )
