"""The edge resolves its unit at start and refuses before it contacts the drone (A3 S6).

Covers EDGE-1 (every refusal exits with the standard message before the drone is built, so a
recording fake drone sees no call), CAP-6's fake part (only the fake adapter is simulated), the
static feature gates of FEA-7 and FEA-10, RES-11 at the edge, and the launcher layer written for
another unit. With no new input a Tello, a fake and a MAVLink start resolve to the registry
defaults, which are today's values.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from types import SimpleNamespace

import pytest

from flightrisk import edge_device
from flightrisk.edge_resolution import (
    AIRFRAME_ENV_VAR,
    LAUNCHER_PARAMS_ENV_VAR,
    MODEL_ENV_VAR,
    PARAMS_FILE_ENV_VAR,
    EdgeLayers,
    EdgeStartRefused,
    ModelChoice,
    choose_model,
    resolve_edge,
    static_feature_gates,
)
from flightrisk.hardware.catalogue import BUILTIN_MODELS
from flightrisk.hardware.features import FEATURES, FORWARD_OBSTACLE_SENSING
from flightrisk.hardware.inventory import ENV_FILE
from flightrisk.params.spec import Source
from flightrisk.params.table import TABLE

_PARSER = edge_device._build_parser()
_LESS_SAFE_ID = "battery.min_takeoff_pct"  # Tello default 30, safer higher: 28 is less safe.
# A Tello test-adapter fact with an environment override.
_FACT_ID = "takeoff.late_s"
_FACT_ENV = "FLIGHTRISK_TELLO_TAKEOFF_LATE_S"
_MAVLINK = ["--drone", "mavlink", "--frame-heading-deg", "0", "--mavlink-address", "tcpout://127.0.0.1:1"]


class RecordingDrone:
    """A fake drone that records every call made on it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def record(*args, **kwargs):
            self.calls.append(name)
            return None

        return record


class ReachedTheDrone(Exception):
    """Raised by the patched ``_build_drone``: the start was not refused."""


@pytest.fixture
def edge_env(monkeypatch, tmp_path):
    """No model input, no inventory, no params file and no launcher layer unless a test adds one."""
    for name in (MODEL_ENV_VAR, AIRFRAME_ENV_VAR, LAUNCHER_PARAMS_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "no-inventory.json"))
    monkeypatch.setenv(PARAMS_FILE_ENV_VAR, str(tmp_path / "no-params.json"))
    return monkeypatch


@pytest.fixture
def drone_calls(monkeypatch):
    """Patch the drone and the runner; the list holds every drone built and every call made on one."""
    drones: list[RecordingDrone] = []

    def build_drone(args, log):
        drone = RecordingDrone()
        drones.append(drone)
        raise ReachedTheDrone()

    monkeypatch.setattr(edge_device, "_build_drone", build_drone)
    monkeypatch.setattr(edge_device, "_build_runner", lambda *a, **k: SimpleNamespace())
    return drones


def _args(argv):
    args = _PARSER.parse_args(argv)
    edge_device._resolve_drone_and_source(args, _PARSER)
    return args


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        return lambda event, **fields: self.events.append((event, fields))


def _start(argv):
    """Run ``_run_flight_edge`` as ``main`` does; returns the exit message, or None when the drone was reached."""
    args = _args(argv)
    try:
        edge_device._run_flight_edge(args, _Log(), stop_event=threading.Event(), argv=argv, parser=_PARSER)
    except ReachedTheDrone:
        return None
    except SystemExit as exc:
        return str(exc.code)
    raise AssertionError("the edge neither refused nor reached the drone")


def _refused(argv, drone_calls):
    message = _start(argv)
    assert message is not None, "the start was not refused"
    assert drone_calls == [] or all(d.calls == [] for d in drone_calls)
    assert drone_calls == [], "the drone was built before the refusal"
    assert message.startswith("edge_device: ") and message.endswith("Refusing to start.")
    return message


def _params_file(tmp_path, monkeypatch, units):
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"version": 1, "units": units}), encoding="utf-8")
    monkeypatch.setenv(PARAMS_FILE_ENV_VAR, str(path))


def _inventory(tmp_path, monkeypatch, units):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"version": 1, "rev": 1, "units": units}), encoding="utf-8")
    monkeypatch.setenv(ENV_FILE, str(path))


# --- Unchanged starts -------------------------------------------------------------------------------------


@pytest.mark.parametrize("argv,model_id", [
    (["--source", "fake", "--drone-id", "drone-1"], "tello"),
    (["--source", "tello", "--drone-id", "drone-1"], "tello"),
    (_MAVLINK + ["--source", "camera", "--drone-id", "drone-1"], "generic_multirotor"),
])
def test_a_start_with_no_new_input_resolves_to_the_defaults(edge_env, argv, model_id):
    args = _args(argv)
    edge_device._resolve_edge_start(args, _Log(), argv=argv, parser=_PARSER)
    resolution = args.edge_resolution
    profile = BUILTIN_MODELS[model_id].profile
    expected = {spec.id: spec.defaults[profile] for spec in TABLE if spec.defaults.get(profile) is not None}
    assert {k: v.value for k, v in resolution.values.items()} == expected
    assert {v.source for v in resolution.values.values()} == {Source.PROFILE}
    assert all(g["src"] == "default" for g in resolution.gates.values())
    assert resolution.report.unit_rev
    assert resolution.simulated is (args.source == "fake")


def test_an_unrefused_start_reaches_the_drone_with_the_resolution_kept(edge_env, drone_calls):
    assert _start(["--source", "fake", "--drone-id", "drone-1"]) is None
    assert len(drone_calls) == 1


def test_the_resolution_runs_once(edge_env, monkeypatch):
    args = _args(["--source", "fake", "--drone-id", "drone-1"])
    edge_device._resolve_edge_start(args, _Log())
    first = args.edge_resolution
    monkeypatch.setattr(edge_device, "_drone_kind", lambda a: pytest.fail("resolved twice"))
    edge_device._resolve_edge_start(args, _Log())
    assert args.edge_resolution is first


def test_main_passes_its_command_line_to_the_resolution(edge_env, monkeypatch):
    seen = {}
    monkeypatch.setattr(edge_device, "_run_flight_edge", lambda args, log, **kw: seen.update(kw))
    monkeypatch.setattr(edge_device, "_init_edge_telemetry", lambda log: False)
    monkeypatch.setattr(edge_device, "_limit_torch_threads", lambda *a: None)
    argv = ["--source", "fake", "--drone-id", "drone-1"]
    edge_device.main(argv)
    assert seen["argv"] == argv and seen["parser"] is not None


# --- CAP-6: only the fake adapter is simulated ------------------------------------------------------------


@pytest.mark.parametrize("source,simulated", [("fake", True), ("tello", False)])
def test_only_the_fake_adapter_is_simulated(edge_env, source, simulated):
    args = _args(["--source", source, "--drone-id", "drone-1"])
    edge_device._resolve_edge_start(args, _Log())
    assert args.edge_resolution.simulated is simulated


@pytest.mark.parametrize("source", ["fake", "tello"])
def test_a_tello_fact_override_is_honoured_on_the_test_adapter(edge_env, source):
    edge_env.setenv(_FACT_ENV, "12")
    args = _args(["--source", source, "--drone-id", "drone-1"])
    edge_device._resolve_edge_start(args, _Log())
    value = args.edge_resolution.values[_FACT_ID]
    assert (value.source, value.value) == (Source.ENV, 12)


def test_a_mavlink_unit_is_production_and_ignores_fact_overrides(edge_env):
    # IGNORED_ON_PRODUCTION_UNIT of tests/test_param_parity.py: no MAVLink unit is simulated until B1.
    edge_env.setenv("FLIGHTRISK_OBSTACLE_STEP_CM", "300")
    args = _args(_MAVLINK + ["--source", "camera", "--drone-id", "drone-1"])
    edge_device._resolve_edge_start(args, _Log())
    assert args.edge_resolution.simulated is False
    value = args.edge_resolution.values["sensing.step_cm"]
    assert (value.source, value.value) == (Source.PROFILE, 500)


# --- Refusals before the drone is contacted ---------------------------------------------------------------


def test_a_launcher_layer_for_another_unit_is_refused(edge_env, drone_calls):
    edge_env.setenv(LAUNCHER_PARAMS_ENV_VAR, json.dumps({"unit_id": "drone-9"}))
    message = _refused(["--source", "fake", "--drone-id", "drone-1"], drone_calls)
    assert "drone-9" in message and "drone-1" in message


def test_a_launcher_layer_for_this_unit_is_accepted(edge_env, drone_calls):
    edge_env.setenv(LAUNCHER_PARAMS_ENV_VAR, json.dumps({"unit_id": "drone-1"}))
    assert _start(["--source", "fake", "--drone-id", "drone-1"]) is None


def test_an_unknown_id_in_the_launcher_layer_is_refused(edge_env, drone_calls):
    edge_env.setenv(LAUNCHER_PARAMS_ENV_VAR, json.dumps({"values": {"no.such_id": 1}}))
    assert "no.such_id" in _refused(["--source", "fake", "--drone-id", "drone-1"], drone_calls)


def test_a_fact_in_the_launcher_layer_is_refused_without_naming_it(edge_env, drone_calls):
    edge_env.setenv(LAUNCHER_PARAMS_ENV_VAR, json.dumps({"values": {_FACT_ID: 12}}))
    message = _refused(["--source", "tello", "--drone-id", "drone-1"], drone_calls)
    assert _FACT_ID not in message and _FACT_ENV not in message


def test_an_unconfirmed_less_safe_params_file_value_is_refused(edge_env, tmp_path, drone_calls):
    _params_file(tmp_path, edge_env, {"drone-1": {_LESS_SAFE_ID: 28}})
    message = _refused(["--source", "fake", "--drone-id", "drone-1"], drone_calls)
    assert f"--confirm-less-safe {_LESS_SAFE_ID}=28" in message


def test_a_confirmation_for_another_value_does_not_count(edge_env, tmp_path, drone_calls):
    _params_file(tmp_path, edge_env, {"drone-1": {_LESS_SAFE_ID: 28}})
    _refused(["--source", "fake", "--drone-id", "drone-1", "--confirm-less-safe", f"{_LESS_SAFE_ID}=27"],
             drone_calls)


def test_an_exactly_confirmed_less_safe_value_starts(edge_env, tmp_path, drone_calls):
    _params_file(tmp_path, edge_env, {"drone-1": {_LESS_SAFE_ID: 28}})
    assert _start(["--source", "fake", "--drone-id", "drone-1", "--confirm-less-safe", f"{_LESS_SAFE_ID}=28"]) is None


def test_a_cross_value_constraint_today_accepts_is_refused(edge_env, drone_calls):
    # K-6, pinned in ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT: a link-loss detect time under 1.5 heartbeats.
    edge_env.setenv("FLIGHTRISK_LINK_LOSS_LAND_S", "1.4")
    _refused(["--source", "fake", "--drone-id", "drone-1"], drone_calls)


def test_a_stored_safety_feature_switched_off_is_refused(edge_env, tmp_path, drone_calls):
    _inventory(tmp_path, edge_env, [{"unit_id": "drone-1", "model": "tello",
                                     "features": {FORWARD_OBSTACLE_SENSING: False}}])
    # The inventory itself is refused while the model is chosen, before the resolution runs.
    with pytest.raises(SystemExit):
        _args(["--source", "tello", "--drone-id", "drone-1"])
    args = SimpleNamespace(source="tello", drone=None, drone_id="drone-1", model_choice=None)
    with pytest.raises(SystemExit, match="Refusing to start"):
        edge_device._resolve_edge_start(args, _Log())
    assert drone_calls == []


def test_a_launcher_switching_obstacle_sensing_off_is_refused(edge_env, drone_calls):
    edge_env.setenv(LAUNCHER_PARAMS_ENV_VAR, json.dumps({"features": {FORWARD_OBSTACLE_SENSING: False}}))
    _refused(["--source", "fake", "--drone-id", "drone-1"], drone_calls)


def test_a_statically_unavailable_feature_switched_on_is_refused(edge_env, drone_calls):
    edge_env.setenv(LAUNCHER_PARAMS_ENV_VAR, json.dumps({"features": {"birdseye_search": True}}))
    message = _refused(["--source", "fake", "--drone-id", "drone-1"], drone_calls)
    assert "birdseye search" in message


# --- FEA-10: obstacle sensing on MAVLink needs --source camera or url -------------------------------------


@pytest.mark.parametrize("source,ok", [("camera", True), ("url", True), ("file", False)])
def test_mavlink_obstacle_sensing_follows_the_source(edge_env, tmp_path, source, ok):
    choice = choose_model(drone="mavlink", drone_id="drone-1", environ={ENV_FILE: str(tmp_path / "none.json")})
    resolution = resolve_edge(choice=choice, drone="mavlink", drone_id="drone-1", source=source,
                              environ={ENV_FILE: str(tmp_path / "none.json"),
                                       PARAMS_FILE_ENV_VAR: str(tmp_path / "none.json")}, parser=_PARSER)
    gate = resolution.gates[FORWARD_OBSTACLE_SENSING]
    assert gate["ok"] is ok and gate["on"] is ok


def test_mavlink_obstacle_sensing_switched_on_without_camera_or_url_is_refused(edge_env, tmp_path, drone_calls):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    _inventory(tmp_path, edge_env, [{"unit_id": "drone-1", "model": "generic_multirotor",
                                     "features": {FORWARD_OBSTACLE_SENSING: True}}])
    message = _refused(_MAVLINK + ["--source", "file", "--video", str(video), "--drone-id", "drone-1"],
                       drone_calls)
    assert "--source camera or url" in message


def test_mavlink_obstacle_sensing_switched_on_with_a_camera_starts(edge_env, tmp_path):
    _inventory(tmp_path, edge_env, [{"unit_id": "drone-1", "model": "generic_multirotor",
                                     "features": {FORWARD_OBSTACLE_SENSING: True}}])
    args = _args(_MAVLINK + ["--source", "camera", "--drone-id", "drone-1"])
    edge_device._resolve_edge_start(args, _Log())
    assert args.edge_resolution.gates[FORWARD_OBSTACLE_SENSING]["src"] == "inventory"


# --- FEA-7, static part -----------------------------------------------------------------------------------


def _blind_tello_layers():
    model = dataclasses.replace(BUILTIN_MODELS["tello"], cameras=())
    choice = ModelChoice(model_id="tello", source="test", model=model)
    return EdgeLayers(choice=choice, profile="tello", unit_id="drone-1", layers={}, confirmations={},
                      features={}, launcher=None)


def test_required_sensing_with_neither_sensing_nor_avoidance_is_refused():
    values = {"sensing.mode": SimpleNamespace(value="required")}
    with pytest.raises(EdgeStartRefused, match="required obstacle sensing"):
        static_feature_gates(_blind_tello_layers(), values, source="tello")


def test_sensing_off_on_a_unit_without_sensing_is_accepted():
    values = {"sensing.mode": SimpleNamespace(value="off")}
    gates = static_feature_gates(_blind_tello_layers(), values, source="tello")
    assert set(gates) == set(FEATURES) and gates[FORWARD_OBSTACLE_SENSING]["on"] is False


def test_required_sensing_on_mavlink_without_a_camera_source_is_left_to_the_takeoff(edge_env, tmp_path):
    env = {ENV_FILE: str(tmp_path / "none.json"), PARAMS_FILE_ENV_VAR: str(tmp_path / "none.json")}
    choice = choose_model(drone="mavlink", drone_id="drone-1", environ=env)
    resolution = resolve_edge(choice=choice, drone="mavlink", drone_id="drone-1", source="file", environ=env,
                              parser=_PARSER)
    assert resolution.values["sensing.mode"].value == "required"
    assert resolution.gates["onboard_avoidance"]["ok"] is True
