"""Tello drone controller wrapper around DJITelloPy.

Handles connection, video streaming, flight commands, and keepalive.
Standard Tello (TLW004) — SDK 1.3, WiFi AP mode only.
"""

import threading
import time
from typing import Callable

import cv2
import numpy as np
from djitellopy import Tello

from amber.config import get_config
from amber.drone.controller import DroneCapabilities, DroneController, DroneState

# Backward-compatible re-export so `from amber.drone.tello import DroneState` still works
__all__ = ["TelloController", "DroneState", "DroneCapabilities", "DroneController"]


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

    def connect(self) -> bool:
        """Connect to the Tello and start video stream."""
        try:
            self.tello.connect()
            self.state.battery = self.tello.get_battery()
            self.state.is_connected = True
            print(f"[{self.name}] Connected. Battery: {self.state.battery}%")

            self.tello.streamon()
            self._running = True
            self._start_keepalive()
            self._start_state_polling()
            return True
        except Exception as e:
            print(f"[{self.name}] Connection failed: {e}")
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
        print(f"[{self.name}] Disconnected.")

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
                        print(f"[{self.name}] Frozen frame detected ({self._frozen_count} identical), recovering...")
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
            print(f"[{self.name}] Video stream stale, restarting...")
            try:
                self.tello.streamoff()
            except Exception:
                pass
            time.sleep(1)
            try:
                self.tello.streamon()
                print(f"[{self.name}] Video stream restarted.")
            except Exception as e:
                print(f"[{self.name}] Stream recovery failed: {e}")
            self._stream_recovering = False
        threading.Thread(target=_do_recover, daemon=True, name=f"{self.name}-stream-recover").start()

    def on_frame(self, callback: Callable[[np.ndarray], None]):
        """Register a callback that receives each new frame."""
        self._frame_callbacks.append(callback)

    # --- Flight commands ---
    # All movement commands are serialized through _cmd_lock to prevent
    # concurrent UDP commands from saturating the Tello's single channel.

    def takeoff(self):
        with self._cmd_lock:
            self.tello.takeoff()
            self.state.is_flying = True
            print(f"[{self.name}] Takeoff.")

    def land(self):
        with self._cmd_lock:
            self.tello.land()
            self.state.is_flying = False
            print(f"[{self.name}] Landing.")

    def move(self, direction: str, distance_cm: int):
        """Move in a direction. Drops command if another is in-flight."""
        if not self._cmd_lock.acquire(blocking=False):
            return
        try:
            distance_cm = max(20, min(500, distance_cm))
            cmd = getattr(self.tello, f"move_{direction}", None)
            if cmd:
                cmd(distance_cm)
        finally:
            self._cmd_lock.release()

    def rotate(self, degrees: int):
        """Rotate clockwise/counter-clockwise. Drops if another command is in-flight."""
        if not self._cmd_lock.acquire(blocking=False):
            return
        try:
            if degrees > 0:
                self.tello.rotate_clockwise(min(360, degrees))
            else:
                self.tello.rotate_counter_clockwise(min(360, abs(degrees)))
        finally:
            self._cmd_lock.release()

    def rc_control(self, lr: int, fb: int, ud: int, yaw: int):
        """Send RC joystick control. Each value -100 to 100."""
        self.tello.send_rc_control(lr, fb, ud, yaw)

    def hover(self):
        """Stop all movement and hover in place."""
        self.tello.send_rc_control(0, 0, 0, 0)

    def goto_gps(self, lat: float, lon: float, alt_m: float) -> None:
        """Navigate to GPS coordinates. Not supported on Tello."""
        raise NotImplementedError("Tello does not have GPS")

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

    def _start_state_polling(self):
        """Poll drone state every 2 seconds. Skips when a flight command is active."""
        def _poll():
            poll_failures = 0
            while self._running:
                if self._cmd_lock.locked():
                    time.sleep(2)
                    continue
                try:
                    self.state.battery = self.tello.get_battery()
                    self.state.height = self.tello.get_height()
                    self.state.temperature = self.tello.get_temperature()
                    self.state.flight_time = self.tello.get_flight_time()
                    poll_failures = 0
                    if self.state.is_flying and self.state.height == 0:
                        zero_height_count = getattr(self, '_zero_height_count', 0) + 1
                        self._zero_height_count = zero_height_count
                        if zero_height_count >= 3:
                            self.state.is_flying = False
                            self._zero_height_count = 0
                            print(f"[{self.name}] Crash detected — height 0 for {zero_height_count} polls.")
                    else:
                        self._zero_height_count = 0
                except Exception:
                    poll_failures += 1
                    if poll_failures >= 3 and self.state.is_connected:
                        self.state.is_connected = False
                        self._running = False
                        print(f"[{self.name}] Connection lost — {poll_failures} consecutive poll failures.")
                time.sleep(2)

        self._state_thread = threading.Thread(
            target=_poll, daemon=True, name=f"{self.name}-state"
        )
        self._state_thread.start()
