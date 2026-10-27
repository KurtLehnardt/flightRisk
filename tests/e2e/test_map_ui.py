"""Static checks for the dashboard map UI, fleet panel and LAND ALL control (t10)."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

DASHBOARD = Path(__file__).resolve().parents[2] / "flightrisk" / "dashboard"
INDEX = (DASHBOARD / "templates" / "index.html").read_text(encoding="utf-8")


def _client():
    from flightrisk.dashboard.app import app

    return app.test_client()


def test_leaflet_assets_served():
    client = _client()
    for path in ("/static/leaflet/leaflet.js", "/static/leaflet/leaflet.css"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        resp.close()


def test_leaflet_license_present():
    text = (DASHBOARD / "static" / "leaflet" / "LICENSE").read_text(encoding="utf-8")
    assert "BSD 2-Clause" in text


def test_index_has_map_panel_and_handlers():
    for needle in (
        'id="search-map"',
        "/static/leaflet/leaflet.js",
        "/static/leaflet/leaflet.css",
        "L.CRS.Simple",
        "socket.on('fleet_plan'",
        "socket.on('fleet_status'",
        "socket.on('fleet_detection'",
        "socket.on('fleet_event'",
        "socket.on('search_plan'",
        "rel_x_cm",
    ):
        assert needle in INDEX, needle


def test_index_has_no_remote_script_or_css():
    assert not re.search(r"""(?:src|href)\s*=\s*["']?\s*(?:https?:)?//""", INDEX, re.I)


def test_tile_layer_has_offline_fallback():
    assert "L.tileLayer(" in INDEX
    assert "errorTileUrl" in INDEX


def test_index_has_badge():
    assert 'id="appearance-only-badge"' in INDEX
    assert "appearance_only" in INDEX


def test_fleet_panel_present():
    assert 'id="fleet-panel"' in INDEX


def test_land_all_button_and_hotkey():
    assert 'id="land-all"' in INDEX
    # Shift+L matches the physical key, so Caps Lock cannot stop LAND ALL (R-111).
    assert "e.shiftKey && e.code === 'KeyL'" in INDEX
    assert "socket.emit('land_all'" in INDEX
    # The existing plain 'l' binding must stay.
    assert "'l': 'land'" in INDEX


def test_land_all_hotkey_needs_modifier_in_text_fields():
    """Inside an editable field the hotkey needs Ctrl or Cmd; outside, Shift+L alone works (R-115).

    Intended change from R-111, which let a bare Shift+L land the fleet from inside a text field.
    """
    guard = "if (!((e.shiftKey && e.code === 'KeyL') || (e.shiftKey && e.key === 'L'))) return;"
    start = INDEX.index(guard)
    end = INDEX.index("}, true);", start)
    body = INDEX[start:end]
    assert "if (isEditableTarget(e.target) && !(e.ctrlKey || e.metaKey)) return;" in body
    assert "landAll();" in body
    # Caps Lock cannot stop it: the physical key is matched, never only e.key.
    assert "e.code === 'KeyL'" in guard
