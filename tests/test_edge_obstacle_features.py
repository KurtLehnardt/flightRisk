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
from flightrisk.params.resolver import LAUNCHER_NEEDS_CONFIRMATION
from flightrisk.params.spec import Source
from flightrisk.params.table import TABLE
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


def _wire(args, drone=None, log=None):
    envelope = SafetyEnvelope.from_env(alt_slot_cm=100, environ={})
    return edge_device._build_obstacle_wiring(args, log or _Log(), drone or _Drone(), "runner", envelope, os.environ)


class _Recorder:
    """A structured log that keeps ``(level, event, fields)``."""

    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda event, **fields: self.lines.append((level, event, fields))


def _launcher(edge_env, mode, confirmed=None):
    layer = {"unit_id": "drone-1", "values": {"sensing.mode": mode}}
    if confirmed is not None:
        layer["confirmed"] = confirmed
    edge_env.setenv(LAUNCHER_PARAMS_ENV_VAR, json.dumps(layer))


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
    # The resolver leaves the fact override out of the resolution on a production unit, but today's MAVLink
    # camera edge senses with it, so the settings keep it (plan risk R6 leaves the change to B1).
    edge_env.setenv("FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH", "0.5")
    args = _resolved(_FIELD)
    assert "sensing.min_safe_depth" in args.edge_resolution.values
    assert args.edge_resolution.values["sensing.min_safe_depth"].source != Source.ENV
    _, resolved = _settings(_FIELD, args)
    assert resolved["min_safe_depth"] == 0.5


_OBSTACLE_OVERRIDES = {
    "FLIGHTRISK_OBSTACLE_TURN": ("turn", "0", False),
    "FLIGHTRISK_OBSTACLE_STEP_CM": ("step_cm", "300", 300),
    "FLIGHTRISK_OBSTACLE_SETTLE_S": ("settle_s", "0.5", 0.5),
    "FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH": ("min_safe_depth", "0.5", 0.5),
    "FLIGHTRISK_OBSTACLE_PERSON_FRACTION": ("person_fraction", "0.6", 0.6),
    "FLIGHTRISK_OBSTACLE_CLEAR_VOTES": ("clear_votes", "3", 3),
    "FLIGHTRISK_OBSTACLE_RELOOK": ("relook", "1", 1),
    "FLIGHTRISK_OBSTACLE_SIDESTEP_CM": ("sidestep_cm", "40", 40),
    "FLIGHTRISK_OBSTACLE_SIDESTEPS": ("sidesteps", "1", 1),
}


@pytest.mark.parametrize("name", sorted(_OBSTACLE_OVERRIDES))
def test_an_obstacle_override_on_a_production_mavlink_unit_is_logged_as_read_by_the_edge(edge_env, caplog, name):
    # The resolver leaves the fact override out of the resolution, but the MAVLink camera edge senses with
    # it, so the log says the edge reads it rather than that it is ignored (plan risk R6 leaves it to B1).
    setting, raw, value = _OBSTACLE_OVERRIDES[name]
    edge_env.setenv(name, raw)
    with caplog.at_level("DEBUG", logger="flightrisk.params.resolver"):
        args = _resolved(_FIELD)
    assert args.edge_resolution.simulated is False
    row = next(spec.id for spec in TABLE if spec.env == name)
    assert args.edge_resolution.values[row].source is Source.PROFILE  # No value changes in the resolution.
    lines = [(r.levelname, r.getMessage()) for r in caplog.records
             if r.name == "flightrisk.params.resolver" and name in r.getMessage()]
    assert lines == [("INFO", f"{name} is not resolved on drone-1, a production unit; the edge reads it itself, "
                              "and uses it only when the unit senses obstacles with a camera")]
    _, resolved = _settings(_FIELD, args)
    assert resolved[setting] == value


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


# Switching sensing off is less safe, so a launcher off needs the launcher's confirmation (item 47). Today's
# edge ignored the launcher value and sensed as usual; the environment variable keeps today's reader.


def test_only_sensing_mode_needs_the_launchers_confirmation():
    assert LAUNCHER_NEEDS_CONFIRMATION == frozenset({"sensing.mode"})


@pytest.mark.parametrize("argv", [_ROOM, _FIELD], ids=["room", "field"])
@pytest.mark.parametrize("confirmed", [None, {}, {"sensing.mode": "required"}], ids=["none", "empty", "other"])
def test_an_unconfirmed_launcher_sensing_off_refuses_the_start(edge_env, argv, confirmed):
    _launcher(edge_env, "off", confirmed)
    with pytest.raises(SystemExit) as refused:
        _resolved(argv)
    text = str(refused.value)
    assert "`sensing.mode` off from launcher is less safe than the" in text
    assert text.endswith("default of required. Confirm it in the launcher at Start to use it. Refusing to start.")
    assert not _StubCamera.built


def test_a_confirmation_flag_does_not_count_with_a_launcher_layer(edge_env):
    _launcher(edge_env, "off")
    argv = _ROOM + ["--confirm-less-safe", "sensing.mode=off"]
    with pytest.raises(SystemExit, match="Confirm it in the launcher at Start"):
        _resolved(argv)


@pytest.mark.parametrize("argv", [_ROOM, _FIELD], ids=["room", "field"])
def test_a_confirmed_launcher_sensing_off_flies_without_a_sensor_and_warns(edge_env, caplog, argv):
    _launcher(edge_env, "off", {"sensing.mode": "off"})
    with caplog.at_level("WARNING", logger="flightrisk.params.resolver"):
        args = _resolved(argv)
    assert args.edge_resolution.values["sensing.mode"].source == Source.LAUNCHER
    assert [(r.levelname, r.getMessage()) for r in caplog.records if "sensing.mode" in r.getMessage()] == [
        ("WARNING", "confirmed less-safe sensing.mode='off' from launcher on drone-1")]
    log = _Recorder()
    wiring = _wire(args, log=log)
    assert isinstance(wiring.sensor, NullSensor) and not _StubCamera.built
    off = [(level, fields["detail"]) for level, event, fields in log.lines if event == "edge_obstacle_sensing_off"]
    assert off == [("warning", "sensing.mode=off from launcher: this drone flies its lanes with NO obstacle sensing")]


def test_the_environment_sensing_off_is_unchanged(edge_env):
    edge_env.setenv("FLIGHTRISK_OBSTACLE_SENSING", "off")
    args = _resolved(_ROOM)
    assert args.edge_resolution.values["sensing.mode"].source == Source.ENV
    today, resolved = _settings(_ROOM, args)
    assert resolved == today and resolved["mode"] == "off"
    log = _Recorder()
    assert isinstance(_wire(args, log=log).sensor, NullSensor)
    off = [fields["detail"] for level, event, fields in log.lines if event == "edge_obstacle_sensing_off"]
    assert off == ["FLIGHTRISK_OBSTACLE_SENSING=off: this drone flies its lanes with NO obstacle sensing"]


@pytest.mark.parametrize("argv", [_ROOM, _FIELD], ids=["room", "field"])
def test_a_launcher_sensing_required_builds_the_camera_sensor_as_before(edge_env, argv):
    _launcher(edge_env, "required")
    wiring = _wire(_resolved(argv))
    assert isinstance(wiring.sensor, _StubCamera) and len(_StubCamera.built) == 1


def test_a_launcher_sensing_on_is_refused_as_before(edge_env):
    # "on" is not one of the row's choices, so the resolver refused it before A3.4 too.
    _launcher(edge_env, "on", {"sensing.mode": "on"})
    with pytest.raises(SystemExit, match=r"cannot do `sensing.mode` on from launcher: .* takes only required, off"):
        _resolved(_ROOM)


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
