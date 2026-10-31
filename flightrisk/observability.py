"""Structured logging and metrics collection for the FlightRisk pipeline.

Provides JSON-formatted logging with component/event context and
per-session metrics tracking for detection, matching, and reasoning.
"""

import datetime
import json
import logging
import time
import threading
from typing import Any, Optional

from flightrisk.config import get_config
from flightrisk.obs_log import Throttle, setup_logging

#: Per-frame convenience events (``face_result``, ``scoring``, ``battery``)
#: are logged at most once per this many seconds per component and level;
#: the skipped ones are counted in the next line's ``suppressed`` field.
PER_FRAME_LOG_INTERVAL_S = 5.0
_per_frame_throttle = Throttle(PER_FRAME_LOG_INTERVAL_S)


# StructuredLogger loggers don't propagate, so a handler on a parent logger
# never sees their records. Extra handlers (e.g. the OTel log exporter) are
# registered here and attached to every StructuredLogger, old and new.
_structured_loggers: list[logging.Logger] = []
_extra_handlers: list[logging.Handler] = []


def register_log_handler(handler: logging.Handler) -> None:
    """Attach ``handler`` to all existing and future StructuredLoggers."""
    if handler not in _extra_handlers:
        _extra_handlers.append(handler)
    for lg in _structured_loggers:
        if handler not in lg.handlers:
            lg.addHandler(handler)


class StructuredLogger:
    """JSON-formatted structured logger with component context.

    Each log entry includes: timestamp, level, component, event,
    and arbitrary context kwargs.

    Records propagate to the ``flightrisk`` logger configured by
    :func:`flightrisk.obs_log.setup_logging`, which writes the shared JSON
    line format (with trace ids, node, session and redacted fields) to the
    console and, when OpenTelemetry is on, to Loki. The record's message is
    still this entry as JSON, for handlers attached with
    :func:`register_log_handler`. ``level`` defaults to the
    ``FLIGHTRISK_LOG_LEVEL`` of the ``flightrisk`` logger.
    """

    def __init__(self, component: str = "flightrisk", level: int | None = None):
        self.component = component
        setup_logging()
        self._logger = logging.getLogger(f"flightrisk.{component}")
        if level is not None:
            self._logger.setLevel(level)
        # Older versions gave every StructuredLogger its own stderr handler
        # and stopped propagation; the shared pipeline replaces both.
        self._logger.propagate = True

        if self._logger not in _structured_loggers:
            _structured_loggers.append(self._logger)
        for extra in _extra_handlers:
            if extra not in self._logger.handlers:
                self._logger.addHandler(extra)

    def _emit(self, level: str, event: str, **kwargs: Any) -> None:
        log_level = getattr(logging, level.upper(), logging.INFO)
        if not self._logger.isEnabledFor(log_level):
            return
        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "level": level,
            "component": self.component,
            "event": event,
        }
        entry.update(kwargs)
        category = None
        try:
            from flightrisk.obs_events import route_structured

            category = route_structured(self.component, event, kwargs)
        except Exception:  # noqa: BLE001 -- routing is best effort
            category = None
        extra = {
            "fr_event": event,
            "fr_fields": kwargs,
            "fr_component": self.component,
            "fr_category": category,
        }
        self._logger.log(log_level, json.dumps(entry, default=str), extra=extra)

    def _per_frame(self, level: str, event: str, **kwargs: Any) -> None:
        allowed, suppressed = _per_frame_throttle.allow((self.component, event, level))
        if not allowed:
            return
        if suppressed:
            kwargs["suppressed"] = suppressed
        self._emit(level, event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        self._emit("info", event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._emit("warning", event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._emit("error", event, **kwargs)

    def debug(self, event: str, **kwargs: Any) -> None:
        self._emit("debug", event, **kwargs)

    # --- Convenience methods for common pipeline events ---

    def detection(self, count: int, frame_id: Optional[int] = None, **kwargs: Any) -> None:
        """Log a person detection event."""
        self._emit("info", "detection", count=count, frame_id=frame_id, **kwargs)

    def match(self, score: float, match_type: str, **kwargs: Any) -> None:
        """Log a match event (reid, face, or description)."""
        self._emit("info", "match", score=round(score, 4), match_type=match_type, **kwargs)

    def face_result(self, success: bool, score: float = 0.0, **kwargs: Any) -> None:
        """Log a face recognition result (per frame, so throttled)."""
        self._per_frame("info", "face_result", success=success, score=round(score, 4), **kwargs)

    def reasoning(self, duration_ms: float, result: Optional[dict] = None, **kwargs: Any) -> None:
        """Log a reasoning/LLM call."""
        self._emit("info", "reasoning", duration_ms=round(duration_ms, 1), result=result, **kwargs)

    def scoring(self, combined: float, reid: float = 0.0, face: float = 0.0, **kwargs: Any) -> None:
        """Log a multi-feature scoring result (per matched frame, so throttled)."""
        self._per_frame("info", "scoring", combined=round(combined, 4),
                        reid=round(reid, 4), face=round(face, 4), **kwargs)

    def drone_command(self, command: str, **kwargs: Any) -> None:
        """Log a drone command."""
        self._emit("info", "drone_command", command=command, **kwargs)

    def battery(self, battery_level: int, is_flying: bool = False, **kwargs: Any) -> None:
        """Log a battery status event.

        Called every frame while flying, so it is throttled per level: a
        change of level (info, warning, error) is logged at once.
        """
        cfg = get_config().drone
        lvl = "warning" if battery_level <= cfg.battery_warn_threshold else "info"
        if battery_level <= cfg.battery_critical_threshold:
            lvl = "error"
        self._per_frame(lvl, "battery", battery_level=battery_level, is_flying=is_flying, **kwargs)


class MetricsCollector:
    """Per-session metrics tracking for the FlightRisk pipeline.

    Thread-safe counters for frames, detections, matches, face detection,
    reasoning calls, and errors.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """Clear all metrics for a new session."""
        with self._lock:
            self._started_at = time.time()
            self._frames_processed = 0
            self._persons_detected = 0
            self._matches = {"reid": 0, "face": 0, "description": 0}
            self._score_min = float("inf")
            self._score_max = 0.0
            self._score_sum = 0.0
            self._score_count = 0
            self._faces_checked = 0
            self._faces_found = 0
            self._reasoning_calls = 0
            self._reasoning_latency_ms = 0.0
            self._pipeline_errors = 0

    def inc_frames(self, n: int = 1) -> None:
        with self._lock:
            self._frames_processed += n

    def inc_persons(self, n: int) -> None:
        with self._lock:
            self._persons_detected += n

    def record_match(self, match_type: str, score: float) -> None:
        """Record a match event. match_type is 'reid', 'face', or 'description'."""
        with self._lock:
            if match_type in self._matches:
                self._matches[match_type] += 1
            self._score_count += 1
            self._score_sum += score
            if score < self._score_min:
                self._score_min = score
            if score > self._score_max:
                self._score_max = score

    def record_face_check(self, found: bool) -> None:
        """Record a face detection attempt."""
        with self._lock:
            self._faces_checked += 1
            if found:
                self._faces_found += 1

    def snapshot(self) -> dict:
        """Return a snapshot of all metrics as a dict."""
        with self._lock:
            elapsed = time.time() - self._started_at
            avg_score = (self._score_sum / self._score_count) if self._score_count > 0 else 0.0
            face_rate = (self._faces_found / self._faces_checked * 100) if self._faces_checked > 0 else 0.0
            avg_reasoning_ms = (self._reasoning_latency_ms / self._reasoning_calls) if self._reasoning_calls > 0 else 0.0

            return {
                "session_elapsed_s": round(elapsed, 1),
                "frames_processed": self._frames_processed,
                "persons_detected": self._persons_detected,
                "matches": dict(self._matches),
                "matches_total": sum(self._matches.values()),
                "score_distribution": {
                    "min": round(self._score_min, 4) if self._score_count > 0 else 0.0,
                    "max": round(self._score_max, 4),
                    "avg": round(avg_score, 4),
                    "count": self._score_count,
                },
                "face_detection": {
                    "checked": self._faces_checked,
                    "found": self._faces_found,
                    "rate_pct": round(face_rate, 1),
                },
                "reasoning": {
                    "calls": self._reasoning_calls,
                    "total_latency_ms": round(self._reasoning_latency_ms, 1),
                    "avg_latency_ms": round(avg_reasoning_ms, 1),
                },
                "pipeline_errors": self._pipeline_errors,
            }
