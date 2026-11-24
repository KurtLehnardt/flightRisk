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

import inspect
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - import only for annotations
    from flightrisk.drone.snapshot import TelemetrySnapshot
    from flightrisk.hardware.catalogue import CameraSpec


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
    gps_fix_ok: bool | None = None  # a 3D fix or better; None when the backend does not report it


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
    #: The edge senses obstacles itself and flies lanes in short sensed steps
    #: (``flightrisk.drone.obstacle_sensor``). The backend provides ``on_frame``.
    STEPPED_SENSING = "stepped_sensing"
    #: The autopilot avoids obstacles by itself, so the edge flies lanes as
    #: usual and only watches progress. The backend provides
    #: ``avoidance_status()`` (the list of avoidance problems, empty when none).
    ONBOARD_AVOIDANCE = "onboard_avoidance"
    #: The backend provides ``settled_height()``, a measured height it has
    #: waited to settle after takeoff.
    SETTLED_HEIGHT = "settled_height"
    #: The backend provides ``last_takeoff_reply_s()``, how long the takeoff
    #: command took to be answered.
    TAKEOFF_REPLY_TIME = "takeoff_reply_time"
    #: The backend's ``takeoff()`` accepts an ``abort`` keyword so a pending
    #: takeoff can be cancelled.
    TAKEOFF_ABORT = "takeoff_abort"
    #: The backend provides ``measured_heading_deg()``, a heading read from its
    #: sensors rather than a commanded one.
    MEASURED_HEADING = "measured_heading"
    #: The backend provides ``state_temperature()``, a temperature reading from
    #: its state stream.
    STATE_TEMPERATURE = "state_temperature"
    #: The backend provides ``measured_pose()``, a position and heading
    #: estimate read from the vehicle.
    MEASURED_POSE = "measured_pose"
    #: The backend provides ``link_status()``, a report on the health of its
    #: link. Any backend that has the method declares this, and a report that
    #: is not ok refuses takeoff. For the MAVLink backend the report carries
    #: the autopilot's preflight and failsafe checks.
    LINK_HEALTH = "link_health"


#: Capabilities every currently supported backend provides.
BASIC_FLIGHT: frozenset[Capability] = frozenset({
    Capability.TAKEOFF_LAND,
    Capability.HOVER,
    Capability.RELATIVE_MOVE,
    Capability.ROTATE,
    Capability.RC_CONTROL,
    Capability.VIDEO,
})


#: Capabilities a duck-typed object declares by providing a callable of this name.
#: The conformance honesty check reuses this table, so a declared member
#: without its method fails there.
INFERRED_BY_METHOD: tuple[tuple[Capability, str], ...] = (
    (Capability.SETTLED_HEIGHT, "settled_height"),
    (Capability.TAKEOFF_REPLY_TIME, "last_takeoff_reply_s"),
    (Capability.MEASURED_HEADING, "measured_heading_deg"),
    (Capability.STATE_TEMPERATURE, "state_temperature"),
    (Capability.MEASURED_POSE, "measured_pose"),
    (Capability.LINK_HEALTH, "link_status"),
)


def _takeoff_accepts_abort(drone: Any) -> bool:
    """True when ``drone.takeoff`` is callable and takes an ``abort`` keyword.

    An unreadable signature or a missing ``takeoff`` gives False, never an error.
    """
    takeoff = getattr(drone, "takeoff", None)
    if not callable(takeoff):
        return False
    try:
        return "abort" in inspect.signature(takeoff).parameters
    except (TypeError, ValueError):
        return False


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
    #: Which safety-envelope profile the fleet edge applies to this drone
    #: (``flightrisk.drone.safety.limits_for``): ``"room"`` (indoor,
    #: centimetre moves, the Tello) or ``"field"`` (outdoor, GPS, metre-scale
    #: moves, slots, ceiling and geofence).
    flight_scale: str = "room"

    def supports(self, capability: Capability) -> bool:
        return capability in self.features

    @classmethod
    def inferred_from(cls, drone: Any) -> "DroneCapabilities":
        """Best-effort capabilities for an object that does not declare any.

        Only legacy duck-typed objects (older test doubles, third-party
        controllers written before capabilities existed) reach this. It is
        the single place where method presence is still inspected, so the
        rest of the code can rely on ``supports()`` alone.

        Each probe reads the attribute of that name, so a property that raises
        propagates out of here and out of ``capabilities_of``. Every built-in
        backend declares its capabilities and so never reaches the probe.
        """
        features = set(BASIC_FLIGHT)
        if callable(getattr(drone, "return_to_home", None)):
            features.add(Capability.RETURN_TO_HOME)
        if callable(getattr(drone, "goto_gps", None)):
            features.add(Capability.GOTO_GPS)
        for capability, method in INFERRED_BY_METHOD:
            if callable(getattr(drone, method, None)):
                features.add(capability)
        if _takeoff_accepts_abort(drone):
            features.add(Capability.TAKEOFF_ABORT)
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
    #: The command was not executed and the backend gave no reason. This is
    #: what a bare ``False`` return means: for the Tello, a move or rotate
    #: dropped because a flight command or a landing holds the channel, or
    #: a takeoff refused because a landing is in progress or was requested.
    NOT_EXECUTED = "not_executed"
    #: A move ended before its target because the autopilot's own obstacle
    #: avoidance held the vehicle back. It is not an error: ``detail`` says
    #: where the vehicle stopped and the caller applies its cut policy.
    STOPPED_SHORT = "stopped_short"


@dataclass(frozen=True)
class CommandResult:
    """Reported outcome of one flight command.

    Truthiness follows :attr:`ok`, so a result that was not executed is
    falsy like the bare ``False`` it replaces. Callers must still normalize
    every return value with :func:`command_result` and read ``.ok``: a
    legacy ``None`` return is falsy but means success.
    """

    command: str
    outcome: CommandOutcome = CommandOutcome.OK
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is CommandOutcome.OK

    def __bool__(self) -> bool:
        return self.ok


class CommandNotExecutedError(RuntimeError):
    """Raised by callers that need a command to have taken effect when it did not.

    Carries the non-OK :class:`CommandResult` as ``result``.
    """

    def __init__(self, result: CommandResult):
        self.result = result
        detail = f": {result.detail}" if result.detail else ""
        super().__init__(f"{result.command} not executed ({result.outcome.value}){detail}")


class CommandRefusedError(RuntimeError):
    """Raised by a controller when the drone answered a flight command with a refusal.

    ``command`` is the command as sent (for example ``down 80``) and
    ``reply`` the drone's answer (for example ``error No valid imu``). It is
    a ``RuntimeError``, so every caller that already treats a refused
    command as an error keeps doing so; the lane executor reads ``reply`` to
    tell a transient refusal it may retry from any other.
    """

    def __init__(self, message: str, *, command: str, reply: str):
        super().__init__(message)
        self.command = command
        self.reply = reply
        # Set once the lane executor has counted this refusal in telemetry.
        self.counted = False


@dataclass(frozen=True)
class SettledHeight:
    """A height the drone held steadily, read from fresh state (``settled_height()``).

    ``height_cm`` is the newest height of the run of packets that showed it
    steady. ``tof_cm`` is the lowest downward time-of-flight distance of
    that run, or None when any packet of it had no usable distance.
    ``samples`` is the number of packets in the run and ``waited_s`` how
    long the wait took. ``vgx_cm_s`` and ``vgy_cm_s`` are the largest
    horizontal speeds of the run (None when not reported): the settle judges
    vertical steadiness only, and these are logged for calibration.
    """

    height_cm: int
    tof_cm: int | None
    samples: int
    waited_s: float
    vgx_cm_s: int | None = None
    vgy_cm_s: int | None = None


def command_result(value: Any, command: str) -> CommandResult:
    """Normalize a command's return value to a :class:`CommandResult`.

    This is the one way callers read a command's return value (see
    ``docs/drone-interface.md``, "Command results"). Backends may return a
    :class:`CommandResult` or one of the legacy values:

    * ``None`` or ``True``: the command was executed (``OK``).
    * ``False``: the command was not executed and the backend gave no
      reason (``NOT_EXECUTED``).

    Any other value counts as ``OK``, as it always has.
    """
    if isinstance(value, CommandResult):
        return value
    if value is False:
        return CommandResult(command, CommandOutcome.NOT_EXECUTED, "controller returned False")
    return CommandResult(command=command)


#: Why a drone link is unhealthy (``DroneLinkStatus.reason``). A bounded set
#: of codes, safe as a metric label; the human text is in ``detail``.
DRONE_LINK_REASONS = frozenset({
    "not_connected",
    "no_state",
    "state_stale",
    "command_failed",
    "unreadable",
    # An autopilot backend reports its preflight checks (GPS fix, home
    # position, required failsafes) as a link that is not ready for takeoff.
    "preflight",
    # The vehicle is under someone else's control (an RC pilot switched the
    # flight mode, or the autopilot's own failsafe took over).
    "external_control",
})


@dataclass(frozen=True)
class DroneLinkStatus:
    """Whether the controller can talk to its drone right now (read-only, sends nothing).

    A backend that can tell (the Tello controller) exposes it as
    ``link_status()``; a backend without one is not gated by it. The lane
    executor refuses a takeoff while ``ok`` is false and never gates a
    landing on it.

    * ``ok``: the drone link is healthy.
    * ``age_s``: seconds since the drone last reported state, or ``None``
      when that is unknown (no state ever arrived, or the backend cannot
      timestamp it).
    * ``reason``: ``None`` when healthy, else one of ``DRONE_LINK_REASONS``.
    * ``detail``: ``None`` when healthy, else a short operator-facing text
      such as ``Tello unreachable (no state for 12 s)``.
    """

    ok: bool
    age_s: float | None = None
    reason: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class FrameFreshness:
    """How current a controller's newest decoded video frame is (read-only, sends nothing).

    A backend that can tell (the Tello controller) exposes it as
    ``frame_freshness()``; the single-drone search refuses to start on a
    frame that is not current, because its obstacle guard would check the
    path on an old picture (``flightrisk/dashboard/single_search.py``).

    * ``decoded``: a frame has been decoded since the video stream started.
      False while the decoder still hands out its black placeholder.
    * ``age_s``: seconds since the newest frame was decoded (for a reader
      that stamps frames, since its first datagram arrived), on the
      monotonic clock; ``None`` when unknown.
    * ``stale_cause``: why the newest frame cannot be trusted (the bound
      reader's ``FrameStamp.stale_cause``, such as ``decoder backlog``), or
      ``None``.
    """

    decoded: bool
    age_s: float | None = None
    stale_cause: str | None = None


def require_executed(value: Any, command: str) -> CommandResult:
    """Normalize ``value`` like :func:`command_result`, raising if it was not executed.

    For callers whose next step assumes the command took effect, such as a
    land that a fail-safe path counts as done. Raises
    :class:`CommandNotExecutedError` for a non-OK result.
    """
    result = command_result(value, command)
    if not result.ok:
        raise CommandNotExecutedError(result)
    return result


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

    Neither ``land()`` nor ``takeoff()`` may ever report ``DROPPED_BUSY``: a
    busy command channel is waited for (or, for a land, interrupted), never
    a reason to drop the command. ``land()`` returns (``None`` or an ``OK``
    result) only once the land was executed and raises otherwise; it never
    returns a non-OK result. ``takeoff()`` returns ``True``/``None``/``OK``
    once airborne, and may return ``False`` or a non-OK result only when it
    deliberately sent nothing (for example because a landing is in
    progress); errors raise. Callers still treat any non-OK land result as
    a failed land (see ``require_executed``).
    """

    def takeoff(self) -> CommandResult | bool | None: ...
    def land(self) -> CommandResult | None: ...
    def hover(self) -> CommandResult | None: ...


@runtime_checkable
class Motion(Protocol):
    def move(self, direction: str, distance_cm: int) -> CommandResult | bool | None: ...
    def rotate(self, degrees: int) -> CommandResult | bool | None: ...
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
class CameraSource(Protocol):
    """Optional role: a backend that knows its cameras and where each one points.

    A backend that does not implement it is still a valid drone. The frame
    wrapper then falls back to the fixed mount's pitch, and to unknown.
    """

    def cameras(self) -> "tuple[CameraSpec, ...]": ...
    def camera_pitch_deg(self, camera_id: str) -> float | None: ...


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
    "DRONE_LINK_REASONS",
    "Capability",
    "CommandNotExecutedError",
    "CommandOutcome",
    "CommandRefusedError",
    "CommandResult",
    "DroneCapabilities",
    "DroneController",
    "DroneLinkStatus",
    "DroneState",
    "FlightControl",
    "FrameFreshness",
    "GpsDroneController",
    "Lifecycle",
    "Motion",
    "Navigation",
    "SettledHeight",
    "TelemetrySource",
    "VideoSource",
    "capabilities_of",
    "command_result",
    "require_executed",
]
