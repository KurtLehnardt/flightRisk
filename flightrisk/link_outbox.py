"""Per-connection outbound queues, so one stalled link never delays another.

A WebSocket ``send`` can wait a long time: on a connection that is closing
(for example after its keepalive failed because a cable was pulled) it waits
up to the connection's ``close_timeout``, and on a link whose peer stopped
reading it waits for the send buffer to drain. Code that sends the same
message to several peers and waits for all of them therefore stalls every
peer behind the slowest one.

``LinkOutbox`` gives each connection its own queue and writer task. Callers
only ever append to a queue (they never wait for the network); the writer
sends one message at a time with a timeout and skips nothing but what the
queue limits drop. Three queues are served in priority order:

* ``URGENT``: replies, acks, nacks, hellos and pongs. Small and never
  dropped while the connection is open, except past ``urgent_max`` (a
  connection that far behind is dead).
* ``TELEMETRY``: bounded; the oldest is dropped when full, because only
  the latest telemetry matters.
* ``BULK``: detections (crops, embeddings, thumbnails); bounded, oldest
  dropped.

``is_open`` is also used by the ground station's broadcasts to skip a
connection that is closing instead of waiting for it.

Privacy: log events carry the connection label (a URL without credentials),
queue names and counts only, never message contents.
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from flightrisk import obs_comm
from flightrisk.observability import StructuredLogger

__all__ = ["BULK", "TELEMETRY", "URGENT", "LinkOutbox", "is_open"]

URGENT = "urgent"
TELEMETRY = "telemetry"
BULK = "bulk"
_ORDER = (URGENT, TELEMETRY, BULK)

# A payload is the text to send, or a callable that returns it (or None to
# send nothing) right before the write, for a pong whose t3 must be stamped
# as late as possible.
Payload = str | Callable[[], str | None]


def is_open(ws: Any) -> bool:
    """True unless *ws* reports a state other than OPEN (a test double without ``state`` counts as open)."""
    state = getattr(ws, "state", None)
    return state is None or state is State.OPEN


class LinkOutbox:
    """The outbound queues and writer task of one connection; see the module docstring.

    Every method must be called on the event loop that owns *ws*.
    """

    def __init__(
        self,
        ws: Any,
        *,
        label: str,
        send_timeout_s: float,
        logger: StructuredLogger,
        urgent_max: int = 256,
        telemetry_max: int = 4,
        bulk_max: int = 2,
    ):
        self.ws = ws
        self.label = label
        self._send_timeout_s = float(send_timeout_s)
        self._log = logger
        self._queues: dict[str, deque[Payload]] = {kind: deque() for kind in _ORDER}
        self._limits = {URGENT: int(urgent_max), TELEMETRY: int(telemetry_max), BULK: int(bulk_max)}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.closed = False
        self.dropped: dict[str, int] = dict.fromkeys(_ORDER, 0)
        self.timeouts = 0
        self.sent = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    def put(self, kind: str, payload: Payload) -> bool:
        """Queue *payload* for this connection; False when the connection is closed or closing."""
        if self.closed or not is_open(self.ws):
            return False
        queue = self._queues[kind]
        if len(queue) >= self._limits[kind]:
            queue.popleft()
            self._note_drop(kind)
        queue.append(payload)
        self._wake.set()
        return True

    def pending(self) -> int:
        return sum(len(queue) for queue in self._queues.values())

    def close(self) -> None:
        """Drop everything queued and stop the writer; nothing more is sent on this connection."""
        self.closed = True
        for queue in self._queues.values():
            queue.clear()
        task = self._task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._wake.set()

    def _next(self) -> Payload | None:
        for kind in _ORDER:
            queue = self._queues[kind]
            if queue:
                return queue.popleft()
        return None

    async def _run(self) -> None:
        try:
            while not self.closed:
                payload = self._next()
                if payload is None:
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                if not is_open(self.ws):
                    break
                if callable(payload):
                    try:
                        payload = payload()
                    except Exception as exc:  # noqa: BLE001 - one bad reply must not stop the writer
                        self._log.error("link_outbox_payload_error", url=self.label, error=repr(exc))
                        continue
                    if payload is None:
                        continue
                try:
                    await asyncio.wait_for(self.ws.send(payload), timeout=self._send_timeout_s)
                except ConnectionClosed:
                    break
                except (asyncio.TimeoutError, TimeoutError):
                    self._note_timeout()
                    continue
                self.sent += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - logged; the reader notices the dead connection
            self._log.error("link_outbox_writer_error", url=self.label, error=repr(exc))
        finally:
            self.closed = True
            for queue in self._queues.values():
                queue.clear()

    def _note_drop(self, kind: str) -> None:
        self.dropped[kind] += 1
        obs_comm.edge_outbox_dropped(kind)
        count = self.dropped[kind]
        if count == 1 or count % 50 == 0:
            self._log.warning("link_outbox_dropped", url=self.label, queue=kind, dropped=count)

    def _note_timeout(self) -> None:
        self.timeouts += 1
        if self.timeouts == 1 or self.timeouts % 20 == 0:
            self._log.warning(
                "link_outbox_send_timeout", url=self.label, timeout_s=self._send_timeout_s, count=self.timeouts
            )
