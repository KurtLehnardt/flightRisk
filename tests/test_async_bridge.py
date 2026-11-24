"""Tests for flightrisk.async_bridge.AsyncBridge.

AsyncBridge is the shared background-thread event loop bridge used by both
flightrisk.drone.mavlink.MavlinkController (via its _loop/_loop_thread/_run
compatibility properties) and flightrisk.transport's EdgeTransportSync /
GroundTransportSync. These tests exercise it directly, independent of
either caller.
"""

import asyncio
import threading
import time

import pytest

from flightrisk.async_bridge import AsyncBridge


async def _return(value):
    return value


async def _raise(exc):
    raise exc


class TestStartStopLoop:
    def test_not_running_before_start(self):
        bridge = AsyncBridge(name="t")
        assert bridge.is_running is False
        assert bridge.loop is None
        assert bridge.loop_thread is None

    def test_start_loop_starts_a_running_loop(self):
        bridge = AsyncBridge(name="t")
        try:
            bridge.start_loop()
            assert bridge.is_running is True
            assert bridge.loop is not None
            assert bridge.loop_thread is not None
            assert bridge.loop_thread.is_alive()
        finally:
            bridge.stop_loop()

    def test_start_loop_twice_does_not_replace_running_loop(self):
        bridge = AsyncBridge(name="t")
        try:
            bridge.start_loop()
            first_loop = bridge.loop
            first_thread = bridge.loop_thread

            bridge.start_loop()
            assert bridge.loop is first_loop
            assert bridge.loop_thread is first_thread
        finally:
            bridge.stop_loop()

    def test_stop_loop_clears_state(self):
        bridge = AsyncBridge(name="t")
        bridge.start_loop()
        bridge.stop_loop()
        assert bridge.is_running is False
        assert bridge.loop is None
        assert bridge.loop_thread is None

    def test_stop_loop_without_start_is_a_safe_noop(self):
        bridge = AsyncBridge(name="t")
        bridge.stop_loop()  # must not raise
        assert bridge.is_running is False

    def test_restart_after_stop_works(self):
        bridge = AsyncBridge(name="t")
        bridge.start_loop()
        bridge.stop_loop()
        try:
            bridge.start_loop()
            assert bridge.is_running is True
        finally:
            bridge.stop_loop()


class TestStopLoopRace:
    def test_repeated_stop_loop_is_idempotent(self):
        bridge = AsyncBridge(name="t")
        bridge.start_loop()
        loop = bridge.loop
        for _ in range(3):
            bridge.stop_loop()  # must never raise
        assert loop.is_closed()
        assert bridge.loop is None
        assert bridge.loop_thread is None

    def test_concurrent_stop_loop_closes_loop_exactly_once(self):
        bridge = AsyncBridge(name="t")
        bridge.start_loop()
        loop = bridge.loop
        original_close = loop.close
        close_calls = []

        def slow_close():
            # Widen the window so an unserialized second caller would
            # enter close() while the first is still tearing down.
            close_calls.append(threading.get_ident())
            time.sleep(0.05)
            original_close()

        loop.close = slow_close
        n = 8
        barrier = threading.Barrier(n)
        errors = []

        def worker():
            barrier.wait()
            try:
                bridge.stop_loop()
            except BaseException as exc:  # noqa: BLE001 - the test asserts none escape
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert errors == []
        assert len(close_calls) == 1
        assert loop.is_closed()

    def test_stop_loop_while_loop_is_busy(self):
        bridge = AsyncBridge(name="t")
        bridge.start_loop()
        started = threading.Event()

        async def busy():
            started.set()
            await asyncio.sleep(0.2)

        future = asyncio.run_coroutine_threadsafe(busy(), bridge.loop)
        assert started.wait(timeout=5)
        loop = bridge.loop
        bridge.stop_loop()  # must not raise
        bridge.stop_loop()
        assert loop.is_closed()
        assert bridge.loop is None
        future.cancel()

    def test_stop_loop_never_raises_when_close_fails(self):
        bridge = AsyncBridge(name="t")
        bridge.start_loop()
        loop = bridge.loop
        original_close = loop.close

        def failing_close():
            original_close()
            raise ValueError("Invalid file descriptor: -1")

        loop.close = failing_close
        bridge.stop_loop()  # must not raise
        assert bridge.loop is None
        assert bridge.loop_thread is None


class TestRun:
    def test_run_dispatches_coroutine_and_returns_result(self):
        bridge = AsyncBridge(name="t")
        bridge.start_loop()
        try:
            result = bridge.run(_return(42))
            assert result == 42
        finally:
            bridge.stop_loop()

    def test_run_propagates_coroutine_exceptions(self):
        bridge = AsyncBridge(name="t")
        bridge.start_loop()
        try:
            with pytest.raises(ValueError, match="boom"):
                bridge.run(_raise(ValueError("boom")))
        finally:
            bridge.stop_loop()

    def test_run_raises_without_loop(self):
        bridge = AsyncBridge(name="t")
        with pytest.raises(RuntimeError, match="Event loop"):
            bridge.run(_return(1))

    def test_run_raises_after_stop(self):
        bridge = AsyncBridge(name="t")
        bridge.start_loop()
        bridge.stop_loop()
        with pytest.raises(RuntimeError, match="Event loop"):
            bridge.run(_return(1))

    def test_run_uses_explicit_timeout(self):
        bridge = AsyncBridge(name="t")
        bridge.start_loop()
        try:
            with pytest.raises(TimeoutError):
                bridge.run(asyncio.sleep(999), timeout=0.1)
        finally:
            bridge.stop_loop()

    def test_run_uses_cmd_timeout_default_when_no_timeout_given(self):
        bridge = AsyncBridge(name="t", cmd_timeout=0.1)
        bridge.start_loop()
        try:
            with pytest.raises(TimeoutError):
                bridge.run(asyncio.sleep(999))
        finally:
            bridge.stop_loop()

    def test_run_cancels_future_on_timeout(self):
        bridge = AsyncBridge(name="t")
        bridge.start_loop()
        try:
            with pytest.raises(TimeoutError):
                bridge.run(asyncio.sleep(999), timeout=0.1)
            # The bridge's loop must still be usable afterwards -- a timed
            # out command shouldn't wedge the loop for subsequent calls.
            assert bridge.run(_return("still alive")) == "still alive"
        finally:
            bridge.stop_loop()

    def test_multiple_sequential_runs_share_one_loop(self):
        bridge = AsyncBridge(name="t")
        bridge.start_loop()
        try:
            results = [bridge.run(_return(i)) for i in range(5)]
            assert results == [0, 1, 2, 3, 4]
        finally:
            bridge.stop_loop()


class TestThreadNaming:
    def test_loop_thread_name_includes_bridge_name(self):
        bridge = AsyncBridge(name="my-bridge")
        try:
            bridge.start_loop()
            assert "my-bridge" in bridge.loop_thread.name
        finally:
            bridge.stop_loop()

    def test_loop_thread_is_daemon(self):
        bridge = AsyncBridge(name="t")
        try:
            bridge.start_loop()
            assert bridge.loop_thread.daemon is True
        finally:
            bridge.stop_loop()


def _stop_with_a_pending_task(caplog):
    import gc
    import logging

    caplog.set_level(logging.DEBUG, logger="asyncio")
    bridge = AsyncBridge(name="pending-task")
    bridge.start_loop()
    started = threading.Event()

    async def forever():
        started.set()
        await asyncio.sleep(60)

    future = asyncio.run_coroutine_threadsafe(forever(), bridge.loop)
    assert started.wait(5)
    bridge.stop_loop()
    del future
    gc.collect()
    return [r.getMessage() for r in caplog.records if "Task was destroyed" in r.getMessage()]


@pytest.mark.filterwarnings("error")
def test_stop_loop_cancels_pending_tasks_so_none_is_destroyed_pending(caplog):
    assert _stop_with_a_pending_task(caplog) == []


def test_stop_loop_from_the_loop_thread_returns_at_once_and_the_loop_can_then_be_closed():
    bridge = AsyncBridge(name="self-stop")
    bridge.start_loop()
    loop, thread = bridge.loop, bridge.loop_thread
    elapsed = []

    async def stop_from_inside():
        start = time.monotonic()
        bridge.stop_loop()  # on the loop's own thread: no join, no drain, nothing waits on itself
        elapsed.append(time.monotonic() - start)

    asyncio.run_coroutine_threadsafe(stop_from_inside(), loop).result(timeout=10)
    assert elapsed and elapsed[0] < 0.5
    assert bridge.loop is None
    thread.join(5)
    assert not thread.is_alive()
    loop.close()


def test_a_task_that_swallows_cancellation_does_not_hang_or_break_stop_loop(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="flightrisk.async_bridge")
    bridge = AsyncBridge(name="stubborn")
    bridge.start_loop()
    started = threading.Event()

    async def stubborn():
        started.set()
        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                pass

    future = asyncio.run_coroutine_threadsafe(stubborn(), bridge.loop)
    assert started.wait(5)
    # This task can never finish, so the loop closes with it pending. Silence asyncio's later
    # "Task was destroyed" report (private flag, the one asyncio itself uses) so it cannot land in another test.
    for task in asyncio.all_tasks(bridge.loop):
        task._log_destroy_pending = False
    begin = time.monotonic()
    bridge.stop_loop()  # must not raise
    assert time.monotonic() - begin < 4.0
    assert bridge.loop is None and bridge.loop_thread is None
    assert any("stubborn" in r.getMessage() for r in caplog.records)
    future.cancel()


def test_a_command_cancelled_by_stop_loop_raises_runtime_error():
    bridge = AsyncBridge(name="cancelled-cmd", cmd_timeout=30)
    bridge.start_loop()
    started = threading.Event()
    outcome = []

    async def slow():
        started.set()
        await asyncio.sleep(60)

    def caller():
        try:
            bridge.run(slow())
        except BaseException as exc:  # noqa: BLE001 - the type is what is being checked
            outcome.append(exc)

    thread = threading.Thread(target=caller)
    thread.start()
    assert started.wait(5)
    bridge.stop_loop()
    thread.join(10)
    assert len(outcome) == 1 and isinstance(outcome[0], RuntimeError) and "event loop stopped" in str(outcome[0])


class TestStartLoopWaitsForRunning:
    def _delay_thread_run(self, monkeypatch, delay):
        real_run = threading.Thread.run

        def slow_run(self):
            time.sleep(delay)
            real_run(self)

        monkeypatch.setattr(threading.Thread, "run", slow_run)

    def test_run_straight_after_start_loop_succeeds_when_the_thread_is_slow(self, monkeypatch):
        self._delay_thread_run(monkeypatch, 0.2)
        bridge = AsyncBridge(name="slow-start")
        bridge.start_loop()
        try:
            assert bridge.is_running

            async def _one():
                return 1

            assert bridge.run(_one(), timeout=5) == 1
        finally:
            bridge.stop_loop()

    def test_start_loop_raises_and_leaves_nothing_running_when_the_loop_never_starts(self, monkeypatch):
        import flightrisk.async_bridge as ab

        monkeypatch.setattr(ab, "_START_TIMEOUT", 0.1)
        self._delay_thread_run(monkeypatch, 0.5)
        before = {t for t in threading.enumerate()}
        bridge = AsyncBridge(name="never-start")
        with pytest.raises(RuntimeError, match="did not start"):
            bridge.start_loop()
        assert bridge.loop is None
        assert bridge.loop_thread is None
        assert not bridge.is_running
        time.sleep(1.0)
        leaked = [t for t in threading.enumerate() if t not in before and t.is_alive() and "never-start" in t.name]
        assert leaked == []
