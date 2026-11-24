"""The edge builds its obstacle wiring from the resolved values (Phase A3, step S14; EDGE-2, SEAM-4, FEA-7, FACT-3).

``ObstacleSettings.resolved`` reads ``args.edge_resolution`` in the start path; a direct constructor call and a
start without a resolution keep today's settings exactly. The camera sensor is built only when forward
obstacle sensing is available and on (SEAM-4). Required sensing without it needs the autopilot's avoidance at
takeoff, which ``_tell_mavlink_drone`` hands to the MAVLink drone's preflight (FEA-7).
"""

from __future__ import annotations

import dataclasses
import json
import os

import pytest

from flightrisk import edge_device
from flightrisk.drone import mavlink_fleet, obstacle_camera
from flightrisk.drone.controller import Capability, DroneCapabilities
from flightrisk.drone.obstacle_sensor import NullSensor
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.edge_resolution import LAUNCHER_PARAMS_ENV_VAR, OBSTACLE_ROWS, PARAMS_FILE_ENV_VAR
from flightrisk.hardware.features import FORWARD_OBSTACLE_SENSING
from flightrisk.params.spec import Source
from tests.test_edge_envelope_resolved import _FAKE, _FIELD, _ROOM, _args, _Log
from tests.test_edge_guards_resolved import _resolved, _with_layer, edge_env  # noqa: F401 - fixture


class _StubCamera:
    built: list = []

    def __init__(self, frames, runner=None, **kwargs):
        _StubCamera.built.append(kwargs)


@pytest.fixture(autouse=True)
def stub_camera(monkeypatch):
    _StubCamera.built = []
    monkeypatch.setattr(obstacle_camera, "ForwardCameraSensor", _StubCamera)


class _Drone:
    """A drone with an ``on_frame`` hook and the MAVLink sensing setter; declares what it is given."""

    def __init__(self, avoidance=False):
        caps = {Capability.ONBOARD_AVOIDANCE} if avoidance else set()
        self.capabilities = DroneCapabilities(features=frozenset(caps))
        self.modes = []

    def on_frame(self, callback):
        pass

    def set_obstacle_sensing(self, mode):
        self.modes.append(mode)


def _kind(argv):
    return edge_device._drone_kind(_args(argv))


def _settings(argv, args=None):
    args = args or _resolved(argv)
    envelope = SafetyEnvelope.from_env(environ={})
    return (vars(edge_device.ObstacleSettings(_kind(argv), envelope, os.environ)),
            vars(edge_device.ObstacleSettings.resolved(_kind(argv), envelope, os.environ, args.edge_resolution)))


def _wire(args, drone=None):
    envelope = SafetyEnvelope.from_env(alt_slot_cm=100, environ={})
    return edge_device._build_obstacle_wiring(args, _Log(), drone or _Drone(), "runner", envelope, os.environ)


def _gate_off(args):
    resolution = args.edge_resolution
    gates = dict(resolution.gates)
    gates[FORWARD_OBSTACLE_SENSING] = {**gates[FORWARD_OBSTACLE_SENSING], "on": False, "ok": False}
    args.edge_resolution = dataclasses.replace(resolution, gates=gates)
    return args


# --- No resolution and today's inputs ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", [None, "fake", "mavlink"])
def test_without_a_resolution_the_settings_are_todays(kind):
    environ = {"FLIGHTRISK_OBSTACLE_STEP_CM": "60", "FLIGHTRISK_OBSTACLE_TURN": "0"}
    envelope = SafetyEnvelope.from_env(environ={})
    today = edge_device.ObstacleSettings(kind, envelope, environ)
    assert vars(edge_device.ObstacleSettings.resolved(kind, envelope, environ, None)) == vars(today)


@pytest.mark.parametrize("argv", [_ROOM, _FAKE, _FIELD], ids=["room", "fake", "field"])
def test_todays_start_gives_todays_settings(edge_env, argv):
    today, resolved = _settings(argv)
    assert resolved == today


@pytest.mark.parametrize("argv", [_ROOM, _FIELD], ids=["room", "field"])
def test_an_environment_value_keeps_todays_reader(edge_env, argv):
    edge_env.setenv("FLIGHTRISK_OBSTACLE_STEP_CM", "60" if argv is _ROOM else "300")
    edge_env.setenv("FLIGHTRISK_OBSTACLE_SIDESTEP_CM", "40")
    today, resolved = _settings(argv)
    assert resolved == today
    assert resolved["step_cm"] == (60 if argv is _ROOM else 300)


def test_a_production_mavlink_unit_still_flies_todays_obstacle_override(edge_env):
    # The resolver ignores the fact override on a production unit, but today's MAVLink camera edge senses
    # with it, so the settings keep it (plan risk R6 leaves the change to B1).
    edge_env.setenv("FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH", "0.5")
    args = _resolved(_FIELD)
    assert "sensing.min_safe_depth" in args.edge_resolution.values
    assert args.edge_resolution.values["sensing.min_safe_depth"].source != Source.ENV
    _, resolved = _settings(_FIELD, args)
    assert resolved["min_safe_depth"] == 0.5


# --- The launcher layer and the params file ---------------------------------------------------------------


@pytest.mark.parametrize("row", sorted(set(OBSTACLE_ROWS.values()) - {"sensing.mode", "sensing.max_decode_lag_s"}))
def test_a_launcher_value_for_an_obstacle_fact_refuses_the_start(edge_env, row):
    edge_env.setenv(LAUNCHER_PARAMS_ENV_VAR, json.dumps({"unit_id": "drone-1", "values": {row: 1}}))
    with pytest.raises(SystemExit, match=r"Refusing to start\.$"):
        _resolved(_ROOM)


def test_a_params_file_value_for_an_obstacle_fact_refuses_the_start(edge_env, tmp_path):
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"version": 1, "units": {"drone-1": {"sensing.step_cm": 60}}}), encoding="utf-8")
    edge_env.setenv(PARAMS_FILE_ENV_VAR, str(path))
    with pytest.raises(SystemExit, match=r"Refusing to start\.$"):
        _resolved(_ROOM)


def test_a_launcher_sensing_mode_reaches_the_settings(edge_env):
    edge_env.setenv(LAUNCHER_PARAMS_ENV_VAR, json.dumps({"unit_id": "drone-1", "values": {"sensing.mode": "required"}}))
    args = _resolved(_ROOM)
    assert args.edge_resolution.values["sensing.mode"].source == Source.LAUNCHER
    _, resolved = _settings(_ROOM, args)
    assert resolved["mode"] == "required"


def test_the_environment_beats_the_launcher(edge_env):
    edge_env.setenv(LAUNCHER_PARAMS_ENV_VAR, json.dumps({"unit_id": "drone-1", "values": {"sensing.mode": "required"}}))
    edge_env.setenv("FLIGHTRISK_OBSTACLE_SENSING", "off")
    args = _resolved(_ROOM)
    assert args.edge_resolution.values["sensing.mode"].source == Source.ENV
    _, resolved = _settings(_ROOM, args)
    assert resolved["mode"] == "off"


@pytest.mark.parametrize("source", [Source.LAUNCHER, Source.FILE, Source.PROFILE])
def test_a_resolved_value_from_another_layer_reaches_the_settings(edge_env, source):
    args = _with_layer(_resolved(_ROOM), source, {"sensing.step_cm": 70, "avoid.sidestep_cm": 30})
    today, resolved = _settings(_ROOM, args)
    assert (resolved["step_cm"], resolved["sidestep_cm"]) == (70, 30)
    assert ("FLIGHTRISK_OBSTACLE_STEP_CM", 70, 50) in resolved["non_default"]
    assert today["step_cm"] == 50


# --- SEAM-4: the camera sensor needs forward obstacle sensing -------------------------------------------


@pytest.mark.parametrize("argv", [_ROOM, _FIELD], ids=["room", "field"])
def test_todays_start_with_sensing_on_builds_the_camera_sensor(edge_env, argv):
    wiring = _wire(_resolved(argv))
    assert isinstance(wiring.sensor, _StubCamera) and len(_StubCamera.built) == 1


def test_todays_start_with_sensing_off_builds_no_camera_sensor(edge_env):
    edge_env.setenv("FLIGHTRISK_OBSTACLE_SENSING", "off")
    wiring = _wire(_resolved(_ROOM))
    assert isinstance(wiring.sensor, NullSensor) and not _StubCamera.built


def test_no_camera_sensor_when_forward_sensing_is_unavailable(edge_env):
    wiring = _wire(_gate_off(_resolved(_FIELD)))
    assert wiring.sensor is None and wiring.sensor_error is None and not _StubCamera.built


def test_a_drone_without_a_takeoff_avoidance_check_refuses_to_fly_without_the_sensor(edge_env):
    wiring = _wire(_gate_off(_resolved(_ROOM)))
    assert not _StubCamera.built
    assert wiring.sensor is None and wiring.sensor_error
    assert "obstacle_sensor_error" in wiring.executor_kwargs()


# --- FEA-7: required sensing ----------------------------------------------------------------------------


def test_required_sensing_is_accepted_when_forward_sensing_is_available(edge_env):
    drone = _Drone()
    assert edge_device._tell_mavlink_drone(drone, _wire(_resolved(_FIELD), drone)) == "stepped"


def test_required_sensing_falls_back_to_onboard_avoidance_at_takeoff(edge_env):
    drone = _Drone(avoidance=True)
    mode = edge_device._tell_mavlink_drone(drone, _wire(_gate_off(_resolved(_FIELD)), drone))
    assert mode == "autopilot" and mode in mavlink_fleet._NEEDS_AVOIDANCE_MODES


def test_required_sensing_without_onboard_avoidance_is_refused_at_takeoff(edge_env):
    drone = _Drone(avoidance=False)
    mode = edge_device._tell_mavlink_drone(drone, _wire(_gate_off(_resolved(_FIELD)), drone))
    # ``none``: the MAVLink drone's preflight refuses every takeoff until avoidance_status() is empty.
    assert mode == mavlink_fleet.SENSING_NONE and mode in mavlink_fleet._NEEDS_AVOIDANCE_MODES
