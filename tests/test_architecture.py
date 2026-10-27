"""Architecture rules for the drone abstraction.

The dashboard must depend only on the drone registry and the role protocols,
never on a concrete backend module. Concrete backends are named in exactly
one bootstrap module, ``flightrisk/drone/backends.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "flightrisk"
CONCRETE_BACKENDS = ("flightrisk.drone.tello", "flightrisk.drone.mavlink")
#: Modules allowed to import concrete backends directly.
BOOTSTRAP_MODULES = {ROOT / "drone" / "backends.py"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", None)) in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def _violations(path: Path) -> list[str]:
    return sorted(
        mod for mod in _imported_modules(path)
        if any(mod == b or mod.startswith(b + ".") for b in CONCRETE_BACKENDS)
    )


def test_dashboard_does_not_import_concrete_drone_backends():
    offenders = {
        str(path.relative_to(ROOT)): _violations(path)
        for path in sorted((ROOT / "dashboard").rglob("*.py"))
        if path not in BOOTSTRAP_MODULES and _violations(path)
    }
    assert offenders == {}


def test_checker_detects_a_direct_import(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("from flightrisk.drone import tello\nimport flightrisk.drone.mavlink as m\n")
    assert _violations(bad) == ["flightrisk.drone.mavlink", "flightrisk.drone.tello"]
