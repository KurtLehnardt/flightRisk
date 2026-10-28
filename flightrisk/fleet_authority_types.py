"""Values and constants of the edge authority (``flightrisk.fleet_authority``).

Split out of ``fleet_authority.py`` to keep it under the file size limit:
the ``Decision`` actions and record, the executor hook protocol, the
callable ``EpochSeen`` and the authority's per-connection and per-command
records. ``flightrisk.fleet_authority`` re-exports the public names.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from flightrisk import fleet_protocol as fp

__all__ = [
    "ACCEPT",
    "CLOSE",
    "CLOSE_LOG_EVERY",
    "EPOCH_ADOPT_HEADROOM",
    "HANDLED",
    "IGNORE",
    "LAND",
    "LANDING_LOG_INTERVAL_S",
    "REFUSE",
    "Decision",
    "EpochSeen",
    "ExecutorHooks",
]

# Decision actions.
LAND = "land"        # execute a landing (always)
ACCEPT = "accept"    # deliver to the executor (or answer the ping)
HANDLED = "handled"  # handled here (a hello or a handover); only the replies go out
REFUSE = "refuse"    # a command was refused; the nack (when one can be sent) is in replies
IGNORE = "ignore"    # dropped without a reply
CLOSE = "close"      # close this connection (protocol error)

# An epoch this close to MAX_EPOCH is adopted only when a handover from the
# current authority announced it: no leader could take over after it.
EPOCH_ADOPT_HEADROOM = 1024
_MAX_UNANNOUNCED_EPOCH = fp.MAX_EPOCH - EPOCH_ADOPT_HEADROOM

# Manual-takeover commands (``fleet_protocol.manual``) are fenced like the
# other flight commands. ``manual_hold``, like ``hold``, only stops the drone.
_MOTION_TYPES = (fp.FENCED_COMMAND_TYPES - {fp.HANDOVER}) | fp.MANUAL_COMMAND_TYPES
_STOP_TYPES = frozenset({fp.HOLD, fp.MANUAL_HOLD})
_PLAN_BOUND_TYPES = frozenset({fp.START_AT, fp.RESUME})
# Configuration commands (a detection profile switch) are obeyed only from
# the authority, like a video toggle; they never move the drone.
_UNFENCED_AUTHORITY_TYPES = frozenset({fp.PING}) | fp.LEGACY_GROUND_TO_EDGE_TYPES | fp.CONFIG_COMMAND_TYPES
# ``set_ceiling`` is taken only from the authority's connection too; the
# executor then checks its fence against the epoch the edge obeys.
_UNFENCED_AUTHORITY_TYPES |= {fp.SET_CEILING}
_FLEET_TYPES = fp.FENCED_COMMAND_TYPES | fp.FENCED_CONTROL_TYPES | fp.MANUAL_COMMAND_TYPES
_REFUSAL_EVENTS = {"epoch_conflict_in_flight": "epoch_conflict_in_flight", "stale_epoch": "fleet_stale_epoch_rejected"}
# A ground that floods landings must not flood the log: per connection and
# landing type, at most one ``edge_landing_command`` per this many seconds.
LANDING_LOG_INTERVAL_S = 1.0
# ``edge_connection_refused`` is logged for the first refusal of each
# (connection label, reason) and then every this many.
CLOSE_LOG_EVERY = 20


class EpochSeen(int):
    """``epoch_seen`` as a plain ``int`` that can also be called.

    ``EdgeAuthority.epoch_seen`` has always been a property; PR-5's executor
    calls ``authority.epoch_seen()``. Calling the value returns it as an
    ``int``, so both spellings read the same epoch.
    """

    __slots__ = ()

    def __call__(self) -> int:
        return int(self)


@dataclass(frozen=True)
class Decision:
    """What to do with one received message.

    ``replies`` go back on the same connection, in order, already complete
    for the connection's agreed protocol version. ``admitted`` is the
    authority (epoch, leader) under which an accepted message was taken; the
    uplink stores it with the message and passes it to ``begin_dispatch``.
    """

    action: str
    feed_watchdog: bool = False
    replies: tuple[dict[str, Any], ...] = ()
    reason: str = ""
    admitted: tuple[int, str | None] | None = None


class ExecutorHooks(Protocol):
    """The minimal executor interface the authority needs (see the module docstring)."""

    def is_active(self) -> bool: ...

    def supports(self, mtype: str) -> bool: ...

    def snapshot(self) -> Mapping[str, Any]: ...

    def fleet_status(self) -> Mapping[str, Any] | None: ...

    def invalidate_plan(self, reason: str) -> None: ...


class _GroundedHooks:
    """Hooks for an edge without a drone: never active, executes nothing."""

    def is_active(self) -> bool:
        return False

    def supports(self, mtype: str) -> bool:
        return False

    def snapshot(self) -> Mapping[str, Any]:
        return {}

    def fleet_status(self) -> Mapping[str, Any] | None:
        return None

    def invalidate_plan(self, reason: str) -> None:
        return None


@dataclass
class _Conn:
    label: str
    node_id: str | None = None
    role: str | None = None
    epoch: int | None = None
    proto: int | None = None  # agreed by the first hello
    v1: bool = False          # first fleet message was not a hello


@dataclass
class _CmdEntry:
    mtype: str
    node_id: str | None
    epoch: int | None
    plan_id: str | None
    reply: dict[str, Any] | None = None


@dataclass(frozen=True)
class _Handover:
    to_node: str
    epoch_next: int
    from_epoch: int


def _clean_battery(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if 0 <= value <= 100 else None


def _clean_lane_index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
