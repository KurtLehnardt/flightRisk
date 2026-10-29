"""The mobile contract fixtures are what the real Python code produces, and they stay safe to ship.

``tests/fixtures/mobile_contract/`` is the one contract the iOS and Android
apps test their decoders against (``docs/mobile-contract.md``). These tests:

* regenerate every fixture with ``scripts/export_mobile_contract_fixtures.py``
  in a temporary directory and fail when the result differs from the committed
  files (only ``_meta.generated_at_commit`` may differ), so a change to any
  payload the phones read shows up here first;
* check the structure both apps rely on: every file parses, has a ``_meta``
  block and named cases, keeps the case names the apps reference, and stays
  small;
* check that nothing in a fixture is or looks like a secret;
* check that the spec document and the dashboard UI it quotes still agree.

Nothing here contacts a drone, an edge or a running ground station.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from flightrisk import fleet_protocol as fp

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "mobile_contract"
SCRIPT = ROOT / "scripts" / "export_mobile_contract_fixtures.py"
DOC = ROOT / "docs" / "mobile-contract.md"
UI_JS = ROOT / "flightrisk" / "dashboard" / "static" / "ui" / "ui.js"

#: The file and case names the apps reference. Renaming one breaks both apps, so it must break here too.
REQUIRED_CASES = {
    "socketio_frames.json": {
        "engineio_open", "socketio_connect_with_auth", "socketio_connect_without_auth", "socketio_connect_ack",
        "socketio_connect_error", "socketio_event_client_to_server", "socketio_event_client_to_server_with_payload",
        "socketio_event_server_to_client", "socketio_event_with_ack_id", "socketio_ack", "engineio_ping",
        "engineio_pong", "socketio_disconnect", "engineio_close",
    },
    "status.json": {"fleet_two_drones", "no_fleet_sim_source"},
    "fleet_status.json": {
        "all_connected", "one_down", "one_lost", "one_held_orphan_countdown", "one_manual", "drone_link_down",
        "non_fleet_local_drone", "non_fleet_no_drone",
    },
    "fleet_event.json": {
        "plan_refused_unconnected", "plan_refused_manual_control", "plan_invalidated", "start", "start_refused",
        "start_refused_drone_unreachable", "drone_lost", "retask", "retask_failed", "land_all", "land_all_failed",
        "profile_switch_pending",
    },
    "fleet_plan.json": {"two_drones", "map_plan_lost_zone_uncovered"},
    "fleet_detection.json": {"new_detection"},
    "frame.json": {"edge_video_with_match", "edge_detections_only"},
    "match_alert.json": {"edge_match", "reminder_on_connect"},
    "detection_profiles.json": {"pending", "fallback", "refused", "unsupported", "standby_controls_disabled", "no_board"},
    "ceiling_settings.json": {
        "on_ground_in_sync", "edge_pending", "edge_refused", "slot_above_limit", "locked_in_flight", "unavailable",
    },
    "reasoning_provider.json": {"local_gemma_cloud_blocked", "off", "cloud_allowed_no_key", "cloud_allowed"},
    "manual_status.json": {"held_orphan_countdown", "manual_controlled"},
    "manual_ack.json": {"land_ok", "land_unknown_drone", "resume_search_ok", "resume_search_not_manual"},
    "manual_result.json": {"land_acked", "resume_search_acked", "land_no_reply"},
    "land_all_ack.json": {"fleet_reached_all", "local_fleet", "reached_no_drone"},
    "command_ack.json": {"land"},
    "stream_video_state.json": {"off"},
    "error.json": {
        "land_all_reached_no_drone", "manual_needs_fleet_mode", "profiles_need_edge_source", "no_drones_connected",
        "manual_refusal",
    },
    "edge_ground_to_edge.json": {
        "ground_hello", "heartbeat_v1", "heartbeat_v2", "clock", "ping", "set_target", "stream_video", "land",
        "emergency_land_all",
    },
    "edge_edge_to_ground.json": {
        "detections_appearance_only", "detections_with_face", "detections_no_video", "telemetry_v1",
        "telemetry_v2_drone_link_down", "ack_assign_lanes", "nack_assign_lanes_geofence", "nack_v2_drone_unreachable",
        "profile_ack_fallback", "profile_nack_in_flight", "ceiling_ack", "ceiling_nack", "manual_ack", "manual_nack",
        "pong",
    },
    "nack_reasons.json": {
        "assign_lanes_v1", "fenced_command_v2", "set_detection_profile", "profile_fallback", "set_ceiling",
        "manual_command", "control_modes", "hold_reasons", "edge_states",
    },
    "client_emits.json": set(),  # checked by test_client_emit_allow_list
    "unknown_keys.json": set(),  # checked by test_unknown_key_variants_differ_only_by_the_unknown_key
}

#: The only events a phone may emit (docs/mobile-contract.md, "Events a phone may emit").
ALLOWED = {
    "get_fleet_status", "get_detection_profiles", "get_ceiling_settings", "get_reasoning_provider",
    "get_manual_status", "land_all", "drone_command", "set_detection_profile", "set_fleet_detection_profile",
    "manual_resume_search",
}
NEVER = {
    "plan_fleet", "start_fleet", "manual_take_control", "manual_command", "set_ceiling_settings",
    "set_reasoning_provider", "set_stream_video",
}
DIRECTIONS = {"server_to_client", "client_to_server", "ground_to_edge", "edge_to_ground", "reference"}

SECRET_KEY_RE = re.compile(r"token|secret|passw|api_?key|credential|authoriz|bearer|private_?key|cookie", re.IGNORECASE)
SECRET_VALUE_RES = (
    re.compile(r"sk-ant-"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"Bearer\s+\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
MAX_FILE_BYTES = 256 * 1024


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _committed() -> dict[str, dict]:
    return {path.name: _load(path) for path in sorted(FIXTURES.glob("*.json"))}


def _without_commit(doc: dict) -> dict:
    out = json.loads(json.dumps(doc))
    out["_meta"].pop("generated_at_commit", None)
    return out


def _walk(value, path=()):
    """Yield (path, key, value) for every key and (path, None, value) for every scalar."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield path, key, item
            yield from _walk(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, path + (index,))
    else:
        yield path, None, value


@pytest.fixture(scope="module")
def regenerated(tmp_path_factory) -> dict[str, dict]:
    out = tmp_path_factory.mktemp("mobile_contract")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out)],
        cwd=ROOT, capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    return {path.name: _load(path) for path in sorted(out.glob("*.json"))}


# ---------------------------------------------------------------------------
# Golden: the committed fixtures are what the code produces today
# ---------------------------------------------------------------------------


def test_regenerated_fixtures_match_the_committed_files(regenerated):
    committed = _committed()
    assert sorted(regenerated) == sorted(committed), (
        "the fixture set changed; run: venv/bin/python scripts/export_mobile_contract_fixtures.py"
    )
    changed = [name for name in committed if _without_commit(committed[name]) != _without_commit(regenerated[name])]
    assert not changed, (
        f"these fixtures no longer match what the code produces: {changed}. If the change is intended, "
        "regenerate them (venv/bin/python scripts/export_mobile_contract_fixtures.py), update "
        "docs/mobile-contract.md and tell both app teams."
    )


def test_committed_files_are_rendered_deterministically():
    """Sorted keys, two-space indent, one trailing newline: a regenerate is a clean diff."""
    for path in sorted(FIXTURES.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert text == json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n", path.name


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_every_expected_file_and_case_exists():
    committed = _committed()
    assert set(committed) == set(REQUIRED_CASES)
    for name, cases in REQUIRED_CASES.items():
        missing = cases - set(committed[name]["cases"])
        assert not missing, f"{name} lost cases {sorted(missing)}"


@pytest.mark.parametrize("name", sorted(REQUIRED_CASES))
def test_file_has_meta_and_well_formed_cases(name):
    doc = _load(FIXTURES / name)
    assert set(doc) == {"_meta", "cases"}
    meta = doc["_meta"]
    for key in ("description", "file", "generated_at_commit", "regenerate", "sources", "spec"):
        assert meta.get(key), f"{name}: _meta.{key} is missing"
    assert meta["file"] == name
    assert meta["regenerate"] == "venv/bin/python scripts/export_mobile_contract_fixtures.py"
    assert meta["spec"] == "docs/mobile-contract.md"
    assert re.fullmatch(r"[0-9a-f]{40}|unknown", meta["generated_at_commit"])
    assert doc["cases"], f"{name} has no cases"
    for case_name, entry in doc["cases"].items():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", case_name), case_name
        assert entry["direction"] in DIRECTIONS, (name, case_name)
        assert ("payload" in entry) != ("frame" in entry), (name, case_name)
        assert isinstance(entry["notes"], str)


def test_files_are_small():
    for path in sorted(FIXTURES.glob("*.json")):
        assert path.stat().st_size < MAX_FILE_BYTES, path.name


def test_edge_link_samples_validate_and_fit_the_message_cap():
    g2e = _load(FIXTURES / "edge_ground_to_edge.json")["cases"]
    for name, entry in g2e.items():
        fp.validate_ground_to_edge(entry["payload"], proto=2 if name in ("ground_hello", "heartbeat_v2") else 1)
    e2g = _load(FIXTURES / "edge_edge_to_ground.json")["cases"]
    for name, entry in e2g.items():
        fp.validate_edge_to_ground(entry["payload"], proto=2 if "v2" in name else 1)
        size = len(json.dumps(entry["payload"], separators=(",", ":")).encode())
        assert size <= fp.LEGACY_MAX_BYTES, name
    phone = e2g["detections_appearance_only"]["payload"]
    assert phone["type"] == "detections"
    assert "drone_id" not in phone and "detection_profile" not in phone
    assert all("face_embedding" not in det for det in phone["detections"])


def test_nack_reasons_are_the_protocol_constants():
    cases = _load(FIXTURES / "nack_reasons.json")["cases"]
    assert cases["assign_lanes_v1"]["payload"] == sorted(fp.NACK_REASONS)
    assert cases["fenced_command_v2"]["payload"] == sorted(fp.NACK_REASONS_V2)
    assert cases["set_detection_profile"]["payload"] == sorted(fp.PROFILE_NACK_REASONS)
    assert cases["set_ceiling"]["payload"] == sorted(fp.CEILING_NACK_REASONS)
    assert cases["manual_command"]["payload"] == sorted(fp.MANUAL_NACK_REASONS)
    assert cases["control_modes"]["payload"] == sorted(fp.CONTROL_MODES)


def test_client_emit_allow_list():
    from flightrisk.dashboard.app import socketio

    cases = _load(FIXTURES / "client_emits.json")["cases"]
    allowed = {entry["event"] for entry in cases.values() if entry["allowed"]}
    forbidden = {entry["event"] for entry in cases.values() if not entry["allowed"]}
    assert allowed == ALLOWED
    assert NEVER <= forbidden
    assert not allowed & forbidden
    handlers = set(socketio.server.handlers["/"])
    assert allowed | forbidden <= handlers, "every listed event must exist on the dashboard"
    drone_command = cases["drone_command"]["payload"]
    assert drone_command == {"command": "land", "drone_id": "drone-1"}


def test_unknown_key_variants_differ_only_by_the_unknown_key():
    doc = _load(FIXTURES / "unknown_keys.json")
    key = doc["_meta"]["unknown_key"]
    map_keys = set(doc["_meta"]["map_keys"])

    def strip(value, parent=None):
        if isinstance(value, list):
            return [strip(item) for item in value]
        if isinstance(value, dict):
            if parent in map_keys:
                assert key not in value
                return {k: strip(v) for k, v in value.items()}
            assert value.get(key) == "ignore me", "every record object carries the unknown key"
            return {k: strip(v, k) for k, v in value.items() if k != key}
        return value

    for name, entry in doc["cases"].items():
        source = _load(FIXTURES / entry["source_file"])["cases"][entry["source_case"]]
        assert strip(entry["payload"]) == source["payload"], name


# ---------------------------------------------------------------------------
# No secrets
# ---------------------------------------------------------------------------


def test_no_key_is_named_like_a_secret():
    for path in sorted(FIXTURES.glob("*.json")):
        for where, key, _value in _walk(_load(path)):
            if key is not None:
                assert not SECRET_KEY_RE.search(str(key)), f"{path.name}: key {key!r} at {where}"


def test_no_value_looks_like_a_secret():
    for path in sorted(FIXTURES.glob("*.json")):
        for where, key, value in _walk(_load(path)):
            if not isinstance(value, str):
                continue
            for pattern in SECRET_VALUE_RES:
                assert not pattern.search(value), f"{path.name}: {where} matches {pattern.pattern}"
            # The one credential-shaped string is the auth frame, and it carries the fake key.
            if "api_key" in value:
                assert re.fullmatch(r'40\{"api_key":"test-key"\}', value), f"{path.name}: {where}"


def test_no_real_host_or_home_path_leaks():
    for path in sorted(FIXTURES.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert "192.168.10.1" not in text, path.name
        assert "/Users/" not in text and "\\Users\\" not in text, path.name


# ---------------------------------------------------------------------------
# The spec document and the UI it quotes
# ---------------------------------------------------------------------------


def test_spec_names_every_fixture_file_and_event():
    text = DOC.read_text(encoding="utf-8")
    for name in REQUIRED_CASES:
        assert f"tests/fixtures/mobile_contract/{name}" in text or f"`{name}`" in text, name
    for event in ALLOWED | NEVER:
        assert f"`{event}`" in text, event
    for secret_name in ("FLIGHTRISK_DASHBOARD_API_KEY", "FLIGHTRISK_EDGE_TOKEN", "FLIGHTRISK_CLAUDE_API_KEY"):
        assert secret_name in text


def test_the_ui_derivations_the_spec_copies_are_unchanged():
    """docs/mobile-contract.md copies these rules from ui.js; a change there must update the spec."""
    js = UI_JS.read_text(encoding="utf-8")
    for line in (
        "if (d.lost === true) return { text: 'LOST', key: 'lost' };",
        "if (d.connected === false) return { text: 'DOWN', key: 'down' };",
        "if (d.connected === true) return { text: 'CONNECTED', key: 'connected' };",
        "const landIn = typeof d.orphan_land_in_s === 'number' ? d.orphan_land_in_s : null;",
        "if (landIn !== null) return { text: 'HELD: auto-landing in ' + landIn + ' s, no controller', cls: 'held' };",
        "if (id && id === manualDroneId) return { text: 'MANUAL: under your control', cls: 'manual' };",
        "if (d.control_mode === 'held' || d.edge_control_mode === 'held') return { text: 'HELD: hovering, search paused', cls: 'held' };",
        "if (d.control_mode === 'manual') return { text: 'MANUAL', cls: 'manual' };",
        "if (d.drone_link_ok === false) return 'Drone link down: ' + (d.drone_link_detail || 'the edge cannot reach the drone') + '. Land still works.';",
        "manualDroneId = data.manual_controlled || null;",
    ):
        assert line in js, line
