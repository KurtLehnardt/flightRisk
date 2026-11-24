"""Detection profile requirement fields (FEA-8 fields) and behaviour neutrality."""

from __future__ import annotations

import dataclasses

import pytest

from flightrisk.vision.profiles import DetectionProfile, REGISTRY, builtin_profiles

# Public values of every built-in profile, taken before requires_sensor and requires_view existed.
SNAPSHOT = {
    "default": dict(label="Startup default (yolo11n)", weights="yolo11n.pt", weights_required=False,
                    confidence=0.4, nms_iou=None, input_size=None, tiled=None, tile_size=None,
                    tile_overlap=None, min_person_px=None, view="any", face_weight_scale=1.0,
                    reid_weight_scale=1.0, fallback=None, weights_env="FLIGHTRISK_DETECTOR_MODEL"),
    "top_down": dict(label="Top-down search (HERIDAL)", weights="models/heridal_yolo11s.pt",
                     weights_required=True, confidence=0.5, nms_iou=None, input_size=None, tiled=True,
                     tile_size=None, tile_overlap=None, min_person_px=32, view="nadir",
                     face_weight_scale=1.0, reid_weight_scale=1.0, fallback="level", weights_env=None),
    "level": dict(label="Level flight (faces)", weights="yolo11n.pt", weights_required=False,
                  confidence=0.4, nms_iou=None, input_size=None, tiled=False, tile_size=None,
                  tile_overlap=None, min_person_px=None, view="oblique", face_weight_scale=1.0,
                  reid_weight_scale=1.0, fallback=None, weights_env=None),
}
NEW_FIELDS = {"requires_sensor", "requires_view"}


def _profile(**kw):
    return DetectionProfile(name="x", label="X", description="d", weights="w.pt", **kw)


def test_builtin_profiles_resolve_to_the_same_values_as_before():
    assert {p.name for p in builtin_profiles()} == set(SNAPSHOT)
    for profile in builtin_profiles():
        actual = {k: v for k, v in dataclasses.asdict(profile).items()
                  if k not in NEW_FIELDS | {"name", "description"}}
        assert actual == SNAPSHOT[profile.name], profile.name


def test_builtin_profiles_state_no_requirement():
    for profile in builtin_profiles():
        assert profile.requires_sensor is None
        assert profile.requires_view is None


def test_registry_resolves_builtins_unchanged():
    for name, values in SNAPSHOT.items():
        assert REGISTRY.get(name).weights == values["weights"]


def test_describe_does_not_gain_the_new_fields():
    for profile in builtin_profiles():
        assert not NEW_FIELDS & set(profile.describe())


def test_fields_default_to_none():
    p = _profile()
    assert (p.requires_sensor, p.requires_view) == (None, None)


@pytest.mark.parametrize("sensor", ["rgb", "thermal", None])
def test_requires_sensor_accepts_known_values(sensor):
    assert _profile(requires_sensor=sensor).requires_sensor == sensor


@pytest.mark.parametrize("view", ["level", "nadir", "any", None])
def test_requires_view_accepts_known_values(view):
    assert _profile(requires_view=view).requires_view == view


def test_unknown_values_are_refused():
    with pytest.raises(ValueError):
        _profile(requires_sensor="infrared")
    with pytest.raises(ValueError):
        _profile(requires_view="oblique")
