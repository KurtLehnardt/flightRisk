"""A simulated autopilot behind MAVSDK-Python's asyncio API, for tests without SITL.

``SimVehicle`` is a small kinematic model of a PX4 or ArduPilot multirotor:
arming checks, takeoff, reposition targets (``goto_location``), land, return
to launch, offboard body velocity, flight modes, battery drain, GPS fix and
parameters. ``fake_system_class(vehicle)`` returns a class with the
``mavsdk.System`` surface that ``flightrisk.drone.mavsdk_link`` and
``flightrisk.drone.mavlink.MavlinkController`` use, bound to that vehicle, so a
test monkeypatches ``System`` and drives the real link code.

This is simulation-free in the SITL sense: no autopilot firmware runs. It
checks the edge's logic (commands, waits, mode handling, failsafe mapping),
not how a real PX4 or ArduPilot behaves. ``time_scale`` speeds the model up
so a 30 m lane takes a fraction of a second.

Test hooks: ``set_pilot_mode`` (an RC pilot flips the mode switch),
``trigger_failsafe`` (the autopilot's own RTL or LAND), ``cut_link`` and
``restore_link`` (telemetry stops), ``wind_drift_m`` (pushes the vehicle).
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace
from typing import Any

EARTH_RADIUS_M = 6_371_008.8

ARDUPILOT_PARAMS = {
    "FENCE_ENABLE": 1, "FENCE_ACTION": 1, "BATT_FS_LOW_ACT": 2, "BATT_LOW_VOLT": 10.5, "BATT_LOW_MAH": 0,
    "FS_THR_ENABLE": 1, "RTL_ALT": 1500, "SYSID_THISMAV": 1,
}
PX4_PARAMS = {
    "GF_ACTION": 2, "GF_MAX_HOR_DIST": 100.0, "GF_MAX_VER_DIST": 40.0, "COM_LOW_BAT_ACT": 3, "NAV_RCL_ACT": 2,
    "NAV_DLL_ACT": 2, "RTL_RETURN_ALT": 15.0, "MAV_SYS_ID": 1,
}
#: Parameters stored as floats (get_param_int refuses them, as MAVSDK does).
_FLOAT_PARAMS = {"BATT_LOW_VOLT", "GF_MAX_HOR_DIST", "GF_MAX_VER_DIST", "RTL_RETURN_ALT"}


class FlightMode(Enum):
    UNKNOWN = 0
    READY = 1
    TAKEOFF = 2
    HOLD = 3
    MISSION = 4
    RETURN_TO_LAUNCH = 5
    LAND = 6
    OFFBOARD = 7
    FOLLOW_ME = 8
    MANUAL = 9
    ALTCTL = 10
    POSCTL = 11
    ACRO = 12
    STABILIZED = 13


class LandedState(Enum):
    UNKNOWN = 0
    ON_GROUND = 1
    IN_AIR = 2
    TAKING_OFF = 3
    LANDING = 4


class FixType(Enum):
    NO_GPS = 0
    NO_FIX = 1
    FIX_2D = 2
    FIX_3D = 3
    FIX_DGPS = 4
    RTK_FLOAT = 5
    RTK_FIXED = 6


class SimActionError(RuntimeError):
    """Stands in for ``mavsdk.action.ActionError``."""


@dataclass
class _Goal:
    north: float
    east: float
    alt: float


class SimVehicle:
    """Kinematic multirotor with PX4- or ArduPilot-like modes. Thread-safe; advanced by ``step``."""

    def __init__(
        self,
        autopilot: str = "ardupilot",
        *,
        home: tuple[float, float, float] = (40.6894, -111.7310, 1600.0),
        heading_deg: float = 0.0,
        time_scale: float = 10.0,
        speed_mps: float = 5.0,
        climb_mps: float = 2.0,
        battery_pct: float = 95.0,
        drain_pct_per_s: float = 0.0,
        satellites: int = 12,
        fix: FixType = FixType.FIX_3D,
        params: dict[str, float] | None = None,
        reports_in_air: bool = True,
    ):
        self.autopilot = autopilot
        self.home_lat, self.home_lon, self.home_alt = home
        self.time_scale = float(time_scale)
        self.max_speed = float(speed_mps)
        self.climb = float(climb_mps)
        self.lock = threading.RLock()
        self.north = 0.0
        self.east = 0.0
        self.alt = 0.0
        self.heading = float(heading_deg)
        self.armed = False
        self.in_air = False
        self.landed = LandedState.ON_GROUND
        self.mode = FlightMode.HOLD if autopilot == "px4" else FlightMode.STABILIZED
        self.battery = float(battery_pct)
        self.drain = float(drain_pct_per_s)
        self.satellites = int(satellites)
        self.fix = fix
        self.params = dict(params if params is not None else (PX4_PARAMS if autopilot == "px4" else ARDUPILOT_PARAMS))
        self.goal: _Goal | None = None
        self.goal_yaw: float | None = None
        self.velocity_body = (0.0, 0.0, 0.0, 0.0)
        self.speed = self.max_speed
        self.takeoff_alt = 2.5
        self.linked = True
        self.reports_in_air = reports_in_air
        self.commands: list[tuple] = []
        self.armable = True
        self.refuse_takeoffs = 0

    # --- geometry -------------------------------------------------------

    def latlon(self) -> tuple[float, float]:
        lat = self.home_lat + math.degrees(self.north / EARTH_RADIUS_M)
        lon = self.home_lon + math.degrees(self.east / (EARTH_RADIUS_M * math.cos(math.radians(self.home_lat))))
        return lat, lon

    def ned_of(self, lat: float, lon: float) -> tuple[float, float]:
        north = math.radians(lat - self.home_lat) * EARTH_RADIUS_M
        east = math.radians(lon - self.home_lon) * EARTH_RADIUS_M * math.cos(math.radians(self.home_lat))
        return north, east

    # --- test hooks -----------------------------------------------------

    def set_pilot_mode(self, mode: str = "POSCTL") -> None:
        """An RC pilot flips the mode switch: autonomous targets are dropped, the vehicle holds."""
        with self.lock:
            self.mode = FlightMode[mode]
            self.goal = None
            self.velocity_body = (0.0, 0.0, 0.0, 0.0)

    def trigger_failsafe(self, mode: str = "RETURN_TO_LAUNCH") -> None:
        """The autopilot's own failsafe switches mode by itself."""
        with self.lock:
            self._enter(FlightMode[mode])

    def cut_link(self) -> None:
        with self.lock:
            self.linked = False

    def restore_link(self) -> None:
        with self.lock:
            self.linked = True

    def wind_drift_m(self, north: float, east: float) -> None:
        with self.lock:
            self.north += north
            self.east += east

    # --- commands -------------------------------------------------------

    def _record(self, *cmd: Any) -> None:
        self.commands.append(cmd)

    def _guided(self) -> FlightMode:
        return FlightMode.OFFBOARD if self.autopilot == "ardupilot" else FlightMode.HOLD

    def arm(self) -> None:
        with self.lock:
            self._record("arm")
            if not self.armable or self.fix.value < 3:
                raise SimActionError("COMMAND_DENIED: prearm checks failed")
            self.armed = True
            if self.autopilot == "ardupilot":
                self.mode = FlightMode.OFFBOARD

    def takeoff(self) -> None:
        with self.lock:
            self._record("takeoff", self.takeoff_alt)
            if self.refuse_takeoffs > 0:
                # ArduCopter in SITL refuses takeoffs for a while ("GUIDED not ready").
                self.refuse_takeoffs -= 1
                raise SimActionError("COMMAND_DENIED: GUIDED not ready")
            if not self.armed:
                raise SimActionError("COMMAND_DENIED: not armed")
            self.mode = FlightMode.TAKEOFF if self.autopilot == "px4" else FlightMode.OFFBOARD
            self.landed = LandedState.TAKING_OFF
            self.goal = _Goal(self.north, self.east, self.takeoff_alt)

    def goto(self, lat: float, lon: float, abs_alt: float, yaw: float) -> None:
        with self.lock:
            self._record("goto", lat, lon, abs_alt, yaw)
            if not self.in_air:
                raise SimActionError("COMMAND_DENIED: not flying")
            north, east = self.ned_of(lat, lon)
            self.goal = _Goal(north, east, abs_alt - self.home_alt)
            self.goal_yaw = yaw if math.isfinite(yaw) else None
            self.mode = self._guided()

    def land(self) -> None:
        with self.lock:
            self._record("land")
            self._enter(FlightMode.LAND)

    def return_to_launch(self) -> None:
        with self.lock:
            self._record("return_to_launch")
            self._enter(FlightMode.RETURN_TO_LAUNCH)

    def _enter(self, mode: FlightMode) -> None:
        self.mode = mode
        self.velocity_body = (0.0, 0.0, 0.0, 0.0)
        if mode is FlightMode.LAND:
            self.goal = _Goal(self.north, self.east, 0.0)
            if self.in_air:
                self.landed = LandedState.LANDING
        elif mode is FlightMode.RETURN_TO_LAUNCH:
            rtl = self.params.get("RTL_ALT", 1500) / 100.0 if self.autopilot == "ardupilot" else self.params.get(
                "RTL_RETURN_ALT", 15.0)
            climb_to = self.alt if rtl <= 0 else max(self.alt, rtl)
            self.goal = _Goal(self.north, self.east, climb_to)
            self._rtl_phase = "climb"

    def set_offboard_velocity(self, forward: float, right: float, down: float, yaw_rate: float) -> None:
        with self.lock:
            self.velocity_body = (forward, right, down, yaw_rate)

    # --- physics --------------------------------------------------------

    def step(self, dt: float) -> None:
        """Advance the model by *dt* real seconds (times ``time_scale``)."""
        dt *= self.time_scale
        with self.lock:
            self.battery = max(0.0, self.battery - self.drain * dt)
            if self.mode is FlightMode.OFFBOARD and any(self.velocity_body) and self.in_air:
                fwd, right, down, yaw_rate = self.velocity_body
                h = math.radians(self.heading)
                self.north += (fwd * math.cos(h) - right * math.sin(h)) * dt
                self.east += (fwd * math.sin(h) + right * math.cos(h)) * dt
                self.alt = max(0.0, self.alt - down * dt)
                self.heading = (self.heading + yaw_rate * dt) % 360.0
                return
            goal = self.goal
            if goal is None:
                return
            if self.mode is FlightMode.RETURN_TO_LAUNCH and getattr(self, "_rtl_phase", "") == "climb" \
                    and abs(self.alt - goal.alt) < 0.05:
                self._rtl_phase = "home"
                self.goal = goal = _Goal(0.0, 0.0, self.alt)
            elif self.mode is FlightMode.RETURN_TO_LAUNCH and getattr(self, "_rtl_phase", "") == "home" \
                    and math.hypot(self.north, self.east) < 0.05:
                self._rtl_phase = "land"
                self.goal = goal = _Goal(0.0, 0.0, 0.0)
                self.landed = LandedState.LANDING
            dn, de = goal.north - self.north, goal.east - self.east
            dist = math.hypot(dn, de)
            step = min(dist, self.speed * dt)
            if dist > 1e-9:
                self.north += dn / dist * step
                self.east += de / dist * step
            dz = goal.alt - self.alt
            self.alt += max(-self.climb * dt, min(self.climb * dt, dz))
            if self.goal_yaw is not None:
                err = (self.goal_yaw - self.heading + 180.0) % 360.0 - 180.0
                turn = max(-90.0 * dt, min(90.0 * dt, err))
                self.heading = (self.heading + turn) % 360.0
            if self.alt > 0.2 and self.armed:
                self.in_air = True
                if self.landed is LandedState.TAKING_OFF and self.alt >= self.takeoff_alt - 0.1:
                    self.landed = LandedState.IN_AIR
                    if self.autopilot == "px4":
                        self.mode = FlightMode.HOLD
                elif self.landed is LandedState.ON_GROUND:
                    self.landed = LandedState.IN_AIR
            if self.in_air and self.alt <= 0.0 and goal.alt <= 0.0:
                self.in_air = False
                self.armed = False
                self.landed = LandedState.ON_GROUND
                self.goal = None
                if self.autopilot == "px4":
                    self.mode = FlightMode.HOLD

    def snapshot(self) -> SimpleNamespace:
        with self.lock:
            lat, lon = self.latlon()
            return SimpleNamespace(
                lat=lat, lon=lon, abs_alt=self.home_alt + self.alt, rel_alt=self.alt, heading=self.heading,
                armed=self.armed, in_air=self.in_air if self.reports_in_air else None, landed=self.landed,
                mode=self.mode, battery=self.battery, sats=self.satellites, fix=self.fix,
                gpos_ok=self.fix.value >= 3, home_ok=self.fix.value >= 3, armable=self.armable and self.fix.value >= 3,
                linked=self.linked,
            )


# ---------------------------------------------------------------------------
# The MAVSDK surface
# ---------------------------------------------------------------------------

_STREAM_PERIOD_S = 0.02


class _Plugin(SimpleNamespace):
    pass


def fake_system_class(vehicle: SimVehicle, *, physics_period_s: float = 0.01) -> type:
    """A ``mavsdk.System`` stand-in bound to *vehicle* (one vehicle per class)."""

    class FakeSystem:
        def __init__(self, mavsdk_server_address: str | None = None, port: int = 50051, **_: Any):
            self.vehicle = vehicle
            self._physics: asyncio.Task | None = None
            v = vehicle

            async def _stream(make):
                while True:
                    snap = v.snapshot()
                    if snap.linked:
                        yield make(snap)
                    await asyncio.sleep(_STREAM_PERIOD_S)

            def stream(make):
                return lambda: _stream(make)

            async def _noop(*args, **kwargs):
                return None

            self.core = _Plugin(connection_state=stream(lambda s: SimpleNamespace(is_connected=True)))
            self.telemetry = _Plugin(
                position=stream(lambda s: SimpleNamespace(latitude_deg=s.lat, longitude_deg=s.lon,
                                                          absolute_altitude_m=s.abs_alt, relative_altitude_m=s.rel_alt)),
                in_air=self._in_air_stream,
                landed_state=stream(lambda s: s.landed),
                armed=stream(lambda s: s.armed),
                flight_mode=stream(lambda s: s.mode),
                health=stream(lambda s: SimpleNamespace(
                    is_gyrometer_calibration_ok=True, is_accelerometer_calibration_ok=True,
                    is_magnetometer_calibration_ok=True, is_local_position_ok=s.gpos_ok,
                    is_global_position_ok=s.gpos_ok, is_home_position_ok=s.home_ok, is_armable=s.armable)),
                home=stream(lambda s: SimpleNamespace(latitude_deg=v.home_lat, longitude_deg=v.home_lon,
                                                      absolute_altitude_m=v.home_alt, relative_altitude_m=0.0)),
                battery=stream(lambda s: SimpleNamespace(remaining_percent=s.battery)),
                gps_info=stream(lambda s: SimpleNamespace(num_satellites=s.sats, fix_type=s.fix)),
                heading=stream(lambda s: SimpleNamespace(heading_deg=s.heading)),
                **{f"set_rate_{n}": _noop for n in ("position", "in_air", "landed_state", "battery", "gps_info",
                                                     "health")},
            )

            async def _set_takeoff_altitude(alt):
                v.takeoff_alt = float(alt)

            async def _set_current_speed(speed):
                v.speed = min(float(speed), v.max_speed)

            async def _goto(lat, lon, alt, yaw):
                v.goto(lat, lon, alt, yaw)

            def _sync(fn):
                async def _call(*args):
                    fn(*args)
                return _call

            self.action = _Plugin(
                arm=_sync(v.arm), takeoff=_sync(v.takeoff), land=_sync(v.land),
                return_to_launch=_sync(v.return_to_launch), goto_location=_goto,
                set_takeoff_altitude=_set_takeoff_altitude, set_current_speed=_set_current_speed,
            )

            async def _get_int(name):
                if name not in v.params or name in _FLOAT_PARAMS:
                    raise RuntimeError(f"param {name}: does not exist or wrong type")
                return int(v.params[name])

            async def _get_float(name):
                if name not in v.params or name not in _FLOAT_PARAMS:
                    raise RuntimeError(f"param {name}: does not exist or wrong type")
                return float(v.params[name])

            async def _set_int(name, value):
                v.params[name] = int(value)

            async def _set_float(name, value):
                _FLOAT_PARAMS.add(name)
                v.params[name] = float(value)

            self.param = _Plugin(get_param_int=_get_int, get_param_float=_get_float, set_param_int=_set_int,
                                 set_param_float=_set_float)

            # Offboard and manual control (the dashboard MavlinkController's surface).
            async def _set_velocity_body(sp):
                args = getattr(sp, "args", None) or (sp.forward_m_s, sp.right_m_s, sp.down_m_s,
                                                     sp.yawspeed_deg_s)
                v.set_offboard_velocity(*args)

            async def _offboard_start():
                with v.lock:
                    v.mode = FlightMode.OFFBOARD

            async def _offboard_stop():
                with v.lock:
                    v.velocity_body = (0.0, 0.0, 0.0, 0.0)
                    v.mode = v._guided() if v.autopilot == "px4" else FlightMode.OFFBOARD

            self.offboard = _Plugin(set_velocity_body=_set_velocity_body, start=_offboard_start, stop=_offboard_stop)
            async def _manual_input(x, y, z, r):
                v.commands.append(("manual_control", x, y, z, r))

            self.manual_control = _Plugin(set_manual_control_input=_manual_input)

        async def _in_air_stream_gen(self):
            while True:
                snap = self.vehicle.snapshot()
                if snap.linked and snap.in_air is not None:
                    yield snap.in_air
                await asyncio.sleep(_STREAM_PERIOD_S)

        def _in_air_stream(self):
            return self._in_air_stream_gen()

        async def connect(self, system_address: str | None = None) -> None:
            self.address = system_address
            loop = asyncio.get_running_loop()

            async def _physics():
                last = time.monotonic()
                while True:
                    await asyncio.sleep(physics_period_s)
                    now = time.monotonic()
                    vehicle.step(now - last)
                    last = now

            self._physics = loop.create_task(_physics())

    return FakeSystem


def install(monkeypatch: Any, vehicle: SimVehicle) -> type:
    """Point ``flightrisk.drone.mavsdk_link.System`` at a fake bound to *vehicle*; returns the class."""
    import flightrisk.drone.mavsdk_link as link_mod

    cls = fake_system_class(vehicle)
    monkeypatch.setattr(link_mod, "System", cls)
    return cls
