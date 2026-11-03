"""Onboard obstacle avoidance on the MAVLink path: preflight, capability, STOPPED_SHORT.

Runs against the simulated MAVSDK of ``tests.support.fake_mavsdk``. These
tests check the edge's own logic; how a real ArduCopter behaves under
BendyRuler has not been flown.
"""
from __future__ import annotations

import re
import threading
import time

import pytest

from flightrisk.drone.autopilot import (
    ARDUPILOT,
    AVOIDANCE_PARAMS,
    PX4,
    avoidance_problems,
)
from flightrisk.drone.controller import Capability, CommandOutcome, command_result
from flightrisk.drone.mavlink_fleet import (
    SENSING_NONE,
    MavlinkFleetDrone,
    MavlinkFleetOptions,
)
from flightrisk.drone.mavsdk_link import MavsdkAutopilotLink
from flightrisk.drone.obstacle_sensor import SENSING_OFF, SENSING_STEPPED
from tests.support.fake_mavsdk import SimVehicle, install

WAIT_S = 10.0

#: A configured ArduCopter: BendyRuler, a proximity source, 3 m margin, 15 m lookahead.
AVOIDING = {"OA_TYPE": 1, "PRX1_TYPE": 5, "OA_MARGIN_MAX": 3.0, "OA_BR_LOOKAHEAD": 15.0}


def wait_for(predicate, timeout=WAIT_S, interval=0.02):
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result or time.monotonic() >= deadline:
            return result
        time.sleep(interval)


@pytest.fixture
def make_drone(monkeypatch):
    built = []

    def _make(vehicle=None, avoidance=None, **options):
        vehicle = vehicle or SimVehicle(ARDUPILOT, time_scale=20.0)
        if avoidance is not None:
            vehicle.params.update(avoidance)
        install(monkeypatch, vehicle)
        drone = MavlinkFleetDrone("drone-1", MavsdkAutopilotLink("tcpout://127.0.0.1:1"), MavlinkFleetOptions(**options))
        drone.bind_slot_source(lambda: 1500.0)
        assert drone.connect() is True
        built.append(drone)
        return drone, vehicle

    yield _make
    for drone in built:
        drone.disconnect()


def avoidance_known(drone):
    return wait_for(lambda: drone.avoidance_status() != ["avoidance parameters not read yet"])


def airborne(drone, vehicle):
    assert wait_for(lambda: drone.link_status().ok), drone.link_status()
    assert command_result(drone.takeoff(), "takeoff").ok
    assert vehicle.in_air


# ---------------------------------------------------------------------------
# avoidance_problems
# ---------------------------------------------------------------------------


def test_param_names_per_autopilot():
    assert AVOIDANCE_PARAMS[ARDUPILOT] == ("OA_TYPE", "PRX1_TYPE", "OA_MARGIN_MAX", "OA_BR_LOOKAHEAD")
    assert AVOIDANCE_PARAMS[PX4] == ()


def test_ardupilot_reference_settings_pass():
    assert avoidance_problems(AVOIDING, ARDUPILOT) == []


@pytest.mark.parametrize("oa_type", [1, 2, 3])
def test_ardupilot_accepts_each_planner(oa_type):
    values = dict(AVOIDING, OA_TYPE=oa_type)
    if oa_type == 2:
        values.pop("OA_BR_LOOKAHEAD")  # Dijkstra alone does not use the BendyRuler lookahead
    assert avoidance_problems(values, ARDUPILOT) == []


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"OA_TYPE": 0}, "OA_TYPE=0"),
        ({"OA_TYPE": 4}, "OA_TYPE=4"),
        ({"OA_TYPE": None}, "cannot read OA_TYPE"),
        ({"PRX1_TYPE": 0}, "PRX1_TYPE=0"),
        ({"PRX1_TYPE": None}, "cannot read PRX1_TYPE"),
        ({"OA_MARGIN_MAX": 1.5}, "OA_MARGIN_MAX=1.5"),
        ({"OA_MARGIN_MAX": None}, "cannot read OA_MARGIN_MAX"),
        ({"OA_BR_LOOKAHEAD": 4.0}, "OA_BR_LOOKAHEAD=4"),
        ({"OA_BR_LOOKAHEAD": None}, "cannot read OA_BR_LOOKAHEAD"),
    ],
)
def test_ardupilot_refuses_unsafe_or_unreadable_values(change, needle):
    problems = avoidance_problems(dict(AVOIDING, **change), ARDUPILOT)
    assert any(needle in p for p in problems), problems


def test_lookahead_is_only_checked_with_bendyruler():
    values = dict(AVOIDING, OA_TYPE=2, OA_BR_LOOKAHEAD=1.0)
    assert avoidance_problems(values, ARDUPILOT) == []
    values["OA_TYPE"] = 3
    assert any("OA_BR_LOOKAHEAD" in p for p in avoidance_problems(values, ARDUPILOT))


def test_missing_parameters_fail_closed():
    assert len(avoidance_problems({}, ARDUPILOT)) == 3  # no lookahead check without a readable OA_TYPE


def test_px4_is_never_available_and_says_why():
    problems = avoidance_problems({"CP_DIST": 5.0}, PX4)
    assert len(problems) == 1 and "Position mode" in problems[0]


def test_unknown_autopilot_fails_closed():
    assert avoidance_problems(AVOIDING, None)
    assert avoidance_problems(AVOIDING, "betaflight")


# ---------------------------------------------------------------------------
# Capability and status after a parameter read
# ---------------------------------------------------------------------------


def test_status_before_any_read_is_not_available():
    drone = MavlinkFleetDrone("d", object())  # type: ignore[arg-type]  # never connected: no parameter read
    assert drone.avoidance_status() == ["avoidance parameters not read yet"]
    assert not drone.capabilities.supports(Capability.ONBOARD_AVOIDANCE)


def test_capability_appears_after_a_read_with_avoidance_configured(make_drone):
    drone, _ = make_drone(avoidance=AVOIDING)
    assert avoidance_known(drone)
    assert drone.avoidance_status() == []
    assert drone.capabilities.supports(Capability.ONBOARD_AVOIDANCE)
    assert drone.capabilities.supports(Capability.RELATIVE_MOVE)  # the other features stay


def test_no_capability_when_avoidance_is_not_configured(make_drone):
    drone, _ = make_drone()
    assert avoidance_known(drone)
    problems = drone.avoidance_status()
    assert problems and any("OA_TYPE" in p for p in problems)
    assert not drone.capabilities.supports(Capability.ONBOARD_AVOIDANCE)


def test_capability_follows_a_later_read(make_drone):
    drone, vehicle = make_drone(avoidance=AVOIDING)
    assert avoidance_known(drone)
    vehicle.params["OA_TYPE"] = 0
    drone.refresh_params()
    assert not drone.capabilities.supports(Capability.ONBOARD_AVOIDANCE)
    assert any("OA_TYPE=0" in p for p in drone.avoidance_status())
    vehicle.params["OA_TYPE"] = 3
    drone.refresh_params()
    assert drone.capabilities.supports(Capability.ONBOARD_AVOIDANCE)


def test_px4_never_gets_the_capability(make_drone):
    drone, _ = make_drone(SimVehicle(PX4, time_scale=20.0))
    assert avoidance_known(drone)
    assert "Position mode" in drone.avoidance_status()[0]
    assert not drone.capabilities.supports(Capability.ONBOARD_AVOIDANCE)


# ---------------------------------------------------------------------------
# STOPPED_SHORT
# ---------------------------------------------------------------------------


def _block(vehicle):
    """The vehicle no longer makes progress toward its target (an obstacle in the way)."""
    vehicle.max_speed = vehicle.speed = 0.0


def test_a_missed_deadline_with_avoidance_returns_stopped_short(make_drone):
    drone, vehicle = make_drone(avoidance=AVOIDING, move_timeout_margin_s=0.5, stationary_s=0.3)
    airborne(drone, vehicle)
    assert avoidance_known(drone) and drone.capabilities.supports(Capability.ONBOARD_AVOIDANCE)
    _block(vehicle)
    result = drone.move("forward", 1000)
    assert result.outcome is CommandOutcome.STOPPED_SHORT
    assert result.command == "move"
    assert "x=0 cm, y=0 cm" in result.detail and "plan frame" in result.detail
    # The commanded target is now where the vehicle stopped, so the next move starts from there.
    pose = drone.measured_pose()
    assert pose is not None and abs(pose.y_cm) < 50
    assert vehicle.commands[-1][0] == "goto", "a hold at the stopped position is re-sent"
    vehicle.max_speed = vehicle.speed = 5.0
    assert drone.move("forward", 300).outcome is CommandOutcome.OK


def test_stopped_short_reports_the_measured_pose_not_the_target(make_drone):
    drone, vehicle = make_drone(avoidance=AVOIDING, move_timeout_margin_s=0.5, stationary_s=0.3)
    airborne(drone, vehicle)
    assert avoidance_known(drone)
    assert drone.move("forward", 400).ok
    _block(vehicle)
    result = drone.move("forward", 1000)
    assert result.outcome is CommandOutcome.STOPPED_SHORT
    y_cm = int(re.search(r"y=(-?\d+) cm", result.detail).group(1))
    assert 300 <= y_cm <= 500, result.detail


def test_a_missed_deadline_without_avoidance_still_raises(make_drone):
    drone, vehicle = make_drone(move_timeout_margin_s=0.5, stationary_s=0.3)
    airborne(drone, vehicle)
    assert avoidance_known(drone) and not drone.capabilities.supports(Capability.ONBOARD_AVOIDANCE)
    _block(vehicle)
    with pytest.raises(TimeoutError, match="not reached"):
        drone.move("forward", 1000)


def test_a_vehicle_still_moving_at_the_deadline_raises(make_drone):
    drone, vehicle = make_drone(avoidance=AVOIDING, move_timeout_margin_s=0.5, stationary_s=1.0, speed_mps=15.0)
    airborne(drone, vehicle)
    assert avoidance_known(drone)
    vehicle.max_speed = vehicle.speed = 0.05  # about 1 m/s at the test's time scale: slow, but not stationary
    with pytest.raises(TimeoutError, match="not reached"):
        drone.move("forward", 2000)


def test_a_pilot_taking_over_during_the_move_is_not_stopped_short(make_drone):
    drone, vehicle = make_drone(avoidance=AVOIDING, move_timeout_margin_s=2.0, stationary_s=0.3)
    airborne(drone, vehicle)
    assert avoidance_known(drone)
    _block(vehicle)
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("r", drone.move("forward", 1000)))
    worker.start()
    time.sleep(0.2)
    vehicle.set_pilot_mode("POSCTL")
    worker.join(10.0)
    assert out["r"].outcome is CommandOutcome.NOT_EXECUTED
    assert drone.external_control


# ---------------------------------------------------------------------------
# Start gate
# ---------------------------------------------------------------------------


def preflight_detail(drone):
    status = drone.link_status()
    return status.reason, status.detail or ""


def test_unset_sensing_keeps_todays_behaviour(make_drone):
    drone, _ = make_drone()
    assert wait_for(lambda: drone.link_status().ok), drone.link_status()


@pytest.mark.parametrize("mode", [SENSING_STEPPED, "forward_only", SENSING_OFF])
def test_a_stepped_sensor_or_sensing_off_needs_no_avoidance(make_drone, mode):
    drone, _ = make_drone()
    drone.set_obstacle_sensing(mode)
    assert wait_for(lambda: drone.link_status().ok), drone.link_status()


def test_no_avoidance_and_no_sensor_is_a_preflight_problem(make_drone):
    drone, vehicle = make_drone()
    drone.set_obstacle_sensing(SENSING_NONE)
    status = wait_for(lambda: (s := drone.link_status()).reason == "preflight" and "OA_TYPE" in (s.detail or "") and s)
    assert status, drone.link_status()
    assert not status.ok
    assert "OA_TYPE" in status.detail and "FLIGHTRISK_OBSTACLE_SENSING=off" in status.detail
    result = drone.takeoff()
    assert result.outcome is CommandOutcome.REJECTED and "no obstacle avoidance" in result.detail
    assert vehicle.commands == [], "nothing may be sent when the preflight fails"


def test_avoidance_configured_passes_the_gate(make_drone):
    drone, _ = make_drone(avoidance=AVOIDING)
    drone.set_obstacle_sensing(SENSING_NONE)
    assert wait_for(lambda: drone.link_status().ok), drone.link_status()


def test_px4_without_a_sensor_cannot_take_off(make_drone):
    drone, _ = make_drone(SimVehicle(PX4, time_scale=20.0))
    drone.set_obstacle_sensing(SENSING_NONE)
    status = wait_for(lambda: (s := drone.link_status()).reason == "preflight" and "Position mode" in (s.detail or "") and s)
    assert status, drone.link_status()
    drone.set_obstacle_sensing(SENSING_OFF)
    assert wait_for(lambda: drone.link_status().ok), drone.link_status()


def test_unknown_sensing_mode_is_refused(make_drone):
    drone, _ = make_drone()
    with pytest.raises(ValueError, match="sensing mode"):
        drone.set_obstacle_sensing("sideways")
    drone.set_obstacle_sensing(None)
