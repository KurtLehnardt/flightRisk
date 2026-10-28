"""Command telemetry for any drone backend, applied by the backend registry.

:func:`instrument_drone` wraps the flight commands of one controller
instance (``takeoff``, ``land``, ``hover``, ``move``, ``rotate``,
``return_to_home``) so that every call

* runs in a span ``drone.<command>``,
* is counted in ``flightrisk_drone_commands{drone_id, backend, command, outcome}``
  and timed in ``flightrisk_drone_command_duration_ms`` (a land also in
  ``flightrisk_drone_land_duration_ms``), where ``outcome`` is the
  normalized :class:`~flightrisk.drone.controller.CommandResult` outcome
  (``ok``, ``dropped_busy``, ``not_executed``, ``rejected``) or ``error``
  when the command raised,
* is logged as a ``drone_comm`` event (direction ``out``, channel
  ``local``) with its numeric arguments; a land that was not executed or
  raised is also a ``flight_safety`` event.

The wrappers return exactly what the original returned and re-raise exactly
what it raised. Only instance attributes are replaced, so the controller's
type, its ``isinstance`` checks against the role protocols and
``inspect.signature`` (which follows ``__wrapped__``) are unchanged.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any

from flightrisk import obs_events
from flightrisk import obs_metrics as M
from flightrisk.drone.controller import command_result
from flightrisk.obs_log import log_event
from flightrisk.obs_redact import safe_token

_log = logging.getLogger("flightrisk.drone.commands")

#: Commands that are wrapped. ``rc_control`` is left alone: it streams at a high rate.
COMMANDS = ("takeoff", "land", "hover", "move", "rotate", "return_to_home")

try:
    from opentelemetry import trace as _trace
    from opentelemetry.trace import Status, StatusCode
except ImportError:  # pragma: no cover
    _trace = None


def _args_summary(command: str, args: tuple, kwargs: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if command == "move" and args:
        out["move_direction"] = safe_token(args[0])
        if len(args) > 1 and isinstance(args[1], (int, float)):
            out["distance_cm"] = args[1]
    elif command == "rotate" and args and isinstance(args[0], (int, float)):
        out["degrees"] = args[0]
    if "abort" in kwargs:
        out["abort_flag"] = True
    return out


def _wrap(drone: Any, command: str, backend: str, original):
    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        drone_id = getattr(drone, "name", None) or "unknown"
        t0 = time.perf_counter()
        tracer = _trace.get_tracer("flightrisk.drone") if _trace is not None else None
        span_cm = tracer.start_as_current_span(f"drone.{command}") if tracer is not None else None
        span = span_cm.__enter__() if span_cm is not None else None
        outcome = "error"
        detail = None
        error: BaseException | None = None
        try:
            value = original(*args, **kwargs)
            result = command_result(value, command)
            outcome = result.outcome.value
            detail = result.detail or None
            return value
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                _record(drone_id, backend, command, outcome, detail, error, (time.perf_counter() - t0) * 1000.0,
                        _args_summary(command, args, kwargs), span)
            except Exception:  # noqa: BLE001, S110 -- telemetry never changes command behaviour
                pass
            if span_cm is not None:
                if error is not None:
                    span_cm.__exit__(type(error), error, error.__traceback__)
                else:
                    span_cm.__exit__(None, None, None)

    return wrapper


def _record(drone_id, backend, command, outcome, detail, error, duration_ms, args_summary, span) -> None:
    labels = {
        "drone_id": safe_token(drone_id),
        "backend": safe_token(backend),
        "command": command,
        "outcome": safe_token(outcome),
    }
    M.drone_commands.add(1, labels)
    M.drone_command_duration.record(duration_ms, {"backend": labels["backend"], "command": command, "outcome": labels["outcome"]})
    if command == "land":
        M.land_duration.record(duration_ms, {"backend": labels["backend"], "outcome": labels["outcome"]})
    if span is not None:
        span.set_attribute("flightrisk.drone_id", str(drone_id))
        span.set_attribute("flightrisk.drone.backend", str(backend))
        span.set_attribute("flightrisk.outcome", str(outcome))
        if outcome != "ok":
            span.set_status(Status(StatusCode.ERROR, str(outcome)))
    level = logging.INFO if outcome == "ok" else logging.WARNING
    if command == "land" and outcome != "ok":
        level = logging.ERROR
    fields: dict[str, Any] = dict(args_summary)
    if detail:
        fields["detail"] = detail
    if error is not None:
        fields["error_type"] = type(error).__name__
        fields["error"] = str(error)
    log_event(_log, level, "drone_comm", f"command {command} {outcome}", category="drone_comm", component="drone",
              direction="out", channel="local", type="command", command=command, peer=drone_id, drone_id=drone_id,
              backend=backend, outcome=outcome, duration_ms=round(duration_ms, 2), **fields)
    if command == "land" and outcome != "ok":
        obs_events.flight_safety("land_failed", reason=outcome, drone_id=drone_id, level=logging.ERROR,
                                 component="drone", log=False)


def instrument_drone(drone: Any, backend: str) -> Any:
    """Wrap ``drone``'s flight commands in place (idempotent) and return it."""
    if drone is None or getattr(drone, "_flightrisk_instrumented", False):
        return drone
    for command in COMMANDS:
        original = getattr(drone, command, None)
        if original is None or not callable(original):
            continue
        try:
            setattr(drone, command, _wrap(drone, command, backend, original))
        except (AttributeError, TypeError):
            # A controller with __slots__ or read-only attributes stays uninstrumented.
            return drone
    try:
        drone._flightrisk_instrumented = True
    except (AttributeError, TypeError):
        pass
    return drone


__all__ = ["COMMANDS", "instrument_drone"]
