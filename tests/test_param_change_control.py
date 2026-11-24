"""Change control for the safety values (plan step S11): docs, CODEOWNERS, change log, import grep."""

from __future__ import annotations

import ast
import hashlib
import re
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


def _runtime_param_imports(tree):
    """The names a parsed module imports from params or hardware at run time, in source order.

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


def test_no_runtime_module_imports_params_or_hardware():
    """CMP-1 and AM-3: fails on any runtime import of params or hardware from a path that
    ``tests/param_adopters.py`` names in no section.

    Modules inside params and hardware are skipped, and the exemptions of ``_runtime_param_imports`` apply.
    """
    from tests.param_adopters import PARAM_ADOPTERS

    named = set().union(*PARAM_ADOPTERS.values())
    offenders = []
    for path in (ROOT / "flightrisk").rglob("*.py"):
        rel = path.relative_to(ROOT).parts
        if rel[1] in ("params", "hardware") or path.relative_to(ROOT).as_posix() in named:
            continue
        for n in _runtime_param_imports(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{'/'.join(rel)}: {n}")
    assert not offenders, offenders


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
