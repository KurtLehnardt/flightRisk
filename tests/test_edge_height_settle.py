"""The slot climb waits for a steady height that agrees with the time-of-flight sensor.

On 2026-10-28 two Tellos read their height as 130 cm and 20 cm in the instant after the takeoff reply
(both were about 80 cm up), and one flew ``up 110`` towards the ceiling. The climb now needs steady
readings that agree with ``read_tof_cm`` and lands when they never settle. A controller without
``read_tof_cm`` (the fake, MAVLink) keeps its single reading.
"""
from __future__ import annotations

import types

import pytest

from flightrisk.drone import lane_flight
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController, TelloController
from tests.lane_executor_support import FakeClock, QuietLogger

WAIT_S = 10.0


@pytest.fixture(autouse=True)
def quick_settling(monkeypatch):
    monkeypatch.setattr(lane_flight, "_HEIGHT_SAMPLE_S", 0.002)
    monkeypatch.setattr(lane_flight, "_HEIGHT_SETTLE_TIMEOUT_S", 0.04)  # 21 tries


@pytest.fixture
def make_executor():
    created: list[LaneExecutor] = []

    def _make(*, alt_slot_cm=100, heights=None, tof=None):
        clock = FakeClock()
        drone = FakeTelloController(name="drone-1", battery=90, takeoff_height_cm=80)
        drone.connect()
        if tof is not None:
            drone.read_tof_cm = tof if callable(tof) else (lambda: tof)
        if heights is not None:
            it = iter(heights)
            last = [heights[-1]]

            def read_height():
                try:
                    last[0] = next(it)
                except StopIteration:
                    pass
                return last[0]

            drone.read_height_cm = read_height
        envelope = SafetyEnvelope(alt_slot_cm=alt_slot_cm, clock=clock, ceiling_cm=200, ceiling_margin_cm=60)
        log = QuietLogger()
        executor = LaneExecutor(drone, envelope, "drone-1", send=lambda m: None, clock=clock, logger=log)
        created.append(executor)
        return executor, drone, log

    yield _make
    for executor in created:
        executor.shutdown(timeout=2.0)


def events(log, name):
    return [fields for _, event, fields in log.events if event == name]


def test_steady_agreeing_readings_climb_from_the_settled_height(make_executor):
    executor, drone, _ = make_executor(alt_slot_cm=100, heights=[80, 80, 80, 80], tof=82)
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == [("takeoff",), ("move", "up", 20)]


def test_a_stale_low_height_that_never_agrees_with_tof_lands_and_does_not_climb(make_executor):
    # The 2026-10-28 case: the Tello read 20 cm while the drone was at about 80 cm.
    executor, drone, log = make_executor(alt_slot_cm=130, heights=[20], tof=80)
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert not any(c[:1] == ("move",) for c in drone.calls)
    assert drone.calls[-1] == ("land",)
    assert events(log, "edge_altitude_unsettled")[0]["heights_cm"][-1] == 20
    assert ("warning", "edge_land", {"drone_id": "drone-1", "reason": "height_unsettled"}) in log.events


def test_a_stale_high_height_that_never_agrees_with_tof_lands_and_does_not_descend(make_executor):
    executor, drone, log = make_executor(alt_slot_cm=100, heights=[130], tof=80)
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert not any(c[:1] == ("move",) for c in drone.calls)
    assert drone.calls[-1] == ("land",)
    assert events(log, "edge_altitude_unsettled")


def test_a_height_that_settles_after_a_stale_start_climbs_from_the_settled_value(make_executor):
    executor, drone, log = make_executor(alt_slot_cm=100, heights=[20, 45, 70, 80, 80, 80], tof=82)
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == [("takeoff",), ("move", "up", 20)]
    assert not events(log, "edge_altitude_unsettled")


def test_unsteady_readings_are_not_trusted_even_when_they_average_to_tof(make_executor):
    executor, drone, log = make_executor(alt_slot_cm=100, heights=[40, 120, 40, 120], tof=80)
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert not any(c[:1] == ("move",) for c in drone.calls)
    assert events(log, "edge_altitude_unsettled")


def test_missing_readings_count_as_unsettled(make_executor):
    executor, drone, log = make_executor(alt_slot_cm=100, heights=[80], tof=lambda: None)
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls[-1] == ("land",)
    assert events(log, "edge_altitude_unsettled")


def test_a_controller_without_a_tof_reader_keeps_its_single_reading(make_executor):
    executor, drone, log = make_executor(alt_slot_cm=100)
    assert not hasattr(drone, "read_tof_cm")
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == [("takeoff",), ("move", "up", 20)]
    assert not events(log, "edge_altitude_unsettled")


def test_tello_controller_reads_tof_from_the_state_and_none_when_unknown():
    ctrl = TelloController.__new__(TelloController)
    ctrl.tello = types.SimpleNamespace(get_distance_tof=lambda: 85)
    assert ctrl.read_tof_cm() == 85
    ctrl.tello = types.SimpleNamespace(get_distance_tof=lambda: 0)
    assert ctrl.read_tof_cm() is None

    def broken():
        raise KeyError("tof")

    ctrl.tello = types.SimpleNamespace(get_distance_tof=broken)
    assert ctrl.read_tof_cm() is None


def test_skipping_the_slot_climb_flies_from_the_hover_height_without_reading_sensors(make_executor):
    # Two disagreeing sensors would land the drone; the operator opted out of the climb for a test.
    executor, drone, log = make_executor(alt_slot_cm=130, heights=[20], tof=80)
    executor.skip_slot_climb = True
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == [("takeoff",)]
    assert not events(log, "edge_altitude_unsettled")
    assert events(log, "edge_alt_slot_climb_skipped")[0]["reason"] == "operator"


def test_the_slot_climb_stays_on_by_default(make_executor):
    executor, _drone, _ = make_executor(alt_slot_cm=100)
    assert getattr(executor, "skip_slot_climb", False) is False


def test_the_no_slot_climb_flag_and_environment_variable():
    from flightrisk import edge_device

    parser = edge_device._build_parser()
    assert edge_device._slot_climb_disabled(parser.parse_args(["--source", "fake"]), {}) is False
    assert edge_device._slot_climb_disabled(parser.parse_args(["--source", "fake", "--no-slot-climb"]), {}) is True
    assert edge_device._slot_climb_disabled(parser.parse_args(["--source", "fake"]), {"FLIGHTRISK_NO_SLOT_CLIMB": "1"}) is True
    assert edge_device._slot_climb_disabled(parser.parse_args(["--source", "fake"]), {"FLIGHTRISK_NO_SLOT_CLIMB": "0"}) is False
