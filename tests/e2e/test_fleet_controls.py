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
