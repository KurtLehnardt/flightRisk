"""Design section 27 item 53, part 2: the edge's enforcement of the altitude cap switch.

The cap goes off only through the launcher's ``confirmed`` object with a complete acknowledgement, on a
unit whose airframe maximum the inventory states. Every other source of an off value is refused. With
the cap off, the field ceiling and slot bounds rise to the airframe maximum for inventory and launcher
values only; the airframe maximum itself comes only from the inventory.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from flightrisk import edge_device
from flightrisk.edge_resolution import (
    LAUNCHER_PARAMS_ENV_VAR,
    PARAMS_FILE_ENV_VAR,
    EdgeStartRefused,
    choose_model,
    resolve_edge,
)
from flightrisk.hardware.inventory import ENV_FILE
from flightrisk.params import resolver
from flightrisk.params.altitude import ACK_ID, AIRFRAME_MAX_ID, CAP_ID, CAP_ON_ID
from flightrisk.params.resolver import (
    INVENTORY_ONLY,
    LAUNCHER_CONFIRMED,
    ParamRefused,
    parse_confirm_flags,
    resolve,
    resolve_unit,
)
from flightrisk.params.spec import Source

ROOT = Path(__file__).resolve().parent.parent
G = "generic_multirotor"
CEILING = "envelope.ceiling_cm"
SLOT = "envelope.alt_slot_cm"
_PARSER = edge_device._build_parser()


def _ack(**changes):
    ack = {"id": ACK_ID, "text_version": "2026-11-1", "at": "2026-11-24T18:00:00Z", "by": "operator"}
    ack.update(changes)
    return {"value": False, "ack": {k: v for k, v in ack.items() if v is not _DROP}}


_DROP = object()


def _cap_off(record=None, **values):
    return {CAP_ON_ID: False, **values, LAUNCHER_CONFIRMED: {CAP_ON_ID: _ack() if record is None else record}}


def _known(cm=30000):
    return {AIRFRAME_MAX_ID: cm}


# --- Every other source of an off value is refused ---------------------------------------------------------

def _edge(tmp_path, *, params=None, launcher=None, environ=None, argv=()):
    env = {ENV_FILE: str(tmp_path / "none.json"), PARAMS_FILE_ENV_VAR: str(tmp_path / "none.json")}
    if params is not None:
        path = tmp_path / "params.json"
        path.write_text(json.dumps({"version": 1, "units": {"drone-1": params}}), encoding="utf-8")
        env[PARAMS_FILE_ENV_VAR] = str(path)
    if launcher is not None:
        env[LAUNCHER_PARAMS_ENV_VAR] = json.dumps({"unit_id": "drone-1", **launcher})
    env.update(environ or {})
    choice = choose_model(drone="mavlink", drone_id="drone-1", environ=env)
    return resolve_edge(choice=choice, drone="mavlink", drone_id="drone-1", source="file", environ=env,
                        argv=list(argv), parser=_PARSER)


def test_an_unchanged_edge_start_keeps_the_cap_on(tmp_path):
    values = _edge(tmp_path).values
    assert values[CAP_ON_ID].value is True and values[CAP_ID].value == 12000
    assert values[CEILING].hi == 12000 and values[SLOT].hi == 12000


def test_cap_off_from_the_params_file_is_refused_at_the_edge(tmp_path):
    with pytest.raises(EdgeStartRefused, match=CAP_ON_ID):
        _edge(tmp_path, params={CAP_ON_ID: False})


def test_cap_off_from_the_inventory_is_refused():
    with pytest.raises(ParamRefused, match=CAP_ON_ID):
        resolve(G, "g1", {Source.INVENTORY: {CAP_ON_ID: False, **_known()}})


def test_no_environment_name_or_flag_maps_to_the_cap_switch():
    names = {**resolver._ENV_NAMES, **resolver._CLI_NAMES}
    assert not [key for key, name in names.items() if name.spec.id in (CAP_ON_ID, CAP_ID, AIRFRAME_MAX_ID)]


def test_an_environment_variable_cannot_turn_the_cap_off(tmp_path):
    environ = {"FLIGHTRISK_REGULATORY_ALTITUDE_CAP_ON": "0", "FLIGHTRISK_ALTITUDE_CAP_ON": "false",
               "regulatory.altitude_cap_on": "false"}
    assert _edge(tmp_path, environ=environ).values[CAP_ON_ID].value is True


def test_a_flag_cannot_turn_the_cap_off():
    result = resolve(G, "g1", {Source.CLI: {"--altitude-cap-on": "false", "--regulatory-altitude-cap-on": "0"}})
    assert result[CAP_ON_ID].value is True


def test_confirm_less_safe_flag_for_the_cap_is_refused(tmp_path):
    with pytest.raises(ParamRefused, match="only the launcher's acknowledgement"):
        parse_confirm_flags([f"{CAP_ON_ID}=false"])
    with pytest.raises(EdgeStartRefused, match="--confirm-less-safe cannot confirm"):
        _edge(tmp_path, argv=["--confirm-less-safe", f"{CAP_ON_ID}=false"])


def test_no_ground_message_can_set_the_cap():
    """No wire message carries a parameter value to the edge: the edge resolves only at start."""
    callers = [p for p in (ROOT / "flightrisk").rglob("*.py")
               if "resolve_unit(" in p.read_text(encoding="utf-8") and p.name != "resolver.py"]
    assert [p.relative_to(ROOT).as_posix() for p in callers] == ["flightrisk/edge_resolution.py"]
    for path in (ROOT / "flightrisk" / "fleet_protocol").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "altitude_cap" not in text and LAUNCHER_CONFIRMED not in text.replace("confirmed_", ""), path


# --- The acknowledgement record ----------------------------------------------------------------------------

def test_a_complete_acknowledgement_turns_the_cap_off_and_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="flightrisk.params.resolver"):
        result = resolve_unit(G, "g1", {Source.INVENTORY: _known(), Source.LAUNCHER: _cap_off()})
    assert result[CAP_ON_ID].value is False
    lines = [r.getMessage() for r in caplog.records if "altitude cap off" in r.getMessage()]
    assert len(lines) == 1 and all(r.levelno == logging.WARNING for r in caplog.records
                                   if "altitude cap off" in r.getMessage())
    for part in ("g1", "cap 12000 cm", "airframe maximum 30000 cm", ACK_ID, "text_version 2026-11-1",
                 "at 2026-11-24T18:00:00Z", "by operator"):
        assert part in lines[0]


def test_a_complete_acknowledgement_at_the_edge(tmp_path):
    """At the edge the inventory states no maximum for a built-in model, so the cap cannot go off."""
    launcher = {"values": {CAP_ON_ID: False}, "confirmed": {CAP_ON_ID: _ack()}}
    with pytest.raises(EdgeStartRefused, match="The inventory must declare the airframe maximum"):
        _edge(tmp_path, launcher=launcher)


@pytest.mark.parametrize("record, problem", [
    (False, "not an acknowledgement record"),
    ("false", "not an acknowledgement record"),
    ({"value": False}, "no `ack` object"),
    ({"value": True, "ack": _ack()["ack"]}, "`value` is not false"),
    ({"value": "false", "ack": _ack()["ack"]}, "`value` is not false"),
    ({**_ack(), "extra": 1}, "unknown keys extra"),
    (_ack(id="inside_faa_jurisdiction"), "`id` is not outside_faa_jurisdiction"),
    (_ack(id=_DROP), "`id` is not"),
    (_ack(text_version=""), "no `text_version`"),
    (_ack(text_version=3), "no `text_version`"),
    (_ack(text_version=_DROP), "no `text_version`"),
    (_ack(by="  "), "no `by`"),
    (_ack(by=_DROP), "no `by`"),
    (_ack(at="yesterday"), "`at` is not an ISO 8601 time in UTC"),
    (_ack(at="2026-11-24T11:00:00-07:00"), "`at` is not an ISO 8601 time in UTC"),
    (_ack(at="2026-11-24T18:00:00"), "`at` is not an ISO 8601 time in UTC"),
    (_ack(at=_DROP), "`at` is not"),
    ({"value": False, "ack": {**_ack()["ack"], "note": "x"}}, "unknown keys note"),
])
def test_an_incomplete_acknowledgement_is_refused(record, problem):
    with pytest.raises(ParamRefused, match="needs the operator's complete acknowledgement") as info:
        resolve_unit(G, "g1", {Source.INVENTORY: _known(), Source.LAUNCHER: _cap_off(record)})
    assert problem in str(info.value)


def test_an_acknowledgement_with_offset_zero_is_accepted():
    record = _ack(at="2026-11-24T18:00:00+00:00")
    assert resolve_unit(G, "g1", {Source.INVENTORY: _known(),
                                  Source.LAUNCHER: _cap_off(record)})[CAP_ON_ID].value is False


def test_cap_off_without_any_confirmation_is_refused():
    with pytest.raises(ParamRefused, match=CAP_ON_ID):
        resolve_unit(G, "g1", {Source.INVENTORY: _known(), Source.LAUNCHER: {CAP_ON_ID: False}})


def test_an_incomplete_acknowledgement_is_refused_at_the_edge(tmp_path):
    launcher = {"values": {CAP_ON_ID: False}, "confirmed": {CAP_ON_ID: False}}
    with pytest.raises(EdgeStartRefused, match="complete acknowledgement"):
        _edge(tmp_path, launcher=launcher)


def test_cap_off_with_an_unknown_maximum_is_refused():
    with pytest.raises(ParamRefused) as info:
        resolve_unit(G, "drone-7", {Source.LAUNCHER: _cap_off()})
    assert "drone-7 cannot turn the altitude cap off" in str(info.value)
    assert "The inventory must declare the airframe maximum" in str(info.value)


# --- Row 8 comes only from the inventory -------------------------------------------------------------------

def test_row_8_is_inventory_only():
    assert INVENTORY_ONLY == frozenset({AIRFRAME_MAX_ID})
    assert resolve(G, "g1", {Source.INVENTORY: _known()})[AIRFRAME_MAX_ID].value == 30000


@pytest.mark.parametrize("source", [Source.FILE, Source.LAUNCHER])
def test_row_8_from_another_layer_is_refused(source):
    with pytest.raises(ParamRefused, match="only the inventory may state it"):
        resolve(G, "g1", {source: _known()})


def test_row_8_from_the_params_file_is_refused_at_the_edge(tmp_path):
    with pytest.raises(EdgeStartRefused, match="only the inventory may state it"):
        _edge(tmp_path, params=_known())


# --- Raised bounds with the cap off ------------------------------------------------------------------------

def test_cap_off_with_a_known_maximum_raises_ceiling_and_slot_to_it():
    launcher = _cap_off(**{CEILING: 30000, SLOT: 29000})
    result = resolve_unit(G, "g1", {Source.INVENTORY: _known(), Source.LAUNCHER: launcher})
    assert result[CEILING].value == 30000 and result[SLOT].value == 29000
    assert result[CEILING].hi == 30000 and result[SLOT].hi == 30000


@pytest.mark.parametrize("row", [CEILING, SLOT])
def test_cap_off_refuses_a_value_above_the_airframe_maximum(row):
    with pytest.raises(ParamRefused, match="allows 1000 to 30000 cm"):
        resolve_unit(G, "g1", {Source.INVENTORY: _known(), Source.LAUNCHER: _cap_off(**{row: 30001})})


def test_an_inventory_value_rises_with_the_cap_off():
    confirmed = {**_cap_off()[LAUNCHER_CONFIRMED], CEILING: 30000}
    result = resolve_unit(G, "g1", {Source.INVENTORY: {**_known(), CEILING: 30000, SLOT: 25000},
                                    Source.LAUNCHER: {CAP_ON_ID: False, LAUNCHER_CONFIRMED: confirmed}})
    assert result[SLOT].value == 25000 and result[SLOT].source is Source.INVENTORY
    assert result[CEILING].value == 30000 and result[CEILING].source is Source.INVENTORY


@pytest.mark.parametrize("row", [CEILING, SLOT])
def test_cap_on_keeps_12000(row):
    result = resolve(G, "g1", {Source.INVENTORY: _known()})
    assert result[row].hi == 12000
    with pytest.raises(ParamRefused, match="allows 1000 to 12000 cm"):
        resolve(G, "g1", {Source.INVENTORY: _known(), Source.LAUNCHER: {row: 12001}})


@pytest.mark.parametrize("row", [CEILING, SLOT])
def test_the_params_file_keeps_12000_with_the_cap_off(row):
    with pytest.raises(ParamRefused, match="allows 1000 to 12000 cm"):
        resolve(G, "g1", {Source.FILE: {row: 12001}, Source.INVENTORY: _known(), Source.LAUNCHER: _cap_off()})


@pytest.mark.parametrize("name, value, row", [
    ("FLIGHTRISK_FIELD_CEILING_M", "150", CEILING),
    ("FLIGHTRISK_FIELD_ALT_SLOT_M", "150", SLOT),
])
def test_an_environment_value_above_12000_is_refused_with_the_cap_off(name, value, row):
    with pytest.raises(ParamRefused, match="12000"):
        resolve(G, "g1", {Source.INVENTORY: _known(), Source.LAUNCHER: _cap_off(), Source.ENV: {name: value}},
                today_accepts=_today())


@pytest.mark.parametrize("flag, row", [("--ceiling-m", CEILING), ("--alt-slot-m", SLOT)])
def test_a_flag_value_above_12000_is_refused_with_the_cap_off(flag, row):
    with pytest.raises(ParamRefused, match="12000"):
        resolve(G, "g1", {Source.INVENTORY: _known(), Source.LAUNCHER: _cap_off(), Source.CLI: {flag: "150"}},
                today_accepts=_today())


def _today():
    from flightrisk.params.today import TODAY_ACCEPTS

    return TODAY_ACCEPTS


def test_an_environment_value_at_or_below_12000_overrides_a_raised_launcher_value():
    result = resolve(G, "g1", {Source.INVENTORY: _known(), Source.LAUNCHER: _cap_off(**{CEILING: 30000}),
                               Source.ENV: {"FLIGHTRISK_FIELD_CEILING_M": "100"}}, today_accepts=_today())
    assert result[CEILING].value == 10000 and result[CEILING].source is Source.ENV


@pytest.mark.parametrize("scale", ["room"])
def test_indoor_bounds_never_rise(scale):
    result = resolve(G, "g1", {Source.INVENTORY: _known(), Source.LAUNCHER: _cap_off()}, scale=scale)
    assert result[CEILING].hi == 12000 and result[SLOT].hi == 12000
    with pytest.raises(ParamRefused, match="allows 1000 to 12000 cm"):
        resolve(G, "g1", {Source.INVENTORY: _known(), Source.LAUNCHER: _cap_off(**{CEILING: 20000})},
                scale=scale)


def test_tello_bounds_never_change():
    result = resolve("tello", "t1", {})
    assert result[CEILING].hi == 1000 and result[SLOT].hi == 500
    with pytest.raises(ParamRefused):
        resolve("tello", "t1", {Source.LAUNCHER: {CEILING: 1001}})
    with pytest.raises(ParamRefused):
        resolve("tello", "t1", {Source.INVENTORY: {AIRFRAME_MAX_ID: 3000}})


def test_coverage_max_alt_stays_120_m():
    from flightrisk.drone import coverage

    assert coverage.MAX_ALT_M == 120
