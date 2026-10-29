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
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ARDUPILOT",
    "AUTOPILOTS",
    "FAILSAFE_RULES",
    "PX4",
    "AutopilotLink",
    "AutopilotState",
    "FailsafeRule",
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


@dataclass(frozen=True)
class FailsafeRule:
    """One required autopilot setting, checked from parameters.

    ``names`` are alternative parameter names (the first that exists is
    used, for renames across firmware versions); ``ok`` decides whether the
    value is acceptable; ``needs`` is the operator-facing requirement.
    """

    names: tuple[str, ...]
    ok: Callable[[float], bool]
    needs: str


def _nonzero(value: float) -> bool:
    return value != 0


def _positive(value: float) -> bool:
    return value > 0


#: Required failsafes per autopilot. Each is a reason the edge refuses to
#: take off when it is not configured. These are the minimum for a drone the
#: edge flies beyond a few metres: a geofence the autopilot enforces itself,
#: a battery failsafe that lands or returns before the pack is empty (so a
#: drone whose companion computer dies never hovers until it falls), and an
#: RC-loss failsafe (a safety pilot with a transmitter is required).
FAILSAFE_RULES: dict[str, tuple[FailsafeRule, ...]] = {
    ARDUPILOT: (
        FailsafeRule(("FENCE_ENABLE",), lambda v: v == 1, "FENCE_ENABLE=1 (autopilot geofence on)"),
        FailsafeRule(("FENCE_ACTION",), _nonzero, "FENCE_ACTION not 0 (geofence breach must act)"),
        FailsafeRule(("BATT_FS_LOW_ACT",), _nonzero, "BATT_FS_LOW_ACT not 0 (low-battery failsafe action)"),
        FailsafeRule(
            ("BATT_LOW_VOLT", "BATT_LOW_MAH"),
            _positive,
            "BATT_LOW_VOLT or BATT_LOW_MAH above 0 (low-battery failsafe threshold)",
        ),
        FailsafeRule(("FS_THR_ENABLE",), _nonzero, "FS_THR_ENABLE not 0 (RC-loss failsafe)"),
    ),
    PX4: (
        FailsafeRule(("GF_ACTION",), _nonzero, "GF_ACTION not 0 (geofence breach must act)"),
        FailsafeRule(
            ("GF_MAX_HOR_DIST", "GF_MAX_VER_DIST"),
            _positive,
            "GF_MAX_HOR_DIST or GF_MAX_VER_DIST above 0 (autopilot geofence on)",
        ),
        FailsafeRule(("COM_LOW_BAT_ACT",), _nonzero, "COM_LOW_BAT_ACT not 0 (low-battery failsafe action)"),
        FailsafeRule(("NAV_RCL_ACT",), _nonzero, "NAV_RCL_ACT not 0 (RC-loss failsafe)"),
        FailsafeRule(("NAV_DLL_ACT",), _nonzero, "NAV_DLL_ACT not 0 (data-link-loss failsafe)"),
    ),
}

#: Return-to-launch altitude parameter: name and the factor to centimetres.
_RTL_ALT_PARAM = {ARDUPILOT: ("RTL_ALT", 1.0), PX4: ("RTL_RETURN_ALT", 100.0)}


def _first_value(read: Callable[[str], float | None], names: tuple[str, ...]) -> tuple[str, float | None]:
    """The first of *names* that reads as a number (and its value), else the first name and None."""
    for name in names:
        value = read(name)
        if _finite(value):
            return name, float(value)
    return names[0], None


def failsafe_problems(autopilot: str | None, read: Callable[[str], float | None]) -> list[str]:
    """Required failsafes that are not configured, as operator-facing text. Fails closed.

    *read* returns a parameter's value, or None when it does not exist or
    cannot be read. An unknown autopilot, or a parameter that cannot be
    read, is itself a problem.
    """
    rules = FAILSAFE_RULES.get(autopilot or "")
    if rules is None:
        return [f"autopilot type unknown ({autopilot!r}); cannot check its failsafes"]
    problems: list[str] = []
    for rule in rules:
        if len(rule.names) > 1:
            values = [(name, read(name)) for name in rule.names]
            readable = [float(v) for _, v in values if _finite(v)]
            if not readable:
                problems.append(f"cannot read {' / '.join(rule.names)}; need {rule.needs}")
            elif not any(rule.ok(v) for v in readable):
                problems.append(f"need {rule.needs}")
            continue
        name, value = _first_value(read, rule.names)
        if value is None:
            problems.append(f"cannot read {name}; need {rule.needs}")
        elif not rule.ok(value):
            problems.append(f"{name}={value:g}; need {rule.needs}")
    return problems


def rtl_altitude_cm(autopilot: str | None, read: Callable[[str], float | None]) -> float | None:
    """The autopilot's return-to-launch altitude in centimetres above home, or None if unreadable."""
    entry = _RTL_ALT_PARAM.get(autopilot or "")
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
    and home-position health flags, a home position, a battery reading, the
    autopilot's own arming checks (``armable``), and *param_problems* (the
    failsafe checks, read separately because parameter reads are slow).
    """
    if not state.connected:
        return ["autopilot not connected"]
    if not state.fresh(stale_after_s):
        age = state.telemetry_age_s
        return [f"autopilot telemetry stale ({'never received' if age is None else f'{age:.0f} s old'})"]
    problems: list[str] = []
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
    if state.home_position_ok is not True or not state.has_home:
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
