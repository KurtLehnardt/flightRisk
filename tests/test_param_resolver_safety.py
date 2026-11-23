"""Resolver safety: less-safe values, confirmations, switches, constraints, custom models, fleets (A1, S7)."""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest

from flightrisk.hardware.catalogue import BUILTIN_MODELS
from flightrisk.params import constraints, custom
from flightrisk.params.constraints import (
    A1_CONSTRAINTS,
    DEFERRED,
    FACT_CONFLICT_REFUSED,
    check_constraints,
)
from flightrisk.params.resolver import (
    FACT_NOT_SETTABLE,
    LAUNCHER_CONFIRMED,
    LayerValue,
    ParamRefused,
    is_less_safe,
    needs_confirmation,
    parse_confirm_flags,
    resolve,
    resolve_unit,
    spec_for,
    today_accepts_from_golden,
)
from flightrisk.params.spec import Confirm, EffectiveValue, Kind, Safer, Source
from flightrisk.params.table import TABLE

G = "generic_multirotor"
TODAY = today_accepts_from_golden(json.loads(
    (Path(__file__).resolve().parent / "golden" / "flight_params.json").read_text(encoding="utf-8")))
CEILING = "envelope.ceiling_cm"
GEOFENCE = "envelope.geofence_cm"
#: Words an operator message must never contain, because they name a test-adapter fact or its variable.
FACT_WORDS = ("tello.", "sensing.step", "sensing.turn", "avoid.", "FLIGHTRISK_", "calibrat")


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# Less safe and non-default (RES-8)

LESS_SAFE_ROWS = [
    (spec.id, profile)
    for spec in TABLE if spec.confirm is Confirm.LESS_SAFE
    for profile, default in spec.defaults.items() if default is not None
]


def _less_safe_example(spec, base):
    """A value less safe than ``base`` for the row, or None when the row has none."""
    if spec.safer is not Safer.NONE and _number(base):
        step = 1 if spec.kind is Kind.INT else 0.5
        return base - step if spec.safer is Safer.HIGHER else base + step
    if spec.off_value is not None and spec.off_value != base:
        return spec.off_value
    return None


@pytest.mark.parametrize("param_id,profile", LESS_SAFE_ROWS)
def test_every_confirm_less_safe_row_marks_a_less_safe_value(param_id, profile):
    spec = spec_for(param_id)
    base = spec.defaults[profile]
    example = _less_safe_example(spec, base)
    if example is None:
        # Only a placeholder, derived or unset default has no less-safe example.
        assert not _number(base) and spec.off_value is None
        return
    assert is_less_safe(spec, example, base)
    assert not is_less_safe(spec, base, base)
    if spec.safer is not Safer.NONE and _number(base):
        safer = base + 1 if spec.safer is Safer.HIGHER else base - 1
        assert not is_less_safe(spec, safer, base)


def test_an_off_default_is_not_less_safe():
    out = resolve("tello", "drone-1", {})
    assert out["avoid.sidestep_cm"].value == 0
    assert not out["avoid.sidestep_cm"].less_safe


def test_a_less_safe_file_value_is_marked_and_needs_confirmation():
    out = resolve(G, "drone-1", {Source.FILE: {GEOFENCE: 4000}})
    value = out[GEOFENCE]
    assert (value.less_safe, value.non_default, value.base_default) == (True, True, 3000)
    assert needs_confirmation(spec_for(GEOFENCE), value)


def test_confirm_non_default_rows_need_confirmation_whenever_they_differ_from_the_base():
    spec = dataclasses.replace(spec_for(CEILING), confirm=Confirm.NON_DEFAULT)
    base = resolve(G, "drone-1", {})[CEILING]
    safer = resolve(G, "drone-1", {Source.FILE: {CEILING: 2000}})[CEILING]
    assert not needs_confirmation(spec, base)
    assert safer.non_default and not safer.less_safe
    assert needs_confirmation(spec, safer)
    assert not needs_confirmation(spec_for(CEILING), safer)


def test_less_safe_is_measured_against_the_base_never_the_custom_default():
    model = custom.custom_model("site_hex", {
        "extends": G, "param_bounds": {CEILING: {"lo": 1000, "hi": 6000}}, "param_defaults": {CEILING: 2000},
    })
    out = resolve(model, "hex-1", {Source.FILE: {CEILING: 2500}})
    value = out[CEILING]
    assert (value.default, value.base_default) == (2000, 3000)
    # 2500 is above the custom default but below the 3000 cm base, so it is not less safe.
    assert not value.less_safe
    narrowed = resolve(model, "hex-1", {})[CEILING]
    assert (narrowed.value, narrowed.non_default, narrowed.source) == (2000, True, Source.PROFILE)
    assert narrowed.source_detail == "inventory.json models.site_hex"


# Confirmations (RES-11, resolver part)

def test_a_less_safe_file_value_without_a_launcher_is_refused_naming_the_flag():
    with pytest.raises(ParamRefused) as err:
        resolve_unit(G, "drone-1", {Source.FILE: {GEOFENCE: LayerValue(4000, "params.json defaults")}})
    message = err.value.message
    assert "`envelope.geofence_cm` 4000 cm from params.json defaults is less safe" in message
    assert "--confirm-less-safe envelope.geofence_cm=4000" in message


def test_the_exact_confirmation_is_accepted():
    confirmations = parse_confirm_flags(["envelope.geofence_cm=4000"])
    out = resolve_unit(G, "drone-1", {Source.FILE: {GEOFENCE: 4000}}, confirmations=confirmations)
    assert out[GEOFENCE].value == 4000


@pytest.mark.parametrize("confirmations", [
    {GEOFENCE: "4500"}, {GEOFENCE: "3999"}, {CEILING: "4000"}, {},
])
def test_a_confirmation_of_another_value_or_id_does_not_count(confirmations):
    with pytest.raises(ParamRefused):
        resolve_unit(G, "drone-1", {Source.FILE: {GEOFENCE: 4000}}, confirmations=confirmations)


def test_an_inventory_value_needs_the_same_confirmation():
    layers = {Source.INVENTORY: {"battery.critical_pct": 12}}
    with pytest.raises(ParamRefused):
        resolve_unit("tello", "drone-1", layers)
    out = resolve_unit("tello", "drone-1", layers, confirmations={"battery.critical_pct": "12"})
    assert out["battery.critical_pct"].less_safe


def test_with_a_launcher_layer_only_its_confirmed_object_counts():
    file_layer = {GEOFENCE: 4000}
    confirmed = {Source.FILE: file_layer, Source.LAUNCHER: {LAUNCHER_CONFIRMED: {GEOFENCE: 4000}}}
    assert resolve_unit(G, "drone-1", confirmed)[GEOFENCE].value == 4000
    unconfirmed = {Source.FILE: file_layer, Source.LAUNCHER: {CEILING: 2000}}
    with pytest.raises(ParamRefused) as err:
        resolve_unit(G, "drone-1", unconfirmed, confirmations={GEOFENCE: "4000"})
    assert "launcher" in err.value.message


def test_a_less_safe_environment_value_is_accepted_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="flightrisk.params.resolver"):
        out = resolve_unit("tello", "drone-1", {Source.ENV: {"FLIGHTRISK_GEOFENCE_CM": "600"}})
    assert out[GEOFENCE].less_safe and out[GEOFENCE].source is Source.ENV
    assert any("less-safe" in r.getMessage() for r in caplog.records)


def test_a_malformed_confirm_flag_is_refused():
    with pytest.raises(ParamRefused):
        parse_confirm_flags(["envelope.geofence_cm"])


# Safety switches stored as off (RES-10)

SWITCHES = [
    ("sensing.mode", "off", "tello"),
    ("link.require_failsafes", False, G),
]


@pytest.mark.parametrize("source", [Source.FILE, Source.INVENTORY])
@pytest.mark.parametrize("param_id,off,model", SWITCHES)
def test_a_safety_switch_stored_as_off_is_refused(param_id, off, model, source):
    with pytest.raises(ParamRefused) as err:
        resolve(model, "drone-1", {source: {param_id: off}})
    assert "cannot be stored as off" in err.value.message
    assert param_id in err.value.message


@pytest.mark.parametrize("source", [Source.FILE, Source.INVENTORY])
@pytest.mark.parametrize("param_id,off", [("sensing.turn", False), ("tello.temp_refuse_c", "off")])
def test_a_fact_switch_stored_as_off_is_refused_without_naming_it(param_id, off, source):
    with pytest.raises(ParamRefused) as err:
        resolve("tello", "drone-1", {source: {param_id: LayerValue(off, "params.json defaults")}})
    assert err.value.message == FACT_NOT_SETTABLE.format(detail="params.json defaults")
    assert not any(word in err.value.message for word in FACT_WORDS)


def test_forward_obstacle_sensing_stored_as_off_is_refused_in_both_files():
    with pytest.raises(ParamRefused) as err:
        resolve("tello", "drone-1", {}, features={"forward_obstacle_sensing": False, "birdseye_search": False})
    assert "`forward_obstacle_sensing` off from inventory.json units.drone-1 features" in err.value.message
    with pytest.raises(ParamRefused):
        resolve("tello", "drone-1", {Source.FILE: {"forward_obstacle_sensing": False}})
    assert resolve("tello", "drone-1", {}, features={"birdseye_search": False})


def test_a_switch_may_be_off_for_one_session_from_the_launcher_or_environment():
    by_launcher = resolve("tello", "drone-1", {Source.LAUNCHER: {"sensing.mode": "off"}})["sensing.mode"]
    by_env = resolve("tello", "drone-1", {Source.ENV: {"FLIGHTRISK_OBSTACLE_SENSING": "off"}})["sensing.mode"]
    assert (by_launcher.value, by_launcher.less_safe) == ("off", True)
    assert (by_env.value, by_env.less_safe) == ("off", True)


# Constraints (RES-5, PAR-6)

def _with(model: str, **values) -> dict[str, EffectiveValue]:
    """The model's defaults with some values replaced, as a layer would set them."""
    out = resolve(model, "drone-1", {})
    for key, value in values.items():
        param_id = key.replace("__", ".")
        out[param_id] = dataclasses.replace(out[param_id], value=value, source=Source.FILE,
                                            source_detail="params.json defaults")
    return out


#: One failing case per enforced constraint: model, values and a phrase of its operator message.
FAILING = {
    "K-1": ("tello", {"battery__critical_pct": 25}, "must be below `battery.return_floor_pct` 25 %"),
    "K-2": (G, {"battery__return_floor_pct": 29}, "must be at least `battery.critical_pct` 15 %"),
    "K-3": ("tello", {"battery__min_takeoff_pct": 20}, "must be above `battery.return_floor_pct` 25 %"),
    "K-6": ("tello", {"link__loss_detect_s": 1.0}, "1.5 times the 1 s heartbeat interval"),
    "K-8": ("tello", {"envelope__ceiling_cm": 180}, "leaves 120 cm, below the 130 cm altitude slot"),
    "K-9": ("tello", {"tello__temp_warn_c": 96}, None),
    "K-10": ("tello", {"avoid__sidestep_cm": 10}, None),
    "K-11": (G, {"retask__window_s": 0.5}, "must be at least 1 s"),
    "K-12": (G, {"airspace__sweep_band_floor_cm": 1800}, "The airspace bands do not fit"),
}


def test_every_constraint_is_enforced_or_deferred_with_its_owner():
    enforced = {check.id for check in A1_CONSTRAINTS}
    assert enforced == set(FAILING)
    assert enforced | set(DEFERRED) == {f"K-{n}" for n in range(1, 19)}
    # K-3 is enforced for the Tello and deferred to C6 for the generic multirotor only.
    assert enforced & set(DEFERRED) == {"K-3"}
    assert DEFERRED["K-3"] == ("resolver, generic_multirotor profile", "C6")
    assert constraints.K_3.profiles == frozenset({"tello"})


@pytest.mark.parametrize("constraint_id", sorted(FAILING))
def test_each_constraint_passes_on_defaults_and_fails_with_its_message(constraint_id):
    model, values, phrase = FAILING[constraint_id]
    check = next(c for c in A1_CONSTRAINTS if c.id == constraint_id)
    assert check_constraints(resolve(model, "drone-1", {}), BUILTIN_MODELS[model].name, (check,)) is None
    violation = check_constraints(_with(model, **values), BUILTIN_MODELS[model].name, (check,))
    assert violation is not None and violation.constraint_id == constraint_id
    if phrase is None:
        # A constraint over test-adapter facts never names them.
        assert violation.message == FACT_CONFLICT_REFUSED
    else:
        assert phrase in violation.message and BUILTIN_MODELS[model].name in violation.message
        assert "Refusing to start." in violation.message
    assert not any(word in violation.message for word in FACT_WORDS)


def test_a_failed_constraint_refuses_the_unit():
    with pytest.raises(ParamRefused) as err:
        resolve_unit("tello", "drone-1", {Source.FILE: {"battery.critical_pct": 30}})
    assert "`battery.critical_pct` 30 % (from file) must be below" in err.value.message
    with pytest.raises(ParamRefused) as err:
        resolve_unit("tello", "drone-1", {Source.ENV: {"FLIGHTRISK_TELLO_TEMP_WARN_C": "100"}})
    assert err.value.message == FACT_CONFLICT_REFUSED


def test_k9_is_skipped_when_the_temperature_refusal_is_off():
    out = resolve_unit("tello", "drone-1", {Source.ENV: {"FLIGHTRISK_TELLO_TEMP_REFUSE_C": "off"}})
    assert out["tello.temp_refuse_c"].value == "off"


@pytest.mark.parametrize("value", ["15", "19", "20", "25"])
def test_k3_is_enforced_for_the_tello(value):
    with pytest.raises(ParamRefused) as err:
        resolve_unit("tello", "drone-1", {Source.ENV: {"FLIGHTRISK_MIN_TAKEOFF_BATTERY": value}},
                     today_accepts=TODAY)
    assert "must be above `battery.return_floor_pct` 25 %" in err.value.message


def test_k3_is_enforced_for_a_tello_file_value():
    with pytest.raises(ParamRefused):
        resolve_unit("tello", "drone-1", {Source.FILE: {"battery.min_takeoff_pct": 20}},
                     confirmations={"battery.min_takeoff_pct": "20"})


def test_k3_waits_for_c6_on_the_generic_multirotor():
    out = resolve_unit(G, "drone-1", {Source.ENV: {"FLIGHTRISK_MIN_TAKEOFF_BATTERY": "25"}}, today_accepts=TODAY)
    assert out["battery.min_takeoff_pct"].value == 25


@pytest.mark.parametrize("model", sorted(BUILTIN_MODELS))
def test_every_profile_default_satisfies_the_a1_constraint_set(model):
    out = resolve_unit(model, "drone-1", {})
    name = BUILTIN_MODELS[model].name
    assert check_constraints(out, name, A1_CONSTRAINTS + (constraints.K_16,),
                             profile=BUILTIN_MODELS[model].profile) is None
    if BUILTIN_MODELS[model].profile == "tello":
        assert check_constraints(out, name, (constraints.K_3,)) is None
    # K-3 is skipped for generic_multirotor: its 30 % takeoff minimum equals its 30 % floor
    # until C6 raises the minimum to 40 % (section 27 item 33, decision R-2).


# Custom models (CAT-5, CONF-3, K-18)

def _hex(**record):
    return custom.custom_model("site_hex", {"extends": G, "name": "Site hexacopter", **record})


@pytest.mark.parametrize("record,field", [
    ({"param_bounds": {CEILING: {"lo": 900, "hi": 6000}}}, "`param_bounds.envelope.ceiling_cm`"),
    ({"param_bounds": {CEILING: {"lo": 1000, "hi": 13000}}}, "`param_bounds.envelope.ceiling_cm`"),
    ({"scale": "room"}, "`scale`"),
    ({"adapter": "tello_sdk_udp"}, "`adapter`"),
    ({"frame_path": "drone_video"}, "`frame_path`"),
    ({"profile": "tello"}, "`profile`"),
    ({"environment": "indoor"}, "`environment`"),
    ({"indoor_profile_measured": True}, "`indoor_profile_measured`"),
    ({"param_defaults": {"envelope.no_such_cm": 5}}, "`param_defaults.envelope.no_such_cm`"),
    ({"param_bounds": {"envelope.no_such_cm": {"lo": 1, "hi": 2}}}, "`param_bounds.envelope.no_such_cm`"),
    ({"top_speed": 20}, "`top_speed`"),
])
def test_a_custom_model_that_widens_changes_or_adds_is_refused_naming_the_field(record, field):
    with pytest.raises(ParamRefused) as err:
        _hex(**record)
    assert err.value.message.startswith("Custom model site_hex is refused: ")
    assert field in err.value.message


def test_a_custom_model_cannot_add_a_row_its_base_does_not_have():
    with pytest.raises(ParamRefused) as err:
        custom.custom_model("my_tello", {"extends": "tello", "param_defaults": {"battery.rth_min_pct": 50}})
    assert "`param_defaults.battery.rth_min_pct` adds a parameter the Ryze Tello does not have" in err.value.message


def test_a_custom_model_cannot_store_a_fact_and_the_refusal_does_not_name_it():
    with pytest.raises(ParamRefused) as err:
        custom.custom_model("my_tello", {"extends": "tello", "param_defaults": {"sensing.step_cm": 40}})
    assert not any(word in err.value.message for word in FACT_WORDS)


def test_a_custom_model_that_narrows_with_defaults_inside_is_accepted():
    model = _hex(param_bounds={CEILING: {"lo": 1000, "hi": 6000}, "separation.base_cm": {"lo": 300, "hi": 900}},
                 param_defaults={CEILING: 3000, "separation.base_cm": 400}, positioning="rtk")
    assert (model.model.id, model.model.extends, model.model.profile) == ("site_hex", G, G)
    out = resolve_unit(model, "hex-1", {})
    assert (out[CEILING].lo, out[CEILING].hi, out[CEILING].value) == (1000, 6000, 3000)
    assert out["separation.base_cm"].value == 400 and not out["separation.base_cm"].less_safe
    with pytest.raises(ParamRefused) as err:
        resolve(model, "hex-1", {Source.FILE: {CEILING: 7000}})
    assert "the Site hexacopter allows 1000 to 6000 cm" in err.value.message


def test_a_custom_default_outside_its_bound_is_refused():
    with pytest.raises(ParamRefused) as err:
        _hex(param_bounds={CEILING: {"lo": 1000, "hi": 2000}}, param_defaults={CEILING: 2500})
    assert "`param_defaults.envelope.ceiling_cm` 2500 is outside 1000 to 2000" in err.value.message


def test_a_custom_default_less_safe_than_its_base_is_refused_with_the_conf3_message():
    with pytest.raises(ParamRefused) as err:
        _hex(param_defaults={CEILING: 4000})
    assert err.value.message == (
        "`envelope.ceiling_cm` 4000 in inventory.json models.site_hex: "
        "this custom default is less safe than its base; a confirmation for it arrives in C5."
    )
    assert custom.CUSTOM_LESS_SAFE == (
        "this custom default is less safe than its base; a confirmation for it arrives in C5"
    )


# Unknown models (CAP-2) and fleets (RES-12)

def test_an_unknown_model_takes_its_scales_built_in_bounds_and_safer_defaults():
    model = custom.unknown_model("mystery", "field")
    out = resolve_unit(model, "x-1", {})
    built_in = resolve(G, "x-1", {})
    assert set(out) == set(built_in)
    for param_id, value in out.items():
        assert (value.value, value.lo, value.hi) == (built_in[param_id].value, built_in[param_id].lo,
                                                     built_in[param_id].hi)
    assert model.model.fact("sensor_height_cm").status == "unverified"
    assert set(resolve_unit(custom.unknown_model("toy", "room"), "x-1", {})) == set(resolve("tello", "x-1", {}))


def test_two_custom_models_with_disjoint_bounds_are_refused_naming_both_units():
    low = custom.custom_model("hex_low", {"extends": G, "param_bounds": {"separation.base_cm": {"lo": 150, "hi": 300}}})
    high = custom.custom_model("hex_high", {
        "extends": G, "param_bounds": {"separation.base_cm": {"lo": 400, "hi": 800}},
        "param_defaults": {"separation.base_cm": 400},
    })
    with pytest.raises(ParamRefused) as err:
        custom.fleet_bounds({"hex-1": low, "hex-2": high})
    message = err.value.message
    assert "`separation.base_cm` has no value every unit allows" in message
    assert "hex-1 (hex_low) allows 150 to 300 cm" in message and "hex-2 (hex_high) allows 400 to 800 cm" in message


def test_a_fleet_takes_the_intersection_and_the_safer_default():
    wide = custom.custom_model("hex_wide", {
        "extends": G, "param_bounds": {"separation.base_cm": {"lo": 200, "hi": 900}},
        "param_defaults": {"separation.base_cm": 250},
    })
    got = custom.fleet_bounds({"hex-1": G, "hex-2": wide})["separation.base_cm"]
    assert (got.bound.lo, got.bound.hi, got.default) == (200, 900, 250)


# Critic round 1, F3: a NaN or infinite end must never widen a custom model's bound.
NAN, INF = float("nan"), float("inf")


@pytest.mark.parametrize("bounds", [
    {"battery.critical_pct": {"lo": NAN, "hi": 50}},
    {"battery.critical_pct": {"lo": 10, "hi": NAN}},
    {CEILING: {"lo": 150, "hi": NAN}},
    {CEILING: {"lo": -INF, "hi": 200}},
    {CEILING: {"lo": 150, "hi": INF}},
    {CEILING: {"lo": 150, "hi": 10 ** 400}},
])
def test_a_custom_bound_with_a_non_finite_end_is_refused(bounds):
    with pytest.raises(ParamRefused) as err:
        custom.custom_model("my-tello", {"extends": "tello", "param_bounds": bounds})
    assert err.value.message.startswith("Custom model my-tello is refused: ")


@pytest.mark.parametrize("record", [
    {"param_defaults": {CEILING: NAN}},
    {"name": "x", "mounts": [{"weight_g": INF}]},
])
def test_a_non_finite_number_anywhere_in_a_custom_model_is_refused(record):
    with pytest.raises(ParamRefused):
        custom.custom_model("my-tello", {"extends": "tello", **record})


def test_the_critics_nan_model_is_refused():
    with pytest.raises(ParamRefused):
        custom.custom_model("my-tello", {"extends": "tello", "param_bounds": {
            "battery.critical_pct": {"lo": NAN, "hi": 50}, CEILING: {"lo": 150, "hi": NAN}}})


@pytest.mark.parametrize("param_id,value", [("battery.critical_pct", -100), (CEILING, 99999)])
def test_an_inventory_value_outside_the_base_bound_is_refused_on_a_custom_model(param_id, value):
    model = custom.custom_model("my-tello", {"extends": "tello", "param_bounds": {
        "battery.critical_pct": {"lo": 10, "hi": 50}, CEILING: {"lo": 150, "hi": 1000}}})
    with pytest.raises(ParamRefused):
        resolve_unit(model, "t-1", {Source.INVENTORY: {param_id: value}},
                     confirmations={param_id: str(value)})


# Critic round 1, F4: a custom model's narrowed bound holds for every layer, environment and flags included.
def _low_ceiling():
    return custom.custom_model("low-ceiling", {"extends": "tello", "param_bounds": {CEILING: {"lo": 150, "hi": 200}}})


@pytest.mark.parametrize("layers", [
    {Source.ENV: {"FLIGHTRISK_CEILING_CM": "1000"}},
    {Source.ENV: {"FLIGHTRISK_CEILING_CM": "201"}},
    {Source.FILE: {CEILING: 1000}},
    {Source.INVENTORY: {CEILING: 1000}},
    {Source.LAUNCHER: {CEILING: 1000}},
])
def test_a_narrowed_bound_is_enforced_for_every_layer(layers):
    with pytest.raises(ParamRefused) as err:
        resolve(_low_ceiling(), "t-1", layers, today_accepts=TODAY)
    assert "200" in err.value.message


def test_a_narrowed_bound_is_enforced_for_a_flag():
    hex_model = _hex(param_bounds={CEILING: {"lo": 1000, "hi": 6000}})
    with pytest.raises(ParamRefused):
        resolve(hex_model, "hex-1", {Source.CLI: {"--ceiling-m": "100"}}, today_accepts=TODAY)
    assert resolve(hex_model, "hex-1", {Source.CLI: {"--ceiling-m": "50"}},
                   today_accepts=TODAY)[CEILING].value == 5000


def test_a_value_inside_both_today_and_the_narrowed_bound_is_accepted():
    out = resolve(_low_ceiling(), "t-1", {Source.ENV: {"FLIGHTRISK_CEILING_CM": "180"}}, today_accepts=TODAY)
    assert (out[CEILING].value, out[CEILING].lo, out[CEILING].hi) == (180, 150, 200)


def test_a_fact_refusal_carries_no_param_id():
    with pytest.raises(ParamRefused) as err:
        resolve("tello", "drone-1", {Source.FILE: {"sensing.step_cm": 40}})
    assert err.value.param_id is None
