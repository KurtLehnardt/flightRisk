"""Fleet control protocol shared by the ground station and the edge devices.

This module is the single source of truth for the JSON control messages that
travel over the existing edge/ground WebSocket (see ``flightrisk/transport.py``)
when several drones fly as one coordinated fleet. It is deliberately pure: it
imports no networking code, so the fleet coordinator, the edge device and the
transport can all share it, and it can be unit-tested without sockets.

Ground -> edge messages::

    {"type": "ping", "t1": <ground_monotonic_s>}
    {"type": "clock", "offset_s": <median offset, edge_clock - ground_clock>}
    {"type": "assign_lanes", "drone_id": .., "alt_cm": 100,
     "lanes": [{"direction": "forward", "distance_cm": 200}, ...],
     "zone": {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}}
    {"type": "start_at", "t_ground_s": <ground_monotonic_s>}
    {"type": "takeoff"}
    {"type": "land"}
    {"type": "emergency_land_all"}
    {"type": "heartbeat", "seq": n}

Edge -> ground messages::

    {"type": "pong", "t1": .., "t2": <edge_recv>, "t3": <edge_send>}
    {"type": "telemetry", "drone_id": .., "battery": .., "height_cm": ..,
     "is_flying": .., "rel_x_cm": .., "rel_y_cm": .., "heading_deg": ..,
     "lane_index": .., "lanes_total": .., "link_age_s": ..,
     "takeoff_at_local_s": ..}
    {"type": "ack", "for": "start_at", "drone_id": ..,
     "executed_at_ground_s": <t_edge_exec - offset_s>}
    {"type": "ack", "for": "assign_lanes", "drone_id": .., "battery": <percent>,
     "lanes_total": <n>}
    {"type": "nack", "for": "assign_lanes", "drone_id": ..,
     "reason": "geofence|altitude|battery|other_drone|invalid",
     "lanes_total": <n received>}

An edge sends ``nack`` when it refuses a lane assignment, so the ground can
hand those lanes to another drone or report them as uncovered instead of
counting them as searched.

Clock convention (standard NTP): for one ping/pong round with ground send time
``t1``, edge receive time ``t2``, edge send time ``t3`` and ground receive time
``t4``, the offset is ``((t2 - t1) + (t3 - t4)) / 2``, which estimates
``edge_clock - ground_clock``, and the round-trip time is
``(t4 - t1) - (t3 - t2)``. The ground keeps the median of eight rounds and
sends that offset to the edge unchanged in a ``clock`` message. The edge then
fires a ``start_at`` when its own monotonic clock reaches
``t_ground_s + offset_s``, and reports the execution time back in ground time
as ``t_edge_exec - offset_s``.
"""
from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PING",
    "PONG",
    "CLOCK",
    "ASSIGN_LANES",
    "START_AT",
    "TAKEOFF",
    "LAND",
    "EMERGENCY_LAND_ALL",
    "HEARTBEAT",
    "TELEMETRY",
    "ACK",
    "NACK",
    "NACK_REASONS",
    "GROUND_TO_EDGE_TYPES",
    "EDGE_TO_GROUND_TYPES",
    "CLOCK_SYNC_ROUNDS",
    "CLOCK_SYNC_MIN_SAMPLES",
    "HEARTBEAT_INTERVAL_S",
    "TELEMETRY_INTERVAL_S",
    "PONG_BUDGET_S",
    "LANE_DIRECTIONS",
    "TELEMETRY_FIELDS",
    "ProtocolError",
    "ClockSample",
    "ClockEstimate",
    "EdgeClock",
    "make_ping",
    "make_pong",
    "answer_ping",
    "make_clock",
    "make_assign_lanes",
    "make_start_at",
    "make_takeoff",
    "make_land",
    "make_emergency_land_all",
    "make_heartbeat",
    "make_telemetry",
    "make_ack_start_at",
    "make_ack_assign_lanes",
    "make_nack_assign_lanes",
    "validate_message",
    "sample_from_pong",
    "estimate_clock",
    "ground_to_edge_s",
    "edge_to_ground_s",
    "start_skews_s",
]

# ---------------------------------------------------------------------------
# Message types and protocol constants
# ---------------------------------------------------------------------------

# Ground -> edge.
PING = "ping"
CLOCK = "clock"
ASSIGN_LANES = "assign_lanes"
START_AT = "start_at"
TAKEOFF = "takeoff"
LAND = "land"
EMERGENCY_LAND_ALL = "emergency_land_all"
HEARTBEAT = "heartbeat"

# Edge -> ground.
PONG = "pong"
TELEMETRY = "telemetry"
ACK = "ack"
NACK = "nack"

# Why an edge refused a lane assignment (design section 3).
NACK_REASONS = frozenset({"geofence", "altitude", "battery", "other_drone", "invalid"})

GROUND_TO_EDGE_TYPES = frozenset(
    {PING, CLOCK, ASSIGN_LANES, START_AT, TAKEOFF, LAND, EMERGENCY_LAND_ALL, HEARTBEAT}
)
# The original edge-to-ground set. ``NACK`` also travels edge to ground and is
# checked by ``validate_message``, but it is kept out of this set, which other
# modules and their tests treat as fixed.
EDGE_TO_GROUND_TYPES = frozenset({PONG, TELEMETRY, ACK})

# The ground runs this many ping/pong rounds per edge and keeps the median.
CLOCK_SYNC_ROUNDS = 8
# A sync with fewer successful rounds than this is rejected rather than
# trusted, because a median of one or two samples is not robust to jitter.
CLOCK_SYNC_MIN_SAMPLES = 4
# The ground sends a heartbeat this often; the edge link watchdog lands the
# drone when ground messages stop for FLIGHTRISK_LINK_LOSS_LAND_S.
HEARTBEAT_INTERVAL_S = 1.0
# The edge sends telemetry this often.
TELEMETRY_INTERVAL_S = 0.5
# The edge must answer a ping within this budget (edge receive to edge send).
PONG_BUDGET_S = 0.020

# Lanes are horizontal moves flown at the assigned altitude slot. Vertical
# moves are not lane directions, because they would break the altitude
# separation between drones.
LANE_DIRECTIONS = frozenset({"forward", "back", "left", "right"})

TELEMETRY_FIELDS = (
    "drone_id",
    "battery",
    "height_cm",
    "is_flying",
    "rel_x_cm",
    "rel_y_cm",
    "heading_deg",
    "lane_index",
    "lanes_total",
    "link_age_s",
    "takeoff_at_local_s",
)

_ZONE_FIELDS = ("x_cm", "y_cm", "w_cm", "h_cm")


class ProtocolError(ValueError):
    """A fleet control message does not match the protocol schema."""


# ---------------------------------------------------------------------------
# Field checks
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    """True for a finite int or float (bool is rejected even though it is an int)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require(msg: Mapping[str, Any], key: str) -> Any:
    if key not in msg:
        raise ProtocolError(f"{msg.get('type')!r} message is missing {key!r}")
    return msg[key]


def _require_number(msg: Mapping[str, Any], key: str) -> float:
    value = _require(msg, key)
    if not _is_number(value):
        raise ProtocolError(f"{msg.get('type')!r} field {key!r} must be a finite number, got {value!r}")
    return value


def _require_optional_number(msg: Mapping[str, Any], key: str) -> None:
    value = _require(msg, key)
    if value is not None and not _is_number(value):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be a finite number or null, got {value!r}"
        )


def _require_optional_int(msg: Mapping[str, Any], key: str) -> None:
    value = _require(msg, key)
    if value is not None and not (_is_int(value) and value >= 0):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be a non-negative integer or null, got {value!r}"
        )


def _require_drone_id(msg: Mapping[str, Any]) -> str:
    value = _require(msg, "drone_id")
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{msg.get('type')!r} field 'drone_id' must be a non-empty string, got {value!r}")
    return value


def _validate_lanes(msg: Mapping[str, Any]) -> None:
    lanes = _require(msg, "lanes")
    if not isinstance(lanes, list):
        raise ProtocolError(f"'assign_lanes' field 'lanes' must be a list, got {lanes!r}")
    for index, lane in enumerate(lanes):
        if not isinstance(lane, Mapping):
            raise ProtocolError(f"'assign_lanes' lane {index} must be an object, got {lane!r}")
        direction = lane.get("direction")
        if direction not in LANE_DIRECTIONS:
            raise ProtocolError(
                f"'assign_lanes' lane {index} direction must be one of "
                f"{sorted(LANE_DIRECTIONS)}, got {direction!r}"
            )
        distance = lane.get("distance_cm")
        if not _is_number(distance) or distance <= 0:
            raise ProtocolError(
                f"'assign_lanes' lane {index} distance_cm must be a positive number, got {distance!r}"
            )


def _validate_zone(msg: Mapping[str, Any]) -> None:
    zone = _require(msg, "zone")
    if not isinstance(zone, Mapping):
        raise ProtocolError(f"'assign_lanes' field 'zone' must be an object, got {zone!r}")
    for key in _ZONE_FIELDS:
        value = zone.get(key)
        if not _is_number(value):
            raise ProtocolError(f"'assign_lanes' zone field {key!r} must be a finite number, got {value!r}")
    if zone["w_cm"] < 0 or zone["h_cm"] < 0:
        raise ProtocolError("'assign_lanes' zone width and height must not be negative")


def validate_message(msg: Any) -> str:
    """Check a fleet control message against the protocol schema.

    Returns the message type when the message is a valid fleet control
    message. Raises ``ProtocolError`` when the message is not an object, has
    no string ``type``, or is a known fleet type whose fields do not match the
    schema. Message types this protocol does not define (for example the
    legacy ``detections``, ``set_target`` and ``stream_video`` messages) are
    returned unchecked, so callers decide for themselves how to treat them.
    """
    if not isinstance(msg, Mapping):
        raise ProtocolError(f"control message must be an object, got {type(msg).__name__}")
    mtype = msg.get("type")
    if not isinstance(mtype, str) or not mtype:
        raise ProtocolError(f"control message has no string 'type': {mtype!r}")

    if mtype == PING:
        _require_number(msg, "t1")
    elif mtype == PONG:
        for key in ("t1", "t2", "t3"):
            _require_number(msg, key)
    elif mtype == CLOCK:
        _require_number(msg, "offset_s")
    elif mtype == ASSIGN_LANES:
        _require_drone_id(msg)
        alt = _require_number(msg, "alt_cm")
        if alt <= 0:
            raise ProtocolError(f"'assign_lanes' field 'alt_cm' must be positive, got {alt!r}")
        _validate_lanes(msg)
        _validate_zone(msg)
    elif mtype == START_AT:
        _require_number(msg, "t_ground_s")
    elif mtype == HEARTBEAT:
        seq = _require(msg, "seq")
        if not (_is_int(seq) and seq >= 0):
            raise ProtocolError(f"'heartbeat' field 'seq' must be a non-negative integer, got {seq!r}")
    elif mtype in (TAKEOFF, LAND, EMERGENCY_LAND_ALL):
        pass
    elif mtype == TELEMETRY:
        _require_drone_id(msg)
        is_flying = _require(msg, "is_flying")
        if not isinstance(is_flying, bool):
            raise ProtocolError(f"'telemetry' field 'is_flying' must be a boolean, got {is_flying!r}")
        for key in ("battery", "height_cm", "rel_x_cm", "rel_y_cm", "heading_deg",
                    "link_age_s", "takeoff_at_local_s"):
            _require_optional_number(msg, key)
        for key in ("lane_index", "lanes_total"):
            _require_optional_int(msg, key)
    elif mtype == ACK:
        target = _require(msg, "for")
        if not isinstance(target, str) or not target:
            raise ProtocolError(f"'ack' field 'for' must be a non-empty string, got {target!r}")
        _require_drone_id(msg)
        if target == START_AT:
            _require_number(msg, "executed_at_ground_s")
        elif target == ASSIGN_LANES:
            _require_number(msg, "battery")
            lanes_total = _require(msg, "lanes_total")
            if not (_is_int(lanes_total) and lanes_total >= 0):
                raise ProtocolError(
                    f"'ack' for 'assign_lanes' field 'lanes_total' must be a non-negative integer, "
                    f"got {lanes_total!r}"
                )
    elif mtype == NACK:
        target = _require(msg, "for")
        if target != ASSIGN_LANES:
            raise ProtocolError(f"'nack' field 'for' must be {ASSIGN_LANES!r}, got {target!r}")
        _require_drone_id(msg)
        reason = _require(msg, "reason")
        if not isinstance(reason, str) or reason not in NACK_REASONS:
            raise ProtocolError(
                f"'nack' field 'reason' must be one of {sorted(NACK_REASONS)}, got {reason!r}"
            )
        lanes_total = _require(msg, "lanes_total")
        if not (_is_int(lanes_total) and lanes_total >= 0):
            raise ProtocolError(
                f"'nack' field 'lanes_total' must be a non-negative integer, got {lanes_total!r}"
            )
    return mtype


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------
#
# Every builder validates what it produces, so a malformed message fails
# loudly at the sender instead of being dropped by the receiver.


def _checked(msg: dict[str, Any]) -> dict[str, Any]:
    validate_message(msg)
    return msg


def make_ping(t1: float | None = None) -> dict[str, Any]:
    """Ground -> edge clock probe; ``t1`` defaults to ``time.monotonic()``."""
    return _checked({"type": PING, "t1": time.monotonic() if t1 is None else t1})


def make_pong(t1: float, t2: float, t3: float) -> dict[str, Any]:
    """Edge -> ground reply carrying the echoed ``t1`` and the edge's ``t2``/``t3``."""
    return _checked({"type": PONG, "t1": t1, "t2": t2, "t3": t3})


def answer_ping(
    ping: Mapping[str, Any],
    t2: float,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Build the pong for *ping*, which the edge received at edge time *t2*.

    ``t3`` is read from *clock* (the edge's monotonic clock) as the last step,
    so the caller should send the returned message straight away.
    """
    if ping.get("type") != PING:
        raise ProtocolError(f"answer_ping expects a 'ping' message, got {ping.get('type')!r}")
    t1 = _require_number(ping, "t1")
    return make_pong(t1, t2, clock())


def make_clock(offset_s: float) -> dict[str, Any]:
    """Ground -> edge: the median offset (edge clock minus ground clock), unchanged."""
    return _checked({"type": CLOCK, "offset_s": offset_s})


def make_assign_lanes(
    drone_id: str,
    alt_cm: float,
    lanes: Iterable[Mapping[str, Any]],
    zone: Mapping[str, Any],
) -> dict[str, Any]:
    """Ground -> edge lane assignment for one drone."""
    return _checked({
        "type": ASSIGN_LANES,
        "drone_id": drone_id,
        "alt_cm": alt_cm,
        "lanes": [dict(lane) for lane in lanes],
        "zone": dict(zone),
    })


def make_start_at(t_ground_s: float) -> dict[str, Any]:
    """Ground -> edge: start (take off, then fly lanes) at this ground monotonic time."""
    return _checked({"type": START_AT, "t_ground_s": t_ground_s})


def make_takeoff() -> dict[str, Any]:
    return {"type": TAKEOFF}


def make_land() -> dict[str, Any]:
    return {"type": LAND}


def make_emergency_land_all() -> dict[str, Any]:
    """Ground -> every edge: land immediately, ahead of anything else."""
    return {"type": EMERGENCY_LAND_ALL}


def make_heartbeat(seq: int) -> dict[str, Any]:
    return _checked({"type": HEARTBEAT, "seq": seq})


def make_telemetry(
    drone_id: str,
    battery: float | None,
    height_cm: float | None,
    is_flying: bool,
    rel_x_cm: float | None,
    rel_y_cm: float | None,
    heading_deg: float | None,
    lane_index: int | None,
    lanes_total: int | None,
    link_age_s: float | None,
    takeoff_at_local_s: float | None,
) -> dict[str, Any]:
    """Edge -> ground state report, sent every ``TELEMETRY_INTERVAL_S``."""
    return _checked({
        "type": TELEMETRY,
        "drone_id": drone_id,
        "battery": battery,
        "height_cm": height_cm,
        "is_flying": is_flying,
        "rel_x_cm": rel_x_cm,
        "rel_y_cm": rel_y_cm,
        "heading_deg": heading_deg,
        "lane_index": lane_index,
        "lanes_total": lanes_total,
        "link_age_s": link_age_s,
        "takeoff_at_local_s": takeoff_at_local_s,
    })


def make_ack_start_at(drone_id: str, executed_at_ground_s: float) -> dict[str, Any]:
    """Edge -> ground: the edge executed ``start_at`` at this time, in ground time."""
    return _checked({
        "type": ACK,
        "for": START_AT,
        "drone_id": drone_id,
        "executed_at_ground_s": executed_at_ground_s,
    })


def make_ack_assign_lanes(drone_id: str, battery: float, lanes_total: int) -> dict[str, Any]:
    """Edge -> ground: the edge accepted its lane assignment."""
    return _checked({
        "type": ACK,
        "for": ASSIGN_LANES,
        "drone_id": drone_id,
        "battery": battery,
        "lanes_total": lanes_total,
    })


def make_nack_assign_lanes(drone_id: str, reason: str, lanes_total: int) -> dict[str, Any]:
    """Edge -> ground: the edge refused its lane assignment for ``reason``.

    ``lanes_total`` is the number of lanes in the refused assignment, so the
    ground can tell which of its assignments was refused.
    """
    return _checked({
        "type": NACK,
        "for": ASSIGN_LANES,
        "drone_id": drone_id,
        "reason": reason,
        "lanes_total": lanes_total,
    })


# ---------------------------------------------------------------------------
# Clock synchronisation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClockSample:
    """One ping/pong round.

    ``t1`` and ``t4`` are on the ground's monotonic clock; ``t2`` and ``t3``
    are on the edge's monotonic clock.
    """

    t1: float
    t2: float
    t3: float
    t4: float

    @property
    def offset_s(self) -> float:
        """Estimated edge clock minus ground clock for this round."""
        return ((self.t2 - self.t1) + (self.t3 - self.t4)) / 2.0

    @property
    def rtt_s(self) -> float:
        """Network round-trip time, excluding the edge's turnaround."""
        return (self.t4 - self.t1) - (self.t3 - self.t2)

    @property
    def turnaround_s(self) -> float:
        """Time the edge took between receiving the ping and sending the pong."""
        return self.t3 - self.t2


@dataclass(frozen=True)
class ClockEstimate:
    """Median clock offset and round-trip time over several rounds."""

    offset_s: float
    rtt_s: float
    rounds: int
    measured_at_s: float | None = None
    samples: tuple[ClockSample, ...] = ()


def sample_from_pong(pong: Mapping[str, Any], t4: float) -> ClockSample:
    """Turn a received pong (ground receive time *t4*) into a ``ClockSample``.

    Raises ``ProtocolError`` for a malformed pong or for timestamps that run
    backwards on either clock, since such a round cannot be trusted.
    """
    if pong.get("type") != PONG:
        raise ProtocolError(f"expected a 'pong' message, got {pong.get('type')!r}")
    validate_message(pong)
    if not _is_number(t4):
        raise ProtocolError(f"t4 must be a finite number, got {t4!r}")
    sample = ClockSample(t1=pong["t1"], t2=pong["t2"], t3=pong["t3"], t4=t4)
    if sample.t3 < sample.t2:
        raise ProtocolError("pong t3 (edge send) is earlier than t2 (edge receive)")
    if sample.t4 < sample.t1:
        raise ProtocolError("pong t4 (ground receive) is earlier than t1 (ground send)")
    return sample


def estimate_clock(
    samples: Iterable[ClockSample],
    measured_at_s: float | None = None,
) -> ClockEstimate:
    """Median offset and median round-trip time over *samples*.

    Raises ``ProtocolError`` when *samples* is empty.
    """
    samples = list(samples)
    if not samples:
        raise ProtocolError("cannot estimate the clock offset from zero samples")
    return ClockEstimate(
        offset_s=statistics.median(s.offset_s for s in samples),
        rtt_s=statistics.median(s.rtt_s for s in samples),
        rounds=len(samples),
        measured_at_s=measured_at_s,
        samples=tuple(samples),
    )


def ground_to_edge_s(t_ground_s: float, offset_s: float) -> float:
    """Convert a ground monotonic time to the edge's monotonic clock."""
    return t_ground_s + offset_s


def edge_to_ground_s(t_edge_s: float, offset_s: float) -> float:
    """Convert an edge monotonic time to the ground's monotonic clock."""
    return t_edge_s - offset_s


def start_skews_s(executed_at_ground_s: Mapping[str, float]) -> dict[str, float]:
    """Per-drone start skew: each drone's start time minus the earliest one."""
    if not executed_at_ground_s:
        return {}
    earliest = min(executed_at_ground_s.values())
    return {drone_id: t - earliest for drone_id, t in executed_at_ground_s.items()}


class EdgeClock:
    """The edge's view of the ground clock.

    Holds the offset from the latest ``clock`` message and converts between
    ground time and the edge's own monotonic clock. ``offset_s`` is ``None``
    until the first ``clock`` message arrives; conversions raise
    ``RuntimeError`` until then, so an edge can never act on a ``start_at``
    with an unknown offset by accident.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._offset_s: float | None = None

    @property
    def offset_s(self) -> float | None:
        return self._offset_s

    @property
    def synced(self) -> bool:
        return self._offset_s is not None

    def now(self) -> float:
        """The edge's monotonic clock."""
        return self._clock()

    def apply(self, msg: Mapping[str, Any]) -> float:
        """Store the offset from a ``clock`` message and return it."""
        if msg.get("type") != CLOCK:
            raise ProtocolError(f"expected a 'clock' message, got {msg.get('type')!r}")
        validate_message(msg)
        self._offset_s = float(msg["offset_s"])
        return self._offset_s

    def _require_offset(self) -> float:
        offset = self._offset_s
        if offset is None:
            raise RuntimeError("edge clock is not synchronised: no 'clock' message received yet")
        return offset

    def to_local(self, t_ground_s: float) -> float:
        """Edge monotonic time at which the ground clock reads *t_ground_s*."""
        return ground_to_edge_s(t_ground_s, self._require_offset())

    def to_ground(self, t_edge_s: float) -> float:
        """Ground monotonic time corresponding to edge time *t_edge_s*."""
        return edge_to_ground_s(t_edge_s, self._require_offset())

    def seconds_until(self, t_ground_s: float) -> float:
        """Seconds from now (edge clock) until the ground clock reads *t_ground_s*."""
        return self.to_local(t_ground_s) - self.now()
