"""The edge builds the resolver's layers at start (A3 S5).

Covers EDGE-4 (the launcher layer: valid, invalid JSON, an unknown id, its
``confirmed`` object), RES-11 at the edge (a less-safe value from the params
file needs an exact ``--confirm-less-safe``), FACT-3's layer form (a fact in
the launcher layer is refused by the resolver), the command-line layer, and
``FLIGHTRISK_AIRFRAME`` as the alias of ``--airframe`` (detail 7.3).

``build_layers`` only builds the layers; these tests pass them to
``resolve_unit`` themselves to show they are in the form the resolver takes.
"""

from __future__ import annotations

import json

import pytest

from flightrisk import edge_device
from flightrisk.edge_resolution import (
    AIRFRAME_ENV_VAR,
    LAUNCHER_PARAMS_ENV_VAR,
    MODEL_ENV_VAR,
    PARAMS_FILE_ENV_VAR,
    SOURCE_AIRFRAME_ENV,
    SOURCE_AIRFRAME_FLAG,
    SOURCE_MODEL_ENV,
    SOURCE_MODEL_FLAG,
    build_layers,
    choose_model,
    command_line_layer,
    parse_launcher_layer,
)
from flightrisk.hardware.inventory import ENV_FILE, parse_inventory
from flightrisk.params.resolver import LAUNCHER_CONFIRMED, ParamRefused, resolve_unit
from flightrisk.params.spec import Source

_PARSER = edge_device._build_parser()
_LESS_SAFE_ID = "battery.min_takeoff_pct"  # Tello default 30, safer higher: 28 is less safe.


@pytest.fixture
def environ(tmp_path):
    """An environment with no model input, no inventory and no params file unless a test adds one."""
    return {ENV_FILE: str(tmp_path / "no-inventory.json"), PARAMS_FILE_ENV_VAR: str(tmp_path / "no-params.json")}


def _params_file(tmp_path, environ, units):
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"version": 1, "units": units}), encoding="utf-8")
    environ[PARAMS_FILE_ENV_VAR] = str(path)


def _build(environ, argv=(), drone="tello", drone_id="drone-1", load_inventory=None):
    choice = choose_model(drone=drone, drone_id=drone_id, environ=environ, load_inventory=load_inventory)
    return build_layers(choice=choice, drone_id=drone_id, environ=environ, argv=argv, parser=_PARSER,
                        load_inventory=load_inventory)


def _resolve(built):
    return resolve_unit(built.profile, built.unit_id, built.layers, confirmations=built.confirmations,
                        features=built.features)


# --- The layers and their form ------------------------------------------------------------------------


def test_no_input_gives_empty_layers_in_precedence_order_and_no_launcher_layer(environ):
    built = _build(environ)
    assert list(built.layers) == [Source.FILE, Source.INVENTORY, Source.ENV, Source.CLI]
    assert built.layers[Source.FILE] == {} and built.layers[Source.CLI] == {}
    assert built.launcher is None and built.confirmations == {} and built.features == {}
    assert (built.profile, built.unit_id) == ("tello", "drone-1")
    assert all(v.source is Source.PROFILE for v in _resolve(built).values())


def test_environment_layer_is_a_copy_of_the_environment(environ):
    environ["FLIGHTRISK_MIN_TAKEOFF_BATTERY"] = "40"
    built = _build(environ)
    environ["FLIGHTRISK_MIN_TAKEOFF_BATTERY"] = "50"
    assert built.layers[Source.ENV]["FLIGHTRISK_MIN_TAKEOFF_BATTERY"] == "40"
    effective = _resolve(built)[_LESS_SAFE_ID]
    assert (effective.value, effective.source) == (40, Source.ENV)


def test_inventory_features_are_passed_as_features(environ):
    inventory = parse_inventory({"version": 1, "rev": 1, "units": [
        {"unit_id": "drone-1", "model": "tello", "features": {"forward_obstacle_sensing": True}}]})
    built = _build(environ, load_inventory=lambda: inventory)
    assert dict(built.features) == {"forward_obstacle_sensing": True}
    assert built.layers[Source.INVENTORY] == {}


# --- The command line ---------------------------------------------------------------------------------


def test_command_line_layer_keeps_given_flight_flags_as_text_and_never_a_default():
    layer, confirms = command_line_layer(
        ["--drone", "tello", "--ceiling-m=3", "--speed-mps", "1.5", "--confirm-less-safe", "a.b=1"], _PARSER)
    assert layer == {"--ceiling-m": "3", "--speed-mps": "1.5"}
    assert confirms == ["a.b=1"]
    assert command_line_layer(["--drone", "tello"], _PARSER) == ({}, [])


def test_command_line_layer_expands_an_abbreviation_as_argparse_does_and_the_last_flag_wins():
    layer, _ = command_line_layer(["--speed", "1.5", "--speed-mps=2"], _PARSER)
    assert layer == {"--speed-mps": "2"}
    assert _PARSER.parse_args(["--speed", "1.5"]).speed_mps == 1.5


def test_command_line_value_resolves_with_source_cli(environ):
    built = _build(environ, argv=["--drone", "tello", "--alt-slot", "120"])
    effective = _resolve(built)["envelope.alt_slot_cm"]
    assert (effective.value, effective.source) == (120, Source.CLI)


# --- The launcher layer (EDGE-4) ----------------------------------------------------------------------


def test_valid_launcher_layer_is_passed_with_its_confirmed_object(environ):
    environ[LAUNCHER_PARAMS_ENV_VAR] = json.dumps({
        "unit_id": "drone-1", "model": "tello", "values": {"envelope.geofence_cm": 400},
        "features": {"forward_obstacle_sensing": True}, "confirmed": {_LESS_SAFE_ID: 28},
    })
    built = _build(environ)
    assert list(built.layers) == [Source.FILE, Source.INVENTORY, Source.LAUNCHER, Source.ENV, Source.CLI]
    assert built.layers[Source.LAUNCHER] == {"envelope.geofence_cm": 400, LAUNCHER_CONFIRMED: {_LESS_SAFE_ID: 28}}
    assert (built.launcher.unit_id, built.launcher.model) == ("drone-1", "tello")
    assert dict(built.launcher.features) == {"forward_obstacle_sensing": True}
    effective = _resolve(built)["envelope.geofence_cm"]
    assert (effective.value, effective.source) == (400, Source.LAUNCHER)


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"text"', "1e999999"])
def test_launcher_layer_that_is_not_a_json_object_refuses_the_start(environ, raw):
    environ[LAUNCHER_PARAMS_ENV_VAR] = raw
    with pytest.raises(ParamRefused) as exc:
        parse_launcher_layer(environ)
    assert LAUNCHER_PARAMS_ENV_VAR in exc.value.message and "Refusing to start." in exc.value.message
    assert exc.value.source is Source.LAUNCHER


def test_invalid_launcher_json_refuses_the_start_from_build_layers(environ, tmp_path):
    environ[LAUNCHER_PARAMS_ENV_VAR] = "{not json"
    choice = choose_model(drone="tello", drone_id="drone-1", model_flag="tello", environ={})
    with pytest.raises(ParamRefused, match="is not valid JSON"):
        build_layers(choice=choice, drone_id="drone-1", environ=environ, parser=_PARSER)


@pytest.mark.parametrize("data, named", [
    ({"values": {"no.such_id": 1}}, "no.such_id"),
    ({"confirmed": {"no.such_id": 1}}, "no.such_id"),
    ({"features": {"no_such_feature": True}}, "no_such_feature"),
    ({"value": {"envelope.geofence_cm": 400}}, "value"),
])
def test_launcher_layer_with_an_unknown_id_or_key_refuses_the_start(environ, data, named):
    environ[LAUNCHER_PARAMS_ENV_VAR] = json.dumps(data)
    with pytest.raises(ParamRefused) as exc:
        _build(environ, argv=["--drone", "tello"])
    assert named in exc.value.message and "Refusing to start." in exc.value.message


@pytest.mark.parametrize("data", [{"values": [1]}, {"confirmed": "x"}, {"features": {"level_search": "on"}},
                                  {"unit_id": 3}])
def test_launcher_layer_with_a_malformed_section_refuses_the_start(environ, data):
    environ[LAUNCHER_PARAMS_ENV_VAR] = json.dumps(data)
    with pytest.raises(ParamRefused):
        parse_launcher_layer(environ)


def test_empty_launcher_variable_is_no_launcher_layer(environ):
    environ[LAUNCHER_PARAMS_ENV_VAR] = "  "
    assert parse_launcher_layer(environ) is None
    assert Source.LAUNCHER not in _build(environ).layers


def test_fact_in_the_launcher_layer_is_refused_by_the_resolver_without_naming_it(environ):
    environ[LAUNCHER_PARAMS_ENV_VAR] = json.dumps({"values": {"takeoff.late_s": 5.0}})
    built = _build(environ)
    with pytest.raises(ParamRefused) as exc:
        _resolve(built)
    assert "not a setting" in exc.value.message and "takeoff.late_s" not in exc.value.message


# --- A less-safe value from the params file (RES-11) --------------------------------------------------


def test_params_file_layer_is_read_for_the_unit(environ, tmp_path):
    _params_file(tmp_path, environ, {"drone-1": {"envelope.geofence_cm": 400}})
    built = _build(environ)
    assert built.layers[Source.FILE]["envelope.geofence_cm"].value == 400
    effective = _resolve(built)["envelope.geofence_cm"]
    assert (effective.value, effective.source) == (400, Source.FILE)


def test_less_safe_params_file_value_is_refused_without_a_confirmation(environ, tmp_path):
    _params_file(tmp_path, environ, {"drone-1": {_LESS_SAFE_ID: 28}})
    with pytest.raises(ParamRefused) as exc:
        _resolve(_build(environ, argv=["--drone", "tello"]))
    assert f"--confirm-less-safe {_LESS_SAFE_ID}=28" in exc.value.message


def test_less_safe_params_file_value_is_accepted_with_the_exact_confirmation(environ, tmp_path):
    _params_file(tmp_path, environ, {"drone-1": {_LESS_SAFE_ID: 28}})
    built = _build(environ, argv=["--drone", "tello", "--confirm-less-safe", f"{_LESS_SAFE_ID}=28"])
    assert dict(built.confirmations) == {_LESS_SAFE_ID: "28"}
    effective = _resolve(built)[_LESS_SAFE_ID]
    assert (effective.value, effective.source, effective.less_safe) == (28, Source.FILE, True)


def test_less_safe_params_file_value_is_refused_when_another_value_is_confirmed(environ, tmp_path):
    _params_file(tmp_path, environ, {"drone-1": {_LESS_SAFE_ID: 28}})
    built = _build(environ, argv=["--drone", "tello", "--confirm-less-safe", f"{_LESS_SAFE_ID}=26"])
    with pytest.raises(ParamRefused, match=f"--confirm-less-safe {_LESS_SAFE_ID}=28"):
        _resolve(built)


def test_launcher_confirmed_accepts_the_params_file_value_and_the_flag_no_longer_counts(environ, tmp_path):
    _params_file(tmp_path, environ, {"drone-1": {_LESS_SAFE_ID: 28}})
    environ[LAUNCHER_PARAMS_ENV_VAR] = json.dumps({"confirmed": {_LESS_SAFE_ID: 28}})
    assert _resolve(_build(environ))[_LESS_SAFE_ID].value == 28
    environ[LAUNCHER_PARAMS_ENV_VAR] = json.dumps({"confirmed": {}})
    built = _build(environ, argv=["--confirm-less-safe", f"{_LESS_SAFE_ID}=28"])
    with pytest.raises(ParamRefused, match="Confirm it in the launcher"):
        _resolve(built)


def test_broken_params_file_refuses_the_start(environ, tmp_path):
    path = tmp_path / "params.json"
    path.write_text("{", encoding="utf-8")
    environ[PARAMS_FILE_ENV_VAR] = str(path)
    with pytest.raises(ParamRefused, match="cannot be parsed"):
        _build(environ)


def test_malformed_confirmation_flag_refuses_the_start(environ):
    with pytest.raises(ParamRefused, match="id=value"):
        _build(environ, argv=["--confirm-less-safe=novalue"])


# --- FLIGHTRISK_AIRFRAME (detail 7.3) -----------------------------------------------------------------


def test_airframe_variable_names_the_model():
    choice = choose_model(drone="tello", drone_id="d", environ={AIRFRAME_ENV_VAR: "tello_edu"},
                          load_inventory=lambda: None)
    assert (choice.model_id, choice.source) == ("tello_edu", SOURCE_AIRFRAME_ENV)


def test_airframe_flag_comes_before_the_airframe_variable():
    choice = choose_model(drone="tello", drone_id="d", airframe_flag="tello", environ={AIRFRAME_ENV_VAR: "tello_edu"},
                          load_inventory=lambda: None)
    assert (choice.model_id, choice.source) == ("tello", SOURCE_AIRFRAME_FLAG)


def test_model_flag_and_variable_come_before_the_airframe_variable():
    env = {AIRFRAME_ENV_VAR: "tello_edu", MODEL_ENV_VAR: "tello"}
    assert choose_model(drone="tello", drone_id="d", environ=env).source == SOURCE_MODEL_ENV
    assert choose_model(drone="tello", drone_id="d", model_flag="tello", environ=env).source == SOURCE_MODEL_FLAG


def test_airframe_variable_comes_before_the_inventory_and_the_launcher():
    inventory = parse_inventory({"version": 1, "rev": 1, "units": [{"unit_id": "d", "model": "tello"}]})
    env = {AIRFRAME_ENV_VAR: "tello_edu", LAUNCHER_PARAMS_ENV_VAR: json.dumps({"model": "tello"})}
    choice = choose_model(drone="tello", drone_id="d", environ=env, load_inventory=lambda: inventory)
    assert (choice.model_id, choice.source) == ("tello_edu", SOURCE_AIRFRAME_ENV)


def test_airframe_variable_that_does_not_fit_the_backend_refuses_the_edge(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(MODEL_ENV_VAR, raising=False)
    monkeypatch.delenv(LAUNCHER_PARAMS_ENV_VAR, raising=False)
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "no-inventory.json"))
    monkeypatch.setenv(AIRFRAME_ENV_VAR, "tello_edu")
    parser = edge_device._build_parser()
    args = parser.parse_args(["--drone", "mavlink", "--frame-heading-deg", "0"])
    with pytest.raises(SystemExit) as exc:
        edge_device._resolve_drone_and_source(args, parser)
    assert exc.value.code == 2
    assert f"model tello_edu (from {AIRFRAME_ENV_VAR}) does not match --drone mavlink" in capsys.readouterr().err
