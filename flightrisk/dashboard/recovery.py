"""Operator match responses and the post-confirmation flight sequence.

Only an explicit operator action (the Confirm / Dismiss buttons on the match
pop-up, delivered over the authenticated SocketIO channel or the API-key-gated
REST endpoint) reaches :func:`handle_alert_response`. Nothing produced by the
vision pipeline or the Gemma reasoning model can trigger the flight sequence.

Confirm flight sequence (runs on its own daemon thread, never on the socket
handler or the frame loop):

1. Stop the search pattern and hover.
2. If the drone declares ``Capability.RETURN_TO_HOME`` *and* the battery is
   at or above :data:`RTH_MIN_BATTERY_PCT`, fly home, then land.
3. Otherwise land in place.
4. Any exception at any step falls back to landing in place.

No hardware controller on ``main`` implements ``return_to_home()`` yet: the
Tello has no GPS and the dashboard does not track a dead-reckoned pose, and
the MAVLink controller does not wire up RTL. Until one does, Confirm stops the
search and lands in place. A backend opts in by declaring the capability and
implementing the ``Navigation`` role (see :mod:`flightrisk.drone.controller`),
with no change here. Contract for implementers: ``return_to_home()`` must
block until the drone is back over its takeoff point (raising on failure or
timeout) and must not land by itself; this module issues the land command
afterwards. A non-OK :class:`CommandResult` from it counts as a failure.

With no primary drone (an edge fleet), Confirm does not hover or return home.
It lands every drone in place through LAND ALL. The Tello has no return-to-home,
so each drone lands wherever it currently is.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from flightrisk.dashboard.alert_latch import alert_latch
from flightrisk.dashboard.safety import DEFAULT_RTH_MIN_BATTERY_PCT, SafetyPolicy
from flightrisk.dashboard.state import app_state, fleet_lock
from flightrisk.drone.controller import Capability, capabilities_of, command_result
from flightrisk.drone.snapshot import read_telemetry

_log = logging.getLogger(__name__)

#: Minimum battery percentage required to attempt a return-to-home before
#: landing. Below this the drone lands where it is. The value is deliberately
#: conservative: a Tello flies roughly 12-13 minutes on a full pack, the
#: existing frame-loop failsafe force-lands at 10 %, and a search pattern can
#: take the drone several hundred metres from its takeoff point. Requiring
#: 40 % leaves a large margin for the return leg plus wind and battery sag, so
#: a return is never started that the critical-battery failsafe would have to
#: abort mid-flight. An unknown battery reading (0) always lands in place.
RTH_MIN_BATTERY_PCT = DEFAULT_RTH_MIN_BATTERY_PCT

EmitFn = Callable[[str, dict], Any]


def _safe_emit(emit: EmitFn | None, event: str, data: dict) -> None:
    """Emit without ever letting a UI/transport error interrupt flight logic."""
    if emit is None:
        return
    try:
        emit(event, data)
    except Exception:  # noqa: BLE001 -- UI errors must never block flight logic
        _log.exception("recovery_emit_failed event=%s", event)


def _status(emit: EmitFn | None, stage: str, message: str, **extra: Any) -> None:
    payload = {"stage": stage, "message": message}
    payload.update(extra)
    _safe_emit(emit, "recovery_status", payload)


def run_confirm_recovery(
    drone: Any,
    emit: EmitFn | None = None,
    min_battery_pct: int = RTH_MIN_BATTERY_PCT,
) -> str:
    """Execute the post-confirmation flight sequence synchronously.

    Args:
        drone: The primary ``DroneController`` (or ``None`` if none connected).
        emit: ``emit(event, data)`` callable for UI status updates.
        min_battery_pct: Battery threshold for attempting return-to-home.

    Returns:
        A short outcome string (``"no_drone"``, ``"not_flying"``,
        ``"returned_and_landed"``, ``"landed_in_place"``,
        ``"failsafe_landed"`` or ``"failsafe_land_failed"``), mainly for tests
        and logs.
    """
    if drone is None:
        _log.warning("confirm_recovery: no drone connected; nothing to fly")
        _status(emit, "no_drone", "No drone connected. Search stopped.")
        return "no_drone"

    policy = SafetyPolicy(rth_min_battery_pct=min_battery_pct)
    capabilities = capabilities_of(drone)
    snapshot = read_telemetry(drone)
    if not snapshot.is_flying:
        _log.info("confirm_recovery: drone is not flying; nothing to do")
        _status(emit, "not_flying", "Drone is on the ground. Search stopped.")
        return "not_flying"

    try:
        drone.hover()
        battery = int(policy.battery_pct(snapshot) or 0)
        has_rth = capabilities.supports(Capability.RETURN_TO_HOME)

        if policy.can_return_home(capabilities, battery):
            _log.info(
                "confirm_recovery: battery %d%% >= %d%%, returning home before landing",
                battery, min_battery_pct,
            )
            _status(emit, "returning_home", "Returning home...", battery=battery)
            result = command_result(drone.return_to_home(), "return_to_home")
            if not result.ok:
                raise RuntimeError(f"return to home not executed ({result.outcome.value})")
            _log.info("confirm_recovery: reached home, landing")
            _status(emit, "landing", "Landing", battery=battery)
            drone.land()
            _status(emit, "landed", "Landed at home.", battery=battery)
            return "returned_and_landed"

        if has_rth:
            _log.critical(
                "confirm_recovery: battery %d%% below return-to-home minimum %d%%; landing in place",
                battery, min_battery_pct,
            )
            reason = "low_battery"
            message = f"Battery {battery}% too low to return home. Landing in place."
        else:
            _log.critical(
                "confirm_recovery: drone has no return-to-home capability; landing in place"
            )
            reason = "no_return_to_home"
            message = "Return-to-home not supported by this drone. Landing in place."
        _status(emit, "landing", message, battery=battery, reason=reason)
        drone.land()
        _status(emit, "landed", "Landed in place.", battery=battery, reason=reason)
        return "landed_in_place"
    except Exception as exc:  # noqa: BLE001 -- any failure must fall back to landing
        _log.critical("confirm_recovery: error during flight sequence (%s); fail-safe landing", exc, exc_info=True)
        _status(emit, "landing", "Error during return. Fail-safe landing in place.", error=str(exc))
        try:
            drone.land()
        except Exception as land_exc:  # noqa: BLE001 -- report, never raise from the flight thread
            _log.critical(
                "confirm_recovery: FAIL-SAFE LAND FAILED (%s); operator must take manual control",
                land_exc, exc_info=True,
            )
            _status(
                emit, "error",
                "Fail-safe landing failed. Take manual control of the drone now.",
                error=str(land_exc),
            )
            return "failsafe_land_failed"
        _status(emit, "landed", "Landed in place (fail-safe).")
        return "failsafe_landed"


def run_fleet_confirm_landing(emit: EmitFn | None = None) -> str:
    """Land every drone of a multi-drone fleet after the operator confirms a match.

    A fleet (edge source with a coordinator) has no single primary drone to
    return home, so the confirmed match lands the whole fleet through the
    same path as the LAND ALL button (``_land_all``): ground transport
    broadcast, fleet coordinator and local drones, all at once.
    """
    from flightrisk.dashboard.app import _land_all  # lazy: app imports this module

    _status(emit, "landing", "Match confirmed. Landing the fleet.")
    try:
        report = _land_all()
    except Exception as exc:  # noqa: BLE001 -- report, never raise from the flight thread
        _log.critical("confirm_recovery: fleet landing failed (%s)", exc, exc_info=True)
        _status(emit, "error", "Fleet landing failed. Land every drone manually now.", error=str(exc))
        return "failsafe_land_failed"
    if report.get("reached_any"):
        _status(emit, "landed", "Landing order sent to the fleet.")
        return "fleet_landed"
    _status(emit, "error", "Landing order reached no drone. Land every drone manually now.")
    return "failsafe_land_failed"


class ConfirmRecovery:
    """Runs :func:`run_confirm_recovery` at most once per confirmed match."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = False
        self.thread: threading.Thread | None = None

    def start(self, drone: Any, emit: EmitFn | None = None, fleet: bool = False) -> bool:
        """Start the flight sequence on a daemon thread.

        Returns False (and does nothing) if it was already started, so a
        double-clicked Confirm can never issue two flight sequences.
        """
        with self._lock:
            if self._started:
                return False
            self._started = True
            t = threading.Thread(
                target=run_fleet_confirm_landing if fleet else run_confirm_recovery,
                args=(emit,) if fleet else (drone, emit),
                daemon=True,
                name="confirm-recovery",
            )
            self.thread = t
        t.start()
        return True

    def reset(self) -> None:
        with self._lock:
            self._started = False
            self.thread = None


#: Process-wide recovery runner.
confirm_recovery = ConfirmRecovery()


def reset_alert_flow() -> None:
    """Reset the alert latch and recovery runner for a new search session."""
    alert_latch.reset()
    confirm_recovery.reset()


def _record_feedback(payload: dict, feedback: str, notes: str | None) -> None:
    match_id = payload.get("match_id")
    db = app_state.db
    if not match_id or db is None:
        return
    try:
        db.add_feedback(match_id, app_state.session_id or "unknown", feedback, notes)
    except Exception:  # noqa: BLE001 -- feedback is best-effort; never block a response
        _log.exception("alert_feedback_write_failed match_id=%s", match_id)


def handle_alert_response(
    alert_id: Any,
    action: Any,
    socketio: Any,
    notes: str | None = None,
) -> tuple[dict, int]:
    """Apply an operator's Confirm/Dismiss to the outstanding match alert.

    Never blocks: the confirm flight sequence runs on its own thread.

    Returns:
        ``(response_body, http_status)``. Duplicate or stale responses (for
        example a double click) return ``ok=False`` with status 409 and change
        nothing.
    """
    if action not in ("confirm", "dismiss"):
        return {"ok": False, "error": "action must be 'confirm' or 'dismiss'"}, 400
    if not isinstance(alert_id, str) or not alert_id:
        return {"ok": False, "error": "alert_id is required"}, 400
    notes = notes[:500] if isinstance(notes, str) else None

    if action == "dismiss":
        payload = alert_latch.dismiss(alert_id)
        if payload is None:
            return {"ok": False, "error": "no matching alert awaiting a response",
                    "state": alert_latch.snapshot()}, 409
        _log.info("match_alert_dismissed alert_id=%s track=%s", alert_id, payload.get("track_id"))
        _record_feedback(payload, "rejected", notes)
        body = {"ok": True, "alert_id": alert_id, "action": "dismiss", "state": alert_latch.snapshot()}
        _safe_emit(socketio.emit, "match_alert_resolved", dict(body))
        return body, 200

    payload = alert_latch.confirm(alert_id)
    if payload is None:
        return {"ok": False, "error": "no matching alert awaiting a response",
                "state": alert_latch.snapshot()}, 409
    # Stop the search pattern for good; matching stays off (latch RESOLVED).
    app_state.search_active = False
    _log.critical(
        "match_alert_confirmed_by_operator alert_id=%s track=%s; search stopped, starting recovery",
        alert_id, payload.get("track_id"),
    )
    _record_feedback(payload, "confirmed", notes)
    with fleet_lock:
        fleet = app_state.fleet
        drone = fleet.primary if fleet else None
    body = {"ok": True, "alert_id": alert_id, "action": "confirm", "state": alert_latch.snapshot()}
    _safe_emit(socketio.emit, "match_alert_resolved", dict(body))
    # No single primary drone but a fleet supervisor (edge source): land the
    # whole fleet through the LAND ALL path instead of doing nothing.
    fleet_mode = drone is None and app_state.fleet_supervisor is not None
    body["recovery_started"] = confirm_recovery.start(drone, socketio.emit, fleet=fleet_mode)
    return body, 200
