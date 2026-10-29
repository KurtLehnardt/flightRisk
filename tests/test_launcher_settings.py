"""Launcher settings: validation, the fleet list and presets (flightrisk/launcher/settings.py)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from flightrisk.launcher.settings import (
    DEFAULT_PRESET,
    ERROR,
    WARNING,
    DroneSettings,
    LauncherSettings,
    PresetError,
    PresetStore,
    SettingsError,
    builtin_presets,
    errors,
    validate,
)

PROFILES = ("default", "top_down", "level")


def _errors(settings: LauncherSettings, **kwargs) -> list[str]:
    kwargs.setdefault("is_windows", False)
    kwargs.setdefault("profile_names", PROFILES)
    return [i.message for i in errors(validate(settings, **kwargs))]


def _fields(settings: LauncherSettings, **kwargs) -> set[str]:
    kwargs.setdefault("is_windows", False)
    kwargs.setdefault("profile_names", PROFILES)
    return {i.field for i in errors(validate(settings, **kwargs))}


def _tellos(*ips: str, ifindexes=None, run_here=True) -> tuple[DroneSettings, ...]:
    ifindexes = ifindexes or [None] * len(ips)
    return tuple(
        DroneSettings(drone_id=f"drone-{k + 1}", source="tello", local_ip=ip, ifindex=ifindexes[k], run_here=run_here,
                      alt_slot_cm=100)
        for k, ip in enumerate(ips)
    )


# ---- presets and the fleet list -----------------------------------------------


def test_builtin_presets_cover_the_layouts_being_tested():
    presets = builtin_presets()
    for name in ("Layout A (ground here)", "Layout B (everything here)", "Single drone", "Rehearsal with fake drones"):
        assert name in presets
    assert DEFAULT_PRESET in presets


@pytest.mark.parametrize("name", list(builtin_presets()))
@pytest.mark.parametrize("is_windows", [True, False])
def test_builtin_presets_are_valid(name, is_windows):
    assert _errors(builtin_presets()[name], is_windows=is_windows) == []


def test_two_real_tellos_here_need_adapter_ips():
    preset = builtin_presets()["Layout B (everything here)"]
    cleared = replace(preset, drones=tuple(replace(d, local_ip="") for d in preset.drones))
    assert _fields(cleared) == {"drones.0.local_ip", "drones.1.local_ip"}
    # One real drone and one fake drone need no adapter IP.
    mixed = replace(cleared, drones=(cleared.drones[0], replace(cleared.drones[1], source="fake")))
    assert _errors(mixed) == []


def test_fleet_list_is_exactly_the_selected_drones():
    settings = LauncherSettings(drones=(
        DroneSettings(drone_id="drone-1", source="fake"),
        DroneSettings(drone_id="drone-3", source="fake"),
        DroneSettings(drone_id="scout", source="fake", run_here=False),
    ), network="cable")
    assert settings.fleet_ids() == ("drone-1", "drone-3", "scout")
    assert [d.drone_id for d in settings.local_drones()] == ["drone-1", "drone-3"]


def test_edges_only_runs_every_drone_here():
    settings = LauncherSettings(role="edges", drones=(DroneSettings(drone_id="drone-2", run_here=False, source="fake"),))
    assert [d.drone_id for d in settings.local_drones()] == ["drone-2"]
    assert settings.edge_ws_url() == "ws://10.42.0.1:9000"


def test_bind_hosts_are_loopback_unless_cable_layout():
    assert LauncherSettings(network="loopback").edge_bind_hosts() == ("127.0.0.1",)
    assert LauncherSettings(network="cable", cable_address="10.42.0.1").edge_bind_hosts() == ("127.0.0.1", "10.42.0.1")


def test_token_needed_only_off_loopback():
    assert not LauncherSettings(network="loopback").needs_token()
    assert LauncherSettings(network="cable").needs_token()
    assert LauncherSettings(role="edges", ground_host="10.42.0.1").needs_token()
    assert not LauncherSettings(role="edges", ground_host="127.0.0.1").needs_token()
    assert not LauncherSettings(role="edges", ground_host="localhost").needs_token()


# ---- validation -----------------------------------------------------------------


def test_drone_count_limits():
    assert "drones" in _fields(replace(LauncherSettings(), drones=()))
    five = tuple(DroneSettings(drone_id=f"drone-{k}", source="fake") for k in range(1, 6))
    assert "drones" in _fields(replace(LauncherSettings(), drones=five))
    four = five[:4]
    assert _errors(replace(LauncherSettings(), drones=four)) == []


def test_duplicate_and_bad_drone_ids_are_refused():
    dup = (DroneSettings(drone_id="drone-1", source="fake"), DroneSettings(drone_id="drone-1", source="fake"))
    assert any("used twice" in m for m in _errors(LauncherSettings(drones=dup)))
    bad = (DroneSettings(drone_id="drone 1", source="fake"),)
    assert "drones.0.drone_id" in _fields(LauncherSettings(drones=bad))


def test_two_edges_on_one_local_ip_are_refused():
    settings = LauncherSettings(drones=_tellos("192.168.10.2", "192.168.10.2"))
    messages = _errors(settings)
    assert any("would share local IP 192.168.10.2" in m for m in messages)
    assert _fields(settings) == {"drones.0.local_ip", "drones.1.local_ip"}


def test_two_edges_on_two_local_ips_are_accepted():
    assert _errors(LauncherSettings(drones=_tellos("192.168.10.2", "192.168.10.50"))) == []


def test_same_interface_index_is_refused_on_windows():
    settings = LauncherSettings(drones=_tellos("192.168.10.2", "192.168.10.50", ifindexes=[12, 12]))
    assert any("interface index 12" in m for m in _errors(settings, is_windows=True))
    ok = LauncherSettings(drones=_tellos("192.168.10.2", "192.168.10.50", ifindexes=[12, 23]))
    assert _errors(ok, is_windows=True) == []


def test_interface_index_is_windows_only_and_needs_local_ip():
    on_mac = LauncherSettings(drones=_tellos("192.168.10.2", ifindexes=[12]))
    assert any("only supported on Windows" in m for m in _errors(on_mac, is_windows=False))
    no_ip = LauncherSettings(drones=(DroneSettings(source="tello", ifindex=12),))
    assert any("needs the local IP" in m for m in _errors(no_ip, is_windows=True))


def test_remote_drones_need_the_cable_layout():
    settings = LauncherSettings(network="loopback", drones=_tellos("", run_here=False))
    assert any("listens on loopback only" in m for m in _errors(settings))
    assert _errors(replace(settings, network="cable")) == []


def test_remote_drones_do_not_need_adapter_ips_here():
    # Layout A: both real drones fly from the other computer, which picks its own adapters.
    settings = LauncherSettings(network="cable", drones=_tellos("", "", run_here=False))
    assert _errors(settings) == []


def test_slot_above_ceiling_is_refused():
    settings = LauncherSettings(drones=(DroneSettings(source="fake", alt_slot_cm=160),))
    assert any("above the ceiling limit 140 cm" in m for m in _errors(settings))
    assert _errors(replace(settings, ceiling_cm=240)) == []


def test_ceiling_out_of_range_is_refused():
    assert "ceiling_cm" in _fields(LauncherSettings(ceiling_cm=120))
    assert "ceiling_cm" in _fields(LauncherSettings(margin_cm=10))


def test_ports_must_differ_and_avoid_the_launcher():
    same = LauncherSettings(dashboard_port=9000, edge_port=9000, drones=(DroneSettings(source="fake"),))
    assert "edge_port" in _fields(same)
    clash = LauncherSettings(dashboard_port=5500, drones=(DroneSettings(source="fake"),))
    assert "dashboard_port" in _fields(clash, reserved_ports={5500: "the launcher window"})
    low = LauncherSettings(edge_port=80, drones=(DroneSettings(source="fake"),))
    assert "edge_port" in _fields(low)


def test_edges_only_needs_a_ground_host():
    assert "ground_host" in _fields(LauncherSettings(role="edges", ground_host="", drones=(DroneSettings(source="fake"),)))
    assert "ground_host" in _fields(LauncherSettings(role="edges", ground_host="0.0.0.0", drones=(DroneSettings(source="fake"),)))
    assert _errors(LauncherSettings(role="edges", ground_host="ground.local", drones=(DroneSettings(source="fake"),))) == []


def test_cable_address_must_be_a_real_ipv4():
    for bad in ("", "127.0.0.1", "0.0.0.0", "not-an-ip"):
        assert "cable_address" in _fields(LauncherSettings(network="cable", cable_address=bad, drones=(DroneSettings(source="fake"),)))


def test_unknown_profile_and_bad_endpoint_are_refused():
    assert "detector_profile" in _fields(LauncherSettings(detector_profile="nope", drones=(DroneSettings(source="fake"),)))
    bad_otel = LauncherSettings(otel_enabled=True, otel_endpoint="localhost:4317", drones=(DroneSettings(source="fake"),))
    assert "otel_endpoint" in _fields(bad_otel)


def test_fake_drone_with_adapter_settings_only_warns():
    issues = validate(LauncherSettings(drones=(DroneSettings(source="fake", local_ip="192.168.10.2"),)),
                      is_windows=False, profile_names=PROFILES)
    assert [i.level for i in issues] == [WARNING]


def test_issue_levels_are_error_or_warning():
    issues = validate(LauncherSettings(llm=True, drones=(DroneSettings(drone_id="", source="fake"),)),
                      is_windows=False, profile_names=PROFILES)
    assert {i.level for i in issues} == {ERROR, WARNING}


# ---- JSON -------------------------------------------------------------------------


def test_settings_round_trip_through_json():
    for preset in builtin_presets().values():
        assert LauncherSettings.from_json(json.loads(json.dumps(preset.to_json()))) == preset


def test_from_json_coerces_numbers_from_the_window_and_ignores_unknown_keys():
    data = builtin_presets()["Single drone"].to_json()
    data["dashboard_port"] = "5801"
    data["drones"][0]["ifindex"] = ""
    data["token"] = "should-be-ignored"
    settings = LauncherSettings.from_json(data)
    assert settings.dashboard_port == 5801
    assert settings.drones[0].ifindex is None
    assert "token" not in json.dumps(settings.to_json())


@pytest.mark.parametrize("patch", [
    {"dashboard_port": "abc"}, {"stream_video": "yes"}, {"drones": {}}, {"role": 5}, {"drones": [{"alt_slot_cm": True}]},
])
def test_from_json_rejects_wrong_types(patch):
    data = {**LauncherSettings().to_json(), **patch}
    with pytest.raises(SettingsError):
        LauncherSettings.from_json(data)


# ---- preset store ---------------------------------------------------------------


def test_preset_save_and_load(tmp_path):
    store = PresetStore(tmp_path / "launcher.json")
    custom = replace(builtin_presets()["Single drone"], dashboard_port=5801)
    store.save_preset("Gym test", custom)
    store.save_last(custom)
    again = PresetStore(tmp_path / "launcher.json")
    assert again.presets()["Gym test"] == custom
    assert again.last() == custom
    assert list(again.presets())[:len(builtin_presets())] == list(builtin_presets())
    again.delete_preset("Gym test")
    assert "Gym test" not in PresetStore(tmp_path / "launcher.json").presets()


def test_builtin_presets_cannot_be_overwritten_or_deleted(tmp_path):
    store = PresetStore(tmp_path / "launcher.json")
    with pytest.raises(PresetError):
        store.save_preset("Single drone", LauncherSettings())
    with pytest.raises(PresetError):
        store.delete_preset("Single drone")
    with pytest.raises(PresetError):
        store.save_preset("  ", LauncherSettings())


def test_presets_never_contain_the_token(tmp_path):
    token = "tok-SECRET-value-123"
    path = tmp_path / "launcher.json"
    store = PresetStore(path, secret=lambda: token)
    store.save_preset("Ok", LauncherSettings())
    assert token not in path.read_text()
    # A field that happens to carry the token is refused rather than written.
    with pytest.raises(PresetError):
        store.save_preset("Leak", replace(LauncherSettings(), otel_endpoint=f"http://x/{token}"))
    assert token not in path.read_text()


def test_bad_preset_file_is_reported_not_fatal(tmp_path):
    path = tmp_path / "launcher.json"
    path.write_text("{not json")
    store = PresetStore(path)
    assert set(store.presets()) == set(builtin_presets())
    assert store.load_problems
    path.write_text(json.dumps({"version": 1, "presets": {"Broken": {"dashboard_port": "x"}}, "last": None}))
    store = PresetStore(path)
    assert "Broken" not in store.presets()
    assert any("Broken" in p for p in store.load_problems)


def test_launcher_file_env_override(tmp_path, monkeypatch):
    from flightrisk.launcher.settings import default_launcher_file

    monkeypatch.setenv("FLIGHTRISK_LAUNCHER_FILE", str(tmp_path / "x.json"))
    assert default_launcher_file() == tmp_path / "x.json"


# ---- review round 1 -----------------------------------------------------------------


def test_one_tello_without_local_ip_needs_an_adapter_when_two_can_reach_it():
    from flightrisk.launcher.netinfo import Adapter

    two = [Adapter("lo0", "127.0.0.1", 1), Adapter("Wi-Fi", "192.168.10.2", 12), Adapter("Wi-Fi 2", "192.168.10.50", 23)]
    one = [Adapter("lo0", "127.0.0.1", 1), Adapter("Wi-Fi", "192.168.10.2", 12), Adapter("Ethernet", "10.42.0.2", 5)]
    single = builtin_presets()["Single drone"]
    messages = _errors(single, adapters=two, is_windows=True)
    assert any("choose the adapter" in m and "Wi-Fi 2 192.168.10.50" in m for m in messages)
    assert _errors(single, adapters=one, is_windows=True) == []
    picked = replace(single, drones=(replace(single.drones[0], local_ip="192.168.10.50"),))
    assert _errors(picked, adapters=two, is_windows=True) == []
    fake = replace(single, drones=(replace(single.drones[0], source="fake"),))
    assert _errors(fake, adapters=two) == []
    remote = LauncherSettings(network="cable", drones=(DroneSettings(run_here=False),))
    assert _errors(remote, adapters=two) == []


@pytest.mark.parametrize("drone_id", ["drone:1", "drone.1", "-drone", "drone 1", "a" * 65])
def test_drone_ids_are_safe_file_names(drone_id):
    assert "drones.0.drone_id" in _fields(LauncherSettings(drones=(DroneSettings(drone_id=drone_id, source="fake"),)))


def test_drone_ids_with_hyphen_and_underscore_are_fine():
    assert _errors(LauncherSettings(drones=(DroneSettings(drone_id="Drone_2-b", source="fake"),))) == []


def test_geofence_has_a_maximum():
    assert "geofence_cm" in _fields(LauncherSettings(geofence_cm=10 ** 12, drones=(DroneSettings(source="fake"),)))
    assert "geofence_cm" not in _fields(LauncherSettings(geofence_cm=5000, drones=(DroneSettings(source="fake"),)))


@pytest.mark.parametrize("value", ["a\x00b", "line\nbreak", "tab\tx", "del\x7f", "x" * 300])
def test_text_fields_refuse_control_characters_and_huge_values(value):
    with pytest.raises(SettingsError):
        LauncherSettings.from_json({**LauncherSettings().to_json(), "otel_endpoint": value})


def test_a_setting_containing_the_token_is_an_error():
    token = "drone-tok-123"
    settings = LauncherSettings(drones=(DroneSettings(drone_id=token, source="fake"),))
    issues = errors(validate(settings, is_windows=False, profile_names=PROFILES, secret=token))
    assert any(i.field == "settings" and token not in i.message for i in issues)
    assert not any(i.field == "settings" for i in errors(validate(settings, is_windows=False, profile_names=PROFILES)))


def test_cloud_reasoning_inherited_is_called_out():
    settings = LauncherSettings(llm=True, drones=(DroneSettings(source="fake"),))
    cloud = validate(settings, is_windows=False, profile_names=PROFILES, environ={"FLIGHTRISK_REASONING_PROVIDER": "claude"})
    assert any("Claude cloud" in i.message for i in cloud)
    local = validate(settings, is_windows=False, profile_names=PROFILES, environ={})
    assert any("local Ollama" in i.message for i in local)


def test_unreadable_adapter_list_fails_closed_for_a_drone_without_a_local_ip():
    from flightrisk.launcher.netinfo import Adapter

    single = builtin_presets()["Single drone"]
    for adapters in ([], [Adapter("lo0", "127.0.0.1", 1)]):
        assert any("could not list" in m for m in _errors(single, adapters=adapters))
    picked = replace(single, drones=(replace(single.drones[0], local_ip="192.168.10.2"),))
    assert _errors(picked, adapters=[]) == []
    fake = replace(single, drones=(replace(single.drones[0], source="fake"),))
    assert _errors(fake, adapters=[]) == []
