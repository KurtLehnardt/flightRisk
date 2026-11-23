"""Tests for the parameter registry table: its 160 rows, and that it equals the golden file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flightrisk.fleet_protocol.checks import _is_private_key
from flightrisk.params import table as table_mod
from flightrisk.params.spec import Bound, Confirm, Edit, Kind, ParamSpec
from flightrisk.params.table import (
    DEFERRED,
    DERIVED_NADIR_SWATH,
    FACT_NOTE,
    PENDING,
    PER_MISSION,
    PLACEHOLDER,
    ROWS_1_86,
    SAFE_VALUES,
    TABLE,
    UNSET,
)
from flightrisk.params import table as _t

PROFILES = ("tello", "generic_multirotor")
MARKERS = {UNSET, DEFERRED, PER_MISSION, DERIVED_NADIR_SWATH}

#: Row numbers of the fact rows (parameters.md section 1), by id.
FACT_IDS = {
    "takeoff.late_s", "takeoff.settle_samples", "takeoff.settle_tolerance_cm", "takeoff.settle_timeout_s",
    "takeoff.refusal_retries", "takeoff.refusal_hold_s", "mode.hover_test", "tello.temp_warn_c",
    "tello.temp_refuse_c", "sensing.turn", "sensing.step_cm", "sensing.settle_s", "sensing.min_safe_depth",
    "sensing.person_fraction", "sensing.max_decode_lag_s", "sensing.view_wait_s", "sensing.sense_timeout_s",
    "sensing.sense_retries", "sensing.sensor_down_s", "sensing.person_wait_s", "sensing.retry_pause_s",
    "move.heading_tolerance_deg", "avoid.clear_votes", "avoid.relook", "avoid.sidestep_cm", "avoid.sidesteps",
    "avoid.sidestep_pass_cm", "position.dr_growth_per_m_cm", "position.dr_growth_per_turn_cm",
    "position.dr_growth_per_min_hover_cm", "tello.refusals_per_flight", "envelope.fence_margin_cm",
}
LESS_SAFE_FACTS = {"tello.temp_refuse_c", "sensing.turn"}


def _by_id() -> dict[str, ParamSpec]:
    return {row.id: row for row in TABLE}


def test_row_count_and_removed_rows() -> None:
    # Rows 1 to 86 minus the removed 53 and 85 is 84 rows; row 71 holds two ids in one spec.
    assert len(ROWS_1_86) == 84
    assert table_mod.TABLE[: len(ROWS_1_86)] == ROWS_1_86
    ids = _by_id()
    assert not any("floor_ratio" in i or i == "sensing.metric" for i in ids)


def test_ids_unique() -> None:
    ids = [row.id for row in TABLE]
    assert len(ids) == len(set(ids))


#: Ids the message privacy screen flags: "frames" contains the forbidden part "frame". The id is fixed by
#: parameters.md row 156 and design.md, so it cannot be renamed here; a flight parameter id must never be
#: sent as a key of a fleet control message (values travel, ids do not). Orchestrator to decide (S5 report).
PRIVATE_KEY_CLASHES = {"escort.handoff_frames"}


def test_ids_passing_the_private_key_screen_are_all_but_the_known_clash() -> None:
    flagged = {row.id for row in TABLE if _is_private_key(row.id)}
    assert flagged == PRIVATE_KEY_CLASHES


def _numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@pytest.mark.parametrize("row", TABLE, ids=lambda r: r.id)
def test_default_inside_bound(row: ParamSpec) -> None:
    for profile in PROFILES:
        default = row.defaults[profile]
        bound = row.bounds[profile]
        if default is None:
            assert bound is None, f"{row.id}/{profile}: bound without a default"
            continue
        if bound is None or not _numeric(default):
            continue
        if row.off_value is not None and default == row.off_value:
            continue
        assert bound.lo <= default <= bound.hi, f"{row.id}/{profile}: {default} outside {bound}"


@pytest.mark.parametrize("row", TABLE, ids=lambda r: r.id)
def test_read_only_rows_have_fixed_bounds(row: ParamSpec) -> None:
    for profile in PROFILES:
        bound = row.bounds[profile]
        if row.edit[profile] is Edit.READ_ONLY and bound is not None:
            assert isinstance(bound, Bound)
            assert bound.lo == bound.hi, f"{row.id}/{profile}"


@pytest.mark.parametrize("row", [r for r in TABLE if r.id in FACT_IDS], ids=lambda r: r.id)
def test_fact_rows(row: ParamSpec) -> None:
    for profile in PROFILES:
        assert row.edit[profile] is Edit.FACT
    expected = Confirm.LESS_SAFE if row.id in LESS_SAFE_FACTS else Confirm.NONE
    assert row.confirm is expected
    assert FACT_NOTE in row.description


def test_fact_set_is_exact() -> None:
    facts = {row.id for row in TABLE if any(row.edit[p] is Edit.FACT for p in PROFILES)}
    assert facts == FACT_IDS


def test_fact_rows_are_never_operator_facing() -> None:
    for row in TABLE:
        if row.id in FACT_IDS:
            text = row.description.casefold()
            assert "calibrat" not in text and "tune" not in text


def test_non_numeric_defaults_are_the_known_set() -> None:
    found = set()
    for row in ROWS_1_86:
        for profile in PROFILES:
            default = row.defaults[profile]
            if isinstance(default, str) and default in MARKERS:
                found.add((row.id, profile))
    assert found == {
        ("plan.strip_cm", "generic_multirotor"),
        ("plan.area_cm", "generic_multirotor"),
        ("plan.camera_footprint_cm", "tello"),
        ("plan.camera_footprint_cm", "generic_multirotor"),
        ("plan.snap_target_cm", "generic_multirotor"),
    }


def test_pending_names_known_rows_and_prs() -> None:
    ids = _by_id()
    for row_id, entries in PENDING.items():
        assert row_id in ids
        for entry in entries:
            assert entry["pr"] in {"B4", "C5", "C6", "C7", "D1", "D2", "D5", "E1"}
            profile = entry["profile"]
            if entry["value"] is not None and profile is not None:
                # Today's value stays the default; the later value must differ.
                assert ids[row_id].defaults[profile] != entry["value"]


@pytest.mark.parametrize(
    ("row_id", "tello", "generic"),
    [
        ("envelope.geofence_cm", 500, 3000),
        ("envelope.ceiling_cm", 200, 3000),
        ("envelope.ceiling_margin_cm", 60, 500),
        ("envelope.alt_slot_cm", 100, 1500),
        ("plan.alt_slots_cm", (100, 130), (1000, 1500)),
        ("move.min_cm", 20, 20),
        ("move.max_cm", 500, 5000),
        ("envelope.max_geofence_cm", 5000, 50000),
        ("battery.min_takeoff_pct", 30, 30),
        ("battery.critical_pct", 15, 15),
        ("link.loss_detect_s", 3.0, 3.0),
        ("link.loss_wait_s", 0, 10.0),
        ("takeoff.height_cm", 80, 500),
        ("sensing.step_cm", 50, 500),
        ("avoid.sidestep_cm", 0, 0),
    ],
)
def test_today_defaults(row_id: str, tello: object, generic: object) -> None:
    row = _by_id()[row_id]
    assert row.defaults["tello"] == tello
    assert row.defaults["generic_multirotor"] == generic


def test_defaults_are_immutable() -> None:
    row = _by_id()["envelope.geofence_cm"]
    with pytest.raises(TypeError):
        row.defaults["tello"] = 1  # type: ignore[index]


# ---------------------------------------------------------------------------------------------------
# Table equals golden (PAR-3).
# ---------------------------------------------------------------------------------------------------

GOLDEN = Path(__file__).resolve().parent / "golden" / "flight_params.json"

#: The one place where table markers and golden encodings are mapped. The table keeps a marker string
#: for a default that is not a number; the golden file keeps a one-key dict. Both must agree exactly.
MARKER_TO_GOLDEN: dict[str, dict] = {
    UNSET: {"unset": True},
    DEFERRED: {"deferred": True},
    PLACEHOLDER: {"placeholder": True},
    PER_MISSION: {"derived": "per mission"},
    DERIVED_NADIR_SWATH: {"derived": "derived from the nadir swath (field_scale)"},
    _t.DERIVED_MISSION_AREA: {"derived": "mission area"},
    _t.DERIVED_LOSS_TIMEOUT: {"derived": "detect + wait + margin"},
    _t.DERIVED_LOST_WINDOW_FLOOR: {"derived": "20 s floor"},
    _t.DERIVED_LOST_WINDOW_FIELD: {
        "derived": "detect + wait + stall bound + GCS timeout + time home + landing + margin"
    },
    _t.DERIVED_REFUSED_STEPS: {"derived": "runtime: sum of refused step lengths"},
}


def _golden_value(value: object) -> object:
    """A table default or pending value in the golden file's encoding."""
    if isinstance(value, str) and value in MARKER_TO_GOLDEN:
        return dict(MARKER_TO_GOLDEN[value])
    if isinstance(value, tuple):
        return list(value)
    return value


def _golden_safer(row: ParamSpec) -> str:
    """The golden file writes a named safe value, or higher, lower or none."""
    if row.id in SAFE_VALUES:
        safe = SAFE_VALUES[row.id]
        return str(safe).lower() if isinstance(safe, bool) else safe
    return row.safer.value


def _golden_pending(row: ParamSpec, profile: str) -> list[dict]:
    out = []
    for entry in PENDING.get(row.id, ()):
        if entry["profile"] == profile:
            item = {"pr": entry["pr"], "item": entry["item"], "value": _golden_value(entry["value"])}
            if "where" in entry:
                item["where"] = entry["where"]
            out.append(item)
    return out


def _golden_rows() -> list[dict]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["rows"]


def test_table_has_160_rows() -> None:
    assert len(TABLE) == 160
    assert len(_golden_rows()) == 160
    assert len({row.id for row in TABLE}) == 160


def test_table_and_golden_hold_the_same_rows_in_order() -> None:
    assert [row.id for row in TABLE] == [row["id"] for row in _golden_rows()]


#: parameters.md unit cells spelled differently from the ``Unit`` value, by ``Unit`` value.
UNIT_SPELLINGS: dict[str, set[str]] = {"cm_list": {"cm_list", "cm", "cm (w, h)"}}


def _assert_same_kind(row: ParamSpec, default: object, golden: object, profile: str) -> None:
    """``==`` lets True equal 1 and 5 equal 5.0; compare the kinds as well."""
    if isinstance(default, str) and default in MARKER_TO_GOLDEN:
        return
    assert isinstance(default, bool) == isinstance(golden, bool), (profile, "bool")
    if row.kind is Kind.INT:
        assert type(default) is int and type(golden) is int, (profile, "int")
    elif row.kind is Kind.FLOAT:
        assert isinstance(default, (int, float)) and isinstance(golden, (int, float)), (profile, "float")
    elif row.kind is Kind.ENUM:
        assert isinstance(default, str) and isinstance(golden, str), (profile, "enum")


@pytest.mark.parametrize("golden", _golden_rows(), ids=lambda r: f"{r['row']}-{r['id']}")
def test_table_equals_golden(golden: dict) -> None:
    row = {r.id: r for r in TABLE}[golden["id"]]
    assert row.applies.value == golden["applies"], "applies"
    assert golden["unit"] in UNIT_SPELLINGS.get(row.unit.value, {row.unit.value}), "unit"
    # Every environment name and CLI flag, with the factor that turns its value into the registry unit.
    scales = {a.name: a.scale_factor for a in row.env_aliases}
    names = ([row.env] if row.env else []) + list(row.cli)
    for name in names:
        scales.setdefault(name, table_mod.NAME_SCALES.get(name, 1.0))
    assert set(scales) == set(golden["env_aliases"]), "aliases"
    for name, alias in golden["env_aliases"].items():
        assert scales[name] == alias["scale"], (name, "scale")
    assert set(golden["profiles"]) == set(PROFILES)
    for profile in PROFILES:
        entry = golden["profiles"][profile]
        default, bound = row.defaults[profile], row.bounds[profile]
        if entry is None:
            # The row does not apply to this profile: no default and no bound in the table.
            assert default is None and bound is None, profile
            continue
        assert default is not None, profile
        assert _golden_value(default) == entry["default"], (profile, "default")
        _assert_same_kind(row, default, entry["default"], profile)
        lo, hi = (bound.lo, bound.hi) if bound is not None else (None, None)
        assert (lo, hi) == (entry["lo"], entry["hi"]), (profile, "bounds")
        assert _golden_pending(row, profile) == entry["pending"], (profile, "pending")
        assert row.edit[profile].value == entry["edit"], (profile, "edit")
        assert row.confirm.value == entry["confirm"], (profile, "confirm")
        assert _golden_safer(row) == entry["safer"], (profile, "safer")
        assert row.safety_switch == entry["safety_switch"], (profile, "safety_switch")
        # The golden file records an off value for numeric switches only (a bool or enum switch is told
        # by safety_switch and its choices); the table keeps the off position of those as well.
        off = None if row.kind in (Kind.BOOL, Kind.ENUM) else row.off_value
        assert off == entry["off_value"], (profile, "off_value")
        # The golden file lists choices only for the profile whose bound cell names them (the other cell
        # says "same"); the order of choices carries no meaning; a bool row has the choices true and false whether or not the table lists them.
        if "choices" in entry:
            table_choices = list(row.choices) or (["true", "false"] if row.kind is Kind.BOOL else [])
            assert sorted(table_choices) == sorted(entry["choices"]), (profile, "choices")


def test_every_pending_entry_is_in_the_golden_file() -> None:
    seen = {
        (row["id"], profile) for row in _golden_rows() for profile, e in row["profiles"].items() if e and e["pending"]
    }
    assert {(row_id, entry["profile"]) for row_id, entries in PENDING.items() for entry in entries} == seen


def test_non_numeric_defaults_of_rows_87_to_162() -> None:
    found = {
        (row.id, profile)
        for row in table_mod.ROWS_87_162
        for profile in PROFILES
        if isinstance(row.defaults[profile], str) and row.defaults[profile] in MARKER_TO_GOLDEN
    }
    assert found == {
        ("battery.return_rate_pct_per_100m", "tello"),
        ("battery.return_rate_pct_per_100m", "generic_multirotor"),
        ("battery.hover_rate_pct_per_min", "tello"),
        ("battery.hover_rate_pct_per_min", "generic_multirotor"),
        ("position.dr_growth_per_m_cm", "tello"),
        ("position.dr_growth_per_turn_cm", "tello"),
        ("position.dr_growth_per_min_hover_cm", "tello"),
        ("find.local_search_max_radius_cm", "tello"),
        ("airspace.obstacle_top_cm", "generic_multirotor"),
        ("envelope.fence_margin_cm", "tello"),
    }


def test_tello_sdk_rows_are_read_only_tello_only() -> None:
    rows = [r for r in TABLE if r.id.startswith("tello_sdk.")]
    assert len(rows) == 24
    for row in rows:
        assert row.edit["tello"] is Edit.READ_ONLY
        assert row.defaults["generic_multirotor"] is None


T, G = "tello", "generic_multirotor"
#: Bound cells the generator cannot read as numbers: the golden file holds lo = hi = None for them, so
#: test_table_equals_golden compares nothing there. A new unreadable cell must be added here on purpose.
UNREAD_BOUND_CELLS = {
    (5, G),
    (5, T),
    (17, T),
    (60, G),
    (60, T),
    (71, G),
    (71, T),
    (109, T),
    (110, T),
    (113, G),
    (113, T),
    (114, G),
    (114, T),
    (116, T),
    (117, T),
    (118, T),
    (121, G),
    (121, T),
    (127, G),
    (127, T),
    (130, G),
    (130, T),
    (131, G),
    (131, T),
    (135, G),
    (135, T),
    (138, G),
    (139, G),
    (140, G),
}


def test_unread_bound_cells_are_listed() -> None:
    no_bound = {"none", "derived", "same", "n/a", "-", ""}
    found = {
        (row["row"], profile)
        for row in _golden_rows()
        for profile, entry in row["profiles"].items()
        if entry and entry["lo"] is None and entry["hi"] is None and "choices" not in entry
        and entry["bound_text"].strip().lower() not in no_bound
    }
    assert found == UNREAD_BOUND_CELLS
