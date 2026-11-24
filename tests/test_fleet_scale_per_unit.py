"""Per-unit environments, cameras and limits in the fleet scales and search patterns.

``fleet_scale.scale_for`` gives the plan scale of an environment name
(``"room"`` or ``"field"``) and a camera passed in as values. The pattern
builders of ``fleet_patterns`` take each drone's ``safety.EnvelopeLimits``
(``unit_limits``), the area cap and the planning footprint margins, build each
drone's lanes within its own limits and check the plan with the limits it was
built with. With no new argument, or the room profile given explicitly for
every drone, every scale and plan is today's.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from flightrisk.drone import coordinator
from flightrisk.drone import fleet_patterns as fp
from flightrisk.drone.coverage import OPEN_QUAD_4K, nadir_swath_m, tello_sweep_model
from flightrisk.drone.fleet_scale import (
    PI_CAMERA_MODULE_3,
    ROOM_SCALE,
    field_scale,
    scale_for,
)
from flightrisk.drone.safety import (
    DEFAULT_GEOFENCE_CM,
    FIELD_LIMITS,
    MAX_MOVE_CM,
    ROOM_LIMITS,
)

SQUARE = fp.SQUARE_FOOTPRINT
# The 4 m demo room, as the existing pattern tests model the room camera.
ROOM_CAMERA = tello_sweep_model(max_range_m=4.0)
# The room profile's slots with field-sized moves that cannot be shorter than 1 m.
COARSE = dataclasses.replace(ROOM_LIMITS, name="coarse", min_move_cm=100, max_move_cm=5000)
# A field fleet in the square model: slots within the field profile and a fence that holds 60 m lanes.
FIELD = {"footprint": SQUARE, "alt_slots_cm": (1500, 2000), "geofence_cm": 6000}
QUAD_VALUES = {
    "hfov_deg": OPEN_QUAD_4K.hfov_deg, "width_px": OPEN_QUAD_4K.width_px, "height_px": OPEN_QUAD_4K.height_px,
}


def _ids(n):
    return [f"d{i}" for i in range(1, n + 1)]


def _distances(plan, drone_id=None):
    return [
        lane["distance_cm"] for drone in plan.drones if drone_id in (None, drone.drone_id) for lane in drone.lanes
    ]


def _attempt(builder, *args, **kwargs):
    """The plan, or the refusal message: both must stay as they are."""
    try:
        return builder(*args, **kwargs)
    except ValueError as exc:
        return ("refused", str(exc))


def _builds(extra):
    """Today's builds for one to four drones; ``extra(ids)`` adds keyword arguments to each call."""
    builds = []
    for n in (1, 2, 3, 4):
        ids = _ids(n)
        for area in ((400, 400), (1200, 800)):
            for lkp in (None, (area[0] // 2, area[1] // 2)):
                builds.append(_attempt(fp.plan_search, ids, area, None, lkp, footprint=ROOM_CAMERA, **extra(ids)))
        builds.append(_attempt(
            fp.plan_parallel_split, ids, (1200, 600), 120, footprint=SQUARE, geofence_cm=None, **extra(ids)))
        builds.append(_attempt(fp.plan_parallel_split, ids, (60, 400), 120, footprint=SQUARE, **extra(ids)))
    two = _ids(2)
    builds.append(_attempt(
        fp.plan_split_expanding_square, two, (1200, 800), (600, 400), None, footprint=ROOM_CAMERA, **extra(two)))
    builds.append(_attempt(
        fp.plan_split_expanding_square, two, (1200, 800), (30, 400), None, footprint=ROOM_CAMERA, **extra(two)))
    builds.append(_attempt(fp.plan_square_spiral, "d1", (1200, 800), (600, 400), None, footprint=ROOM_CAMERA,
                           **extra(["d1"])))
    return builds


# --- Scales: the room scale is unchanged, the field scale follows the camera given ---


def test_the_room_scale_is_unchanged_whatever_camera_is_given():
    assert ROOM_SCALE.profile == ROOM_LIMITS.name
    assert ROOM_SCALE.coordinator_kwargs() == {
        "area_cm": (400, 400),
        "strip_cm": 75,
        "alt_slots_cm": tuple(coordinator.DEFAULT_ALT_SLOTS_CM),
        "geofence_cm": float(DEFAULT_GEOFENCE_CM),
        "zone_gap_cm": coordinator.DEFAULT_ZONE_GAP_CM,
    }
    for n in (1, 2, 4):
        assert scale_for("room", n) is ROOM_SCALE
        assert scale_for("room", n, camera=OPEN_QUAD_4K) is ROOM_SCALE
        assert scale_for("room", n, camera=PI_CAMERA_MODULE_3) is ROOM_SCALE
        assert scale_for("room", n, **QUAD_VALUES) is ROOM_SCALE


def test_a_field_environment_takes_its_scale_from_the_camera_given():
    given = scale_for("field", 2, camera=OPEN_QUAD_4K)
    assert given == field_scale(2, camera=OPEN_QUAD_4K)
    assert scale_for("field", 2, **QUAD_VALUES) == given
    # The lanes are spaced from the swath of that camera at the lowest slot (15 m by default).
    assert given.swath_cm == pytest.approx(2 * 1500 * math.tan(math.radians(OPEN_QUAD_4K.hfov_deg / 2)))
    assert given.swath_cm == pytest.approx(nadir_swath_m(OPEN_QUAD_4K, 15.0) * 100)
    assert given.strip_cm <= given.swath_cm * 0.8
    # A wider lens than the default camera gives wider lanes than the default field scale.
    assert given.strip_cm > field_scale(2).strip_cm


def test_without_a_camera_the_field_scale_keeps_its_defaults():
    for n in (1, 2):
        assert scale_for("field", n) == field_scale(n)
        assert scale_for("field", n, camera=PI_CAMERA_MODULE_3) == field_scale(n)


def test_field_options_reach_the_field_scale():
    options = {"area_m": (70, 30), "alt_slots_m": (15, 20), "overlap": 0.3, "zone_gap_m": 2.0, "limits": FIELD_LIMITS}
    assert scale_for("field", 2, **options) == field_scale(2, **options)
    assert scale_for("field", 2, camera=OPEN_QUAD_4K, **options) == field_scale(2, camera=OPEN_QUAD_4K, **options)
    # field_scale's own refusals still apply.
    with pytest.raises(ValueError, match="altitude slots must be at least 5 m apart"):
        scale_for("field", 2, **QUAD_VALUES, alt_slots_m=(15, 17))


@pytest.mark.parametrize("environment", ["moon", "Room", "FIELD", "", None, 1])
def test_an_unknown_environment_is_refused(environment):
    with pytest.raises(ValueError, match="environment must be one of 'room', 'field'"):
        scale_for(environment, 2)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"hfov_deg": 66.0}, "needs hfov_deg, width_px and height_px; missing height_px, width_px"),
        ({"camera": OPEN_QUAD_4K, "hfov_deg": 66.0}, "either as a CameraModel or as values, not both"),
        ({"camera": "wide"}, "camera must be a coverage.CameraModel, got str"),
        ({"hfov_deg": 0, "width_px": 640, "height_px": 480}, "hfov_deg must be a positive number"),
        ({"hfov_deg": float("nan"), "width_px": 640, "height_px": 480}, "hfov_deg must be a positive number"),
        ({"hfov_deg": 180, "width_px": 640, "height_px": 480}, "hfov_deg must be below a half turn"),
        ({"hfov_deg": 66.0, "width_px": 0, "height_px": 480}, "width_px must be a positive whole number of pixels"),
        ({"hfov_deg": 66.0, "width_px": 640.5, "height_px": 480}, "width_px must be a positive whole number"),
        ({"hfov_deg": 66.0, "width_px": 640, "height_px": True}, "height_px must be a positive whole number"),
    ],
)
def test_a_malformed_camera_is_refused_for_either_environment(kwargs, message):
    for environment in ("room", "field"):
        with pytest.raises(ValueError, match=message):
            scale_for(environment, 2, **kwargs)


def test_the_room_scale_takes_no_field_options():
    with pytest.raises(ValueError, match="the room scale is fixed and takes no field options, got alt_slots_m, area_m"):
        scale_for("room", 2, area_m=(60.0, 60.0), alt_slots_m=(15.0, 20.0))


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "2"])
def test_the_drone_count_is_checked_for_either_environment(bad):
    for environment in ("room", "field"):
        with pytest.raises(ValueError, match="n_drones must be a positive integer"):
            scale_for(environment, bad)


# --- Patterns: room units build today's plans ---


def test_explicit_room_limits_build_todays_plans_and_refusals():
    today = _builds(lambda ids: {})
    assert any(isinstance(plan, fp.FleetSearchPlan) for plan in today)
    assert any(isinstance(plan, tuple) for plan in today)

    def room(ids):
        return {
            "unit_limits": {drone_id: ROOM_LIMITS for drone_id in ids},
            "max_area_side_cm": fp.MAX_AREA_SIDE_CM,
            "min_sweep_cm": fp.MIN_SWEEP_CM,
            "altitude_deadband_cm": fp.ALTITUDE_DEADBAND_CM,
            "altitude_tolerance_cm": fp.ALTITUDE_TOLERANCE_CM,
        }

    assert _builds(room) == today


def test_units_the_plan_does_not_fly_are_ignored():
    ids = _ids(2)
    with_spare = {"d1": ROOM_LIMITS, "d2": ROOM_LIMITS, "spare": COARSE}
    for lkp in (None, (600, 400)):
        assert fp.plan_search(ids, (1200, 800), None, lkp, footprint=ROOM_CAMERA, unit_limits=with_spare) == (
            fp.plan_search(ids, (1200, 800), None, lkp, footprint=ROOM_CAMERA))


def test_room_lanes_keep_todays_moves():
    lanes = fp.expanding_square_lanes(initial_side_cm=450, growth_cm=200, num_expansions=5)
    assert fp.expanding_square_lanes(450, 200, 5, limits=ROOM_LIMITS, max_area_side_cm=fp.MAX_AREA_SIDE_CM) == lanes
    assert max(lane["distance_cm"] for lane in lanes) <= MAX_MOVE_CM


# --- Patterns: a long-move profile gives metre-scale lanes that pass their own check ---


def test_a_field_profile_gives_metre_scale_parallel_lanes_that_pass_their_own_check():
    ids = _ids(2)
    limits = {drone_id: FIELD_LIMITS for drone_id in ids}
    plan = fp.plan_parallel_split(ids, (4000, 6000), 1000, unit_limits=limits, **FIELD)
    assert plan.problems == () and plan.feasible
    distances = _distances(plan)
    assert max(distances) > MAX_MOVE_CM
    assert all(FIELD_LIMITS.min_move_cm <= d <= FIELD_LIMITS.max_move_cm for d in distances)
    assert all(isinstance(d, int) and not isinstance(d, bool) for d in distances)
    assert fp.check_plan(plan, unit_limits=limits) == []
    # The same area for room units is cut into moves of at most 5 m, five times as many per drone.
    room = fp.plan_parallel_split(ids, (4000, 6000), 1000, footprint=SQUARE, geofence_cm=6000)
    assert room.problems == () and max(_distances(room)) == MAX_MOVE_CM
    assert len(_distances(room)) > 4 * len(distances)


def test_the_ring_patterns_follow_a_field_profile_too():
    two = {drone_id: FIELD_LIMITS for drone_id in _ids(2)}
    one = {"d1": FIELD_LIMITS}
    square = fp.plan_split_expanding_square(_ids(2), (4000, 4000), (2000, 2000), 1000, unit_limits=two, **FIELD)
    spiral = fp.plan_square_spiral(
        "d1", (4000, 4000), (2000, 2000), 1000, footprint=SQUARE, alt_cm=1500, geofence_cm=6000, unit_limits=one)
    chosen = fp.plan_search(_ids(2), (4000, 4000), 1000, (2000, 2000), unit_limits=two, **FIELD)
    assert chosen.pattern is fp.PatternKind.SPLIT_EXPANDING_SQUARE
    for plan, limits in ((square, two), (spiral, one), (chosen, two)):
        assert plan.problems == () and plan.feasible
        assert max(_distances(plan)) > MAX_MOVE_CM
        assert all(FIELD_LIMITS.min_move_cm <= d <= FIELD_LIMITS.max_move_cm for d in _distances(plan))
        assert fp.check_plan(plan, unit_limits=limits) == []


def test_expanding_square_lanes_follow_the_limits_given():
    field = fp.expanding_square_lanes(1000, 1000, 2, limits=FIELD_LIMITS)
    assert [lane["distance_cm"] for lane in field] == [1000, 1000, 2000, 2000, 3000, 3000, 4000, 4000]
    assert len(fp.expanding_square_lanes(1000, 1000, 2)) == 2 + 2 + 4 + 4 + 6 + 6 + 8 + 8
    with pytest.raises(ValueError, match="initial_side_cm must be at least 100 cm, got 50"):
        fp.expanding_square_lanes(50, limits=COARSE)
    with pytest.raises(ValueError, match="initial_side_cm must be at most 20000 cm"):
        fp.expanding_square_lanes(30_000, limits=FIELD_LIMITS)
    assert len(fp.expanding_square_lanes(30_000, 0, 1, limits=FIELD_LIMITS, max_area_side_cm=None)) == 4 * 6


def test_no_area_cap_builds_an_area_the_default_cap_refuses():
    kwargs = {"footprint": SQUARE, "alt_slots_cm": (1500,), "geofence_cm": 26_000, "unit_limits": {"d1": FIELD_LIMITS}}
    with pytest.raises(ValueError, match="area width must be at most 20000 cm, got 25000"):
        fp.plan_parallel_split(["d1"], (25_000, 1000), 1000, **kwargs)
    plan = fp.plan_parallel_split(["d1"], (25_000, 1000), 1000, max_area_side_cm=None, **kwargs)
    assert plan.problems == () and plan.feasible
    assert fp.check_plan(plan, unit_limits=kwargs["unit_limits"], max_area_side_cm=None) == []


# --- Patterns: a mixed fleet gives each drone its own limits ---


def test_a_mixed_fleet_gives_each_drone_its_own_limits():
    limits = {"d1": ROOM_LIMITS, "d2": FIELD_LIMITS}
    kwargs = {"footprint": SQUARE, "geofence_cm": 4000, "unit_limits": limits}
    plan = fp.plan_parallel_split(["d1", "d2"], (2000, 3000), 400, alt_slots_cm=(100, 1500), **kwargs)
    assert plan.problems == () and plan.feasible
    assert [drone.alt_cm for drone in plan.drones] == [100, 1500]
    assert max(_distances(plan, "d1")) == MAX_MOVE_CM
    assert max(_distances(plan, "d2")) > MAX_MOVE_CM
    # Checked as two room units, only the field unit's lanes and slot are refused.
    problems = fp.check_plan(plan, unit_limits={"d1": ROOM_LIMITS, "d2": ROOM_LIMITS})
    assert problems and not [p for p in problems if p.startswith("d1")]
    assert "d2: altitude slot 1500 cm is out of range" in problems
    # Each slot must lie within the range of the drone that flies it.
    with pytest.raises(ValueError, match="altitude slot must be within 50-500 cm, got 1500"):
        fp.plan_parallel_split(["d1", "d2"], (2000, 3000), 400, alt_slots_cm=(1500, 100), **kwargs)


def test_what_the_drones_share_uses_the_largest_minimum_move():
    coarse = {"d1": ROOM_LIMITS, "d2": COARSE}
    with pytest.raises(ValueError, match="placement_offset_cm must be 0 or at least 100 cm, got 50"):
        fp.choose_pattern(2, (1200, 800), (600, 400), unit_limits=coarse)
    with pytest.raises(ValueError, match="placement_offset_cm must be 0 or at least 100 cm, got 50"):
        fp.plan_search(_ids(2), (1200, 800), 200, (600, 400), footprint=SQUARE, unit_limits=coarse)
    with pytest.raises(ValueError, match="area width must be at least 100 cm, got 60"):
        fp.plan_parallel_split(["d1", "d2"], (60, 400), 120, footprint=SQUARE, unit_limits=coarse)
    chosen = fp.choose_pattern(2, (1200, 800), (600, 400), unit_limits=coarse, placement_offset_cm=100)
    assert chosen.kind is fp.PatternKind.SPLIT_EXPANDING_SQUARE


def test_each_strip_must_hold_its_own_drones_minimum_move():
    # 260 cm of width less one 170 cm gap leaves two 45 cm strips: enough for room moves, not for 1 m ones.
    room = fp.plan_parallel_split(["d1", "d2"], (260, 400), 120, footprint=SQUARE, geofence_cm=None)
    assert [drone.region.x1 - drone.region.x0 for drone in room.drones] == [45, 45]
    with pytest.raises(ValueError, match="area width 260 cm is too narrow for 2 strips with 170 cm gaps"):
        fp.plan_parallel_split(
            ["d1", "d2"], (260, 400), 120, footprint=SQUARE, geofence_cm=None,
            unit_limits={"d1": ROOM_LIMITS, "d2": dataclasses.replace(ROOM_LIMITS, min_move_cm=50, max_move_cm=500)},
        )


# --- The built plan is checked with the limits it was built with ---


def test_the_plan_is_checked_with_the_limits_it_was_built_with():
    ids = _ids(2)
    limits = {drone_id: FIELD_LIMITS for drone_id in ids}
    plan = fp.plan_parallel_split(ids, (4000, 6000), 1000, unit_limits=limits, **FIELD)
    assert plan.problems == tuple(fp.check_plan(plan, unit_limits=limits)) == ()
    # The room profile, the default, refuses the same plan.
    room_problems = fp.check_plan(plan)
    lane = "d1: lane 0 {'direction': 'forward', 'distance_cm': 3000} is not a 20-500 cm horizontal move"
    assert lane in room_problems
    message = plan.assign_lanes_message("d2", unit_limits=limits, allow_unchecked=True)
    assert message["alt_cm"] == 2000
    assert [lane["distance_cm"] for lane in message["lanes"]] == _distances(plan, "d2")
    with pytest.raises(ValueError, match="refusing assign_lanes for d2: the plan is infeasible"):
        plan.assign_lanes_message("d2", allow_unchecked=True)


def test_the_footprint_margins_reach_every_builder_and_its_check():
    margins = {"altitude_deadband_cm": 30, "altitude_tolerance_cm": 15}
    built = [
        fp.plan_parallel_split(_ids(2), (1200, 800), None, footprint=ROOM_CAMERA, **margins),
        fp.plan_split_expanding_square(_ids(2), (1200, 800), (600, 400), None, footprint=ROOM_CAMERA, **margins),
        fp.plan_square_spiral("d1", (1200, 800), (600, 400), None, footprint=ROOM_CAMERA, **margins),
        fp.plan_search(_ids(2), (1200, 800), None, (600, 400), footprint=ROOM_CAMERA, **margins),
    ]
    for plan in built:
        assert plan.footprint.altitude_margin_cm == 45
        assert plan.footprint.lateral_cm == fp.planning_footprint(
            ROOM_CAMERA, None, [drone.alt_cm for drone in plan.drones], 800, **margins).lateral_cm
        assert list(plan.problems) == fp.check_plan(plan)


def test_min_sweep_cm_reaches_the_ring_patterns_and_the_choice():
    with pytest.raises(ValueError, match="the square spiral needs an area at least 450 cm on each side, got 400 x 400"):
        fp.plan_square_spiral("d1", (400, 400), (200, 200), 450, footprint=SQUARE, min_sweep_cm=450)
    choice = fp.choose_pattern(1, (400, 400), (200, 200), min_sweep_cm=450)
    assert choice == fp.PatternChoice(
        fp.PatternKind.PARALLEL_SPLIT, "the area is narrower than 450 cm for rings; parallel split"
    )
    plan = fp.plan_search(["d1"], (400, 400), 450, (200, 200), footprint=SQUARE, min_sweep_cm=450)
    assert plan.pattern is fp.PatternKind.PARALLEL_SPLIT and plan.sweep_cm == 450


# --- Slots are an argument, and the slots given are what the drones fly ---


def test_the_slots_given_are_the_slots_the_drones_fly():
    assert fp.DEFAULT_ALT_SLOTS_CM == coordinator.DEFAULT_ALT_SLOTS_CM
    slots = (100, 120, 140)
    for n in (1, 2, 3, 4):
        for lkp in (None, (600, 400)):
            plan = fp.plan_search(_ids(n), (1200, 800), None, lkp, footprint=ROOM_CAMERA, alt_slots_cm=slots)
            assert [drone.alt_cm for drone in plan.drones] == [slots[i % len(slots)] for i in range(n)]
            for drone in plan.drones:
                assert drone.assign_lanes_fields()["alt_cm"] == drone.alt_cm
    split = fp.plan_split_expanding_square(_ids(2), (1200, 800), (600, 400), None, footprint=ROOM_CAMERA,
                                           alt_slots_cm=(110, 140))
    assert split.feasible and [drone.alt_cm for drone in split.drones] == [110, 140]
    assert split.assign_lanes_message("d2")["alt_cm"] == 140
    spiral = fp.plan_square_spiral("d1", (1200, 800), (600, 400), None, footprint=ROOM_CAMERA, alt_cm=120)
    assert spiral.feasible and spiral.drones[0].alt_cm == 120


def test_a_slot_no_drone_flies_must_suit_every_drone():
    kwargs = {"footprint": SQUARE, "geofence_cm": 4000}
    with pytest.raises(ValueError, match="altitude slot must be within 50-500 cm, got 1500"):
        fp.plan_parallel_split(["d1"], (2000, 3000), 400, alt_slots_cm=(100, 1500), **kwargs)
    with pytest.raises(ValueError, match="alt_slots_cm must contain at least one altitude"):
        fp.plan_parallel_split(["d1"], (2000, 3000), 400, alt_slots_cm=(), unit_limits={"d1": FIELD_LIMITS}, **kwargs)


# --- Malformed per-unit arguments are refused by every builder ---


def _every_builder(unit_limits):
    two = _ids(2)
    return [
        lambda: fp.plan_parallel_split(two, (1200, 800), 200, footprint=SQUARE, unit_limits=unit_limits),
        lambda: fp.plan_split_expanding_square(
            two, (1200, 800), (600, 400), 200, footprint=SQUARE, unit_limits=unit_limits),
        lambda: fp.plan_square_spiral("d2", (1200, 800), (600, 400), 200, footprint=SQUARE, unit_limits=unit_limits),
        lambda: fp.plan_search(two, (1200, 800), 200, (600, 400), footprint=SQUARE, unit_limits=unit_limits),
    ]


def test_a_mapping_that_lacks_a_planned_drone_is_refused_by_every_builder():
    for build in _every_builder({"d1": FIELD_LIMITS}):
        with pytest.raises(ValueError, match="unit_limits has no envelope limits for 'd2'"):
            build()


@pytest.mark.parametrize("bad", [ROOM_LIMITS, [("d1", ROOM_LIMITS)], "room"])
def test_unit_limits_must_be_a_mapping(bad):
    for build in _every_builder(bad):
        with pytest.raises(TypeError, match="unit_limits must map drone ids to safety.EnvelopeLimits"):
            build()
    with pytest.raises(TypeError, match="unit_limits must map drone ids to safety.EnvelopeLimits"):
        fp.choose_pattern(2, (1200, 800), (600, 400), unit_limits=bad)


def test_unit_limits_values_must_be_envelope_limits():
    for build in _every_builder({"d1": ROOM_LIMITS, "d2": "field"}):
        with pytest.raises(TypeError, match="limits must be a safety.EnvelopeLimits, got str"):
            build()
    with pytest.raises(TypeError, match="limits must be a safety.EnvelopeLimits, got str"):
        fp.expanding_square_lanes(limits="field")


@pytest.mark.parametrize("bad", [0, -5, float("inf"), "big", True])
def test_a_malformed_area_cap_is_refused(bad):
    with pytest.raises(ValueError, match="max_area_side_cm"):
        fp.plan_search(_ids(2), (1200, 800), 200, footprint=SQUARE, max_area_side_cm=bad)
    with pytest.raises(ValueError, match="max_area_side_cm"):
        fp.expanding_square_lanes(max_area_side_cm=bad)


@pytest.mark.parametrize("bad", [0, 1.5, "wide", None])
def test_a_malformed_min_sweep_is_refused(bad):
    with pytest.raises(ValueError, match="min_sweep_cm"):
        fp.choose_pattern(1, (400, 400), (200, 200), min_sweep_cm=bad)
    with pytest.raises(ValueError, match="min_sweep_cm"):
        fp.plan_square_spiral("d1", (400, 400), (200, 200), 120, footprint=SQUARE, min_sweep_cm=bad)
