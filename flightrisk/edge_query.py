"""The edge's answer to the read-only ``describe_unit`` query (PROTO-4).

A ground asks an edge to describe its unit; the edge answers with an ``ack``
built by ``fleet_protocol.make_describe_unit_ack`` from the report it
resolved at start (``EdgeResolution.report``, whose ``wire()`` gives
``unit``, ``gates``, ``values`` and ``unit_rev``). Nothing here reads runtime state, so the
answer is the same before takeoff, in flight, while landing and while
holding.

Rules:

* **Never a sign of a live authority.** The caller routes the query here
  before it feeds the link-loss watchdog, and never feeds it for a query.
* **Never fenced and never a command.** Any ground may ask, so no fence,
  epoch or authority check applies, and handling a query never lands the
  drone, even when building the answer fails.
* **Per-connection limits.** Each connection keeps its own replies to its
  last ``MAX_CACHED`` query ids, so a repeated ``cmd_id`` gets the same
  reply again; this cache is apart from any authority cache. A connection
  gets at most one new answer per ``MIN_INTERVAL_S``; a faster new query
  gets a ``nack`` with reason ``busy``, which is not cached, so a retry of
  that ``cmd_id`` after the interval is answered.
* **Protocol v2 only.** On a v1 link the query is refused with no reply,
  because v1 defines no reply to it (S20).
* **Off the control thread.** ``LinkQueries`` decides the reply under a lock
  (a few dictionary operations; a new ack is built at most once per
  interval per connection) and hands the send to its own thread, so a slow
  or blocked socket never delays the commands handled after the query.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from flightrisk import fleet_protocol as fp

#: Replies a connection keeps, by ``cmd_id``.
MAX_CACHED = 8
#: Seconds between two new answers on one connection.
MIN_INTERVAL_S = 2.0
#: Replies waiting for the sender thread; more are dropped with a warning.
OUTBOX_SIZE = 8


class ConnectionQueries:
    """One connection's query state: its cached replies and when it last got a new answer."""

    def __init__(self, max_cached: int = MAX_CACHED) -> None:
        self.max_cached = max_cached
        self.replies: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.answered_at: float | None = None

    def cached(self, cmd_id: str) -> dict[str, Any] | None:
        reply = self.replies.get(cmd_id)
        if reply is not None:
            self.replies.move_to_end(cmd_id)
        return reply

    def remember(self, cmd_id: str, reply: dict[str, Any]) -> None:
        self.replies[cmd_id] = reply
        self.replies.move_to_end(cmd_id)
        while len(self.replies) > self.max_cached:
            self.replies.popitem(last=False)


def _report_of(resolution: Any) -> Any:
    report = getattr(resolution, "report", None)
    if report is None:
        raise ValueError("the edge resolution has no report to describe")
    return report


class EdgeQuery:
    """Builds the reply to one query from the edge's start-time resolution."""

    def __init__(
        self,
        resolution: Any,
        drone_id: str,
        *,
        log: Any = None,
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        self._report = _report_of(resolution)
        self.drone_id = drone_id
        self._log = log
        self.min_interval_s = min_interval_s

    @property
    def unit_rev(self) -> str | None:
        """The ``unit_rev`` of the start-time report, which the edge ``hello`` carries (PROTO-2)."""
        value = getattr(self._report, "unit_rev", None)
        return value if fp.is_unit_rev(value) else None

    def answer(self, msg: Mapping[str, Any], state: ConnectionQueries, *, proto: Any, now: float) -> dict[str, Any] | None:
        """The reply to *msg* on the connection whose state is *state*, or None when it gets none."""
        try:
            fp.validate_describe_unit(msg, proto=proto)
        except fp.ProtocolError as exc:
            return self._refuse(msg, proto, str(exc))
        cmd_id = msg["cmd_id"]
        if msg["drone_id"] != self.drone_id:
            return self._refuse(msg, proto, "the query names another drone")
        reply = state.cached(cmd_id)
        if reply is not None:
            return dict(reply)
        if state.answered_at is not None and now - state.answered_at < self.min_interval_s:
            self._note("edge_query_busy", cmd_id=cmd_id)
            return fp.make_describe_unit_nack(msg["drone_id"], cmd_id, "busy")
        state.answered_at = now
        reply = self._build(msg["drone_id"], cmd_id)
        state.remember(cmd_id, reply)
        return dict(reply)

    def _build(self, drone_id: str, cmd_id: str) -> dict[str, Any]:
        try:
            # ``UnitReport.wire`` gives plain copies of unit, gates, values and unit_rev.
            return fp.make_describe_unit_ack(drone_id, cmd_id, **self._report.wire())
        except (fp.ProtocolError, TypeError, ValueError) as exc:
            if self._log is not None:
                self._log.error("edge_query_build_failed", cmd_id=cmd_id, error=str(exc)[:200])
            return fp.make_describe_unit_nack(drone_id, cmd_id, "invalid", detail="the unit description is unavailable")

    def _refuse(self, msg: Mapping[str, Any], proto: Any, why: str) -> dict[str, Any] | None:
        """A ``nack invalid`` on a v2 link when the ids allow one; nothing on a v1 link."""
        self._note("edge_query_refused", proto=proto, cmd_id=msg.get("cmd_id"), why=why[:200])
        if not (isinstance(proto, int) and not isinstance(proto, bool) and proto >= 2):
            return None
        try:
            return fp.make_describe_unit_nack(msg.get("drone_id"), msg.get("cmd_id"), "invalid", detail=why[:200])
        except (fp.ProtocolError, TypeError, ValueError):
            return None

    def _note(self, event: str, **fields: Any) -> None:
        if self._log is not None:
            self._log.info(event, drone_id=self.drone_id, **fields)


def link_of(transport: Any) -> tuple[Any, int]:
    """The transport's current connection and its protocol version (1 when it has none).

    ``EdgeTransportSync`` keeps both on the ``EdgeTransport`` it wraps.
    """
    for candidate in (transport, getattr(transport, "_transport", None)):
        proto = getattr(candidate, "proto", None) if candidate is not None else None
        if isinstance(proto, int) and not isinstance(proto, bool):
            return getattr(candidate, "websocket", None), proto
    return None, 1


class LinkQueries:
    """Answers the queries that arrive on one transport, one state per connection, and sends off-thread."""

    def __init__(
        self,
        query: EdgeQuery,
        transport: Any,
        send: Callable[[dict[str, Any]], Any],
        *,
        log: Any = None,
        clock: Callable[[], float] = time.monotonic,
        max_cached: int = MAX_CACHED,
    ) -> None:
        self.query = query
        self._transport = transport
        self._send = send
        self._log = log
        self._clock = clock
        self._max_cached = max_cached
        self._lock = threading.Lock()
        self._connection: Any = None
        self.state = ConnectionQueries(max_cached)
        self._outbox: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=OUTBOX_SIZE)
        self._sender: threading.Thread | None = None

    def handle(self, msg: Mapping[str, Any]) -> dict[str, Any] | None:
        """Decide the reply to *msg*, queue it for sending and return it. Never raises."""
        try:
            connection, proto = link_of(self._transport)
            with self._lock:
                if connection is not self._connection:
                    # A new connection starts with an empty cache and no limit.
                    self._connection, self.state = connection, ConnectionQueries(self._max_cached)
                reply = self.query.answer(msg, self.state, proto=proto, now=self._clock())
            if reply is not None:
                self._post(reply)
            return reply
        except Exception as exc:  # noqa: BLE001 - a query must never take the control thread down
            if self._log is not None:
                self._log.warning("edge_query_error", error=repr(exc)[:200])
            return None

    def _post(self, reply: dict[str, Any]) -> None:
        if self._sender is None:
            self._sender = threading.Thread(target=self._drain, daemon=True, name="edge-query-replies")
            self._sender.start()
        try:
            self._outbox.put_nowait(reply)
        except queue.Full:
            if self._log is not None:
                self._log.warning("edge_query_reply_dropped", cmd_id=reply.get("cmd_id"))

    def _drain(self) -> None:
        while True:
            reply = self._outbox.get()
            try:
                self._send(reply)
            except Exception as exc:  # noqa: BLE001 - one failed send must not stop the next
                if self._log is not None:
                    self._log.warning("edge_query_send_error", error=repr(exc)[:200])
            finally:
                self._outbox.task_done()


def for_link(resolution: Any, drone_id: str, transport: Any, send: Callable[[dict[str, Any]], Any], *,
             log: Any = None) -> LinkQueries | None:
    """The query handler of an edge that resolved its unit, or None for one that did not."""
    if resolution is None or getattr(resolution, "report", None) is None:
        return None
    return LinkQueries(EdgeQuery(resolution, drone_id, log=log), transport, send, log=log)


def route(runner: Any, msg: Mapping[str, Any], log: Any) -> None:
    """Hand a query to the runner's handler (``runner.edge_query``). Never raises."""
    queries = getattr(runner, "edge_query", None)
    if not isinstance(queries, LinkQueries):
        log.debug("edge_query_unsupported", type=msg.get("type"))
        return
    queries.handle(msg)
