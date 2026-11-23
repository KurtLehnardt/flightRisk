"""Tests of the resource checks in tests/support/leaks.py: a check that cannot fail proves nothing."""

import socket
import sys
import threading
import warnings

import pytest

from tests.support.leaks import ErrorWatch, snapshot

pytestmark = pytest.mark.filterwarnings("ignore:coroutine .EdgeTransport.send. was never awaited")


class _Parked:
    """A thread that runs until released."""

    def __init__(self, name: str):
        self.release = threading.Event()
        self.thread = threading.Thread(target=self.release.wait, name=name, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.release.set()
        self.thread.join(5)


def test_a_leaked_thread_is_found_even_when_an_equally_named_thread_was_alive_before():
    older = _Parked("edge-transport-loop")  # names repeat across the suite
    try:
        before = snapshot()
        leaked = _Parked("edge-transport-loop")
        try:
            assert snapshot().leaked_since(before, settle_s=0.3) == {"threads": ["edge-transport-loop"]}
        finally:
            leaked.stop()
        assert snapshot().leaked_since(before, settle_s=1.0) == {}
    finally:
        older.stop()


def test_a_thread_that_ends_before_the_check_is_not_reported():
    before = snapshot()
    brief = _Parked("short-lived")
    brief.stop()
    assert snapshot().leaked_since(before, settle_s=1.0) == {}


def test_a_leaked_socket_is_found():
    before = snapshot()
    sock = socket.socket()
    if sys.platform == "win32":
        # psutil reads the TCP table, which holds only listening and connected sockets: a TCP socket that is
        # merely bound (as the ZBook showed) is not listed. Listening puts it in the table.
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
    try:
        found = snapshot().leaked_since(before, settle_s=0.2)
        assert "sockets" in found if sys.platform == "win32" else "fds" in found
    finally:
        sock.close()
    assert snapshot().leaked_since(before, settle_s=1.0) == {}


def test_an_unfinished_task_is_found():
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        before = snapshot()
        task = loop.create_task(asyncio.sleep(60))
        found = snapshot().leaked_since(before, settle_s=0.2)
        assert found.get("tasks"), found
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
    finally:
        loop.close()
    assert snapshot().leaked_since(before, settle_s=1.0) == {}


class _Lazy:
    """An object that warns when anything asks for its ``__class__``, like a lazily imported module."""

    def __getattribute__(self, name):
        if name == "__class__":
            warnings.warn("touched a lazily loaded object", FutureWarning, stacklevel=2)
        return object.__getattribute__(self, name)


def test_taking_a_snapshot_does_not_touch_unrelated_objects():
    bystander = _Lazy()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        snapshot()
    assert bystander is not None


def test_error_watch_records_thread_and_log_errors():
    import logging

    with ErrorWatch() as watch:
        def boom() -> None:
            raise ValueError("boom")

        thread = threading.Thread(target=boom)
        thread.start()
        thread.join()
        logging.getLogger("websockets.server").error("handler failed")
    assert any("boom" in e for e in watch.errors)
    assert any("handler failed" in e for e in watch.errors)


def test_the_process_wide_logging_listener_started_after_a_snapshot_is_not_a_leak():
    """The first structured logger starts a QueueListener thread that lives on; that is not a test's leak.

    Runs in a fresh interpreter so the listener really starts after the snapshot, whatever ran before.
    """
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        from tests.support.leaks import snapshot
        before = snapshot()
        from flightrisk import obs_log
        obs_log.setup_logging()
        assert obs_log._listener is not None and obs_log._listener._thread.is_alive()
        print(snapshot().leaked_since(before, settle_s=0.3))
        """
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "{}"


def test_a_leaked_thread_named_like_the_logging_listener_is_still_found():
    before = snapshot()
    impostor = _Parked("Thread-1 (_monitor)")
    try:
        assert snapshot().leaked_since(before, settle_s=0.3) == {"threads": ["Thread-1 (_monitor)"]}
    finally:
        impostor.stop()


def test_handle_noise_is_ignored_only_on_windows(monkeypatch):
    from tests.support import leaks

    monkeypatch.setattr(leaks, "_IS_WINDOWS", False)
    assert leaks.fd_delta_is_leak(1) and not leaks.fd_delta_is_leak(0)
    monkeypatch.setattr(leaks, "_IS_WINDOWS", True)
    assert not leaks.fd_delta_is_leak(leaks.WINDOWS_HANDLE_NOISE)
    assert leaks.fd_delta_is_leak(leaks.WINDOWS_HANDLE_NOISE + 1)


def test_a_new_socket_is_a_leak_on_windows_whatever_the_handle_tolerance(monkeypatch):
    from tests.support import leaks

    monkeypatch.setattr(leaks, "_IS_WINDOWS", True)
    monkeypatch.setattr(leaks, "open_fds", lambda: 100)
    monkeypatch.setattr(leaks, "_warm_up", lambda: None)
    counts = iter([4, 5])  # before, then the snapshot inside leaked_since
    monkeypatch.setattr(leaks, "open_sockets", lambda: next(counts))
    before = leaks.snapshot()
    assert before.leaked_since(before, settle_s=0) == {"sockets": 1}
