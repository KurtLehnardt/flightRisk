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
import concurrent.futures
import logging
import threading
import time

logger = logging.getLogger(__name__)

__all__ = ["AsyncBridge"]

DEFAULT_CMD_TIMEOUT = 30.0  # seconds for sync-over-async calls


_DRAIN_TIMEOUT_S = 2.0  # how long stop_loop waits for cancelled tasks to unwind


def _drain_and_close(loop: asyncio.AbstractEventLoop, name: str) -> None:
    """Finish a stopped loop the way ``asyncio.run`` does, then close it.

    Runs on the calling thread, after the loop thread has stopped, so nothing is scheduled across
    threads. Every task is cancelled and awaited, repeating while new tasks appear (a task may start
    another while it unwinds), within ``_DRAIN_TIMEOUT_S``. A task still pending when the bound passes
    is logged by name. Async generators are then shut down and the loop is closed.
    """
    deadline = time.monotonic() + _DRAIN_TIMEOUT_S
    try:
        while True:
            tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
            if not tasks:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("[%s] Tasks still pending after %.0fs: %s", name, _DRAIN_TIMEOUT_S, sorted(repr(t.get_coro()) for t in tasks))
                break
            for task in tasks:
                task.cancel()
            loop.run_until_complete(asyncio.wait(tasks, timeout=remaining))
        loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=1.0))
    except Exception:
        logger.debug("[%s] Draining the event loop failed", name, exc_info=True)
    finally:
        loop.close()

# Longest start_loop() waits for the loop thread to begin running.
_START_TIMEOUT = 5.0


def _run_loop(loop: asyncio.AbstractEventLoop, abandoned: threading.Event) -> None:
    """Thread target: run *loop*, and close it on this thread if its start was abandoned.

    ``stop_loop()`` closes a loop that ``start_loop()`` published. A loop that ``start_loop()`` gave
    up on is never published, and its thread may only stop long after ``start_loop()`` returned, so
    the thread closes that loop itself and its self-pipe sockets are released.
    """
    try:
        loop.run_forever()
    finally:
        if abandoned.is_set():
            loop.close()


def _abandon(loop: asyncio.AbstractEventLoop, abandoned: threading.Event) -> None:
    """Make an unpublished loop's thread stop the loop and then close it."""
    abandoned.set()  # set before the stop is queued, so the thread sees it once run_forever() returns
    loop.call_soon_threadsafe(loop.stop)


class AsyncBridge:
    """Runs an asyncio event loop on a background thread for sync-over-async patterns."""

    def __init__(self, name: str = "async-bridge", cmd_timeout: float = DEFAULT_CMD_TIMEOUT):
        self._name = name
        self._cmd_timeout = cmd_timeout
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._stops = 0  # incremented by every stop_loop(), under _stop_lock

    @property
    def is_running(self) -> bool:
        loop = self.loop  # one read: stop_loop() may clear it from another thread
        return bool(loop and loop.is_running())

    def start_loop(self) -> None:
        """Start the background event loop and wait until it is running.

        No-ops if already running -- calling ``start_loop()`` twice without
        an intervening ``stop_loop()`` would otherwise orphan the first
        loop/thread. ``_start_lock`` serializes callers, so two concurrent
        calls start one loop and one thread.

        The wait for the loop holds only ``_start_lock``. The loop's first
        callback takes no lock and ``stop_loop()`` takes only ``_stop_lock``,
        so neither can block on a start. A ``stop_loop()`` that runs while a
        start is waiting is not missed: the start sees it when it would
        publish the loop, and shuts the new loop down instead.

        Raises ``RuntimeError`` if the loop is not running within
        ``_START_TIMEOUT`` seconds, which bounds the whole call.
        """
        with self._start_lock:
            if self.is_running:
                return
            with self._stop_lock:
                stops_before = self._stops
            loop = asyncio.new_event_loop()
            abandoned = threading.Event()
            thread = threading.Thread(
                target=_run_loop,
                args=(loop, abandoned),
                daemon=True,
                name=f"{self._name}-loop",
            )
            # Thread.start() returns before run_forever() has marked the loop as
            # running, and run() refuses a loop that is not running. Queue a
            # callback first, so that once it has fired the loop is certainly
            # running, and wait for it before returning.
            started = threading.Event()
            loop.call_soon(started.set)
            thread.start()
            if not started.wait(timeout=_START_TIMEOUT):
                # Never hand back a loop that is not running. The thread may only
                # run much later, so it stops and closes the loop itself.
                _abandon(loop, abandoned)
                raise RuntimeError(
                    f"[{self._name}] Event loop did not start within {_START_TIMEOUT:g}s"
                )
            with self._stop_lock:
                if self._stops == stops_before:
                    self.loop = loop
                    self.loop_thread = thread
                    return
            logger.debug("[%s] stop_loop() ran during start_loop(); not keeping the new loop", self._name)
            _abandon(loop, abandoned)

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
            self._stops += 1
            loop = self.loop
            thread = self.loop_thread
            try:
                if loop is not None and loop.is_running():
                    try:
                        loop.call_soon_threadsafe(loop.stop)
                    except RuntimeError:
                        pass  # the loop closed between the check and the call
                if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                    thread.join(timeout=5.0)
                if loop is not None and not loop.is_closed() and not loop.is_running():
                    _drain_and_close(loop, self._name)
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
        that case. ``stop_loop()`` cancels a command that is still running, and
        the caller then gets ``RuntimeError("event loop stopped")`` rather than
        a bare ``CancelledError``.
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
        except concurrent.futures.CancelledError:
            raise RuntimeError("event loop stopped") from None
        except TimeoutError:
            future.cancel()
            logger.error("[%s] Command timed out after %.0fs", self._name, effective_timeout)
            raise
