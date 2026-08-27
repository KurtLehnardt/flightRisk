"""DroneController protocol and shared types for multi-backend support.

Defines a formal Protocol that all drone backends (Tello, MAVLink, etc.)
must satisfy, plus shared dataclasses for state and capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

import numpy as np


@dataclass
class DroneState:
    battery: int = 0
    height: int = 0  # cm above takeoff
    temperature: int = 0
    flight_time: int = 0  # seconds
    is_flying: bool = False
    is_connected: bool = False
    # Extended fields (MAVLink drones populate these)
    latitude: float | None = None
    longitude: float | None = None
    altitude_msl: float | None = None  # meters AMSL
    heading: int | None = None  # degrees 0-359
    ground_speed: float | None = None  # m/s
    flight_mode: str | None = None


@dataclass
class DroneCapabilities:
    has_gps: bool = False
    has_rtsp: bool = False
    min_move_cm: int = 20
    max_move_cm: int = 500
    max_altitude_m: int = 120
    supports_missions: bool = False


@runtime_checkable
class DroneController(Protocol):
    """Common interface satisfied by every drone backend.

    Deliberately excludes `goto_gps` — not all backends (e.g. Tello) have
    GPS. Requiring it here forced non-GPS backends to implement a method
    that only raises `NotImplementedError`, a Liskov Substitution
    violation that pushed the "does this drone support X?" check onto
    every caller. See `GpsDroneController` for the GPS-capable subset, or
    check `capabilities.has_gps` for a cheap boolean test.
    """

    name: str
    host: str
    state: DroneState
    capabilities: DroneCapabilities

    def connect(self) -> bool: ...
    def disconnect(self) -> None: ...
    def get_frame(self) -> np.ndarray | None: ...
    def on_frame(self, cb: Callable[[np.ndarray], None]) -> None: ...
    def takeoff(self) -> None: ...
    def land(self) -> None: ...
    def move(self, direction: str, distance_cm: int) -> None: ...
    def rotate(self, degrees: int) -> None: ...
    def hover(self) -> None: ...
    def rc_control(self, lr: int, fb: int, ud: int, yaw: int) -> None: ...


@runtime_checkable
class GpsDroneController(DroneController, Protocol):
    """DroneController subset for backends that support GPS navigation.

    Callers that need `goto_gps` should either narrow with
    `isinstance(drone, GpsDroneController)` or check
    `drone.capabilities.has_gps` before dispatching.
    """

    def goto_gps(self, lat: float, lon: float, alt: float, timeout: float = 300.0) -> None: ...
