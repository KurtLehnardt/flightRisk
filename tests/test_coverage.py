"""Tests for flightrisk.drone.coverage."""

import pytest

from flightrisk.drone import coverage
from flightrisk.drone.coverage import (
    GROUND_TEAM,
    OPEN_QUAD_4K,
    PRESETS,
    TELLO,
    plan_coverage,
    time_to_search_table,
)


def test_presets_have_sources():
    assert len(PRESETS) == 3
    for preset in PRESETS:
        assert "http" in preset.source or "ASSUMPTION" in preset.source


def test_altitude_capped_at_120():
    plan = plan_coverage(OPEN_QUAD_4K, min_person_px=2)
    assert plan.alt_m == 120.0
    plan = plan_coverage(OPEN_QUAD_4K, min_person_px=2, max_alt_m=500)
    assert plan.alt_m <= 120.0


def test_min_pixels_is_a_parameter():
    low = plan_coverage(TELLO, min_person_px=40)
    high = plan_coverage(TELLO, min_person_px=10)
    assert low.alt_m < high.alt_m
    assert low.person_px_at_alt == pytest.approx(40)


def test_lane_spacing_equals_swath_times_one_minus_overlap():
    plan = plan_coverage(OPEN_QUAD_4K, min_person_px=24, overlap=0.25)
    assert plan.lane_spacing_m == pytest.approx(plan.swath_m * 0.75)
    assert plan.area_rate_m2_per_s == pytest.approx(plan.lane_spacing_m * plan.speed_mps)


def test_time_scales_inverse_with_n():
    rows = time_to_search_table()
    for preset in PRESETS:
        hours = {r["n"]: r["hours"] for r in rows if r["preset"] == preset.name}
        assert set(hours) == {1, 2, 3, 5}
        assert hours[1] / hours[2] == pytest.approx(2, rel=0.05)
        assert hours[1] / hours[5] == pytest.approx(5, rel=0.05)


def test_ground_team_slower_than_quad():
    rows = time_to_search_table(n_drones=(1,))
    hours = {r["preset"]: r["hours"] for r in rows}
    assert hours[GROUND_TEAM.name] > hours[TELLO.name]
    assert hours[GROUND_TEAM.name] > hours[OPEN_QUAD_4K.name]


def test_battery_swaps_counted():
    plan = plan_coverage(TELLO, min_person_px=24)
    assert plan.battery_swaps_per_km2 >= 1
    assert plan.time_per_km2_s > 0


def test_cli_prints_markdown_table(capsys):
    assert coverage.main(["--min-px", "24"]) == 0
    out = capsys.readouterr().out
    assert "| preset | n | alt_m | lane_m | hours |" in out
    assert "|---|" in out
    assert "| Tello | 5 |" in out
