"""Launcher command lines, child environments and the edge token (flightrisk/launcher/commands.py)."""

from __future__ import annotations

from dataclasses import replace

from flightrisk.launcher.commands import (
    REDACTED,
    TOKEN_ENV,
    EdgeToken,
    Redactor,
    build_plan,
    load_token,
    parse_env_file,
)
from flightrisk.launcher.settings import (
    DroneSettings,
    LauncherSettings,
    builtin_presets,
)

TOKEN = "s3cret-Token-for-tests"
PY = "/venv/bin/python"


def _plan(settings, token=TOKEN, base_env=None):
    return build_plan(settings, EdgeToken(token, "environment"), PY, base_env or {})


def _arg(argv, flag):
    return argv[argv.index(flag) + 1]


# ---- layouts ---------------------------------------------------------------------


def test_layout_a_ground_here_binds_loopback_and_cable_and_starts_no_edges():
    plan = _plan(builtin_presets()["Layout A (ground here)"])
    assert [p.name for p in plan] == ["ground"]
    argv = plan[0].argv
    assert argv[:3] == (PY, "-m", "flightrisk.dashboard")
    assert _arg(argv, "--source") == "edge"
    assert _arg(argv, "--edge-host") == "127.0.0.1,10.42.0.1"
    assert _arg(argv, "--host") == "127.0.0.1"
    assert _arg(argv, "--fleet") == "drone-1,drone-2"
    assert "--no-llm" in argv
    assert plan[0].env["FLIGHTRISK_FLEET_NET"] == "ap"
    assert plan[0].ready_ports == (("127.0.0.1", 5555), ("127.0.0.1", 9000))


def test_layout_b_everything_here_uses_loopback_only():
    settings = replace(builtin_presets()["Layout B (everything here)"], drones=(
        DroneSettings(drone_id="drone-1", local_ip="192.168.10.2", alt_slot_cm=100),
        DroneSettings(drone_id="drone-2", local_ip="192.168.10.50", alt_slot_cm=130),
    ))
    plan = _plan(settings)
    assert [p.name for p in plan] == ["ground", "edge-drone-1", "edge-drone-2"]
    assert _arg(plan[0].argv, "--edge-host") == "127.0.0.1"
    assert plan[0].env["FLIGHTRISK_FLEET_NET"] == "station"
    for spec, ip, slot in zip(plan[1:], ("192.168.10.2", "192.168.10.50"), ("100", "130")):
        argv = spec.argv
        assert argv[:3] == (PY, "-m", "flightrisk.edge_device")
        assert _arg(argv, "--source") == "tello"
        assert _arg(argv, "--tello-host") == "192.168.10.1"
        assert _arg(argv, "--tello-local-ip") == ip
        assert "--tello-ifindex" not in argv
        assert _arg(argv, "--alt-slot") == slot
        assert _arg(argv, "--ws-url") == "ws://127.0.0.1:9000"
        assert spec.ready_ports == ()


def test_single_drone_has_no_adapter_flags():
    plan = _plan(builtin_presets()["Single drone"])
    assert [p.name for p in plan] == ["ground", "edge-drone-1"]
    assert _arg(plan[0].argv, "--fleet") == "drone-1"
    assert "--tello-local-ip" not in plan[1].argv


def test_rehearsal_uses_fake_drones_and_its_own_ports():
    plan = _plan(builtin_presets()["Rehearsal with fake drones"])
    assert _arg(plan[0].argv, "--port") == "5800"
    assert _arg(plan[0].argv, "--edge-port") == "9800"
    for spec in plan[1:]:
        assert _arg(spec.argv, "--source") == "fake"
        assert "--tello-host" not in spec.argv
        assert _arg(spec.argv, "--ws-url") == "ws://127.0.0.1:9800"


def test_edges_only_dials_the_ground_host_with_interface_index():
    settings = LauncherSettings(role="edges", ground_host="10.42.0.1", ground_port=9000, drones=(
        DroneSettings(drone_id="drone-1", local_ip="192.168.10.2", ifindex=12, alt_slot_cm=50),
        DroneSettings(drone_id="drone-2", local_ip="192.168.10.50", ifindex=23, alt_slot_cm=80),
    ))
    plan = _plan(settings)
    assert [p.name for p in plan] == ["edge-drone-1", "edge-drone-2"]
    assert _arg(plan[0].argv, "--ws-url") == "ws://10.42.0.1:9000"
    assert _arg(plan[0].argv, "--tello-ifindex") == "12"
    assert _arg(plan[1].argv, "--tello-ifindex") == "23"


def test_ground_fleet_matches_selected_drones_including_remote_ones():
    settings = LauncherSettings(network="cable", drones=(
        DroneSettings(drone_id="drone-1", source="fake"),
        DroneSettings(drone_id="drone-7", source="tello", run_here=False),
    ))
    plan = _plan(settings)
    assert _arg(plan[0].argv, "--fleet") == "drone-1,drone-7"
    assert [p.name for p in plan] == ["ground", "edge-drone-1"]


def test_shared_options_reach_the_children():
    settings = replace(builtin_presets()["Rehearsal with fake drones"], stream_video=True, llm=True, ceiling_cm=240,
                       margin_cm=50, geofence_cm=600, detector_profile="level", otel_enabled=True,
                       otel_endpoint="http://localhost:4318")
    ground, edge = _plan(settings)[:2]
    assert "--no-llm" not in ground.argv and "--stream-video" in ground.argv
    assert "--stream-video" in edge.argv
    assert _arg(edge.argv, "--detection-profile") == "level"
    assert edge.env["FLIGHTRISK_CEILING_CM"] == "240"
    assert edge.env["FLIGHTRISK_CEILING_MARGIN_CM"] == "50"
    assert edge.env["FLIGHTRISK_GEOFENCE_CM"] == "600"
    assert edge.env["FLIGHTRISK_OTEL_ENABLED"] == "1"
    assert edge.env["OTEL_EXPORTER"] == "otlp-http"
    assert edge.env["OTEL_RESOURCE_ATTRIBUTES"] == "service.instance.id=edge-drone-1"
    assert ground.env["OTEL_EXPORTER"] == "otlp-http"
    off = _plan(builtin_presets()["Rehearsal with fake drones"])[1]
    assert off.env["FLIGHTRISK_OTEL_ENABLED"] == "0"


def test_inherited_variables_that_would_override_the_choice_are_removed():
    base = {
        "PATH": "/usr/bin",
        "FLIGHTRISK_GROUND_URLS": "ws://elsewhere:9000",
        "FLIGHTRISK_TELLO_LOCAL_IP": "192.168.10.9",
        "FLIGHTRISK_NO_LLM": "1",
        "FLIGHTRISK_FLEET_DRONES": "drone-9",
        TOKEN_ENV: "inherited-old-token",
    }
    for spec in _plan(builtin_presets()["Rehearsal with fake drones"], base_env=base):
        assert spec.env["PATH"] == "/usr/bin"
        for key in ("FLIGHTRISK_GROUND_URLS", "FLIGHTRISK_TELLO_LOCAL_IP", "FLIGHTRISK_NO_LLM", "FLIGHTRISK_FLEET_DRONES"):
            assert key not in spec.env
        assert spec.env[TOKEN_ENV] == TOKEN
        assert spec.env["PYTHONUNBUFFERED"] == "1"


def test_no_token_means_no_token_variable():
    for spec in build_plan(builtin_presets()["Rehearsal with fake drones"], EdgeToken(None, None), PY, {TOKEN_ENV: "x"}):
        assert TOKEN_ENV not in spec.env


# ---- the token never leaks ---------------------------------------------------------


def test_token_never_appears_in_command_lines_or_display_env():
    for preset in builtin_presets().values():
        for spec in _plan(preset):
            assert TOKEN not in " ".join(spec.argv)
            assert TOKEN not in spec.command_line()
            assert TOKEN not in repr(spec)
            assert all(TOKEN not in v for v in spec.display_env.values())
            assert TOKEN_ENV not in spec.display_env


def test_edge_token_repr_hides_the_value():
    token = EdgeToken(TOKEN, "environment")
    assert TOKEN not in repr(token) and TOKEN not in str(token)
    assert token.status == "set"
    assert EdgeToken("", "x").status == "missing"
    assert EdgeToken("", "x").source is None


def test_redactor_removes_every_token_it_has_seen():
    red = Redactor(EdgeToken(TOKEN, "environment"))
    red.add("second-token-value")
    text = f"a {TOKEN} b 'second-token-value' c"
    assert red(text) == f"a {REDACTED} b '{REDACTED}' c"
    assert red.bytes(text.encode()) == red(text).encode()
    assert Redactor(None)("nothing") == "nothing"


# ---- token loading ---------------------------------------------------------------------


def test_parse_env_file_handles_export_quotes_and_comments():
    text = (
        "# fleet settings\n"
        "export FLIGHTRISK_FLEET_NET=ap\n"
        "export FLIGHTRISK_EDGE_TOKEN='quoted value'\n"
        "FLIGHTRISK_GROUND_LOCAL=1  # comment\n"
        "not a line\n"
    )
    values = parse_env_file(text)
    assert values["FLIGHTRISK_EDGE_TOKEN"] == "quoted value"
    assert values["FLIGHTRISK_FLEET_NET"] == "ap"
    assert values["FLIGHTRISK_GROUND_LOCAL"] == "1"
    assert "not a line" not in values


def test_load_token_prefers_environment_then_file(tmp_path):
    env_file = tmp_path / "fleet.env"
    env_file.write_text(f'export FLIGHTRISK_EDGE_TOKEN="{TOKEN}"\n')
    from_file = load_token({}, env_file)
    assert from_file.reveal() == TOKEN and from_file.source == str(env_file)
    from_env = load_token({TOKEN_ENV: "env-token"}, env_file)
    assert from_env.reveal() == "env-token" and from_env.source == "environment"
    missing = load_token({}, tmp_path / "absent.env")
    assert missing.status == "missing"
