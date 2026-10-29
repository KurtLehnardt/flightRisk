"""MavlinkFleetDrone and MavsdkAutopilotLink against a simulated MAVSDK (no SITL, no hardware).

The real link code runs over ``tests.support.fake_mavsdk``: a kinematic
autopilot behind MAVSDK's asyncio API. These tests check the edge's own logic
(frame math, preflight, failsafe mapping, external control, watchdogs), not
how a real PX4 or ArduPilot behaves.
"""
from __future__ import annotations

import math
import threading
import time

import pytest

from flightrisk.drone.autopilot import (
    ARDUPILOT,
    PX4,
    AutopilotState,
    autonomous_modes,
    failsafe_problems,
    params_reader,
    preflight_problems,
    rtl_altitude_cm,
)
from flightrisk.drone.controller import Capability, CommandOutcome, command_result
from flightrisk.drone.mavlink_fleet import (
    MavlinkFleetDrone,
    MavlinkFleetOptions,
    _to_ned,
    _to_plan,
)
from flightrisk.drone.mavsdk_link import MavsdkAutopilotLink
from tests.support.fake_mavsdk import (
    ARDUPILOT_PARAMS,
    PX4_PARAMS,
    FixType,
    SimActionError,
    SimVehicle,
    install,
)

WAIT_S = 10.0


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

    def _make(vehicle=None, slot_cm=1500.0, **options):
        vehicle = vehicle or SimVehicle(ARDUPILOT, time_scale=20.0)
        install(monkeypatch, vehicle)
        drone = MavlinkFleetDrone("drone-1", MavsdkAutopilotLink("tcpout://127.0.0.1:1"), MavlinkFleetOptions(**options))
        drone.bind_slot_source(lambda: slot_cm)
        assert drone.connect() is True
        built.append(drone)
        return drone, vehicle

    yield _make
    for drone in built:
        drone.disconnect()


def ready(drone):
    return wait_for(lambda: drone.link_status().ok)


def airborne(drone, vehicle):
    assert ready(drone), drone.link_status()
    assert command_result(drone.takeoff(), "takeoff").ok
    assert vehicle.in_air


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("heading", [0.0, 37.0, 90.0, 180.0, 271.5])
def test_plan_frame_round_trips(heading):
    for x, y in ((0, 10), (10, 0), (-3.5, 7.25)):
        north, east = _to_ned(x, y, heading)
        assert _to_plan(north, east, heading) == pytest.approx((x, y))
    # Ahead along the heading, right is 90 degrees clockwise of it.
    north, east = _to_ned(0, 1, 90)
    assert (north, east) == pytest.approx((0, 1))
    north, east = _to_ned(1, 0, 90)
    assert (north, east) == pytest.approx((-1, 0))


def test_failsafe_rules_pass_the_reference_settings():
    assert failsafe_problems(ARDUPILOT, params_reader(ARDUPILOT_PARAMS), geofence_cm=3000, ceiling_cm=3000,
                             slot_cm=1500) == []
    assert failsafe_problems(PX4, params_reader(PX4_PARAMS), geofence_cm=3000, ceiling_cm=3000, slot_cm=1500) == []
    assert failsafe_problems(None, params_reader({})) == ["autopilot type unknown (None); cannot check its failsafes"]


@pytest.mark.parametrize(
    ("autopilot", "change", "needle"),
    [
        # Values that do nothing, hover, or stop the motors in the air (parameter references cited in autopilot.py).
        (PX4, {"GF_ACTION": 1}, "GF_ACTION=1"),            # warning only
        (PX4, {"GF_ACTION": 4}, "GF_ACTION=4"),            # terminate
        (PX4, {"NAV_RCL_ACT": 6}, "NAV_RCL_ACT=6"),        # disarm
        (PX4, {"NAV_RCL_ACT": 1}, "NAV_RCL_ACT=1"),        # hold: hovers
        (PX4, {"NAV_DLL_ACT": 5}, "NAV_DLL_ACT=5"),        # terminate
        (PX4, {"COM_LOW_BAT_ACT": 0}, "COM_LOW_BAT_ACT=0"),  # warning only
        (PX4, {"GF_MAX_HOR_DIST": 0, "GF_MAX_VER_DIST": 500}, "GF_MAX_HOR_DIST above 0"),  # no horizontal fence
        (PX4, {"GF_MAX_HOR_DIST": 40.0}, "GF_MAX_HOR_DIST=40 m is inside the edge geofence"),  # < sqrt(2)*30+5
        (PX4, {"GF_MAX_VER_DIST": 25.0}, "above the 30 m ceiling"),
        (PX4, {"RTL_RETURN_ALT": 60.0}, "RTL_RETURN_ALT return altitude 60 m is above"),  # PX4's default
        (PX4, {"MAV_SYS_ID": 2}, "system id is 2, expected 1"),
        (ARDUPILOT, {"BATT_FS_LOW_ACT": 5}, "BATT_FS_LOW_ACT=5"),  # terminate
        (ARDUPILOT, {"BATT_FS_CRT_ACT": 0}, "BATT_FS_CRT_ACT=0"),
        (ARDUPILOT, {"FENCE_ACTION": 0}, "FENCE_ACTION=0"),  # report only
        (ARDUPILOT, {"FENCE_ENABLE": 0}, "FENCE_ENABLE=0"),
        (ARDUPILOT, {"FENCE_TYPE": 1}, "need the circle fence"),  # altitude only: no horizontal fence
        (ARDUPILOT, {"FENCE_TYPE": 6}, "need the max-altitude fence"),
        (ARDUPILOT, {"FENCE_RADIUS": 30.0}, "FENCE_RADIUS=30 m is inside the edge geofence"),
        (ARDUPILOT, {"FENCE_ALT_MAX": 150.0}, "FENCE_ALT_MAX=150 m"),
        (ARDUPILOT, {"FS_THR_ENABLE": 2}, "FS_THR_ENABLE=2"),  # continue a mission
        (ARDUPILOT, {"BATT_ARM_VOLT": 0}, "BATT_ARM_VOLT above 0"),  # remaining % counts mAh since boot
        (ARDUPILOT, {"BATT_LOW_VOLT": 0, "BATT_LOW_MAH": 0}, "BATT_LOW_VOLT or BATT_LOW_MAH"),
        (ARDUPILOT, {"RTL_ALT": 3000}, "RTL_ALT return altitude 30 m is above"),
        (ARDUPILOT, {"SYSID_THISMAV": 3}, "system id is 3"),
    ],
)
def test_failsafe_rules_refuse_unsafe_values(autopilot, change, needle):
    params = {**(PX4_PARAMS if autopilot == PX4 else ARDUPILOT_PARAMS), **change}
    problems = failsafe_problems(autopilot, params_reader(params), geofence_cm=3000, ceiling_cm=3000, slot_cm=1500)
    assert any(needle in p for p in problems), problems


def test_failsafe_rules_fail_closed_on_unreadable_parameters():
    unreadable = {k: v for k, v in PX4_PARAMS.items() if k != "NAV_DLL_ACT"}
    assert any("cannot read NAV_DLL_ACT" in p for p in failsafe_problems(PX4, params_reader(unreadable)))
    unreadable = {k: v for k, v in ARDUPILOT_PARAMS.items() if k != "RTL_ALT"}
    assert any("cannot read RTL_ALT" in p for p in failsafe_problems(ARDUPILOT, params_reader(unreadable)))


def test_rtl_altitude_units():
    assert rtl_altitude_cm(ARDUPILOT, params_reader({"RTL_ALT": 1500})) == 1500
    assert rtl_altitude_cm(PX4, params_reader({"RTL_RETURN_ALT": 15.0})) == 1500
    assert rtl_altitude_cm(PX4, params_reader({})) is None


def test_preflight_fails_closed_on_missing_values():
    good = AutopilotState(
        connected=True, telemetry_age_s=0.1, autopilot=ARDUPILOT, gps_fix_type=3, satellites=12,
        global_position_ok=True, home_position_ok=True, armable=True, lat_deg=1.0, lon_deg=2.0, abs_alt_m=10.0,
        rel_alt_m=0.0, battery_pct=90.0, home_lat_deg=1.0, home_lon_deg=2.0, home_abs_alt_m=10.0,
    )
    assert preflight_problems(good, stale_after_s=3) == []
    assert preflight_problems(AutopilotState(), stale_after_s=3) == ["autopilot not connected"]
    stale = AutopilotState(connected=True, telemetry_age_s=9.0)
    assert preflight_problems(stale, stale_after_s=3) == ["autopilot telemetry stale (9 s old)"]
    bare = AutopilotState(connected=True, telemetry_age_s=0.1)
    problems = preflight_problems(bare, stale_after_s=3)
    for needle in ("autopilot type unknown", "no 3D GPS fix", "GPS satellites", "global position", "home position",
                   "no position", "battery level unknown", "not armable"):
        assert any(needle in p for p in problems), (needle, problems)


def test_autonomous_modes_tell_pilot_modes_apart():
    # ArduPilot LOITER shows as HOLD and is a pilot mode; PX4 HOLD is the edge's own.
    assert "HOLD" not in autonomous_modes(ARDUPILOT)
    assert "HOLD" in autonomous_modes(PX4)
    assert "POSCTL" not in autonomous_modes(PX4) | autonomous_modes(ARDUPILOT)
    assert autonomous_modes(None) == frozenset()


def test_options_are_validated():
    with pytest.raises(ValueError, match="failsafe_action"):
        MavlinkFleetOptions(failsafe_action="hover")
    with pytest.raises(ValueError, match="speed_mps"):
        MavlinkFleetOptions(speed_mps=40)
    with pytest.raises(ValueError, match="takeoff_alt_m"):
        MavlinkFleetOptions(takeoff_alt_m=1.0)


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def test_capabilities_are_honest(make_drone):
    drone, _ = make_drone()
    caps = drone.capabilities
    assert caps.flight_scale == "field"
    assert caps.has_gps
    assert not caps.supports(Capability.RC_CONTROL)
    assert not caps.supports(Capability.VIDEO)
    result = drone.rc_control(0, 10, 0, 0)
    assert result.outcome is CommandOutcome.REJECTED
    assert drone.get_frame() is None


def test_ready_once_parameters_read_and_link_reports_age(make_drone):
    drone, _ = make_drone()
    assert ready(drone), drone.link_status()
    status = drone.link_status()
    assert status.ok and status.age_s is not None and status.age_s < 1.0
    assert drone.read_battery() == 95
    assert drone.read_height_cm() == 0
    assert drone.state.is_flying is False


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        (lambda v: setattr(v, "fix", FixType.FIX_2D), "no 3D GPS fix"),
        (lambda v: setattr(v, "satellites", 4), "GPS satellites"),
        (lambda v: v.params.update(FENCE_ENABLE=0), "FENCE_ENABLE=0"),
        (lambda v: v.params.update(FS_THR_ENABLE=0), "FS_THR_ENABLE=0"),
        (lambda v: v.params.update(RTL_ALT=3000), "return altitude 30 m is above"),
        (lambda v: setattr(v, "armable", False), "not armable"),
    ],
)
def test_takeoff_refused_while_not_ready(make_drone, change, needle):
    vehicle = SimVehicle(ARDUPILOT, time_scale=20.0)
    change(vehicle)
    drone, vehicle = make_drone(vehicle)
    status = wait_for(lambda: (s := drone.link_status()).reason == "preflight" and needle in (s.detail or "") and s)
    assert status, drone.link_status()
    assert not status.ok
    result = drone.takeoff()
    assert result.outcome is CommandOutcome.REJECTED and needle in result.detail
    assert vehicle.commands == [], "nothing may be sent when the preflight fails"


def test_rtl_altitude_checked_against_the_live_slot(make_drone):
    slot = {"cm": 1500.0}
    vehicle = SimVehicle(ARDUPILOT, time_scale=20.0)
    install_slot = make_drone(vehicle, slot_cm=1500.0)[0]
    install_slot.bind_slot_source(lambda: slot["cm"])
    assert ready(install_slot)
    slot["cm"] = 1000.0  # a plan moved this drone to a lower slot
    assert "return altitude 15 m is above this drone's slot 10 m" in install_slot.link_status().detail


def test_return_altitude_is_checked_whatever_the_failsafe_action(make_drone):
    """The autopilot's own RC-loss and battery failsafes return by themselves, even with --failsafe-action land."""
    vehicle = SimVehicle(ARDUPILOT, time_scale=20.0)
    vehicle.params["RTL_ALT"] = 6000
    drone, _ = make_drone(vehicle, failsafe_action="land")
    assert wait_for(lambda: "RTL_ALT return altitude 60 m is above" in (drone.link_status().detail or ""))
    assert drone.check_slot(1500) is not None and "below the autopilot's return altitude" in drone.check_slot(1500)


def test_unverified_failsafes_skip_only_the_parameter_checks(make_drone):
    vehicle = SimVehicle(ARDUPILOT, time_scale=20.0)
    vehicle.params["FENCE_ENABLE"] = 0
    drone, _ = make_drone(vehicle, require_failsafes=False)
    assert ready(drone)
    vehicle.fix = FixType.NO_FIX
    assert wait_for(lambda: "no 3D GPS fix" in (drone.link_status().detail or ""))


def test_stale_telemetry_is_reported_and_values_unknown(make_drone):
    drone, vehicle = make_drone(stale_after_s=0.3)
    assert ready(drone)
    vehicle.cut_link()
    status = wait_for(lambda: (s := drone.link_status()).reason == "state_stale" and s)
    assert status and "autopilot unreachable" in status.detail
    assert drone.read_battery() is None
    assert drone.read_height_cm() is None
    assert drone.takeoff().outcome is CommandOutcome.REJECTED


# ---------------------------------------------------------------------------
# Flight
# ---------------------------------------------------------------------------


def test_lanes_fly_in_the_frame_heading(make_drone):
    drone, vehicle = make_drone(frame_heading_deg=90.0)  # "ahead" is east
    airborne(drone, vehicle)
    assert vehicle.commands[:2] == [("arm",), ("takeoff", 5.0)]
    assert command_result(drone.move("up", 1000), "move").ok
    assert drone.read_height_cm() == pytest.approx(1500, abs=70)
    assert command_result(drone.move("forward", 3000), "move").ok
    assert command_result(drone.move("right", 1000), "move").ok
    # Forward 30 m east, then right 10 m south.
    assert vehicle.east == pytest.approx(30.0, abs=1.0)
    assert vehicle.north == pytest.approx(-10.0, abs=1.0)
    pose = drone.measured_pose()
    assert (pose.x_cm, pose.y_cm) == pytest.approx((1000, 3000), abs=100)
    lat, lon = drone.geo_position()
    assert lat < vehicle.home_lat and lon > vehicle.home_lon
    # goto altitudes are AMSL: home 1600 m plus the 15 m slot.
    assert vehicle.commands[-1][3] == pytest.approx(1615.0)


def test_px4_rotate_turns_the_vehicle(make_drone):
    drone, vehicle = make_drone(SimVehicle(PX4, time_scale=20.0), frame_heading_deg=0.0)
    airborne(drone, vehicle)
    assert command_result(drone.rotate(90), "rotate").ok
    assert vehicle.heading == pytest.approx(90.0, abs=10.0)
    assert command_result(drone.move("forward", 500), "move").ok
    assert vehicle.east == pytest.approx(5.0, abs=0.6)
    assert drone.measured_pose().heading_deg == pytest.approx(90.0, abs=10.0)


def test_ardupilot_rotate_turns_the_plan_without_waiting_for_a_yaw(make_drone):
    """N9: ArduCopter ignores a reposition's yaw, so waiting for the heading would time out and land."""
    drone, vehicle = make_drone(frame_heading_deg=0.0, move_timeout_margin_s=1.0)
    airborne(drone, vehicle)
    started = time.monotonic()
    result = command_result(drone.rotate(90), "rotate")
    assert result.ok and time.monotonic() - started < 1.0
    assert command_result(drone.move("forward", 500), "move").ok
    assert vehicle.east == pytest.approx(5.0, abs=0.6) and vehicle.north == pytest.approx(0.0, abs=0.6)


def test_frame_heading_defaults_to_the_takeoff_heading(make_drone):
    drone, vehicle = make_drone(SimVehicle(ARDUPILOT, time_scale=20.0, heading_deg=180.0))
    airborne(drone, vehicle)
    assert command_result(drone.move("forward", 1000), "move").ok
    assert vehicle.north == pytest.approx(-10.0, abs=1.0)


def test_px4_flies_and_holds_without_external_control(make_drone):
    drone, vehicle = make_drone(SimVehicle(PX4, time_scale=20.0), frame_heading_deg=0.0)
    airborne(drone, vehicle)
    assert command_result(drone.move("forward", 1000), "move").ok
    assert drone.external_control is None
    assert vehicle.north == pytest.approx(10.0, abs=1.0)


def test_move_on_the_ground_is_not_executed(make_drone):
    drone, vehicle = make_drone()
    assert ready(drone)
    result = drone.move("forward", 500)
    assert result.outcome is CommandOutcome.NOT_EXECUTED
    assert drone.move("sideways", 500).outcome is CommandOutcome.REJECTED
    assert vehicle.commands == []


def test_move_watchdog_raises_when_the_target_is_not_reached(make_drone):
    vehicle = SimVehicle(ARDUPILOT, time_scale=20.0)
    drone, vehicle = make_drone(vehicle, move_timeout_margin_s=0.5)
    airborne(drone, vehicle)
    vehicle.max_speed = vehicle.speed = 0.0001
    with pytest.raises(TimeoutError, match="not reached"):
        drone.move("forward", 100)


def test_takeoff_watchdog(make_drone):
    vehicle = SimVehicle(ARDUPILOT, time_scale=1.0, climb_mps=0.01)
    drone, vehicle = make_drone(vehicle, takeoff_timeout_s=0.5)
    assert ready(drone)
    with pytest.raises(TimeoutError, match="takeoff did not reach"):
        drone.takeoff()


def test_a_refused_takeoff_is_retried_until_accepted(make_drone):
    vehicle = SimVehicle(ARDUPILOT, time_scale=20.0)
    vehicle.refuse_takeoffs = 2
    drone, vehicle = make_drone(vehicle)
    assert ready(drone)
    assert command_result(drone.takeoff(), "takeoff").ok
    assert [c for c in vehicle.commands if c[0] == "takeoff"] == [("takeoff", 5.0)] * 3
    assert vehicle.commands.count(("arm",)) == 1


def test_a_takeoff_refused_until_the_deadline_raises(make_drone):
    vehicle = SimVehicle(ARDUPILOT, time_scale=20.0)
    vehicle.refuse_takeoffs = 100
    drone, vehicle = make_drone(vehicle, takeoff_timeout_s=3.0)
    assert ready(drone)
    with pytest.raises(RuntimeError, match="GUIDED not ready"):
        drone.takeoff()


def test_takeoff_honours_abort(make_drone):
    drone, vehicle = make_drone()
    assert ready(drone)
    abort = threading.Event()
    abort.set()
    assert drone.takeoff(abort=abort).outcome is CommandOutcome.NOT_EXECUTED
    assert vehicle.commands == []


# ---------------------------------------------------------------------------
# Landing and failsafes
# ---------------------------------------------------------------------------


def test_land_on_the_ground_succeeds_without_sending(make_drone):
    drone, vehicle = make_drone()
    assert ready(drone)
    assert command_result(drone.land(), "land").ok
    assert command_result(drone.land(), "land").ok
    assert vehicle.commands == []


@pytest.mark.parametrize("reason", ["link_loss", "low_battery", "geofence_breach"])
def test_failsafe_reasons_return_to_launch(make_drone, reason):
    drone, vehicle = make_drone()
    airborne(drone, vehicle)
    result = command_result(drone.land_for_reason(reason), "land")
    assert result.ok and result.detail == "RETURN_TO_LAUNCH"
    assert vehicle.commands[-1] == ("return_to_launch",)
    # The executor retries a landing every 2 s; a return already under way is not turned into a landing.
    assert wait_for(lambda: drone.state.flight_mode == "RETURN_TO_LAUNCH")
    assert command_result(drone.land_for_reason(reason), "land").detail.endswith("already in progress")
    assert vehicle.commands.count(("return_to_launch",)) == 1
    assert wait_for(lambda: not drone.state.is_flying)
    assert math.hypot(vehicle.north, vehicle.east) < 0.5


def test_failsafe_action_land_lands_in_place(make_drone):
    drone, vehicle = make_drone(failsafe_action="land")
    airborne(drone, vehicle)
    assert command_result(drone.land_for_reason("link_loss"), "land").detail == "LAND"
    assert vehicle.commands[-1] == ("land",)


@pytest.mark.parametrize("reason", ["emergency_land_all", "ground_land", "geofence", "executor_error"])
def test_other_reasons_land_in_place(make_drone, reason):
    drone, vehicle = make_drone()
    airborne(drone, vehicle)
    assert command_result(drone.land_for_reason(reason), "land").detail == "LAND"
    assert vehicle.commands[-1] == ("land",)


def test_operator_land_overrides_a_return(make_drone):
    drone, vehicle = make_drone()
    airborne(drone, vehicle)
    drone.land_for_reason("link_loss")
    assert wait_for(lambda: drone.state.flight_mode == "RETURN_TO_LAUNCH")
    assert command_result(drone.land_for_reason("emergency_land_all"), "land").detail == "LAND"
    assert vehicle.commands[-1] == ("land",)


def test_land_interrupts_a_move_in_progress(make_drone):
    drone, vehicle = make_drone(speed_mps=1.0)
    airborne(drone, vehicle)
    vehicle.time_scale = 1.0
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("r", drone.move("forward", 3000)))
    worker.start()
    time.sleep(0.3)
    started = time.monotonic()
    assert command_result(drone.land(), "land").ok
    worker.join(5.0)
    assert not worker.is_alive()
    assert time.monotonic() - started < 2.0, "a landing must not wait for the move"
    assert out["r"].outcome is CommandOutcome.NOT_EXECUTED


def test_land_raises_when_the_autopilot_is_not_connected(make_drone):
    drone, _ = make_drone()
    drone.disconnect()
    with pytest.raises(RuntimeError, match="not connected"):
        drone.land()


# ---------------------------------------------------------------------------
# External control
# ---------------------------------------------------------------------------


def test_pilot_mode_switch_latches_external_control(make_drone):
    drone, vehicle = make_drone(speed_mps=1.0)
    airborne(drone, vehicle)
    vehicle.time_scale = 1.0
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("r", drone.move("forward", 3000)))
    worker.start()
    time.sleep(0.2)
    vehicle.set_pilot_mode("POSCTL")
    worker.join(5.0)
    assert out["r"].outcome is CommandOutcome.NOT_EXECUTED
    assert "POSCTL" in drone.external_control
    status = drone.link_status()
    assert status.reason == "external_control" and not status.ok
    before = list(vehicle.commands)
    assert drone.move("forward", 500).outcome is CommandOutcome.REJECTED
    # Automatic landings never fight the pilot ...
    for reason in ("link_loss", "low_battery", "executor_error", "geofence_breach", "shutdown"):
        assert command_result(drone.land_for_reason(reason), "land").ok
    assert vehicle.commands == before
    # ... an operator's explicit land is still sent.
    drone.land_for_reason("emergency_land_all")
    assert vehicle.commands[-1] == ("land",)


def test_ardupilot_loiter_is_a_pilot_mode(make_drone):
    drone, vehicle = make_drone()
    airborne(drone, vehicle)
    vehicle.set_pilot_mode("HOLD")  # ArduCopter LOITER
    assert wait_for(lambda: drone.external_control is not None)


def test_autopilot_failsafe_latches_external_control(make_drone):
    drone, vehicle = make_drone()
    airborne(drone, vehicle)
    vehicle.trigger_failsafe("RETURN_TO_LAUNCH")
    assert wait_for(lambda: drone.external_control is not None)
    assert "by itself" in drone.external_control
    before = list(vehicle.commands)
    assert command_result(drone.land_for_reason("link_loss"), "land").ok
    assert vehicle.commands == before


def test_next_takeoff_clears_external_control(make_drone):
    drone, vehicle = make_drone()
    airborne(drone, vehicle)
    vehicle.set_pilot_mode("POSCTL")
    assert wait_for(lambda: drone.external_control is not None)
    drone.land_for_reason("manual_land")
    assert wait_for(lambda: not drone.state.is_flying)
    assert ready(drone), drone.link_status()
    assert command_result(drone.takeoff(), "takeoff").ok
    assert drone.external_control is None


def test_snapshot_reports_sensed_flight_and_si_units(make_drone):
    drone, vehicle = make_drone()
    airborne(drone, vehicle)
    snap = drone.snapshot()
    assert snap.backend == "mavlink-fleet" and snap.is_flying and snap.is_flying_source == "sensed"
    assert snap.height_m.value == pytest.approx(5.0, abs=0.7)
    assert snap.battery_pct.unit == "%"
    assert snap.latitude_deg.value == pytest.approx(vehicle.home_lat, abs=1e-4)


def test_each_link_runs_its_own_mavsdk_server(monkeypatch):
    """Two links in one process must not share MAVSDK's default gRPC port (SITL showed cross-talk)."""
    ports = []
    vehicle = SimVehicle(ARDUPILOT, time_scale=20.0)
    cls = install(monkeypatch, vehicle)

    class Recording(cls):
        def __init__(self, mavsdk_server_address=None, port=50051, **kw):
            ports.append(port)
            super().__init__(mavsdk_server_address, port, **kw)

    import flightrisk.drone.mavsdk_link as link_mod

    monkeypatch.setattr(link_mod, "System", Recording)
    links = [MavsdkAutopilotLink("tcpout://127.0.0.1:1") for _ in range(2)]
    try:
        for link in links:
            link.connect()
    finally:
        for link in links:
            link.disconnect()
    assert len(ports) == 2 and ports[0] != ports[1] and 50051 not in ports


# ---------------------------------------------------------------------------
# Review round: landing never fights a pilot or the autopilot (B1, B2)
# ---------------------------------------------------------------------------


def test_autopilot_land_after_an_edge_return_is_not_overridden(make_drone):
    """B1(b): the edge asked for RTL; the autopilot's critical-battery failsafe (or the pilot) switched to LAND."""
    drone, vehicle = make_drone()
    airborne(drone, vehicle)
    vehicle.time_scale = 0.5
    drone.land_for_reason("link_loss")
    assert wait_for(lambda: drone.state.flight_mode == "RETURN_TO_LAUNCH")
    vehicle.trigger_failsafe("LAND")
    assert wait_for(lambda: drone.state.flight_mode == "LAND")
    assert "from RETURN_TO_LAUNCH to LAND" in drone.external_control
    sent = vehicle.commands.count(("return_to_launch",))
    assert command_result(drone.land_for_reason("link_loss", retry=True), "land").ok
    assert command_result(drone.land_for_reason("low_battery"), "land").ok
    assert vehicle.commands.count(("return_to_launch",)) == sent
    assert drone.state.flight_mode == "LAND"


def test_edge_land_never_overrides_an_autopilot_return(make_drone):
    """B1(c): the edge's own LAND (on_complete) must not pull a drone out of the autopilot's RTL."""
    drone, vehicle = make_drone()
    airborne(drone, vehicle)
    vehicle.time_scale = 0.5
    drone.land_for_reason("on_complete")
    assert wait_for(lambda: drone.state.flight_mode == "LAND")
    vehicle.trigger_failsafe("RETURN_TO_LAUNCH")
    assert wait_for(lambda: drone.external_control is not None)
    sent = vehicle.commands.count(("land",))
    drone.land_for_reason("on_complete", retry=True)
    drone.land_for_reason("on_complete")
    assert vehicle.commands.count(("land",)) == sent


def test_retries_resend_only_a_mode_the_autopilot_never_entered(make_drone):
    drone, vehicle = make_drone()
    airborne(drone, vehicle)
    vehicle.time_scale = 0.5
    real_land = drone.link.land
    drone.link.land = lambda: vehicle._record("land")  # the command is lost: the mode never changes
    drone.land_for_reason("ground_land")
    drone.land_for_reason("ground_land", retry=True)
    assert vehicle.commands.count(("land",)) == 2  # never entered: sent again
    drone.link.land = real_land
    drone.land_for_reason("ground_land", retry=True)
    assert wait_for(lambda: drone.state.flight_mode == "LAND")
    drone.land_for_reason("ground_land", retry=True)
    assert vehicle.commands.count(("land",)) == 3  # entered: not sent again


def test_a_refused_return_falls_back_to_land(make_drone):
    """B2: GPS degraded and the ground link drops; RTL is denied, so the drone must land."""
    drone, vehicle = make_drone()
    airborne(drone, vehicle)

    def refuse():
        vehicle._record("return_to_launch")
        raise SimActionError("COMMAND_DENIED: RTL needs position")

    drone.link.return_to_launch = refuse
    result = command_result(drone.land_for_reason("low_battery"), "land")
    assert result.ok and result.detail == "LAND"
    assert vehicle.commands[-2:] == [("return_to_launch",), ("land",)]
    assert wait_for(lambda: not drone.state.is_flying)
    # Later failsafe landings in this flight land in place.
    assert drone._rtl_failed


def test_a_return_never_entered_is_replaced_by_a_landing(make_drone):
    drone, vehicle = make_drone(rtl_accept_s=0.5)
    airborne(drone, vehicle)
    drone.link.return_to_launch = lambda: vehicle._record("return_to_launch")  # accepted, never entered
    drone.land_for_reason("link_loss")
    assert command_result(drone.land_for_reason("link_loss", retry=True), "land").detail.startswith("waiting")
    time.sleep(0.6)
    assert command_result(drone.land_for_reason("link_loss", retry=True), "land").detail == "LAND"
    assert vehicle.commands[-1] == ("land",)


# ---------------------------------------------------------------------------
# Review round: other fixes
# ---------------------------------------------------------------------------


def test_wrong_system_id_is_not_ready(make_drone):
    drone, _ = make_drone(sysid=7)
    assert wait_for(lambda: "system id is 1, expected 7" in (drone.link_status().detail or ""))


def test_altitude_reference_is_taken_at_each_send(make_drone):
    """N4: home moves at arming (pre-arm barometer drift); targets follow the autopilot's own height."""
    drone, vehicle = make_drone()
    airborne(drone, vehicle)
    vehicle.home_alt += 3.0  # the reference the autopilot reports heights against moved
    assert wait_for(lambda: drone.link.state().abs_alt_m == pytest.approx(1608.0, abs=0.2))
    assert command_result(drone.move("up", 500), "move").ok
    assert vehicle.alt == pytest.approx(10.0, abs=0.7)


def test_a_stale_stream_is_not_trusted(make_drone, monkeypatch):
    """N7: the battery stream stopped while others go on; its last value must not count."""
    drone, _ = make_drone()
    assert ready(drone)
    real_state = drone.link.state

    def stale_battery():
        st = real_state()
        return type(st)(**{**st.__dict__, "stream_age_s": {**st.stream_age_s, "battery": 99.0}})

    monkeypatch.setattr(drone.link, "state", stale_battery)
    assert drone.read_battery() is None
    assert "telemetry stale or missing: battery" in drone.link_status().detail


def test_telemetry_fields_report_speed_and_mode(make_drone):
    drone, vehicle = make_drone()
    airborne(drone, vehicle)
    fields = drone.telemetry_fields()
    assert fields["autopilot"] == ARDUPILOT and fields["speed_set"] is True and fields["external_control"] is False
    assert fields["flight_mode"] == "OFFBOARD"
