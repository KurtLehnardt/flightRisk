"""Edge-side fleet authority: which ground station a drone's edge obeys.

In the two-laptop fleet (plan section 2) every edge connects to every
ground station but obeys at most one of them, its *authority*. For every
message an edge receives, ``EdgeAuthority.on_message`` returns a
``Decision``: execute a landing, accept, handle here (``hello``,
``handover``), refuse with a ``nack``, ignore, or close the connection. The
class does no I/O: ``flightrisk.edge_uplink`` decodes frames, feeds them in
and carries out the decisions, so the rules are testable without sockets.

The rules are listed in ``docs/fleet-protocol-v2.md`` ("Edge authority").
In short: landing is never fenced and never nacked; a connection must send a
ground ``hello`` before anything but a landing counts; the edge adopts a
higher leader epoch only while the drone is inactive (or through an
announced in-flight handover), keeps the first leader it accepted at an
epoch, and obeys fenced commands, heartbeats, clocks, pings, targets and
video toggles only from that authority at ``epoch_seen``; only the
authority's messages feed the link watchdog; a command id runs at most once.

Executor interface (``ExecutorHooks``, adapted for ``LaneExecutor`` by
``flightrisk.edge_uplink.LaneExecutorHooks``): ``is_active()`` (flying,
taking off, landing or a flight sequence pending; an error counts as
active), ``supports(type)`` (the executor implements that command; others
are refused with ``invalid``), ``snapshot()`` (``is_flying``,
``lane_index``, ``battery`` for the edge ``hello``), ``fleet_status()``
(optional ``plan_id``, ``state``, ``plan_valid`` for v2 telemetry) and
``invalidate_plan(reason)`` (called after a new epoch is adopted).

PR-5's lane executor takes an ``authority=`` object with ``check(msg)`` and
``epoch_seen()``; ``EdgeAuthority`` provides both (``epoch_seen`` is an
``int`` that can also be called), so it can be passed there directly.

Thread safety: every public method may be called from any thread. Hooks
(``supports`` included) are never called while the authority's lock is
held, so the executor may call ``outbound`` while holding its own lock, and
a hook may take the executor's lock. Privacy: log events carry ids, epochs,
types, reasons and sizes only, never message bodies, embeddings, frames or
the token.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable, Mapping
from dataclasses import replace
from typing import Any

from flightrisk import fleet_protocol as fp
from flightrisk.fleet_authority_types import (
    _FLEET_TYPES,
    _MAX_UNANNOUNCED_EPOCH,
    _MOTION_TYPES,
    _PLAN_BOUND_TYPES,
    _REFUSAL_EVENTS,
    _UNFENCED_AUTHORITY_TYPES,
    ACCEPT,
    CLOSE,
    CLOSE_LOG_EVERY,
    EPOCH_ADOPT_HEADROOM,
    HANDLED,
    IGNORE,
    LAND,
    LANDING_LOG_INTERVAL_S,
    REFUSE,
    Decision,
    EpochSeen,
    ExecutorHooks,
    _clean_battery,
    _clean_lane_index,
    _CmdEntry,
    _Conn,
    _GroundedHooks,
    _Handover,
)
from flightrisk.observability import StructuredLogger

__all__ = [
    "ACCEPT",
    "CLOSE",
    "EPOCH_ADOPT_HEADROOM",
    "HANDLED",
    "IGNORE",
    "LAND",
    "REFUSE",
    "Decision",
    "EdgeAuthority",
    "EpochSeen",
    "ExecutorHooks",
]

class EdgeAuthority:
    """Decides which ground station this edge obeys; see the module docstring."""

    def __init__(
        self,
        drone_id: str,
        node_id: str,
        *,
        hooks: ExecutorHooks | None = None,
        legacy: bool = False,
        inflight_handover: bool = False,
        logger: StructuredLogger | None = None,
        cmd_cache_size: int = fp.CMD_ID_CACHE_SIZE,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not fp.is_identifier(drone_id):
            raise ValueError(f"drone_id must be a protocol identifier, got {drone_id!r}")
        if not fp.is_identifier(node_id):
            raise ValueError(f"node_id must be a protocol identifier, got {node_id!r}")
        self.drone_id = drone_id
        self.node_id = node_id
        self.legacy = bool(legacy)
        self.inflight_handover = bool(inflight_handover)
        self._hooks: ExecutorHooks = hooks if hooks is not None else _GroundedHooks()
        self._log = logger if logger is not None else StructuredLogger(component="edge-authority")
        self._cache_size = max(int(cmd_cache_size), fp.CMD_ID_CACHE_SIZE)
        self._clock = clock

        self._lock = threading.RLock()
        self._tls = threading.local()
        self._conns: dict[Hashable, _Conn] = {}
        self._epoch_seen = 0
        self._authority_leader: str | None = None
        self._authority_key: Hashable | None = None
        self._handover: _Handover | None = None
        # (epoch, plan_id) of the plan the executor acked in the current epoch.
        self._plan: tuple[int, str] | None = None
        self._cache: OrderedDict[str, _CmdEntry] = OrderedDict()
        # Latest accepted cmd_id per command type, for stamping executor
        # replies that do not echo one (a start_at ack comes from a worker).
        self._latest: dict[str, str] = {}
        # True while a motion command from the authority is being dispatched
        # to the executor (between recv() handing it out and the next recv()).
        self._dispatching = False
        # Bumped whenever a motion dispatch ends (_dispatching goes back to
        # False). on_message reads it before sampling the hooks; a change by
        # the time it holds the lock means a motion command finished being
        # dispatched in between (it may have launched a takeoff after the
        # sample), so the drone counts as active. A dispatch that started in
        # between and is still running is caught by _dispatching itself.
        self._dispatch_gen = 0
        self._refusal_counts: dict[tuple[str, str], int] = {}
        self._close_counts: dict[tuple[str, str], int] = {}
        # (label, type) -> (last logged at, landings not logged since).
        self._landing_log: dict[tuple[str, str], tuple[float, int]] = {}

    # -- Introspection -----------------------------------------------------

    @property
    def epoch_seen(self) -> EpochSeen:
        """The leader epoch this edge obeys (0 before any); also callable, see ``EpochSeen``."""
        with self._lock:
            return EpochSeen(self._epoch_seen)

    @property
    def authority_leader(self) -> str | None:
        with self._lock:
            return self._authority_leader

    @property
    def authority_key(self) -> Hashable | None:
        with self._lock:
            return self._authority_key

    def bind_hooks(self, hooks: ExecutorHooks) -> None:
        """Attach the executor hooks once the executor exists."""
        with self._lock:
            self._hooks = hooks

    def proto_for(self, key: Hashable) -> int:
        """The protocol version to decode and validate with on *key* (1 until a hello)."""
        with self._lock:
            conn = self._conns.get(key)
            return conn.proto if conn is not None and conn.proto is not None else 1

    def broadcast_keys(self) -> list[Hashable]:
        """Connections that receive telemetry, acks and nacks.

        Those that agreed a version in a ``hello``, plus, in legacy mode,
        monitor-only v1 links. Nothing is sent to a connection before then.
        """
        with self._lock:
            return [
                key for key, conn in self._conns.items()
                if conn.proto is not None or (self.legacy and conn.v1)
            ]

    # -- Connection lifecycle ----------------------------------------------

    def opened(self, key: Hashable, label: str = "") -> None:
        """A new connection (a reconnect is a new connection, with a fresh hello state)."""
        with self._lock:
            if key in self._conns:
                self.closed(key)
            self._conns[key] = _Conn(label=label)

    def closed(self, key: Hashable) -> None:
        """A connection ended. The authority's epoch and leader are kept."""
        with self._lock:
            conn = self._conns.pop(key, None)
            if key == self._authority_key:
                self._authority_key = None
                self._log.warning(
                    "edge_authority_disconnected",
                    drone_id=self.drone_id,
                    leader_id=self._authority_leader,
                    epoch=self._epoch_seen,
                    url=conn.label if conn else None,
                )

    # -- Inbound -----------------------------------------------------------

    def on_decode_error(self, key: Hashable, size: int) -> Decision:
        """A frame failed strict decoding and was not a landing: dropped."""
        with self._lock:
            conn = self._conns.get(key)
            self._note_refusal(conn, "undecodable", "decode_error", size=size)
        return Decision(IGNORE, reason="decode_error")

    def on_message(self, key: Hashable, msg: Mapping[str, Any]) -> Decision:
        """Decide what to do with one decoded ground-to-edge message from *key*."""
        mtype = msg.get("type")
        # Hooks are sampled before taking the lock (they never run under it):
        # is_active only when the message could adopt an epoch (unsampled
        # counts as active), the snapshot for a hello, supports for a
        # command. The dispatch generation is read first; if it changed by
        # the time the lock is held, a motion command was dispatched in
        # between and the is_active sample may predate its takeoff.
        with self._lock:
            generation = self._dispatch_gen
        active = True
        snapshot: Mapping[str, Any] = {}
        supported = False
        if mtype == fp.HELLO or (mtype == fp.HEARTBEAT and self._may_adopt(msg)):
            active = self._hooks_active()
        if mtype == fp.HELLO:
            snapshot = self._hooks_snapshot()
        if mtype in _MOTION_TYPES:
            supported = self._supports(mtype)
        post: list[Callable[[], None]] = []
        with self._lock:
            if self._dispatch_gen != generation:
                active = True
            conn = self._conns.get(key)
            if conn is None:
                # Never opened, or already closed: a landing still lands
                # (without a reply, the connection is gone); nothing else
                # counts, and no state is kept for the dead connection.
                if mtype in fp.FAILSAFE_TYPES:
                    decision = self._landing(key, _Conn(label=""), msg, mtype)
                else:
                    decision = Decision(IGNORE, reason="connection_closed")
            elif mtype in fp.FAILSAFE_TYPES:
                decision = self._landing(key, conn, msg, mtype)
            elif mtype == fp.HELLO:
                decision = self._hello(key, conn, msg, active, snapshot, post)
            elif conn.proto is None:
                decision = self._before_hello(conn, mtype)
            elif mtype in _UNFENCED_AUTHORITY_TYPES:
                if key == self._authority_key:
                    decision = Decision(ACCEPT, feed_watchdog=True)
                else:
                    self._note_refusal(conn, mtype, "not_authority")
                    decision = Decision(IGNORE, reason="not_authority")
            elif mtype in fp.FENCED_CONTROL_TYPES:
                decision = self._control(key, conn, msg, mtype, active, post)
            elif mtype in fp.FENCED_COMMAND_TYPES:
                decision = self._command(key, conn, msg, mtype, supported)
            else:
                decision = Decision(IGNORE, reason="unexpected_type")
            if decision.action in (ACCEPT, LAND):
                decision = replace(decision, admitted=(self._epoch_seen, self._authority_leader))
        for action in post:
            action()
        return decision

    def _may_adopt(self, msg: Mapping[str, Any]) -> bool:
        fence = fp.fence_of(msg)
        with self._lock:
            return fence is not None and (fence.epoch > self._epoch_seen or self._authority_key is None)

    def _hooks_active(self) -> bool:
        try:
            return bool(self._hooks.is_active())
        except Exception:  # noqa: BLE001 - an executor error counts as active
            return True

    def _hooks_snapshot(self) -> Mapping[str, Any]:
        try:
            return dict(self._hooks.snapshot())
        except Exception:  # noqa: BLE001 - no snapshot, hello carries nulls
            return {}

    def _landing(self, key: Hashable, conn: _Conn, msg: Mapping[str, Any], mtype: str) -> Decision:
        self._log_landing(key, conn, msg, mtype)
        # The drone is landing: the plan it was flying is over, so a start_at
        # or resume naming it is refused (no_valid_plan) until a new plan is
        # acked.
        self._plan = None
        replies: tuple[dict[str, Any], ...] = ()
        if conn.proto is not None and conn.proto >= 2:
            replies = (fp.make_ack(mtype, self.drone_id, fp.cmd_id_of(msg), epoch_seen=self._epoch_seen),)
        return Decision(LAND, feed_watchdog=key == self._authority_key, replies=replies, reason=mtype)

    def _log_landing(self, key: Hashable, conn: _Conn, msg: Mapping[str, Any], mtype: str) -> None:
        """Log a landing, at most once per LANDING_LOG_INTERVAL_S per connection and type."""
        slot = (conn.label, mtype)
        now = self._clock()
        last, skipped = self._landing_log.get(slot, (None, 0))
        if last is not None and now - last < LANDING_LOG_INTERVAL_S:
            self._landing_log[slot] = (last, skipped + 1)
            return
        self._landing_log[slot] = (now, 0)
        fence = fp.fence_of(msg)
        self._log.warning(
            "edge_landing_command", drone_id=self.drone_id, type=mtype, from_node=conn.node_id,
            from_authority=key == self._authority_key, epoch=fence.epoch if fence else None,
            epoch_seen=self._epoch_seen, not_logged_since=skipped,
        )

    def _hello(
        self,
        key: Hashable,
        conn: _Conn,
        msg: Mapping[str, Any],
        active: bool,
        snapshot: Mapping[str, Any],
        post: list[Callable[[], None]],
    ) -> Decision:
        try:
            hello = fp.parse_hello(msg)
        except fp.ProtocolError:
            return Decision(IGNORE, reason="invalid_hello")
        if not isinstance(hello, fp.GroundHello):
            return Decision(IGNORE, reason="edge_hello_from_ground")
        if conn.v1:
            return self._close(conn, "hello_on_v1_link")
        if conn.node_id is not None and hello.node_id != conn.node_id:
            return self._close(conn, "node_id_changed", node_id=hello.node_id, was=conn.node_id)
        try:
            agreed = fp.negotiate_proto(hello.proto, local_min=1 if self.legacy else 2, current=conn.proto)
        except fp.ProtocolError:
            return self._close(conn, "protocol_refused", proto=hello.proto, was=conn.proto)
        first = conn.proto is None
        conn.node_id, conn.role, conn.epoch, conn.proto = hello.node_id, hello.role, hello.epoch, agreed
        if first:
            self._log.info("edge_ground_hello", drone_id=self.drone_id, url=conn.label, node_id=hello.node_id,
                           role=hello.role, epoch=hello.epoch, proto=agreed)
        if hello.role == "leader":
            self._try_adopt(key, conn, hello.epoch, hello.node_id, "hello", active, post)
        elif key == self._authority_key:
            self._authority_key = None
            self._log.warning("edge_authority_released", drone_id=self.drone_id,
                              leader_id=self._authority_leader, epoch=self._epoch_seen)
        reply = self._edge_hello(agreed, snapshot)
        return Decision(HANDLED, feed_watchdog=key == self._authority_key, replies=(reply,), reason="hello")

    def _edge_hello(self, proto: int, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        plan = self._plan[1] if self._plan is not None and self._plan[0] == self._epoch_seen else None
        return fp.make_edge_hello(
            self.drone_id,
            self.node_id,
            self._epoch_seen,
            plan_id=plan,
            lane_index=_clean_lane_index(snapshot.get("lane_index")),
            is_flying=snapshot.get("is_flying") is True,
            battery=_clean_battery(snapshot.get("battery")),
            proto=proto,
        )

    def _before_hello(self, conn: _Conn, mtype: Any) -> Decision:
        if mtype in _FLEET_TYPES:
            conn.v1 = True
            if not self.legacy:
                return self._close(conn, "v1_link_refused", type=mtype)
        self._note_refusal(conn, str(mtype), "no_hello")
        return Decision(IGNORE if mtype not in fp.FENCED_COMMAND_TYPES else REFUSE, reason="no_hello")

    def _close(self, conn: _Conn, reason: str, **fields: Any) -> Decision:
        slot = (conn.label, reason)
        count = self._close_counts.get(slot, 0) + 1
        self._close_counts[slot] = count
        if count == 1 or count % CLOSE_LOG_EVERY == 0:
            self._log.warning("edge_connection_refused", drone_id=self.drone_id, url=conn.label, reason=reason,
                              count=count, **fields)
        return Decision(CLOSE, reason=reason)

    def _control(
        self,
        key: Hashable,
        conn: _Conn,
        msg: Mapping[str, Any],
        mtype: str,
        active: bool,
        post: list[Callable[[], None]],
    ) -> Decision:
        fence = fp.fence_of(msg)
        if fence is None or fence.leader_id != conn.node_id:
            self._note_refusal(conn, mtype, "unfenced" if fence is None else "leader_id_mismatch")
            return Decision(IGNORE, reason="not_authority")
        if (
            mtype == fp.HEARTBEAT
            and conn.role == "leader"
            and fence.epoch == conn.epoch
            and (fence.epoch > self._epoch_seen or self._authority_key is None)
        ):
            self._try_adopt(key, conn, fence.epoch, fence.leader_id, "heartbeat", active, post)
        if self._current(key, fence):
            return Decision(ACCEPT, feed_watchdog=True)
        reason = "stale_epoch" if fence.epoch < self._epoch_seen else "not_authority"
        self._note_refusal(conn, mtype, reason, epoch=fence.epoch)
        return Decision(IGNORE, reason=reason)

    def _current(self, key: Hashable, fence: fp.Fence | None) -> bool:
        return (
            fence is not None
            and key == self._authority_key
            and fence.epoch == self._epoch_seen
            and fence.leader_id == self._authority_leader
        )

    def _try_adopt(
        self,
        key: Hashable,
        conn: _Conn,
        epoch: int,
        leader_id: str,
        via: str,
        active: bool,
        post: list[Callable[[], None]],
    ) -> bool:
        if epoch < self._epoch_seen:
            return False
        if epoch == self._epoch_seen:
            if leader_id != self._authority_leader:
                self._note_refusal(conn, via, "epoch_tie", epoch=epoch, leader_id=leader_id)
                return False
            # The same leader on a new connection takes over only when its
            # old connection is gone (a reconnect); a second live connection
            # from it (two URLs reaching one ground) never steals the role.
            if self._authority_key is None:
                self._authority_key = key
                self._log.info(
                    "edge_authority_rebound", drone_id=self.drone_id, leader_id=leader_id, epoch=epoch, url=conn.label
                )
            return True
        handover = self._handover
        announced = (
            handover is not None
            and handover.to_node == leader_id
            and handover.epoch_next == epoch
            and handover.from_epoch == self._epoch_seen
        )
        if epoch > _MAX_UNANNOUNCED_EPOCH and not announced:
            self._log.error(
                "fleet_epoch_refused_near_max", drone_id=self.drone_id, epoch=epoch, leader_id=leader_id
            )
            return False
        if (active or self._dispatching) and not (self.inflight_handover and announced):
            self._note_refusal(conn, via, "epoch_conflict_in_flight", epoch=epoch, leader_id=leader_id)
            return False
        previous = (self._epoch_seen, self._authority_leader)
        self._epoch_seen = epoch
        self._authority_leader = leader_id
        self._authority_key = key
        self._handover = None
        self._plan = None
        self._latest.clear()
        self._log.warning("fleet_epoch_adopted", drone_id=self.drone_id, epoch=epoch, leader_id=leader_id,
                          previous_epoch=previous[0], previous_leader=previous[1], via=via, in_flight=active,
                          url=conn.label)
        hooks = self._hooks
        post.append(lambda: self._safe_invalidate(hooks, f"epoch_{epoch}_adopted"))
        return True

    def _safe_invalidate(self, hooks: ExecutorHooks, reason: str) -> None:
        try:
            hooks.invalidate_plan(reason)
        except Exception as exc:  # noqa: BLE001 - logged, adoption stands
            self._log.error("edge_plan_invalidate_error", drone_id=self.drone_id, error=repr(exc))

    def _command(self, key: Hashable, conn: _Conn, msg: Mapping[str, Any], mtype: str, supported: bool) -> Decision:
        cmd_id = fp.cmd_id_of(msg)
        fence = fp.fence_of(msg)
        current = self._current(key, fence) and fence is not None and fence.leader_id == conn.node_id
        if cmd_id is not None:
            entry = self._cache.get(cmd_id)
            if entry is not None:
                if entry.mtype != mtype:
                    nack = self._nack(conn, msg, mtype, cmd_id, "invalid",
                                      detail="cmd_id was used for another command")
                    return Decision(REFUSE, feed_watchdog=current, replies=nack, reason="cmd_id_reused")
                if entry.reply is not None and conn.proto is not None and conn.proto >= 2:
                    return Decision(REFUSE, feed_watchdog=current, replies=(dict(entry.reply),), reason="duplicate")
                return Decision(IGNORE, feed_watchdog=current, reason="duplicate")
        if fence is None or fence.leader_id != conn.node_id:
            return self._refuse(key, conn, msg, mtype, cmd_id, "not_authority", feed=False)
        if fence.epoch < self._epoch_seen:
            return self._refuse(key, conn, msg, mtype, cmd_id, "stale_epoch", feed=False)
        if not current:
            return self._refuse(key, conn, msg, mtype, cmd_id, "not_authority", feed=False)
        if mtype == fp.HANDOVER:
            return self._handover_command(conn, msg, cmd_id, fence)
        if mtype in _PLAN_BOUND_TYPES:
            plan_id = msg.get("plan_id")
            if self._plan is None or self._plan[0] != self._epoch_seen:
                return self._refuse(key, conn, msg, mtype, cmd_id, "no_valid_plan", feed=True)
            if plan_id != self._plan[1]:
                return self._refuse(key, conn, msg, mtype, cmd_id, "plan_mismatch", feed=True)
        if not supported:
            return self._refuse(
                key, conn, msg, mtype, cmd_id, "invalid", feed=True, detail=f"this edge does not implement {mtype}"
            )
        if cmd_id is not None:
            plan_id = msg.get("plan_id") if isinstance(msg.get("plan_id"), str) else None
            self._remember(cmd_id, _CmdEntry(mtype, conn.node_id, fence.epoch, plan_id))
            self._latest[mtype] = cmd_id
        return Decision(ACCEPT, feed_watchdog=True)

    def _supports(self, mtype: str) -> bool:
        try:
            return bool(self._hooks.supports(mtype))
        except Exception:  # noqa: BLE001 - unknown support counts as unsupported
            return False

    def _handover_command(
        self, conn: _Conn, msg: Mapping[str, Any], cmd_id: str | None, fence: fp.Fence
    ) -> Decision:
        to_node, epoch_next = msg.get("to_node"), msg.get("epoch_next")
        self._handover = _Handover(to_node, epoch_next, fence.epoch)
        self._log.warning("edge_handover_announced", drone_id=self.drone_id, from_leader=fence.leader_id,
                          to_node=to_node, epoch_next=epoch_next)
        if cmd_id is None or conn.proto is None or conn.proto < 2:
            return Decision(HANDLED, feed_watchdog=True, reason="handover")
        ack = fp.make_ack(fp.HANDOVER, self.drone_id, cmd_id, epoch_seen=self._epoch_seen)
        self._remember(cmd_id, _CmdEntry(fp.HANDOVER, conn.node_id, fence.epoch, None, reply=ack))
        return Decision(HANDLED, feed_watchdog=True, replies=(ack,), reason="handover")

    def _nack(
        self,
        conn: _Conn,
        msg: Mapping[str, Any],
        mtype: str,
        cmd_id: str | None,
        reason: str,
        detail: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """The nack for a refused command, or nothing when the link cannot carry one."""
        if cmd_id is None or conn.proto is None or conn.proto < 2:
            return ()
        lanes = msg.get("lanes")
        lanes_total = min(len(lanes), fp.MAX_LANES) if isinstance(lanes, list) else 0
        return (fp.make_nack(mtype, self.drone_id, reason, cmd_id, epoch_seen=self._epoch_seen,
                             lanes_total=lanes_total if mtype == fp.ASSIGN_LANES else None, detail=detail),)

    def _refuse(
        self,
        key: Hashable,
        conn: _Conn,
        msg: Mapping[str, Any],
        mtype: str,
        cmd_id: str | None,
        reason: str,
        *,
        feed: bool,
        detail: str | None = None,
    ) -> Decision:
        replies = self._nack(conn, msg, mtype, cmd_id, reason, detail)
        fence = fp.fence_of(msg)
        self._note_refusal(conn, mtype, reason, epoch=fence.epoch if fence else None)
        # Only the authority's refusals are final for their cmd_id. Caching a
        # refusal from any other connection would let a ground that is not
        # obeyed fill the cache and evict the authority's entries, which
        # guard its commands against running twice; such a refusal is simply
        # decided again (with the same answer) if the command is resent.
        if cmd_id is not None and key == self._authority_key:
            self._remember(cmd_id, _CmdEntry(mtype, conn.node_id, fence.epoch if fence else None, None,
                                             reply=replies[0] if replies else None))
        return Decision(REFUSE, feed_watchdog=feed, replies=replies, reason=reason)

    def _remember(self, cmd_id: str, entry: _CmdEntry) -> None:
        self._cache[cmd_id] = entry
        self._cache.move_to_end(cmd_id)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _note_refusal(self, conn: _Conn | None, mtype: str, reason: str, **fields: Any) -> None:
        """Log a refusal: the first of each (type, reason), then every 50th."""
        key = (str(mtype), reason)
        count = self._refusal_counts.get(key, 0) + 1
        self._refusal_counts[key] = count
        if count == 1 or count % 50 == 0:
            event = _REFUSAL_EVENTS.get(reason, "edge_message_refused")
            self._log.warning(event, drone_id=self.drone_id, type=mtype, reason=reason,
                              from_node=conn.node_id if conn else None, url=conn.label if conn else None,
                              epoch_seen=self._epoch_seen, count=count, **fields)

    # -- Dispatch (the executor's consumer thread) -------------------------

    def begin_dispatch(self, msg: Mapping[str, Any] | None, admitted: tuple[int, str | None] | None = None):
        """Called on the consumer thread when the inbox hands out *msg* (``None`` when it hands out nothing).

        Returns the message to execute, or ``None`` to drop it: when a new
        authority was adopted since it was accepted (``stale_epoch``), or
        when it would move the drone (anything but ``hold``) while the
        authority's connection is gone (``not_authority``): a takeoff into a
        dead link is never started. The nack is then available from
        ``stale_reply``. Also records the command as the consumer thread's
        current one, so an executor reply that does not echo its ``cmd_id``
        is stamped with it, and marks a motion command as being dispatched,
        which blocks adopting a new epoch until the next call.
        """
        self._tls.current = None
        with self._lock:
            if self._dispatching:
                self._dispatching = False
                self._dispatch_gen += 1
            self._tls.stale_reply = None
            if msg is None:
                return None
            mtype = msg.get("type")
            if mtype in fp.FAILSAFE_TYPES:
                return msg
            reason = None
            if admitted is not None and admitted != (self._epoch_seen, self._authority_leader):
                reason = "stale_epoch"
            elif mtype in _MOTION_TYPES and mtype != fp.HOLD and self._authority_key is None:
                reason = "not_authority"
            if reason is not None:
                self._drop_at_dispatch(msg, mtype, reason, admitted)
                return None
            if mtype in _MOTION_TYPES:
                self._dispatching = True
                self._tls.current = (fp.cmd_id_of(msg), mtype)
            return msg

    def _drop_at_dispatch(self, msg: Mapping[str, Any], mtype: Any, reason: str, admitted: Any) -> None:
        self._log.warning(
            "fleet_stale_command_dropped",
            drone_id=self.drone_id,
            type=mtype,
            reason=reason,
            admitted_epoch=admitted[0] if admitted else None,
            epoch_seen=self._epoch_seen,
        )
        cmd_id = fp.cmd_id_of(msg)
        if mtype not in fp.FENCED_COMMAND_TYPES or cmd_id is None:
            return
        lanes = msg.get("lanes")
        nack = fp.make_nack(
            mtype, self.drone_id, reason, cmd_id, epoch_seen=self._epoch_seen,
            lanes_total=(min(len(lanes), fp.MAX_LANES) if isinstance(lanes, list) else 0)
            if mtype == fp.ASSIGN_LANES else None,
        )
        entry = self._cache.get(cmd_id)
        if entry is not None:
            entry.reply = nack
        self._tls.stale_reply = nack

    def stale_reply(self) -> dict[str, Any] | None:
        """The nack produced by the last ``begin_dispatch`` on this thread, if it dropped a command."""
        return getattr(self._tls, "stale_reply", None)

    def check(self, msg: Mapping[str, Any]) -> str | None:
        """PR-5's ``CommandAuthority.check``: None to execute *msg*, else ``stale_epoch`` or ``not_authority``.

        The uplink already fenced every command before the executor sees it
        (``on_message``) and re-checked it when it was handed out
        (``begin_dispatch``), so this returns None for those messages. It
        repeats the same test without side effects: a landing is always
        accepted; any other message must carry the fence of the current
        authority at ``epoch_seen``, and a motion command other than
        ``hold`` also needs the authority's connection to be up.
        """
        mtype = msg.get("type")
        if mtype in fp.FAILSAFE_TYPES:
            return None
        fence = fp.fence_of(msg)
        with self._lock:
            if fence is not None and fence.epoch < self._epoch_seen:
                return "stale_epoch"
            if fence is None or fence.epoch != self._epoch_seen or fence.leader_id != self._authority_leader:
                return "not_authority"
            if mtype in _MOTION_TYPES and mtype != fp.HOLD and self._authority_key is None:
                return "not_authority"
            return None

    # -- Outbound (executor -> grounds) ------------------------------------

    def outbound(self, msg: Mapping[str, Any]) -> dict[str, Any]:
        """Complete an executor message for protocol v2 and record command outcomes.

        Telemetry gains ``epoch`` (``epoch_seen``), ``plan_id``, ``state`` and
        ``plan_valid`` when the executor did not set them; acks and nacks
        gain the ``cmd_id`` of the command they answer (when the executor
        did not echo it), ``epoch_seen`` and, for an ack, ``result``. The
        reply is cached for its ``cmd_id`` so a duplicate gets it again, and
        an ack for ``assign_lanes`` makes that plan the one ``start_at`` and
        ``resume`` must name.
        """
        out = dict(msg)
        mtype = out.get("type")
        status = None
        if mtype == fp.TELEMETRY:
            try:
                status = self._hooks.fleet_status()
            except Exception:  # noqa: BLE001 - telemetry still goes out
                status = None
        with self._lock:
            if mtype == fp.TELEMETRY:
                self._complete_telemetry(out, status or {})
            elif mtype in (fp.ACK, fp.NACK):
                self._complete_reply(out)
        return out

    def _complete_telemetry(self, out: dict[str, Any], status: Mapping[str, Any]) -> None:
        out.setdefault("epoch", self._epoch_seen)
        if "plan_id" not in out:
            plan = status.get("plan_id")
            if plan is None and self._plan is not None and self._plan[0] == self._epoch_seen:
                plan = self._plan[1]
            out["plan_id"] = plan
        if "state" not in out:
            state = status.get("state")
            if state not in fp.EDGE_STATES:
                state = "hovering" if out.get("is_flying") is True else "idle"
            out["state"] = state
        if "plan_valid" not in out:
            # Without the executor's own answer (PR-5) the safe claim is "no".
            out["plan_valid"] = status.get("plan_valid") is True

    def _complete_reply(self, out: dict[str, Any]) -> None:
        target = out.get("for")
        out.setdefault("drone_id", self.drone_id)
        if "cmd_id" not in out and target in fp.FENCED_COMMAND_TYPES:
            current = getattr(self._tls, "current", None)
            if current is not None and current[1] == target and current[0] is not None:
                out["cmd_id"] = current[0]
            elif target in self._latest:
                out["cmd_id"] = self._latest[target]
        out.setdefault("epoch_seen", self._epoch_seen)
        if out.get("type") == fp.ACK:
            out.setdefault("result", "ok")
        cmd_id = out.get("cmd_id")
        entry = self._cache.get(cmd_id) if isinstance(cmd_id, str) else None
        if entry is None or entry.mtype != target:
            return
        if entry.reply is None:
            entry.reply = dict(out)
        if (
            out.get("type") == fp.ACK
            and target == fp.ASSIGN_LANES
            and entry.epoch == self._epoch_seen
            and entry.plan_id is not None
        ):
            self._plan = (entry.epoch, entry.plan_id)

    def shape_for(self, key: Hashable, msg: Mapping[str, Any]) -> tuple[dict[str, Any], int] | None:
        """*msg* shaped for connection *key* and the version to validate it with, or ``None``."""
        with self._lock:
            conn = self._conns.get(key)
            if conn is None:
                return None
            proto = conn.proto if conn.proto is not None else 1
        try:
            return fp.shape_for_peer(msg, proto), proto
        except fp.ProtocolError:
            return None
