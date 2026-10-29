"""The launcher window: a small web page served by the standard library on 127.0.0.1.

The page and its API listen on loopback only. Every API call must carry the
per-run session key that is embedded in the page, as the ``X-Launcher-Session``
header, and the ``Host`` (and any ``Origin``) must name this server, so another
web page in the same browser cannot start or stop anything (a custom header
needs a CORS preflight, which this server never grants) and DNS rebinding is
refused. The page never receives the edge token, only whether it is set.

``LauncherApp`` holds the state and does the work; ``Handler`` only translates
HTTP to calls on it, so the app is testable without a socket.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from flightrisk.launcher import preflight as pf
from flightrisk.launcher.adaptermap import confirmed_mapping, propose_adapters
from flightrisk.launcher.ceilingfile import (
    CeilingWriteError,
    plan_ceiling_write,
    write_ceiling,
)
from flightrisk.launcher.commands import (
    CONTROLLED_ENV,
    EdgeToken,
    Redactor,
    build_plan,
    load_token,
)
from flightrisk.launcher.netinfo import Adapter
from flightrisk.launcher.processes import (
    LOG_ROOT,
    ProcessError,
    ProcessManager,
    claim_run_dir,
    new_run_log_dir,
    prune_log_runs,
)
from flightrisk.launcher.settings import (
    DEFAULT_PRESET,
    ERROR,
    MAX_DRONES,
    Issue,
    LauncherSettings,
    PresetError,
    PresetStore,
    SettingsError,
    builtin_presets,
    errors,
    validate,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_HEADER = "X-Launcher-Session"
MAX_BODY = 256 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def stop_warning(windows: bool) -> str:
    """What stopping does to drones in the air, for every stop dialog and prompt."""
    remote = "an edge on another computer lands its drone through the link-loss rule when the ground stops."
    if windows:
        return ("Each edge on this computer is asked to land with its own land key, given time to land, and then "
                f"stopped; {remote} This Windows stop is not yet verified on hardware: land every drone with LAND ALL "
                "in the dashboard first.")
    return ("Drones in the air will land: each edge on this computer lands its drone as it shuts down, and "
            f"{remote} Land them first with LAND ALL in the dashboard.")


class LauncherApp:
    """Launcher state and actions, independent of HTTP."""

    def __init__(
        self,
        *,
        port: int,
        store: PresetStore | None = None,
        token_loader: Callable[[], EdgeToken] = load_token,
        manager: ProcessManager | None = None,
        preflight_env: pf.PreflightEnv | None = None,
        python: str = sys.executable,
        on_quit: Callable[[str], None] | None = None,
        environ: dict[str, str] | None = None,
        ceiling_path: Path | None = None,
    ):
        self.port = port
        #: The dashboard's ceiling file; None means FLIGHTRISK_CEILING_FILE in the environment, else the default.
        self.ceiling_path = ceiling_path
        self.environ = dict(os.environ if environ is None else environ)
        self._token_loader = token_loader
        self.redact = Redactor()
        self.token = self.reload_token()
        self.store = store or PresetStore(secret=lambda: self.token.reveal())
        if manager is None:
            manager = ProcessManager(self.redact, log_dir=new_run_log_dir(LOG_ROOT, port))
        self.manager = manager
        self.preflight_env = preflight_env or pf.PreflightEnv()
        self.python = python
        self.on_quit = on_quit
        self.last_checks: list[dict[str, Any]] = []
        self._adapters_cache: list[dict[str, Any]] | None = None
        self._start_lock = threading.Lock()
        self._stop_threads: list[threading.Thread] = []

    # ---- helpers ---------------------------------------------------------

    def prepare_logs(self) -> list[Path]:
        """Claim this run's log folder and prune old ones. Call only once the window's port is bound,
        so a second launcher that fails with "already running" touches no logs."""
        claim_run_dir(self.manager.log_dir)
        return prune_log_runs(self.manager.log_dir.parent, current=self.manager.log_dir)

    def reload_token(self) -> EdgeToken:
        self.token = self._token_loader()
        self.redact.add(self.token)
        return self.token

    def reserved_ports(self) -> dict[int, str]:
        return {self.port: "the launcher window"}

    def parse(self, body: dict[str, Any]) -> LauncherSettings:
        try:
            return LauncherSettings.from_json(body.get("settings"))
        except SettingsError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from None

    def _validate(self, settings: LauncherSettings, *, fresh_adapters: bool = False) -> list:
        adapters = [Adapter(a["name"], a["ipv4"], a["index"], a["up"]) for a in self.adapters(refresh=fresh_adapters)]
        return validate(
            settings, is_windows=self.preflight_env.is_windows, reserved_ports=self.reserved_ports(),
            adapters=adapters, secret=self.token.reveal(), environ=self.environ,
        )

    def issues(self, settings: LauncherSettings, *, fresh_adapters: bool = False) -> list[dict[str, str]]:
        return [i.to_json() for i in self._validate(settings, fresh_adapters=fresh_adapters)]

    def inherited_env(self) -> list[str]:
        """Names (never values) of FLIGHTRISK_* and OTEL_* variables the children inherit unchanged."""
        return sorted(
            k for k in self.environ
            if k.startswith(("FLIGHTRISK_", "OTEL_")) and k not in CONTROLLED_ENV
        )

    def profiles(self) -> list[dict[str, Any]]:
        rows = []
        for row in self.preflight_env.weights():
            available = bool(row["found"]) or not row["required"]
            rows.append({"name": row["name"], "label": row["label"], "available": available, "weights": row["weights"]})
        return rows

    def ceiling_plan(self, settings: LauncherSettings):
        return plan_ceiling_write(settings, self.ceiling_path, self.environ)

    def adapter_proposals(self, body: dict[str, Any]) -> dict[str, Any]:
        """Per real drone flown from here: the adapters in its Tello host's network and, when safe, a pre-selection."""
        settings = self.parse(body)
        adapters = [Adapter(a["name"], a["ipv4"], a["index"], a["up"]) for a in self.adapters(refresh=True)]
        proposals = propose_adapters(settings, adapters, self.store.adapter_choices())
        return {"proposals": [p.to_json() for p in proposals], "adapters": self.adapters()}

    def adapters(self, refresh: bool = False) -> list[dict[str, Any]]:
        if refresh or self._adapters_cache is None:
            self._adapters_cache = [a.to_json() for a in self.preflight_env.adapters()]
        return self._adapters_cache

    # ---- API -------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        token = self.reload_token()
        return {
            "token": {"status": token.status, "source": token.source},
            "processes": self.manager.snapshot(),
            "busy": self.manager.busy(),
            "events": [self.redact(e) for e in self.manager.events[-50:]],
            "stopping": self.manager.stopping(),
            "stop_warning": stop_warning(self.manager.windows),
            "is_windows": self.preflight_env.is_windows,
        }

    def room_ceiling(self) -> tuple[int, int] | None:
        """The dashboard's saved ceiling and margin, or None when the file is missing values or invalid."""
        try:
            ceiling, margin, error, _path = self.preflight_env.ceiling_file()
        except Exception:  # noqa: BLE001 - the presets keep their defaults
            return None
        return None if error else (int(ceiling), int(margin))

    def bootstrap(self) -> dict[str, Any]:
        presets = self.store.presets()
        room = self.room_ceiling()
        if room is not None:
            # Built-in presets start from the room's ceiling as saved in the dashboard, not the code defaults.
            builtin = builtin_presets()
            presets = {
                name: replace(s, ceiling_cm=room[0], margin_cm=room[1]) if name in builtin else s
                for name, s in presets.items()
            }
        last = self.store.last()
        settings = last or presets[DEFAULT_PRESET]
        return {
            **self.state(),
            "settings": settings.to_json(),
            "presets": [{"name": name, "builtin": name in builtin_presets(), "settings": s.to_json()} for name, s in presets.items()],
            "store_problems": list(self.store.load_problems),
            "profiles": self.profiles(),
            "adapters": self.adapters(),
            "platform": {"is_windows": self.preflight_env.is_windows, "name": sys.platform, "host": socket.gethostname()},
            "max_drones": MAX_DRONES,
            "launcher_port": self.port,
        }

    def validate(self, body: dict[str, Any]) -> dict[str, Any]:
        settings = self.parse(body)
        token = self.reload_token()
        plan = build_plan(settings, token, self.python, self.environ)
        return {
            "issues": self.issues(settings),
            "inherited_env": self.inherited_env(),
            "ceiling_file": self.ceiling_plan(settings).to_json(),
            "fleet": list(settings.fleet_ids()) if settings.runs_ground else [],
            "needs_token": settings.needs_token(),
            "plan": [
                {"name": p.name, "command": self.redact(p.command_line()), "env": {k: self.redact(v) for k, v in p.display_env.items()}}
                for p in plan
            ],
        }

    def preflight(self, body: dict[str, Any]) -> dict[str, Any]:
        settings = self.parse(body)
        self.reload_token()
        found = self._validate(settings, fresh_adapters=True)
        issues = [i.to_json() for i in found]
        if errors(found):
            return {"issues": issues, "checks": [], "ok": False}
        if self.manager.busy() and bool(body.get("probe_drones")):
            # The edges hold the drones' command ports; a probe now would compete with them.
            raise ApiError(HTTPStatus.CONFLICT, "stop the running processes before checking the drones")
        token = self.reload_token()
        checks = pf.run_preflight(settings, token, self.preflight_env, probe_drones=bool(body.get("probe_drones")))
        self.last_checks = [self._redact_check(c.to_json()) for c in checks]
        return {"issues": issues, "checks": self.last_checks, "ok": not any(c.status == pf.FAIL for c in checks)}

    def _redact_check(self, row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "name": self.redact(row["name"]), "detail": self.redact(row["detail"])}

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        settings = self.parse(body)
        if not self._start_lock.acquire(blocking=False):
            raise ApiError(HTTPStatus.CONFLICT, "a start is already in progress")
        try:
            if self.manager.busy():
                raise ApiError(HTTPStatus.CONFLICT, "processes are already running; stop them first")
            self.reload_token()
            found = self._validate(settings, fresh_adapters=True)
            if errors(found):
                return {"started": False, "issues": [i.to_json() for i in found], "checks": []}
            token = self.reload_token()
            checks = pf.run_preflight(settings, token, self.preflight_env, probe_drones=True)
            self.last_checks = [self._redact_check(c.to_json()) for c in checks]
            if any(c.status == pf.FAIL for c in checks):
                return {"started": False, "issues": [i.to_json() for i in found], "checks": self.last_checks}
            written = None
            if settings.runs_ground:
                # The dashboard's plan decides the altitude slots on a dashboard flight, so it must read the
                # slots the edges are started with. Only here, on Start, and never while processes run.
                try:
                    written = write_ceiling(settings, self.ceiling_path, processes_running=self.manager.busy(),
                                            environ=self.environ)
                except (CeilingWriteError, OSError) as exc:
                    problem = Issue(ERROR, "ceiling_cm", f"the ceiling file was not written, so nothing was started: {exc}")
                    return {"started": False, "issues": [i.to_json() for i in found] + [problem.to_json()],
                            "checks": self.last_checks}
            try:
                self.store.save_last(settings)
            except (OSError, PresetError):
                pass  # remembering the settings is a convenience; it must not stop a start
            try:
                self.manager.start(build_plan(settings, token, self.python, self.environ))
            except ProcessError as exc:
                raise ApiError(HTTPStatus.CONFLICT, str(exc)) from None
            self._remember_adapters(settings)
            return {"started": True, "issues": [i.to_json() for i in found], "checks": self.last_checks,
                    "ceiling_file": written.to_json() if written is not None else None}
        finally:
            self._start_lock.release()

    def _remember_adapters(self, settings: LauncherSettings) -> None:
        """Remember the adapter confirmed for each real drone, by adapter name. A convenience; never stops a start."""
        try:
            adapters = [Adapter(a["name"], a["ipv4"], a["index"], a["up"]) for a in self.adapters()]
            self.store.remember_adapters(confirmed_mapping(settings, adapters))
        except (OSError, PresetError):
            pass

    def stop(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("confirm") is not True:
            raise ApiError(HTTPStatus.BAD_REQUEST, "stopping needs confirmation")
        if self.manager.stopping():
            return {"stopping": True}  # already stopping; a second signal would interrupt a landing
        thread = threading.Thread(target=self.manager.stop_all, daemon=True, name="launcher-stop")
        self._stop_threads.append(thread)
        thread.start()
        return {"stopping": True}

    def wait_for_stops(self, timeout: float = 60.0) -> None:
        """Let a stop the operator already asked for finish (it may still have to terminate a process)."""
        for thread in list(self._stop_threads):
            thread.join(timeout)

    def restart(self, body: dict[str, Any]) -> dict[str, Any]:
        name = body.get("name")
        if not isinstance(name, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "name the process to restart")
        if name not in self.manager.names():
            raise ApiError(HTTPStatus.NOT_FOUND, f"no process named {name!r} was started by this launcher")
        try:
            self.manager.restart(name, background=True)
        except ProcessError as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from None
        return {"restarting": name}

    def logs(self, name: str, offset: int) -> dict[str, Any]:
        try:
            text, new_offset, path = self.manager.read_log(name, offset)
        except ProcessError as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, str(exc)) from None
        return {"name": name, "text": text, "offset": new_offset, "log": path}

    def save_preset(self, body: dict[str, Any]) -> dict[str, Any]:
        settings = self.parse(body)
        try:
            self.store.save_preset(str(body.get("name") or ""), settings)
        except PresetError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from None
        except OSError as exc:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, f"cannot write the presets file: {exc}") from None
        return {"saved": True}

    def delete_preset(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            self.store.delete_preset(str(body.get("name") or ""))
        except PresetError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from None
        return {"deleted": True}

    def quit(self, body: dict[str, Any]) -> dict[str, Any]:
        action = body.get("action")
        if action not in ("stop", "leave"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "choose stop or leave")
        if self.on_quit is not None:
            threading.Thread(target=self.on_quit, args=(action,), daemon=True, name="launcher-quit").start()
        return {"quitting": action}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


_STATIC = {
    "/launcher.js": ("launcher.js", "text/javascript; charset=utf-8"),
    "/launcher.css": ("launcher.css", "text/css; charset=utf-8"),
}
_CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def _redact_tree(value: Any, redact: Callable[[str], str]) -> Any:
    """*value* with the token removed from every string in it, before it is encoded as JSON."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(str(k)): _redact_tree(v, redact) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_tree(v, redact) for v in value]
    return value


_KEY_REQUIRED = (
    b"This page needs the key from the launcher. Open the address that the launcher printed in its terminal "
    b"(http://127.0.0.1:<port>/?key=...), or restart the launcher."
)


def make_handler(app: LauncherApp, session: str) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{app.port}", f"localhost:{app.port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "FlightRiskLauncher"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            return  # quiet: a console write can block on Windows while text is selected

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", _CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            # Redacted before encoding (JSON escaping could hide a token from a later text search), and after.
            self._send(status, app.redact(json.dumps(_redact_tree(data, app.redact))).encode("utf-8"), "application/json")

        def _host_ok(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin in allowed_origins

        def _api_ok(self) -> bool:
            supplied = self.headers.get(SESSION_HEADER, "")
            # Bytes: compare_digest raises TypeError for a str that is not ASCII.
            return self._host_ok() and secrets.compare_digest(supplied.encode("utf-8", "surrogateescape"), session.encode("utf-8"))

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad Content-Length") from None
            if length > MAX_BODY:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send JSON")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ApiError(HTTPStatus.BAD_REQUEST, "the body is not JSON") from None
            if not isinstance(data, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "the body must be a JSON object")
            return data

        def _dispatch(self, method: str) -> None:
            url = urlsplit(self.path)
            path = url.path
            if method == "GET" and path in ("/", "/index.html"):
                if not self._host_ok():
                    return self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                # Like Jupyter: the page (and so the session key in it) is served only to whoever has the
                # address the launcher opened, which carries the key; a Host header alone is not enough.
                key = (parse_qs(url.query).get("key") or [""])[0]
                if not secrets.compare_digest(key.encode("utf-8"), session.encode("utf-8")):
                    return self._send(HTTPStatus.FORBIDDEN, _KEY_REQUIRED, "text/plain; charset=utf-8")
                page = (STATIC_DIR / "launcher.html").read_text(encoding="utf-8").replace("__SESSION__", session)
                return self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")
            if method == "GET" and path in _STATIC:
                if not self._host_ok():
                    return self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                name, ctype = _STATIC[path]
                return self._send(HTTPStatus.OK, (STATIC_DIR / name).read_bytes(), ctype)
            if not path.startswith("/api/"):
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            if not self._api_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            try:
                if method == "GET":
                    query = parse_qs(url.query)
                    if path == "/api/bootstrap":
                        return self._json(HTTPStatus.OK, app.bootstrap())
                    if path == "/api/state":
                        return self._json(HTTPStatus.OK, app.state())
                    if path == "/api/adapters":
                        return self._json(HTTPStatus.OK, {"adapters": app.adapters(refresh=True)})
                    if path == "/api/logs":
                        name = (query.get("name") or [""])[0]
                        try:
                            offset = max(0, int((query.get("offset") or ["0"])[0]))
                        except ValueError:
                            offset = 0
                        return self._json(HTTPStatus.OK, app.logs(name, offset))
                elif method == "POST":
                    routes = {
                        "/api/validate": app.validate,
                        "/api/preflight": app.preflight,
                        "/api/adapter-proposals": app.adapter_proposals,
                        "/api/start": app.start,
                        "/api/stop": app.stop,
                        "/api/restart": app.restart,
                        "/api/presets/save": app.save_preset,
                        "/api/presets/delete": app.delete_preset,
                        "/api/quit": app.quit,
                    }
                    if path in routes:
                        return self._json(HTTPStatus.OK, routes[path](self._body()))
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except ApiError as exc:
                return self._json(exc.status, {"error": exc.message, **exc.extra})
            except Exception as exc:  # noqa: BLE001 - the window must get an answer, and never a token
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"internal error: {type(exc).__name__}: {exc}"})

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

    return Handler


def exclusive_bind_options(sock: Any, windows: bool) -> None:
    """On Windows, make the bind exclusive, so a second launcher cannot share a port that is already listening."""
    if windows:
        sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)


def server_class(windows: bool | None = None) -> type[ThreadingHTTPServer]:
    """The HTTP server class: on Windows without address reuse (which there means sharing a live port)."""
    if windows is None:
        windows = sys.platform == "win32"

    class LauncherHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = not windows
        daemon_threads = True

        def server_bind(self) -> None:
            exclusive_bind_options(self.socket, windows)
            super().server_bind()

    return LauncherHTTPServer


def make_server(app: LauncherApp, session: str | None = None, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, str]:
    """A server for *app* on 127.0.0.1:``app.port`` (port 0 picks a free one and updates ``app.port``).

    The window's address is ``http://127.0.0.1:<port>/?key=<session>``.
    """
    session = session or secrets.token_urlsafe(24)
    if host != "127.0.0.1":
        raise ValueError("the launcher only listens on 127.0.0.1")
    server = server_class()((host, app.port), BaseHTTPRequestHandler)
    app.port = server.server_address[1]
    server.RequestHandlerClass = make_handler(app, session)
    return server, session


__all__ = ["ApiError", "LauncherApp", "make_server"]
