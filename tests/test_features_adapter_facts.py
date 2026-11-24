"""Feature reasons from adapter facts, not adapter ids (XC-02, SEAM-4 consumer, FEA-10 unchanged).

``feature_reason`` reads the adapter spec's ``stepped_sensing``, ``declares_avoidance`` and ``short_name``
facts instead of comparing a model's adapter id with the Tello or MAVLink id. The tables below are the
reasons the code gave before that change, captured by running it on the unmodified branch and pasted in as
literals, so every built-in model, and custom models on every catalogued adapter and on an uncatalogued one,
must keep exactly the same reason for every feature.
"""

from __future__ import annotations

import dataclasses
from types import MappingProxyType

import pytest

from flightrisk.hardware import catalogue, features
from flightrisk.hardware.catalogue import ADAPTER_SPECS, BUILTIN_MODELS, AdapterSpec, CameraSpec
from flightrisk.hardware.features import (
    BIRDSEYE_SEARCH, FEATURE_IDS, FORWARD_OBSTACLE_SENSING, ONBOARD_AVOIDANCE, feature_reason, resolve_features,
)

#: Column order of every expectation row. Pinned here so a reordered FEATURE_IDS cannot shift the columns.
COLUMNS = (
    "forward_obstacle_sensing", "onboard_avoidance", "level_search", "birdseye_search", "thermal_detection",
    "appearance_match", "claude_second_opinion",
)

PITCH = "declare the gimbal's mission pitch"
NO_DOWN = "no camera looks down: needs a downward mount or a gimbal with a mission pitch of -60 degrees or lower"
NO_LEVEL = "no camera mount looks level"
NO_LEVEL_RGB = "no level RGB camera for obstacle sensing"
NO_THERMAL = "no thermal camera"
NEEDS_SOURCE = "obstacle sensing from the camera needs --source camera or url"
TELLO_NO_AVOIDANCE = "the Tello has no onboard avoidance"
TELLO_ONE_CAMERA = "the Tello has one fixed forward camera"
NOT_STEPPED = "the drone does not support stepped sensing"
NO_AVOIDANCE = "this drone has no onboard avoidance"

#: Keyword arguments of ``feature_reason`` for each context of the built-in and adapter-swap tables.
CONTEXTS = {
    "default": {},
    "camera": {"source": "camera"},
    "stepped": {"source": None, "stepped_sensing": True},
    "unstepped": {"stepped_sensing": False},
    "camera_unstepped": {"source": "camera", "stepped_sensing": False},
}

#: Contexts of the gimbal table: no mission pitch, a level one, and a nadir one.
GIMBAL_CONTEXTS = {
    "default": {},
    "level": {"mission_pitch_deg": {"gimbal": 0}},
    "level_camera": {"mission_pitch_deg": {"gimbal": 0}, "source": "camera"},
    "level_unstepped": {"mission_pitch_deg": {"gimbal": 0}, "stepped_sensing": False},
    "nadir": {"mission_pitch_deg": {"gimbal": -90}},
}

#: Every catalogued adapter and one that is not in ADAPTER_SPECS.
ADAPTERS = ("tello_sdk_udp", "mavlink", "dji_msdk", "unknown_adapter")

GIMBAL_RGB = CameraSpec(
    id="rgb", mount="gimbal", mount_kind="gimbal", orientation=None, pitch_min_deg=-90, pitch_max_deg=0,
    pitch_measured=True, stream_w_px=1920, stream_h_px=1080, hfov_deg=70.0, fps=30,
)
GIMBAL_IR = dataclasses.replace(GIMBAL_RGB, id="ir", sensor="thermal", stream_w_px=640, stream_h_px=512,
                                hfov_deg=40.0)

# The three tables were captured from the code before the adapter facts existed.
# Built-in models. Keys: (model id, context).
BUILTIN_EXPECTED = {
    ("tello", "default"): (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello", "camera"): (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello", "stepped"): (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello", "unstepped"): (NOT_STEPPED, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello", "camera_unstepped"):
        (NOT_STEPPED, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello_edu", "default"): (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello_edu", "camera"): (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello_edu", "stepped"): (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello_edu", "unstepped"): (NOT_STEPPED, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello_edu", "camera_unstepped"):
        (NOT_STEPPED, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("generic_multirotor", "default"): (NEEDS_SOURCE, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "camera"): (None, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "stepped"): (NEEDS_SOURCE, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "unstepped"): (NEEDS_SOURCE, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "camera_unstepped"): (None, None, None, NO_DOWN, NO_THERMAL, None, None),
}

# A built-in model moved to each adapter. Keys: (built-in model id, adapter id, context).
ADAPTER_SWAP_EXPECTED = {
    ("tello", "tello_sdk_udp", "default"):
        (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello", "tello_sdk_udp", "camera"):
        (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello", "tello_sdk_udp", "stepped"):
        (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello", "tello_sdk_udp", "unstepped"):
        (NOT_STEPPED, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello", "tello_sdk_udp", "camera_unstepped"):
        (NOT_STEPPED, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("tello", "mavlink", "default"): (NOT_STEPPED, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "mavlink", "camera"): (NOT_STEPPED, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "mavlink", "stepped"): (None, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "mavlink", "unstepped"): (NOT_STEPPED, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "mavlink", "camera_unstepped"): (NOT_STEPPED, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "dji_msdk", "default"): (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "dji_msdk", "camera"): (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "dji_msdk", "stepped"): (None, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "dji_msdk", "unstepped"): (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "dji_msdk", "camera_unstepped"): (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "unknown_adapter", "default"): (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "unknown_adapter", "camera"): (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "unknown_adapter", "stepped"): (None, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "unknown_adapter", "unstepped"): (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("tello", "unknown_adapter", "camera_unstepped"):
        (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "tello_sdk_udp", "default"):
        (NEEDS_SOURCE, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("generic_multirotor", "tello_sdk_udp", "camera"):
        (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("generic_multirotor", "tello_sdk_udp", "stepped"):
        (NEEDS_SOURCE, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("generic_multirotor", "tello_sdk_udp", "unstepped"):
        (NEEDS_SOURCE, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("generic_multirotor", "tello_sdk_udp", "camera_unstepped"):
        (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, NO_THERMAL, None, None),
    ("generic_multirotor", "mavlink", "default"): (NEEDS_SOURCE, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "mavlink", "camera"): (None, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "mavlink", "stepped"): (NEEDS_SOURCE, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "mavlink", "unstepped"): (NEEDS_SOURCE, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "mavlink", "camera_unstepped"): (None, None, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "dji_msdk", "default"):
        (NEEDS_SOURCE, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "dji_msdk", "camera"): (None, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "dji_msdk", "stepped"):
        (NEEDS_SOURCE, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "dji_msdk", "unstepped"):
        (NEEDS_SOURCE, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "dji_msdk", "camera_unstepped"):
        (None, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "unknown_adapter", "default"):
        (NEEDS_SOURCE, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "unknown_adapter", "camera"):
        (None, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "unknown_adapter", "stepped"):
        (NEEDS_SOURCE, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "unknown_adapter", "unstepped"):
        (NEEDS_SOURCE, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
    ("generic_multirotor", "unknown_adapter", "camera_unstepped"):
        (None, NO_AVOIDANCE, None, NO_DOWN, NO_THERMAL, None, None),
}

# One gimbal carrying an RGB and a thermal camera, on each frame path and adapter.
# Keys: (frame path, adapter id, gimbal context).
GIMBAL_EXPECTED = {
    ("drone_video", "tello_sdk_udp", "default"): (PITCH, TELLO_NO_AVOIDANCE, PITCH, PITCH, PITCH, None, None),
    ("drone_video", "tello_sdk_udp", "level"):
        (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, None, None, None),
    ("drone_video", "tello_sdk_udp", "level_camera"):
        (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, None, None, None),
    ("drone_video", "tello_sdk_udp", "level_unstepped"):
        (NOT_STEPPED, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, None, None, None),
    ("drone_video", "tello_sdk_udp", "nadir"):
        (NO_LEVEL_RGB, TELLO_NO_AVOIDANCE, NO_LEVEL, None, None, None, None),
    ("drone_video", "mavlink", "default"): (PITCH, None, PITCH, PITCH, PITCH, None, None),
    ("drone_video", "mavlink", "level"): (NOT_STEPPED, None, None, NO_DOWN, None, None, None),
    ("drone_video", "mavlink", "level_camera"): (NOT_STEPPED, None, None, NO_DOWN, None, None, None),
    ("drone_video", "mavlink", "level_unstepped"): (NOT_STEPPED, None, None, NO_DOWN, None, None, None),
    ("drone_video", "mavlink", "nadir"): (NO_LEVEL_RGB, None, NO_LEVEL, None, None, None, None),
    ("drone_video", "dji_msdk", "default"): (PITCH, NO_AVOIDANCE, PITCH, PITCH, PITCH, None, None),
    ("drone_video", "dji_msdk", "level"): (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, None, None, None),
    ("drone_video", "dji_msdk", "level_camera"): (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, None, None, None),
    ("drone_video", "dji_msdk", "level_unstepped"): (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, None, None, None),
    ("drone_video", "dji_msdk", "nadir"): (NO_LEVEL_RGB, NO_AVOIDANCE, NO_LEVEL, None, None, None, None),
    ("drone_video", "unknown_adapter", "default"): (PITCH, NO_AVOIDANCE, PITCH, PITCH, PITCH, None, None),
    ("drone_video", "unknown_adapter", "level"): (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, None, None, None),
    ("drone_video", "unknown_adapter", "level_camera"):
        (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, None, None, None),
    ("drone_video", "unknown_adapter", "level_unstepped"):
        (NOT_STEPPED, NO_AVOIDANCE, None, NO_DOWN, None, None, None),
    ("drone_video", "unknown_adapter", "nadir"): (NO_LEVEL_RGB, NO_AVOIDANCE, NO_LEVEL, None, None, None, None),
    ("capture_source", "tello_sdk_udp", "default"): (PITCH, TELLO_NO_AVOIDANCE, PITCH, PITCH, PITCH, None, None),
    ("capture_source", "tello_sdk_udp", "level"):
        (NEEDS_SOURCE, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, None, None, None),
    ("capture_source", "tello_sdk_udp", "level_camera"):
        (None, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, None, None, None),
    ("capture_source", "tello_sdk_udp", "level_unstepped"):
        (NEEDS_SOURCE, TELLO_NO_AVOIDANCE, None, TELLO_ONE_CAMERA, None, None, None),
    ("capture_source", "tello_sdk_udp", "nadir"):
        (NO_LEVEL_RGB, TELLO_NO_AVOIDANCE, NO_LEVEL, None, None, None, None),
    ("capture_source", "mavlink", "default"): (PITCH, None, PITCH, PITCH, PITCH, None, None),
    ("capture_source", "mavlink", "level"): (NEEDS_SOURCE, None, None, NO_DOWN, None, None, None),
    ("capture_source", "mavlink", "level_camera"): (None, None, None, NO_DOWN, None, None, None),
    ("capture_source", "mavlink", "level_unstepped"): (NEEDS_SOURCE, None, None, NO_DOWN, None, None, None),
    ("capture_source", "mavlink", "nadir"): (NO_LEVEL_RGB, None, NO_LEVEL, None, None, None, None),
    ("capture_source", "dji_msdk", "default"): (PITCH, NO_AVOIDANCE, PITCH, PITCH, PITCH, None, None),
    ("capture_source", "dji_msdk", "level"): (NEEDS_SOURCE, NO_AVOIDANCE, None, NO_DOWN, None, None, None),
    ("capture_source", "dji_msdk", "level_camera"): (None, NO_AVOIDANCE, None, NO_DOWN, None, None, None),
    ("capture_source", "dji_msdk", "level_unstepped"):
        (NEEDS_SOURCE, NO_AVOIDANCE, None, NO_DOWN, None, None, None),
    ("capture_source", "dji_msdk", "nadir"): (NO_LEVEL_RGB, NO_AVOIDANCE, NO_LEVEL, None, None, None, None),
    ("capture_source", "unknown_adapter", "default"): (PITCH, NO_AVOIDANCE, PITCH, PITCH, PITCH, None, None),
    ("capture_source", "unknown_adapter", "level"): (NEEDS_SOURCE, NO_AVOIDANCE, None, NO_DOWN, None, None, None),
    ("capture_source", "unknown_adapter", "level_camera"): (None, NO_AVOIDANCE, None, NO_DOWN, None, None, None),
    ("capture_source", "unknown_adapter", "level_unstepped"):
        (NEEDS_SOURCE, NO_AVOIDANCE, None, NO_DOWN, None, None, None),
    ("capture_source", "unknown_adapter", "nadir"): (NO_LEVEL_RGB, NO_AVOIDANCE, NO_LEVEL, None, None, None, None),
}

def _reasons(model, context):
    return {fid: feature_reason(fid, model, **context) for fid in FEATURE_IDS}


def _swapped(base_id, adapter):
    return dataclasses.replace(BUILTIN_MODELS[base_id], adapter=adapter)


def _gimballed(frame_path, adapter):
    return dataclasses.replace(BUILTIN_MODELS["generic_multirotor"], adapter=adapter, frame_path=frame_path,
                               cameras=(GIMBAL_RGB, GIMBAL_IR))


def test_columns_are_every_feature():
    assert sorted(COLUMNS) == sorted(FEATURE_IDS)
    assert len(COLUMNS) == len(FEATURE_IDS)


def test_tables_cover_every_model_and_context():
    assert set(BUILTIN_EXPECTED) == {(m, c) for m in BUILTIN_MODELS for c in CONTEXTS}
    assert set(ADAPTER_SWAP_EXPECTED) == {
        (b, a, c) for b in ("tello", "generic_multirotor") for a in ADAPTERS for c in CONTEXTS}
    assert set(GIMBAL_EXPECTED) == {
        (f, a, c) for f in ("drone_video", "capture_source") for a in ADAPTERS for c in GIMBAL_CONTEXTS}
    assert "unknown_adapter" not in ADAPTER_SPECS


@pytest.mark.parametrize("key", sorted(BUILTIN_EXPECTED))
def test_builtin_model_reasons_are_unchanged(key):
    model_id, context = key
    assert _reasons(BUILTIN_MODELS[model_id], CONTEXTS[context]) == dict(zip(COLUMNS, BUILTIN_EXPECTED[key]))


@pytest.mark.parametrize("key", sorted(ADAPTER_SWAP_EXPECTED))
def test_custom_model_reasons_are_unchanged_on_every_adapter(key):
    base_id, adapter, context = key
    expected = dict(zip(COLUMNS, ADAPTER_SWAP_EXPECTED[key]))
    assert _reasons(_swapped(base_id, adapter), CONTEXTS[context]) == expected


@pytest.mark.parametrize("key", sorted(GIMBAL_EXPECTED))
def test_gimbal_model_reasons_are_unchanged_on_every_adapter(key):
    frame_path, adapter, context = key
    expected = dict(zip(COLUMNS, GIMBAL_EXPECTED[key]))
    assert _reasons(_gimballed(frame_path, adapter), GIMBAL_CONTEXTS[context]) == expected


def test_builtin_adapter_facts():
    tello, mavlink, dji = ADAPTER_SPECS["tello_sdk_udp"], ADAPTER_SPECS["mavlink"], ADAPTER_SPECS["dji_msdk"]
    assert (tello.stepped_sensing, tello.declares_avoidance, tello.short_name) == (True, False, "Tello")
    assert (mavlink.stepped_sensing, mavlink.declares_avoidance, mavlink.short_name) == (False, True, "")
    assert (dji.stepped_sensing, dji.declares_avoidance, dji.short_name) == (False, False, "")


@pytest.fixture
def custom_adapter(monkeypatch):
    """Install one more adapter spec and return a drone_video model (one fixed forward camera) that uses it."""

    def install(**facts):
        spec = AdapterSpec("custom_adapter", "a custom adapter for this test", **facts)
        specs = MappingProxyType({**ADAPTER_SPECS, spec.id: spec})
        monkeypatch.setattr(catalogue, "ADAPTER_SPECS", specs)
        monkeypatch.setattr(features, "ADAPTER_SPECS", specs)
        return _swapped("tello", spec.id)

    return install


def test_custom_adapter_with_stepped_sensing_makes_forward_sensing_available(custom_adapter):
    model = custom_adapter(stepped_sensing=True)
    assert feature_reason(FORWARD_OBSTACLE_SENSING, model) is None
    gates = resolve_features(model)
    assert gates[FORWARD_OBSTACLE_SENSING]["ok"] is True
    assert gates[FORWARD_OBSTACLE_SENSING]["on"] is True
    # An explicit backend answer still wins over the adapter fact.
    assert feature_reason(FORWARD_OBSTACLE_SENSING, model, stepped_sensing=False) == NOT_STEPPED


def test_custom_adapter_without_stepped_sensing_leaves_forward_sensing_unavailable(custom_adapter):
    model = custom_adapter()
    assert feature_reason(FORWARD_OBSTACLE_SENSING, model) == NOT_STEPPED
    assert feature_reason(FORWARD_OBSTACLE_SENSING, model, stepped_sensing=True) is None


def test_custom_adapter_declaring_avoidance_makes_onboard_avoidance_available(custom_adapter):
    assert feature_reason(ONBOARD_AVOIDANCE, custom_adapter(declares_avoidance=True)) is None
    assert feature_reason(ONBOARD_AVOIDANCE, custom_adapter()) == NO_AVOIDANCE


def test_custom_adapter_short_name_drives_the_worded_reasons(custom_adapter):
    model = custom_adapter(short_name="Skylark")
    assert feature_reason(ONBOARD_AVOIDANCE, model) == "the Skylark has no onboard avoidance"
    assert feature_reason(BIRDSEYE_SEARCH, model) == "the Skylark has one fixed forward camera"
    assert feature_reason(BIRDSEYE_SEARCH, custom_adapter()) == NO_DOWN
