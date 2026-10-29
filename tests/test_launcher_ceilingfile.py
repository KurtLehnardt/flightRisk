"""The launcher writes the dashboard's ceiling file on Start (flightrisk/launcher/ceilingfile.py)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from flightrisk.dashboard.ceiling import CeilingStore
from flightrisk.launcher.ceilingfile import (
    CeilingWriteError,
    ceiling_file_path,
    plan_ceiling_write,
    write_ceiling,
)
from flightrisk.launcher.settings import (
    DroneSettings,
    LauncherSettings,
    builtin_presets,
    errors,
    validate,
)

REHEARSAL = builtin_presets()["Rehearsal with fake drones"]


def _settings(**changes) -> LauncherSettings:
    return replace(REHEARSAL, **changes)


def test_the_path_follows_the_environment_like_the_dashboard(tmp_path):
    assert ceiling_file_path({"FLIGHTRISK_CEILING_FILE": str(tmp_path / "c.json")}) == tmp_path / "c.json"


def test_write_creates_the_file_in_the_dashboards_format(tmp_path):
    path = tmp_path / "ceiling.json"
    settings = _settings(ceiling_cm=240, margin_cm=50)
    write_ceiling(settings, path)
    assert json.loads(path.read_text()) == {
        "version": 1, "ceiling_cm": 240, "margin_cm": 50, "slots_cm": {"drone-1": 100, "drone-2": 130},
    }
    # The dashboard's own loader reads exactly what the launcher wrote (the two stay in sync by construction).
    loaded, error = CeilingStore(path).load()
    assert error is None and (loaded.ceiling_cm, loaded.margin_cm, dict(loaded.slots_cm)) == (240, 50, {"drone-1": 100, "drone-2": 130})
    assert list(tmp_path.iterdir()) == [path], "no temporary file is left behind"


def test_write_keeps_other_keys_in_the_file(tmp_path):
    path = tmp_path / "ceiling.json"
    path.write_text(json.dumps({"version": 1, "ceiling_cm": 300, "margin_cm": 60, "slots_cm": {"old": 90}, "room": "garage"}))
    write_ceiling(_settings(), path)
    data = json.loads(path.read_text())
    assert data["room"] == "garage"
    assert data["ceiling_cm"] == 200 and data["slots_cm"] == {"drone-1": 100, "drone-2": 130}


def test_an_unreadable_file_is_replaced_and_the_plan_says_so(tmp_path):
    path = tmp_path / "ceiling.json"
    path.write_text("{not json")
    plan = plan_ceiling_write(_settings(), path)
    assert plan.will_write and plan.differs and "unusable" in plan.note
    write_ceiling(_settings(), path)
    assert CeilingStore(path).load()[1] is None


def test_the_plan_describes_what_differs_from_the_file(tmp_path):
    path = tmp_path / "ceiling.json"
    assert "no ceiling file yet" in plan_ceiling_write(_settings(), path).changes[0]
    write_ceiling(_settings(), path)
    same = plan_ceiling_write(_settings(), path)
    assert not same.differs and same.previous == same.values
    changed = plan_ceiling_write(_settings(ceiling_cm=250, drones=(
        replace(REHEARSAL.drones[0], alt_slot_cm=110), REHEARSAL.drones[1])), path)
    assert changed.differs
    assert changed.changes == ("ceiling 200 cm becomes 250 cm", "drone-1 slot 100 cm becomes 110 cm")
    assert str(path) in changed.note and "ceiling 200 cm becomes 250 cm" in changed.note
    assert changed.to_json()["path"] == str(path) and changed.to_json()["values"]["ceiling_cm"] == 250


@pytest.mark.parametrize("changes, why", [
    ({"ceiling_cm": 149}, "ceiling"),
    ({"ceiling_cm": 1001}, "ceiling"),
    ({"margin_cm": 29}, "margin"),
])
def test_ceiling_and_margin_limits_are_the_dashboards(tmp_path, changes, why):
    path = tmp_path / "ceiling.json"
    plan = plan_ceiling_write(_settings(**changes), path)
    assert not plan.will_write and plan.error and why in plan.error
    with pytest.raises(CeilingWriteError):
        write_ceiling(_settings(**changes), path)
    assert not path.exists()


def test_a_slot_above_ceiling_minus_margin_is_refused_and_nothing_is_written(tmp_path):
    path = tmp_path / "ceiling.json"
    too_high = _settings(drones=(replace(REHEARSAL.drones[0], alt_slot_cm=160), REHEARSAL.drones[1]))
    # The window's own validation refuses it first, and the file writer refuses it as well.
    assert any(i.field == "drones.0.alt_slot_cm" for i in errors(validate(too_high, adapters=[])))
    with pytest.raises(CeilingWriteError, match="drone-1"):
        write_ceiling(too_high, path)
    assert not path.exists()
    exactly = _settings(drones=(replace(REHEARSAL.drones[0], alt_slot_cm=140), REHEARSAL.drones[1]))
    write_ceiling(exactly, path)
    assert json.loads(path.read_text())["slots_cm"]["drone-1"] == 140


def test_edges_only_never_writes(tmp_path):
    path = tmp_path / "ceiling.json"
    edges = _settings(role="edges", ground_host="127.0.0.1")
    plan = plan_ceiling_write(edges, path)
    assert not plan.will_write and not plan.differs
    with pytest.raises(CeilingWriteError):
        write_ceiling(edges, path)
    assert not path.exists()


def test_nothing_is_written_while_launcher_processes_run(tmp_path):
    path = tmp_path / "ceiling.json"
    write_ceiling(_settings(), path)
    before = path.read_text()
    with pytest.raises(CeilingWriteError, match="running"):
        write_ceiling(_settings(ceiling_cm=300), path, processes_running=True)
    assert path.read_text() == before


def test_a_failed_write_leaves_the_old_file_and_no_temporary(tmp_path, monkeypatch):
    path = tmp_path / "ceiling.json"
    write_ceiling(_settings(), path)
    before = path.read_text()
    monkeypatch.setattr("flightrisk.launcher.ceilingfile.os.replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        write_ceiling(_settings(ceiling_cm=300), path)
    assert path.read_text() == before and list(tmp_path.iterdir()) == [path]


def test_one_slot_per_drone_in_the_window(tmp_path):
    path = tmp_path / "ceiling.json"
    three = _settings(drones=tuple(DroneSettings(drone_id=f"d{k}", source="fake", alt_slot_cm=80 + k) for k in range(3)))
    write_ceiling(three, path)
    assert json.loads(path.read_text())["slots_cm"] == {"d0": 80, "d1": 81, "d2": 82}
