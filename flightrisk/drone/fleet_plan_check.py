"""Inputs, planning footprints and safety checks for fleet search plans.

``flightrisk.drone.fleet_patterns`` builds the plans and re-exports every
public name of this module, so callers import from ``fleet_patterns`` only.
Nothing here imports ``fleet_patterns``: the plan objects are used through
their attributes.

Separation margin
-----------------
``check_plan`` requires the guaranteed (commanded) separation between any two
drones to be at least ``min_separation_cm + 2 * placement_tolerance_cm``. The
operator places each drone by tape measure with an assumed error of up to
``placement_tolerance_cm`` (plan section 3.1, 10 cm until rung L3 measures
it). If both drones are misplaced toward each other, the true separation is
the commanded one minus twice the tolerance, which is then still at least
``min_separation_cm``. That is why ``DEFAULT_GAP_CM`` is 170 cm: 150 cm of
D_min plus 2 x 10 cm. Dead-reckoning drift is extra; ``required_gap_cm``
sizes the gap for it once the drift fraction has been measured.

Planning footprint
------------------
The patterns rely on a rectangle that the camera is guaranteed to see,
relative to the drone: every ground point at a lateral offset of at most
``lateral_cm / 2`` and an along-track distance (ahead, along ``+y``) between
``near_cm`` and ``far_cm``. ``planning_footprint`` derives it:

* ``SQUARE_FOOTPRINT`` (the string ``"square"``) is the plan's idealised model:
  a square of side ``sweep_cm`` centred on the drone. Only use it for model
  studies, such as reproducing the plan's worked example. It has no altitude
  or placement margin, and a plan built with it is not flyable without the
  explicit ``allow_unchecked`` override (see ``FleetSearchPlan``).
* a ``coverage.SweepModel`` is the real forward camera: a trapezoid from the
  near edge (where the bottom of the image meets the ground) to the far edge
  (the detection range), widening with distance. Each drone is modelled at
  its own altitude slot (the model's ``height_m`` is replaced by it).
* a ``coverage_sim.ForwardFootprint`` describes that trapezoid at one
  altitude only, so it is refused unless every drone of the plan flies the
  same altitude slot; it is taken to be the footprint at that slot. For
  another height its near edge is scaled with the height, as for a level
  camera over flat ground (the ``SweepModel`` geometry).

Margins of the real camera
--------------------------
The rectangle and the coverage gate use, for each drone, the *gate camera*:
the ground that stays visible however the drone deviates within the margins
of the edge executor and the operator.

* Altitude: the executor's slot correction (``safety.SafetyEnvelope.slot_move``)
  does nothing while the rounded height error is below the 20 cm minimum
  move, so a drone may hold up to ``ALTITUDE_DEADBAND_CM`` above its slot,
  plus ``ALTITUDE_TOLERANCE_CM`` of height-sensor error (a guess until
  hardware rung L1 measures it). A level camera sees less near ground the
  higher it flies, so the gate camera is the camera at ``slot +
  ALTITUDE_MARGIN_CM``; lower heights see a superset. These are the room
  profile's defaults: ``planning_footprint`` takes them as
  ``altitude_deadband_cm`` and ``altitude_tolerance_cm``, and the planning
  footprint records their sum as its ``altitude_margin_cm``, so the coverage
  gate always uses the gate camera the plan was derived with.
* Placement: each drone may be placed up to ``placement_tolerance_cm`` off in
  ``x`` and ``y``. The gate camera is eroded by that tolerance
  (``ForwardFootprint.eroded``): it keeps only ground that stays visible for
  any such offset. Planning with it starts lanes ``placement_tolerance_cm``
  further back, ends them that much later, and makes each pass reach that
  much past every edge and seam, so neighbouring drones' coverage overlaps
  by twice the tolerance.

The lateral width is chosen first (``sweep_cm``, or by default the width of
the gate camera at its near edge, at least ``MIN_SWEEP_CM``, which keeps the
standoff at its minimum); the along-track window then starts where that
width is fully visible. The window is the one every drone's gate camera sees.

Lanes along ``y`` use only the lateral width and the window's two ends.
Legs across the heading (the rings of the split expanding square and the
square spiral) are spaced by ``along_cm`` and placed as if the camera were
centred ``ahead_cm`` in front of the drone. Because the rectangle is inside
the gate camera, everything the square-model pattern would cover is also
covered by the real camera, within the margins.

Per-unit limits
---------------
Each drone's lanes and altitude slot are checked against its own
``safety.EnvelopeLimits`` profile. Helpers that act on one drone take one
``limits`` (``ROOM_LIMITS`` by default). ``check_plan`` takes
``unit_limits``, a mapping from every drone id of the plan to its profile:
``None``, the default, means ``ROOM_LIMITS`` for every drone, and a mapping
that lacks a drone of the plan is refused with ``ValueError`` rather than
falling back to a default (ids the plan does not use are ignored). A lane
must be one whole-centimetre move within its drone's ``min_move_cm`` to
``max_move_cm``, and the planner needs ``max_move_cm`` to be at least twice
``min_move_cm`` (a leg just longer than one move splits into two moves of
at least the minimum, and a leg shorter than the minimum is flown as an
overshoot pair).

The search area is a fleet bound, not a per-drone one: each side must be at
least the largest ``min_move_cm`` of the units, so every unit can fly one
move across it (the room profile's 20 cm by default), and at most
``max_area_side_cm`` (``MAX_AREA_SIDE_CM`` by default, ``None`` for no cap).
The same cap bounds the plan's distance limits (geofence, minimum
separation and standoff).

Coverage gate
-------------
Whatever the footprint, ``check_plan`` measures the fraction of the search
area the drones' gate cameras ever see with ``coverage_sim.covered_fraction``
(exact along every horizontal line, with every polygon edge as a breakpoint
in ``y``) and refuses a plan below ``MIN_COVERAGE`` (99.5%, plan section 5.1).

Message caps
------------
Each drone's ``assign_lanes`` must fit the caps of the fleet protocol v2
(PR #96): at most ``MAX_LANES_PER_DRONE`` lanes, and at most
``MAX_MESSAGE_BYTES`` encoded as compact JSON, of which
``MESSAGE_RESERVE_BYTES`` are kept free for the v2 envelope fields.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from flightrisk.drone import coverage_sim as cs
from flightrisk.drone.coverage import SweepModel
from flightrisk.drone.move_range import _envelope, _int_cm, _move_range
from flightrisk.drone.safety import (
    DEFAULT_GEOFENCE_CM,
    HORIZONTAL_DIRECTIONS,
    MAX_MOVE_CM,
    MIN_ALT_SLOT_CM,
    MIN_MOVE_CM,
    ROOM_LIMITS,
    EnvelopeLimits,
    format_cm,
    slot_below_minimum,
)
from flightrisk.fleet_protocol import MAX_LANES as _PROTOCOL_MAX_LANES
from flightrisk.fleet_protocol import MAX_MESSAGE_BYTES as _PROTOCOL_MAX_MESSAGE_BYTES
from flightrisk.fleet_protocol import MAX_ROSTER, is_identifier, make_assign_lanes

__all__ = [
    "ALTITUDE_DEADBAND_CM",
    "ALTITUDE_MARGIN_CM",
    "ALTITUDE_TOLERANCE_CM",
    "COVERAGE_LINE_SPACING_CM",
    "DEFAULT_GAP_CM",
    "DEFAULT_MIN_SEPARATION_CM",
    "DEFAULT_PLACEMENT_TOLERANCE_CM",
    "DEFAULT_PLAN_GEOFENCE_CM",
    "MAX_AREA_SIDE_CM",
    "MAX_LANES_PER_DRONE",
    "MAX_MESSAGE_BYTES",
    "MAX_MOVES_PER_DRONE",
    "MESSAGE_RESERVE_BYTES",
    "MIN_COVERAGE",
    "MIN_SWEEP_CM",
    "SQUARE_FOOTPRINT",
    "STANDOFF_FROM_GEOFENCE",
    "PlanningFootprint",
    "check_plan",
    "gate_footprints",
    "planning_footprint",
    "required_gap_cm",
    "simulated_coverage",
]

DEFAULT_MIN_SEPARATION_CM = 150
"""Minimum horizontal separation D_min a plan must guarantee (plan section 3.5)."""

DEFAULT_PLACEMENT_TOLERANCE_CM = 10
"""Operator placement error assumed per drone (plan section 3.1, a guess until L3)."""

DEFAULT_GAP_CM = DEFAULT_MIN_SEPARATION_CM + 2 * DEFAULT_PLACEMENT_TOLERANCE_CM
"""Default corridor between drone regions: D_min plus the placement tolerance of both drones (170 cm)."""

DEFAULT_PLAN_GEOFENCE_CM = DEFAULT_GEOFENCE_CM
"""Geofence half-width plans are checked against by default: the edge's own default (``safety``)."""

MIN_COVERAGE = 0.995
"""Smallest simulated fraction of the area a feasible plan must see (plan section 5.1)."""

MAX_AREA_SIDE_CM = 20_000
"""Default largest area side accepted (``max_area_side_cm``): the room profile's cap, twice its
drone's published 100 m range. The ground passes each fleet's own cap, or None when the units are
not range limited."""

MAX_LANES_PER_DRONE = _PROTOCOL_MAX_LANES
"""Most lanes one drone's ``assign_lanes`` may carry: ``MAX_LANES`` of the fleet
protocol v2, imported from ``flightrisk.fleet_protocol`` so the two cannot drift."""

MAX_MOVES_PER_DRONE = MAX_LANES_PER_DRONE
"""Most executor moves one drone may be given. Every lane is already one move within the
drone's envelope profile (``min_move_cm`` to ``max_move_cm``, 20-500 cm for the room
profile), so this equals the lane cap of the fleet protocol whatever the drone; at 1.5 s
of overhead per move it is far beyond one battery."""

MAX_MESSAGE_BYTES = _PROTOCOL_MAX_MESSAGE_BYTES
"""Largest encoded control message: ``MAX_MESSAGE_BYTES`` of the fleet protocol v2,
imported from ``flightrisk.fleet_protocol``, measured as compact JSON
(``separators=(",", ":")``) in UTF-8."""

MESSAGE_RESERVE_BYTES = 1_024
"""Bytes of ``MAX_MESSAGE_BYTES`` kept free for the fields protocol v2 adds to a
message (``epoch``, ``leader_id``, ``cmd_id``, ``plan_id``, ``plan_seq``,
``on_complete`` and ``hover_timeout_s``), so a plan that fits here still fits there."""

ALTITUDE_DEADBAND_CM = MIN_MOVE_CM
"""How far above its slot a drone may hold: ``safety.SafetyEnvelope.slot_move`` makes
no correction while the rounded height error is below the 20 cm minimum move. The room
profile's default of ``planning_footprint``'s ``altitude_deadband_cm``."""

ALTITUDE_TOLERANCE_CM = 10
"""Height-sensor error assumed on top of the deadband (a guess until hardware rung L1).
The room profile's default of ``planning_footprint``'s ``altitude_tolerance_cm``."""

ALTITUDE_MARGIN_CM = ALTITUDE_DEADBAND_CM + ALTITUDE_TOLERANCE_CM
"""The gate camera is the camera this far above the slot (30 cm)."""

COVERAGE_LINE_SPACING_CM = 10.0
"""Largest spacing of the extra evaluation lines of the coverage gate's exact row scan."""

MIN_SWEEP_CM = 2 * MIN_MOVE_CM
"""Smallest sweep width accepted. With at least two minimum moves per sweep, a
pass can always stop half a sweep (20 cm or more) inside an edge, which leaves
room for the overshoot pair a leg shorter than 20 cm needs. The room profile's
default of ``planning_footprint``'s ``min_sweep_cm``."""

SQUARE_FOOTPRINT = "square"
"""Pass as ``footprint`` to plan with the plan's idealised square footprint."""

STANDOFF_FROM_GEOFENCE = "geofence"
"""Default ``max_standoff_cm``: the plan's ``geofence_cm``, or ``DEFAULT_PLAN_GEOFENCE_CM``
when the fence check is off. A drone that takes off on or inside the near edge can
never fly more than one fence half-width behind it, so a larger standoff cannot be
flown from there; the limit also bounds the floor the operator must keep clear."""

_MAX_SWEEP_CM = 2 * MAX_AREA_SIDE_CM
_MAX_PLACEMENT_TOLERANCE_CM = 500
_MAX_STAGGER_S = 60.0
# The room profile's (the Tello's) lowest slot (``safety.MIN_ALT_SLOT_CM``) and its
# highest. The checks now read each drone's ``EnvelopeLimits``; the names are kept.
_MIN_ALT_SLOT_CM = MIN_ALT_SLOT_CM
_MAX_ALT_SLOT_CM = MAX_MOVE_CM
_FROM_PLAN: Any = object()


# ----------------------------------------------------------------------
# Input validation
# ----------------------------------------------------------------------


def _number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return float(value)


def _pair(name: str, value: Any) -> tuple[Any, Any]:
    try:
        a, b = value
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a pair (x, y), got {value!r}") from None
    return a, b


def _slot_range(limits: Any) -> tuple[float, float]:
    """The lowest and highest altitude slot of an envelope profile."""
    limits = _envelope(limits)
    low = _number(f"{limits.name} min_alt_slot_cm", limits.min_alt_slot_cm)
    high = _number(f"{limits.name} max_alt_slot_cm", limits.max_alt_slot_cm)
    if not 0 < low <= high:
        raise ValueError(
            f"{limits.name} altitude slot range must be positive and not empty, "
            f"got {format_cm(low)}-{format_cm(high)} cm"
        )
    return low, high


def _per_drone_limits(unit_limits: Any, drone_ids: Sequence[Any]) -> list[EnvelopeLimits]:
    """Each drone's envelope profile, in the order of ``drone_ids``.

    ``None`` is ``ROOM_LIMITS`` for every drone. A mapping must hold a valid
    profile for every drone id; one that lacks a drone is refused rather than
    falling back to a default. Ids that are not in ``drone_ids`` are ignored.
    """
    if unit_limits is None:
        return [ROOM_LIMITS] * len(drone_ids)
    if not isinstance(unit_limits, Mapping):
        raise TypeError(f"unit_limits must map drone ids to safety.EnvelopeLimits, got {type(unit_limits).__name__}")
    missing = [drone_id for drone_id in drone_ids if drone_id not in unit_limits]
    if missing:
        raise ValueError(f"unit_limits has no envelope limits for {', '.join(map(repr, missing))}")
    profiles = [unit_limits[drone_id] for drone_id in drone_ids]
    for limits in profiles:
        _move_range(limits)
        _slot_range(limits)
    return profiles


def _fleet_min_move_cm(unit_limits: Any) -> int:
    """The largest ``min_move_cm`` of the units in ``unit_limits`` (the room profile's when None)."""
    if unit_limits is None:
        return _move_range(ROOM_LIMITS)[0]
    if not isinstance(unit_limits, Mapping):
        raise TypeError(f"unit_limits must map drone ids to safety.EnvelopeLimits, got {type(unit_limits).__name__}")
    if not unit_limits:
        raise ValueError("unit_limits must hold the envelope limits of at least one drone")
    return max(_move_range(limits)[0] for limits in unit_limits.values())


def _area_cap(max_area_side_cm: Any) -> int | None:
    """``None`` (no cap) or the largest area side as whole centimetres.

    Area sides are whole centimetres, so a fractional cap acts as its whole part.
    """
    if max_area_side_cm is None:
        return None
    cap = _number("max_area_side_cm", max_area_side_cm)
    if cap <= 0:
        raise ValueError(f"max_area_side_cm must be a positive distance or None, got {max_area_side_cm!r}")
    return math.floor(cap)


def _area(
    area_cm: Any,
    *,
    unit_limits: Mapping[str, EnvelopeLimits] | None = None,
    max_area_side_cm: float | None = MAX_AREA_SIDE_CM,
) -> tuple[int, int]:
    """The search area as whole-centimetre ``(width, depth)``.

    Each side must be at least the largest ``min_move_cm`` of the units in
    ``unit_limits`` (``ROOM_LIMITS`` when None), so every unit can fly one
    move across the area, and at most ``max_area_side_cm`` (None: no cap).
    """
    low = _fleet_min_move_cm(unit_limits)
    cap = _area_cap(max_area_side_cm)
    if cap is not None and cap < low:
        raise ValueError(f"max_area_side_cm {cap} cm is below the {low} cm shortest area side")
    if isinstance(area_cm, (int, float)) and not isinstance(area_cm, bool):
        width = depth = area_cm
    else:
        width, depth = _pair("area_cm", area_cm)
    return (
        _int_cm("area width", width, low, cap),
        _int_cm("area depth", depth, low, cap),
    )


def _ids(drone_ids: Sequence[str]) -> list[str]:
    if isinstance(drone_ids, str):
        raise TypeError(f"drone_ids must be a sequence of ids, got the string {drone_ids!r}")
    ids = [str(d) for d in drone_ids]
    if not ids:
        raise ValueError("at least one drone id is required")
    if len(set(ids)) != len(ids):
        raise ValueError(f"drone ids must be unique, got {ids!r}")
    if len(ids) > MAX_ROSTER:
        raise ValueError(f"a plan can have at most {MAX_ROSTER} drones (the fleet protocol roster cap), got {len(ids)}")
    for drone_id in ids:
        if not is_identifier(drone_id):
            raise ValueError(f"{drone_id!r} is not a drone id (an identifier: letters, digits, . _ : -)")
    return ids


def _slots(alt_slots_cm: Sequence[int], *, limits: EnvelopeLimits = ROOM_LIMITS) -> list[int]:
    """Whole-centimetre altitude slots, each within the slot range of ``limits``."""
    low, high = _slot_range(limits)
    slots = [_int_cm("altitude slot", a) for a in alt_slots_cm]
    if not slots:
        raise ValueError("alt_slots_cm must contain at least one altitude")
    for slot in slots:
        if not low <= slot <= high:
            reason = slot_below_minimum(slot, limits)
            raise ValueError(
                f"altitude slot must be within {format_cm(low)}-{format_cm(high)} cm, got {slot}"
                + (f": {reason}" if reason else "")
            )
    return slots


def _lkp(lkp_cm: Any, width: int, depth: int) -> tuple[int, int]:
    x, y = _pair("lkp_cm", lkp_cm)
    x, y = _int_cm("LKP x", x), _int_cm("LKP y", y)
    if not (0 <= x <= width and 0 <= y <= depth):
        raise ValueError(f"LKP ({x}, {y}) is outside the {width} x {depth} cm area")
    return x, y


def _optional_limit(name: str, value: Any, cap: int | None = MAX_AREA_SIDE_CM) -> float | None:
    """``None`` (the check is explicitly switched off) or a finite, positive distance of at most ``cap``.

    ``cap`` is the validated ``max_area_side_cm`` (``_area_cap``); None leaves no upper bound.
    """
    if value is None:
        return None
    value = _number(name, value)
    if cap is None:
        if value <= 0:
            raise ValueError(f"{name} must be a positive distance or None, got {value!r}")
        return value
    if not 0 < value <= cap:
        raise ValueError(f"{name} must be within (0, {cap}] cm or None, got {value!r}")
    return value


def _tolerance(value: Any) -> float:
    value = _number("placement_tolerance_cm", value)
    if not 0 <= value <= _MAX_PLACEMENT_TOLERANCE_CM:
        raise ValueError(f"placement_tolerance_cm must be within 0-{_MAX_PLACEMENT_TOLERANCE_CM} cm, got {value!r}")
    return value


def _stagger(value: Any) -> float:
    value = _number("stagger_s", value)
    if not 0 <= value <= _MAX_STAGGER_S:
        raise ValueError(f"stagger_s must be within 0-{_MAX_STAGGER_S:g} s, got {value!r}")
    return value


def _max_standoff(value: Any, geofence_cm: float | None, cap: int | None = MAX_AREA_SIDE_CM) -> float | None:
    if isinstance(value, str):
        if value != STANDOFF_FROM_GEOFENCE:
            raise ValueError(f"max_standoff_cm must be a distance, None or {STANDOFF_FROM_GEOFENCE!r}, got {value!r}")
        return geofence_cm if geofence_cm is not None else float(DEFAULT_PLAN_GEOFENCE_CM)
    return _optional_limit("max_standoff_cm", value, cap)


def _safety_inputs(
    geofence_cm: Any,
    min_separation_cm: Any,
    placement_tolerance_cm: Any,
    max_standoff_cm: Any = STANDOFF_FROM_GEOFENCE,
    *,
    max_area_side_cm: float | None = MAX_AREA_SIDE_CM,
) -> dict:
    """Validated ``geofence_cm``, ``min_separation_cm``, ``placement_tolerance_cm`` and ``max_standoff_cm`` plan fields.

    The distances are bounded by ``max_area_side_cm`` (None: only finite and positive).
    """
    cap = _area_cap(max_area_side_cm)
    fence = _optional_limit("geofence_cm", geofence_cm, cap)
    return {
        "geofence_cm": fence,
        "min_separation_cm": _optional_limit("min_separation_cm", min_separation_cm, cap),
        "placement_tolerance_cm": _tolerance(placement_tolerance_cm),
        "max_standoff_cm": _max_standoff(max_standoff_cm, fence, cap),
    }


def _altitude_margin(altitude_deadband_cm: Any, altitude_tolerance_cm: Any) -> float:
    """The gate camera's height above the slot: the deadband plus the height-sensor tolerance."""
    for name, value in (("altitude_deadband_cm", altitude_deadband_cm), ("altitude_tolerance_cm", altitude_tolerance_cm)):
        if _number(name, value) <= 0:
            raise ValueError(f"{name} must be a positive distance, got {value!r}")
    return altitude_deadband_cm + altitude_tolerance_cm


def required_gap_cm(
    min_separation_cm: float,
    drift_fraction: float,
    path_length_cm: float,
    placement_tolerance_cm: float = DEFAULT_PLACEMENT_TOLERANCE_CM,
) -> int:
    """Gap that keeps ``min_separation_cm`` despite drift and placement error on both drones.

    Implements ``gap >= D_min + 2 * (k * L + placement tolerance)`` from plan
    sections 3.1 and 3.5, where ``k`` is the dead-reckoning drift fraction and
    ``L`` the longest path flown before re-anchoring. Both ``k`` (3% is the
    plan's provisional guess) and the placement tolerance (10 cm) are
    unmeasured; set them from hardware rung L3 before relying on the result.
    Use the result as ``gap_cm``. To make a plan refuse a smaller gap, pass
    ``min_separation_cm=required_gap_cm(D_min, k, L, placement_tolerance_cm=0)``:
    ``check_plan`` adds the placement tolerance of both drones itself.
    """
    values = (min_separation_cm, drift_fraction, path_length_cm, placement_tolerance_cm)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError(
            "min_separation_cm, drift_fraction, path_length_cm and placement_tolerance_cm must be finite and not negative"
        )
    return math.ceil(min_separation_cm + 2 * (drift_fraction * path_length_cm + placement_tolerance_cm))


# ----------------------------------------------------------------------
# Planning footprint
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PlanningFootprint:
    """The rectangle of ground the planner relies on, relative to the drone (see the module docstring).

    ``kind`` is ``"square"`` or ``"forward"``. Every point with
    ``|dx| <= lateral_cm / 2`` and ``near_cm <= dy <= far_cm`` is inside the
    gate camera of every drone of the plan. ``along_cm`` is the spacing of
    legs across the heading and ``ahead_cm`` the centre of their band ahead
    of the drone (``ahead_cm - along_cm / 2 >= near_cm`` and ``ahead_cm +
    along_cm / 2 <= far_cm``). ``source`` is the footprint the caller passed
    and ``slot_cm`` the altitude a ``ForwardFootprint`` source describes.
    ``altitude_margin_cm`` is how far above its slot the gate camera sits
    (``ALTITUDE_MARGIN_CM`` unless ``planning_footprint`` was given other
    altitude margins); the coverage gate uses it too.
    """

    kind: str
    lateral_cm: int
    near_cm: float
    far_cm: float
    along_cm: int
    ahead_cm: int
    source: Any = None
    slot_cm: int | None = None
    altitude_margin_cm: float = ALTITUDE_MARGIN_CM

    def _camera_at(self, height_cm: float) -> cs.ForwardFootprint:
        if isinstance(self.source, SweepModel):
            return cs.ForwardFootprint.from_sweep_model(replace(self.source, height_m=height_cm / 100))
        if isinstance(self.source, cs.ForwardFootprint) and self.slot_cm:
            # A level camera over flat ground: the near edge moves out in proportion to the height.
            return replace(self.source, near_cm=self.source.near_cm * height_cm / self.slot_cm)
        raise TypeError(f"no camera model for footprint source {self.source!r}")

    def camera(self, alt_cm: float) -> cs.Footprint:
        """The real footprint of a drone holding exactly ``alt_cm``, for estimates."""
        if self.kind == SQUARE_FOOTPRINT:
            return cs.RectFootprint.square(self.lateral_cm)
        return self._camera_at(alt_cm)

    def gate_camera(self, alt_cm: float, placement_tolerance_cm: float) -> cs.Footprint:
        """The ground a drone of slot ``alt_cm`` sees within the altitude and placement margins.

        The camera sits ``altitude_margin_cm`` above the slot. The square model
        has no margins: it is an idealised model study.
        """
        if self.kind == SQUARE_FOOTPRINT:
            return cs.RectFootprint.square(self.lateral_cm)
        return self._camera_at(alt_cm + self.altitude_margin_cm).eroded(placement_tolerance_cm)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "lateral_cm": self.lateral_cm,
            "near_cm": self.near_cm,
            "far_cm": self.far_cm,
            "along_cm": self.along_cm,
            "ahead_cm": self.ahead_cm,
            "slot_cm": self.slot_cm,
        }


def planning_footprint(
    footprint: Any,
    sweep_cm: Any,
    alts_cm: Sequence[int],
    depth_cm: int,
    placement_tolerance_cm: float = DEFAULT_PLACEMENT_TOLERANCE_CM,
    *,
    min_sweep_cm: int = MIN_SWEEP_CM,
    altitude_deadband_cm: float = ALTITUDE_DEADBAND_CM,
    altitude_tolerance_cm: float = ALTITUDE_TOLERANCE_CM,
) -> PlanningFootprint:
    """Resolve the ``footprint`` and ``sweep_cm`` arguments of the plan builders.

    ``footprint`` is ``SQUARE_FOOTPRINT``, a ``coverage.SweepModel`` or a
    ``coverage_sim.ForwardFootprint`` (only when every altitude in
    ``alts_cm`` is the same); ``sweep_cm`` is the lane spacing (an upper
    bound for a camera footprint, and optional there). The forward rectangle
    is derived from every drone's gate camera (the module docstring).

    ``min_sweep_cm`` is the smallest sweep width accepted, and
    ``altitude_deadband_cm`` plus ``altitude_tolerance_cm`` how far above
    its slot a drone may hold; the defaults are the room profile's
    (``MIN_SWEEP_CM``, ``ALTITUDE_DEADBAND_CM`` and
    ``ALTITUDE_TOLERANCE_CM``). The result records the altitude margin, so
    ``check_plan`` gates the plan with the same gate cameras.
    """
    min_sweep = _int_cm("min_sweep_cm", min_sweep_cm, 1, _MAX_SWEEP_CM)
    margin = _altitude_margin(altitude_deadband_cm, altitude_tolerance_cm)
    if isinstance(footprint, str):
        if footprint != SQUARE_FOOTPRINT:
            raise ValueError(f"footprint must be {SQUARE_FOOTPRINT!r}, a SweepModel or a ForwardFootprint, got {footprint!r}")
        if sweep_cm is None:
            raise ValueError("the square footprint needs sweep_cm")
        side = _int_cm("sweep_cm", sweep_cm, min_sweep, _MAX_SWEEP_CM)
        return PlanningFootprint(SQUARE_FOOTPRINT, side, -side / 2, side / 2, side, 0, SQUARE_FOOTPRINT)
    alts = sorted(set(alts_cm))
    tolerance = _tolerance(placement_tolerance_cm)
    if isinstance(footprint, SweepModel):
        base = PlanningFootprint("forward", 0, 0, 0, 0, 0, footprint, altitude_margin_cm=margin)
    elif isinstance(footprint, cs.ForwardFootprint):
        if len(alts) != 1:
            raise ValueError(
                f"a ForwardFootprint describes the camera at one altitude, but the drones fly altitude slots {alts} cm; "
                "give every drone the same slot, or pass a SweepModel so each slot is modelled"
            )
        base = PlanningFootprint("forward", 0, 0, 0, 0, 0, footprint, alts[0], altitude_margin_cm=margin)
    else:
        raise TypeError(
            f"footprint must be {SQUARE_FOOTPRINT!r}, a coverage.SweepModel or a coverage_sim.ForwardFootprint, "
            f"got {type(footprint).__name__}"
        )
    cameras = [base.gate_camera(alt, tolerance) for alt in alts]
    half_tan = min(c.half_tan for c in cameras)
    inset = max(c.inset_cm for c in cameras)
    near = max(c.near_cm for c in cameras)
    far = min(c.far_cm for c in cameras)
    reach = min(min(c.max_half_width_cm for c in cameras), far * half_tan - inset)
    if far <= near or reach <= 0:
        raise ValueError(
            f"the camera sees nothing on the ground at altitude slots {alts} cm within the altitude and "
            f"placement margins: near edge {near:.0f} cm, far edge {far:.0f} cm"
        )
    widest = math.floor(2 * reach - 1e-6)
    if sweep_cm is None:
        lateral = min(max(math.floor(2 * (near * half_tan - inset) - 1e-6), min_sweep), widest)
    else:
        lateral = min(_int_cm("sweep_cm", sweep_cm, min_sweep, _MAX_SWEEP_CM), widest)
    if lateral < min_sweep:
        raise ValueError(
            f"the camera footprint is at most {max(lateral, 0)} cm wide where it is fully visible, "
            f"below the {min_sweep} cm minimum sweep"
        )
    lo = math.ceil(max(near, (lateral / 2 + inset) / half_tan) + 1e-6)
    hi = math.floor(far - 1e-6)
    if hi < lo:
        raise ValueError(
            f"a {lateral} cm wide pass is only fully visible beyond the camera's {far:.0f} cm far edge; "
            "use a narrower sweep_cm"
        )
    window = hi - lo
    along = min(window - window % 2, depth_cm + depth_cm % 2)
    return replace(base, lateral_cm=lateral, near_cm=lo, far_cm=hi, along_cm=along, ahead_cm=lo + along // 2)


# ----------------------------------------------------------------------
# Plan checks
# ----------------------------------------------------------------------


def gate_footprints(plan: Any, placement_tolerance_cm: float | None = None) -> list[cs.Footprint]:
    """Every drone's gate camera: its footprint within the altitude and placement margins.

    ``placement_tolerance_cm`` defaults to the plan's own. A plan without a
    planning footprint is a square-model plan of ``sweep_cm``.
    """
    if plan.footprint is None:
        return [cs.RectFootprint.square(plan.sweep_cm)] * len(plan.drones)
    tolerance = plan.placement_tolerance_cm if placement_tolerance_cm is None else placement_tolerance_cm
    return [plan.footprint.gate_camera(drone.alt_cm, tolerance) for drone in plan.drones]


def simulated_coverage(plan: Any, placement_tolerance_cm: float | None = None) -> float:
    """Fraction of the search area the gate cameras of ``plan`` ever see (``coverage_sim.covered_fraction``)."""
    width, depth = plan.area_cm
    paths = [drone.waypoints() for drone in plan.drones]
    return cs.covered_fraction(
        width, depth, paths, gate_footprints(plan, placement_tolerance_cm), line_spacing_cm=COVERAGE_LINE_SPACING_CM
    )


def _lane_problems(drone: Any, *, limits: EnvelopeLimits = ROOM_LIMITS) -> list[str]:
    """Lanes that are not one whole-centimetre horizontal move within the move range of ``limits``."""
    low, high = _move_range(limits)
    problems = []
    if not drone.lanes:
        # The executor refuses start_at with no lanes (``no_lanes``).
        problems.append(f"{drone.drone_id}: has no lanes to fly; one footprint already covers its area")
    if len(drone.lanes) > MAX_LANES_PER_DRONE:
        problems.append(
            f"{drone.drone_id}: {len(drone.lanes)} lanes exceed the {MAX_LANES_PER_DRONE} lane cap of one assign_lanes"
        )
    for index, lane in enumerate(drone.lanes):
        if not isinstance(lane, Mapping):
            problems.append(f"{drone.drone_id}: lane {index} {lane!r} is not a {low}-{high} cm horizontal move")
            continue
        distance = lane.get("distance_cm")
        if (
            set(lane) != {"direction", "distance_cm"}
            or lane.get("direction") not in HORIZONTAL_DIRECTIONS
            or isinstance(distance, bool)
            or not isinstance(distance, int)
            or not low <= distance <= high
        ):
            problems.append(f"{drone.drone_id}: lane {index} {lane!r} is not a {low}-{high} cm horizontal move")
    return problems


def _message_problems(drone: Any) -> list[str]:
    """The drone's ``assign_lanes`` must be valid and fit the protocol v2 size cap."""
    try:
        fields = drone.assign_lanes_fields()
        message = make_assign_lanes(drone.drone_id, fields["alt_cm"], fields["lanes"], fields["zone"])
        size = len(json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        return [f"{drone.drone_id}: its assign_lanes message is invalid: {exc}"]
    limit = MAX_MESSAGE_BYTES - MESSAGE_RESERVE_BYTES
    if size > limit:
        return [f"{drone.drone_id}: its assign_lanes message is {size} bytes, above the {limit} byte limit"]
    return []


def _path_problems(drone: Any, flight_area: tuple[int, int, int, int], geofence_cm: float | None) -> list[str]:
    problems = []
    fx0, fy0, fx1, fy1 = flight_area
    x0, y0 = drone.takeoff_cm
    fence_reported = False
    for point in drone.waypoints():
        if not drone.region.contains(point):
            problems.append(f"{drone.drone_id}: waypoint {point} leaves its region {drone.region.zone_dict()}")
            break
        if not (fx0 <= point[0] <= fx1 and fy0 <= point[1] <= fy1):
            problems.append(f"{drone.drone_id}: waypoint {point} leaves the flight area {flight_area}")
            break
        rel = (point[0] - x0, point[1] - y0)
        if geofence_cm is not None and not fence_reported and (abs(rel[0]) > geofence_cm or abs(rel[1]) > geofence_cm):
            problems.append(f"{drone.drone_id}: waypoint {rel} (relative to takeoff) leaves the {geofence_cm:g} cm geofence")
            fence_reported = True
    return problems


def check_plan(
    plan: Any,
    *,
    geofence_cm: Any = _FROM_PLAN,
    min_separation_cm: Any = _FROM_PLAN,
    placement_tolerance_cm: Any = _FROM_PLAN,
    max_standoff_cm: Any = _FROM_PLAN,
    min_coverage: float | None = MIN_COVERAGE,
    unit_limits: Mapping[str, EnvelopeLimits] | None = None,
    max_area_side_cm: float | None = MAX_AREA_SIDE_CM,
) -> list[str]:
    """Every reason ``plan`` must not be flown, or an empty list.

    Checks that each lane is a move the executor accepts, that each drone has
    at most ``MAX_LANES_PER_DRONE`` lanes and an ``assign_lanes`` message
    within the protocol's size cap; that every waypoint lies inside the
    drone's region and the plan's flight area (the search area, extended
    ``standoff_cm`` behind its near edge); that the standoff is at most
    ``max_standoff_cm``; that every waypoint lies inside the square geofence
    of half-width ``geofence_cm`` around the drone's takeoff point; that
    regions are at least ``gap_cm`` apart; that the guaranteed separation is
    at least ``min_separation_cm + 2 * placement_tolerance_cm``; and that the
    gate cameras see at least ``min_coverage`` of the area. The limits
    default to the values the plan was built with; ``None`` skips a check
    (for model studies only), and non-finite values are refused.

    ``unit_limits`` maps every drone id of the plan to its
    ``safety.EnvelopeLimits``: each lane must be a move within that
    profile's move range and each altitude slot within its slot range.
    ``None`` checks every drone against ``ROOM_LIMITS``; a mapping that
    lacks a drone of the plan raises ``ValueError``. ``max_area_side_cm``
    bounds the geofence, separation and standoff distances
    (``MAX_AREA_SIDE_CM`` by default; ``None`` leaves them only finite and
    positive).
    """
    return _check(
        plan, geofence_cm=geofence_cm, min_separation_cm=min_separation_cm,
        placement_tolerance_cm=placement_tolerance_cm, max_standoff_cm=max_standoff_cm, min_coverage=min_coverage,
        unit_limits=unit_limits, max_area_side_cm=max_area_side_cm,
    )[0]


def _check(
    plan: Any,
    *,
    geofence_cm: Any = _FROM_PLAN,
    min_separation_cm: Any = _FROM_PLAN,
    placement_tolerance_cm: Any = _FROM_PLAN,
    max_standoff_cm: Any = _FROM_PLAN,
    min_coverage: float | None = MIN_COVERAGE,
    unit_limits: Mapping[str, EnvelopeLimits] | None = None,
    max_area_side_cm: float | None = MAX_AREA_SIDE_CM,
) -> tuple[list[str], float | None]:
    """``check_plan`` plus the measured coverage (None when the lanes are too broken to simulate)."""
    fence = plan.geofence_cm if geofence_cm is _FROM_PLAN else geofence_cm
    min_sep = plan.min_separation_cm if min_separation_cm is _FROM_PLAN else min_separation_cm
    tolerance = plan.placement_tolerance_cm if placement_tolerance_cm is _FROM_PLAN else placement_tolerance_cm
    standoff_cap = plan.max_standoff_cm if max_standoff_cm is _FROM_PLAN else max_standoff_cm
    limits = _safety_inputs(fence, min_sep, tolerance, standoff_cap, max_area_side_cm=max_area_side_cm)
    if min_coverage is not None:
        min_coverage = _number("min_coverage", min_coverage)
        if not 0 <= min_coverage <= 1:
            raise ValueError(f"min_coverage must be within [0, 1], got {min_coverage!r}")
    profiles = _per_drone_limits(unit_limits, [drone.drone_id for drone in plan.drones])
    problems: list[str] = []
    width, depth = plan.area_cm
    standoff = plan.standoff_cm
    if isinstance(standoff, bool) or not isinstance(standoff, int) or standoff < 0:
        problems.append(f"standoff_cm {standoff!r} must be a whole, non-negative number of centimetres")
        standoff = 0
    elif limits["max_standoff_cm"] is not None and standoff > limits["max_standoff_cm"]:
        problems.append(
            f"the plan flies {standoff} cm behind the near edge of the area, beyond the "
            f"{limits['max_standoff_cm']:g} cm max_standoff_cm"
        )
    flight_area = (0, -standoff, width, depth)
    broken = False
    if len(plan.drones) > MAX_ROSTER:
        problems.append(f"the plan has {len(plan.drones)} drones, above the {MAX_ROSTER} drone roster cap of the fleet protocol")
    for drone, profile in zip(plan.drones, profiles):
        if not is_identifier(drone.drone_id):
            problems.append(f"{drone.drone_id!r} is not a drone id (an identifier: letters, digits, . _ : -)")
        lane_problems = _lane_problems(drone, limits=profile)
        problems.extend(lane_problems)
        lowest, highest = _slot_range(profile)
        if not lowest <= drone.alt_cm <= highest:
            low = slot_below_minimum(drone.alt_cm, profile)
            problems.append(f"{drone.drone_id}: altitude slot {drone.alt_cm} cm is out of range" + (f" ({low})" if low else ""))
        if drone.lanes and lane_problems:  # a drone with no lanes at all is not "broken lanes"
            broken = True
            continue
        problems.extend(_message_problems(drone))
        problems.extend(_path_problems(drone, flight_area, limits["geofence_cm"]))
    for i, a in enumerate(plan.drones):
        for b in plan.drones[i + 1:]:
            distance = a.region.distance_to(b.region)
            if distance < plan.gap_cm:
                problems.append(f"{a.drone_id} and {b.drone_id}: regions are {distance:g} cm apart, below the gap")
    separation = plan.separation_cm
    if limits["min_separation_cm"] is not None and len(plan.drones) > 1:
        required = limits["min_separation_cm"] + 2 * limits["placement_tolerance_cm"]
        if separation < required:
            problems.append(
                f"guaranteed separation {separation:g} cm is below the {required:g} cm minimum "
                f"({limits['min_separation_cm']:g} cm D_min plus 2 x {limits['placement_tolerance_cm']:g} cm "
                "placement tolerance)"
            )
    coverage = None
    if broken:
        if min_coverage is not None:
            problems.append("coverage was not simulated because some lanes are not valid moves")
    else:
        coverage = simulated_coverage(plan, limits["placement_tolerance_cm"])
        if min_coverage is not None and coverage < min_coverage:
            problems.append(
                f"the {plan.footprint_kind} camera footprint sees only {coverage:.1%} of the area "
                f"in simulation, below the {min_coverage:.1%} minimum"
            )
    return problems, coverage
