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


# ---------------------------------------------------------------------------
# mavsdk version check, server binary lookup and the Windows Job Object path.
# These run on every platform: the platform and the Windows API are faked.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["4.0.0", "4.2.1", "5.1"])
def test_mavsdk_4_and_later_is_refused_with_the_reason(version):
    with pytest.raises(RuntimeError, match=r"native binding.*mavsdk-grpc.*<4"):
        link_mod.check_mavsdk_version(version)


@pytest.mark.parametrize("version", ["3.17.2", "3.99.0", "2.0.0", None, "", "dev"])
def test_mavsdk_3_and_unknown_versions_pass(version):
    link_mod.check_mavsdk_version(version)


def test_connect_refuses_real_mavsdk_4(monkeypatch):
    class FakeReal:
        pass

    monkeypatch.setattr(link_mod, "System", FakeReal)
    monkeypatch.setattr(link_mod, "_REAL_SYSTEM", FakeReal)
    monkeypatch.setattr(link_mod, "_installed_mavsdk_version", lambda: "4.2.1")
    with pytest.raises(RuntimeError, match="mavsdk 4.2.1 is installed"):
        link_mod.MavsdkAutopilotLink("tcpout://127.0.0.1:1").connect()


@pytest.mark.parametrize("windows, expected", [(False, "mavsdk_server"), (True, "mavsdk_server.exe")])
def test_server_binary_lookup_per_platform(monkeypatch, tmp_path, windows, expected):
    monkeypatch.setattr(link_mod, "_is_windows", lambda: windows)
    (tmp_path / "mavsdk_server").write_text("posix")
    (tmp_path / "mavsdk_server.exe").write_text("windows")
    assert link_mod.server_binary_name() == expected
    assert link_mod.locate_server_binary(tmp_path) == tmp_path / expected


@pytest.mark.parametrize("windows, present", [(True, "mavsdk_server"), (False, "mavsdk_server.exe")])
def test_server_binary_lookup_does_not_take_the_other_platforms_file(monkeypatch, tmp_path, windows, present):
    monkeypatch.setattr(link_mod, "_is_windows", lambda: windows)
    (tmp_path / present).write_text("x")
    with pytest.raises(FileNotFoundError):
        link_mod.locate_server_binary(tmp_path)


class _FakeKernel32:
    def __init__(self, assign_ok=True):
        self.calls = []
        self.assign_ok = assign_ok
        self.flags = None

        def create(attrs, name):
            self.calls.append("create")
            return 4242

        create.restype = None  # a plain function accepts attributes, as a ctypes function pointer does
        self.CreateJobObjectW = create

    def SetInformationJobObject(self, job, info_class, info_ref, size):
        self.calls.append(("set", info_class))
        self.flags = info_ref._obj.BasicLimitInformation.LimitFlags
        return 1

    def AssignProcessToJobObject(self, job, process):
        self.calls.append(("assign", job.value, process.value))
        return 1 if self.assign_ok else 0

    def CloseHandle(self, handle):
        self.calls.append("close")


class _FakePopen:
    def __init__(self, cmd, **kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        self._handle = 777
        self.stdout = iter([b"hello\n"])
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


def _windows_env(monkeypatch, kernel32):
    from flightrisk.drone import supervised

    monkeypatch.setattr(supervised, "_is_windows", lambda: True)
    monkeypatch.setattr(supervised, "_kernel32", lambda: kernel32)
    monkeypatch.setattr(supervised, "_job_handle", None)
    monkeypatch.setattr(supervised.subprocess, "Popen", _FakePopen)
    return supervised


def test_windows_child_goes_into_a_kill_on_close_job(monkeypatch):
    kernel32 = _FakeKernel32()
    supervised = _windows_env(monkeypatch, kernel32)
    proc = supervised.spawn_tied_to_parent(["mavsdk_server.exe", "-p", "1", "tcpout://127.0.0.1:5760"])
    assert proc.cmd == ["mavsdk_server.exe", "-p", "1", "tcpout://127.0.0.1:5760"]  # no /bin/sh
    assert kernel32.flags == 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert ("set", 9) in kernel32.calls
    assert ("assign", 4242, 777) in kernel32.calls
    assert not proc.terminated


def test_windows_job_is_created_once_for_several_children(monkeypatch):
    kernel32 = _FakeKernel32()
    supervised = _windows_env(monkeypatch, kernel32)
    supervised.spawn_tied_to_parent(["a.exe"])
    supervised.spawn_tied_to_parent(["b.exe"])
    assert kernel32.calls.count("create") == 1
    assert sum(1 for c in kernel32.calls if isinstance(c, tuple) and c[0] == "assign") == 2


def test_windows_falls_back_to_atexit_cleanup_when_the_job_is_refused(monkeypatch, caplog):
    kernel32 = _FakeKernel32(assign_ok=False)
    supervised = _windows_env(monkeypatch, kernel32)
    registered = []
    monkeypatch.setattr(supervised.atexit, "register", lambda fn: registered.append(fn))
    with caplog.at_level("WARNING"):
        proc = supervised.spawn_tied_to_parent(["mavsdk_server.exe"])
    assert "clean exit only" in caplog.text
    assert len(registered) == 1
    registered[0]()
    assert proc.terminated
