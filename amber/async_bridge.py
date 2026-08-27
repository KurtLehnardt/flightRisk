"""Shared background-thread asyncio event loop bridge.

Runs a dedicated asyncio event loop on a daemon thread so that
synchronous, threading-based code (e.g. the Flask/SocketIO dashboard
app) can drive an async API by submitting coroutines onto it via
``asyncio.run_coroutine_threadsafe`` and blocking for the result.

This is the "sync-over-async" bridge pattern that was previously
duplicated between ``amber.drone.mavlink.MavlinkController`` (MAVSDK)
and ``amber.transport.EdgeTransportSync`` / ``GroundTransportSync``
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


class AsyncBridge:
    """Runs an asyncio event loop on a background thread for sync-over-async patterns."""

    def __init__(self, name: str = "async-bridge", cmd_timeout: float = DEFAULT_CMD_TIMEOUT):
        self._name = name
        self._cmd_timeout = cmd_timeout
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return bool(self.loop and self.loop.is_running())

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
        """Stop the background event loop and join its thread."""
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.loop_thread and self.loop_thread.is_alive():
            self.loop_thread.join(timeout=5.0)
        if self.loop and not self.loop.is_closed():
            self.loop.close()
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
            raise RuntimeError("Event loop is not running")
        effective_timeout = self._cmd_timeout if timeout is None else timeout
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=effective_timeout)
        except TimeoutError:
            future.cancel()
            logger.error("[%s] Command timed out after %.0fs", self._name, effective_timeout)
            raise
