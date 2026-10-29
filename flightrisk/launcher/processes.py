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

Stop: the edges first, then the ground, and a process is terminated only
after its grace period:

* macOS and Linux: each child runs in its own session and gets SIGINT, which
  an edge handles like Ctrl+C: it lands its drone, then exits.
* Windows: each child runs with no console at all (CREATE_NO_WINDOW, in its
  own process group), so closing the launcher's console window cannot end
  it. An edge's stdin is a pipe from the launcher, and Stop types the edge's
  own local land key into it ("l" and Enter, ``start_local_land_key`` in
  ``flightrisk/edge_device.py``), waits for the edge to log that it took the
  key, gives the landing time to finish, and then terminates the edge. The
  ground has no such key and is terminated (after the edges). If the
  launcher exits, the pipe closes, the edge's key reader ends quietly, and
  the edge keeps flying. This is not yet verified on Windows hardware.

Restarting an edge is refused on Windows until that stop is verified on
hardware, and an edge that had to be terminated without landing is not
restarted by the same request, because its drone may still be in the air.
"""

from __future__ import annotations

import contextlib
import json
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
#: Windows: the edge's local land key, the log event that confirms the edge took it, how long to wait for
#: that event, and how long a landing is given to finish before the edge is terminated.
LAND_KEY = b"l\n"
LAND_KEY_ACK = "edge_local_land_key"
LAND_KEY_ACK_S = 5.0
LAND_SETTLE_S = 10.0
#: Run folders whose owner is alive, or that changed this recently, are never pruned.
PRUNE_MIN_AGE_S = 600.0
OWNER_FILE = "launcher-owner.json"
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


def pid_alive(pid: int) -> bool:
    """True when process *pid* exists. Never signals it (on Windows os.kill would terminate it)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - when in doubt, treat it as alive (never prune it)
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, owned by someone else
    return True


def claim_run_dir(path: Path, pid: int | None = None) -> None:
    """Create a run folder and record which launcher owns it, so another launcher never prunes it."""
    path.mkdir(parents=True, exist_ok=True)
    (path / OWNER_FILE).write_text(json.dumps({"pid": os.getpid() if pid is None else pid, "started": time.time()}),
                                   encoding="utf-8")


def _owner_alive(path: Path, alive: Callable[[int], bool]) -> bool:
    try:
        data = json.loads((path / OWNER_FILE).read_text(encoding="utf-8"))
        pid = int(data.get("pid", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return alive(pid)


def _newest_change(path: Path) -> float:
    newest = 0.0
    for item in [path, *path.rglob("*")]:
        try:
            newest = max(newest, item.stat().st_mtime)
        except OSError:
            pass
    return newest


def prune_log_runs(
    root: Path,
    *,
    keep: int = KEEP_RUNS,
    max_bytes: int = MAX_LOG_BYTES,
    current: Path | None = None,
    alive: Callable[[int], bool] = pid_alive,
    now: float | None = None,
    min_age_s: float = PRUNE_MIN_AGE_S,
) -> list[Path]:
    """Remove the oldest run folders beyond *keep* folders or *max_bytes* in total. Returns what was removed.

    Only folders named like a run (``YYYYmmdd-HHMMSS-port``) are touched, never *current*, never one whose
    owning launcher is still running, and never one that changed in the last *min_age_s* seconds.
    """
    root = Path(root)
    now = time.time() if now is None else now
    try:
        runs = sorted(p for p in root.iterdir() if p.is_dir() and _RUN_DIR.match(p.name) and p != current)
    except OSError:
        return []
    sizes = {p: _folder_size(p) for p in runs}
    removable = [p for p in runs if not _owner_alive(p, alive) and now - _newest_change(p) >= min_age_s]
    removed = []
    while removable and (len(runs) >= keep or sum(sizes[p] for p in runs) > max_bytes):
        oldest = removable.pop(0)
        shutil.rmtree(oldest, ignore_errors=True)
        runs.remove(oldest)
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
    land_key_offset: int | None = None
    scrubbed: bool = False
    starts: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    restart_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class ProcessManager:
    """The children of one launcher.

    ``popen`` (``subprocess.Popen`` by default) and ``port_open`` are injectable
    for tests, and so is ``windows`` (which stop channel is used). Only processes created
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
        land_key_ack_s: float = LAND_KEY_ACK_S,
        land_settle_s: float = LAND_SETTLE_S,
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
        self.land_key_ack_s = land_key_ack_s
        self.land_settle_s = land_settle_s
        self._lock = threading.RLock()
        # One stop, restart or start at a time; a plain Lock so a worker thread may release it.
        self._op_lock = threading.Lock()
        self._stop_requests = 0
        self._stop_count_lock = threading.Lock()
        self._order: list[str] = []
        self._procs: dict[str, Managed] = {}
        self._abort = threading.Event()
        self._sequence: threading.Thread | None = None
        self.events: list[str] = []

    # ---- queries -------------------------------------------------------

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
        """True from the moment a stop of everything is asked for until it is done (also while it waits its turn)."""
        with self._stop_count_lock:
            return self._stop_requests > 0

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

    # ---- start -----------------------------------------------------------

    def _popen_kwargs(self) -> dict[str, Any]:
        if self.windows:
            # No console at all: closing the launcher's console window cannot end the child, and there is
            # no window that a click could freeze.
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            return {"creationflags": flags}
        # Own session: a Ctrl+C in the launcher's terminal does not reach the children; the launcher decides.
        return {"start_new_session": True}

    def _stdin_for(self, spec: ProcessSpec) -> Any:
        """Windows edges get a pipe for their local land key; everything else reads nothing."""
        return subprocess.PIPE if self.windows and spec.kind == "edge" else subprocess.DEVNULL

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
                managed.land_key_offset = None
                managed.returncode = None
                managed.scrubbed = False
                managed.message = ""
                managed.popen = self._popen(
                    list(spec.argv), cwd=str(self.cwd), env=dict(spec.env),
                    stdin=self._stdin_for(spec), stdout=log, stderr=subprocess.STDOUT, **self._popen_kwargs(),
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

    def _signal_stop(self, managed: Managed) -> None:
        with managed.lock:
            if managed.popen is None or managed.popen.poll() is not None:
                managed.stop_requested = True
                return
            if managed.stop_requested and managed.status == STOPPING:
                return  # already asked once; asking again would interrupt its landing
            managed.stop_requested = True
            managed.status = STOPPING
            popen = managed.popen
        try:
            if not self.windows:
                popen.send_signal(signal.SIGINT)
            elif managed.spec.kind == "edge" and popen.stdin is not None:
                managed.land_key_offset = self._log_size(managed)
                popen.stdin.write(LAND_KEY)
                popen.stdin.flush()
            else:
                popen.terminate()
        except (OSError, ValueError):
            pass

    def _log_size(self, managed: Managed) -> int:
        try:
            return managed.log_path.stat().st_size
        except OSError:
            return 0

    def _log_has(self, managed: Managed, offset: int, needle: str) -> bool:
        try:
            with open(managed.log_path, "rb") as handle:
                handle.seek(offset)
                return needle.encode("utf-8") in handle.read()
        except OSError:
            return False

    def _wait_exit(self, popen: Any, seconds: float, until: Callable[[], bool] | None = None) -> bool:
        """Wait up to *seconds* for *popen* to exit (True), or for *until* to become true (False)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if popen.poll() is not None:
                return True
            if until is not None and until():
                return False
            time.sleep(self.poll_s)
        return popen.poll() is not None

    def _terminate(self, managed: Managed) -> None:
        popen = managed.popen
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

    def _finish_windows_edge(self, managed: Managed) -> None:
        """Windows: the land key was typed; wait for the edge to take it and land, then terminate it."""
        popen = managed.popen
        name = managed.spec.name
        offset = managed.land_key_offset or 0
        if self._wait_exit(popen, self.land_key_ack_s, until=lambda: self._log_has(managed, offset, LAND_KEY_ACK)):
            return
        if self._log_has(managed, offset, LAND_KEY_ACK):
            self._event(f"{name}: took the land key; giving the landing {self.land_settle_s:.0f} s")
            if self._wait_exit(popen, self.land_settle_s):
                return
        else:
            managed.forced = True
            self._event(f"{name}: did not confirm the land key; terminating it, so its drone may still be in the air")
        self._terminate(managed)
        if popen.stdin is not None:
            with contextlib.suppress(OSError, ValueError):
                popen.stdin.close()

    def _finish_stop(self, managed: Managed, grace_s: float) -> None:
        popen = managed.popen
        if popen is None:
            return
        if self.windows and managed.land_key_offset is not None and popen.poll() is None:
            self._finish_windows_edge(managed)
            self.refresh()
            return
        try:
            popen.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            if managed.spec.kind == "edge":
                managed.forced = True
            self._event(f"{managed.spec.name}: still running after {int(grace_s)} s; terminating it")
            self._terminate(managed)
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
        """Stop every child this launcher started: the edges first, then the ground.

        A second call while one runs waits for it and then finds nothing left to signal.
        """
        with self._stop_count_lock:
            self._stop_requests += 1
        try:
            self._abort.set()
            seq = self._sequence
            if seq is not None and seq is not threading.current_thread():
                seq.join(timeout=self.ready_timeout_s + 5)
            with self._op_lock:
                self._stop_everything()
        finally:
            with self._stop_count_lock:
                self._stop_requests -= 1

    def _stop_everything(self) -> None:
        with self._lock:
            procs = [self._procs[n] for n in self._order]
        edges = [m for m in procs if m.spec.kind != "ground"]
        grounds = [m for m in procs if m.spec.kind == "ground"]
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
        if self.stopping():
            raise ProcessError("a stop is in progress; wait for it to finish")
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
            # Decided from this stop only: an edge that is already down, or lands and exits now, restarts.
            managed.forced = False
            self._stop_group([managed], self._grace(managed))
            if managed.spec.kind == "edge" and managed.forced:
                with managed.lock:
                    managed.message = ("not restarted: it had to be terminated without landing, so its drone may still "
                                       "be in the air (a Tello lands by itself about 15 s after its commands stop). Make "
                                       "sure the drone is down, then press Restart again, or Stop and Start")
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
