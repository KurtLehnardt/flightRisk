"""UI v2 (docs/ui-v2-design.md): template contract and the second-port listener.

The template checks read index_v2.html (with the shared ceiling card included),
static/v2/ui.js and static/v2/ui.css as text and with an HTML parser:

* every capability of the current UI has exactly one control in v2, and no
  control id or command button is duplicated;
* v2 uses exactly the same Socket.IO events and REST calls as the current UI;
* LAND ALL and its Shift+L handler sit outside every collapsible section, and
  LAND ALL is the only element painted with the danger colour;
* per-drone connection state, battery and the held/auto-land countdown are
  always visible (outside every collapsible section);
* Start keeps its window.confirm.

The server checks start a real UI v2 listener on a free loopback port: it
serves v2 while the main app still serves the current UI, accepts only its
own origin, shares Socket.IO broadcasts and accepts commands. Without the
option no second listener is created.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import flightrisk.dashboard.__main__ as dashboard_main
from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.app import (
    UI_V2_ENVIRON_KEY,
    SourceConfig,
    app,
    make_ui_v2_server,
    run_dashboard,
    socketio_origin_allowed,
)
from flightrisk.dashboard.state import app_state

DASH = Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard"
INCLUDE = '{% include "ceiling_settings.html" %}'
CEILING = (DASH / "templates" / "ceiling_settings.html").read_text()
V1_RAW = (DASH / "templates" / "index.html").read_text()
V1 = V1_RAW.replace(INCLUDE, CEILING)
V2_RAW = (DASH / "templates" / "index_v2.html").read_text()
V2 = V2_RAW.replace(INCLUDE, CEILING)
JS = (DASH / "static" / "v2" / "ui.js").read_text()
CSS = (DASH / "static" / "v2" / "ui.css").read_text()

# Every capability of the current UI and the one v2 control that offers it.
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
    "single-drone pattern": "search-pattern",
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

# Status and readout ids of the current UI that v2 keeps under the same id.
SHARED_STATUS_IDS = (
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
    "ceiling-unavailable", "ceiling-max", "ceiling-msg",
)

V2_SECTIONS = {"fleet", "mission", "ceiling", "video-detection", "session", "advanced"}


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
        self.cards: dict[str, dict] = {}
        self.toggles: dict[str, dict] = {}
        self.bodies: dict[str, dict] = {}
        self.tags: dict[str, str] = {}

    def _record(self, a, tag):
        eid = a.get("id")
        if not eid:
            return
        self.ids.append(eid)
        self.tags[eid] = tag
        self.sections[eid] = [f["card"] for f in self.stack if f["card"]]
        self.in_body[eid] = any(f["body"] for f in self.stack)
        self.ancestors[eid] = [f["id"] for f in self.stack if f["id"]] + [f["tag"] for f in self.stack]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        self._record(a, tag)
        if tag in self.VOID:
            return
        card = a.get("data-card") if "panel-section" in classes else None
        owner = next((f["card"] for f in reversed(self.stack) if f["card"]), None)
        if card:
            self.cards[card] = a
        if "card-toggle" in classes:
            self.toggles[owner] = a
        if "card-body" in classes:
            self.bodies[owner] = a
        self.stack.append({"tag": tag, "id": a.get("id"), "card": card, "body": "card-body" in classes})

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()


def _tree(markup=V2):
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


def test_status_ids_of_the_current_ui_are_kept():
    ids = set(_tree().ids)
    missing = [eid for eid in SHARED_STATUS_IDS if eid not in ids]
    assert not missing, missing


def test_v2_uses_exactly_the_same_socket_events_and_rest_calls():
    v2_code = V2 + JS
    assert _emitted(v2_code) == _emitted(V1)
    assert _listened(v2_code) == _listened(V1)
    assert _fetched(v2_code) == _fetched(V1)
    # Per-session and per-match detail URLs are built from a prefix in both UIs.
    for prefix in ("'/api/sessions/' +", "'/api/matches/' +"):
        assert prefix in V1 and prefix in JS


def test_command_buttons_are_not_repeated():
    # Each manual action and each direct action is bound once, and the two pads never share an id.
    manual = re.findall(r'data-manual="([a-z]+)"', V2)
    direct = re.findall(r'data-direct="([a-z]+)"', V2)
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
    assert re.search(r'id="direct-control"[^>]*\bhidden\b', V2)
    assert re.search(r'id="manual-overlay"[^>]*\bhidden\b', V2)
    assert "$('direct-control').hidden = fleetMode;" in JS
    assert "if (!keyboardControlEnabled || fleetMode) return;" in JS
    # The status event decides the mode; manual status and a fleet status with manual state imply fleet mode.
    assert "setFleetMode(!!(data.fleet && data.fleet.drones > 0));" in JS
    assert JS.count("setFleetMode(true);") == 2


def test_sections_are_the_six_collapsible_groups_with_large_chevrons():
    t = _tree()
    assert set(t.cards) == V2_SECTIONS
    for card in V2_SECTIONS:
        toggle = t.toggles[card]
        assert toggle.get("type") == "button", card
        assert toggle.get("aria-controls") == f"card-{card}-body", card
        assert t.bodies[card].get("id") == f"card-{card}-body", card
    chevron = re.search(r"\.card-chevron\s*\{([^}]*)\}", CSS).group(1)
    assert int(re.search(r"font-size:\s*(\d+)px", chevron).group(1)) >= 20
    for prop in ("min-width", "min-height"):
        assert int(re.search(prop + r":\s*(\d+)px", chevron).group(1)) >= 40
    assert ".card-collapsed > .card-body { display: none; }" in CSS
    assert "prefers-reduced-motion" in CSS


def test_section_state_storage_is_guarded_and_separate_from_the_current_ui():
    helper = JS[JS.index("COLLAPSIBLE SECTIONS"):JS.index("// ========== SOUNDS")]
    assert "'flightrisk.v2.card.'" in helper
    assert helper.count("try {") == 2 and helper.count("catch (e)") == 2


def test_land_all_is_always_visible_outside_every_section():
    t = _tree()
    assert t.ids.count("land-all") == 1
    assert t.sections["land-all"] == [] and t.in_body["land-all"] is False
    assert "header" in t.ancestors["land-all"]
    assert "aside" not in t.ancestors["land-all"] and "main" not in t.ancestors["land-all"]
    assert re.search(r"\.topbar\s*\{[^}]*position:\s*sticky", CSS)
    assert 'aria-keyshortcuts="Shift+L"' in V2


def test_land_all_is_the_only_strong_red_element():
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL)
    rules = re.findall(r"([^{}]+)\{([^}]*var\(--danger\)[^}]*)\}", css)
    selectors = [sel.strip() for sel, _ in rules]
    assert selectors and all(sel.startswith(".land-all") for sel in selectors), selectors
    assert "background: var(--danger)" in CSS


def test_shift_l_handler_matches_the_current_ui():
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
    # The countdown also marks the drone's tile, as in the current UI.
    assert JS.count("'HELD: auto-landing in ' + landIn + ' s, no controller'") == 2
    # The manual panel sits under the tiles in the main area, outside the sidebar.
    assert V2.index('id="video-tiles"') < V2.index('id="manual-overlay"') < V2.index('<aside')
    assert t.sections["manual-overlay"] == []


def test_start_keeps_its_confirmation_and_plan_gate():
    start = JS[JS.index("function startFleet()"):]
    start = start[: start.index("\n    }\n")]
    assert "if (!fleetPlanReceived || fleetStartInFlight) return;" in start
    assert start.index("window.confirm(") < start.index("socket.emit('start_fleet')")
    assert re.search(r'id="fleet-start"[^>]*\bdisabled\b', V2)


def test_manual_takeover_rules_match_the_current_ui():
    take = JS[JS.index("function manualTakeControl"):]
    take = take[: take.index("\n    }\n")]
    assert "if (droneId === manualDroneId) return;" in take
    assert take.index("droneId === manualDroneId") < take.index("socket.emit")
    assert "$('manual-land').disabled = false;" in JS
    assert "'slot_band'" in JS and "d.drone_link_ok === false" in JS


def test_dynamic_values_never_go_through_innerhtml():
    assert "innerHTML" not in JS
    assert "insertAdjacentHTML" not in JS and "document.write" not in JS
    assert "onclick=" not in V2_RAW


def test_light_and_dark_themes_with_one_accent():
    assert "@media (prefers-color-scheme: dark)" in CSS
    assert '<meta name="color-scheme" content="light dark">' in V2
    root = re.search(r":root\s*\{([^}]*)\}", CSS).group(1)
    assert len(re.findall(r"--accent:", root)) == 1


def test_current_ui_is_untouched_by_v2():
    assert "/static/v2/" not in V1_RAW
    assert "index_v2" not in V1_RAW


# ---------------------------------------------------------------- server


def test_main_app_serves_the_current_ui_and_the_v2_mark_serves_v2():
    client = app.test_client()
    main = client.get("/").get_data(as_text=True)
    assert "AMBER" in main and "/static/v2/ui.js" not in main
    v2 = client.get("/", environ_overrides={UI_V2_ENVIRON_KEY: True}).get_data(as_text=True)
    assert "/static/v2/ui.js" in v2 and "<title>FlightRisk</title>" in v2
    # The ceiling card is included in v2 as well.
    assert 'id="ceiling-section"' in v2
    for path in ("/static/v2/ui.js", "/static/v2/ui.css"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        resp.close()


def test_origin_rules_are_per_listener(monkeypatch):
    monkeypatch.delenv("FLIGHTRISK_CORS_ORIGINS", raising=False)
    monkeypatch.setattr(app_module, "_dashboard_bind", ("127.0.0.1", 5600))
    monkeypatch.setattr(app_module, "_ui_v2_port", 5601)
    main, v2 = {}, {UI_V2_ENVIRON_KEY: True}
    assert socketio_origin_allowed("http://127.0.0.1:5600", main)
    assert not socketio_origin_allowed("http://127.0.0.1:5601", main)
    assert socketio_origin_allowed("http://127.0.0.1:5601", v2)
    assert socketio_origin_allowed("http://localhost:5601", v2)
    assert not socketio_origin_allowed("http://127.0.0.1:5600", v2)
    for env in (main, v2):
        assert not socketio_origin_allowed("http://evil.example", env)
        assert not socketio_origin_allowed("http://127.0.0.1:5602", env)
        assert not socketio_origin_allowed(None, env)
    # Without a UI v2 port nothing changes for the main port.
    monkeypatch.setattr(app_module, "_ui_v2_port", None)
    assert socketio_origin_allowed("http://127.0.0.1:5600", main)
    assert not socketio_origin_allowed("http://127.0.0.1:5601", main)


@pytest.fixture
def v2_listener(clean_app_state, monkeypatch):
    """A real UI v2 listener on a free loopback port; the main bind is a port nothing listens on."""
    monkeypatch.delenv("FLIGHTRISK_CORS_ORIGINS", raising=False)
    monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", None)
    # Flask-SocketIO's test client replaces the shared server's packet senders on the instance, so after any
    # earlier test that used it, real clients would never receive a packet. Use the real senders here; the
    # test client's versions are put back afterwards.
    for name in ("_send_packet", "_send_eio_packet"):
        if name in vars(app_module.socketio.server):
            monkeypatch.delattr(app_module.socketio.server, name)
    server = make_ui_v2_server("127.0.0.1", 0)
    port = server.server_port
    monkeypatch.setattr(app_module, "_dashboard_bind", ("127.0.0.1", 1))
    monkeypatch.setattr(app_module, "_ui_v2_port", port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()
    thread.join(timeout=5)


def _handshake(port, origin):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/socket.io/?EIO=4&transport=polling", headers={"Origin": origin})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_listener_serves_v2_and_checks_its_own_origin(v2_listener):
    with urllib.request.urlopen(f"http://127.0.0.1:{v2_listener}/", timeout=5) as resp:
        body = resp.read().decode()
    assert "/static/v2/ui.js" in body and "AMBER" not in body
    assert _handshake(v2_listener, f"http://127.0.0.1:{v2_listener}") == 200
    assert _handshake(v2_listener, f"http://localhost:{v2_listener}") == 200
    assert _handshake(v2_listener, "http://127.0.0.1:1") == 400  # the main port's page connects to the main port
    assert _handshake(v2_listener, "http://evil.example") == 400


def test_listener_shares_broadcasts_and_accepts_commands(v2_listener):
    socketio_client = pytest.importorskip("socketio")
    app_state.fleet_supervisor = None
    app_state.ground_transport = None
    app_state.fleet = None
    received: list[tuple[str, object]] = []
    client = socketio_client.Client()
    client.on("*", lambda event, *args: received.append((event, args[0] if args else None)))
    client.connect(f"http://127.0.0.1:{v2_listener}", transports=["polling"], wait_timeout=10)
    try:
        # A broadcast from the shared Socket.IO server (as the frame loop and the ground do) reaches this client.
        app_module.socketio.emit("ui_v2_probe", {"n": 1})
        # A command from this port is handled by the same app: LAND ALL answers with its report.
        client.emit("land_all")
        deadline = time.time() + 10
        while time.time() < deadline:
            names = [name for name, _ in received]
            if "ui_v2_probe" in names and "land_all_ack" in names:
                break
            time.sleep(0.1)
    finally:
        client.disconnect()
    names = [name for name, _ in received]
    assert "status" in names
    assert ("ui_v2_probe", {"n": 1}) in received
    ack = next(payload for name, payload in received if name == "land_all_ack")
    assert "reached_any" in json.loads(json.dumps(ack))


@pytest.fixture
def no_server(clean_app_state, monkeypatch):
    monkeypatch.delenv("FLIGHTRISK_DASHBOARD_HOST", raising=False)
    monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", None)
    monkeypatch.setattr(app_module, "_init_pipeline", MagicMock())
    monkeypatch.setattr(app_module, "_frame_loop", lambda socketio: None)
    monkeypatch.setattr(app_module, "_ui_v2_port", None)
    monkeypatch.setattr(app_module, "_dashboard_bind", None)
    run = MagicMock()
    monkeypatch.setattr(app_module.socketio, "run", run)
    make = MagicMock()
    monkeypatch.setattr(app_module, "make_ui_v2_server", make)
    return run, make


def test_without_the_option_no_second_listener_starts(no_server):
    run, make = no_server
    run_dashboard(SourceConfig(source="file"), port=5998)
    make.assert_not_called()
    assert app_module._ui_v2_port is None
    run.assert_called_once()
    assert run.call_args.kwargs["port"] == 5998


def test_with_the_option_a_second_listener_starts_on_the_same_host(no_server):
    run, make = no_server
    run_dashboard(SourceConfig(source="file"), port=5998, ui_v2_port=5997)
    make.assert_called_once_with("127.0.0.1", 5997)
    assert app_module._ui_v2_port == 5997
    deadline = time.time() + 5
    while not make.return_value.serve_forever.called and time.time() < deadline:
        time.sleep(0.01)
    make.return_value.serve_forever.assert_called_once()
    assert run.call_args.kwargs["host"] == "127.0.0.1" and run.call_args.kwargs["port"] == 5998


def test_same_port_for_both_uis_is_refused_before_anything_starts(no_server):
    run, make = no_server
    with pytest.raises(ValueError):
        run_dashboard(SourceConfig(source="file"), port=5998, ui_v2_port=5998)
    make.assert_not_called()
    run.assert_not_called()
    app_module._init_pipeline.assert_not_called()


def _run_main(argv):
    with patch.object(sys, "argv", ["flightrisk-dashboard"] + argv):
        dashboard_main.main()


def test_cli_option_and_environment_variable(monkeypatch):
    monkeypatch.delenv("FLIGHTRISK_UI_V2_PORT", raising=False)
    with patch.object(dashboard_main, "run_dashboard") as run:
        _run_main(["--source", "file", "--video", "clip.mp4"])
    # Absent: the call is exactly what it was before UI v2 existed.
    assert "ui_v2_port" not in run.call_args.kwargs
    with patch.object(dashboard_main, "run_dashboard") as run:
        _run_main(["--source", "file", "--video", "clip.mp4", "--ui-v2-port", "5601"])
    assert run.call_args.kwargs["ui_v2_port"] == 5601
    monkeypatch.setenv("FLIGHTRISK_UI_V2_PORT", "5602")
    with patch.object(dashboard_main, "run_dashboard") as run:
        _run_main(["--source", "file", "--video", "clip.mp4"])
    assert run.call_args.kwargs["ui_v2_port"] == 5602
    # The command line wins over the environment.
    with patch.object(dashboard_main, "run_dashboard") as run:
        _run_main(["--source", "file", "--video", "clip.mp4", "--ui-v2-port", "5603"])
    assert run.call_args.kwargs["ui_v2_port"] == 5603


@pytest.mark.parametrize(
    "argv, env",
    [
        (["--ui-v2-port", "5555"], None),
        (["--port", "6000", "--ui-v2-port", "6000"], None),
        (["--ui-v2-port", "0"], None),
        (["--ui-v2-port", "70000"], None),
        ([], "not-a-port"),
    ],
)
def test_cli_rejects_bad_ports(monkeypatch, argv, env):
    monkeypatch.delenv("FLIGHTRISK_PORT", raising=False)
    if env is None:
        monkeypatch.delenv("FLIGHTRISK_UI_V2_PORT", raising=False)
    else:
        monkeypatch.setenv("FLIGHTRISK_UI_V2_PORT", env)
    with patch.object(dashboard_main, "run_dashboard") as run, pytest.raises(SystemExit) as exit_info:
        _run_main(["--source", "file", "--video", "clip.mp4"] + argv)
    assert exit_info.value.code == 2
    run.assert_not_called()
