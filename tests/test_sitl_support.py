"""Unit tests for tests/integration/sitl_support.py: no SITL, no mavsdk, no real sleeps."""
from __future__ import annotations

import io
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from flightrisk.drone.autopilot import AutopilotState
from tests.integration import sitl_support as ss

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process handling")

READY = AutopilotState(
    connected=True, autopilot="ardupilot", armed=False, in_air=False, gps_fix_type=3,
    global_position_ok=True, home_position_ok=True, armable=True,
)


class FakeLink:
    """A link whose state becomes ready on poll number ``ready_on`` (never when None)."""

    def __init__(self, ready_on=None, params=None, autopilot="ardupilot"):
        self.ready_on = ready_on
        self.params = params or {}
        self.autopilot = autopilot
        self.polls = 0
        self.disconnects = 0
        self.connects = 0

    def connect(self):
        self.connects += 1

    def disconnect(self):
        self.disconnects += 1

    def state(self):
        self.polls += 1
        if self.ready_on is not None and self.polls >= self.ready_on:
            return READY.__class__(**{**READY.__dict__, "autopilot": self.autopilot})
        return AutopilotState(connected=True, gps_fix_type=3, global_position_ok=True, armable=True)

    def get_param(self, name):
        return self.params.get(name)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)


def _broken(**changes):
    return ss.ready_problems(AutopilotState(**{**READY.__dict__, "stream_age_s": {}, **changes}))


def test_ready_state_has_no_problems():
    assert ss.ready_problems(READY) == []


@pytest.mark.parametrize("change, text", [
    ({"connected": False}, "not connected"),
    ({"gps_fix_type": 2}, "GPS fix"),
    ({"gps_fix_type": None}, "GPS fix"),
    ({"global_position_ok": False}, "global position"),
    ({"home_position_ok": None}, "home position"),
    ({"armable": False}, "armable"),
    ({"armed": True}, "armed"),
    ({"in_air": True}, "in the air"),
])
def test_ready_problems_names_each_missing_flag(change, text):
    problems = _broken(**change)
    assert len(problems) == 1 and text in problems[0]


def test_wait_ready_returns_report_on_third_poll_and_disconnects_once():
    link = FakeLink(ready_on=3, params={"MAV_SYSID": 4})
    report = ss.wait_ready("tcpout://127.0.0.1:5760", 60, lambda address: link, poll_s=0)
    assert (report.address, report.autopilot, report.sysid) == ("tcpout://127.0.0.1:5760", "ardupilot", 4)
    assert link.polls == 3 and link.disconnects == 1


def test_wait_ready_reads_the_autopilots_system_id_parameter():
    legacy = FakeLink(ready_on=1, params={"SYSID_THISMAV": 7})
    assert ss.wait_ready("a", 5, lambda a: legacy).sysid == 7
    px4 = FakeLink(ready_on=1, params={"MAV_SYS_ID": 2, "MAV_SYSID": 9}, autopilot="px4")
    assert ss.wait_ready("a", 5, lambda a: px4).sysid == 2
    assert ss.wait_ready("a", 5, lambda a: FakeLink(ready_on=1)).sysid is None


def test_wait_ready_times_out_naming_the_problem_and_still_disconnects(monkeypatch):
    ticks = iter(range(0, 1000, 10))
    monkeypatch.setattr(ss.time, "monotonic", lambda: next(ticks))
    link = FakeLink(ready_on=None)
    with pytest.raises(ss.SitlNotReady, match="home position"):
        ss.wait_ready("a", 25, lambda address: link, poll_s=0)
    assert link.disconnects == 1


def test_wait_ready_reports_a_failed_connect_and_disconnects():
    class Broken(FakeLink):
        def connect(self):
            raise RuntimeError("no heartbeat")

    link = Broken()
    with pytest.raises(ss.SitlNotReady, match="no heartbeat"):
        ss.wait_ready("a", 5, lambda address: link)
    assert link.disconnects == 1


def test_sitl_env_defaults_to_nothing():
    env = ss.sitl_env({})
    assert env == ss.SitlEnv(addresses=(), autopilot=None, version=None, sysids=(), ready_timeout_s=300.0)


def test_sitl_env_parses_the_harness_exports():
    env = ss.sitl_env({
        "FLIGHTRISK_SITL_ADDRESSES": "udpin://0.0.0.0:14540, udpin://0.0.0.0:14541",
        "FLIGHTRISK_SITL_SYSIDS": "1,2", "FLIGHTRISK_SITL_AUTOPILOT": "PX4",
        "FLIGHTRISK_SITL_VERSION": "1.17.0", "FLIGHTRISK_SITL_READY_TIMEOUT_S": "90",
    })
    assert env.addresses == ("udpin://0.0.0.0:14540", "udpin://0.0.0.0:14541")
    assert (env.sysids, env.autopilot, env.version, env.ready_timeout_s) == ((1, 2), "px4", "1.17.0", 90.0)


def test_sitl_env_falls_back_to_the_single_address_variables():
    env = ss.sitl_env({"FLIGHTRISK_SITL_ADDRESS": "tcpout://127.0.0.1:5760", "FLIGHTRISK_SITL_ADDRESS_2": "x://b"})
    assert env.addresses == ("tcpout://127.0.0.1:5760", "x://b") and env.sysids == (1, 1)


@pytest.mark.parametrize("env", [
    {"FLIGHTRISK_SITL_ADDRESSES": "a,b", "FLIGHTRISK_SITL_SYSIDS": "1"},
    {"FLIGHTRISK_SITL_ADDRESS": "a", "FLIGHTRISK_SITL_SYSIDS": "1,2"},
    {"FLIGHTRISK_SITL_ADDRESS": "a", "FLIGHTRISK_SITL_SYSIDS": "one"},
    {"FLIGHTRISK_SITL_AUTOPILOT": "betaflight"},
])
def test_sitl_env_rejects_bad_values(env):
    with pytest.raises(ValueError):
        ss.sitl_env(env)


def test_sysid_for_uses_the_list_else_one():
    env = {"FLIGHTRISK_SITL_ADDRESSES": "a,b", "FLIGHTRISK_SITL_SYSIDS": "3,5"}
    assert ss.sysid_for("b", env) == 5
    assert ss.sysid_for("elsewhere", env) == 1
    assert ss.sysid_for("a", {}) == 1


def test_require_autopilot_skips_on_a_mismatch_and_passes_on_a_match(monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_SITL_ADDRESS", "tcpout://127.0.0.1:5760")
    monkeypatch.setenv("FLIGHTRISK_SITL_AUTOPILOT", "px4")
    with pytest.raises(pytest.skip.Exception, match="ardupilot"):
        ss.require_autopilot("ardupilot")
    ss.require_autopilot("px4")


def test_require_autopilot_skips_without_an_address(monkeypatch):
    monkeypatch.delenv("FLIGHTRISK_SITL_ADDRESS", raising=False)
    monkeypatch.delenv("FLIGHTRISK_SITL_ADDRESSES", raising=False)
    with pytest.raises(pytest.skip.Exception):
        ss.require_autopilot("px4")


def test_require_autopilot_probes_once_when_undeclared(monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_SITL_ADDRESS", "tcpout://127.0.0.1:5760")
    monkeypatch.delenv("FLIGHTRISK_SITL_AUTOPILOT", raising=False)
    monkeypatch.setattr(ss, "_probed", {})
    calls = []

    def fake_wait_ready(address, timeout_s, link_factory=None):
        calls.append(address)
        return ss.ReadyReport(address, "ardupilot", 1, 0.0)

    monkeypatch.setattr(ss, "wait_ready", fake_wait_ready)
    ss.require_autopilot("ardupilot")
    with pytest.raises(pytest.skip.Exception):
        ss.require_autopilot("px4")
    assert calls == ["tcpout://127.0.0.1:5760"]


@posix_only
def test_child_servers_come_from_pgrep_of_this_process(monkeypatch):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="101\n102\n", stderr="")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    assert ss.child_mavsdk_servers() == [101, 102]
    assert seen == [["pgrep", "-P", str(os.getpid()), "-x", "mavsdk_server"]]


def test_child_servers_are_empty_on_windows(monkeypatch):
    monkeypatch.setattr(ss.sys, "platform", "win32")
    monkeypatch.setattr(ss.subprocess, "run", lambda *a, **k: pytest.fail("must not run a process"))
    assert ss.child_mavsdk_servers() == []
    assert ss.foreign_mavsdk_servers(["tcpout://127.0.0.1:5760"]) == []


@posix_only
def test_reaper_terminates_then_kills_the_survivor(monkeypatch):
    monkeypatch.setattr(ss, "child_mavsdk_servers", lambda: [201, 202])
    signals = []
    stubborn = {202}
    monkeypatch.setattr(ss.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(ss, "_alive", lambda pid: pid in stubborn)
    clock = iter(float(i) for i in range(0, 100))
    monkeypatch.setattr(ss.time, "monotonic", lambda: next(clock))
    assert ss.reap_child_mavsdk_servers() == [201, 202]
    import signal
    assert signals == [(201, signal.SIGTERM), (202, signal.SIGTERM), (202, signal.SIGKILL)]


def test_reaper_does_nothing_without_children(monkeypatch):
    monkeypatch.setattr(ss, "child_mavsdk_servers", lambda: [])
    monkeypatch.setattr(ss.os, "kill", lambda *a: pytest.fail("nothing to kill"))
    assert ss.reap_child_mavsdk_servers() == []


@posix_only
def test_foreign_servers_name_an_address_and_skip_own_children(monkeypatch):
    me = os.getpid()
    listing = "\n".join([
        f"  900     1 /x/bin/mavsdk_server -p 50051 tcpout://127.0.0.1:5760",
        f"  901     1 /x/bin/mavsdk_server -p 50052 udp://:14541",
        f"  902     1 /x/bin/mavsdk_server -p 50053 tcpout://127.0.0.1:5770",
        f"  903   {me} /x/bin/mavsdk_server -p 50054 tcpout://127.0.0.1:5760",
        f"  904     1 /usr/bin/python tcpout://127.0.0.1:5760",
    ])
    monkeypatch.setattr(ss.subprocess, "run",
                        lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout=listing, stderr=""))
    found = ss.foreign_mavsdk_servers(["tcpout://127.0.0.1:5760", "udpin://0.0.0.0:14541"])
    assert [pid for pid, _ in found] == [900, 901]


def test_wait_port_released_pauses_for_tcp(monkeypatch):
    pauses = []
    monkeypatch.setattr(ss.time, "sleep", pauses.append)
    ss.wait_port_released("tcpout://127.0.0.1:5760")
    assert pauses == [2.0]


def test_wait_port_released_binds_for_udp(monkeypatch):
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("0.0.0.0", 0))
    port = probe.getsockname()[1]
    probe.close()
    ss.wait_port_released(f"udpin://0.0.0.0:{port}", timeout_s=2)


def test_main_prints_one_line_per_address_and_exits_nonzero_if_any_fails():
    links = {
        "a": FakeLink(ready_on=1, params={"MAV_SYSID": 1}),
        "b": FakeLink(ready_on=None),
    }
    out = io.StringIO()
    code = ss.main(["wait-ready", "--timeout", "0", "--address", "a", "--address", "b"], links.__getitem__, out)
    lines = out.getvalue().splitlines()
    assert code == 1
    assert lines[0].startswith("READY a autopilot=ardupilot sysid=1 after ") and lines[0].endswith(" s")
    assert lines[1].startswith("NOT READY b: ") and "home position" in lines[1]


def test_main_exits_zero_when_all_ready():
    out = io.StringIO()
    link = FakeLink(ready_on=1, params={"MAV_SYS_ID": 2}, autopilot="px4")
    assert ss.main(["wait-ready", "--timeout", "5", "--address", "x"], lambda a: link, out) == 0
    assert out.getvalue().startswith("READY x autopilot=px4 sysid=2 after ")
