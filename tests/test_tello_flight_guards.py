"""Flight guards after the Tello desk incident of 2026-11-21 (``flightrisk/drone/flight_guards.py``).

The incident: one standard Tello took off from a desk about 122 cm above the
floor with a 20 cm altitude slot. The takeoff ``ok`` came 8.8 s after the
takeoff was sent; about 100 ms later the climb read the height once
(``h`` 100 cm, while the drone was really 30 to 40 cm above the desk) and
sent ``down 80``, which the Tello answered ``error No valid imu``. The drone
drifted off the desk edge and came down to about 30 cm above the floor.

These tests pin the fixes:

* the move to the slot waits for a settled, fresh height read after the
  takeoff reply, and never moves on a height read before it;
* a descent is bounded by the downward distance (``tof``);
* a takeoff answered too late lands instead of climbing;
* a transient refusal is retried a bounded number of times, from a new
  settled height (a slot move) or through a new sense gate (a sensed lane
  step), and is counted in telemetry;
* the hover-only test sends no move to the slot and flies no lane;
* a Tello slot below 50 cm is refused with operator text;
* a reply to a land's interrupting ``stop`` is never read as the takeoff's.

The executor tests run a real ``LaneExecutor`` on fake Tellos with a fake
clock. The wire tests run the real ``TelloController`` over ``FakeTelloWire``
(``tests/conftest.py``), whose state stream carries ``h`` and ``tof``.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.dashboard.ceiling import CeilingSettings
from flightrisk.drone import tello as tello_module
from flightrisk.drone.controller import (
    CommandRefusedError,
    DroneLinkStatus,
    SettledHeight,
)
from flightrisk.drone.fleet_plan_check import _slots
from flightrisk.drone.flight_guards import (
    DESCENT_CLEARANCE_CM,
    ENV_HOVER_TEST,
    ENV_REFUSAL_RETRIES,
    ENV_SETTLE_SAMPLES,
    ENV_TAKEOFF_LATE_S,
    FlightGuards,
    transient_refusal,
)
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.lane_flight import (
    EVENT_DESCENT_REFUSED,
    EVENT_HEIGHT_SETTLED,
    EVENT_HEIGHT_UNSETTLED,
    EVENT_HOVER_TEST,
    EVENT_MOVE_REFUSED,
    EVENT_TAKEOFF_LATE,
)
from flightrisk.drone.obstacle_sensor import FakeObstacleSensor
from flightrisk.drone.safety import MIN_ALT_SLOT_CM, SafetyEnvelope, slot_below_minimum
from flightrisk.edge_device import _flight_guards
from tests.lane_executor_support import (
    FakeClock,
    QuietLogger,
    SteppedTello,
    assign,
    make_v2_executor,
    reply_for,
    send,
    start_at,
    takeoff,
    validate_sent,
    wait_for,
)

WAIT_S = 10.0
# Short holds so the retry tests run quickly (the smallest allowed).
FAST = FlightGuards(refusal_hold_s=0.5)


# ---------------------------------------------------------------------------
# A fake Tello with a settled height and scripted refusals
# ---------------------------------------------------------------------------


class GuardedTello(SteppedTello):
    """A fake Tello that reports settled heights from a script and refuses scripted moves.

    ``read_height_cm`` returns ``stale_height_cm`` while flying: the height
    the edge read right after the late takeoff reply on 2026-11-21. Each
    ``settled_height`` call takes the next entry of ``settled`` (None: the
    drone did not settle). ``refusals`` maps a command (``"down 30"``) or a
    direction (``"forward"``) to the replies its next attempts are refused
    with. ``takeoff_s`` advances the fake clock during the takeoff, as a late
    takeoff reply does. Every takeoff, settle and move is written to
    ``timeline`` in order.
    """

    def __init__(self, *, settled=(), refusals=None, clock: FakeClock | None = None, takeoff_s: float = 0.0,
                 stale_height_cm: int = 100, timeline: list | None = None, **kwargs):
        super().__init__(auto=True, name="drone-2", battery=78, **kwargs)
        self.settled_script: list[SettledHeight | None] = list(settled)
        self.refusals: dict[str, list[str]] = {k: list(v) for k, v in (refusals or {}).items()}
        self.clock = clock
        self.takeoff_s = takeoff_s
        self.stale_height_cm = stale_height_cm
        self.timeline = timeline if timeline is not None else []
        self.settle_args: list[dict[str, Any]] = []

    def takeoff(self, abort: threading.Event | None = None) -> bool:
        ok = super().takeoff(abort=abort)
        if self.clock is not None and self.takeoff_s:
            self.clock.advance(self.takeoff_s)
        self.timeline.append(("takeoff_ok",))
        return ok

    def read_height_cm(self) -> int | None:
        return self.stale_height_cm if self.state.is_flying else 0

    def settled_height(self, *, samples, tolerance_cm, timeout_s, abort=None):
        self.settle_args.append({"samples": samples, "tolerance_cm": tolerance_cm, "timeout_s": timeout_s})
        reading = self.settled_script.pop(0) if self.settled_script else None
        self.timeline.append(("settled", None if reading is None else reading.height_cm))
        return reading

    def move(self, direction: str, distance_cm: int) -> bool:
        command = f"{direction} {distance_cm}"
        self.timeline.append(("move", command))
        queue = self.refusals.get(command) or self.refusals.get(direction)
        if queue:
            reply = queue.pop(0)
            raise CommandRefusedError(f"[{self.name}] '{command}' answered {reply!r} (not retried)",
                                      command=command, reply=reply)
        return super().move(direction, distance_cm)

    def land(self) -> None:
        self.timeline.append(("land",))
        super().land()


def settled(height_cm: int, tof_cm: int | None) -> SettledHeight:
    return SettledHeight(height_cm, tof_cm, 5, 0.3)


def events(log: QuietLogger, name: str) -> list[dict[str, Any]]:
    return [fields for _, event, fields in log.events if event == name]


def land_reasons(log: QuietLogger) -> list[str]:
    return [fields.get("reason") for fields in events(log, "edge_land")]


def moves(drone: GuardedTello) -> list[str]:
    return [entry[1] for entry in drone.timeline if entry[0] == "move"]


def take_off(executor: LaneExecutor, cmd_id: str = "t1") -> None:
    send(executor, takeoff(cmd_id=cmd_id))
    assert wait_for(lambda: not executor.is_busy)


@pytest.fixture
def guarded():
    """``(executor, drone, sent, clock, log)`` on a ``GuardedTello`` at a 50 cm slot; shuts down afterwards."""
    made: list[tuple[LaneExecutor, list[dict]]] = []

    def _make(guards: FlightGuards | None = None, alt_slot_cm: float = 50, **drone_kwargs):
        clock = FakeClock()
        drone = GuardedTello(clock=clock, **drone_kwargs)
        executor, drone, sent, clock, log = make_v2_executor(
            drone, clock, alt_slot_cm=alt_slot_cm, flight_guards=guards or FAST,
        )
        made.append((executor, sent))
        return executor, drone, sent, clock, log

    yield _make
    for executor, sent in made:
        executor.shutdown(timeout=WAIT_S)
        validate_sent(sent)


# ---------------------------------------------------------------------------
# Settled height before the move to the slot
# ---------------------------------------------------------------------------


def test_incident_replay_late_ok_and_stale_height_send_no_down_before_a_settled_read(guarded):
    """The 2026-11-21 timeline: 'ok' 8.8 s late, a stale 100 cm read, then the real height.

    Before the fix the climb read the stale 100 cm and sent 'down 50' (at a
    50 cm slot; 'down 80' at the incident's 20 cm slot) about 100 ms after
    the reply. Now nothing moves until a settled read, and the move comes
    from that read: the drone really hovers at 80 cm with 90 cm below it.
    """
    executor, drone, sent, _clock, log = guarded(settled=[settled(80, 90)], takeoff_s=8.8)
    take_off(executor)
    assert drone.timeline == [("takeoff_ok",), ("settled", 80), ("move", "down 30")]
    assert "down 50" not in moves(drone)
    (climb,) = events(log, "edge_alt_slot_climb")
    assert (climb["height_cm"], climb["tof_cm"], climb["direction"], climb["distance_cm"]) == (80, 90, "down", 30)
    (took,) = events(log, "edge_takeoff")
    assert took["reply_s"] == pytest.approx(8.8)
    assert not events(log, EVENT_TAKEOFF_LATE)
    assert reply_for(sent, "t1")["type"] == "ack"
    # The settle used the configured guard values.
    assert drone.settle_args == [{"samples": 5, "tolerance_cm": 10.0, "timeout_s": 5.0}]


def test_a_drone_that_never_settles_lands_without_any_move(guarded):
    executor, drone, _sent, _clock, log = guarded(settled=[None], takeoff_s=8.8)
    take_off(executor)
    assert moves(drone) == []
    assert drone.timeline[-1] == ("land",)
    (unsettled,) = events(log, EVENT_HEIGHT_UNSETTLED)
    assert unsettled["last_height_cm"] == 100 and unsettled["timeout_s"] == 5.0
    assert land_reasons(log) == ["height_unsettled"]


def test_desk_descent_is_bounded_by_tof_and_lands_instead(guarded):
    """The desk case: h says 100 cm, but only 45 cm of air is below the drone. No 'down' is sent."""
    executor, drone, _sent, _clock, log = guarded(settled=[settled(100, 45)])
    take_off(executor)
    assert moves(drone) == []
    (refused,) = events(log, EVENT_DESCENT_REFUSED)
    assert (refused["height_cm"], refused["tof_cm"], refused["distance_cm"]) == (100, 45, 50)
    assert refused["clearance_cm"] == DESCENT_CLEARANCE_CM == MIN_ALT_SLOT_CM
    assert land_reasons(log) == ["tof_clearance"]


def test_a_descent_that_keeps_the_clearance_is_flown(guarded):
    # 120 cm below the drone: down 50 leaves 70 cm, more than the 50 cm clearance.
    executor, drone, _sent, _clock, log = guarded(settled=[settled(100, 120)])
    take_off(executor)
    assert moves(drone) == ["down 50"]
    assert not events(log, EVENT_DESCENT_REFUSED)


def test_a_descent_exactly_at_the_clearance_is_flown_and_one_cm_more_is_not(guarded):
    executor, drone, _sent, _clock, _log = guarded(settled=[settled(100, 100)])
    take_off(executor)
    assert moves(drone) == ["down 50"]
    executor, drone, _sent, _clock, log = guarded(settled=[settled(100, 99)])
    take_off(executor)
    assert moves(drone) == []
    assert land_reasons(log) == ["tof_clearance"]


def test_a_descent_without_a_usable_distance_is_refused(guarded):
    executor, drone, _sent, _clock, log = guarded(settled=[settled(100, None)])
    take_off(executor)
    assert moves(drone) == []
    (refused,) = events(log, EVENT_DESCENT_REFUSED)
    assert refused["tof_cm"] is None and "no usable downward distance" in refused["detail"]


def test_a_climb_is_not_bounded_by_tof(guarded):
    """A climb is bounded by the ceiling, not the distance below (a low tof over a desk does not stop it)."""
    executor, drone, _sent, _clock, _log = guarded(alt_slot_cm=130, settled=[settled(80, 40)])
    take_off(executor)
    assert moves(drone) == ["up 50"]


def test_a_drone_without_settled_height_keeps_its_single_read():
    """MAVLink and the plain fake have no settled_height: one height read, no tof bound, no late check."""
    executor, drone, _sent, _clock, log = make_v2_executor(alt_slot_cm=50)
    try:
        drone.takeoff_height_cm = 100
        take_off(executor)
        assert ("move", "down", 50) in drone.calls
        assert not events(log, EVENT_HEIGHT_SETTLED)
    finally:
        executor.shutdown(timeout=WAIT_S)


# ---------------------------------------------------------------------------
# Late takeoff reply
# ---------------------------------------------------------------------------


def test_a_takeoff_answered_later_than_the_limit_lands_instead_of_climbing(guarded):
    """The 16 s 'ok' seen on 2026-11-22: land, never move to the slot."""
    executor, drone, sent, _clock, log = guarded(settled=[settled(80, 90)], takeoff_s=16.0)
    take_off(executor)
    assert moves(drone) == []
    assert ("settled", 80) not in drone.timeline
    (late,) = events(log, EVENT_TAKEOFF_LATE)
    assert late["reply_s"] == pytest.approx(16.0) and late["takeoff_late_s"] == 10.0
    assert land_reasons(log) == ["takeoff_late"]
    assert reply_for(sent, "t1")["type"] == "nack"


def test_the_late_limit_is_configurable(guarded):
    executor, drone, _sent, _clock, log = guarded(FlightGuards(takeoff_late_s=3.0), settled=[settled(80, 90)],
                                                  takeoff_s=4.7)
    take_off(executor)
    assert moves(drone) == []
    assert land_reasons(log) == ["takeoff_late"]


# ---------------------------------------------------------------------------
# Transient refusals
# ---------------------------------------------------------------------------


def test_transient_refusal_matches_only_the_two_known_replies():
    assert transient_refusal("error No valid imu")
    assert transient_refusal("  ERROR   not joystick ")
    assert not transient_refusal("error Motor stop")
    assert not transient_refusal("error")
    assert not transient_refusal(None)


def test_a_refused_slot_move_is_retried_from_a_new_settled_height(guarded):
    """'up 60' refused 'error No valid imu' after a late 'ok': hold, settle again, recompute."""
    executor, drone, _sent, _clock, log = guarded(
        alt_slot_cm=130, settled=[settled(70, 80), settled(80, 90)], refusals={"up 60": ["error No valid imu"]},
    )
    take_off(executor)
    # The retry is computed from the new reading (80 cm), not resent as 'up 60'.
    assert drone.timeline == [
        ("takeoff_ok",), ("settled", 70), ("move", "up 60"), ("settled", 80), ("move", "up 50"),
    ]
    (refused,) = events(log, EVENT_MOVE_REFUSED)
    assert (refused["reply"], refused["retry"], refused["retries"], refused["hold_s"]) == ("error No valid imu", 1, 2, 0.5)
    telemetry = executor.telemetry()
    assert telemetry["move_refusals"] == {
        "total": 1, "last_reason": "error No valid imu", "last_cmd": "up 60", "last_at": pytest.approx(1000.0),
    }
    assert "move_refusals_total" not in telemetry  # one grouped field, not several
    fp.validate_edge_to_ground(telemetry, proto=2)
    assert "executor_error" not in land_reasons(log)


def test_refusals_beyond_the_retries_land_and_stay_visible(guarded):
    executor, drone, _sent, _clock, log = guarded(
        settled=[settled(100, 200)] * 3, refusals={"down": ["error No valid imu"] * 5},
    )
    take_off(executor)
    assert moves(drone) == ["down 50", "down 50", "down 50"]
    assert [e["retry"] for e in events(log, EVENT_MOVE_REFUSED)] == [1, 2, None]
    assert land_reasons(log) == ["executor_error"]
    refusals = executor.telemetry()["move_refusals"]
    assert (refusals["total"], refusals["last_cmd"], refusals["last_reason"]) == (3, "down 50", "error No valid imu")
    assert events(log, EVENT_MOVE_REFUSED)[-1]["detail"].endswith("2 retries used, landing")


def test_retries_can_be_turned_off(guarded):
    executor, drone, _sent, _clock, log = guarded(
        FlightGuards(refusal_retries=0, refusal_hold_s=0.5), settled=[settled(100, 200)],
        refusals={"down": ["error Not joystick"]},
    )
    take_off(executor)
    assert moves(drone) == ["down 50"]
    assert land_reasons(log) == ["executor_error"]
    assert executor.telemetry()["move_refusals"]["total"] == 1


def test_any_other_refusal_lands_at_once_and_is_not_counted(guarded):
    executor, drone, _sent, _clock, log = guarded(settled=[settled(100, 200)], refusals={"down": ["error Motor stop"]})
    take_off(executor)
    assert moves(drone) == ["down 50"]
    assert not events(log, EVENT_MOVE_REFUSED)
    assert land_reasons(log) == ["executor_error"]
    assert "move_refusals" not in executor.telemetry()


def test_a_landing_during_the_hold_ends_the_retry(guarded):
    executor, drone, _sent, _clock, log = guarded(
        FlightGuards(refusal_hold_s=5.0), settled=[settled(100, 200)] * 2, refusals={"down": ["error No valid imu"]},
    )
    send(executor, takeoff(cmd_id="t1"))
    assert wait_for(lambda: events(log, EVENT_MOVE_REFUSED))
    started = time.monotonic()
    executor.land_now("ground_land")
    assert wait_for(lambda: not executor.is_busy)
    assert time.monotonic() - started < 3.0
    assert moves(drone) == ["down 50"]


class TimelineSensor(FakeObstacleSensor):
    """A sensor that writes each look to the drone's timeline."""

    def __init__(self, timeline: list, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeline = timeline

    def sense(self, after, timeout_s=2.0):
        self.timeline.append(("sense",))
        return super().sense(after, timeout_s)


def _sensed_executor(timeline: list, refusals: dict[str, list[str]], turn: bool = True):
    clock = FakeClock()
    holder: dict[str, Any] = {}
    sensor = TimelineSensor(timeline, lambda: holder["envelope"].pose_snapshot())
    drone = GuardedTello(clock=clock, settled=[settled(100, 200)], refusals=refusals, timeline=timeline)
    drone.connect()
    envelope = SafetyEnvelope(alt_slot_cm=100, geofence_cm=500, clock=clock)
    holder["envelope"] = envelope
    log = QuietLogger()
    sent: list[dict] = []
    executor = LaneExecutor(drone, envelope, "drone-1", send=sent.append, clock=clock, logger=log, fleet_v2=True,
                            obstacle_sensor=sensor, obstacle_takeoff_probe=False, obstacle_turn=turn,
                            flight_guards=FAST)
    executor.handle_clock({"type": "clock", "offset_s": 0.0})
    return executor, drone, clock, log, sent


def test_a_refused_sensed_lane_step_is_retried_through_a_new_gate():
    """'forward 50' refused outdoors after 3 steps on 2026-11-22: retry only after a fresh look."""
    timeline: list = []
    executor, _drone, clock, log, sent = _sensed_executor(timeline, {"forward 50": ["error No valid imu"]})
    try:
        send(executor, assign(lanes=[{"direction": "forward", "distance_cm": 100}]))
        t = clock() + 0.5
        send(executor, start_at(t))
        clock.set(t)
        assert wait_for(lambda: events(log, "edge_lanes_complete"))
        flown = [entry for entry in timeline if entry[0] in ("sense", "move")]
        assert flown == [
            ("sense",), ("move", "forward 50"),  # refused
            ("sense",), ("move", "forward 50"),  # the retry, after a new look
            ("sense",), ("move", "forward 50"),
        ]
        (refused,) = events(log, EVENT_MOVE_REFUSED)
        assert refused["lane_index"] == 0 and refused["retry"] == 1
        assert executor.telemetry()["move_refusals"]["total"] == 1
    finally:
        executor.shutdown(timeout=WAIT_S)
    validate_sent(sent)


def test_an_unsensed_lane_move_is_never_retried():
    """Without a sensor a lane is one long move that may have been partly flown: it lands, counted."""
    executor, drone, _sent, clock, log = make_v2_executor(
        GuardedTello(settled=[settled(100, 200)], refusals={"forward": ["error No valid imu"]}),
        alt_slot_cm=100, flight_guards=FAST,
    )
    try:
        send(executor, assign(lanes=[{"direction": "forward", "distance_cm": 200}]))
        t = clock() + 0.5
        send(executor, start_at(t))
        clock.set(t)
        assert wait_for(lambda: "executor_error" in land_reasons(log))
        assert moves(drone) == ["forward 200"]
        (refused,) = events(log, EVENT_MOVE_REFUSED)
        assert refused["retry"] is None and "not retried" in refused["detail"]
        assert executor.telemetry()["move_refusals"]["total"] == 1
    finally:
        executor.shutdown(timeout=WAIT_S)


# ---------------------------------------------------------------------------
# Hover-only test
# ---------------------------------------------------------------------------


def test_hover_test_takes_off_and_hovers_with_sensing_required():
    """FLIGHTRISK_HOVER_TEST=1: the start's probe look still runs; no move to the slot and no lane."""
    timeline: list = []
    clock = FakeClock()
    holder: dict[str, Any] = {}
    sensor = TimelineSensor(timeline, lambda: holder["envelope"].pose_snapshot())
    drone = GuardedTello(clock=clock, settled=[settled(80, 90)], timeline=timeline)
    drone.connect()
    envelope = SafetyEnvelope(alt_slot_cm=100, geofence_cm=500, clock=clock)
    holder["envelope"] = envelope
    log = QuietLogger()
    sent: list[dict] = []
    executor = LaneExecutor(drone, envelope, "drone-1", send=sent.append, clock=clock, logger=log, fleet_v2=True,
                            obstacle_sensor=sensor, flight_guards=FlightGuards(hover_test=True))
    executor.handle_clock({"type": "clock", "offset_s": 0.0})
    try:
        send(executor, assign(lanes=[{"direction": "forward", "distance_cm": 200}]))
        t = clock() + 0.5
        send(executor, start_at(t, cmd_id="s1"))
        clock.set(t)
        assert wait_for(lambda: len(events(log, EVENT_HOVER_TEST)) == 2)
        assert wait_for(lambda: not executor.is_busy)
        # The probe look before the takeoff, then only the settle: nothing moves.
        assert timeline == [("sense",), ("takeoff_ok",), ("settled", 80)]
        assert [e["skipped"] for e in events(log, EVENT_HOVER_TEST)] == ["climb", "lanes"]
        assert drone.state.is_flying
        assert reply_for(sent, "s1")["type"] == "ack"
    finally:
        executor.shutdown(timeout=WAIT_S)
    validate_sent(sent)


def test_hover_test_still_lands_a_drone_that_does_not_settle(guarded):
    executor, drone, _sent, _clock, log = guarded(FlightGuards(hover_test=True), settled=[None])
    take_off(executor)
    assert moves(drone) == []
    assert land_reasons(log) == ["height_unsettled"]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_guard_settings_are_read_from_the_environment():
    assert _flight_guards({}) == FlightGuards()
    guards = _flight_guards({ENV_TAKEOFF_LATE_S: "3", ENV_REFUSAL_RETRIES: "0", ENV_SETTLE_SAMPLES: "8",
                             ENV_HOVER_TEST: "1"})
    assert (guards.takeoff_late_s, guards.refusal_retries, guards.settle_samples, guards.hover_test) == (3.0, 0, 8, True)


@pytest.mark.parametrize(
    "environ",
    [{ENV_TAKEOFF_LATE_S: "1"}, {ENV_REFUSAL_RETRIES: "4"}, {ENV_REFUSAL_RETRIES: "1.5"}, {ENV_SETTLE_SAMPLES: "nan"},
     {ENV_HOVER_TEST: "maybe"}],
)
def test_a_bad_guard_setting_refuses_to_start(environ):
    with pytest.raises(SystemExit):
        _flight_guards(environ)


def test_guard_values_are_range_checked():
    with pytest.raises(ValueError):
        FlightGuards(refusal_retries=4)
    with pytest.raises(ValueError):
        FlightGuards(settle_samples=True)


# ---------------------------------------------------------------------------
# The Tello's lowest altitude slot
# ---------------------------------------------------------------------------


def test_a_tello_slot_below_50_cm_is_refused_with_operator_text():
    assert MIN_ALT_SLOT_CM == 50
    with pytest.raises(ValueError, match="altitude slot 20 cm is too low: a Tello's altitude slot must be at least 50 cm"):
        SafetyEnvelope(alt_slot_cm=20)
    with pytest.raises(ValueError, match="downward vision"):
        SafetyEnvelope(alt_slot_cm=49)
    assert SafetyEnvelope(alt_slot_cm=50).alt_slot_cm == 50
    assert slot_below_minimum(50) is None and slot_below_minimum(600) is None
    problem = CeilingSettings(200, 60, {"drone-2": 100}).slot_problem("drone-2", 20)
    assert problem is not None
    assert problem.startswith("drone-2 slot 20 cm is outside 50-500 cm (altitude slot 20 cm is too low")
    with pytest.raises(ValueError, match="too low"):
        _slots([20])


def test_the_launcher_refuses_a_low_slot_with_the_reason():
    from dataclasses import replace

    from flightrisk.launcher.settings import builtin_presets, errors, validate

    rehearsal = builtin_presets()["Rehearsal with fake drones"]
    low = replace(rehearsal, drones=(replace(rehearsal.drones[0], alt_slot_cm=20), *rehearsal.drones[1:]))
    found = [i for i in errors(validate(low, adapters=[])) if i.field == "drones.0.alt_slot_cm"]
    assert found and "too low" in found[0].message and "downward vision positioning" in found[0].message


# ---------------------------------------------------------------------------
# The real Tello controller over the wire
# ---------------------------------------------------------------------------


def _airborne_controller(tello_on_wire, **wire_kwargs):
    ctrl, wire = tello_on_wire(**wire_kwargs)
    ctrl.read_battery = lambda: 78
    ctrl.link_status = lambda: DroneLinkStatus(ok=True, age_s=0.1)
    return ctrl, wire


def test_settled_height_counts_only_packets_after_the_call(tello_on_wire):
    ctrl, wire = _airborne_controller(tello_on_wire)
    wire.tof_cm = 90
    wire.start_state(100)
    time.sleep(0.3)
    wire.stop_state()  # the cached 100 cm is stale from now on
    started = time.monotonic()
    assert ctrl.settled_height(samples=5, tolerance_cm=10, timeout_s=0.6) is None
    assert time.monotonic() - started >= 0.55
    wire.start_state(80)
    reading = ctrl.settled_height(samples=5, tolerance_cm=10, timeout_s=2.0)
    assert reading is not None and (reading.height_cm, reading.tof_cm, reading.samples) == (80, 90, 5)


def test_settled_height_waits_out_a_drift_over_an_edge(tello_on_wire):
    """A tof that jumps (the drone drifting off a desk edge) is not settled."""
    ctrl, wire = _airborne_controller(tello_on_wire)
    wire.tof_cm = 45
    wire.start_state(100)
    flip = threading.Event()

    def drift() -> None:
        while not flip.wait(0.05):
            wire.tof_cm = 160 if wire.tof_cm == 45 else 45

    threading.Thread(target=drift, daemon=True).start()
    try:
        assert ctrl.settled_height(samples=5, tolerance_cm=10, timeout_s=0.8) is None
    finally:
        flip.set()


def test_settled_height_reports_no_tof_for_an_out_of_range_reading(tello_on_wire):
    ctrl, wire = _airborne_controller(tello_on_wire)
    wire.tof_cm = 6553
    wire.start_state(90)
    reading = ctrl.settled_height(samples=5, tolerance_cm=10, timeout_s=2.0)
    assert reading is not None and reading.height_cm == 90 and reading.tof_cm is None


def test_settled_height_stops_on_abort(tello_on_wire):
    ctrl, _wire = _airborne_controller(tello_on_wire)
    abort = threading.Event()
    threading.Timer(0.2, abort.set).start()
    started = time.monotonic()
    assert ctrl.settled_height(samples=5, tolerance_cm=10, timeout_s=5.0, abort=abort) is None
    assert time.monotonic() - started < 1.0


def _wire_executor(ctrl, alt_slot_cm: float = 50):
    sent: list[dict] = []
    log = QuietLogger()
    executor = LaneExecutor(ctrl, SafetyEnvelope(alt_slot_cm=alt_slot_cm), "drone-2", send=sent.append, logger=log,
                            fleet_v2=True)
    executor.handle_clock({"type": "clock", "offset_s": 0.0})
    return executor, sent, log


def test_incident_replay_on_the_wire_no_down_before_a_settled_read(tello_on_wire, monkeypatch):
    """The real controller and the real clock: 'ok' after 8.8 s, a stale cached h of 100 cm, then fresh state.

    The state stream shows h 100 cm while the drone climbs and goes silent
    just before the reply, so the cached height is 100 cm when the 'ok'
    arrives. Fresh state from 1 s later shows the truth: 40 cm, 50 cm above
    the surface below. The old climb would have sent 'down 50' about 100 ms
    after the reply; now nothing is sent: 40 cm is within a move of the slot.
    """
    monkeypatch.setattr(tello_module, "_LAND_DEADLINE_S", 5.0)
    ctrl, wire = _airborne_controller(tello_on_wire, delays={"takeoff": 8.8})
    wire.start_state(0)
    marks: dict[str, float] = {}

    def resume_with_truth() -> None:
        wire.height_cm, wire.tof_cm = 40, 50
        marks["fresh"] = time.monotonic()
        wire.start_state()

    def on_send(command: str) -> None:
        if command == "takeoff":
            marks["takeoff"] = time.monotonic()
            wire.height_cm, wire.tof_cm = 100, 45
            threading.Timer(8.5, wire.stop_state).start()
            threading.Timer(9.8, resume_with_truth).start()
        elif command == "land":
            wire.height_cm, wire.tof_cm = 0, 10

    wire.on_send = on_send
    executor, sent, log = _wire_executor(ctrl)
    try:
        send(executor, takeoff(cmd_id="t1"))
        assert wait_for(lambda: not executor.is_busy, timeout=20.0)
        assert reply_for(sent, "t1")["type"] == "ack"
        assert not [c for c in wire.sent if c.split()[0] in ("down", "up")]
        assert ctrl.read_height_cm() == 40
        (settle,) = events(log, EVENT_HEIGHT_SETTLED)
        assert (settle["height_cm"], settle["tof_cm"]) == (40, 50)
        (took,) = events(log, "edge_takeoff")
        assert took["reply_s"] >= 8.7
    finally:
        executor.shutdown(timeout=WAIT_S)
    validate_sent(sent)


def test_desk_replay_on_the_wire_lands_instead_of_descending(tello_on_wire, monkeypatch):
    """Steady h 100 cm with 45 cm below (over a desk): 'down 50' would leave 0 cm of clearance. It lands."""
    monkeypatch.setattr(tello_module, "_LAND_DEADLINE_S", 5.0)
    ctrl, wire = _airborne_controller(tello_on_wire, delays={"takeoff": 1.0})
    wire.start_state(0)

    def on_send(command: str) -> None:
        if command == "takeoff":
            wire.height_cm, wire.tof_cm = 100, 45
        elif command == "land":
            wire.height_cm, wire.tof_cm = 0, 10

    wire.on_send = on_send
    executor, _sent, log = _wire_executor(ctrl)
    try:
        send(executor, takeoff(cmd_id="t1"))
        assert wait_for(lambda: "land" in wire.sent, timeout=15.0)
        assert not [c for c in wire.sent if c.split()[0] in ("down", "up")]
        assert land_reasons(log)[:1] == ["tof_clearance"]
    finally:
        executor.shutdown(timeout=WAIT_S)


def _takeoff_replies(caplog) -> list[dict]:
    return [dict(r.fr_fields) for r in caplog.records if getattr(r, "fr_event", None) == "tello_takeoff_reply"]


def test_a_reply_to_the_lands_stop_is_not_read_as_the_takeoff_reply(tello_on_wire, monkeypatch, caplog):
    """2026-11-22: LAND ALL during a 16 s takeoff; the stop was answered 'unknown command: stop'.

    That reply was read as the takeoff's refusal and gave a bogus
    executor_error. Now the takeoff ends as interrupted by the landing
    (False, not an exception), the reply is logged as ignored, and the
    takeoff is never sent again.
    """
    monkeypatch.setattr(tello_module, "_LAND_DEADLINE_S", 1.5)
    ctrl, wire = tello_on_wire(hold={"takeoff"})
    wire.script = lambda command: [("unknown command: stop", 0.02)] if command == "stop" else None
    outcome: list[Any] = []

    def take_off_now() -> None:
        try:
            outcome.append(ctrl.takeoff())
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            outcome.append(exc)

    taker = threading.Thread(target=take_off_now, daemon=True)
    with caplog.at_level(logging.INFO, logger="flightrisk.drone.tello"):
        taker.start()
        assert wire.entered.wait(WAIT_S)
        try:
            ctrl.land()
        except RuntimeError:
            pass  # no state stream: the land is not confirmed here, and is retried by its callers
        taker.join(WAIT_S)
    assert outcome == [False]
    assert wire.sent.count("takeoff") == 1
    assert wire.sent[1:3] == ["stop", "land"]
    replies = _takeoff_replies(caplog)
    assert [(r["reply"], r["verdict"]) for r in replies] == [("unknown command: stop", "ignored_landing")]
    # The drone may still lift: until a land is confirmed it counts as flying.
    assert ctrl.state.is_flying is True


def test_an_unknown_command_reply_naming_a_command_is_stray():
    assert tello_module._stray_reply("unknown command: stop")
    assert tello_module._stray_reply("unknown command: sdk?")
    assert not tello_module._stray_reply("error Motor stop")


@pytest.mark.parametrize(
    "reply",
    ["85~88C", "50~53c", " -2 ~ 3 C ", "78", "100.0", "10dm", "100mm", "12s", "220.123456",
     "pitch:0;roll:0;yaw:-2;", "agx:-3.00;agy:1.00;agz:-1000.00;"],
)
def test_query_shaped_replies_are_recognised(reply):
    assert tello_module._query_reply(reply)
    assert tello_module._stray_reply(reply)


@pytest.mark.parametrize(
    "reply", ["ok", "OK", "error", "error No valid imu", "error Not joystick", "error Motor stop",
              "unknown command: stop", "0TQDG12345", "85~88C ok", "~C", ""],
)
def test_flight_answers_are_not_query_shaped(reply):
    assert not tello_module._query_reply(reply)


def test_a_late_temperature_reply_is_not_the_takeoffs_answer(tello_on_wire, caplog):
    """A ``temp?`` answered after its own wait ended ("85~88C") is skipped; the takeoff's own 'ok' decides."""
    ctrl, wire = tello_on_wire()
    wire.script = lambda command: [("85~88C", 0.02), ("ok", 0.3)] if command == "takeoff" else None
    with caplog.at_level(logging.INFO, logger="flightrisk.drone.tello"):
        assert ctrl.takeoff() is True
    assert [(r["reply"], r["verdict"]) for r in _takeoff_replies(caplog)] == [
        ("85~88C", "skipped_stray"), ("ok", "accepted"),
    ]


def test_a_late_query_reply_is_not_a_moves_answer(tello_on_wire, caplog):
    ctrl, wire = tello_on_wire()
    assert ctrl.takeoff() is True
    wire.script = lambda command: [("85~88C", 0.02), ("ok", 0.2)] if command == "forward 50" else None
    with caplog.at_level(logging.INFO, logger="flightrisk.drone.tello"):
        assert ctrl.move("forward", 50) is True
    skipped = [dict(r.fr_fields) for r in caplog.records if getattr(r, "fr_event", None) == "tello_reply_skipped"]
    assert [(s["command"], s["reply"]) for s in skipped] == [("forward 50", "85~88C")]
    # The move's own refusal still raises, after a skipped battery answer.
    wire.script = lambda command: [("78", 0.02), ("error No valid imu", 0.1)] if command == "down 60" else None
    with pytest.raises(CommandRefusedError) as raised:
        ctrl.move("down", 60)
    assert raised.value.reply == "error No valid imu"


def test_a_refused_move_raises_command_refused_error_with_the_reply(tello_on_wire):
    ctrl, wire = tello_on_wire()
    assert ctrl.takeoff() is True
    wire.script = lambda command: [("error No valid imu", 0.02)] if command.startswith("down") else None
    with pytest.raises(CommandRefusedError) as raised:
        ctrl.move("down", 60)
    assert (raised.value.command, raised.value.reply) == ("down 60", "error No valid imu")
    assert "not retried" in str(raised.value)


# ---------------------------------------------------------------------------
# Telemetry and the drone card
# ---------------------------------------------------------------------------


def test_refusal_telemetry_is_validated():
    base = {"type": "telemetry", "drone_id": "drone-2", "battery": 78, "height_cm": 80, "is_flying": True,
            "rel_x_cm": 0.0, "rel_y_cm": 0.0, "heading_deg": 0.0, "lane_index": 0, "lanes_total": 1,
            "link_age_s": 0.1, "takeoff_at_local_s": 1.0}
    good = {"total": 2, "last_reason": "error No valid imu", "last_cmd": "down 30", "last_at": 1234.5}
    fp.validate_edge_to_ground({**base, "move_refusals": good}, proto=1)
    fp.validate_edge_to_ground({**base, "move_refusals": {"total": 1}}, proto=1)
    for bad in (5, {"total": 0}, {"total": True}, {**good, "last_reason": 5}, {**good, "last_cmd": ""},
                {**good, "last_at": "now"}, {**good, "last_reason": "error " * 40}):
        with pytest.raises(fp.ProtocolError, match="move_refusals"):
            fp.validate_edge_to_ground({**base, "move_refusals": bad}, proto=1)


NODE = shutil.which("node")
UI_JS = Path(__file__).resolve().parents[1] / "flightrisk" / "dashboard" / "static" / "ui" / "ui.js"


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_drone_card_shows_refused_moves(tmp_path):
    js = UI_JS.read_text()
    block = js[js.index("// BEGIN obstacle-display"):js.index("// END obstacle-display")]
    harness = tmp_path / "h.js"
    harness.write_text(
        "const src = require('fs').readFileSync(process.argv[2], 'utf8');\n"
        "const f = new Function('fmt', src + '; return {refusalBadge};')((v) => v);\n"
        "console.log(JSON.stringify([f.refusalBadge({}), f.refusalBadge({move_refusals: {total: 1}}),\n"
        "  f.refusalBadge({move_refusals: {total: 3, last_cmd: 'down 30', last_reason: 'error No valid imu'}})]));\n"
    )
    src = tmp_path / "block.js"
    src.write_text(block)
    out = subprocess.run([NODE, str(harness), str(src)], capture_output=True, text=True, check=True)
    none, one, three = json.loads(out.stdout)
    assert none is None
    assert one["text"] == "DRONE REFUSED 1 move" and one["cls"] == "held"
    assert three["text"] == "DRONE REFUSED 3 moves"
    assert three["title"].startswith('Last: down 30 answered "error No valid imu".')
