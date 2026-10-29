"""``AutopilotLink`` over MAVSDK-Python: one PX4 or ArduPilot autopilot via MAVLink.

The link runs MAVSDK's asyncio API on a background event loop
(``flightrisk.async_bridge.AsyncBridge``) and exposes blocking calls. Telemetry
streams (position, in-air, landed state, armed, flight mode, health, home,
battery, GPS info, heading) are subscribed once at ``connect()`` and cached;
``state()`` returns the cache as one ``AutopilotState`` and never blocks on
the network.

Addresses are MAVSDK connection URLs, for example ``udpin://0.0.0.0:14540``
(listen for an autopilot or a MAVLink router sending UDP to this port),
``tcpout://127.0.0.1:5760`` (connect to a TCP server such as ArduPilot SITL),
or ``serial:///dev/ttyAMA0:921600`` (a Pixhawk TELEM port wired to a
Raspberry Pi's UART). MAVSDK 2 and later dropped the older ``udp://`` form
for listening; use ``udpin://``.

Battery: MAVSDK 2 and later report ``remaining_percent`` in percent
(0 to 100); this link passes it through.

Autopilot type: probed from parameters that exist on only one of them
(``FENCE_ENABLE`` on ArduPilot, ``GF_ACTION`` on PX4) unless given.
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from typing import Any

from flightrisk.async_bridge import AsyncBridge
from flightrisk.drone.autopilot import ARDUPILOT, AUTOPILOTS, PX4, AutopilotState

try:  # MAVSDK is optional: only an edge that flies an autopilot needs it.
    from mavsdk import System
except ImportError:  # pragma: no cover - exercised only where mavsdk is absent
    System = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = ["MavsdkAutopilotLink", "System"]

#: How long connect() waits for the autopilot's first heartbeat.
DEFAULT_CONNECT_TIMEOUT_S = 30.0
#: Timeout of one command (arm, takeoff, goto, land, return, parameter read).
DEFAULT_COMMAND_TIMEOUT_S = 10.0
#: Telemetry rate requested for the streams the edge watches, in Hz.
_TELEMETRY_RATE_HZ = 10.0
_PROBE_PARAMS = ((ARDUPILOT, "FENCE_ENABLE"), (PX4, "GF_ACTION"))


class MavsdkAutopilotLink:
    """Blocking ``AutopilotLink`` over MAVSDK-Python (see the module docstring)."""

    def __init__(
        self,
        address: str,
        *,
        autopilot: str | None = None,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
        mavsdk_server_address: str | None = None,
        mavsdk_server_port: int = 50051,
    ):
        if autopilot is not None and autopilot not in AUTOPILOTS:
            raise ValueError(f"autopilot must be one of {AUTOPILOTS} or None, got {autopilot!r}")
        self.address = address
        self._configured_autopilot = autopilot
        self._connect_timeout_s = float(connect_timeout_s)
        self._command_timeout_s = float(command_timeout_s)
        self._server_address = mavsdk_server_address
        self._server_port = int(mavsdk_server_port)
        self._bridge = AsyncBridge(name="mavsdk-link", cmd_timeout=self._command_timeout_s)
        self._system: Any = None
        self._tasks: list[asyncio.Task] = []
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {}
        self._last_message_s: float | None = None
        self._connected = False
        self._autopilot: str | None = autopilot

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Connect, subscribe to telemetry and detect the autopilot. Raises on failure."""
        if System is None:
            raise RuntimeError("mavsdk is not installed; install it with: pip install mavsdk")
        if self._connected:
            return
        self._bridge.start_loop()
        try:
            self._bridge.run(self._connect_async(), timeout=self._connect_timeout_s + 5.0)
        except BaseException:
            self._teardown()
            raise
        if self._autopilot is None:
            self._autopilot = self._probe_autopilot()
        logger.info("mavsdk link connected: address=%s autopilot=%s", self.address, self._autopilot)

    def disconnect(self) -> None:
        """Stop the telemetry subscriptions and the event loop. Idempotent; sends no command."""
        if self._bridge.is_running:
            try:
                self._bridge.run(self._cancel_tasks(), timeout=5.0)
            except Exception as exc:  # noqa: BLE001 - teardown continues
                logger.warning("mavsdk link: cancelling telemetry failed: %r", exc)
        self._teardown()

    def _teardown(self) -> None:
        with self._lock:
            self._connected = False
        self._system = None
        self._bridge.stop_loop()

    async def _connect_async(self) -> None:
        kwargs: dict[str, Any] = {}
        if self._server_address is not None:
            kwargs = {"mavsdk_server_address": self._server_address, "port": self._server_port}
        system = System(**kwargs)
        await system.connect(system_address=self.address)

        async def _first_heartbeat() -> None:
            async for state in system.core.connection_state():
                if state.is_connected:
                    return

        await asyncio.wait_for(_first_heartbeat(), timeout=self._connect_timeout_s)
        self._system = system
        with self._lock:
            self._connected = True
            self._last_message_s = time.monotonic()
        await self._set_rates(system)
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._watch(system.telemetry.position(), self._on_position)),
            loop.create_task(self._watch(system.telemetry.in_air(), self._on_value("in_air", bool))),
            loop.create_task(self._watch(system.telemetry.landed_state(), self._on_enum("landed_state"))),
            loop.create_task(self._watch(system.telemetry.armed(), self._on_value("armed", bool))),
            loop.create_task(self._watch(system.telemetry.flight_mode(), self._on_enum("flight_mode"))),
            loop.create_task(self._watch(system.telemetry.health(), self._on_health)),
            loop.create_task(self._watch(system.telemetry.home(), self._on_home)),
            loop.create_task(self._watch(system.telemetry.battery(), self._on_battery)),
            loop.create_task(self._watch(system.telemetry.gps_info(), self._on_gps_info)),
            loop.create_task(self._watch(system.telemetry.heading(), self._on_heading)),
            loop.create_task(self._watch(system.core.connection_state(), self._on_connection)),
        ]

    async def _set_rates(self, system: Any) -> None:
        """Ask for the streams the edge watches at ``_TELEMETRY_RATE_HZ``; best effort."""
        for name in ("set_rate_position", "set_rate_in_air", "set_rate_landed_state", "set_rate_battery",
                     "set_rate_gps_info", "set_rate_health"):
            setter = getattr(system.telemetry, name, None)
            if setter is None:
                continue
            try:
                await asyncio.wait_for(setter(_TELEMETRY_RATE_HZ), timeout=2.0)
            except Exception as exc:  # noqa: BLE001 - some autopilots refuse some rates
                logger.debug("mavsdk link: %s failed: %r", name, exc)

    async def _cancel_tasks(self) -> None:
        """Cancel the telemetry subscriptions and anything else still running on this link's loop."""
        current = asyncio.current_task()
        tasks = [t for t in {*self._tasks, *asyncio.all_tasks()} if t is not current and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks = []

    async def _watch(self, stream: Any, handler: Any) -> None:
        try:
            async for item in stream:
                handler(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one broken stream must not stop the others
            logger.warning("mavsdk link: telemetry stream ended: %r", exc)

    # ------------------------------------------------------------------
    # Telemetry handlers (event-loop thread)
    # ------------------------------------------------------------------

    def _store(self, **values: Any) -> None:
        with self._lock:
            self._values.update(values)
            self._last_message_s = time.monotonic()

    def _on_value(self, key: str, cast: Any):
        return lambda item: self._store(**{key: cast(item)})

    def _on_enum(self, key: str):
        return lambda item: self._store(**{key: getattr(item, "name", str(item))})

    def _on_position(self, p: Any) -> None:
        self._store(
            lat_deg=p.latitude_deg, lon_deg=p.longitude_deg,
            abs_alt_m=p.absolute_altitude_m, rel_alt_m=p.relative_altitude_m,
        )

    def _on_home(self, p: Any) -> None:
        self._store(home_lat_deg=p.latitude_deg, home_lon_deg=p.longitude_deg, home_abs_alt_m=p.absolute_altitude_m)

    def _on_health(self, h: Any) -> None:
        self._store(
            global_position_ok=bool(h.is_global_position_ok),
            home_position_ok=bool(h.is_home_position_ok),
            armable=bool(h.is_armable),
        )

    def _on_battery(self, b: Any) -> None:
        pct = b.remaining_percent
        self._store(battery_pct=float(pct) if isinstance(pct, (int, float)) and math.isfinite(pct) else None)

    def _on_gps_info(self, g: Any) -> None:
        fix = getattr(g.fix_type, "value", g.fix_type)
        self._store(gps_fix_type=int(fix), satellites=int(g.num_satellites))

    def _on_heading(self, h: Any) -> None:
        self._store(heading_deg=float(h.heading_deg) % 360.0)

    def _on_connection(self, c: Any) -> None:
        with self._lock:
            self._connected = bool(c.is_connected)
            if c.is_connected:
                self._last_message_s = time.monotonic()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def state(self) -> AutopilotState:
        """The cached telemetry as one consistent ``AutopilotState``. Never blocks on the network."""
        with self._lock:
            values = dict(self._values)
            last = self._last_message_s
            connected = self._connected
        age = None if last is None else max(0.0, time.monotonic() - last)
        return AutopilotState(connected=connected, telemetry_age_s=age, autopilot=self._autopilot, **values)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _call(self, make_coro: Any, timeout: float | None = None) -> Any:
        system = self._system
        if system is None:
            raise RuntimeError("autopilot not connected")
        return self._bridge.run(make_coro(system), timeout=self._command_timeout_s if timeout is None else timeout)

    def arm(self) -> None:
        self._call(lambda s: s.action.arm())

    def takeoff(self, altitude_m: float) -> None:
        async def _takeoff(s: Any) -> None:
            await s.action.set_takeoff_altitude(float(altitude_m))
            await s.action.takeoff()

        self._call(_takeoff)

    def goto(self, lat_deg: float, lon_deg: float, abs_alt_m: float, yaw_deg: float) -> None:
        self._call(lambda s: s.action.goto_location(float(lat_deg), float(lon_deg), float(abs_alt_m), float(yaw_deg)))

    def land(self) -> None:
        self._call(lambda s: s.action.land())

    def return_to_launch(self) -> None:
        self._call(lambda s: s.action.return_to_launch())

    def set_speed(self, speed_mps: float) -> None:
        self._call(lambda s: s.action.set_current_speed(float(speed_mps)))

    def get_param(self, name: str) -> float | None:
        """A parameter's value (integer or float), or None when it does not exist or cannot be read."""
        for getter in ("get_param_int", "get_param_float"):
            try:
                value = self._call(lambda s, g=getter: getattr(s.param, g)(name))
            except Exception as exc:  # noqa: BLE001 - wrong type, missing, or timeout: try the other, then None
                logger.debug("mavsdk link: %s(%s) failed: %r", getter, name, exc)
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                return float(value)
        return None

    def set_param(self, name: str, value: float) -> None:
        """Set an integer or float parameter (used by SITL setup; the edge never changes parameters)."""
        if float(value).is_integer():
            try:
                self._call(lambda s: s.param.set_param_int(name, int(value)))
                return
            except Exception as exc:  # noqa: BLE001 - a float parameter: retried below
                logger.debug("mavsdk link: set_param_int(%s) failed, trying float: %r", name, exc)
        self._call(lambda s: s.param.set_param_float(name, float(value)))

    def _probe_autopilot(self) -> str | None:
        for autopilot, name in _PROBE_PARAMS:
            if self.get_param(name) is not None:
                return autopilot
        logger.warning("mavsdk link: could not tell PX4 from ArduPilot (neither FENCE_ENABLE nor GF_ACTION read)")
        return None
