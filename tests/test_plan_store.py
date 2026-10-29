"""Saved map plans: round trip, validation and the plans directory (docs/map-and-plan-editing.md, section 7)."""

from __future__ import annotations

import json
import os

import pytest

from flightrisk.dashboard import plan_store
from flightrisk.dashboard.plan_store import PlanStoreError

SPEC = {
    "pattern": "sector",
    "area": {"x_cm": -50, "y_cm": 20, "w_cm": 400, "h_cm": 300},
    "strip_cm": 80,
    "paths": {"drone-1": [[-50, 20], [0, 300], [120, 250]]},
}
ANCHOR = {"lat": 40.6894, "lon": -111.731, "heading_deg": 37.5}
VIEW = {"mode": "latlon", "center": [40.69, -111.73], "zoom": 18, "base_layer": "usgs-imagery"}


def test_save_and_load_round_trip(tmp_path):
    saved = plan_store.save_plan("Ridge search 1", SPEC, ANCHOR, VIEW, base=tmp_path)
    loaded = plan_store.load_plan("Ridge search 1", base=tmp_path)
    assert loaded["plan"] == saved["plan"]
    assert loaded["plan"]["paths"] == SPEC["paths"]
    assert loaded["anchor"] == ANCHOR
    assert loaded["view"] == {"mode": "latlon", "center": [40.69, -111.73], "zoom": 18.0, "base_layer": "usgs-imagery"}
    assert loaded["saved_at"] == saved["saved_at"]
    assert plan_store.list_plans(base=tmp_path) == [{"name": "Ridge search 1", "saved_at": saved["saved_at"]}]


def test_the_default_directory_is_under_home(tmp_path, monkeypatch):
    monkeypatch.delenv(plan_store.PLANS_DIR_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert plan_store.plans_dir() == tmp_path / "home" / ".flightrisk" / "plans"
    plan_store.save_plan("p1", SPEC)
    assert (tmp_path / "home" / ".flightrisk" / "plans" / "p1.json").is_file()
    assert plan_store.load_plan("p1")["anchor"] is None


def test_the_environment_overrides_the_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(plan_store.PLANS_DIR_ENV, str(tmp_path / "elsewhere"))
    plan_store.save_plan("p2", SPEC)
    assert (tmp_path / "elsewhere" / "p2.json").is_file()


@pytest.mark.parametrize("name", [
    "", " lead", ".hidden", "../escape", "a/b", "a\\b", "x" * 65, "trailing.", None, 5, "semi;colon", "nul\x00",
])
def test_unsafe_names_are_refused(tmp_path, name):
    with pytest.raises(PlanStoreError):
        plan_store.save_plan(name, SPEC, base=tmp_path)
    with pytest.raises(PlanStoreError):
        plan_store.load_plan(name, base=tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("spec, anchor, view, message", [
    ({"pattern": "zigzag", "area": SPEC["area"]}, None, None, "plan is not valid"),
    (SPEC, {"lat": 100, "lon": 0}, None, "anchor is not valid"),
    (SPEC, None, {"mode": "3d"}, "mode"),
    (SPEC, None, {"center": [0]}, "center"),
    (SPEC, None, {"zoom": 99}, "zoom"),
    (SPEC, None, {"base_layer": "../x"}, "base layer"),
    (SPEC, None, {"token": "abc"}, "unknown map view fields"),
])
def test_invalid_content_is_refused(tmp_path, spec, anchor, view, message):
    with pytest.raises(PlanStoreError, match=message):
        plan_store.save_plan("p", spec, anchor, view, base=tmp_path)


def test_a_tampered_file_is_validated_on_load(tmp_path):
    plan_store.save_plan("p", SPEC, base=tmp_path)
    doc = json.loads((tmp_path / "p.json").read_text())
    doc["plan"]["pattern"] = "zigzag"
    (tmp_path / "p.json").write_text(json.dumps(doc))
    with pytest.raises(PlanStoreError, match="not valid"):
        plan_store.load_plan("p", base=tmp_path)
    assert plan_store.list_plans(base=tmp_path) == []


def test_an_oversized_or_unreadable_file_is_refused(tmp_path):
    (tmp_path / "big.json").write_text("x" * (plan_store.MAX_PLAN_BYTES + 1))
    (tmp_path / "bad.json").write_text("{not json")
    for name in ("big", "bad", "missing"):
        with pytest.raises(PlanStoreError):
            plan_store.load_plan(name, base=tmp_path)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_a_symlinked_plan_is_not_followed(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text('{"plan": {}}')
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "link.json").symlink_to(secret)
    with pytest.raises(PlanStoreError, match="no saved plan"):
        plan_store.load_plan("link", base=plans)


def test_a_saved_plan_holds_no_extra_fields(tmp_path):
    plan_store.save_plan("p", dict(SPEC), ANCHOR, VIEW, base=tmp_path)
    doc = json.loads((tmp_path / "p.json").read_text())
    assert set(doc) == {"name", "version", "plan", "anchor", "view", "saved_at"}


def test_a_deeply_nested_file_is_refused_and_does_not_break_the_list(tmp_path):
    plan_store.save_plan("good", SPEC, base=tmp_path)
    (tmp_path / "deep.json").write_text("[" * 100_000 + "]" * 100_000)
    with pytest.raises(PlanStoreError, match="cannot be read"):
        plan_store.load_plan("deep", base=tmp_path)
    assert [p["name"] for p in plan_store.list_plans(base=tmp_path)] == ["good"]


@pytest.mark.parametrize("version", [True, "1", 2])
def test_a_wrong_version_is_refused(tmp_path, version):
    plan_store.save_plan("p", SPEC, base=tmp_path)
    doc = json.loads((tmp_path / "p.json").read_text())
    doc["version"] = version
    (tmp_path / "p.json").write_text(json.dumps(doc))
    with pytest.raises(PlanStoreError, match="version"):
        plan_store.load_plan("p", base=tmp_path)
    with pytest.raises(PlanStoreError, match="version"):
        plan_store.save_plan("q", dict(SPEC, version=version), base=tmp_path)
