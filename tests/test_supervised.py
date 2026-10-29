"""flightrisk.drone.supervised: a child that dies with the edge, however the edge dies (N2).

A stand-in child (a sleeping Python process that writes its pid) is started
by a parent process through ``spawn_tied_to_parent``; the parent is then
killed with SIGKILL, as the OOM killer would. The child must be gone within
a few seconds. Nothing here talks to an autopilot.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import flightrisk.drone.mavsdk_link as link_mod

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX supervisor")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); ask ps whether it is defunct.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
    return bool(state) and not state.startswith("Z")


def test_child_dies_when_the_parent_is_killed(tmp_path):
    pid_file = tmp_path / "child.pid"
    child = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(120)"
    parent_code = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from flightrisk.drone.supervised import spawn_tied_to_parent\n"
        f"spawn_tied_to_parent([sys.executable, '-c', {child!r}])\n"
        "time.sleep(120)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code])
    try:
        deadline = time.monotonic() + 20
        while not (pid_file.exists() and pid_file.read_text()):
            assert time.monotonic() < deadline, "the child never started"
            time.sleep(0.05)
        child_pid = int(pid_file.read_text())
        assert _alive(child_pid)
        parent.send_signal(signal.SIGKILL)
        parent.wait(10)
        deadline = time.monotonic() + 6
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(child_pid), "the child outlived its SIGKILLed parent"
    finally:
        parent.kill()
        if pid_file.exists() and pid_file.read_text():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_terminate_stops_the_child(tmp_path):
    from flightrisk.drone.supervised import spawn_tied_to_parent

    pid_file = tmp_path / "child.pid"
    proc = spawn_tied_to_parent(
        [sys.executable, "-c", f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(120)"]
    )
    deadline = time.monotonic() + 20
    while not (pid_file.exists() and pid_file.read_text()):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    child_pid = int(pid_file.read_text())
    proc.terminate()
    proc.wait(10)
    deadline = time.monotonic() + 5
    while _alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(child_pid)


def test_the_link_runs_its_server_under_the_supervisor_for_real_mavsdk(monkeypatch):
    started = []

    class FakeReal:
        pass

    monkeypatch.setattr(link_mod, "System", FakeReal)
    monkeypatch.setattr(link_mod, "_REAL_SYSTEM", FakeReal)
    from flightrisk.drone import supervised

    monkeypatch.setattr(supervised, "spawn_tied_to_parent", lambda cmd, **kw: started.append(cmd) or object())
    link = link_mod.MavsdkAutopilotLink("serial:///dev/ttyAMA0:921600")
    assert link._start_own_server(54321) == "127.0.0.1"
    assert started and started[0][1:] == ["-p", "54321", "serial:///dev/ttyAMA0:921600"]
    assert started[0][0].endswith("mavsdk_server")


def test_a_fake_mavsdk_never_starts_a_server(monkeypatch):
    monkeypatch.setattr(link_mod, "System", type("Fake", (), {}))
    assert link_mod.MavsdkAutopilotLink("tcpout://127.0.0.1:1")._start_own_server(1) is None


@pytest.mark.parametrize("address", ["-p", "udp://:14540", "tcpout://", "nonsense"])
def test_link_refuses_bad_addresses(address):
    with pytest.raises(ValueError, match="MAVLink address must look like"):
        link_mod.MavsdkAutopilotLink(address)
