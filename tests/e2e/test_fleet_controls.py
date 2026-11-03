"""Static checks for the fleet Plan search and Start search controls (t24)."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

DASHBOARD = Path(__file__).resolve().parents[2] / "flightrisk" / "dashboard"
INDEX = (DASHBOARD / "templates" / "index.html").read_text(encoding="utf-8")
JS = (DASHBOARD / "static" / "ui" / "ui.js").read_text(encoding="utf-8")
LAND_ALL_HANDLER_START = "document.addEventListener('keydown', function (e) {\n        if (e.repeat) return;"


def _function_body(name: str) -> str:
    match = re.search(r"function " + name + r"\(.*?\n    \}\n", JS, re.DOTALL)
    assert match, name
    return match.group(0)


def _land_all_tag() -> str:
    return re.search(r'<button[^>]*id="land-all"[^>]*>', INDEX).group(0)


def test_plan_and_start_buttons_emit_events():
    assert re.search(r'<button[^>]*id="fleet-plan"', INDEX)
    assert re.search(r'<button[^>]*id="fleet-start"', INDEX)
    assert "on('fleet-plan', 'click', planFleet);" in JS
    assert "on('fleet-start', 'click', startFleet);" in JS
    # Plan sends the plan shown on the map, or nothing extra when the map editor is not loaded.
    assert "socket.emit('plan_fleet', plan ? { plan: plan } : {})" in _function_body("planFleet")
    # Start sends the plan the map shows; the server compares it with the plan the drones hold.
    assert "socket.emit('start_fleet', shown ? { plan: shown } : {})" in _function_body("startFleet")
    assert 'id="fleet-error"' in INDEX
    errors = JS[JS.index("socket.on('error'"):]
    assert "showFleetError(" in errors[: errors.index("});")]


def test_start_requires_confirm_and_plan():
    assert re.search(r'<button[^>]*id="fleet-start"[^>]*\bdisabled\b', INDEX)
    body = _function_body("startFleet")
    assert body.index("fleetPlanReceived") < body.index("window.confirm") < body.index("socket.emit('start_fleet'")
    assert "fleetStartInFlight = true" in body
    assert "socket.on('fleet_plan'" in JS[JS.index("let fleetPlanReceived"):]
    assert "fleetPlanReceived = true" in JS


def test_land_all_always_enabled():
    tag = _land_all_tag()
    assert "disabled" not in tag
    assert "on('land-all', 'click', landAll);" in JS
    # Nothing in the script looks the button up to disable or hide it.
    assert "$('land-all')" not in JS and "getElementById('land-all')" not in JS
    assert "e.shiftKey && e.key === 'L'" in JS


def test_land_all_hotkey_ignores_repeat_and_text_fields_without_modifier():
    start = JS.index(LAND_ALL_HANDLER_START)
    end = JS.index("}, true);", start)
    handler = JS[start:end]
    assert handler.index("if (e.repeat) return;") < handler.index("landAll();")
    assert "isEditableTarget(e.target) && !(e.ctrlKey || e.metaKey)" in handler
    assert handler.index("isEditableTarget(e.target)") < handler.index("e.preventDefault()")
    editable = _function_body("isEditableTarget")
    for token in ("isContentEditable", "TEXTAREA", "INPUT"):
        assert token in editable
    # The button is wired to the same function and the handler still runs in the capture phase.
    assert "on('land-all', 'click', landAll);" in JS
    assert JS[end:].startswith("}, true);")


def test_hotkey_select_not_editable_and_altgr_ignored():
    """A focused select is not a text field, and Alt (AltGr is Ctrl+Alt) never lands the fleet (R-116)."""
    start = JS.index(LAND_ALL_HANDLER_START)
    end = JS.index("}, true);", start)
    handler = JS[start:end]
    # Alt is rejected before the Ctrl/Cmd check could let AltGr through,
    # and before anything is prevented or sent.
    assert "if (e.altKey) return;" in handler
    alt_at = handler.index("if (e.altKey) return;")
    assert alt_at < handler.index("isEditableTarget(e.target)")
    assert alt_at < handler.index("e.preventDefault()") < handler.index("landAll();")
    # A select takes no typed text, so Shift+L alone still works from it;
    # text inputs, textareas and contenteditable elements still need Ctrl or Cmd.
    editable = _function_body("isEditableTarget")
    assert "SELECT" not in editable
    for token in ("isContentEditable", "TEXTAREA", "INPUT"):
        assert token in editable
    # The LAND ALL tooltip names the in-field shortcut.
    assert re.search(r'title="[^"]*Ctrl/Cmd\+Shift\+L[^"]*text field', _land_all_tag())


def test_obstacle_sensing_is_shown_on_the_card_the_banner_and_the_not_searched_box():
    """The obstacle badges, banners, re-flight statuses and segment colours are wired (design section 6)."""
    card = _function_body("renderDroneStatus")
    assert "senseBadge(d)" in card and "obstacleStopBadge(d)" in card
    assert "OBSTACLE STOP" in JS and "SENSE OFF" in JS and "SENSE AUTOPILOT" in JS and "STALE" in JS
    events = JS[JS.index("socket.on('fleet_event'"):]
    events = events[: events.index("// ========== PLAN AND START")]
    assert "kind === 'lane_blocked'" in events and "kind.indexOf('reflight_') === 0" in events
    for sentence in (
        "' to finish its lanes'", "'offered to '", "' will re-fly it after its lanes'", "' is re-flying it'",
        "', not offered again'", "'not re-flown: '",
    ):
        assert sentence in JS
    assert 'id="fleet-uncovered"' in INDEX
    assert "applyUncovered(data.uncovered)" in JS
    # The Start confirmation names drones flying without full sensing before it asks.
    assert "unsensedWarning(droneInfo" in _function_body("startFleet")
    map_js = (DASHBOARD / "static" / "ui" / "map_plan.js").read_text(encoding="utf-8")
    assert "setUncoveredSegments" in map_js and "plan-segment-reflight" in map_js and "plan-segment-final" in map_js
