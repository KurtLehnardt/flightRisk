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
  drone that follows the default and runs another profile, and to drones
  that first report support afterwards. With no fleet default and no
  per-drone choice nothing is sent, so every edge keeps its startup profile.
* **Replies.** The edge's ``ack`` (active profile, fallback) or ``nack``
  (reason and an operator-facing ``detail``, for example "only on the
  ground") is shown per drone. No reply within ``reply_timeout_s`` is shown
  as such.

The edge enforces the safety rule (no switch in flight); the ground never
second-guesses it, so the operator always sees the edge's own reason.
Commands go out on a worker thread (``spawn``), never on a transport
delivery thread or a Socket.IO handler.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from flightrisk import fleet_protocol as fp
from flightrisk.vision.profiles import REGISTRY, ProfileRegistry

__all__ = ["EVENT", "MAX_DRONES", "REPLY_TIMEOUT_S", "ProfileBoard"]

log = logging.getLogger(__name__)

#: Socket.IO event carrying ``ProfileBoard.payload()`` to the browser.
EVENT = "detection_profiles"
#: Drones tracked at most; drone ids come off the network.
MAX_DRONES = 64
#: Seconds the ground waits for an edge's answer (a model load takes a few seconds).
REPLY_TIMEOUT_S = 20.0


@dataclass
class _DroneProfile:
    drone_id: str
    supported: bool = False
    active: str | None = None
    fallback: bool = False
    fallback_reason: str | None = None
    requested: str | None = None
    override: str | None = None
    pending_cmd: str | None = None
    refusal_reason: str | None = None
    refusal_detail: str | None = None
    note: str | None = None
    flying: bool | None = None
    auto_applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "drone_id": self.drone_id,
            "supported": self.supported,
            "active": self.active,
            "fallback": self.fallback,
            "fallback_reason": self.fallback_reason,
            "requested": self.requested,
            "override": self.override,
            "pending": self.pending_cmd is not None,
            "refusal": (
                {"reason": self.refusal_reason, "detail": self.refusal_detail}
                if self.refusal_reason is not None else None
            ),
            "note": self.note,
            "flying": self.flying,
        }


def _spawn_thread(fn: Callable[..., None], *args: Any) -> None:
    threading.Thread(target=fn, args=args, daemon=True, name="detection-profile-send").start()


class ProfileBoard:
    """Per-drone detection profile state and commands; see the module docstring.

    Args:
        transport: The ground transport (``GroundTransportSync``); only
            ``send_command(drone_id, msg, timeout_s=..., timeout=...)`` is used.
        emit: ``emit(event, payload)`` to every browser (``socketio.emit``).
        registry: Profiles the operator can choose from.
        fleet_drones: Drone ids of a configured fleet, listed before they report.
        reply_timeout_s: How long one command waits for the edge's answer.
        spawn: Runs ``fn(*args)`` in the background (tests run it inline).
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
    ):
        self._transport = transport
        self._emit_fn = emit
        self._registry = registry if registry is not None else REGISTRY
        self._reply_timeout_s = float(reply_timeout_s)
        self._spawn = spawn
        self._lock = threading.Lock()
        self._drones: dict[str, _DroneProfile] = {}
        self.fleet_default: str | None = None
        for drone_id in fleet_drones:
            self._drone(drone_id)

    # -- Browser state -------------------------------------------------------

    def payload(self) -> dict[str, Any]:
        """The ``detection_profiles`` event: the choices, the fleet default and each drone."""
        with self._lock:
            drones = {drone_id: state.to_dict() for drone_id, state in sorted(self._drones.items())}
            default = self.fleet_default
        return {
            "profiles": [profile.describe() for profile in self._registry],
            "fleet_default": default,
            "drones": drones,
        }

    def publish(self) -> None:
        try:
            self._emit_fn(EVENT, self.payload())
        except Exception:  # a browser update must never break the caller
            log.warning("detection_profiles emit failed", exc_info=True)

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
            changed, auto = self._note_active_locked(state, profile if fp.is_profile_name(profile) else None)
        if changed or new:
            self.publish()
        if auto is not None:
            self._send(drone_id, auto, reason="fleet_default")

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
            state.supported = True
            if msg.get("cmd_id") == state.pending_cmd:
                state.pending_cmd = None
            state.note = None
            if mtype == fp.ACK:
                state.active = msg.get("profile") if fp.is_profile_name(msg.get("profile")) else state.active
                state.fallback = msg.get("fallback") is True
                state.fallback_reason = msg.get("fallback_reason") if state.fallback else None
                state.refusal_reason = state.refusal_detail = None
            else:
                if fp.is_profile_name(msg.get("profile")):
                    state.active = msg["profile"]
                state.refusal_reason = str(msg.get("reason"))
                detail = msg.get("detail")
                state.refusal_detail = detail if isinstance(detail, str) else None
        log.info(
            "detection profile %s from %s: %s",
            "ack" if mtype == fp.ACK else "nack", drone_id,
            msg.get("profile") if mtype == fp.ACK else msg.get("reason"),
        )
        self.publish()
        return True

    def _observe_telemetry(self, drone_id: str, msg: Mapping[str, Any]) -> None:
        profile = msg.get("detection_profile")
        flying = msg.get("is_flying") if isinstance(msg.get("is_flying"), bool) else None
        with self._lock:
            state = self._drone(drone_id)
            if state is None:
                return
            changed = state.flying != flying
            state.flying = flying
            auto = None
            if fp.is_profile_name(profile):
                fallback = msg.get("detection_profile_fallback") is True
                if state.fallback != fallback:
                    state.fallback, changed = fallback, True
                    state.fallback_reason = "weights_missing" if fallback else None
                active_changed, auto = self._note_active_locked(state, profile)
                changed = changed or active_changed
        if changed:
            self.publish()
        if auto is not None:
            self._send(drone_id, auto, reason="fleet_default")

    def _note_active_locked(self, state: _DroneProfile, profile: str | None) -> tuple[bool, str | None]:
        """Record a reported profile; returns (changed, profile to auto-apply or None)."""
        if profile is None:
            return False, None
        changed = not state.supported or state.active != profile
        first = not state.supported
        state.supported = True
        state.active = profile
        auto = None
        if (
            first
            and not state.auto_applied
            and self.fleet_default is not None
            and state.override is None
            and state.pending_cmd is None
            and profile != self.fleet_default
        ):
            state.auto_applied = True
            auto = self.fleet_default
        return changed, auto

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
        if profile in (None, ""):
            with self._lock:
                self.fleet_default = None
            self.publish()
            return None, []
        if profile not in self._registry:
            return f"Unknown detection profile {profile!r}.", []
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
            state.pending_cmd = msg["cmd_id"]
            state.requested = profile
            state.note = None
            state.refusal_reason = state.refusal_detail = None
        log.info("detection profile %s requested for %s (%s)", profile, drone_id, reason)
        self.publish()
        self._spawn(self._send_and_wait, drone_id, msg)

    def _send_and_wait(self, drone_id: str, msg: dict[str, Any]) -> None:
        note = None
        reply = None
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
        log.warning("detection profile command to %s got no reply", drone_id)
        self.publish()

    def _drone(self, drone_id: str) -> _DroneProfile | None:
        """The state for *drone_id*, created when there is room. Caller holds the lock (or is __init__)."""
        state = self._drones.get(drone_id)
        if state is None and fp.is_identifier(drone_id) and len(self._drones) < MAX_DRONES:
            state = self._drones[drone_id] = _DroneProfile(drone_id)
        return state
