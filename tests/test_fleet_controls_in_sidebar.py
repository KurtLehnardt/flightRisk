"""The fleet controls live in the right sidebar Controls tab, not in the fleet display panel."""
import re
from html.parser import HTMLParser
from pathlib import Path

INDEX = (Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard" / "templates" / "index.html").read_text()


class _Ancestry(HTMLParser):
    """Record, for each element id, the ids of its ancestors."""

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
        self.stack.append(eid)

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()


def _ancestry():
    p = _Ancestry()
    p.feed(INDEX)
    return p.ancestors


def test_fleet_buttons_are_in_controls_tab_not_fleet_section():
    anc = _ancestry()
    for eid in ("fleet-plan", "fleet-start", "land-all"):
        assert "tab-controls" in anc[eid], eid
        assert "fleet-section" not in anc[eid], eid


def test_fleet_display_stays_in_fleet_section():
    anc = _ancestry()
    for eid in ("fleet-banner", "fleet-panel", "search-map"):
        assert "fleet-section" in anc[eid], eid


def test_single_land_all_wired_once():
    assert len(re.findall(r'id="land-all"', INDEX)) == 1
    assert len(re.findall(r'onclick="landAll\(\)"', INDEX)) == 1
    assert INDEX.count("function landAll(") == 1


def test_land_actions_are_labelled_apart():
    assert ">LAND ALL drones<" in INDEX
    assert ">Land this drone<" in INDEX
    assert len(re.findall(r"sendCmd\('land'\)", INDEX)) == 1
