"""What an edge resolves at start: the golden defaults (EDGE-5) and every environment name and flag (EDGE-7).

Every case goes through ``resolve_edge`` with an empty environment (bar a params file that does not
exist) and no inventory, so nothing of the machine running the test leaks in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flightrisk import edge_device
from flightrisk.edge_resolution import PARAMS_FILE_ENV_VAR, choose_model, resolve_edge
from flightrisk.params import resolver
from flightrisk.params.spec import Edit, Kind, Scope, Source
from flightrisk.params.table import TABLE
from flightrisk.params.today import TODAY_ACCEPTS

GOLDEN = json.loads((Path(__file__).parent / "golden" / "flight_params.json").read_text(encoding="utf-8"))
PARSER = edge_device._build_parser()
SPECS = {spec.id: spec for spec in TABLE}
DRONE_ROWS = [row for row in GOLDEN["rows"] if SPECS[row["id"]].scope is Scope.DRONE]

#: The edge drone kind, the ``--source`` it starts with and the golden column it flies.
EDGE_KINDS = {
    "tello": ("tello", "tello"),
    "fake": ("tello", "tello"),
    "mavlink": ("camera", "generic_multirotor"),
}

#: Names left out of EDGE-7, each with the reason no valid non-default value reaches the edge.
SKIPPED_NAMES = {
    "FLIGHTRISK_LINK_LOSS_WAIT_S": (
        "a room-only name: the room model (Tello) has no bound for it, so the resolver ignores it "
        "(IGNORED_WITHOUT_BOUND), and the only model with a bound is a field unit, which never reads it"
    ),
}


def _start(drone, tmp_path, environ=None, argv=()):
    environ = {PARAMS_FILE_ENV_VAR: str(tmp_path / "no-params.json"), **(environ or {})}
    choice = choose_model(drone=drone, drone_id="drone-1", environ=environ, load_inventory=lambda: None)
    return resolve_edge(choice=choice, drone=drone, drone_id="drone-1", source=EDGE_KINDS[drone][0],
                        environ=environ, argv=argv, parser=PARSER, load_inventory=lambda: None)


# --- EDGE-5: a unit with no setting resolves to the golden column of its model ---------------------------


def _golden_cases():
    for drone, (_, column) in EDGE_KINDS.items():
        for row in DRONE_ROWS:
            yield pytest.param(drone, column, row, id=f"{drone}-{row['id']}")


def _plain(value):
    return tuple(value) if isinstance(value, (list, tuple)) else value


@pytest.fixture(scope="module")
def defaults_by_drone(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("edge-defaults")
    return {drone: _start(drone, tmp).values for drone in EDGE_KINDS}


@pytest.mark.parametrize("drone,column,row", list(_golden_cases()))
def test_a_unit_with_no_setting_resolves_to_the_golden_default_of_its_model(defaults_by_drone, drone, column, row):
    entry = row["profiles"][column]
    values = defaults_by_drone[drone]
    if entry is None or entry["default"] is None:
        assert row["id"] not in values
        return
    effective = values[row["id"]]
    default = entry["default"]
    if isinstance(default, dict) and "placeholder" in default:
        assert effective.value == "placeholder"
    elif isinstance(default, dict) and "unset" in default:
        # Design section 27 item 53: the generic airframe maximum is unknown until the inventory states it.
        assert effective.value == "unset"
    elif isinstance(default, dict):
        assert str(effective.value).startswith("derived")
    else:
        assert _plain(effective.value) == _plain(default)
    assert effective.source is Source.PROFILE
    assert effective.non_default is False


def test_the_golden_table_has_a_drone_row_for_every_registry_drone_row():
    assert {row["id"] for row in DRONE_ROWS} == {spec.id for spec in TABLE if spec.scope is Scope.DRONE}
    # 101 before item 53 added regulatory.altitude_cap_cm and regulatory.altitude_cap_on.
    assert len(DRONE_ROWS) == 103


# --- EDGE-7: every name of a drone-scope row, set to a valid non-default value -------------------------


def _names():
    """``(name, is_flag, spec)`` for every environment name and flag of every drone-scope row."""
    for spec in TABLE:
        if spec.scope is not Scope.DRONE:
            continue
        for name in ([spec.env] if spec.env else []) + [alias.name for alias in spec.env_aliases]:
            yield name, False, spec
        for flag in spec.cli:
            yield flag, True, spec


ALL_NAMES = list(_names())


#: Names that need another unit than the default pick: the Tello takes only ``return`` and ``land`` as
#: its default's alternative in the wrong direction, so its failsafe action is tried on MAVLink.
UNIT_OF = {"FLIGHTRISK_FAILSAFE_ACTION": "mavlink"}

#: Values (in the row's unit, per golden column) that keep the cross-row checks of ``constraints.py``
#: satisfied while the name under test is the only change: ceiling minus margin must clear the slot, and
#: the Tello's temperature refusal must sit above its warning, and the land battery must sit below the return floor.
VALUE_OF = {
    "envelope.ceiling_cm": {"tello": 1000, "generic_multirotor": 12000},
    "envelope.ceiling_margin_cm": {"tello": 65, "generic_multirotor": 400},
    "envelope.alt_slot_cm": {"tello": 120, "generic_multirotor": 2000},
    "tello.temp_refuse_c": {"tello": 100},
    "battery.critical_pct": {"tello": 12, "generic_multirotor": 12},
}


def _unit_for(name, is_flag, spec):
    """The edge drone kind that reads ``name``: the simulated Tello where it applies, else MAVLink."""
    if name in UNIT_OF:
        return UNIT_OF[name]
    named = (resolver._CLI_NAMES if is_flag else resolver._ENV_NAMES)[name]
    tello_ok = ("room" in named.scales and spec.defaults.get("tello") is not None
                and spec.edit.get("tello") is not Edit.READ_ONLY)
    return "fake" if tello_ok else "mavlink"


def _inside(value, lo, hi, lo_open=False):
    return (lo is None or value > lo if lo_open else lo is None or value >= lo) and (hi is None or value <= hi)


def _valid_non_default(name, spec, column):
    """A value (in the row's unit) inside today's range and the target bound, not the default; None if none."""
    default = spec.defaults[column]
    if column in VALUE_OF.get(spec.id, {}):
        return VALUE_OF[spec.id][column]
    if spec.kind is Kind.ENUM:
        return next((c for c in spec.choices if c != default), None)
    bound = spec.bounds.get(column)
    today = (TODAY_ACCEPTS.get(name) or {}).get(column)
    if today is None and not resolver.read_today(name):
        today = resolver._OPEN  # No code reads the name today: the target bound alone limits it.
    lo = max(x for x in (bound and bound.lo, today and today.lo) if x is not None) if (bound or today) else None
    his = [x for x in (bound and bound.hi, today and today.hi) if x is not None]
    hi = min(his) if his else None
    if lo is None or hi is None:
        return None
    candidates = [lo, hi, (lo + hi) / 2, lo + 1]
    if today is resolver._OPEN:
        lo, hi = bound.lo, bound.hi
        candidates = [lo, hi, (lo + hi) / 2, lo + 1]
    for value in candidates:
        value = int(value) if spec.kind is Kind.INT else float(value)
        if value != default and _inside(value, bound and bound.lo, bound and bound.hi) \
                and today is not None and _inside(value, today.lo, today.hi, today.lo_open):
            if not resolver.is_less_safe(spec, value, default):
                return value
    return None


def _text(value, scale):
    shown = value / scale if scale != 1.0 else value
    return str(int(shown)) if float(shown).is_integer() and not isinstance(shown, str) and isinstance(value, int) else str(shown)


def _edge_7_cases():
    for name, is_flag, spec in ALL_NAMES:
        if name in SKIPPED_NAMES:
            continue
        yield pytest.param(name, is_flag, spec, id=name)


def test_every_skipped_name_is_a_drone_scope_name_with_a_reason():
    assert set(SKIPPED_NAMES) <= {name for name, _, _ in ALL_NAMES}
    assert all(SKIPPED_NAMES.values())


def test_the_edge_7_table_holds_every_drone_name_but_the_skipped_ones():
    assert len(list(_edge_7_cases())) == len(ALL_NAMES) - len(SKIPPED_NAMES)


@pytest.mark.parametrize("name,is_flag,spec", list(_edge_7_cases()))
def test_a_valid_non_default_value_of_every_name_reaches_the_edge_with_its_source(tmp_path, name, is_flag, spec):
    drone = _unit_for(name, is_flag, spec)
    column = EDGE_KINDS[drone][1]
    value = _valid_non_default(name, spec, column)
    assert value is not None, f"{name} has no valid non-default value on {column}; add it to SKIPPED_NAMES"
    named = (resolver._CLI_NAMES if is_flag else resolver._ENV_NAMES)[name]
    text = _text(value, named.scale) if not isinstance(value, str) else value
    argv = (name, text) if is_flag else ()
    if spec.confirm.value != "none":  # A less-safe or non-default value is confirmed in the row's unit.
        argv += ("--confirm-less-safe", f"{spec.id}={value}")
    environ = {} if is_flag else {name: text}
    resolution = _start(drone, tmp_path, environ, argv)
    effective = resolution.values[spec.id]
    assert effective.source is (Source.CLI if is_flag else Source.ENV)
    assert effective.value == value
    assert effective.non_default is True


def test_a_fact_override_counts_on_the_fake_unit_and_is_ignored_on_a_production_unit(tmp_path):
    spec = SPECS["sensing.step_cm"]
    assert spec.edit["tello"] is Edit.FACT and spec.edit["generic_multirotor"] is Edit.FACT
    environ = {spec.env: "100"}
    fake = _start("fake", tmp_path, environ).values[spec.id]
    assert fake.value == 100 and fake.source is Source.ENV
    production = _start("mavlink", tmp_path, environ).values[spec.id]
    assert production.value == spec.defaults["generic_multirotor"] and production.source is Source.PROFILE
