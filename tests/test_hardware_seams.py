"""Seams added in phase A2: the backend spec's adapter and models, the camera role, frame metadata."""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import pytest

from flightrisk.drone import registry
from flightrisk.drone.controller import CameraSource
from flightrisk.drone.obstacle_sensor import FrameStamp
from flightrisk.hardware import catalogue
from flightrisk.hardware.frames import FrameMeta, with_meta


def _meta(**kw):
    base = dict(camera_id="c", mount="m", sensor="rgb", pitch_deg=None, stamp=None,
                width_px=960, height_px=720, channel_order="rgb")
    base.update(kw)
    return FrameMeta(**base)


def test_registration_without_adapter_or_models_still_works():
    name = "seam_test_backend"
    try:
        spec = registry.register(name, lambda i, h, o: None, default_host=lambda o: "h")
        assert spec.adapter == ""
        assert spec.models == ()
        assert registry.get(name) is spec
    finally:
        registry.unregister(name)


def test_builtin_adapter_and_model_mappings():
    tello = registry.get("tello")
    assert tello.adapter == "tello_sdk_udp"
    assert tello.models == ("tello", "tello_edu")
    mav = registry.get("mavlink")
    assert mav.adapter == "mavlink"
    assert mav.models == ("generic_multirotor",)
    assert registry.get("sim").adapter == ""


def test_declared_adapters_and_models_exist_in_the_catalogue():
    for name in ("tello", "mavlink"):
        spec = registry.get(name)
        assert spec.adapter in catalogue.ADAPTERS
        for model in spec.models:
            assert model in catalogue.BUILTIN_MODELS


def test_frame_meta_is_frozen_and_carries_the_stamp():
    stamp = FrameStamp(captured_at=1.5)
    meta = _meta(stamp=stamp, pitch_deg=-60.0)
    assert meta.stamp is stamp
    assert _meta().stamp is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        meta.pitch_deg = 0.0


def test_with_meta_is_a_signature_only():
    with pytest.raises(NotImplementedError):
        with_meta(object(), None, lambda camera_id: None)


def test_camera_source_is_an_optional_role():
    class Plain:
        pass

    class WithCameras:
        def cameras(self):
            return ()

        def camera_pitch_deg(self, camera_id):
            return None

    assert not isinstance(Plain(), CameraSource)
    assert isinstance(WithCameras(), CameraSource)
