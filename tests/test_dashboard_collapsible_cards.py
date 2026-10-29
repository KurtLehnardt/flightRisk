"""Every sidebar section is collapsible through one shared helper, and safety-critical controls never hide."""

import re
from html.parser import HTMLParser
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard"
INDEX = (DASH / "templates" / "index.html").read_text()
CEILING = (DASH / "templates" / "ceiling_settings.html").read_text()
# The ceiling card lives in its own file that index.html includes; test the combined markup.
PAGE = INDEX.replace('{% include "ceiling_settings.html" %}', CEILING)
JS = (DASH / "static" / "ui" / "ui.js").read_text()
CSS = (DASH / "static" / "ui" / "ui.css").read_text()
HELPER = JS[JS.index("// ========== COLLAPSIBLE SECTIONS"):JS.index("// ========== SOUNDS")]

# The six sidebar sections, in page order.
CARDS = ["fleet", "mission", "ceiling", "video-detection", "session", "advanced"]


class _Cards(HTMLParser):
    VOID = frozenset({"input", "br", "img", "meta", "link", "hr"})

    def __init__(self):
        super().__init__()
        self.stack = []  # (tag, id, in_body_of_card)
        self.in_body = {}  # element id -> card ids whose collapsible body contains it
        self.toggles = {}
        self.bodies = {}
        self.cards = {}
        self.tags = {}
        self.attrs = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.tags[a["id"]] = tag
            self.attrs[a["id"]] = a
        if tag in self.VOID:
            if a.get("id"):
                self.in_body[a["id"]] = [c for t, c, b in self.stack if b]
            return
        classes = (a.get("class") or "").split()
        card = a.get("data-card") if "panel-section" in classes else None
        if card:
            self.cards[card] = a
        if "card-toggle" in classes:
            owner = next((c for t, c, _ in reversed(self.stack) if c), None)
            self.toggles[owner] = a
        if "card-body" in classes:
            owner = next((c for t, c, _ in reversed(self.stack) if c), None)
            self.bodies[owner] = a
        if a.get("id"):
            self.in_body[a["id"]] = [c for t, c, b in self.stack if b]
        is_body = "card-body" in classes
        owner = card or (
            next((c for t, c, _ in reversed(self.stack) if c), None)
            if is_body
            else None
        )
        self.stack.append((tag, owner if (card or is_body) else None, is_body))

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()


def _parse():
    p = _Cards()
    p.feed(PAGE)
    return p


def test_every_card_is_registered():
    assert set(_parse().cards) == set(CARDS)


def test_each_card_has_an_accessible_toggle_and_matching_body():
    p = _parse()
    for card in CARDS:
        t = p.toggles[card]
        assert t.get("type") == "button", card
        assert t.get("aria-expanded") == "true", card  # the markup starts expanded; the helper applies saved state
        controls = t.get("aria-controls")
        assert controls == f"card-{card}-body", card
        assert p.bodies[card].get("id") == controls, card
    # Only Advanced starts collapsed on a first visit.
    assert [c for c in CARDS if p.cards[c].get("data-default") == "collapsed"] == ["advanced"]


def test_shared_helper_is_present_and_guards_storage():
    assert ".panel-section[data-card]" in HELPER
    assert ".card-collapsed > .card-body { display: none; }" in CSS
    assert HELPER.count("window.localStorage") == 2
    assert HELPER.count("try {") == 2 and HELPER.count("catch (e)") == 2
    assert "aria-expanded" in HELPER
    # A prefix of its own, so states saved by the earlier layout (flightrisk.card.*) do not carry over.
    assert "'flightrisk.ui.card.'" in HELPER


def test_helper_hides_with_css_not_dom_removal():
    for forbidden in ("removeChild", ".remove()", "innerHTML"):
        assert forbidden not in HELPER


def test_safety_critical_controls_stay_outside_collapsible_bodies():
    p = _parse()
    for eid in ("land-all", "drone-status", "manual-overlay", "manual-land"):
        assert eid in p.in_body, eid
        assert p.in_body[eid] == [], f"{eid} is inside a collapsible body"


def test_body_tracking_detects_collapsible_content():
    # Guards the safety test above against a parser that never reports anything as inside a body.
    p = _parse()
    assert p.in_body["ceiling-cm"] == ["ceiling"]
    assert p.in_body["match-log"] == ["session"]
    assert p.in_body["fleet-start"] == ["mission"]


def test_live_ids_remain_in_dom():
    ids = set(re.findall(r'id="([^"]+)"', PAGE))
    for eid in (
        "ceiling-cm",
        "ceiling-margin-cm",
        "ceiling-save",
        "ceiling-slots",
        "ceiling-body",
        "profile-drone-list",
        "reasoning-status",
        "search-status",
        "rec-status",
        "match-log",
        "obs-frames",
        "tuner-result",
        "threshold-slider",
    ):
        assert eid in ids, eid


def test_history_and_sessions_disclosures_are_keyboard_accessible():
    p = _parse()
    for button, cid in (("target-history-toggle", "target-history-container"), ("sessions-toggle", "sessions-container")):
        assert p.tags[button] == "button", button
        a = p.attrs[button]
        assert a.get("type") == "button" and a.get("aria-controls") == cid and a.get("aria-expanded") == "false"
        assert "hidden" in p.attrs[cid], cid
    assert "setAttribute('aria-expanded'" in JS[JS.index("function toggleDisclosure"):]


def _rule(selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert match, f"missing CSS rule for {selector}"
    return match.group(1)


def test_chevron_is_large_with_a_comfortable_hit_area():
    chevron = _rule(".card-chevron")
    size = int(re.search(r"font-size:\s*(\d+)px", chevron).group(1))
    assert 20 <= size <= 24
    for prop in ("min-width", "min-height"):
        assert int(re.search(prop + r":\s*(\d+)px", chevron).group(1)) >= 40
    assert int(re.search(r"min-height:\s*(\d+)px", _rule(".card-toggle")).group(1)) >= 40


def test_chevron_keeps_rotation_and_respects_reduced_motion():
    assert "rotate(-90deg)" in _rule(".card-collapsed .card-chevron")
    assert "transition: transform" in _rule(".card-chevron")
    assert re.search(
        r"prefers-reduced-motion:\s*reduce\)\s*\{[^}]*\.card-chevron[^}]*transition:\s*none",
        CSS,
    )
    assert "outline: 2px solid var(--accent)" in _rule(".card-toggle:focus-visible")


def test_sections_are_in_their_page_order():
    positions = [PAGE.index(f'data-card="{card}"') for card in CARDS]
    assert positions == sorted(positions)
    # The ceiling card follows the mission controls and precedes the video options.
    assert PAGE.index('data-card="mission"') < PAGE.index('data-card="ceiling"') < PAGE.index('data-card="video-detection"')
