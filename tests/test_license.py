"""Tests for LICENSE file and licensing documentation."""

import re
from pathlib import Path


def test_license_text_is_gplv3():
    """Verify that LICENSE contains the full GPLv3 text with the correct header."""
    license_path = Path(__file__).parent.parent / "LICENSE"
    assert license_path.exists(), "LICENSE file not found"

    content = license_path.read_text()

    # Verify it starts with the GPLv3 header
    assert "GNU GENERAL PUBLIC LICENSE" in content, "LICENSE missing GPLv3 header"
    assert "Version 3, 29 June 2007" in content, "LICENSE missing GPLv3 version line"

    # Verify it's not empty or truncated (GPLv3 is ~2600 lines)
    lines = content.split('\n')
    assert len(lines) > 600, f"LICENSE file seems truncated ({len(lines)} lines)"


def test_licensing_doc_notes():
    """Verify that docs/licensing.md has the required licensing notes."""
    licensing_path = Path(__file__).parent.parent / "docs" / "licensing.md"
    assert licensing_path.exists(), "docs/licensing.md file not found"

    content = licensing_path.read_text()

    # Verify SPDX identifier for FlightRisk
    assert "GPL-3.0-or-later" in content, "docs/licensing.md missing SPDX GPL-3.0-or-later identifier"

    # Verify Ultralytics AGPL-3.0 note
    assert "AGPL-3.0" in content, "docs/licensing.md missing AGPL-3.0 note for Ultralytics"
    assert "Ultralytics" in content or "YOLO" in content, "docs/licensing.md missing Ultralytics reference"

    # Verify GPLv3 §13 reference for combining licenses
    assert "Section 13" in content or "§ 13" in content or "section 13" in content.lower(), \
        "docs/licensing.md missing GPLv3 Section 13 reference"

    # Verify AGPL terms note
    assert "AGPL terms still apply" in content or "AGPL terms apply" in content, \
        "docs/licensing.md missing note that AGPL terms apply to YOLO component"

    # Verify detector swap item
    assert "swap" in content.lower() or "alternative" in content.lower() or "Apache" in content, \
        "docs/licensing.md missing detector swap/alternative item"

    # Verify InsightFace non-commercial research note
    assert "InsightFace" in content, "docs/licensing.md missing InsightFace reference"
    assert "non-commercial research" in content or "research only" in content, \
        "docs/licensing.md missing InsightFace non-commercial research note"
