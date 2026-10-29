"""Launcher preflight checks with injected fakes (flightrisk/launcher/preflight.py).

No packet is sent to any drone: the Tello probe runs against a fake socket.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from flightrisk.launcher.commands import EdgeToken
from flightrisk.launcher.netinfo import Adapter
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
