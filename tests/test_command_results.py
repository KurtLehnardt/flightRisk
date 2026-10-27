"""The command-result convention, applied by every caller of a drone command.

Backends may return a ``CommandResult`` or the legacy ``True``/``None``
(executed) and ``False`` (not executed). Every caller normalizes the return
value with ``command_result()`` (or ``require_executed()`` where the next step
assumes the command took effect) and acts on ``.ok``. These tests drive the
callers through the new interface, with ``FakeDrone`` (the ``sim`` backend)
returning results that were not executed, and check that:

* a dropped move is never recorded in the lane executor's dead-reckoned pose,
  and a refused takeoff is never logged or reported as a takeoff (H2);
* a land that was not executed is never counted as a landing by the confirm
  recovery, the critical-battery path or LAND ALL (H1);
* the edge device's ``FakeTelloController`` (``--source fake``) is still a
  ``DroneController`` whose telemetry adapts through the state fallback (M4).
"""

from __future__ import annotations

import threading
import types
from dataclasses import replace

import pytest

from flightrisk.drone.controller import (
    CommandNotExecutedError,
    CommandOutcome,
    CommandResult,
    DroneController,
)
from flightrisk.drone.fake import FakeDrone, FakeDroneConfig
from flightrisk.drone.fleet import DroneFleet
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.snapshot import legacy_telemetry_payload, read_telemetry
from flightrisk.drone.tello import FakeTelloController
from flightrisk.observability import StructuredLogger

WAIT_S = 10.0

LANES = [
    {"direction": "forward", "distance_cm": 200},
    {"direction": "right", "distance_cm": 100},
]


def _flying(config: FakeDroneConfig | None = None, cls=FakeDrone, name: str = "drone-1") -> FakeDrone:
    drone = cls(name, config=config or FakeDroneConfig())
    drone.connect()
    return drone


class _DropsLandOnce(FakeDrone):
    """Drops its first land as busy (breaking the land contract), then lands."""

    def land(self):
        if not getattr(self, "_dropped_once", False):
            self._dropped_once = True
            self.calls.append(("land",))
            return CommandResult("land", CommandOutcome.DROPPED_BUSY, "another command in flight")
        return super().land()


class _DropsSecondMove(FakeDrone):
    """Flies the first move, drops the second as busy, flies the rest."""

    def move(self, direction, distance_cm):
        self._moves_seen = getattr(self, "_moves_seen", 0) + 1
        if self._moves_seen == 2:
            self.calls.append(("move", direction, distance_cm))
            return CommandResult("move", CommandOutcome.DROPPED_BUSY, "another command in flight")
        return super().move(direction, distance_cm)


# ---------------------------------------------------------------------------
# Lane executor (H2)
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, start: float = 1000.0):
        self._t = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._t

    def set(self, t: float) -> None:
        with self._lock:
            self._t = t


@pytest.fixture
def run_lanes():
    """Fly ``LANES`` on ``drone`` through a LaneExecutor; return (executor, sent)."""
    created: list[LaneExecutor] = []

    def _run(drone):
        clock = _Clock()
        # The simulated drone takes off to 100 cm, its altitude slot, so the
        # only moves are the lane moves.
        envelope = SafetyEnvelope(alt_slot_cm=100, clock=clock)
        sent: list[dict] = []
        executor = LaneExecutor(
            drone, envelope, "drone-1", send=sent.append, clock=clock,
            logger=StructuredLogger(component="test-command-results"),
        )
        created.append(executor)
        executor.handle_clock({"type": "clock", "offset_s": 0.0})
        assert executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": LANES})
        t_start = clock() + 1.0
        assert executor.handle_start_at({"type": "start_at", "t_ground_s": t_start})
        clock.set(t_start)
        assert executor.wait_idle(WAIT_S)
        return executor, sent

    yield _run
    for executor in created:
        executor.shutdown(timeout=2.0)


def _start_acks(sent):
    return [m for m in sent if m.get("type") == "ack" and m.get("for") == "start_at"]


def test_lanes_fly_and_record_with_the_sim_backend(run_lanes):
    """Baseline: OK results are recorded exactly like the legacy True/None."""
    drone = _flying()
    executor, sent = run_lanes(drone)
    pose = executor.envelope.pose_snapshot()
    assert (pose.x_cm, pose.y_cm) == (pytest.approx(100), pytest.approx(200))
    assert executor.lane_index == executor.lanes_total == 2
    assert len(_start_acks(sent)) == 1


def test_dropped_first_move_is_not_recorded(run_lanes):
    drone = _flying(FakeDroneConfig(drop_commands=frozenset({"move"})))
    executor, _ = run_lanes(drone)
    assert ("move", "forward", 200) in drone.calls  # sent to the drone, which dropped it
    pose = executor.envelope.pose_snapshot()
    assert (pose.x_cm, pose.y_cm) == (0.0, 0.0)  # never dead-reckoned
    assert executor.lane_index == 0  # the lane was not advanced past
    # A move the controller did not fly ends the flight with a landing.
    assert drone.calls[-1] == ("land",)
    assert drone.state.is_flying is False


def test_dropped_move_mid_plan_is_not_recorded(run_lanes):
    drone = _flying(cls=_DropsSecondMove)
    executor, _ = run_lanes(drone)
    pose = executor.envelope.pose_snapshot()
    # Only the first lane (forward 200) was flown; the dropped "right 100"
    # is not in the pose.
    assert (pose.x_cm, pose.y_cm) == (pytest.approx(0), pytest.approx(200))
    assert executor.lane_index == 1
    assert drone.calls[-1] == ("land",)


def test_refused_takeoff_is_not_recorded_as_taken_off(run_lanes):
    # A takeoff that was not executed (a contract-breaking drop, injected).
    drone = _flying(FakeDroneConfig(drop_commands=frozenset({"takeoff"})))
    executor, sent = run_lanes(drone)
    assert drone.state.is_flying is False
    assert executor.telemetry()["takeoff_at_local_s"] is None
    assert _start_acks(sent) == []  # the start is never acknowledged as executed
    assert [c for c in drone.calls if c[0] == "move"] == []


def test_takeoff_refused_by_abort_is_not_executed():
    drone = _flying()
    aborted = threading.Event()
    aborted.set()
    result = drone.takeoff(abort=aborted)
    assert result.outcome is CommandOutcome.NOT_EXECUTED
    assert drone.state.is_flying is False


def test_land_not_executed_is_logged_and_retried_by_the_monitor():
    drone = _flying(cls=_DropsLandOnce)
    drone.takeoff()
    clock = _Clock()
    log = types.SimpleNamespace(events=[])

    class _Log(StructuredLogger):
        def _emit(self, level, event, **kwargs):
            log.events.append((level, event, kwargs))

    executor = LaneExecutor(
        drone, SafetyEnvelope(alt_slot_cm=100, clock=clock), "drone-1", send=lambda m: None,
        clock=clock, logger=_Log(component="t"), land_retry_s=2.0,
    )
    try:
        executor.land_now("operator")
        assert drone.state.is_flying is True
        assert any(event == "edge_land_not_executed" for _, event, _ in log.events)
        # The monitor retries a land that did not take while the drone flies.
        clock.set(clock() + 2.0)
        executor.tick()
        assert drone.state.is_flying is False
        assert [c for c in drone.calls if c == ("land",)] == [("land",), ("land",)]
    finally:
        executor.shutdown(timeout=2.0)


# ---------------------------------------------------------------------------
# Confirm recovery (H1)
# ---------------------------------------------------------------------------


def _recover(drone):
    from flightrisk.dashboard.recovery import run_confirm_recovery

    statuses = []
    outcome = run_confirm_recovery(drone, emit=lambda event, data: statuses.append(data))
    return outcome, statuses


@pytest.mark.parametrize("supports_rth", [True, False])
def test_recovery_never_reports_landed_for_a_dropped_land(supports_rth):
    drone = _flying(FakeDroneConfig(drop_commands=frozenset({"land"}), supports_return_to_home=supports_rth))
    drone.takeoff()
    outcome, statuses = _recover(drone)
    assert outcome == "failsafe_land_failed"
    assert drone.snapshot().is_flying is True
    assert not any(s["stage"] == "landed" for s in statuses)
    assert statuses[-1]["stage"] == "error"
    assert "manual control" in statuses[-1]["message"]
    # The land in place and the fail-safe land were both tried.
    assert [c for c in drone.calls if c == ("land",)] == [("land",), ("land",)]


def test_recovery_fail_safe_land_after_a_dropped_land():
    drone = _flying(FakeDroneConfig(supports_return_to_home=False), cls=_DropsLandOnce)
    drone.takeoff()
    outcome, statuses = _recover(drone)
    assert outcome == "failsafe_landed"
    assert drone.snapshot().is_flying is False
    assert statuses[-1] == {"stage": "landed", "message": "Landed in place (fail-safe)."}


# ---------------------------------------------------------------------------
# Critical-battery land in the frame loop (H1)
# ---------------------------------------------------------------------------


def test_critical_battery_land_not_executed_is_retried(clean_app_state, monkeypatch):
    from flightrisk.dashboard import pipeline

    now = [500.0]
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: now[0])
    events = []
    socketio = types.SimpleNamespace(emit=lambda e, d=None, *a, **k: events.append((e, d)))
    drone = _flying(FakeDroneConfig(drop_commands=frozenset({"land"})))
    drone.takeoff()
    drone.set_telemetry(battery_pct=8.0)
    pipeline.app_state.battery_critical = False
    pipeline.app_state.battery_critical_retry_at = 0.0

    def frame():
        pipeline._apply_battery_policy(socketio, drone, drone.snapshot(), 8.0)

    frame()
    assert pipeline.app_state.battery_critical is False  # not landed: no critical flag
    assert [e for e, _ in events] == ["battery_critical", "error"]
    assert "not executed" in events[-1][1]["message"]

    def lands():
        return len([c for c in drone.calls if c == ("land",)])

    assert lands() == 1

    frame()  # the next frame, inside the retry interval: nothing is sent
    assert lands() == 1

    now[0] += pipeline.CRITICAL_LAND_RETRY_S
    drone.config = replace(drone.config, drop_commands=frozenset())
    frame()
    assert lands() == 2
    assert pipeline.app_state.battery_critical is True
    assert drone.snapshot().is_flying is False
    # The operator was told about the critical battery once.
    assert [e for e, _ in events].count("battery_critical") == 1


# ---------------------------------------------------------------------------
# LAND ALL and the fleet broadcast (H1)
# ---------------------------------------------------------------------------


def _sim_fleet(config: FakeDroneConfig) -> DroneFleet:
    fleet = DroneFleet(factory=lambda name, host: FakeDrone(name, host, config=config))
    assert fleet.register("drone-1", host="sim://1")
    fleet.get("drone-1").takeoff()
    return fleet


def test_broadcast_reports_a_dropped_land_as_a_failure():
    fleet = _sim_fleet(FakeDroneConfig(drop_commands=frozenset({"land"})))
    results = fleet.broadcast_command("land")
    assert isinstance(results["drone-1"], CommandNotExecutedError)
    assert results["drone-1"].result.outcome is CommandOutcome.DROPPED_BUSY

    ok = _sim_fleet(FakeDroneConfig())
    assert ok.broadcast_command("land") == {"drone-1": None}


def test_land_all_does_not_count_a_dropped_land_as_reached(clean_app_state, monkeypatch):
    from flightrisk.dashboard import app as app_module
    from flightrisk.dashboard.state import app_state

    emitted = []
    monkeypatch.setattr(app_module, "socketio", types.SimpleNamespace(emit=lambda e, d=None, **k: emitted.append((e, d))))
    app_state.fleet_supervisor = None
    app_state.ground_transport = None
    app_state.logger = None
    app_state.fleet = _sim_fleet(FakeDroneConfig(drop_commands=frozenset({"land"})))

    report = app_module._land_all()
    assert report == {"coordinator": None, "transport": None, "local": 0, "reached_any": False}
    assert emitted and emitted[-1][0] == "error"


# ---------------------------------------------------------------------------
# The two simulated drones (M4)
# ---------------------------------------------------------------------------


def test_edge_fake_tello_still_satisfies_the_drone_controller_protocol():
    drone = FakeTelloController(name="drone-2", battery=77)
    assert isinstance(drone, DroneController)
    drone.connect()
    assert drone.takeoff() is True
    snap = read_telemetry(drone)  # no snapshot(): adapted from its state cache
    assert snap.drone_id == "drone-2"
    assert snap.battery_pct.value == 77 and snap.is_flying is True
    assert legacy_telemetry_payload(snap, include_connected=True) == {
        "battery": drone.state.battery,
        "height": drone.state.height,
        "temperature": drone.state.temperature,
        "flight_time": drone.state.flight_time,
        "is_flying": drone.state.is_flying,
        "is_connected": drone.state.is_connected,
    }


def test_edge_source_fake_still_builds_the_fake_tello():
    from flightrisk.edge_device import _build_drone, _build_parser

    args = _build_parser().parse_args(["--source", "fake", "--drone-id", "drone-2"])
    assert isinstance(_build_drone(args, StructuredLogger(component="test-command-results")), FakeTelloController)


def test_sim_backend_is_not_named_fake():
    from flightrisk.drone import registry

    assert "sim" in registry.names()
    assert "fake" not in registry.names()
    assert isinstance(registry.get("sim").create("s1", "sim://1", {}), FakeDrone)
