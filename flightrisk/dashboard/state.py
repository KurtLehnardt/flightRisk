"""Typed state container for the Amber dashboard.

Replaces the untyped ``_state = {}`` dict with a ``@dataclass`` that
provides IDE auto-complete, static type checking, and explicit
``threading.Lock`` instances for cross-thread mutations.

Thread-safety notes
-------------------
The ``match_history_lock`` guards ``match_history`` across the frame
loop, Gemma worker, and Flask/SocketIO request threads.  The
``gemma_thread_lock`` serialises Gemma worker startup.  Fleet swaps
are already atomic under the GIL (single-pointer reassignment), but
callers that iterate ``fleet`` members should hold ``fleet_lock`` if
they might race with ``disconnect_all`` / ``deregister``.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SourceConfig:
    """Video/telemetry source configuration.

    Groups the source-selection parameters that used to be passed as five
    separate args to ``run_dashboard()`` / ``_init_pipeline()``.

    ``source`` is either a registered drone backend name (see
    ``flightrisk.drone.registry``) or one of the non-drone sources ``file``
    and ``edge``. Backend-specific settings belong in ``drone_options``; the
    dashboard passes them, unread, to the backend's registry entry.
    """
    source: str = "tello"
    # Deprecated: MAVLink-specific settings kept as fields for backward
    # compatibility with existing callers and the --mavlink-address /
    # --rtsp-url flags. They reach the backend through drone_settings() like
    # any other option; the dashboard itself no longer reads them.
    mavlink_address: str = "udp://:14540"
    rtsp_url: str | None = None
    edge_ws: str = "ws://localhost:9000"
    video_path: str | None = None
    # -- Edge / ground-station source mode --
    # The ground station runs a WebSocket *server* that edge devices connect
    # to. ``edge_host``/``edge_port`` are the server bind address (distinct
    # from ``edge_ws``, a legacy client-side URL kept for back-compat).
    # Defaults to loopback so ``GroundTransport``'s secure-by-default guard
    # (which refuses a non-loopback bind without a token) is satisfied.
    stream_video: bool = False
    edge_host: str = "127.0.0.1"
    edge_port: int = 9000
    # -- Privacy and reasoning --
    # ``appearance_only`` set to True never constructs face recognition and
    # disables the face signals. ``None`` falls back to
    # ``VisionConfig.appearance_only``; the ``python -m flightrisk.dashboard``
    # launcher passes True unless the operator asks for face matching.
    # ``enable_llm`` set to False never constructs the Gemma reasoning agent
    # and never starts its worker thread (``--no-llm``).
    appearance_only: bool | None = None
    enable_llm: bool = True
    # -- Multi-drone fleet (``source == "edge"`` only) --
    # A non-empty ``fleet_drones`` makes the edge source build a
    # ``FleetCoordinator`` with one link per drone id. The search area is
    # ``fleet_area_cm`` (width, depth) and ``fleet_strip_cm`` is the lane
    # spacing. ``fleet_net`` is the network path in use (``station`` or
    # ``ap``), reported in the dashboard status.
    fleet_drones: tuple[str, ...] = ()
    fleet_area_cm: tuple[int, int] = (400, 400)
    fleet_strip_cm: int = 75
    fleet_net: str = "station"
    # -- Reasoning backend: "claude" | "gemma" | "off"; None = config default
    # (FLIGHTRISK_REASONING_PROVIDER, else "gemma"; claude needs the cloud
    # opt-in). ``--no-llm`` passes "off" here as well as ``enable_llm=False``.
    reasoning_provider: str | None = None
    # -- Per-backend settings (``--drone-opt key=value``), interpreted only by
    # the selected backend's registry entry.
    drone_options: dict[str, Any] = field(default_factory=dict)

    def drone_settings(self) -> dict[str, Any]:
        """Settings mapping handed to the drone backend's ``options_from``."""
        settings: dict[str, Any] = {
            "mavlink_address": self.mavlink_address,
            "rtsp_url": self.rtsp_url,
        }
        settings.update(self.drone_options)
        return settings


@dataclass
class AppState:
    """Central mutable state for the dashboard.

    Every field that was previously a key in ``_state: dict`` is now an
    explicit attribute.  Optional heavyweight components default to
    ``None``; scalar flags default to their zero-value.
    """

    # -- Drone / fleet --
    fleet: Any = None
    auto_connect_stop: threading.Event | None = None

    # -- Edge / ground-station (source == "edge") --
    ground_transport: Any = None  # GroundTransportSync (WebSocket server)
    ground_station: Any = None  # GroundStation (scorer over edge detections)
    stream_video: bool = False  # whether edge devices should stream full frames
    fleet_supervisor: Any = None  # FleetSupervisor (edge source with a fleet)

    # -- Privacy mode (set by _init_pipeline) --
    appearance_only: bool = False

    # -- Vision pipeline components (lazy-init) --
    detector: Any = None
    reid: Any = None
    face: Any = None
    scorer: Any = None
    tracker: Any = None
    reasoning: Any = None

    # -- Source configuration --
    source_config: SourceConfig | None = None
    source: str | None = None
    edge_ws: str | None = None
    video_path: str | None = None

    # -- Video capture (file) --
    cap: Any = None

    # -- Runtime flags --
    running: bool = False
    search_active: bool = False
    battery_warned: bool = False
    # True once the critical-battery land was executed (or raised). A land
    # that was not executed leaves it False and sets
    # battery_critical_retry_at (time.monotonic()) for the next attempt.
    battery_critical: bool = False
    battery_critical_retry_at: float = 0.0

    # -- Target --
    target_photo: str | None = None
    target_photo_path: str | None = None
    target_description: str | None = None

    # -- Detection stats --
    match_history: list = field(default_factory=list)
    drone_telemetry: dict = field(default_factory=dict)
    fps: float = 0
    persons_detected: int = 0

    # -- Session / persistence --
    recorder: Any = None
    logger: Any = None
    metrics: Any = None
    db: Any = None
    session_id: str | None = None

    # -- Observability --
    tracer: Any = None
    otel_metrics: Any = None

    # -- Gemma worker --
    gemma_thread: threading.Thread | None = None

    # -- Obstacle avoidance --
    obstacle_guard: Any = None

    # -- Target canon --
    canon: Any = None

    # -- Configurable signal registry --
    signal_registry: Any = None


# ---------------------------------------------------------------------------
# Module-level singleton + locks
# ---------------------------------------------------------------------------

#: The single ``AppState`` instance shared across all dashboard threads.
app_state = AppState()

#: Guards ``app_state.match_history`` across threads.
match_history_lock = threading.Lock()

#: Serialises Gemma worker thread startup.
gemma_thread_lock = threading.Lock()

#: Guards fleet swap operations (register/deregister/disconnect_all).
fleet_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Gemma async queue and rate-limit state
# ---------------------------------------------------------------------------

#: Queue for Gemma reasoning work items.
gemma_queue: queue.Queue = queue.Queue(maxsize=10)

#: track_key -> last_call_timestamp (rate-limit per spatial track).
gemma_last_call: dict[str, float] = {}

#: track_key -> timestamp of last alert emit (cooldown per spatial track).
alerted_tracks: dict[str, float] = {}

#: Seconds before re-alerting for the same spatial track.
ALERT_COOLDOWN = 10.0

#: Seconds between Gemma calls for the same track.
GEMMA_RATE_LIMIT = 5.0


# ---------------------------------------------------------------------------
# Backward-compatible dict-like access layer
#
# Existing code (including tests) uses ``_state[key]`` / ``_state.get(key)``
# / ``_state["key"] = val``.  This thin wrapper delegates to ``app_state``
# attributes so callers can migrate incrementally.
# ---------------------------------------------------------------------------

class _StateDictCompat:
    """Dict-like facade over :data:`app_state` for backward compatibility.

    Supports ``__getitem__``, ``__setitem__``, ``__contains__``, and
    ``.get()`` so that ``_state["foo"]`` keeps working during the
    migration from the old ``dict`` to the typed ``AppState``.
    """

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(app_state, key)
        except AttributeError:
            raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(app_state, key, value)

    def __contains__(self, key: str) -> bool:
        return hasattr(app_state, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(app_state, key, default)

    def update(self, mapping: dict) -> None:
        """Bulk-set attributes on ``app_state``."""
        for key, value in mapping.items():
            setattr(app_state, key, value)

    def clear(self) -> None:
        """Reset ``app_state`` to a fresh ``AppState()``."""
        fresh = AppState()
        import dataclasses
        for f in dataclasses.fields(AppState):
            setattr(app_state, f.name, getattr(fresh, f.name))

    def copy(self) -> dict:
        """Return a plain-dict snapshot of ``app_state`` fields."""
        import dataclasses
        return {f.name: getattr(app_state, f.name) for f in dataclasses.fields(AppState)}


#: Backward-compatible dict-like accessor for ``app_state``.
_state = _StateDictCompat()
