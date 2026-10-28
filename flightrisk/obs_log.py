"""One structured JSON log format for every FlightRisk module.

Call :func:`setup_logging` once per process (``init_telemetry`` and
``StructuredLogger`` both do it). It configures the ``flightrisk`` logger,
which every module logger (``logging.getLogger(__name__)``) and every
``StructuredLogger`` propagates to, with:

* a console handler that writes one JSON object per line to stderr. It sits
  behind a bounded :class:`DropQueueHandler` and a ``QueueListener`` thread,
  so a caller (the frame loop, the lane-executor monitor) never waits on a
  slow terminal or pipe: when the queue is full the record is dropped and
  counted, never blocked on;
* optionally an OpenTelemetry handler (see :func:`install_otel_handler`),
  whose ``BatchLogRecordProcessor`` exports to the collector from its own
  thread. The OTLP body is the same JSON line.

Every line carries ``timestamp`` (ISO-8601 UTC with microseconds), ``level``,
``logger``, ``component``, ``event``, ``message``, ``trace_id`` and
``span_id`` of the active span, ``node`` (``service.instance.id``),
``session_id``, ``drone_id`` when known, plus the event's own fields after
redaction (:mod:`flightrisk.obs_redact`). Error records also get
``error_type``, ``error_message`` and a ``fingerprint`` (a hash of the
exception type and its top frames) that groups repeats of the same failure;
a fingerprint seen again within :data:`ERROR_REPEAT_S` seconds is logged
once with a ``suppressed`` count (metrics still count every one). The stack
trace is included only while the log level is DEBUG.

``FLIGHTRISK_LOG_LEVEL`` (default ``INFO``) sets the level.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as _dt
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import traceback
from collections.abc import Iterator, Mapping
from typing import Any

from flightrisk.obs_redact import redact_text, sanitize_fields

LOG_LEVEL_ENV = "FLIGHTRISK_LOG_LEVEL"
NODE_ENV = "FLIGHTRISK_NODE"

#: Root of every FlightRisk logger.
ROOT_LOGGER = "flightrisk"

#: Console queue size; records beyond it are dropped (and counted), never waited for.
QUEUE_SIZE = 10_000

#: A repeated error fingerprint is logged at most once per this many seconds.
ERROR_REPEAT_S = 10.0

#: Keys every JSON line owns; an event field with the same name is renamed ``field_<key>``.
_RESERVED = frozenset({
    "timestamp", "level", "logger", "component", "event", "message", "trace_id", "span_id",
    "node", "session_id", "category", "error_type", "error_message", "fingerprint", "stack",
})

_lock = threading.RLock()
_configured = False
_queue_handler: DropQueueHandler | None = None
_listener: logging.handlers.QueueListener | None = None
_otel_handler: logging.Handler | None = None
_session_id: str | None = None
_drone_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("flightrisk_drone_id", default=None)
_error_hook = None  # set by obs_metrics: (component, error_type, fingerprint, level) -> None

try:  # tracing is optional
    from opentelemetry import trace as _otel_trace
except ImportError:  # pragma: no cover - opentelemetry is in requirements
    _otel_trace = None


# ---------------------------------------------------------------------------
# Context: node, session, drone
# ---------------------------------------------------------------------------

def node_id() -> str | None:
    """This process's ``service.instance.id`` (from OTEL_RESOURCE_ATTRIBUTES or FLIGHTRISK_NODE)."""
    explicit = os.environ.get(NODE_ENV)
    if explicit:
        return explicit
    for part in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        key, _, value = part.partition("=")
        if key.strip() == "service.instance.id" and value.strip():
            return value.strip()
    return None


def set_session_id(session_id: str | None) -> None:
    """Record the current search session id; it is added to every later log line."""
    global _session_id
    _session_id = session_id


def current_session_id() -> str | None:
    return _session_id


@contextlib.contextmanager
def log_context(drone_id: str | None = None) -> Iterator[None]:
    """Add ``drone_id`` to every log line written inside the block (this thread/task only)."""
    token = _drone_ctx.set(drone_id)
    try:
        yield
    finally:
        _drone_ctx.reset(token)


def _trace_ids() -> tuple[str | None, str | None]:
    if _otel_trace is None:
        return None, None
    try:
        ctx = _otel_trace.get_current_span().get_span_context()
    except Exception:  # noqa: BLE001
        return None, None
    if not ctx or not ctx.is_valid:
        return None, None
    return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"


# ---------------------------------------------------------------------------
# Fingerprints and throttling
# ---------------------------------------------------------------------------

def error_fingerprint(exc_info: Any = None, *, logger_name: str = "", template: str = "") -> tuple[str, str, str]:
    """Return ``(fingerprint, error_type, where)`` for grouping repeats of one failure.

    With an exception: a hash of its type and its innermost three frames
    (module and function, not line numbers, so an unrelated edit does not
    change it). Without one: a hash of the logger name and the message
    template, before arguments are filled in.
    """
    if exc_info and isinstance(exc_info, tuple) and exc_info[0] is not None:
        etype, _value, tb = exc_info
        frames = traceback.extract_tb(tb)[-3:] if tb is not None else []
        parts = [etype.__name__] + [f"{os.path.basename(f.filename)}:{f.name}" for f in frames]
        where = f"{os.path.basename(frames[-1].filename)}:{frames[-1].name}:{frames[-1].lineno}" if frames else ""
        error_type = etype.__name__
    else:
        parts = [logger_name, template]
        where = logger_name
        error_type = "logged_error"
    digest = hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()[:12]
    return digest, error_type, where


class Throttle:
    """Allow one event per key every ``interval_s`` seconds, counting the rest.

    ``allow(key)`` returns ``(True, suppressed_since_last)`` when the event
    may be logged and ``(False, 0)`` otherwise. Thread-safe; the key set is
    bounded (the oldest keys are forgotten past ``max_keys``).
    """

    def __init__(self, interval_s: float, max_keys: int = 4096, clock=time.monotonic):
        self.interval_s = interval_s
        self._clock = clock
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._last: dict[Any, float] = {}
        self._suppressed: dict[Any, int] = {}

    def allow(self, key: Any) -> tuple[bool, int]:
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.interval_s:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return False, 0
            if last is None and len(self._last) >= self._max_keys:
                oldest = min(self._last, key=self._last.get)
                self._last.pop(oldest, None)
                self._suppressed.pop(oldest, None)
            self._last[key] = now
            return True, self._suppressed.pop(key, 0)


_error_throttle = Throttle(ERROR_REPEAT_S)


# ---------------------------------------------------------------------------
# Record enrichment (runs on the calling thread, before any queue)
# ---------------------------------------------------------------------------

class ContextFilter(logging.Filter):
    """Stamp trace ids, node, session, drone and error fingerprint onto a record.

    Runs on the thread that logged, so the trace context is the caller's.
    It is idempotent (a record reaching two handlers is enriched once) and
    returns False for an error repeat inside the throttle window.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        decided = getattr(record, "fr_keep", None)
        if decided is not None:
            return decided
        trace_id, span_id = _trace_ids()
        record.fr_trace_id = trace_id
        record.fr_span_id = span_id
        if getattr(record, "fr_drone_id", None) is None:
            record.fr_drone_id = _drone_ctx.get()
        keep = True
        if record.levelno >= logging.ERROR or record.exc_info:
            fields = getattr(record, "fr_fields", None) or {}
            fp, etype, where = error_fingerprint(
                record.exc_info, logger_name=record.name,
                template=str(getattr(record, "fr_event", None) or record.msg),
            )
            if etype == "logged_error" and isinstance(fields.get("error_type"), str):
                etype = fields["error_type"]
            record.fr_fingerprint = fp
            record.fr_error_type = etype
            record.fr_where = where
            if _error_hook is not None and record.levelno >= logging.ERROR:
                try:
                    _error_hook(_component_of(record), etype, fp, record.levelname.lower())
                except Exception:  # noqa: BLE001, S110 -- metrics must never break logging
                    pass
            allowed, suppressed = _error_throttle.allow(fp)
            keep = allowed
            if suppressed:
                record.fr_suppressed = suppressed
        record.fr_keep = keep
        return keep


def _component_of(record: logging.LogRecord) -> str:
    component = getattr(record, "fr_component", None)
    if component:
        return component
    name = record.name
    if name.startswith(ROOT_LOGGER + "."):
        name = name[len(ROOT_LOGGER) + 1:]
    return name.split(".")[0] if name else "flightrisk"


def _iso(created: float) -> str:
    return _dt.datetime.fromtimestamp(created, tz=_dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def build_payload(record: logging.LogRecord) -> dict[str, Any]:
    """The JSON object for one record (see the module docstring for the keys)."""
    fields: Mapping[str, Any] = getattr(record, "fr_fields", None) or {}
    event = getattr(record, "fr_event", None)
    if event:
        message = getattr(record, "fr_message", None) or event
    else:
        event = "log"
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            message = str(record.msg)
    trace_id = getattr(record, "fr_trace_id", None)
    span_id = getattr(record, "fr_span_id", None)
    if trace_id is None and not hasattr(record, "fr_trace_id"):
        trace_id, span_id = _trace_ids()
    payload: dict[str, Any] = {
        "timestamp": _iso(record.created),
        "level": record.levelname.lower(),
        "logger": record.name,
        "component": _component_of(record),
        "event": event,
        "message": redact_text(message),
    }
    category = getattr(record, "fr_category", None)
    if category:
        payload["category"] = category
    if trace_id:
        payload["trace_id"] = trace_id
        payload["span_id"] = span_id
    node = node_id()
    if node:
        payload["node"] = node
    if _session_id:
        payload["session_id"] = _session_id
    drone_id = fields.get("drone_id") or getattr(record, "fr_drone_id", None)
    if drone_id:
        payload["drone_id"] = drone_id
    fingerprint = getattr(record, "fr_fingerprint", None)
    if fingerprint:
        payload["fingerprint"] = fingerprint
        payload["error_type"] = getattr(record, "fr_error_type", None)
        where = getattr(record, "fr_where", None)
        if where:
            payload["where"] = where
        if record.exc_info and record.exc_info[1] is not None:
            payload["error_message"] = redact_text(record.exc_info[1])
        suppressed = getattr(record, "fr_suppressed", 0)
        if suppressed:
            payload["suppressed"] = suppressed
    debug = logging.getLogger(ROOT_LOGGER).isEnabledFor(logging.DEBUG)
    if debug:
        stack = getattr(record, "exc_text", None)
        if not stack and record.exc_info and record.exc_info[0] is not None:
            stack = "".join(traceback.format_exception(*record.exc_info))
        if stack:
            payload["stack"] = redact_text(stack, 8000)
    for key, value in sanitize_fields(fields).items():
        if key == "traceback":
            # Stack traces only at DEBUG; the fingerprint groups the error otherwise.
            if debug:
                payload["stack"] = redact_text(value, 8000) if isinstance(value, str) else value
            continue
        if key == "drone_id":
            continue
        payload[f"field_{key}" if key in _RESERVED else key] = value
    return payload


class JsonFormatter(logging.Formatter):
    """Formats a record as one line of JSON (see :func:`build_payload`)."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            return json.dumps(build_payload(record), default=str, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 -- never lose a record to a formatting bug
            return json.dumps({
                "timestamp": _iso(record.created), "level": record.levelname.lower(),
                "logger": record.name, "event": "log_format_error",
                "message": redact_text(record.msg), "error_type": type(exc).__name__,
            })


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

class _StderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is at emit time (survives stream swaps in tests)."""

    def __init__(self) -> None:
        super().__init__(stream=sys.stderr)

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:
        pass


class _NotEchoed(logging.Filter):
    """Skips records already printed to the console by :func:`notice`."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "fr_console_echo", False)


class _NotFlightRisk(logging.Filter):
    """Passes only records from loggers outside ``flightrisk`` (those have their own handler)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return not (record.name == ROOT_LOGGER or record.name.startswith(ROOT_LOGGER + "."))


class DropQueueHandler(logging.handlers.QueueHandler):
    """A QueueHandler that never blocks: a full queue drops the record and counts it."""

    def __init__(self, q: queue.Queue):
        super().__init__(q)
        self.dropped = 0
        self.listener = None

    def close(self) -> None:
        # logging.shutdown() (registered by the logging module at exit)
        # closes every handler; stopping the listener here writes out what
        # is still queued, so no exit hook of our own is needed.
        listener, self.listener = self.listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:  # noqa: BLE001, S110 -- never raise from logging shutdown
                pass
        super().close()

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # Like the base class, but keep the structured attributes and leave
        # formatting to the listener thread. The message is merged with its
        # args here, because the args may change after this call returns.
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info))
        clone = logging.makeLogRecord(record.__dict__)
        clone.msg = msg
        clone.args = None
        # The traceback object holds frames (and their locals) alive; the
        # fingerprint and text are already taken from it.
        if clone.exc_info:
            clone.exc_info = (clone.exc_info[0], clone.exc_info[1], None)
        return clone


def _level_from_env() -> int:
    raw = os.environ.get(LOG_LEVEL_ENV, "INFO").strip().upper()
    level = logging.getLevelName(raw)
    return level if isinstance(level, int) else logging.INFO


def setup_logging(level: int | str | None = None, *, console: bool = True) -> logging.Logger:
    """Configure the ``flightrisk`` logger once. Safe to call repeatedly.

    ``level`` overrides ``FLIGHTRISK_LOG_LEVEL``. Returns the logger.
    """
    global _configured, _queue_handler, _listener
    root = logging.getLogger(ROOT_LOGGER)
    with _lock:
        if level is not None:
            root.setLevel(logging.getLevelName(level.upper()) if isinstance(level, str) else level)
        elif not _configured:
            root.setLevel(_level_from_env())
        if _configured:
            return root
        if console:
            q: queue.Queue = queue.Queue(QUEUE_SIZE)
            handler = DropQueueHandler(q)
            handler.addFilter(ContextFilter())
            handler.addFilter(_NotEchoed())
            stderr = _StderrHandler()
            stderr.setFormatter(JsonFormatter())
            listener = logging.handlers.QueueListener(q, stderr, respect_handler_level=False)
            listener.start()
            handler.listener = listener
            root.addHandler(handler)
            _queue_handler, _listener = handler, listener
            # Third-party loggers (werkzeug, socketio, the OTel SDK, ...) get the
            # same JSON format through the same queue. A handler on the Python
            # root logger also turns a library's implicit logging.basicConfig()
            # into a no-op, so FlightRisk records are never printed twice.
            others = DropQueueHandler(q)
            others.addFilter(_NotFlightRisk())
            others.addFilter(ContextFilter())
            logging.getLogger().addHandler(others)
        _configured = True
        return root


def console_dropped() -> int:
    """How many console records were dropped because the queue was full."""
    return _queue_handler.dropped if _queue_handler is not None else 0


def console_queue_depth() -> int:
    return _queue_handler.queue.qsize() if _queue_handler is not None else 0


def install_otel_handler(handler: logging.Handler) -> None:
    """Attach the OpenTelemetry log handler to the ``flightrisk`` logger.

    A previously installed one is replaced, so records are never exported twice.
    """
    global _otel_handler
    with _lock:
        root = logging.getLogger(ROOT_LOGGER)
        if _otel_handler is not None:
            root.removeHandler(_otel_handler)
        handler.addFilter(ContextFilter())
        if handler.formatter is None:
            handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
        _otel_handler = handler


def otel_handler() -> logging.Handler | None:
    return _otel_handler


def set_error_hook(hook) -> None:
    """Register ``hook(component, error_type, fingerprint, level)``, called for every ERROR+ record."""
    global _error_hook
    _error_hook = hook


# ---------------------------------------------------------------------------
# Structured logging helpers
# ---------------------------------------------------------------------------

def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str | None = None,
    *,
    category: str | None = None,
    component: str | None = None,
    exc_info: Any = None,
    console_echo: bool = False,
    **fields: Any,
) -> None:
    """Log one structured event: ``event`` name, optional message and fields."""
    if not logger.isEnabledFor(level):
        return
    extra = {
        "fr_event": event,
        "fr_fields": fields,
        "fr_message": message,
        "fr_category": category,
        "fr_console_echo": console_echo,
    }
    if component:
        extra["fr_component"] = component
    logger.log(level, message or event, exc_info=exc_info, extra=extra, stacklevel=2)


#: Print operator notices to stdout as well as logging them.
CONSOLE_ECHO_ENV = "FLIGHTRISK_CONSOLE_ECHO"


def notice(logger: logging.Logger, event: str, text: str, level: int = logging.INFO, **fields: Any) -> None:
    """An operator-facing console line that is also a structured log event.

    Replaces the bare ``print()`` calls in the drone, vision and reasoning
    modules. ``text`` is printed to stdout exactly as before (the operator
    reads it in the terminal, and tests check it), unless
    ``FLIGHTRISK_CONSOLE_ECHO=0``; the structured event goes to the log
    pipeline (OTLP/Loki) but not to the JSON console, so the terminal does
    not show it twice.
    """
    echo = os.environ.get(CONSOLE_ECHO_ENV, "1").strip().lower() not in ("0", "false", "no", "off")
    if echo:
        print(text, flush=True)
    log_event(logger, level, event, text, console_echo=echo, **fields)


def throttled(throttle: Throttle, key: Any, logger: logging.Logger, level: int, event: str, **fields: Any) -> bool:
    """Log ``event`` only if ``throttle`` allows ``key``; adds ``suppressed`` when some were skipped."""
    allowed, suppressed = throttle.allow(key)
    if not allowed:
        return False
    if suppressed:
        fields["suppressed"] = suppressed
    log_event(logger, level, event, **fields)
    return True


__all__ = [
    "ERROR_REPEAT_S",
    "LOG_LEVEL_ENV",
    "ContextFilter",
    "DropQueueHandler",
    "JsonFormatter",
    "Throttle",
    "build_payload",
    "console_dropped",
    "console_queue_depth",
    "current_session_id",
    "error_fingerprint",
    "install_otel_handler",
    "log_context",
    "log_event",
    "node_id",
    "notice",
    "otel_handler",
    "set_error_hook",
    "set_session_id",
    "setup_logging",
    "throttled",
]
