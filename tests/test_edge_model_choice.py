"""The edge chooses its model and checks it against its backend before contacting a drone (A3 S4).

Covers detail 7.3 (model order), SEAM-3 and EDGE-6: the order of ``--model``,
``FLIGHTRISK_MODEL``, ``--airframe``, the inventory, the launcher layer and the
backend default; a refusal for each of scale, adapter and frame path; and that
an invocation with none of these inputs starts exactly as before.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from flightrisk import edge_device
from flightrisk.edge_resolution import (
    LAUNCHER_PARAMS_ENV_VAR,
    MODEL_ENV_VAR,
    SOURCE_AIRFRAME_FLAG,
    SOURCE_BACKEND_DEFAULT,
    SOURCE_INVENTORY,
    SOURCE_LAUNCHER,
    SOURCE_MODEL_ENV,
    SOURCE_MODEL_FLAG,
    BackendProfile,
    ModelChoice,
    ModelRefused,
    backend_profile,
    check_model_against_backend,
    choose_model,
)
from flightrisk.hardware.catalogue import BUILTIN_MODELS
from flightrisk.hardware.inventory import ENV_FILE, parse_inventory

_MAVLINK_ARGS = ["--drone", "mavlink", "--frame-heading-deg", "0"]


@pytest.fixture(autouse=True)
def _no_model_inputs(monkeypatch, tmp_path):
    """No model flag, variable, inventory or launcher layer unless a test sets one."""
    monkeypatch.delenv(MODEL_ENV_VAR, raising=False)
    monkeypatch.delenv(LAUNCHER_PARAMS_ENV_VAR, raising=False)
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "no-inventory.json"))


def _resolve(argv):
    parser = edge_device._build_parser()
    args = parser.parse_args(argv)
    edge_device._resolve_drone_and_source(args, parser)
    return args


def _refusal(argv, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        _resolve(argv)
    assert exc.value.code == 2
    return capsys.readouterr().err


def _inventory(units, models=None):
    return parse_inventory({"version": 1, "rev": 1, "models": models or {}, "units": units})


# --- Behaviour-neutral without any input -------------------------------------------------------------

@pytest.mark.parametrize("argv, model_id", [
    (["--drone", "tello"], "tello"),
    (["--drone", "fake"], "tello"),
    (["--source", "tello"], "tello"),
    (["--source", "fake"], "tello"),
    (_MAVLINK_ARGS, "generic_multirotor"),
    (_MAVLINK_ARGS + ["--source", "file", "--video", "x.mp4"], "generic_multirotor"),
])
def test_without_inputs_the_model_is_the_backend_default(argv, model_id):
    args = _resolve(argv)
    assert args.model_choice.model_id == model_id
    assert args.model_choice.source == SOURCE_BACKEND_DEFAULT
    assert args.confirm_less_safe is None


@pytest.mark.parametrize("argv", [[], ["--source", "file", "--video", "x.mp4"], ["--source", "url"]])
def test_an_edge_that_flies_nothing_chooses_no_model(argv):
    assert _resolve(argv).model_choice is None


def test_an_edge_that_flies_nothing_ignores_a_model_it_cannot_check():
    assert _resolve(["--model", "generic_multirotor"]).model_choice is None


def test_backend_profiles_match_their_default_models():
    for drone, frame_path, model_id in (
        ("tello", "drone_video", "tello"), ("fake", "drone_video", "tello"),
        ("mavlink", "capture_source", "generic_multirotor"),
    ):
        model = BUILTIN_MODELS[model_id]
        assert backend_profile(drone, frame_path) == BackendProfile(drone, model.adapter, model.scale, frame_path)


# --- Model order (detail 7.3) ------------------------------------------------------------------------

_FULL_INVENTORY = _inventory([{"unit_id": "drone-1", "model": "tello_edu"}])


def _choose(**kw):
    defaults = dict(drone="tello", drone_id="drone-1", environ={}, load_inventory=lambda: None)
    defaults.update(kw)
    return choose_model(**defaults)


def test_model_flag_comes_first():
    choice = _choose(model_flag="tello_edu", airframe_flag="tello", environ={MODEL_ENV_VAR: "tello"},
                     load_inventory=lambda: _FULL_INVENTORY)
    assert (choice.model_id, choice.source) == ("tello_edu", SOURCE_MODEL_FLAG)


def test_model_variable_comes_before_airframe():
    choice = _choose(airframe_flag="tello", environ={MODEL_ENV_VAR: "tello_edu"})
    assert (choice.model_id, choice.source) == ("tello_edu", SOURCE_MODEL_ENV)


def test_airframe_comes_before_the_inventory():
    def inventory():
        raise AssertionError("the inventory is read only when no flag or variable names a model")

    choice = _choose(airframe_flag="tello_edu", load_inventory=inventory)
    assert (choice.model_id, choice.source) == ("tello_edu", SOURCE_AIRFRAME_FLAG)


def test_inventory_unit_comes_before_the_launcher():
    launcher = {LAUNCHER_PARAMS_ENV_VAR: json.dumps({"unit_id": "drone-1", "model": "tello"})}
    choice = _choose(environ=launcher, load_inventory=lambda: _FULL_INVENTORY)
    assert (choice.model_id, choice.source) == ("tello_edu", SOURCE_INVENTORY)


def test_inventory_unit_is_matched_by_drone_id():
    launcher = {LAUNCHER_PARAMS_ENV_VAR: json.dumps({"model": "tello_edu"})}
    choice = _choose(drone_id="drone-2", environ=launcher, load_inventory=lambda: _FULL_INVENTORY)
    assert (choice.model_id, choice.source) == ("tello_edu", SOURCE_LAUNCHER)


def test_launcher_model_comes_before_the_backend_default():
    choice = _choose(environ={LAUNCHER_PARAMS_ENV_VAR: json.dumps({"unit_id": "drone-1", "model": "tello_edu"})})
    assert (choice.model_id, choice.source) == ("tello_edu", SOURCE_LAUNCHER)


def test_launcher_layer_without_a_model_falls_through_to_the_default():
    choice = _choose(environ={LAUNCHER_PARAMS_ENV_VAR: json.dumps({"unit_id": "drone-1", "values": {}})})
    assert (choice.model_id, choice.source) == ("tello", SOURCE_BACKEND_DEFAULT)


def test_inventory_custom_model_is_resolved_from_its_base():
    inv = _inventory([{"unit_id": "drone-1", "model": "site_hex"}],
                     {"site_hex": {"extends": "generic_multirotor", "name": "Site hexacopter"}})
    choice = _choose(drone="mavlink", load_inventory=lambda: inv)
    assert (choice.model_id, choice.source, choice.model.adapter) == ("site_hex", SOURCE_INVENTORY, "mavlink")
    flagged = _choose(drone="mavlink", model_flag="site_hex", load_inventory=lambda: inv)
    assert flagged.model.scale == "field"


def test_inventory_file_and_variable_reach_the_edge(monkeypatch, tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"version": 1, "rev": 1, "models": {},
                                "units": [{"unit_id": "drone-3", "model": "tello_edu"}]}), encoding="utf-8")
    monkeypatch.setenv(ENV_FILE, str(path))
    args = _resolve(["--drone", "tello", "--drone-id", "drone-3"])
    assert (args.model_choice.model_id, args.model_choice.source) == ("tello_edu", SOURCE_INVENTORY)
    monkeypatch.setenv(MODEL_ENV_VAR, "tello")
    assert _resolve(["--drone", "tello", "--drone-id", "drone-3"]).model_choice.source == SOURCE_MODEL_ENV


# --- Refusals ------------------------------------------------------------------------------------------

def _single_difference(field: str, value: str) -> str:
    model = dataclasses.replace(BUILTIN_MODELS["generic_multirotor"], id="odd", **{field: value})
    backend = backend_profile("mavlink", "capture_source")
    with pytest.raises(ModelRefused) as exc:
        check_model_against_backend(ModelChoice("odd", SOURCE_MODEL_FLAG, model), backend)
    return str(exc.value)


def test_a_scale_mismatch_is_refused_naming_both():
    text = _single_difference("scale", "room")
    assert "model odd" in text and "--drone mavlink" in text
    assert "scale room (the model) is not field (the backend)" in text
    assert "adapter" not in text and "frame path" not in text


def test_an_adapter_mismatch_is_refused_naming_both():
    text = _single_difference("adapter", "tello_sdk_udp")
    assert "model odd" in text and "--drone mavlink" in text
    assert "adapter tello_sdk_udp (the model) is not mavlink (the backend)" in text
    assert "scale" not in text and "frame path" not in text


def test_a_frame_path_mismatch_is_refused_naming_both():
    text = _single_difference("frame_path", "drone_video")
    assert "model odd" in text and "--drone mavlink" in text
    assert "frame path drone_video (the model) is not capture_source (the backend)" in text
    assert "scale" not in text and "adapter" not in text


def test_a_tello_cannot_fly_on_the_mavlink_backend(capsys):
    err = _refusal(_MAVLINK_ARGS + ["--model", "tello"], capsys)
    assert "model tello (from --model) does not match --drone mavlink" in err
    for part in ("scale room", "adapter tello_sdk_udp", "frame path drone_video"):
        assert part in err


@pytest.mark.parametrize("drone", ["tello", "fake"])
def test_a_field_model_cannot_fly_on_the_tello_backend(drone, capsys, monkeypatch):
    monkeypatch.setenv(MODEL_ENV_VAR, "generic_multirotor")
    err = _refusal(["--drone", drone], capsys)
    assert f"model generic_multirotor (from {MODEL_ENV_VAR}) does not match --drone {drone}" in err


def test_the_refusal_comes_before_any_drone_is_contacted(monkeypatch, capsys):
    def contacted(*_a, **_k):
        raise AssertionError("the edge contacted a drone before refusing its model")

    monkeypatch.setattr(edge_device, "_build_mavlink_drone", contacted)
    with pytest.raises(SystemExit) as exc:
        edge_device.main(_MAVLINK_ARGS + ["--airframe", "tello_edu"])
    assert exc.value.code == 2
    assert "model tello_edu (from --airframe) does not match --drone mavlink" in capsys.readouterr().err


def test_mismatch_refusals_name_no_parameter_value(capsys):
    err = _refusal(_MAVLINK_ARGS + ["--model", "tello"], capsys)
    message = err.split("error:", 1)[1]
    assert not any(ch.isdigit() for ch in message), message


def test_an_unknown_model_is_refused(capsys):
    err = _refusal(["--drone", "tello", "--model", "no_such_model"], capsys)
    assert "model 'no_such_model' (from --model) is neither built in nor defined in the inventory" in err


def test_an_unreadable_inventory_is_refused(monkeypatch, tmp_path, capsys):
    path = tmp_path / "inventory.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(ENV_FILE, str(path))
    assert "the inventory is refused" in _refusal(["--drone", "tello"], capsys)


@pytest.mark.parametrize("raw", ["{not json", "[]", json.dumps({"model": 7}), json.dumps({"model": " "})])
def test_a_broken_launcher_layer_is_refused(raw, monkeypatch, capsys):
    monkeypatch.setenv(LAUNCHER_PARAMS_ENV_VAR, raw)
    assert LAUNCHER_PARAMS_ENV_VAR in _refusal(["--drone", "tello"], capsys)


# --- --confirm-less-safe is parsed and stored only -----------------------------------------------------

def test_confirm_less_safe_is_repeatable_and_stored():
    args = _resolve(["--drone", "tello", "--confirm-less-safe", "a.b=1", "--confirm-less-safe", " c.d = off "])
    assert args.confirm_less_safe == [("a.b", "1"), ("c.d", "off")]


@pytest.mark.parametrize("raw", ["a.b", "=1", "a.b=", " = "])
def test_confirm_less_safe_needs_id_and_value(raw, capsys):
    err = _refusal(["--drone", "tello", "--confirm-less-safe", raw], capsys)
    assert "--confirm-less-safe takes id=value" in err
