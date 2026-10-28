"""``drone_comm`` events: every message between the ground station and the edges.

The transport (:mod:`flightrisk.transport`) calls these hooks with the
decoded message; they never see raw bytes, embeddings or images (only the
message size and a few scalar fields: type, drone id, ``cmd_id``, ``epoch``,
``plan_id``, ``seq``, the ``for``/``reason`` of an ack or nack).

* Commands, acks, nacks, ``hello``, ``clock``, hold/resume/land,
  ``emergency_land_all``, handover and target/stream toggles are logged one
  event per message, each in a short span so the log line links to a trace.
* High-rate traffic (``telemetry``, ``heartbeat``, ``ping``/``pong``,
  ``detections``, ``peer_heartbeat``) is counted on every message and logged
  as one aggregated event per (side, direction, type, drone) every
  :data:`AGGREGATE_INTERVAL_S` seconds. Edge telemetry also feeds the drone
  gauges and the ``drone_telemetry_snapshot`` event.

Every hook is wrapped so that a telemetry failure can never affect the link.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Mapping
from typing import Any

from flightrisk import fleet_protocol as fp
from flightrisk import obs_metrics as M
from flightrisk.obs_log import log_event
from flightrisk.obs_redact import safe_token

_log = logging.getLogger("flightrisk.comm")

#: Seconds between aggregated events for high-rate message types.
AGGREGATE_INTERVAL_S = 5.0

#: Message types that are counted every time and logged in aggregate.
SAMPLED_TYPES = frozenset({"telemetry", "heartbeat", "ping", "pong", "detections", "peer_heartbeat", "node_state"})

#: Scalar fields copied from a message into its event.
_SCALAR_FIELDS = ("cmd_id", "epoch", "plan_id", "seq", "for", "reason", "leader_id", "node_id", "proto",
                  "lanes_total", "t_ground_s", "enabled", "epoch_next", "role", "profile", "requested",
                  "fallback")

#: Message types that become a ``type`` label as themselves; anything else is "other",
#: so a client that invents types cannot create series without end.
KNOWN_TYPES = frozenset(
    set(fp.GROUND_TO_EDGE_TYPES_V2) | set(fp.EDGE_TO_GROUND_TYPES_V2) | set(fp.PEER_TYPES)
    | set(fp.LEGACY_GROUND_TO_EDGE_TYPES) | set(fp.LEGACY_EDGE_TO_GROUND_TYPES) | SAMPLED_TYPES
    | set(fp.CONFIG_COMMAND_TYPES)
    | {"command", "clock_sync", "link_up", "link_down", "reconnect", "unknown", "broadcast"}
)

_local_drone: list[str | None] = [None]


def _reason_label(value: Any) -> str:
    if value is None:
        return "none"
    known = isinstance(value, str) and (value in fp.NACK_REASONS or value in fp.PROFILE_NACK_REASONS)
    return value if known else "other"


def type_label(value: Any) -> str:
    """A bounded ``type`` label: a known message type, or "other"."""
    token = safe_token(value, "unknown")
    return token if token in KNOWN_TYPES else "other"

try:
    from opentelemetry import trace as _trace

    _tracer = _trace.get_tracer("flightrisk.comm")
except ImportError:  # pragma: no cover
    _tracer = None


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            logging.getLogger("flightrisk.obs").debug("comm_hook_failed %s", fn.__name__, exc_info=True)
            return None

    return wrapper


class _Aggregator:
    def __init__(self, interval_s: float = AGGREGATE_INTERVAL_S, clock=time.monotonic):
        self._interval_s = interval_s
        self._clock = clock
        # Re-entrant: a signal handler may log while this thread is inside add().
        self._lock = threading.RLock()
        self._windows: dict[tuple, list] = {}  # key -> [start, count, bytes]

    def add(self, key: tuple, size: int) -> tuple[int, int, float] | None:
        """Count one message; return (count, bytes, window_s) when the window closes."""
        now = self._clock()
        with self._lock:
            w = self._windows.get(key)
            if w is None:
                self._windows[key] = [now, 1, size]
                return None
            w[1] += 1
            w[2] += size
            if now - w[0] < self._interval_s:
                return None
            out = (w[1], w[2], now - w[0])
            self._windows[key] = [now, 0, 0]
            return out


_agg = _Aggregator()


def _fields(msg: Mapping[str, Any]) -> dict[str, Any]:
    out = {}
    for k in _SCALAR_FIELDS:
        v = msg.get(k)
        if v is not None and isinstance(v, (str, int, float, bool)):
            out["for_" if k == "for" else k] = v if not isinstance(v, str) else v[:64]
    detections = msg.get("detections")
    if isinstance(detections, list):
        out["detections"] = len(detections)
    lanes = msg.get("lanes")
    if isinstance(lanes, list):
        out["lanes"] = len(lanes)
    if msg.get("type") == "set_target":
        out["has_reid_embedding"] = msg.get("reid_embedding") is not None
        out["has_face_embedding"] = msg.get("face_embedding") is not None
    return out


def _record(side: str, direction: str, msg: Mapping[str, Any], drone_id: str | None, size: int | None,
            outcome: str = "ok", **extra: Any) -> None:
    mtype = msg.get("type") if isinstance(msg.get("type"), str) else "unknown"
    peer = drone_id if isinstance(drone_id, str) and drone_id else (msg.get("drone_id") if isinstance(msg.get("drone_id"), str) else None)
    labels = {
        "side": side,
        "direction": direction,
        "type": type_label(mtype),
        "drone_id": M.drone_label(peer),
        "outcome": safe_token(outcome),
    }
    M.comm_messages.add(1, labels)
    if size:
        M.comm_bytes.add(size, {"side": side, "direction": direction})
    if peer and direction == "in":
        M.note_link(labels["drone_id"], side)
    if mtype in SAMPLED_TYPES and outcome == "ok":
        window = _agg.add((side, direction, mtype, labels["drone_id"]), size or 0)
        if window is None:
            return
        count, nbytes, span_s = window
        log_event(_log, logging.INFO, "drone_comm", f"{direction} {mtype} x{count}", category="drone_comm",
                  component=side, direction=direction, type=mtype, peer=peer, drone_id=peer, aggregated=True,
                  count=count, bytes=nbytes, window_s=round(span_s, 2), rate_per_s=round(count / span_s, 2) if span_s else None)
        return
    fields = _fields(msg)
    fields.update(extra)
    level = logging.INFO
    if mtype == "emergency_land_all" or outcome not in ("ok",):
        level = logging.WARNING
    if mtype == "nack":
        level = logging.WARNING
    name = f"fleet.{'send' if direction == 'out' else 'recv'} {mtype}"
    if _tracer is not None:
        with _tracer.start_as_current_span(name) as span:
            span.set_attribute("flightrisk.side", side)
            span.set_attribute("flightrisk.msg.type", mtype)
            if peer:
                span.set_attribute("flightrisk.drone_id", peer)
            if size:
                span.set_attribute("messaging.message.body.size", size)
            span.set_attribute("flightrisk.outcome", outcome)
            _log_one(level, side, direction, mtype, peer, size, outcome, fields)
    else:
        _log_one(level, side, direction, mtype, peer, size, outcome, fields)


def _log_one(level, side, direction, mtype, peer, size, outcome, fields) -> None:
    log_event(_log, level, "drone_comm", f"{direction} {mtype} {peer or ''} {outcome}".strip(), category="drone_comm",
              component=side, direction=direction, type=mtype, peer=peer, drone_id=peer, size=size, outcome=outcome, **fields)


# ---------------------------------------------------------------------------
# Ground side
# ---------------------------------------------------------------------------

@_safe
def ground_received(msg: Any, size: int | None, drone_id: str | None = None) -> None:
    """A message the ground read from an edge (after JSON decoding)."""
    if not isinstance(msg, Mapping):
        return
    mtype = msg.get("type")
    peer = msg.get("drone_id") if isinstance(msg.get("drone_id"), str) else drone_id
    _record("ground", "in", msg, peer, size)
    if mtype == "telemetry" and isinstance(peer, str):
        from flightrisk import obs_events

        obs_events.edge_telemetry(peer, msg)
        link_age = msg.get("link_age_s")
        if isinstance(link_age, (int, float)) and not isinstance(link_age, bool):
            M.note_link(M.drone_label(peer), "edge_reported")
    elif mtype in ("ack", "nack"):
        M.comm_acks.add(1, {
            "kind": mtype,
            "for": type_label(msg.get("for")),
            "reason": _reason_label(msg.get("reason")),
            "drone_id": M.drone_label(peer),
        })


@_safe
def ground_sent(msg: Any, drone_id: str | None, size: int | None = None, outcome: str = "ok") -> None:
    """A control message the ground sent (or failed to send) to one drone."""
    if isinstance(msg, Mapping):
        _record("ground", "out", msg, drone_id, size, outcome)
        if outcome != "ok":
            M.comm_dropped.add(1, {"side": "ground", "type": type_label(msg.get("type")), "reason": safe_token(outcome)})


@_safe
def ground_broadcast(msg: Any, reached: int, clients: int, size: int | None = None) -> None:
    """A message the ground sent to every eligible edge (``reached`` of ``clients``)."""
    if not isinstance(msg, Mapping):
        return
    outcome = "ok" if reached == clients and clients > 0 else ("no_clients" if clients == 0 else "partial")
    if msg.get("type") in SAMPLED_TYPES and outcome == "no_clients":
        return  # a heartbeat with nobody connected is not news
    _record("ground", "out", msg, "broadcast", size, outcome, reached=reached, clients=clients)


@_safe
def link_state(drone_id: Any, connected: bool, side: str = "ground") -> None:
    """An edge connection for ``drone_id`` appeared or went away."""
    if not isinstance(drone_id, str) or not drone_id:
        return
    M.note_link_state(M.drone_label(drone_id), connected)
    if connected:
        M.note_link(M.drone_label(drone_id), side)
    log_event(_log, logging.INFO if connected else logging.WARNING, "drone_comm",
              f"link {'up' if connected else 'down'} {drone_id}", category="drone_comm", component=side,
              direction="link", type="link_up" if connected else "link_down", peer=drone_id, drone_id=drone_id)


@_safe
def clock_sync(drone_id: str | None, estimate: Any) -> None:
    """Result of a clock-sync run: median offset and round-trip time."""
    if estimate is None:
        return
    rtt_s = getattr(estimate, "rtt_s", None)
    offset_s = getattr(estimate, "offset_s", None)
    peer = drone_id or "unknown"
    if isinstance(rtt_s, (int, float)):
        M.comm_rtt.record(rtt_s * 1000.0, {"drone_id": M.drone_label(peer)})
    if isinstance(offset_s, (int, float)):
        M.clock_offset.set(offset_s * 1000.0, {"drone_id": M.drone_label(peer)})
    log_event(_log, logging.INFO, "drone_comm", f"clock sync {peer}", category="drone_comm", component="ground",
              direction="out", type="clock_sync", peer=peer, drone_id=peer,
              rtt_ms=round(rtt_s * 1000.0, 3) if isinstance(rtt_s, (int, float)) else None,
              offset_ms=round(offset_s * 1000.0, 3) if isinstance(offset_s, (int, float)) else None,
              rounds=getattr(estimate, "rounds", None))


# ---------------------------------------------------------------------------
# Edge side
# ---------------------------------------------------------------------------

@_safe
def edge_received(msg: Any, size: int | None) -> None:
    """A message the edge read from the ground."""
    if isinstance(msg, Mapping):
        _record("edge", "in", msg, _local_drone[0], size)
        if _local_drone[0]:
            M.note_link(M.drone_label(_local_drone[0]), "edge")


@_safe
def edge_sent(msg: Any, ok: bool = True, size: int | None = None) -> None:
    """A message the edge sent (or could not send) to the ground."""
    if not isinstance(msg, Mapping):
        return
    drone_id = msg.get("drone_id")
    if isinstance(drone_id, str) and drone_id:
        _local_drone[0] = drone_id
    _record("edge", "out", msg, _local_drone[0], size, "ok" if ok else "not_sent")
    if not ok:
        M.comm_dropped.add(1, {"side": "edge", "type": type_label(msg.get("type")), "reason": "not_sent"})


@_safe
def edge_outbox_dropped(queue: str) -> None:
    """A message dropped from a full per-connection outbox on the edge (multi-ground uplink)."""
    M.comm_dropped.add(1, {"side": "edge", "type": safe_token(queue), "reason": "outbox_full"})


@_safe
def edge_reconnect(outcome: str, attempt: int | None = None) -> None:
    """One edge reconnect attempt (``scheduled``, ``ok``, ``failed``, ``gave_up``)."""
    M.link_reconnects.add(1, {"outcome": safe_token(outcome)})
    if outcome in ("ok", "gave_up"):
        log_event(_log, logging.INFO if outcome == "ok" else logging.ERROR, "drone_comm", f"reconnect {outcome}",
                  category="drone_comm", component="edge", direction="link", type="reconnect", outcome=outcome,
                  attempt=attempt, drone_id=_local_drone[0])


__all__ = [
    "AGGREGATE_INTERVAL_S",
    "SAMPLED_TYPES",
    "clock_sync",
    "edge_received",
    "edge_reconnect",
    "edge_sent",
    "ground_broadcast",
    "ground_received",
    "ground_sent",
    "link_state",
]
