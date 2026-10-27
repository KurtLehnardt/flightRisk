"""Plain-text assertions for the one-page plan and the roadmap/budget appendix."""

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
PLAN = DOCS / "one-page-plan.md"
ROADMAP = DOCS / "roadmap-budget.md"


def _section(text: str, heading: str) -> str:
    """Return the body of the level-2 section whose heading starts with `heading`."""
    match = re.search(
        rf"^## {re.escape(heading)}[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M
    )
    assert match, f"missing section: {heading}"
    return match.group(1)


def _table_rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        if line.startswith("|") and not re.match(r"^\|[-| ]+\|$", line):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows[1:]  # drop the header row


def _total(section: str) -> int:
    """Sum the Total USD column of item rows and check it matches the Total row."""
    items = 0
    stated = None
    for cells in _table_rows(section):
        if cells[0] == "Total":
            stated = int(cells[3])
        else:
            qty, unit, total = int(cells[1]), int(cells[2]), int(cells[3])
            assert qty * unit == total, f"row arithmetic wrong: {cells[0]}"
            items += total
    assert stated is not None, "missing Total row"
    assert stated == items
    return items


def test_5k_table_sums_to_at_most_5k():
    assert _total(_section(ROADMAP.read_text(), "$5K milestone")) <= 5000


def test_10k_table_sums_between_9k_and_10k():
    section = _section(ROADMAP.read_text(), "$10K plan")
    assert 9000 <= _total(section) <= 10000
    for needle in ("SIYI A8 mini", "Jetson Orin Nano Super"):
        rows = [r for r in _table_rows(section) if needle in r[0]]
        assert rows and rows[0][4].startswith("http"), needle


def test_milestones_cite_code_paths():
    section = _section(ROADMAP.read_text(), "Milestones")
    for n in range(1, 5):
        line = next((ln for ln in section.splitlines() if ln.startswith(f"- M{n}")), "")
        assert re.search(r"`flightrisk/[\w/]+\.py`|`flightrisk/\w+/`", line), f"M{n}"


def test_licensing_rows_present():
    text = _section(ROADMAP.read_text(), "Licensing and swap table")
    for needle in ("GPL-3.0-or-later", "AGPL-3.0", "Detector swap"):
        assert needle in text
    assert "section 13" in text or "§13" in text


def test_regulatory_note_short_with_urls():
    section = _section(ROADMAP.read_text(), "Regulatory note")
    lines = [ln for ln in section.splitlines() if ln.strip()]
    assert len(lines) <= 8
    assert len(re.findall(r"https?://", section)) >= 3


def test_kubernetes_paragraph():
    section = _section(ROADMAP.read_text(), "Kubernetes")
    assert "no Kubernetes" in section
    assert "docker compose" in section
    assert "50+" in section


def test_one_page_plan_length_cap():
    text = PLAN.read_text()
    assert len(text.split()) <= 500
    assert len([ln for ln in text.splitlines() if ln.strip()]) <= 70


def test_one_page_plan_sections():
    text = PLAN.read_text()
    for heading in (
        "Next milestones",
        "People and resources needed",
        "Rough timeline",
        "$5K milestone and $10K plan",
        "After the hackathon (next 90 days)",
        "Honest limits",
    ):
        assert f"## {heading}" in text, heading
    assert "canopy" in text
    assert "roadmap-budget.md" in text


def test_5k_milestone_stated_in_both_docs():
    for path in (PLAN, ROADMAP):
        text = path.read_text()
        for needle in ("$5,000", "altitude-benchmark.md", "Jetson"):
            assert needle in text, f"{path.name}: {needle}"
    assert "$10,000" in PLAN.read_text()
    assert "$10,000" in ROADMAP.read_text()


def test_honest_limits_in_both_docs():
    for path in (PLAN, ROADMAP):
        text = path.read_text()
        assert "canopy" in text and "open terrain" in text, path.name
