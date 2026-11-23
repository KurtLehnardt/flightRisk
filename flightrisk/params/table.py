"""The flight parameter registry, the rows of ``parameters.md`` section 1.

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
PLACEHOLDER = "placeholder"
DERIVED_MISSION_AREA = "derived:mission_area"
DERIVED_LOSS_TIMEOUT = "derived:ground_loss_timeout"
DERIVED_LOST_WINDOW_FLOOR = "derived:lost_window_floor"
DERIVED_LOST_WINDOW_FIELD = "derived:lost_window_field"
DERIVED_REFUSED_STEPS = "derived:refused_step_sum"

#: Sentence every fact row carries in its description (risk R6).
FACT_NOTE = "Test-adapter fact, read-only, not operator-facing."

#: The safe value of rows whose safer direction is a named value, not higher or lower.
SAFE_VALUES: dict[str, Any] = {
    "link.require_failsafes": True,
    "tello.temp_refuse_c": "on",
    "sensing.turn": True,
    "sensing.mode": "required",
    "stranded.action": "return",
    "task.end_action": "return",
    "return.field_method": "edge_guided",
}

#: Later values from rows tagged "from <PR>" in parameters.md, one tuple per row id, as the golden file
#: records them. ``profile`` names the profile the tag belongs to; ``where`` is "bounds" when the later
#: change is to the bound, not the default. ``value`` is None when the later change is no single value.
PENDING: dict[str, tuple[dict[str, Any], ...]] = {
    "envelope.geofence_cm": ({"pr": "C5", "item": 29, "profile": T, "value": 450},),
    "battery.min_takeoff_pct": ({"pr": "C6", "item": 33, "profile": G, "value": 40},),
    "battery.critical_pct": ({"pr": "B4", "item": 8, "profile": G, "value": "autopilot_critical_level"},),
    "link.loss_wait_s": (
        {"pr": "C7", "item": 34, "profile": T, "value": 10},
        {"pr": "C7", "item": 34, "profile": T, "value": None, "where": "bounds"},
        {"pr": "C5", "item": 26, "profile": G, "value": None, "where": "bounds"},
    ),
    "link.loss_action": ({"pr": "C7", "item": 34, "profile": T, "value": "return"},),
    "separation.base_cm": ({"pr": "D1", "item": 9, "profile": G, "value": 500},),
    "find.on_find_policy": (
        {"pr": "E1", "item": 38, "profile": T, "value": "keep_watch"},
        {"pr": "E1", "item": 38, "profile": G, "value": "keep_watch"},
    ),
    "link.ground_loss_timeout_s": (
        {"pr": "C7", "item": 34, "profile": T, "value": DERIVED_LOSS_TIMEOUT},
        {"pr": "D2", "item": 15, "profile": G, "value": DERIVED_LOSS_TIMEOUT},
    ),
    "lost.flying_window_s": (
        {"pr": "C7", "item": 46, "profile": T, "value": DERIVED_LOST_WINDOW_FLOOR},
        {"pr": "D2", "item": 18, "profile": G, "value": DERIVED_LOST_WINDOW_FIELD},
    ),
    "search.required_overlap_pct": ({"pr": "D5", "item": 37, "profile": G, "value": 30},),
    "stranded.action": (
        {"pr": "C7", "item": 34, "profile": T, "value": "return"},
        {"pr": "C6", "item": 32, "profile": G, "value": "return"},
    ),
    "task.end_action": (
        {"pr": "C7", "item": 34, "profile": T, "value": "return"},
        {"pr": "C6", "item": 32, "profile": G, "value": "return"},
    ),
    "return.field_method": ({"pr": "C6", "item": 45, "profile": G, "value": "edge_guided"},),
}

#: Scale factor of a canonical environment name or CLI flag that is not given in the registry unit,
#: by name. A name absent here is read in the registry unit. An ``EnvAlias`` carries its own factor.
NAME_SCALES: dict[str, float] = {
    "--geofence-m": 100.0,
    "--ceiling-m": 100.0,
    "--ceiling-margin-m": 100.0,
    "--alt-slot-m": 100.0,
    "FLIGHTRISK_FIELD_ALT_SLOT_M": 100.0,
    "--takeoff-alt-m": 100.0,
    # The edge passes --speed-mps to MavlinkFleetOptions.speed_mps in m/s; the registry is in cm/s.
    "--speed-mps": 100.0,
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
       _ND, (0, 10.0), (None, _B(1.5, 30)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS,
       env="FLIGHTRISK_LINK_LOSS_WAIT_S",
       env_aliases=(EnvAlias("FLIGHTRISK_FIELD_LINK_LOSS_HOLD_S", 1.0, "field"),), cli=("--link-loss-hold-s",)),
    _p("link.loss_action", Unit.ENUM, Kind.ENUM, "What the drone does after a long ground-link loss; the environment spelling rtl reads as return.", _ND,
       ("land", "return"), (None, None), Safer.NONE, Confirm.NONE, _FE, _NS,
       choices=("return", "land"), env="FLIGHTRISK_FAILSAFE_ACTION", cli=("--failsafe-action",)),
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

SDK_NOTE = "Tello SDK or driver fact, shown with its source (D-F18)."


def _sdk(id: str, unit: Unit, kind: Kind, default: Any, text: str) -> ParamSpec:
    """One read-only Tello SDK or driver row (rows 87 to 110); a scalar is its own fixed bound."""
    scalar = not isinstance(default, tuple)
    return _p(id, unit, kind, f"{text} {SDK_NOTE}", _ND, (default, None),
              (_B(default, default) if scalar else None, None), Safer.NONE, Confirm.NONE, _RO, _NS)


_I, _F, _S, _C = Unit.PORT, Unit.S, Unit.CM, Unit.COUNT

# Rows 87 to 110: Tello SDK and driver facts, read-only, tello only.
ROWS_87_110: tuple[ParamSpec, ...] = (
    _sdk("tello_sdk.command_port", _I, Kind.INT, 8889, "SDK command channel."),
    _sdk("tello_sdk.state_port", _I, Kind.INT, 8890, "SDK state stream."),
    _sdk("tello_sdk.video_port", _I, Kind.INT, 11111, "SDK video stream."),
    _sdk("tello_sdk.command_reply_timeout_s", _F, Kind.FLOAT, 7.0, "Response timeout of a command."),
    _sdk("tello_sdk.command_gap_s", _F, Kind.FLOAT, 0.1, "Smallest gap between commands."),
    _sdk("tello_sdk.state_fresh_s", _F, Kind.FLOAT, 0.5, "Age below which a state packet is fresh."),
    _sdk("tello_sdk.state_lost_s", _F, Kind.FLOAT, 3.0, "Silence after which the state counts as lost."),
    _sdk("tello_sdk.motor_off_s", _F, Kind.FLOAT, 2.0, "Motor time unchanged this long proves the motors off."),
    _sdk("tello_sdk.motor_run_gap_s", _F, Kind.FLOAT, 1.0, "Gap used to decide the motors are running."),
    _sdk("tello_sdk.takeoff_confirm_s", _F, Kind.FLOAT, 6.0, "Window after a bare error reply to a takeoff."),
    _sdk("tello_sdk.takeoff_ground_after_s", _F, Kind.FLOAT, 3.0, "Seconds before a takeoff may be called grounded."),
    _sdk("tello_sdk.takeoff_lift_guard_s", _F, Kind.FLOAT, 10.0, "Guard window for the lift after a takeoff."),
    _sdk("tello_sdk.land_deadline_s", _F, Kind.FLOAT, 25.0, "Deadline of a landing."),
    _sdk("tello_sdk.land_ground_wait_s", _F, Kind.FLOAT, 6.0, "Wait for the ground reading after a landing."),
    _sdk("tello_sdk.land_stall_s", _F, Kind.FLOAT, 1.0, "Stall time while landing."),
    _sdk("tello_sdk.land_descent_stall_s", _F, Kind.FLOAT, 2.0, "Descent stall time while landing."),
    _sdk("tello_sdk.land_descent_cm", _S, Kind.INT, 5, "Descent that counts as progress while landing."),
    _sdk("tello_sdk.land_attempts", _C, Kind.INT, 3, "Landing attempts."),
    _sdk("tello_sdk.connect_attempts", _C, Kind.INT, 3, "Connection attempts."),
    _sdk("tello_sdk.keepalive_interval_s", _F, Kind.FLOAT, 10.0, "Keepalive interval, below the Tello's own 15 s auto-land."),
    _sdk("tello_sdk.keepalive_timeout_s", _F, Kind.FLOAT, 5, "Keepalive reply timeout."),
    _sdk("tello_sdk.tof_out_of_range_cm", _S, Kind.INT, 6553, "The tof reading out of the sensor's range."),
    _sdk("tello_sdk.temp_reading_range_c", Unit.DEG_C_PAIR, Kind.INT_PAIR, (-40, 150), "A temp reply outside it is unknown."),
    _sdk("tello_sdk.video_size_px", Unit.PX_PAIR, Kind.INT_PAIR, (960, 720), "Video stream size, unverified."),
)

_MS = Scope.MISSION
_PL = Applies.PLAN
_IF = Applies.IN_FLIGHT
_LS = (Safer.HIGHER, Confirm.LESS_SAFE)
_NB = (None, None)

# Rows 111 to 128: spec v1 rows, every one new.
ROWS_111_128: tuple[ParamSpec, ...] = (
    _p("battery.return_floor_pct", Unit.PCT, Kind.INT, "Lowest battery level that starts a return.", _ND,
       (25, 30), (_B(15, 90), _B(20, 90)), *_LS, _FE, _NS),
    _p("battery.landing_reserve_pct", Unit.PCT, Kind.INT, "Charge left on landing at home.", _ND,
       (10, 15), (_B(5, 50), _B(5, 50)), *_LS, _FE, _NS),
    _p("battery.return_rate_pct_per_100m", Unit.PCT, Kind.FLOAT, "Battery cost of flying home per 100 m.", _ND,
       (PLACEHOLDER, PLACEHOLDER), _NB, *_LS, _FE, _NS),
    _p("battery.hover_rate_pct_per_min", Unit.PCT, Kind.FLOAT, "Battery cost of one minute of hover.", _ND,
       (PLACEHOLDER, PLACEHOLDER), _NB, *_LS, _FE, _NS),
    _p("position.uncertainty_bound_cm", Unit.CM, Kind.INT, "Largest position uncertainty before landing in place.", _ND,
       (50, None), (_B(20, 75), None), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS),
    _p("position.dr_growth_per_m_cm", Unit.CM, Kind.FLOAT, "Dead-reckoning growth per metre flown. " + FACT_NOTE, _ND,
       (PLACEHOLDER, None), _NB, Safer.NONE, Confirm.NONE, _FACT, _NS),
    _p("position.dr_growth_per_turn_cm", Unit.CM, Kind.FLOAT, "Dead-reckoning growth per turn. " + FACT_NOTE, _ND,
       (PLACEHOLDER, None), _NB, Safer.NONE, Confirm.NONE, _FACT, _NS),
    _p("position.dr_growth_per_min_hover_cm", Unit.CM, Kind.FLOAT, "Dead-reckoning growth per minute of hover. " + FACT_NOTE,
       _ND, (PLACEHOLDER, None), _NB, Safer.NONE, Confirm.NONE, _FACT, _NS),
    _p("home.accuracy_cm", Unit.CM, Kind.INT, "Home accuracy needed before takeoff.", _ND,
       (None, 200), (None, _B(50, 500)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS),
    _p("home.max_offset_cm", Unit.CM, Kind.INT, "Largest offset between the autopilot's home and the drone.", _ND,
       (None, 500), (None, _B(100, 1000)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS),
    _p("separation.landing_cm", Unit.CM, Kind.INT, "Homes closer than this share a landing area.", _MS,
       (150, 1000), _NB, *_LS, _FE, _PL),
    _p("airspace.close_band_floor_cm", Unit.CM, Kind.INT, "Height at which the close band starts.", _MS,
       (None, 1000), (None, _B(1000, 12000)), *_LS, _FE, _PL),
    _p("airspace.band_gap_cm", Unit.CM, Kind.INT, "Gap between the close band and the sweep band.", _MS,
       (None, 500), (None, _B(300, 2000)), *_LS, _FE, _PL),
    _p("retask.window_s", Unit.S, Kind.FLOAT, "Hover after the last task, then return.", _MS,
       (20, 20), (_B(1, 120), _B(1, 120)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _PL),
    _p("return.blocked_wait_s", Unit.S, Kind.FLOAT, "Wait for a blocked return before holding or landing.", _ND,
       (30, 30), (_B(0, 120), _B(0, 120)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS),
    _p("return.person_cue_wait_s", Unit.S, Kind.FLOAT, "Wait for a person's answer before landing.", _ND,
       (30, 30), (_B(0, 300), _B(0, 300)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS),
    _p("noentry.lost_margin_cm", Unit.CM, Kind.INT, "Margin added to a lost drone's no-entry radius.", _MS,
       (100, 500), _NB, *_LS, _FE, _PL),
    _p("search.step_cycle_speed_cm_s", Unit.CM_PER_S, Kind.INT, "Speed cap of a step-and-look cycle.", _ND,
       (30, None), (_B(30, 30), None), Safer.NONE, Confirm.NONE, _RO, _PL),
)

# Rows 129 to 145: find, search, escort and link rows.
ROWS_129_145: tuple[ParamSpec, ...] = (
    _p("find.on_find_policy", Unit.ENUM, Kind.ENUM, "What the fleet does when a target is found.", _MS,
       ("land_all", "land_all"), _NB, Safer.NONE, Confirm.NONE, _UI, _IF,
       choices=("keep_watch", "return_all", "land_all")),
    _p("find.converge_count", Unit.COUNT, Kind.INT, "Drones that converge on a find.", _MS,
       (1, 2), _NB, Safer.NONE, Confirm.NONE, _UI, _IF),
    _p("find.approach_distance_cm", Unit.CM, Kind.INT, "Distance at which a drone approaches a find.", _MS,
       (300, 1500), _NB, *_LS, _UI, _PL),
    _p("find.verification_window_s", Unit.S, Kind.FLOAT, "Seconds allowed to verify a find.", _MS,
       (60, 60), (_B(10, 300), _B(10, 300)), Safer.NONE, Confirm.NONE, _UI, _IF),
    _p("find.local_search_on", Unit.BOOL, Kind.BOOL, "Whether a find starts a local search.", _MS,
       (True, True), _NB, Safer.NONE, Confirm.NONE, _UI, _IF, choices=("true", "false")),
    _p("find.running_speed_cm_s", Unit.CM_PER_S, Kind.INT, "Speed of a running target the fleet follows.", _MS,
       (300, 400), (_B(50, 800), _B(50, 800)), Safer.HIGHER, Confirm.NONE, _UI, _IF),
    _p("find.local_search_max_radius_cm", Unit.CM, Kind.INT, "Largest radius of a local search.", _MS,
       (DERIVED_MISSION_AREA, 100000), _NB, Safer.NONE, Confirm.NONE, _UI, _IF),
    _p("find.local_search_time_s", Unit.S, Kind.FLOAT, "Longest local search.", _MS,
       (600, 600), (_B(1, 3600), _B(1, 3600)), Safer.NONE, Confirm.NONE, _UI, _IF),
    _p("search.required_overlap_pct", Unit.PCT, Kind.INT, "Overlap between neighbouring lanes.", _MS,
       (30, 20), (_B(10, 80), _B(10, 80)), Safer.HIGHER, Confirm.NONE, _UI, _IF),
    _p("escort.count", Unit.COUNT, Kind.INT, "Drones that escort a target.", _MS,
       (None, 1), _NB, Safer.NONE, Confirm.NONE, _UI, _IF),
    _p("escort.standoff_cm", Unit.CM, Kind.INT, "Distance an escort keeps from the target.", _MS,
       (None, 2000), _NB, *_LS, _UI, _PL),
    _p("escort.altitude_cm", Unit.CM, Kind.INT, "Height of an escort.", _MS,
       (None, 1500), _NB, *_LS, _UI, _PL),
    _p("escort.handoff_margin_pct", Unit.PCT, Kind.INT, "Battery points above the return threshold at a handoff.", _MS,
       (None, 10), (None, _B(5, 30)), *_LS, _UI, _IF),
    _p("escort.lost_from_view_s", Unit.S, Kind.FLOAT, "Seconds out of view before an escort counts the target lost.", _MS,
       (None, 10), (None, _B(3, 60)), Safer.NONE, Confirm.NONE, _UI, _IF),
    _p("link.ground_loss_margin_s", Unit.S, Kind.FLOAT, "Margin the ground adds before it declares a drone lost.", Scope.GROUND,
       (2, 2), (_B(1, 10), _B(1, 10)), *_LS, _FE, _NS),
    _p("link.ground_loss_timeout_s", Unit.S, Kind.FLOAT, "Time without contact before the ground declares a drone lost.",
       Scope.GROUND, (5.0, 5.0), _NB, Safer.NONE, Confirm.NONE, _RO, _NS),
    _p("lost.flying_window_s", Unit.S, Kind.FLOAT, "Window in which a lost drone's area stays closed.", _MS,
       (20, 20.0), _NB, Safer.NONE, Confirm.NONE, _RO, _PL),
)

# Rows 146 to 162: airspace, position, return, stranded and test-adapter rows.
ROWS_146_162: tuple[ParamSpec, ...] = (
    _p("airspace.close_band_ceiling_cm", Unit.CM, Kind.INT, "Top of the close band.", _MS,
       (None, 1500), (None, _B(1000, 12000)), Safer.NONE, Confirm.NONE, _FE, _PL),
    _p("airspace.sweep_band_floor_cm", Unit.CM, Kind.INT, "Bottom of the sweep band.", _MS,
       (None, 2000), (None, _B(1500, 12000)), *_LS, _FE, _PL),
    _p("airspace.sweep_band_ceiling_cm", Unit.CM, Kind.INT, "Top of the sweep band.", _MS,
       (None, 2500), (None, _B(2000, 12000)), Safer.NONE, Confirm.NONE, _FE, _PL),
    _p("airspace.obstacle_top_cm", Unit.CM, Kind.INT, "Highest known obstacle per area; required, no default.", _MS,
       (None, UNSET), (None, _B(0, 12000)), *_LS, _UI, _PL),
    _p("airspace.obstacle_margin_cm", Unit.CM, Kind.INT, "Height kept above the obstacle top.", _MS,
       (None, 500), (None, _B(200, 2000)), *_LS, _FE, _PL),
    _p("position.gps_sigma_k", Unit.RATIO, Kind.FLOAT, "Multiplier on the GPS one-sigma accuracy.", _ND,
       (None, 2), (None, _B(2, 5)), *_LS, _FE, _NS),
    _p("return.progress_min_cm", Unit.CM, Kind.INT, "Gain toward home the progress monitor requires.", _ND,
       (20, 200), (_B(10, 200), _B(100, 2000)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS),
    _p("return.progress_timeout_s", Unit.S, Kind.FLOAT, "Progress monitor window.", _ND,
       (20, 20), (_B(5, 60), _B(5, 60)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS),
    _p("separation.landing_interval_s", Unit.S, Kind.FLOAT, "Spacing of the landing order in a shared landing area.", _MS,
       (10, 30), (_B(5, 120), _B(10, 300)), *_LS, _FE, _PL),
    _p("find.ambiguity_margin", Unit.RATIO, Kind.FLOAT, "Score gap below which a sighting is ambiguous.", _MS,
       (0.05, 0.05), (_B(0, 0.3), _B(0, 0.3)), Safer.HIGHER, Confirm.NONE, _UI, _IF),
    _p("escort.handoff_frames", Unit.COUNT, Kind.INT, "Frames the relief must see the same target.", _MS,
       (None, 5), (None, _B(3, 30)), *_LS, _FE, _PL),
    _p("stranded.lost_idle_s", Unit.S, Kind.FLOAT, "Idle-controller countdown for a lost drone.", Scope.GROUND,
       (30, 30), (_B(5, 300), _B(5, 300)), Safer.LOWER, Confirm.LESS_SAFE, _FE, _NS,
       env="FLIGHTRISK_MANUAL_LOST_IDLE_S"),
    _p("stranded.action", Unit.ENUM, Kind.ENUM, "What the stranded countdown does when it ends.", Scope.GROUND,
       ("land", "land"), _NB, Safer.NONE, Confirm.NONE, _FE, _NS, choices=("land", "return")),
    _p("task.end_action", Unit.ENUM, Kind.ENUM, "What a drone does after its last task and the retask window.", _MS,
       ("land", "land"), _NB, Safer.NONE, Confirm.NONE, _FE, _PL, choices=("land", "hover", "return")),
    _p("return.field_method", Unit.ENUM, Kind.ENUM, "How a MAVLink edge flies the returns it starts.", _ND,
       (None, "autopilot_rtl"), _NB, Safer.NONE, Confirm.NONE, _RO, _NS, choices=("autopilot_rtl", "edge_guided")),
    _p("tello.refusals_per_flight", Unit.COUNT, Kind.INT, "Cap on refused lane steps per flight. " + FACT_NOTE, _ND,
       (2, None), (_B(0, 6), None), Safer.NONE, Confirm.NONE, _FACT, _NS, env="FLIGHTRISK_TELLO_REFUSALS_PER_FLIGHT"),
    _p("envelope.fence_margin_cm", Unit.CM, Kind.INT, "Runtime fence margin, grown by each refused step. " + FACT_NOTE, _ND,
       (DERIVED_REFUSED_STEPS, None), _NB, Safer.NONE, Confirm.NONE, _FACT, _IF),
)

ROWS_87_162: tuple[ParamSpec, ...] = ROWS_87_110 + ROWS_111_128 + ROWS_129_145 + ROWS_146_162

#: The whole registry: rows 1 to 162 minus the removed 53 and 85, row 71 holding two ids, so 160 rows.
TABLE: tuple[ParamSpec, ...] = ROWS_1_86 + ROWS_87_162
