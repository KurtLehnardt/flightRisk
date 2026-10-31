"""Tests for the privacy policy and privacy design documents (R-006, R-083)."""

import re
from pathlib import Path

DOCS = Path(__file__).parent.parent / "docs"
POLICY = DOCS / "privacy-policy.md"
DESIGN = DOCS / "privacy-design.md"


def test_policy_has_no_unqualified_no_data_leaves_claim():
    text = POLICY.read_text()
    # The old blanket claim contradicted the cloud LLM disclosure.
    assert "No biometric data ever leaves your phone" not in text
    for line in text.splitlines():
        if re.search(r"\bnever leaves?\b|\bentirely on[- ]device\b", line, re.IGNORECASE):
            qualified = re.search(
                r"embedding|template|feature vector|default|disabled|unless|except",
                line,
                re.IGNORECASE,
            )
            assert qualified, f"unqualified claim: {line}"
    # The cloud reasoning disclosure must still be present as its own section
    # (the older "Cloud LLM" wording was replaced) and say images are sent.
    assert re.search(r"^## 5\. Cloud Reasoning", text, re.MULTILINE)
    assert "are sent to Anthropic (Claude)" in text
    # Android section 5B: the second way images leave the phone must be disclosed.
    assert re.search(r"^## 5B\. Android", text, re.MULTILINE)
    assert "camera view" in text
    assert "reference photo" in text
    assert "can show a face" in text


def test_privacy_design_doc_sections():
    text = DESIGN.read_text()
    headings = {m.group(1).strip() for m in re.finditer(r"^## (.+)$", text, re.MULTILINE)}
    for section in ("Appearance-only", "Faces at altitude", "Captures", "Test fixtures", "Retention"):
        assert section in headings, f"missing section {section}"
    # R-083: faces line.
    assert "not resolvable" in text
