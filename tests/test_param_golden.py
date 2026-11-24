"""The golden file of today's flight parameters, its generator and the tag rule.

The golden file is read by tests only. Every value of an ``origin: today`` row is checked against the
constant it replaces, found by importing the real module, so a base that lacks a prerequisite fix fails
here by name.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden" / "flight_params.json"
SCRIPT = ROOT / "scripts" / "make_param_golden.py"
DESIGN = ROOT / "docs" / "design" / "hardware-profiles"
PROFILES = ("tello", "generic_multirotor")


def _load():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _generator():
    spec = importlib.util.spec_from_file_location("make_param_golden", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GEN = _generator()


def _resolve(ref: str):
    """Read the runtime value a ref names, with the generator's reader (module constant, parameter
    default, or a literal read from source), optionally one tuple item and scaled by a factor."""
    return _GEN.resolve(ref)


def _entries():
    for row in _load()["rows"]:
        for profile in PROFILES:
            entry = row["profiles"][profile]
            if entry is not None:
                yield pytest.param(row, profile, entry, id=f"{row['row']}-{row['id']}-{profile}")


def test_golden_holds_the_162_rows_and_the_schema_keys():
    data = _load()
    assert len(data["rows"]) == 162
    assert len({row["id"] for row in data["rows"]}) == 162
    keys = {"default", "lo", "hi", "safer", "confirm", "edit", "off_value", "safety_switch", "origin"}
    for row in data["rows"]:
        assert set(row["profiles"]) == set(PROFILES)
        for entry in row["profiles"].values():
            if entry is not None:
                assert keys <= set(entry), row["id"]
                assert entry["origin"] in {"today", "new"}
    assert [c["id"] for c in data["constraints"]] == [f"K-{n}" for n in range(1, 19)]


@pytest.mark.parametrize("row,profile,entry", list(_entries()))
def test_today_default_equals_the_imported_constant(row, profile, entry):
    if entry["today_ref"] is None:
        assert entry["origin"] == "new" or entry.get("today_ref_reason")
        return
    assert entry["origin"] == "today"
    assert _resolve(entry["today_ref"]) == entry["default"]


@pytest.mark.parametrize("row", _load()["rows"], ids=lambda r: f"{r['row']}-{r['id']}")
def test_today_accepts_equals_the_imported_range_constants(row):
    for alias in row["env_aliases"].values():
        accepts = alias["today_accepts"]
        if accepts is None:
            assert alias["today_accepts_reason"]
            continue
        for profile, rng in accepts.items():
            assert rng["policy"] in {"refuse", "clamp"}
            for side in ("lo", "hi"):
                ref = rng[f"{side}_ref"]
                assert isinstance(rng[f"{side}_open"], bool)
                if ref is None and rng[side] is not None:
                    # A literal end of today's check (LITERAL_ENDS) has no constant; it carries its reason.
                    assert rng[f"{side}_reason"], (profile, side)
                    continue
                assert (rng[side] is None) == (ref is None) or _resolve(ref) is None
                if ref is not None:
                    assert _resolve(ref) == rng[side]


def _default(pid: str, profile: str):
    row = next(r for r in _load()["rows"] if r["id"] == pid)
    return row["profiles"][profile]["default"]


def test_pre1_values_of_the_merged_fixes_are_pinned():
    assert _default("avoid.sidestep_cm", "tello") == 0
    assert _default("plan.alt_slots_cm", "tello") == [100, 130]
    assert _default("tello.temp_warn_c", "tello") == 92
    assert _default("tello.refusals_per_flight", "tello") == 2


def test_min_start_battery_equals_the_tello_takeoff_battery_default():
    from flightrisk.drone.coordinator import MIN_START_BATTERY

    assert MIN_START_BATTERY == _default("battery.min_takeoff_pct", "tello")


def test_the_generator_refuses_to_overwrite_an_existing_file(tmp_path):
    target = tmp_path / "existing.json"
    target.write_text("keep me", encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), str(target)], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 2
    assert str(target) in result.stderr
    assert target.read_text(encoding="utf-8") == "keep me"


def test_regenerating_gives_the_committed_bytes(tmp_path):
    target = tmp_path / "again.json"
    result = subprocess.run([sys.executable, str(SCRIPT), str(target)], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == GOLDEN.read_bytes()


def test_nothing_but_the_generator_writes_the_golden_file():
    write_calls = re.compile(r"open\([^)]*flight_params[^)]*[\"'][wax+]|flight_params[^\n]*\.(write_text|write_bytes)\(")
    offenders = []
    for folder in ("tests", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            if path.name in {"test_param_golden.py", "make_param_golden.py"}:
                continue
            if write_calls.search(path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(str(path))
    assert offenders == []


def _section_27_items():
    text = (DESIGN / "design.md").read_text(encoding="utf-8")
    body = text.split("## 27. Behaviour changes, by PR", 1)[1].split("\n## ", 1)[0]
    items = {}
    for bullet in re.split(r"\n(?=- \*\*)", body):
        head = re.match(r"- \*\*([^:]+):\*\*", bullet)
        if not head:
            continue
        prs = [p.strip() for p in re.sub(r"\([^)]*\)", "", head.group(1)).replace(" to ", ",").split(",")]
        for number in re.findall(r"(?:^|\s)(\d+)\.\s", bullet[head.end():]):
            for pr in prs:
                items.setdefault(pr, set()).add(int(number))
    return items


def test_every_from_pr_tag_names_a_section_27_item_of_that_pr():
    items = _section_27_items()
    tags = re.findall(r"from ([A-E]\d), section 27 item (\d+)", (DESIGN / "parameters.md").read_text(encoding="utf-8"))
    assert len(tags) >= 15
    for pr, item in tags:
        assert int(item) in items.get(pr, set()), f"tag 'from {pr}, section 27 item {item}' is not listed under {pr}"


def test_golden_pending_entries_match_section_27():
    items = _section_27_items()
    for row in _load()["rows"]:
        for entry in row["profiles"].values():
            for pending in (entry or {}).get("pending", []):
                assert pending["item"] in items.get(pending["pr"], set()), row["id"]


def test_table_defaults_equal_the_constants_they_replace():
    """Every origin today row with a today_ref has a table default equal to the driver constant."""
    from flightrisk.params.table import TABLE

    by_id = {row.id: row for row in TABLE}
    checked = 0
    for golden in _load()["rows"]:
        for profile in PROFILES:
            entry = golden["profiles"][profile]
            if entry is None or entry["origin"] != "today" or not entry.get("today_ref"):
                continue
            value = _resolve(entry["today_ref"])
            default = by_id[golden["id"]].defaults[profile]
            default = list(default) if isinstance(default, tuple) else default
            assert default == value, (golden["id"], profile)
            checked += 1
    assert checked > 60


def test_every_doc_only_today_value_is_listed_once_with_a_reason():
    """The today values not checked against code are exactly the generator's DOC_ONLY_TODAY list."""
    data = _load()
    found = {
        (row["row"], profile)
        for row in data["rows"]
        for profile, entry in row["profiles"].items()
        if entry and entry["origin"] == "today" and entry["today_ref"] is None
    }
    listed = {(d["row"], d["profile"]) for d in data["doc_only_today"]}
    assert found == listed == set(_GEN.DOC_ONLY_TODAY)
    assert all(d["reason"] for d in data["doc_only_today"])
    assert len(found) == 26


def test_rows_111_to_162_are_today_values_only_with_a_ref_or_a_reason():
    for row in _load()["rows"]:
        if row["row"] < 111:
            continue
        for profile, entry in row["profiles"].items():
            if entry and entry["origin"] == "today":
                assert entry["today_ref"] or (row["row"], profile) in _GEN.DOC_ONLY_TODAY, row["id"]


def test_ground_loss_margin_is_implicit_today():
    """Row 143's 2 s is today's ground loss timeout minus the edge's link-loss watchdog."""
    timeout = _resolve("ast:flightrisk/dashboard/app.py:FLEET_LOSS_TIMEOUT_S")
    detect = _resolve("flightrisk.drone.safety:DEFAULT_LINK_LOSS_LAND_S")
    margin = {r["id"]: r for r in _load()["rows"]}["link.ground_loss_margin_s"]["profiles"]
    assert timeout - detect == margin["tello"]["default"] == margin["generic_multirotor"]["default"]


def test_a_cli_scale_taken_from_a_ref_is_the_edge_wiring():
    """A flag scaled by a today_ref's factor feeds that attribute directly in edge_device.py."""
    source = (ROOT / "flightrisk" / "edge_device.py").read_text(encoding="utf-8")
    checked = 0
    for row in _load()["rows"]:
        for name, alias in row["env_aliases"].items():
            if name.startswith("--") and alias["scale_from"] != "name":
                dest = name[2:].replace("-", "_")
                assert f'add_argument("{name}"' in source, name
                assert f"{dest}=args.{dest}" in source, name
                checked += 1
    assert checked >= 2
    speed = {r["id"]: r for r in _load()["rows"]}["move.speed_cm_s"]["env_aliases"]["--speed-mps"]
    assert speed["scale"] == 100
