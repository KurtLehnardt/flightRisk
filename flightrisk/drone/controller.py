"""Drone interfaces: role protocols, capabilities, command results and state.

The dashboard and the flight logic around it depend only on the small role
protocols defined here, never on a concrete backend such as the Tello or
MAVLink controller (dependency inversion). Each role covers one concern, so a
backend implements exactly the roles it supports (interface segregation):

* ``Lifecycle``       -- identity, capabilities, ``connect()`` / ``disconnect()``
* ``TelemetrySource`` -- ``snapshot()`` returning a normalized
  :class:`flightrisk.drone.snapshot.TelemetrySnapshot`
* ``FlightControl``   -- ``takeoff()`` / ``land()`` / ``hover()``
* ``Motion``          -- relative ``move()`` / ``rotate()`` / ``rc_control()``
* ``Navigation``      -- ``return_to_home()`` (optional)
* ``GpsDroneController`` -- ``goto_gps()`` (optional)
* ``VideoSource``     -- ``get_frame()`` / ``on_frame()``

Optional roles are declared through :class:`DroneCapabilities` and queried
with ``capabilities_of(drone).supports(Capability.X)`` instead of
``isinstance``/``hasattr`` checks against concrete types. No method may raise
``NotImplementedError`` to signal "unsupported" (Liskov substitution): a
backend that cannot do something does not declare the capability, and if the
method exists anyway it returns a ``REJECTED`` :class:`CommandResult`.

``DroneController`` is kept unchanged as the legacy aggregate protocol (the
mandatory roles plus the mutable ``state`` cache) so existing code and tests
that type against it keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - import only for annotations
    from flightrisk.drone.snapshot import TelemetrySnapshot


@dataclass
class DroneState:
    """Mutable raw-state cache that a backend updates as data arrives.

    This is the legacy view, kept for backward compatibility. Zero defaults
    here cannot be told apart from real measurements, so new consumers should
    read :meth:`TelemetrySource.snapshot` (via
    :func:`flightrisk.drone.snapshot.read_telemetry`) instead.
    """

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


class Capability(str, Enum):
    """Optional behaviours a backend can declare.

    Sensor availability is not listed here: a sensor a backend cannot read
    simply shows up as ``None`` in its telemetry snapshot.
    """

    TAKEOFF_LAND = "takeoff_land"
    HOVER = "hover"
    RELATIVE_MOVE = "relative_move"
    ROTATE = "rotate"
    RC_CONTROL = "rc_control"
    VIDEO = "video"
    GOTO_GPS = "goto_gps"
    RETURN_TO_HOME = "return_to_home"


#: Capabilities every currently supported backend provides.
BASIC_FLIGHT: frozenset[Capability] = frozenset({
    Capability.TAKEOFF_LAND,
    Capability.HOVER,
    Capability.RELATIVE_MOVE,
    Capability.ROTATE,
    Capability.RC_CONTROL,
    Capability.VIDEO,
})


@dataclass
class DroneCapabilities:
    has_gps: bool = False
    has_rtsp: bool = False
    min_move_cm: int = 20
    max_move_cm: int = 500
    max_altitude_m: int = 120
    supports_missions: bool = False
    #: Optional roles this backend implements. Callers ask
    #: ``supports(Capability.X)`` rather than probing for methods.
    features: frozenset[Capability] = BASIC_FLIGHT

    def supports(self, capability: Capability) -> bool:
        return capability in self.features

    @classmethod
    def inferred_from(cls, drone: Any) -> "DroneCapabilities":
        """Best-effort capabilities for an object that does not declare any.

        Only legacy duck-typed objects (older test doubles, third-party
        controllers written before capabilities existed) reach this. It is
        the single place where method presence is still inspected, so the
        rest of the code can rely on ``supports()`` alone.
        """
        features = set(BASIC_FLIGHT)
        if callable(getattr(drone, "return_to_home", None)):
            features.add(Capability.RETURN_TO_HOME)
        if callable(getattr(drone, "goto_gps", None)):
            features.add(Capability.GOTO_GPS)
        return cls(features=frozenset(features))


def capabilities_of(drone: Any) -> DroneCapabilities:
    """Return the capabilities ``drone`` declares (inferred for legacy objects)."""
    caps = getattr(drone, "capabilities", None)
    if isinstance(caps, DroneCapabilities):
        return caps
    return DroneCapabilities.inferred_from(drone)


class CommandOutcome(str, Enum):
    """What happened to a command that did not raise.

    Hard failures (link errors, the drone refusing a command) are still
    raised as exceptions, exactly as before. These outcomes cover the cases
    that used to be silent.
    """

    #: The command was sent and the backend reported no error.
    OK = "ok"
    #: The command was not sent because another command was in flight.
    DROPPED_BUSY = "dropped_busy"
    #: The backend refused the command (unsupported or invalid argument).
    REJECTED = "rejected"


@dataclass(frozen=True)
class CommandResult:
    """Reported outcome of one flight command."""

    command: str
    outcome: CommandOutcome = CommandOutcome.OK
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is CommandOutcome.OK


def command_result(value: Any, command: str) -> CommandResult:
    """Normalize a command's return value to a :class:`CommandResult`.

    Legacy controllers return ``None`` on success; that maps to ``OK``. A
    plain ``False`` (the "command was not sent" convention some controllers
    use) maps to ``DROPPED_BUSY`` so it is reported rather than ignored.
    """
    if isinstance(value, CommandResult):
        return value
    if value is False:
        return CommandResult(command, CommandOutcome.DROPPED_BUSY, "controller returned False")
    return CommandResult(command=command)


# ---------------------------------------------------------------------------
# Role protocols (interface segregation)
# ---------------------------------------------------------------------------


@runtime_checkable
class Lifecycle(Protocol):
    name: str
    host: str
    capabilities: DroneCapabilities

    def connect(self) -> bool: ...
    def disconnect(self) -> None: ...


@runtime_checkable
class TelemetrySource(Protocol):
    def snapshot(self) -> "TelemetrySnapshot": ...


@runtime_checkable
class FlightControl(Protocol):
    """Take off, land and hold position.

    ``hover()`` is the "stop all motion" command; there is deliberately no
    separate ``stop()`` because on the Tello the only other stop is the
    motor-kill ``emergency`` command, which must never be one call away.
    ``land()`` must be idempotent: landing a drone that is already on the
    ground succeeds without error.
    """

    def takeoff(self) -> CommandResult | None: ...
    def land(self) -> CommandResult | None: ...
    def hover(self) -> CommandResult | None: ...


@runtime_checkable
class Motion(Protocol):
    def move(self, direction: str, distance_cm: int) -> CommandResult | None: ...
    def rotate(self, degrees: int) -> CommandResult | None: ...
    def rc_control(self, lr: int, fb: int, ud: int, yaw: int) -> CommandResult | None: ...


@runtime_checkable
class Navigation(Protocol):
    """Optional: fly back over the takeoff point (``Capability.RETURN_TO_HOME``).

    ``return_to_home()`` blocks until the drone is back over its takeoff
    point, raises on failure or timeout, and does not land by itself.
    """

    def return_to_home(self) -> CommandResult | None: ...


@runtime_checkable
class VideoSource(Protocol):
    def get_frame(self) -> np.ndarray | None: ...
    def on_frame(self, cb: Callable[[np.ndarray], None]) -> None: ...


@runtime_checkable
class DroneController(Protocol):
    """Common interface satisfied by every drone backend.

    Legacy aggregate of the mandatory roles (``Lifecycle``,
    ``FlightControl``, ``Motion``, ``VideoSource``) plus the raw ``state``
    cache. New code should depend on the narrowest role it needs.

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

    Callers that need `goto_gps` should check
    ``capabilities_of(drone).supports(Capability.GOTO_GPS)`` (or narrow with
    `isinstance(drone, GpsDroneController)`) before dispatching.
    """

    def goto_gps(self, lat: float, lon: float, alt: float, timeout: float = 300.0) -> None: ...


__all__ = [
    "BASIC_FLIGHT",
    "Capability",
    "CommandOutcome",
    "CommandResult",
    "DroneCapabilities",
    "DroneController",
    "DroneState",
    "FlightControl",
    "GpsDroneController",
    "Lifecycle",
    "Motion",
    "Navigation",
    "TelemetrySource",
    "VideoSource",
    "capabilities_of",
    "command_result",
]
