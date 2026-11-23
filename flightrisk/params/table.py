"""The flight parameter registry, rows 1 to 86 of ``parameters.md`` section 1.

Pure data. Every default is the value today's code uses (risk R1): where a
later PR changes a value, the row keeps today's value and ``PENDING`` records
the later value with the PR that makes the change. Rows 53 and 85 were removed
and are absent. Distances are centimetres.

Non-numeric default markers (risk R3) are the module constants ``UNSET``,
``DEFERRED``, ``PER_MISSION`` and ``DERIVED_NADIR_SWATH``. A ``None`` default
means the row does not apply to that model.

Tello tuning facts have ``Edit.FACT`` and are read-only and never user-facing
(risk R6).
"""

from __future__ import annotations

from typing import Any, Optional

from flightrisk.params.spec import (
    Applies,
    Bound,
    Confirm,
    Edit,
    EnvAlias,
    Kind,
    ParamSpec,
    Safer,
    Scope,
    Unit,
)

T = "tello"
G = "generic_multirotor"

UNSET = "unset"
DEFERRED = "deferred"
PER_MISSION = "per_mission"
DERIVED_NADIR_SWATH = "derived:nadir_swath"

#: Sentence every fact row carries in its description (risk R6).
FACT_NOTE = "Test-adapter fact, read-only, not operator-facing."

#: The safe value of rows whose safer direction is a named value, not higher or lower.
SAFE_VALUES: dict[str, Any] = {
    "link.require_failsafes": True,
    "tello.temp_refuse_c": "on",
    "sensing.turn": True,
    "sensing.mode": "required",
}

#: Later values from rows tagged "from <PR>" in parameters.md. ``value`` is
#: None when the later value is a behaviour change, not a number.
PENDING: dict[str, tuple[dict[str, Any], ...]] = {
    "envelope.geofence_cm": ({"pr": "C5", "item": 29, "profile": T, "value": 450},),
    "envelope.alt_slot_cm": ({"pr": "C5", "item": 30, "profile": None, "value": None, "what": "confirmation"},),
    "battery.min_takeoff_pct": ({"pr": "C6", "item": 33, "profile": G, "value": 40},),
    "battery.critical_pct": ({"pr": "B4", "item": 8, "profile": None, "value": None, "what": "autopilot critical level"},),
    "link.loss_wait_s": (
        {"pr": "C7", "item": 34, "profile": T, "value": 10.0},
        {"pr": "C5", "item": 26, "profile": G, "value": None, "what": "bound 1.5-30"},
    ),
    "link.loss_action": ({"pr": "C7", "item": 34, "profile": T, "value": "return"},),
    "stranded.countdown_s": (
        {"pr": "C6", "item": 32, "profile": G, "value": None, "what": "ends in a return (field)"},
        {"pr": "C7", "item": 34, "profile": T, "value": None, "what": "ends in a return (indoor)"},
    ),
    "separation.base_cm": ({"pr": "D1", "item": 9, "profile": G, "value": 500},),
    "plan.lost_obstacle_s": ({"pr": "D2", "item": 18, "profile": None, "value": None, "what": "row removed"},),
}


def _p(
    id: str,
    unit: Unit,
    kind: Kind,
    description: str,
    scope: Scope,
    defaults: tuple[Any, Any],
    bounds: tuple[Optional[Bound], Optional[Bound]],
    safer: Safer,
    confirm: Confirm,
    edit: tuple[Edit, Edit],
    applies: Applies,
    **extra: Any,
) -> ParamSpec:
    """Build one row from a (tello, generic) pair per column."""
    return ParamSpec(
        id=id,
        unit=unit,
        kind=kind,
        description=description,
        group=id.split(".", 1)[0],
        scope=scope,
        defaults={T: defaults[0], G: defaults[1]},
        bounds={T: bounds[0], G: bounds[1]},
        safer=safer,
        confirm=confirm,
        edit={T: edit[0], G: edit[1]},
        applies=applies,
        **extra,
    )


_B = Bound
_ND = Scope.DRONE
_UI = (Edit.UI, Edit.UI)
_FE = (Edit.FILE_ENV, Edit.FILE_ENV)
_RO = (Edit.READ_ONLY, Edit.READ_ONLY)
_FACT = (Edit.FACT, Edit.FACT)
_NS = Applies.NEXT_START
_NONE = (Safer.NONE, Confirm.NONE)

ROWS_1_86: tuple[ParamSpec, ...] = (
    _p("envelope.geofence_cm", Unit.CM, Kind.INT, "Horizontal geofence half-width.", _ND,
       (500, 3000), (_B(40, 5000), _B(500, 50000)), Safer.LOWER, Confirm.LESS_SAFE, _UI, _NS,
       env="FLIGHTRISK_GEOFENCE_CM",
       env_aliases=(EnvAlias("FLIGHTRISK_FIELD_GEOFENCE_M", 100.0, "field"),), cli=("--geofence-m",)),
    _p("envelope.ceiling_cm", Unit.CM, Kind.INT, "Flight ceiling above the takeoff point.", _ND,
       (200, 3000), (_B(150, 1000), _B(1000, 12000)), Safer.LOWER, Confirm.LESS_SAFE, _UI, Applies.ON_GROUND,
       env="FLIGHTRISK_CEILING_CM",
       env_aliases=(EnvAlias("FLIGHTRISK_FIELD_CEILING_M", 100.0, "field"),), cli=("--ceiling-m",)),
    _p("envelope.ceiling_margin_cm", Unit.CM, Kind.INT, "Margin kept below the ceiling.", _ND,
       (60, 500), (_B(30, 500), _B(300, 3000)), Safer.HIGHER, Confirm.LESS_SAFE, _UI, Applies.ON_GROUND,
       env="FLIGHTRISK_CEILING_MARGIN_CM",
       env_aliases=(EnvAlias("FLIGHTRISK_FIELD_CEILING_MARGIN_M", 100.0, "field"),),
       cli=("--ceiling-margin-m",)),
    _p("envelope.alt_slot_cm", Unit.CM, Kind.INT, "Altitude slot of one drone; also its return altitude.", _ND,
       (100, 1500), (_B(50, 500), _B(1000, 12000)), Safer.HIGHER, Confirm.LESS_SAFE, _UI, Applies.ON_GROUND,
       env="FLIGHTRISK_FIELD_ALT_SLOT_M", cli=("--alt-slot", "--alt-slot-m")),
    _p("plan.alt_slots_cm", Unit.CM_LIST, Kind.INT_LIST, "Zone default altitude slots.", Scope.MISSION,
       ((100, 130), (1000, 1500)), (None, None), Safer.NONE, Confirm.NONE, _FE, Applies.PLAN),
    _p("move.min_cm", Unit.CM, Kind.INT, "Smallest move the drone accepts.", _ND,
       (20, 20), (_B(20, 20), _B(20, 20)), Safer.NONE, Confirm.NONE, _RO, _NS),
    _p("move.max_cm", Unit.CM, Kind.INT, "Largest single move the drone accepts.", _ND,
       (500, 5000), (_B(500, 500), _B(5000, 5000)), Safer.NONE, Confirm.NONE, _RO, _NS),
    _p("envelope.max_altitude_cm", Unit.CM, Kind.INT, "Highest altitude the model declares.", _ND,
       (1000, 12000), (_B(1000, 1000), _B(12000, 12000)), Safer.NONE, Confirm.NONE, _RO, _NS),
    _p("envelope.fence_breach_margin_cm", Unit.CM, Kind.INT, "GPS noise allowance before a breach landing.", _ND,
       (None, 300), (None, _B(100, 1000)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS),
    _p("envelope.autopilot_fence_margin_cm", Unit.CM, Kind.INT,
       "Margin by which the autopilot fence contains the edge fence.", _ND,
       (None, 500), (None, _B(200, 2000)), Safer.NONE, Confirm.NONE, _FE, _NS),
    _p("battery.min_takeoff_pct", Unit.PCT, Kind.INT, "Lowest battery level that allows a takeoff.", _ND,
       (30, 30), (_B(20, 95), _B(20, 95)), Safer.HIGHER, Confirm.LESS_SAFE, _FE, _NS,
       env="FLIGHTRISK_MIN_TAKEOFF_BATTERY"),
    _p("battery.critical_pct", Unit.PCT, Kind.INT, "Battery level that lands the drone at once.", _ND,
       (15, 15), (_B(10, 50), _B(10, 50)), Safer.HIGHER, Confirm.LESS_SAFE, _FE, _NS,
       env="FLIGHTRISK_LAND_BATTERY"),
    _p("battery.display_warn_pct", Unit.PCT, Kind.INT, "Battery level the ground display shows as low.",
       Scope.GROUND, (20, 20), (_B(5, 80), _B(5, 80)), Safer.NONE, Confirm.NONE, _FE, _NS),
    _p("battery.display_critical_pct", Unit.PCT, Kind.INT,
       "Battery level the ground display shows as critical.", Scope.GROUND,
       (10, 10), (_B(5, 50), _B(5, 50)), Safer.NONE, Confirm.NONE, _FE, _NS),
    _p("battery.rth_min_pct", Unit.PCT, Kind.INT, "Lowest battery level for a return-to-home recovery.", _ND,
       (None, 40), (None, _B(20, 90)), Safer.HIGHER, Confirm.LESS_SAFE, _FE, _NS),
    _p("link.loss_detect_s", Unit.S, Kind.FLOAT, "Seconds without a link before it counts as lost.", _ND,
       (3.0, 3.0), (_B(1.5, 10), _B(1.5, 30)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS,
       env="FLIGHTRISK_LINK_LOSS_LAND_S"),
    _p("link.loss_wait_s", Unit.S, Kind.FLOAT, "Extra hold after a long ground-link loss, before the loss action.",
       _ND, (0, 10.0), (_B(1.5, 30), _B(1.5, 30)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS,
       off_value=0, env="FLIGHTRISK_LINK_LOSS_WAIT_S",
       env_aliases=(EnvAlias("FLIGHTRISK_FIELD_LINK_LOSS_HOLD_S", 1.0),), cli=("--link-loss-hold-s",)),
    _p("link.loss_action", Unit.ENUM, Kind.ENUM, "What the drone does after a long ground-link loss.", _ND,
       ("land", "rtl"), (None, None), Safer.NONE, Confirm.NONE, _FE, _NS,
       choices=("return", "land", "rtl"), env="FLIGHTRISK_FAILSAFE_ACTION", cli=("--failsafe-action",)),
    _p("link.require_failsafes", Unit.BOOL, Kind.BOOL, "Refuse to fly unless the autopilot failsafes are set.", _ND,
       (None, True), (None, None), Safer.NONE, Confirm.LESS_SAFE, _FE, _NS,
       off_value=False, safety_switch=True),
    _p("link.drone_stale_s", Unit.S, Kind.FLOAT, "Seconds of silence from the drone before its data is stale.", _ND,
       (None, 3.0), (None, _B(1, 10)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS),
    _p("link.rtl_accept_s", Unit.S, Kind.FLOAT, "Seconds the autopilot has to accept a return command.", _ND,
       (None, 5.0), (None, _B(2, 30)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS),
    _p("gps.min_satellites", Unit.COUNT, Kind.INT, "Fewest GPS satellites that allow a takeoff.", _ND,
       (None, 8), (None, _B(6, 20)), Safer.HIGHER, Confirm.LESS_SAFE, _FE, _NS),
    _p("stranded.countdown_s", Unit.S, Kind.INT, "Seconds before a stranded drone lands.", Scope.GROUND,
       (120, 120), (_B(30, 600), _B(30, 600)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS,
       env="FLIGHTRISK_MANUAL_ORPHAN_LAND_S"),
    _p("takeoff.reply_timeout_s", Unit.S, Kind.FLOAT, "Seconds to wait for the takeoff reply.", _ND,
       (20.0, 60.0), (_B(20, 20), _B(20, 180)), Safer.NONE, Confirm.NONE, (Edit.READ_ONLY, Edit.FILE_ENV), _NS),
    # Rows 25 to 33: takeoff.
    _p("takeoff.late_s", Unit.S, Kind.FLOAT, "Seconds after which a takeoff counts as late. " + FACT_NOTE, _ND,
       (10.0, None), (_B(2, 20), None), Safer.NONE, Confirm.NONE, _FACT, _NS,
       env="FLIGHTRISK_TELLO_TAKEOFF_LATE_S"),
    _p("takeoff.settle_samples", Unit.COUNT, Kind.INT, "Height samples that must agree after takeoff. " + FACT_NOTE,
       _ND, (5, None), (_B(2, 50), None), Safer.NONE, Confirm.NONE, _FACT, _NS,
       env="FLIGHTRISK_TELLO_SETTLE_SAMPLES"),
    _p("takeoff.settle_tolerance_cm", Unit.CM, Kind.FLOAT, "Height spread that counts as settled. " + FACT_NOTE,
       _ND, (10.0, None), (_B(2, 50), None), Safer.NONE, Confirm.NONE, _FACT, _NS,
       env="FLIGHTRISK_TELLO_SETTLE_TOLERANCE_CM"),
    _p("takeoff.settle_timeout_s", Unit.S, Kind.FLOAT, "Seconds allowed to settle after takeoff. " + FACT_NOTE,
       _ND, (5.0, None), (_B(1, 10), None), Safer.NONE, Confirm.NONE, _FACT, _NS,
       env="FLIGHTRISK_TELLO_SETTLE_TIMEOUT_S"),
    _p("takeoff.refusal_retries", Unit.COUNT, Kind.INT, "Retries after the drone refuses a takeoff. " + FACT_NOTE,
       _ND, (2, None), (_B(0, 3), None), Safer.NONE, Confirm.NONE, _FACT, _NS,
       env="FLIGHTRISK_TELLO_REFUSAL_RETRIES"),
    _p("takeoff.refusal_hold_s", Unit.S, Kind.FLOAT, "Seconds held between takeoff refusals. " + FACT_NOTE,
       _ND, (2.5, None), (_B(0.5, 5), None), Safer.NONE, Confirm.NONE, _FACT, _NS,
       env="FLIGHTRISK_TELLO_REFUSAL_HOLD_S"),
    _p("takeoff.descent_clearance_cm", Unit.CM, Kind.INT, "Clearance kept when descending to the lowest slot.", _ND,
       (50, None), (_B(50, 50), None), Safer.NONE, Confirm.NONE, _RO, _NS),
    _p("takeoff.height_cm", Unit.CM, Kind.INT, "Hover height after takeoff.", _ND,
       (80, 500), (_B(80, 80), _B(200, 2000)), Safer.NONE, Confirm.NONE, (Edit.READ_ONLY, Edit.FILE_ENV), _NS,
       cli=("--takeoff-alt-m",)),
    _p("mode.hover_test", Unit.BOOL, Kind.BOOL, "Hover-only test mode. " + FACT_NOTE, Scope.MISSION,
       (False, False), (None, None), Safer.NONE, Confirm.NONE, _FACT, _NS, env="FLIGHTRISK_HOVER_TEST"),
    # Rows 34 to 36: Tello ground detection.
    _p("tello.ground_height_cm", Unit.CM, Kind.INT, "Height at or below which the Tello counts as on the ground.",
       _ND, (10, None), (_B(10, 10), None), Safer.NONE, Confirm.NONE, _RO, _NS),
    _p("tello.ground_tof_cm", Unit.CM, Kind.INT, "Time-of-flight reading at or below which the Tello is grounded.",
       _ND, (30, None), (_B(30, 30), None), Safer.NONE, Confirm.NONE, _RO, _NS),
    _p("tello.ground_readings", Unit.COUNT, Kind.INT, "Consecutive ground readings that confirm a landing.", _ND,
       (3, None), (_B(3, 3), None), Safer.NONE, Confirm.NONE, _RO, _NS),
    # Rows 37 to 39: temperature and sensing switches.
    _p("tello.temp_warn_c", Unit.DEG_C, Kind.INT, "Temperature that raises a warning. " + FACT_NOTE, _ND,
       (92, None), (_B(40, 120), None), Safer.NONE, Confirm.NONE, _FACT, _NS,
       env="FLIGHTRISK_TELLO_TEMP_WARN_C"),
    _p("tello.temp_refuse_c", Unit.DEG_C, Kind.INT, "Temperature that refuses a takeoff. " + FACT_NOTE, _ND,
       (95, None), (_B(40, 120), None), Safer.NONE, Confirm.LESS_SAFE, _FACT, _NS,
       off_value="off", safety_switch=True, env="FLIGHTRISK_TELLO_TEMP_REFUSE_C"),
    _p("sensing.turn", Unit.BOOL, Kind.BOOL, "Turn toward an obstacle before a sense. " + FACT_NOTE, Scope.MISSION,
       (True, True), (None, None), Safer.NONE, Confirm.LESS_SAFE, _FACT, _NS,
       off_value=False, safety_switch=True, env="FLIGHTRISK_OBSTACLE_TURN"),
    _p("sensing.mode", Unit.ENUM, Kind.ENUM, "Whether obstacle sensing is required.", Scope.MISSION,
       ("required", "required"), (None, None), Safer.NONE, Confirm.LESS_SAFE, _UI, _NS,
       choices=("required", "off"), off_value="off", safety_switch=True, env="FLIGHTRISK_OBSTACLE_SENSING"),
    # Rows 41 to 52: obstacle sensing facts.
    _p("sensing.step_cm", Unit.CM, Kind.INT, "Move length between obstacle senses. " + FACT_NOTE, _ND,
       (50, 500), (_B(20, 100), _B(20, 5000)), Safer.NONE, Confirm.NONE, _FACT, _NS,
       env="FLIGHTRISK_OBSTACLE_STEP_CM"),
    _p("sensing.settle_s", Unit.S, Kind.FLOAT, "Seconds to settle before a sense. " + FACT_NOTE, _ND,
       (0.3, 0.3), (_B(0.1, 3.0), _B(0.1, 3.0)), Safer.NONE, Confirm.NONE, _FACT, _NS,
       env="FLIGHTRISK_OBSTACLE_SETTLE_S"),
    _p("sensing.min_safe_depth", Unit.RATIO, Kind.FLOAT, "Depth ratio below which a view is blocked. " + FACT_NOTE,
       _ND, (0.35, 0.35), (_B(0.05, 0.9), _B(0.05, 0.9)), Safer.NONE, Confirm.NONE, _FACT, _NS,
       env="FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH"),
    _p("sensing.person_fraction", Unit.RATIO, Kind.FLOAT, "View fraction a person must fill to block. " + FACT_NOTE,
       _ND, (0.55, 0.55), (_B(0.2, 0.9), _B(0.2, 0.9)), Safer.NONE, Confirm.NONE, _FACT, _NS,
       env="FLIGHTRISK_OBSTACLE_PERSON_FRACTION"),
    _p("sensing.max_decode_lag_s", Unit.S, Kind.FLOAT, "Largest accepted video decode lag. " + FACT_NOTE, _ND,
       (0.3, None), (_B(0.1, 2.0), None), Safer.NONE, Confirm.NONE, _FACT, _NS,
       env="FLIGHTRISK_MAX_DECODE_LAG_S"),
    _p("sensing.view_wait_s", Unit.S, Kind.FLOAT, "Seconds to wait for a camera frame. " + FACT_NOTE, _ND,
       (0.7, 0.7), (_B(0.2, 3.0), _B(0.2, 3.0)), Safer.NONE, Confirm.NONE, _FACT, _NS),
    _p("sensing.sense_timeout_s", Unit.S, Kind.FLOAT, "Seconds budget for one sense. " + FACT_NOTE, _ND,
       (2.0, 2.0), (_B(0.5, 10), _B(0.5, 10)), Safer.NONE, Confirm.NONE, _FACT, _NS),
    _p("sensing.sense_retries", Unit.COUNT, Kind.INT, "Sense attempts before the sensor counts as failed. " + FACT_NOTE,
       _ND, (3, 3), (_B(1, 5), _B(1, 5)), Safer.NONE, Confirm.NONE, _FACT, _NS),
    _p("sensing.sensor_down_s", Unit.S, Kind.FLOAT, "Seconds of failed senses before the sensor counts as down. " + FACT_NOTE,
       _ND, (10.0, 10.0), (_B(2, 30), _B(2, 30)), Safer.NONE, Confirm.NONE, _FACT, _NS),
    _p("sensing.person_wait_s", Unit.S, Kind.FLOAT, "Seconds to wait for a person detection. " + FACT_NOTE, _ND,
       (1.0, 1.0), (_B(0.2, 5), _B(0.2, 5)), Safer.NONE, Confirm.NONE, _FACT, _NS),
    _p("sensing.retry_pause_s", Unit.S, Kind.FLOAT, "Pause between sense retries. " + FACT_NOTE, _ND,
       (0.5, 0.5), (_B(0.1, 3), _B(0.1, 3)), Safer.NONE, Confirm.NONE, _FACT, _NS),
    _p("move.heading_tolerance_deg", Unit.DEG, Kind.FLOAT, "Largest heading error accepted before a move. " + FACT_NOTE,
       _ND, (10.0, 10.0), (_B(3, 30), _B(3, 30)), Safer.NONE, Confirm.NONE, _FACT, _NS),
    # Rows 54 to 58: avoidance facts (row 53 was removed).
    _p("avoid.clear_votes", Unit.COUNT, Kind.INT, "Clear views needed to pass an obstacle. " + FACT_NOTE,
       Scope.MISSION, (2, 2), (_B(1, 5), _B(1, 5)), Safer.NONE, Confirm.NONE, _FACT, _NS,
       env="FLIGHTRISK_OBSTACLE_CLEAR_VOTES"),
    _p("avoid.relook", Unit.COUNT, Kind.INT, "Extra looks before a sidestep. " + FACT_NOTE, Scope.MISSION,
       (2, 2), (_B(0, 5), _B(0, 5)), Safer.NONE, Confirm.NONE, _FACT, _NS, env="FLIGHTRISK_OBSTACLE_RELOOK"),
    _p("avoid.sidestep_cm", Unit.CM, Kind.INT, "Sidestep length; 0 switches the sidestep off. " + FACT_NOTE, _ND,
       (0, 0), (_B(20, 100), _B(20, 100)), Safer.NONE, Confirm.NONE, _FACT, _NS,
       off_value=0, env="FLIGHTRISK_OBSTACLE_SIDESTEP_CM"),
    _p("avoid.sidesteps", Unit.COUNT, Kind.INT, "Most sidesteps per obstacle. " + FACT_NOTE, Scope.MISSION,
       (2, 2), (_B(0, 4), _B(0, 4)), Safer.NONE, Confirm.NONE, _FACT, _NS, env="FLIGHTRISK_OBSTACLE_SIDESTEPS"),
    _p("avoid.sidestep_pass_cm", Unit.CM, Kind.INT, "Distance flown past an obstacle after a sidestep. " + FACT_NOTE,
       _ND, (150, 150), (_B(50, 400), _B(50, 400)), Safer.NONE, Confirm.NONE, _FACT, _NS),
    # Rows 59 to 74: plan and manual values.
    _p("plan.strip_cm", Unit.CM, Kind.INT, "Spacing between search strips.", Scope.MISSION,
       (75, DERIVED_NADIR_SWATH), (_B(20, 1000), _B(200, 10000)), Safer.NONE, Confirm.NONE, _UI, Applies.PLAN,
       env="FLIGHTRISK_FLEET_STRIP_CM", cli=("--fleet-strip-cm",)),
    _p("plan.area_cm", Unit.CM_LIST, Kind.INT_PAIR, "Search area width and height.", Scope.MISSION,
       ((400, 400), PER_MISSION), (None, None), Safer.NONE, Confirm.NONE, _UI, Applies.PLAN,
       env="FLIGHTRISK_FLEET_AREA_CM", cli=("--fleet-area-cm",)),
    _p("plan.zone_gap_cm", Unit.CM, Kind.INT, "Gap kept between neighbouring zones.", Scope.MISSION,
       (100, 0), (_B(40, 1000), _B(0, 5000)), Safer.HIGHER, Confirm.LESS_SAFE, _FE, Applies.PLAN),
    _p("separation.base_cm", Unit.CM, Kind.INT, "Base separation between drones.", Scope.MISSION,
       (150, 150), (_B(100, 1000), _B(150, 5000)), Safer.HIGHER, Confirm.LESS_SAFE, _FE, Applies.PLAN),
    _p("plan.placement_tolerance_cm", Unit.CM, Kind.INT, "Position error accepted at a placement.", Scope.MISSION,
       (10, 10), (_B(0, 500), _B(0, 500)), Safer.HIGHER, Confirm.LESS_SAFE, _FE, Applies.PLAN),
    _p("plan.altitude_tolerance_cm", Unit.CM, Kind.INT, "Height-sensor error allowed in a plan check.", Scope.MISSION,
       (10, 10), (_B(5, 50), _B(5, 500)), Safer.HIGHER, Confirm.LESS_SAFE, _FE, Applies.PLAN),
    _p("plan.placement_offset_cm", Unit.CM, Kind.INT, "Offset between drones at placement.", Scope.MISSION,
       (50, 50), (_B(0, 500), _B(0, 5000)), Safer.NONE, Confirm.NONE, _FE, Applies.PLAN),
    _p("plan.takeoff_stagger_s", Unit.S, Kind.FLOAT, "Seconds between drone takeoffs.", Scope.MISSION,
       (2.0, 2.0), (_B(0, 60), _B(0, 60)), Safer.NONE, Confirm.NONE, _FE, Applies.PLAN),
    _p("plan.min_slot_separation_cm", Unit.CM, Kind.INT, "Smallest gap between distinct altitude slots.", Scope.MISSION,
       (None, 500), (None, _B(300, 2000)), Safer.HIGHER, Confirm.LESS_SAFE, _FE, Applies.PLAN),
    _p("plan.dedup_radius_cm", Unit.CM, Kind.INT, "Radius that merges detections on the shared grid.", Scope.MISSION,
       (80, 80), (_B(20, 500), _B(20, 5000)), Safer.NONE, Confirm.NONE, _FE, Applies.PLAN),
    _p("plan.lost_obstacle_s", Unit.S, Kind.FLOAT, "Seconds before an obstacle-lost drone is handled.", Scope.MISSION,
       (20, 20), (_B(15, 120), _B(15, 300)), Safer.HIGHER, Confirm.LESS_SAFE, _FE, Applies.PLAN),
    _p("plan.camera_footprint_cm", Unit.CM, Kind.INT, "Camera footprint override; unset uses the strip.", Scope.MISSION,
       (UNSET, UNSET), (_B(20, 5000), _B(20, 5000)), Safer.NONE, Confirm.NONE, _FE, Applies.PLAN,
       env="FLIGHTRISK_CAMERA_FOOTPRINT_CM"),
    # Row 71 is one row with two ids, plan.snap_target_cm and plan.snap_tolerance_cm; it is one spec here.
    _p("plan.snap_target_cm", Unit.CM_LIST, Kind.INT_PAIR,
       "Snap target and snap tolerance, derived from the minimum move.", Scope.MISSION,
       ((25, 40), DEFERRED), (None, None), Safer.NONE, Confirm.NONE, _RO, Applies.PLAN),
    _p("plan.field_fence_margin_cm", Unit.CM, Kind.INT, "Margin kept inside the field fence.", Scope.MISSION,
       (None, 500), (None, _B(200, 2000)), Safer.HIGHER, Confirm.LESS_SAFE, _FE, Applies.PLAN),
    _p("manual.step_cm", Unit.CM, Kind.INT, "Move length of one manual step.", Scope.GROUND,
       (30, 30), (_B(20, 100), _B(20, 1000)), Safer.NONE, Confirm.NONE, _FE, _NS),
    _p("manual.rotate_deg", Unit.DEG, Kind.INT, "Angle of one manual rotation.", Scope.GROUND,
       (30, 30), (_B(5, 90), _B(5, 90)), Safer.NONE, Confirm.NONE, _FE, _NS),
    # Rows 75 to 86: move and avoidance values of the generic multirotor.
    _p("move.speed_cm_s", Unit.CM_PER_S, Kind.INT, "Cruise speed.", _ND,
       (None, 300), (None, _B(50, 1500)), Safer.LOWER, Confirm.NONE, _FE, _NS, cli=("--speed-mps",)),
    _p("move.arrival_radius_cm", Unit.CM, Kind.INT, "Horizontal radius that counts as arrived.", _ND,
       (None, 100), (None, _B(30, 500)), Safer.NONE, Confirm.NONE, _FE, _NS),
    _p("move.arrival_alt_cm", Unit.CM, Kind.INT, "Height error that counts as arrived.", _ND,
       (None, 70), (None, _B(20, 300)), Safer.NONE, Confirm.NONE, _FE, _NS),
    _p("move.timeout_margin_s", Unit.S, Kind.FLOAT, "Seconds added to a move's expected duration.", _ND,
       (None, 20.0), (None, _B(5, 120)), Safer.NONE, Confirm.NONE, _FE, _NS),
    _p("move.yaw_rate_dps", Unit.DEG_PER_S, Kind.FLOAT, "Yaw rate of a turn.", _ND,
       (None, 30.0), (None, _B(5, 180)), Safer.NONE, Confirm.NONE, _FE, _NS),
    _p("move.stationary_s", Unit.S, Kind.FLOAT, "Seconds without motion that count as stationary.", _ND,
       (None, 3.0), (None, _B(1, 30)), Safer.NONE, Confirm.NONE, _FE, _NS),
    _p("move.stationary_radius_cm", Unit.CM, Kind.INT, "Radius within which a drone counts as stationary.", _ND,
       (None, 50), (None, _B(10, 300)), Safer.NONE, Confirm.NONE, _FE, _NS),
    _p("avoid.autopilot_min_margin_cm", Unit.CM, Kind.INT, "Smallest autopilot avoidance margin that counts.", _ND,
       (None, 200), (None, _B(100, 1000)), Safer.HIGHER, Confirm.LESS_SAFE, _FE, _NS),
    _p("avoid.autopilot_min_lookahead_cm", Unit.CM, Kind.INT, "Smallest autopilot avoidance lookahead.", _ND,
       (None, 500), (None, _B(200, 2000)), Safer.HIGHER, Confirm.LESS_SAFE, _FE, _NS),
    _p("envelope.max_geofence_cm", Unit.CM, Kind.INT, "Upper bound of the geofence.", _ND,
       (5000, 50000), (_B(5000, 5000), _B(50000, 50000)), Safer.NONE, Confirm.NONE, _RO, _NS),
    _p("sensing.level_pitch_tolerance_deg", Unit.DEG, Kind.FLOAT, "Largest pitch at which a sense gate judges a view.",
       _ND, (None, 5.0), (None, _B(1, 10)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS),
)

#: The whole registry so far; later steps append rows 87 to 162.
TABLE: tuple[ParamSpec, ...] = ROWS_1_86
