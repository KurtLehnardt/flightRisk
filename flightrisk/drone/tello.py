"""Tello drone controller wrapper around DJITelloPy.

Handles connection, video streaming, flight commands, and keepalive.
Standard Tello (TLW004) — SDK 1.3, WiFi AP mode only.

``FakeTelloController`` is an in-process stand-in with the same interface. It
never touches the network, so the edge device can fly a simulated drone
(``python -m flightrisk.edge_device --source fake``) and tests can exercise
flight logic without hardware.
"""

import threading
import time
from typing import Callable

import numpy as np
from djitellopy import Tello

from flightrisk.config import get_config
from flightrisk.drone.controller import DroneCapabilities, DroneController, DroneState

# Backward-compatible re-export so `from flightrisk.drone.tello import DroneState` still works
__all__ = [
    "TelloController",
    "FakeTelloController",
    "DroneState",
    "DroneCapabilities",
    "DroneController",
]

# How long an interrupting land waits for an in-flight command to release the
# command channel before it gives up on the confirming land. djitellopy waits
# up to 7 s per attempt and retries three times, so 25 s covers a stuck move.
_LAND_CONFIRM_WAIT_S = 25.0


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
        # Set by land() and cleared by takeoff(). While it is set, move() and
        # rotate() refuse to send anything, so a lane executor cannot push a
        # move in behind a landing.
        self._landing = False

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
            self._landing = False
            self.tello.takeoff()
            self.state.is_flying = True
            print(f"[{self.name}] Takeoff.")

    def land(self):
        """Land now, interrupting any flight command that is still in progress.

        If the command channel is free this is an ordinary ``land``. If a
        move, rotate or takeoff is still waiting for its reply, the land does
        not queue behind it: ``stop`` (hover, accepted at any time by SDK 2.0
        and later) and ``land`` are sent straight away without waiting for a
        reply, then a normal ``land`` confirms once the channel is free. The
        confirming land raises like any other command failure, so callers can
        retry.
        """
        self._landing = True
        if self._cmd_lock.acquire(blocking=False):
            try:
                self.tello.land()
                self.state.is_flying = False
                print(f"[{self.name}] Landing.")
            finally:
                self._cmd_lock.release()
            return

        print(f"[{self.name}] Landing now (interrupting an in-flight command).")
        for raw in ("stop", "land"):
            try:
                self.tello.send_command_without_return(raw)
            except Exception as e:
                print(f"[{self.name}] Immediate '{raw}' failed: {e}")
        if not self._cmd_lock.acquire(timeout=_LAND_CONFIRM_WAIT_S):
            raise RuntimeError(f"[{self.name}] land not confirmed: command channel still busy")
        try:
            self.tello.land()
            self.state.is_flying = False
            print(f"[{self.name}] Landing confirmed.")
        finally:
            self._cmd_lock.release()

    def move(self, direction: str, distance_cm: int) -> bool:
        """Move in a direction. Drops command if another is in-flight.

        Returns True when the command was sent and acknowledged, and False
        when it was dropped (another command is in flight, a landing is in
        progress, or the direction is unknown). Command failures raise.
        """
        if self._landing:
            return False
        if not self._cmd_lock.acquire(blocking=False):
            return False
        try:
            if self._landing:
                return False
            distance_cm = max(20, min(500, distance_cm))
            cmd = getattr(self.tello, f"move_{direction}", None)
            if cmd:
                cmd(distance_cm)
                return True
            return False
        finally:
            self._cmd_lock.release()

    def rotate(self, degrees: int) -> bool:
        """Rotate clockwise/counter-clockwise. Drops if another command is in-flight.

        Returns True when the command was sent, False when it was dropped.
        """
        if self._landing:
            return False
        if not self._cmd_lock.acquire(blocking=False):
            return False
        try:
            if self._landing:
                return False
            if degrees > 0:
                self.tello.rotate_clockwise(min(360, degrees))
            else:
                self.tello.rotate_counter_clockwise(min(360, abs(degrees)))
            return True
        finally:
            self._cmd_lock.release()

    def read_battery(self) -> int | None:
        """Battery percentage from the latest state packet, or None if unknown.

        Reads the state stream (no UDP command), so it is safe to call while
        a flight command is in progress.
        """
        try:
            battery = int(self.tello.get_battery())
        except Exception:
            return None
        self.state.battery = battery
        return battery

    def read_height_cm(self) -> int | None:
        """Height above takeoff in cm from the latest state packet, or None if unknown."""
        try:
            height = int(self.tello.get_height())
        except Exception:
            return None
        self.state.height = height
        return height

    def rc_control(self, lr: int, fb: int, ud: int, yaw: int):
        """Send RC joystick control. Each value -100 to 100."""
        self.tello.send_rc_control(lr, fb, ud, yaw)

    def hover(self):
        """Stop all movement and hover in place."""
        self.tello.send_rc_control(0, 0, 0, 0)

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


class FakeTelloController:
    """In-process simulated Tello with the ``TelloController`` interface.

    Nothing here touches the network. Flight commands update a simulated
    state and are appended to ``calls`` as tuples (``("takeoff",)``,
    ``("land",)``, ``("move", direction, distance_cm)``,
    ``("rotate", degrees)``), so tests can assert on exactly what a real
    drone would have been told. Like the real drone, it refuses to move while
    on the ground.

    ``speed_cm_s`` makes moves take simulated time (distance divided by
    speed), which lets a fake edge show lane progress in the dashboard. A
    landing interrupts a move that is in progress.
    """

    TAKEOFF_HEIGHT_CM = 80

    def __init__(
        self,
        name: str = "fake-tello",
        battery: int = 100,
        takeoff_height_cm: int = TAKEOFF_HEIGHT_CM,
        speed_cm_s: float | None = None,
        frame_shape: tuple[int, int, int] = (720, 960, 3),
    ):
        self.name = name
        self.host = "fake"
        self.state = DroneState(battery=battery)
        self.capabilities = DroneCapabilities(
            has_gps=False,
            has_rtsp=False,
            min_move_cm=20,
            max_move_cm=500,
            max_altitude_m=10,
            supports_missions=False,
        )
        self.calls: list[tuple] = []
        self.takeoff_height_cm = takeoff_height_cm
        self.speed_cm_s = speed_cm_s
        self._frame_shape = frame_shape
        self._frame_callbacks: list[Callable[[np.ndarray], None]] = []
        self._lock = threading.Lock()
        self._interrupt = threading.Event()

    def _record(self, *call) -> None:
        with self._lock:
            self.calls.append(call)

    def connect(self) -> bool:
        self.state.is_connected = True
        print(f"[{self.name}] Fake drone connected. Battery: {self.state.battery}%")
        return True

    def disconnect(self) -> None:
        if self.state.is_flying:
            self.land()
        self.state.is_connected = False

    def get_frame(self) -> np.ndarray | None:
        if not self.state.is_connected:
            return None
        frame = np.zeros(self._frame_shape, dtype=np.uint8)
        for cb in self._frame_callbacks:
            cb(frame)
        return frame

    def on_frame(self, callback: Callable[[np.ndarray], None]) -> None:
        self._frame_callbacks.append(callback)

    def takeoff(self) -> None:
        self._record("takeoff")
        self._interrupt.clear()
        self.state.is_flying = True
        self.state.height = self.takeoff_height_cm

    def land(self) -> None:
        self._interrupt.set()
        self._record("land")
        self.state.is_flying = False
        self.state.height = 0

    def _fly_for(self, distance_cm: float) -> None:
        if self.speed_cm_s:
            self._interrupt.wait(distance_cm / self.speed_cm_s)

    def move(self, direction: str, distance_cm: int) -> bool:
        if not self.state.is_flying:
            raise RuntimeError(f"[{self.name}] cannot move while landed")
        if direction not in ("forward", "back", "left", "right", "up", "down"):
            return False
        self._record("move", direction, distance_cm)
        self._fly_for(distance_cm)
        if direction == "up":
            self.state.height += distance_cm
        elif direction == "down":
            self.state.height = max(0, self.state.height - distance_cm)
        return True

    def rotate(self, degrees: int) -> bool:
        if not self.state.is_flying:
            raise RuntimeError(f"[{self.name}] cannot rotate while landed")
        self._record("rotate", degrees)
        return True

    def hover(self) -> None:
        self._record("hover")

    def rc_control(self, lr: int, fb: int, ud: int, yaw: int) -> None:
        self._record("rc", lr, fb, ud, yaw)

    def read_battery(self) -> int | None:
        return self.state.battery

    def read_height_cm(self) -> int | None:
        return self.state.height
