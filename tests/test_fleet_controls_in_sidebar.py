"""The fleet commands live in the sidebar, LAND ALL in the top bar, and the fleet display in the main area."""
import re
from html.parser import HTMLParser
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard"
INDEX = (DASH / "templates" / "index.html").read_text()
JS = (DASH / "static" / "ui" / "ui.js").read_text()


class _Ancestry(HTMLParser):
    """Record, for each element id, the ids and tags of its ancestors."""

    VOID = frozenset({"input", "br", "img", "meta", "link", "hr"})

    def __init__(self):
        super().__init__()
        self.stack = []
        self.ancestors = {}

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        eid = dict(attrs).get("id")
        if eid:
            self.ancestors[eid] = [a for a in self.stack if a]
        self.stack.append(eid or tag)

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()


def _ancestry():
    p = _Ancestry()
    p.feed(INDEX)
    return p.ancestors


def test_fleet_buttons_are_in_the_sidebar_and_land_all_in_the_top_bar():
    anc = _ancestry()
    for eid in ("fleet-plan", "fleet-start"):
        assert "aside" in anc[eid] and "main" not in anc[eid], eid
    assert "header" in anc["land-all"]
    assert "aside" not in anc["land-all"] and "main" not in anc["land-all"]


def test_fleet_display_stays_in_the_main_area():
    anc = _ancestry()
    for eid in ("fleet-banner", "fleet-uncovered", "search-map", "drone-status"):
        assert "main" in anc[eid] and "aside" not in anc[eid], eid


def test_single_land_all_wired_once():
    assert len(re.findall(r'id="land-all"', INDEX)) == 1
    assert JS.count("on('land-all', 'click', landAll)") == 1
    assert JS.count("function landAll(") == 1
    assert "onclick=" not in INDEX


def test_land_actions_are_labelled_apart():
    assert re.search(r'id="land-all"[^>]*>LAND ALL <kbd>', INDEX)
    assert re.search(r'id="land-selected"[^>]*>Land drone<', INDEX)
    assert len(re.findall(r"sendCmd\('land'\)", JS)) == 1


def test_plan_refused_event_shows_a_readable_message_and_the_detail():
    # An unknown fleet_event shows its raw name in the banner; plan_refused must say why the plan was refused.
    handler = JS[JS.index("socket.on('fleet_event'"):JS.index("// ========== PLAN AND START")]
    assert "kind === 'plan_refused'" in handler
    branch = handler[handler.index("kind === 'plan_refused'"):handler.index("kind === 'start_refused'")]
    assert "showFleetBanner('PLAN REFUSED')" in branch
    assert "showFleetError('Plan refused: ' + (ev.detail" in branch
