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


def _package_of(path: Path) -> str | None:
    """Dotted package containing ``path`` (``flightrisk.dashboard`` for app.py), if under ROOT."""
    try:
        rel = path.resolve().relative_to(ROOT.parent)
    except ValueError:
        return None
    return ".".join(rel.parent.parts)


def _absolute_module(node: ast.ImportFrom, package: str | None) -> str | None:
    """The absolute module an ``ImportFrom`` names, resolving relative imports against ``package``."""
    if not node.level:
        return node.module
    if not package:
        return None
    parts = package.split(".")
    if node.level - 1 > len(parts):
        return None
    base = parts[: len(parts) - (node.level - 1)]
    if node.module:
        base.append(node.module)
    return ".".join(base) or None


def _imported_modules(path: Path, package: str | None = None) -> set[str]:
    if package is None:
        package = _package_of(path)
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_module(node, package)
            if module:
                found.add(module)
                found.update(f"{module}.{alias.name}" for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", None)) in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def _violations(path: Path, package: str | None = None) -> list[str]:
    return sorted(
        mod for mod in _imported_modules(path, package)
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


def test_checker_detects_a_relative_import(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from ..drone import tello\n"
        "from ..drone.mavlink import MavlinkController\n"
        "from . import safety\n"
    )
    assert _violations(bad, package="flightrisk.dashboard") == [
        "flightrisk.drone.mavlink",
        "flightrisk.drone.mavlink.MavlinkController",
        "flightrisk.drone.tello",
    ]


def test_package_of_dashboard_module():
    assert _package_of(ROOT / "dashboard" / "app.py") == "flightrisk.dashboard"
