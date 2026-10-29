"""XSS guard for the dashboard template (PR #88 review, finding A).

Match reasoning is LLM output about camera images, so an attacker who can put
text in front of the drone can steer it. That text, and every other server- or
detection-derived value, must never reach ``innerHTML`` unescaped.

Two layers of checks:

* A static check over the dashboard script (``static/ui/ui.js``) and every
  inline ``<script>`` in ``index.html`` and the included
  ``ceiling_settings.html``: in each statement that builds HTML (an
  ``innerHTML``-style sink, a string literal containing a tag, or an
  assignment to a ``*Html`` variable), every dynamic value must be wrapped in
  ``escapeHtml(...)`` / ``Number(...)``, be a ternary condition, or be a
  trusted local (``*Html`` fragments, which are themselves checked where they
  are built, plus a short explicit allowlist).
* The dashboard script goes further: it writes every dynamic value with
  ``textContent`` or DOM nodes, so it has no HTML-building statement at all.
"""

import re
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard"
SCRIPT = DASH / "static" / "ui" / "ui.js"
TEMPLATES = (DASH / "templates" / "index.html", DASH / "templates" / "ceiling_settings.html")
PAYLOAD = "<img src=x onerror=alert(1)>"

# Locals whose value is built only from fixed string literals (or, for
# matchId, guarded by Number.isInteger) and so may be concatenated raw.
SAFE_LOCALS = {"gradeClass", "levelClass", "levelLabel", "qClass", "matchId", "html"}
JS_KEYWORDS = {"return", "const", "let", "var", "true", "false", "null", "undefined"}
SINK_RE = re.compile(r"\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b")
TAG_RE = re.compile(r"<[A-Za-z/!]")


# ---------------------------------------------------------------------------
# A tiny JS scanner: enough to split statements and mask string literals
# ---------------------------------------------------------------------------

def _inline_scripts(html: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", html, flags=re.DOTALL)


def _segments(js: str) -> list[tuple[str, list[str]]]:
    """Split ``js`` into statement-ish segments.

    Returns ``(masked_code, literals)`` pairs: string and template literals are
    replaced by ``\\x00<n>\\x00`` placeholders (their text kept in
    ``literals``), comments are dropped, and segments break on ``;``, ``{``
    and ``}`` outside literals.
    """
    segments: list[tuple[str, list[str]]] = []
    code: list[str] = []
    literals: list[str] = []
    i, n = 0, len(js)

    def flush():
        text = "".join(code).strip()
        if text:
            segments.append((text, list(literals)))
        code.clear()
        literals.clear()

    while i < n:
        ch = js[i]
        if js.startswith("//", i):
            i = js.find("\n", i)
            i = n if i == -1 else i
            continue
        if js.startswith("/*", i):
            end = js.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch in "'\"`":
            j = i + 1
            while j < n and js[j] != ch:
                j += 2 if js[j] == "\\" else 1
            literal = js[i + 1:j]
            if ch == "`":
                literal = "`" + literal  # mark template literals
            code.append(f"\x00{len(literals)}\x00")
            literals.append(literal)
            i = j + 1
            continue
        if ch in ";{}":
            flush()
        else:
            code.append(ch)
        i += 1
    flush()
    return segments


def _strip_calls(code: str, name_re: str) -> str:
    """Remove every ``name(...)`` call (balanced parentheses) from ``code``."""
    pattern = re.compile(rf"\b(?:{name_re})\s*\(")
    while True:
        m = pattern.search(code)
        if not m:
            return code
        depth, j = 1, m.end()
        while j < len(code) and depth:
            depth += {"(": 1, ")": -1}.get(code[j], 0)
            j += 1
        code = code[:m.start()] + " " + code[j:]


def _is_html_statement(code: str, literals: list[str]) -> bool:
    if SINK_RE.search(code):
        return True
    if any(TAG_RE.search(lit) for lit in literals):
        return True
    lhs = re.match(r"\s*(?:const|let|var)?\s*([A-Za-z_$][\w$]*)\s*\+?=(?![=>])", code)
    return bool(lhs and (lhs.group(1).endswith("Html") or lhs.group(1) == "html"))


def unsafe_values(code: str, literals: list[str]) -> list[str]:
    """Dynamic values in one HTML-building statement that are not escaped."""
    if any(lit.startswith("`") and "${" in lit for lit in literals):
        return ["${...} template literal"]
    m = re.search(r"(?<![=!<>+\-*/])\+?=(?![=>])", code)
    if m:
        rhs = code[m.end():]
    elif code.startswith("return"):
        rhs = code[len("return"):]
    else:
        rhs = code
    rhs = _strip_calls(rhs, r"[A-Za-z_$][\w$]*Html|Number")
    rhs = re.sub(r"\x00\d+\x00", " ", rhs)
    # A bare member chain directly before "?" is a ternary condition, not output.
    rhs = re.sub(r"[A-Za-z_$][\w$.]*\s*\?(?![?.])", " ", rhs)
    roots = re.findall(r"(?<![\w$.])[A-Za-z_$][\w$]*", rhs)
    return [
        r for r in roots
        if r not in SAFE_LOCALS and r not in JS_KEYWORDS and not r.endswith("Html")
    ]


def _violations(js: str) -> list[str]:
    found = []
    for code, literals in _segments(js):
        if _is_html_statement(code, literals):
            bad = unsafe_values(code, literals)
            if bad:
                found.append(f"{' '.join(code.split())[:160]!r} -> unescaped {bad}")
    return found


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------

def _page_scripts() -> list[str]:
    scripts = [SCRIPT.read_text(encoding="utf-8")]
    for template in TEMPLATES:
        scripts += _inline_scripts(template.read_text(encoding="utf-8"))
    return scripts


class TestStaticSinkCheck:
    def test_page_has_no_unescaped_html_sinks(self):
        violations = [v for js in _page_scripts() for v in _violations(js)]
        assert not violations, "Unescaped values reach HTML sinks:\n" + "\n".join(violations)

    def test_scanner_reads_the_page_scripts(self):
        # Guards against the scanner silently reading nothing: the dashboard script and the ceiling card's
        # inline script are both split into statements.
        scripts = _page_scripts()
        assert len(scripts) >= 2
        assert all(len(_segments(js)) >= 20 for js in scripts)

    def test_dashboard_script_builds_no_html_at_all(self):
        html_statements = [s for s in _segments(SCRIPT.read_text(encoding="utf-8")) if _is_html_statement(*s)]
        assert html_statements == []

    @pytest.mark.parametrize("snippet", [
        # The original PR #88 sinks.
        "entry.innerHTML = '<div class=\"match-reasoning\">' + data.reasoning + '</div>';",
        "detail.innerHTML = matches.map(m => '<span>' + m.match_type + '</span>' + m.reasoning).join('');",
        "x.innerHTML = 'AI: ' + (m.gemma_confidence ? ' | AI: ' + m.gemma_confidence : '');",
        "el.insertAdjacentHTML('beforeend', '<b>' + data.reasoning + '</b>');",
        "const rowHtml = '<li>' + issue + '</li>';",
        "el.innerHTML = `<p>${data.reasoning}</p>`;",
        "el.innerHTML = '<p>' + String(data.reasoning) + '</p>';",
    ])
    def test_checker_flags_unescaped_sinks(self, snippet):
        assert _violations(snippet), f"checker missed: {snippet}"

    @pytest.mark.parametrize("snippet", [
        "entry.innerHTML = '<div class=\"match-reasoning\">' + escapeHtml(data.reasoning || '') + '</div>';",
        "x.innerHTML = '<i>' + (m.gemma_confidence ? ' | AI: ' + escapeHtml(m.gemma_confidence) : '') + '</i>';",
        "el.innerHTML = '<b>' + Number(c) + '</b>' + snapshotHtml;",
        "el.textContent = data.reasoning;",
        "item.innerHTML = sessionMatchHtml(m);",
    ])
    def test_checker_accepts_escaped_sinks(self, snippet):
        assert not _violations(snippet)


class TestServerSideReasoningPayload:
    def test_reasoning_text_reaches_client_verbatim(self):
        """The parser keeps the LLM's text as-is; safe rendering is the page's job.

        This pins down why the checks above are required: the payload survives
        parsing unchanged, so the page must write it as text, never as HTML.
        """
        from flightrisk.reasoning.agent import FlightRiskAgent

        result = FlightRiskAgent._parse_match_response(
            FlightRiskAgent.__new__(FlightRiskAgent),
            f"MATCH: YES\nCONFIDENCE: high\nREASONING: {PAYLOAD}",
        )
        assert result["reasoning"] == PAYLOAD
        assert result["match"] is True
