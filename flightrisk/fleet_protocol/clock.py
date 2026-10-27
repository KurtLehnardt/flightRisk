"""Clock synchronisation: ping/pong samples, the median offset and the edge clock."""
from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from flightrisk.fleet_protocol.checks import (
    ProtocolError,
    _is_number,
)
from flightrisk.fleet_protocol.constants import (
    CLOCK,
    PONG,
)
from flightrisk.fleet_protocol.validate import (
    validate_message,
)

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
