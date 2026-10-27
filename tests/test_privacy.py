"""Privacy minimum: captures are off by default and nothing leaks into the real captures dir."""

import subprocess
import sys
from pathlib import Path

import numpy as np

from flightrisk.dashboard import alerts
from flightrisk.dashboard.state import app_state

REAL_CAPTURES = Path(alerts.__file__).resolve().parent.parent.parent / "captures"


def _snapshot_args():
    return (
        np.zeros((100, 100, 3), dtype=np.uint8),
        np.zeros((50, 50, 3), dtype=np.uint8),
        0.9,
        {"match": True},
    )


def _listing(path):
    return sorted(p.name for p in path.iterdir()) if path.exists() else None


def test_maybe_save_noop_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr("flightrisk.dashboard.alerts.CAPTURES_DIR", tmp_path)
    monkeypatch.delenv("FLIGHTRISK_SAVE_CAPTURES", raising=False)
    monkeypatch.setattr(app_state, "logger", None)
    assert alerts.maybe_save_match_snapshot(*_snapshot_args()) is None
    assert list(tmp_path.iterdir()) == []


def test_maybe_save_noop_when_explicitly_false(tmp_path, monkeypatch):
    monkeypatch.setattr("flightrisk.dashboard.alerts.CAPTURES_DIR", tmp_path)
    assert alerts.maybe_save_match_snapshot(*_snapshot_args(), save_captures=False) is None
    assert list(tmp_path.iterdir()) == []


def test_maybe_save_writes_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr("flightrisk.dashboard.alerts.CAPTURES_DIR", tmp_path)
    monkeypatch.setattr(app_state, "logger", None)
    assert alerts.maybe_save_match_snapshot(*_snapshot_args(), save_captures=True) is None
    names = [p.name for p in tmp_path.iterdir()]
    assert any("frame" in n for n in names)
    assert any("meta" in n for n in names)


def test_env_flag_enables_captures(tmp_path, monkeypatch):
    monkeypatch.setattr("flightrisk.dashboard.alerts.CAPTURES_DIR", tmp_path)
    monkeypatch.setattr(app_state, "logger", None)
    monkeypatch.setattr(alerts, "_captures_enabled", lambda: True)
    alerts.maybe_save_match_snapshot(*_snapshot_args())
    assert list(tmp_path.iterdir()) != []


def test_alerts_import_has_no_mkdir_side_effect():
    code = (
        "import pathlib\n"
        "def boom(*a, **k):\n"
        "    raise AssertionError('mkdir called at import time')\n"
        "pathlib.Path.mkdir = boom\n"
        "import flightrisk.dashboard.alerts\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REAL_CAPTURES.parent),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def test_conftest_redirects_captures_dir_away_from_real_dir(tmp_path):
    assert Path(alerts.CAPTURES_DIR).resolve() != REAL_CAPTURES.resolve()
    assert tmp_path in Path(alerts.CAPTURES_DIR).parents


def test_snapshot_does_not_touch_real_captures_dir(monkeypatch):
    before = _listing(REAL_CAPTURES)
    monkeypatch.setattr(app_state, "logger", None)
    alerts._save_match_snapshot(*_snapshot_args())
    alerts.maybe_save_match_snapshot(*_snapshot_args(), save_captures=True)
    assert _listing(REAL_CAPTURES) == before
    assert Path(alerts.CAPTURES_DIR).exists()
