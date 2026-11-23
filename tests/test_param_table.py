"""Tests for the parameter registry table, rows 1 to 86."""

from __future__ import annotations

import pytest

from flightrisk.fleet_protocol.checks import _is_private_key
from flightrisk.params import table as table_mod
from flightrisk.params.spec import Bound, Confirm, Edit, ParamSpec
from flightrisk.params.table import (
    DEFERRED,
    DERIVED_NADIR_SWATH,
    FACT_NOTE,
    PENDING,
    PER_MISSION,
    ROWS_1_86,
    UNSET,
)

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
    "avoid.sidestep_pass_cm",
}
LESS_SAFE_FACTS = {"tello.temp_refuse_c", "sensing.turn"}


def _by_id() -> dict[str, ParamSpec]:
    return {row.id: row for row in ROWS_1_86}


def test_row_count_and_removed_rows() -> None:
    # Rows 1 to 86 minus the removed 53 and 85 is 84 rows; row 71 holds two ids in one spec.
    assert len(ROWS_1_86) == 84
    assert table_mod.TABLE[: len(ROWS_1_86)] == ROWS_1_86
    ids = _by_id()
    assert not any("floor_ratio" in i or i == "sensing.metric" for i in ids)


def test_ids_unique() -> None:
    ids = [row.id for row in ROWS_1_86]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("row", ROWS_1_86, ids=lambda r: r.id)
def test_id_passes_private_key_screen(row: ParamSpec) -> None:
    assert not _is_private_key(row.id)


def _numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@pytest.mark.parametrize("row", ROWS_1_86, ids=lambda r: r.id)
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


@pytest.mark.parametrize("row", ROWS_1_86, ids=lambda r: r.id)
def test_read_only_rows_have_fixed_bounds(row: ParamSpec) -> None:
    for profile in PROFILES:
        bound = row.bounds[profile]
        if row.edit[profile] is Edit.READ_ONLY and bound is not None:
            assert isinstance(bound, Bound)
            assert bound.lo == bound.hi, f"{row.id}/{profile}"


@pytest.mark.parametrize("row", [r for r in ROWS_1_86 if r.id in FACT_IDS], ids=lambda r: r.id)
def test_fact_rows(row: ParamSpec) -> None:
    for profile in PROFILES:
        assert row.edit[profile] is Edit.FACT
    expected = Confirm.LESS_SAFE if row.id in LESS_SAFE_FACTS else Confirm.NONE
    assert row.confirm is expected
    assert FACT_NOTE in row.description


def test_fact_set_is_exact() -> None:
    facts = {row.id for row in ROWS_1_86 if any(row.edit[p] is Edit.FACT for p in PROFILES)}
    assert facts == FACT_IDS


def test_fact_rows_are_never_operator_facing() -> None:
    for row in ROWS_1_86:
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
            assert entry["pr"] in {"B4", "C5", "C6", "C7", "D1", "D2", "D5"}
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
