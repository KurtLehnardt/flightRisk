"""Start, watch, restart and stop the child processes the launcher started, and only those.

Each launcher run writes its logs to a folder of its own,
``logs/launcher/<date>-<time>-<port>/<name>.log``, so two launchers (for example
a rehearsal beside a live ground) never share a file. Old run folders are
removed at start beyond ``KEEP_RUNS`` folders or ``MAX_LOG_BYTES`` in total.
A child writes straight to its log file, so it keeps running (and logging) if
the launcher exits and leaves it running. The window reads the log back
through ``read_log``, which removes the edge token; when a child exits, its
log file is scrubbed of the token as well.

Start order: the ground first; the edges start only once the ground's
dashboard and edge link ports accept connections.

Stop: the edges first, then the ground. Each child is asked to shut down the
way Ctrl+C does, so an edge lands its own drone if it is flying, and it is
terminated only if it has not exited after a grace period:

* macOS and Linux: SIGINT. Each child runs in its own session.
* Windows: CTRL_BREAK_EVENT, which the edge handles like Ctrl+C. Each child
  runs in its own process group sharing the launcher's console, with no
  window of its own. Without a console (the launcher started without one)
  this is impossible, the children run with no console at all and can only
  be terminated; then, when a ground runs here, it is stopped first and the
  edges are given the link-loss time to land their drones, and when only
  edges run here, Stop does not land any drone and the window says so.

Restarting an edge is refused on Windows until the Ctrl+Break stop is
verified on hardware, and an edge that had to be terminated is never
restarted, because its drone may still be in the air.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flightrisk.launcher.commands import ProcessSpec, Redactor

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_ROOT = REPO_ROOT / "logs" / "launcher"
#: At most this many run folders are kept in LOG_ROOT, and at most this many bytes in total.
KEEP_RUNS = 20
MAX_LOG_BYTES = 500 * 1024 * 1024
_RUN_DIR = re.compile(r"^\d{8}-\d{6}-\d+$")
READY_TIMEOUT_S = 180.0
EDGE_STOP_GRACE_S = 15.0
# The dashboard does not always exit on SIGINT, so it gets a short grace before SIGTERM.
GROUND_STOP_GRACE_S = 5.0
KILL_WAIT_S = 5.0
#: The edge's default FLIGHTRISK_LINK_LOSS_LAND_S, and the time a landing takes after it.
DEFAULT_LINK_LOSS_S = 3.0
LANDING_ALLOWANCE_S = 7.0
MAX_LOG_READ = 64 * 1024

# Status values shown in the window.
PENDING = "pending"
STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"
STOPPED = "stopped"
EXITED = "exited"
NOT_STARTED = "not started"
ALIVE = (STARTING, RUNNING, STOPPING)

WINDOWS_EDGE_RESTART_REFUSED = (
    "restarting an edge is not available on Windows yet: stopping it may not land its drone. "
    "Land it with LAND ALL in the dashboard, then Stop and Start again"
)


class ProcessError(RuntimeError):
    """A request the process manager refuses; the message says why."""


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def console_attached() -> bool:
    """Windows: True when this process has a console its children can share (for Ctrl+Break)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # noqa: BLE001 - no console API means no console
        return False


def new_run_log_dir(root: Path, port: int, now: float | None = None) -> Path:
    """The log folder of one launcher run: ``<root>/<YYYYmmdd-HHMMSS>-<port>`` (not created yet)."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return Path(root) / f"{stamp}-{port}"


def _folder_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def prune_log_runs(root: Path, *, keep: int = KEEP_RUNS, max_bytes: int = MAX_LOG_BYTES, current: Path | None = None) -> list[Path]:
    """Remove the oldest run folders beyond *keep* folders or *max_bytes* in total. Returns what was removed.

    Only folders named like a run (``YYYYmmdd-HHMMSS-port``) are touched, never *current*.
    """
    root = Path(root)
    try:
        runs = sorted(p for p in root.iterdir() if p.is_dir() and _RUN_DIR.match(p.name) and p != current)
    except OSError:
        return []
    sizes = {p: _folder_size(p) for p in runs}
    removed = []
    while runs and (len(runs) >= keep or sum(sizes[p] for p in runs) > max_bytes):
        oldest = runs.pop(0)
        shutil.rmtree(oldest, ignore_errors=True)
        removed.append(oldest)
    return removed


@dataclass
class Managed:
    """One child the launcher started (or is about to start)."""

    spec: ProcessSpec
    log_path: Path
    status: str = PENDING
    popen: Any = None
    returncode: int | None = None
    started_at: float | None = None
    message: str = ""
    stop_requested: bool = False
    forced: bool = False
    scrubbed: bool = False
    starts: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    restart_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class ProcessManager:
    """The children of one launcher.

    ``popen`` (``subprocess.Popen`` by default) and ``port_open`` are injectable
    for tests, and so are ``windows`` and ``graceful`` (whether a stop can ask a
    child to shut down rather than terminate it). Only processes created
    through this object are ever signalled.
    """

    def __init__(
        self,
        redactor: Redactor,
        *,
        log_dir: Path | None = None,
        cwd: Path = REPO_ROOT,
        popen: Callable[..., Any] = subprocess.Popen,
        port_open: Callable[[str, int], bool] = _tcp_open,
        ready_timeout_s: float = READY_TIMEOUT_S,
        edge_grace_s: float = EDGE_STOP_GRACE_S,
        ground_grace_s: float = GROUND_STOP_GRACE_S,
        poll_s: float = 0.25,
        windows: bool | None = None,
        graceful: bool | None = None,
        link_loss_wait_s: float | None = None,
    ):
        self.redact = redactor
        self.log_dir = Path(log_dir) if log_dir is not None else new_run_log_dir(LOG_ROOT, os.getpid())
        self.cwd = Path(cwd)
        self._popen = popen
        self._port_open = port_open
        self.ready_timeout_s = ready_timeout_s
        self.edge_grace_s = edge_grace_s
        self.ground_grace_s = ground_grace_s
        self.poll_s = poll_s
        self.windows = sys.platform == "win32" if windows is None else windows
        if graceful is None:
            graceful = console_attached() if self.windows else True
        self.graceful = graceful
        self._link_loss_wait_s = link_loss_wait_s
        self._lock = threading.RLock()
        # One stop, restart or start at a time; a plain Lock so a worker thread may release it.
        self._op_lock = threading.Lock()
        self._stopping = threading.Event()
        self._order: list[str] = []
        self._procs: dict[str, Managed] = {}
        self._abort = threading.Event()
        self._sequence: threading.Thread | None = None
        self.events: list[str] = []

    # ---- queries -------------------------------------------------------

    @property
    def stop_lands_drones(self) -> bool:
        """True when Stop asks each edge to land its drone (False only on Windows without a console)."""
        return self.graceful

    def ground_here(self) -> bool:
        """True when this launcher started a ground station."""
        with self._lock:
            return any(m.spec.kind == "ground" for m in self._procs.values())

    def names(self) -> list[str]:
        with self._lock:
            return list(self._order)

    def any_alive(self) -> bool:
        self.refresh()
        with self._lock:
            return any(m.status in ALIVE for m in self._procs.values())

    def busy(self) -> bool:
        """True while a start sequence or any child is active."""
        seq = self._sequence
        return (seq is not None and seq.is_alive()) or self.any_alive()

    def stopping(self) -> bool:
        """True while a stop of everything is in progress."""
        return self._stopping.is_set()

    def can_restart(self, managed: Managed) -> bool:
        return not (self.windows and managed.spec.kind == "edge")

    def snapshot(self) -> list[dict[str, Any]]:
        self.refresh()
        with self._lock:
            rows = []
            for name in self._order:
                m = self._procs[name]
                rows.append({
                    "name": name,
                    "kind": m.spec.kind,
                    "drone_id": m.spec.drone_id,
                    "status": m.status,
                    "returncode": m.returncode,
                    "pid": getattr(m.popen, "pid", None) if m.status in ALIVE else None,
                    "message": self.redact(m.message),
                    "log": str(m.log_path),
                    "command": self.redact(m.spec.command_line()),
                    "env": {k: self.redact(v) for k, v in m.spec.display_env.items()},
                    "started_at": m.started_at,
                    "restartable": self.can_restart(m),
                })
            return rows

    def _event(self, text: str) -> None:
        with self._lock:
            self.events.append(f"{time.strftime('%H:%M:%S')} {self.redact(text)}")
            del self.events[:-200]

    def link_loss_wait_s(self) -> float:
        """How long edges keep running after the ground stops, so a flying drone lands through the link-loss rule.

        The edges' own FLIGHTRISK_LINK_LOSS_LAND_S (inherited or default) plus the landing itself.
        """
        if self._link_loss_wait_s is not None:
            return self._link_loss_wait_s
        longest = DEFAULT_LINK_LOSS_S
        with self._lock:
            specs = [m.spec for m in self._procs.values() if m.spec.kind == "edge"]
        for spec in specs:
            try:
                value = float(spec.env.get("FLIGHTRISK_LINK_LOSS_LAND_S") or DEFAULT_LINK_LOSS_S)
            except ValueError:
                value = DEFAULT_LINK_LOSS_S
            if not math.isnan(value) and value > longest:
                longest = min(value, 120.0)
        return longest + LANDING_ALLOWANCE_S

    # ---- start -----------------------------------------------------------

    def _popen_kwargs(self) -> dict[str, Any]:
        if self.windows:
            group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
            if self.graceful:
                # Shares the launcher's console (QuickEdit is off there), so it can receive Ctrl+Break,
                # and opens no window of its own.
                return {"creationflags": group}
            # No console at all: no window that a click could freeze.
            return {"creationflags": group | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
        # Own session: a Ctrl+C in the launcher's terminal does not reach the children; the launcher decides.
        return {"start_new_session": True}

    def start(self, plan: Sequence[ProcessSpec], *, wait: bool = False) -> None:
        """Start *plan* in order on a background thread. Refused while anything is running."""
        if not plan:
            raise ProcessError("nothing to start")
        names = [spec.name for spec in plan]
        if len(set(names)) != len(names):
            raise ProcessError(f"two processes would share a name: {', '.join(names)}")
        if not self._op_lock.acquire(blocking=False):
            raise ProcessError("a stop or restart is in progress; wait for it to finish")
        try:
            with self._lock:
                if self.busy():
                    raise ProcessError("processes are already running; stop them first")
                self._abort.clear()
                self._order = names
                self._procs = {spec.name: Managed(spec, self.log_dir / f"{spec.name}.log") for spec in plan}
                self._sequence = threading.Thread(target=self._run_sequence, args=(list(plan),), daemon=True, name="launcher-start")
                self._sequence.start()
        finally:
            self._op_lock.release()
        if wait:
            self._sequence.join()

    def _run_sequence(self, plan: list[ProcessSpec]) -> None:
        for i, spec in enumerate(plan):
            if self._abort.is_set():
                self._mark_rest(plan[i:], "start cancelled")
                return
            managed = self._procs[spec.name]
            if not self._spawn(managed):
                self._mark_rest(plan[i + 1:], f"not started because {spec.name} did not start")
                return
            if spec.ready_ports and not self._wait_ready(managed):
                self._mark_rest(plan[i + 1:], f"not started because {spec.name} is not ready")
                return

    def _mark_rest(self, specs: Sequence[ProcessSpec], why: str) -> None:
        with self._lock:
            for spec in specs:
                m = self._procs[spec.name]
                if m.status == PENDING:
                    m.status = NOT_STARTED
                    m.message = why

    def _spawn(self, managed: Managed) -> bool:
        spec = managed.spec
        # A new file for every start, so nothing is renamed under a running process (Windows refuses that).
        managed.starts += 1
        suffix = "" if managed.starts == 1 else f".restart-{managed.starts - 1}"
        managed.log_path = self.log_dir / f"{spec.name}{suffix}.log"
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            # Not a with block: the child gets its own copy of the handle, and this one is closed below.
            log = open(managed.log_path, "wb")  # noqa: SIM115
        except OSError as exc:
            with managed.lock:
                managed.status, managed.message = EXITED, f"cannot open the log file: {exc}"
            self._event(f"{spec.name}: cannot open its log file")
            return False
        try:
            header = (
                f"# {spec.name} started by the FlightRisk launcher at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"# command: {spec.command_line()}\n"
            )
            log.write(self.redact(header).encode("utf-8"))
            log.flush()
            with managed.lock:
                managed.status = STARTING
                managed.stop_requested = False
                managed.forced = False
                managed.returncode = None
                managed.scrubbed = False
                managed.message = ""
                managed.popen = self._popen(
                    list(spec.argv), cwd=str(self.cwd), env=dict(spec.env),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **self._popen_kwargs(),
                )
                managed.started_at = time.time()
                if not spec.ready_ports:
                    managed.status = RUNNING
        except (OSError, ValueError) as exc:
            # ValueError: for example a NUL in an argument or environment value.
            with managed.lock:
                managed.popen = None
                managed.status, managed.message = EXITED, self.redact(f"could not start: {exc}")
            self._event(f"{spec.name}: could not start")
            return False
        finally:
            log.close()
        self._event(f"{spec.name}: started (pid {managed.popen.pid})")
        return True

    def _wait_ready(self, managed: Managed) -> bool:
        deadline = time.monotonic() + self.ready_timeout_s
        ports = managed.spec.ready_ports
        while time.monotonic() < deadline:
            if self._abort.is_set() or managed.stop_requested:
                return False
            if managed.popen.poll() is not None:
                self.refresh()
                return False
            if all(self._port_open(host, port) for host, port in ports):
                with managed.lock:
                    if managed.status == STARTING:
                        managed.status = RUNNING
                self._event(f"{managed.spec.name}: listening on " + ", ".join(f"{h}:{p}" for h, p in ports))
                return True
            time.sleep(self.poll_s)
        with managed.lock:
            managed.message = f"not listening after {int(self.ready_timeout_s)} s; see its log"
        self._event(f"{managed.spec.name}: not ready in time")
        return False

    # ---- watch -----------------------------------------------------------

    def refresh(self) -> None:
        """Record the exit code of every child that has exited."""
        with self._lock:
            procs = list(self._procs.values())
        for m in procs:
            with m.lock:
                if m.popen is None or m.status not in ALIVE:
                    continue
                code = m.popen.poll()
                if code is None:
                    continue
                m.returncode = code
                m.status = STOPPED if m.stop_requested else EXITED
            self._event(f"{m.spec.name}: {'stopped' if m.stop_requested else 'exited'} with code {code}")
            self._scrub(m)

    def _scrub(self, managed: Managed) -> None:
        """Remove the token from a finished child's log file (defence in depth: the children never log it)."""
        if managed.scrubbed:
            return
        managed.scrubbed = True
        try:
            data = managed.log_path.read_bytes()
        except OSError:
            return
        clean = self.redact.bytes(data)
        if clean != data:
            try:
                managed.log_path.write_bytes(clean)
            except OSError:
                pass

    def read_log(self, name: str, offset: int = 0, limit: int = MAX_LOG_READ) -> tuple[str, int, str]:
        """New log text of *name* from byte *offset*: whole lines only, with the token removed.

        Returns ``(text, new_offset, log_path)``; a new path means a restart began a new file.
        When more than *limit* bytes are new, reading starts at the first whole line of the last
        *limit* bytes, so a line (and a token in it) is never cut in half.
        """
        with self._lock:
            managed = self._procs.get(name)
        if managed is None:
            raise ProcessError(f"no process named {name!r}")
        path = managed.log_path
        try:
            with open(path, "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                if offset > size:
                    offset = 0
                skip_partial = False
                if size - offset > limit:
                    offset = size - limit
                    skip_partial = offset > 0
                handle.seek(offset)
                data = handle.read(limit)
        except OSError:
            return "", offset, str(path)
        if skip_partial:
            start = data.find(b"\n")
            if start < 0:
                return "", offset + len(data), str(path)
            offset += start + 1
            data = data[start + 1:]
        cut = data.rfind(b"\n")
        if cut < 0:
            return "", offset, str(path)
        chunk = data[: cut + 1]
        return self.redact(chunk.decode("utf-8", errors="replace")), offset + len(chunk), str(path)

    # ---- stop and restart --------------------------------------------------

    def _graceful_signal(self) -> int | None:
        if not self.windows:
            return signal.SIGINT
        if self.graceful:
            return getattr(signal, "CTRL_BREAK_EVENT", 1)
        return None

    def _signal_stop(self, managed: Managed) -> None:
        with managed.lock:
            if managed.popen is None or managed.popen.poll() is not None:
                managed.stop_requested = True
                return
            if managed.stop_requested and managed.status == STOPPING:
                return  # already asked once; a second Ctrl+C would interrupt its landing
            managed.stop_requested = True
            managed.status = STOPPING
            popen = managed.popen
        sig = self._graceful_signal()
        try:
            if sig is None:
                popen.terminate()
            else:
                popen.send_signal(sig)
        except (OSError, ValueError):
            pass

    def _finish_stop(self, managed: Managed, grace_s: float) -> None:
        popen = managed.popen
        if popen is None:
            return
        try:
            popen.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            managed.forced = True
            self._event(f"{managed.spec.name}: still running after {int(grace_s)} s; terminating it")
            try:
                popen.terminate()
                popen.wait(timeout=KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                popen.kill()
                try:
                    popen.wait(timeout=KILL_WAIT_S)
                except subprocess.TimeoutExpired:
                    pass
            except OSError:
                pass
        else:
            if self._graceful_signal() is None:
                managed.forced = True  # terminated outright
        self.refresh()

    def _stop_group(self, group: list[Managed], grace_s: float) -> None:
        for m in group:
            self._signal_stop(m)
        threads = [threading.Thread(target=self._finish_stop, args=(m, grace_s), daemon=True) for m in group]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def stop_all(self) -> None:
        """Stop every child this launcher started (see the module docstring for the order).

        A second call while one runs waits for it and then finds nothing left to signal.
        """
        self._abort.set()
        seq = self._sequence
        if seq is not None and seq is not threading.current_thread():
            seq.join(timeout=self.ready_timeout_s + 5)
        with self._op_lock:
            self._stopping.set()
            try:
                self._stop_everything()
            finally:
                self._stopping.clear()

    def _stop_everything(self) -> None:
        with self._lock:
            procs = [self._procs[n] for n in self._order]
        edges = [m for m in procs if m.spec.kind != "ground"]
        grounds = [m for m in procs if m.spec.kind == "ground"]
        live_edges = any(m.popen is not None and m.popen.poll() is None for m in edges)
        if not self.graceful and grounds and live_edges:
            # Children can only be terminated: stop the ground first so each edge's link-loss rule lands its drone.
            wait_s = self.link_loss_wait_s()
            self._stop_group(grounds, self.ground_grace_s)
            self._event(f"waiting up to {wait_s:.0f} s so flying drones land through the link-loss rule")
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline and any(m.popen is not None and m.popen.poll() is None for m in edges):
                time.sleep(self.poll_s)
            self._stop_group(edges, self.edge_grace_s)
        else:
            if not self.graceful and live_edges:
                self._event("terminating the edges: this does not land their drones")
            self._stop_group(edges, self.edge_grace_s)
            self._stop_group(grounds, self.ground_grace_s)
        with self._lock:
            for m in procs:
                if m.status == PENDING:
                    m.status, m.message = NOT_STARTED, "start cancelled"

    def _managed(self, name: str) -> Managed:
        with self._lock:
            managed = self._procs.get(name)
        if managed is None:
            raise ProcessError(f"no process named {name!r} was started by this launcher")
        return managed

    def stop_one(self, name: str) -> None:
        managed = self._managed(name)
        with self._op_lock:
            self._stop_group([managed], self._grace(managed))

    def _grace(self, managed: Managed) -> float:
        return self.ground_grace_s if managed.spec.kind == "ground" else self.edge_grace_s

    def restart(self, name: str, *, background: bool = False) -> None:
        """Stop *name* (if it runs) and start it again with the same command and environment.

        The checks run at once and raise ``ProcessError``: an unknown name, a start sequence or a
        stop still running, a restart of the same process already running, or an edge on Windows.
        With *background* the stop and start then run on a thread.
        """
        managed = self._managed(name)
        seq = self._sequence
        if seq is not None and seq.is_alive():
            raise ProcessError("the start sequence is still running; wait for it to finish")
        if not self.can_restart(managed):
            raise ProcessError(WINDOWS_EDGE_RESTART_REFUSED)
        if not managed.restart_lock.acquire(blocking=False):
            raise ProcessError(f"{name} is already restarting")
        if not self._op_lock.acquire(blocking=False):
            managed.restart_lock.release()
            raise ProcessError("a stop or another restart is in progress; wait for it to finish")
        if background:
            threading.Thread(target=self._restart_locked, args=(managed,), daemon=True, name=f"launcher-restart-{name}").start()
        else:
            self._restart_locked(managed)

    def _restart_locked(self, managed: Managed) -> None:
        try:
            self._stop_group([managed], self._grace(managed))
            if managed.spec.kind == "edge" and managed.forced:
                with managed.lock:
                    managed.message = ("not restarted: it did not shut down by itself, so its drone may still be in the "
                                       "air. Land it from the dashboard, then press Restart again")
                self._event(f"{managed.spec.name}: not restarted (it had to be terminated)")
                return
            if self._spawn(managed) and managed.spec.ready_ports:
                threading.Thread(target=self._wait_ready, args=(managed,), daemon=True,
                                 name=f"launcher-ready-{managed.spec.name}").start()
        finally:
            self._op_lock.release()
            managed.restart_lock.release()

    def leave_running(self) -> list[dict[str, Any]]:
        """Signal nothing: the rows of the children that keep running after the launcher exits."""
        return [row for row in self.snapshot() if row["status"] in ALIVE]
