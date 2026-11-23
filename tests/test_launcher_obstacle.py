"""Obstacle sensing mode and depth threshold in the launcher (settings, edge environment, API, window).

* the two settings: defaults, JSON coercion, validation against the edge's own range, persistence in presets
  and the last-used settings, and a preset file from before they existed;
* precedence: ``FLIGHTRISK_OBSTACLE_SENSING`` and ``FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH`` in the launcher's
  environment win over the window, reach the edge unchanged, and are shown as the effective value;
* the edges' environment: the window's values when the environment does not set them, never the ground's;
* ``/api/start`` refuses a start with weaker sensing until the operator confirms that exact mode with the one-time
  nonce of the refusal, before any drone is probed or process started; a confirmation is never replayed;
* inherited ``FLIGHTRISK_TELLO_TEMP_WARN_C`` and ``..._REFUSE_C`` the edges would refuse block Start, and the drone
  probe marks a hot drone;
* the window's script (``chooseSensing``, ``startRequest``, ``renderObstacle``) runs under Node with stand-ins for
  the page and the API; the page carries the controls and the "applies on next Start" note.

The children are stand-in sleepers and no drone is contacted.
"""

from __future__ import annotations

import json
import os
import shutil
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
from flightrisk.drone.temp_preflight import ENV_TELLO_TEMP_REFUSE_C, ENV_TELLO_TEMP_WARN_C
from flightrisk.launcher.commands import EdgeToken, Redactor, build_plan
from flightrisk.launcher.preflight import INFO, PASS, WARN, TelloProbe, obstacle_threshold_check, run_preflight
from flightrisk.launcher.processes import ProcessManager
from flightrisk.launcher.server import SENSING_NONCE_TTL_S, STATIC_DIR, LauncherApp, make_server
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


@pytest.mark.parametrize("raw", [0.9004, "0.9004", 0.0496, 0.04999])
def test_rounding_never_pulls_a_depth_into_the_range(raw):
    """A value outside 0.05 to 0.9 is kept as it is, so validation refuses it instead of the rounded value."""
    settings = LauncherSettings.from_json({**REHEARSAL.to_json(), "obstacle_min_safe_depth": raw})
    assert settings.obstacle_min_safe_depth == float(raw)
    (issue,) = _issues(settings, level=ERROR)
    assert "min safe depth must be from 0.05 to 0.9" in issue.message


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


def _refused(client, body):
    """POST /api/start expecting the sensing refusal; its one-time nonce."""
    status, data = client.post("/api/start", body)
    assert status == 409 and data["needs_confirmation"] == "obstacle_sensing", data
    assert isinstance(data["confirm_nonce"], str) and len(data["confirm_nonce"]) >= 16
    return data["confirm_nonce"]


def _stopped(client):
    assert client.post("/api/stop", {"confirm": True})[0] == 200
    assert _wait(lambda: not client.get("/api/state")[1]["busy"])


def test_start_with_sensing_off_is_refused_until_confirmed_with_the_refusals_nonce(launcher_with):
    client, app, _ = launcher_with()
    body = {"settings": _settings(obstacle_sensing="off")}
    status, data = client.post("/api/start", body)
    assert status == 409
    assert data["needs_confirmation"] == "obstacle_sensing" and data["mode"] == "off"
    assert data["mode_source"] == "window"
    assert data["error"].startswith("Obstacle sensing will be OFF for the edges this launcher starts (drone-1, drone-2).")
    assert app.manager.snapshot() == []  # nothing started
    nonce = data["confirm_nonce"]
    # A confirmation of anything but the exact mode is no confirmation, and it uses up the nonce it presents.
    for wrong in (True, "yes", "required", "OFF", None):
        used, nonce = nonce, _refused(client, {**body, "confirm_sensing": wrong, "confirm_nonce": nonce})
    # The exact mode without a nonce, with one this launcher never issued, or with a used one, is not enough either.
    _refused(client, {**body, "confirm_sensing": "off"})
    _refused(client, {**body, "confirm_sensing": "off", "confirm_nonce": "made-up-nonce-0123456789"})
    _refused(client, {**body, "confirm_sensing": "off", "confirm_nonce": used})
    status, data = client.post("/api/start", {**body, "confirm_sensing": "off", "confirm_nonce": nonce})
    assert status == 200 and data["started"] is True, data
    assert _wait(lambda: [p["status"] for p in client.get("/api/state")[1]["processes"]] == ["running"] * 3)
    state = client.get("/api/state")[1]
    assert state["running_obstacle"]["mode"] == "off" and state["running_obstacle"]["downgraded"] is True
    assert any("obstacle sensing off confirmed by the operator for drone-1, drone-2" in e for e in state["events"])
    edges = [p for p in state["processes"] if p["name"].startswith("edge-")]
    assert all(p["env"][ENV_OBSTACLE_SENSING] == "off" for p in edges)


def test_a_confirmed_start_cannot_be_replayed_after_stop(launcher_with):
    """Confirm, start, stop, then the very same start request again: refused, and asked again."""
    client, app, _ = launcher_with()
    body = {"settings": _settings(obstacle_sensing="off")}
    confirmed = {**body, "confirm_sensing": "off", "confirm_nonce": _refused(client, body)}
    status, data = client.post("/api/start", confirmed)
    assert status == 200 and data["started"] is True
    _stopped(client)
    status, data = client.post("/api/start", confirmed)
    assert status == 409 and data["needs_confirmation"] == "obstacle_sensing"
    assert data["confirm_nonce"] != confirmed["confirm_nonce"]


def _restart(client, name):
    return client.post("/api/restart", {"name": name})


def _edge_name(client):
    return next(p["name"] for p in client.get("/api/state")[1]["processes"] if p["name"].startswith("edge-"))


def _all_running(client):
    return _wait(lambda: all(p["status"] == "running" for p in client.get("/api/state")[1]["processes"]))


def test_restart_relaunches_a_sensing_off_edge_during_its_confirmed_run_only(launcher_with):
    """Regression (round 2 critic probe): after Stop, Restart must not relaunch an edge with sensing off unasked."""
    client, app, _ = launcher_with()
    body = {"settings": _settings(obstacle_sensing="off")}
    status, data = client.post("/api/start", {**body, "confirm_sensing": "off", "confirm_nonce": _refused(client, body)})
    assert status == 200 and data["started"] is True
    assert _all_running(client)
    edge = _edge_name(client)
    # During the confirmed run (an edge that crashed, say) Restart is allowed.
    status, data = _restart(client, edge)
    assert status == 200 and data == {"restarting": edge}
    assert _all_running(client)
    _stopped(client)
    status, data = _restart(client, edge)
    assert status == 409, data
    assert data["error"].endswith("Press Start to confirm sensing off again.")
    assert data["needs_confirmation"] == "obstacle_sensing" and data["mode"] == "off"
    assert not client.get("/api/state")[1]["busy"]  # nothing was relaunched
    # A new confirmed Start opens a new run, in which Restart works again.
    status, data = client.post("/api/start", {**body, "confirm_sensing": "off", "confirm_nonce": _refused(client, body)})
    assert status == 200 and data["started"] is True
    assert _all_running(client)
    assert _restart(client, edge)[0] == 200


def test_restart_of_an_edge_with_an_inherited_off_is_held_back_after_stop_too(launcher_with):
    client, _, _ = launcher_with({ENV_OBSTACLE_SENSING: "off"})
    body = {"settings": _settings()}
    status, _ = client.post("/api/start", {**body, "confirm_sensing": "off", "confirm_nonce": _refused(client, body)})
    assert status == 200 and _all_running(client)
    edge = _edge_name(client)
    _stopped(client)
    assert _restart(client, edge)[0] == 409


def test_restart_after_stop_is_not_held_back_with_sensing_required(launcher_with):
    client, _, _ = launcher_with()
    status, data = client.post("/api/start", {"settings": _settings(obstacle_min_safe_depth=0.77)})
    assert status == 200 and _all_running(client)
    edge = _edge_name(client)
    _stopped(client)
    assert _restart(client, edge)[0] == 200


def test_stop_forgets_an_outstanding_nonce(launcher_with):
    client, app, _ = launcher_with()
    body = {"settings": _settings(obstacle_sensing="off")}
    nonce = _refused(client, body)
    assert client.post("/api/stop", {"confirm": True})[0] == 200
    _refused(client, {**body, "confirm_sensing": "off", "confirm_nonce": nonce})


def test_a_nonce_expires_after_five_minutes(launcher_with):
    client, app, _ = launcher_with()
    now = [1000.0]
    app._clock = lambda: now[0]
    body = {"settings": _settings(obstacle_sensing="off")}
    nonce = _refused(client, body)
    now[0] += SENSING_NONCE_TTL_S + 1
    nonce = _refused(client, {**body, "confirm_sensing": "off", "confirm_nonce": nonce})
    now[0] += SENSING_NONCE_TTL_S - 1  # still valid
    status, data = client.post("/api/start", {**body, "confirm_sensing": "off", "confirm_nonce": nonce})
    assert status == 200 and data["started"] is True
    assert SENSING_NONCE_TTL_S == 300


def test_a_nonce_only_confirms_the_edges_it_was_issued_for(launcher_with):
    client, _, _ = launcher_with()
    nonce = _refused(client, {"settings": _settings(obstacle_sensing="off")})
    one = _settings(obstacle_sensing="off", drones=(
        DroneSettings(drone_id="drone-1", source="fake", alt_slot_cm=100),))
    _refused(client, {"settings": one, "confirm_sensing": "off", "confirm_nonce": nonce})


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
    status, data = client.post("/api/start", {"settings": _settings(), "confirm_sensing": "off",
                                              "confirm_nonce": data["confirm_nonce"]})
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


# ---------------------------------------------------------------------------
# Preflight: a threshold that was never calibrated
# ---------------------------------------------------------------------------


def test_the_shipped_threshold_with_sensing_required_is_a_preflight_warning():
    check = obstacle_threshold_check(REHEARSAL, {})
    assert (check.name, check.status) == ("Obstacle threshold", WARN)
    assert check.detail == UNCALIBRATED_DEPTH_NOTE
    assert check.detail.startswith("The obstacle threshold has not been calibrated (H1), so lanes may be cut on "
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


# ---------------------------------------------------------------------------
# The edges' temperature levels and the drone probe
# ---------------------------------------------------------------------------


def _temp_issues(settings, environ):
    found = validate(settings, profile_names=PROFILES, environ=environ, is_windows=False)
    return [i for i in found if i.field == "tello_temp"]


@pytest.mark.parametrize("environ, named", [
    ({ENV_TELLO_TEMP_WARN_C: "abc"}, "FLIGHTRISK_TELLO_TEMP_WARN_C='abc' must be a whole number"),
    ({ENV_TELLO_TEMP_REFUSE_C: "200"}, "FLIGHTRISK_TELLO_TEMP_REFUSE_C='200' must be a whole number"),
    ({ENV_TELLO_TEMP_REFUSE_C: "70"}, "the refusal level (70 C) is below the warn level (92 C)"),
])
def test_an_inherited_temperature_level_the_edge_would_refuse_blocks_the_start(environ, named):
    (issue,) = _temp_issues(REHEARSAL, environ)
    assert issue.level == ERROR
    assert named in issue.message
    assert "in the launcher's environment" in issue.message and "the edges would refuse to start" in issue.message


def test_good_or_absent_temperature_levels_and_a_computer_without_edges_pass():
    assert _temp_issues(REHEARSAL, {}) == []
    assert _temp_issues(REHEARSAL, {ENV_TELLO_TEMP_WARN_C: "60", ENV_TELLO_TEMP_REFUSE_C: "off"}) == []
    remote_only = builtin_presets()["Layout A (ground here)"]
    assert _temp_issues(remote_only, {ENV_TELLO_TEMP_WARN_C: "abc"}) == []


def test_a_bad_inherited_temperature_level_starts_nothing(launcher_with):
    client, app, probes = launcher_with({ENV_TELLO_TEMP_REFUSE_C: "hot"})
    status, data = client.post("/api/start", {"settings": REHEARSAL.to_json()})
    assert status == 200 and data["started"] is False
    assert [i["field"] for i in data["issues"] if i["level"] == "error"] == ["tello_temp"]
    assert app.manager.snapshot() == [] and probes == []


@pytest.mark.parametrize("temp, status, note", [
    ("53~56C", PASS, None),
    ("90~91C", PASS, None),
    ("92~93C", WARN, "at or above the 92 C warn level: power it on last or set it on a fan"),
    ("96~98℃", WARN, "at or above the 95 C refusal level: its edge refuses the takeoff until it cools"),
    ("error", PASS, None),
    (None, PASS, None),
])
def test_the_drone_probe_marks_a_hot_drone(temp, status, note):
    from tests.test_launcher_server import _preflight_env as env_with

    settings = replace(REHEARSAL, drones=(
        DroneSettings(drone_id="drone-1", source="tello", local_ip="192.168.10.2", alt_slot_cm=100),))
    env = env_with(lambda d: TelloProbe(True, battery=77, temp=temp, detail="fake"))
    (row,) = [c for c in run_preflight(settings, EdgeToken(TOKEN, "environment"), env, probe_drones=True)
              if c.name == "Drone drone-1"]
    assert row.status == status
    assert (note in row.detail) if note else ("level" not in row.detail)


# ---------------------------------------------------------------------------
# The window's script under Node
# ---------------------------------------------------------------------------

NODE = shutil.which("node")
JS_HARNESS = r"""
const fs = require('fs');
const block = fs.readFileSync(process.argv[2], 'utf8');
const scenario = fs.readFileSync(process.argv[3], 'utf8');
function fakeEl(id) {
  return { id, value: '', disabled: false, hidden: false, textContent: '', className: '', title: '', children: [],
    replaceChildren(...c) { this.children = c; }, appendChild(c) { this.children.push(c); return c; } };
}
const elements = {};
const $ = (sel) => elements[sel] || (elements[sel] = fakeEl(sel));
const el = (tag, attrs, text) => ({ tag, attrs: attrs || {}, textContent: text === undefined || text === null ? '' : String(text) });
const calls = [], asks = [], changes = [];
let responses = [], answers = [];
async function api(path, body) {
  calls.push({ path, body: body === undefined ? null : JSON.parse(JSON.stringify(body)) });
  const r = responses.shift();
  if (!r) throw new Error('no scripted answer for ' + path);
  if (r.status !== 200) {
    const e = new Error((r.data && r.data.error) || ('HTTP ' + r.status));
    e.status = r.status; e.data = r.data; throw e;
  }
  return r.data;
}
async function ask(title, text, buttons) { asks.push({ title, text }); return answers.shift() ? { value: 'confirm' } : null; }
function changed(rerender) { changes.push(!!rerender); }
const settings = { obstacle_sensing: 'required', obstacle_min_safe_depth: 0.35 };
const f = new Function('$', 'el', 'api', 'ask', 'changed', 'settings', block +
  '\nreturn { chooseSensing, startRequest, renderObstacle, renderRunningObstacle, forgetSensingConfirmation, ' +
  'state: () => ({ sensingConfirmed, lastObstacle }) };')($, el, api, ask, changed, settings);
const script = (r, a) => { responses = r; answers = a || []; };
const run = new Function('f', '$', 'settings', 'calls', 'asks', 'changes', 'script',
  'return (async () => {\n' + scenario + '\n})();');
run(f, $, settings, calls, asks, changes, script).then(
  (out) => console.log(JSON.stringify(out === undefined ? null : out)),
  (e) => { console.error(e && e.stack ? e.stack : e); process.exit(1); });
"""


def _launcher_block() -> str:
    return SCRIPT[SCRIPT.index("// BEGIN obstacle-sensing"):SCRIPT.index("// END obstacle-sensing")]


def _node(tmp_path, scenario: str):
    block = tmp_path / "block.js"
    block.write_text(_launcher_block(), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(JS_HARNESS, encoding="utf-8")
    run = tmp_path / "scenario.js"
    run.write_text(scenario, encoding="utf-8")
    out = subprocess.run([NODE, str(harness), str(block), str(run)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


REFUSAL = {"status": 409, "data": {"error": "Obstacle sensing will be OFF ...", "needs_confirmation": "obstacle_sensing",
                                   "mode": "off", "mode_source": "window", "edges": ["drone-1", "drone-2"],
                                   "confirm_nonce": "n1"}}
#: /api/validate's answer while the mode control asks: the same two edges as REFUSAL.
VALIDATED = {"status": 200, "data": {"obstacle": {"warning": "The launcher warning.", "edges": ["drone-1", "drone-2"]}}}
STARTED = {"status": 200, "data": {"started": True}}


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
class TestWindowScript:
    def test_choosing_off_asks_and_cancel_keeps_required(self, tmp_path):
        out = _node(tmp_path, """
            const select = $('#obstacle_sensing');
            select.value = 'off';
            script([{ status: 200, data: { obstacle: { warning: 'The launcher warning.' } } }], [false]);
            await f.chooseSensing(select);
            const cancelled = { mode: settings.obstacle_sensing, shown: select.value, confirmed: f.state().sensingConfirmed };
            select.value = 'off';
            script([{ status: 200, data: { obstacle: { warning: 'The launcher warning.' } } }], [true]);
            await f.chooseSensing(select);
            const accepted = { mode: settings.obstacle_sensing, confirmed: f.state().sensingConfirmed };
            select.value = 'required';
            await f.chooseSensing(select);
            return { cancelled, accepted, back: { mode: settings.obstacle_sensing, confirmed: f.state().sensingConfirmed },
                     asks, calls: calls.map((c) => c.path) };
        """)
        assert out["cancelled"] == {"mode": "required", "shown": "required", "confirmed": None}
        assert out["accepted"] == {"mode": "off", "confirmed": {"mode": "off", "edges": None}}  # no edges answered
        assert out["back"] == {"mode": "required", "confirmed": None}  # choosing required again asks nothing
        assert [a["text"] for a in out["asks"]] == ["The launcher warning."] * 2
        assert out["calls"] == ["/api/validate", "/api/validate"]

    def test_a_required_start_sends_no_confirmation(self, tmp_path):
        out = _node(tmp_path, """
            script([{ status: 200, data: { started: true } }]);
            const res = await f.startRequest();
            return { res, calls, asks };
        """)
        assert out["res"] == {"started": True} and out["asks"] == []
        assert out["calls"] == [{"path": "/api/start", "body": {"settings": {"obstacle_sensing": "required",
                                                                              "obstacle_min_safe_depth": 0.35}}}]

    def test_the_refusal_is_asked_and_the_nonce_echoed_once(self, tmp_path):
        out = _node(tmp_path, f"""
            script([{json.dumps(REFUSAL)}, {json.dumps(STARTED)}], [true]);
            const res = await f.startRequest();
            return {{ res, calls, asks, confirmed: f.state().sensingConfirmed }};
        """)
        assert out["res"] == {"started": True}
        assert len(out["asks"]) == 1 and out["asks"][0]["text"] == "Obstacle sensing will be OFF ..."
        first, second = out["calls"]
        assert "confirm_sensing" not in first["body"] and "confirm_nonce" not in first["body"]
        assert (second["body"]["confirm_sensing"], second["body"]["confirm_nonce"]) == ("off", "n1")
        assert out["confirmed"] is None

    def test_cancel_starts_nothing(self, tmp_path):
        out = _node(tmp_path, f"""
            script([{json.dumps(REFUSAL)}], [false]);
            const res = await f.startRequest();
            return {{ res, calls: calls.length }};
        """)
        assert out == {"res": None, "calls": 1}

    def test_a_confirmation_in_the_mode_control_counts_for_the_next_start_only(self, tmp_path):
        out = _node(tmp_path, f"""
            const select = $('#obstacle_sensing');
            select.value = 'off';
            script([{json.dumps(VALIDATED)}], [true]);
            await f.chooseSensing(select);
            const asked = asks.length;
            script([{json.dumps(REFUSAL)}, {json.dumps(STARTED)}], []);
            await f.startRequest();
            const afterFirst = {{ asks: asks.length, confirmed: f.state().sensingConfirmed }};
            // The next Start (after a Stop, say) is asked again: the confirmation is not replayed.
            script([{json.dumps(REFUSAL)}], [false]);
            const res = await f.startRequest();
            return {{ asked, afterFirst, asks: asks.length, res,
                      nonces: calls.filter((c) => c.path === '/api/start').map((c) => c.body.confirm_nonce || null) }};
        """)
        assert out["asked"] == 1
        assert out["afterFirst"] == {"asks": 1, "confirmed": None}  # the control's yes spared the second question
        assert out["asks"] == 2 and out["res"] is None
        assert out["nonces"] == [None, "n1", None]

    @pytest.mark.parametrize("validated, refused_edges", [
        (VALIDATED, ["drone-1"]),                                         # the drone list changed since the yes
        (VALIDATED, ["drone-1", "drone-2", "drone-3"]),
        ({"status": 200, "data": {"obstacle": {"warning": "W"}}}, ["drone-1", "drone-2"]),  # edges unknown then
        ({"status": 500, "data": {"error": "down"}}, ["drone-1", "drone-2"]),
    ])
    def test_a_confirmation_for_other_edges_is_asked_again(self, tmp_path, validated, refused_edges):
        refusal = {"status": 409, "data": {**REFUSAL["data"], "edges": refused_edges}}
        out = _node(tmp_path, f"""
            const select = $('#obstacle_sensing');
            select.value = 'off';
            script([{json.dumps(validated)}], [true]);
            await f.chooseSensing(select);
            script([{json.dumps(refusal)}, {json.dumps(STARTED)}], [true]);
            const res = await f.startRequest();
            return {{ res, asks: asks.length, last: calls[calls.length - 1].body }};
        """)
        assert out["asks"] == 2  # the mode control's yes, and again at Start
        assert out["res"] == {"started": True}
        assert (out["last"]["confirm_sensing"], out["last"]["confirm_nonce"]) == ("off", "n1")

    def test_stop_and_a_preset_load_forget_the_confirmation(self, tmp_path):
        out = _node(tmp_path, f"""
            const select = $('#obstacle_sensing');
            select.value = 'off';
            script([{json.dumps(VALIDATED)}], [true]);
            await f.chooseSensing(select);
            f.forgetSensingConfirmation();
            script([{json.dumps(REFUSAL)}], [false]);
            const res = await f.startRequest();
            return {{ res, asks: asks.length }};
        """)
        assert out == {"res": None, "asks": 2}
        load = SCRIPT[SCRIPT.index("function loadPreset()"):SCRIPT.index("async function savePreset()")]
        stop = SCRIPT[SCRIPT.index("async function stop()"):SCRIPT.index("async function stop()") + 600]
        assert "forgetSensingConfirmation();" in load and "forgetSensingConfirmation();" in stop

    def test_a_confirmation_that_expired_says_so(self, tmp_path):
        out = _node(tmp_path, f"""
            script([{json.dumps(REFUSAL)}, {json.dumps(REFUSAL)}], [true]);
            try {{ await f.startRequest(); return 'started'; }} catch (e) {{ return e.message; }}
        """)
        assert out.startswith("Not started: the confirmation of obstacle sensing off expired")

    def test_an_environment_override_shows_the_effective_value_in_the_disabled_control(self, tmp_path):
        ob = {"mode": "off", "mode_source": "environment", "mode_variable": ENV_OBSTACLE_SENSING,
              "min_safe_depth": 0.77, "min_safe_depth_source": "environment",
              "min_safe_depth_variable": ENV_OBSTACLE_MIN_SAFE_DEPTH, "min_safe_depth_default": 0.35,
              "edges": ["drone-1"], "downgraded": True, "uncalibrated_note": None}
        out = _node(tmp_path, f"""
            const controls = () => ({{
                mode: [$('#obstacle_sensing').value, $('#obstacle_sensing').disabled],
                depth: [$('#obstacle_min_safe_depth').value, $('#obstacle_min_safe_depth').disabled],
                note: [$('#obstacle-uncalibrated').hidden, $('#obstacle-uncalibrated').textContent],
                text: $('#obstacle-effective').children.map((c) => c.textContent).join(' ') }});
            f.renderObstacle({json.dumps(ob)});
            const overridden = controls();
            f.renderObstacle(null);
            const cleared = controls();
            f.renderObstacle({json.dumps({**ob, "mode": "required", "mode_source": "window",
                                          "min_safe_depth": 0.35, "min_safe_depth_source": "window",
                                          "downgraded": False, "uncalibrated_note": "Not calibrated."})});
            return {{ overridden, cleared, window: controls() }};
        """)
        overridden, cleared, window = out["overridden"], out["cleared"], out["window"]
        # The window still holds required and 0.35; the disabled controls show what the edges will use.
        assert overridden["mode"] == ["off", True] and overridden["depth"] == ["0.77", True]
        assert "mode off (from FLIGHTRISK_OBSTACLE_SENSING in the launcher's environment" in overridden["text"]
        assert overridden["note"] == [True, ""]
        # No answer from /api/validate: nothing stays disabled, and the window's values are back.
        assert cleared["mode"] == ["required", False] and cleared["depth"] == ["0.35", False]
        assert cleared["text"] == ""
        assert window["mode"] == ["required", False] and window["depth"] == ["0.35", False]
        assert window["note"] == [False, "Not calibrated."]

    def test_an_answer_from_validate_keeps_what_the_operator_is_typing(self, tmp_path):
        ob = {"mode": "required", "mode_source": "window", "mode_variable": ENV_OBSTACLE_SENSING,
              "min_safe_depth": 0, "min_safe_depth_source": "window",
              "min_safe_depth_variable": ENV_OBSTACLE_MIN_SAFE_DEPTH, "min_safe_depth_default": 0.35,
              "edges": ["drone-1"], "downgraded": False, "uncalibrated_note": None}
        out = _node(tmp_path, f"""
            const depth = $('#obstacle_min_safe_depth');
            depth.value = '0.';            // half typed; the input handler stored Number('0.') = 0
            settings.obstacle_min_safe_depth = 0;
            f.renderObstacle({json.dumps(ob)});
            return depth.value;
        """)
        assert out == "0."

    def test_the_running_edges_values_are_shown_while_processes_run(self, tmp_path):
        out = _node(tmp_path, """
            f.renderRunningObstacle({ mode: 'off', min_safe_depth: 0.77, downgraded: true });
            const shown = [$('#obstacle-running').hidden, $('#obstacle-running').textContent, $('#obstacle-running').className];
            f.renderRunningObstacle(null);
            return { shown, hidden: $('#obstacle-running').hidden };
        """)
        assert out["shown"][0] is False and "mode off and min safe depth 0.77" in out["shown"][1]
        assert out["shown"][2] == "small warning" and out["hidden"] is True
