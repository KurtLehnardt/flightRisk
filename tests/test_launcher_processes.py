"""The launcher's process manager with small stand-in child processes (flightrisk/launcher/processes.py).

The children are short Python scripts, never FlightRisk itself, so these tests
start no ground station, no edge and talk to no drone.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from flightrisk.launcher.commands import REDACTED, TOKEN_ENV, ProcessSpec, Redactor
from flightrisk.launcher.processes import (
    EXITED,
    NOT_STARTED,
    RUNNING,
    STOPPED,
    STOPPING,
    ProcessError,
    ProcessManager,
    new_run_log_dir,
    prune_log_runs,
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
    text, offset, _ = m.read_log("edge-drone-1", 0)
    assert "hello from edge-drone-1" in text
    assert TOKEN not in text and REDACTED in text
    assert offset > 0
    more, offset2, _ = m.read_log("edge-drone-1", offset)
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
    assert _wait(lambda: "\nignoring ctrl+c\n" in m.read_log("edge-drone-1", 0)[0])
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


class _Recorder:
    """A Popen factory for Windows tests on any platform: it records the Windows creation flags (which POSIX
    Popen refuses), every signal and terminate, and turns Ctrl+Break into SIGINT for the stand-in child."""

    def __init__(self):
        self.flags = []
        self.calls = []
        recorder = self

        class Child(subprocess.Popen):
            def send_signal(self, sig):
                recorder.calls.append(("signal", self.args[-1], sig))
                super().send_signal(signal.SIGINT)

            def terminate(self):
                recorder.calls.append(("terminate", self.args[-1], None))
                super().send_signal(signal.SIGKILL)

        self.child = Child

    def __call__(self, argv, **kwargs):
        self.flags.append(kwargs.pop("creationflags", None))
        return self.child(argv, **kwargs)


def _windows(manager, graceful, **kwargs):
    recorder = _Recorder()
    m = manager(popen=recorder, port_open=lambda host, port: True, windows=True, graceful=graceful, **kwargs)
    return m, recorder


def test_windows_with_a_console_asks_edges_to_land_with_ctrl_break_first(manager):
    m, rec = _windows(manager, graceful=True)
    m.start([_spec("ground", "ground", ready_ports=(("127.0.0.1", 5800),)), _spec("edge-drone-1")], wait=True)
    assert _wait(lambda: all(r["status"] == RUNNING for r in m.snapshot()))
    # A new process group that shares the launcher's console, and no CREATE_NO_WINDOW.
    assert rec.flags == [0x200, 0x200]
    m.stop_all()
    ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", 1)
    assert rec.calls == [("signal", "edge-drone-1", ctrl_break), ("signal", "ground", ctrl_break)]
    assert m.stop_lands_drones


def test_windows_edges_only_without_a_console_says_stop_does_not_land(manager):
    m, rec = _windows(manager, graceful=False)
    m.start([_spec("edge-drone-1"), _spec("edge-drone-2")], wait=True)
    assert rec.flags == [0x200 | 0x08000000] * 2
    assert not m.stop_lands_drones
    m.stop_all()
    assert sorted(c[:2] for c in rec.calls) == [("terminate", "edge-drone-1"), ("terminate", "edge-drone-2")]
    assert any("does not land" in e for e in m.events)
    assert {r["status"] for r in m.snapshot()} == {STOPPED}


def test_windows_without_a_console_stops_the_ground_first_and_waits_for_link_loss(manager):
    m, rec = _windows(manager, graceful=False, link_loss_wait_s=0.6)
    m.start([_spec("ground", "ground", ready_ports=(("127.0.0.1", 5800),)), _spec("edge-drone-1")], wait=True)
    assert _wait(lambda: all(r["status"] == RUNNING for r in m.snapshot()))
    started = time.monotonic()
    m.stop_all()
    assert [c[:2] for c in rec.calls] == [("terminate", "ground"), ("terminate", "edge-drone-1")]
    assert time.monotonic() - started >= 0.6
    assert any("link-loss" in e for e in m.events)


def test_link_loss_wait_follows_the_inherited_setting(manager):
    m, _ = _windows(manager, graceful=False)
    slow = replace(_spec("edge-drone-1"), env={**_spec("edge-drone-1").env, "FLIGHTRISK_LINK_LOSS_LAND_S": "15"})
    m.start([slow], wait=True)
    assert m.link_loss_wait_s() == 22.0
    m2, _ = _windows(manager, graceful=False)
    m2.start([_spec("edge-drone-2")], wait=True)
    assert m2.link_loss_wait_s() == 10.0


def test_windows_refuses_to_restart_an_edge_but_restarts_the_ground(manager):
    m, rec = _windows(manager, graceful=True)
    m.start([_spec("ground", "ground", ready_ports=(("127.0.0.1", 5800),)), _spec("edge-drone-1")], wait=True)
    assert _wait(lambda: all(r["status"] == RUNNING for r in m.snapshot()))
    with pytest.raises(ProcessError, match="not available on Windows"):
        m.restart("edge-drone-1")
    assert {r["name"]: r["restartable"] for r in m.snapshot()} == {"ground": True, "edge-drone-1": False}
    assert not any(c[1] == "edge-drone-1" for c in rec.calls), "a refused restart must not touch the edge"
    m.restart("ground")
    assert _wait(lambda: _status(m, "ground")["status"] == RUNNING)


def test_an_edge_that_had_to_be_terminated_is_not_restarted(manager):
    stubborn = "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\nprint('up', flush=True)\ntime.sleep(60)\n"
    m = manager(edge_grace_s=0.3)
    m.start([_spec("edge-drone-1", code=stubborn)], wait=True)
    assert _wait(lambda: "\nup\n" in m.read_log("edge-drone-1", 0)[0])
    m.restart("edge-drone-1")
    row = _status(m, "edge-drone-1")
    assert row["status"] == STOPPED and "not restarted" in row["message"]


def test_repeated_stop_signals_each_process_once(manager):
    calls = []

    class Recording(subprocess.Popen):
        def send_signal(self, sig):
            calls.append(self.args[-1])
            super().send_signal(sig)

    slow_exit = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGINT, lambda *a: (time.sleep(0.5), sys.exit(0)))\n"
        "print('up', flush=True)\n"
        "time.sleep(60)\n"
    )
    m = manager(popen=Recording)
    m.start([_spec("edge-drone-1", code=slow_exit)], wait=True)
    assert _wait(lambda: "\nup\n" in m.read_log("edge-drone-1", 0)[0])
    threads = [threading.Thread(target=m.stop_all) for _ in range(3)]
    for t in threads:
        t.start()
    assert _wait(lambda: m.stopping())
    for t in threads:
        t.join()
    assert calls == ["edge-drone-1"], "a second SIGINT would interrupt the edge's landing"
    assert _status(m, "edge-drone-1")["status"] == STOPPED


def test_overlapping_restarts_and_restart_during_stop_are_refused(manager):
    slow_exit = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGINT, lambda *a: (time.sleep(0.5), sys.exit(0)))\n"
        "print('up', flush=True)\n"
        "time.sleep(60)\n"
    )
    m = manager(port_open=lambda host, port: True)
    m.start([_spec("ground", "ground", code=slow_exit, ready_ports=(("127.0.0.1", 5800),)),
             _spec("edge-drone-1", code=slow_exit)], wait=True)
    assert _wait(lambda: all(r["status"] == RUNNING for r in m.snapshot()))
    m.restart("ground", background=True)
    with pytest.raises(ProcessError, match="already restarting"):
        m.restart("ground", background=True)
    with pytest.raises(ProcessError, match="in progress"):
        m.restart("edge-drone-1", background=True)
    assert _wait(lambda: _status(m, "ground")["status"] == RUNNING and not m._op_lock.locked())
    # A stop that is running refuses a restart, and nothing is started again after it.
    stopper = threading.Thread(target=m.stop_all)
    stopper.start()
    assert _wait(lambda: m.stopping())
    with pytest.raises(ProcessError):
        m.restart("ground", background=True)
    stopper.join()
    assert {r["status"] for r in m.snapshot()} == {STOPPED}
    pids = {r["name"]: r["pid"] for r in m.snapshot()}
    assert pids == {"ground": None, "edge-drone-1": None}


def test_two_launchers_never_share_log_files(tmp_path):
    root = tmp_path / "logs"
    a = ProcessManager(Redactor(TOKEN), log_dir=new_run_log_dir(root, 5500), cwd=tmp_path)
    b = ProcessManager(Redactor(TOKEN), log_dir=new_run_log_dir(root, 5501), cwd=tmp_path)
    try:
        a.start([_spec("ground", "ground", code="print('live ground', flush=True); import time; time.sleep(60)")], wait=True)
        b.start([_spec("ground", "ground", code="print('rehearsal', flush=True); import time; time.sleep(60)")], wait=True)
        assert _wait(lambda: "live ground" in a.read_log("ground", 0)[0] and "rehearsal" in b.read_log("ground", 0)[0])
        assert a.log_dir != b.log_dir
        assert "rehearsal" not in a.read_log("ground", 0)[0]
        assert not list(root.rglob("*.previous.log"))
    finally:
        a.stop_all()
        b.stop_all()


def test_restart_writes_a_new_log_file_and_keeps_the_old_one(manager):
    m = manager()
    m.start([_spec("edge-drone-1")], wait=True)
    first = Path(_status(m, "edge-drone-1")["log"])
    m.restart("edge-drone-1")
    second = Path(_status(m, "edge-drone-1")["log"])
    assert first != second and first.exists() and second.name == "edge-drone-1.restart-1.log"
    assert _wait(lambda: m.read_log("edge-drone-1", 0)[2] == str(second))


def test_old_run_folders_are_pruned(tmp_path):
    root = tmp_path / "logs"
    for k in range(25):
        run = root / f"20261029-0000{k:02d}-5500"
        run.mkdir(parents=True)
        (run / "ground.log").write_text("x" * 10)
    (root / "keep-me").mkdir()
    current = root / "20261029-000099-5500"
    removed = prune_log_runs(root, keep=20, current=current)
    assert len(removed) == 6 and removed[0].name == "20261029-000000-5500"
    assert (root / "keep-me").exists()
    big = prune_log_runs(root, keep=100, max_bytes=50)
    assert len([p for p in root.iterdir() if p.name != "keep-me"]) == 5 and len(big) == 14


def test_log_tail_never_cuts_a_line(manager, tmp_path):
    m = manager()
    line = "x" * 50 + TOKEN + "\n"
    m.start([_spec("edge-drone-1", code=f"import sys; sys.stdout.write({line!r} * 3000); sys.stdout.flush()")], wait=True)
    assert _wait(lambda: _status(m, "edge-drone-1")["status"] == EXITED)
    for limit in (1000, 1001, 1033, 4096):
        text, _, _ = m.read_log("edge-drone-1", 0, limit=limit)
        assert text, limit
        for row in text.splitlines():
            assert row == "x" * 50 + REDACTED, (limit, row[:20])
        assert TOKEN[-6:] not in text


def test_a_start_that_fails_with_a_bad_value_does_not_leave_the_launcher_busy(manager):
    m = manager()
    bad = replace(_spec("edge-drone-1"), env={"PATH": "/usr/bin", "BAD": "a\x00b"})
    m.start([bad], wait=True)
    row = _status(m, "edge-drone-1")
    assert row["status"] == EXITED and "could not start" in row["message"]
    assert not m.busy()


def test_stopping_status_is_reported(manager):
    slow_exit = "import signal, sys, time\nsignal.signal(signal.SIGINT, lambda *a: (time.sleep(0.6), sys.exit(0)))\nprint('up', flush=True)\ntime.sleep(60)\n"
    m = manager()
    m.start([_spec("edge-drone-1", code=slow_exit)], wait=True)
    assert _wait(lambda: "\nup\n" in m.read_log("edge-drone-1", 0)[0])
    stopper = threading.Thread(target=m.stop_all)
    stopper.start()
    assert _wait(lambda: _status(m, "edge-drone-1")["status"] == STOPPING)
    stopper.join()
