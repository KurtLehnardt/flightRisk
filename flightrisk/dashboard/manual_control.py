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
  example after the controlling browser went away).
* **Controller gone.** When the browser that took control disconnects
  (closed or reloaded), the drone stays paused: it keeps hovering where it
  is, and its edge reports it ``held``. Nothing is sent, because the safer
  choice is to start no motion without an operator watching; an
  autonomous return could start while no one looks at the dashboard. The
  dashboard shows the drone as held after a reload, and the operator takes
  control again or presses Resume search. The edge's ground-link watchdog
  and battery landings still apply.
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
import queue
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from flightrisk import fleet_protocol as fp
from flightrisk import obs_events

log = logging.getLogger("flightrisk.dashboard.manual")

__all__ = ["MANUAL_ACTIONS", "MANUAL_REPLY_TIMEOUT_S", "ManualControl"]

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
    ):
        self._drone_ids = tuple(drone_ids)
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
            return {
                "control_mode": self.mode(drone_id),
                "edge_control_mode": edge_mode,
                "manual_supported": edge_mode is not None,
                "manual_last": dict(self._last[drone_id]) if drone_id in self._last else None,
            }

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
            self._flying[drone_id] = msg.get("is_flying") if isinstance(msg.get("is_flying"), bool) else None
            hold_pending = any(p.drone_id == drone_id and p.mtype == fp.MANUAL_HOLD for p in self._pending.values())
            recent_hold = self._hold_sent_at is not None and self._clock() - self._hold_sent_at < _RELEASE_GRACE_S
            if drone_id == self._controlled and mode == "autonomous" and not hold_pending and not recent_hold:
                # The edge ended manual control itself (a new plan on the
                # ground, or an edge restart): this dashboard no longer has it.
                self._controlled = None
                self._controller_sid = None
                released = True
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
        return dropped

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
        fence = self._fence_now()
        if previous is not None:
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
        """The browser *sid* went away; if it held control, the drone stays held. Returns that drone id."""
        with self._lock:
            if sid is None or sid != self._controller_sid or self._controlled is None:
                return None
            drone_id = self._controlled
            self._controlled = None
            self._controller_sid = None
        obs_events.flight_safety("manual_controller_lost", reason="disconnect", drone_id=drone_id)
        log.warning("Manual controller of %s disconnected; %s stays held (hovering) until "
                    "someone takes control or presses Resume search", drone_id, drone_id)
        self._emit_status()
        return drone_id
