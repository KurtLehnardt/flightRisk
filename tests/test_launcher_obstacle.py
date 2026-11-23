"""Obstacle sensing mode and depth threshold in the launcher (settings, edge environment, API, window).

* the two settings: defaults, JSON coercion, validation against the edge's own range, persistence in presets
  and the last-used settings, and a preset file from before they existed;
* precedence: ``FLIGHTRISK_OBSTACLE_SENSING`` and ``FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH`` in the launcher's
  environment win over the window, reach the edge unchanged, and are shown as the effective value;
* the edges' environment: the window's values when the environment does not set them, never the ground's;
* ``/api/start`` refuses a start with weaker sensing until the operator confirms that exact mode, before any
  drone is probed or process started;
* the window's page and script carry the controls, the "applies on next Start" note and the confirmation.

The children are stand-in sleepers and no drone is contacted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import replace

import pytest

from flightrisk.drone.obstacle_sensor import (
    ENV_OBSTACLE_MIN_SAFE_DEPTH,
    ENV_OBSTACLE_SENSING,
    OBSTACLE_MIN_SAFE_DEPTH,
    OBSTACLE_MIN_SAFE_DEPTH_RANGE,
)
from flightrisk.edge_device import ObstacleSettings
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.launcher.commands import EdgeToken, Redactor, build_plan
from flightrisk.launcher.preflight import INFO, PASS, WARN, obstacle_threshold_check, run_preflight
from flightrisk.launcher.processes import ProcessManager
from flightrisk.launcher.server import STATIC_DIR, LauncherApp, make_server
from flightrisk.launcher.settings import (
    ERROR,
    DroneSettings,
    WARNING,
    LauncherSettings,
    PresetStore,
    UNCALIBRATED_DEPTH_NOTE,
    SettingsError,
    builtin_presets,
    effective_obstacle,
    format_depth,
    obstacle_env,
    sensing_warning,
    validate,
)
from tests.test_launcher_server import SLEEPER, TOKEN, Client, _preflight_env, _wait

PY = "/venv/bin/python"
REHEARSAL = builtin_presets()["Rehearsal with fake drones"]
PROFILES = ("default",)


def _issues(settings, environ=None, level=None):
    found = validate(settings, profile_names=PROFILES, environ=environ or {}, is_windows=False)
    return [i for i in found if (level is None or i.level == level) and i.field.startswith("obstacle")]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_defaults_are_the_edges_defaults_and_every_preset_starts_required():
    assert (LauncherSettings().obstacle_sensing, LauncherSettings().obstacle_min_safe_depth) == ("required", 0.35)
    assert OBSTACLE_MIN_SAFE_DEPTH == 0.35 and OBSTACLE_MIN_SAFE_DEPTH_RANGE == (0.05, 0.9)
    for name, preset in builtin_presets().items():
        assert preset.obstacle_sensing == "required", name
        assert preset.obstacle_min_safe_depth == OBSTACLE_MIN_SAFE_DEPTH, name


def test_a_settings_file_from_before_the_obstacle_settings_loads_with_the_defaults():
    old = REHEARSAL.to_json()
    del old["obstacle_sensing"], old["obstacle_min_safe_depth"]
    loaded = LauncherSettings.from_json(old)
    assert (loaded.obstacle_sensing, loaded.obstacle_min_safe_depth) == ("required", 0.35)


@pytest.mark.parametrize("raw, value", [
    (0.77, 0.77), ("0.77", 0.77), (" 0.5 ", 0.5), (1, 1.0), (0.123456, 0.123), ("7.7e-1", 0.77),
])
def test_the_depth_is_read_as_a_number(raw, value):
    assert LauncherSettings.from_json({**REHEARSAL.to_json(), "obstacle_min_safe_depth": raw}).obstacle_min_safe_depth == value


@pytest.mark.parametrize("raw", ["abc", "0,77", "5_0", "nan", "inf", True, None, "", [0.5], float("nan"), float("inf")])
def test_a_depth_that_is_not_a_number_is_refused(raw):
    with pytest.raises(SettingsError, match="obstacle_min_safe_depth"):
        LauncherSettings.from_json({**REHEARSAL.to_json(), "obstacle_min_safe_depth": raw})


def test_the_mode_is_read_case_insensitively_and_a_non_text_mode_is_refused():
    assert LauncherSettings.from_json({**REHEARSAL.to_json(), "obstacle_sensing": "OFF"}).obstacle_sensing == "off"
    with pytest.raises(SettingsError, match="obstacle_sensing"):
        LauncherSettings.from_json({**REHEARSAL.to_json(), "obstacle_sensing": 0})


@pytest.mark.parametrize("field, value, message", [
    ("obstacle_sensing", "stepped", "obstacle sensing must be required or off"),
    ("obstacle_sensing", "forward_only", "obstacle sensing must be required or off"),
    ("obstacle_min_safe_depth", 0.04, "min safe depth must be from 0.05 to 0.9"),
    ("obstacle_min_safe_depth", 0.95, "min safe depth must be from 0.05 to 0.9"),
    ("obstacle_min_safe_depth", 0.0, "min safe depth must be from 0.05 to 0.9"),
])
def test_values_outside_the_edges_range_are_errors(field, value, message):
    (issue,) = _issues(replace(REHEARSAL, **{field: value}), level=ERROR)
    assert issue.field == field and message in issue.message


@pytest.mark.parametrize("depth", [0.05, 0.35, 0.77, 0.9])
def test_every_valid_window_value_is_one_the_edge_accepts(depth):
    """The launcher's range is the edge's: what the window lets through, the edge starts with."""
    settings = replace(REHEARSAL, obstacle_min_safe_depth=depth)
    assert not _issues(settings, level=ERROR)
    env = obstacle_env(settings, {})
    parsed = ObstacleSettings("tello", SafetyEnvelope(alt_slot_cm=100), env)
    assert (parsed.mode, parsed.min_safe_depth) == ("required", depth)


def test_off_and_a_raised_depth_are_warnings_not_errors():
    off = _issues(replace(REHEARSAL, obstacle_sensing="off"))
    assert [i.level for i in off] == [WARNING] and "no obstacle protection" in off[0].message
    raised = _issues(replace(REHEARSAL, obstacle_min_safe_depth=0.77))
    assert [i.level for i in raised] == [WARNING] and "above the default 0.35" in raised[0].message
    assert _issues(REHEARSAL) == []


def test_the_settings_persist_in_presets_and_the_last_used_settings(tmp_path):
    path = tmp_path / "launcher.json"
    store = PresetStore(path)
    field = replace(REHEARSAL, obstacle_sensing="off", obstacle_min_safe_depth=0.77)
    store.save_preset("Field", field)
    store.save_last(field)
    raw = json.loads(path.read_text())
    assert raw["presets"]["Field"]["obstacle_sensing"] == "off"
    assert raw["presets"]["Field"]["obstacle_min_safe_depth"] == 0.77
    again = PresetStore(path)
    assert again.presets()["Field"] == field and again.last() == field


@pytest.mark.parametrize("value, text", [(0.77, "0.77"), (0.5, "0.5"), (0.05, "0.05"), (0.9, "0.9"), (0.123, "0.123")])
def test_the_depth_reaches_the_edge_as_a_plain_decimal(value, text):
    assert format_depth(value) == text


# ---------------------------------------------------------------------------
# Precedence and the edges' environment
# ---------------------------------------------------------------------------


def test_without_the_variables_the_edges_get_the_windows_values_and_the_ground_does_not():
    settings = replace(REHEARSAL, obstacle_sensing="off", obstacle_min_safe_depth=0.77)
    ground, *edges = build_plan(settings, EdgeToken(TOKEN, "environment"), PY, {})
    assert ENV_OBSTACLE_SENSING not in ground.env and ENV_OBSTACLE_MIN_SAFE_DEPTH not in ground.env
    for edge in edges:
        assert edge.env[ENV_OBSTACLE_SENSING] == "off" and edge.env[ENV_OBSTACLE_MIN_SAFE_DEPTH] == "0.77"
        assert edge.display_env[ENV_OBSTACLE_SENSING] == "off"
        assert edge.display_env[ENV_OBSTACLE_MIN_SAFE_DEPTH] == "0.77"
    eff = effective_obstacle(settings, {})
    assert (eff.mode, eff.mode_source, eff.depth, eff.depth_source) == ("off", "window", 0.77, "window")


def test_an_inherited_variable_wins_and_reaches_the_edge_unchanged():
    settings = replace(REHEARSAL, obstacle_sensing="required", obstacle_min_safe_depth=0.35)
    env = {ENV_OBSTACLE_MIN_SAFE_DEPTH: "0.77", "PATH": "/bin"}
    _, edge, _ = build_plan(settings, EdgeToken(TOKEN, "environment"), PY, env)
    assert edge.env[ENV_OBSTACLE_MIN_SAFE_DEPTH] == "0.77"
    assert ENV_OBSTACLE_MIN_SAFE_DEPTH not in edge.display_env  # the launcher did not set it
    assert edge.env[ENV_OBSTACLE_SENSING] == "required"  # the window's, as the environment has none
    eff = effective_obstacle(settings, env)
    assert (eff.depth, eff.depth_source, eff.mode_source) == (0.77, "environment", "window")
    (issue,) = [i for i in _issues(settings, env) if i.field == "obstacle_min_safe_depth" and "wins" in i.message]
    assert issue.level == WARNING and "wins over the window's 0.35" in issue.message


def test_an_empty_inherited_variable_counts_as_unset():
    settings = replace(REHEARSAL, obstacle_min_safe_depth=0.6)
    env = {ENV_OBSTACLE_MIN_SAFE_DEPTH: "  ", ENV_OBSTACLE_SENSING: ""}
    _, edge, _ = build_plan(settings, EdgeToken(TOKEN, "environment"), PY, env)
    assert edge.env[ENV_OBSTACLE_MIN_SAFE_DEPTH] == "0.6" and edge.env[ENV_OBSTACLE_SENSING] == "required"
    assert effective_obstacle(settings, env).depth_source == "window"


def test_an_inherited_off_is_the_effective_mode_and_is_never_silent():
    settings = replace(REHEARSAL, obstacle_sensing="required")
    env = {ENV_OBSTACLE_SENSING: "OFF"}
    eff = effective_obstacle(settings, env)
    assert (eff.mode, eff.mode_source, eff.downgraded) == ("off", "environment", True)
    messages = [i.message for i in _issues(settings, env)]
    assert any("wins over the window's required" in m for m in messages)
    assert any("no obstacle protection" in m for m in messages)
    warning = sensing_warning(eff, ("drone-1", "drone-2"))
    assert "OFF for the edges this launcher starts (drone-1, drone-2)" in warning
    assert ENV_OBSTACLE_SENSING in warning and "wins over the window" in warning


@pytest.mark.parametrize("env, field", [
    ({ENV_OBSTACLE_SENSING: "maybe"}, "obstacle_sensing"),
    ({ENV_OBSTACLE_MIN_SAFE_DEPTH: "0.95"}, "obstacle_min_safe_depth"),
    ({ENV_OBSTACLE_MIN_SAFE_DEPTH: "abc"}, "obstacle_min_safe_depth"),
    ({ENV_OBSTACLE_MIN_SAFE_DEPTH: "nan"}, "obstacle_min_safe_depth"),
])
def test_an_inherited_value_the_edge_would_refuse_blocks_the_start(env, field):
    errors = _issues(REHEARSAL, env, level=ERROR)
    assert [i.field for i in errors] == [field]
    assert "the edges would refuse to start" in errors[0].message


def test_without_a_local_edge_the_environment_is_not_checked():
    remote_only = builtin_presets()["Layout A (ground here)"]
    assert not remote_only.local_drones()
    assert _issues(remote_only, {ENV_OBSTACLE_SENSING: "maybe"}) == []


def test_the_sensing_warning_is_full_sentences_about_weaker_protection():
    eff = effective_obstacle(replace(REHEARSAL, obstacle_sensing="off"), {})
    text = sensing_warning(eff, ("drone-1",))
    assert text.startswith("Obstacle sensing will be OFF for the edges this launcher starts (drone-1).")
    assert "no obstacle protection" in text and text.endswith(".")
    assert all(sentence[0].isupper() for sentence in text.split(". ") if sentence)


# ---------------------------------------------------------------------------
# The API: confirm before weaker sensing starts
# ---------------------------------------------------------------------------


@pytest.fixture
def launcher_with(tmp_path):
    """A launcher over HTTP whose inherited environment the test chooses."""
    made = []

    def _make(environ=None):
        manager = ProcessManager(
            Redactor(TOKEN), log_dir=tmp_path / f"logs{len(made)}", cwd=tmp_path,
            popen=lambda argv, **kw: subprocess.Popen([sys.executable, "-c", SLEEPER], **kw),
            port_open=lambda host, port: True, edge_grace_s=5.0, ground_grace_s=5.0, poll_s=0.05,
        )
        probes = []
        env = _preflight_env()
        app = LauncherApp(
            port=0, store=PresetStore(tmp_path / f"launcher{len(made)}.json", secret=lambda: TOKEN),
            token_loader=lambda: EdgeToken(TOKEN, "environment"), manager=manager,
            preflight_env=replace(env, tello_probe=lambda d: probes.append(d) or env.tello_probe(d)),
            python=sys.executable, environ={"PATH": os.environ.get("PATH", ""), **(environ or {})},
        )
        server, session = make_server(app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        made.append((manager, server))
        return Client(app.port, session), app, probes

    yield _make
    for manager, server in made:
        manager.stop_all()
        server.shutdown()
        server.server_close()


def _settings(**changes):
    return replace(REHEARSAL, **changes).to_json()


def test_start_with_sensing_off_is_refused_until_confirmed(launcher_with):
    client, app, _ = launcher_with()
    body = {"settings": _settings(obstacle_sensing="off")}
    status, data = client.post("/api/start", body)
    assert status == 409
    assert data["needs_confirmation"] == "obstacle_sensing" and data["mode"] == "off"
    assert data["mode_source"] == "window"
    assert data["error"].startswith("Obstacle sensing will be OFF for the edges this launcher starts (drone-1, drone-2).")
    assert app.manager.snapshot() == []  # nothing started
    # A confirmation of anything but the exact mode is no confirmation.
    for wrong in (True, "yes", "required", "OFF", None):
        assert client.post("/api/start", {**body, "confirm_sensing": wrong})[0] == 409
    status, data = client.post("/api/start", {**body, "confirm_sensing": "off"})
    assert status == 200 and data["started"] is True, data
    assert _wait(lambda: [p["status"] for p in client.get("/api/state")[1]["processes"]] == ["running"] * 3)
    state = client.get("/api/state")[1]
    assert state["running_obstacle"]["mode"] == "off" and state["running_obstacle"]["downgraded"] is True
    assert any("obstacle sensing off confirmed by the operator for drone-1, drone-2" in e for e in state["events"])
    edges = [p for p in state["processes"] if p["name"].startswith("edge-")]
    assert all(p["env"][ENV_OBSTACLE_SENSING] == "off" for p in edges)


def test_the_refusal_comes_before_any_drone_is_probed(launcher_with):
    client, _, probes = launcher_with()
    settings = _settings(obstacle_sensing="off", drones=(
        DroneSettings(drone_id="drone-1", source="tello", local_ip="192.168.10.2", alt_slot_cm=100),))
    status, data = client.post("/api/start", {"settings": settings})
    assert status == 409 and probes == []


def test_required_needs_no_confirmation(launcher_with):
    client, _, _ = launcher_with()
    status, data = client.post("/api/start", {"settings": _settings(obstacle_min_safe_depth=0.77)})
    assert status == 200 and data["started"] is True
    assert _wait(lambda: len(client.get("/api/state")[1]["processes"]) == 3)
    state = client.get("/api/state")[1]
    edges = [p for p in state["processes"] if p["name"].startswith("edge-")]
    assert all(p["env"][ENV_OBSTACLE_MIN_SAFE_DEPTH] == "0.77" for p in edges)
    assert state["running_obstacle"]["min_safe_depth"] == 0.77


def test_an_inherited_off_needs_the_same_confirmation(launcher_with):
    client, _, _ = launcher_with({ENV_OBSTACLE_SENSING: "off"})
    status, data = client.post("/api/start", {"settings": _settings(obstacle_sensing="required")})
    assert status == 409 and data["mode"] == "off" and data["mode_source"] == "environment"
    assert ENV_OBSTACLE_SENSING in data["error"]
    status, data = client.post("/api/start", {"settings": _settings(), "confirm_sensing": "off"})
    assert status == 200 and data["started"] is True


def test_a_start_with_no_local_edge_needs_no_confirmation(launcher_with):
    client, app, _ = launcher_with({ENV_OBSTACLE_SENSING: "off"})
    remote = builtin_presets()["Layout A (ground here)"]
    app._require_sensing_confirmation(replace(remote, obstacle_sensing="off"), {})  # does not raise


def test_validate_and_bootstrap_show_the_effective_values(launcher_with):
    client, _, _ = launcher_with({ENV_OBSTACLE_MIN_SAFE_DEPTH: "0.77"})
    status, data = client.post("/api/validate", {"settings": _settings(obstacle_sensing="off")})
    assert status == 200
    ob = data["obstacle"]
    assert (ob["mode"], ob["mode_source"], ob["min_safe_depth"], ob["min_safe_depth_source"]) == (
        "off", "window", 0.77, "environment")
    assert ob["edges"] == ["drone-1", "drone-2"] and ob["downgraded"] is True
    assert ob["applies"] == "on the next Start" and ob["warning"].startswith("Obstacle sensing will be OFF")
    assert ENV_OBSTACLE_MIN_SAFE_DEPTH in data["inherited_env"]
    boot = client.get("/api/bootstrap")[1]
    assert boot["obstacle_limits"] == {"modes": ["required", "off"], "min_safe_depth_range": [0.05, 0.9],
                                       "min_safe_depth_default": 0.35}
    assert boot["settings"]["obstacle_sensing"] == "required"
    assert client.get("/api/state")[1]["running_obstacle"] is None


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

PAGE = (STATIC_DIR / "launcher.html").read_text(encoding="utf-8")
SCRIPT = (STATIC_DIR / "launcher.js").read_text(encoding="utf-8")


def test_the_page_has_both_controls_and_says_when_a_change_applies():
    assert 'data-field="obstacle_sensing"' in PAGE and 'data-field="obstacle_min_safe_depth"' in PAGE
    assert '<option value="required">' in PAGE and '<option value="off">' in PAGE
    assert 'min="0.05" max="0.9"' in PAGE
    assert "Applies on next Start." in PAGE
    assert "wins over this window" in PAGE


def test_the_script_asks_before_a_weaker_mode_and_sends_the_confirmation():
    # Choosing off goes through chooseSensing, which asks; the generic field wiring skips the mode.
    assert 'if (key === "obstacle_sensing") continue;' in SCRIPT
    assert "async function chooseSensing(select)" in SCRIPT
    assert 'select.value = settings.obstacle_sensing;' in SCRIPT  # Cancel puts the old mode back
    assert "confirm_sensing: sensingConfirmed" in SCRIPT
    assert 'e.data.needs_confirmation === "obstacle_sensing"' in SCRIPT
    # Loading a preset forgets an earlier confirmation, so a saved "off" is asked again.
    load = SCRIPT[SCRIPT.index("function loadPreset()"):SCRIPT.index("async function savePreset()")]
    assert "sensingConfirmed = null;" in load
    assert "Applies on next Start." in SCRIPT


# ---------------------------------------------------------------------------
# Preflight: a threshold that was never calibrated
# ---------------------------------------------------------------------------


def test_the_shipped_threshold_with_sensing_required_is_a_preflight_warning():
    check = obstacle_threshold_check(REHEARSAL, {})
    assert (check.name, check.status) == ("Obstacle threshold", WARN)
    assert check.detail == UNCALIBRATED_DEPTH_NOTE
    assert check.detail.startswith("The obstacle threshold has not been calibrated (H1), so lanes will be cut on "
                                   "every gate until it is set from recordings.")
    assert "0.35" in check.detail and check.detail.endswith(".")


@pytest.mark.parametrize("settings, environ, status, detail", [
    (replace(REHEARSAL, obstacle_min_safe_depth=0.77), {}, PASS, "min safe depth 0.77 (from the window)"),
    (REHEARSAL, {ENV_OBSTACLE_MIN_SAFE_DEPTH: "0.77"}, PASS,
     "min safe depth 0.77 (from FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH in the launcher's environment)"),
    (replace(REHEARSAL, obstacle_min_safe_depth=0.77), {ENV_OBSTACLE_MIN_SAFE_DEPTH: "0.35"}, WARN,
     UNCALIBRATED_DEPTH_NOTE),
    (replace(REHEARSAL, obstacle_sensing="off"), {}, INFO, "is not used: obstacle sensing is off"),
])
def test_the_threshold_check_follows_the_effective_values(settings, environ, status, detail):
    check = obstacle_threshold_check(settings, environ)
    assert check.status == status and detail in check.detail


def test_the_warning_is_in_the_preflight_and_never_blocks_the_start(launcher_with):
    checks = run_preflight(REHEARSAL, EdgeToken(TOKEN, "environment"), _preflight_env(), probe_drones=False)
    (row,) = [c for c in checks if c.name == "Obstacle threshold"]
    assert row.status == WARN
    remote_only = run_preflight(builtin_presets()["Layout A (ground here)"], EdgeToken(TOKEN, "environment"),
                                _preflight_env(), probe_drones=False)
    assert not [c for c in remote_only if c.name == "Obstacle threshold"]  # no edge starts there
    client, _, _ = launcher_with()
    status, data = client.post("/api/start", {"settings": REHEARSAL.to_json()})
    assert status == 200 and data["started"] is True
    (row,) = [c for c in data["checks"] if c["name"] == "Obstacle threshold"]
    assert row["status"] == "WARN" and row["detail"] == UNCALIBRATED_DEPTH_NOTE


def test_the_window_shows_the_same_note_next_to_the_threshold(launcher_with):
    client, _, _ = launcher_with()
    ob = client.post("/api/validate", {"settings": REHEARSAL.to_json()})[1]["obstacle"]
    assert ob["uncalibrated"] is True and ob["uncalibrated_note"] == UNCALIBRATED_DEPTH_NOTE
    ob = client.post("/api/validate", {"settings": _settings(obstacle_min_safe_depth=0.77)})[1]["obstacle"]
    assert ob["uncalibrated"] is False and ob["uncalibrated_note"] is None
    ob = client.post("/api/validate", {"settings": _settings(obstacle_sensing="off")})[1]["obstacle"]
    assert ob["uncalibrated_note"] is None
    assert 'id="obstacle-uncalibrated"' in PAGE
    assert 'note.textContent = ob.uncalibrated_note || "";' in SCRIPT
