"""Resolver core: precedence, sources, bounds, aliases, facts and the params file (A1, step S6)."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from flightrisk.params import files
from flightrisk.params.resolver import (
    CALLER_LAYERS,
    FACT_OVERRIDE_REFUSED,
    PRECEDENCE,
    LayerValue,
    ParamRefused,
    resolve,
    today_accepts_from_golden,
)
from flightrisk.params.spec import Source
from flightrisk.params.table import TABLE

GOLDEN = Path(__file__).resolve().parent / "golden" / "flight_params.json"
TODAY = today_accepts_from_golden(json.loads(GOLDEN.read_text(encoding="utf-8")))
G = "generic_multirotor"
CEILING = "envelope.ceiling_cm"

#: One value per layer for ``envelope.ceiling_cm`` on the generic multirotor, all inside 1000 to 12000 cm.
CEILING_LAYERS = {
    Source.FILE: {CEILING: 2000},
    Source.INVENTORY: {CEILING: 2500},
    Source.LAUNCHER: {CEILING: 4000},
    Source.ENV: {"FLIGHTRISK_FIELD_CEILING_M": "35"},
    Source.CLI: {"--ceiling-m": "45"},
}
CEILING_VALUES = {
    Source.PROFILE: 3000, Source.FILE: 2000, Source.INVENTORY: 2500, Source.LAUNCHER: 4000,
    Source.ENV: 3500, Source.CLI: 4500,
}


def _resolve(model=G, layers=None, **kw):
    kw.setdefault("today_accepts", TODAY)
    return resolve(model, "drone-1", layers or {}, **kw)


def test_precedence_order_is_res_1():
    assert PRECEDENCE == (Source.PROFILE, Source.FILE, Source.INVENTORY, Source.LAUNCHER, Source.ENV, Source.CLI)


@pytest.mark.parametrize("lower, higher", list(zip(PRECEDENCE, PRECEDENCE[1:])))
def test_each_adjacent_pair_the_higher_source_wins(lower, higher):
    layers = {s: CEILING_LAYERS[s] for s in (lower, higher) if s in CEILING_LAYERS}
    got = _resolve(layers=layers)[CEILING]
    assert got.source is higher
    assert got.value == CEILING_VALUES[higher]
    without = _resolve(layers={s: v for s, v in layers.items() if s is not higher})[CEILING]
    assert without.source is lower
    assert without.value == CEILING_VALUES[lower]


def test_every_value_carries_source_and_source_detail():
    out = _resolve(layers=CEILING_LAYERS)
    assert out
    for value in out.values():
        assert value.source in PRECEDENCE
        assert value.source_detail
    assert out[CEILING].source_detail == "--ceiling-m"
    assert out["envelope.geofence_cm"].source is Source.PROFILE
    assert out["envelope.geofence_cm"].source_detail == G


def test_layer_value_detail_names_where_in_the_layer():
    got = _resolve(layers={Source.FILE: {CEILING: LayerValue(2000, "params.json units.drone-1")}})[CEILING]
    assert (got.source, got.source_detail, got.value) == (Source.FILE, "params.json units.drone-1", 2000)


def test_field_ceiling_metres_alias_is_multiplied_by_100():
    got = _resolve(layers={"env": {"FLIGHTRISK_FIELD_CEILING_M": "30"}})[CEILING]
    assert got.value == 3000
    assert got.source is Source.ENV
    assert got.source_detail == "FLIGHTRISK_FIELD_CEILING_M"


def test_room_alias_never_applies_to_field_and_field_alias_never_to_room():
    field = _resolve(layers={"env": {"FLIGHTRISK_CEILING_CM": "500"}})[CEILING]
    assert field.source is Source.PROFILE
    room = _resolve("tello", layers={"env": {"FLIGHTRISK_FIELD_CEILING_M": "5"}})[CEILING]
    assert room.source is Source.PROFILE
    assert _resolve("tello", layers={"env": {"FLIGHTRISK_CEILING_CM": "500"}})[CEILING].value == 500


def test_out_of_bound_file_value_is_refused_with_id_value_unit_source_model_and_bound():
    layer = {Source.FILE: {CEILING: LayerValue(99999, "params.json units.drone-1")}}
    with pytest.raises(ParamRefused) as err:
        _resolve(layers=layer)
    text = err.value.message
    for part in (CEILING, "99999", "cm", "params.json units.drone-1", "Generic multirotor", "1000", "12000"):
        assert part in text


def test_env_value_is_checked_against_today_range_and_file_value_against_target_bound():
    # Today's Tello edge accepts any geofence; the target bound is 40 to 5000 cm (risk R4).
    env = _resolve("tello", layers={"env": {"FLIGHTRISK_GEOFENCE_CM": "6000"}})["envelope.geofence_cm"]
    assert env.value == 6000
    with pytest.raises(ParamRefused) as err:
        _resolve("tello", layers={"file": {"envelope.geofence_cm": 6000}})
    assert "5000" in err.value.message


def test_env_value_outside_today_range_is_refused_with_its_variable():
    with pytest.raises(ParamRefused) as err:
        _resolve(layers={"env": {"FLIGHTRISK_FIELD_GEOFENCE_M": "600"}})
    assert "FLIGHTRISK_FIELD_GEOFENCE_M" in err.value.message
    assert "60000" in err.value.message


def test_today_clamp_policy_is_reproduced():
    got = _resolve(layers={"env": {"FLIGHTRISK_MANUAL_ORPHAN_LAND_S": "10"}})["stranded.countdown_s"]
    assert got.value == 30
    assert got.source is Source.ENV


def test_malformed_env_value_is_refused():
    with pytest.raises(ParamRefused) as err:
        _resolve(layers={"env": {"FLIGHTRISK_FIELD_CEILING_M": "high"}})
    assert CEILING in err.value.message


def test_empty_env_value_counts_as_unset():
    assert _resolve(layers={"env": {"FLIGHTRISK_FIELD_CEILING_M": " "}})[CEILING].source is Source.PROFILE


def test_off_value_is_accepted_and_marked_less_safe():
    got = _resolve(layers={"env": {"FLIGHTRISK_OBSTACLE_SENSING": "off"}})["sensing.mode"]
    assert (got.value, got.less_safe) == ("off", True)
    fact = _resolve("tello", layers={"env": {"FLIGHTRISK_TELLO_TEMP_REFUSE_C": "off"}})["tello.temp_refuse_c"]
    assert (fact.value, fact.less_safe, fact.source) == ("off", True, Source.ENV)


def test_fact_override_is_ignored_on_a_production_unit_with_a_log_line(caplog):
    caplog.set_level("WARNING")
    got = _resolve(layers={"env": {"FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH": "0.5"}})["sensing.min_safe_depth"]
    assert (got.value, got.source) == (0.35, Source.PROFILE)
    assert sum("FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("model, simulated", [("tello", False), ("tello_edu", False), (G, True)])
def test_fact_override_is_honoured_on_a_test_adapter_or_simulated_unit(model, simulated):
    layers = {"env": {"FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH": "0.5"}}
    got = _resolve(model, layers=layers, simulated=simulated)["sensing.min_safe_depth"]
    assert (got.value, got.source) == (0.5, Source.ENV)


def test_refused_fact_override_names_neither_the_fact_nor_the_variable():
    with pytest.raises(ParamRefused) as err:
        _resolve("tello", layers={"env": {"FLIGHTRISK_TELLO_SETTLE_SAMPLES": "500"}})
    assert err.value.message == FACT_OVERRIDE_REFUSED
    assert "settle" not in err.value.message.lower()
    assert "FLIGHTRISK" not in err.value.message


@pytest.mark.parametrize("source", [Source.FILE, Source.INVENTORY, Source.LAUNCHER])
def test_fact_in_a_stored_layer_is_refused_without_naming_it(source):
    with pytest.raises(ParamRefused) as err:
        _resolve("tello", layers={source: {"sensing.min_safe_depth": 0.5}})
    assert "sensing" not in err.value.message


def test_read_only_flag_is_ignored_and_read_only_file_value_refused():
    got = _resolve("tello", layers={"cli": {"--takeoff-alt-m": "2"}})["takeoff.height_cm"]
    assert (got.value, got.source) == (80, Source.PROFILE)
    with pytest.raises(ParamRefused):
        _resolve("tello", layers={"file": {"takeoff.height_cm": 200}})
    assert _resolve(layers={"cli": {"--takeoff-alt-m": "6"}})["takeoff.height_cm"].value == 600


def test_field_only_flag_is_refused_at_room_scale():
    with pytest.raises(ParamRefused) as err:
        _resolve("tello", layers={"cli": {"--ceiling-m": "5"}})
    assert "--ceiling-m" in err.value.message


def test_unknown_id_in_a_stored_layer_and_unknown_layer_are_refused():
    with pytest.raises(ParamRefused):
        _resolve(layers={"inventory": {"envelope.no_such_cm": 1}})
    with pytest.raises(ValueError):
        _resolve(layers={"ground": {CEILING: 2000}})


def test_rows_that_do_not_apply_to_the_model_are_absent():
    tello = _resolve("tello")
    assert "move.speed_cm_s" not in tello
    assert len(tello) == sum(1 for s in TABLE if s.defaults["tello"] is not None)


def test_resolved_value_always_equals_the_winning_source_value():
    """Seeded property test (``hypothesis`` is not a dev dependency)."""
    rng = random.Random(20261123)
    for _ in range(300):
        present = [s for s in CALLER_LAYERS if rng.random() < 0.5]
        metres = {s: rng.randint(10, 120) for s in (Source.ENV, Source.CLI)}
        cms = {s: rng.randint(1000, 12000) for s in (Source.FILE, Source.INVENTORY, Source.LAUNCHER)}
        layers = {}
        for s in present:
            if s in cms:
                layers[s] = {CEILING: cms[s]}
            else:
                layers[s] = {"FLIGHTRISK_FIELD_CEILING_M" if s is Source.ENV else "--ceiling-m": str(metres[s])}
        got = _resolve(layers=layers)[CEILING]
        winner = present[-1] if present else Source.PROFILE
        expected = 3000 if winner is Source.PROFILE else cms.get(winner, metres.get(winner, 0) * 100)
        assert (got.source, got.value) == (winner, expected)


# params.json (RES-7, FACT-2 file part)

def _write(tmp_path, data):
    path = tmp_path / "params.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_missing_params_file_is_an_empty_layer(tmp_path):
    loaded = files.load_params_file(tmp_path / "params.json")
    assert loaded.layer_for(G, "drone-1") == {}


def test_units_beat_models_beat_defaults(tmp_path):
    path = _write(tmp_path, {
        "version": 1,
        "defaults": {CEILING: 2000, "envelope.ceiling_margin_cm": 400},
        "models": {G: {CEILING: 2500, "envelope.geofence_cm": 4000}},
        "units": {"drone-1": {CEILING: 2800}},
    })
    layer = files.load_params_file(path).layer_for(G, "drone-1")
    out = _resolve(layers={Source.FILE: layer})
    assert (out[CEILING].value, out[CEILING].source_detail) == (2800, "params.json units.drone-1")
    assert out["envelope.geofence_cm"].source_detail == f"params.json models.{G}"
    assert out["envelope.ceiling_margin_cm"].source_detail == "params.json defaults"
    other = files.load_params_file(path).layer_for(G, "drone-2")
    assert other[CEILING].value == 2500


@pytest.mark.parametrize("data", [
    {"version": 1, "defaults": {"envelope.no_such_cm": 5}},
    {"version": 1, "defaults": {CEILING: "high"}},
    {"version": 1, "defaults": {CEILING: True}},
    {"version": 1, "units": {"drone-1": {"sensing.mode": "sometimes"}}},
    {"version": 2, "defaults": {}},
    {"version": 1, "extras": {}},
    [1, 2],
])
def test_unknown_id_wrong_type_or_wrong_schema_is_refused(tmp_path, data):
    with pytest.raises(ParamRefused):
        files.load_params_file(_write(tmp_path, data))


def test_unparsable_file_is_refused_without_falling_back(tmp_path):
    with pytest.raises(ParamRefused) as err:
        files.load_params_file(_write(tmp_path, "{not json"))
    assert "cannot be parsed" in err.value.message


def test_unreadable_file_is_refused(tmp_path):
    (tmp_path / "params.json").mkdir()
    with pytest.raises(ParamRefused) as err:
        files.load_params_file(tmp_path / "params.json")
    assert "cannot be read" in err.value.message


@pytest.mark.parametrize("fact_id", [s.id for s in TABLE if s.is_fact("tello")][:6])
def test_fact_id_in_params_file_is_refused_without_naming_it(tmp_path, fact_id):
    spec = next(s for s in TABLE if s.id == fact_id)
    value = spec.defaults["tello"] if spec.defaults["tello"] is not None else spec.defaults[G]
    with pytest.raises(ParamRefused) as err:
        files.load_params_file(_write(tmp_path, {"version": 1, "defaults": {fact_id: value}}))
    assert fact_id not in err.value.message


def test_read_only_everywhere_id_in_params_file_is_refused(tmp_path):
    ro = next(s for s in TABLE if set(e.value for e in s.edit.values()) == {"read_only"})
    with pytest.raises(ParamRefused):
        files.load_params_file(_write(tmp_path, {"version": 1, "defaults": {ro.id: ro.defaults[G]}}))


# Critic round 1, F1: a value that overflows to infinity after unit scaling must be refused, as today.
@pytest.mark.parametrize("source,name,text", [
    (Source.ENV, "FLIGHTRISK_FIELD_CEILING_M", "1e307"),
    (Source.ENV, "FLIGHTRISK_FIELD_GEOFENCE_M", "1e307"),
    (Source.ENV, "FLIGHTRISK_FIELD_CEILING_MARGIN_M", "1e307"),
    (Source.ENV, "FLIGHTRISK_FIELD_ALT_SLOT_M", "1e307"),
    (Source.CLI, "--ceiling-m", "1e307"),
    (Source.CLI, "--geofence-m", "1e307"),
    (Source.CLI, "--ceiling-margin-m", "1e307"),
    (Source.CLI, "--alt-slot-m", "1e307"),
    (Source.CLI, "--speed-mps", "1e307"),
    (Source.CLI, "--takeoff-alt-m", "1e307"),
])
def test_a_metre_value_that_overflows_once_scaled_is_refused(source, name, text):
    with pytest.raises(ParamRefused):
        _resolve(G, {source: {name: text}}, scale="field")


@pytest.mark.parametrize("text", ["1e308", "1e309"])
def test_a_centimetre_value_near_the_float_limit_is_refused(text):
    # Today's room envelope refuses both (1e309 parses as inf); the geofence has no upper end today.
    with pytest.raises(ParamRefused):
        _resolve("tello", {Source.ENV: {"FLIGHTRISK_CEILING_CM": text}})


def test_a_non_finite_number_is_never_inside_a_range():
    from flightrisk.params.resolver import TodayRange, _outside_range
    assert _outside_range(float("inf"), TodayRange(None, None)) == "hi"
    assert _outside_range(float("-inf"), TodayRange(None, None)) == "lo"
    assert _outside_range(float("nan"), TodayRange(0, 10)) is not None
    assert _outside_range(10 ** 400, TodayRange(None, None)) == "hi"
