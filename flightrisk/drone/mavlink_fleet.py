"""A PX4 or ArduPilot drone as one drone of a FlightRisk fleet (``--drone mavlink`` on the edge).

``MavlinkFleetDrone`` gives the edge's ``LaneExecutor`` the same controller
interface the Tello has (``takeoff``, ``land``, ``move``, ``rotate``,
``state``, ``read_battery``, ``read_height_cm``, ``link_status``), so the fleet
protocol, lane plans, epoch fencing, holds, manual takeover and the safety
envelope work unchanged. Underneath it commands an autopilot through an
``AutopilotLink`` (``flightrisk.drone.mavsdk_link`` over MAVSDK).

How lanes become autopilot setpoints
------------------------------------
Every lane move is a *position target*. The drone keeps the commanded target
in the plan frame (centimetres, ``x`` to the right of the frame heading, ``y``
ahead along it, altitude above the takeoff point), exactly the frame the
executor dead-reckons in, so the commanded path and the executor's pose never
drift apart. A move adds the move vector to the target, converts it to
latitude, longitude and altitude above mean sea level from the takeoff point
(a flat-earth offset, accurate to centimetres over a few hundred metres), and
sends it with ``goto_location`` (MAVLink ``MAV_CMD_DO_REPOSITION``). It then
waits until the autopilot's own position estimate is within the arrival
radius of the target.

Why ``goto_location`` and not offboard position setpoints: the autopilot
holds the last target by itself. Offboard mode (PX4 OFFBOARD, ArduPilot
GUIDED with ``SET_POSITION_TARGET_LOCAL_NED``) needs a continuous setpoint
stream from the companion computer, so a stalled or crashed edge process
triggers the autopilot's offboard-loss failsafe mid-lane and the edge has
to run a setpoint thread whose death is itself a hazard. With a reposition
target nothing has to stream: if the edge dies, the drone holds its last
target and the autopilot's own failsafes (battery, RC loss, geofence, and on
PX4 data-link loss) bring it down. The cost is a stop at the end of every
move; long lanes keep that rare (moves up to 50 m).

Watchdogs
---------
* Every move and takeoff has a deadline (twice the expected time plus a
  margin). A target not reached in time raises, and the executor lands.
* A landing interrupts any move or takeoff that is waiting (``land`` never
  waits for a move).
* The plan frame is set at each takeoff from the ground: the origin is the
  takeoff point and the frame heading is ``frame_heading_deg`` when given
  (recommended for a fleet: every drone then shares one compass frame,
  whatever way it was placed), else the vehicle's heading at takeoff (the
  Tello's convention).

External control
----------------
While the drone flies, any flight-mode change the edge did not make
*latches external control* for the rest of the flight (until it has landed
and disarmed): a mode outside the edge's own (an RC pilot flipped the mode
switch, a ground control station changed it), a landing or return the edge
did not ask for (the autopilot's own failsafe), and, once the autopilot has
entered a landing or return the edge asked for, any other mode, including
the other landing mode (a critical-battery LAND during the edge's return,
or a return during the edge's landing). From then on the drone sends no
move or takeoff, none of the edge's automatic landings and none of the
monitor's landing retries, so it never fights the pilot or the autopilot. A
new operator landing command (LAND ALL, a ground ``land``, the local land
key, Ctrl+C) sends one LAND, once per command. PX4 limitation: the edge uses
PX4's HOLD itself, so a pilot's HOLD switch is not told apart.

Landing and failsafe mapping
----------------------------
``land_for_reason`` maps the executor's landing reasons: ``link_loss``,
``low_battery`` and ``geofence_breach`` use ``failsafe_action`` (``rtl``
by default: the autopilot's return to launch; ``land`` lands in place); every
other reason lands in place. A return the autopilot refuses, or has not
entered within ``rtl_accept_s``, is replaced by a LAND, and later failsafe
landings of that flight land in place. The monitor's retries re-send the
edge's mode only while the autopilot has not entered it. Landing on the
ground is a no-op that succeeds.

Preflight
---------
``link_status()`` reports the drone not ready (reason ``preflight``) while
it is on the ground and any of these fail: fresh telemetry, a known
autopilot (PX4 or ArduPilot), a 3D GPS fix with enough satellites, the
autopilot's own global-position, home-position and arming checks, a battery
reading, and the required failsafes read from parameters
(``flightrisk.drone.autopilot.failsafe_problems``): only actions that land
or return, a horizontal fence containing the edge's geofence, an altitude
fence above the ceiling, a battery check at arming, the system id, and a
return altitude at or below this drone's slot, whatever ``failsafe_action``
(the autopilot's own RC-loss and battery failsafes return by themselves).
Parameters are read in the background after ``connect()`` and every
``param_refresh_s`` while on the ground; until the first read completes the
drone is not ready. The lane executor refuses a takeoff while the link is
not ready and never gates a landing on it.

None of this has flown real hardware. See ``docs/open-drone-support.md``.
"""
from __future__ import annotations

import contextlib
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from flightrisk.drone.autopilot import (
    ARDUPILOT,
    DEFAULT_MIN_SATELLITES,
    DEFAULT_SYSID,
    FAILSAFE_PARAMS,
    AutopilotLink,
    AutopilotState,
    autonomous_modes,
    failsafe_problems,
    preflight_problems,
    rtl_altitude_cm,
)
from flightrisk.drone.controller import (
    Capability,
    CommandOutcome,
    CommandResult,
    DroneCapabilities,
    DroneLinkStatus,
    DroneState,
)
from flightrisk.drone.geo import EARTH_RADIUS_M, offset_latlon
from flightrisk.drone.safety import FIELD_LIMITS, RelativePose
from flightrisk.drone.snapshot import (
    DEGREE,
    IS_FLYING_SENSED,
    METRE,
    PERCENT,
    TelemetrySnapshot,
    reading,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AUTOMATIC_LAND_REASONS",
    "FAILSAFE_ACTIONS",
    "FAILSAFE_REASONS",
    "MavlinkFleetDrone",
    "MavlinkFleetOptions",
]

#: What the drone does for the failsafe landing reasons.
FAILSAFE_ACTIONS = ("rtl", "land")
#: Landing reasons that use ``failsafe_action`` instead of landing in place.
FAILSAFE_REASONS = frozenset({"link_loss", "low_battery", "geofence_breach"})
#: Landing reasons the edge decides on its own. Under external control they
#: send nothing; every other reason (an operator's land) is always sent.
AUTOMATIC_LAND_REASONS = frozenset({
    "link_loss",
    "low_battery",
    "geofence",
    "geofence_breach",
    "ceiling",
    "ceiling_exceeded",
    "height_unknown",
    "hover_timeout",
    "on_complete",
    "executor_error",
    "control_error",
    "monitor_error",
    "shutdown",
})
_LANDING_MODES = frozenset({"LAND", "RETURN_TO_LAUNCH"})
_VERTICAL = {"up": 1.0, "down": -1.0}
_HORIZONTAL = {"forward": (0.0, 1.0), "back": (0.0, -1.0), "right": (1.0, 0.0), "left": (-1.0, 0.0)}
_POLL_S = 0.1
_TAKEOFF_RETRY_S = 2.0
_HEADING_TOLERANCE_DEG = 10.0
_DETAIL_MAX = 600
#: Takeoff height range (metres): above ground effect, below any sensible slot.
_TAKEOFF_ALT_RANGE_M = (2.0, 20.0)


@dataclass(frozen=True)
class MavlinkFleetOptions:
    """Settings of one MAVLink fleet drone. The defaults are deliberately conservative."""

    #: Height the takeoff climbs to before the executor climbs to the slot.
    takeoff_alt_m: float = 5.0
    #: Horizontal cruise speed requested from the autopilot.
    speed_mps: float = 3.0
    #: Compass bearing of the plan's "ahead" axis; None uses the vehicle's
    #: heading at takeoff.
    frame_heading_deg: float | None = None
    #: What link loss, low battery and a measured geofence breach do:
    #: ``rtl`` (return to launch) or ``land`` (land in place).
    failsafe_action: str = "rtl"
    #: Refuse takeoff unless the autopilot's failsafes are configured.
    require_failsafes: bool = True
    min_satellites: int = DEFAULT_MIN_SATELLITES
    #: Telemetry older than this means the autopilot link is down.
    stale_after_s: float = 3.0
    takeoff_timeout_s: float = 60.0
    #: Horizontal arrival radius of a position target (smaller for short moves).
    arrival_radius_m: float = 1.0
    #: Vertical arrival tolerance.
    arrival_alt_m: float = 0.7
    #: Added to twice the expected time of a move to make its deadline.
    move_timeout_margin_s: float = 20.0
    #: Degrees per second assumed for a rotation's deadline.
    yaw_rate_dps: float = 30.0
    #: How often failsafe parameters are read again while on the ground.
    param_refresh_s: float = 30.0
    #: How far outside the geofence the measured position may be before the
    #: edge lands for a breach (GPS noise allowance), in centimetres.
    fence_breach_margin_cm: float = 300.0
    #: The autopilot's MAVLink system id; the preflight refuses another one
    #: (None skips the check). Every factory autopilot is 1.
    sysid: int | None = DEFAULT_SYSID
    #: A return to launch the autopilot has not entered within this many
    #: seconds (refused, or no position for it) is replaced by a landing.
    rtl_accept_s: float = 5.0

    def __post_init__(self) -> None:
        if self.failsafe_action not in FAILSAFE_ACTIONS:
            raise ValueError(f"failsafe_action must be one of {FAILSAFE_ACTIONS}, got {self.failsafe_action!r}")
        for name in ("takeoff_alt_m", "speed_mps", "stale_after_s", "takeoff_timeout_s", "arrival_radius_m",
                     "arrival_alt_m", "move_timeout_margin_s", "yaw_rate_dps", "param_refresh_s", "rtl_accept_s"):
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be a positive number, got {value!r}")
        lo, hi = _TAKEOFF_ALT_RANGE_M
        if not lo <= self.takeoff_alt_m <= hi:
            raise ValueError(f"takeoff_alt_m must be within {lo:g}-{hi:g} m, got {self.takeoff_alt_m!r}")
        if self.sysid is not None and not (isinstance(self.sysid, int) and 1 <= self.sysid <= 255):
            raise ValueError(f"sysid must be 1 to 255 or None, got {self.sysid!r}")
        if self.speed_mps > 15:
            raise ValueError(f"speed_mps must be at most 15, got {self.speed_mps!r}")
        if self.frame_heading_deg is not None and not math.isfinite(self.frame_heading_deg):
            raise ValueError("frame_heading_deg must be finite or None")


@dataclass
class _Frame:
    """The plan frame of the current flight: takeoff point, home altitude, frame heading."""

    lat_deg: float
    lon_deg: float
    home_abs_alt_m: float
    heading_deg: float


@dataclass
class _Target:
    """The commanded position in the plan frame (centimetres) and pose heading (degrees)."""

    x_cm: float = 0.0
    y_cm: float = 0.0
    alt_cm: float = 0.0
    heading_deg: float = 0.0


@dataclass
class _ParamCheck:
    """The failsafe parameters as last read; the checks run on them against the live envelope."""

    values: dict[str, float | None] = field(default_factory=dict)
    autopilot: str | None = None
    checked_at: float | None = None


def _clean(text: str) -> str:
    return "".join(ch for ch in text if ch.isprintable())[:_DETAIL_MAX]


class MavlinkFleetDrone:
    """A PX4 or ArduPilot multirotor flown by the fleet edge (see the module docstring)."""

    def __init__(
        self,
        name: str,
        link: AutopilotLink,
        options: MavlinkFleetOptions | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self.link = link
        self.host = getattr(link, "address", "mavlink")
        self.options = options if options is not None else MavlinkFleetOptions()
        self.capabilities = DroneCapabilities(
            has_gps=True,
            has_rtsp=False,
            min_move_cm=int(FIELD_LIMITS.min_move_cm),
            max_move_cm=int(FIELD_LIMITS.max_move_cm),
            max_altitude_m=int(FIELD_LIMITS.max_alt_slot_cm / 100),
            supports_missions=False,
            features=frozenset({Capability.TAKEOFF_LAND, Capability.HOVER, Capability.RELATIVE_MOVE,
                                Capability.ROTATE}),
            flight_scale=FIELD_LIMITS.name,
        )
        #: Horizontal slack before a measured position outside the geofence lands the drone.
        self.fence_breach_margin_cm = float(self.options.fence_breach_margin_cm)
        self._clock = clock
        self._lock = threading.RLock()
        self._frame: _Frame | None = None
        self._target = _Target()
        # Set by every landing: a waiting move or takeoff stops at once.
        self._interrupt = threading.Event()
        self._external: str | None = None
        # The landing mode the edge asked for this flight (LAND or
        # RETURN_TO_LAUNCH), or None; when it asked, and whether the
        # autopilot has reported that mode since.
        self._requested_landing: str | None = None
        self._landing_requested_at = 0.0
        self._landing_accepted = False
        # A return the autopilot refused (or never entered) this flight:
        # every later failsafe landing lands in place.
        self._rtl_failed = False
        # Whether the autopilot took the requested cruise speed (None: not tried).
        self._speed_set: bool | None = None
        self._params = _ParamCheck()
        self._param_stop = threading.Event()
        self._param_thread: threading.Thread | None = None
        self._slot_cm: Callable[[], float | None] | None = None
        self._envelope: Any = None
        self._connected = False

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def bind_slot_source(self, slot_cm: Callable[[], float | None]) -> None:
        """Give the drone its current altitude slot (for the return-altitude check)."""
        self._slot_cm = slot_cm

    def bind_envelope(self, envelope: Any) -> None:
        """Check the autopilot's fence, altitude fence and return altitude against this live envelope.

        The edge binds its ``SafetyEnvelope``: its geofence, ceiling and
        altitude slot (which a plan can change) are read at every check.
        """
        self._envelope = envelope
        self._slot_cm = lambda: envelope.alt_slot_cm

    def _limits(self) -> dict[str, float | None]:
        env = self._envelope
        return {
            "geofence_cm": getattr(env, "geofence_cm", None) if env is not None else None,
            "ceiling_cm": getattr(env, "ceiling_cm", None) if env is not None else None,
            "slot_cm": self._slot_cm() if self._slot_cm is not None else None,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        try:
            self.link.connect()
        except Exception as exc:  # noqa: BLE001 - reported, not raised (Lifecycle contract)
            logger.error("[%s] autopilot connect failed: %r", self.name, exc)
            return False
        with self._lock:
            self._connected = True
        self._param_stop.clear()
        self._param_thread = threading.Thread(target=self._param_loop, daemon=True, name=f"{self.name}-params")
        self._param_thread.start()
        return True

    def disconnect(self) -> None:
        """Stop reading parameters and close the link. Idempotent; sends no flight command."""
        self._param_stop.set()
        thread = self._param_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            self._connected = False
        try:
            self.link.disconnect()
        except Exception as exc:  # noqa: BLE001 - teardown continues
            logger.warning("[%s] autopilot disconnect failed: %r", self.name, exc)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _autopilot_state(self) -> AutopilotState:
        state = self.link.state()
        self._note_mode(state)
        return state

    @staticmethod
    def _flying(state: AutopilotState) -> bool:
        if state.in_air is not None:
            return bool(state.in_air)
        # No in-air report: armed and clearly off the ground counts as flying.
        return bool(state.armed) and isinstance(state.rel_alt_m, (int, float)) and state.rel_alt_m > 1.0

    def _note_mode(self, state: AutopilotState) -> None:
        """Latch external control on any flight-mode change the edge did not make (see the module docstring).

        While no landing is requested, only the edge's own flight modes are
        expected. Once it requested a landing or a return, that mode is
        expected (the previous one is tolerated until the autopilot enters
        it); after the autopilot entered it, any other mode, including the
        other landing mode, is someone else: a pilot, a ground control
        station or the autopilot's own failsafe.
        """
        mode = state.flight_mode
        if not self._flying(state):
            if state.armed is False:
                # Landed and disarmed: that flight is over, and so is the external control.
                with self._lock:
                    ended, self._external = self._external, None
                if ended is not None:
                    logger.info("[%s] external control ended: the drone landed and disarmed", self.name)
            return
        if mode is None:
            return
        autonomous = autonomous_modes(state.autopilot)
        with self._lock:
            if self._external is not None:
                return
            requested = self._requested_landing
            if requested is None:
                if mode not in autonomous:
                    reason = f"flight mode {mode} set outside FlightRisk (RC pilot or ground control station)"
                elif mode in _LANDING_MODES:
                    reason = f"autopilot switched to {mode} by itself (its own failsafe)"
                else:
                    return
            elif mode == requested:
                self._landing_accepted = True
                return
            elif self._landing_accepted or mode not in autonomous or mode in _LANDING_MODES:
                reason = f"flight mode changed from {requested} to {mode} outside FlightRisk " \
                         "(RC pilot, ground control station or the autopilot's own failsafe)"
            else:
                return  # still in the previous mode, waiting for the autopilot to switch
            self._external = reason
        self._interrupt.set()
        logger.warning("[%s] external control latched: %s", self.name, reason)

    @property
    def external_control(self) -> str | None:
        """Why the edge no longer commands this flight, or None (reads the latest telemetry first)."""
        with contextlib.suppress(Exception):  # an unreadable link leaves the latch as it is
            self._autopilot_state()
        return self._external_latched()

    def _external_latched(self) -> str | None:
        with self._lock:
            return self._external

    @property
    def state(self) -> DroneState:
        """The legacy state view, rebuilt from the autopilot's latest telemetry."""
        st = self._autopilot_state()
        fresh = st.fresh(self.options.stale_after_s)
        return DroneState(
            battery=round(st.battery_pct) if fresh and _number(st.battery_pct) else 0,
            height=round(st.rel_alt_m * 100) if fresh and _number(st.rel_alt_m) else 0,
            is_flying=self._flying(st),
            is_connected=fresh,
            latitude=st.lat_deg,
            longitude=st.lon_deg,
            altitude_msl=st.abs_alt_m,
            heading=None if st.heading_deg is None else int(st.heading_deg) % 360,
            flight_mode=st.flight_mode,
        )

    def _fresh(self, st: AutopilotState, stream: str) -> bool:
        """The link and *stream* are fresh (a stream that ended keeps its last, stale values)."""
        if not st.stream_age_s:
            return st.fresh(self.options.stale_after_s)
        return st.stream_fresh(stream, self.options.stale_after_s)

    def read_battery(self) -> int | None:
        """Battery percentage, or None when unknown or stale (an unknown battery never permits a takeoff)."""
        st = self._autopilot_state()
        if not self._fresh(st, "battery") or not _number(st.battery_pct):
            return None
        return round(st.battery_pct)

    def read_height_cm(self) -> int | None:
        """Height above home in centimetres, or None when unknown or stale."""
        st = self._autopilot_state()
        if not self._fresh(st, "position") or not _number(st.rel_alt_m):
            return None
        return round(st.rel_alt_m * 100)

    def measured_pose(self) -> RelativePose | None:
        """The autopilot's measured position in the plan frame, or None before the first takeoff."""
        st = self._autopilot_state()
        with self._lock:
            frame, heading = self._frame, self._target.heading_deg
        if frame is None or not st.has_position or not self._fresh(st, "position"):
            return None
        north, east = _offset_m(frame.lat_deg, frame.lon_deg, st.lat_deg, st.lon_deg)
        x_m, y_m = _to_plan(north, east, frame.heading_deg)
        yaw = st.heading_deg
        pose_heading = heading if yaw is None else (yaw - frame.heading_deg) % 360.0
        return RelativePose(x_m * 100.0, y_m * 100.0, pose_heading)

    def geo_position(self) -> tuple[float, float] | None:
        """Latitude and longitude in degrees, or None."""
        st = self._autopilot_state()
        if not st.has_position or not self._fresh(st, "position"):
            return None
        return float(st.lat_deg), float(st.lon_deg)

    def telemetry_fields(self) -> dict[str, Any]:
        """Optional telemetry fields the executor adds (every ground accepts extra fields)."""
        st = self.link.state()
        return {
            "autopilot": st.autopilot,
            "flight_mode": st.flight_mode,
            "speed_set": self._speed_set,
            "external_control": self._external_latched() is not None,
        }

    def check_slot(self, alt_cm: float) -> str | None:
        """Why the altitude slot *alt_cm* is not allowed for this drone, or None.

        The autopilot's own return altitude must not be above the slot: its
        RC-loss and battery failsafes return by themselves, and a return
        from a lower slot would climb through the other drones' slots. The
        lane executor asks this before it accepts a plan's slot, in the air
        too.
        """
        if not self.options.require_failsafes:
            return None
        with self._lock:
            check = self._params
        if check.checked_at is None:
            return "the autopilot's return altitude has not been read yet"
        rtl_cm = rtl_altitude_cm(check.autopilot, check.values.get)
        if rtl_cm is None:
            return "the autopilot's return altitude cannot be read"
        if rtl_cm > alt_cm + 1e-6:
            return (f"slot {alt_cm / 100:g} m is below the autopilot's return altitude {rtl_cm / 100:g} m; "
                    "an autopilot return would climb through other slots")
        return None

    def snapshot(self) -> TelemetrySnapshot:
        st = self._autopilot_state()
        fresh = st.fresh(self.options.stale_after_s)
        at = time.time() - (st.telemetry_age_s or 0.0) if fresh else None
        return TelemetrySnapshot(
            backend="mavlink-fleet",
            drone_id=self.name,
            captured_at=time.time(),
            connected=fresh,
            is_flying=self._flying(st),
            is_flying_source=IS_FLYING_SENSED,
            flight_mode=st.flight_mode,
            battery_pct=reading(st.battery_pct, PERCENT, "mavlink.battery", at),
            height_m=reading(st.rel_alt_m, METRE, "mavlink.rel_alt", at),
            altitude_msl_m=reading(st.abs_alt_m, METRE, "mavlink.position", at),
            latitude_deg=reading(st.lat_deg, DEGREE, "mavlink.position", at),
            longitude_deg=reading(st.lon_deg, DEGREE, "mavlink.position", at),
            heading_deg=reading(st.heading_deg, DEGREE, "mavlink.heading", at),
        )

    # ------------------------------------------------------------------
    # Readiness
    # ------------------------------------------------------------------

    def preflight_problems(self, state: AutopilotState | None = None) -> list[str]:
        """Why a takeoff would be refused now (empty when ready). See the module docstring."""
        st = self._autopilot_state() if state is None else state
        params = self._param_problems() if self.options.require_failsafes else []
        return preflight_problems(
            st,
            stale_after_s=self.options.stale_after_s,
            min_satellites=self.options.min_satellites,
            param_problems=params,
        )

    def _param_problems(self) -> list[str]:
        """The failsafe checks on the parameters as last read, against the live envelope."""
        with self._lock:
            check = self._params
        if check.checked_at is None:
            return ["failsafe parameters not checked yet"]
        return failsafe_problems(check.autopilot, check.values.get, sysid=self.options.sysid, **self._limits())

    def link_status(self) -> DroneLinkStatus:
        """Whether the edge may command this drone now (read-only; sends nothing)."""
        st = self._autopilot_state()
        age = st.telemetry_age_s
        if not st.connected:
            return DroneLinkStatus(False, age, "not_connected", "autopilot not connected")
        if not st.fresh(self.options.stale_after_s):
            text = "never received" if age is None else f"no telemetry for {age:.0f} s"
            return DroneLinkStatus(False, age, "state_stale", f"autopilot unreachable ({text})")
        external = self._external_latched()
        if external is not None:
            return DroneLinkStatus(False, age, "external_control", _clean(external))
        if not self._flying(st):
            problems = self.preflight_problems(st)
            if problems:
                return DroneLinkStatus(False, age, "preflight", _clean("not ready: " + "; ".join(problems)))
        return DroneLinkStatus(True, age)

    def _param_loop(self) -> None:
        while not self._param_stop.is_set():
            try:
                if not self._flying(self.link.state()):
                    self.refresh_params()
            except Exception as exc:  # noqa: BLE001 - logged; the check stays failed
                logger.warning("[%s] failsafe parameter check failed: %r", self.name, exc)
            self._param_stop.wait(self.options.param_refresh_s)

    def refresh_params(self) -> list[str]:
        """Read the failsafe parameters now; returns the problems found (stored for later checks)."""
        refresh = getattr(self.link, "refresh_autopilot", None)
        if callable(refresh) and self.link.state().autopilot is None:
            refresh()  # the type could not be probed at connect; try again
        autopilot = self.link.state().autopilot
        names = FAILSAFE_PARAMS.get(autopilot or "", ())
        values = {name: self.link.get_param(name) for name in names}
        with self._lock:
            self._params = _ParamCheck(values=values, autopilot=autopilot, checked_at=self._clock())
        found = self._param_problems()
        if found:
            logger.warning("[%s] autopilot failsafes not ready: %s", self.name, "; ".join(found))
        return found

    # ------------------------------------------------------------------
    # Flight commands
    # ------------------------------------------------------------------

    def takeoff(self, abort: threading.Event | None = None) -> CommandResult:
        """Arm, take off to ``takeoff_alt_m`` and wait until there; sends nothing when refused.

        Refused (``NOT_EXECUTED``) when *abort* is set or a landing is in
        progress, and (``REJECTED``) when the preflight fails. Raises when
        the autopilot refuses a command or the takeoff does not reach its
        height before the deadline.
        """
        if abort is not None and abort.is_set():
            return CommandResult("takeoff", CommandOutcome.NOT_EXECUTED, "aborted")
        st = self._autopilot_state()
        if self._flying(st):
            return CommandResult("takeoff")  # already flying: nothing to do
        problems = self.preflight_problems(st)
        if problems:
            return CommandResult("takeoff", CommandOutcome.REJECTED, _clean("; ".join(problems)))
        home_alt = st.home_abs_alt_m if _number(st.home_abs_alt_m) else st.abs_alt_m - st.rel_alt_m
        heading = self.options.frame_heading_deg
        if heading is None:
            heading = st.heading_deg if _number(st.heading_deg) else 0.0
        alt_m = self.options.takeoff_alt_m
        with self._lock:
            if abort is not None and abort.is_set():
                return CommandResult("takeoff", CommandOutcome.NOT_EXECUTED, "aborted")
            # A new flight: this spot is the origin of the plan frame.
            self._frame = _Frame(float(st.lat_deg), float(st.lon_deg), float(home_alt), float(heading) % 360.0)
            self._target = _Target(alt_cm=alt_m * 100.0)
            self._external = None
            self._requested_landing = None
            self._landing_accepted = False
            self._rtl_failed = False
            self._interrupt.clear()
        logger.info("[%s] takeoff to %.1f m (frame heading %.0f deg)", self.name, alt_m, heading)
        speed_set = self._try_set_speed(quiet=True)
        self._speed_set = speed_set
        deadline = self._clock() + self.options.takeoff_timeout_s
        refused = self._arm_and_take_off(alt_m, abort, deadline)
        if refused is not None:
            return refused
        while True:
            st = self._autopilot_state()
            if self._interrupted(abort):
                return CommandResult("takeoff")  # it did take off; the landing takes over
            if self._flying(st) and _number(st.rel_alt_m) and st.rel_alt_m >= alt_m - self.options.arrival_alt_m:
                if not speed_set:
                    # ArduCopter SITL refuses the speed change on the ground.
                    self._speed_set = self._try_set_speed(quiet=False)
                return CommandResult("takeoff")
            if self._clock() >= deadline:
                raise TimeoutError(f"takeoff did not reach {alt_m:g} m within {self.options.takeoff_timeout_s:g} s")
            self._interrupt.wait(_POLL_S)

    def _arm_and_take_off(
        self, alt_m: float, abort: threading.Event | None, deadline: float
    ) -> CommandResult | None:
        """Arm and send the takeoff, retrying a refusal until *deadline*. None once accepted.

        ArduCopter refuses a takeoff for a while after its preflight checks
        pass ("GUIDED not ready" in SITL, about 20 s); the refusal sends the
        drone nowhere, so it is retried every ``_TAKEOFF_RETRY_S`` until the
        deadline, then raised. A landing or abort stops the retries
        (``NOT_EXECUTED``): nothing is lifted.
        """
        while True:
            if self._interrupted(abort):
                return CommandResult("takeoff", CommandOutcome.NOT_EXECUTED, "aborted before takeoff")
            try:
                if not self.link.state().armed:
                    self.link.arm()
                if self._interrupted(abort):
                    # A landing arrived while arming: do not lift. The land
                    # path treats a vehicle on the ground as landed.
                    return CommandResult("takeoff", CommandOutcome.NOT_EXECUTED, "aborted after arming")
                self.link.takeoff(alt_m)
                return None
            except Exception as exc:
                if self._clock() + _TAKEOFF_RETRY_S >= deadline:
                    raise
                logger.warning("[%s] takeoff refused, retrying: %r", self.name, exc)
                self._interrupt.wait(_TAKEOFF_RETRY_S)

    def _try_set_speed(self, *, quiet: bool) -> bool:
        """Ask the autopilot for ``speed_mps``; best effort (its own cruise speed applies otherwise)."""
        try:
            self.link.set_speed(self.options.speed_mps)
            return True
        except Exception as exc:  # noqa: BLE001 - logged; never a reason to refuse a flight
            log = logger.debug if quiet else logger.warning
            log("[%s] set_speed refused, autopilot cruise speed applies: %r", self.name, exc)
            return False

    def _interrupted(self, abort: threading.Event | None = None) -> bool:
        return self._interrupt.is_set() or (abort is not None and abort.is_set())

    def move(self, direction: str, distance_cm: int) -> CommandResult:
        """Fly to the commanded target plus one move; returns once the autopilot is there."""
        if direction not in _HORIZONTAL and direction not in _VERTICAL:
            return CommandResult("move", CommandOutcome.REJECTED, f"unknown direction {direction!r}")
        if not _number(distance_cm) or distance_cm <= 0 or distance_cm > FIELD_LIMITS.max_move_cm:
            return CommandResult("move", CommandOutcome.REJECTED, f"distance {distance_cm!r} cm out of range")
        refusal = self._command_refusal()
        if refusal is not None:
            return refusal("move")
        with self._lock:
            target = _Target(**vars(self._target))
        if direction in _VERTICAL:
            target.alt_cm += _VERTICAL[direction] * distance_cm
        else:
            target.x_cm, target.y_cm = RelativePose(target.x_cm, target.y_cm, target.heading_deg).endpoint(
                direction, distance_cm
            )
        expected_s = distance_cm / 100.0 / self.options.speed_mps
        return self._fly_to(target, "move", 2 * expected_s + self.options.move_timeout_margin_s, distance_cm / 100.0)

    def rotate(self, degrees: int) -> CommandResult:
        """Yaw by *degrees* (clockwise positive) over the commanded position; returns once turned."""
        if not _number(degrees):
            return CommandResult("rotate", CommandOutcome.REJECTED, f"invalid rotation {degrees!r}")
        refusal = self._command_refusal()
        if refusal is not None:
            return refusal("rotate")
        with self._lock:
            target = _Target(**vars(self._target))
        target.heading_deg = (target.heading_deg + degrees) % 360.0
        if self.link.state().autopilot == ARDUPILOT:
            # ArduCopter ignores the yaw of a reposition (MAV_CMD_DO_REPOSITION)
            # and, in GUIDED, turns to face its direction of travel
            # (WP_YAW_BEHAVIOR). The rotation therefore turns the plan frame's
            # "forward" for the next moves without waiting for a heading the
            # autopilot will never hold.
            with self._lock:
                if self._interrupt.is_set():
                    return CommandResult("rotate", CommandOutcome.NOT_EXECUTED, "interrupted by a landing")
                self._target = target
            return CommandResult("rotate", CommandOutcome.OK, "plan heading turned; ArduCopter faces its travel")
        timeout = abs(degrees) / self.options.yaw_rate_dps * 2 + self.options.move_timeout_margin_s
        return self._fly_to(target, "rotate", timeout, 0.0, check_heading=True)

    def hover(self) -> CommandResult:
        """Hold the commanded position (the autopilot already holds it between moves)."""
        refusal = self._command_refusal()
        if refusal is not None:
            return refusal("hover")
        with self._lock:
            target = _Target(**vars(self._target))
        self._send_target(target)
        return CommandResult("hover")

    def rc_control(self, lr: int, fb: int, ud: int, yaw: int) -> CommandResult:
        """Not supported: stick input over MAVLink is not sent; the safety pilot's transmitter flies by hand."""
        return CommandResult("rc_control", CommandOutcome.REJECTED, "use the RC transmitter for manual flight")

    def get_frame(self) -> np.ndarray | None:
        """No video here: the edge reads the companion computer's camera (``--source``)."""
        return None

    def on_frame(self, cb: Callable[[np.ndarray], None]) -> None:
        """No video here (see ``get_frame``); the callback is never called."""
        return

    def _command_refusal(self) -> Callable[[str], CommandResult] | None:
        st = self._autopilot_state()
        external = self._external_latched()
        if external is not None:
            return lambda name: CommandResult(name, CommandOutcome.REJECTED, _clean("external control: " + external))
        if not st.fresh(self.options.stale_after_s):
            return lambda name: CommandResult(name, CommandOutcome.NOT_EXECUTED, "autopilot telemetry stale")
        with self._lock:
            landing, frame = self._requested_landing, self._frame
        if landing is not None or self._interrupt.is_set():
            return lambda name: CommandResult(name, CommandOutcome.NOT_EXECUTED, "landing in progress")
        if not self._flying(st) or frame is None:
            return lambda name: CommandResult(name, CommandOutcome.NOT_EXECUTED, "not flying")
        return None

    def _send_target(self, target: _Target) -> None:
        with self._lock:
            frame = self._frame
        assert frame is not None
        north, east = _to_ned(target.x_cm / 100.0, target.y_cm / 100.0, frame.heading_deg)
        lat, lon = offset_latlon(frame.lat_deg, frame.lon_deg, north, east)
        # Both autopilots reset home at arming, so the altitude reference is
        # taken now: the AMSL of the height the autopilot reports as 0.
        st = self.link.state()
        base = st.abs_alt_m - st.rel_alt_m if _number(st.abs_alt_m) and _number(st.rel_alt_m) else frame.home_abs_alt_m
        self.link.goto(lat, lon, base + target.alt_cm / 100.0, (frame.heading_deg + target.heading_deg) % 360.0)

    def _fly_to(
        self, target: _Target, command: str, timeout_s: float, distance_m: float, check_heading: bool = False
    ) -> CommandResult:
        self._send_target(target)
        radius = min(self.options.arrival_radius_m, max(0.3, distance_m / 3.0))
        deadline = self._clock() + timeout_s
        while True:
            if self._interrupt.is_set():
                external = self._external_latched()
                detail = "external control" if external else "interrupted by a landing"
                return CommandResult(command, CommandOutcome.NOT_EXECUTED, detail)
            if self._arrived(target, radius, check_heading):
                with self._lock:
                    if self._interrupt.is_set():
                        continue
                    self._target = target
                return CommandResult(command)
            if self._clock() >= deadline:
                raise TimeoutError(f"{command} target not reached within {timeout_s:.0f} s")
            self._interrupt.wait(_POLL_S)

    def _arrived(self, target: _Target, radius_m: float, check_heading: bool) -> bool:
        st = self._autopilot_state()
        if not self._fresh(st, "position") or not st.has_position:
            return False
        with self._lock:
            frame = self._frame
        if frame is None:
            return False
        north, east = _offset_m(frame.lat_deg, frame.lon_deg, st.lat_deg, st.lon_deg)
        tn, te = _to_ned(target.x_cm / 100.0, target.y_cm / 100.0, frame.heading_deg)
        if math.hypot(north - tn, east - te) > radius_m:
            return False
        if abs(st.rel_alt_m - target.alt_cm / 100.0) > self.options.arrival_alt_m:
            return False
        if check_heading:
            if not _number(st.heading_deg):
                return False
            want = (frame.heading_deg + target.heading_deg) % 360.0
            error = abs((st.heading_deg - want + 180.0) % 360.0 - 180.0)
            return error <= _HEADING_TOLERANCE_DEG
        return True

    # ------------------------------------------------------------------
    # Landing
    # ------------------------------------------------------------------

    def land(self) -> CommandResult:
        """Land in place (an operator's land). Never dropped; see ``land_for_reason``."""
        return self.land_for_reason("land")

    def land_for_reason(self, reason: str, *, retry: bool = False) -> CommandResult:
        """Land, or return to launch, for the executor's landing *reason*. Returns once the autopilot accepted it.

        *retry* is True when the executor's monitor repeats a landing that
        has not finished yet. Rules (conservative; see the module docstring):

        * On the ground (disarmed) it succeeds without sending.
        * Under external control a retry and every automatic reason send
          nothing; a new operator command (LAND ALL, a ground land, the
          local key, Ctrl+C) sends one LAND.
        * A retry re-sends the edge's own mode only while the autopilot has
          not entered it yet. A return the autopilot refused, or has not
          entered within ``rtl_accept_s``, is replaced by a LAND.
        * The failsafe reasons return (``failsafe_action`` ``rtl``) unless a
          return already failed this flight; every other reason lands.

        Raises when the autopilot is not connected or refuses a LAND, so the
        executor retries.
        """
        st = self._autopilot_state()
        if not st.connected:
            raise RuntimeError("autopilot not connected; land not sent")
        # Any waiting move or takeoff stops now, whatever happens below.
        self._interrupt.set()
        if not self._flying(st) and st.landed_state not in ("TAKING_OFF",) and not st.armed:
            return CommandResult("land")
        now = self._clock()
        with self._lock:
            external = self._external
            requested, accepted = self._requested_landing, self._landing_accepted
            waited = now - self._landing_requested_at
            rtl_failed = self._rtl_failed
        if external is not None:
            if retry or reason in AUTOMATIC_LAND_REASONS:
                logger.warning("[%s] %s landing not sent: %s", self.name, reason, external)
                return CommandResult("land", CommandOutcome.OK, "external control; not sent")
            logger.warning("[%s] operator %s under external control: sending LAND once", self.name, reason)
            return self._send_landing("LAND", reason)
        wanted = "RETURN_TO_LAUNCH" if (
            reason in FAILSAFE_REASONS and self.options.failsafe_action == "rtl" and not rtl_failed
        ) else "LAND"
        if requested == "RETURN_TO_LAUNCH" and not accepted and waited >= self.options.rtl_accept_s:
            logger.error("[%s] the autopilot did not enter RETURN_TO_LAUNCH within %.0f s; landing instead",
                         self.name, self.options.rtl_accept_s)
            with self._lock:
                self._rtl_failed = True
            return self._send_landing("LAND", reason)
        if retry and requested is not None:
            if accepted:
                return CommandResult("land", CommandOutcome.OK, f"{requested} in progress")
            if requested == "RETURN_TO_LAUNCH":
                return CommandResult("land", CommandOutcome.OK, "waiting for the autopilot to enter RETURN_TO_LAUNCH")
            return self._send_landing(requested, reason)  # never accepted: send it again
        if requested == wanted and accepted and st.flight_mode == wanted:
            return CommandResult("land", CommandOutcome.OK, f"{wanted} already in progress")
        return self._send_landing(wanted, reason)

    def _send_landing(self, mode: str, reason: str) -> CommandResult:
        """Send LAND or RETURN_TO_LAUNCH and record it. A refused return falls back to LAND at once."""
        with self._lock:
            self._requested_landing = mode
            self._landing_requested_at = self._clock()
            self._landing_accepted = False
        logger.warning("[%s] %s for %s", self.name, "return to launch" if mode != "LAND" else "land", reason)
        if mode == "LAND":
            self.link.land()
            return CommandResult("land", CommandOutcome.OK, "LAND")
        try:
            self.link.return_to_launch()
        except Exception as exc:  # noqa: BLE001 - a refused return must still bring the drone down
            logger.error("[%s] return to launch refused (%r); landing instead", self.name, exc)
            with self._lock:
                self._rtl_failed = True
            return self._send_landing("LAND", reason)
        return CommandResult("land", CommandOutcome.OK, "RETURN_TO_LAUNCH")


# ---------------------------------------------------------------------------
# Frame math
# ---------------------------------------------------------------------------


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _offset_m(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """North and east metres from (lat0, lon0) to (lat, lon); flat earth, fine over a few km."""
    north = math.radians(lat - lat0) * EARTH_RADIUS_M
    east = math.radians(lon - lon0) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    return north, east


def _to_ned(x_m: float, y_m: float, heading_deg: float) -> tuple[float, float]:
    """Plan frame (x right, y ahead of *heading_deg*) to north and east metres."""
    h = math.radians(heading_deg)
    return y_m * math.cos(h) - x_m * math.sin(h), y_m * math.sin(h) + x_m * math.cos(h)


def _to_plan(north_m: float, east_m: float, heading_deg: float) -> tuple[float, float]:
    """North and east metres to the plan frame (x right, y ahead of *heading_deg*)."""
    h = math.radians(heading_deg)
    return -north_m * math.sin(h) + east_m * math.cos(h), north_m * math.cos(h) + east_m * math.sin(h)
