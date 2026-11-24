"""Today's accepted ranges for the environment names and flags, as runtime data.

Generated once by ``scripts/make_param_today.py`` from
``tests/golden/flight_params.json`` (each alias's ``today_accepts`` and its
``policy``). Do not edit by hand: ``tests/test_param_today.py`` checks that
``TODAY_ACCEPTS`` equals what ``resolver.today_accepts_from_golden`` builds from
the golden file. The edge passes it to ``resolve_unit`` at start, so a name
today's code reads keeps today's range at the environment and flag layers.
"""

from __future__ import annotations

from types import MappingProxyType

from flightrisk.params.resolver import TodayAccepts, TodayRange

_R = TodayRange

#: ``{name: {profile: TodayRange or None}}``; a ``None`` range means today's range is not known.
TODAY_ACCEPTS: TodayAccepts = MappingProxyType({
    'FLIGHTRISK_GEOFENCE_CM': MappingProxyType({
        'tello': _R(0, None, lo_open=True),
        'generic_multirotor': _R(0, 50000, lo_open=True),
    }),
    'FLIGHTRISK_FIELD_GEOFENCE_M': MappingProxyType({
        'tello': _R(0, None, lo_open=True),
        'generic_multirotor': _R(0, 50000, lo_open=True),
    }),
    '--geofence-m': MappingProxyType({
        'tello': _R(0, None, lo_open=True),
        'generic_multirotor': _R(0, 50000, lo_open=True),
    }),
    'FLIGHTRISK_CEILING_CM': MappingProxyType({
        'tello': _R(150, 1000),
        'generic_multirotor': _R(1000, 12000),
    }),
    'FLIGHTRISK_FIELD_CEILING_M': MappingProxyType({
        'tello': _R(150, 1000),
        'generic_multirotor': _R(1000, 12000),
    }),
    '--ceiling-m': MappingProxyType({
        'tello': _R(150, 1000),
        'generic_multirotor': _R(1000, 12000),
    }),
    'FLIGHTRISK_CEILING_MARGIN_CM': MappingProxyType({
        'tello': _R(30, None),
        'generic_multirotor': _R(300, None),
    }),
    'FLIGHTRISK_FIELD_CEILING_MARGIN_M': MappingProxyType({
        'tello': _R(30, None),
        'generic_multirotor': _R(300, None),
    }),
    '--ceiling-margin-m': MappingProxyType({
        'tello': _R(30, None),
        'generic_multirotor': _R(300, None),
    }),
    '--alt-slot': MappingProxyType({
        'tello': _R(50, 500),
        'generic_multirotor': _R(1000, 12000),
    }),
    '--alt-slot-m': MappingProxyType({
        'tello': _R(50, 500),
        'generic_multirotor': _R(1000, 12000),
    }),
    'FLIGHTRISK_FIELD_ALT_SLOT_M': MappingProxyType({
        'tello': _R(50, 500),
        'generic_multirotor': _R(1000, 12000),
    }),
    'FLIGHTRISK_MIN_TAKEOFF_BATTERY': MappingProxyType({
        'tello': _R(15, 100),
        'generic_multirotor': _R(15, 100),
    }),
    'FLIGHTRISK_LAND_BATTERY': MappingProxyType({}),
    'FLIGHTRISK_LINK_LOSS_LAND_S': MappingProxyType({
        'tello': _R(0, None, lo_open=True),
        'generic_multirotor': _R(0, None, lo_open=True),
    }),
    'FLIGHTRISK_FIELD_LINK_LOSS_HOLD_S': MappingProxyType({
        'generic_multirotor': _R(0, None),
    }),
    '--link-loss-hold-s': MappingProxyType({
        'generic_multirotor': _R(0, None),
    }),
    'FLIGHTRISK_LINK_LOSS_WAIT_S': MappingProxyType({
        'generic_multirotor': _R(0, None),
    }),
    'FLIGHTRISK_FAILSAFE_ACTION': MappingProxyType({}),
    '--failsafe-action': MappingProxyType({}),
    'FLIGHTRISK_MANUAL_ORPHAN_LAND_S': MappingProxyType({
        'tello': _R(30.0, None, policy='clamp'),
        'generic_multirotor': _R(30.0, None, policy='clamp'),
    }),
    'FLIGHTRISK_TELLO_TAKEOFF_LATE_S': MappingProxyType({
        'tello': _R(2.0, 20.0),
    }),
    'FLIGHTRISK_TELLO_SETTLE_SAMPLES': MappingProxyType({
        'tello': _R(2, 50),
    }),
    'FLIGHTRISK_TELLO_SETTLE_TOLERANCE_CM': MappingProxyType({
        'tello': _R(2.0, 50.0),
    }),
    'FLIGHTRISK_TELLO_SETTLE_TIMEOUT_S': MappingProxyType({
        'tello': _R(1.0, 10.0),
    }),
    'FLIGHTRISK_TELLO_REFUSAL_RETRIES': MappingProxyType({
        'tello': _R(0, 3),
    }),
    'FLIGHTRISK_TELLO_REFUSAL_HOLD_S': MappingProxyType({
        'tello': _R(0.5, 5.0),
    }),
    '--takeoff-alt-m': MappingProxyType({
        'generic_multirotor': _R(200.0, 2000.0),
    }),
    'FLIGHTRISK_HOVER_TEST': MappingProxyType({}),
    'FLIGHTRISK_TELLO_TEMP_WARN_C': MappingProxyType({
        'tello': _R(40, 120),
    }),
    'FLIGHTRISK_TELLO_TEMP_REFUSE_C': MappingProxyType({
        'tello': _R(40, 120),
    }),
    'FLIGHTRISK_OBSTACLE_TURN': MappingProxyType({}),
    'FLIGHTRISK_OBSTACLE_SENSING': MappingProxyType({}),
    'FLIGHTRISK_OBSTACLE_STEP_CM': MappingProxyType({
        'tello': _R(20, 100),
        'generic_multirotor': _R(20, 5000),
    }),
    'FLIGHTRISK_OBSTACLE_SETTLE_S': MappingProxyType({
        'tello': _R(0.1, 3.0),
        'generic_multirotor': _R(0.1, 3.0),
    }),
    'FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH': MappingProxyType({
        'tello': _R(0.05, 0.9),
        'generic_multirotor': _R(0.05, 0.9),
    }),
    'FLIGHTRISK_OBSTACLE_PERSON_FRACTION': MappingProxyType({
        'tello': _R(0.2, 0.9),
        'generic_multirotor': _R(0.2, 0.9),
    }),
    'FLIGHTRISK_MAX_DECODE_LAG_S': MappingProxyType({
        'tello': _R(0.1, 2.0),
    }),
    'FLIGHTRISK_OBSTACLE_CLEAR_VOTES': MappingProxyType({
        'tello': _R(1, 5),
        'generic_multirotor': _R(1, 5),
    }),
    'FLIGHTRISK_OBSTACLE_RELOOK': MappingProxyType({
        'tello': _R(0, 5),
        'generic_multirotor': _R(0, 5),
    }),
    'FLIGHTRISK_OBSTACLE_SIDESTEP_CM': MappingProxyType({
        'tello': _R(20, 100),
        'generic_multirotor': _R(20, 100),
    }),
    'FLIGHTRISK_OBSTACLE_SIDESTEPS': MappingProxyType({
        'tello': _R(0, 4),
        'generic_multirotor': _R(0, 4),
    }),
    'FLIGHTRISK_FLEET_STRIP_CM': MappingProxyType({
        'tello': _R(0, None, lo_open=True),
        'generic_multirotor': _R(0, None, lo_open=True),
    }),
    '--fleet-strip-cm': MappingProxyType({
        'tello': _R(0, None, lo_open=True),
        'generic_multirotor': _R(0, None, lo_open=True),
    }),
    'FLIGHTRISK_FLEET_AREA_CM': MappingProxyType({}),
    '--fleet-area-cm': MappingProxyType({}),
    'FLIGHTRISK_CAMERA_FOOTPRINT_CM': MappingProxyType({
        'tello': _R(20, 5000),
        'generic_multirotor': _R(20, 5000),
    }),
    '--speed-mps': MappingProxyType({
        'generic_multirotor': _R(0, 1500, lo_open=True),
    }),
    'FLIGHTRISK_MANUAL_LOST_IDLE_S': MappingProxyType({
        'tello': _R(5.0, None, policy='clamp'),
        'generic_multirotor': _R(5.0, None, policy='clamp'),
    }),
    'FLIGHTRISK_TELLO_REFUSALS_PER_FLIGHT': MappingProxyType({
        'tello': _R(0, 6),
    }),
})
