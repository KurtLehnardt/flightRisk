"""The FlightRisk telemetry event catalog: one helper per event family.

Each helper writes one structured log event (stable ``event`` name, a
``category``, documented fields; see ``docs/observability-events.md``) and
updates the matching metrics in :mod:`flightrisk.obs_metrics`. They are
called at the existing choke points (dashboard handlers, the frame loop, the
alert latch, recovery, the drone backends and the transport), never raise,
and never block: logging goes through the non-blocking pipeline in
:mod:`flightrisk.obs_log`, and per-frame data is sampled.

Families: ``user_input``, ``target_image``, ``error``, ``drone_comm`` (in
:mod:`flightrisk.obs_comm`), ``drone_telemetry_snapshot``, ``alert``,
``flight_safety``, ``detection_pipeline`` and ``reasoning``.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import threading
import time
from collections.abc import Mapping
from typing import Any

from flightrisk import obs_metrics as M
from flightrisk.obs_log import Throttle, log_event
from flightrisk.obs_redact import content_capture_enabled, safe_token, sanitize

_log = logging.getLogger("flightrisk.events")

#: Seconds between two ``drone_telemetry_snapshot`` log events per drone.
SNAPSHOT_LOG_INTERVAL_S = 5.0
#: Seconds between two ``detection_pipeline`` summary events.
PIPELINE_LOG_INTERVAL_S = 5.0

_snapshot_throttle = Throttle(SNAPSHOT_LOG_INTERVAL_S)
_safety_throttle = Throttle(10.0)


def _safe(fn):
    """Telemetry must never take the caller down."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            logging.getLogger("flightrisk.obs").debug("obs_helper_failed %s", fn.__name__, exc_info=True)
            return None

    return wrapper


# ---------------------------------------------------------------------------
# user_input
# ---------------------------------------------------------------------------

#: Socket events that only read state; logged at DEBUG.
QUERY_EVENTS = frozenset({"get_reasoning_provider", "get_signal_config", "get_fleet_status"})


@_safe
def user_input(
    action: str,
    *,
    channel: str,
    payload: Any = None,
    actor: str | None = None,
    remote_addr: str | None = None,
    outcome: str = "ok",
    duration_ms: float | None = None,
    status: int | None = None,
    error: str | None = None,
    component: str = "dashboard",
) -> None:
    """One operator action over Socket.IO or REST (``user_input``).

    ``payload`` is sanitized: secrets redacted, image data reduced to its
    size, and free text (descriptions, notes) kept only with
    ``FLIGHTRISK_TELEMETRY_CONTENT`` on (notes capped at 500 characters).
    """
    level = logging.DEBUG if action in QUERY_EVENTS else logging.INFO
    if outcome not in ("ok",):
        level = max(level, logging.WARNING if outcome != "error" else logging.ERROR)
    fields: dict[str, Any] = {
        "action": action,
        "channel": channel,
        "outcome": outcome,
        "content_capture": content_capture_enabled(),
    }
    if actor:
        fields["actor"] = actor
    if remote_addr:
        fields["remote_addr"] = remote_addr
    if status is not None:
        fields["status"] = status
    if duration_ms is not None:
        fields["duration_ms"] = round(duration_ms, 2)
    if payload is not None:
        fields["input"] = sanitize(payload)
    if error:
        fields["error"] = error
    log_event(_log, level, "user_input", f"{channel} {action} {outcome}", category="user_input", component=component, **fields)


# ---------------------------------------------------------------------------
# target_image
# ---------------------------------------------------------------------------

M_target_changes = M._counter("flightrisk.target.changes", description="Target reference changes by source (upload, revert, startup, clear)")


def _image_dims(img: Any) -> tuple[int | None, int | None, int | None]:
    shape = getattr(img, "shape", None)
    if not shape:
        return None, None, None
    h, w = int(shape[0]), int(shape[1])
    c = int(shape[2]) if len(shape) > 2 else 1
    return w, h, c


def _sniff_mime(raw: bytes | None) -> str | None:
    if not raw:
        return None
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:2] in (b"BM",):
        return "image/bmp"
    return "application/octet-stream"


@_safe
def target_image(
    img: Any,
    *,
    source: str,
    raw: bytes | None = None,
    quality: Any = None,
    face_report: Mapping[str, Any] | None = None,
    face_detected: bool | None = None,
    component: str = "dashboard",
) -> dict[str, Any]:
    """Log a new target reference (``target_image``) and store its artifact.

    Numbers only go into the event: size, dimensions, MIME type, quality
    scores and face-report numbers. With content capture on, the image is
    also stored as a local artifact (see :mod:`flightrisk.obs_artifacts`) and
    the event carries its sha256, artifact reference and a short-lived
    preview URL. The image itself is never in a log line or a label.
    Returns the logged fields (for tests).
    """
    content = content_capture_enabled()
    w, h, c = _image_dims(img)
    fields: dict[str, Any] = {
        "source": source,
        "width": w,
        "height": h,
        "channels": c,
        "content_capture": content,
    }
    if raw is not None:
        fields["bytes"] = len(raw)
        fields["mime"] = _sniff_mime(raw)
    if quality is not None:
        fields["quality"] = _quality_numbers(quality)
    if face_report:
        fields["face_report"] = {k: v for k, v in face_report.items() if isinstance(v, (int, float, bool)) or v is None}
    if face_detected is not None:
        fields["face_detected"] = bool(face_detected)
    if content and img is not None:
        from flightrisk import obs_artifacts

        stored = obs_artifacts.default_store().store_target(img, raw=raw)
        if stored is not None:
            fields.update(stored)
    M_target_changes.add(1, {"source": safe_token(source)})
    log_event(_log, logging.INFO, "target_image", f"target set from {source}", category="target_image", component=component, **fields)
    return fields


def _quality_numbers(quality: Any) -> dict[str, Any]:
    if isinstance(quality, Mapping):
        src = quality
    else:
        src = {k: getattr(quality, k) for k in ("overall_score", "grade", "dimensions") if hasattr(quality, k)}
    out: dict[str, Any] = {}
    for k, v in src.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = round(float(v), 4)
        elif k == "grade" and isinstance(v, str) and len(v) <= 2:
            out[k] = v
        elif isinstance(v, Mapping):
            out[k] = {dk: round(float(dv), 4) for dk, dv in v.items() if isinstance(dv, (int, float)) and not isinstance(dv, bool)}
    issues = getattr(quality, "issues", None)
    if isinstance(issues, list):
        out["issues"] = len(issues)
    return out


@_safe
def target_cleared(component: str = "dashboard") -> None:
    M_target_changes.add(1, {"source": "clear"})
    log_event(_log, logging.INFO, "target_image", "target cleared", category="target_image", component=component, source="clear")


# ---------------------------------------------------------------------------
# error
# ---------------------------------------------------------------------------

@_safe
def report_error(
    logger: logging.Logger | None,
    event: str,
    exc: BaseException | None = None,
    *,
    level: int = logging.ERROR,
    component: str | None = None,
    **fields: Any,
) -> None:
    """Log a failure that used to be silent (``error`` category).

    The record carries the exception type and redacted message, and the log
    pipeline adds a fingerprint (type plus top frames) that groups repeats;
    a repeat within 10 s is collapsed into a ``suppressed`` count. ERROR and
    above also increment ``flightrisk_error_logs``.
    """
    lg = logger or _log
    if exc is not None:
        fields.setdefault("error_type", type(exc).__name__)
        fields.setdefault("error", str(exc))
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    log_event(lg, level, event, event, category="error", component=component, exc_info=exc_info, **fields)


# ---------------------------------------------------------------------------
# drone_telemetry_snapshot
# ---------------------------------------------------------------------------

_SPEED_FIELDS = (("ground", "ground_speed_mps"), ("vx", "vx_mps"), ("vy", "vy_mps"), ("vz", "vz_mps"))
_ATTITUDE_FIELDS = (("pitch", "pitch_deg"), ("roll", "roll_deg"), ("yaw", "yaw_deg"))
_HEIGHT_FIELDS = ("height_m", "tof_m", "baro_altitude_m")


@_safe
def telemetry_snapshot(snapshot: Any, *, component: str = "dashboard") -> None:
    """Export a :class:`~flightrisk.drone.snapshot.TelemetrySnapshot`.

    Metrics on every call (battery, height per source, speed, attitude,
    flying, and the telemetry age gauge). One ``drone_telemetry_snapshot``
    log event per drone every :data:`SNAPSHOT_LOG_INTERVAL_S` with each
    reading's value, unit, source and age: how height and speed were
    determined.
    """
    drone_id = snapshot.drone_id or "unknown"
    backend = snapshot.backend or "unknown"
    base = {"drone_id": drone_id, "backend": backend}
    newest = None
    battery = snapshot.battery_pct
    if battery is not None:
        M.drone_battery.set(battery.value, base)
        newest = battery.measured_at
    for name in _HEIGHT_FIELDS:
        r = getattr(snapshot, name, None)
        if r is not None:
            M.drone_height.set(r.value, {**base, "source": r.source, "kind": name})
            newest = max(newest or 0.0, r.measured_at)
    for axis, name in _SPEED_FIELDS:
        r = getattr(snapshot, name, None)
        if r is not None:
            M.drone_speed.set(r.value, {**base, "axis": axis, "source": r.source})
    for axis, name in _ATTITUDE_FIELDS:
        r = getattr(snapshot, name, None)
        if r is not None:
            M.drone_attitude.set(r.value, {**base, "axis": axis})
    M.drone_flying.set(1 if snapshot.is_flying else 0, base)
    M.note_telemetry(drone_id, backend, newest)

    allowed, suppressed = _snapshot_throttle.allow(("snapshot", drone_id))
    if not allowed:
        return
    now = time.time()
    readings = {}
    for name, r in snapshot.readings().items():
        if r is None:
            continue
        readings[name] = {"value": round(float(r.value), 4), "unit": r.unit, "source": r.source, "age_s": round(r.age_s(now), 3)}
    height = snapshot.height_m
    quality = []
    if snapshot.is_flying and height is not None and height.value <= 0:
        quality.append("height_zero_while_flying")
    oldest = snapshot.oldest_age_s(now)
    fields: dict[str, Any] = {
        "drone_id": drone_id,
        "backend": backend,
        "connected": snapshot.connected,
        "is_flying": snapshot.is_flying,
        "is_flying_source": snapshot.is_flying_source,
        "flight_mode": snapshot.flight_mode,
        "readings": readings,
        "oldest_age_s": round(oldest, 3) if oldest is not None else None,
        "quality_flags": quality,
    }
    if suppressed:
        fields["samples_since_last"] = suppressed + 1
    log_event(_log, logging.INFO, "drone_telemetry_snapshot", category="drone_telemetry", component=component, **fields)


@_safe
def edge_telemetry(drone_id: str, msg: Mapping[str, Any], *, side: str = "ground") -> None:
    """Export a fleet ``telemetry`` message received from an edge (numbers only)."""
    label = M.drone_label(drone_id)
    base = {"drone_id": label, "backend": "edge"}
    battery = msg.get("battery")
    if isinstance(battery, (int, float)) and not isinstance(battery, bool):
        M.drone_battery.set(battery, base)
    height = msg.get("height_cm")
    if isinstance(height, (int, float)) and not isinstance(height, bool):
        M.drone_height.set(height / 100.0, {**base, "source": "edge.height_cm", "kind": "height_m"})
    flying = msg.get("is_flying")
    if isinstance(flying, bool):
        M.drone_flying.set(1 if flying else 0, base)
    M.note_telemetry(label, "edge", time.time())
    allowed, suppressed = _snapshot_throttle.allow(("edge", label))
    if not allowed:
        return
    keep = ("battery", "height_cm", "is_flying", "rel_x_cm", "rel_y_cm", "heading_deg", "lane_index",
            "lanes_total", "link_age_s", "epoch", "plan_id", "state", "plan_valid")
    readings = {k: msg.get(k) for k in keep if k in msg and (msg.get(k) is None or isinstance(msg.get(k), (int, float, bool, str)))}
    fields: dict[str, Any] = {
        "drone_id": drone_id,
        "backend": "edge",
        "side": side,
        "readings": readings,
        "height_source": "edge.height_cm (Tello state field h, polled on the edge)",
        "position_source": "edge dead reckoning (rel_x_cm/rel_y_cm from commanded moves)",
    }
    if suppressed:
        fields["samples_since_last"] = suppressed + 1
    log_event(_log, logging.INFO, "drone_telemetry_snapshot", category="drone_telemetry", component="ground", **fields)


# ---------------------------------------------------------------------------
# alert
# ---------------------------------------------------------------------------

@_safe
def alert(transition: str, *, alert_id: str | None, phase: str, awaiting_s: float | None = None,
          renotify_count: int | None = None, alert_level: str | None = None, component: str = "dashboard") -> None:
    """Alert latch transition (``alert``): opened, renotified, confirmed, dismissed, aborted, reset."""
    M.alert_transitions.add(1, {"transition": transition})
    M.set_alert_phase(phase)
    if transition in ("confirmed", "dismissed") and awaiting_s is not None:
        M.alert_response.record(awaiting_s, {"action": transition})
    level = logging.WARNING if transition in ("opened", "renotified", "confirmed") else logging.INFO
    if transition == "reset":
        level = logging.DEBUG  # every new session resets the latch
    fields: dict[str, Any] = {"transition": transition, "phase": phase}
    if alert_id:
        fields["alert_id"] = alert_id
    if awaiting_s is not None:
        fields["awaiting_s"] = round(awaiting_s, 3)
    if renotify_count is not None:
        fields["renotify_count"] = renotify_count
    if alert_level:
        fields["alert_level"] = alert_level
    log_event(_log, level, "alert", f"alert {transition}", category="alert", component=component, **fields)


# ---------------------------------------------------------------------------
# flight_safety
# ---------------------------------------------------------------------------

@_safe
def flight_safety(kind: str, *, reason: str | None = None, drone_id: str | None = None,
                  level: int = logging.WARNING, component: str = "dashboard", log: bool = True, **fields: Any) -> None:
    """A flight-safety event: battery warning, failsafe land, link loss, geofence, obstacle, ..."""
    M.flight_safety_events.add(1, {
        "kind": safe_token(kind),
        "reason": safe_token(reason, "none"),
        "drone_id": safe_token(drone_id, "none"),
    })
    if not log:
        return
    if drone_id:
        fields["drone_id"] = drone_id
    if reason:
        fields["reason"] = reason
    log_event(_log, level, "flight_safety", f"{kind}{' (' + reason + ')' if reason else ''}",
              category="flight_safety", component=component, kind=kind, **fields)


@_safe
def obstacle_check(check: Mapping[str, Any] | None, *, action: str | None = None, min_safe_depth: float | None = None,
                   waypoint: int | None = None, retry: int | None = None, drone_id: str | None = None) -> None:
    """One obstacle-guard decision; triggers (anything but ``clear``) are flight-safety events."""
    act = action or (check or {}).get("action") or "unknown"
    M.obstacle_checks.add(1, {"action": safe_token(act)})
    depth = (check or {}).get("center_depth")
    margin = None
    if isinstance(depth, (int, float)) and isinstance(min_safe_depth, (int, float)) and min_safe_depth > 0:
        # Histograms take non-negative values only, so the metric is the ratio
        # (above 1 is blocked); the log keeps the signed margin.
        margin = round(float(min_safe_depth) - float(depth), 4)
        M.obstacle_depth_ratio.record(max(0.0, float(depth) / float(min_safe_depth)), {"action": safe_token(act)})
    if act == "clear":
        return
    flight_safety("obstacle", reason=act, drone_id=drone_id, center_depth=depth, margin=margin,
                  confidence=(check or {}).get("confidence"), waypoint=waypoint, retry=retry)


# ---------------------------------------------------------------------------
# detection_pipeline
# ---------------------------------------------------------------------------

class PipelineStats:
    """Per-frame stage timings and counts, with a periodic summary event.

    ``stage(name, ms)`` records ``flightrisk_stage_duration_ms{stage}``.
    ``frame_done(...)`` records the frame-level metrics and, every
    :data:`PIPELINE_LOG_INTERVAL_S`, logs one ``detection_pipeline`` event
    with frame count, persons, per-stage mean and max, and score summaries.
    """

    def __init__(self, interval_s: float = PIPELINE_LOG_INTERVAL_S, clock=time.monotonic):
        self._interval_s = interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._reset(clock())

    def _reset(self, now: float) -> None:
        self._window_start = now
        self._frames = 0
        self._persons = 0
        self._stages: dict[str, list[float]] = {}
        self._scores: dict[str, list[float]] = {}
        self._levels: dict[str, int] = {}

    @_safe
    def stage(self, name: str, ms: float) -> None:
        M.stage_duration.record(ms, {"stage": name})
        with self._lock:
            agg = self._stages.setdefault(name, [0.0, 0.0, 0])
            agg[0] += ms
            agg[1] = max(agg[1], ms)
            agg[2] += 1

    @_safe
    def signal_scores(self, scores: Mapping[str, float]) -> None:
        for signal, value in scores.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                M.signal_score.record(float(value), {"signal": safe_token(signal)})
                with self._lock:
                    agg = self._scores.setdefault(signal, [0.0, 0.0, 0])
                    agg[0] += float(value)
                    agg[1] = max(agg[1], float(value))
                    agg[2] += 1

    @_safe
    def alert_level(self, level: str) -> None:
        M.alert_levels.add(1, {"level": safe_token(level)})
        with self._lock:
            self._levels[level] = self._levels.get(level, 0) + 1

    @_safe
    def frame_done(self, persons: int, source: str, component: str = "dashboard", profile: str | None = None) -> None:
        """Record one frame. *profile* (an edge's detection profile) becomes a bounded ``profile`` label."""
        if profile is None:
            M.detections_per_frame.record(persons)
            M.frames.add(1, {"source": safe_token(source)})
        else:
            label = _profile_label(profile)
            M.detections_per_frame.record(persons, {"profile": label})
            M.frames.add(1, {"source": safe_token(source), "profile": label})
        M.note_frame()
        now = self._clock()
        with self._lock:
            self._frames += 1
            self._persons += persons
            if now - self._window_start < self._interval_s:
                return
            window = now - self._window_start
            summary = {
                "frames": self._frames,
                "window_s": round(window, 2),
                "fps": round(self._frames / window, 2) if window > 0 else None,
                "persons": self._persons,
                "stages_ms": {k: {"mean": round(v[0] / v[2], 2), "max": round(v[1], 2)} for k, v in self._stages.items() if v[2]},
                "scores": {k: {"mean": round(v[0] / v[2], 4), "max": round(v[1], 4), "n": v[2]} for k, v in self._scores.items() if v[2]},
                "alert_levels": dict(self._levels),
            }
            self._reset(now)
        log_event(_log, logging.INFO, "detection_pipeline", category="detection_pipeline", component=component, source=source, **summary)


class StageTimer:
    """``with StageTimer(stats, "detect"):`` records the block's duration."""

    __slots__ = ("_name", "_stats", "_t0")

    def __init__(self, stats: PipelineStats, name: str):
        self._stats, self._name = stats, name

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self._stats.stage(self._name, (time.perf_counter() - self._t0) * 1000.0)
        return False


@_safe
def model_load(model: str, outcome: str, *, error: BaseException | None = None, component: str = "dashboard", **fields: Any) -> None:
    """A model or signal provider loaded (or failed to)."""
    M.model_loads.add(1, {"model": safe_token(model), "outcome": safe_token(outcome)})
    level = logging.INFO if outcome == "ok" else logging.WARNING
    if error is not None:
        fields["error_type"] = type(error).__name__
        fields["error"] = str(error)
    log_event(_log, level, "model_load", f"{model} {outcome}", category="detection_pipeline", component=component,
              model=model, outcome=outcome, **fields)


# ---------------------------------------------------------------------------
# reasoning (metadata only)
# ---------------------------------------------------------------------------

@_safe
def reasoning_call(*, provider: str, model: str | None, kind: str, duration_ms: float, outcome: str,
                   tokens_in: int | None = None, tokens_out: int | None = None, error_type: str | None = None,
                   component: str = "reasoning") -> None:
    """One reasoning call: provider, model, latency, token counts and outcome. Never the text."""
    labels = {"provider": safe_token(provider), "kind": safe_token(kind), "outcome": safe_token(outcome)}
    M.reasoning_calls.add(1, labels)
    M.reasoning_duration.record(duration_ms, labels)
    if isinstance(tokens_in, int):
        M.reasoning_tokens.add(tokens_in, {"provider": labels["provider"], "direction": "input"})
    if isinstance(tokens_out, int):
        M.reasoning_tokens.add(tokens_out, {"provider": labels["provider"], "direction": "output"})
    level = logging.INFO if outcome in ("match", "no_match", "ok") else logging.WARNING
    log_event(_log, level, "reasoning", f"{provider} {kind} {outcome}", category="reasoning", component=component,
              provider=provider, model=model, kind=kind, duration_ms=round(duration_ms, 1), outcome=outcome,
              tokens_in=tokens_in, tokens_out=tokens_out, error_type=error_type)


def reasoning_outcome(result: Mapping[str, Any] | None) -> str:
    """Bounded outcome label for a reasoning result dict (never its text)."""
    if not isinstance(result, Mapping):
        return "error"
    confidence = result.get("confidence")
    if confidence in ("error", "unavailable", "off"):
        return str(confidence)
    return "match" if result.get("match") else "no_match"


@_safe
def queue_dropped(queue_name: str, *, component: str = "dashboard", **fields: Any) -> None:
    """An item dropped because a bounded queue was full."""
    M.queue_dropped.add(1, {"queue": safe_token(queue_name)})
    from flightrisk.obs_log import throttled

    throttled(_safety_throttle, ("queue", queue_name), _log, logging.WARNING, "queue_dropped",
              category="error", component=component, queue=queue_name, **fields)


def short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# StructuredLogger event router
# ---------------------------------------------------------------------------

#: StructuredLogger events that are flight-safety events: event -> (kind, default reason).
#: The lane executor, edge device and dashboard already log these by name;
#: routing them here counts them in ``flightrisk_flight_safety_events``
#: without touching those modules.
SAFETY_EVENTS: dict[str, tuple[str, str | None]] = {
    "edge_land": ("land", None),
    "edge_link_lost": ("link_loss", "link_loss"),
    "edge_land_not_executed": ("land_failed", None),
    "edge_land_error": ("land_failed", "error"),
    "edge_shutdown_land_error": ("land_failed", "shutdown"),
    "edge_geofence_refused": ("geofence_refused", "geofence"),
    "edge_takeoff_refused": ("takeoff_refused", None),
    "edge_drone_link_unhealthy": ("drone_link_unhealthy", None),
    "edge_drone_link_restored": ("drone_link_restored", None),
    "edge_assign_lanes_refused": ("plan_refused", None),
    "edge_start_refused": ("start_refused", None),
    "edge_fail_safe_idle": ("fail_safe", None),
    "edge_altitude_unknown": ("altitude_unknown", "height_unknown"),
    "edge_local_land_key": ("manual_land", "land_key"),
    "edge_sigint_land": ("manual_land", "sigint"),
    "edge_local_land_error": ("land_failed", "land_key"),
    "edge_sigint_land_error": ("land_failed", "sigint"),
    "edge_control_fail_safe_error": ("land_failed", "control_error"),
    "battery_critical_land_not_executed": ("land_failed", "low_battery"),
    "battery_critical_land_failed": ("land_failed", "low_battery"),
    "land_all_sent": ("land_all", "operator"),
    "land_all_reached_no_drone": ("land_all_failed", "no_drone_reached"),
    "edge_unplanned_landing": ("unplanned_landing", None),
    "edge_hold_refused": ("hold_refused", None),
    "edge_nudge_refused": ("nudge_refused", None),
    "edge_launch_refused": ("launch_refused", None),
    "edge_command_refused": ("command_refused", None),
    "edge_plan_invalidated": ("plan_invalidated", None),
    "edge_retask_not_flown": ("retask_not_flown", None),
    "edge_takeoff": ("takeoff", None),
    "edge_pose_reset": ("pose_reset", None),
    "edge_lanes_complete": ("lanes_complete", None),
}

#: StructuredLogger events about the ground/edge link.
COMM_EVENTS = frozenset({
    "link_outbox_send_timeout", "link_outbox_payload_error", "link_outbox_writer_error", "edge_ground_hello",
    "edge_authority_released", "edge_connection_refused", "edge_handover_announced", "fleet_epoch_adopted",
    "edge_message_refused", "edge_queued_motion_discarded", "edge_detections_dropped_no_authority",
    "edge_uplink_frame_error", "edge_send_invalid", "edge_reply_invalid",
    "edge_telemetry_dropped", "edge_send_error", "edge_send_unavailable", "edge_control_recv_error",
    "edge_connect", "edge_control_stream_video", "edge_clock_offset", "edge_clock_invalid",
    "ground_bind_address_restored", "ground_bind_recovered", "ground_bind_listener_unreachable",
    "ground_bind_listener_rebound", "edge_message_error", "fleet_detection_error",
})

ALERT_EVENTS = frozenset({"match_alert_unanswered"})

#: StructuredLogger events about detection profiles (flightrisk/edge_profiles.py).
PROFILE_EVENTS = frozenset({"profile_selected", "profile_fallback", "profile_switch_refused"})
#: Reasons a profile event may carry as a label; anything else is "other".
_PROFILE_REASONS = frozenset({
    "in_flight", "unknown_profile", "unavailable", "load_failed", "busy", "invalid", "weights_missing",
})


def _profile_label(value: Any) -> str:
    """A bounded ``profile`` label: a registered profile name, ``none`` or ``other``."""
    from flightrisk.vision.profiles import profile_label

    return profile_label(value)


def route_structured(component: str, event: str, fields: Mapping[str, Any]) -> str | None:
    """Category for a StructuredLogger event, counting flight-safety and comm ones.

    Called for every StructuredLogger record, so it is a dictionary lookup
    and, for the few events above, one counter increment.
    """
    safety = SAFETY_EVENTS.get(event)
    if safety is not None:
        kind, default_reason = safety
        reason = fields.get("reason") if isinstance(fields.get("reason"), str) else default_reason
        if event == "edge_land" and reason == "link_loss":
            kind = "watchdog_land"
        if event == "edge_land_not_executed" and isinstance(fields.get("outcome"), str):
            reason = fields["outcome"]
        drone_id = fields.get("drone_id")
        M.flight_safety_events.add(1, {
            "kind": kind,
            "reason": safe_token(reason, "none"),
            "drone_id": safe_token(drone_id, "none"),
        })
        return "flight_safety"
    if event in COMM_EVENTS:
        if event == "fleet_epoch_adopted" and isinstance(fields.get("epoch"), int):
            M.fleet_epoch.set(fields["epoch"], {"drone_id": safe_token(fields.get("drone_id"))})
        if event == "edge_detections_dropped_no_authority":
            M.comm_dropped.add(1, {"side": "edge", "type": "detections", "reason": "no_authority"})
        if event in ("edge_telemetry_dropped", "edge_send_error", "edge_send_unavailable"):
            M.comm_dropped.add(1, {"side": "edge", "type": safe_token(fields.get("type"), "telemetry"),
                                   "reason": "queue_full" if event == "edge_telemetry_dropped" else "send_error"})
        return "drone_comm"
    if event in PROFILE_EVENTS:
        reason = fields.get("reason")
        M.profile_events.add(1, {
            "event": event,
            "profile": _profile_label(fields.get("profile")),
            "reason": reason if isinstance(reason, str) and reason in _PROFILE_REASONS else "none",
        })
        return "detection_pipeline"
    if event in ALERT_EVENTS:
        return "alert"
    if event in ("frame_loop_error", "gemma_worker_error"):
        return "error"
    return None


__all__ = [
    "QUERY_EVENTS",
    "PipelineStats",
    "StageTimer",
    "alert",
    "edge_telemetry",
    "flight_safety",
    "model_load",
    "obstacle_check",
    "queue_dropped",
    "reasoning_call",
    "reasoning_outcome",
    "report_error",
    "target_cleared",
    "target_image",
    "telemetry_snapshot",
    "user_input",
]
