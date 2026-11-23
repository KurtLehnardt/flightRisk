"""Ground half of manual takeover: one controlling drone at a time.

The dashboard's video tiles select a drone; ``ManualControl`` turns those
clicks into the manual commands of ``flightrisk/fleet_protocol/manual.py``
and tracks their answers. The operator's view is ``docs/manual-takeover.md``.

Rules:

* **One controlled drone.** ``take_control(X)`` sends ``manual_hold`` to X
  (X pauses its search and hovers). If another drone Y was controlled, Y is
  sent ``resume_search`` first, so it goes back to its search. Taking
  control of the drone already controlled does nothing.
* **Release.** ``resume_search(X)`` releases X only, and is the only way to
  release a drone besides taking control of another one. It is also accepted
  for a drone that its edge reports paused while no one controls it (for
  example after the controlling browser went away). It is refused (reason
  ``lost``) for a drone the fleet coordinator has declared lost: that
  drone's lanes were handed on, or wait for the operator's Re-task, so it
  must not fly them too. Switching control away from a lost drone leaves it
  held instead of resuming it.
* **Controller gone.** When the browser that took control disconnects
  (closed, reloaded or crashed), the drone stays paused: it keeps hovering
  where it is, and its edge reports it ``held``. No motion is started
  without an operator, so no autonomous return begins while no one may be
  watching. An orphan-landing countdown starts for that drone
  (``FLIGHTRISK_MANUAL_ORPHAN_LAND_S``, 120 s by default, at least 30 s):
  when it runs out with no controller, the ground sends the drone the
  ordinary ``land`` (``reason: controller_timeout``), which is never fenced
  or refused. The countdown is cancelled when ``resume_search`` for that
  drone succeeds, when someone takes control of it again (and starts again
  if that controller disconnects too), when the drone lands, is declared
  lost, reports autonomous control again, or when LAND ALL is pressed.
  Reconnecting alone does not cancel it. Each drone has its own countdown,
  and ``drone_status`` reports the seconds left (``orphan_land_in_s``) so a
  reloaded dashboard shows it. The countdown lives here, on the ground,
  because the ground owns the browser sessions: the edge cannot tell an
  operator who walked away from one who is only idle. If the ground process
  itself dies, the edge's ground-link watchdog lands the drone within
  ``FLIGHTRISK_LINK_LOSS_LAND_S`` (3 s), sooner than this countdown.
  The battery landing still applies throughout. A drone that lands while
  its countdown runs, by the countdown's own ``land`` or by itself (its
  battery landing, for example), is remembered as orphan-landed
  (``orphan_landing``) until a new plan, or until it reports flying again
  after its landing was seen: the supervisor then makes its unflown lanes
  wait for the operator's confirmation instead of handing them on.
* **Held with no controller, whatever the cause.** The same countdown also
  starts whenever a flying drone's edge reports it ``manual`` or ``held``
  and no dashboard controls it, not only after a disconnect: for example
  when a ``resume_search`` way back was blocked by an obstacle after the
  edge had acknowledged it (the edge then puts the drone back under manual
  control by itself), or when the drone a tile switch tried to resume
  refused. It does not start while a ``resume_search`` or a ``land`` for
  that drone awaits its answer, for ``_RELEASE_GRACE_S`` after a
  ``resume_search`` was acknowledged (telemetry sent before the release may
  still say ``manual``), while the edge reports the drone landing, or for a
  drone on the ground. A running countdown is never restarted by later
  telemetry. It starts as ``manual_orphan_started`` and is cancelled as
  above. A drone with no countdown running (one that was already landing
  when it became held, for example) is still remembered as orphan-landed,
  as having landed by itself, when its telemetry goes from flying to on the
  ground while its edge reports it ``manual`` or ``held``, no dashboard
  controls it and no operator ``land`` awaits its answer.
* **Commands.** Moves, rotations and Takeoff go only to the controlled
  drone, and are refused here (reason ``drone_unreachable``) while its
  telemetry says the drone link is down. Land is never gated: it goes to
  any fleet drone, at once, as the ordinary ``land``.
* **Unsupported edges.** A drone whose telemetry has no ``control_mode`` is
  run by an edge that does not know manual takeover; ``take_control`` is
  refused with reason ``unsupported`` and says so.
* **LAND ALL** does not go through this class (``app._land_all``), and
  ``on_land_all`` drops every manual command still waiting to be sent.
* **Non-blocking.** Commands are queued and sent by one worker thread in
  order, and answers arrive through ``on_reply``, so a slow or dead link
  never holds up a socket handler (and so never delays a LAND ALL from the
  same browser). A command with no answer after ``reply_timeout_s`` is
  reported as failed.

Every decision is logged as a structured ``flight_safety`` event
(``manual_take_control``, ``manual_release``, ``manual_command``,
``manual_result``, ``manual_controller_lost``).
"""
from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from flightrisk import fleet_protocol as fp
from flightrisk import obs_events

log = logging.getLogger("flightrisk.dashboard.manual")

__all__ = [
    "DEFAULT_ORPHAN_LAND_S",
    "MANUAL_ACTIONS",
    "MANUAL_REPLY_TIMEOUT_S",
    "MIN_ORPHAN_LAND_S",
    "ORPHAN_LANDED_BY_GROUND",
    "ORPHAN_LANDED_ITSELF",
    "ORPHAN_LAND_ENV",
    "ManualControl",
    "manual_orphan_land_s",
]

# A drone held for manual control whose controller went away is landed after
# this long (docs/manual-takeover.md). The minimum keeps a mistyped value from
# landing a drone the moment a browser reloads.
ORPHAN_LAND_ENV = "FLIGHTRISK_MANUAL_ORPHAN_LAND_S"
DEFAULT_ORPHAN_LAND_S = 120.0
MIN_ORPHAN_LAND_S = 30.0


def manual_orphan_land_s(environ: Mapping[str, str] | None = None) -> float:
    """The orphan-landing time in seconds from ``FLIGHTRISK_MANUAL_ORPHAN_LAND_S``.

    Unset or empty means ``DEFAULT_ORPHAN_LAND_S`` (120). A value below
    ``MIN_ORPHAN_LAND_S`` (30) is raised to it, with a warning. Raises
    ``ValueError`` for a value that is not a finite number.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(ORPHAN_LAND_ENV) or "").strip()
    if not raw:
        return DEFAULT_ORPHAN_LAND_S
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{ORPHAN_LAND_ENV} must be a number of seconds, got {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{ORPHAN_LAND_ENV} must be a finite number of seconds, got {raw!r}")
    if value < MIN_ORPHAN_LAND_S:
        log.warning("%s=%s is below the %g s minimum; using %g s", ORPHAN_LAND_ENV, raw,
                    MIN_ORPHAN_LAND_S, MIN_ORPHAN_LAND_S)
        return MIN_ORPHAN_LAND_S
    return value

# How long a manual command may wait for its answer. A step of at most
# 100 cm takes the Tello a couple of seconds; a takeoff and climb longer.
MANUAL_REPLY_TIMEOUT_S = 4.0
_TAKEOFF_REPLY_TIMEOUT_S = 20.0
_RESUME_REPLY_TIMEOUT_S = 6.0
# Telemetry that still says ``autonomous`` this soon after a manual_hold is
# not taken as the edge releasing the drone (it may predate the hold).
_RELEASE_GRACE_S = 3.0
DEFAULT_STEP_CM = 30
DEFAULT_ROTATE_DEG = 30
# An orphan landing that could not be sent is tried again after this long.
_ORPHAN_LAND_RETRY_S = 2.0
#: How a held drone with no controller came to land (``ManualControl.orphan_landing``):
#: the ground's countdown sent it the landing, or it landed by itself (its battery
#: landing, the edge's link-loss landing) while the countdown ran.
ORPHAN_LANDED_BY_GROUND = fp.ORPHAN_LAND_REASON
ORPHAN_LANDED_ITSELF = "landed"

# Dashboard command names -> what they send.
MANUAL_ACTIONS = frozenset({
    "takeoff", "land", "forward", "back", "left", "right", "up", "down", "cw", "ccw", "hover",
})
_MOVES = fp.MANUAL_TRANSLATIONS
_ROTATIONS = fp.MANUAL_ROTATIONS


@dataclass
class _Pending:
    drone_id: str
    action: str
    mtype: str
    sent_at: float
    timeout_s: float


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


class ManualControl:
    """Manual takeover for the fleet supervisor (see the module docstring)."""

    def __init__(
        self,
        drone_ids: Iterable[str],
        send: Callable[[str, dict[str, Any]], None],
        emit: Callable[[str, dict[str, Any]], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        fence: Callable[[], fp.Fence | None] | None = None,
        reply_timeout_s: float = MANUAL_REPLY_TIMEOUT_S,
        background: bool = True,
        orphan_land_s: float | None = None,
        lost: Callable[[str], bool] | None = None,
    ):
        self._drone_ids = tuple(drone_ids)
        # Whether the fleet coordinator has declared a drone lost: such a
        # drone's lanes are no longer its own, so it never resumes its search.
        self._lost = lost
        self._send = send
        self._emit_fn = emit
        self._clock = clock
        self._fence = fence if fence is not None else (lambda: None)
        self._reply_timeout_s = float(reply_timeout_s)
        self._lock = threading.RLock()
        self._controlled: str | None = None
        self._controller_sid: str | None = None
        self._hold_sent_at: float | None = None
        # Per drone, from its latest telemetry.
        self._edge_mode: dict[str, str | None] = {}
        self._link_ok: dict[str, bool | None] = {}
        self._link_detail: dict[str, str | None] = {}
        self._flying: dict[str, bool | None] = {}
        self._last: dict[str, dict[str, Any]] = {}
        self._pending: dict[tuple[str, str], _Pending] = {}
        # Orphan-landing deadlines (ground clock), one per held drone whose
        # controller went away.
        self.orphan_land_s = manual_orphan_land_s() if orphan_land_s is None else max(
            MIN_ORPHAN_LAND_S, float(orphan_land_s)
        )
        self._orphans: dict[str, float] = {}
        # Drones that landed while held with no controller, since the last
        # ``forget_orphan_landings`` (a new plan), with how they landed
        # (``ORPHAN_LANDED_BY_GROUND`` or ``ORPHAN_LANDED_ITSELF``): the
        # supervisor makes their lanes wait for the operator
        # (``orphan_landing``). ``_orphan_down`` holds those whose telemetry
        # has shown them on the ground since; one that reports flying after
        # that is in the air again and loses the mark.
        self._orphan_landed: dict[str, str] = {}
        self._orphan_down: set[str] = set()
        # When each drone's last ``resume_search`` was acknowledged (ground
        # clock): telemetry this soon after may predate the edge's release.
        self._resumed_at: dict[str, float] = {}
        self._background = bool(background)
        self._queue: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue()
        self._worker: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def controlled(self) -> str | None:
        with self._lock:
            return self._controlled

    def mode(self, drone_id: str) -> str:
        """``manual`` (this dashboard controls it), ``held`` (paused, no controller) or ``autonomous``."""
        with self._lock:
            if drone_id == self._controlled:
                return "manual"
            if self._edge_mode.get(drone_id) in ("manual", "held"):
                return "held"
            return "autonomous"

    def paused(self, drone_id: str) -> bool:
        """True while the drone's search is paused for manual control (controlled here or reported paused)."""
        return self.mode(drone_id) != "autonomous"

    def paused_drones(self) -> list[str]:
        return [drone_id for drone_id in self._drone_ids if self.paused(drone_id)]

    def drone_status(self, drone_id: str) -> dict[str, Any]:
        with self._lock:
            edge_mode = self._edge_mode.get(drone_id)
            deadline = self._orphans.get(drone_id)
            return {
                "control_mode": self.mode(drone_id),
                "edge_control_mode": edge_mode,
                "manual_supported": edge_mode is not None,
                "manual_last": dict(self._last[drone_id]) if drone_id in self._last else None,
                # Seconds until the ground lands this held drone for having no
                # controller, or None when no countdown runs.
                "orphan_land_in_s": None if deadline is None else max(0, math.ceil(deadline - self._clock())),
            }

    # ------------------------------------------------------------------
    # Orphan landing (a held drone whose controller went away)
    # ------------------------------------------------------------------

    def _start_orphan_locked(self, drone_id: str) -> bool:
        """Start the orphan-landing countdown for *drone_id*. Caller holds ``_lock``."""
        if self._flying.get(drone_id) is False:
            return False  # already on the ground: nothing to land
        self._orphans[drone_id] = self._clock() + self.orphan_land_s
        return True

    def _unattended_locked(self, drone_id: str, mode: Any, state: Any) -> bool:
        """True when *drone_id*'s edge reports it paused for manual control and nobody here controls it.

        Caller holds ``_lock``. Only a drone without a countdown, flying (or
        not known to be on the ground) and not landing counts, and not while
        a ``resume_search`` or ``land`` for it awaits its answer or within
        ``_RELEASE_GRACE_S`` of an acknowledged ``resume_search``: in those
        cases the edge is about to release or land the drone.
        """
        if mode not in ("manual", "held") or drone_id == self._controlled or drone_id in self._orphans:
            return False
        if state == "landing" or self._flying.get(drone_id) is False:
            return False
        if any(p.drone_id == drone_id and p.mtype in (fp.RESUME_SEARCH, fp.LAND) for p in self._pending.values()):
            return False
        resumed = self._resumed_at.get(drone_id)
        return resumed is None or self._clock() - resumed >= _RELEASE_GRACE_S

    def _landed_unattended_locked(self, drone_id: str, mode: Any, was_flying: bool | None) -> bool:
        """True when *drone_id*, held with nobody controlling it, has just landed with no countdown running.

        Caller holds ``_lock``. Its edge reports it ``manual`` or ``held``,
        this dashboard does not control it, its previous telemetry showed it
        flying and this one on the ground, it carries no orphan-landing mark
        yet, and no ``land`` the operator sent awaits its answer (then the
        operator landed it). A held drone that was already landing (its
        battery landing, for example) when it lost its controller gets no
        countdown (``_unattended_locked``), so this is how its landing is seen.
        """
        if mode not in ("manual", "held") or drone_id == self._controlled or drone_id in self._orphan_landed:
            return False
        if was_flying is not True or self._flying.get(drone_id) is not False:
            return False
        return not any(p.drone_id == drone_id and p.mtype == fp.LAND and p.action == "land"
                       for p in self._pending.values())

    def _note_orphan_flight_locked(self, drone_id: str) -> str | None:
        """Track an orphan-landed drone's flight state from its telemetry. Caller holds ``_lock``.

        The landing is seen once the telemetry shows the drone on the ground.
        A drone that reports flying after that has taken off again (a person
        flies it, or a new start): its orphan-landing mark is removed, and
        how it had landed is returned so the caller can log it. Returns None
        otherwise. Telemetry that still shows the drone flying right after
        the countdown sent its landing does not count: the landing was not
        seen yet.
        """
        if drone_id not in self._orphan_landed:
            return None
        flying = self._flying.get(drone_id)
        if flying is False:
            self._orphan_down.add(drone_id)
            return None
        if flying is True and drone_id in self._orphan_down:
            self._orphan_down.discard(drone_id)
            return self._orphan_landed.pop(drone_id)
        return None

    def cancel_orphan(self, drone_id: str, reason: str) -> bool:
        """Stop *drone_id*'s orphan-landing countdown; True if one was running."""
        with self._lock:
            deadline = self._orphans.pop(drone_id, None)
        if deadline is None:
            return False
        obs_events.flight_safety("manual_orphan_cancelled", reason=reason, drone_id=drone_id, level=logging.INFO)
        self._emit_status()
        return True

    def orphan_landing(self, drone_id: str) -> str | None:
        """How *drone_id* landed while held with no controller, or None.

        ``ORPHAN_LANDED_BY_GROUND`` once the orphan countdown has sent it its
        landing, ``ORPHAN_LANDED_ITSELF`` when its telemetry showed it on the
        ground while the countdown ran (a battery landing, for example). The
        mark lasts until ``forget_orphan_landings`` (a new plan), or until
        the drone reports flying again after its landing was seen.
        """
        with self._lock:
            return self._orphan_landed.get(drone_id)

    def orphan_landed(self, drone_id: str) -> bool:
        """True while *drone_id* carries an orphan-landing mark (``orphan_landing``)."""
        return self.orphan_landing(drone_id) is not None

    def orphaned(self, drone_id: str) -> bool:
        """True while *drone_id*'s orphan countdown runs, and while it carries an orphan-landing mark."""
        with self._lock:
            return drone_id in self._orphans or drone_id in self._orphan_landed

    def forget_orphan_landings(self) -> None:
        """Forget which drones landed while held with no controller; the supervisor calls it for a new plan."""
        with self._lock:
            self._orphan_landed.clear()
            self._orphan_down.clear()

    def check_orphans(self) -> list[str]:
        """Land every held drone whose controller has been gone for ``orphan_land_s``; returns their ids.

        Each drone whose landing was sent is remembered (``orphan_landed``).
        """
        now = self._clock()
        with self._lock:
            due = [drone_id for drone_id, deadline in self._orphans.items() if now >= deadline]
            for drone_id in due:
                del self._orphans[drone_id]
        for drone_id in due:
            msg = fp.make_manual_land(reason=fp.ORPHAN_LAND_REASON)
            obs_events.flight_safety(
                "manual_orphan_land", reason=fp.ORPHAN_LAND_REASON, drone_id=drone_id,
                waited_s=self.orphan_land_s,
            )
            log.warning("No controller for held %s for %g s: landing it", drone_id, self.orphan_land_s)
            with self._lock:
                self._pending[(drone_id, msg["cmd_id"])] = _Pending(
                    drone_id, "auto_land", fp.LAND, now, self._reply_timeout_s,
                )
            # Sent from the calling (monitor) thread, never queued behind
            # other commands: a landing must not wait.
            try:
                self._send(drone_id, msg)
                with self._lock:
                    self._orphan_landed[drone_id] = ORPHAN_LANDED_BY_GROUND
                    self._orphan_down.discard(drone_id)
            except Exception as exc:  # noqa: BLE001 - retried, never raised into the monitor
                with self._lock:
                    self._pending.pop((drone_id, msg["cmd_id"]), None)
                    # Try again shortly; if the edge stays unreachable its own
                    # link-loss landing applies.
                    self._orphans.setdefault(drone_id, now + _ORPHAN_LAND_RETRY_S)
                self._result(drone_id, "auto_land", False, reason="not_delivered",
                             detail=f"{str(exc)[:160]}; retrying in {_ORPHAN_LAND_RETRY_S:g} s")
        if due:
            self._emit_status()
        return due

    def status(self) -> dict[str, Any]:
        """The ``manual_status`` event: who is controlled and each drone's mode."""
        with self._lock:
            return {
                "controlled": self._controlled,
                "drones": {drone_id: self.drone_status(drone_id) for drone_id in self._drone_ids},
            }

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        try:
            self._emit_fn(name, payload)
        except Exception:  # the browser view must never stop control
            log.warning("manual emit %s failed", name, exc_info=True)

    def _emit_status(self) -> None:
        self._emit("manual_status", self.status())

    def _result(self, drone_id: str, action: str, ok: bool, *, reason: str | None = None,
                detail: str | None = None, **extra: Any) -> dict[str, Any]:
        """Record and emit the outcome of one operator action."""
        result = {"drone_id": drone_id, "action": action, "ok": ok, "reason": reason, "detail": detail, **extra}
        with self._lock:
            self._last[drone_id] = {key: result[key] for key in ("action", "ok", "reason", "detail")}
        obs_events.flight_safety(
            "manual_result", reason=reason or ("ok" if ok else "refused"), drone_id=drone_id,
            level=logging.INFO if ok else logging.WARNING, action=action, ok=ok, detail=detail,
        )
        self._emit("manual_result", result)
        return result

    # ------------------------------------------------------------------
    # Telemetry and replies
    # ------------------------------------------------------------------

    def observe_telemetry(self, drone_id: str, msg: Mapping[str, Any]) -> None:
        """Track the drone's control mode, flight state and drone link from its telemetry."""
        if drone_id not in self._drone_ids:
            return
        mode = msg.get("control_mode")
        released = False
        with self._lock:
            self._edge_mode[drone_id] = mode if mode in fp.CONTROL_MODES else None
            self._link_ok[drone_id] = msg.get("drone_link_ok") if isinstance(msg.get("drone_link_ok"), bool) else None
            detail = msg.get("drone_link_detail")
            self._link_detail[drone_id] = detail if isinstance(detail, str) else None
            was_flying = self._flying.get(drone_id)
            self._flying[drone_id] = msg.get("is_flying") if isinstance(msg.get("is_flying"), bool) else None
            orphan_over = None
            orphan_started = False
            self_landed = False
            flying_again = self._note_orphan_flight_locked(drone_id)
            if drone_id in self._orphans:
                if self._flying[drone_id] is False:
                    orphan_over = "landed"
                    # It landed by itself while nobody controlled it (its
                    # battery landing, for example): its lanes wait for the
                    # operator exactly as after the countdown's own landing.
                    self._orphan_landed[drone_id] = ORPHAN_LANDED_ITSELF
                    self._orphan_down.add(drone_id)
                    self_landed = True
                elif mode == "autonomous":
                    orphan_over = "edge_released"
            elif self._landed_unattended_locked(drone_id, mode, was_flying):
                # Held with nobody controlling it, it landed with no countdown
                # running: it was already landing when it became held, so no
                # countdown started. Its lanes wait for the operator all the same.
                self._orphan_landed[drone_id] = ORPHAN_LANDED_ITSELF
                self._orphan_down.add(drone_id)
                self_landed = True
            elif self._unattended_locked(drone_id, mode, msg.get("state")):
                # Held with nobody controlling it (a blocked way back, a
                # refused resume): it must not hover until its battery runs
                # out, so it gets the same countdown as after a disconnect.
                orphan_started = self._start_orphan_locked(drone_id)
            hold_pending = any(p.drone_id == drone_id and p.mtype == fp.MANUAL_HOLD for p in self._pending.values())
            recent_hold = self._hold_sent_at is not None and self._clock() - self._hold_sent_at < _RELEASE_GRACE_S
            if drone_id == self._controlled and mode == "autonomous" and not hold_pending and not recent_hold:
                # The edge ended manual control itself (a new plan on the
                # ground, or an edge restart): this dashboard no longer has it.
                self._controlled = None
                self._controller_sid = None
                released = True
        if flying_again is not None:
            obs_events.flight_safety("manual_orphan_landing_cleared", reason="flying_again", drone_id=drone_id,
                                     landing=flying_again, level=logging.INFO)
            log.warning("%s reports flying again after landing while held with no controller; it is no longer "
                        "treated as orphan-landed", drone_id)
        if self_landed:
            # A lost drone was sent land by the fleet coordinator, so it did not land by itself.
            lost = self._is_lost(drone_id)
            obs_events.flight_safety("manual_orphan_landed", reason=ORPHAN_LANDED_ITSELF, drone_id=drone_id,
                                     lost=lost)
            if lost:
                log.warning("Held %s landed while nobody controlled it, after it was declared lost and the ground "
                            "sent it land", drone_id)
            else:
                log.warning("Held %s landed by itself while nobody controlled it; its unflown lanes wait for the "
                            "operator", drone_id)
        if orphan_over is not None:
            self.cancel_orphan(drone_id, orphan_over)
        if orphan_started:
            obs_events.flight_safety("manual_orphan_started", reason="no_controller", drone_id=drone_id,
                                     control_mode=mode, orphan_land_s=self.orphan_land_s)
            log.warning("%s's edge reports it %s with no controller; it lands in %g s unless someone takes "
                        "control or presses Resume search", drone_id, mode, self.orphan_land_s)
            self._emit_status()
        if released:
            obs_events.flight_safety("manual_release", reason="edge_released", drone_id=drone_id)
            self._result(drone_id, "release", True, reason="edge_released",
                         detail=f"{drone_id}'s edge reports autonomous control again; manual control ended")
            self._emit_status()

    def on_reply(self, msg: Mapping[str, Any]) -> bool:
        """Handle an edge ``ack``/``nack``; True when it answered a manual command (consumed here)."""
        if msg.get("type") not in (fp.ACK, fp.NACK):
            return False
        drone_id, cmd_id = msg.get("drone_id"), msg.get("cmd_id")
        if not isinstance(drone_id, str) or not isinstance(cmd_id, str):
            return False
        with self._lock:
            pending = self._pending.pop((drone_id, cmd_id), None)
        if pending is None:
            return fp.is_manual_reply(msg)
        ok = msg.get("type") == fp.ACK
        reason = None if ok else str(msg.get("reason") or "refused")
        detail = msg.get("detail") if isinstance(msg.get("detail"), str) else None
        lost_control = False
        if not ok and pending.mtype == fp.MANUAL_HOLD:
            with self._lock:
                if self._controlled == drone_id:
                    # The drone refused to pause (for example it is landing):
                    # it is not under manual control.
                    self._controlled = None
                    self._controller_sid = None
                    lost_control = True
        extra = {key: msg[key] for key in ("control_mode", "x_cm", "y_cm", "heading_deg", "height_cm",
                                           "target_x_cm", "target_y_cm") if key in msg}
        self._result(drone_id, pending.action, ok, reason=reason, detail=detail, **extra)
        if ok and pending.mtype == fp.RESUME_SEARCH:
            # The search resumed: no orphan landing for this drone. If the
            # way back is blocked later, the edge reports the drone manual
            # again and the countdown starts from that telemetry.
            with self._lock:
                self._resumed_at[drone_id] = self._clock()
            self.cancel_orphan(drone_id, "resume_search")
        if lost_control or pending.mtype in (fp.MANUAL_HOLD, fp.RESUME_SEARCH):
            self._emit_status()
        return True

    def check_timeouts(self) -> list[str]:
        """Report manual commands that got no answer in time; returns their actions."""
        now = self._clock()
        expired = []
        with self._lock:
            for key, pending in list(self._pending.items()):
                if now - pending.sent_at >= pending.timeout_s:
                    del self._pending[key]
                    expired.append(pending)
        for pending in expired:
            self._result(pending.drone_id, pending.action, False, reason="no_reply",
                         detail=f"no answer from {pending.drone_id} within {pending.timeout_s:g} s")
        return [pending.action for pending in expired]

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _queue_send(self, drone_id: str, msg: dict[str, Any], action: str, timeout_s: float | None = None) -> None:
        """Register *msg* as pending and send it (on the worker thread unless ``background`` is off)."""
        with self._lock:
            self._pending[(drone_id, msg["cmd_id"])] = _Pending(
                drone_id, action, msg["type"], self._clock(),
                self._reply_timeout_s if timeout_s is None else timeout_s,
            )
        if not self._background:
            self._deliver(drone_id, msg)
            return
        self._queue.put((drone_id, msg))
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run_worker, daemon=True, name="manual-control-send")
                self._worker.start()

    def _run_worker(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=30.0)
            except queue.Empty:
                return
            if item is None:
                return
            self._deliver(*item)

    def _deliver(self, drone_id: str, msg: dict[str, Any]) -> None:
        try:
            self._send(drone_id, msg)
        except Exception as exc:  # noqa: BLE001 - reported to the operator, never raised into a handler
            with self._lock:
                pending = self._pending.pop((drone_id, msg["cmd_id"]), None)
            if pending is not None:
                self._result(drone_id, pending.action, False, reason="not_delivered", detail=str(exc)[:200])

    def on_land_all(self) -> int:
        """LAND ALL was pressed: drop every manual command still waiting to be sent; returns how many."""
        dropped = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            drone_id, msg = item
            with self._lock:
                self._pending.pop((drone_id, msg["cmd_id"]), None)
            dropped += 1
        if dropped:
            obs_events.flight_safety("manual_commands_dropped", reason="land_all", dropped=dropped)
        with self._lock:
            orphans = list(self._orphans)
        for drone_id in orphans:
            self.cancel_orphan(drone_id, "land_all")
        return dropped

    def _is_lost(self, drone_id: str) -> bool:
        """True when the coordinator has declared *drone_id* lost. An unreadable answer counts as lost."""
        check = self._lost
        if check is None:
            return False
        try:
            return bool(check(drone_id))
        except Exception:  # fail closed: never resume a drone that may be lost
            log.warning("lost check failed for %s", drone_id, exc_info=True)
            return True

    def _fence_now(self) -> fp.Fence | None:
        try:
            return self._fence()
        except Exception:  # noqa: BLE001 - no fence is the v1 link's form
            return None

    # ------------------------------------------------------------------
    # Operator actions
    # ------------------------------------------------------------------

    def take_control(self, drone_id: str, sid: str | None = None) -> dict[str, Any]:
        """Pause *drone_id*'s search and give this dashboard its controls; resume the previously controlled drone."""
        if drone_id not in self._drone_ids:
            return self._result(str(drone_id), "take_control", False, reason="invalid",
                                detail=f"{drone_id!r} is not in the fleet")
        with self._lock:
            if drone_id == self._controlled:
                # Clicking the active tile again does nothing (a reloaded
                # browser takes over the controller role).
                self._controller_sid = sid if sid is not None else self._controller_sid
                return {"drone_id": drone_id, "action": "take_control", "ok": True, "reason": None,
                        "detail": "already under manual control", "noop": True}
            if drone_id not in self._edge_mode:
                refusal = ("no_telemetry", f"no telemetry from {drone_id} yet; wait for its edge to connect")
            elif self._edge_mode.get(drone_id) is None:
                refusal = (
                    "unsupported",
                    (
                        f"{drone_id}'s edge does not support manual control (it reports no control_mode); "
                        "update the edge software"
                    ),
                )
            else:
                refusal = None
            if refusal is None:
                previous = self._controlled
                self._controlled = drone_id
                self._controller_sid = sid
                self._hold_sent_at = self._clock()
        if refusal is not None:
            return self._result(drone_id, "take_control", False, reason=refusal[0], detail=refusal[1])
        # A person is flying it again: no orphan landing (it starts again if
        # this controller goes away too).
        self.cancel_orphan(drone_id, "take_control")
        fence = self._fence_now()
        if previous is not None and self._is_lost(previous):
            # A lost drone never resumes its search (``resume_search``): it
            # stays held, and with nobody controlling it its orphan
            # countdown starts from its next telemetry.
            obs_events.flight_safety("manual_release", reason="switch", drone_id=previous, to_drone=drone_id,
                                     resumed=False)
            self._result(previous, "resume_search", False, reason="lost",
                         detail=(f"{previous} was declared lost, so it stays held instead of resuming its search; "
                                 f"take control of it again and land it"))
        elif previous is not None:
            obs_events.flight_safety("manual_release", reason="switch", drone_id=previous, to_drone=drone_id)
            self._queue_send(previous, fp.make_resume_search(drone_id=previous, fence=fence), "resume_search",
                             _RESUME_REPLY_TIMEOUT_S)
        obs_events.flight_safety("manual_take_control", reason="operator", drone_id=drone_id, previous=previous)
        self._queue_send(drone_id, fp.make_manual_hold(drone_id=drone_id, fence=fence), "take_control")
        self._emit_status()
        return {"drone_id": drone_id, "action": "take_control", "ok": True, "reason": None,
                "detail": None, "previous": previous}

    def resume_search(self, drone_id: str, sid: str | None = None) -> dict[str, Any]:
        """End manual control of *drone_id* only, and let it resume its search."""
        if drone_id not in self._drone_ids:
            return self._result(str(drone_id), "resume_search", False, reason="invalid",
                                detail=f"{drone_id!r} is not in the fleet")
        if self._is_lost(drone_id):
            # Its unflown lanes were handed on, or wait for the operator's
            # Re-task: resuming would fly them twice. It stays as it is.
            return self._result(
                drone_id, "resume_search", False, reason="lost",
                detail=(f"{drone_id} was declared lost, so its lanes are no longer its own and it cannot resume "
                        f"its search; land it, then make a new plan to fly it again"),
            )
        with self._lock:
            controlled = drone_id == self._controlled
            paused_elsewhere = not controlled and self._edge_mode.get(drone_id) in ("manual", "held")
            if controlled:
                self._controlled = None
                self._controller_sid = None
        if not (controlled or paused_elsewhere):
            return self._result(drone_id, "resume_search", False, reason="not_manual",
                                detail=f"{drone_id} is not under manual control")
        obs_events.flight_safety("manual_release", reason="resume_search", drone_id=drone_id)
        self._queue_send(drone_id, fp.make_resume_search(drone_id=drone_id, fence=self._fence_now()),
                         "resume_search", _RESUME_REPLY_TIMEOUT_S)
        self._emit_status()
        return {"drone_id": drone_id, "action": "resume_search", "ok": True, "reason": None, "detail": None}

    def command(self, drone_id: str, action: str, *, sid: str | None = None,
                distance_cm: Any = None, degrees: Any = None) -> dict[str, Any]:
        """Send one manual action (a step, a rotation, takeoff or land) to *drone_id*."""
        if drone_id not in self._drone_ids:
            return self._result(str(drone_id), str(action), False, reason="invalid",
                                detail=f"{drone_id!r} is not in the fleet")
        if action not in MANUAL_ACTIONS:
            return self._result(drone_id, str(action), False, reason="invalid", detail=f"unknown command {action!r}")
        if action == "land":
            # Landing is never gated: any fleet drone, controlled or not,
            # link up or down, always as the ordinary land.
            obs_events.flight_safety("manual_command", reason="land", drone_id=drone_id, action="land")
            self._queue_send(drone_id, fp.make_manual_land(), "land")
            return {"drone_id": drone_id, "action": "land", "ok": True, "reason": None, "detail": None}
        with self._lock:
            controlled = drone_id == self._controlled
            link_ok = self._link_ok.get(drone_id)
            link_detail = self._link_detail.get(drone_id)
        if not controlled:
            return self._result(drone_id, action, False, reason="not_manual",
                                detail=f"click {drone_id}'s video tile to take manual control first")
        if link_ok is False:
            return self._result(drone_id, action, False, reason="drone_unreachable",
                                detail=link_detail or f"{drone_id}'s edge cannot reach the drone")
        fence = self._fence_now()
        if action == "takeoff":
            msg = fp.make_manual_takeoff(drone_id=drone_id, fence=fence)
            timeout = _TAKEOFF_REPLY_TIMEOUT_S
        elif action == "hover":
            # Stop: the drone already hovers after every step; this only
            # renews the dead-man and confirms the hold.
            msg = fp.make_manual_hold(drone_id=drone_id, fence=fence)
            timeout = None
        elif action in _ROTATIONS:
            amount = _clamp(degrees, fp.MANUAL_ROTATE_MIN_DEG, fp.MANUAL_ROTATE_MAX_DEG, DEFAULT_ROTATE_DEG)
            msg = fp.make_manual_move(action, degrees=amount, drone_id=drone_id, fence=fence)
            timeout = None
        else:
            amount = _clamp(distance_cm, fp.MANUAL_STEP_MIN_CM, fp.MANUAL_STEP_MAX_CM, DEFAULT_STEP_CM)
            msg = fp.make_manual_move(action, distance_cm=amount, drone_id=drone_id, fence=fence)
            timeout = None
        obs_events.flight_safety("manual_command", reason=action, drone_id=drone_id, action=action, level=logging.INFO)
        self._queue_send(drone_id, msg, action, timeout)
        return {"drone_id": drone_id, "action": action, "ok": True, "reason": None, "detail": None}

    def controller_disconnected(self, sid: str | None) -> str | None:
        """The browser *sid* went away; if it held control, the drone stays held and its orphan countdown starts.

        Returns that drone id.
        """
        with self._lock:
            if sid is None or sid != self._controller_sid or self._controlled is None:
                return None
            drone_id = self._controlled
            self._controlled = None
            self._controller_sid = None
            counting = self._start_orphan_locked(drone_id)
        obs_events.flight_safety("manual_controller_lost", reason="disconnect", drone_id=drone_id,
                                 orphan_land_s=self.orphan_land_s if counting else None)
        log.warning("Manual controller of %s disconnected; %s stays held (hovering)%s until "
                    "someone takes control or presses Resume search", drone_id, drone_id,
                    f" and lands in {self.orphan_land_s:g} s" if counting else "")
        self._emit_status()
        return drone_id
