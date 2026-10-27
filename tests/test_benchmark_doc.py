"""Checks on the committed altitude benchmark artifacts (R-072).

These tests read the committed results, plot and doc. They never run the
benchmark or touch the dataset.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "altitude-benchmark.md"
PLOT = ROOT / "docs" / "img" / "altitude_recall.png"
EXPECTED_BINS = ["[0,8)", "[8,16)", "[16,24)", "[24,32)", "[32,48)", "[48,64)", "[64,inf)"]


def _results_rows() -> list[list[str]]:
    rows = []
    for line in DOC.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 6 and cells[0].startswith("[") and cells[0].endswith(")"):
            rows.append(cells)
    return rows


def test_results_table_well_formed():
    rows = _results_rows()
    assert [r[0] for r in rows] == EXPECTED_BINS
    n_total = 0
    for r in rows:
        n_total += int(r[1])
        for value in r[2:]:
            assert 0.0 <= float(value) <= 1.0
    assert n_total > 100


def test_table_matches_committed_json():
    single = json.loads((ROOT / "results" / "altitude_single.json").read_text())
    tiled = json.loads((ROOT / "results" / "altitude_tiled.json").read_text())
    for row, bs, bt in zip(_results_rows(), single["bins"], tiled["bins"]):
        assert int(row[1]) == bs["n_gt"] == bt["n_gt"]
        assert abs(float(row[2]) - bs["recall"]) < 0.0006
        assert abs(float(row[4]) - bt["recall"]) < 0.0006


def test_threshold_stated():
    text = DOC.read_text()
    for mode, key in (("single", "single"), ("tiled", "tiled")):
        match = re.search(rf"min_person_px \({mode}\):\s*(\S+)", text)
        assert match, f"min_person_px ({mode}) missing"
        assert match.group(1).strip(".*") != "pending"
        data = json.loads((ROOT / "results" / f"altitude_{key}.json").read_text())
        assert "min_person_px" in data


def test_split_named():
    text = DOC.read_text()
    assert "HERIDAL" in text
    assert "101" in text and "test" in text.lower()


def test_plot_exists():
    assert PLOT.is_file()
    assert PLOT.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "docs/img/altitude_recall.png" in DOC.read_text() or "img/altitude_recall.png" in DOC.read_text()
