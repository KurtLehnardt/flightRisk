"""The effective-values report and ``unit_rev`` (flightrisk/params/report.py, PROTO-5)."""
from __future__ import annotations

import dataclasses
import json
import re

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.hardware.catalogue import BUILTIN_MODELS
from flightrisk.params.report import build_report, canonical_json, unit_rev_of
from flightrisk.params.resolver import resolve_unit
from flightrisk.params.spec import Scope
from flightrisk.params.table import TABLE

SPECS = {spec.id: spec for spec in TABLE}


@pytest.fixture(scope="module")
def tello():
    return build_report(BUILTIN_MODELS["tello"], resolve_unit("tello", "drone-1", {}))


def _fact_ids(profile: str) -> set[str]:
    return {spec.id for spec in TABLE if spec.is_fact(profile)}


def test_tello_report_contains_no_fact_id_or_value(tello):
    facts = _fact_ids("tello")
    assert facts, "the registry has Tello facts"
    assert not facts & set(tello.values)
    text = json.dumps(tello.wire())
    for fact_id in facts:
        assert fact_id not in text


def test_fact_values_do_not_reach_unit_rev():
    resolution = resolve_unit("tello", "drone-1", {})
    fact_id = next(pid for pid in resolution if SPECS.get(pid) and SPECS[pid].is_fact("tello"))
    changed = dict(resolution)
    old = changed[fact_id]
    changed[fact_id] = dataclasses.replace(old, value=(old.value or 0) + 7 if not isinstance(old.value, (tuple, str)) else "x")
    model = BUILTIN_MODELS["tello"]
    assert build_report(model, changed).unit_rev == build_report(model, resolution).unit_rev


def test_rows_are_drone_scope_and_carry_their_edit_kind(tello):
    for param_id, row in tello.values.items():
        assert SPECS[param_id].scope is Scope.DRONE
        assert row["edit"] in {"ui", "file_env", "read_only"}
        assert row["edit"] == SPECS[param_id].edit["tello"].value


def test_unit_rev_is_twelve_lowercase_hex_digits(tello):
    assert re.fullmatch(r"[0-9a-f]{12}", tello.unit_rev)


def test_unit_rev_is_stable_under_key_order_and_float_formatting(tello):
    unit, gates, values = dict(tello.unit), dict(tello.gates), dict(tello.values)
    reordered = {key: values[key] for key in reversed(list(values))}
    assert unit_rev_of(unit, gates, reordered) == tello.unit_rev
    a = {"x": {"v": 1.0, "lo": 0.1 + 0.2}}
    b = {"x": {"lo": 0.3, "v": 1}}
    assert canonical_json(a) == canonical_json(b)
    assert unit_rev_of(unit, gates, a) == unit_rev_of(unit, gates, b)


def test_unit_rev_changes_exactly_when_the_start_time_resolution_changes():
    model = BUILTIN_MODELS["tello"]
    base = resolve_unit("tello", "drone-1", {})
    again = resolve_unit("tello", "drone-1", {})
    assert build_report(model, base).unit_rev == build_report(model, again).unit_rev
    row_id = next(pid for pid in sorted(base) if SPECS.get(pid) and SPECS[pid].scope is Scope.DRONE
                  and not SPECS[pid].is_fact("tello") and isinstance(base[pid].value, int)
                  and not isinstance(base[pid].value, bool))
    changed = dict(base)
    changed[row_id] = dataclasses.replace(base[row_id], value=base[row_id].value + 1)
    assert build_report(model, changed).unit_rev != build_report(model, base).unit_rev
    gated = build_report(model, base, gates={"thermal": {"on": True, "ok": False, "why": "no camera", "src": "file"}})
    assert gated.unit_rev != build_report(model, base).unit_rev


def test_a_later_set_ceiling_does_not_change_unit_rev(tello):
    rev = tello.unit_rev
    fp.make_set_ceiling(250, 50, 90, drone_id="drone-1")
    wire = tello.wire()
    wire["values"].clear()
    assert tello.unit_rev == rev
    assert unit_rev_of(tello.unit, tello.gates, tello.values) == rev
    with pytest.raises(dataclasses.FrozenInstanceError):
        tello.unit_rev = "000000000000"  # type: ignore[misc]


@pytest.mark.parametrize("model_id", sorted(BUILTIN_MODELS))
def test_every_builtin_report_makes_a_valid_reply(model_id):
    model = BUILTIN_MODELS[model_id]
    report = build_report(model, resolve_unit(model_id, "drone-1", {}))
    assert report.unit["link"] in fp.LINK_KINDS
    ack = fp.make_describe_unit_ack("drone-1", "c1", **report.wire())
    assert fp.validate_edge_to_ground(ack, proto=2) == "ack"
    assert not _fact_ids(model.profile) & set(ack["values"])


def test_list_values_are_text_not_lists_of_numbers():
    model = BUILTIN_MODELS["generic_multirotor"]
    report = build_report(model, resolve_unit("generic_multirotor", "drone-1", {}))
    for row in report.values.values():
        for key in ("v", "d"):
            assert not isinstance(row[key], (list, dict))
