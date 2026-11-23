"""Shared background-thread asyncio event loop bridge.

Runs a dedicated asyncio event loop on a daemon thread so that
synchronous, threading-based code (e.g. the Flask/SocketIO dashboard
app) can drive an async API by submitting coroutines onto it via
``asyncio.run_coroutine_threadsafe`` and blocking for the result.

This is the "sync-over-async" bridge pattern that was previously
duplicated between ``flightrisk.drone.mavlink.MavlinkController`` (MAVSDK)
and ``flightrisk.transport.EdgeTransportSync`` / ``GroundTransportSync``
(WebSocket transports). Both now share this single implementation for
loop lifecycle management (start/stop) and coroutine dispatch with
timeout handling.
"""
from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)

__all__ = ["AsyncBridge"]

DEFAULT_CMD_TIMEOUT = 30.0  # seconds for sync-over-async calls


_DRAIN_TIMEOUT_S = 2.0  # how long stop_loop waits for cancelled tasks to unwind


async def _cancel_pending_tasks() -> None:
    """Cancel every other task on the running loop and wait for each to finish."""
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class AsyncBridge:
    """Runs an asyncio event loop on a background thread for sync-over-async patterns."""

    def __init__(self, name: str = "async-bridge", cmd_timeout: float = DEFAULT_CMD_TIMEOUT):
        self._name = name
        self._cmd_timeout = cmd_timeout
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread: threading.Thread | None = None
        self._stop_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        loop = self.loop  # one read: stop_loop() may clear it from another thread
        return bool(loop and loop.is_running())

    def start_loop(self) -> None:
        """Start the background event loop.

        No-ops if already running -- calling ``start_loop()`` twice without
        an intervening ``stop_loop()`` would otherwise orphan the first
        loop/thread.
        """
        if self.is_running:
            return
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(
            target=self.loop.run_forever,
            daemon=True,
            name=f"{self._name}-loop",
        )
        self.loop_thread.start()

    def stop_loop(self) -> None:
        """Stop the background event loop and join its thread.

        Idempotent, safe under concurrent calls, and never raises. A lock
        serializes callers so the loop is closed exactly once: closing a
        selector event loop from two threads at once makes the second
        ``_close_self_pipe`` hit an already closed socket and raise
        ``ValueError: Invalid file descriptor: -1``. The loop is only closed
        once its thread has really stopped, because closing a running loop
        raises ``RuntimeError``.
        """
        with self._stop_lock:
            loop = self.loop
            thread = self.loop_thread
            try:
                if loop is not None and loop.is_running():
                    if thread is not threading.current_thread():
                        # A task still pending when the loop closes is reported later as
                        # "Task was destroyed but it is pending!" (an ERROR log, and a
                        # RuntimeWarning in the tests), so cancel and await them first.
                        try:
                            asyncio.run_coroutine_threadsafe(_cancel_pending_tasks(), loop).result(timeout=_DRAIN_TIMEOUT_S)
                        except Exception:
                            logger.debug("[%s] Pending tasks did not unwind before the loop was stopped", self._name, exc_info=True)
                    try:
                        loop.call_soon_threadsafe(loop.stop)
                    except RuntimeError:
                        pass  # the loop closed between the check and the call
                if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                    thread.join(timeout=5.0)
                if loop is not None and not loop.is_closed() and not loop.is_running():
                    loop.close()
                elif loop is not None and loop.is_running():
                    logger.warning("[%s] Event loop still running after stop; not closing it", self._name)
            except Exception:
                logger.exception("[%s] Error while stopping the event loop", self._name)
            finally:
                self.loop = None
                self.loop_thread = None

    def run(self, coro, timeout: float | None = None):
        """Submit *coro* to the event loop and block for the result.

        Raises ``RuntimeError`` if the loop isn't running, or ``TimeoutError``
        if *coro* doesn't complete within *timeout* seconds (defaulting to
        this bridge's ``cmd_timeout``); the pending future is cancelled in
        that case.
        """
        # Snapshot self.loop into a local: another thread could set it to
        # None (e.g. via stop_loop()) between the is_running check and
        # run_coroutine_threadsafe if we re-read the attribute.
        loop = self.loop
        if loop is None or not loop.is_running():
            # Close the unstarted coroutine so it is not reported as never awaited.
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            raise RuntimeError("Event loop is not running")
        effective_timeout = self._cmd_timeout if timeout is None else timeout
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            # The loop closed between the check and the schedule.
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            raise
        try:
            return future.result(timeout=effective_timeout)
        except TimeoutError:
            future.cancel()
            logger.error("[%s] Command timed out after %.0fs", self._name, effective_timeout)
            raise
