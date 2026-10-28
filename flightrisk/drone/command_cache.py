"""At-most-once bookkeeping of fleet command ids (``cmd_id``) on the edge.

Protocol v2 (``docs/fleet-protocol-v2.md``, "Command ids and idempotency")
asks the edge to remember the outcome of at least the last
``CMD_ID_CACHE_SIZE`` (256) command ids, each with the reply it sent, and
never to execute a command whose id it has already seen:

* A command id is recorded as *pending* when the command arrives, before it
  runs, so a retry that arrives while the first copy is still running (a
  scheduled ``start_at``, a ``nudge`` in flight) is recognised and not
  executed again.
* The first outcome is final, refusals included. A duplicate of a finished
  command gets the cached ``ack`` or ``nack`` resent unchanged; a duplicate
  of a pending one gets nothing now, because the original's reply follows.
* A cached id arriving with a different message type is a sender bug and is
  refused without executing anything.
* Landing never goes through this cache: ``land`` and
  ``emergency_land_all`` are executed every time they arrive.

The cache is least recently used: a hit refreshes an entry, and when it is
full the oldest finished entry is evicted first. Only a few commands can be
pending at once (one flight sequence runs at a time), so a pending entry is
evicted only if the cache holds nothing else.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from flightrisk.fleet_protocol import CMD_ID_CACHE_SIZE

__all__ = ["DONE", "MISMATCH", "NEW", "PENDING", "CommandCache"]

# Results of ``CommandCache.claim``.
NEW = "new"
PENDING = "pending"
DONE = "done"
MISMATCH = "mismatch"


@dataclass
class _Entry:
    mtype: str
    done: bool = False
    reply: dict[str, Any] | None = None


class CommandCache:
    """LRU of command ids with their first outcome. Thread-safe."""

    def __init__(self, size: int = CMD_ID_CACHE_SIZE):
        if size < 1:
            raise ValueError(f"cache size must be at least 1, got {size!r}")
        self._size = int(size)
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, cmd_id: object) -> bool:
        with self._lock:
            return cmd_id in self._entries

    def claim(self, cmd_id: str, mtype: str) -> tuple[str, dict[str, Any] | None]:
        """Record *cmd_id* as pending if it is new.

        Returns ``(NEW, None)`` for a new id (the caller executes the
        command), ``(PENDING, None)`` for a duplicate of a command still
        running, ``(DONE, reply)`` for a duplicate of a finished one (the
        caller resends *reply*, which may be None if none was sent), or
        ``(MISMATCH, None)`` when the id is cached for another type.
        """
        with self._lock:
            entry = self._entries.get(cmd_id)
            if entry is None:
                self._entries[cmd_id] = _Entry(mtype)
                self._evict()
                return NEW, None
            if entry.mtype != mtype:
                return MISMATCH, None
            self._entries.move_to_end(cmd_id)
            if not entry.done:
                return PENDING, None
            return DONE, None if entry.reply is None else dict(entry.reply)

    def finish(self, cmd_id: str, mtype: str, reply: dict[str, Any] | None) -> bool:
        """Record the outcome of *cmd_id*. Returns False if it already had one.

        An id that was never claimed (a handler called directly rather than
        through the executor's dispatch) is recorded as finished, so its
        outcome is final too. The caller sends *reply* only when this
        returns True.
        """
        with self._lock:
            entry = self._entries.get(cmd_id)
            if entry is None:
                entry = _Entry(mtype)
                self._entries[cmd_id] = entry
                self._evict()
            elif entry.done:
                return False
            entry.done = True
            entry.reply = None if reply is None else dict(reply)
            return True

    def is_pending(self, cmd_id: str) -> bool:
        with self._lock:
            entry = self._entries.get(cmd_id)
            return entry is not None and not entry.done

    def _evict(self) -> None:
        """Drop entries beyond the size, oldest finished first. Caller holds ``_lock``."""
        while len(self._entries) > self._size:
            victim = next((key for key, entry in self._entries.items() if entry.done), None)
            if victim is None:
                victim = next(iter(self._entries))
            del self._entries[victim]
