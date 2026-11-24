"""The edge takes its flight guards and temperature levels from the resolved values (Phase A3, step S12, EDGE-2).

``_flight_guards`` and ``_temp_limits`` read ``args.edge_resolution`` once the edge has resolved its unit.
With today's inputs they are exactly today's (the S1 fixture). Every guard and temperature row is a fact
of the drone, so the params file and the launcher layer cannot set one (FACT-2): such a value refuses
the start. A resolved value from any layer other than the environment or the command line reaches the
settings; an environment value keeps today's reader; a test-adapter override on a production MAVLink
unit reaches neither.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
from types import SimpleNamespace

import pytest

from flightrisk import edge_device
from flightrisk.drone.flight_guards import FlightGuards
from flightrisk.drone.temp_preflight import TempLimits
from flightrisk.edge_resolution import (
    GUARD_ROWS,
    LAUNCHER_PARAMS_ENV_VAR,
    PARAMS_FILE_ENV_VAR,
    TEMP_ROWS,
)
from flightrisk.hardware.inventory import ENV_FILE
from flightrisk.params.spec import Source
from tests.test_edge_envelope_resolved import (
    _FAKE,
    _FIELD,
    _PARSER,
    _ROOM,
    FIXTURE,
    _args,
    _jsonable,
    _Log,
)
from tests.test_param_parity import G


class ReachedTheExecutor(Exception):
    """Raised by the patched ``LaneExecutor``: the start was not refused."""


@pytest.fixture
def edge_env(monkeypatch, tmp_path):
    """No FlightRisk variable, no inventory and no params file unless a test adds one."""
    for name in [n for n in os.environ if n.startswith(G)]:
        monkeypatch.delenv(name)
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "no-inventory.json"))
    monkeypatch.setenv(PARAMS_FILE_ENV_VAR, str(tmp_path / "no-params.json"))
    return monkeypatch


def _resolved(argv):
    args = _args(argv)
    edge_device._resolve_edge_start(args, _Log(), argv=argv, parser=_PARSER)
    assert args.edge_resolution is not None
    return args


def _settings(argv):
    """Today's guards and levels (no resolution) and the ones built from the resolution, for ``argv``."""
    today = edge_device._flight_guards(os.environ), edge_device._temp_limits(None, None)
    args = _resolved(argv)
    return today, (edge_device._flight_guards(os.environ, args), edge_device._temp_limits(None, None, args=args)), args


def _with_layer(args, source, values):
    """``args`` whose resolution holds ``values`` ({row: value}) as if the layer ``source`` had set them."""
    resolution = args.edge_resolution
    changed = dict(resolution.values)
    for row, value in values.items():
        changed[row] = dataclasses.replace(resolution.values[row], value=value, source=source)
    args.edge_resolution = dataclasses.replace(resolution, values=changed)
    return args


# --- Today's inputs build today's settings ---------------------------------------------------------------


@pytest.mark.parametrize("argv", [_ROOM, _FAKE, _FIELD])
def test_with_no_input_the_resolved_settings_are_the_baseline(edge_env, argv):
    _, (guards, limits), _ = _settings(argv)
    assert _jsonable(guards) == FIXTURE["start_objects"]["flight_guards_empty_env"]
    assert _jsonable(limits) == FIXTURE["start_objects"]["temp_limits_from_env_empty"]


@pytest.mark.parametrize("argv", [_ROOM, _FIELD])
def test_with_nothing_new_resolved_the_objects_are_kept(edge_env, argv):
    args = _resolved(argv)
    guards, limits = FlightGuards(), TempLimits()
    assert edge_device._resolved_flight_guards(args, guards) is guards
    assert edge_device._resolved_temp_limits(args, limits) is limits


def test_the_module_defaults_are_the_registry_values():
    from flightrisk.drone import flight_guards, temp_preflight
    from flightrisk.params import table

    rows = {spec.id: spec.defaults[table.T] for spec in table.TABLE}
    for name, row in GUARD_ROWS.items():
        assert getattr(flight_guards, "DEFAULT_" + name.upper()) == rows[row]
    assert (temp_preflight.DEFAULT_TEMP_WARN_C, temp_preflight.DEFAULT_TEMP_REFUSE_C) == (
        rows[TEMP_ROWS["warn_c"]], rows[TEMP_ROWS["refuse_c"]])
    assert temp_preflight.TEMP_READING_RANGE_C == rows["tello_sdk.temp_reading_range_c"] == (-40, 150)


# --- The params file and the launcher layer ---------------------------------------------------------------


@pytest.mark.parametrize("row", sorted(set(GUARD_ROWS.values()) | set(TEMP_ROWS.values())))
def test_a_launcher_value_for_a_guard_or_temperature_fact_refuses_the_start(edge_env, row):
    edge_env.setenv(LAUNCHER_PARAMS_ENV_VAR, json.dumps({"unit_id": "drone-1", "values": {row: 3}}))
    with pytest.raises(SystemExit, match=r"is not a setting and cannot be stored\. Refusing to start\.$"):
        _resolved(_ROOM)


def test_a_params_file_value_for_a_guard_fact_refuses_the_start(edge_env, tmp_path):
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"version": 1, "units": {"drone-1": {"takeoff.late_s": 5}}}), encoding="utf-8")
    edge_env.setenv(PARAMS_FILE_ENV_VAR, str(path))
    with pytest.raises(SystemExit, match=r"is not a setting and cannot be stored\. Refusing to start\.$"):
        _resolved(_ROOM)


@pytest.mark.parametrize("source", [Source.FILE, Source.LAUNCHER, Source.PROFILE])
def test_a_resolved_value_from_a_stored_layer_reaches_the_guards_and_the_levels(edge_env, source):
    args = _with_layer(_resolved(_ROOM), source, {
        "takeoff.late_s": 5.0, "takeoff.settle_samples": 7, "tello.refusals_per_flight": 1,
        "tello.temp_warn_c": 90, "tello.temp_refuse_c": 93,
    })
    guards = edge_device._flight_guards(os.environ, args)
    assert (guards.takeoff_late_s, guards.settle_samples, guards.refusals_per_flight) == (5.0, 7, 1)
    assert guards.settle_timeout_s == FlightGuards().settle_timeout_s
    assert edge_device._temp_limits(None, None, args=args) == TempLimits(warn_c=90, refuse_c=93)


def test_a_resolved_refusal_switched_off_is_stored_as_none(edge_env):
    args = _with_layer(_resolved(_ROOM), Source.PROFILE, {"tello.temp_refuse_c": "off"})
    assert edge_device._temp_limits(None, None, args=args) == TempLimits(refuse_c=None)


def test_an_invalid_resolved_setting_refuses_the_start(edge_env):
    args = _with_layer(_resolved(_ROOM), Source.LAUNCHER, {"takeoff.late_s": 50.0, "tello.temp_warn_c": 20})
    with pytest.raises(SystemExit, match=r"^edge_device: the resolved flight guard settings are invalid: .*\.$"):
        edge_device._flight_guards(os.environ, args)
    with pytest.raises(SystemExit, match=r"^edge_device: the resolved temperature levels are invalid: .*\.$"):
        edge_device._temp_limits(None, None, args=args)


# --- The environment -----------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [_ROOM, _FAKE])
def test_an_environment_value_keeps_todays_reader(edge_env, argv):
    edge_env.setenv(G + "TELLO_TAKEOFF_LATE_S", "5")
    edge_env.setenv(G + "TELLO_TEMP_REFUSE_C", "off")
    today, resolved, args = _settings(argv)
    assert args.edge_resolution.values["takeoff.late_s"].source is Source.ENV
    assert resolved == today
    assert (resolved[0].takeoff_late_s, resolved[1].refuse_c) == (5.0, None)


def test_the_environment_wins_over_the_launcher(edge_env):
    edge_env.setenv(G + "TELLO_TAKEOFF_LATE_S", "5")
    args = _resolved(_ROOM)
    # A launcher value under the environment's: the resolution keeps the environment's, and so do the guards.
    assert args.edge_resolution.values["takeoff.late_s"].source is Source.ENV
    _with_layer(args, Source.LAUNCHER, {"takeoff.settle_samples": 7})
    guards = edge_device._flight_guards(os.environ, args)
    assert (guards.takeoff_late_s, guards.settle_samples) == (5.0, 7)


def test_the_hover_test_keeps_todays_reader(edge_env):
    edge_env.setenv(G + "HOVER_TEST", "1")
    today, (guards, _), _ = _settings(_FIELD)
    assert today[0].hover_test is True
    assert guards.hover_test is True


def test_the_hover_test_on_a_production_mavlink_unit_is_not_logged_as_ignored(edge_env, caplog):
    edge_env.setenv(G + "HOVER_TEST", "1")
    with caplog.at_level(logging.DEBUG, logger="flightrisk.params.resolver"):
        _, (guards, _), args = _settings(_FIELD)
    assert args.edge_resolution.simulated is False
    assert guards.hover_test is True  # The edge honours it.
    lines = [r.getMessage() for r in caplog.records if r.name == "flightrisk.params.resolver"]
    hover = [line for line in lines if "FLIGHTRISK_HOVER_TEST" in line]
    assert hover and not any("ignored" in line for line in hover)
    assert any("the edge reads it itself" in line for line in hover)


def test_only_the_hover_test_is_read_by_the_edge():
    from flightrisk.params.resolver import READ_BY_THE_EDGE

    assert READ_BY_THE_EDGE == frozenset({"mode.hover_test"})


# --- Test-adapter overrides on a production MAVLink unit --------------------------------------------------


def test_a_tello_fact_override_on_a_production_mavlink_unit_reaches_neither(edge_env):
    for short, raw in {"TELLO_TAKEOFF_LATE_S": "5", "TELLO_SETTLE_SAMPLES": "7", "TELLO_REFUSALS_PER_FLIGHT": "1",
                       "TELLO_TEMP_WARN_C": "90", "TELLO_TEMP_REFUSE_C": "off"}.items():
        edge_env.setenv(G + short, raw)
    (today_guards, today_limits), (guards, limits), args = _settings(_FIELD)
    assert args.edge_resolution.simulated is False
    assert not set(GUARD_ROWS.values()) & set(args.edge_resolution.values)
    assert (today_guards.takeoff_late_s, today_limits.refuse_c) == (5.0, None)  # Today's reader honours them.
    assert _jsonable(guards) == FIXTURE["start_objects"]["flight_guards_empty_env"]
    assert _jsonable(limits) == FIXTURE["start_objects"]["temp_limits_from_env_empty"]


def test_a_malformed_tello_override_still_refuses_a_mavlink_start(edge_env):
    edge_env.setenv(G + "TELLO_TAKEOFF_LATE_S", "soon")
    with pytest.raises(SystemExit, match="Refusing to start"):
        edge_device._flight_guards(os.environ, _args(_FIELD))


@pytest.mark.parametrize("argv,guards,limits", [
    (_ROOM, FlightGuards(takeoff_late_s=5.0), TempLimits(refuse_c=None)),
    (_FIELD, FlightGuards(), TempLimits()),
])
def test_the_flight_edge_flies_the_resolved_settings(edge_env, argv, guards, limits):
    edge_env.setenv(G + "TELLO_TAKEOFF_LATE_S", "5")
    edge_env.setenv(G + "TELLO_TEMP_REFUSE_C", "off")
    seen = {}

    def executor(*args, **kwargs):
        seen.update(kwargs)
        raise ReachedTheExecutor()

    edge_env.setattr(edge_device, "LaneExecutor", executor)
    edge_env.setattr(edge_device, "_build_runner", lambda *a, **k: SimpleNamespace())
    edge_env.setattr(edge_device, "_build_drone", lambda args, log: SimpleNamespace())
    edge_env.setattr(edge_device, "_build_obstacle_wiring",
                     lambda *a, **k: SimpleNamespace(executor_kwargs=dict))
    edge_env.setattr(edge_device, "_build_transport", lambda *a, **k: None)
    edge_env.setattr(edge_device.EdgeFrames, "for_drone", staticmethod(lambda drone: None))
    with pytest.raises(ReachedTheExecutor):
        edge_device._run_flight_edge(_args(argv), _Log(), stop_event=threading.Event(), argv=argv, parser=_PARSER)
    assert (seen["flight_guards"], seen["temp_limits"]) == (guards, limits)


# --- Where no resolution exists, the settings are today's -------------------------------------------------


def test_without_a_resolution_the_readers_are_todays(edge_env):
    edge_env.setenv(G + "TELLO_TAKEOFF_LATE_S", "5")
    edge_env.setenv(G + "TELLO_TEMP_WARN_C", "90")
    for argv in (_ROOM, _FIELD):
        args = _args(argv)
        assert getattr(args, "edge_resolution", None) is None
        assert edge_device._flight_guards(os.environ, args) == edge_device._flight_guards(os.environ)
        assert edge_device._temp_limits(None, None, args=args) == edge_device._temp_limits(None, None)
    assert _jsonable(edge_device._flight_guards({}, _args(_FIELD))) == FIXTURE["start_objects"]["flight_guards_empty_env"]
    assert _jsonable(edge_device._temp_limits(None, None, {}, args=_args(_ROOM))) == (
        FIXTURE["start_objects"]["temp_limits_from_env_empty"])
