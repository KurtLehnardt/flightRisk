"""MAVLink drone controller via MAVSDK-Python.

Wraps the async MAVSDK API behind the sync DroneController protocol
using a background asyncio event loop and run_coroutine_threadsafe.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from typing import Callable

import cv2
import numpy as np

from amber.drone.controller import DroneCapabilities, DroneController, DroneState

logger = logging.getLogger(__name__)

try:
    from mavsdk import System
    from mavsdk.action import ActionError
    from mavsdk.offboard import (
        OffboardError,
        VelocityBodyYawspeed,
    )

    HAS_MAVSDK = True
except ImportError:
    HAS_MAVSDK = False

__all__ = ["MavlinkController", "HAS_MAVSDK"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CMD_TIMEOUT = 30.0  # seconds for sync-over-async calls
_MOVE_SPEED = 1.0  # m/s for move commands
_YAW_RATE = 60.0  # deg/s for rotate commands
_RTSP_RECONNECT_DELAY = 5.0  # seconds before RTSP reconnect attempt
_HEARTBEAT_TIMEOUT = 5.0  # seconds without telemetry -> disconnected
_GPS_ARRIVAL_RADIUS = 2.0  # meters – how close is "arrived"


def _extract_ip(address: str) -> str:
    """Pull an IP/hostname from a MAVSDK address string like 'udp://:14540' or 'serial:///dev/...'."""
    match = re.search(r"://([^:/]+)", address)
    if match and match.group(1):
        return match.group(1)
    return "127.0.0.1"


class MavlinkController:
    """Manages a MAVLink drone via MAVSDK-Python.

    All public methods are synchronous; internally they dispatch
    coroutines onto a dedicated asyncio event loop running in a
    background thread.
    """

    def __init__(
        self,
        name: str,
        host: str = "udp://:14540",
        rtsp_url: str | None = None,
    ):
        if not HAS_MAVSDK:
            raise RuntimeError(
                "mavsdk package is required but not installed. "
                "Install it with: pip install mavsdk"
            )

        self.name = name
        self.host = host
        self.state = DroneState()
        self.capabilities = DroneCapabilities(
            has_gps=True,
            has_rtsp=True,
            min_move_cm=10,
            max_move_cm=50000,
            max_altitude_m=120,
            supports_missions=True,
        )

        host_ip = _extract_ip(host)
        self.rtsp_url = rtsp_url or f"rtsp://{host_ip}:8554/camera"

        # Async internals
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._system: System | None = None
        self._telemetry_tasks: list[asyncio.Task] = []

        # Video internals
        self._cap: cv2.VideoCapture | None = None
        self._frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()
        self._frame_callbacks: list[Callable[[np.ndarray], None]] = []
        self._frame_thread: threading.Thread | None = None
        self._last_frame_time: float = 0.0

        # Thread safety
        self._cmd_lock = threading.Lock()
        self._running = False

        # Telemetry heartbeat
        self._last_telemetry_time: float = 0.0

    # ------------------------------------------------------------------
    # Async event loop management
    # ------------------------------------------------------------------

    def _start_loop(self) -> None:
        """Start the background asyncio event loop."""
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name=f"{self.name}-mavlink-loop",
        )
        self._loop_thread.start()

    def _stop_loop(self) -> None:
        """Stop the background event loop."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5.0)
        if self._loop and not self._loop.is_closed():
            self._loop.close()
        self._loop = None
        self._loop_thread = None

    def _run(self, coro, timeout: float = _CMD_TIMEOUT):
        """Submit *coro* to the event loop and block for the result."""
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("Event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            logger.error("[%s] Command timed out after %.0fs", self.name, timeout)
            raise
        except Exception:
            raise

    # ------------------------------------------------------------------
    # Telemetry subscriptions
    # ------------------------------------------------------------------

    async def _subscribe_position(self, system: System) -> None:
        async for position in system.telemetry.position():
            self.state.latitude = position.latitude_deg
            self.state.longitude = position.longitude_deg
            self.state.altitude_msl = position.absolute_altitude_m
            self._last_telemetry_time = time.monotonic()

    async def _subscribe_heading(self, system: System) -> None:
        async for heading in system.telemetry.heading():
            self.state.heading = int(heading.heading_deg) % 360
            self._last_telemetry_time = time.monotonic()

    async def _subscribe_battery(self, system: System) -> None:
        async for battery in system.telemetry.battery():
            self.state.battery = int(battery.remaining_percent * 100)
            self._last_telemetry_time = time.monotonic()

    async def _subscribe_flight_mode(self, system: System) -> None:
        async for mode in system.telemetry.flight_mode():
            self.state.flight_mode = str(mode)
            self._last_telemetry_time = time.monotonic()
            if "RETURN" in self.state.flight_mode.upper():
                logger.warning("[%s] RTL mode detected", self.name)

    async def _subscribe_gps_info(self, system: System) -> None:
        async for gps_info in system.telemetry.gps_info():
            if gps_info.fix_type.value < 3:
                logger.warning(
                    "[%s] Poor GPS fix: %s", self.name, gps_info.fix_type
                )
            self._last_telemetry_time = time.monotonic()

    async def _monitor_heartbeat(self) -> None:
        """Set is_connected=False if no telemetry arrives for _HEARTBEAT_TIMEOUT seconds."""
        while self._running:
            await asyncio.sleep(1.0)
            if self._last_telemetry_time > 0:
                elapsed = time.monotonic() - self._last_telemetry_time
                if elapsed > _HEARTBEAT_TIMEOUT:
                    if self.state.is_connected:
                        logger.warning(
                            "[%s] Heartbeat lost (%.1fs)", self.name, elapsed
                        )
                        self.state.is_connected = False
                else:
                    self.state.is_connected = True

    # ------------------------------------------------------------------
    # Async command implementations
    # ------------------------------------------------------------------

    async def _connect_async(self) -> bool:
        system = System()
        await system.connect(system_address=self.host)

        logger.info("[%s] Waiting for drone connection...", self.name)

        # Wait until armed-ready and has GPS
        async for health in system.telemetry.health():
            if health.is_armable and health.is_global_position_ok:
                break

        logger.info("[%s] Connected and healthy", self.name)
        self._system = system
        self.state.is_connected = True
        self._last_telemetry_time = time.monotonic()

        # Start telemetry subscriptions
        self._telemetry_tasks = [
            self._loop.create_task(self._subscribe_position(system)),
            self._loop.create_task(self._subscribe_heading(system)),
            self._loop.create_task(self._subscribe_battery(system)),
            self._loop.create_task(self._subscribe_flight_mode(system)),
            self._loop.create_task(self._subscribe_gps_info(system)),
            self._loop.create_task(self._monitor_heartbeat()),
        ]
        return True

    async def _disconnect_async(self) -> None:
        # Cancel telemetry tasks
        for task in self._telemetry_tasks:
            task.cancel()
        self._telemetry_tasks.clear()

        # Land if still flying
        if self.state.is_flying and self._system:
            try:
                await self._system.action.land()
            except Exception as exc:
                logger.error("[%s] Land on disconnect failed: %s", self.name, exc)

        self._system = None
        self.state.is_connected = False
        self.state.is_flying = False

    async def _takeoff_async(self) -> None:
        system = self._system
        if not system:
            raise RuntimeError("Not connected")

        await system.action.arm()
        await system.action.takeoff()
        self.state.is_flying = True
        logger.info("[%s] Takeoff", self.name)

    async def _land_async(self) -> None:
        system = self._system
        if not system:
            raise RuntimeError("Not connected")

        await system.action.land()
        self.state.is_flying = False
        logger.info("[%s] Land", self.name)

    async def _move_async(self, direction: str, distance_cm: int) -> None:
        system = self._system
        if not system:
            raise RuntimeError("Not connected")

        distance_m = distance_cm / 100.0
        duration = distance_m / _MOVE_SPEED

        vx, vy, vz = 0.0, 0.0, 0.0
        direction = direction.lower()
        if direction == "forward":
            vx = _MOVE_SPEED
        elif direction == "back":
            vx = -_MOVE_SPEED
        elif direction == "right":
            vy = _MOVE_SPEED
        elif direction == "left":
            vy = -_MOVE_SPEED
        elif direction == "up":
            vz = -_MOVE_SPEED  # NED: negative Z = up
        elif direction == "down":
            vz = _MOVE_SPEED  # NED: positive Z = down
        else:
            raise ValueError(f"Unknown direction: {direction}")

        setpoint = VelocityBodyYawspeed(vx, vy, vz, 0.0)

        # CRITICAL: send one setpoint BEFORE starting offboard mode
        await system.offboard.set_velocity_body(setpoint)

        try:
            await system.offboard.start()
        except OffboardError as exc:
            logger.warning("[%s] Offboard start failed, retrying: %s", self.name, exc)
            await system.offboard.set_velocity_body(setpoint)
            await system.offboard.start()

        await asyncio.sleep(duration)

        # Stop
        await system.offboard.set_velocity_body(
            VelocityBodyYawspeed(0.0, 0.0, 0.0, 0.0)
        )
        await system.offboard.stop()

    async def _rotate_async(self, degrees: int) -> None:
        system = self._system
        if not system:
            raise RuntimeError("Not connected")

        duration = abs(degrees) / _YAW_RATE
        yaw_rate = _YAW_RATE if degrees > 0 else -_YAW_RATE

        setpoint = VelocityBodyYawspeed(0.0, 0.0, 0.0, yaw_rate)

        # Send setpoint before starting offboard
        await system.offboard.set_velocity_body(setpoint)

        try:
            await system.offboard.start()
        except OffboardError as exc:
            logger.warning("[%s] Offboard start failed, retrying: %s", self.name, exc)
            await system.offboard.set_velocity_body(setpoint)
            await system.offboard.start()

        await asyncio.sleep(duration)

        await system.offboard.set_velocity_body(
            VelocityBodyYawspeed(0.0, 0.0, 0.0, 0.0)
        )
        await system.offboard.stop()

    async def _hover_async(self) -> None:
        system = self._system
        if not system:
            raise RuntimeError("Not connected")

        setpoint = VelocityBodyYawspeed(0.0, 0.0, 0.0, 0.0)
        await system.offboard.set_velocity_body(setpoint)

        try:
            await system.offboard.start()
        except OffboardError:
            await system.offboard.set_velocity_body(setpoint)
            await system.offboard.start()

        await system.offboard.set_velocity_body(setpoint)

    async def _goto_gps_async(self, lat: float, lon: float, alt_m: float) -> None:
        system = self._system
        if not system:
            raise RuntimeError("Not connected")

        heading = self.state.heading or 0
        await system.action.goto_location(lat, lon, alt_m, float(heading))

        # Monitor position until within arrival radius
        async for position in system.telemetry.position():
            dlat = position.latitude_deg - lat
            dlon = position.longitude_deg - lon
            # Rough meter conversion
            dx = dlat * 111_320.0
            dy = dlon * 111_320.0 * math.cos(math.radians(lat))
            dz = position.absolute_altitude_m - alt_m
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            if dist < _GPS_ARRIVAL_RADIUS:
                break

    # ------------------------------------------------------------------
    # Public synchronous API (DroneController protocol)
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Connect to the MAVLink drone and start telemetry."""
        self._running = True
        self._start_loop()
        try:
            return self._run(self._connect_async())
        except Exception as exc:
            logger.error("[%s] Connect failed: %s", self.name, exc)
            self._running = False
            self._stop_loop()
            return False

    def disconnect(self) -> None:
        """Disconnect from the drone, cancel subscriptions, stop event loop."""
        self._running = False
        if self._loop and self._loop.is_running():
            try:
                self._run(self._disconnect_async(), timeout=10.0)
            except Exception as exc:
                logger.error("[%s] Disconnect error: %s", self.name, exc)

        # Stop video capture
        if self._cap is not None:
            self._cap.release()
            self._cap = None

        self._stop_loop()
        self.state.is_connected = False
        self.state.is_flying = False
        logger.info("[%s] Disconnected", self.name)

    def get_frame(self) -> np.ndarray | None:
        """Read a frame from the RTSP stream."""
        if self._cap is None:
            self._cap = cv2.VideoCapture(self.rtsp_url)

        with self._frame_lock:
            ret, frame = self._cap.read()
            if ret and frame is not None:
                self._frame = frame
                self._last_frame_time = time.time()
                return frame

            # RTSP dropout – reconnect after delay
            if self._last_frame_time > 0 and (time.time() - self._last_frame_time > _RTSP_RECONNECT_DELAY):
                logger.warning("[%s] RTSP dropout, reconnecting...", self.name)
                self._cap.release()
                self._cap = cv2.VideoCapture(self.rtsp_url)
                self._last_frame_time = time.time()  # reset to avoid tight loop
            return None

    def on_frame(self, cb: Callable[[np.ndarray], None]) -> None:
        """Register a frame callback and start the reader thread if needed."""
        self._frame_callbacks.append(cb)
        if self._frame_thread is None or not self._frame_thread.is_alive():
            self._frame_thread = threading.Thread(
                target=self._frame_reader_loop,
                daemon=True,
                name=f"{self.name}-frame-reader",
            )
            self._frame_thread.start()

    def _frame_reader_loop(self) -> None:
        """Background thread that reads frames and dispatches to callbacks."""
        while self._running:
            frame = self.get_frame()
            if frame is not None:
                for cb in self._frame_callbacks:
                    try:
                        cb(frame)
                    except Exception as exc:
                        logger.error("[%s] Frame callback error: %s", self.name, exc)
            else:
                time.sleep(0.03)  # ~30fps pacing on empty reads

    def takeoff(self) -> None:
        with self._cmd_lock:
            self._run(self._takeoff_async())

    def land(self) -> None:
        with self._cmd_lock:
            self._run(self._land_async())

    def move(self, direction: str, distance_cm: int) -> None:
        with self._cmd_lock:
            self._run(self._move_async(direction, distance_cm))

    def rotate(self, degrees: int) -> None:
        with self._cmd_lock:
            self._run(self._rotate_async(degrees))

    def hover(self) -> None:
        with self._cmd_lock:
            self._run(self._hover_async())

    def rc_control(self, lr: int, fb: int, ud: int, yaw: int) -> None:
        """Send manual control input. Values -100..100 mapped to -1.0..1.0."""
        system = self._system
        if not system:
            raise RuntimeError("Not connected")

        def _norm(v: int) -> float:
            return max(-1.0, min(1.0, v / 100.0))

        self._run(
            system.manual_control.set_manual_control_input(
                _norm(fb),  # pitch (forward/back)
                _norm(lr),  # roll (left/right)
                _norm(ud),  # thrust (up/down) -- note: 0 = -1.0 mapped, but MAVSDK expects 0..1 for throttle
                _norm(yaw),  # yaw
            )
        )

    def goto_gps(self, lat: float, lon: float, alt_m: float) -> None:
        with self._cmd_lock:
            self._run(self._goto_gps_async(lat, lon, alt_m))
