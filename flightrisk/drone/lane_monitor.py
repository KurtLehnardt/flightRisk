"""The lane executor's monitor: watchdog, battery, land retries, hover timeout and telemetry.

``LaneExecutor`` (``flightrisk.drone.lane_executor``) inherits
``MonitorMixin``. A monitor thread calls ``tick`` every 50 ms. Each tick:

* retries a landing that has not taken effect while the drone still reports
  flying;
* lands the drone when its measured height is above the ceiling
  (``SafetyEnvelope.above_ceiling``), when the ground link has been silent
  past the threshold, when the battery is low, or when the plan's end-of-lanes hover
  (``on_complete: hover``) has lasted ``hover_timeout_s`` with the drone
  idle (a hold restarts that countdown: it starts again from zero once the
  drone is idle after its lanes; the hover timeout is checked again under
  the executor lock as the landing is recorded, so a hold acknowledged
  first always stops it);
* for a drone that reports its own position (``measured_pose()``, an
  autopilot), lands it with reason ``geofence_breach`` when that position
  is outside the geofence plus the drone's ``fence_breach_margin_cm``; the
  autopilot backend maps that reason to its return to launch;
* notices a landing the executor did not ask for (the Tello's own auto-land
  or a crash), and then invalidates the plan and clears any hold, as for
  every other landing;
* emits ``telemetry`` every 0.5 s, and at the same pace logs each change
  of the drone link (``edge_drone_link_unhealthy`` when the controller can
  no longer reach its drone, ``edge_drone_link_restored`` when it can
  again). An unhealthy drone link is only reported: it refuses takeoffs
  (``lane_flight``) and never lands, holds or delays anything.

The monitor never waits on the ground link: it hands telemetry to a sender
thread through a small queue that drops its oldest message when full, so a
stalled uplink cannot hold up the link-loss, battery and land-retry checks.

Telemetry is the v1 message with the optional ``ceiling_cm``,
``ceiling_margin_cm`` and ``alt_slot_cm`` fields, plus the protocol v2 fields ``epoch``,
``plan_id``, ``state`` and ``plan_valid`` once the executor is in v2 mode
(constructed with ``fleet_v2=True``, or after its first command with a
``cmd_id``). A v1 ground accepts the extra fields. When the drone controller
reports its link (``link_status()``, the Tello), telemetry on every version
also carries ``drone_link_ok``, ``drone_link_age_s`` and
``drone_link_detail``: while the link is down, ``battery`` and
``height_cm`` are the controller's last cached values, and the flag tells
the ground so. A controller with ``measured_pose()`` reports that pose in
``rel_x_cm``/``rel_y_cm``/``heading_deg`` (the Tello reports its
dead-reckoned pose), and one with ``geo_position()`` adds the optional
``lat`` and ``lon``.
"""
from __future__ import annotations

from typing import Any

__all__ = ["MonitorMixin"]


class MonitorMixin:
    """Monitor and telemetry for ``LaneExecutor`` (see the module docstring)."""

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
                unplanned = self._note_flight_state_locked(flying)
                if land_reason is not None and not flying and not self._worker_alive():
                    self._land_reason = None
                retry_due = now - self._last_land_attempt_s >= self.land_retry_s
                hover_due = self._hover_timeout_due_locked(flying, now)
                deadman = self._manual_deadman_due_locked(now)
            if deadman:
                # No manual command for the dead-man time: the drone hovers in
                # place (held). This never lands; the checks below still do.
                self._log.warning(
                    "edge_manual_deadman_hold", drone_id=self.drone_id, deadman_s=self.manual_deadman_s, is_flying=flying
                )
            if unplanned:
                self._log.error("edge_unplanned_landing", drone_id=self.drone_id, plan_id=self.plan_id)
            if self._link_holding and not self.envelope.link_lost():
                self._end_link_hold()
            if flying:
                height = self._read_height()
                if land_reason is not None:
                    if retry_due:
                        # Marked as a retry (thread-local, so a test double of
                        # land_now keeps its one-argument signature).
                        self._retrying.active = True
                        try:
                            self.land_now(land_reason)
                        finally:
                            self._retrying.active = False
                elif self.envelope.above_ceiling(height):
                    # The measured height is above the ceiling itself: stop
                    # climbing (the landing aborts any sequence) and land.
                    self._log.error(
                        "edge_ceiling_exceeded",
                        drone_id=self.drone_id,
                        height_cm=height,
                        ceiling_cm=self.envelope.ceiling_cm,
                        alt_slot_cm=self.envelope.alt_slot_cm,
                    )
                    self.land_now("ceiling_exceeded")
                elif (breach := self._measured_fence_breach()) is not None:
                    # An autopilot that reports its own position is outside
                    # the geofence (wind, a bad estimate): bring it back.
                    self._log.error(
                        "edge_geofence_breach",
                        drone_id=self.drone_id,
                        x_cm=round(breach[0], 1),
                        y_cm=round(breach[1], 1),
                        geofence_cm=self.envelope.geofence_cm,
                    )
                    self.land_now("geofence_breach")
                elif self.envelope.link_lost() and not self._link_loss_landing_due():
                    # Outdoor drones first hold position (link_loss_hold_s):
                    # the next move is not sent; the plan resumes if the
                    # ground comes back.
                    self._begin_link_hold()
                elif self.envelope.link_lost():
                    self._log.error(
                        "edge_link_lost",
                        drone_id=self.drone_id,
                        link_age_s=round(self.envelope.link_age_s(), 3),
                    )
                    self.land_now("link_loss")
                elif self.envelope.should_land(self._read_battery()):
                    self.land_now("low_battery")
                elif hover_due:
                    self._log.warning(
                        "edge_hover_timeout", drone_id=self.drone_id, hover_timeout_s=self._hover_timeout_s
                    )
                    # Checked again under the lock as the landing is recorded:
                    # a hold (or a nudge, a re-task) since the check above
                    # stops it.
                    self.land_now(
                        "hover_timeout", only_if=lambda: self._hover_timeout_due_locked(self._is_flying(), now)
                    )
        except Exception as exc:
            self._log.error("edge_monitor_error", drone_id=self.drone_id, error=repr(exc))
            try:
                self.fail_safe("monitor_error")
            except Exception:
                pass

        if self._next_telemetry_s is None or now >= self._next_telemetry_s:
            self._next_telemetry_s = now + self.telemetry_interval_s
            try:
                self._watch_drone_link()
            except Exception as exc:  # noqa: BLE001 - logging only, never a reason to land
                self._log.warning("edge_drone_link_watch_error", drone_id=self.drone_id, error=repr(exc))
            try:
                self._post_telemetry(self.telemetry())
            except Exception as exc:
                self._log.warning("edge_telemetry_error", drone_id=self.drone_id, error=repr(exc))

    def _note_flight_state_locked(self, flying: bool) -> bool:
        """Track flying -> grounded; True for a landing nobody requested. Caller holds ``_lock``.

        Such a landing (the Tello's own auto-land, a crash, a state poller
        that saw the drone on the ground) invalidates the plan and clears a
        hold, like every landing.
        """
        was_flying, self._was_flying = self._was_flying, flying
        if not was_flying or flying or self._land_reason is not None or self._land_inflight:
            return False
        self._plan_valid = False
        self._held = False
        self._parked = None
        self._flying_lanes = False
        self._hover_since = None
        return True

    def _link_loss_landing_due(self) -> bool:
        """True when a link loss must land now: no hold delay, or the delay is over."""
        hold_s = getattr(self.envelope, "link_loss_hold_s", None)
        return not hold_s or self.envelope.link_loss_due()

    def _begin_link_hold(self) -> None:
        with self._lock:
            started, self._link_holding = not self._link_holding, True
        if started:
            self._log.warning(
                "edge_link_lost_holding",
                drone_id=self.drone_id,
                link_age_s=round(self.envelope.link_age_s(), 3),
                hold_s=self.envelope.link_loss_hold_s,
            )

    def _end_link_hold(self) -> None:
        """The ground link is back: end a link-loss hold and continue the plan where it stopped."""
        with self._lock:
            if not self._link_holding:
                return
            self._link_holding = False
            parked, self._link_parked = self._link_parked, False
            resume = (
                parked
                and self._flying_lanes
                and self._plan_valid
                and not self._held
                and self._manual is None
                and self._land_reason is None
                and not self._worker_alive()
                and self._step < len(self._moves)
                and self._is_flying()
            )
            launched = resume and self._launch("lanes")
        self._log.info("edge_link_restored", drone_id=self.drone_id, resumed=bool(launched))

    def _measured_fence_breach(self) -> tuple[float, float] | None:
        """The measured position when it is outside the geofence plus the drone's margin, else None.

        Only a controller with ``measured_pose()`` (an autopilot that knows
        its own position) is checked; the Tello's position is dead-reckoned
        from the moves the envelope already allowed. The margin is the
        controller's ``fence_breach_margin_cm`` (GPS noise allowance).
        """
        probe = getattr(self.drone, "measured_pose", None)
        if not callable(probe):
            return None
        pose = probe()
        if pose is None:
            return None
        margin = float(getattr(self.drone, "fence_breach_margin_cm", 0.0) or 0.0)
        limit = self.envelope.geofence_cm + margin
        if abs(pose.x_cm) <= limit and abs(pose.y_cm) <= limit:
            return None
        return pose.x_cm, pose.y_cm

    def _hover_timeout_due_locked(self, flying: bool, now: float) -> bool:
        """True once the end-of-lanes hover has lasted ``hover_timeout_s``. Caller holds ``_lock``.

        The countdown runs only while the drone hovers idle after its last
        lane with ``on_complete: hover``: not while holding, landing or
        running any sequence (a nudge, a re-task), and it restarts after.
        """
        idle_after_lanes = (
            flying
            and self._on_complete == "hover"
            and self._flying_lanes
            and self._step >= len(self._moves)
            and not self._worker_alive()
            and not self._held
            and self._manual is None
            and self._land_reason is None
        )
        if not idle_after_lanes:
            self._hover_since = None
            return False
        if self._hover_since is None:
            self._hover_since = now
            return False
        return now - self._hover_since >= self._hover_timeout_s

    def _watch_drone_link(self) -> None:
        """Log each change of the drone link once. Never lands or holds: takeoff checks it itself."""
        link = self._drone_link()
        problem = None if link is None or link.ok else (link.reason, link.detail)
        previous, self._link_problem = self._link_problem, problem
        if problem is None:
            if previous is not None:
                self._log.info("edge_drone_link_restored", drone_id=self.drone_id, reason=previous[0])
            return
        if previous is None or previous[0] != problem[0]:
            self._log.warning(
                "edge_drone_link_unhealthy",
                drone_id=self.drone_id,
                reason=link.reason,
                detail=link.detail,
                drone_link_age_s=link.age_s,
                is_flying=self._is_flying(),
            )

    def _post_telemetry(self, msg: dict[str, Any]) -> None:
        """Queue telemetry for the sender thread; never waits on the ground link.

        When the queue is full the oldest message is dropped. Once the
        executor is stopping the message is dropped: the sender thread is
        exiting or gone, and sending from the calling thread could block the
        monitor on a stalled uplink. Without a running sender thread
        (``tick()`` called directly, before ``start()``) the message is sent
        from the calling thread.
        """
        with self._telemetry_cv:
            if self._stop.is_set():
                return
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
        """Build a ``telemetry`` message (design section 3; v2 fields in v2 mode)."""
        pose = self._reported_pose()
        flying = self._is_flying()
        with self._lock:
            takeoff_at = self._takeoff_at_local_s
            lanes_total = self._lanes_total
            v2 = self._v2_telemetry
            state = self._state_locked(flying)
            plan_valid = self._plan_valid
            control_mode = self._control_mode_locked()
        msg = {
            "type": "telemetry",
            "drone_id": self.drone_id,
            "battery": self._read_battery(),
            "height_cm": self._read_height(),
            "is_flying": flying,
            "rel_x_cm": round(pose.x_cm, 1),
            "rel_y_cm": round(pose.y_cm, 1),
            "heading_deg": round(pose.heading_deg, 1),
            "lane_index": self.lane_index,
            "lanes_total": lanes_total,
            "link_age_s": round(self.envelope.link_age_s(), 3),
            "takeoff_at_local_s": takeoff_at,
        }
        # Optional fields (every ground accepts extra telemetry fields): the
        # active ceiling, margin and altitude slot. Their presence also tells
        # the ground that this edge takes ``set_ceiling``.
        msg.update(self.envelope.ceiling_status())
        # Optional on every version: the manual-control mode. Its presence
        # also tells the ground that this edge takes the manual commands.
        msg["control_mode"] = control_mode
        if v2:
            msg.update(epoch=self.epoch_seen, plan_id=self.plan_id, state=state, plan_valid=plan_valid)
        geo = getattr(self.drone, "geo_position", None)
        if callable(geo):
            # Optional: an autopilot with GPS also reports where it is.
            position = geo()
            if position is not None:
                msg.update(lat=position[0], lon=position[1])
        link = self._drone_link()
        if link is not None:
            msg.update(
                drone_link_ok=link.ok,
                drone_link_age_s=link.age_s,
                drone_link_detail=None if link.ok else (link.detail or "drone unreachable"),
            )
        return msg
