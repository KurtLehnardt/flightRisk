"""Dashboard page (docs/ui-design.md): template contract and how the page is served.

The checks read templates/index.html (with the included ceiling card),
static/ui/ui.js and static/ui/ui.css as text and with an HTML parser:

* every capability of the dashboard has exactly one control, and no control
  id or command button is duplicated;
* the page uses exactly the Socket.IO events and REST calls that the backend
  serves, pinned below;
* LAND ALL and its Shift+L handler sit outside every collapsible section, and
  LAND ALL is the only element painted with the danger colour;
* per-drone connection state, battery and the held/auto-land countdown are
  always visible (outside every collapsible section);
* Start keeps its window.confirm and its plan gate.

The server checks show that / on the dashboard's own port serves this page
and that the A/B comparison option (--ui-v2-port) is gone. The section and
chevron rules are in tests/test_dashboard_collapsible_cards.py.
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import pytest

import flightrisk.dashboard.__main__ as dashboard_main
from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.app import app

DASH = Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard"
INCLUDE = '{% include "ceiling_settings.html" %}'
CEILING = (DASH / "templates" / "ceiling_settings.html").read_text()
PAGE_RAW = (DASH / "templates" / "index.html").read_text()
PAGE = PAGE_RAW.replace(INCLUDE, CEILING)
JS = (DASH / "static" / "ui" / "ui.js").read_text()
# The search plan editor (docs/map-and-plan-editing.md) is part of the page's contract too.
MAP_JS = (DASH / "static" / "ui" / "map_plan.js").read_text()
CSS = (DASH / "static" / "ui" / "ui.css").read_text()

# The Socket.IO events the page emits and listens to and the REST calls it makes, outside the ceiling card.
# They are exactly the set the earlier dashboard layout used (kept on the legacy-ui branch), so the backend
# serves every one of them; a change here is a change to the page's contract with the backend.
EMITTED = {
    "deregister_drone", "drone_command", "get_detection_profiles", "get_fleet_status", "get_manual_status",
    "get_reasoning_provider", "land_all", "manual_command", "manual_resume_search", "manual_take_control",
    "match_alert_response", "plan_fleet", "register_drone", "remove_all_drones", "restart_dashboard",
    "revert_target", "set_description", "set_detection_profile", "set_fleet_detection_profile",
    "set_primary_drone", "set_reasoning_provider", "set_sensitivity_preset", "set_stream_video", "set_threshold",
    "start_fleet", "start_recording", "start_search", "stop_recording", "stop_search",
    # The search plan editor (docs/map-and-plan-editing.md).
    "fleet_plan_edited", "get_map_config", "list_plans", "load_plan", "map_cache_status", "preview_plan",
    "save_plan",
}
LISTENED = {
    "battery_critical", "battery_warning", "confidence_progress", "connect", "description_set",
    "detection_profiles", "disconnect", "drone_registered", "error", "face_report", "fleet_detection",
    "fleet_event", "land_all_ack", "fleet_plan", "fleet_status", "fleet_telemetry", "frame", "manual_ack", "manual_result",
    "manual_status", "match_alert", "match_alert_reminder", "match_alert_resolved", "match_alert_response_ack",
    "metrics_update", "obstacle_detected", "reasoning_provider", "recording_started", "recording_stopped",
    "recovery_status", "search_complete", "search_paused", "search_plan", "search_progress", "status",
    "stream_video_state", "target_photo", "target_set", "threshold_updated",
    # The search plan editor.
    "map_cache_status", "map_config", "plan_list", "plan_loaded", "plan_preview", "plan_saved",
}
FETCHED = {
    "/api/clear-target", "/api/feedback-stats", "/api/matches", "/api/sessions", "/api/target-history",
    "/api/threshold-suggestion", "/api/upload-target",
}

# Every capability of the dashboard and the one control that offers it.
CAPABILITIES = {
    # Safety and fleet search
    "land all drones": "land-all",
    "plan fleet search": "fleet-plan",
    "start fleet search": "fleet-start",
    # Fleet section
    "select a drone": "drone-selector",
    "land the selected drone (Land this drone)": "land-selected",
    "add a drone": "drone-add",
    "retry the primary drone": "drone-retry",
    "remove the selected drone (and the per-row remove)": "drone-remove",
    "remove all drones": "drone-remove-all",
    "set primary drone": "drone-set-primary",
    "keyboard flight control": "keyboard-toggle",
    "direct forward": "direct-forward",
    "direct back": "direct-back",
    "direct left": "direct-left",
    "direct right": "direct-right",
    "direct up": "direct-up",
    "direct down": "direct-down",
    "direct rotate left": "direct-ccw",
    "direct rotate right": "direct-cw",
    "direct hover (Stop)": "direct-hover",
    "direct takeoff": "direct-takeoff",
    # Mission section
    "upload target photo": "target-upload",
    "clear target photo": "clear-target-btn",
    "describe target": "target-description",
    "set description": "set-description",
    "target history and revert": "target-history-toggle",
    "search pattern (fleet and single drone)": "search-pattern",
    "search area x": "plan-area-x",
    "search area y": "plan-area-y",
    "search area width": "plan-area-w",
    "search area depth": "plan-area-h",
    "anchor latitude": "plan-anchor-lat",
    "anchor longitude": "plan-anchor-lon",
    "anchor heading": "plan-anchor-heading",
    "pick the anchor on the map": "plan-anchor-pick",
    "anchor from drone GPS": "plan-anchor-gps",
    "search area centre latitude": "plan-center-lat",
    "search area centre longitude": "plan-center-lon",
    "drone whose path is edited": "plan-drone",
    "revert a path to the pattern": "plan-revert",
    "delete the selected vertex": "plan-delete-vertex",
    "saved plan name": "plan-name",
    "save plan": "plan-save",
    "saved plans": "plan-load-list",
    "load plan": "plan-load",
    "start pattern search": "start-search",
    "stop pattern search": "stop-search",
    "fleet detection profile": "profile-fleet-default",
    "per-drone detection profile": "profile-drone-list",
    "map grid view": "map-mode-grid",
    "map lat/lon view": "map-mode-latlon",
    # Flight settings
    "ceiling height": "ceiling-cm",
    "ceiling margin": "ceiling-margin-cm",
    "altitude slots": "ceiling-slots",
    "save ceiling settings": "ceiling-save",
    # Video and detection
    "stream video": "video-toggle",
    "sensitivity presets": "threshold-slider",
    "manual threshold": "threshold-slider",
    "analyze threshold": "tuner-analyze",
    "apply suggested threshold": "tuner-apply",
    "AI reasoning backend": "reasoning-provider",
    # Session and recording
    "record session": "rec-btn",
    "match log and feedback": "match-log",
    "past sessions": "sessions-toggle",
    # Advanced
    "restart dashboard": "restart-dashboard",
    "observability counters": "obs-frames",
    "link timing (offset, RTT, skew)": "fleet-panel-body",
    # Manual takeover panel (contextual, under the tiles)
    "manual takeoff": "manual-takeoff",
    "manual land": "manual-land",
    "manual resume search": "manual-resume",
    "manual forward": "manual-forward",
    "manual back": "manual-back",
    "manual left": "manual-left",
    "manual right": "manual-right",
    "manual up": "manual-up",
    "manual down": "manual-down",
    "manual rotate left": "manual-ccw",
    "manual rotate right": "manual-cw",
    # Match alert dialog
    "confirm match": "overlay-confirm-btn",
    "reject match": "overlay-reject-btn",
    "decide later": "overlay-later-btn",
}

# Status and readout ids the page renders into.
STATUS_IDS = (
    "status-label", "appearance-only-badge", "face-off-badge", "rec-indicator", "video-tiles",
    "video-tile-default", "video-feed", "detection-overlay", "detections-only-indicator", "video-placeholder",
    "hud-fps", "hud-persons", "hud-match", "hud-confidence", "search-bar", "search-bar-fill", "manual-overlay",
    "manual-drone-id", "manual-mode", "manual-reason", "search-map", "fleet-banner", "fleet-error",
    "fleet-uncovered", "fleet-panel-body", "match-count", "feedback-stats", "quality-feedback",
    "target-preview", "upload-prompt", "target-upload-zone", "threshold-val", "tuner-result", "tuner-current",
    "tuner-suggested", "tuner-confidence", "tuner-reason", "reasoning-status", "reasoning-unavailable",
    "search-status", "rec-status", "rec-btn-text", "video-status", "keyboard-status", "keyboard-legend",
    "profile-controls-note", "detection-profiles-section", "target-history-container", "target-history-list",
    "sessions-container", "sessions-list", "obs-persons", "obs-errors", "obs-matches-total", "obs-matches-reid",
    "obs-matches-face", "obs-matches-desc", "obs-face-rate", "obs-reasoning", "obs-reasoning-lat",
    "match-overlay", "match-overlay-card", "overlay-title", "overlay-subtitle", "overlay-snapshot",
    "overlay-score", "overlay-reasoning", "overlay-status", "toast", "ceiling-section", "ceiling-body",
    "ceiling-unavailable", "ceiling-max", "ceiling-msg", "plan-editor", "plan-check", "plan-corners",
)


class _Tree(HTMLParser):
    """Records, for every element id, the collapsible sections and card bodies around it."""

    VOID = frozenset({"input", "br", "img", "meta", "link", "hr"})

    def __init__(self):
        super().__init__()
        self.stack: list[dict] = []
        self.ids: list[str] = []
        self.sections: dict[str, list[str]] = {}
        self.in_body: dict[str, bool] = {}
        self.ancestors: dict[str, list[str]] = {}

    def _record(self, a):
        eid = a.get("id")
        if not eid:
            return
        self.ids.append(eid)
        self.sections[eid] = [f["card"] for f in self.stack if f["card"]]
        self.in_body[eid] = any(f["body"] for f in self.stack)
        self.ancestors[eid] = [f["id"] for f in self.stack if f["id"]] + [f["tag"] for f in self.stack]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        self._record(a)
        if tag in self.VOID:
            return
        card = a.get("data-card") if "panel-section" in classes else None
        self.stack.append({"tag": tag, "id": a.get("id"), "card": card, "body": "card-body" in classes})

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()


def _tree(markup=PAGE):
    t = _Tree()
    t.feed(markup)
    return t


def _quoted_names(pattern, text):
    names = set()
    for call in re.findall(pattern, text):
        names.update(re.findall(r"'([a-z_]+)'", call))
    return names


def _emitted(text):
    return _quoted_names(r"socket\.emit\(([^,)]*)", text)


def _listened(text):
    return set(re.findall(r"socket\.on\('([a-z_]+)'", text))


def _fetched(text):
    return set(re.findall(r"fetch\('(/api/[a-z-]+)", text))


# ---------------------------------------------------------------- template contract


def test_every_capability_has_exactly_one_control():
    ids = _tree().ids
    for capability, eid in CAPABILITIES.items():
        assert ids.count(eid) == 1, f"{capability}: #{eid} appears {ids.count(eid)} times"


def test_no_element_id_is_duplicated():
    ids = _tree().ids
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, dupes


def test_status_ids_are_present():
    ids = set(_tree().ids)
    missing = [eid for eid in STATUS_IDS if eid not in ids]
    assert not missing, missing


def test_page_uses_exactly_the_pinned_socket_events_and_rest_calls():
    code = PAGE_RAW + JS + MAP_JS
    assert _emitted(code) == EMITTED
    assert _listened(code) == LISTENED
    assert _fetched(code) == FETCHED
    # Per-session and per-match detail URLs are built from a prefix.
    for prefix in ("'/api/sessions/' +", "'/api/matches/' +"):
        assert prefix in JS


def test_every_emitted_event_has_a_server_handler():
    handlers = app_module.socketio.server.handlers.get("/", {})
    ceiling = _emitted(CEILING)
    assert ceiling, "the ceiling card emits its own events"
    missing = sorted(e for e in EMITTED | ceiling if e not in handlers)
    assert not missing, missing


def test_command_buttons_are_not_repeated():
    # Each manual action and each direct action is bound once, and the two pads never share an id.
    manual = re.findall(r'data-manual="([a-z]+)"', PAGE)
    direct = re.findall(r'data-direct="([a-z]+)"', PAGE)
    assert sorted(manual) == sorted(set(manual)) and len(manual) == 10
    assert sorted(direct) == sorted(set(direct)) and len(direct) == 10
    # Safety and mission commands are emitted from one place each.
    for event in ("land_all", "plan_fleet", "start_fleet", "manual_take_control", "manual_resume_search",
                  "drone_command", "set_stream_video", "set_reasoning_provider", "match_alert_response"):
        assert JS.count(f"socket.emit('{event}'") == 1, event
    # No sidebar section lists the manual takeover buttons.
    t = _tree()
    for eid in ("manual-takeoff", "manual-land", "manual-resume", "manual-forward"):
        assert t.sections[eid] == [] and t.in_body[eid] is False, eid


def test_direct_pad_and_manual_panel_are_never_shown_together():
    # The direct pad is for a single drone without a fleet; in fleet mode the manual panel flies a drone.
    assert re.search(r'id="direct-control"[^>]*\bhidden\b', PAGE)
    assert re.search(r'id="manual-overlay"[^>]*\bhidden\b', PAGE)
    assert "$('direct-control').hidden = fleetMode;" in JS
    assert "if (!keyboardControlEnabled || fleetMode) return;" in JS
    # The status event decides the mode; manual status and a fleet status with manual state imply fleet mode.
    assert "setFleetMode(!!(data.fleet && data.fleet.drones > 0));" in JS
    assert JS.count("setFleetMode(true);") == 2


def test_land_all_is_always_visible_outside_every_section():
    t = _tree()
    assert t.ids.count("land-all") == 1
    assert t.sections["land-all"] == [] and t.in_body["land-all"] is False
    assert "header" in t.ancestors["land-all"]
    assert "aside" not in t.ancestors["land-all"] and "main" not in t.ancestors["land-all"]
    assert re.search(r"\.topbar\s*\{[^}]*position:\s*sticky", CSS)
    assert 'aria-keyshortcuts="Shift+L"' in PAGE


def test_land_all_is_the_only_strong_red_element():
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL)
    rules = re.findall(r"([^{}]+)\{([^}]*var\(--danger\)[^}]*)\}", css)
    selectors = [sel.strip() for sel, _ in rules]
    assert selectors and all(sel.startswith(".land-all") for sel in selectors), selectors
    assert "background: var(--danger)" in CSS
    assert "var(--danger)" not in CEILING


def test_shift_l_handler():
    assert "socket.emit('land_all');" in JS
    handler = JS[JS.index("document.addEventListener('keydown', function (e) {"):]
    handler = handler[: handler.index("}, true);")]
    for needle in ("if (e.repeat) return;", "e.code === 'KeyL'", "e.key === 'L'", "if (e.altKey) return;",
                   "isEditableTarget(e.target) && !(e.ctrlKey || e.metaKey)", "landAll();"):
        assert needle in handler, needle
    # The manual and direct key handlers stand aside for Shift+L.
    assert "if (e.shiftKey) return;" in JS
    assert "if ((e.shiftKey && e.code === 'KeyL') || (e.shiftKey && e.key === 'L')) return;" in JS


def test_drone_state_battery_and_countdown_are_always_visible():
    t = _tree()
    assert t.sections["drone-status"] == [] and t.in_body["drone-status"] is False
    assert "main" in t.ancestors["drone-status"]
    for needle in ("text: 'LOST'", "text: 'DOWN'", "text: 'CONNECTED'", "metaItem('Battery'",
                   "'HELD: auto-landing in ' + landIn + ' s, no controller'", "d.orphan_land_in_s"):
        assert needle in JS, needle
    # The countdown shows on the drone's status card and on its video tile.
    assert JS.count("'HELD: auto-landing in ' + landIn + ' s, no controller'") == 2
    # The manual panel sits under the tiles in the main area, outside the sidebar.
    assert PAGE.index('id="video-tiles"') < PAGE.index('id="manual-overlay"') < PAGE.index('<aside')
    assert t.sections["manual-overlay"] == []


def test_start_keeps_its_confirmation_and_plan_gate():
    start = JS[JS.index("function startFleet()"):]
    start = start[: start.index("\n    }\n")]
    assert "if (!fleetPlanReceived || fleetStartInFlight) return;" in start
    assert start.index("window.confirm(") < start.index("socket.emit('start_fleet'")
    assert re.search(r'id="fleet-start"[^>]*\bdisabled\b', PAGE)


def test_manual_takeover_rules():
    take = JS[JS.index("function manualTakeControl"):]
    take = take[: take.index("\n    }\n")]
    assert "if (droneId === manualDroneId) return;" in take
    assert take.index("droneId === manualDroneId") < take.index("socket.emit")
    assert "$('manual-land').disabled = false;" in JS
    assert "'slot_band'" in JS and "d.drone_link_ok === false" in JS


def test_dynamic_values_never_go_through_innerhtml():
    assert "innerHTML" not in JS
    assert "insertAdjacentHTML" not in JS and "document.write" not in JS
    assert "onclick=" not in PAGE_RAW


def test_light_and_dark_themes_with_one_accent():
    assert "@media (prefers-color-scheme: dark)" in CSS
    assert '<meta name="color-scheme" content="light dark">' in PAGE
    root = re.search(r":root\s*\{([^}]*)\}", CSS).group(1)
    assert len(re.findall(r"--accent:", root)) == 1


def test_ceiling_card_uses_only_tokens_the_stylesheet_defines():
    style = CEILING[CEILING.index("<style>"):CEILING.index("</style>")]
    used = set(re.findall(r"var\((--[a-z0-9-]+)", style))
    defined = set(re.findall(r"(--[a-z0-9-]+):", re.search(r":root\s*\{([^}]*)\}", CSS).group(1)))
    assert used and used <= defined, sorted(used - defined)


# ---------------------------------------------------------------- server


def test_root_serves_the_dashboard_page_and_its_assets():
    client = app.test_client()
    page = client.get("/").get_data(as_text=True)
    assert "/static/ui/ui.js" in page and "/static/ui/ui.css" in page and "<title>FlightRisk</title>" in page
    assert 'id="ceiling-section"' in page and 'id="land-all"' in page
    for path in ("/static/ui/ui.js", "/static/ui/ui.css", "/static/socket.io.min.js", "/static/leaflet/leaflet.js"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        resp.close()
    resp = client.get("/static/v2/ui.js")
    assert resp.status_code == 404
    resp.close()


def test_the_ab_comparison_listener_is_gone(monkeypatch):
    for name in ("UI_V2_PORT_ENV", "UI_V2_ENVIRON_KEY", "make_ui_v2_server", "ui_v2_wsgi_app", "_ui_v2_port"):
        assert not hasattr(app_module, name), name
    monkeypatch.delenv("FLIGHTRISK_PORT", raising=False)
    argv = ["flightrisk-dashboard", "--source", "file", "--video", "clip.mp4", "--ui-v2-port", "5601"]
    with patch.object(sys, "argv", argv), patch.object(dashboard_main, "run_dashboard") as run, \
            pytest.raises(SystemExit) as exit_info:
        dashboard_main.main()
    assert exit_info.value.code == 2
    run.assert_not_called()
