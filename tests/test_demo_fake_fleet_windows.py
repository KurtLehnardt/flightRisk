"""scripts/demo_fake_fleet.py on Windows: it must import, and it must not use POSIX-only calls there."""

import importlib.util
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "demo_fake_fleet.py"


def _load(name="demo_fake_fleet_windows"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launcher():
    return _load()


def test_module_imports_when_signal_has_no_sigkill(monkeypatch):
    monkeypatch.delattr(signal, "SIGKILL", raising=False)
    module = _load("demo_fake_fleet_no_sigkill")
    assert module.ProcessGroup is not None
    # monkeypatch restores signal.SIGKILL on teardown; a fresh load then sees it again.


def test_windows_spawn_uses_a_new_process_group(launcher, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    popen = MagicMock()
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    launcher.ProcessGroup().spawn("drone-1", ["x"], {})
    kwargs = popen.call_args.kwargs
    assert kwargs["creationflags"] == 512
    assert "start_new_session" not in kwargs


def test_posix_spawn_still_starts_a_new_session(launcher, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    popen = MagicMock()
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    launcher.ProcessGroup().spawn("drone-1", ["x"], {})
    kwargs = popen.call_args.kwargs
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs


def test_windows_stops_children_with_terminate_and_kill(launcher, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    killpg = MagicMock()
    monkeypatch.setattr(launcher.os, "killpg", killpg, raising=False)
    proc = MagicMock()
    proc.poll.return_value = None
    group = launcher.ProcessGroup()
    group.procs["drone-1"] = proc

    assert group.kill_one("drone-1") is True
    proc.kill.assert_called_once_with()
    proc.terminate.assert_not_called()

    launcher._signal_group(proc, signal.SIGTERM)
    proc.terminate.assert_called_once_with()
    killpg.assert_not_called()


def test_posix_kill_one_defaults_to_sigkill_on_the_group(launcher, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    killpg = MagicMock()
    monkeypatch.setattr(launcher.os, "killpg", killpg)
    proc = MagicMock(pid=4242)
    proc.poll.return_value = None
    group = launcher.ProcessGroup()
    group.procs["drone-1"] = proc
    assert group.kill_one("drone-1") is True
    killpg.assert_called_once_with(4242, signal.SIGKILL)
