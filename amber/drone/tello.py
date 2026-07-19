"""Tello drone controller wrapper around DJITelloPy.

Handles connection, video streaming, flight commands, and keepalive.
Standard Tello (TLW004) — SDK 1.3, WiFi AP mode only.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np
from djitellopy import Tello


@dataclass
class DroneState:
    battery: int = 0
    height: int = 0  # cm
    temperature: int = 0
    flight_time: int = 0  # seconds
    is_flying: bool = False
    is_connected: bool = False


class TelloController:
    """Manages a single Tello drone connection and video stream."""

    def __init__(self, name: str = "drone"):
        self.name = name
        self.tello = Tello()
        self.state = DroneState()
        self._frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()
        self._keepalive_thread: threading.Thread | None = None
        self._state_thread: threading.Thread | None = None
        self._running = False
        self._frame_callbacks: list[Callable[[np.ndarray], None]] = []

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
        """Land if flying, stop streams, disconnect."""
        self._running = False
        if self.state.is_flying:
            self.land()
        try:
            self.tello.streamoff()
            self.tello.end()
        except Exception:
            pass
        self.state.is_connected = False
        print(f"[{self.name}] Disconnected.")

    def get_frame(self) -> np.ndarray | None:
        """Get the latest video frame as a BGR numpy array."""
        if not self.state.is_connected:
            return None
        try:
            frame_read = self.tello.get_frame_read()
            frame = frame_read.frame
            if frame is not None:
                with self._frame_lock:
                    self._frame = frame.copy()
                for cb in self._frame_callbacks:
                    cb(frame)
            return frame
        except Exception:
            return None

    def on_frame(self, callback: Callable[[np.ndarray], None]):
        """Register a callback that receives each new frame."""
        self._frame_callbacks.append(callback)

    # --- Flight commands ---

    def takeoff(self):
        self.tello.takeoff()
        self.state.is_flying = True
        print(f"[{self.name}] Takeoff.")

    def land(self):
        self.tello.land()
        self.state.is_flying = False
        print(f"[{self.name}] Landing.")

    def move(self, direction: str, distance_cm: int):
        """Move in a direction. direction: forward, back, left, right, up, down."""
        distance_cm = max(20, min(500, distance_cm))
        cmd = getattr(self.tello, f"move_{direction}", None)
        if cmd:
            cmd(distance_cm)

    def rotate(self, degrees: int):
        """Rotate clockwise (positive) or counter-clockwise (negative)."""
        if degrees > 0:
            self.tello.rotate_clockwise(min(360, degrees))
        else:
            self.tello.rotate_counter_clockwise(min(360, abs(degrees)))

    def rc_control(self, lr: int, fb: int, ud: int, yaw: int):
        """Send RC joystick control. Each value -100 to 100."""
        self.tello.send_rc_control(lr, fb, ud, yaw)

    def hover(self):
        """Stop all movement and hover in place."""
        self.tello.send_rc_control(0, 0, 0, 0)

    # --- Internal threads ---

    def _start_keepalive(self):
        """Send keepalive every 10s to prevent auto-landing."""
        def _keepalive():
            while self._running:
                try:
                    self.tello.send_keepalive()
                except Exception:
                    pass
                time.sleep(10)

        self._keepalive_thread = threading.Thread(
            target=_keepalive, daemon=True, name=f"{self.name}-keepalive"
        )
        self._keepalive_thread.start()

    def _start_state_polling(self):
        """Poll drone state every 2 seconds."""
        def _poll():
            while self._running:
                try:
                    self.state.battery = self.tello.get_battery()
                    self.state.height = self.tello.get_height()
                    self.state.temperature = self.tello.get_temperature()
                    self.state.flight_time = self.tello.get_flight_time()
                except Exception:
                    pass
                time.sleep(2)

        self._state_thread = threading.Thread(
            target=_poll, daemon=True, name=f"{self.name}-state"
        )
        self._state_thread.start()
