"""Edge-side lane executor: flies assigned search lanes on one Tello.

The ground coordinator plans a fleet search and sends each edge its lanes as
relative moves (design section 3, "Binding contracts"). ``LaneExecutor``
accepts those control messages, flies the lanes through a drone controller
(``TelloController`` or ``FakeTelloController``) and reports back:

* ``assign_lanes`` stores a lane plan after checking it against the
  ``SafetyEnvelope`` and answers with an ``ack`` carrying the battery level.
  A plan it refuses is answered with a ``nack`` giving the reason, so the
  ground never counts refused lanes as searched. A plan that arrives while
  the drone is flying replaces the current one (re-tasking), and the drone
  switches to it after its current move. Plans are ordered and invalidated
  as described in ``flightrisk.drone.lane_plan``.
* ``clock`` stores the ground clock offset (edge clock minus ground clock).
* ``start_at`` schedules takeoff for the moment the edge's monotonic clock
  reaches ``t_ground_s + offset_s``, then the drone climbs to its altitude
  slot and flies its lanes. An ``ack`` reports when the start executed, in
  ground time (``t_edge_exec - offset_s``). It is refused unless the current
  plan is valid and, when the message names a ``plan_id``, is that plan.
* ``takeoff`` takes off and climbs to the altitude slot without flying lanes.
* ``land`` and ``emergency_land_all`` land at once from the calling thread.
  They never wait behind a queued or running flight sequence, and every
  landing clears a hold and invalidates the plan.
* ``ping`` is answered with ``pong`` timestamps for clock sync, and
  ``heartbeat`` (like every ground message) resets the link-loss watchdog.
* Protocol v2 adds ``hold``, ``resume`` and ``nudge`` (``V2_CONTROL_TYPES``),
  ``cmd_id`` idempotency, generic acks and nacks, and the authority hook;
  see ``flightrisk.drone.lane_commands``. Their routing from the edge's
  control loop belongs to the edge authority: ``CONTROL_TYPES`` stays the
  v1 set, and ``ALL_CONTROL_TYPES`` is everything ``handle_message`` takes.

The monitor (``flightrisk.drone.lane_monitor``) runs every 50 ms: link-loss
and battery landings, land retries, the end-of-lanes hover timeout and
telemetry.

Fail-safe rules: any unexpected exception while flying a sequence or running
the monitor lands the drone. Takeoff is refused while the drone controller
reports that it cannot reach the drone (``link_status()``: no Tello state
for ``_STATE_LOST_S``, or a failed keepalive or send), both when ``start_at``
arrives and right before the takeoff is sent; landing never looks at the
drone link. Takeoff is refused below the battery threshold,
and any move the geofence refuses ends the flight with a landing. Position is
dead-reckoned from the commanded moves (the frame is described in
``flightrisk.drone.safety``). Every move is claimed under the executor lock
just before it is sent, so a hold or a landing stops the sequence before its
next move and never interrupts the move in flight.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from flightrisk.drone.command_cache import CommandCache
from flightrisk.drone.controller import DroneLinkStatus, command_result
from flightrisk.drone.lane_commands import (
    COMMAND_TYPES,
    V2_CONTROL_TYPES,
    CommandAuthority,
    FleetCommandMixin,
    PermissiveAuthority,
)
from flightrisk.drone.lane_flight import FlightSequenceMixin, MoveNotExecutedError
from flightrisk.drone.lane_monitor import MonitorMixin
from flightrisk.drone.lane_plan import (
    LanePlanMixin,
    PlanIdentity,
    parse_lanes,
    split_lane_moves,
)
from flightrisk.drone.safety import SafetyEnvelope, is_finite_number
from flightrisk.fleet_protocol import MAX_TEXT_LEN, is_identifier
from flightrisk.observability import StructuredLogger

__all__ = [
    "ALL_CONTROL_TYPES",
    "CONTROL_TYPES",
    "V2_CONTROL_TYPES",
    "CommandAuthority",
    "LaneExecutor",
    "MoveNotExecutedError",
    "PermissiveAuthority",
    "parse_lanes",
    "split_lane_moves",
]

# Ground->edge message types the executor handles in protocol v1 (design
# section 3). The edge device routes exactly these to the executor.
CONTROL_TYPES = frozenset({
    "takeoff",
    "land",
    "emergency_land_all",
    "assign_lanes",
    "start_at",
    "ping",
    "clock",
    "heartbeat",
})
# Everything ``handle_message`` takes, protocol v2 commands included.
ALL_CONTROL_TYPES = CONTROL_TYPES | V2_CONTROL_TYPES

TELEMETRY_INTERVAL_S = 0.5
# Telemetry waiting for the sender thread. Only the newest matters, so when
# the uplink stalls the oldest queued message is dropped.
TELEMETRY_QUEUE_SIZE = 4
# How long shutdown() waits for the telemetry sender, which may be stuck on
# a stalled uplink. It is a daemon thread.
_TELEMETRY_JOIN_S = 0.5
MONITOR_INTERVAL_S = 0.05
# A landing that has not taken effect is retried at this interval.
LAND_RETRY_S = 2.0
# A start time further ahead than this, or further in the past, is treated as
# a clock error and refused rather than flown.
MAX_START_LEAD_S = 120.0
MAX_START_LATE_S = 5.0

# Why a start_at was refused (logged) -> the v2 nack reason.
_START_NACK_REASONS = {
    "invalid_t_ground_s": "invalid",
    "invalid_plan_id": "invalid",
    "no_clock_offset": "invalid",
    "too_far_ahead": "invalid",
    "too_late": "invalid",
    "no_valid_plan": "no_valid_plan",
    "no_lanes": "no_valid_plan",
    "plan_mismatch": "plan_mismatch",
    "busy": "busy",
    "holding": "busy",
    "shut_down": "busy",
    "landing": "landing",
    "geofence": "geofence",
    "drone_unreachable": "drone_unreachable",
}


class LaneExecutor(FleetCommandMixin, LanePlanMixin, FlightSequenceMixin, MonitorMixin):
    """Executes ground control messages against one drone inside a safety envelope."""

    CONTROL_TYPES = CONTROL_TYPES
    ALL_CONTROL_TYPES = ALL_CONTROL_TYPES

    def __init__(
        self,
        drone: Any,
        envelope: SafetyEnvelope,
        drone_id: str,
        send: Callable[[dict[str, Any]], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        logger: StructuredLogger | None = None,
        telemetry_interval_s: float = TELEMETRY_INTERVAL_S,
        monitor_interval_s: float = MONITOR_INTERVAL_S,
        land_retry_s: float = LAND_RETRY_S,
        max_start_lead_s: float = MAX_START_LEAD_S,
        max_start_late_s: float = MAX_START_LATE_S,
        telemetry_queue_size: int = TELEMETRY_QUEUE_SIZE,
        authority: CommandAuthority | None = None,
        fleet_v2: bool = False,
        command_cache_size: int | None = None,
    ):
        self.drone = drone
        self.envelope = envelope
        self.drone_id = str(drone_id)
        self._send_fn = send
        self._clock = clock
        self._sleep = sleep
        self._log = logger if logger is not None else StructuredLogger(component="lane-executor")
        self.telemetry_interval_s = float(telemetry_interval_s)
        self.monitor_interval_s = float(monitor_interval_s)
        self.land_retry_s = float(land_retry_s)
        self.max_start_lead_s = float(max_start_lead_s)
        self.max_start_late_s = float(max_start_late_s)
        # Decides whether a fenced command comes from the leader the edge
        # obeys (see ``lane_commands``). The default accepts everything.
        self._authority = authority if authority is not None else PermissiveAuthority()
        self._commands = CommandCache() if command_cache_size is None else CommandCache(command_cache_size)
        # Telemetry carries the v2 fields once true (see ``lane_monitor``).
        self._v2_telemetry = bool(fleet_v2)

        self._lock = threading.RLock()
        # Set by every landing (and by a hold that cancels a pending start);
        # tells a running flight sequence to stop sending commands. Cleared
        # only when a new sequence is launched.
        self._abort = threading.Event()
        # (nack reason, detail) for a sequence aborted before it executed,
        # when that was not a landing.
        self._cancel: tuple[str, str | None] | None = None
        self._stop = threading.Event()
        # Set for good by shutdown(); no flight sequence may start afterwards.
        self._closed = False
        self._worker: threading.Thread | None = None
        # The thread running the current flight sequence, or None. A thread
        # clears it (under ``_lock``) once it will send no more commands, so
        # a re-task arriving as the last lane finishes still gets flown.
        self._active_thread: threading.Thread | None = None
        # Kind and phase of that sequence: kind is start_at, takeoff, lanes,
        # climb or nudge; phase is pre_takeoff, taking_off, climbing, lanes or
        # nudge.
        self._active_kind: str | None = None
        self._phase: str | None = None
        # True while a claimed move (or takeoff) is with the drone.
        self._moving = False
        self._monitor: threading.Thread | None = None

        # Lane plan as received, and flattened into Tello-sized moves of
        # (lane_index, chunk_index, direction, cm). ``_step`` indexes the next
        # move to fly. ``_linked_gens`` holds earlier plan generations whose
        # progress carried into the current plan.
        self._lanes: list[tuple[str, int]] = []
        self._moves: list[tuple[int, int, str, int]] = []
        self._step = 0
        self._lanes_total = 0
        self._plan_gen = 0
        self._linked_gens: set[int] = set()
        self._zone: Any = None
        # Identity, validity, content and end-of-lanes action of the current
        # plan, and the newest (epoch, plan_seq) ever accepted (lane_plan).
        self._plan_identity: PlanIdentity | None = None
        self._plan_newest: tuple[int, int] | None = None
        self._plan_valid = False
        self._plan_content: Any = None
        self._on_complete: str | None = None
        self._hover_timeout_s = 0.0
        self._hover_since: float | None = None
        # Hold state: whether a hold is in force, and what resume restarts
        # (lanes, climb or hover).
        self._held = False
        self._parked: str | None = None

        self._offset_s: float | None = None
        self._takeoff_at_local_s: float | None = None
        # True once this flight has started flying a lane plan. Only then
        # does a new assign_lanes re-task the drone in the air; a drone that
        # was merely taken off waits for start_at.
        self._flying_lanes = False
        # Reason for the landing currently wanted, or None. While it is set
        # and the drone still reports flying, the monitor retries the land
        # and new takeoffs are refused.
        self._land_reason: str | None = None
        # Landings announced by land_now that have not yet been recorded
        # under ``_lock``. land_now counts itself here before it waits for
        # the lock, so no flight sequence can be launched in between (see
        # ``_landing_wanted_locked``). Guarded by ``_land_gate``, a lock that
        # is never held while taking ``_lock`` (land_now releases ``_lock``
        # before it decrements). It is re-entrant: the SIGINT
        # handler calls ``land_now`` on the main thread, which may be inside
        # ``land_now`` itself (holding the gate) when a second Ctrl+C lands.
        self._land_gate = threading.RLock()
        self._land_requests = 0
        self._last_land_attempt_s = 0.0
        # True while some thread is inside drone.land(). A second land
        # request meanwhile (for example emergency_land_all arriving through
        # both the transport callback and recv()) does not send another one.
        self._land_inflight = False
        # Test-only: called inside ``_launch`` (under ``_lock``) right after
        # the landing check returned False. Always None in production.
        self._after_launch_check: Callable[[], None] | None = None
        # Flight state the monitor saw last, to notice an unrequested landing.
        self._was_flying = False
        self._next_telemetry_s: float | None = None
        # Telemetry handed from the monitor to the sender thread. The deque
        # drops its oldest entry when full.
        self._telemetry_queue: deque[dict[str, Any]] = deque(maxlen=max(1, int(telemetry_queue_size)))
        self._telemetry_cv = threading.Condition()
        self._telemetry_sender: threading.Thread | None = None
        self._telemetry_dropped = 0
        # The drone-link problem (reason, detail) the monitor last logged,
        # or None while the link is healthy (or not reported), so each
        # change is logged once (see ``lane_monitor``).
        self._link_problem: tuple[str | None, str | None] | None = None
        # Operator text of the last takeoff refused because the drone was
        # unreachable; written and read on the flight sequence thread.
        self._unreachable_detail: str | None = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def now(self) -> float:
        """The edge clock used for scheduling (monotonic seconds)."""
        return self._clock()

    @property
    def offset_s(self) -> float | None:
        with self._lock:
            return self._offset_s

    @property
    def lanes_total(self) -> int:
        with self._lock:
            return self._lanes_total

    @property
    def lane_index(self) -> int:
        """Index of the lane being flown, or ``lanes_total`` once all are done."""
        with self._lock:
            if self._step < len(self._moves):
                return self._moves[self._step][0]
            return self._lanes_total

    @property
    def is_busy(self) -> bool:
        """True while a flight sequence (scheduled start, takeoff, lanes or nudge) is running."""
        with self._lock:
            return self._worker_alive()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait for the running flight sequence to finish. Returns True if idle."""
        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    def is_active(self) -> bool:
        """True while the drone flies or anything that can make it fly or land is under way.

        That is: the drone reports flying, a flight sequence (a scheduled
        ``start_at`` still waiting, a takeoff, lanes, a climb or a nudge) is
        running, or a landing is in progress or has been requested. The edge
        authority adopts a new leader epoch only while this is False. An
        unreadable flight state counts as active.
        """
        try:
            flying = self._is_flying()
        except Exception:  # noqa: BLE001 - an unreadable state counts as flying
            return True
        with self._lock:
            worker = self._worker
            busy = self._worker_alive() or self._land_inflight or self._land_requests > 0
        return flying or busy or (worker is not None and worker.is_alive())

    def fleet_status(self) -> dict[str, Any]:
        """The protocol v2 status fields for telemetry and the edge ``hello``.

        Returns ``plan_id`` (kept after the plan is invalidated, or None),
        ``state`` (as in telemetry, or ``unknown`` while the flight state is
        unreadable), ``plan_valid`` and ``epoch`` (the epoch the authority
        reports the edge obeys, 0 before any), read together under the
        executor lock. The authority's ``epoch_seen()`` must be non-blocking.
        """
        try:
            flying: bool | None = self._is_flying()
        except Exception:  # noqa: BLE001 - an unreadable state is reported, not raised
            flying = None
        # Read before taking the executor lock, so the safety lock is never
        # coupled to the authority's own lock.
        epoch = self.epoch_seen
        with self._lock:
            return {
                "plan_id": None if self._plan_identity is None else self._plan_identity.plan_id,
                "state": "unknown" if flying is None else self._state_locked(flying),
                "plan_valid": self._plan_valid,
                "epoch": epoch,
            }

    def _worker_alive(self) -> bool:
        """True while a flight sequence may still send commands. Caller holds ``_lock``."""
        return self._active_thread is not None

    def _landing_wanted_locked(self) -> bool:
        """True from the moment ``land_now`` is called until that landing is over. Caller holds ``_lock``.

        While it is true no flight sequence is launched (``_launch``) and
        ``start_at``, ``resume``, ``nudge`` and ``hold`` are refused with
        ``landing``.
        """
        if self._land_requests > 0:
            return True
        return self._land_reason is not None and (self._land_inflight or self._is_flying())

    def _is_flying(self) -> bool:
        return bool(getattr(self.drone.state, "is_flying", False))

    def _drone_link(self) -> DroneLinkStatus | None:
        """The drone controller's link status, or None when it reports none. Never raises.

        Only a controller with ``link_status()`` (the Tello) reports one;
        the fake Tello and MAVLink do not, and are never refused for it. A
        ``link_status()`` that raises counts as an unhealthy link
        (``unreadable``), so a takeoff is refused rather than flown blind.
        Takeoff only: no landing ever looks at this.
        """
        probe = getattr(self.drone, "link_status", None)
        if not callable(probe):
            return None
        try:
            status = probe()
        except Exception as exc:  # noqa: BLE001 - an unreadable link refuses takeoff
            # Not logged here (this runs with every telemetry); the monitor
            # logs the change and a refused takeoff logs the detail.
            return DroneLinkStatus(False, None, "unreadable", f"drone link status unreadable ({type(exc).__name__})")
        if not isinstance(status, DroneLinkStatus):
            return DroneLinkStatus(False, None, "unreadable", "drone link status unreadable")
        if status.ok:
            return status
        # The detail travels in nacks and telemetry: printable, at most
        # MAX_TEXT_LEN characters, never empty.
        detail = "".join(ch for ch in str(status.detail or "") if ch.isprintable())[:MAX_TEXT_LEN]
        return DroneLinkStatus(False, status.age_s, status.reason, detail or "drone unreachable")

    def _read_battery(self) -> Any:
        reader = getattr(self.drone, "read_battery", None)
        if callable(reader):
            return reader()
        return getattr(self.drone.state, "battery", None)

    def _read_height(self) -> Any:
        reader = getattr(self.drone, "read_height_cm", None)
        if callable(reader):
            return reader()
        return getattr(self.drone.state, "height", None)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the monitor thread (watchdog, battery gate, telemetry) and the telemetry sender."""
        with self._lock:
            if self._monitor is not None and self._monitor.is_alive():
                return
            self._stop.clear()
            with self._telemetry_cv:
                if self._telemetry_sender is None or not self._telemetry_sender.is_alive():
                    self._telemetry_sender = threading.Thread(
                        target=self._telemetry_loop, daemon=True, name=f"{self.drone_id}-telemetry"
                    )
                    self._telemetry_sender.start()
            self._monitor = threading.Thread(
                target=self._monitor_loop, daemon=True, name=f"{self.drone_id}-monitor"
            )
            self._monitor.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop all threads, landing first if the drone is flying.

        After shutdown the executor refuses every takeoff, start and re-task.
        """
        with self._lock:
            self._closed = True
            self._cancel = ("busy", "shut_down")
        self._stop.set()
        self._abort.set()
        with self._telemetry_cv:
            self._telemetry_cv.notify_all()
        try:
            if self._is_flying():
                self.land_now("shutdown")
        except Exception as exc:
            self._log.error("edge_shutdown_land_error", drone_id=self.drone_id, error=repr(exc))
        with self._lock:
            worker, monitor = self._worker, self._monitor
        for thread in (worker, monitor):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout)
        sender = self._telemetry_sender
        if sender is not None and sender is not threading.current_thread():
            sender.join(min(timeout, _TELEMETRY_JOIN_S))

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.monitor_interval_s)

    # ------------------------------------------------------------------
    # Control-message entry points
    # ------------------------------------------------------------------

    def note_ground_message(self) -> None:
        """Reset the link-loss watchdog. Call for every message from the ground."""
        self.envelope.note_ground_message()

    def handle_message(self, msg: dict[str, Any], recv_t: float | None = None) -> None:
        """Dispatch one ground control message of a type in ``ALL_CONTROL_TYPES``.

        ``recv_t`` is the edge clock reading taken when the message arrived;
        it becomes the ``t2`` of a ``pong``. Exceptions propagate to the
        caller, which must land (see ``edge_device._handle_control``).
        Landing is executed every time, never fenced and never cached.
        """
        mtype = msg.get("type")
        if mtype == "emergency_land_all":
            self.land_now("emergency_land_all")
            self._ack_landing(msg)
        elif mtype == "land":
            self.land_now("ground_land")
            self._ack_landing(msg)
        elif mtype == "ping":
            self.handle_ping(msg, recv_t=recv_t)
        elif mtype == "heartbeat":
            self._log.debug("edge_heartbeat", drone_id=self.drone_id, seq=msg.get("seq"))
        elif mtype == "clock":
            self.handle_clock(msg)
        elif isinstance(mtype, str) and mtype in COMMAND_TYPES:
            self._dispatch_command(msg)
        else:
            self._log.debug("edge_flight_control_unknown", drone_id=self.drone_id, type=mtype)

    def handle_ping(self, msg: dict[str, Any], recv_t: float | None = None) -> None:
        """Answer a clock-sync ping with ``pong{t1, t2, t3}`` in edge clock seconds."""
        t2 = recv_t if recv_t is not None else self._clock()
        pong = {"type": "pong", "t1": msg.get("t1"), "t2": t2, "t3": self._clock()}
        self._emit(pong)

    def handle_clock(self, msg: dict[str, Any]) -> bool:
        """Store the ground clock offset (edge clock minus ground clock)."""
        offset = msg.get("offset_s")
        if not is_finite_number(offset):
            self._log.warning("edge_clock_invalid", drone_id=self.drone_id, offset_s=offset)
            return False
        with self._lock:
            self._offset_s = float(offset)
        self._log.info("edge_clock_offset", drone_id=self.drone_id, offset_s=float(offset))
        return True

    def handle_start_at(self, msg: dict[str, Any]) -> bool:
        """Schedule takeoff plus lanes at ``t_ground_s + offset_s`` on the edge clock.

        Refused unless the current plan is valid (``no_valid_plan``) and,
        when the message names a ``plan_id`` (a v2 command must), is that
        plan (``plan_mismatch``); also while holding (``busy``), and, for a
        v2 command, when the rest of the plan, flown from the current
        dead-reckoned position, would leave the geofence (``geofence``).
        A start that would take off is refused with ``drone_unreachable``
        (``detail`` naming the problem, for example ``Tello unreachable (no
        state for 12 s)``) while the drone controller reports its link down;
        the link is checked again right before the takeoff.
        """
        t_ground = msg.get("t_ground_s")
        if not is_finite_number(t_ground):
            return self._refuse_start(msg, "invalid_t_ground_s", t_ground_s=t_ground)
        plan_id = msg.get("plan_id")
        if (plan_id is not None or self._is_v2(msg)) and not is_identifier(plan_id):
            return self._refuse_start(msg, "invalid_plan_id")
        refusal: str | None = None
        lead = 0.0
        # Read before the executor lock: the controller takes its own locks.
        link = self._drone_link()
        with self._lock:
            offset = self._offset_s
            if offset is None:
                refusal = "no_clock_offset"
            elif not self._plan_valid:
                refusal = "no_valid_plan"
            elif plan_id is not None and plan_id != self.plan_id:
                refusal = "plan_mismatch"
            elif self._step >= len(self._moves):
                refusal = "no_lanes"
            elif self._is_v2(msg) and self._path_outside(self._moves, self._step) is not None:
                # The drone moved since the plan was checked (a nudge). A v1
                # start keeps the v1 behaviour: the in-flight geofence check
                # lands the drone before the move that would leave the fence.
                refusal = "geofence"
            elif self._worker_alive():
                refusal = "busy"
            elif self._held:
                refusal = "holding"
            elif self._landing_wanted_locked():
                refusal = "landing"
            elif link is not None and not link.ok and not self._is_flying():
                # The start would take off into a dead drone link. Checked
                # again right before the takeoff (``_do_takeoff``).
                refusal = "drone_unreachable"
            else:
                t_edge = float(t_ground) + offset
                lead = t_edge - self._clock()
                if lead > self.max_start_lead_s:
                    refusal = "too_far_ahead"
                elif lead < -self.max_start_late_s:
                    refusal = "too_late"
                elif not self._launch("start_at", t_edge=t_edge, offset_s=offset, cmd=msg):
                    refusal = "shut_down" if self._closed else "landing"
        if refusal is not None:
            # The nack is sent outside the lock, like every other message.
            fields: dict[str, Any] = {"lead_s": lead} if refusal in ("too_far_ahead", "too_late") else {}
            if refusal == "drone_unreachable" and link is not None:
                fields.update(link_reason=link.reason, drone_link_age_s=link.age_s)
                return self._refuse_start(msg, refusal, detail=link.detail or "drone unreachable", **fields)
            return self._refuse_start(msg, refusal, **fields)
        self._log.info(
            "edge_start_scheduled",
            drone_id=self.drone_id,
            t_ground_s=float(t_ground),
            offset_s=offset,
            t_edge_s=t_edge,
            lead_s=round(lead, 4),
            plan_id=self.plan_id,
        )
        return True

    def _refuse_start(self, msg: dict[str, Any], reason: str, detail: str | None = None, **fields: Any) -> bool:
        """Log and nack a refused start. *detail* (operator text) overrides the nack's default detail."""
        if detail is not None:
            fields["detail"] = detail
        self._log.warning("edge_start_refused", drone_id=self.drone_id, reason=reason, **fields)
        nack = _START_NACK_REASONS.get(reason, "invalid")
        if detail is None and reason != nack:
            detail = reason
        self._nack(msg, nack, **({"detail": detail} if detail is not None else {}))
        return False

    def handle_takeoff(self, msg: dict[str, Any] | None = None) -> bool:
        """Take off and climb to the altitude slot, then hover."""
        with self._lock:
            if self._is_flying():
                # A drone that is still landing is told so, as for every other
                # command; only a drone flying for good is "already_flying".
                refusal = "landing" if self._landing_wanted_locked() else "already_flying"
            elif self._worker_alive():
                refusal = "busy"
            elif not self._launch("takeoff", cmd=msg):
                refusal = "shut_down" if self._closed else "landing"
            else:
                return True
        if refusal == "already_flying":
            self._log.info("edge_takeoff_ignored", drone_id=self.drone_id, reason=refusal)
        else:
            self._log.warning("edge_takeoff_ignored", drone_id=self.drone_id, reason=refusal)
        if refusal == "landing":
            self._nack(msg, "landing")
        else:
            self._nack(msg, "busy", **({"detail": refusal} if refusal != "busy" else {}))
        return False

    def fail_safe(self, reason: str) -> None:
        """React to an unexpected error: land unless the drone is idle on the ground.

        An unreadable flight state counts as flying. A pending scheduled
        start is cancelled either way.
        """
        try:
            flying = self._is_flying()
        except Exception:
            flying = True
        if flying or self.is_busy:
            self.land_now(reason)
        else:
            with self._lock:
                self._cancel = ("busy", "fail_safe")
            self._abort.set()
            self._log.warning("edge_fail_safe_idle", drone_id=self.drone_id, reason=reason)

    def land_now(self, reason: str, *, only_if: Callable[[], bool] | None = None) -> bool:
        """Land immediately from the calling thread. Returns False if *only_if* stopped it.

        Announces the landing and sets the abort flag before it waits for
        the executor lock, so a running sequence sends nothing more and no
        new sequence can be launched from that moment on (see
        ``_landing_wanted_locked``); under the lock it records the landing
        and sets the abort flag again. Then it calls ``drone.land()``
        directly; nothing is queued. If another thread is already inside
        ``drone.land()``, this call returns without sending a second land. A
        failed land (one that raised, or returned a result that was not
        executed) is logged and retried by the monitor while the drone still
        reports flying. Every landing clears a hold and invalidates the plan.

        *only_if* is for the landings a hold must be able to stop (the
        end-of-lanes hover timeout and the plan's ``on_complete`` landing).
        It is called under the executor lock, in the same step that records
        the landing, and the landing is skipped when it returns False (for
        example because a hold or a re-task arrived after the landing was
        decided). Such a landing is not announced early. A landing for
        battery, link loss, the geofence, an error or a ground or operator
        command never passes *only_if*: a hold never stops it.
        """
        announced = only_if is None
        counted = False
        owner = False
        try:
            try:
                if announced:
                    with self._land_gate:
                        self._land_requests += 1
                        counted = True
                    self._abort.set()
                with self._lock:
                    if not announced and not only_if():
                        self._log.info("edge_land_skipped", drone_id=self.drone_id, reason=reason)
                        return False
                    self._land_reason = reason
                    # Set again under the lock, together with _land_reason, so the
                    # abort flag is set from here on whatever ran before.
                    self._abort.set()
                    self._last_land_attempt_s = self._clock()
                    self._flying_lanes = False
                    was_held, self._held, self._parked = self._held, False, None
                    was_valid, self._plan_valid = self._plan_valid, False
                    self._hover_since = None
                    # Last statement under the lock: from here the outer
                    # finally owns the flag and resets it.
                    owner = not self._land_inflight
                    self._land_inflight = True
            finally:
                # Taken after the executor lock is released, so the gate is
                # never held while taking _lock (a gate-holding re-entrant
                # call takes gate -> _lock; the reverse order could deadlock).
                if counted:
                    with self._land_gate:
                        self._land_requests -= 1
            if was_held:
                self._log.warning("edge_hold_cleared", drone_id=self.drone_id, reason=reason)
            if was_valid:
                self._log.warning("edge_plan_invalidated", drone_id=self.drone_id, reason=reason, plan_id=self.plan_id)
            if not owner:
                self._log.info("edge_land_already_in_progress", drone_id=self.drone_id, reason=reason)
                return True
            self._log.warning("edge_land", drone_id=self.drone_id, reason=reason)
            try:
                result = command_result(self.drone.land(), "land")
                if not result.ok:
                    # A land that was not executed is a failed land: the monitor
                    # retries it while the drone still reports flying.
                    self._log.error(
                        "edge_land_not_executed",
                        drone_id=self.drone_id,
                        reason=reason,
                        outcome=result.outcome.value,
                    )
            except Exception as exc:
                self._log.error("edge_land_error", drone_id=self.drone_id, reason=reason, error=repr(exc))
            return True
        finally:
            if owner:
                with self._lock:
                    self._land_inflight = False

    def _emit(self, msg: dict[str, Any]) -> None:
        """Send a message to the ground. Transport errors are logged, never raised."""
        try:
            self._send_fn(msg)
        except Exception as exc:
            self._log.warning("edge_send_error", drone_id=self.drone_id, type=msg.get("type"), error=repr(exc))
