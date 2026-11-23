"""Tests for the hardware catalogue."""

import dataclasses
from pathlib import Path

import pytest

from flightrisk.drone import coverage
from flightrisk.hardware import catalogue as cat

ROOT = Path(__file__).resolve().parent.parent
MODELS = cat.BUILTIN_MODELS


def _is_sourced(source: str) -> bool:
    return source.startswith("http") or "ASSUMPTION" in source or source.startswith("flightrisk/") or "http" in source


def test_builtins_present():
    assert {"tello", "tello_edu", "generic_multirotor", "dji_msdk_slot"} <= set(MODELS)
    assert MODELS["tello_edu"].profile == "tello"
    assert set(cat.ADAPTER_SPECS) == set(cat.ADAPTERS)
    assert cat.ADAPTER_SPECS["dji_msdk"].slot


@pytest.mark.parametrize("model", list(MODELS.values()), ids=lambda m: m.id)
def test_every_field_is_set(model):
    for f in dataclasses.fields(model):
        if f.name in ("extends", "coverage_name", "coverage_source"):
            continue
        value = getattr(model, f.name)
        assert value is not None, f.name
        if f.name not in ("cameras", "envelope", "safety", "facts"):
            assert value != "", f.name
    assert model.support in cat.SUPPORT_LEVELS and model.scale in cat.SCALES
    assert model.adapter in cat.ADAPTERS and model.frame_path in cat.FRAME_PATHS
    assert model.link in cat.LINKS and model.positioning in cat.POSITIONING
    for cam in model.cameras:
        assert cam.mount_kind in ("fixed", "gimbal")
        assert cam.stream_w_px > 0 and cam.stream_h_px > 0 and cam.hfov_deg > 0 and cam.fps > 0


@pytest.mark.parametrize("model", [m for m in MODELS.values() if m.support != "slot"], ids=lambda m: m.id)
def test_every_fact_is_complete_and_sourced(model):
    assert set(model.facts) == set(cat.CONSERVATIVE_FACTS)
    for group in (model.envelope, model.safety, model.facts):
        for name, fact in group.items():
            assert fact.status in cat.FACT_STATUSES, name
            assert _is_sourced(fact.source), (name, fact.source)
    assert all(_is_sourced(s) for s in model.sources)


def test_fact_status_is_validated():
    with pytest.raises(ValueError):
        cat.CapabilityFact(1, "ASSUMPTION", "made_up")


def test_models_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        MODELS["tello"].scale = "field"
    with pytest.raises(TypeError):
        MODELS["tello"].facts["tracking"] = None


def test_tello_coverage_camera_equals_coverage_tello():
    got = MODELS["tello"].coverage_camera("front", 0.0)
    for f in dataclasses.fields(coverage.CameraModel):
        assert getattr(got, f.name) == getattr(coverage.TELLO, f.name), f.name
    assert MODELS["tello_edu"].coverage_camera("front", 0.0) == got


def test_coverage_camera_takes_the_given_pitch_and_rejects_unknown_camera():
    assert MODELS["generic_multirotor"].coverage_camera("main", -90.0).gimbal_pitch_deg == -90.0
    with pytest.raises(KeyError):
        MODELS["tello"].coverage_camera("rear", 0.0)


def test_missing_fact_is_most_conservative_and_unverified():
    slot = MODELS["dji_msdk_slot"]
    for name, value in cat.CONSERVATIVE_FACTS.items():
        fact = slot.fact(name)
        assert fact.value == value and fact.status == "unverified", name
    assert cat.conservative_fact("tracking").value is False
    with pytest.raises(KeyError):
        cat.conservative_fact("not_a_fact")


def test_stated_fact_is_returned_as_is():
    assert MODELS["tello"].fact("command_loss_delay_s").value == 15


def test_committed_docs_equal_rendered_text():
    path = ROOT / "docs" / "hardware-catalogue.md"
    assert path.read_text(encoding="utf-8") == cat.render_catalogue_markdown()
