"""OpenTelemetry metric instruments for the FlightRisk dashboards.

Every instrument is created once, at import, from the global meter. Before
``init_telemetry`` installs a ``MeterProvider`` (and whenever
``FLIGHTRISK_OTEL_ENABLED`` is off) the OpenTelemetry API hands out proxy
instruments that do nothing, so recording costs a function call and no
caller needs to check whether telemetry is on.

Label values are bounded: drone ids, node, backend, component, outcome,
message type, level, stage and signal. Alert ids, track ids, file names and
free text are never labels. Prometheus names are the OTel names with dots
turned into underscores (the collector runs with ``add_metric_suffixes:
false``), for example ``flightrisk.drone.commands`` becomes
``flightrisk_drone_commands``. ``observability/README.md`` lists them all.

Values that must age (telemetry age, link age, time since the last frame)
are observable gauges computed when the exporter collects, so a drone that
stops reporting shows a growing age instead of a frozen one.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

from flightrisk.obs_log import obs_enter as _obs_enter
from flightrisk.obs_log import obs_exit as _obs_exit
from flightrisk.obs_redact import safe_token

try:
    from opentelemetry import metrics as _otel_metrics
    from opentelemetry.metrics import CallbackOptions, Observation
except ImportError:  # pragma: no cover - opentelemetry is a hard requirement
    _otel_metrics = None
    CallbackOptions = Any  # type: ignore[assignment,misc]
    Observation = None  # type: ignore[assignment]

try:
    import psutil as _psutil
except ImportError:  # pragma: no cover - optional
    _psutil = None

#: Buckets (ms) for every latency histogram.
LATENCY_BUCKETS_MS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000]
#: Buckets for scores in [0, 1].
SCORE_BUCKETS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 0.9, 1.0]
#: Buckets for counts per frame.
COUNT_BUCKETS = [0, 1, 2, 3, 5, 8, 13, 20, 50]
#: Buckets (s) for operator response time to an alert.
RESPONSE_BUCKETS_S = [1, 2, 5, 10, 20, 30, 60, 120, 300, 600]
#: Buckets for the obstacle depth ratio (centre depth / min_safe_depth; above 1 is blocked).
DEPTH_RATIO_BUCKETS = [0.25, 0.5, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 2.0, 3.0]

#: Histogram name -> bucket boundaries, applied as SDK views in telemetry.py.
HISTOGRAM_BUCKETS: dict[str, list[float]] = {
    "flightrisk.http.duration_ms": LATENCY_BUCKETS_MS,
    "flightrisk.socket.duration_ms": LATENCY_BUCKETS_MS,
    "flightrisk.stage.duration_ms": LATENCY_BUCKETS_MS,
    "flightrisk.drone.command_duration_ms": LATENCY_BUCKETS_MS,
    "flightrisk.drone.land_duration_ms": LATENCY_BUCKETS_MS,
    "flightrisk.reasoning.duration_ms": LATENCY_BUCKETS_MS,
    "flightrisk.frame.duration_ms": LATENCY_BUCKETS_MS,
    "flightrisk.comm.rtt_ms": LATENCY_BUCKETS_MS,
    "flightrisk.signal.score": SCORE_BUCKETS,
    "flightrisk.match.score": SCORE_BUCKETS,
    "flightrisk.detections.per_frame": COUNT_BUCKETS,
    "flightrisk.alert.response_s": RESPONSE_BUCKETS_S,
    "flightrisk.obstacle.depth_ratio": DEPTH_RATIO_BUCKETS,
}

_STARTED = time.time()


class _Noop:
    def add(self, *a, **k):
        pass

    def record(self, *a, **k):
        pass

    def set(self, *a, **k):
        pass


def _meter():
    if _otel_metrics is None:  # pragma: no cover
        return None
    return _otel_metrics.get_meter("flightrisk.obs")


_m = _meter()


class GuardedInstrument:
    """An instrument whose updates are skipped when they arrive re-entrantly.

    The SDK updates an instrument under non-reentrant locks. A signal handler
    that interrupts this thread inside one of those updates and then records a
    metric itself (the edge's Ctrl+C handler logs a flight-safety event before
    it lands) would wait for a lock its own thread holds. Such a nested update
    is dropped instead (see :func:`flightrisk.obs_log.obs_enter`).
    """

    __slots__ = ("_inner",)

    def __init__(self, inner):
        self._inner = inner

    def add(self, *args, **kwargs):
        if not _obs_enter():
            return
        try:
            self._inner.add(*args, **kwargs)
        finally:
            _obs_exit()

    def record(self, *args, **kwargs):
        if not _obs_enter():
            return
        try:
            self._inner.record(*args, **kwargs)
        finally:
            _obs_exit()

    def set(self, *args, **kwargs):
        if not _obs_enter():
            return
        try:
            self._inner.set(*args, **kwargs)
        finally:
            _obs_exit()


def _counter(name: str, unit: str = "1", description: str = ""):
    return GuardedInstrument(_m.create_counter(name, unit=unit, description=description)) if _m else _Noop()


def _hist(name: str, unit: str = "1", description: str = ""):
    return GuardedInstrument(_m.create_histogram(name, unit=unit, description=description)) if _m else _Noop()


def _gauge(name: str, unit: str = "1", description: str = ""):
    return GuardedInstrument(_m.create_gauge(name, unit=unit, description=description)) if _m else _Noop()


def _observable(name: str, callback: Callable, unit: str = "1", description: str = "") -> None:
    if _m:
        _m.create_observable_gauge(name, callbacks=[callback], unit=unit, description=description)


# --- RED: dashboard HTTP and Socket.IO handlers -----------------------------
http_requests = _counter("flightrisk.http.requests", description="Dashboard REST requests by route, method, status class and outcome")
http_duration = _hist("flightrisk.http.duration_ms", "ms", "Dashboard REST request duration")
socket_events = _counter("flightrisk.socket.events", description="Socket.IO events from the browser by event and outcome")
socket_duration = _hist("flightrisk.socket.duration_ms", "ms", "Socket.IO handler duration")
socket_emits = _counter("flightrisk.socket.emits", description="Socket.IO events sent to the browser by event")

# --- Detection and matching pipeline ----------------------------------------
stage_duration = _hist("flightrisk.stage.duration_ms", "ms", "Frame pipeline stage duration (detect, track, reid, face, score, encode, emit)")
frames = _counter("flightrisk.frames", description="Frames processed by the frame loop, by source")
detections_per_frame = _hist("flightrisk.detections.per_frame", description="Persons detected per frame")
signal_score = _hist("flightrisk.signal.score", description="Per-signal match score of the best candidate (0..1)")
alert_levels = _counter("flightrisk.alert.levels", description="Scored candidates by alert level")
model_loads = _counter("flightrisk.model.loads", description="Model and signal provider loads by model and outcome")

# --- Reasoning (metadata only, never text) ----------------------------------
reasoning_calls = _counter("flightrisk.reasoning.calls", description="Reasoning calls by provider, kind and outcome")
reasoning_tokens = _counter("flightrisk.reasoning.tokens", description="Reasoning tokens by provider and direction (input/output)")
# The duration histogram keeps its PR #88 name; labels were added.
reasoning_duration = _hist("flightrisk.reasoning.duration_ms", "ms", "Reasoning call latency")

# --- Queues -----------------------------------------------------------------
queue_dropped = _counter("flightrisk.queue.dropped", description="Items dropped because a bounded queue was full, by queue")

# --- Alerts -----------------------------------------------------------------
alert_transitions = _counter("flightrisk.alert.transitions", description="Alert latch transitions (opened, renotified, confirmed, dismissed, aborted, reset)")
alert_response = _hist("flightrisk.alert.response_s", "s", "Seconds from alert to operator confirm/dismiss")

# --- Errors -----------------------------------------------------------------
error_logs = _counter("flightrisk.error.logs", description="ERROR and CRITICAL log records by component, error type and fingerprint")

# --- Drones and flight safety -----------------------------------------------
drone_battery = _gauge("flightrisk.drone.battery_pct", "%", "Battery by drone")
drone_height = _gauge("flightrisk.drone.height_m", "m", "Height above takeoff by drone and source (e.g. tello.h, tello.tof, sim.height, edge.telemetry)")
drone_speed = _gauge("flightrisk.drone.speed_mps", "m/s", "Speed by drone, axis (ground, vx, vy, vz) and source")
drone_attitude = _gauge("flightrisk.drone.attitude_deg", "deg", "Attitude by drone and axis (pitch, roll, yaw)")
drone_flying = _gauge("flightrisk.drone.flying", description="1 while the drone reports flying")
drone_commands = _counter("flightrisk.drone.commands", description="Drone commands by drone, backend, command and outcome (ok, dropped_busy, not_executed, rejected, error)")
drone_command_duration = _hist("flightrisk.drone.command_duration_ms", "ms", "Drone command duration by backend, command and outcome")
land_duration = _hist("flightrisk.drone.land_duration_ms", "ms", "Land command duration until confirmed or failed")
flight_safety_events = _counter("flightrisk.flight_safety.events", description="Flight-safety events by kind and reason (battery, link loss, watchdog land, geofence, obstacle, land failures)")
obstacle_checks = _counter("flightrisk.obstacle.checks", description="Obstacle guard checks by action (clear, go_left, go_right, reverse, unavailable, no_frame)")
obstacle_depth_ratio = _hist("flightrisk.obstacle.depth_ratio", description="Obstacle guard centre depth over min_safe_depth (relative MiDaS units; above 1 the path is blocked)")

# --- Drone communication and fleet link -------------------------------------
comm_messages = _counter("flightrisk.comm.messages", description="Ground/edge messages by side, direction, type and drone")
comm_bytes = _counter("flightrisk.comm.bytes", "By", "Ground/edge message bytes by side and direction")
comm_rtt = _hist("flightrisk.comm.rtt_ms", "ms", "Clock-sync round-trip time by drone")
comm_acks = _counter("flightrisk.comm.acks", description="Edge acks and nacks by command and reason")
comm_dropped = _counter("flightrisk.comm.dropped", description="Messages not delivered by side, type and reason")
link_reconnects = _counter("flightrisk.link.reconnects", description="Edge reconnect attempts by outcome")
clock_offset = _gauge("flightrisk.link.clock_offset_ms", "ms", "Edge clock offset by drone")
fleet_epoch = _gauge("flightrisk.fleet.epoch", description="Leader epoch an edge adopted, by drone")
start_skew = _gauge("flightrisk.fleet.start_skew_ms", "ms", "Spread of start_at execution across the fleet")

# ---------------------------------------------------------------------------
# Observable state (read when the exporter collects)
# ---------------------------------------------------------------------------

# Re-entrant: a signal handler may record a metric while this thread is inside a note_*().
_state_lock = threading.RLock()

#: At most this many distinct drone ids become metric labels from network input;
#: later ones are labelled "other". A fleet is a handful of drones; without a
#: limit a client that sends random drone ids would create series without end.
MAX_DRONE_LABELS = 64
_drone_labels: set[str] = set()


def drone_label(value: Any, default: str = "unknown") -> str:
    """A bounded metric label for a drone id that came off the network."""
    token = safe_token(value, default)
    if token == default:
        return token
    with _state_lock:
        if token in _drone_labels:
            return token
        if len(_drone_labels) >= MAX_DRONE_LABELS:
            return "other"
        _drone_labels.add(token)
    return token
_telemetry_seen: dict[tuple[str, str], float] = {}  # (drone_id, backend) -> measured_at (epoch s)
_link_seen: dict[tuple[str, str], float] = {}       # (drone_id, side) -> last message (monotonic s)
_link_connected: dict[str, bool] = {}
_last_frame_at: list[float | None] = [None]
_frame_source_active: list[bool] = [False]
_queues: dict[str, Callable[[], int]] = {}
_alert_phase: list[str] = ["searching"]
ALERT_PHASES = ("searching", "awaiting_response", "resolved")


def note_telemetry(drone_id: str, backend: str, measured_at: float | None) -> None:
    """Remember when ``drone_id`` last had a measured reading (epoch seconds)."""
    if measured_at is None:
        return
    with _state_lock:
        _telemetry_seen[(drone_id, backend)] = measured_at


def note_link(drone_id: str, side: str, connected: bool | None = None) -> None:
    """Remember that a message from ``drone_id``'s link arrived just now."""
    with _state_lock:
        _link_seen[(drone_id, side)] = time.monotonic()
        if connected is not None:
            _link_connected[drone_id] = connected


def note_link_state(drone_id: str, connected: bool) -> None:
    with _state_lock:
        _link_connected[drone_id] = connected


def note_frame(active: bool = True) -> None:
    """Called once per processed frame (cheap)."""
    _last_frame_at[0] = time.monotonic()
    _frame_source_active[0] = active


def set_frame_source_active(active: bool) -> None:
    _frame_source_active[0] = active


def register_queue(name: str, depth: Callable[[], int]) -> None:
    """Export ``depth()`` as ``flightrisk_queue_depth{queue=name}``."""
    with _state_lock:
        _queues[name] = depth


def set_alert_phase(phase: str) -> None:
    _alert_phase[0] = phase


def _observe_telemetry_age(options: CallbackOptions):
    now = time.time()
    with _state_lock:
        items = list(_telemetry_seen.items())
    for (drone_id, backend), at in items:
        yield Observation(max(0.0, now - at), {"drone_id": drone_id, "backend": backend})


def _observe_link_age(options: CallbackOptions):
    now = time.monotonic()
    with _state_lock:
        items = list(_link_seen.items())
    for (drone_id, side), at in items:
        yield Observation(max(0.0, now - at), {"drone_id": drone_id, "side": side})


def _observe_link_connected(options: CallbackOptions):
    with _state_lock:
        items = list(_link_connected.items())
    for drone_id, connected in items:
        yield Observation(1 if connected else 0, {"drone_id": drone_id})


def _observe_frame_age(options: CallbackOptions):
    at = _last_frame_at[0]
    if at is None:
        return
    yield Observation(time.monotonic() - at, {"source_active": str(bool(_frame_source_active[0])).lower()})


def _observe_queues(options: CallbackOptions):
    with _state_lock:
        items = list(_queues.items())
    from flightrisk import obs_log

    yield Observation(obs_log.console_queue_depth(), {"queue": "log_console"})
    for name, depth in items:
        try:
            yield Observation(int(depth()), {"queue": name})
        except Exception:  # noqa: BLE001, S112 -- a broken probe must not stop the export
            continue


def _observe_alert_phase(options: CallbackOptions):
    current = _alert_phase[0]
    for phase in ALERT_PHASES:
        yield Observation(1 if phase == current else 0, {"phase": phase})


class _ProcessSampler:
    """CPU, memory, threads and open file descriptors of this process (USE)."""

    def __init__(self) -> None:
        self._proc = _psutil.Process() if _psutil is not None else None
        self._last_cpu = (time.monotonic(), sum(os.times()[:2]))

    def cpu_percent(self) -> float:
        if self._proc is not None:
            try:
                return float(self._proc.cpu_percent(None))
            except Exception:  # noqa: BLE001, S110
                pass
        now, cpu = time.monotonic(), sum(os.times()[:2])
        last_t, last_cpu = self._last_cpu
        self._last_cpu = (now, cpu)
        return 100.0 * (cpu - last_cpu) / max(1e-6, now - last_t)

    def rss_bytes(self) -> float:
        if self._proc is not None:
            try:
                return float(self._proc.memory_info().rss)
            except Exception:  # noqa: BLE001, S110
                pass
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return float(peak if os.uname().sysname == "Darwin" else peak * 1024)

    def threads(self) -> int:
        return threading.active_count()

    def open_fds(self) -> int:
        if self._proc is not None and hasattr(self._proc, "num_fds"):
            try:
                return int(self._proc.num_fds())
            except Exception:  # noqa: BLE001, S110
                pass
        for path in ("/proc/self/fd", "/dev/fd"):
            try:
                return len(os.listdir(path))
            except OSError:
                continue
        return 0


_sampler = _ProcessSampler()


def _observe_process(options: CallbackOptions):
    yield Observation(_sampler.cpu_percent(), {"resource": "cpu_percent"})
    yield Observation(_sampler.rss_bytes(), {"resource": "memory_rss_bytes"})
    yield Observation(_sampler.threads(), {"resource": "threads"})
    yield Observation(_sampler.open_fds(), {"resource": "open_fds"})


def _observe_uptime(options: CallbackOptions):
    yield Observation(time.time() - _STARTED)


def _observe_log_dropped(options: CallbackOptions):
    from flightrisk import obs_log

    yield Observation(obs_log.console_dropped())


_observable("flightrisk.drone.telemetry_age_s", _observe_telemetry_age, "s", "Age of the newest measured telemetry by drone")
_observable("flightrisk.link.age_s", _observe_link_age, "s", "Seconds since the last message on a drone's link, by side")
_observable("flightrisk.link.connected", _observe_link_connected, description="1 while a drone's edge link is connected")
_observable("flightrisk.frame_loop.last_frame_age_s", _observe_frame_age, "s", "Seconds since the frame loop last processed a frame")
_observable("flightrisk.queue.depth", _observe_queues, description="Depth of bounded queues (reasoning, log console, transport dispatchers)")
_observable("flightrisk.alert.phase", _observe_alert_phase, description="1 for the alert latch's current phase")
_observable("flightrisk.process.usage", _observe_process, description="Process CPU %, RSS bytes, threads and open fds")
_observable("flightrisk.process.uptime_s", _observe_uptime, "s", "Seconds since the process started")
_observable("flightrisk.log.dropped", _observe_log_dropped, description="Console log records dropped because the log queue was full")


def _error_hook(component: str, error_type: str, fingerprint: str, level: str) -> None:
    error_logs.add(1, {"component": safe_token(component), "error_type": safe_token(error_type), "fingerprint": fingerprint, "level": level})


def _install_error_hook() -> None:
    from flightrisk import obs_log

    obs_log.set_error_hook(_error_hook)


_install_error_hook()


__all__ = [
    "HISTOGRAM_BUCKETS",
    "MAX_DRONE_LABELS",
    "drone_label",
    "note_frame",
    "note_link",
    "note_link_state",
    "note_telemetry",
    "register_queue",
    "set_alert_phase",
    "set_frame_source_active",
]
