"""Change control for the safety values (plan step S11): docs, CODEOWNERS, change log, import grep."""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path

from flightrisk.params.docs import (
    is_fact_row,
    is_read_only_row,
    render_flight_parameters_markdown,
)
from flightrisk.params.table import TABLE

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "docs" / "design" / "hardware-profiles" / "safety-value-changes.md"
OWNED = (
    "flightrisk/params/table.py",
    "tests/golden/flight_params.json",
    "tests/param_literal_allowlist.py",
)
CHECKER_PATHS = (
    "tests/test_param_literals.py",
    "tests/test_param_change_control.py",
    "tests/param_adopters.py",
    "tests/tello_coupling_baseline.py",
    "tests/test_tello_coupling.py",
    "tests/fixtures/edge_start_baseline.json",
    "tests/test_edge_start_baseline.py",
    "tests/tools/make_edge_baseline.py",
    "docs/design/hardware-profiles/safety-value-changes.md",
)
# The A1 baseline entry. Editing it needs an edit here too, which CODEOWNERS makes owned (F6, F7).
MIN_ENTRIES = 1
BASELINE = {
    "previous": "c69c2a1f30b24c0e4d06525c75ffdc744d20e6df4bf39744ec46aaacf35f650c",
    "table": "f949df64f14cee5c1c025d683cabea1d56a493941db05c214c33f7deac628378",
    "golden": "7403c28493386027b0c383326d30d2bb2645fa54c154e289276f9246289d2504",
    "allowlist": "49966ba5ce72cc90ec17e4890d171c9a67078dec3c69ea91c391f5569b39e750",
}


def test_generated_doc_equals_committed():
    assert (ROOT / "docs" / "flight-parameters.md").read_text(encoding="utf-8") == render_flight_parameters_markdown()


def test_doc_never_presents_a_fact_as_a_setting():
    text = render_flight_parameters_markdown()
    head, _, facts = text.partition("## Test-adapter facts")
    for row in TABLE:
        if is_fact_row(row):
            assert f"`{row.id}`" not in head
            for name in (row.env, *(a.name for a in row.env_aliases), *row.cli):
                if name:
                    assert name not in text, name
    for word in ("tun", "calibrat", "threshold"):
        assert word not in facts.lower(), word


def test_doc_lists_read_only_rows_only_under_read_only_headings():
    sections = render_flight_parameters_markdown().split("\n## ")
    settings = "\n".join(s for s in sections if s.startswith("Settings: "))
    read_only = "\n".join(s for s in sections if s.startswith("Read-only values: "))
    for row in TABLE:
        if is_read_only_row(row) or is_fact_row(row):
            assert f"`{row.id}`" not in settings, row.id
        if is_read_only_row(row):
            assert f"`{row.id}`" in read_only, row.id


def test_codeowners_names_the_owned_paths():
    owners = {}
    for line in (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            path, *who = line.split()
            owners[path.lstrip("/")] = who
    for path in OWNED + CHECKER_PATHS:
        assert owners.get(path), path
    for path in ("flightrisk/dashboard/roles.py", "flightrisk/dashboard/devices.py",
                 "flightrisk/fleet_protocol/constants.py", "flightrisk/drone/rth/"):
        assert owners.get(path), path


def _entries(text: str) -> list[tuple[str, str]]:
    """``(text before the entry's heading, entry text)`` for each ``## `` entry of the log."""
    starts = [m.start() + 1 for m in re.finditer(r"\n## ", text)]
    return [(text[:s], text[s:e]) for s, e in zip(starts, starts[1:] + [len(text)])]


def chain_errors(text: str) -> list[str]:
    """Every entry must record the SHA-256 of the log text before its heading."""
    errors = []
    for prefix, entry in _entries(text):
        match = re.search(r"SHA-256 previous: ([0-9a-f]{64})", entry)
        if not match or match.group(1) != hashlib.sha256(prefix.encode("utf-8")).hexdigest():
            errors.append(entry.splitlines()[0])
    return errors


def test_latest_log_entry_holds_current_hashes():
    entries = _entries(LOG.read_text(encoding="utf-8"))
    assert entries
    latest = entries[-1][1]
    for label, path in zip(("table", "golden", "allowlist"), OWNED):
        match = re.search(rf"SHA-256 {label}: ([0-9a-f]{{64}})", latest)
        assert match, label
        actual = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        assert match.group(1) == actual, f"{path} changed without a new entry in safety-value-changes.md"


def test_log_hash_chain_is_unbroken():
    assert chain_errors(LOG.read_text(encoding="utf-8")) == []


def test_log_baseline_entry_is_pinned():
    entries = _entries(LOG.read_text(encoding="utf-8"))
    assert len(entries) >= MIN_ENTRIES
    first = entries[0][1]
    assert first.startswith("## A1 baseline\n")
    for label, sha in BASELINE.items():
        assert f"SHA-256 {label}: {sha}" in first, label


def test_editing_an_old_log_entry_breaks_the_chain():
    text = LOG.read_text(encoding="utf-8")
    appended = text + "\n## Planted\n\n- SHA-256 previous: " + hashlib.sha256(text.encode() + b"\n").hexdigest() + "\n"
    assert chain_errors(appended) == []
    edited = appended.replace("- Value: none changed.", "- Value: geofence changed.", 1)
    errors = chain_errors(edited)
    assert edited != appended and errors and errors[-1] == "## Planted"
    deleted = appended.replace(_entries(appended)[0][1], "", 1)
    assert chain_errors(deleted)


#: Modules outside params and hardware may import these, because they hold no registry or catalogue
#: value: the drone id pattern only (A2a moved it out of the launcher so the inventory can share it).
LEAF_IMPORTS_ALLOWED = {"flightrisk.hardware.ids", "flightrisk.hardware.ids.DRONE_ID"}


def _type_checking_nodes(tree):
    """Every node inside an ``if TYPE_CHECKING:`` block; such imports never run."""
    inside = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and (
            (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING")
            or (
                isinstance(node.test, ast.Attribute)
                and node.test.attr == "TYPE_CHECKING"
                and isinstance(node.test.value, ast.Name)
                and node.test.value.id == "typing"
            )
        ):
            for child in node.body:
                inside.update(ast.walk(child))
    return inside


def test_the_allowed_leaf_imports_hold_no_registry_or_catalogue_value():
    """ids.py imports only re, and the hardware package's __init__ imports nothing, so importing the
    id rule never loads the catalogue."""
    tree = ast.parse((ROOT / "flightrisk" / "hardware" / "ids.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "re" and not node.level, ast.dump(node)
        elif isinstance(node, ast.Import):
            assert [a.name for a in node.names] == ["re"], ast.dump(node)
    package = ast.parse((ROOT / "flightrisk" / "hardware" / "__init__.py").read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(package) if isinstance(n, (ast.Import, ast.ImportFrom))]


#: CMP-1 scope (plan step S3, risk R3): the only modules outside params and hardware that may import them at
#: run time. Exactly the files PR A3 owns in tasks.md, plus the plan's new ``edge_resolution.py``. A4 and A5
#: widen this set in their own PRs, when they adopt; every other module stays forbidden.
A3_ADOPTED = frozenset({
    "flightrisk/edge_device.py",
    "flightrisk/edge.py",
    "flightrisk/edge_profiles.py",
    "flightrisk/edge_uplink.py",
    "flightrisk/fleet_authority.py",
    "flightrisk/fleet_authority_types.py",
    "flightrisk/drone/safety.py",
    "flightrisk/drone/obstacle_sensor.py",
    "flightrisk/drone/obstacle.py",
    "flightrisk/drone/flight_guards.py",
    "flightrisk/drone/temp_preflight.py",
    "flightrisk/drone/tello.py",
    "flightrisk/drone/tello_bound.py",
    "flightrisk/drone/fake.py",
    "flightrisk/drone/lane_flight.py",
    "flightrisk/drone/lane_executor.py",
    "flightrisk/drone/lane_monitor.py",
    "flightrisk/drone/lane_avoid.py",
    "flightrisk/drone/mavlink_fleet.py",
    "flightrisk/drone/autopilot.py",
    "flightrisk/drone/mavsdk_link.py",
    "flightrisk/drone/autopilot_fence.py",
    "flightrisk/edge_resolution.py",
})
#: New files A3 creates; they need not exist before the PR that adds them.
A3_NEW_FILES = frozenset({"flightrisk/drone/autopilot_fence.py", "flightrisk/edge_resolution.py"})
TASKS = ROOT / "docs" / "design" / "hardware-profiles" / "tasks.md"


def _a3_ownership_list() -> set[str]:
    """The ``flightrisk/*.py`` paths of the tasks.md ownership rows whose first owner is A3, braces expanded."""
    paths = set()
    for line in TASKS.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.startswith("|") or len(cells) < 2 or not re.match(r"A3\b", cells[1]):
            continue
        for item in re.findall(r"`([^`]+)`", cells[0]):
            brace = re.fullmatch(r"(.*)\{([^}]*)\}(.*)", item)
            names = [brace.group(1) + n + brace.group(3) for n in brace.group(2).split(",")] if brace else [item]
            paths.update(n for n in names if n.startswith("flightrisk/") and n.endswith(".py"))
    return paths


def test_adopted_set_equals_the_a3_ownership_list():
    """The allowance cannot quietly grow: it is exactly tasks.md's A3 rows plus the plan's edge_resolution.py."""
    listed = _a3_ownership_list()
    assert len([p for p in listed if p.startswith("flightrisk/drone/") and p not in A3_NEW_FILES]) == 15
    assert A3_ADOPTED == listed | {"flightrisk/edge_resolution.py"}
    for rel in A3_ADOPTED - A3_NEW_FILES:
        assert (ROOT / rel).is_file(), rel


#: Ledger sections that a phase other than A3 owns, mapped to that phase's first-owner name in tasks.md.
PHASE_ROW_NAMES = {"X-legacy": "XL", "A4": "A4"}
#: Per-phase scope: the only modules a phase section of ``tests/param_adopters.py`` may name. Each set is exactly
#: the ``flightrisk/*.py`` paths of that phase's tasks.md ownership rows, without the params and hardware modules,
#: which never need an allowance. A set grows only with a reviewed tasks.md edit, and none may overlap A3_ADOPTED.
PHASE_ADOPTED: dict[str, frozenset[str]] = {
    "X-legacy": frozenset({
        "flightrisk/drone/fleet.py",
        "flightrisk/drone/mavlink.py",
    }),
    "A4": frozenset({
        "flightrisk/drone/coordinator.py",
        "flightrisk/config.py",
        "flightrisk/observability.py",
        "flightrisk/drone/fleet_plan_check.py",
        "flightrisk/drone/fleet_plan_model.py",
        "flightrisk/drone/fleet_patterns.py",
        "flightrisk/drone/fleet_scale.py",
        "flightrisk/drone/plan_edit.py",
        "flightrisk/drone/plan_paths.py",
        "flightrisk/drone/coverage.py",
        "flightrisk/drone/coverage_sim.py",
        "flightrisk/drone/geolocate.py",
        "flightrisk/drone/search.py",
        "flightrisk/ground_params.py",
        "flightrisk/dashboard/unit_descriptions.py",
        "flightrisk/dashboard/unit_cards.py",
        "flightrisk/recorder.py",
        "flightrisk/fleet_ground_link.py",
    }),
}
#: New files a phase creates; they need not exist before the PR that adds them.
PHASE_NEW_FILES = frozenset({
    "flightrisk/ground_params.py",
    "flightrisk/dashboard/unit_descriptions.py",
    "flightrisk/dashboard/unit_cards.py",
})


def _phase_ownership_list(row_name: str) -> set[str]:
    """The ``flightrisk/*.py`` paths of the tasks.md ownership rows whose first owner is ``row_name``, braces
    expanded, without the params and hardware modules."""
    paths = set()
    for line in TASKS.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.startswith("|") or len(cells) < 2 or not re.match(rf"{re.escape(row_name)}\b", cells[1]):
            continue
        for item in re.findall(r"`([^`]+)`", cells[0]):
            brace = re.fullmatch(r"(.*)\{([^}]*)\}(.*)", item)
            names = [brace.group(1) + n + brace.group(3) for n in brace.group(2).split(",")] if brace else [item]
            paths.update(
                n for n in names
                if n.startswith("flightrisk/") and n.endswith(".py")
                and not n.startswith(("flightrisk/params/", "flightrisk/hardware/"))
            )
    return paths


def phase_cap_problems(
    ledger: dict[str, frozenset[str]], caps: dict[str, frozenset[str]], a3_adopted: frozenset[str]
) -> list[str]:
    """One message per path that a phase section of ``ledger`` names outside that phase's cap in ``caps``, and
    one per path that sits in a phase cap and in ``a3_adopted``."""
    problems = []
    for section, cap in caps.items():
        for rel in sorted(ledger.get(section, frozenset()) - cap):
            problems.append(f"{section}: {rel} is outside the phase allowance")
        for rel in sorted(cap & a3_adopted):
            problems.append(f"{section}: {rel} sits in the phase allowance and in A3_ADOPTED")
    return problems


def _non_phase_ledger_paths() -> frozenset[str]:
    """Every path that ``tests/param_adopters.py`` names in a section that ``PHASE_ADOPTED`` does not cap."""
    from tests.param_adopters import PARAM_ADOPTERS

    return frozenset().union(*(paths for section, paths in PARAM_ADOPTERS.items() if section not in PHASE_ADOPTED))


def test_phase_allowances_equal_their_tasks_md_rows():
    """A phase allowance cannot quietly grow: it is exactly its phase's tasks.md rows, and it never overlaps A3's."""
    from tests.param_adopters import PARAM_ADOPTERS

    for section, cap in PHASE_ADOPTED.items():
        assert section in PARAM_ADOPTERS, section
        assert cap == _phase_ownership_list(PHASE_ROW_NAMES[section]), section
        assert not cap & A3_ADOPTED, sorted(cap & A3_ADOPTED)
        for rel in cap - PHASE_NEW_FILES:
            assert (ROOT / rel).is_file(), rel
    assert len(PHASE_ADOPTED["A4"]) == 18
    assert len(PHASE_ADOPTED["X-legacy"]) == 2


def test_phase_sections_stay_inside_their_allowance():
    """Each phase section of the ledger names only modules that its own allowance holds."""
    from tests.param_adopters import PARAM_ADOPTERS

    assert phase_cap_problems(PARAM_ADOPTERS, PHASE_ADOPTED, A3_ADOPTED) == []


def test_phase_cap_rule_catches_each_violation():
    """The cap rule on synthetic ledgers: a phase section outside its allowance fails, and so does a cap that
    overlaps A3_ADOPTED. Sections the caps do not name are left to the A3_ADOPTED check."""
    clean = {
        "X-core": frozenset(),
        "X-legacy": frozenset({"flightrisk/drone/fleet.py"}),
        "A3": frozenset({"flightrisk/edge_device.py"}),
        "A4": frozenset({"flightrisk/config.py", "flightrisk/ground_params.py"}),
    }
    assert phase_cap_problems(clean, PHASE_ADOPTED, A3_ADOPTED) == []
    dashboard = {**clean, "A4": clean["A4"] | {"flightrisk/dashboard/app.py"}}
    assert phase_cap_problems(dashboard, PHASE_ADOPTED, A3_ADOPTED) == [
        "A4: flightrisk/dashboard/app.py is outside the phase allowance",
    ]
    edge = {**clean, "A4": clean["A4"] | {"flightrisk/edge_device.py"}}
    assert phase_cap_problems(edge, PHASE_ADOPTED, A3_ADOPTED) == [
        "A4: flightrisk/edge_device.py is outside the phase allowance",
    ]
    overlapping = {**PHASE_ADOPTED, "X-legacy": PHASE_ADOPTED["X-legacy"] | {"flightrisk/drone/tello.py"}}
    assert phase_cap_problems(clean, overlapping, A3_ADOPTED) == [
        "X-legacy: flightrisk/drone/tello.py sits in the phase allowance and in A3_ADOPTED",
    ]


def _runtime_param_imports(tree):
    """The names a parsed module imports from params or hardware at run time.

    An import inside ``if TYPE_CHECKING:`` never runs, and the drone id pattern holds no registry or
    catalogue value, so neither counts.
    """
    skipped = _type_checking_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if node in skipped:
            continue
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names = [base] + [f"{base}.{a.name}" for a in node.names]
            if node.level:
                names = [("." * node.level) + n for n in names]
        for n in names:
            bare = n.lstrip(".")
            if bare in LEAF_IMPORTS_ALLOWED:
                continue
            if re.match(r"(flightrisk\.)?(params|hardware)(\.|$)", bare) and (
                not n.startswith(".") or bare.split(".")[0] in ("params", "hardware")
            ):
                found.append(n)
    return found


def runtime_param_importers(root: Path, allowed: frozenset[str]) -> list[str]:
    """Every run-time import of params or hardware under ``root/flightrisk`` outside params, hardware and ``allowed``.

    The exemptions of ``_runtime_param_imports`` apply.
    """
    offenders = []
    for path in sorted((root / "flightrisk").rglob("*.py")):
        rel = path.relative_to(root).parts
        if rel[1] in ("params", "hardware") or "/".join(rel) in allowed:
            continue
        for n in _runtime_param_imports(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{'/'.join(rel)}: {n}")
    return offenders


def _ledger_paths() -> frozenset[str]:
    """Every path that ``tests/param_adopters.py`` names, in any section."""
    from tests.param_adopters import PARAM_ADOPTERS

    return frozenset().union(*PARAM_ADOPTERS.values())


def test_no_runtime_module_imports_params_or_hardware():
    """CMP-1 and AM-3: fails on any runtime import of params or hardware from a path that
    ``tests/param_adopters.py`` names in no section.

    Modules inside params and hardware are skipped, and the exemptions of ``_runtime_param_imports`` apply.
    """
    offenders = runtime_param_importers(ROOT, _ledger_paths())
    assert not offenders, offenders


def test_only_adopted_modules_import_params_or_hardware():
    """CMP-1 scope: outside params, hardware and tests, only the modules A3 adopts import them at run time.

    The guard above lets a module import params or hardware only when the ledger names it, so keeping every
    ledger path inside ``A3_ADOPTED`` keeps every runtime importer inside it too. A path in any section of the
    ledger that ``A3_ADOPTED`` does not hold fails here, until the phase that adopts it widens the allowance.
    The A4 and X-legacy sections are capped by ``PHASE_ADOPTED`` instead, which pins each to its tasks.md rows.
    """
    outside = sorted(_non_phase_ledger_paths() - A3_ADOPTED)
    assert not outside, f"named in tests/param_adopters.py but outside A3_ADOPTED: {outside}"


def test_every_param_adopter_exists_and_imports_params_or_hardware():
    """No dead ledger entries: each named path exists and still imports one of the two packages."""
    from tests.param_adopters import PARAM_ADOPTERS

    assert list(PARAM_ADOPTERS) == [
        "X-core", "X-legacy", "A3", "A4", "A5", "B1", "B2", "B3", "B4", "D4",
    ]
    seen = set()
    for section, paths in PARAM_ADOPTERS.items():
        assert isinstance(paths, frozenset), section
        for rel in paths:
            assert rel.startswith("flightrisk/") and rel.endswith(".py"), rel
            assert not rel.startswith(("flightrisk/params/", "flightrisk/hardware/")), rel
            assert rel not in seen, f"{rel} sits in two sections"
            seen.add(rel)
            path = ROOT / rel
            assert path.is_file(), f"{section}: {rel} does not exist"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            assert _runtime_param_imports(tree), f"{section}: {rel} no longer imports params or hardware"


def test_adopter_import_rule_flags_runtime_and_ignores_type_checking(tmp_path):
    """The shared rule behind the guard and the dead-entry test, exercised while the ledger is empty."""
    runtime = tmp_path / "runtime.py"
    runtime.write_text("from flightrisk.params import TABLE\nimport flightrisk.hardware.profiles\n", encoding="utf-8")
    assert _runtime_param_imports(ast.parse(runtime.read_text(encoding="utf-8"))) == [
        "flightrisk.params",
        "flightrisk.params.TABLE",
        "flightrisk.hardware.profiles",
    ]
    typing_only = tmp_path / "typing_only.py"
    typing_only.write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from flightrisk.params import TABLE\n"
        "from flightrisk.hardware.ids import DRONE_ID\n",
        encoding="utf-8",
    )
    assert _runtime_param_imports(ast.parse(typing_only.read_text(encoding="utf-8"))) == []


# --- Coupling (plan step S3): the MAVLink build path never loads the Tello driver. ---

TELLO = "flightrisk.drone.tello"


def _module_file(root: Path, module: str) -> Path | None:
    base = root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _string_import(node) -> str | None:
    """The module a call ``importlib.import_module("x")``, ``import_module("x")`` or ``__import__("x")`` loads.

    Only a string literal first argument counts; a relative name is resolved against a literal ``package``.
    """
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
    first = node.args[0]
    if name not in ("import_module", "__import__") or not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return None
    target = first.value
    if name == "import_module" and target.startswith("."):
        package = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "package"), None)
        if not (isinstance(package, ast.Constant) and isinstance(package.value, str)):
            return None
        level = len(target) - len(target.lstrip("."))
        base = package.value.split(".")
        base = base[: len(base) - (level - 1)]
        target = ".".join(base + ([target.lstrip(".")] if target.lstrip(".") else []))
    return target


def _imports_in(nodes, module: str, is_package: bool, root: Path, skipped=frozenset()) -> set[str]:
    """The ``flightrisk`` modules the import statements and string imports among ``nodes`` load.

    Relative imports are resolved. A call of ``importlib.import_module`` or ``__import__`` with a string literal
    counts as an import of that module (``_string_import``).
    """
    found = set()
    for node in nodes:
        if node in skipped:
            continue
        targets = []
        string_target = _string_import(node)
        if string_target:
            targets = [string_target]
        elif isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = module.split(".") if is_package else module.split(".")[:-1]
                package = package[: len(package) - (node.level - 1)]
                base = ".".join(package + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            targets = [base] + [f"{base}.{a.name}" for a in node.names]
        for target in targets:
            if target.split(".")[0] == "flightrisk" and _module_file(root, target):
                found.add(target)
    return found


def _module_level_nodes(tree):
    """Every node that runs when the module is imported: not inside a function, a lambda or ``if TYPE_CHECKING:``."""
    lazy = set(_type_checking_nodes(tree))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for child in ast.iter_child_nodes(node):
                if not isinstance(child, (ast.arguments, ast.expr)) or isinstance(node, ast.Lambda):
                    lazy.update(ast.walk(child))
    return [n for n in ast.walk(tree) if n not in lazy]


def import_closure(root: Path, start: set[str]) -> dict[str, str]:
    """Every module loaded by importing ``start``, mapped to the module that first imported it.

    Edges are module-level imports only (parent packages included): an import inside a function runs only
    when that function is called, so a Tello-only helper's lazy import does not count.
    """
    parent = {m: "<_build_mavlink_drone>" for m in start}
    queue = sorted(start)
    while queue:
        module = queue.pop()
        path = _module_file(root, module)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        loads = _imports_in(_module_level_nodes(tree), module, path.name == "__init__.py", root)
        pieces = module.split(".")
        loads.update(".".join(pieces[:i]) for i in range(1, len(pieces)))
        for dep in sorted(loads - parent.keys()):
            parent[dep] = module
            queue.append(dep)
    return parent


def mavlink_build_path_modules(root: Path) -> dict[str, str]:
    """The modules ``edge_device._build_mavlink_drone`` reaches: edge_device's module-level imports plus the
    imports inside that function, followed transitively."""
    path = root / "flightrisk" / "edge_device.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_build_mavlink_drone")
    start = {"flightrisk.edge_device"} | _imports_in(ast.walk(build), "flightrisk.edge_device", False, root)
    return import_closure(root, start)


def _chain(parent: dict[str, str], module: str) -> str:
    chain = [module]
    while chain[-1] in parent:
        chain.append(parent[chain[-1]])
    return " <- ".join(chain)


def test_mavlink_build_path_never_imports_tello():
    """SEAM-6: no module that ``edge_device._build_mavlink_drone`` reaches imports ``flightrisk.drone.tello``."""
    reached = mavlink_build_path_modules(ROOT)
    assert "flightrisk.drone.mavlink_fleet" in reached and "flightrisk.drone.mavsdk_link" in reached
    assert TELLO not in reached, _chain(reached, TELLO)


def test_coupling_walk_sees_a_module_level_tello_import_but_not_a_lazy_one(tmp_path):
    pkg = tmp_path / "flightrisk"
    (pkg / "drone").mkdir(parents=True)
    for name in ("__init__.py", "drone/__init__.py", "drone/tello.py", "drone/helper.py"):
        (pkg / name).write_text("")
    (pkg / "edge_device.py").write_text(
        "def _build_mavlink_drone(args, log):\n    from flightrisk.drone import mavlink_fleet\n\n"
        "def _build_tello(args):\n    from flightrisk.drone.tello import TelloController\n"
    )
    (pkg / "drone" / "mavlink_fleet.py").write_text(
        "from . import helper\n\ndef later():\n    from flightrisk.drone import tello\n"
    )
    assert TELLO not in mavlink_build_path_modules(tmp_path)
    (pkg / "drone" / "helper.py").write_text("from .tello import TelloController\n")
    reached = mavlink_build_path_modules(tmp_path)
    assert _chain(reached, TELLO).startswith(
        "flightrisk.drone.tello <- flightrisk.drone.helper <- flightrisk.drone.mavlink_fleet"
    )


def test_coupling_walk_counts_a_module_level_string_import(tmp_path):
    pkg = tmp_path / "flightrisk"
    (pkg / "drone").mkdir(parents=True)
    for name in ("__init__.py", "drone/__init__.py", "drone/tello.py", "drone/helper.py"):
        (pkg / name).write_text("")
    (pkg / "edge_device.py").write_text("def _build_mavlink_drone(args, log):\n    from flightrisk.drone import helper\n")
    (pkg / "drone" / "helper.py").write_text(
        "import importlib\n\ndef later():\n    importlib.import_module('flightrisk.drone.tello')\n"
    )
    assert TELLO not in mavlink_build_path_modules(tmp_path)
    for line in (
        "import importlib\n_T = importlib.import_module('flightrisk.drone.tello')\n",
        "from importlib import import_module\n_T = import_module('.tello', 'flightrisk.drone')\n",
        "_T = __import__('flightrisk.drone.tello')\n",
        "class Holder:\n    T = __import__('flightrisk.drone.tello')\n",
    ):
        (pkg / "drone" / "helper.py").write_text(line)
        reached = mavlink_build_path_modules(tmp_path)
        assert _chain(reached, TELLO).startswith("flightrisk.drone.tello <- flightrisk.drone.helper"), line


def test_mavlink_build_path_leaves_tello_unloaded_in_a_fresh_interpreter():
    """SEAM-6, run for real: importing the MAVLink build path's modules never loads ``flightrisk.drone.tello``."""
    code = (
        "import sys\n"
        "import flightrisk.edge_device, flightrisk.drone.mavlink_fleet, flightrisk.drone.mavsdk_link\n"
        "print('flightrisk.drone.tello' in sys.modules)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "False", result.stdout[-2000:]
