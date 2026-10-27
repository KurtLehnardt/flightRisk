"""Static checks for the fleet Plan search and Start search controls (t24)."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

INDEX = (
    Path(__file__).resolve().parents[2] / "flightrisk" / "dashboard" / "templates" / "index.html"
).read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    match = re.search(r"function " + name + r"\(.*?\n        \}\n", INDEX, re.S)
    assert match, name
    return match.group(0)


def test_plan_and_start_buttons_emit_events():
    assert re.search(r'<button id="fleet-plan"[^>]*onclick="planFleet\(\)"', INDEX)
    assert re.search(r'<button id="fleet-start"[^>]*onclick="startFleet\(\)"', INDEX)
    assert "socket.emit('plan_fleet')" in _function_body("planFleet")
    assert "socket.emit('start_fleet')" in _function_body("startFleet")
    assert 'id="fleet-error"' in INDEX
    assert "socket.on('error'" in INDEX[INDEX.index("function showFleetError"):]


def test_start_requires_confirm_and_plan():
    assert re.search(r'<button id="fleet-start"[^>]*\bdisabled\b', INDEX)
    body = _function_body("startFleet")
    assert body.index("fleetPlanReceived") < body.index("window.confirm") < body.index("socket.emit('start_fleet')")
    assert "fleetStartInFlight = true" in body
    assert "socket.on('fleet_plan'" in INDEX[INDEX.index("let fleetPlanReceived"):]
    assert "fleetPlanReceived = true" in INDEX


def test_land_all_always_enabled():
    tag = re.search(r'<button id="land-all"[^>]*>', INDEX).group(0)
    assert "disabled" not in tag
    assert 'onclick="landAll()"' in tag
    assert "document.getElementById('land-all')" not in INDEX
    assert "e.shiftKey && e.key === 'L'" in INDEX


def test_land_all_hotkey_ignores_repeat_and_text_fields_without_modifier():
    start = INDEX.index("document.addEventListener('keydown', function(e) {\n            if (e.repeat) return;")
    end = INDEX.index("}, true);", start)
    handler = INDEX[start:end]
    assert handler.index("if (e.repeat) return;") < handler.index("landAll();")
    assert "isEditableTarget(e.target) && !(e.ctrlKey || e.metaKey)" in handler
    assert handler.index("isEditableTarget(e.target)") < handler.index("e.preventDefault()")
    editable = _function_body("isEditableTarget")
    for token in ("isContentEditable", "TEXTAREA", "INPUT"):
        assert token in editable
    # The button is unchanged and the handler still runs in the capture phase.
    assert 'onclick="landAll()"' in INDEX
    assert INDEX[end:].startswith("}, true);")


def test_hotkey_select_not_editable_and_altgr_ignored():
    """A focused select is not a text field, and Alt (AltGr is Ctrl+Alt) never lands the fleet (R-116)."""
    start = INDEX.index("document.addEventListener('keydown', function(e) {\n            if (e.repeat) return;")
    end = INDEX.index("}, true);", start)
    handler = INDEX[start:end]
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
    tag = re.search(r'<button id="land-all"[^>]*>', INDEX).group(0)
    assert re.search(r'title="[^"]*Ctrl/Cmd\+Shift\+L[^"]*text field', tag)
