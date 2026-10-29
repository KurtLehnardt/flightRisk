"""The launcher's process manager with small stand-in child processes (flightrisk/launcher/processes.py).

The children are short Python scripts, never FlightRisk itself, so these tests
start no ground station, no edge and talk to no drone.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from flightrisk.launcher.commands import REDACTED, TOKEN_ENV, ProcessSpec, Redactor
from flightrisk.launcher.processes import (
    EXITED,
    NOT_STARTED,
    RUNNING,
    STOPPED,
    ProcessError,
    ProcessManager,
)

TOKEN = "proc-test-token-abc123"

SLEEPER = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGINT, lambda *a: sys.exit(0))\n"
    "print('hello from', sys.argv[1], flush=True)\n"
    "print('token is', os.environ.get('" + TOKEN_ENV + "'), flush=True)\n"
    "time.sleep(60)\n"
)


def _spec(name, kind="edge", code=SLEEPER, ready_ports=()):
    return ProcessSpec(
        name=name, kind=kind, argv=(sys.executable, "-c", code, name),
        env={"PATH": "/usr/bin:/bin", TOKEN_ENV: TOKEN, "PYTHONUNBUFFERED": "1", "SYSTEMROOT": "C:\\Windows"},
        ready_ports=ready_ports,
    )


def _wait(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _status(manager, name):
    return {row["name"]: row for row in manager.snapshot()}[name]


@pytest.fixture
def manager(tmp_path):
    managers = []

    def make(**kwargs):
        kwargs.setdefault("log_dir", tmp_path / "logs")
        kwargs.setdefault("cwd", tmp_path)
        kwargs.setdefault("edge_grace_s", 5.0)
        kwargs.setdefault("ground_grace_s", 5.0)
        kwargs.setdefault("poll_s", 0.05)
        m = ProcessManager(Redactor(TOKEN), **kwargs)
        managers.append(m)
        return m

    yield make
    for m in managers:
        m.stop_all()


def test_ground_starts_first_and_edges_wait_until_it_listens(manager):
    ready = threading.Event()
    spawned = []

    def popen(argv, **kwargs):
        spawned.append(argv[-1])
        return subprocess.Popen(argv, **kwargs)

    m = manager(popen=popen, port_open=lambda host, port: ready.is_set())
    m.start([_spec("ground", "ground", ready_ports=(("127.0.0.1", 5800),)), _spec("edge-drone-1"), _spec("edge-drone-2")])
    assert _wait(lambda: spawned == ["ground"])
    time.sleep(0.4)
    assert spawned == ["ground"], "edges must not start before the ground listens"
    assert _status(m, "edge-drone-1")["status"] == "pending"
    ready.set()
    assert _wait(lambda: all(r["status"] == RUNNING for r in m.snapshot()))
    assert spawned == ["ground", "edge-drone-1", "edge-drone-2"]


def test_edges_are_not_started_when_the_ground_exits(manager):
    m = manager(port_open=lambda host, port: False)
    m.start([_spec("ground", "ground", code="import sys; sys.exit(4)", ready_ports=(("127.0.0.1", 5800),)), _spec("edge-drone-1")],
            wait=True)
    ground = _status(m, "ground")
    assert ground["status"] == EXITED and ground["returncode"] == 4
    edge = _status(m, "edge-drone-1")
    assert edge["status"] == NOT_STARTED and "ground" in edge["message"]


def test_ready_timeout_stops_the_sequence(manager):
    m = manager(port_open=lambda host, port: False, ready_timeout_s=0.5)
    m.start([_spec("ground", "ground", ready_ports=(("127.0.0.1", 5800),)), _spec("edge-drone-1")], wait=True)
    assert _status(m, "edge-drone-1")["status"] == NOT_STARTED
    assert "not listening" in _status(m, "ground")["message"]


def test_exit_code_is_reported(manager):
    m = manager()
    m.start([_spec("edge-drone-1", code="import sys; print('bye'); sys.exit(3)")], wait=True)
    assert _wait(lambda: _status(m, "edge-drone-1")["status"] == EXITED)
    assert _status(m, "edge-drone-1")["returncode"] == 3


def test_log_is_written_and_read_back_without_the_token(manager, tmp_path):
    m = manager()
    m.start([_spec("edge-drone-1")], wait=True)
    assert _wait(lambda: f"token is {REDACTED}" in m.read_log("edge-drone-1", 0)[0])
    text, offset = m.read_log("edge-drone-1", 0)
    assert "hello from edge-drone-1" in text
    assert TOKEN not in text and REDACTED in text
    assert offset > 0
    more, offset2 = m.read_log("edge-drone-1", offset)
    assert more == "" and offset2 == offset
    # Once the child exits, its log file is scrubbed too.
    m.stop_all()
    log = (tmp_path / "logs" / "edge-drone-1.log").read_text()
    assert TOKEN not in log and "hello from" in log
    # The command line written at the top of the log never carried the token.
    assert log.startswith("# edge-drone-1 started by the FlightRisk launcher")


def test_restart_replaces_the_process(manager):
    m = manager()
    m.start([_spec("edge-drone-1")], wait=True)
    first = _status(m, "edge-drone-1")["pid"]
    m.restart("edge-drone-1")
    assert _wait(lambda: _status(m, "edge-drone-1")["status"] == RUNNING and _status(m, "edge-drone-1")["pid"] != first)
    with pytest.raises(ProcessError):
        m.restart("not-mine")


def test_stop_all_stops_edges_before_ground_and_only_own_processes(manager):
    order = []

    class Recording(subprocess.Popen):
        def send_signal(self, sig):
            order.append(self.args[-1])
            super().send_signal(sig)

        def terminate(self):
            order.append(self.args[-1])
            super().terminate()

    m = manager(popen=Recording, port_open=lambda host, port: True)
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        m.start([_spec("ground", "ground", ready_ports=(("127.0.0.1", 5800),)), _spec("edge-drone-1")], wait=True)
        assert _wait(lambda: all(r["status"] == RUNNING for r in m.snapshot()))
        m.stop_all()
        assert order == ["edge-drone-1", "ground"]
        assert {r["status"] for r in m.snapshot()} == {STOPPED}
        assert stranger.poll() is None, "a process the launcher did not start must keep running"
    finally:
        stranger.kill()
        stranger.wait()


def test_stubborn_child_is_terminated_after_the_grace_period(manager):
    stubborn = (
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "print('ignoring ctrl+c', flush=True)\n"
        "time.sleep(60)\n"
    )
    m = manager(edge_grace_s=0.5)
    m.start([_spec("edge-drone-1", code=stubborn)], wait=True)
    assert _wait(lambda: "ignoring" in m.read_log("edge-drone-1", 0)[0])
    started = time.monotonic()
    m.stop_all()
    assert time.monotonic() - started < 10
    assert _status(m, "edge-drone-1")["status"] == STOPPED


def test_second_start_is_refused_while_running(manager):
    m = manager()
    m.start([_spec("edge-drone-1")], wait=True)
    with pytest.raises(ProcessError):
        m.start([_spec("edge-drone-2")])
    with pytest.raises(ProcessError):
        m.stop_one("someone-else")


def test_duplicate_names_are_refused(manager):
    with pytest.raises(ProcessError):
        manager().start([_spec("edge-drone-1"), _spec("edge-drone-1")])


def test_snapshot_and_leave_running_show_no_token(manager):
    m = manager()
    m.start([_spec("edge-drone-1")], wait=True)
    rows = m.leave_running()
    assert [r["name"] for r in rows] == ["edge-drone-1"]
    assert TOKEN not in repr(m.snapshot()) and TOKEN not in "\n".join(m.events)


def test_windows_stops_the_ground_first_and_gives_edges_the_link_loss_wait(manager):
    order = []

    class Recording(subprocess.Popen):
        def terminate(self):
            order.append(("terminate", self.args[-1]))
            super().terminate()

    m = manager(popen=Recording, port_open=lambda host, port: True, windows=True, link_loss_wait_s=0.6)
    m.start([_spec("ground", "ground", ready_ports=(("127.0.0.1", 5800),)), _spec("edge-drone-1")], wait=True)
    assert _wait(lambda: all(r["status"] == RUNNING for r in m.snapshot()))
    started = time.monotonic()
    m.stop_all()
    assert order == [("terminate", "ground"), ("terminate", "edge-drone-1")]
    assert time.monotonic() - started >= 0.6
    assert any("link-loss" in e for e in m.events)
    assert {r["status"] for r in m.snapshot()} == {STOPPED}
