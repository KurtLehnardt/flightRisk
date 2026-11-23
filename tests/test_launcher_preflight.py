"""Launcher preflight checks with injected fakes (flightrisk/launcher/preflight.py).

No packet is sent to any drone: the Tello probe runs against a fake socket.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from flightrisk.launcher.commands import EdgeToken
from flightrisk.launcher.netinfo import (
    Adapter,
    DefaultRoute,
    default_route_interface,
    parse_get_netroute,
    parse_ip_route_default,
    parse_route_get_default,
)
from flightrisk.launcher.preflight import (
    FAIL,
    INFO,
    PASS,
    PROBE_COMMANDS,
    WARN,
    PreflightEnv,
    TelloProbe,
    probe_tello,
    run_preflight,
)
from flightrisk.launcher.settings import (
    DroneSettings,
    LauncherSettings,
    builtin_presets,
)

TOKEN = "preflight-secret-token"


class FakeTelloSocket:
    """A UDP socket double that answers like a Tello and records everything sent."""

    def __init__(self, replies=None, stray=()):
        self.replies = {"command": "ok", "battery?": "87", "temp?": "60~62C", **(replies or {})}
        self.sent: list[tuple[str, tuple]] = []
        self.bound = None
        self.options = []
        self.closed = False
        self._inbox: list[bytes] = list(stray)

    def setsockopt(self, *args):
        self.options.append(args)

    def bind(self, address):
        self.bound = address

    def settimeout(self, value):
        self.timeout = value

    def sendto(self, data, address):
        text = data.decode()
        self.sent.append((text, address))
        reply = self.replies.get(text)
        if reply is not None:
            if text.endswith("?"):
                self._inbox.append(b"ok")  # a duplicate acknowledgement first, as a real Tello sometimes sends
            self._inbox.append(reply.encode())
        return len(data)

    def recvfrom(self, size):
        if self._inbox:
            return self._inbox.pop(0), ("192.168.10.1", 8889)
        if self.timeout == 0.0:
            raise BlockingIOError
        raise TimeoutError

    def close(self):
        self.closed = True


def test_probe_reads_battery_and_temperature_with_query_commands_only():
    sock = FakeTelloSocket()
    result = probe_tello("192.168.10.1", "192.168.10.50", 23, socket_factory=lambda: sock, is_windows=True, timeout=0.2)
    assert result == TelloProbe(True, battery=87, temp="60~62C", detail="192.168.10.1 via 192.168.10.50")
    assert [text for text, _ in sock.sent] == ["command", "battery?", "temp?"]
    assert all(text in PROBE_COMMANDS for text, _ in sock.sent)
    assert all(address == ("192.168.10.1", 8889) for _, address in sock.sent)
    assert sock.bound == ("192.168.10.50", 8889)
    assert sock.options and sock.options[0][2] == (23).to_bytes(4, "big")
    assert sock.closed


def test_probe_never_sends_a_flight_command():
    from flightrisk.launcher import preflight

    with pytest.raises(ValueError):
        preflight._ask(FakeTelloSocket(), "192.168.10.1", "takeoff", 0.1)
    assert not {"takeoff", "land", "emergency", "up 20", "motoron"} & set(PROBE_COMMANDS)


def test_probe_ignores_stale_replies_and_reports_no_answer():
    stale = FakeTelloSocket(stray=[b"\xff\xfe\x00binary", b"ok"])
    assert probe_tello("192.168.10.1", socket_factory=lambda: stale, timeout=0.2).battery == 87
    silent = FakeTelloSocket(replies={"command": None})
    silent.replies["command"] = None
    result = probe_tello("192.168.10.1", "192.168.10.2", socket_factory=lambda: silent, timeout=0.05)
    assert not result.ok and "no reply" in result.detail
    assert [t for t, _ in silent.sent] == ["command"]


def test_probe_without_local_ip_skips_interface_pin_off_windows():
    sock = FakeTelloSocket()
    probe_tello("192.168.10.1", "", 12, socket_factory=lambda: sock, is_windows=False, timeout=0.1)
    assert sock.options == []
    assert sock.bound == ("", 8889)


def _env(**overrides) -> PreflightEnv:
    base = {
        "in_venv": lambda: True,
        "python": "/venv/bin/python",
        "module_found": lambda name: True,
        "port_free": lambda host, port: True,
        "tcp_reachable": lambda host, port: True,
        "adapters": lambda: [Adapter("lo0", "127.0.0.1", 1), Adapter("en0", "192.168.10.2", 14),
                          Adapter("en7", "192.168.10.50", 15), Adapter("bridge0", "10.42.0.1", 20)],
        "weights": lambda: [
            {"name": "default", "label": "Default", "weights": "yolo11n.pt", "required": False, "found": "yolo11n.pt", "fallback": None},
            {"name": "top_down", "label": "Top", "weights": "models/heridal_yolo11s.pt", "required": True, "found": None, "fallback": "level"},
            {"name": "level", "label": "Level", "weights": "yolo11n.pt", "required": False, "found": None, "fallback": None},
        ],
        "ceiling_file": lambda: (200, 60, None, "/home/x/.flightrisk/ceiling.json"),
        "tello_probe": lambda d: TelloProbe(True, battery=80, temp="55C", detail=d.tello_host),
        "environ": {},
        "is_windows": False,
        "default_route": lambda: None,
    }
    base.update(overrides)
    return PreflightEnv(**base)


def _by_name(checks):
    return {c.name: c for c in checks}


def _two_tellos(**kw):
    return LauncherSettings(drones=(
        DroneSettings(drone_id="drone-1", local_ip="192.168.10.2"),
        DroneSettings(drone_id="drone-2", local_ip="192.168.10.50", alt_slot_cm=130),
    ), **kw)


def test_all_pass_for_a_good_setup_and_battery_is_shown():
    checks = run_preflight(_two_tellos(), EdgeToken(None, None), _env())
    assert not [c for c in checks if c.status == FAIL]
    rows = _by_name(checks)
    assert rows["Drone drone-1"].data["battery"] == 80
    assert "battery 80%" in rows["Drone drone-1"].detail
    assert rows["Adapter for drone-2"].status == PASS
    assert rows["Edge token"].status == INFO


def test_missing_dependency_and_busy_port_fail():
    env = _env(module_found=lambda name: name != "djitellopy", port_free=lambda host, port: port != 9000)
    rows = _by_name(run_preflight(builtin_presets()["Single drone"], EdgeToken(None, None), env))
    assert rows["Dependencies"].status == FAIL and "djitellopy" in rows["Dependencies"].detail
    assert rows["Edge link port 9000"].status == FAIL
    assert rows["Dashboard port 5555"].status == PASS


def test_token_required_for_cable_layout_and_shown_only_as_status():
    settings = builtin_presets()["Layout A (ground here)"]
    missing = _by_name(run_preflight(settings, EdgeToken(None, None), _env()))
    assert missing["Edge token"].status == FAIL
    present = run_preflight(settings, EdgeToken(TOKEN, "environment"), _env())
    assert _by_name(present)["Edge token"].status == PASS
    assert all(TOKEN not in (c.name + c.detail + repr(c.data)) for c in present)


def test_cable_address_must_be_on_an_adapter():
    settings = builtin_presets()["Layout A (ground here)"]
    env = _env(adapters=lambda: [Adapter("en0", "192.168.1.5", 4)])
    rows = _by_name(run_preflight(settings, EdgeToken(TOKEN, "environment"), env))
    assert rows["Cable address 10.42.0.1"].status == FAIL
    rows = _by_name(run_preflight(settings, EdgeToken(TOKEN, "environment"), _env()))
    assert rows["Cable address 10.42.0.1"].status == PASS


def test_local_ip_must_exist_and_match_the_interface_index():
    settings = LauncherSettings(role="edges", drones=(
        DroneSettings(drone_id="drone-1", local_ip="192.168.10.9"),
        DroneSettings(drone_id="drone-2", local_ip="192.168.10.50", ifindex=99),
    ))
    rows = _by_name(run_preflight(settings, EdgeToken(TOKEN, "environment"), _env(is_windows=True)))
    assert rows["Adapter for drone-1"].status == FAIL
    assert rows["Adapter for drone-2"].status == FAIL and "index 15" in rows["Adapter for drone-2"].detail


def test_unreachable_drone_fails_and_low_battery_warns():
    def probe(drone):
        if drone.drone_id == "drone-1":
            return TelloProbe(False, detail="no reply from 192.168.10.1 via 192.168.10.2")
        return TelloProbe(True, battery=20, temp="70C", detail="ok")

    rows = _by_name(run_preflight(_two_tellos(), EdgeToken(None, None), _env(tello_probe=probe)))
    assert rows["Drone drone-1"].status == FAIL
    assert rows["Drone drone-2"].status == WARN and "below the 30%" in rows["Drone drone-2"].detail


def test_drones_are_not_probed_unless_asked_and_fake_drones_never():
    probed = []
    env = _env(tello_probe=lambda d: probed.append(d.drone_id) or TelloProbe(True, battery=90))
    run_preflight(_two_tellos(), EdgeToken(None, None), env, probe_drones=False)
    run_preflight(builtin_presets()["Rehearsal with fake drones"], EdgeToken(None, None), env)
    run_preflight(builtin_presets()["Layout A (ground here)"], EdgeToken(TOKEN, "x"), env)
    assert probed == []


def test_detector_weights_for_the_chosen_profile():
    base = builtin_presets()["Rehearsal with fake drones"]
    rows = _by_name(run_preflight(replace(base, detector_profile="top_down"), EdgeToken(None, None), _env()))
    assert rows["Detector weights (top_down)"].status == FAIL
    rows = _by_name(run_preflight(replace(base, detector_profile="level"), EdgeToken(None, None), _env()))
    assert rows["Detector weights (level)"].status == WARN
    rows = _by_name(run_preflight(base, EdgeToken(None, None), _env()))
    assert rows["Detector weights (default)"].status == PASS
    # The ground alone loads no detector weights through the launcher.
    rows = _by_name(run_preflight(builtin_presets()["Layout A (ground here)"], EdgeToken(TOKEN, "x"), _env()))
    assert not any(name.startswith("Detector weights") for name in rows)


def test_ceiling_mismatch_warns():
    env = _env(ceiling_file=lambda: (240, 60, None, "/x/ceiling.json"))
    rows = _by_name(run_preflight(builtin_presets()["Rehearsal with fake drones"], EdgeToken(None, None), env))
    assert rows["Ceiling"].status == WARN and "240 cm" in rows["Ceiling"].detail


def test_edges_only_checks_the_ground_without_failing():
    settings = LauncherSettings(role="edges", drones=(DroneSettings(source="fake"),), ground_host="10.42.0.1")
    rows = _by_name(run_preflight(settings, EdgeToken(TOKEN, "x"), _env(tcp_reachable=lambda h, p: False)))
    assert rows["Ground station 10.42.0.1:9000"].status == WARN
    assert not any(name.startswith("Dashboard port") for name in rows)


def test_not_a_venv_warns():
    rows = _by_name(run_preflight(builtin_presets()["Rehearsal with fake drones"], EdgeToken(None, None),
                                  _env(in_venv=lambda: False)))
    assert rows["Virtual environment"].status == WARN



def test_a_single_drone_without_an_adapter_fails_on_a_two_adapter_machine_and_is_not_probed():
    probed = []
    env = _env(
        adapters=lambda: [Adapter("Wi-Fi", "192.168.10.2", 12), Adapter("Wi-Fi 2", "192.168.10.50", 23)],
        tello_probe=lambda d: probed.append(d.drone_id) or TelloProbe(True, battery=90),
        is_windows=True,
    )
    settings = LauncherSettings(role="edges", drones=(DroneSettings(drone_id="drone-2"),))
    rows = _by_name(run_preflight(settings, EdgeToken(TOKEN, "x"), env))
    assert rows["Adapter for drone-2"].status == FAIL and "choose the adapter" in rows["Adapter for drone-2"].detail
    assert rows["Drone drone-2"].status == FAIL
    assert probed == []
    picked = LauncherSettings(role="edges", drones=(DroneSettings(drone_id="drone-2", local_ip="192.168.10.50", ifindex=23),))
    rows = _by_name(run_preflight(picked, EdgeToken(TOKEN, "x"), env))
    assert rows["Drone drone-2"].status == PASS and probed == ["drone-2"]



def test_unreadable_adapter_list_fails_and_sends_no_probe():
    probed = []
    env = _env(adapters=list, tello_probe=lambda d: probed.append(d.drone_id) or TelloProbe(True, battery=90))
    rows = _by_name(run_preflight(builtin_presets()["Single drone"], EdgeToken(None, None), env))
    assert rows["Adapter for drone-1"].status == FAIL and "set the local IP" in rows["Adapter for drone-1"].detail
    assert rows["Drone drone-1"].status == FAIL
    assert probed == []


# ---------------------------------------------------------------------------
# A real drone with no local IP: with obstacle sensing required its edge refuses to start (#186)
# ---------------------------------------------------------------------------


def _single(**drone):
    return LauncherSettings(drones=(DroneSettings(drone_id="drone-1", **drone),))


def _one_adapter(**overrides):
    overrides.setdefault("default_route", lambda: DefaultRoute("en7", 15))
    return _env(adapters=lambda: [Adapter("lo0", "127.0.0.1", 1), Adapter("en0", "192.168.10.2", 14)], **overrides)


@pytest.mark.parametrize("environ", [{}, {"FLIGHTRISK_OBSTACLE_SENSING": ""}, {"FLIGHTRISK_OBSTACLE_SENSING": "required"},
                                     {"FLIGHTRISK_OBSTACLE_SENSING": " Required "}])
def test_a_single_tello_without_a_local_ip_fails_while_sensing_is_required(environ):
    rows = _by_name(run_preflight(_single(), EdgeToken(None, None), _one_adapter(environ=environ)))
    row = rows["Adapter for drone-1"]
    assert row.status == FAIL
    assert "Obstacle sensing is required" in row.detail and "refuses to start" in row.detail
    # It names the one adapter in the drone's network, and the way to fly without sensing.
    assert "en0 192.168.10.2 is the one adapter in its network" in row.detail
    assert "FLIGHTRISK_OBSTACLE_SENSING=off" in row.detail
    assert row.data == {"sensing": "required"}


def test_a_single_tello_without_a_local_ip_only_warns_with_sensing_off():
    env = _one_adapter(environ={"FLIGHTRISK_OBSTACLE_SENSING": "off"})
    checks = run_preflight(_single(), EdgeToken(None, None), env)
    row = _by_name(checks)["Adapter for drone-1"]
    assert row.status == WARN and "sensing is off" in row.detail and "capture stamps" in row.detail
    assert not [c for c in checks if c.status == FAIL]


def test_an_unknown_sensing_value_counts_as_required():
    """The edge refuses to start on a value it does not know, so it is never a reason to pass."""
    env = _one_adapter(environ={"FLIGHTRISK_OBSTACLE_SENSING": "maybe"})
    assert _by_name(run_preflight(_single(), EdgeToken(None, None), env))["Adapter for drone-1"].status == FAIL


def test_no_adapter_in_the_drones_network_says_to_join_its_wifi():
    env = _env(adapters=lambda: [Adapter("lo0", "127.0.0.1", 1), Adapter("en0", "10.0.0.5", 4)])
    row = _by_name(run_preflight(_single(), EdgeToken(None, None), env))["Adapter for drone-1"]
    assert row.status == FAIL and "join the drone's Wi-Fi" in row.detail


def test_a_drone_without_a_local_ip_is_still_probed_when_one_adapter_reaches_it():
    probed = []
    env = _one_adapter(tello_probe=lambda d: probed.append(d.drone_id) or TelloProbe(True, battery=90))
    rows = _by_name(run_preflight(_single(), EdgeToken(None, None), env))
    assert probed == ["drone-1"] and rows["Drone drone-1"].status == PASS


def test_a_local_ip_on_an_adapter_passes_and_one_on_no_adapter_fails_whatever_the_sensing():
    """Unchanged: an IP not on this computer makes the edge's binding check refuse to start, sensing or not."""
    for environ in ({}, {"FLIGHTRISK_OBSTACLE_SENSING": "off"}):
        env = _one_adapter(environ=environ)
        rows = _by_name(run_preflight(_single(local_ip="192.168.10.2"), EdgeToken(None, None), env))
        assert rows["Adapter for drone-1"].status == PASS
        rows = _by_name(run_preflight(_single(local_ip="192.168.10.9"), EdgeToken(None, None), env))
        assert rows["Adapter for drone-1"].status == FAIL and "not on any adapter" in rows["Adapter for drone-1"].detail


def test_the_existing_adapter_failures_are_not_repeated():
    """A drone already failed for an ambiguous or unreadable adapter list gets no second row."""
    two = _env(adapters=lambda: [Adapter("Wi-Fi", "192.168.10.2", 12), Adapter("Wi-Fi 2", "192.168.10.50", 23)])
    checks = run_preflight(_single(), EdgeToken(None, None), two)
    rows = [c for c in checks if c.name == "Adapter for drone-1"]
    assert len(rows) == 1 and "choose the adapter" in rows[0].detail
    checks = run_preflight(_single(), EdgeToken(None, None), _env(adapters=list))
    rows = [c for c in checks if c.name == "Adapter for drone-1"]
    assert len(rows) == 1 and "set the local IP" in rows[0].detail


def test_fake_drones_and_drones_flown_elsewhere_get_no_adapter_row():
    fake = LauncherSettings(drones=(DroneSettings(drone_id="drone-1", source="fake"),))
    elsewhere = LauncherSettings(network="cable", drones=(DroneSettings(drone_id="drone-1", run_here=False),))
    for settings in (fake, elsewhere):
        rows = _by_name(run_preflight(settings, EdgeToken(TOKEN, "x"), _one_adapter()))
        assert "Adapter for drone-1" not in rows


def test_two_tellos_each_without_a_local_ip_fail_with_the_sensing_reason_too():
    settings = LauncherSettings(role="edges", drones=(
        DroneSettings(drone_id="drone-1"),
        DroneSettings(drone_id="drone-2", alt_slot_cm=130),
    ), ground_host="10.42.0.1")
    rows = _by_name(run_preflight(settings, EdgeToken(TOKEN, "x"), _one_adapter()))
    assert rows["Adapter for drone-1"].status == FAIL and rows["Adapter for drone-2"].status == FAIL


# ---------------------------------------------------------------------------
# When the row may name an adapter (review of #200, finding 7)
# ---------------------------------------------------------------------------

NAMED = "en0 192.168.10.2 is the one adapter in its network"


def _row(settings, env):
    return _by_name(run_preflight(settings, EdgeToken(None, None), env))["Adapter for drone-1"]


def test_one_tello_and_one_safe_adapter_names_it():
    assert NAMED in _row(_single(), _one_adapter()).detail


def test_two_tellos_never_name_one_adapter_and_say_each_needs_its_own():
    settings = LauncherSettings(drones=(
        DroneSettings(drone_id="drone-1", tello_host="192.168.10.1"),
        DroneSettings(drone_id="drone-2", tello_host="192.168.10.1", local_ip="192.168.10.50"),
    ))
    env = _one_adapter()
    row = _row(settings, env)
    assert row.status == FAIL and "is the one adapter" not in row.detail and "en0" not in row.detail
    assert "each real Tello on this computer its own Wi-Fi adapter" in row.detail
    assert "docs/two-tellos-one-pc.md" in row.detail


def test_two_tellos_without_local_ips_warn_with_sensing_off_and_do_not_name_an_adapter():
    settings = LauncherSettings(drones=(
        DroneSettings(drone_id="drone-1"), DroneSettings(drone_id="drone-2"),
    ))
    env = _env(adapters=lambda: [Adapter("en0", "192.168.10.2", 14)],
               default_route=lambda: DefaultRoute("en7", 15), environ={"FLIGHTRISK_OBSTACLE_SENSING": "off"})
    row = _row(settings, env)
    assert row.status == WARN and "sensing is off" in row.detail and "capture stamps" in row.detail
    assert "en0" not in row.detail and "its own Wi-Fi adapter" in row.detail and "docs/two-tellos-one-pc.md" in row.detail


def test_an_adapter_that_is_another_drones_local_ip_is_not_named():
    settings = LauncherSettings(drones=(
        DroneSettings(drone_id="drone-1"),
        DroneSettings(drone_id="drone-2", source="mavlink", local_ip="192.168.10.2"),
    ))
    row = _row(settings, _one_adapter())
    assert NAMED not in row.detail and "en0" not in row.detail
    assert "set the local IP of the Wi-Fi adapter joined to drone-1" in row.detail


@pytest.mark.parametrize("route", [DefaultRoute("en0", None), DefaultRoute(None, 14), DefaultRoute("en0", 14)])
def test_the_default_route_adapter_is_not_named(route):
    row = _row(_single(), _one_adapter(default_route=lambda: route))
    assert row.status == FAIL and "en0" not in row.detail
    assert "not the internet" in row.detail


def test_an_unknown_default_route_fails_closed():
    assert "en0" not in _row(_single(), _one_adapter(default_route=lambda: None)).detail

    def boom():
        raise OSError("no route tool")

    assert "en0" not in _row(_single(), _one_adapter(default_route=boom)).detail


def test_default_route_parsers_and_reader():
    assert parse_route_get_default("   route to: default\n  interface: en0\n").name == "en0"
    assert parse_route_get_default("route: not in table") is None
    assert parse_ip_route_default("default via 10.0.0.1 dev wlan0 proto dhcp metric 600\n").name == "wlan0"
    assert parse_ip_route_default("") is None
    route = parse_get_netroute('[{"InterfaceIndex":9,"InterfaceAlias":"Wi-Fi 2","RouteMetric":50},'
                               '{"InterfaceIndex":4,"InterfaceAlias":"Ethernet","RouteMetric":5}]')
    assert route == DefaultRoute("Ethernet", 4)
    assert parse_get_netroute("not json") is None and parse_get_netroute("") is None
    assert default_route_interface(lambda argv: "", platform="linux") is None

    def failing(argv):
        raise OSError("missing")

    assert default_route_interface(failing, platform="darwin") is None
    assert default_route_interface(lambda argv: "default via 1.1.1.1 dev nonexist0", platform="linux").name == "nonexist0"
