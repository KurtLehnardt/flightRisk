"""Deterministic simulated drone (dashboard backend name ``sim``).

``FakeDrone`` implements every role protocol without hardware, threads or
randomness, so it can drive the dashboard (``--source sim``), unit and
contract tests, and telemetry/metrics harnesses. Its telemetry, command
latency, dropped commands and failures are all configurable through
:class:`FakeDroneConfig`, which can also be built from plain string settings
(``--drone-opt key=value``) via :meth:`FakeDroneConfig.from_settings`.

Not to be confused with ``FakeTelloController`` in
:mod:`flightrisk.drone.tello`, the simulated Tello that the edge device flies
with ``python -m flightrisk.edge_device --source fake``. That one mimics the
Tello controller's own behaviour (moves take time, a land interrupts a move,
``takeoff(abort=...)``) for the fleet and lane-executor paths; this one is a
configurable backend for the dashboard and for testing the drone interfaces.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from typing import Any

import numpy as np

from flightrisk.drone.controller import (
    BASIC_FLIGHT,
    Capability,
    CommandOutcome,
    CommandResult,
    DroneCapabilities,
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
    TelemetrySnapshot,
    reading,
)

DEFAULT_HOST = "sim://drone"

_DIRECTIONS = frozenset({"forward", "back", "left", "right", "up", "down"})


class FakeDroneError(RuntimeError):
    """Raised by a FakeDrone command configured to fail."""


@dataclass(frozen=True)
class FakeDroneConfig:
    """Behaviour of a :class:`FakeDrone`. ``None`` telemetry means "no sensor"."""

    battery_pct: float | None = 100.0
    temperature_c: float | None = 40.0
    flight_time_s: float | None = 0.0
    takeoff_height_m: float = 1.0
    speed_mps: tuple[float, float, float] | None = (0.0, 0.0, 0.0)
    attitude_deg: tuple[float, float, float] | None = (0.0, 0.0, 0.0)
    flight_mode: str | None = None
    connect_ok: bool = True
    #: Seconds every command and connect() takes.
    latency_s: float = 0.0
    #: Commands that are always reported as dropped (busy), never executed.
    #: Fault injection: listing ``land`` or ``takeoff`` here makes the drone
    #: break the FlightControl contract (they must never be dropped), to
    #: test that callers still treat such a result as a failure.
    drop_commands: frozenset[str] = frozenset()
    #: Commands that raise FakeDroneError.
    fail_commands: frozenset[str] = frozenset()
    supports_return_to_home: bool = True
    frame_height: int = 480
    frame_width: int = 640

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> FakeDroneConfig:
        """Build a config from loosely typed settings, ignoring unknown keys.

        Accepts the strings a command line produces: numbers, ``true``/
        ``false``, ``none`` for "no sensor", comma-separated tuples and
        command-name sets.
        """
        kwargs: dict[str, Any] = {}
        defaults = cls()
        for f in fields(cls):
            if f.name not in settings:
                continue
            kwargs[f.name] = _coerce(settings[f.name], getattr(defaults, f.name))
        return cls(**kwargs)


def _coerce(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.lower() in ("none", "null", ""):
        return None
    if isinstance(default, bool):
        return text.lower() in ("1", "true", "yes", "on")
    if isinstance(default, frozenset):
        return frozenset(part.strip() for part in text.split(",") if part.strip())
    if isinstance(default, tuple):
        return tuple(float(part) for part in text.split(","))
    if isinstance(default, int) and not isinstance(default, bool):
        return int(text)
    if isinstance(default, float) or default is None:
        try:
            return float(text)
        except ValueError:
            return text
    return text


class FakeDrone:
    """Simulated drone implementing every role protocol deterministically."""

    def __init__(
        self,
        name: str = "sim-1",
        host: str = DEFAULT_HOST,
        config: FakeDroneConfig | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.name = name
        self.host = host
        self.config = config or FakeDroneConfig()
        self._clock = clock
        self._sleep = sleep
        features = set(BASIC_FLIGHT)
        if self.config.supports_return_to_home:
            features.add(Capability.RETURN_TO_HOME)
        self.capabilities = DroneCapabilities(
            has_gps=False,
            has_rtsp=False,
            min_move_cm=20,
            max_move_cm=500,
            max_altitude_m=10,
            supports_missions=False,
            features=frozenset(features),
        )
        self.state = DroneState()
        #: Every command received, in order: (command, *args).
        self.calls: list[tuple] = []
        self._busy = False
        self._height_m = 0.0
        self._yaw_deg = 0.0
        self._measured_at: float | None = None
        self._frame_callbacks: list[Callable[[np.ndarray], None]] = []
        self._sync_state()

    # --- test / harness controls ---

    def set_busy(self, busy: bool) -> None:
        """Simulate another command holding the command channel.

        While busy, move, rotate, hover, rc_control and return_to_home are
        dropped (``DROPPED_BUSY``). Land and takeoff are not: per the
        FlightControl contract a land interrupts whatever holds the channel
        and a takeoff waits for it, which in this simulation takes no time.
        """
        self._busy = busy

    def set_telemetry(self, **changes: Any) -> None:
        """Change simulated sensor values (FakeDroneConfig field names)."""
        self.config = replace(self.config, **changes)
        if self.state.is_connected:
            self._measured_at = self._clock()
        self._sync_state()

    # --- Lifecycle ---

    def connect(self) -> bool:
        self._delay()
        self.calls.append(("connect",))
        if not self.config.connect_ok:
            return False
        self.state.is_connected = True
        self._measured_at = self._clock()
        self._sync_state()
        return True

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))
        self.state.is_connected = False
        self.state.is_flying = False
        self._height_m = 0.0
        self._sync_state()

    # --- TelemetrySource ---

    def snapshot(self) -> TelemetrySnapshot:
        cfg = self.config
        at = self._measured_at
        speed = cfg.speed_mps
        attitude = cfg.attitude_deg
        vx = vy = vz = ground = None
        if speed is not None:
            vx = reading(speed[0], METRE_PER_SECOND, "sim.vx", at)
            vy = reading(speed[1], METRE_PER_SECOND, "sim.vy", at)
            vz = reading(speed[2], METRE_PER_SECOND, "sim.vz", at)
            ground = reading(float(np.hypot(speed[0], speed[1])), METRE_PER_SECOND, "sim.ground_speed", at)
        pitch = roll = None
        if attitude is not None:
            pitch = reading(attitude[0], DEGREE, "sim.pitch", at)
            roll = reading(attitude[1], DEGREE, "sim.roll", at)
        return TelemetrySnapshot(
            backend="sim",
            drone_id=self.name,
            captured_at=self._clock(),
            connected=self.state.is_connected,
            is_flying=self.state.is_flying,
            is_flying_source=IS_FLYING_COMMANDED,
            flight_mode=cfg.flight_mode,
            battery_pct=reading(cfg.battery_pct, PERCENT, "sim.battery", at),
            height_m=reading(self._height_m, METRE, "sim.height", at),
            temperature_c=reading(cfg.temperature_c, CELSIUS, "sim.temperature", at),
            flight_time_s=reading(cfg.flight_time_s, SECOND, "sim.flight_time", at),
            vx_mps=vx,
            vy_mps=vy,
            vz_mps=vz,
            ground_speed_mps=ground,
            pitch_deg=pitch,
            roll_deg=roll,
            yaw_deg=reading(self._yaw_deg, DEGREE, "sim.yaw", at) if attitude is not None else None,
        )

    # --- VideoSource ---

    def get_frame(self) -> np.ndarray | None:
        if not self.state.is_connected:
            return None
        frame = np.full((self.config.frame_height, self.config.frame_width, 3), 96, dtype=np.uint8)
        for cb in self._frame_callbacks:
            cb(frame)
        return frame

    def on_frame(self, cb: Callable[[np.ndarray], None]) -> None:
        self._frame_callbacks.append(cb)

    # --- FlightControl ---

    def takeoff(self, abort: threading.Event | None = None) -> CommandResult:
        """Take off unless ``abort`` is set (checked before anything happens, like the Tello)."""
        if abort is not None and abort.is_set():
            self.calls.append(("takeoff",))
            return CommandResult("takeoff", CommandOutcome.NOT_EXECUTED, "abort requested")
        return self._command("takeoff", (), self._do_takeoff, waits_for_channel=True)

    def land(self) -> CommandResult:
        return self._command("land", (), self._do_land, waits_for_channel=True)

    def hover(self) -> CommandResult:
        return self._command("hover", (), None)

    # --- Motion ---

    def move(self, direction: str, distance_cm: int) -> CommandResult:
        if direction not in _DIRECTIONS:
            self.calls.append(("move", direction, distance_cm))
            return CommandResult("move", CommandOutcome.REJECTED, "unknown direction")
        distance_cm = max(self.capabilities.min_move_cm, min(self.capabilities.max_move_cm, distance_cm))
        return self._command("move", (direction, distance_cm), lambda: self._do_move(direction, distance_cm))

    def rotate(self, degrees: int) -> CommandResult:
        return self._command("rotate", (degrees,), lambda: self._do_rotate(degrees))

    def rc_control(self, lr: int, fb: int, ud: int, yaw: int) -> CommandResult:
        return self._command("rc_control", (lr, fb, ud, yaw), None)

    # --- Navigation ---

    def return_to_home(self) -> CommandResult:
        if not self.capabilities.supports(Capability.RETURN_TO_HOME):
            self.calls.append(("return_to_home",))
            return CommandResult("return_to_home", CommandOutcome.REJECTED, "not supported by this configuration")
        return self._command("return_to_home", (), None)

    # --- internals ---

    def _delay(self) -> None:
        if self.config.latency_s > 0:
            self._sleep(self.config.latency_s)

    def _command(
        self,
        name: str,
        args: tuple,
        action: Callable[[], None] | None,
        waits_for_channel: bool = False,
    ) -> CommandResult:
        self._delay()
        self.calls.append((name, *args))
        if name in self.config.fail_commands:
            raise FakeDroneError(f"simulated failure: {name}")
        busy = self._busy and not waits_for_channel
        if busy or name in self.config.drop_commands:
            return CommandResult(name, CommandOutcome.DROPPED_BUSY, "another command in flight")
        if action is not None:
            action()
            if self.state.is_connected:
                self._measured_at = self._clock()
        self._sync_state()
        return CommandResult(name)

    def _do_takeoff(self) -> None:
        self.state.is_flying = True
        self._height_m = self.config.takeoff_height_m

    def _do_land(self) -> None:
        self.state.is_flying = False
        self._height_m = 0.0

    def _do_move(self, direction: str, distance_cm: int) -> None:
        if direction == "up":
            self._height_m += distance_cm / 100.0
        elif direction == "down":
            self._height_m = max(0.0, self._height_m - distance_cm / 100.0)

    def _do_rotate(self, degrees: int) -> None:
        self._yaw_deg = (self._yaw_deg + degrees) % 360

    def _sync_state(self) -> None:
        """Mirror the simulated values into the legacy DroneState cache."""
        cfg = self.config
        measured = self._measured_at is not None

        def value(v: float | None) -> float:
            return v if (measured and v is not None) else 0

        self.state.battery = value(cfg.battery_pct)
        self.state.height = int(round(self._height_m * 100)) if measured else 0
        self.state.temperature = value(cfg.temperature_c)
        self.state.flight_time = value(cfg.flight_time_s)
        self.state.flight_mode = cfg.flight_mode


def create(drone_id: str, host: str, options: Mapping[str, Any]) -> FakeDrone:
    """Registry factory for the ``sim`` backend."""
    config = options.get("config")
    if not isinstance(config, FakeDroneConfig):
        config = FakeDroneConfig.from_settings(options)
    return FakeDrone(name=drone_id, host=host, config=config)


__all__ = ["DEFAULT_HOST", "FakeDrone", "FakeDroneConfig", "FakeDroneError", "create"]
