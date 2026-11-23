"""Resource counting and error capture for the network and fleet tests.

``snapshot()`` counts open file descriptors (handles on Windows, when psutil is installed), unfinished asyncio tasks (on every
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
    """Open descriptors (handles on Windows). Zero when this platform cannot count them, so the delta check never fires."""
    if _IS_WINDOWS:
        # Windows has no /dev/fd. psutil counts the process's open handles; without it the check is skipped.
        try:
            import psutil
        except ImportError:
            return 0
        return psutil.Process().num_handles()
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


# Windows counts kernel handles, not descriptors. Handles that are not leaks still appear: the first
# thread pool, DLL loads and event objects. A one-time warm-up (_warm_up, run before the first snapshot)
# takes those first-use costs out, so what is left is small. The ZBook showed deltas of 2 and 3 before
# any warm-up; the tolerance is 2, to absorb a stray event or timer handle, and is meant to be checked
# on Windows again and lowered if the warm-up removes the noise. Sockets are what the tests plant and
# what leaks in practice, so on Windows they are counted separately and exactly (open_sockets), and any
# new socket is a leak. Descriptor counts on macOS and Linux stay exact.
WINDOWS_HANDLE_NOISE = 2
_IS_WINDOWS = sys.platform == "win32"
_warmed_up = False


def fd_delta_is_leak(delta: int) -> bool:
    """Whether *delta* more open descriptors (handles on Windows) than before counts as a leak."""
    return delta > (WINDOWS_HANDLE_NOISE if _IS_WINDOWS else 0)


def open_sockets() -> int:
    """The process's sockets that have an address, counted exactly on Windows; zero elsewhere (fds cover them).

    psutil lists a socket once it is bound or connected, so a socket that was only created is counted by the
    handle count instead. TIME_WAIT entries are left out: the system, not this process, holds them.
    """
    if not _IS_WINDOWS:
        return 0
    try:
        import psutil
    except ImportError:
        return 0
    process = psutil.Process()
    listing = getattr(process, "net_connections", None) or process.connections
    return sum(1 for conn in listing(kind="all") if conn.status != psutil.CONN_TIME_WAIT)


def _warm_up() -> None:
    """Once per process on Windows: pay the first-use handle costs so a "before" snapshot already includes them."""
    global _warmed_up
    if _warmed_up or not _IS_WINDOWS:
        return
    _warmed_up = True
    import concurrent.futures
    import socket

    worker = threading.Thread(target=lambda: None)
    worker.start()
    worker.join()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: None).result()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    probe.close()
    loop = asyncio.new_event_loop()
    loop.close()
    open_fds()
    open_sockets()
    gc.collect()


@dataclass(frozen=True)
class ResourceSnapshot:
    fds: int
    tasks: int
    threads: frozenset[threading.Thread]
    task_names: tuple[str, ...] = ()
    sockets: int = 0

    def leaked_since(self, before: ResourceSnapshot, settle_s: float = 10.0) -> dict[str, object]:
        """What is still open now but was not in *before*; empty when everything was released."""
        deadline = time.monotonic() + settle_s
        while True:
            now = snapshot()
            leaks: dict[str, object] = {}
            if fd_delta_is_leak(now.fds - before.fds):
                leaks["fds"] = now.fds - before.fds
            if now.sockets > before.sockets:
                leaks["sockets"] = now.sockets - before.sockets
            if now.tasks > before.tasks:
                leaks["tasks"] = list(now.task_names)
            extra = sorted(t.name for t in now.threads - before.threads - process_wide_threads())
            if extra:
                leaks["threads"] = extra
            if not leaks or time.monotonic() >= deadline:
                return leaks
            time.sleep(0.1)


def snapshot() -> ResourceSnapshot:
    _warm_up()
    tasks = live_tasks()
    names = tuple(sorted(repr(t.get_coro()) for t in tasks))
    return ResourceSnapshot(
        fds=open_fds(),
        tasks=len(tasks),
        threads=frozenset(t for t in threading.enumerate() if t.is_alive()) - process_wide_threads(),
        task_names=names,
        sockets=open_sockets(),
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
