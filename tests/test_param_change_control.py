"""Change control for the safety values (plan step S11): docs, CODEOWNERS, change log, import grep."""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

from flightrisk.params.docs import is_fact_row, render_flight_parameters_markdown
from flightrisk.params.table import TABLE

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "docs" / "design" / "hardware-profiles" / "safety-value-changes.md"
OWNED = (
    "flightrisk/params/table.py",
    "tests/golden/flight_params.json",
    "tests/param_literal_allowlist.py",
)


def test_generated_doc_equals_committed():
    assert (ROOT / "docs" / "flight-parameters.md").read_text(encoding="utf-8") == render_flight_parameters_markdown()


def test_doc_never_presents_a_fact_as_a_setting():
    text = render_flight_parameters_markdown()
    head, _, facts = text.partition("## Test-adapter facts")
    for row in TABLE:
        if is_fact_row(row):
            assert f"`{row.id}`" not in head
            if row.env:
                assert row.env not in text
    assert "tun" not in facts.lower() and "calibrat" not in facts.lower()


def test_codeowners_names_the_three_paths():
    owners = {}
    for line in (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            path, *who = line.split()
            owners[path.lstrip("/")] = who
    for path in OWNED:
        assert owners.get(path), path
    for path in ("flightrisk/dashboard/roles.py", "flightrisk/dashboard/devices.py",
                 "flightrisk/fleet_protocol/constants.py", "flightrisk/drone/rth/"):
        assert owners.get(path), path


def test_latest_log_entry_holds_current_hashes():
    entries = LOG.read_text(encoding="utf-8").split("\n## ")[1:]
    assert entries
    latest = entries[-1]
    for label, path in zip(("table", "golden", "allowlist"), OWNED):
        match = re.search(rf"SHA-256 {label}: ([0-9a-f]{{64}})", latest)
        assert match, label
        actual = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        assert match.group(1) == actual, f"{path} changed without a new entry in safety-value-changes.md"


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
