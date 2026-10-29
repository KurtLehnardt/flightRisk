"""Template contract for the manual takeover overlay (docs/manual-takeover.md).

Checked as text and with an HTML parser: the overlay sits in the main area
under the video tiles, outside every collapsible card body and outside the
sidebar, has every control the brief asks for,
tile clicks select a drone without releasing it on a second click, the keys
skip editable fields and Shift+L, and LAND ALL stays the single global
control.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard"
INDEX = (DASH / "templates" / "index.html").read_text()
JS = (DASH / "static" / "ui" / "ui.js").read_text()
CSS = (DASH / "static" / "ui" / "ui.css").read_text()
SCRIPT = JS[JS.index("// ========== MANUAL TAKEOVER"):JS.index("// ========== INIT")]


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


def test_overlay_is_in_the_main_area_under_the_tiles_and_outside_collapsible_bodies():
    t = _tree()
    assert "manual-overlay" in t.ancestors
    assert t.in_card_body["manual-overlay"] is False
    assert INDEX.index('id="video-tiles"') < INDEX.index('id="manual-overlay"')
    main = INDEX.index('<main class="main">')
    sidebar = INDEX.index('<aside class="sidebar"')
    assert main < INDEX.index('id="manual-overlay"') < INDEX.index("</main>") < sidebar
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
    assert "videoTilesEl.addEventListener('click'" in SCRIPT
    assert "socket.emit('manual_take_control'" in SCRIPT
    take = SCRIPT[SCRIPT.index("function manualTakeControl"):]
    take = take[: take.index("\n    }\n")]
    # The active tile returns before anything is sent; it never emits a release.
    assert "if (droneId === manualDroneId) return;" in take
    assert take.index("droneId === manualDroneId") < take.index("socket.emit")
    assert "manual_resume_search" not in take


def test_resume_search_is_the_only_release_from_the_overlay():
    assert SCRIPT.count("socket.emit('manual_resume_search'") == 1
    assert "$('manual-resume').addEventListener('click', manualResumeSearch)" in SCRIPT


def test_selected_tile_is_highlighted():
    assert ".video-tile.manual-selected" in CSS
    assert "classList.toggle('manual-selected', selected)" in SCRIPT


def test_controls_are_disabled_with_a_reason_when_the_drone_link_is_down_but_land_never_is():
    assert "d.drone_link_ok === false" in SCRIPT and "Drone link down" in SCRIPT
    assert "$('manual-land').disabled = false" in SCRIPT
    assert "manual-reason" in SCRIPT


def test_keys_only_while_under_control_never_in_fields_and_never_shift_l():
    keys = SCRIPT[SCRIPT.index("document.addEventListener('keydown'"):]
    keys = keys[: keys.index("});")]
    assert "if (!manualDroneId" in keys
    assert "isEditableTarget(t)" in keys and "'SELECT'" in keys
    assert "if (e.shiftKey) return;" in keys  # Shift+L stays LAND ALL
    # The direct-control keyboard handler stands aside while the overlay flies a drone (no double send).
    direct = JS[JS.index("// ========== DIRECT FLIGHT CONTROL"):JS.index("// ========== DRONE CONNECTIONS")]
    assert "if (manualDroneId) return;" in direct


def test_land_all_and_drone_status_stay_global():
    assert len(re.findall(r'id="land-all"', INDEX)) == 1
    t = _tree()
    assert t.in_card_body["land-all"] is False and t.in_card_body["drone-status"] is False
    # The overlay never covers the drone status cards: they come first in the main area, and the overlay is
    # laid out in the page flow rather than floated over other content.
    assert INDEX.index('id="drone-status"') < INDEX.index('id="manual-overlay"')
    overlay = re.search(r"#manual-overlay \{([^}]*)\}", CSS).group(1)
    assert "position: absolute" not in overlay and "position: fixed" not in overlay


def test_tiles_show_the_orphan_landing_countdown_after_a_reload():
    # The countdown comes from the per-drone status (fleet_status and manual_status), so a
    # reloaded dashboard shows it again on that drone's tile.
    assert "d.orphan_land_in_s" in SCRIPT
    assert "'HELD: auto-landing in ' + landIn + ' s, no controller'" in SCRIPT
    connect = JS[JS.index("socket.on('connect', () => {"):]
    connect = connect[: connect.index("});")]
    assert "socket.emit('get_manual_status');" in connect


def test_slot_band_refusals_disable_that_step():
    assert "'slot_band'" in SCRIPT
