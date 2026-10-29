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
While the drone flies, a flight mode the edge did not put it in (an RC pilot
flipped the mode switch, or a ground control station changed it), or a
landing or return the edge did not ask for (the autopilot's own failsafe),
*latches external control* for the rest of the flight. From then on the
drone sends no move or takeoff, and the edge's automatic landings (link
loss, battery, geofence, errors) send nothing either, so it never fights
the pilot or the autopilot's failsafe. An explicit operator landing (LAND
ALL, a ground ``land``, the local land key, Ctrl+C) is still sent.

Landing and failsafe mapping
----------------------------
``land_for_reason`` maps the executor's landing reasons: ``link_loss``,
``low_battery`` and ``geofence_breach`` use ``failsafe_action`` (``rtl``
by default: the autopilot's return to launch; ``land`` lands in place); every
other reason lands in place. A landing or return already under way is not
sent again, so the executor's land retries never turn a return into a
landing. Landing on the ground is a no-op that succeeds.

Preflight
---------
``link_status()`` reports the drone not ready (reason ``preflight``) while
it is on the ground and any of these fail: fresh telemetry, a known
autopilot (PX4 or ArduPilot), a 3D GPS fix with enough satellites, the
autopilot's own global-position, home-position and arming checks, a battery
reading, and the required failsafes read from parameters
(``flightrisk.drone.autopilot.FAILSAFE_RULES``). With ``failsafe_action``
``rtl``, the return altitude must also be at or below this drone's altitude
slot, so a return never climbs above its slot (or another drone's).
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
    DEFAULT_MIN_SATELLITES,
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
_HEADING_TOLERANCE_DEG = 10.0
_DETAIL_MAX = 200


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

    def __post_init__(self) -> None:
        if self.failsafe_action not in FAILSAFE_ACTIONS:
            raise ValueError(f"failsafe_action must be one of {FAILSAFE_ACTIONS}, got {self.failsafe_action!r}")
        for name in ("takeoff_alt_m", "speed_mps", "stale_after_s", "takeoff_timeout_s", "arrival_radius_m",
                     "arrival_alt_m", "move_timeout_margin_s", "yaw_rate_dps", "param_refresh_s"):
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be a positive number, got {value!r}")
        if not FIELD_LIMITS.min_alt_slot_cm <= self.takeoff_alt_m * 100 <= FIELD_LIMITS.max_alt_slot_cm:
            raise ValueError(f"takeoff_alt_m must be within {FIELD_LIMITS.min_alt_slot_cm / 100:g}-"
                             f"{FIELD_LIMITS.max_alt_slot_cm / 100:g} m, got {self.takeoff_alt_m!r}")
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
    problems: list[str] = field(default_factory=lambda: ["failsafe parameters not checked yet"])
    checked_at: float | None = None
    #: The return-to-launch altitude read (cm), or None when unreadable.
    rtl_cm: float | None = None


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
        # RETURN_TO_LAUNCH), or None.
        self._requested_landing: str | None = None
        self._params = _ParamCheck()
        self._param_stop = threading.Event()
        self._param_thread: threading.Thread | None = None
        self._slot_cm: Callable[[], float | None] | None = None
        self._connected = False

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def bind_slot_source(self, slot_cm: Callable[[], float | None]) -> None:
        """Give the drone its current altitude slot (for the return-altitude check)."""
        self._slot_cm = slot_cm

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
        """Latch external control when the flight mode is not one the edge set (see the module docstring)."""
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
        with self._lock:
            if self._external is not None:
                return
            if mode not in autonomous_modes(state.autopilot):
                reason = f"flight mode {mode} set outside FlightRisk (RC pilot or ground control station)"
            elif mode in _LANDING_MODES and self._requested_landing is None:
                reason = f"autopilot switched to {mode} by itself (its own failsafe)"
            else:
                return
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

    def read_battery(self) -> int | None:
        """Battery percentage, or None when unknown or stale (an unknown battery never permits a takeoff)."""
        st = self._autopilot_state()
        if not st.fresh(self.options.stale_after_s) or not _number(st.battery_pct):
            return None
        return round(st.battery_pct)

    def read_height_cm(self) -> int | None:
        """Height above home in centimetres, or None when unknown or stale."""
        st = self._autopilot_state()
        if not st.fresh(self.options.stale_after_s) or not _number(st.rel_alt_m):
            return None
        return round(st.rel_alt_m * 100)

    def measured_pose(self) -> RelativePose | None:
        """The autopilot's measured position in the plan frame, or None before the first takeoff."""
        st = self._autopilot_state()
        with self._lock:
            frame, heading = self._frame, self._target.heading_deg
        if frame is None or not st.has_position or not st.fresh(self.options.stale_after_s):
            return None
        north, east = _offset_m(frame.lat_deg, frame.lon_deg, st.lat_deg, st.lon_deg)
        x_m, y_m = _to_plan(north, east, frame.heading_deg)
        yaw = st.heading_deg
        pose_heading = heading if yaw is None else (yaw - frame.heading_deg) % 360.0
        return RelativePose(x_m * 100.0, y_m * 100.0, pose_heading)

    def geo_position(self) -> tuple[float, float] | None:
        """Latitude and longitude in degrees, or None."""
        st = self._autopilot_state()
        if not st.has_position or not st.fresh(self.options.stale_after_s):
            return None
        return float(st.lat_deg), float(st.lon_deg)

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
        with self._lock:
            check = self._params
        params = list(check.problems) if self.options.require_failsafes else []
        if self.options.require_failsafes and check.checked_at is not None:
            params.extend(self._rtl_problems(st, check.rtl_cm))
        return preflight_problems(
            st,
            stale_after_s=self.options.stale_after_s,
            min_satellites=self.options.min_satellites,
            param_problems=params,
        )

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
        """Read the failsafe parameters now; returns (and stores) the problems found."""
        st = self.link.state()
        read = self.link.get_param
        problems = failsafe_problems(st.autopilot, read)
        rtl_cm = rtl_altitude_cm(st.autopilot, read) if self.options.failsafe_action == "rtl" else None
        with self._lock:
            self._params = _ParamCheck(problems=problems, checked_at=self._clock(), rtl_cm=rtl_cm)
        found = problems + self._rtl_problems(st, rtl_cm)
        if found:
            logger.warning("[%s] autopilot failsafes not ready: %s", self.name, "; ".join(found))
        return found

    def _rtl_problems(self, st: AutopilotState, rtl_cm: float | None) -> list[str]:
        """With ``failsafe_action`` rtl: the return altitude must be readable and at or below the live slot."""
        if self.options.failsafe_action != "rtl" or st.autopilot is None:
            return []
        if rtl_cm is None:
            return ["cannot read the return-to-launch altitude"]
        slot = self._slot_cm() if self._slot_cm is not None else None
        if slot is not None and rtl_cm > slot + 1e-6:
            return [(
                f"return-to-launch altitude {rtl_cm / 100:g} m is above this drone's slot {slot / 100:g} m; "
                "a return would climb (set it at or below the slot)"
            )]
        return []

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
            self._interrupt.clear()
        logger.info("[%s] takeoff to %.1f m (frame heading %.0f deg)", self.name, alt_m, heading)
        try:
            self.link.set_speed(self.options.speed_mps)
        except Exception as exc:  # noqa: BLE001 - the autopilot's own cruise speed applies
            logger.warning("[%s] set_speed failed, autopilot default speed applies: %r", self.name, exc)
        self.link.arm()
        if abort is not None and abort.is_set():
            # A landing arrived while arming: do not lift. The land path
            # handles a vehicle on the ground as already landed.
            return CommandResult("takeoff", CommandOutcome.NOT_EXECUTED, "aborted after arming")
        self.link.takeoff(alt_m)
        deadline = self._clock() + self.options.takeoff_timeout_s
        while True:
            st = self._autopilot_state()
            if self._interrupted(abort):
                return CommandResult("takeoff")  # it did take off; the landing takes over
            if self._flying(st) and _number(st.rel_alt_m) and st.rel_alt_m >= alt_m - self.options.arrival_alt_m:
                return CommandResult("takeoff")
            if self._clock() >= deadline:
                raise TimeoutError(f"takeoff did not reach {alt_m:g} m within {self.options.takeoff_timeout_s:g} s")
            self._interrupt.wait(_POLL_S)

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
        self.link.goto(lat, lon, frame.home_abs_alt_m + target.alt_cm / 100.0,
                       (frame.heading_deg + target.heading_deg) % 360.0)

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
        if not st.fresh(self.options.stale_after_s) or not st.has_position:
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

    def land_for_reason(self, reason: str) -> CommandResult:
        """Land, or return to launch, for the executor's landing *reason*. Returns once the autopilot accepted it.

        Raises when the autopilot is not connected or refuses the command,
        so the executor retries. On the ground it succeeds without sending.
        """
        st = self._autopilot_state()
        if not st.connected:
            raise RuntimeError("autopilot not connected; land not sent")
        # Any waiting move or takeoff stops now, whatever happens below.
        self._interrupt.set()
        if not self._flying(st) and st.landed_state not in ("TAKING_OFF",) and not st.armed:
            return CommandResult("land")
        external = self._external_latched()
        if external is not None and reason in AUTOMATIC_LAND_REASONS:
            logger.warning("[%s] %s landing not sent: %s", self.name, reason, external)
            return CommandResult("land", CommandOutcome.OK, "external control; not sent")
        mode = "RETURN_TO_LAUNCH" if (reason in FAILSAFE_REASONS and self.options.failsafe_action == "rtl") else "LAND"
        with self._lock:
            already = self._requested_landing == mode and st.flight_mode == mode
            self._requested_landing = mode
        if already:
            return CommandResult("land", CommandOutcome.OK, f"{mode} already in progress")
        logger.warning("[%s] %s for %s", self.name, "return to launch" if mode != "LAND" else "land", reason)
        if mode == "LAND":
            self.link.land()
        else:
            self.link.return_to_launch()
        return CommandResult("land", CommandOutcome.OK, mode)


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
