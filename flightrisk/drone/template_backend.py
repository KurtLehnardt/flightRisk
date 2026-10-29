"""TEMPLATE: copy this file to start a new drone backend. It is NOT registered and flies nothing.

This is a working, in-memory skeleton of a drone controller that follows
every rule in ``docs/drone-interface.md`` and passes the conformance suite
(``tests/drone_conformance.py``). Every place that would talk to a real
drone is a call to ``self._send(...)``, marked ``REPLACE``: swap those for
your SDK's calls (cflib for a Crazyflie, an MSP client for iNav, MAVSDK or
pymavlink for PX4 and ArduPilot, Olympe for a Parrot, a ROS 2 action client,
and so on).

How to use it (the five steps are in docs/drone-interface.md):

1. Copy it to ``flightrisk/drone/<yourdrone>.py`` and rename the class.
2. Replace every ``REPLACE`` block with real SDK calls; keep the result
   semantics (``CommandResult``, never ``NotImplementedError``, land never
   dropped, missing sensors are ``None``).
3. Declare only the capabilities you really implement, and the flight
   scale (``room`` for small indoor drones, ``field`` for GPS drones).
4. Register it in ``flightrisk/drone/backends.py`` (one ``register()`` call).
5. Add it to the conformance subjects in ``tests/test_drone_conformance.py``
   with a fake of your SDK, and to ``HARNESSES`` in
   ``tests/test_drone_contract.py``; run both.

Nothing here touches a network, a radio or a device.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

import numpy as np

from flightrisk.drone.controller import (
    Capability,
    CommandOutcome,
    CommandResult,
    DroneCapabilities,
    DroneState,
)
from flightrisk.drone.snapshot import (
    IS_FLYING_COMMANDED,
    METRE,
    PERCENT,
    TelemetrySnapshot,
    reading,
)

__all__ = ["TemplateDrone"]

_HORIZONTAL = ("forward", "back", "left", "right")
_VERTICAL = ("up", "down")


class TemplateDrone:
    """TEMPLATE controller (see the module docstring). Not a real backend; never registered."""

    def __init__(self, name: str, host: str = "template://drone"):
        self.name = name
        self.host = host
        self.state = DroneState()
        # Declare exactly what you implement. This template has no GPS and
        # no video, so it leaves out GOTO_GPS, RETURN_TO_HOME and VIDEO.
        self.capabilities = DroneCapabilities(
            has_gps=False,
            has_rtsp=False,
            min_move_cm=20,
            max_move_cm=500,
            max_altitude_m=10,
            features=frozenset({
                Capability.TAKEOFF_LAND,
                Capability.HOVER,
                Capability.RELATIVE_MOVE,
                Capability.ROTATE,
                Capability.RC_CONTROL,
            }),
            flight_scale="room",
        )
        #: Every command "sent" (REPLACE: your SDK has no such list).
        self.sent: list[tuple] = []
        self._lock = threading.Lock()
        self._measured_at: float | None = None
        self._height_m: float | None = None

    # --- REPLACE: the one place that talks to the drone -----------------

    def _send(self, command: str, *args: object) -> None:
        """REPLACE with your SDK call. Raise on a real failure (link down, refused)."""
        if not self.state.is_connected:
            raise RuntimeError(f"{command}: not connected")
        self.sent.append((command, *args))
        # REPLACE: real telemetry arrives from your SDK's callbacks or polls.
        self._measured_at = time.time()

    # --- Lifecycle ------------------------------------------------------

    def connect(self) -> bool:
        """Connect; return False (never raise) when the drone cannot be reached."""
        # REPLACE: open the link (e.g. cflib's SyncCrazyflie, an MSP serial port).
        with self._lock:
            self.state.is_connected = True
            self.state.battery = 90
            self._height_m = 0.0
            self._measured_at = time.time()
        return True

    def disconnect(self) -> None:
        """Close the link; safe to call twice. Never lands by itself (the caller lands first)."""
        with self._lock:
            self.state.is_connected = False
            self.state.is_flying = False

    # --- Telemetry ------------------------------------------------------

    def snapshot(self) -> TelemetrySnapshot:
        """SI units, ``None`` for anything not measured; never a made-up zero."""
        at = self._measured_at if self.state.is_connected else None
        return TelemetrySnapshot(
            backend="template",
            drone_id=self.name,
            captured_at=time.time(),
            connected=self.state.is_connected,
            is_flying=self.state.is_flying,
            # "sensed" only if the vehicle itself reports being airborne.
            is_flying_source=IS_FLYING_COMMANDED,
            battery_pct=reading(self.state.battery, PERCENT, "template.battery", at),
            height_m=reading(self._height_m, METRE, "template.height", at),
        )

    # --- Flight ---------------------------------------------------------

    def takeoff(self) -> CommandResult:
        self._send("takeoff")
        self.state.is_flying = True
        self._height_m = 0.8
        self.state.height = 80
        return CommandResult("takeoff")

    def land(self) -> CommandResult:
        """Never dropped and idempotent: on the ground it succeeds without sending."""
        if not self.state.is_flying:
            return CommandResult("land")
        self._send("land")
        self.state.is_flying = False
        self._height_m = 0.0
        self.state.height = 0
        return CommandResult("land")

    def hover(self) -> CommandResult:
        self._send("hover")
        return CommandResult("hover")

    def move(self, direction: str, distance_cm: int) -> CommandResult:
        if direction not in _HORIZONTAL + _VERTICAL:
            return CommandResult("move", CommandOutcome.REJECTED, f"unknown direction {direction!r}")
        if not self.capabilities.min_move_cm <= distance_cm <= self.capabilities.max_move_cm:
            return CommandResult("move", CommandOutcome.REJECTED, f"distance {distance_cm} cm out of range")
        if not self.state.is_flying:
            return CommandResult("move", CommandOutcome.NOT_EXECUTED, "not flying")
        self._send("move", direction, distance_cm)
        if direction in _VERTICAL:
            self._height_m = max(0.0, (self._height_m or 0.0) + (distance_cm if direction == "up" else -distance_cm) / 100)
            self.state.height = round(self._height_m * 100)
        return CommandResult("move")

    def rotate(self, degrees: int) -> CommandResult:
        if not self.state.is_flying:
            return CommandResult("rotate", CommandOutcome.NOT_EXECUTED, "not flying")
        self._send("rotate", degrees)
        return CommandResult("rotate")

    def rc_control(self, lr: int, fb: int, ud: int, yaw: int) -> CommandResult:
        """Stick input, each -100..100; out-of-range values are clamped, never passed on."""
        sticks = tuple(max(-100, min(100, int(v))) for v in (lr, fb, ud, yaw))
        self._send("rc", *sticks)
        return CommandResult("rc_control")

    # --- Video (not declared: no VIDEO capability) -----------------------

    def get_frame(self) -> np.ndarray | None:
        return None

    def on_frame(self, cb: Callable[[np.ndarray], None]) -> None:
        return None
