"""Every side-menu card is collapsible through one shared helper, and safety-critical controls never hide."""

import re
from html.parser import HTMLParser
from pathlib import Path

TEMPLATES = (
    Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard" / "templates"
)
INDEX = (TEMPLATES / "index.html").read_text()
CEILING = (TEMPLATES / "ceiling_settings.html").read_text()
# The ceiling card lives in its own file that index.html includes; test the combined markup.
PAGE = INDEX.replace('{% include "ceiling_settings.html" %}', CEILING)

CARDS = {
    "target",
    "ceiling",
    "drone-fleet",
    "detection-profile",
    "flight-controls",
    "ai-reasoning",
    "autonomous-search",
    "session-recording",
    "edge-video",
    "match-log",
    "observability",
    "manual-threshold",
    "threshold-tuner",
}


class _Cards(HTMLParser):
    VOID = frozenset({"input", "br", "img", "meta", "link", "hr"})

    def __init__(self):
        super().__init__()
        self.stack = []  # (tag, id, in_body_of_card)
        self.in_body = {}  # element id -> card ids whose collapsible body contains it
        self.toggles = {}
        self.bodies = {}
        self.cards = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
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
    assert set(_parse().cards) == CARDS


def test_each_card_has_an_accessible_toggle_and_matching_body():
    p = _parse()
    for card in CARDS:
        t = p.toggles[card]
        assert t.get("type") == "button", card
        assert t.get("aria-expanded") == "true", card  # expanded by default
        controls = t.get("aria-controls")
        assert controls == f"card-{card}-body", card
        assert p.bodies[card].get("id") == controls, card


def test_shared_helper_is_present_and_guards_storage():
    assert ".panel-section[data-card]" in INDEX
    assert ".card-collapsed > .card-body { display: none; }" in INDEX
    script = INDEX[INDEX.index("Shared collapsible-card helper") :]
    script = script[: script.index("</script>")]
    assert script.count("window.localStorage") == 2
    assert script.count("try {") == 2 and script.count("catch (e)") == 2
    assert "aria-expanded" in script


def test_helper_hides_with_css_not_dom_removal():
    script = INDEX[INDEX.index("Shared collapsible-card helper") :]
    script = script[: script.index("</script>")]
    for forbidden in ("removeChild", ".remove()", "innerHTML"):
        assert forbidden not in script


def test_safety_critical_controls_stay_outside_collapsible_bodies():
    p = _parse()
    for eid in ("land-all", "fleet-plan", "fleet-start", "fleet-drone-list"):
        assert eid in p.in_body, eid
        assert p.in_body[eid] == [], f"{eid} is inside a collapsible body"


def test_body_tracking_detects_collapsible_content():
    # Guards the safety test above against a parser that never reports anything as inside a body.
    p = _parse()
    assert p.in_body["ceiling-cm"] == ["ceiling"]
    assert p.in_body["match-log"] == ["match-log"]


def test_floating_fleet_status_panel_is_not_a_card():
    # CONNECTED/DOWN/LOST is rendered into #fleet-panel-body; it must not sit inside any card body.
    p = _parse()
    assert p.in_body["fleet-panel-body"] == []


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


def test_history_and_sessions_headers_are_keyboard_accessible():
    for cid in ("target-history-container", "sessions-container"):
        m = re.search(
            rf'<div class="collapsible-header"([^>]*aria-controls="{cid}"[^>]*)>', INDEX
        )
        assert m, cid
        assert (
            'role="button"' in m.group(1)
            and 'tabindex="0"' in m.group(1)
            and 'aria-expanded="false"' in m.group(1)
        )


def _rule(selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", INDEX)
    assert match, f"missing CSS rule for {selector}"
    return match.group(1)


def test_chevron_is_large_with_a_comfortable_hit_area():
    chevron = _rule(".card-chevron")
    size = int(re.search(r"font-size:\s*(\d+)px", chevron).group(1))
    assert 20 <= size <= 24
    for prop in ("min-width", "min-height"):
        assert int(re.search(prop + r":\s*(\d+)px", chevron).group(1)) >= 40
    assert (
        int(re.search(r"min-height:\s*(\d+)px", _rule(".card-toggle")).group(1)) >= 40
    )


def test_chevron_keeps_rotation_and_respects_reduced_motion():
    assert "rotate(-90deg)" in _rule(".card-collapsed .card-chevron")
    assert "transition: transform" in _rule(".card-chevron")
    assert re.search(
        r"prefers-reduced-motion:\s*reduce\)\s*\{[^}]*\.card-chevron[^}]*transition:\s*none",
        INDEX,
    )
    assert "focus-visible" in INDEX and "outline: 2px solid var(--cyan)" in INDEX


def test_ceiling_card_sits_directly_below_flight_controls():
    flight = PAGE.index('data-card="flight-controls"')
    ceiling = PAGE.index('data-card="ceiling"')
    ai_reasoning = PAGE.index('data-card="ai-reasoning"')
    assert flight < ceiling < ai_reasoning
    # The fleet card (LAND ALL, fleet status) keeps its place ahead of flight controls.
    assert PAGE.index('data-card="drone-fleet"') < flight
