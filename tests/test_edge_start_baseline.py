"""The live edge still answers what it answered at the A3 base commit (Phase A3, steps S1 and S2).

``tests/fixtures/edge_start_baseline.json`` was recorded once by
``tests/tools/make_edge_baseline.py`` from the real runtime modules at a2c0c89.
A3 rewires today's readers to the resolver, so ``tests/test_param_parity.py``
would end up comparing the resolver with itself. This test keeps a separate
record: every start object built from an empty environment, every
SafetyEnvelope check on a probe grid, and every parity probe sent through the
readers, must still equal the fixture. The probe section takes its inputs from
the parity harness, so the harness file is pinned by its sha256 below.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

HERE = Path(__file__).parent
FIXTURE = json.loads((HERE / "fixtures" / "edge_start_baseline.json").read_text())

_spec = importlib.util.spec_from_file_location("make_edge_baseline", HERE / "tools" / "make_edge_baseline.py")
maker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maker)


def _same_json(value):
    """``value`` after the same JSON round trip the fixture went through."""
    return json.loads(json.dumps(value, sort_keys=True))


@pytest.fixture(scope="module")
def live_start_objects():
    return _same_json(maker.start_objects())


@pytest.mark.parametrize("name", sorted(FIXTURE["start_objects"]))
def test_start_object_equals_the_baseline(name, live_start_objects):
    assert live_start_objects[name] == FIXTURE["start_objects"][name]


def test_start_objects_have_no_extra_or_missing_entries(live_start_objects):
    assert set(live_start_objects) == set(FIXTURE["start_objects"])


def test_the_baseline_covers_every_a3_module():
    assert set(FIXTURE["start_objects"]["module_constants"]) == set(maker.A3_MODULES)


@pytest.fixture(scope="module")
def live_methods():
    return _same_json(maker.method_results())


@pytest.mark.parametrize("form", sorted(FIXTURE["methods"]))
def test_safety_envelope_methods_equal_the_baseline(form, live_methods):
    assert live_methods[form] == FIXTURE["methods"][form]


def test_methods_have_no_extra_or_missing_forms(live_methods):
    assert set(live_methods) == set(FIXTURE["methods"]) == {"default", "from_env_empty", "for_field"}
    for form in live_methods:
        assert set(live_methods[form]) == set(maker.METHOD_PROBES)


# --- The record is the same on every machine (review finding 1). ---


def _build_with_mavsdk(available: bool):
    """Start objects and method results from freshly imported flightrisk modules, with mavsdk present or not."""
    saved = dict(sys.modules)
    try:
        for name in list(sys.modules):
            if name.split(".")[0] in ("flightrisk", "mavsdk"):
                del sys.modules[name]
        if available:
            fake = types.ModuleType("mavsdk")
            fake.System = type("System", (), {})
            sys.modules["mavsdk"] = fake
        else:
            sys.modules["mavsdk"] = None  # makes ``import mavsdk`` raise ImportError
        mavsdk_link = importlib.import_module("flightrisk.drone.mavsdk_link")
        assert (mavsdk_link.System is None) is not available
        return _same_json({"start_objects": maker.start_objects(), "methods": maker.method_results()})
    finally:
        for name in [n for n in sys.modules if n not in saved]:
            del sys.modules[name]
        sys.modules.update(saved)


def test_the_record_does_not_depend_on_whether_mavsdk_imports():
    with_mavsdk = _build_with_mavsdk(True)
    without_mavsdk = _build_with_mavsdk(False)
    assert with_mavsdk == without_mavsdk
    assert with_mavsdk == {"start_objects": FIXTURE["start_objects"], "methods": FIXTURE["methods"]}
    assert "_REAL_SYSTEM" not in FIXTURE["start_objects"]["module_constants"]["flightrisk.drone.mavsdk_link"]


def test_optional_import_names_follow_assignments():
    source = (
        "try:\n    from mavsdk import System\nexcept ImportError:\n    System = None\n"
        "_REAL = System\n_ALSO = _REAL\nPLAIN = 3\n"
    )
    with mock.patch.object(maker.inspect, "getsource", return_value=source):
        assert maker.optional_import_names(types.ModuleType("probe")) == {"System", "_REAL", "_ALSO"}


# --- A callable or default_factory value outside the allowlists stops generation (finding 5). ---


def test_a_callable_outside_the_allowlist_stops_generation():
    assert maker.jsonable(len, "clock") == {"__repr__": "builtin_function_or_method"}
    with pytest.raises(maker.BaselineError, match="on_land"):
        maker.jsonable(len, "on_land")
    with pytest.raises(maker.BaselineError, match="hook"):
        maker.jsonable({"hook": print}, "options")


def test_a_default_factory_outside_the_allowlist_stops_generation():
    import dataclasses

    @dataclasses.dataclass
    class Probe:
        values: dict = dataclasses.field(default_factory=dict)

    with pytest.raises(maker.BaselineError, match="FACTORY_DEFAULTS"):
        maker._init_defaults(Probe)


# --- The probe section depends on the parity harness (finding 6). ---

#: sha256 of ``tests/test_param_parity.py`` when the baseline was recorded.
PARITY_HARNESS_SHA256 = "3f4da684a22f1140b613f3297f61387811ae4c503c97d46fd6c0ed32a539d9f2"


def test_the_parity_harness_is_the_one_the_baseline_was_recorded_with():
    digest = hashlib.sha256((HERE / "test_param_parity.py").read_bytes()).hexdigest()
    assert digest == PARITY_HARNESS_SHA256, (
        "tests/test_param_parity.py changed since tests/fixtures/edge_start_baseline.json was recorded, and the "
        "baseline's probe section takes its probe list and refusal handling from that harness. Regenerate the "
        "baseline from the A3 base commit's runtime (delete the fixture, run tests/tools/make_edge_baseline.py), "
        f"review the diff, and set PARITY_HARNESS_SHA256 to {digest}."
    )


@pytest.fixture(scope="module")
def live_probes():
    return _same_json(maker.probe_results())


def test_probes_cover_every_case_of_the_parity_test(live_probes):
    from tests.test_param_parity import CASES

    assert len(FIXTURE["probes"]) == len(CASES) == len(live_probes)
    assert set(live_probes) == set(FIXTURE["probes"])


@pytest.mark.parametrize("key", sorted(FIXTURE["probes"]))
def test_probes_reader_output_equals_the_baseline(key, live_probes):
    assert live_probes[key] == FIXTURE["probes"][key], key


# --- Every section 3.1 entry of the A3 plan is recorded (finding 2). ---

SO = ("start_objects",)


def _mc(module: str, *rest) -> tuple:
    return SO + ("module_constants", f"flightrisk.{module}") + rest


def _init(module: str, cls: str, name: str) -> tuple:
    return _mc(module, "__classes__", cls, "init_defaults", name)


def _caps(qualified: str, name: str) -> tuple:
    return SO + ("drone_capabilities", f"flightrisk.{qualified}", name)


#: The 130 entries of section 3.1 of the A3 step plan (deferred allowlist entries in A3-owned files), as
#: (file, scope, name, value, fixture path). The plan is a scratch file, so the list is copied here.
#: A path of None marks an entry the baseline does not record: ``recv_timeout=0.01`` is the default of
#: ``edge_device.run_edge_device``, which has no registry row and which no A3 step rewires (the plan
#: reclassifies it as ``internal``). ``max_altitude_m`` is recorded through the controllers' capabilities.
PLAN_ENTRIES = [
    ("flightrisk/drone/autopilot.py", "<module>", "DEFAULT_MIN_SATELLITES", 8, _mc("drone.autopilot", "DEFAULT_MIN_SATELLITES")),
    ("flightrisk/drone/autopilot.py", "<module>", "FENCE_MARGIN_M", 5.0, _mc("drone.autopilot", "FENCE_MARGIN_M")),
    ("flightrisk/drone/autopilot.py", "<module>", "MIN_OA_LOOKAHEAD_M", 5.0, _mc("drone.autopilot", "MIN_OA_LOOKAHEAD_M")),
    ("flightrisk/drone/autopilot.py", "<module>", "MIN_OA_MARGIN_M", 2.0, _mc("drone.autopilot", "MIN_OA_MARGIN_M")),
    ("flightrisk/drone/fake.py", "<module>.FakeDrone.__init__", "max_move_cm", 500, _caps("drone.fake.FakeDrone", "max_move_cm")),
    ("flightrisk/drone/fake.py", "<module>.FakeDrone.__init__", "min_move_cm", 20, _caps("drone.fake.FakeDrone", "min_move_cm")),
    ("flightrisk/drone/flight_guards.py", "<module>", "DEFAULT_REFUSALS_PER_FLIGHT", 2, _mc("drone.flight_guards", "DEFAULT_REFUSALS_PER_FLIGHT")),
    ("flightrisk/drone/flight_guards.py", "<module>", "DEFAULT_REFUSAL_HOLD_S", 2.5, _mc("drone.flight_guards", "DEFAULT_REFUSAL_HOLD_S")),
    ("flightrisk/drone/flight_guards.py", "<module>", "DEFAULT_REFUSAL_RETRIES", 2, _mc("drone.flight_guards", "DEFAULT_REFUSAL_RETRIES")),
    ("flightrisk/drone/flight_guards.py", "<module>", "DEFAULT_SETTLE_SAMPLES", 5, _mc("drone.flight_guards", "DEFAULT_SETTLE_SAMPLES")),
    ("flightrisk/drone/flight_guards.py", "<module>", "DEFAULT_SETTLE_TIMEOUT_S", 5.0, _mc("drone.flight_guards", "DEFAULT_SETTLE_TIMEOUT_S")),
    ("flightrisk/drone/flight_guards.py", "<module>", "DEFAULT_SETTLE_TOLERANCE_CM", 10.0, _mc("drone.flight_guards", "DEFAULT_SETTLE_TOLERANCE_CM")),
    ("flightrisk/drone/flight_guards.py", "<module>", "DEFAULT_TAKEOFF_LATE_S", 10.0, _mc("drone.flight_guards", "DEFAULT_TAKEOFF_LATE_S")),
    ("flightrisk/drone/lane_executor.py", "<module>.LaneExecutor", "obstacle_clear_votes", 1, _init("drone.lane_executor", "LaneExecutor", "obstacle_clear_votes")),
    ("flightrisk/drone/lane_executor.py", "<module>.LaneExecutor", "obstacle_takeoff_probe", True, _init("drone.lane_executor", "LaneExecutor", "obstacle_takeoff_probe")),
    ("flightrisk/drone/lane_executor.py", "<module>.LaneExecutor", "obstacle_turn", True, _init("drone.lane_executor", "LaneExecutor", "obstacle_turn")),
    ("flightrisk/drone/lane_flight.py", "<module>", "HEADING_TOLERANCE_DEG", 10.0, _mc("drone.lane_flight", "HEADING_TOLERANCE_DEG")),
    ("flightrisk/drone/lane_flight.py", "<module>", "OBSTACLE_RETRY_PAUSE_S", 0.5, _mc("drone.lane_flight", "OBSTACLE_RETRY_PAUSE_S")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>", "_HEADING_TOLERANCE_DEG", 10.0, _mc("drone.mavlink_fleet", "_HEADING_TOLERANCE_DEG")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>", "_STATIONARY_RADIUS_M", 0.5, _mc("drone.mavlink_fleet", "_STATIONARY_RADIUS_M")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "arrival_alt_m", 0.7, SO + ("mavlink_fleet_options", "arrival_alt_m")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "arrival_radius_m", 1.0, SO + ("mavlink_fleet_options", "arrival_radius_m")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "fence_breach_margin_cm", 300.0, SO + ("mavlink_fleet_options", "fence_breach_margin_cm")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "move_timeout_margin_s", 20.0, SO + ("mavlink_fleet_options", "move_timeout_margin_s")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "param_refresh_s", 30.0, SO + ("mavlink_fleet_options", "param_refresh_s")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "rtl_accept_s", 5.0, SO + ("mavlink_fleet_options", "rtl_accept_s")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "speed_mps", 3.0, SO + ("mavlink_fleet_options", "speed_mps")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "stale_after_s", 3.0, SO + ("mavlink_fleet_options", "stale_after_s")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "stationary_s", 3.0, SO + ("mavlink_fleet_options", "stationary_s")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "takeoff_alt_m", 5.0, SO + ("mavlink_fleet_options", "takeoff_alt_m")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "takeoff_timeout_s", 60.0, SO + ("mavlink_fleet_options", "takeoff_timeout_s")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "yaw_rate_dps", 30.0, SO + ("mavlink_fleet_options", "yaw_rate_dps")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "failsafe_action", 'rtl', SO + ("mavlink_fleet_options", "failsafe_action")),
    ("flightrisk/drone/mavlink_fleet.py", "<module>.MavlinkFleetOptions", "require_failsafes", True, SO + ("mavlink_fleet_options", "require_failsafes")),
    ("flightrisk/drone/mavsdk_link.py", "<module>", "DEFAULT_COMMAND_TIMEOUT_S", 10.0, _mc("drone.mavsdk_link", "DEFAULT_COMMAND_TIMEOUT_S")),
    ("flightrisk/drone/mavsdk_link.py", "<module>", "DEFAULT_CONNECT_TIMEOUT_S", 30.0, _mc("drone.mavsdk_link", "DEFAULT_CONNECT_TIMEOUT_S")),
    ("flightrisk/drone/obstacle.py", "<module>.ObstacleGuard", "min_safe_depth", 0.35, _init("drone.obstacle", "ObstacleGuard", "min_safe_depth")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "MAX_DECODE_LAG_S", 0.3, _mc("drone.obstacle_sensor", "MAX_DECODE_LAG_S")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_AVOID_BUDGET_S", 45.0, _mc("drone.obstacle_sensor", "OBSTACLE_AVOID_BUDGET_S")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_CLEAR_VOTES", 2, _mc("drone.obstacle_sensor", "OBSTACLE_CLEAR_VOTES")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_DETECTION_WAIT_S", 1.0, _mc("drone.obstacle_sensor", "OBSTACLE_DETECTION_WAIT_S")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_FRAME_WAIT_S", 0.7, _mc("drone.obstacle_sensor", "OBSTACLE_FRAME_WAIT_S")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_MIN_SAFE_DEPTH", 0.35, _mc("drone.obstacle_sensor", "OBSTACLE_MIN_SAFE_DEPTH")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_PERSON_FRACTION", 0.55, _mc("drone.obstacle_sensor", "OBSTACLE_PERSON_FRACTION")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_RELOOK", 2, _mc("drone.obstacle_sensor", "OBSTACLE_RELOOK")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_RELOOKS_PER_LANE", 3, _mc("drone.obstacle_sensor", "OBSTACLE_RELOOKS_PER_LANE")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_SENSE_RETRIES", 3, _mc("drone.obstacle_sensor", "OBSTACLE_SENSE_RETRIES")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_SENSE_TIMEOUT_S", 2.0, _mc("drone.obstacle_sensor", "OBSTACLE_SENSE_TIMEOUT_S")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_SENSOR_DOWN_S", 10.0, _mc("drone.obstacle_sensor", "OBSTACLE_SENSOR_DOWN_S")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_SETTLE_S", 0.3, _mc("drone.obstacle_sensor", "OBSTACLE_SETTLE_S")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_SIDESTEPS", 2, _mc("drone.obstacle_sensor", "OBSTACLE_SIDESTEPS")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_SIDESTEP_PASS_CM", 150, _mc("drone.obstacle_sensor", "OBSTACLE_SIDESTEP_PASS_CM")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_STEP_CM", 50, _mc("drone.obstacle_sensor", "OBSTACLE_STEP_CM")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_STEP_FIELD_CM", 500, _mc("drone.obstacle_sensor", "OBSTACLE_STEP_FIELD_CM")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_STEP_MAX_CM", 100, _mc("drone.obstacle_sensor", "OBSTACLE_STEP_MAX_CM")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>", "OBSTACLE_STEP_MIN_CM", 20, _mc("drone.obstacle_sensor", "OBSTACLE_STEP_MIN_CM")),
    ("flightrisk/drone/obstacle_sensor.py", "<module>.BlockedZone", "heading_tol_deg", 1.0, _init("drone.obstacle_sensor", "BlockedZone", "heading_tol_deg")),
    ("flightrisk/drone/safety.py", "<module>", "DEFAULT_ALT_SLOTS_CM", 100, _mc("drone.safety", "DEFAULT_ALT_SLOTS_CM", "__seq__", 0)),
    ("flightrisk/drone/safety.py", "<module>", "DEFAULT_ALT_SLOTS_CM", 130, _mc("drone.safety", "DEFAULT_ALT_SLOTS_CM", "__seq__", 1)),
    ("flightrisk/drone/safety.py", "<module>", "DEFAULT_ALT_SLOT_CM", 100, _mc("drone.safety", "DEFAULT_ALT_SLOT_CM")),
    ("flightrisk/drone/safety.py", "<module>", "DEFAULT_CEILING_CM", 200, _mc("drone.safety", "DEFAULT_CEILING_CM")),
    ("flightrisk/drone/safety.py", "<module>", "DEFAULT_CEILING_MARGIN_CM", 60, _mc("drone.safety", "DEFAULT_CEILING_MARGIN_CM")),
    ("flightrisk/drone/safety.py", "<module>", "DEFAULT_FIELD_LINK_LOSS_HOLD_S", 10.0, _mc("drone.safety", "DEFAULT_FIELD_LINK_LOSS_HOLD_S")),
    ("flightrisk/drone/safety.py", "<module>", "DEFAULT_GEOFENCE_CM", 500, _mc("drone.safety", "DEFAULT_GEOFENCE_CM")),
    ("flightrisk/drone/safety.py", "<module>", "DEFAULT_LAND_BATTERY", 15, _mc("drone.safety", "DEFAULT_LAND_BATTERY")),
    ("flightrisk/drone/safety.py", "<module>", "DEFAULT_LINK_LOSS_LAND_S", 3.0, _mc("drone.safety", "DEFAULT_LINK_LOSS_LAND_S")),
    ("flightrisk/drone/safety.py", "<module>", "DEFAULT_MIN_TAKEOFF_BATTERY", 30, _mc("drone.safety", "DEFAULT_MIN_TAKEOFF_BATTERY")),
    ("flightrisk/drone/safety.py", "<module>", "MAX_CEILING_CM", 1000, _mc("drone.safety", "MAX_CEILING_CM")),
    ("flightrisk/drone/safety.py", "<module>", "MAX_MOVE_CM", 500, _mc("drone.safety", "MAX_MOVE_CM")),
    ("flightrisk/drone/safety.py", "<module>", "MIN_ALT_SLOT_CM", 50, _mc("drone.safety", "MIN_ALT_SLOT_CM")),
    ("flightrisk/drone/safety.py", "<module>", "MIN_CEILING_CM", 150, _mc("drone.safety", "MIN_CEILING_CM")),
    ("flightrisk/drone/safety.py", "<module>", "MIN_CEILING_MARGIN_CM", 30, _mc("drone.safety", "MIN_CEILING_MARGIN_CM")),
    ("flightrisk/drone/safety.py", "<module>", "MIN_MOVE_CM", 20, _mc("drone.safety", "MIN_MOVE_CM")),
    ("flightrisk/drone/safety.py", "<module>", "default_alt_slot_cm", 1500, SO + ("field_limits", "default_alt_slot_cm")),
    ("flightrisk/drone/safety.py", "<module>", "default_ceiling_cm", 3000, SO + ("field_limits", "default_ceiling_cm")),
    ("flightrisk/drone/safety.py", "<module>", "default_ceiling_margin_cm", 500, SO + ("field_limits", "default_ceiling_margin_cm")),
    ("flightrisk/drone/safety.py", "<module>", "default_geofence_cm", 3000, SO + ("field_limits", "default_geofence_cm")),
    ("flightrisk/drone/safety.py", "<module>", "max_alt_slot_cm", 12000, SO + ("field_limits", "max_alt_slot_cm")),
    ("flightrisk/drone/safety.py", "<module>", "max_ceiling_cm", 12000, SO + ("field_limits", "max_ceiling_cm")),
    ("flightrisk/drone/safety.py", "<module>", "max_geofence_cm", 50000, SO + ("field_limits", "max_geofence_cm")),
    ("flightrisk/drone/safety.py", "<module>", "max_move_cm", 5000, SO + ("field_limits", "max_move_cm")),
    ("flightrisk/drone/safety.py", "<module>", "min_alt_slot_cm", 1000, SO + ("field_limits", "min_alt_slot_cm")),
    ("flightrisk/drone/safety.py", "<module>", "min_ceiling_cm", 1000, SO + ("field_limits", "min_ceiling_cm")),
    ("flightrisk/drone/safety.py", "<module>", "min_ceiling_margin_cm", 300, SO + ("field_limits", "min_ceiling_margin_cm")),
    ("flightrisk/drone/tello.py", "<module>", "_COMMAND_REPLY_TIMEOUT_S", 7.0, _mc("drone.tello", "_COMMAND_REPLY_TIMEOUT_S")),
    ("flightrisk/drone/tello.py", "<module>", "_CONNECT_ATTEMPTS", 3, _mc("drone.tello", "_CONNECT_ATTEMPTS")),
    ("flightrisk/drone/tello.py", "<module>", "_GROUND_HEIGHT_CM", 10, _mc("drone.tello", "_GROUND_HEIGHT_CM")),
    ("flightrisk/drone/tello.py", "<module>", "_GROUND_READINGS", 3, _mc("drone.tello", "_GROUND_READINGS")),
    ("flightrisk/drone/tello.py", "<module>", "_GROUND_TOF_CM", 30, _mc("drone.tello", "_GROUND_TOF_CM")),
    ("flightrisk/drone/tello.py", "<module>", "_KEEPALIVE_INTERVAL_S", 10.0, _mc("drone.tello", "_KEEPALIVE_INTERVAL_S")),
    ("flightrisk/drone/tello.py", "<module>", "_KEEPALIVE_TIMEOUT_S", 5, _mc("drone.tello", "_KEEPALIVE_TIMEOUT_S")),
    ("flightrisk/drone/tello.py", "<module>", "_LAND_ATTEMPTS", 3, _mc("drone.tello", "_LAND_ATTEMPTS")),
    ("flightrisk/drone/tello.py", "<module>", "_LAND_DEADLINE_S", 25.0, _mc("drone.tello", "_LAND_DEADLINE_S")),
    ("flightrisk/drone/tello.py", "<module>", "_LAND_DESCENT_CM", 5, _mc("drone.tello", "_LAND_DESCENT_CM")),
    ("flightrisk/drone/tello.py", "<module>", "_LAND_DESCENT_STALL_S", 2.0, _mc("drone.tello", "_LAND_DESCENT_STALL_S")),
    ("flightrisk/drone/tello.py", "<module>", "_LAND_GROUND_WAIT_S", 6.0, _mc("drone.tello", "_LAND_GROUND_WAIT_S")),
    ("flightrisk/drone/tello.py", "<module>", "_LAND_STALL_PACKETS", 2, _mc("drone.tello", "_LAND_STALL_PACKETS")),
    ("flightrisk/drone/tello.py", "<module>", "_LAND_STALL_S", 1.0, _mc("drone.tello", "_LAND_STALL_S")),
    ("flightrisk/drone/tello.py", "<module>", "_MOTOR_OFF_S", 2.0, _mc("drone.tello", "_MOTOR_OFF_S")),
    ("flightrisk/drone/tello.py", "<module>", "_MOTOR_RUN_GAP_S", 1.0, _mc("drone.tello", "_MOTOR_RUN_GAP_S")),
    ("flightrisk/drone/tello.py", "<module>", "_SETTLE_BURST_GAP_S", 0.01, _mc("drone.tello", "_SETTLE_BURST_GAP_S")),
    ("flightrisk/drone/tello.py", "<module>", "_SETTLE_MIN_GAP_S", 0.05, _mc("drone.tello", "_SETTLE_MIN_GAP_S")),
    ("flightrisk/drone/tello.py", "<module>", "_STATE_FRESH_S", 0.5, _mc("drone.tello", "_STATE_FRESH_S")),
    ("flightrisk/drone/tello.py", "<module>", "_STATE_LOST_S", 3.0, _mc("drone.tello", "_STATE_LOST_S")),
    ("flightrisk/drone/tello.py", "<module>", "_TAKEOFF_CONFIRM_S", 6.0, _mc("drone.tello", "_TAKEOFF_CONFIRM_S")),
    ("flightrisk/drone/tello.py", "<module>", "_TAKEOFF_GROUND_AFTER_S", 3.0, _mc("drone.tello", "_TAKEOFF_GROUND_AFTER_S")),
    ("flightrisk/drone/tello.py", "<module>", "_TAKEOFF_LIFT_GUARD_S", 10.0, _mc("drone.tello", "_TAKEOFF_LIFT_GUARD_S")),
    ("flightrisk/drone/tello.py", "<module>", "_TAKEOFF_REPLY_TIMEOUT_S", 20.0, _mc("drone.tello", "_TAKEOFF_REPLY_TIMEOUT_S")),
    ("flightrisk/drone/tello.py", "<module>", "_TIME_BETWEEN_COMMANDS_S", 0.1, _mc("drone.tello", "_TIME_BETWEEN_COMMANDS_S")),
    ("flightrisk/drone/tello.py", "<module>", "_TOF_OUT_OF_RANGE_CM", 6553, _mc("drone.tello", "_TOF_OUT_OF_RANGE_CM")),
    ("flightrisk/drone/tello.py", "<module>.FakeTelloController", "TAKEOFF_HEIGHT_CM", 80, _mc("drone.tello", "__classes__", "FakeTelloController", "constants", "TAKEOFF_HEIGHT_CM")),
    ("flightrisk/drone/tello.py", "<module>.FakeTelloController.__init__", "max_move_cm", 500, _caps("drone.tello.FakeTelloController", "max_move_cm")),
    ("flightrisk/drone/tello.py", "<module>.FakeTelloController.__init__", "min_move_cm", 20, _caps("drone.tello.FakeTelloController", "min_move_cm")),
    ("flightrisk/drone/tello.py", "<module>.TelloController.__init__", "max_altitude_m", 10, _caps("drone.tello.TelloController", "max_altitude_m")),
    ("flightrisk/drone/tello.py", "<module>.TelloController.__init__", "max_move_cm", 500, _caps("drone.tello.TelloController", "max_move_cm")),
    ("flightrisk/drone/tello.py", "<module>.TelloController.__init__", "min_move_cm", 20, _caps("drone.tello.TelloController", "min_move_cm")),
    ("flightrisk/drone/tello_bound.py", "<module>", "TELLO_CMD_PORT", 8889, _mc("drone.tello_bound", "TELLO_CMD_PORT")),
    ("flightrisk/drone/tello_bound.py", "<module>", "TELLO_STATE_PORT", 8890, _mc("drone.tello_bound", "TELLO_STATE_PORT")),
    ("flightrisk/drone/tello_bound.py", "<module>", "TELLO_VIDEO_PORT", 11111, _mc("drone.tello_bound", "TELLO_VIDEO_PORT")),
    ("flightrisk/drone/temp_preflight.py", "<module>", "DEFAULT_TEMP_REFUSE_C", 95, _mc("drone.temp_preflight", "DEFAULT_TEMP_REFUSE_C")),
    ("flightrisk/drone/temp_preflight.py", "<module>", "DEFAULT_TEMP_WARN_C", 92, _mc("drone.temp_preflight", "DEFAULT_TEMP_WARN_C")),
    ("flightrisk/drone/temp_preflight.py", "<module>", "TEMP_READING_RANGE_C", -40, _mc("drone.temp_preflight", "TEMP_READING_RANGE_C", "__seq__", 0)),
    ("flightrisk/drone/temp_preflight.py", "<module>", "TEMP_READING_RANGE_C", 150, _mc("drone.temp_preflight", "TEMP_READING_RANGE_C", "__seq__", 1)),
    ("flightrisk/edge_device.py", "<module>", "DEFAULT_STRIDE_MAX_SKIP_S", 1.0, _mc("edge_device", "DEFAULT_STRIDE_MAX_SKIP_S")),
    ("flightrisk/edge_device.py", "<module>", "MAX_TORCH_THREADS", 64, _mc("edge_device", "MAX_TORCH_THREADS")),
    ("flightrisk/edge_device.py", "<module>", "recv_timeout", 0.01, None),
    ("flightrisk/edge_device.py", "<module>.ResilientCaptureSource", "retry_s", 1.0, _init("edge_device", "ResilientCaptureSource", "retry_s")),
    ("flightrisk/edge_device.py", "<module>._build_parser", "--speed-mps", 3.0, SO + ("parser_defaults", "speed_mps")),
    ("flightrisk/edge_device.py", "<module>._build_parser", "--takeoff-alt-m", 5.0, SO + ("parser_defaults", "takeoff_alt_m")),
    ("flightrisk/edge_profiles.py", "<module>", "BUSY_TIMEOUT_S", 60.0, _mc("edge_profiles", "BUSY_TIMEOUT_S")),
]
#: Entries per file in section 3.1 of the plan.
PLAN_COUNTS = {
    "flightrisk/drone/autopilot.py": 4, "flightrisk/drone/fake.py": 2, "flightrisk/drone/flight_guards.py": 7,
    "flightrisk/drone/lane_executor.py": 3, "flightrisk/drone/lane_flight.py": 2, "flightrisk/drone/mavlink_fleet.py": 16,
    "flightrisk/drone/mavsdk_link.py": 2, "flightrisk/drone/obstacle.py": 1, "flightrisk/drone/obstacle_sensor.py": 20,
    "flightrisk/drone/safety.py": 27, "flightrisk/drone/tello.py": 32, "flightrisk/drone/tello_bound.py": 3,
    "flightrisk/drone/temp_preflight.py": 4, "flightrisk/edge_device.py": 6, "flightrisk/edge_profiles.py": 1,
}
UNRECORDED = {("flightrisk/edge_device.py", "<module>", "recv_timeout")}


def test_the_plan_list_has_130_entries_in_the_plan_files():
    assert len(PLAN_ENTRIES) == sum(PLAN_COUNTS.values()) == 130
    counts = {}
    for file, *_ in PLAN_ENTRIES:
        counts[file] = counts.get(file, 0) + 1
    assert counts == PLAN_COUNTS


@pytest.mark.parametrize("file, scope, name, value, path", PLAN_ENTRIES,
                         ids=[f"{e[0].split('/')[-1]}:{e[2]}={e[3]}" for e in PLAN_ENTRIES])
def test_every_plan_entry_is_recorded_in_the_baseline(file, scope, name, value, path):
    if path is None:
        assert (file, scope, name) in UNRECORDED
        return
    node = FIXTURE
    for key in path:
        node = node[key]
    assert node == value and type(node) is type(value), (path, node)
