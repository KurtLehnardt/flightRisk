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
    """
    source: str = "webcam"
    mavlink_address: str = "udp://:14540"
    rtsp_url: str | None = None
    edge_ws: str = "ws://localhost:9000"
    video_path: str | None = None


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
    mavlink_address: str | None = None
    rtsp_url: str | None = None
    edge_ws: str | None = None
    video_path: str | None = None

    # -- Video capture (webcam / file) --
    cap: Any = None

    # -- Runtime flags --
    running: bool = False
    search_active: bool = False
    battery_warned: bool = False
    battery_critical: bool = False

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


#: Backward-compatible dict-like accessor for ``app_state``.
_state = _StateDictCompat()
