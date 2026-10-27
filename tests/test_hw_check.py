"""Tests for scripts/tello_hw_check.py against a fake UDP Tello and a loopback WebSocket peer."""

import importlib.util
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("tello_hw_check", ROOT / "scripts" / "tello_hw_check.py")
hw = importlib.util.module_from_spec(_spec)
sys.modules["tello_hw_check"] = hw
_spec.loader.exec_module(hw)


class FakeTello:
    """UDP responder on 127.0.0.1 that mimics a Tello EDU command socket."""

    def __init__(self, video_port=None, port_supported=False, ap_supported=True, video_packets=10):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.video_port = video_port
        self.port_supported = port_supported
        self.ap_supported = ap_supported
        self.video_packets = video_packets
        self.received = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()

    def _reply(self, cmd):
        if cmd == "command":
            return "ok"
        if cmd == "sdk?":
            return "30"
        if cmd == "sn?":
            return "0TQDG12345"
        if cmd == "battery?":
            return "87"
        if cmd == "wifi?":
            return "90"
        if cmd == "streamon":
            return "ok"
        if cmd == "streamoff":
            return "ok"
        if cmd.startswith("port "):
            return "ok" if self.port_supported else "unknown command: port"
        if cmd.startswith("ap "):
            return "OK,drone will reboot in 3s" if self.ap_supported else "unknown command: ap"
        return "error"

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            cmd = data.decode().strip()
            self.received.append(cmd)
            self.sock.sendto(self._reply(cmd).encode(), addr)
            if cmd == "streamon" and self.video_port:
                for _ in range(self.video_packets):
                    self.sock.sendto(b"\x00" * 1460, ("127.0.0.1", self.video_port))
                self.sock.sendto(b"\x00" * 200, ("127.0.0.1", self.video_port))


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_commands_against_fake_udp_drone(capsys):
    with FakeTello() as drone:
        rc = hw.main(["--drones", "127.0.0.1", "--cmd-port", str(drone.port), "--timeout", "2", "--json"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == 1
    assert doc["overall"] == "PASS"
    assert [c["name"] for c in doc["checks"]] == ["cmd:command", "cmd:sdk?", "cmd:sn?", "cmd:battery?", "cmd:wifi?"]
    assert {c["name"]: c["value"] for c in doc["checks"]}["cmd:battery?"] == "87"
    assert drone.received == ["command", "sdk?", "sn?", "battery?", "wifi?"]
    assert set(doc["checks"][0]) == {"name", "target", "status", "detail", "value"}


def test_unreachable_drone_fails(capsys):
    port = _free_udp_port()
    rc = hw.main(["--drones", "127.0.0.1", "--cmd-port", str(port), "--timeout", "0.2"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out and "OVERALL: FAIL" in out


def test_station_mode_requires_explicit_flag(capsys):
    with FakeTello() as drone:
        with pytest.raises(SystemExit) as exc:
            hw.main(["--station-mode", "ssid", "pw", "--drones", "127.0.0.1", "--cmd-port", str(drone.port)])
        assert exc.value.code == 2
        assert drone.received == []
        # The function itself also refuses without the acknowledgement.
        with pytest.raises(PermissionError):
            hw.station_mode("127.0.0.1", "ssid", "pw", acknowledged=False, port=drone.port)
        assert drone.received == []
        # Never default behaviour: plain runs never send ap.
        hw.main(["--drones", "127.0.0.1", "--cmd-port", str(drone.port), "--timeout", "2"])
        assert not any(c.startswith("ap ") for c in drone.received)
    capsys.readouterr()


def test_station_mode_with_flag_sends_ap(capsys):
    with FakeTello() as drone:
        rc = hw.main([
            "--station-mode", "home", "secret", hw.ACK_FLAG,
            "--drones", "127.0.0.1", "--cmd-port", str(drone.port), "--timeout", "2", "--json",
        ])
        assert rc == 0
        assert drone.received == ["command", "ap home secret"]
    assert json.loads(capsys.readouterr().out)["overall"] == "PASS"


def test_station_mode_unknown_ap_command_fails(capsys):
    with FakeTello(ap_supported=False) as drone:
        rc = hw.main([
            "--station-mode", "home", "secret", hw.ACK_FLAG,
            "--drones", "127.0.0.1", "--cmd-port", str(drone.port), "--timeout", "2",
        ])
    assert rc == 1
    assert "ap rejected" in capsys.readouterr().out


def test_station_mode_rejects_multiple_drones():
    with pytest.raises(SystemExit):
        hw.main(["--station-mode", "a", "b", hw.ACK_FLAG, "--drones", "127.0.0.1,127.0.0.2"])


def test_discover_finds_fake_drone(capsys):
    with FakeTello() as drone:
        rc = hw.main(["--discover", "127.0.0.1/32", "--cmd-port", str(drone.port), "--timeout", "1", "--json"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["checks"][0]["name"] == "discover"
    assert doc["checks"][0]["value"] == ["127.0.0.1"]


def test_discover_none_found_and_bad_subnet():
    res = hw.discover("127.0.0.1/32", port=_free_udp_port(), timeout=0.3)
    assert res[0].status == hw.FAIL
    assert hw.discover("not-a-subnet")[0].status == hw.FAIL
    assert hw.discover("10.0.0.0/8")[0].status == hw.FAIL


def test_port_probe_reports_unknown_command():
    with FakeTello(port_supported=False) as drone:
        res = hw.probe_port("127.0.0.1", port=drone.port, timeout=2)
    assert res.value == "unknown command"
    assert res.status == hw.INFO


def test_port_probe_reports_ok_and_restores_default():
    with FakeTello(port_supported=True) as drone:
        res = hw.probe_port("127.0.0.1", port=drone.port, timeout=2)
        assert res.value == "ok"
        assert drone.received == ["port 8890 11112", "port 8890 11111"]


def test_video_check_counts_packets_and_probes_port(capsys):
    vport = _free_udp_port()
    with FakeTello(video_port=vport, video_packets=10) as drone:
        rc = hw.main([
            "--drones", "127.0.0.1", "--video-check", "--cmd-port", str(drone.port),
            "--video-port", str(vport), "--video-seconds", "1", "--timeout", "2", "--json",
        ])
    doc = json.loads(capsys.readouterr().out)
    checks = {c["name"]: c for c in doc["checks"]}
    assert checks["video"]["status"] == "PASS"
    # Intended de-flake: UDP delivery under load can drop packets, so assert only that some arrived.
    assert checks["video"]["value"]["packets"] > 0
    assert checks["port-probe"]["value"] == "unknown command"
    assert rc == 0


def test_video_check_fails_without_video():
    vport = _free_udp_port()
    with FakeTello(video_port=None) as drone:
        res = hw.video_check("127.0.0.1", port=drone.port, video_port=vport, seconds=0.5, timeout=2)
    assert res[0].status == hw.FAIL


def test_ap_mode_check_round_trips(capsys):
    with FakeTello() as drone:
        rc = hw.main(["--ap-mode-check", "--drones", "127.0.0.1", "--cmd-port", str(drone.port), "--timeout", "2"])
    assert rc == 0
    assert "ap:sn?" in capsys.readouterr().out


def test_link_check_server_client_loopback():
    started = threading.Event()
    stop = threading.Event()
    box = {}

    def serve():
        box["server"] = hw.link_check_server(
            "127.0.0.1", "127.0.0.1", port=0, serve_seconds=30,
            ping_fn=lambda host: (True, "fake ping"),
            ready=lambda p: (box.__setitem__("port", p), started.set()),
            stop_event=stop,
        )

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert started.wait(10)
    try:
        client = hw.link_check_client(
            "127.0.0.1", port=box["port"], timeout=5, ping_fn=lambda host: (True, "fake ping")
        )
    finally:
        stop.set()
        t.join(timeout=10)
    by_name = {r.name: r for r in client}
    assert by_name["link:ping"].status == hw.PASS
    assert by_name["link:websocket"].status == hw.PASS
    assert isinstance(by_name["link:websocket"].value, float)
    server = {r.name: r for r in box["server"]}
    assert server["link:listen"].status == hw.PASS
    assert server["link:ping"].status == hw.PASS


def test_link_check_client_fails_when_no_listener():
    port = _free_udp_port()  # a UDP port number that has no TCP listener
    res = hw.link_check_client("127.0.0.1", port=port, timeout=1, ping_fn=lambda h: (False, "no reply"))
    by_name = {r.name: r for r in res}
    assert by_name["link:ping"].status == hw.FAIL
    assert by_name["link:websocket"].status == hw.FAIL


def test_link_args_validated():
    with pytest.raises(SystemExit):
        hw.main(["--fleet-net", "ap", "--link-role", "client"])
    with pytest.raises(SystemExit):
        hw.main(["--fleet-net", "ap"])
    with pytest.raises(SystemExit):
        hw.main(["--fleet-net", "ap", "--link-role", "client", "--peer", "not an ip; rm -rf"])


def test_dataset_and_demo_readmes():
    datasets = (ROOT / "datasets" / "README.md").read_text()
    demo = (ROOT / "demo" / "README.md").read_text()
    assert "CC-BY-3.0" in datasets
    assert "zenodo" in datasets.lower()
    assert "never committed" in datasets
    assert "two minutes" in demo or "2:00" in demo
