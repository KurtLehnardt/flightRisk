"""Single-alert latch for operator match confirmation.

Before this latch existed, every alert path (the local frame loop, the Gemma
description worker and the edge ground-station consumer) throttled alerts only
per *spatial track key*: the bbox centre rounded to a 50 px grid cell, with a
10 s cooldown. A person walking, or a drone drifting or flying its pattern,
moves the bbox centre across grid cells within a second or two, so every new
cell looked like a brand-new track and fired a fresh ``match_alert`` (plus a DB
row, a snapshot and a full-screen pop-up). Nothing paused matching while the
operator was deciding.

The latch turns that into a small state machine shared by every alert path::

    SEARCHING --(first match)--> AWAITING_RESPONSE --(dismiss)--> SEARCHING
                                        |
                                        +--(confirm)--> RESOLVED

* ``SEARCHING``: matching runs normally; the first match that is not
  suppressed opens exactly one alert and moves to ``AWAITING_RESPONSE``.
* ``AWAITING_RESPONSE``: matching, scoring, LLM calls and new alerts are
  paused. Video keeps streaming. If nobody answers, the *same* alert is
  re-announced every ``renotify_interval`` seconds (never a new alert).
* ``Dismiss`` returns to ``SEARCHING`` and suppresses the dismissed candidate
  for ``dismiss_suppress_seconds`` so the same person does not re-alert at once.
* ``Confirm`` moves to ``RESOLVED``: matching stays off for the rest of the
  session (until the pipeline is re-initialised, which calls ``reset``).

Every public method is safe to call from any thread (frame loop, Gemma worker,
edge transport executor, Flask/SocketIO request threads).
"""

from __future__ import annotations

import copy
import enum
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any

#: Seconds a dismissed candidate (tracker id or spatial grid cell) is ignored
#: before it may raise a new alert. Long enough that an operator who has just
#: said "not my child" is not immediately asked about the same person again,
#: short enough that a genuinely mistaken dismissal is retried within a minute.
DISMISS_SUPPRESS_SECONDS = 60.0

#: Seconds an unanswered alert waits before it is re-announced (same alert id,
#: ``renotify`` flag set). The search stays paused in the meantime rather than
#: silently resuming, so a missed pop-up cannot be lost.
RENOTIFY_INTERVAL_SECONDS = 30.0


class AlertPhase(str, enum.Enum):
    """Lifecycle phase of the match-alert latch."""

    SEARCHING = "searching"
    AWAITING_RESPONSE = "awaiting_response"
    RESOLVED = "resolved"


class MatchAlertLatch:
    """Thread-safe latch that allows one outstanding match alert at a time.

    Args:
        dismiss_suppress_seconds: How long a dismissed candidate is ignored.
        renotify_interval: Seconds between re-announcements of an unanswered
            alert. ``0`` or a negative value disables re-announcement.
        clock: Monotonic time source, injectable for tests.
    """

    def __init__(
        self,
        dismiss_suppress_seconds: float = DISMISS_SUPPRESS_SECONDS,
        renotify_interval: float = RENOTIFY_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.dismiss_suppress_seconds = dismiss_suppress_seconds
        self.renotify_interval = renotify_interval
        self._clock = clock
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._reset_locked()

    # ------------------------------------------------------------------
    # Transition listeners (telemetry)
    # ------------------------------------------------------------------

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """Call ``listener(event)`` after every transition.

        ``event`` has ``transition`` (opened, renotified, confirmed,
        dismissed, aborted, reset), ``alert_id``, ``phase`` (after the
        transition), ``awaiting_s`` (seconds since the alert opened, for
        confirm/dismiss/renotify) and ``renotify_count``. Listeners run
        outside the latch lock and their errors are ignored, so they can
        never change the latch's behaviour.
        """
        self._listeners.append(listener)

    def _notify(self, event: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 -- telemetry must never affect the latch
                pass

    # ------------------------------------------------------------------
    # Internal helpers (caller holds the lock)
    # ------------------------------------------------------------------

    def _reset_locked(self) -> None:
        self._phase = AlertPhase.SEARCHING
        self._alert_id: str | None = None
        self._candidate_keys: tuple[str, ...] = ()
        self._payload: dict | None = None
        self._opened_at = 0.0
        self._last_notified = 0.0
        self._renotify_count = 0
        self._resolution: str | None = None
        self._suppressed: dict[str, float] = {}

    def _prune_suppressed_locked(self, now: float) -> None:
        expired = [k for k, until in self._suppressed.items() if until <= now]
        for k in expired:
            del self._suppressed[k]

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def phase(self) -> AlertPhase:
        with self._lock:
            return self._phase

    @property
    def alert_id(self) -> str | None:
        with self._lock:
            return self._alert_id

    def matching_allowed(self) -> bool:
        """True only while searching; matching/scoring/LLM calls must pause otherwise."""
        with self._lock:
            return self._phase is AlertPhase.SEARCHING

    def is_suppressed(self, keys: Iterable[str]) -> bool:
        """True if any of ``keys`` was dismissed within the suppression window."""
        with self._lock:
            now = self._clock()
            self._prune_suppressed_locked(now)
            return any(k in self._suppressed for k in keys)

    def owns_track(self, track_key: str) -> bool:
        """True if the outstanding alert was raised for ``track_key``.

        Lets the Gemma worker finish the single reasoning call queued for the
        open alert while dropping every other queued LLM call.
        """
        with self._lock:
            if self._phase is not AlertPhase.AWAITING_RESPONSE or self._payload is None:
                return False
            return self._payload.get("track_id") == track_key

    def pending_alert(self) -> dict | None:
        """Copy of the outstanding alert payload, or None if nothing is pending."""
        with self._lock:
            if self._phase is not AlertPhase.AWAITING_RESPONSE or self._payload is None:
                return None
            pending = copy.copy(self._payload)
            pending["renotify"] = True
            pending["renotify_count"] = self._renotify_count
            return pending

    def snapshot(self) -> dict[str, Any]:
        """Plain-dict view of the latch for status endpoints and the UI."""
        with self._lock:
            return {
                "phase": self._phase.value,
                "alert_id": self._alert_id,
                "renotify_count": self._renotify_count,
                "resolution": self._resolution,
            }

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def try_open(self, candidate_keys: Iterable[str]) -> str | None:
        """Atomically claim the single alert slot for a candidate.

        Returns a new alert id when the latch was ``SEARCHING`` and none of
        ``candidate_keys`` is suppressed; the latch is then
        ``AWAITING_RESPONSE``. Returns ``None`` otherwise, in which case the
        caller must not emit an alert, write a DB row or call the LLM.
        """
        keys = tuple(k for k in candidate_keys if k)
        with self._lock:
            if self._phase is not AlertPhase.SEARCHING:
                return None
            now = self._clock()
            self._prune_suppressed_locked(now)
            if any(k in self._suppressed for k in keys):
                return None
            self._phase = AlertPhase.AWAITING_RESPONSE
            self._alert_id = uuid.uuid4().hex
            self._candidate_keys = keys
            self._payload = None
            self._opened_at = now
            self._last_notified = now
            self._renotify_count = 0
            self._resolution = None
            alert_id = self._alert_id
        self._notify({"transition": "opened", "alert_id": alert_id, "phase": AlertPhase.AWAITING_RESPONSE.value})
        return alert_id

    def attach(self, alert_id: str, payload: dict) -> None:
        """Store the emitted alert payload so it can be re-announced verbatim."""
        with self._lock:
            if alert_id == self._alert_id and self._phase is AlertPhase.AWAITING_RESPONSE:
                self._payload = dict(payload)

    def due_renotify(self) -> dict | None:
        """Return the outstanding alert payload if a reminder is due, else None.

        The returned dict is a copy of the original alert with
        ``renotify=True`` and an incrementing ``renotify_count``; the alert id
        is unchanged so the UI treats it as the same logical alert.
        """
        with self._lock:
            if (
                self._phase is not AlertPhase.AWAITING_RESPONSE
                or self._payload is None
                or self.renotify_interval <= 0
            ):
                return None
            now = self._clock()
            if now - self._last_notified < self.renotify_interval:
                return None
            self._last_notified = now
            self._renotify_count += 1
            reminder = copy.copy(self._payload)
            reminder["renotify"] = True
            reminder["renotify_count"] = self._renotify_count
            reminder["awaiting_seconds"] = round(now - self._opened_at, 1)
            event = {"transition": "renotified", "alert_id": self._alert_id, "phase": self._phase.value,
                     "awaiting_s": now - self._opened_at, "renotify_count": self._renotify_count}
        self._notify(event)
        return reminder

    def abort(self, alert_id: str) -> None:
        """Release a claimed slot whose alert could not be delivered.

        Returns to ``SEARCHING`` without suppressing the candidate, so the next
        frame can retry. No-op for any id other than the outstanding one.
        """
        with self._lock:
            if not (self._phase is AlertPhase.AWAITING_RESPONSE and alert_id == self._alert_id):
                return
            self._phase = AlertPhase.SEARCHING
            self._alert_id = None
            self._candidate_keys = ()
            self._payload = None
            self._renotify_count = 0
        self._notify({"transition": "aborted", "alert_id": alert_id, "phase": AlertPhase.SEARCHING.value})

    def dismiss(self, alert_id: str) -> dict | None:
        """Operator said "not a match": resume searching, suppress the candidate.

        Returns the dismissed alert's payload (may be ``{}`` if it was never
        attached) on the first call for the outstanding alert, or ``None`` for
        a stale/duplicate/unknown id so callers can treat repeats as no-ops.
        """
        with self._lock:
            if self._phase is not AlertPhase.AWAITING_RESPONSE or alert_id != self._alert_id:
                return None
            now = self._clock()
            for k in self._candidate_keys:
                self._suppressed[k] = now + self.dismiss_suppress_seconds
            payload = dict(self._payload or {})
            renotified = self._renotify_count
            self._phase = AlertPhase.SEARCHING
            self._alert_id = None
            self._candidate_keys = ()
            self._payload = None
            self._renotify_count = 0
            self._resolution = "dismissed"
            event = {"transition": "dismissed", "alert_id": alert_id, "phase": AlertPhase.SEARCHING.value,
                     "awaiting_s": now - self._opened_at, "renotify_count": renotified,
                     "alert_level": payload.get("alert_level")}
        self._notify(event)
        return payload

    def confirm(self, alert_id: str) -> dict | None:
        """Operator said "yes, that's my child": stop matching for good.

        Returns the confirmed alert's payload on the first call for the
        outstanding alert, or ``None`` for a stale/duplicate/unknown id.
        """
        with self._lock:
            if self._phase is not AlertPhase.AWAITING_RESPONSE or alert_id != self._alert_id:
                return None
            payload = dict(self._payload or {})
            self._phase = AlertPhase.RESOLVED
            self._resolution = "confirmed"
            event = {"transition": "confirmed", "alert_id": alert_id, "phase": AlertPhase.RESOLVED.value,
                     "awaiting_s": self._clock() - self._opened_at, "renotify_count": self._renotify_count,
                     "alert_level": payload.get("alert_level")}
        self._notify(event)
        return payload

    def reset(self) -> None:
        """Return to a fresh ``SEARCHING`` state (new session / pipeline restart)."""
        with self._lock:
            self._reset_locked()
        self._notify({"transition": "reset", "alert_id": None, "phase": AlertPhase.SEARCHING.value})


#: Process-wide latch shared by every dashboard alert path.
alert_latch = MatchAlertLatch()


def _export_transition(event: dict[str, Any]) -> None:
    from flightrisk import obs_events

    obs_events.alert(
        event["transition"],
        alert_id=event.get("alert_id"),
        phase=event["phase"],
        awaiting_s=event.get("awaiting_s"),
        renotify_count=event.get("renotify_count"),
        alert_level=event.get("alert_level"),
    )


alert_latch.add_listener(_export_transition)
