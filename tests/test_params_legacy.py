"""The legacy MavlinkController's move and altitude limits come from one params-owned module."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from flightrisk.params import legacy

ROOT = Path(__file__).resolve().parent.parent
LEGACY_PATH = ROOT / "flightrisk" / "params" / "legacy.py"
MAVLINK_PATH = ROOT / "flightrisk" / "drone" / "mavlink.py"

EXPECTED = {
    "LEGACY_MAVLINK_MIN_MOVE_CM": 10,
    "LEGACY_MAVLINK_MAX_MOVE_CM": 50000,
    "LEGACY_MAVLINK_MAX_ALTITUDE_M": 120,
}


def test_constants_have_the_unchanged_values():
    for name, value in EXPECTED.items():
        assert getattr(legacy, name) == value


def test_controller_capabilities_come_from_the_constants(monkeypatch):
    mav = importlib.import_module("flightrisk.drone.mavlink")
    monkeypatch.setattr(mav, "HAS_MAVSDK", True)
    monkeypatch.setattr(mav, "System", object, raising=False)
    caps = mav.MavlinkController("d1", "udpin://0.0.0.0:14540").capabilities
    assert caps.min_move_cm == 10 == legacy.LEGACY_MAVLINK_MIN_MOVE_CM
    assert caps.max_move_cm == 50000 == legacy.LEGACY_MAVLINK_MAX_MOVE_CM
    assert caps.max_altitude_m == 120 == legacy.LEGACY_MAVLINK_MAX_ALTITUDE_M
    assert caps.flight_scale == "field"


def test_module_holds_only_the_three_constants_and_imports_nothing():
    tree = ast.parse(LEGACY_PATH.read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    body = list(tree.body)
    assert isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
    assigned = {}
    for node in body[1:]:
        assert isinstance(node, ast.Assign) and len(node.targets) == 1
        assert isinstance(node.value, ast.Constant) and type(node.value.value) is int
        assigned[node.targets[0].id] = node.value.value
    assert assigned == EXPECTED


def test_mavlink_imports_the_constants_at_module_level():
    tree = ast.parse(MAVLINK_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "flightrisk.params.legacy"
        for alias in node.names
    }
    assert imported == set(EXPECTED)
