"""Features (FEA-1 to FEA-5, FEA-8 fields, FEA-10, CAT-6)."""

from __future__ import annotations

import dataclasses

import pytest

from flightrisk.hardware.catalogue import BUILTIN_MODELS, CameraSpec
from flightrisk.hardware.features import (
    APPEARANCE_MATCH, BIRDSEYE_SEARCH, CLAUDE_SECOND_OPINION, FEATURE_IDS, FEATURES,
    FORWARD_OBSTACLE_SENSING, LEVEL_SEARCH, ONBOARD_AVOIDANCE, SLOT_ADAPTER_REASON, THERMAL_DETECTION,
    FeatureRefused, feature_reason, model_addable_reason, resolve_features,
)

TELLO = BUILTIN_MODELS["tello"]
GENERIC = BUILTIN_MODELS["generic_multirotor"]


def _cam(**kw):
    base = dict(id="c", mount="m", mount_kind="gimbal", orientation=None, pitch_min_deg=-90,
                pitch_max_deg=0, pitch_measured=True, stream_w_px=1920, stream_h_px=1080,
                hfov_deg=82, fps=30)
    base.update(kw)
    return CameraSpec(**base)


def _with(model, *cameras):
    return dataclasses.replace(model, cameras=tuple(cameras))


def test_seven_features_with_requirements():
    assert len(FEATURE_IDS) == 7
    for feature in FEATURES.values():
        assert feature.static_requirement
        assert feature.when_unavailable
        assert feature.runtime_requirement is None or feature.runtime_requirement


def test_every_unavailable_feature_has_a_reason():
    nothing = _with(GENERIC, _cam(id="t", mount="t", mount_kind="fixed", orientation="forward",
                                  pitch_measured=False, sensor="thermal"))
    for model in (TELLO, GENERIC, nothing):
        for fid in FEATURE_IDS:
            reason = feature_reason(fid, model)
            assert reason is None or reason.strip()


def test_tello_availability_set():
    for fid in (FORWARD_OBSTACLE_SENSING, LEVEL_SEARCH, APPEARANCE_MATCH):
        assert feature_reason(fid, TELLO) is None
    assert feature_reason(BIRDSEYE_SEARCH, TELLO) == "the Tello has one fixed forward camera"
    assert feature_reason(THERMAL_DETECTION, TELLO) == "no thermal camera"
    assert feature_reason(ONBOARD_AVOIDANCE, TELLO) == "the Tello has no onboard avoidance"


def test_tello_edu_matches_tello():
    edu = BUILTIN_MODELS["tello_edu"]
    for fid in FEATURE_IDS:
        assert feature_reason(fid, edu) == feature_reason(fid, TELLO)


def test_birdseye_follows_the_nadir_role():
    assert feature_reason(BIRDSEYE_SEARCH, GENERIC) is not None
    down = _with(GENERIC, _cam(id="d", mount="d", mount_kind="fixed", orientation="downward", pitch_measured=False))
    assert feature_reason(BIRDSEYE_SEARCH, down) is None
    gimbal = _with(GENERIC, _cam())
    assert feature_reason(BIRDSEYE_SEARCH, gimbal) is not None
    assert feature_reason(BIRDSEYE_SEARCH, gimbal, mission_pitch_deg={"m": -60}) is None
    assert feature_reason(BIRDSEYE_SEARCH, gimbal, mission_pitch_deg={"m": -30}) is not None


def test_thermal_needs_a_thermal_camera_on_a_searching_mount():
    dual = _with(GENERIC, _cam(id="rgb"), _cam(id="th", sensor="thermal", radiometric=True))
    assert feature_reason(THERMAL_DETECTION, dual, mission_pitch_deg={"m": -90}) is None
    assert feature_reason(THERMAL_DETECTION, dual) is not None  # no mission pitch, no role
    assert feature_reason(THERMAL_DETECTION, dual, mission_pitch_deg={"m": -90}, search_roles=("level",)) is not None


def test_appearance_match_needs_rgb():
    thermal_only = _with(GENERIC, _cam(sensor="thermal"))
    assert feature_reason(APPEARANCE_MATCH, thermal_only) == "no RGB camera"


def test_claude_second_opinion_is_never_blocked_by_the_unit():
    assert feature_reason(CLAUDE_SECOND_OPINION, TELLO) is None


def test_unknown_feature_is_a_key_error():
    with pytest.raises(KeyError):
        feature_reason("nope", TELLO)


def test_mavlink_obstacle_sensing_needs_camera_or_url_source():
    assert feature_reason(FORWARD_OBSTACLE_SENSING, GENERIC) is not None
    assert feature_reason(FORWARD_OBSTACLE_SENSING, GENERIC, source="none") is not None
    assert feature_reason(FORWARD_OBSTACLE_SENSING, GENERIC, source="camera") is None
    assert feature_reason(FORWARD_OBSTACLE_SENSING, GENERIC, source="url") is None


def test_mavlink_obstacle_sensing_never_depends_on_stepped_sensing():
    assert feature_reason(FORWARD_OBSTACLE_SENSING, GENERIC, source="camera", stepped_sensing=False) is None
    assert feature_reason(FORWARD_OBSTACLE_SENSING, GENERIC, source="none", stepped_sensing=True) is not None


def test_mavlink_obstacle_sensing_needs_a_level_mount():
    down = _with(GENERIC, _cam(id="d", mount="d", mount_kind="fixed", orientation="downward", pitch_measured=False))
    assert feature_reason(FORWARD_OBSTACLE_SENSING, down, source="camera") is not None


def test_tello_obstacle_sensing_needs_stepped_sensing():
    assert feature_reason(FORWARD_OBSTACLE_SENSING, TELLO, stepped_sensing=False) is not None


def test_gates_dictionary_shape_and_defaults():
    gates = resolve_features(TELLO)
    assert set(gates) == set(FEATURE_IDS)
    for gate in gates.values():
        assert set(gate) == {"on", "ok", "why", "src"}
        assert isinstance(gate["on"], bool) and isinstance(gate["ok"], bool)
        assert isinstance(gate["why"], str) and isinstance(gate["src"], str)
    assert gates[FORWARD_OBSTACLE_SENSING]["on"] and gates[LEVEL_SEARCH]["on"]
    assert not gates[BIRDSEYE_SEARCH]["on"] and not gates[BIRDSEYE_SEARCH]["ok"]
    assert gates[BIRDSEYE_SEARCH]["why"]


def test_switching_on_an_unavailable_feature_is_refused_with_its_reason():
    with pytest.raises(FeatureRefused) as err:
        resolve_features(TELLO, {BIRDSEYE_SEARCH: True}, {BIRDSEYE_SEARCH: "launcher"})
    assert str(err.value) == "the Tello has one fixed forward camera"
    assert err.value.feature == BIRDSEYE_SEARCH


def test_unavailable_default_is_off_not_refused():
    gates = resolve_features(GENERIC)
    assert gates[FORWARD_OBSTACLE_SENSING] == {"on": False, "ok": False, "why": gates[FORWARD_OBSTACLE_SENSING]["why"],
                                               "src": "default"}
    assert gates[FORWARD_OBSTACLE_SENSING]["why"]


def test_switching_off_is_allowed_and_records_the_source():
    gates = resolve_features(TELLO, {LEVEL_SEARCH: False}, {LEVEL_SEARCH: "file"})
    assert gates[LEVEL_SEARCH]["on"] is False and gates[LEVEL_SEARCH]["src"] == "file"


def test_available_feature_switched_on_by_request():
    down = _with(GENERIC, _cam(id="d", mount="d", mount_kind="fixed", orientation="downward", pitch_measured=False))
    gates = resolve_features(down, {BIRDSEYE_SEARCH: True}, {BIRDSEYE_SEARCH: "env"})
    assert gates[BIRDSEYE_SEARCH] == {"on": True, "ok": True, "why": "", "src": "env"}


def test_unknown_requested_feature_is_refused():
    with pytest.raises(KeyError):
        resolve_features(TELLO, {"nope": True})


def test_slot_model_is_listed_but_not_addable():
    slot = dataclasses.replace(TELLO, id="dji_air", adapter="dji_msdk", support="slot")
    assert SLOT_ADAPTER_REASON == "no adapter for that protocol is installed"
    assert model_addable_reason(slot) == SLOT_ADAPTER_REASON
    assert model_addable_reason(TELLO) is None
    assert model_addable_reason(GENERIC) is None


def test_role2_resolve_features_refuses_level_and_nadir_on_one_gimbal_naming_the_mount():
    """B3: the critic's probe, through resolve_features rather than the helper."""
    from flightrisk.hardware.features import FeatureRefused, resolve_features
    one_gimbal = _with(GENERIC, _cam(id="rgb", mount="gimbal", sensor="rgb"))
    with pytest.raises(FeatureRefused) as err:
        resolve_features(one_gimbal, {"forward_obstacle_sensing": True, "birdseye_search": True},
                         mission_pitch_deg={"gimbal": 0}, source="camera")
    assert "`gimbal`" in str(err.value)
    assert str(err.value) == ("camera mount `gimbal` cannot look level for obstacle sensing and down for "
                              "bird's-eye search at once")


def test_role2_no_conflict_when_a_fixed_mount_serves_one_role():
    from flightrisk.hardware.features import resolve_features
    two = _with(GENERIC, _cam(id="fwd", mount="front", mount_kind="fixed", orientation="forward",
                              pitch_min_deg=None, pitch_max_deg=None, pitch_measured=False, sensor="rgb"),
                _cam(id="rgb", mount="gimbal", sensor="rgb"))
    gates = resolve_features(two, {"forward_obstacle_sensing": True, "birdseye_search": True},
                             mission_pitch_deg={"gimbal": -90}, source="camera")
    assert gates["birdseye_search"]["on"] and gates["forward_obstacle_sensing"]["on"]


def test_safety_switch_off_needs_confirmation():
    """L3: forward_obstacle_sensing off is refused unless the caller passes the operator's confirmation."""
    with pytest.raises(FeatureRefused, match="confirmation"):
        resolve_features(TELLO, {FORWARD_OBSTACLE_SENSING: False})
    gates = resolve_features(TELLO, {FORWARD_OBSTACLE_SENSING: False}, confirmed_off=True)
    assert gates[FORWARD_OBSTACLE_SENSING]["on"] is False


def test_level_search_conflict_names_the_mount_and_the_feature_requested():
    one_gimbal = _with(GENERIC, _cam(id="rgb", mount="gimbal", sensor="rgb"))
    with pytest.raises(FeatureRefused) as err:
        resolve_features(one_gimbal, {"level_search": True, "birdseye_search": True},
                         mission_pitch_deg={"gimbal": 0}, source="camera")
    assert str(err.value) == ("camera mount `gimbal` cannot look level for level search and down for "
                              "bird's-eye search at once")
