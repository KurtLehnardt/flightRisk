"""Resource counting and error capture for the network and fleet tests.

``snapshot()`` counts open file descriptors, unfinished asyncio tasks (on every
loop, found through the garbage collector) and live threads.
``ResourceSnapshot.leaked_since`` waits for background teardown to settle and
reports what is still open compared with an earlier snapshot.

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
    """Every unfinished asyncio task on any loop in this process."""
    gc.collect()
    return [obj for obj in gc.get_objects() if isinstance(obj, asyncio.Task) and not obj.done()]


@dataclass(frozen=True)
class ResourceSnapshot:
    fds: int
    tasks: int
    threads: frozenset[str]
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
            extra = sorted(now.threads - before.threads)
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
        threads=frozenset(t.name for t in threading.enumerate() if t.is_alive()),
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
