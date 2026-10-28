"""The lane executor's monitor: watchdog, battery, land retries, hover timeout and telemetry.

``LaneExecutor`` (``flightrisk.drone.lane_executor``) inherits
``MonitorMixin``. A monitor thread calls ``tick`` every 50 ms. Each tick:

* retries a landing that has not taken effect while the drone still reports
  flying;
* lands the drone when the ground link has been silent past the threshold,
  when the battery is low, or when the plan's end-of-lanes hover
  (``on_complete: hover``) has lasted ``hover_timeout_s`` with the drone
  idle (a hold restarts that countdown: it starts again from zero once the
  drone is idle after its lanes; the hover timeout is checked again under
  the executor lock as the landing is recorded, so a hold acknowledged
  first always stops it);
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

Telemetry is the v1 message, plus the protocol v2 fields ``epoch``,
``plan_id``, ``state`` and ``plan_valid`` once the executor is in v2 mode
(constructed with ``fleet_v2=True``, or after its first command with a
``cmd_id``). A v1 ground accepts the extra fields. When the drone controller
reports its link (``link_status()``, the Tello), telemetry on every version
also carries ``drone_link_ok``, ``drone_link_age_s`` and
``drone_link_detail``: while the link is down, ``battery`` and
``height_cm`` are the controller's last cached values, and the flag tells
the ground so.
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
            if unplanned:
                self._log.error("edge_unplanned_landing", drone_id=self.drone_id, plan_id=self.plan_id)
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
        pose = self.envelope.pose_snapshot()
        flying = self._is_flying()
        with self._lock:
            takeoff_at = self._takeoff_at_local_s
            lanes_total = self._lanes_total
            v2 = self._v2_telemetry
            state = self._state_locked(flying)
            plan_valid = self._plan_valid
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
        if v2:
            msg.update(epoch=self.epoch_seen, plan_id=self.plan_id, state=state, plan_valid=plan_valid)
        link = self._drone_link()
        if link is not None:
            msg.update(
                drone_link_ok=link.ok,
                drone_link_age_s=link.age_s,
                drone_link_detail=None if link.ok else (link.detail or "drone unreachable"),
            )
        return msg
