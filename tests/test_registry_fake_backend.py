"""The backend the fake drone simulates is named, not inherited from the default."""

from __future__ import annotations

import ast
from pathlib import Path

from flightrisk.drone import registry

_REGISTRY_SOURCE = Path(registry.__file__)


def test_fake_drone_backend_is_tello():
    assert registry.FAKE_DRONE_BACKEND == "tello"


def test_fake_drone_backend_is_registered():
    assert registry.is_registered(registry.FAKE_DRONE_BACKEND)


def test_fake_drone_backend_is_a_string_literal():
    tree = ast.parse(_REGISTRY_SOURCE.read_text(encoding="utf-8"))
    values = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "FAKE_DRONE_BACKEND" for t in node.targets)
    ]
    assert len(values) == 1
    assert isinstance(values[0], ast.Constant)
    assert values[0].value == "tello"
