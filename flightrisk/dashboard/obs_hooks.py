"""RED metrics, spans and ``user_input`` events for the dashboard's handlers.

Two install functions, called once from :mod:`flightrisk.dashboard.app`:

* :func:`install_http_observability` adds request hooks to the Flask app:
  every REST request is counted and timed by route template, method and
  status class (``flightrisk_http_requests``, ``flightrisk_http_duration_ms``),
  and every state-changing request (POST, PUT, PATCH, DELETE) is logged as a
  ``user_input`` event. The timing hook is inserted *before* the API-key gate
  only to read the clock; the gate itself is unchanged and still decides
  first whether a request is served (a refused request is logged with
  outcome ``unauthorized``).
* :func:`install_socketio_observability` wraps the Socket.IO dispatcher so
  every browser event (``set_target``, ``drone_command``, ``land_all``, ...)
  runs inside a span, is counted and timed (``flightrisk_socket_events``,
  ``flightrisk_socket_duration_ms``) and is logged as ``user_input`` with its
  sanitized payload. A handler that answers with an ``error`` event is
  recorded with outcome ``rejected``; one that raises, ``error``. Outgoing
  events are counted by name (``flightrisk_socket_emits``).

Payloads are sanitized by :mod:`flightrisk.obs_redact`: credentials are
redacted (the Socket.IO ``api_key`` included), image data is reduced to its
size, and free text is kept only with ``FLIGHTRISK_TELEMETRY_CONTENT`` on.
"""

from __future__ import annotations

import contextvars
import functools
import logging
import time
from typing import Any

from flightrisk import obs_events
from flightrisk import obs_metrics as M
from flightrisk.obs_log import log_event
from flightrisk.obs_redact import redact_text, safe_token

_log = logging.getLogger("flightrisk.dashboard.obs")

try:
    from opentelemetry import trace as _trace
    from opentelemetry.trace import Status, StatusCode
except ImportError:  # pragma: no cover
    _trace = None

#: Methods whose requests change state and are logged as ``user_input``.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Per-handler state for the Socket.IO dispatcher wrapper (thread/greenlet local).
_handler_state: contextvars.ContextVar[dict | None] = contextvars.ContextVar("flightrisk_socket_handler", default=None)


def _tracer():
    return _trace.get_tracer("flightrisk.dashboard") if _trace is not None else None


def _status_class(status: int) -> str:
    return f"{status // 100}xx"


def _outcome_for_status(status: int) -> str:
    if status == 401 or status == 403:
        return "unauthorized"
    if status >= 500:
        return "error"
    if status >= 400:
        return "rejected"
    return "ok"


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

def install_http_observability(app) -> None:
    """Add RED metrics and ``user_input`` logging to every Flask request."""
    from flask import g, request

    from flightrisk.obs_log import setup_logging

    # Configure logging before any library can call logging.basicConfig().
    setup_logging()

    if getattr(app, "_flightrisk_obs_installed", False):
        return
    app._flightrisk_obs_installed = True

    def _start() -> None:
        g._fr_obs_t0 = time.perf_counter()

    def _finish(status: int, error: BaseException | None = None) -> None:
        if getattr(g, "_fr_obs_done", False):
            return
        g._fr_obs_done = True
        t0 = getattr(g, "_fr_obs_t0", None)
        duration_ms = (time.perf_counter() - t0) * 1000.0 if t0 is not None else None
        rule = request.url_rule.rule if request.url_rule is not None else "unmatched"
        method = request.method
        outcome = "error" if error is not None else _outcome_for_status(status)
        labels = {"route": rule, "method": method, "status_class": _status_class(status), "outcome": outcome}
        M.http_requests.add(1, labels)
        if duration_ms is not None:
            M.http_duration.record(duration_ms, {"route": rule, "method": method})
        if method in MUTATING_METHODS or outcome == "unauthorized":
            obs_events.user_input(
                f"{method} {rule}",
                channel="rest",
                payload=_rest_payload(request),
                remote_addr=request.remote_addr,
                outcome=outcome,
                status=status,
                duration_ms=duration_ms,
                error=type(error).__name__ if error is not None else None,
            )

    def _after(response):
        try:
            _finish(response.status_code)
        except Exception:  # noqa: BLE001 -- observability never fails a request
            _log.debug("http_obs_failed", exc_info=True)
        return response

    def _teardown(exc):
        if exc is not None:
            try:
                _finish(500, exc)
            except Exception:  # noqa: BLE001
                _log.debug("http_obs_failed", exc_info=True)

    # The clock starts before the API-key gate runs; the gate itself is
    # untouched and still the first hook that can refuse a request.
    app.before_request_funcs.setdefault(None, []).insert(0, _start)
    app.after_request(_after)
    app.teardown_request(_teardown)


def _rest_payload(request) -> Any:
    payload: dict[str, Any] = {}
    if request.view_args:
        payload["path"] = dict(request.view_args)
    try:
        body = request.get_json(silent=True, cache=True)
    except Exception:  # noqa: BLE001
        body = None
    if body is not None:
        payload["body"] = body
    if request.files:
        # Field names, file-name length and MIME type only; never the bytes or the file name.
        payload["uploads"] = [{"field": name, "filename_chars": len(f.filename or ""), "mimetype": safe_token(f.mimetype)}
                              for name, f in request.files.items()]
        if request.content_length:
            payload["content_length"] = request.content_length
    return payload or None


# ---------------------------------------------------------------------------
# Socket.IO
# ---------------------------------------------------------------------------

def install_socketio_observability(socketio) -> bool:
    """Wrap the Socket.IO event dispatcher and ``emit``. Returns False if unsupported."""
    if getattr(socketio, "_flightrisk_obs_installed", False):
        return True
    original = getattr(socketio, "_handle_event", None)
    if original is None or not callable(original):
        _log.warning("socketio_observability_unavailable: Flask-SocketIO has no _handle_event")
        return False
    socketio._flightrisk_obs_installed = True

    @functools.wraps(original)
    def _handle_event(handler, message, namespace, sid, *args):
        state = {"rejected": None}
        token = _handler_state.set(state)
        t0 = time.perf_counter()
        outcome = "ok"
        error: BaseException | None = None
        ret = None
        tracer = _tracer()
        span_cm = tracer.start_as_current_span(f"socket {message}") if tracer is not None else None
        span = span_cm.__enter__() if span_cm is not None else None
        try:
            ret = original(handler, message, namespace, sid, *args)
            if message == "connect" and ret is False:
                outcome = "denied"
            elif state["rejected"]:
                outcome = "rejected"
            return ret
        except BaseException as exc:
            outcome = "error"
            error = exc
            raise
        finally:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            _handler_state.reset(token)
            try:
                _record_socket(socketio, message, namespace, sid, args, outcome, duration_ms, error, state, span)
            except Exception:  # noqa: BLE001 -- observability never fails a handler
                _log.debug("socket_obs_failed", exc_info=True)
            if span_cm is not None:
                if error is not None:
                    span_cm.__exit__(type(error), error, error.__traceback__)
                else:
                    span_cm.__exit__(None, None, None)

    socketio._handle_event = _handle_event

    original_emit = socketio.emit

    @functools.wraps(original_emit)
    def emit(event, *args, **kwargs):
        try:
            M.socket_emits.add(1, {"event": safe_token(event)})
            if event == "error":
                state = _handler_state.get()
                message = None
                if args and isinstance(args[0], dict):
                    message = args[0].get("message")
                if state is not None:
                    state["rejected"] = redact_text(message or "error", 300)
                else:
                    log_event(_log, logging.WARNING, "operator_error", redact_text(message or "error", 300),
                              category="error", component="dashboard")
        except Exception:  # noqa: BLE001, S110
            pass
        return original_emit(event, *args, **kwargs)

    socketio.emit = emit
    return True


def _record_socket(socketio, message, namespace, sid, args, outcome, duration_ms, error, state, span) -> None:
    event = safe_token(message, "unknown")
    M.socket_events.add(1, {"event": event, "outcome": outcome})
    M.socket_duration.record(duration_ms, {"event": event})
    remote = None
    try:
        environ = socketio.server.get_environ(sid, namespace=namespace) or {}
        remote = environ.get("REMOTE_ADDR")
    except Exception:  # noqa: BLE001
        remote = None
    payload: Any
    if message == "connect":
        # args are (environ, auth): never log the WSGI environ; the auth
        # dict is sanitized, so an api_key in it is redacted.
        auth = args[1] if len(args) > 1 else None
        payload = {"auth": auth} if auth else None
    else:
        payload = args[0] if len(args) == 1 else (list(args) if args else None)
    if span is not None:
        try:
            span.set_attribute("flightrisk.socket.event", event)
            span.set_attribute("flightrisk.outcome", outcome)
            if outcome in ("error", "rejected"):
                span.set_status(Status(StatusCode.ERROR, outcome))
        except Exception:  # noqa: BLE001, S110
            pass
    obs_events.user_input(
        message,
        channel="socket",
        payload=payload,
        actor=sid[:8] if isinstance(sid, str) else None,
        remote_addr=remote,
        outcome=outcome,
        duration_ms=duration_ms,
        error=(type(error).__name__ if error is not None else state.get("rejected")),
    )


__all__ = ["install_http_observability", "install_socketio_observability"]
