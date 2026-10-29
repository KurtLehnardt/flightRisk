"""Autopilot link: what the fleet edge needs from an open-source flight controller.

An open-source autopilot (PX4 or ArduPilot on a Pixhawk-class board) flies
itself: it holds position, follows position targets, lands, returns to
launch and runs its own failsafes. The edge on the companion computer only
tells it where to go. This module defines that boundary without naming any
SDK:

* ``AutopilotState``: one consistent, timestamped view of what the autopilot
  reports (link freshness, armed and in-air state, flight mode, GPS and home
  position, battery).
* ``AutopilotLink``: the blocking commands the edge sends (connect, arm,
  take off, go to a position, land, return to launch) and the parameter
  reads its preflight needs. ``flightrisk.drone.mavsdk_link`` implements it
  over MAVSDK; tests implement it over a simulated MAVSDK.
* ``preflight_problems``: the checks the edge makes before it lets the
  drone take off. They fail closed: anything that cannot be read is a
  problem.
* ``failsafe_problems``: the autopilot-side failsafes and geofence that must
  be configured, read from parameters, per autopilot.

Flight modes use MAVSDK's ``FlightMode`` names (``HOLD``, ``LAND``,
``RETURN_TO_LAUNCH``, ``OFFBOARD`` and so on). MAVSDK reports ArduCopter's
GUIDED mode as ``OFFBOARD`` and LOITER as ``HOLD``.

Nothing here has been verified on real autopilot hardware; see
``docs/open-drone-support.md`` for what was verified and how.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ARDUPILOT",
    "AUTOPILOTS",
    "DEFAULT_SYSID",
    "FAILSAFE_PARAMS",
    "FAILSAFE_RULES",
    "FENCE_MARGIN_M",
    "PX4",
    "AutopilotLink",
    "AutopilotState",
    "autonomous_modes",
    "failsafe_problems",
    "preflight_problems",
    "rtl_altitude_cm",
]

ARDUPILOT = "ardupilot"
PX4 = "px4"
AUTOPILOTS = (ARDUPILOT, PX4)

#: 3D GPS fix (MAVLink GPS_FIX_TYPE_3D_FIX) or better.
GPS_FIX_3D = 3
DEFAULT_MIN_SATELLITES = 8


@dataclass(frozen=True)
class AutopilotState:
    """What the autopilot reported, as last seen. ``None`` means not reported (yet).

    ``telemetry_age_s`` is the age of the newest message of any kind;
    ``connected`` is false when the link was never made or was closed.
    Altitudes: ``abs_alt_m`` is above mean sea level, ``rel_alt_m`` above the
    home position.
    """

    connected: bool = False
    telemetry_age_s: float | None = None
    autopilot: str | None = None
    armed: bool | None = None
    in_air: bool | None = None
    landed_state: str | None = None
    flight_mode: str | None = None
    lat_deg: float | None = None
    lon_deg: float | None = None
    abs_alt_m: float | None = None
    rel_alt_m: float | None = None
    heading_deg: float | None = None
    battery_pct: float | None = None
    gps_fix_type: int | None = None
    satellites: int | None = None
    global_position_ok: bool | None = None
    home_position_ok: bool | None = None
    armable: bool | None = None
    home_lat_deg: float | None = None
    home_lon_deg: float | None = None
    home_abs_alt_m: float | None = None
    #: Age in seconds of each telemetry stream's last message (``position``,
    #: ``gps``, ``health``, ``battery``, ``in_air``, ``flight_mode``, ...).
    #: A stream that stopped keeps its last values; its age says so.
    stream_age_s: Mapping[str, float] = field(default_factory=dict)

    def stream_fresh(self, stream: str, stale_after_s: float) -> bool:
        """True when *stream* delivered a message within *stale_after_s* (and the link is fresh)."""
        age = self.stream_age_s.get(stream)
        return self.fresh(stale_after_s) and age is not None and age < stale_after_s

    def fresh(self, stale_after_s: float) -> bool:
        """True when connected and the newest message is younger than *stale_after_s*."""
        return (
            self.connected
            and self.telemetry_age_s is not None
            and self.telemetry_age_s < stale_after_s
        )

    @property
    def has_position(self) -> bool:
        return all(_finite(v) for v in (self.lat_deg, self.lon_deg, self.abs_alt_m, self.rel_alt_m))

    @property
    def has_home(self) -> bool:
        return all(_finite(v) for v in (self.home_lat_deg, self.home_lon_deg, self.home_abs_alt_m))


@runtime_checkable
class AutopilotLink(Protocol):
    """Blocking commands to one autopilot. Every command raises on failure or timeout.

    Implementations are thread-safe: the edge sends a land from one thread
    while another waits for a position target to be reached.
    """

    address: str

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def state(self) -> AutopilotState: ...
    def arm(self) -> None: ...
    def takeoff(self, altitude_m: float) -> None: ...
    def goto(self, lat_deg: float, lon_deg: float, abs_alt_m: float, yaw_deg: float) -> None: ...
    def land(self) -> None: ...
    def return_to_launch(self) -> None: ...
    def set_speed(self, speed_mps: float) -> None: ...
    def get_param(self, name: str) -> float | None: ...


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ---------------------------------------------------------------------------
# Flight modes
# ---------------------------------------------------------------------------

#: Modes the edge itself puts each autopilot in. Any other mode while the
#: drone flies means someone else has control: an RC pilot flipped the mode
#: switch, or a ground control station changed it. ArduCopter's GUIDED shows
#: as OFFBOARD in MAVSDK; its LOITER shows as HOLD and is a pilot mode, so
#: HOLD is not in the ArduPilot set. PX4 enters HOLD after a takeoff and
#: while it repositions, so it is in the PX4 set.
_AUTONOMOUS_MODES = {
    ARDUPILOT: frozenset({"OFFBOARD", "TAKEOFF", "LAND", "RETURN_TO_LAUNCH"}),
    PX4: frozenset({"TAKEOFF", "HOLD", "LAND", "RETURN_TO_LAUNCH", "OFFBOARD"}),
}


def autonomous_modes(autopilot: str | None) -> frozenset[str]:
    """The flight modes the edge uses on *autopilot* (empty for an unknown autopilot)."""
    return _AUTONOMOUS_MODES.get(autopilot or "", frozenset())


# ---------------------------------------------------------------------------
# Failsafe parameters
# ---------------------------------------------------------------------------
#
# Parameter meanings are taken from the parameter references:
#   ArduCopter: https://ardupilot.org/copter/docs/parameters.html
#     (FENCE_ENABLE, FENCE_TYPE, FENCE_ACTION, FENCE_RADIUS, FENCE_ALT_MAX,
#     BATT_FS_LOW_ACT, BATT_FS_CRT_ACT, BATT_LOW_VOLT, BATT_LOW_MAH,
#     BATT_ARM_VOLT, FS_THR_ENABLE, RTL_ALT, SYSID_THISMAV / MAV_SYSID)
#   PX4: https://docs.px4.io/main/en/advanced_config/parameter_reference.html
#     (GF_ACTION, GF_MAX_HOR_DIST, GF_MAX_VER_DIST, COM_LOW_BAT_ACT,
#     NAV_RCL_ACT, NAV_DLL_ACT, RTL_RETURN_ALT, MAV_SYS_ID)
# Only values that make the aircraft land or return are accepted: "report
# only" or "warning" does nothing, "hold" hovers until something else acts,
# and "terminate" or "disarm" stops the motors in the air.

#: ArduCopter FENCE_TYPE bits: 1 max altitude, 2 circle, 4 polygon, 8 min altitude.
_AP_FENCE_ALT_MAX_BIT = 1
_AP_FENCE_CIRCLE_BIT = 2
#: ArduCopter FENCE_ACTION: 0 report only (refused); 1 RTL or land; 2 always
#: land; 3 SmartRTL or RTL or land; 4 brake or land; 5 SmartRTL or land.
_AP_FENCE_ACTIONS = frozenset({1, 2, 3, 4, 5})
#: ArduCopter BATT_FS_LOW_ACT / BATT_FS_CRT_ACT: 0 none and 5 terminate are
#: refused; 1 land, 2 RTL, 3 SmartRTL or RTL, 4 SmartRTL or land,
#: 6 DO_LAND_START or RTL, 7 brake or land.
_AP_BATT_ACTIONS = frozenset({1, 2, 3, 4, 6, 7})
#: ArduCopter FS_THR_ENABLE (RC loss): 0 disabled and 2 (continue a mission)
#: are refused; 1 RTL, 3 land, 4 SmartRTL or RTL, 5 SmartRTL or land,
#: 6 DO_LAND_START or RTL, 7 brake or land.
_AP_RC_LOSS_ACTIONS = frozenset({1, 3, 4, 5, 6, 7})
#: PX4 GF_ACTION: 0 none, 1 warning and 4 terminate are refused; 2 hold,
#: 3 return, 5 land.
_PX4_FENCE_ACTIONS = frozenset({2, 3, 5})
#: PX4 COM_LOW_BAT_ACT: 0 warning only is refused; 2 land, 3 return at
#: critical then land at emergency.
_PX4_BATT_ACTIONS = frozenset({2, 3})
#: PX4 NAV_RCL_ACT / NAV_DLL_ACT: 0 disabled, 1 hold (hovers), 5 terminate,
#: 6 disarm are refused; 2 return, 3 land.
_PX4_LOSS_ACTIONS = frozenset({2, 3})

#: The autopilot's own horizontal fence must contain the edge's square
#: geofence (corners at sqrt(2) times the half-width) plus this margin.
FENCE_MARGIN_M = 5.0
#: Regulatory ceiling (AGL) no altitude fence may exceed.
MAX_FENCE_ALT_M = 120.0
#: The system id every factory autopilot uses.
DEFAULT_SYSID = 1

#: Every parameter the checks read, per autopilot (read once, then cached).
FAILSAFE_PARAMS: dict[str, tuple[str, ...]] = {
    ARDUPILOT: (
        "FENCE_ENABLE", "FENCE_TYPE", "FENCE_ACTION", "FENCE_RADIUS", "FENCE_ALT_MAX", "BATT_FS_LOW_ACT",
        "BATT_FS_CRT_ACT", "BATT_LOW_VOLT", "BATT_LOW_MAH", "BATT_ARM_VOLT", "FS_THR_ENABLE", "RTL_ALT",
        "SYSID_THISMAV", "MAV_SYSID",
    ),
    PX4: (
        "GF_ACTION", "GF_MAX_HOR_DIST", "GF_MAX_VER_DIST", "COM_LOW_BAT_ACT", "NAV_RCL_ACT", "NAV_DLL_ACT",
        "RTL_RETURN_ALT", "MAV_SYS_ID",
    ),
}

#: Kept for callers that list the rules; the checks themselves are in ``failsafe_problems``.
FAILSAFE_RULES = FAILSAFE_PARAMS


def _int_in(value: float | None, allowed: frozenset[int]) -> bool:
    return value is not None and float(value).is_integer() and int(value) in allowed


def failsafe_problems(
    autopilot: str | None,
    read: Callable[[str], float | None],
    *,
    geofence_cm: float | None = None,
    ceiling_cm: float | None = None,
    slot_cm: float | None = None,
    sysid: int | None = DEFAULT_SYSID,
) -> list[str]:
    """Required autopilot settings that are missing or unsafe, as operator-facing text. Fails closed.

    *read* returns a parameter's value, or None when it does not exist or
    cannot be read; an unreadable parameter is a problem. With the edge's
    *geofence_cm*, *ceiling_cm* and *slot_cm* the autopilot's fence must
    contain the edge's geofence, its altitude fence must be above the
    ceiling (and at most 120 m), and its return altitude must be at or below
    the slot, whatever the edge's own failsafe action: the autopilot's own
    RC-loss and battery failsafes return by themselves. With *sysid* the
    autopilot's system id must match.
    """
    if autopilot not in AUTOPILOTS:
        return [f"autopilot type unknown ({autopilot!r}); cannot check its failsafes"]
    problems: list[str] = []

    def value(name: str) -> float | None:
        v = read(name)
        return float(v) if _finite(v) else None

    def need(name: str, allowed: frozenset[int], what: str) -> None:
        v = value(name)
        if v is None:
            problems.append(f"cannot read {name}; need {what}")
        elif not _int_in(v, allowed):
            problems.append(f"{name}={v:g}; need one of {sorted(allowed)} ({what})")

    fence_needed_m = None if geofence_cm is None else math.sqrt(2) * geofence_cm / 100.0 + FENCE_MARGIN_M
    ceiling_m = None if ceiling_cm is None else ceiling_cm / 100.0

    if autopilot == ARDUPILOT:
        enable = value("FENCE_ENABLE")
        if enable != 1:
            problems.append(f"FENCE_ENABLE={'unreadable' if enable is None else f'{enable:g}'}; need 1 (autopilot fence on)")
        ftype = value("FENCE_TYPE")
        if ftype is None or not float(ftype).is_integer():
            problems.append("cannot read FENCE_TYPE; need the circle (2) and max-altitude (1) fences")
        else:
            bits = int(ftype)
            if not bits & _AP_FENCE_CIRCLE_BIT:
                problems.append(f"FENCE_TYPE={bits}; need the circle fence (bit 2) so its radius can be checked")
            if not bits & _AP_FENCE_ALT_MAX_BIT:
                problems.append(f"FENCE_TYPE={bits}; need the max-altitude fence (bit 1)")
        need("FENCE_ACTION", _AP_FENCE_ACTIONS, "a fence breach must land or return")
        radius = value("FENCE_RADIUS")
        if radius is None:
            problems.append("cannot read FENCE_RADIUS")
        elif fence_needed_m is not None and radius < fence_needed_m:
            problems.append(f"FENCE_RADIUS={radius:g} m is inside the edge geofence; need at least {fence_needed_m:.0f} m")
        alt_max = value("FENCE_ALT_MAX")
        if alt_max is None:
            problems.append("cannot read FENCE_ALT_MAX")
        elif alt_max > MAX_FENCE_ALT_M or (ceiling_m is not None and alt_max <= ceiling_m):
            low = "" if ceiling_m is None else f"above the {ceiling_m:g} m ceiling and "
            problems.append(f"FENCE_ALT_MAX={alt_max:g} m; need {low}at most {MAX_FENCE_ALT_M:g} m")
        need("BATT_FS_LOW_ACT", _AP_BATT_ACTIONS, "low battery must land or return; 5 terminates")
        need("BATT_FS_CRT_ACT", _AP_BATT_ACTIONS, "critical battery must land or return; 5 terminates")
        thresholds = [v for v in (value("BATT_LOW_VOLT"), value("BATT_LOW_MAH")) if v is not None]
        if not any(v > 0 for v in thresholds):
            problems.append("need BATT_LOW_VOLT or BATT_LOW_MAH above 0 (low-battery threshold)")
        arm_volt = value("BATT_ARM_VOLT")
        if arm_volt is None or arm_volt <= 0:
            # ArduPilot's remaining percentage counts mAh since boot, so a
            # half-empty pack reads 100%: arming must check the voltage.
            problems.append("need BATT_ARM_VOLT above 0 (a partly used pack reads 100% after boot)")
        need("FS_THR_ENABLE", _AP_RC_LOSS_ACTIONS, "RC loss must land or return")
        rtl_name, rtl_factor = "RTL_ALT", 1.0
        sysid_names = ("SYSID_THISMAV", "MAV_SYSID")
    else:
        need("GF_ACTION", _PX4_FENCE_ACTIONS, "a fence breach must hold, return or land")
        hor = value("GF_MAX_HOR_DIST")
        if hor is None or hor <= 0:
            problems.append("need GF_MAX_HOR_DIST above 0 (horizontal autopilot fence)")
        elif fence_needed_m is not None and hor < fence_needed_m:
            problems.append(f"GF_MAX_HOR_DIST={hor:g} m is inside the edge geofence; need at least {fence_needed_m:.0f} m")
        ver = value("GF_MAX_VER_DIST")
        if ver is None or ver <= 0 or ver > MAX_FENCE_ALT_M or (ceiling_m is not None and ver <= ceiling_m):
            low = "" if ceiling_m is None else f"above the {ceiling_m:g} m ceiling and "
            shown = "unreadable" if ver is None else f"{ver:g} m"
            problems.append(f"GF_MAX_VER_DIST={shown}; need {low}at most {MAX_FENCE_ALT_M:g} m")
        need("COM_LOW_BAT_ACT", _PX4_BATT_ACTIONS, "low battery must land or return")
        need("NAV_RCL_ACT", _PX4_LOSS_ACTIONS, "RC loss must return or land; 1 hovers, 5 and 6 stop the motors")
        need("NAV_DLL_ACT", _PX4_LOSS_ACTIONS, "data-link loss must return or land; 1 hovers, 5 and 6 stop the motors")
        rtl_name, rtl_factor = "RTL_RETURN_ALT", 100.0
        sysid_names = ("MAV_SYS_ID",)

    rtl = value(rtl_name)
    if rtl is None:
        problems.append(f"cannot read {rtl_name} (return altitude)")
    elif slot_cm is not None and rtl * rtl_factor > slot_cm + 1e-6:
        problems.append(
            f"{rtl_name} return altitude {rtl * rtl_factor / 100:g} m is above this drone's slot {slot_cm / 100:g} m; "
            "the autopilot's own RC-loss or battery return would climb through other slots (set it at or below "
            "the slot; 0 returns at the current height)"
        )
    if sysid is not None:
        found = next((v for v in (value(n) for n in sysid_names) if v is not None), None)
        if found is None:
            problems.append(f"cannot read {' / '.join(sysid_names)} (system id)")
        elif int(found) != int(sysid):
            problems.append(f"autopilot system id is {found:g}, expected {sysid} (--mavlink-sysid)")
    return problems


def rtl_altitude_cm(autopilot: str | None, read: Callable[[str], float | None]) -> float | None:
    """The autopilot's return-to-launch altitude in centimetres above home, or None if unreadable."""
    entry = {ARDUPILOT: ("RTL_ALT", 1.0), PX4: ("RTL_RETURN_ALT", 100.0)}.get(autopilot or "")
    if entry is None:
        return None
    name, factor = entry
    value = read(name)
    return float(value) * factor if _finite(value) else None


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight_problems(
    state: AutopilotState,
    *,
    stale_after_s: float,
    min_satellites: int = DEFAULT_MIN_SATELLITES,
    param_problems: list[str] | None = None,
) -> list[str]:
    """Why the autopilot is not ready for an edge-commanded takeoff (empty when ready).

    Fails closed: a value that was never reported counts as not ready. The
    checks are the link (fresh telemetry), a known autopilot type, a 3D GPS
    fix with at least *min_satellites*, the autopilot's own global-position
    and home-position health flags, a battery reading, the
    autopilot's own arming checks (``armable``), and *param_problems* (the
    failsafe checks, read separately because parameter reads are slow).
    """
    if not state.connected:
        return ["autopilot not connected"]
    if not state.fresh(stale_after_s):
        age = state.telemetry_age_s
        return [f"autopilot telemetry stale ({'never received' if age is None else f'{age:.0f} s old'})"]
    problems: list[str] = []
    if state.stream_age_s:
        # A stream that ended keeps its last values; refuse on stale ones.
        stale = [name for name in ("position", "gps", "health", "battery")
                 if not state.stream_fresh(name, stale_after_s)]
        if stale:
            problems.append(f"telemetry stale or missing: {', '.join(stale)}")
    if state.autopilot not in AUTOPILOTS:
        problems.append("autopilot type unknown (PX4 or ArduPilot required)")
    fix = state.gps_fix_type
    if fix is None or fix < GPS_FIX_3D:
        problems.append(f"no 3D GPS fix (fix type {fix})")
    sats = state.satellites
    if sats is None or sats < min_satellites:
        problems.append(f"{sats} GPS satellites, need {min_satellites}")
    if state.global_position_ok is not True:
        problems.append("autopilot reports no global position estimate")
    # ArduPilot SITL reports the home-position health flag but MAVSDK's home
    # stream can stay silent (verified in SITL), so the flag is what counts.
    if state.home_position_ok is not True:
        problems.append("home position not set")
    if not state.has_position:
        problems.append("no position reported")
    if not _finite(state.battery_pct):
        problems.append("battery level unknown")
    if state.armable is not True:
        problems.append("autopilot arming checks fail (not armable)")
    if param_problems:
        problems.extend(param_problems)
    return problems


def params_reader(params: Mapping[str, float]) -> Callable[[str], float | None]:
    """A *read* function over a mapping, for tests and cached parameter sets."""
    return lambda name: params.get(name)
