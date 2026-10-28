"""Ground side of detection profiles: per-drone choice, fleet default and dashboard state.

``ProfileBoard`` keeps what the ground knows about each drone's detection
profile (``flightrisk/vision/profiles.py``) and sends
``set_detection_profile`` commands for the dashboard:

* **What each edge runs.** An edge that supports profiles names its active
  profile in every ``detections`` message and, when it flies, in its
  telemetry (with whether it is a fallback). An edge that never names one is
  an older edge: the dashboard shows it as not supporting profiles and the
  ground never sends it a profile command.
* **Per-drone choice and fleet default.** The operator picks a profile per
  drone, or "fleet default". Setting the fleet default sends it to every
  drone that follows the default and runs another profile. With no fleet
  default and no per-drone choice nothing is sent, so every edge keeps its
  startup profile.
* **Mismatch.** A drone whose active profile differs from the operator's
  choice for it (its own choice, else the fleet default) is flagged as a
  mismatch, unless the edge is running that choice's fallback. The choice is
  sent again once each time such a drone first reports support, reports
  that a switch is allowed again (``detection_profile_switchable`` in its
  telemetry going from false to true, the edge's own flight check; an
  ``in_flight`` refusal re-arms it), or reconnects after being silent for
  ``reconnect_gap_s`` (for example an edge restart). Nothing is re-sent
  while a command to that drone is still waiting for its answer. The
  edge's own flight check still decides whether the switch happens.
* **Replies.** The edge's ``ack`` (active profile, fallback) or ``nack``
  (reason and an operator-facing ``detail``, for example "only on the
  ground") is shown per drone. A refusal is shown only when it answers the
  latest command this ground sent to that drone, so a late refusal of an
  older command never hides a newer result. A drone that is not connected,
  or that does not answer within ``reply_timeout_s``, is shown as such.
* **Detector health.** A flying edge reports ``detector_ok: false`` in
  telemetry while its frames fail to process; the dashboard shows it.
* **Standby ground.** In the two-ground fleet an edge obeys profile commands
  only from the leader ground. A ground whose role is ``standby`` refuses
  the operator's choice at once and the dashboard hides the controls,
  rather than sending a command the edge would ignore.

The edge enforces the safety rule (no switch in flight); the ground never
second-guesses it, so the operator always sees the edge's own reason.
Commands go out on a worker thread (``spawn``), never on a transport
delivery thread or a Socket.IO handler.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from flightrisk import fleet_protocol as fp
from flightrisk.vision.profiles import REGISTRY, ProfileRegistry

__all__ = ["EVENT", "MAX_DRONES", "RECONNECT_GAP_S", "REPLY_TIMEOUT_S", "STANDBY_NOTE", "ProfileBoard"]

log = logging.getLogger(__name__)

#: Socket.IO event carrying ``ProfileBoard.payload()`` to the browser.
EVENT = "detection_profiles"
#: Drones tracked at most; drone ids come off the network.
MAX_DRONES = 64
#: Seconds the ground waits for an edge's answer (a model load takes a few seconds).
REPLY_TIMEOUT_S = 20.0
#: A drone silent for at least this long and then heard again counts as reconnected.
RECONNECT_GAP_S = 5.0
#: Shown instead of the controls on a standby ground.
STANDBY_NOTE = (
    "This ground station is the standby. Edges take detection profile changes only from the leader "
    "ground; change them there."
)


@dataclass
class _DroneProfile:
    drone_id: str
    supported: bool = False
    active: str | None = None
    fallback: bool = False
    fallback_reason: str | None = None
    # What the edge says it was last asked for (it differs from active after a fallback).
    edge_requested: str | None = None
    # What this ground last asked for, and the cmd_id of that command.
    requested: str | None = None
    last_cmd: str | None = None
    override: str | None = None
    pending_cmd: str | None = None
    refusal_reason: str | None = None
    refusal_detail: str | None = None
    note: str | None = None
    flying: bool | None = None
    # The edge's own answer to "may the profile change now?"; None until reported.
    switchable: bool | None = None
    detector_ok: bool | None = None
    last_seen: float | None = None


def _spawn_thread(fn: Callable[..., None], *args: Any) -> None:
    threading.Thread(target=fn, args=args, daemon=True, name="detection-profile-send").start()


class ProfileBoard:
    """Per-drone detection profile state and commands; see the module docstring.

    Args:
        transport: The ground transport (``GroundTransportSync``). Uses
            ``send_command(drone_id, msg, timeout_s=..., timeout=...)``, and
            when present ``drone_ids(timeout=...)`` (to report a drone that is
            not connected) and ``identity`` (to know a standby ground).
        emit: ``emit(event, payload)`` to every browser (``socketio.emit``).
        registry: Profiles the operator can choose from.
        fleet_drones: Drone ids of a configured fleet, listed before they report.
        reply_timeout_s: How long one command waits for the edge's answer.
        spawn: Runs ``fn(*args)`` in the background (tests run it inline).
        clock: Monotonic clock for reconnect detection.
        reconnect_gap_s: See ``RECONNECT_GAP_S``.
    """

    def __init__(
        self,
        transport: Any,
        emit: Callable[[str, dict[str, Any]], Any],
        *,
        registry: ProfileRegistry | None = None,
        fleet_drones: tuple[str, ...] | list[str] = (),
        reply_timeout_s: float = REPLY_TIMEOUT_S,
        spawn: Callable[..., None] = _spawn_thread,
        clock: Callable[[], float] = time.monotonic,
        reconnect_gap_s: float = RECONNECT_GAP_S,
    ):
        self._transport = transport
        self._emit_fn = emit
        self._registry = registry if registry is not None else REGISTRY
        self._reply_timeout_s = float(reply_timeout_s)
        self._spawn = spawn
        self._clock = clock
        self._reconnect_gap_s = float(reconnect_gap_s)
        self._lock = threading.Lock()
        self._drones: dict[str, _DroneProfile] = {}
        self.fleet_default: str | None = None
        for drone_id in fleet_drones:
            self._drone(drone_id)

    # -- Browser state -------------------------------------------------------

    def standby(self) -> bool:
        """True when this ground is a standby, whose profile commands the edges would ignore."""
        try:
            identity = getattr(self._transport, "identity", None)
        except Exception:  # noqa: BLE001 - an unreadable role does not hide the controls
            return False
        return getattr(identity, "role", None) == "standby"

    def payload(self) -> dict[str, Any]:
        """The ``detection_profiles`` event: the choices, the fleet default and each drone."""
        standby = self.standby()
        with self._lock:
            drones = {drone_id: self._describe_locked(state) for drone_id, state in sorted(self._drones.items())}
            default = self.fleet_default
        return {
            "profiles": [profile.describe() for profile in self._registry],
            "fleet_default": default,
            "drones": drones,
            "controls_enabled": not standby,
            "controls_note": STANDBY_NOTE if standby else None,
        }

    def pending_drones(self) -> list[str]:
        """Drones with a profile command still waiting for its answer."""
        with self._lock:
            return sorted(d for d, state in self._drones.items() if state.pending_cmd is not None)

    def publish(self) -> None:
        try:
            self._emit_fn(EVENT, self.payload())
        except Exception:  # a browser update must never break the caller
            log.warning("detection_profiles emit failed", exc_info=True)

    def _desired_locked(self, state: _DroneProfile) -> str | None:
        return state.override if state.override is not None else self.fleet_default

    def _mismatch_locked(self, state: _DroneProfile) -> bool:
        desired = self._desired_locked(state)
        if desired is None or not state.supported or state.active == desired:
            return False
        # The edge was asked for the choice and runs its fallback: not a mismatch to fix.
        return not (state.fallback and state.edge_requested == desired)

    def _describe_locked(self, state: _DroneProfile) -> dict[str, Any]:
        return {
            "drone_id": state.drone_id,
            "supported": state.supported,
            "active": state.active,
            "fallback": state.fallback,
            "fallback_reason": state.fallback_reason,
            "requested": state.edge_requested if state.fallback else state.requested,
            "override": state.override,
            "desired": self._desired_locked(state),
            "mismatch": self._mismatch_locked(state),
            "pending": state.pending_cmd is not None,
            "refusal": (
                {"reason": state.refusal_reason, "detail": state.refusal_detail}
                if state.refusal_reason is not None else None
            ),
            "note": state.note,
            "flying": state.flying,
            "switchable": state.switchable,
            "detector_ok": state.detector_ok,
        }

    # -- What the edges report ----------------------------------------------

    def observe_detections(self, msg: Any) -> None:
        """Note the profile an edge's ``DetectionMessage`` names (called for every message)."""
        drone_id = getattr(msg, "drone_id", None)
        profile = getattr(msg, "detection_profile", None)
        if not fp.is_identifier(drone_id):
            return
        with self._lock:
            new = drone_id not in self._drones
            state = self._drone(drone_id)
            if state is None:
                return
            reconnected = self._note_seen_locked(state)
            changed, appeared = self._note_active_locked(state, profile if fp.is_profile_name(profile) else None)
        if changed or new:
            self.publish()
        if appeared or reconnected:
            self._maybe_resend(drone_id, "appeared" if appeared else "reconnected")

    def observe_control(self, msg: Mapping[str, Any]) -> bool:
        """Note an edge's telemetry, or its answer to a profile command.

        Returns True when *msg* was an ``ack``/``nack`` for
        ``set_detection_profile`` (nothing else needs it); telemetry is only
        read and returns False.
        """
        mtype = msg.get("type")
        drone_id = msg.get("drone_id")
        if not fp.is_identifier(drone_id):
            return False
        if mtype == fp.TELEMETRY:
            self._observe_telemetry(drone_id, msg)
            return False
        if mtype not in (fp.ACK, fp.NACK) or msg.get("for") != fp.SET_DETECTION_PROFILE:
            return False
        with self._lock:
            state = self._drone(drone_id)
            if state is None:
                return True
            stale = mtype == fp.NACK and msg.get("cmd_id") != state.last_cmd
            if not stale:
                self._apply_reply_locked(state, msg, mtype)
        if stale:
            # A refusal of an older command (or of another ground's) must not
            # overwrite the answer to the latest one.
            log.info("detection profile: ignoring a refusal of an older command from %s", drone_id)
            return True
        log.info(
            "detection profile %s from %s: %s",
            "ack" if mtype == fp.ACK else "nack", drone_id,
            msg.get("profile") if mtype == fp.ACK else msg.get("reason"),
        )
        self.publish()
        return True

    def _apply_reply_locked(self, state: _DroneProfile, msg: Mapping[str, Any], mtype: str) -> None:
        """Record an edge's answer to a profile command. Caller holds the lock."""
        state.supported = True
        if msg.get("cmd_id") == state.pending_cmd:
            state.pending_cmd = None
        state.note = None
        if fp.is_profile_name(msg.get("requested")):
            state.edge_requested = msg["requested"]
        if mtype == fp.ACK:
            state.active = msg.get("profile") if fp.is_profile_name(msg.get("profile")) else state.active
            state.fallback = msg.get("fallback") is True
            state.fallback_reason = msg.get("fallback_reason") if state.fallback else None
            state.refusal_reason = state.refusal_detail = None
        else:
            if fp.is_profile_name(msg.get("profile")):
                state.active = msg["profile"]
            if msg.get("reason") == "in_flight":
                # Re-arm: the next report that a switch is allowed sends the choice again.
                state.switchable = False
            state.refusal_reason = str(msg.get("reason"))
            detail = msg.get("detail")
            state.refusal_detail = detail if isinstance(detail, str) else None

    def _observe_telemetry(self, drone_id: str, msg: Mapping[str, Any]) -> None:
        profile = msg.get("detection_profile")
        flying = msg.get("is_flying") if isinstance(msg.get("is_flying"), bool) else None
        with self._lock:
            state = self._drone(drone_id)
            if state is None:
                return
            reconnected = self._note_seen_locked(state)
            switchable = msg.get("detection_profile_switchable")
            if isinstance(switchable, bool):
                allowed_again = state.switchable is False and switchable
                changed = state.switchable != switchable
                state.switchable = switchable
            else:
                # An edge that does not report it: fall back to its landing.
                allowed_again = state.flying is True and flying is False
                changed = False
            changed = changed or state.flying != flying
            state.flying = flying
            detector_ok = msg.get("detector_ok")
            if isinstance(detector_ok, bool) and detector_ok != state.detector_ok:
                state.detector_ok, changed = detector_ok, True
            appeared = False
            if fp.is_profile_name(profile):
                fallback = msg.get("detection_profile_fallback") is True
                reason = msg.get("detection_profile_fallback_reason")
                reason = reason if fallback and isinstance(reason, str) and fp.is_profile_name(reason) else (
                    "weights_missing" if fallback else None)
                if state.fallback != fallback or state.fallback_reason != reason:
                    state.fallback, state.fallback_reason, changed = fallback, reason, True
                requested = msg.get("detection_profile_requested")
                state.edge_requested = requested if fp.is_profile_name(requested) else profile
                active_changed, appeared = self._note_active_locked(state, profile)
                changed = changed or active_changed
        if changed:
            self.publish()
        if appeared or reconnected or allowed_again:
            self._maybe_resend(drone_id, "appeared" if appeared else "reconnected" if reconnected else "switchable")

    def _note_seen_locked(self, state: _DroneProfile) -> bool:
        """Record that the drone was heard; True when it was silent long enough to count as reconnected."""
        now = self._clock()
        reconnected = state.last_seen is not None and now - state.last_seen >= self._reconnect_gap_s
        state.last_seen = now
        return reconnected

    def _note_active_locked(self, state: _DroneProfile, profile: str | None) -> tuple[bool, bool]:
        """Record a reported profile; returns (changed, first report of support)."""
        if profile is None:
            return False, False
        first = not state.supported
        changed = first or state.active != profile
        state.supported = True
        state.active = profile
        return changed, first

    def _maybe_resend(self, drone_id: str, reason: str) -> None:
        """Send the operator's choice once more when the drone runs another profile."""
        if self.standby():
            return
        with self._lock:
            state = self._drones.get(drone_id)
            if state is None or state.pending_cmd is not None or not self._mismatch_locked(state):
                return
            target = self._desired_locked(state)
        if target is not None:
            self._send(drone_id, target, reason=reason)

    # -- Operator actions ----------------------------------------------------

    def select(self, drone_id: Any, profile: Any) -> str | None:
        """Choose *profile* for one drone (None or ``""`` follows the fleet default).

        Returns an error message for the operator, or None when the command
        was sent (or nothing needed sending).
        """
        if not fp.is_identifier(drone_id):
            return "Unknown drone."
        follow_default = profile in (None, "")
        if not follow_default and profile not in self._registry:
            return f"Unknown detection profile {profile!r}."
        if self.standby():
            return STANDBY_NOTE
        with self._lock:
            state = self._drones.get(drone_id)
            if state is None:
                return f"Drone {drone_id} has not reported yet."
            if not state.supported:
                return (
                    f"Drone {drone_id} runs an edge that does not report detection profiles; "
                    f"update it to switch profiles."
                )
            state.override = None if follow_default else profile
            target = self.fleet_default if follow_default else profile
        if target is None:
            self.publish()
            return None
        self._send(drone_id, target, reason="operator")
        return None

    def set_fleet_default(self, profile: Any) -> tuple[str | None, list[str]]:
        """Set the fleet default (None or ``""`` clears it); returns (error, drones sent to)."""
        if profile not in (None, "") and profile not in self._registry:
            return f"Unknown detection profile {profile!r}.", []
        if self.standby():
            return STANDBY_NOTE, []
        if profile in (None, ""):
            with self._lock:
                self.fleet_default = None
            self.publish()
            return None, []
        with self._lock:
            self.fleet_default = profile
            targets = [
                drone_id for drone_id, state in sorted(self._drones.items())
                if state.supported and state.override is None and state.active != profile
            ]
        for drone_id in targets:
            self._send(drone_id, profile, reason="fleet_default")
        if not targets:
            self.publish()
        return None, targets

    # -- Sending -------------------------------------------------------------

    def _send(self, drone_id: str, profile: str, *, reason: str) -> None:
        msg = fp.make_set_detection_profile(profile, drone_id=drone_id)
        with self._lock:
            state = self._drone(drone_id)
            if state is None:
                return
            state.pending_cmd = state.last_cmd = msg["cmd_id"]
            state.requested = profile
            state.note = None
            state.refusal_reason = state.refusal_detail = None
        log.info("detection profile %s requested for %s (%s)", profile, drone_id, reason)
        self.publish()
        self._spawn(self._send_and_wait, drone_id, msg)

    def _connected(self, drone_id: str) -> bool:
        """False when the transport says the drone is not connected; True when unknown."""
        drone_ids = getattr(self._transport, "drone_ids", None)
        if not callable(drone_ids):
            return True
        try:
            return drone_id in drone_ids(timeout=self._reply_timeout_s)
        except Exception:  # noqa: BLE001 - an unknown link state does not stop the attempt
            return True

    def _send_and_wait(self, drone_id: str, msg: dict[str, Any]) -> None:
        note = None
        reply = None
        if not self._connected(drone_id):
            note = "not_connected"
        else:
            try:
                reply = self._transport.send_command(
                    drone_id, msg, timeout_s=self._reply_timeout_s, timeout=self._reply_timeout_s + 5.0,
                )
            except Exception as exc:  # noqa: BLE001 - reported to the operator, never raised
                log.warning("detection profile command to %s failed: %r", drone_id, exc)
                note = "send_failed"
        if reply is not None:
            self.observe_control(reply)
            return
        with self._lock:
            state = self._drones.get(drone_id)
            if state is None or state.pending_cmd != msg["cmd_id"]:
                return
            state.pending_cmd = None
            state.note = note or "no_reply"
        log.warning("detection profile command to %s: %s", drone_id, note or "no reply")
        self.publish()

    def _drone(self, drone_id: str) -> _DroneProfile | None:
        """The state for *drone_id*, created when there is room. Caller holds the lock (or is __init__)."""
        state = self._drones.get(drone_id)
        if state is None and fp.is_identifier(drone_id) and len(self._drones) < MAX_DRONES:
            state = self._drones[drone_id] = _DroneProfile(drone_id)
        return state
