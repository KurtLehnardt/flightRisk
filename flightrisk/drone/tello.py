"""Tello drone controller wrapper around DJITelloPy.

Handles connection, video streaming, flight commands, and keepalive.
Standard Tello (TLW004) — SDK 1.3, WiFi AP mode only.
"""

import logging
import math
import threading
import time
from typing import Callable

import numpy as np
from djitellopy import Tello

from flightrisk.config import get_config
from flightrisk.drone.controller import (
    BASIC_FLIGHT,
    CommandOutcome,
    CommandResult,
    DroneCapabilities,
    DroneController,
    DroneState,
)
from flightrisk.drone.snapshot import (
    CELSIUS,
    DEGREE,
    IS_FLYING_COMMANDED,
    METRE,
    METRE_PER_SECOND,
    PERCENT,
    SECOND,
    Reading,
    TelemetrySnapshot,
    reading,
)

# Backward-compatible re-export so `from flightrisk.drone.tello import DroneState` still works
__all__ = ["TelloController", "DroneState", "DroneCapabilities", "DroneController"]

logger = logging.getLogger(__name__)

#: Extra state-packet fields read on each existing poll. djitellopy serves
#: these from the cached UDP state packet, so reading them sends nothing to
#: the drone. (field, djitellopy getter, scale to SI, unit, source label)
#: Units: vgx/vgy/vgz are decimetres per second, tof and baro (as returned
#: by djitellopy) centimetres, attitude degrees. The speed unit follows the
#: Tello SDK community documentation and has not been checked on hardware.
#: Wi-Fi SNR is deliberately not read: it needs a ``wifi?`` query on the
#: command channel, which would compete with flight commands.
_EXTENDED_FIELDS: tuple[tuple[str, str, float, str, str], ...] = (
    ("vx_mps", "get_speed_x", 0.1, METRE_PER_SECOND, "tello.vgx"),
    ("vy_mps", "get_speed_y", 0.1, METRE_PER_SECOND, "tello.vgy"),
    ("vz_mps", "get_speed_z", 0.1, METRE_PER_SECOND, "tello.vgz"),
    ("tof_m", "get_distance_tof", 0.01, METRE, "tello.tof"),
    ("baro_altitude_m", "get_barometer", 0.01, METRE, "tello.baro"),
    ("pitch_deg", "get_pitch", 1.0, DEGREE, "tello.pitch"),
    ("roll_deg", "get_roll", 1.0, DEGREE, "tello.roll"),
    ("yaw_deg", "get_yaw", 1.0, DEGREE, "tello.yaw"),
)


class TelloController:
    """Manages a single Tello drone connection and video stream."""

    def __init__(self, name: str = "drone", host: str | None = None):
        if host is None:
            host = get_config().drone.tello_default_host
        self.name = name
        self.host = host
        self.tello = Tello(host=host)
        self.state = DroneState()
        self.capabilities = DroneCapabilities(
            has_gps=False,
            has_rtsp=False,
            min_move_cm=20,
            max_move_cm=500,
            max_altitude_m=10,
            supports_missions=False,
            features=BASIC_FLIGHT,
        )
        self._frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()
        self._cmd_lock = threading.Lock()
        self._keepalive_thread: threading.Thread | None = None
        self._state_thread: threading.Thread | None = None
        self._running = False
        self._frame_callbacks: list[Callable[[np.ndarray], None]] = []
        self._last_frame_time: float = 0
        self._stream_recovering = False
        self._frozen_count: int = 0
        self._last_frame_hash: int = 0
        # Telemetry bookkeeping for snapshot(): when the battery was last read
        # (connect or poll), when the last state poll succeeded, and the extra
        # state-packet readings from that poll.
        self._battery_at: float | None = None
        self._last_poll_at: float | None = None
        self._extended: dict[str, Reading] = {}
        self._poll_failures = 0

    def connect(self) -> bool:
        """Connect to the Tello and start video stream."""
        try:
            self.tello.connect()
            self.state.battery = self.tello.get_battery()
            self._battery_at = time.time()
            self.state.is_connected = True
            logger.info("tello_connected drone_id=%s battery_pct=%s", self.name, self.state.battery)

            self.tello.streamon()
            self._running = True
            self._start_keepalive()
            self._start_state_polling()
            return True
        except Exception as e:
            logger.warning("tello_connect_failed drone_id=%s error_type=%s", self.name, type(e).__name__)
            return False

    def disconnect(self):
        """Stop streams, disconnect. Landing delegated to Tello.end()."""
        self._running = False
        with self._cmd_lock:
            try:
                self.tello.end()
            except Exception:
                pass
        self.state.is_connected = False
        self.state.is_flying = False
        logger.info("tello_disconnected drone_id=%s", self.name)

    def get_frame(self) -> np.ndarray | None:
        """Get the latest video frame as a BGR numpy array."""
        if not self.state.is_connected:
            return None
        try:
            frame_read = self.tello.get_frame_read()
            frame = frame_read.frame
            if frame is not None:
                # Lightweight frozen frame detection using center pixel hash
                h, w = frame.shape[:2]
                cx, cy = w // 2, h // 2
                sample = frame[max(0,cy-5):cy+5, max(0,cx-5):cx+5].tobytes()
                frame_hash = hash(sample)
                if frame_hash == getattr(self, '_last_frame_hash', None):
                    self._frozen_count = getattr(self, '_frozen_count', 0) + 1
                else:
                    self._frozen_count = 0
                self._last_frame_hash = frame_hash

                if self._frozen_count > 100:
                    if not self._stream_recovering:
                        logger.warning(
                            "tello_frozen_frame drone_id=%s identical_frames=%d", self.name, self._frozen_count
                        )
                        self._recover_stream()
                    return None

                self._last_frame_time = time.time()
                with self._frame_lock:
                    self._frame = frame.copy()
                for cb in self._frame_callbacks:
                    cb(frame)
                return frame
            if (self._last_frame_time > 0
                    and time.time() - self._last_frame_time > 5
                    and not self._stream_recovering):
                self._recover_stream()
            return None
        except Exception:
            return None

    def _recover_stream(self):
        """Restart the video stream after it goes stale."""
        self._stream_recovering = True
        def _do_recover():
            logger.warning("tello_stream_stale drone_id=%s", self.name)
            try:
                self.tello.streamoff()
            except Exception:
                pass
            time.sleep(1)
            try:
                self.tello.streamon()
                logger.info("tello_stream_restarted drone_id=%s", self.name)
            except Exception as e:
                logger.warning(
                    "tello_stream_recovery_failed drone_id=%s error_type=%s", self.name, type(e).__name__
                )
            self._stream_recovering = False
        threading.Thread(target=_do_recover, daemon=True, name=f"{self.name}-stream-recover").start()

    def on_frame(self, callback: Callable[[np.ndarray], None]):
        """Register a callback that receives each new frame."""
        self._frame_callbacks.append(callback)

    # --- Flight commands ---
    # All movement commands are serialized through _cmd_lock to prevent
    # concurrent UDP commands from saturating the Tello's single channel.
    # Every command returns a CommandResult; djitellopy errors still raise.

    def takeoff(self) -> CommandResult:
        with self._cmd_lock:
            self.tello.takeoff()
            self.state.is_flying = True
            logger.info("tello_takeoff drone_id=%s", self.name)
        return CommandResult("takeoff")

    def land(self) -> CommandResult:
        with self._cmd_lock:
            self.tello.land()
            self.state.is_flying = False
            logger.info("tello_land drone_id=%s", self.name)
        return CommandResult("land")

    def move(self, direction: str, distance_cm: int) -> CommandResult:
        """Move in a direction. Drops command if another is in-flight.

        A dropped or unknown-direction move is reported in the returned
        result rather than silently ignored.
        """
        if not self._cmd_lock.acquire(blocking=False):
            logger.warning("tello_command_dropped drone_id=%s command=move reason=busy", self.name)
            return CommandResult("move", CommandOutcome.DROPPED_BUSY, "another command in flight")
        try:
            distance_cm = max(20, min(500, distance_cm))
            cmd = getattr(self.tello, f"move_{direction}", None)
            if cmd:
                cmd(distance_cm)
            else:
                return CommandResult("move", CommandOutcome.REJECTED, "unknown direction")
        finally:
            self._cmd_lock.release()
        return CommandResult("move")

    def rotate(self, degrees: int) -> CommandResult:
        """Rotate clockwise/counter-clockwise. Drops if another command is in-flight."""
        if not self._cmd_lock.acquire(blocking=False):
            logger.warning("tello_command_dropped drone_id=%s command=rotate reason=busy", self.name)
            return CommandResult("rotate", CommandOutcome.DROPPED_BUSY, "another command in flight")
        try:
            if degrees > 0:
                self.tello.rotate_clockwise(min(360, degrees))
            else:
                self.tello.rotate_counter_clockwise(min(360, abs(degrees)))
        finally:
            self._cmd_lock.release()
        return CommandResult("rotate")

    def rc_control(self, lr: int, fb: int, ud: int, yaw: int) -> CommandResult:
        """Send RC joystick control. Each value -100 to 100."""
        self.tello.send_rc_control(lr, fb, ud, yaw)
        return CommandResult("rc_control")

    def hover(self) -> CommandResult:
        """Stop all movement and hover in place."""
        self.tello.send_rc_control(0, 0, 0, 0)
        return CommandResult("hover")

    # --- Telemetry ---

    def snapshot(self) -> TelemetrySnapshot:
        """Normalized telemetry from the last successful state poll.

        Readings keep the time of the poll that produced them, so a poll
        skipped while a flight command holds the command lock shows up as a
        growing ``age_s`` rather than as fresh data. ``is_flying`` follows
        takeoff/land commands, corrected by the zero-height crash heuristic.
        """
        s = self.state
        polled = self._last_poll_at
        snap = TelemetrySnapshot(
            backend="tello",
            drone_id=self.name,
            captured_at=time.time(),
            connected=s.is_connected,
            is_flying=s.is_flying,
            is_flying_source=IS_FLYING_COMMANDED,
            battery_pct=reading(s.battery, PERCENT, "tello.bat", self._battery_at),
            height_m=reading(s.height, METRE, "tello.h", polled, scale=0.01),
            temperature_c=reading(s.temperature, CELSIUS, "tello.templ_temph_avg", polled),
            flight_time_s=reading(s.flight_time, SECOND, "tello.time", polled),
            **self._extended,
        )
        return snap

    # NOTE: no goto_gps() — the standard Tello has no GPS. TelloController
    # intentionally only satisfies `DroneController`, not
    # `GpsDroneController` (see flightrisk/drone/controller.py). Callers must
    # check `isinstance(drone, GpsDroneController)` or
    # `drone.capabilities.has_gps` before invoking GPS navigation.

    # --- Internal threads ---

    def _start_keepalive(self):
        """Send keepalive every 10s to prevent auto-landing."""
        def _keepalive():
            while self._running:
                if not self._cmd_lock.locked():
                    try:
                        self.tello.send_control_command("command", timeout=5)
                    except Exception:
                        pass
                time.sleep(10)

        self._keepalive_thread = threading.Thread(
            target=_keepalive, daemon=True, name=f"{self.name}-keepalive"
        )
        self._keepalive_thread.start()

    def _poll_once(self):
        """One state poll: the body of the 2 s polling loop."""
        try:
            self.state.battery = self.tello.get_battery()
            self.state.height = self.tello.get_height()
            self.state.temperature = self.tello.get_temperature()
            self.state.flight_time = self.tello.get_flight_time()
            self._poll_failures = 0
            now = time.time()
            self._battery_at = now
            self._last_poll_at = now
            self._extended = self._read_extended_state(now)
            if self.state.is_flying and self.state.height == 0:
                zero_height_count = getattr(self, '_zero_height_count', 0) + 1
                self._zero_height_count = zero_height_count
                if zero_height_count >= 3:
                    self.state.is_flying = False
                    self._zero_height_count = 0
                    logger.warning(
                        "tello_crash_detected drone_id=%s zero_height_polls=%d", self.name, zero_height_count
                    )
            else:
                self._zero_height_count = 0
        except Exception:
            self._poll_failures += 1
            if self._poll_failures >= 3 and self.state.is_connected:
                self.state.is_connected = False
                self._running = False
                logger.warning(
                    "tello_connection_lost drone_id=%s consecutive_poll_failures=%d",
                    self.name, self._poll_failures,
                )

    def _read_extended_state(self, now: float) -> dict[str, Reading]:
        """Read the extra cached state-packet fields. Never raises.

        A field that is missing or not numeric is left out (``None`` in the
        snapshot); it never counts as a poll failure.
        """
        extended: dict[str, Reading] = {}
        for field_name, getter, scale, unit, source in _EXTENDED_FIELDS:
            try:
                raw = getattr(self.tello, getter)()
            except Exception:  # noqa: BLE001 -- optional field; absence is reported as None
                continue
            value = reading(raw, unit, source, now, scale=scale)
            if value is not None:
                extended[field_name] = value
        vx, vy = extended.get("vx_mps"), extended.get("vy_mps")
        if vx is not None and vy is not None:
            extended["ground_speed_mps"] = Reading(
                value=math.hypot(vx.value, vy.value),
                unit=METRE_PER_SECOND,
                source="derived(tello.vgx,tello.vgy)",
                measured_at=now,
            )
        return extended

    def _start_state_polling(self):
        """Poll drone state every 2 seconds. Skips when a flight command is active."""
        def _poll():
            self._poll_failures = 0
            while self._running:
                if self._cmd_lock.locked():
                    time.sleep(2)
                    continue
                self._poll_once()
                time.sleep(2)

        self._state_thread = threading.Thread(
            target=_poll, daemon=True, name=f"{self.name}-state"
        )
        self._state_thread.start()
