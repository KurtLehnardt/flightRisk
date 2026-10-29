"""Start, watch, restart and stop the child processes the launcher started, and only those.

Each child runs with no console window of its own. Its output goes straight
to ``logs/launcher/<name>.log``, so a child keeps running (and logging) if the
launcher exits and leaves it running. The window reads the log back through
``read_log``, which removes the edge token; when a child exits, its log file
is scrubbed of the token as well.

Start order: the ground first; the edges start only once the ground's
dashboard and edge link ports accept connections. On macOS and Linux the
edges stop first, then the ground. A stop asks each child to shut down the way Ctrl+C
does (SIGINT; an edge then lands its own drone if it is flying) and
terminates it only if it has not exited after a grace period. On Windows
the children have no console to receive Ctrl+C and can only be terminated,
so the ground is stopped first and the edges are given time to land their
drones through the link-loss rule before they are terminated.
"""

from __future__ import annotations

import os
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
DEFAULT_LOG_DIR = REPO_ROOT / "logs" / "launcher"
READY_TIMEOUT_S = 180.0
EDGE_STOP_GRACE_S = 15.0
# The dashboard does not always exit on SIGINT, so it gets a short grace before SIGTERM.
GROUND_STOP_GRACE_S = 5.0
KILL_WAIT_S = 5.0
#: Windows: after the ground stops, how long the edges keep running so a flying drone lands through the
#: link-loss rule (FLIGHTRISK_LINK_LOSS_LAND_S, 3 s by default, plus the landing itself).
LINK_LOSS_WAIT_S = 10.0
MAX_LOG_READ = 64 * 1024

# Status values shown in the window.
PENDING = "pending"
STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"
STOPPED = "stopped"
EXITED = "exited"
NOT_STARTED = "not started"


class ProcessError(RuntimeError):
    """A request the process manager refuses; the message says why."""


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _popen_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        # No console window per child: a click in a console window freezes the program (QuickEdit).
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    # Own session: a Ctrl+C in the launcher's terminal does not reach the children; the launcher decides.
    return {"start_new_session": True}


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
    scrubbed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class ProcessManager:
    """The children of one launcher.

    ``popen`` (``subprocess.Popen`` by default), ``port_open`` and ``sleep`` are
    injectable for tests. Only processes created through this object are ever
    signalled.
    """

    def __init__(
        self,
        redactor: Redactor,
        *,
        log_dir: Path = DEFAULT_LOG_DIR,
        cwd: Path = REPO_ROOT,
        popen: Callable[..., Any] = subprocess.Popen,
        port_open: Callable[[str, int], bool] = _tcp_open,
        ready_timeout_s: float = READY_TIMEOUT_S,
        edge_grace_s: float = EDGE_STOP_GRACE_S,
        ground_grace_s: float = GROUND_STOP_GRACE_S,
        poll_s: float = 0.25,
        windows: bool | None = None,
        link_loss_wait_s: float = LINK_LOSS_WAIT_S,
    ):
        self.redact = redactor
        self.log_dir = Path(log_dir)
        self.cwd = Path(cwd)
        self._popen = popen
        self._port_open = port_open
        self.ready_timeout_s = ready_timeout_s
        self.edge_grace_s = edge_grace_s
        self.ground_grace_s = ground_grace_s
        self.poll_s = poll_s
        self.windows = sys.platform == "win32" if windows is None else windows
        self.link_loss_wait_s = link_loss_wait_s
        self._lock = threading.RLock()
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
            return any(m.status in (STARTING, RUNNING, STOPPING) for m in self._procs.values())

    def busy(self) -> bool:
        """True while a start sequence or any child is active."""
        seq = self._sequence
        return (seq is not None and seq.is_alive()) or self.any_alive()

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
                    "pid": getattr(m.popen, "pid", None) if m.status in (STARTING, RUNNING, STOPPING) else None,
                    "message": self.redact(m.message),
                    "log": str(m.log_path),
                    "command": self.redact(m.spec.command_line()),
                    "env": {k: self.redact(v) for k, v in m.spec.display_env.items()},
                    "started_at": m.started_at,
                })
            return rows

    def _event(self, text: str) -> None:
        with self._lock:
            self.events.append(f"{time.strftime('%H:%M:%S')} {self.redact(text)}")
            del self.events[:-200]

    # ---- start -----------------------------------------------------------

    def start(self, plan: Sequence[ProcessSpec], *, wait: bool = False) -> None:
        """Start *plan* in order on a background thread. Refused while anything is running."""
        if not plan:
            raise ProcessError("nothing to start")
        names = [spec.name for spec in plan]
        if len(set(names)) != len(names):
            raise ProcessError(f"two processes would share a name: {', '.join(names)}")
        with self._lock:
            if self.busy():
                raise ProcessError("processes are already running; stop them first")
            self._abort.clear()
            self._order = names
            self._procs = {spec.name: Managed(spec, self.log_dir / f"{spec.name}.log") for spec in plan}
            self._sequence = threading.Thread(target=self._run_sequence, args=(list(plan),), daemon=True, name="launcher-start")
            self._sequence.start()
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
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            if managed.log_path.exists():
                os.replace(managed.log_path, managed.log_path.with_suffix(".previous.log"))
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
                managed.returncode = None
                managed.scrubbed = False
                managed.message = ""
                managed.popen = self._popen(
                    list(spec.argv), cwd=str(self.cwd), env=dict(spec.env),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **_popen_kwargs(),
                )
                managed.started_at = time.time()
                if not spec.ready_ports:
                    managed.status = RUNNING
        except OSError as exc:
            with managed.lock:
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
                if m.popen is None or m.status not in (STARTING, RUNNING, STOPPING):
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

    def read_log(self, name: str, offset: int = 0, limit: int = MAX_LOG_READ) -> tuple[str, int]:
        """New log text of *name* from byte *offset*, whole lines only, with the token removed."""
        with self._lock:
            managed = self._procs.get(name)
        if managed is None:
            raise ProcessError(f"no process named {name!r}")
        try:
            with open(managed.log_path, "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                if offset > size:
                    offset = 0  # the file was replaced by a restart
                if size - offset > limit:
                    offset = size - limit
                handle.seek(offset)
                data = handle.read(limit)
        except OSError:
            return "", offset
        cut = data.rfind(b"\n")
        if cut < 0:
            return "", offset
        chunk = data[: cut + 1]
        return self.redact(chunk.decode("utf-8", errors="replace")), offset + len(chunk)

    # ---- stop and restart --------------------------------------------------

    def _signal_stop(self, managed: Managed) -> None:
        with managed.lock:
            managed.stop_requested = True
            if managed.popen is None or managed.popen.poll() is not None:
                return
            managed.status = STOPPING
            popen = managed.popen
        try:
            if self.windows:
                popen.terminate()
            else:
                popen.send_signal(signal.SIGINT)
        except (OSError, ValueError):
            pass

    def _finish_stop(self, managed: Managed, grace_s: float) -> None:
        popen = managed.popen
        if popen is None:
            return
        try:
            popen.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
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
        """Stop every child this launcher started.

        macOS and Linux: the edges first (each lands its own drone on SIGINT),
        then the ground. Windows, where a child can only be terminated: the
        ground first, then a wait of ``link_loss_wait_s`` so that a flying
        drone lands through its edge's link-loss rule, then the edges.
        """
        self._abort.set()
        seq = self._sequence
        if seq is not None and seq is not threading.current_thread():
            seq.join(timeout=self.ready_timeout_s + 5)
        with self._lock:
            procs = [self._procs[n] for n in self._order]
        edges = [m for m in procs if m.spec.kind != "ground"]
        grounds = [m for m in procs if m.spec.kind == "ground"]
        live_edges = any(m.popen is not None and m.popen.poll() is None for m in edges)
        if self.windows and grounds and live_edges:
            self._stop_group(grounds, self.ground_grace_s)
            self._event(f"waiting up to {int(self.link_loss_wait_s)} s so flying drones land through the link-loss rule")
            deadline = time.monotonic() + self.link_loss_wait_s
            while time.monotonic() < deadline and any(m.popen is not None and m.popen.poll() is None for m in edges):
                time.sleep(self.poll_s)
            self._stop_group(edges, self.edge_grace_s)
        else:
            self._stop_group(edges, self.edge_grace_s)
            self._stop_group(grounds, self.ground_grace_s)
        with self._lock:
            for m in procs:
                if m.status == PENDING:
                    m.status, m.message = NOT_STARTED, "start cancelled"

    def stop_one(self, name: str) -> None:
        with self._lock:
            managed = self._procs.get(name)
        if managed is None:
            raise ProcessError(f"no process named {name!r} was started by this launcher")
        self._stop_group([managed], self.ground_grace_s if managed.spec.kind == "ground" else self.edge_grace_s)

    def restart(self, name: str) -> None:
        """Stop *name* (if it runs) and start it again with the same command and environment."""
        with self._lock:
            managed = self._procs.get(name)
            seq = self._sequence
        if managed is None:
            raise ProcessError(f"no process named {name!r} was started by this launcher")
        if seq is not None and seq.is_alive():
            raise ProcessError("the start sequence is still running; wait for it to finish")
        self.stop_one(name)
        if not self._spawn(managed):
            return
        if managed.spec.ready_ports:
            threading.Thread(target=self._wait_ready, args=(managed,), daemon=True, name=f"launcher-ready-{name}").start()

    def leave_running(self) -> list[dict[str, Any]]:
        """Forget nothing and signal nothing: the rows of the children that keep running after the launcher exits."""
        return [row for row in self.snapshot() if row["status"] in (STARTING, RUNNING, STOPPING)]
