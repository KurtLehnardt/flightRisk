"""Change control for the safety values (plan step S11): docs, CODEOWNERS, change log, import grep."""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

from flightrisk.params.docs import is_fact_row, is_read_only_row, render_flight_parameters_markdown
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


def test_no_runtime_module_imports_params_or_hardware():
    """CMP-1: nothing outside params, hardware and tests imports them yet."""
    offenders = []
    for path in (ROOT / "flightrisk").rglob("*.py"):
        rel = path.relative_to(ROOT).parts
        if rel[1] in ("params", "hardware"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
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
                if re.match(r"(flightrisk\.)?(params|hardware)(\.|$)", bare) and (
                    not n.startswith(".") or bare.split(".")[0] in ("params", "hardware")
                ):
                    offenders.append(f"{'/'.join(rel)}: {n}")
    assert not offenders, offenders
