"""Tests for flightrisk.drone.coverage."""

import math

import pytest

from flightrisk.drone import coverage
from flightrisk.drone.coverage import (
    GROUND_TEAM,
    OPEN_QUAD_4K,
    PRESETS,
    TELLO,
    TELLO_VIDEO_HEIGHT_PX,
    TELLO_VIDEO_WIDTH_PX,
    SweepModel,
    plan_coverage,
    tello_sweep_model,
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
    # The open quad is used because the Tello's 30 m ceiling caps both of these.
    low = plan_coverage(OPEN_QUAD_4K, min_person_px=40)
    high = plan_coverage(OPEN_QUAD_4K, min_person_px=10)
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


def test_tello_altitude_capped_by_platform():
    assert TELLO.max_alt_m == 30.0
    plan = plan_coverage(TELLO, min_person_px=24)
    assert plan.alt_m == 30.0
    plan = plan_coverage(TELLO, min_person_px=24, max_alt_m=500)
    assert plan.alt_m == 30.0
    plan = plan_coverage(TELLO, min_person_px=24, max_alt_m=10)
    assert plan.alt_m == 10.0
    assert plan_coverage(OPEN_QUAD_4K, min_person_px=2).alt_m == 120.0


def test_range_limited_plan_marked_infeasible():
    plan = plan_coverage(TELLO, min_person_px=24)
    assert not plan.feasible
    assert "range" in plan.infeasible_reason
    small = plan_coverage(TELLO, min_person_px=24, area_m2=100 * 100)
    assert small.feasible
    assert plan_coverage(OPEN_QUAD_4K, min_person_px=24).feasible
    assert plan_coverage(GROUND_TEAM, min_person_px=24).feasible


def test_cli_shows_infeasible_instead_of_time(capsys):
    assert coverage.main(["--min-px", "24"]) == 0
    out = capsys.readouterr().out
    tello_rows = [line for line in out.splitlines() if line.startswith("| Tello |")]
    assert tello_rows and all("INFEASIBLE" in line for line in tello_rows)
    quad_rows = [line for line in out.splitlines() if line.startswith("| Open quad 4K |")]
    assert quad_rows and all("INFEASIBLE" not in line for line in quad_rows)


# ---------------------------------------------------------------------------
# Forward-footprint sweep model (two-laptop plan section 3.2)
# ---------------------------------------------------------------------------


def test_sweep_model_follows_the_forward_trapezoid_formulas():
    model = SweepModel(height_m=1.5, width_px=960, height_px=720, hfov_deg=80.0, min_person_px=30.0)
    half_tan = math.tan(math.radians(40.0))
    vfov_half = math.atan(half_tan * 720 / 960)
    f_px = 480 / half_tan
    assert model.vfov_deg == pytest.approx(math.degrees(2 * vfov_half))
    assert model.near_m == pytest.approx(1.5 / math.tan(vfov_half))
    assert model.focal_px == pytest.approx(f_px)
    assert model.far_m == pytest.approx(f_px * 1.7 / 30.0)
    assert model.sweep_width_m == pytest.approx(2 * model.far_m * half_tan)


def test_sweep_width_is_calibratable():
    base = tello_sweep_model(height_m=1.0, min_person_px=32.0)
    scaled = tello_sweep_model(height_m=1.0, min_person_px=32.0, efficiency=0.25)
    assert scaled.sweep_width_m == pytest.approx(base.sweep_width_m * 0.25)
    measured = tello_sweep_model(height_m=1.0, min_person_px=32.0, calibrated_sweep_width_m=2.4)
    assert measured.sweep_width_m == 2.4
    assert measured.lane_spacing_cm() == 240
    assert measured.lane_spacing_cm(overlap=0.25) == 180


def test_sweep_model_range_cap_and_threshold():
    loose = tello_sweep_model(min_person_px=24.0)
    strict = tello_sweep_model(min_person_px=48.0)
    assert strict.far_m < loose.far_m
    assert strict.sweep_width_m < loose.sweep_width_m
    capped = tello_sweep_model(max_range_m=4.0)
    assert capped.far_m == 4.0
    assert capped.sweep_width_m == pytest.approx(2 * 4.0 * capped.half_tan)


def test_sweep_model_with_nothing_detectable_is_unusable():
    model = SweepModel(height_m=30.0, width_px=960, height_px=720, hfov_deg=82.6, min_person_px=400.0)
    assert not model.usable
    assert model.sweep_width_m == 0.0
    with pytest.raises(ValueError):
        model.lane_spacing_cm()


def test_tello_sweep_model_uses_the_video_frame_not_the_photo():
    model = tello_sweep_model()
    assert (model.width_px, model.height_px) == (TELLO_VIDEO_WIDTH_PX, TELLO_VIDEO_HEIGHT_PX)
    assert model.hfov_deg == TELLO.hfov_deg
    # The nadir preset itself is unchanged.
    assert TELLO.width_px == 2592 and TELLO.gimbal_pitch_deg == 0.0


def test_sweep_model_rejects_bad_parameters():
    with pytest.raises(ValueError):
        SweepModel(height_m=0, width_px=960, height_px=720, hfov_deg=82.6, min_person_px=32)
    with pytest.raises(ValueError):
        SweepModel(height_m=1, width_px=960, height_px=720, hfov_deg=82.6, min_person_px=32, efficiency=1.5)
    with pytest.raises(ValueError):
        SweepModel.for_camera(GROUND_TEAM, 1.0, 32.0)


def test_sweep_model_refuses_a_camera_that_is_not_forward_facing():
    # The open quad's nadir gimbal (-90 degrees) must not be modelled as a forward trapezoid.
    with pytest.raises(ValueError, match="gimbal pitch"):
        SweepModel.for_camera(OPEN_QUAD_4K, 30.0, 32.0)
    assert SweepModel.for_camera(TELLO, 1.0, 32.0).hfov_deg == TELLO.hfov_deg


def test_diagonal_fov_reading_narrows_the_view():
    # UNVERIFIED hardware question: if the Tello's 82.6 degrees is diagonal,
    # the horizontal half-angle tangent shrinks by 4/5 on a 4:3 frame.
    horizontal = tello_sweep_model(max_range_m=4.0)
    diagonal = tello_sweep_model(max_range_m=4.0, fov_is_diagonal=True)
    assert diagonal.half_tan == pytest.approx(horizontal.half_tan * 0.8)
    assert horizontal.sweep_width_m / diagonal.sweep_width_m == pytest.approx(1.25)
    assert diagonal.near_m == pytest.approx(1.9, abs=0.01)
    assert horizontal.near_m == pytest.approx(1.52, abs=0.01)
    # Uncapped, the far edge's width is resolution-limited and does not change.
    assert tello_sweep_model(fov_is_diagonal=True).sweep_width_m == pytest.approx(tello_sweep_model().sweep_width_m)
