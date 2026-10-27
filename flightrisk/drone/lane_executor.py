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
  switches to it after its current move.
* ``clock`` stores the ground clock offset (edge clock minus ground clock).
* ``start_at`` schedules takeoff for the moment the edge's monotonic clock
  reaches ``t_ground_s + offset_s``, then the drone climbs to its altitude
  slot and flies its lanes. An ``ack`` reports when the start executed, in
  ground time (``t_edge_exec - offset_s``).
* ``takeoff`` takes off and climbs to the altitude slot without flying lanes.
* ``land`` and ``emergency_land_all`` land at once from the calling thread.
  They never wait behind a queued or running flight sequence.
* ``ping`` is answered with ``pong`` timestamps for clock sync, and
  ``heartbeat`` (like every ground message) resets the link-loss watchdog.

A monitor thread calls ``tick`` every 50 ms. It lands the drone when the
ground link has been silent past the threshold or the battery is low, retries
a landing that did not take, and emits ``telemetry`` every 0.5 s. The monitor
never waits on the ground link: it hands telemetry to a sender thread through
a small queue that drops its oldest message when full, so a stalled uplink
cannot hold up the link-loss, battery and land-retry checks.

Fail-safe rules: any unexpected exception while flying a sequence or running
the monitor lands the drone. Takeoff is refused below the battery threshold,
and any move the geofence refuses ends the flight with a landing. Position is
dead-reckoned from the commanded moves (the frame is described in
``flightrisk.drone.safety``).
"""
from __future__ import annotations

import inspect
import math
import threading
import time
from collections import deque
from typing import Any, Callable

from flightrisk.drone.safety import (
    HORIZONTAL_DIRECTIONS,
    MAX_MOVE_CM,
    MIN_MOVE_CM,
    SafetyEnvelope,
    is_finite_number,
)
from flightrisk.observability import StructuredLogger

__all__ = [
    "CONTROL_TYPES",
    "LaneExecutor",
    "parse_lanes",
    "split_lane_moves",
]

# Ground->edge message types the executor handles (design section 3).
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
# Below this much remaining time the start wait switches from event waits to
# short sleeps, so takeoff fires close to the scheduled instant.
_START_SPIN_S = 0.02


def split_lane_moves(distance_cm: float) -> list[int]:
    """Split one lane into Tello-sized moves of 20-500 cm each, as evenly as possible."""
    total = int(round(distance_cm))
    if total < MIN_MOVE_CM:
        raise ValueError(f"lane distance {distance_cm!r} cm is below the {MIN_MOVE_CM} cm minimum move")
    count = max(1, math.ceil(total / MAX_MOVE_CM))
    base, extra = divmod(total, count)
    return [base + (1 if i < extra else 0) for i in range(count)]


def parse_lanes(raw: Any) -> list[tuple[str, int]]:
    """Validate an ``assign_lanes`` lane list and return ``(direction, distance_cm)`` pairs.

    Raises ``ValueError`` describing the first problem found. Only
    horizontal directions are accepted, so a lane plan can never change the
    drone's altitude slot.
    """
    if not isinstance(raw, list):
        raise ValueError("lanes must be a list")
    lanes: list[tuple[str, int]] = []
    for index, lane in enumerate(raw):
        if not isinstance(lane, dict):
            raise ValueError(f"lane {index} is not an object")
        direction = lane.get("direction")
        distance = lane.get("distance_cm")
        if direction not in HORIZONTAL_DIRECTIONS:
            raise ValueError(f"lane {index} has unsupported direction {direction!r}")
        if not is_finite_number(distance) or int(round(distance)) < MIN_MOVE_CM:
            raise ValueError(f"lane {index} has invalid distance_cm {distance!r}")
        lanes.append((direction, int(round(distance))))
    return lanes


class LaneExecutor:
    """Executes ground control messages against one drone inside a safety envelope."""

    CONTROL_TYPES = CONTROL_TYPES

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

        self._lock = threading.RLock()
        # Set by every landing; tells a running flight sequence to stop
        # sending commands. Cleared only when a new sequence is launched.
        self._abort = threading.Event()
        self._stop = threading.Event()
        # Set for good by shutdown(); no flight sequence may start afterwards.
        self._closed = False
        self._worker: threading.Thread | None = None
        # The thread running the current flight sequence, or None. A thread
        # clears it (under ``_lock``) once it will send no more commands, so
        # a re-task arriving as the last lane finishes still gets flown.
        self._active_thread: threading.Thread | None = None
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
        self._last_land_attempt_s = 0.0
        # True while some thread is inside drone.land(). A second land
        # request meanwhile (for example emergency_land_all arriving through
        # both the transport callback and recv()) does not send another one.
        self._land_inflight = False
        self._next_telemetry_s: float | None = None
        # Telemetry handed from the monitor to the sender thread. The deque
        # drops its oldest entry when full.
        self._telemetry_queue: deque[dict[str, Any]] = deque(maxlen=max(1, int(telemetry_queue_size)))
        self._telemetry_cv = threading.Condition()
        self._telemetry_sender: threading.Thread | None = None
        self._telemetry_dropped = 0

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
        """True while a flight sequence (scheduled start, takeoff or lanes) is running."""
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

    def _worker_alive(self) -> bool:
        """True while a flight sequence may still send commands. Caller holds ``_lock``."""
        return self._active_thread is not None

    def _is_flying(self) -> bool:
        return bool(getattr(self.drone.state, "is_flying", False))

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
        """Dispatch one ground control message of a type in ``CONTROL_TYPES``.

        ``recv_t`` is the edge clock reading taken when the message arrived;
        it becomes the ``t2`` of a ``pong``. Exceptions propagate to the
        caller, which must land (see ``edge_device._handle_control``).
        """
        mtype = msg.get("type")
        if mtype == "emergency_land_all":
            self.land_now("emergency_land_all")
        elif mtype == "land":
            self.land_now("ground_land")
        elif mtype == "ping":
            self.handle_ping(msg, recv_t=recv_t)
        elif mtype == "heartbeat":
            self._log.debug("edge_heartbeat", drone_id=self.drone_id, seq=msg.get("seq"))
        elif mtype == "clock":
            self.handle_clock(msg)
        elif mtype == "assign_lanes":
            self.handle_assign_lanes(msg)
        elif mtype == "start_at":
            self.handle_start_at(msg)
        elif mtype == "takeoff":
            self.handle_takeoff()
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

    def handle_assign_lanes(self, msg: dict[str, Any]) -> bool:
        """Validate and store a lane plan, then acknowledge it.

        Refuses a plan addressed to another drone (``other_drone``), with
        malformed lanes (``invalid``), with an out-of-range altitude
        (``altitude``), whose dead-reckoned path leaves the geofence
        (``geofence``), or while the battery level cannot be read
        (``battery``), because the ``ack`` must carry it. A refused plan is
        logged and answered with a ``nack`` carrying that reason and the
        number of lanes received; the current plan is kept unchanged. An
        ``alt_cm`` in the plan replaces the local altitude slot, because the
        coordinator assigns slots across the whole fleet.

        Re-tasking: when the drone is already flying its lanes and the new
        plan starts with the lanes flown so far (including the current one)
        unchanged, progress carries over. The drone keeps its lane index and
        continues into the appended lanes instead of starting again from
        lane 0. Any other plan starts from its first lane at the current
        position.
        """
        raw_lanes = msg.get("lanes")
        lanes_received = len(raw_lanes) if isinstance(raw_lanes, list) else 0
        target = msg.get("drone_id")
        if target is not None and str(target) != self.drone_id:
            return self._refuse_lanes("other_drone", lanes_received, target=target)
        try:
            lanes = parse_lanes(raw_lanes)
        except ValueError as exc:
            return self._refuse_lanes("invalid", lanes_received, error=str(exc))
        alt_cm = msg.get("alt_cm")
        if alt_cm is not None:
            try:
                alt_cm = SafetyEnvelope.validate_alt_slot(alt_cm)
            except ValueError as exc:
                return self._refuse_lanes("altitude", lanes_received, error=str(exc))
        battery = self._read_battery()
        if not is_finite_number(battery):
            return self._refuse_lanes("battery", lanes_received, battery=battery)

        moves = [
            (lane_index, chunk_index, direction, distance)
            for lane_index, (direction, lane_cm) in enumerate(lanes)
            for chunk_index, distance in enumerate(split_lane_moves(lane_cm))
        ]

        with self._lock:
            carried_step = None
            if self._flying_lanes and self._is_flying():
                carried_step = self._carried_step(lanes, moves)
            start_step = carried_step if carried_step is not None else 0

            # Check the path still to be flown against the geofence before
            # accepting anything. A move in progress is not in the pose yet,
            # and it is still part of moves[start_step:].
            pose = self.envelope.pose_snapshot()
            outside = None
            for _, _, direction, distance in moves[start_step:]:
                pose.apply_move(direction, distance)
                if not self.envelope.inside_fence(pose.x_cm, pose.y_cm):
                    outside = (pose.x_cm, pose.y_cm)
                    break
            if outside is None:
                accepted = self._store_plan(msg, lanes, moves, alt_cm, carried_step, start_step)
        if outside is not None:
            # The nack is sent outside the lock, like every other message.
            return self._refuse_lanes(
                "geofence",
                lanes_received,
                geofence_cm=self.envelope.geofence_cm,
                x_cm=round(outside[0], 1),
                y_cm=round(outside[1], 1),
            )
        lane_index, retask = accepted
        self._log.info(
            "edge_lanes_assigned",
            drone_id=self.drone_id,
            lanes_total=len(lanes),
            lane_index=lane_index,
            progress_carried=carried_step is not None,
            alt_slot_cm=self.envelope.alt_slot_cm,
            retask=retask,
        )
        self._emit({
            "type": "ack",
            "for": "assign_lanes",
            "drone_id": self.drone_id,
            "battery": battery,
            "lanes_total": len(lanes),
        })
        return True

    def _store_plan(
        self,
        msg: dict[str, Any],
        lanes: list[tuple[str, int]],
        moves: list[tuple[int, int, str, int]],
        alt_cm: float | None,
        carried_step: int | None,
        start_step: int,
    ) -> tuple[int, bool]:
        """Make an accepted plan current. Caller holds ``_lock``.

        Returns the lane index to report and whether a re-task sequence was
        launched.
        """
        if alt_cm is not None and alt_cm != self.envelope.alt_slot_cm:
            self._log.warning(
                "edge_alt_slot_assigned",
                drone_id=self.drone_id,
                local_cm=self.envelope.alt_slot_cm,
                assigned_cm=alt_cm,
            )
            self.envelope.set_alt_slot(alt_cm)
        if carried_step is not None:
            # A move of the old plan that is still in flight counts
            # towards the new plan when it completes.
            self._linked_gens.add(self._plan_gen)
        else:
            self._linked_gens.clear()
        self._lanes = lanes
        self._moves = moves
        self._step = start_step
        self._lanes_total = len(lanes)
        self._plan_gen += 1
        self._zone = msg.get("zone")
        lane_index = self.lane_index
        retask = (
            self._flying_lanes
            and self._is_flying()
            and self._land_reason is None
            and not self._worker_alive()
        )
        if retask:
            retask = self._launch("lanes")
        return lane_index, retask

    def _refuse_lanes(self, reason: str, lanes_total: int, **fields: Any) -> bool:
        """Log a refused lane plan and tell the ground with a ``nack``. Returns False."""
        self._log.warning("edge_assign_lanes_refused", drone_id=self.drone_id, reason=reason, **fields)
        self._emit({
            "type": "nack",
            "for": "assign_lanes",
            "drone_id": self.drone_id,
            "reason": reason,
            "lanes_total": lanes_total,
        })
        return False

    def _carried_step(
        self, lanes: list[tuple[str, int]], moves: list[tuple[int, int, str, int]]
    ) -> int | None:
        """Step in a new plan that continues the current one, or None. Caller holds ``_lock``.

        Progress carries over only if every lane flown so far, including the
        lane in progress, is unchanged in the new plan.
        """
        if self._step < len(self._moves):
            current = self._moves[self._step][:2]
            lanes_fixed = current[0] + 1
        else:
            current = (self._lanes_total, 0)
            lanes_fixed = self._lanes_total
        if lanes[:lanes_fixed] != self._lanes[:lanes_fixed]:
            return None
        for index, move in enumerate(moves):
            if move[:2] >= current:
                return index
        return len(moves)

    def handle_start_at(self, msg: dict[str, Any]) -> bool:
        """Schedule takeoff plus lanes at ``t_ground_s + offset_s`` on the edge clock."""
        t_ground = msg.get("t_ground_s")
        if not is_finite_number(t_ground):
            return self._refuse_start("invalid_t_ground_s", t_ground_s=t_ground)
        with self._lock:
            offset = self._offset_s
            if offset is None:
                return self._refuse_start("no_clock_offset")
            if self._step >= len(self._moves):
                return self._refuse_start("no_lanes")
            if self._worker_alive():
                return self._refuse_start("busy")
            if self._land_reason is not None and self._is_flying():
                return self._refuse_start("landing")
            t_edge = float(t_ground) + offset
            lead = t_edge - self._clock()
            if lead > self.max_start_lead_s:
                return self._refuse_start("too_far_ahead", lead_s=lead)
            if lead < -self.max_start_late_s:
                return self._refuse_start("too_late", lead_s=lead)
            if not self._launch("start_at", t_edge=t_edge, offset_s=offset):
                return self._refuse_start("shut_down")
        self._log.info(
            "edge_start_scheduled",
            drone_id=self.drone_id,
            t_ground_s=float(t_ground),
            offset_s=offset,
            t_edge_s=t_edge,
            lead_s=round(lead, 4),
        )
        return True

    def _refuse_start(self, reason: str, **fields: Any) -> bool:
        self._log.warning("edge_start_refused", drone_id=self.drone_id, reason=reason, **fields)
        return False

    def handle_takeoff(self) -> bool:
        """Take off and climb to the altitude slot, then hover."""
        with self._lock:
            if self._is_flying():
                self._log.info("edge_takeoff_ignored", drone_id=self.drone_id, reason="already_flying")
                return False
            if self._worker_alive():
                self._log.warning("edge_takeoff_ignored", drone_id=self.drone_id, reason="busy")
                return False
            if not self._launch("takeoff"):
                self._log.warning("edge_takeoff_ignored", drone_id=self.drone_id, reason="shut_down")
                return False
        return True

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
            self._abort.set()
            self._log.warning("edge_fail_safe_idle", drone_id=self.drone_id, reason=reason)

    def land_now(self, reason: str) -> None:
        """Land immediately from the calling thread.

        Sets the abort flag first so a running sequence sends nothing more,
        then calls ``drone.land()`` directly; nothing is queued. If another
        thread is already inside ``drone.land()``, this call returns without
        sending a second land. A failed land is logged and retried by the
        monitor while the drone still reports flying.
        """
        self._abort.set()
        with self._lock:
            self._land_reason = reason
            self._last_land_attempt_s = self._clock()
            self._flying_lanes = False
            duplicate = self._land_inflight
            self._land_inflight = True
        if duplicate:
            self._log.info("edge_land_already_in_progress", drone_id=self.drone_id, reason=reason)
            return
        self._log.warning("edge_land", drone_id=self.drone_id, reason=reason)
        try:
            self.drone.land()
        except Exception as exc:
            self._log.error("edge_land_error", drone_id=self.drone_id, reason=reason, error=repr(exc))
        finally:
            with self._lock:
                self._land_inflight = False

    # ------------------------------------------------------------------
    # Monitor: watchdog, battery, land retries, telemetry
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """Run one round of safety checks and send telemetry when due. Never raises.

        Nothing here waits on the ground link: telemetry is queued for the
        sender thread (see ``_post_telemetry``). The only blocking call is a
        landing, which goes to the drone, not the ground.
        """
        now = self._clock()
        try:
            flying = self._is_flying()
            with self._lock:
                land_reason = self._land_reason
                if land_reason is not None and not flying and not self._worker_alive():
                    self._land_reason = None
                retry_due = now - self._last_land_attempt_s >= self.land_retry_s
            if flying:
                if land_reason is not None:
                    if retry_due:
                        self.land_now(land_reason)
                elif self.envelope.link_lost():
                    self._log.error(
                        "edge_link_lost",
                        drone_id=self.drone_id,
                        link_age_s=round(self.envelope.link_age_s(), 3),
                    )
                    self.land_now("link_loss")
                elif self.envelope.should_land(self._read_battery()):
                    self.land_now("low_battery")
        except Exception as exc:
            self._log.error("edge_monitor_error", drone_id=self.drone_id, error=repr(exc))
            try:
                self.fail_safe("monitor_error")
            except Exception:
                pass

        if self._next_telemetry_s is None or now >= self._next_telemetry_s:
            self._next_telemetry_s = now + self.telemetry_interval_s
            try:
                self._post_telemetry(self.telemetry())
            except Exception as exc:
                self._log.warning("edge_telemetry_error", drone_id=self.drone_id, error=repr(exc))

    def _post_telemetry(self, msg: dict[str, Any]) -> None:
        """Queue telemetry for the sender thread; never waits on the ground link.

        When the queue is full the oldest message is dropped. Without a
        running sender thread (``tick()`` called directly, before
        ``start()``) the message is sent from the calling thread.
        """
        with self._telemetry_cv:
            sender = self._telemetry_sender
            if sender is not None and sender.is_alive():
                if len(self._telemetry_queue) == self._telemetry_queue.maxlen:
                    self._telemetry_dropped += 1
                    if self._telemetry_dropped == 1 or self._telemetry_dropped % 20 == 0:
                        self._log.warning(
                            "edge_telemetry_dropped",
                            drone_id=self.drone_id,
                            dropped=self._telemetry_dropped,
                        )
                self._telemetry_queue.append(msg)
                self._telemetry_cv.notify()
                return
        self._emit(msg)

    def _telemetry_loop(self) -> None:
        """Send queued telemetry until the executor stops. Runs on its own thread."""
        while True:
            with self._telemetry_cv:
                while not self._telemetry_queue and not self._stop.is_set():
                    self._telemetry_cv.wait(0.5)
                if self._stop.is_set():
                    return
                msg = self._telemetry_queue.popleft()
            self._emit(msg)

    def telemetry(self) -> dict[str, Any]:
        """Build a ``telemetry`` message (design section 3)."""
        pose = self.envelope.pose_snapshot()
        with self._lock:
            takeoff_at = self._takeoff_at_local_s
            lanes_total = self._lanes_total
        return {
            "type": "telemetry",
            "drone_id": self.drone_id,
            "battery": self._read_battery(),
            "height_cm": self._read_height(),
            "is_flying": self._is_flying(),
            "rel_x_cm": round(pose.x_cm, 1),
            "rel_y_cm": round(pose.y_cm, 1),
            "heading_deg": round(pose.heading_deg, 1),
            "lane_index": self.lane_index,
            "lanes_total": lanes_total,
            "link_age_s": round(self.envelope.link_age_s(), 3),
            "takeoff_at_local_s": takeoff_at,
        }

    def _emit(self, msg: dict[str, Any]) -> None:
        """Send a message to the ground. Transport errors are logged, never raised."""
        try:
            self._send_fn(msg)
        except Exception as exc:
            self._log.warning("edge_send_error", drone_id=self.drone_id, type=msg.get("type"), error=repr(exc))

    # ------------------------------------------------------------------
    # Flight sequences (worker thread)
    # ------------------------------------------------------------------

    def _launch(self, kind: str, t_edge: float | None = None, offset_s: float = 0.0) -> bool:
        """Start a flight sequence thread. Caller holds ``_lock``.

        Returns False (and starts nothing) once the executor is shut down.
        """
        if self._closed:
            return False
        self._abort.clear()
        self._worker = threading.Thread(
            target=self._run_sequence,
            args=(kind, t_edge, offset_s),
            daemon=True,
            name=f"{self.drone_id}-flight",
        )
        self._active_thread = self._worker
        self._worker.start()
        return True

    def _release_active(self) -> None:
        """Mark the calling sequence as finished. Caller holds ``_lock``."""
        if self._active_thread is threading.current_thread():
            self._active_thread = None

    def _run_sequence(self, kind: str, t_edge: float | None, offset_s: float) -> None:
        try:
            if kind == "start_at":
                if not self._wait_until(t_edge):
                    return
                t_exec = self._clock()
                if not self._is_flying() and not self._do_takeoff(t_exec):
                    return
                # The offset is the one the start was scheduled with, so the
                # reported ground time matches the requested t_ground_s.
                self._emit({
                    "type": "ack",
                    "for": "start_at",
                    "drone_id": self.drone_id,
                    "executed_at_ground_s": t_exec - offset_s,
                })
                self._fly_plan()
            elif kind == "takeoff":
                if self._do_takeoff(self._clock()):
                    self._climb_to_slot()
            elif kind == "lanes":
                self._fly_plan()
        except Exception as exc:
            self._log.error("edge_executor_error", drone_id=self.drone_id, kind=kind, error=repr(exc))
            self.land_now("executor_error")
        finally:
            with self._lock:
                self._release_active()

    def _wait_until(self, t_edge: float) -> bool:
        """Wait for the edge clock to reach ``t_edge``. False if aborted first."""
        while True:
            if self._abort.is_set():
                return False
            remaining = t_edge - self._clock()
            if remaining <= 0:
                return True
            if remaining > _START_SPIN_S:
                self._abort.wait(min(remaining - _START_SPIN_S / 2, 0.05))
            else:
                self._sleep(0.0005)

    def _do_takeoff(self, t_exec: float) -> bool:
        battery = self._read_battery()
        if not self.envelope.can_takeoff(battery):
            self._log.warning(
                "edge_takeoff_refused",
                drone_id=self.drone_id,
                reason="battery",
                battery=battery,
                min_takeoff_battery=self.envelope.min_takeoff_battery,
            )
            return False
        if self.envelope.link_lost():
            # The ground went silent while the start was pending; the
            # watchdog would land the drone straight away, so do not lift.
            self._log.warning(
                "edge_takeoff_refused",
                drone_id=self.drone_id,
                reason="link_loss",
                link_age_s=round(self.envelope.link_age_s(), 3),
            )
            return False
        if self._abort.is_set():
            return False
        with self._lock:
            self._flying_lanes = False
        if self._drone_takeoff() is False:
            # A landing started after the check above. The controller checked
            # the abort flag under its command lock and sent nothing.
            self._log.warning("edge_takeoff_refused", drone_id=self.drone_id, reason="landing")
            return False
        with self._lock:
            self._takeoff_at_local_s = t_exec
        self._log.info("edge_takeoff", drone_id=self.drone_id, battery=battery, at_local_s=t_exec)
        return not self._abort.is_set()

    def _drone_takeoff(self) -> Any:
        """Call ``drone.takeoff``, passing the abort flag when the controller takes one.

        ``TelloController`` and ``FakeTelloController`` accept ``abort`` and
        check it under their command lock immediately before sending the
        takeoff, so a ``land_now`` that runs between the abort check in
        ``_do_takeoff`` and the takeoff itself cannot be followed by a
        takeoff. They return False when they refuse.
        """
        takeoff = self.drone.takeoff
        try:
            accepts_abort = "abort" in inspect.signature(takeoff).parameters
        except (TypeError, ValueError):
            accepts_abort = False
        if accepts_abort:
            return takeoff(abort=self._abort)
        return takeoff()

    def _climb_to_slot(self) -> bool:
        """Move vertically to the altitude slot. Lands if the height is unknown."""
        height = self._read_height()
        try:
            move = self.envelope.slot_move(height)
        except ValueError as exc:
            self._log.error("edge_altitude_unknown", drone_id=self.drone_id, error=str(exc))
            self.land_now("height_unknown")
            return False
        if move is None:
            return True
        direction, distance = move
        self._log.info(
            "edge_alt_slot_climb",
            drone_id=self.drone_id,
            height_cm=height,
            alt_slot_cm=self.envelope.alt_slot_cm,
            direction=direction,
            distance_cm=distance,
        )
        return self._command_move(direction, distance, record=False)

    def _command_move(self, direction: str, distance: int, record: bool) -> bool:
        if self._abort.is_set():
            return False
        result = self.drone.move(direction, distance)
        if result is False:
            if self._abort.is_set():
                return False
            raise RuntimeError(f"move {direction} {distance} cm was dropped by the drone controller")
        if record:
            self.envelope.record_move(direction, distance)
        return not self._abort.is_set()

    def _fly_plan(self) -> None:
        """Fly the current lane plan from its current step, following re-tasks."""
        with self._lock:
            if self._abort.is_set():
                return
            self._flying_lanes = True
        climbed_gen: int | None = None
        while not self._abort.is_set():
            with self._lock:
                gen = self._plan_gen
                step = self._moves[self._step] if self._step < len(self._moves) else None
                if step is None and climbed_gen == gen:
                    # Done. Release under the lock so a plan that arrives
                    # from now on starts a new sequence.
                    self._release_active()
            if climbed_gen != gen:
                # New plan (or first plan): make sure the drone is at its slot,
                # which a re-task may have changed.
                if not self._climb_to_slot():
                    return
                climbed_gen = gen
                continue
            if step is None:
                self._log.info("edge_lanes_complete", drone_id=self.drone_id, lanes_total=self.lanes_total)
                return
            lane_index, _, direction, distance = step
            if not self.envelope.check_move(direction, distance):
                pose = self.envelope.pose_snapshot()
                self._log.error(
                    "edge_geofence_refused",
                    drone_id=self.drone_id,
                    lane_index=lane_index,
                    direction=direction,
                    distance_cm=distance,
                    x_cm=round(pose.x_cm, 1),
                    y_cm=round(pose.y_cm, 1),
                )
                self.land_now("geofence")
                return
            if not self._command_move(direction, distance, record=True):
                return
            with self._lock:
                same_plan = self._plan_gen == gen or gen in self._linked_gens
                if (
                    same_plan
                    and not self._abort.is_set()
                    and self._step < len(self._moves)
                    and self._moves[self._step][:2] == step[:2]
                ):
                    self._step += 1
