"""Template and script contract of the search plan editor (docs/map-and-plan-editing.md).

These read the page, ``static/ui/map_plan.js``, ``static/ui/plan_geom.js`` and
``static/ui/ui.js`` as text. The behaviour behind them is covered by the
socket and geometry tests; the headless-browser check is manual (see the PR).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from flightrisk.dashboard import app as app_module

DASH = Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard"
INDEX = (DASH / "templates" / "index.html").read_text()
UI_JS = (DASH / "static" / "ui" / "ui.js").read_text()
MAP_JS = (DASH / "static" / "ui" / "map_plan.js").read_text()
GEOM_JS = (DASH / "static" / "ui" / "plan_geom.js").read_text()


def test_scripts_load_in_order_from_this_server():
    order = [INDEX.index(f'src="/static/{name}"') for name in
             ("leaflet/leaflet.js", "ui/plan_geom.js", "ui/map_plan.js", "ui/ui.js")]
    assert order == sorted(order)
    for text in (MAP_JS, GEOM_JS):
        assert not re.search(r"https?://", text), "remote URLs come from the server's provider list only"


def test_editor_never_parses_html():
    for text in (MAP_JS, GEOM_JS):
        assert "innerHTML" not in text and "insertAdjacentHTML" not in text and "outerHTML" not in text
    # Leaflet div icons get a DOM node, never an HTML string.
    assert "html: node" in MAP_JS
    assert not re.search(r"html:\s*['\"`]", MAP_JS)


def test_every_editor_control_is_wired():
    for eid in ("plan-area-x", "plan-area-y", "plan-area-w", "plan-area-h", "plan-anchor-lat", "plan-anchor-lon",
                "plan-anchor-heading", "plan-anchor-pick", "plan-anchor-gps", "plan-center-lat", "plan-center-lon",
                "plan-drone", "plan-revert", "plan-delete-vertex", "plan-check", "plan-corners", "plan-name",
                "plan-save", "plan-load-list", "plan-load", "search-pattern"):
        assert f'id="{eid}"' in INDEX, eid
        assert f"'{eid}'" in MAP_JS, eid


def test_paths_can_be_dragged_extended_trimmed_and_reverted():
    assert "draggable: true" in MAP_JS
    assert "marker.on('drag'" in MAP_JS and "marker.on('dragend'" in MAP_JS
    assert "line.on('click'" in MAP_JS and "insertVertex(" in MAP_JS
    assert "marker.on('dblclick contextmenu'" in MAP_JS and "deleteVertex(" in MAP_JS
    assert "$('plan-revert').addEventListener('click'" in MAP_JS
    # The path that will fly is computed with the same snapping as the server while dragging.
    assert "G.pathToLanes(" in MAP_JS


def test_area_can_be_moved_and_resized_on_the_map():
    assert "Drag to move the search area" in MAP_JS
    assert "Drag to resize the search area" in MAP_JS


def test_every_edit_goes_through_the_server_check_and_invalidates_the_plan():
    edited = re.search(r"function edited\(\) \{.*?\n        \}", MAP_JS, re.DOTALL).group(0)
    assert "opts.onEdited()" in edited and "schedulePreview()" in edited
    assert "socket.emit('preview_plan', { plan: s })" in MAP_JS
    body = re.search(r"function mapPlanEdited\(\) \{.*?\n    \}", UI_JS, re.DOTALL).group(0)
    assert "fleetPlanReceived = false" in body and "updateFleetStartButton()" in body
    assert "socket.emit('fleet_plan_edited')" in body
    assert "onEdited: mapPlanEdited" in UI_JS


def test_start_keeps_its_plan_gate_and_confirmation():
    body = re.search(r"function startFleet\(\) \{.*?\n    \}", UI_JS, re.DOTALL).group(0)
    assert body.index("fleetPlanReceived") < body.index("window.confirm") < body.index("socket.emit('start_fleet')")
    # A server-side invalidation also disables Start.
    assert "kind === 'plan_invalidated'" in UI_JS


def test_layer_switch_offline_cache_and_attribution():
    assert "L.control.layers(" in MAP_JS
    assert "'No imagery'" in MAP_JS
    assert "'/tiles/' + p.id + '/{z}/{x}/{y}'" in MAP_JS
    assert "socket.emit('map_cache_status'" in MAP_JS
    assert "attribution: p.attribution" in MAP_JS
    assert "attributionControl: true" in UI_JS
    assert "online only" in MAP_JS


def test_the_map_says_where_positions_are_dead_reckoned():
    assert "dead-reckoned from the anchor and approximate" in MAP_JS
    assert "Anchor not set" in MAP_JS
    assert "' (approx.)'" in UI_JS and "' (GPS)'" in UI_JS


def test_problems_and_uncovered_areas_are_shown():
    assert "plan-problem" in MAP_JS and "plan-uncovered" in MAP_JS
    assert "p.vertex" in MAP_JS
    assert "kind === 'area'" in UI_JS


def test_single_drone_start_sends_the_map_plan_only_without_a_fleet():
    assert "mapPlan.singleSpec()" in UI_JS
    assert "S.config.mode === 'single'" in MAP_JS and "S.config.mode === 'fleet'" in MAP_JS


@pytest.mark.parametrize("path", ["/tiles/usgs-imagery/1/0/0", "/tiles/osm/1/0/0"])
def test_the_tile_route_stays_behind_the_api_key_gate(monkeypatch, path):
    monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", "a-test-key")
    resp = app_module.app.test_client().get(path)
    assert resp.status_code == 401
    assert "Access-Control-Allow-Origin" not in resp.headers
