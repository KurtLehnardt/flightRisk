"""Tests for amber.async_bridge.AsyncBridge.

AsyncBridge is the shared background-thread event loop bridge used by both
amber.drone.mavlink.MavlinkController (via its _loop/_loop_thread/_run
compatibility properties) and amber.transport's EdgeTransportSync /
GroundTransportSync. These tests exercise it directly, independent of
either caller.
"""

import asyncio

import pytest

from amber.async_bridge import AsyncBridge


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
