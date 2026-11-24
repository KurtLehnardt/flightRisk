"""Move and altitude limits the legacy MavlinkController declares for itself.

These three constants are the legacy MavlinkController's own declarations. parameters.md files that controller as
legacy and out of scope for the parameter registry, so they are not registry rows. In particular the 120 m
altitude is not row 8 (envelope.max_altitude_cm) and it is not the generic_multirotor airframe limit that design
section 27 item 53 removes for MavlinkFleetDrone. The values are unchanged from the literals they replace in
flightrisk/drone/mavlink.py; this module only gives them one named home.
"""

#: Smallest move, in centimetres, the legacy MavlinkController accepts.
LEGACY_MAVLINK_MIN_MOVE_CM = 10

#: Largest move, in centimetres, the legacy MavlinkController accepts.
LEGACY_MAVLINK_MAX_MOVE_CM = 50000

#: Highest altitude, in metres, the legacy MavlinkController declares.
LEGACY_MAVLINK_MAX_ALTITUDE_M = 120
