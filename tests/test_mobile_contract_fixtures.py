"""The mobile contract fixtures are what the real Python code produces, and they stay safe to ship.

``tests/fixtures/mobile_contract/`` is the one contract the iOS and Android
apps test their decoders against (``docs/mobile-contract.md``). These tests:

* regenerate every fixture with ``scripts/export_mobile_contract_fixtures.py``
  in a temporary directory and fail when the result differs from the committed
  files (only ``_meta.generated_at_commit`` and ``_meta.library_versions`` may
  differ; the versions are checked against the lock files instead), so a change
  to any payload the phones read shows up here first;
* check the structure both apps rely on: every file parses, has a ``_meta``
  block and named cases, keeps the case names the apps reference, and stays
  small;
* check that nothing in a fixture is or looks like a secret;
* check that the spec document and the dashboard UI it quotes still agree.

Nothing here contacts a drone, an edge or a running ground station.
"""
from __future__ import annotations

import itertools
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
DEMO_ASSET = ROOT / "mobile" / "app" / "src" / "main" / "assets" / "demo" / "demo_scenario.json"
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
        "start_refused_drone_unreachable", "drone_lost", "retask", "retask_failed", "retask_needs_confirmation",
        "land_all", "land_all_failed", "profile_switch_pending",
    },
    "fleet_plan.json": {"two_drones", "map_plan_lost_zone_uncovered"},
    "fleet_detection.json": {"new_detection"},
    "frame.json": {"edge_video_with_match", "edge_detections_only"},
    "match_alert.json": {
        "edge_match", "reminder_on_connect", "resolved_dismiss", "resolved_confirm", "edge_match_awaiting_reasoning",
        "reminder_with_verdict",
    },
    "reasoning_result.json": {
        "match", "no_match", "uncertain", "error", "error_with_detail", "skipped", "description_check",
    },
    "alert_upgrade.json": {"confirmed_match"},
    "demo_scenario.json": {"scenario"},
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
        "detections_appearance_only", "detections_with_face", "detections_no_video", "detections_reid_embedding",
        "telemetry_v1",
        "telemetry_v2_drone_link_down", "ack_assign_lanes", "nack_assign_lanes_geofence", "nack_v2_drone_unreachable",
        "profile_ack_fallback", "profile_nack_in_flight", "ceiling_ack", "ceiling_nack", "manual_ack", "manual_nack",
        "pong",
    },
    "nack_reasons.json": {
        "assign_lanes_v1", "fenced_command_v2", "set_detection_profile", "profile_fallback", "set_ceiling",
        "manual_command", "control_modes", "hold_reasons", "edge_states",
    },
    "catalogue.json": {"builtin_models"},
    "client_emits.json": set(),  # checked by test_client_emit_allow_list
    "match_score_vectors.json": set(),  # checked by tests/test_match_score_vectors.py
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
    """The document without what legitimately differs between machines: the commit and the installed library versions.

    Library versions drift whenever a venv is newer than the lock files (installer/locks). What the phones depend on
    is the frames themselves, which are still compared in full, so the versions are only checked for being recorded.
    """
    out = json.loads(json.dumps(doc))
    out["_meta"].pop("generated_at_commit", None)
    out["_meta"].pop("library_versions", None)
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


LIBRARY_PACKAGES = ("python-engineio", "python-socketio", "flask-socketio")
LOCKS = ("macos-arm64.txt", "windows-x64-ground.txt")


def _locked_version(lock: str, package: str) -> str:
    text = (ROOT / "installer" / "locks" / lock).read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(package)}==([^\s\\;]+)", text, re.MULTILINE)
    assert match, f"{package} is not pinned in installer/locks/{lock}"
    return match.group(1)


@pytest.mark.parametrize("lock", LOCKS)
def test_the_committed_library_versions_are_the_ones_the_lock_files_pin(lock):
    """Independent of the installed venv: the fixture was generated with the versions the installers ship."""
    recorded = _committed()["socketio_frames.json"]["_meta"]["library_versions"]
    assert set(recorded) == set(LIBRARY_PACKAGES)
    assert recorded == {package: _locked_version(lock, package) for package in LIBRARY_PACKAGES}, (
        "regenerate the fixtures from a venv built from the lock files, or update the lock files"
    )


def test_installed_library_major_versions_match_the_committed_fixture(regenerated):
    """Minor drift in the venv is fine; a new major version of the wire libraries would change the protocol."""
    committed = _committed()["socketio_frames.json"]
    now = regenerated["socketio_frames.json"]["_meta"]["library_versions"]
    recorded = committed["_meta"]["library_versions"]
    assert {k: v.split(".")[0] for k, v in now.items()} == {k: v.split(".")[0] for k, v in recorded.items()}
    assert recorded["python-engineio"].startswith("4.") and recorded["python-socketio"].startswith("5.")
    assert "EIO=4" in committed["_meta"]["description"]
    assert regenerated["socketio_frames.json"]["cases"]["engineio_open"] == committed["cases"]["engineio_open"]


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


def test_reid_embedding_sample_matches_the_ground_model_and_is_scored():
    """A phone edge sends appearance embeddings in exactly this form (docs/mobile-contract.md, "Phone as an edge")."""
    import math

    import numpy as np

    from flightrisk.config import VisionConfig
    from flightrisk.edge import EdgeRunner
    from flightrisk.ground import GroundStation
    from flightrisk.vision import reid_spec as rs

    doc = _load(FIXTURES / "edge_edge_to_ground.json")
    spec = doc["_meta"]["reid_embedding"]
    assert spec == {
        "library": rs.REID_LIBRARY, "model": rs.REID_DEFAULT_MODEL, "pretrained": rs.REID_PRETRAINED,
        "dimension": rs.REID_EMBEDDING_DIM, "dtype": "float32", "l2_normalized": True,
        "input_size": rs.REID_INPUT_SIZE,
        "resize": "shortest side to input_size (bicubic), then center crop input_size x input_size",
        "mean": list(rs.REID_MEAN), "std": list(rs.REID_STD),
    }
    assert VisionConfig().reid_model == rs.REID_DEFAULT_MODEL

    payload = doc["cases"]["detections_reid_embedding"]["payload"]
    vector = payload["detections"][0]["reid_embedding"]
    assert len(vector) == spec["dimension"] == 512
    assert all(isinstance(v, float) and math.isfinite(v) for v in vector)
    assert all(float(np.float32(v)) == v for v in vector), "every value is a float32 value"
    assert abs(math.sqrt(math.fsum(v * v for v in vector)) - 1.0) < 1e-6
    assert payload["detections"][1]["reid_embedding"] is None
    assert set(payload["detections"][0]) == {"bbox", "confidence", "reid_embedding", "crop"}

    scored = GroundStation(target_reid_embedding=vector).process_message(EdgeRunner.from_dict(payload))
    assert scored[0]["reid_score"] == pytest.approx(1.0, abs=1e-5)
    assert scored[1]["reid_score"] == 0.0
    # A vector from another model of a different length is not comparable and scores 0, never an error.
    short = dict(payload, detections=[dict(payload["detections"][0], reid_embedding=vector[:256])])
    assert GroundStation(target_reid_embedding=vector).process_message(EdgeRunner.from_dict(short))[0]["reid_score"] == 0.0


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
# The reasoning verdict and the demo scenario
# ---------------------------------------------------------------------------

LEVEL_RANK = {"no_match": 0, "weak_signal": 1, "possible_match": 2, "confirmed_match": 3}


def test_verdict_events_carry_no_image_and_are_keyed_by_the_alert():
    alerts = _load(FIXTURES / "match_alert.json")["cases"]
    verdicts = _load(FIXTURES / "reasoning_result.json")["cases"]
    upgrade = _load(FIXTURES / "alert_upgrade.json")["cases"]["confirmed_match"]["payload"]
    awaiting = alerts["edge_match_awaiting_reasoning"]["payload"]
    assert awaiting["gemma_confidence"] == "pending" and awaiting["reasoning"] == "Awaiting Claude reasoning..."
    assert verdicts["match"]["payload"]["alert_id"] == awaiting["alert_id"] == upgrade["alert_id"]
    assert LEVEL_RANK[upgrade["new_level"]] > LEVEL_RANK[awaiting["alert_level"]]
    reminder = alerts["reminder_with_verdict"]["payload"]
    assert reminder["alert_id"] == awaiting["alert_id"]
    assert (reminder["gemma_confidence"], reminder["gemma_match"], reminder["alert_level"]) == ("high", True, "confirmed_match")
    expected_confidence = {
        "match": "high", "no_match": "high", "uncertain": "low", "error": "error", "error_with_detail": "error",
        "skipped": "skipped", "description_check": "medium",
    }
    ids = set()
    for name, entry in verdicts.items():
        payload = entry["payload"]
        assert entry["event"] == "reasoning_result"
        assert set(payload["result"]) == {"match", "confidence", "reasoning"}, name
        assert payload["result"]["confidence"] == expected_confidence[name], name
        if name == "description_check":
            assert payload["type"] == "description" and "alert_id" not in payload
        else:
            assert set(payload) == {"alert_id", "result", "track_id", "type"} and payload["type"] == "analyze", name
            ids.add(payload["alert_id"])
    assert len(ids) == 6, "each analyze case is its own alert"
    assert verdicts["error"]["payload"]["result"]["reasoning"] == ""
    assert set(upgrade) == {"alert_id", "new_level", "reasoning", "track_id"}
    for doc in ("reasoning_result.json", "alert_upgrade.json"):
        text = (FIXTURES / doc).read_text(encoding="utf-8")
        assert "/9j/" not in text and "snapshot" not in text, doc


def test_demo_scenario_is_a_timed_mission_of_contract_events():
    from flightrisk.dashboard.app import socketio

    doc = _load(FIXTURES / "demo_scenario.json")
    scenario = doc["cases"]["scenario"]["payload"]
    steps = scenario["steps"]
    times = [step["at_ms"] for step in steps]
    assert times == sorted(times) and times[0] == 0
    assert doc["_meta"]["duration_ms"] == times[-1] and 40_000 <= times[-1] <= 90_000
    assert len(steps) <= 40
    events = [step["event"] for step in steps]
    for needed in ("status", "fleet_status", "fleet_plan", "fleet_event", "fleet_detection", "match_alert",
                   "reasoning_result", "alert_upgrade", "match_alert_resolved"):
        assert needed in events, needed
    kinds = [step["payload"].get("event") for step in steps if step["event"] == "fleet_event"]
    assert kinds.index("start") < kinds.index("lane_blocked") < kinds.index("land_all")
    alert = next(step for step in steps if step["event"] == "match_alert")
    verdict = next(step for step in steps if step["event"] == "reasoning_result")
    upgrade = next(step for step in steps if step["event"] == "alert_upgrade")
    assert alert["payload"]["alert_id"] == verdict["payload"]["alert_id"] == upgrade["payload"]["alert_id"]
    assert alert["at_ms"] < verdict["at_ms"] <= upgrade["at_ms"]
    assert verdict["payload"]["result"]["match"] is True
    # Drones fly between the start and LAND ALL, and are on the ground at the end.
    flying = [step for step in steps if step["event"] == "fleet_status"
              and all(d["is_flying"] for d in step["payload"]["drones"].values())]
    assert len(flying) >= 5
    assert all(not d["is_flying"] for d in scenario["landed_fleet_status"]["drones"].values())
    assert all(not d["is_flying"] for d in steps[-1]["payload"]["drones"].values())
    # Gaps stay inside the phones' 30 s fleet_status freshness limit.
    status_times = [step["at_ms"] for step in steps if step["event"] == "fleet_status"]
    assert max(b - a for a, b in itertools.pairwise(status_times)) < 30_000
    assert set(scenario["state"]) == {"detection_profiles", "ceiling_settings", "reasoning_provider", "manual_status"}
    assert set(scenario["replies"]) == {"land_all", "drone_command", "manual_resume_search"}
    assert set(scenario["replies"]) <= set(ALLOWED)
    handled = set(socketio.server.handlers["/"])
    assert set(scenario["replies"]) <= handled



def test_demo_scenario_ids_are_demo_ids_and_a_drone_is_held():
    """Every id the demo sends starts with demo- (as on iOS), and one drone hovers HELD so Resume search can be tried."""
    scenario = _load(FIXTURES / "demo_scenario.json")["cases"]["scenario"]["payload"]
    steps = scenario["steps"]
    drone_ids = set()
    for step in steps:
        payload = step["payload"]
        drone_ids.update((payload.get("drones") or {}).keys())
        if isinstance(payload.get("drone_id"), str):
            drone_ids.add(payload["drone_id"])
    drone_ids.update(scenario["landed_fleet_status"]["drones"])
    drone_ids.update(scenario["state"]["manual_status"]["drones"])
    assert drone_ids == {"demo-drone-1", "demo-drone-2"}
    for name in ("match_alert", "reasoning_result", "alert_upgrade", "match_alert_resolved"):
        step = next(step for step in steps if step["event"] == name)
        assert step["payload"]["alert_id"] == "demo-alert-1", name
    for name in ("reasoning_result", "alert_upgrade"):
        assert next(step for step in steps if step["event"] == name)["payload"]["track_id"] == "demo-track-1"
    text = json.dumps(scenario)
    assert re.search(r'(?<![\w-])drone-[0-9]', text) is None, "a fixture drone id was not renamed"
    held = scenario["held"]
    assert held["drone_id"] == "demo-drone-2" and held["from_ms"] < held["until_ms"]
    assert held["until_ms"] - held["from_ms"] >= 12_000, "the hold must be long enough to try Resume search"
    statuses = [step for step in steps if step["event"] == "fleet_status"]
    in_hold = [s for s in statuses if held["from_ms"] <= s["at_ms"] < held["until_ms"]]
    assert len(in_hold) >= 3
    for step in in_hold:
        drone = step["payload"]["drones"]["demo-drone-2"]
        assert drone["control_mode"] == "held" and drone["is_flying"] is True and drone["orphan_land_in_s"] > 0
        assert step["payload"]["manual_controlled"] is None, "nobody controls the held drone, so a phone may resume it"
    land_all_at = next(step["at_ms"] for step in steps
                       if step["event"] == "fleet_event" and step["payload"]["event"] == "land_all")
    assert land_all_at == held["until_ms"]
    after = [s for s in statuses if s["at_ms"] > land_all_at]
    assert after and all(d["control_mode"] == "autonomous" and d["orphan_land_in_s"] is None
                         for s in after for d in s["payload"]["drones"].values())


def test_the_android_demo_asset_is_the_fixture():
    """The Android app ships a copy of demo_scenario.json; it must be the fixture (copy it after a regenerate)."""
    assert DEMO_ASSET.is_file(), f"missing {DEMO_ASSET}; copy tests/fixtures/mobile_contract/demo_scenario.json there"
    assert _without_commit(_load(DEMO_ASSET)) == _without_commit(_load(FIXTURES / "demo_scenario.json")), (
        "the Android demo asset differs from the fixture; run: cp tests/fixtures/mobile_contract/demo_scenario.json "
        "mobile/app/src/main/assets/demo/demo_scenario.json"
    )


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
    for event in ALLOWED | NEVER | {"reasoning_result", "alert_upgrade", "match_alert_resolved"}:
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


# ---------------------------------------------------------------------------
# catalogue.json: model facts only, never a Tello tuning value
# ---------------------------------------------------------------------------


def _catalogue_payload() -> dict:
    return _load(FIXTURES / "catalogue.json")["cases"]["builtin_models"]["payload"]


def test_catalogue_has_the_builtin_models_cameras_and_feature_reasons():
    from flightrisk.hardware.catalogue import BUILTIN_MODELS, SUPPORT_LEVELS
    from flightrisk.hardware.features import FEATURE_IDS

    payload = _catalogue_payload()
    assert set(payload) == {"models", "features", "support_levels"}
    assert set(payload["models"]) == set(BUILTIN_MODELS)
    assert payload["support_levels"] == list(SUPPORT_LEVELS)
    assert set(payload["features"]) == set(FEATURE_IDS)
    for model_id, model in payload["models"].items():
        assert model["support"] in SUPPORT_LEVELS
        assert model["cameras"], model_id
        assert set(model["features"]) == set(FEATURE_IDS)
        for state in model["features"].values():
            assert state["available"] == (state["why"] is None)


def test_tello_params_fixture_is_intentionally_not_produced():
    assert not (FIXTURES / "tello_params.json").exists()
    assert "tello_params.json" in DOC.read_text(encoding="utf-8")


def test_catalogue_carries_no_registry_fact_row_id_or_fact_value():
    from flightrisk.params.spec import Edit
    from flightrisk.params.table import TABLE

    fact_ids = {row.id for row in TABLE if Edit.FACT in set(row.edit.values())}
    assert fact_ids, "the registry has no fact rows; the check would pass for nothing"
    fact_keys = fact_ids | {fact_id.rsplit(".", 1)[-1] for fact_id in fact_ids}
    fact_values = {
        value for row in TABLE if Edit.FACT in set(row.edit.values())
        for value in row.defaults.values() if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    forbidden_parts = ("threshold", "tuning", "settle", "tello_param")

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key, child
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    camera_numbers = {"stream_w_px", "stream_h_px", "hfov_deg", "fps"}
    for key, child in walk(_catalogue_payload()):
        assert key not in fact_keys, f"catalogue key {key!r} is a registry fact row id"
        assert not any(part in key.lower() for part in forbidden_parts), key
        if isinstance(child, str):
            assert child not in fact_ids, f"catalogue value {child!r} is a registry fact row id"
        if (isinstance(child, (int, float)) and not isinstance(child, bool) and child in fact_values
                and key not in camera_numbers):
            pytest.fail(f"catalogue number {key!r}={child!r} equals a fact row default")
    raw = (FIXTURES / "catalogue.json").read_text(encoding="utf-8")
    assert not any(f'"{fact_id}"' in raw for fact_id in fact_ids)
