"""The Tello adapter constants are read from the parameter registry (A3.5 step S16, PAR-8).

Each SDK timing, ground threshold and move limit of ``flightrisk/drone/tello.py`` must equal the
registry default of its row in the Tello profile column, with the same type, and a changed registry
value must change what the adapter looks up, so the module derives its constants instead of keeping a
copy that only happens to match. These constants decide takeoff and landing confirmation, so the test
also pins the values themselves. The changed values go into a patched copy of the table; the real
table is never edited.
"""

from __future__ import annotations

import dataclasses
import inspect
from unittest.mock import patch

import pytest

from flightrisk.drone import tello
from flightrisk.params import table as registry
from flightrisk.params.defaults import model_default

PROFILE = registry.T

_BY_ID = {spec.id: spec for spec in registry.TABLE}


def _default(param_id: str):
    return _BY_ID[param_id].defaults[PROFILE]


def _same(actual, expected) -> bool:
    return actual == expected and type(actual) is type(expected)


# (module constant, registry row, value and type before the move)
MODULE_CONSTANTS = [
    ("_COMMAND_REPLY_TIMEOUT_S", "tello_sdk.command_reply_timeout_s", 7.0),
    ("_CONNECT_ATTEMPTS", "tello_sdk.connect_attempts", 3),
    ("_GROUND_HEIGHT_CM", "tello.ground_height_cm", 10),
    ("_GROUND_READINGS", "tello.ground_readings", 3),
    ("_GROUND_TOF_CM", "tello.ground_tof_cm", 30),
    ("_KEEPALIVE_INTERVAL_S", "tello_sdk.keepalive_interval_s", 10.0),
    ("_KEEPALIVE_TIMEOUT_S", "tello_sdk.keepalive_timeout_s", 5),
    ("_LAND_ATTEMPTS", "tello_sdk.land_attempts", 3),
    ("_LAND_DEADLINE_S", "tello_sdk.land_deadline_s", 25.0),
    ("_LAND_DESCENT_CM", "tello_sdk.land_descent_cm", 5),
    ("_LAND_DESCENT_STALL_S", "tello_sdk.land_descent_stall_s", 2.0),
    ("_LAND_GROUND_WAIT_S", "tello_sdk.land_ground_wait_s", 6.0),
    ("_LAND_STALL_S", "tello_sdk.land_stall_s", 1.0),
    ("_MOTOR_OFF_S", "tello_sdk.motor_off_s", 2.0),
    ("_MOTOR_RUN_GAP_S", "tello_sdk.motor_run_gap_s", 1.0),
    ("_STATE_FRESH_S", "tello_sdk.state_fresh_s", 0.5),
    ("_STATE_LOST_S", "tello_sdk.state_lost_s", 3.0),
    ("_TAKEOFF_CONFIRM_S", "tello_sdk.takeoff_confirm_s", 6.0),
    ("_TAKEOFF_GROUND_AFTER_S", "tello_sdk.takeoff_ground_after_s", 3.0),
    ("_TAKEOFF_LIFT_GUARD_S", "tello_sdk.takeoff_lift_guard_s", 10.0),
    ("_TAKEOFF_REPLY_TIMEOUT_S", "takeoff.reply_timeout_s", 20.0),
    ("_TIME_BETWEEN_COMMANDS_S", "tello_sdk.command_gap_s", 0.1),
    ("_TOF_OUT_OF_RANGE_CM", "tello_sdk.tof_out_of_range_cm", 6553),
]

# Constants derived from the moved ones keep following them.
DERIVED_CONSTANTS = [
    ("_LAND_REPLY_TIMEOUT_S", "_COMMAND_REPLY_TIMEOUT_S"),
    ("_LINK_STATE_MAX_AGE_S", "_STATE_LOST_S"),
    ("_STREAM_REPLY_TIMEOUT_S", "_KEEPALIVE_TIMEOUT_S"),
]


@pytest.mark.parametrize("name, param_id, before", MODULE_CONSTANTS)
def test_module_constant_equals_its_registry_value(name, param_id, before):
    assert _same(getattr(tello, name), _default(param_id))


@pytest.mark.parametrize("name, param_id, before", MODULE_CONSTANTS)
def test_module_constant_keeps_its_value_and_type(name, param_id, before):
    assert _same(getattr(tello, name), before)


@pytest.mark.parametrize("name, source", DERIVED_CONSTANTS)
def test_derived_constants_follow_the_moved_ones(name, source):
    assert getattr(tello, name) == getattr(tello, source)
    assert tello._KEEPALIVE_WAIT_S == tello._KEEPALIVE_TIMEOUT_S + 1.0


def test_fake_takeoff_height_follows_the_registry():
    assert _same(tello.FakeTelloController.TAKEOFF_HEIGHT_CM, _default("takeoff.height_cm"))
    assert _same(tello.FakeTelloController.TAKEOFF_HEIGHT_CM, 80)
    default = inspect.signature(tello.FakeTelloController.__init__).parameters["takeoff_height_cm"].default
    assert _same(default, 80)
    assert tello.FakeTelloController().takeoff_height_cm == 80


def _assert_move_limits(capabilities):
    assert _same(capabilities.min_move_cm, _default("move.min_cm"))
    assert _same(capabilities.max_move_cm, _default("move.max_cm"))
    assert (capabilities.min_move_cm, capabilities.max_move_cm, capabilities.max_altitude_m) == (20, 500, 10)


def test_fake_capabilities_follow_the_registry():
    _assert_move_limits(tello.FakeTelloController().capabilities)


def test_controller_capabilities_follow_the_registry():
    with patch("flightrisk.drone.tello.Tello"):
        controller = tello.TelloController(host="192.0.2.1")
    _assert_move_limits(controller.capabilities)


def _patched_table(monkeypatch, param_id: str, default):
    """Replace ``registry.TABLE`` with a copy whose ``param_id`` row has a new Tello-column default."""
    real = registry.TABLE
    rows = [
        dataclasses.replace(spec, defaults={**spec.defaults, PROFILE: default}) if spec.id == param_id else spec
        for spec in real
    ]
    monkeypatch.setattr(registry, "TABLE", tuple(rows))
    return real


def test_patched_table_leaves_the_real_table_alone(monkeypatch):
    real = _patched_table(monkeypatch, "tello_sdk.takeoff_confirm_s", 9.5)
    assert registry.TABLE is not real
    assert {spec.id: spec for spec in real}["tello_sdk.takeoff_confirm_s"].defaults[PROFILE] == 6.0


ALL_ROWS = [(name, param_id) for name, param_id, _ in MODULE_CONSTANTS] + [
    ("TAKEOFF_HEIGHT_CM", "takeoff.height_cm"),
    ("min_move_cm", "move.min_cm"),
    ("max_move_cm", "move.max_cm"),
]


@pytest.mark.parametrize("param_id", [param_id for _, param_id in ALL_ROWS])
def test_changing_a_registry_value_changes_the_lookup(monkeypatch, param_id):
    before = model_default(param_id, PROFILE)
    new = before * 2 + 1
    _patched_table(monkeypatch, param_id, new)
    assert model_default(param_id, PROFILE) == new != before


@pytest.mark.parametrize("param_id", ["move.min_cm", "move.max_cm"])
def test_changing_a_move_row_changes_new_capabilities(monkeypatch, param_id):
    _patched_table(monkeypatch, param_id, 77)
    field = param_id.split(".")[1].replace("_cm", "_move_cm")
    assert getattr(tello.FakeTelloController().capabilities, field) == 77
    with patch("flightrisk.drone.tello.Tello"):
        assert getattr(tello.TelloController(host="192.0.2.1").capabilities, field) == 77


def test_controller_max_altitude_is_row_8_in_whole_metres():
    """Row 8 (envelope.max_altitude_cm) is FlightRisk's 10 m Tello ceiling; the controller declares it as an int."""
    assert _same(_default("envelope.max_altitude_cm"), 1000)
    with patch("flightrisk.drone.tello.Tello"):
        assert _same(tello.TelloController(host="192.0.2.1").capabilities.max_altitude_m, 10)


def test_changing_row_8_changes_new_controller_max_altitude(monkeypatch):
    _patched_table(monkeypatch, "envelope.max_altitude_cm", 750)
    with patch("flightrisk.drone.tello.Tello"):
        assert _same(tello.TelloController(host="192.0.2.1").capabilities.max_altitude_m, 7)


def test_unknown_row_is_an_error():
    with pytest.raises(KeyError):
        model_default("no.such_row", PROFILE)
