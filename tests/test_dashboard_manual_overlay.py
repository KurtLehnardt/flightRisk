"""Template contract for the manual takeover overlay (docs/manual-takeover.md).

Checked as text and with an HTML parser: the overlay sits on the video panel
outside every collapsible card body, has every control the brief asks for,
tile clicks select a drone without releasing it on a second click, the keys
skip editable fields and Shift+L, and LAND ALL stays the single global
control.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

INDEX = (Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard" / "templates" / "index.html").read_text()
SCRIPT = INDEX[INDEX.index("// ========== MANUAL TAKEOVER"):]
SCRIPT = SCRIPT[: SCRIPT.index("</script>")]


class _Tree(HTMLParser):
    VOID = frozenset({"input", "br", "img", "meta", "link", "hr"})

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str | None, list[str]]] = []
        self.ancestors: dict[str, list[str]] = {}
        self.in_card_body: dict[str, bool] = {}
        self.buttons: dict[str, str] = {}
        self._button: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.VOID:
            return
        eid = a.get("id")
        classes = (a.get("class") or "").split()
        if eid:
            self.ancestors[eid] = [i for i, _ in self.stack if i]
            self.in_card_body[eid] = any("card-body" in c for _, c in self.stack)
        if tag == "button" and eid:
            self._button = eid
            self.buttons[eid] = ""
        self.stack.append((eid, classes))

    def handle_data(self, data):
        if self._button:
            self.buttons[self._button] += data

    def handle_endtag(self, tag):
        if tag == "button":
            self._button = None
        if tag not in self.VOID and self.stack:
            self.stack.pop()


def _tree():
    t = _Tree()
    t.feed(INDEX)
    return t


def test_overlay_is_on_the_video_panel_under_the_tiles_and_outside_collapsible_bodies():
    t = _tree()
    assert "manual-overlay" in t.ancestors
    assert t.in_card_body["manual-overlay"] is False
    assert "tab-controls" not in t.ancestors["manual-overlay"]
    assert INDEX.index('id="video-tiles"') < INDEX.index('id="manual-overlay"')
    panel = INDEX.index('<div class="video-panel">')
    sidebar = INDEX.index('<aside class="sidebar">')
    assert panel < INDEX.index('id="manual-overlay"') < sidebar
    for eid in ("manual-land", "manual-resume", "manual-takeoff"):
        assert t.in_card_body[eid] is False, eid


def test_overlay_has_every_control_labelled_with_the_drone_and_a_manual_badge():
    t = _tree()
    expected = {
        "manual-takeoff": "Takeoff", "manual-land": "Land", "manual-forward": "Forward", "manual-back": "Back",
        "manual-left": "Left", "manual-right": "Right", "manual-up": "Up", "manual-down": "Down",
        "manual-ccw": "Rotate left", "manual-cw": "Rotate right", "manual-resume": "Resume search",
    }
    for eid, label in expected.items():
        assert t.buttons[eid].strip() == label, eid
    assert '<span class="manual-badge">MANUAL</span>' in INDEX
    assert 'id="manual-drone-id"' in INDEX
    assert re.search(r'id="manual-overlay"[^>]*\bhidden\b', INDEX)  # shown only for a selected drone


def test_tile_click_selects_a_drone_and_the_same_tile_does_not_release():
    assert "getElementById('video-tiles').addEventListener('click'" in SCRIPT
    assert "socket.emit('manual_take_control'" in SCRIPT
    take = SCRIPT[SCRIPT.index("function manualTakeControl"):]
    take = take[: take.index("\n        }\n")]
    # The active tile returns before anything is sent; it never emits a release.
    assert "if (droneId === manualDroneId) return;" in take
    assert take.index("droneId === manualDroneId") < take.index("socket.emit")
    assert "manual_resume_search" not in take


def test_resume_search_is_the_only_release_from_the_overlay():
    assert SCRIPT.count("socket.emit('manual_resume_search'") == 1
    assert "getElementById('manual-resume').addEventListener('click', manualResumeSearch)" in SCRIPT


def test_selected_tile_is_highlighted():
    assert ".video-tile.manual-selected" in INDEX
    assert "classList.toggle('manual-selected', selected)" in SCRIPT


def test_controls_are_disabled_with_a_reason_when_the_drone_link_is_down_but_land_never_is():
    assert "d.drone_link_ok === false" in SCRIPT and "Drone link down" in SCRIPT
    assert "getElementById('manual-land').disabled = false" in SCRIPT
    assert "manual-reason" in SCRIPT


def test_keys_only_while_under_control_never_in_fields_and_never_shift_l():
    keys = SCRIPT[SCRIPT.index("document.addEventListener('keydown'"):]
    keys = keys[: keys.index("});")]
    assert "if (!manualDroneId" in keys
    assert "isEditableTarget(t)" in keys and "'SELECT'" in keys
    assert "if (e.shiftKey) return;" in keys  # Shift+L stays LAND ALL
    # The sidebar keyboard handler stands aside while the overlay flies a drone (no double send).
    assert "if (typeof manualDroneId !== 'undefined' && manualDroneId) return;" in INDEX


def test_land_all_and_fleet_status_stay_global():
    assert len(re.findall(r'id="land-all"', INDEX)) == 1
    t = _tree()
    assert t.in_card_body["land-all"] is False and t.in_card_body["fleet-panel-body"] is False
    # The overlay sits below the floating fleet status panel (z-index 900), never over it.
    assert re.search(r"#manual-overlay \{[^}]*z-index: 850", INDEX)
    assert "manual-overlay" not in t.ancestors["fleet-section"]
