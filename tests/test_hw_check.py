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

    def __init__(self, video_port=None, port_supported=False, ap_supported=True, video_packets=10,
                 strict_source=False, edu_queries=True, raw_replies=None):
        # strict_source: like a real Tello, answer only the source port that sent "command".
        # edu_queries=False: like a standard Tello, "sdk?" and "sn?" are unknown commands.
        self.strict_source = strict_source
        self.edu_queries = edu_queries
        self.raw_replies = raw_replies or {}  # command -> exact reply bytes (for undecodable replies)
        self.locked_addr = None
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
            return "30" if self.edu_queries else "unknown command: sdk?"
        if cmd == "sn?":
            return "0TQDG12345" if self.edu_queries else "unknown command: sn?"
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
            if self.strict_source:
                if cmd == "command" and self.locked_addr is None:
                    self.locked_addr = addr
                if addr != self.locked_addr:
                    continue
            raw = self.raw_replies.get(cmd)
            self.sock.sendto(raw if raw is not None else self._reply(cmd).encode(), addr)
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


def test_commands_use_one_source_port_so_a_strict_tello_answers_all(capsys):
    # A real Tello answers only the port that sent "command"; a new socket per command used to time out.
    with FakeTello(strict_source=True) as drone:
        rc = hw.main(["--drones", "127.0.0.1", "--cmd-port", str(drone.port), "--timeout", "1", "--json"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert [c["status"] for c in doc["checks"]] == ["PASS"] * 5
    assert drone.received == ["command", "sdk?", "sn?", "battery?", "wifi?"]


def test_standard_tello_without_sdk_and_sn_queries_is_info_not_fail(capsys):
    with FakeTello(strict_source=True, edu_queries=False) as drone:
        rc = hw.main(["--drones", "127.0.0.1", "--cmd-port", str(drone.port), "--timeout", "1", "--json"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    by_name = {c["name"]: c for c in doc["checks"]}
    assert by_name["cmd:sdk?"]["status"] == "INFO"
    assert by_name["cmd:sn?"]["status"] == "INFO"
    assert by_name["cmd:battery?"]["status"] == "PASS"
    assert doc["overall"] == "PASS"


def test_real_command_port_sends_from_local_8889_like_djitellopy():
    try:
        sock = hw._command_socket("127.0.0.1", hw.CMD_PORT)
        local = sock.getsockname()[1]
    finally:
        hw.close_command_sockets()
    if local != hw.CMD_PORT:
        pytest.skip("local UDP 8889 is in use on this machine")
    assert local == 8889


def test_undecodable_reply_does_not_crash_the_report_on_a_cp1252_stdout(monkeypatch):
    # Seen on a real Tello: a reply with bytes that are not text became U+FFFD and crashed the table print.
    import io

    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stdout", stdout)
    with FakeTello(raw_replies={"wifi?": b"\xff\xfe9"}) as drone:
        rc = hw.main(["--drones", "127.0.0.1", "--cmd-port", str(drone.port), "--timeout", "1"])
    stdout.flush()
    text = raw.getvalue().decode("cp1252")
    assert rc == 0
    assert "cmd:wifi?" in text
    assert "OVERALL: PASS" in text


def test_command_sockets_are_closed_after_main():
    with FakeTello() as drone:
        hw.main(["--drones", "127.0.0.1", "--cmd-port", str(drone.port), "--timeout", "1"])
    assert hw._CMD_SOCKETS == {}


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
    # UDP delivery under load can drop packets, so assert only that some arrived. The fake sends 10
    # full datagrams and one short one, so at most 11 packets and one completed frame can arrive.
    packets = checks["video"]["value"]["packets"]
    frames = checks["video"]["value"]["frames"]
    assert 0 < packets <= 11 and frames <= 1
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
