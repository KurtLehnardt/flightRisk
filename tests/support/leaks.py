"""Resource counting and error capture for the network and fleet tests.

``snapshot()`` counts open file descriptors, unfinished asyncio tasks (on every
loop, read from asyncio's own registry of tasks) and keeps the live thread
objects. Threads are compared as objects, not by name, because the same names
repeat (``edge-transport-loop`` and ``harness-edge-drone-1`` exist in every
run), so a leaked thread is found even when an equally named one was alive
before. ``ResourceSnapshot.leaked_since`` waits for background teardown to
settle and reports what is still open compared with an earlier snapshot.

``ErrorWatch`` records what a healthy run must never produce: an uncaught
exception in any thread, an unraisable exception, and any log record at ERROR
or above that carries a traceback or comes from asyncio or websockets (their
loggers report a read-loop or handler task that died).
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field


def _fd_dir() -> str:
    return "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"


def open_fds() -> int:
    return len(os.listdir(_fd_dir()))


def live_tasks() -> list[asyncio.Task]:
    """Every unfinished asyncio task on any loop in this process.

    Read from asyncio's registry of scheduled tasks. Scanning the garbage
    collector's objects with ``isinstance`` instead would touch every object
    in the process, and lazily loaded modules (torch) warn when that happens.
    """
    gc.collect()
    registry = getattr(asyncio.tasks, "_scheduled_tasks", None)
    if registry is None:  # a Python without the registry: match the exact type, which reads no attributes
        return [obj for obj in gc.get_objects() if type(obj) is asyncio.Task and not obj.done()]
    return [task for task in list(registry) if not task.done()]


def process_wide_threads() -> frozenset[threading.Thread]:
    """Threads the process starts once and keeps for good, which are never a test's leak.

    ``flightrisk.obs_log.setup_logging()`` starts a ``QueueListener`` thread the first time any structured logger
    is created and leaves it running. Which test creates the first logger depends on the order the tests run in,
    so a snapshot taken before that moment would count the thread as a leak. The thread is identified by being the
    one the listener object holds, not by its name, so a leaked thread that happens to carry the same name is
    still reported.
    """
    try:
        from flightrisk import obs_log
    except ImportError:  # pragma: no cover - the package always imports in this repository
        return frozenset()
    thread = getattr(getattr(obs_log, "_listener", None), "_thread", None)
    return frozenset({thread}) if thread is not None else frozenset()


@dataclass(frozen=True)
class ResourceSnapshot:
    fds: int
    tasks: int
    threads: frozenset[threading.Thread]
    task_names: tuple[str, ...] = ()

    def leaked_since(self, before: ResourceSnapshot, settle_s: float = 10.0) -> dict[str, object]:
        """What is still open now but was not in *before*; empty when everything was released."""
        deadline = time.monotonic() + settle_s
        while True:
            now = snapshot()
            leaks: dict[str, object] = {}
            if now.fds > before.fds:
                leaks["fds"] = now.fds - before.fds
            if now.tasks > before.tasks:
                leaks["tasks"] = list(now.task_names)
            extra = sorted(t.name for t in now.threads - before.threads - process_wide_threads())
            if extra:
                leaks["threads"] = extra
            if not leaks or time.monotonic() >= deadline:
                return leaks
            time.sleep(0.1)


def snapshot() -> ResourceSnapshot:
    tasks = live_tasks()
    names = tuple(sorted(repr(t.get_coro()) for t in tasks))
    return ResourceSnapshot(
        fds=open_fds(),
        tasks=len(tasks),
        threads=frozenset(t for t in threading.enumerate() if t.is_alive()) - process_wide_threads(),
        task_names=names,
    )


@dataclass
class ErrorWatch:
    """Collects errors a run must not produce. Use as a context manager."""

    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._handler = _CollectingHandler(self.errors)
        self._old_thread_hook = None
        self._old_unraisable = None

    def __enter__(self):
        logging.getLogger().addHandler(self._handler)
        self._old_thread_hook = threading.excepthook
        self._old_unraisable = sys.unraisablehook
        threading.excepthook = self._thread_hook
        sys.unraisablehook = self._unraisable_hook
        return self

    def __exit__(self, *exc) -> None:
        logging.getLogger().removeHandler(self._handler)
        threading.excepthook = self._old_thread_hook
        sys.unraisablehook = self._old_unraisable

    def _thread_hook(self, args) -> None:
        if args.exc_type is not SystemExit:
            self.errors.append(f"uncaught exception in thread {getattr(args.thread, 'name', '?')}: {args.exc_value!r}")

    def _unraisable_hook(self, args) -> None:
        self.errors.append(f"unraisable exception: {args.exc_value!r} ({args.err_msg})")


class _CollectingHandler(logging.Handler):
    def __init__(self, sink: list[str]):
        super().__init__(level=logging.ERROR)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        reported_by_loop = record.name.split(".")[0] in ("asyncio", "websockets")
        if record.exc_info or reported_by_loop:
            self._sink.append(f"{record.name}: {record.getMessage()}")
