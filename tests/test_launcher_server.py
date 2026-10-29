"""The launcher window's server and API, driven over HTTP on 127.0.0.1 (flightrisk/launcher/server.py).

This is the window's headless smoke test: it loads the page, its script and
style, and drives Check, Start, logs, Stop, presets and Quit through the same
API the page uses. The children are stand-in sleepers, never FlightRisk
itself, and no drone is contacted (the preflight environment is faked).
"""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import sys
import threading
import time

import pytest

from flightrisk.launcher.commands import EdgeToken, Redactor
from flightrisk.launcher.netinfo import Adapter
from flightrisk.launcher.preflight import PreflightEnv, TelloProbe
from flightrisk.launcher.processes import ProcessManager
from flightrisk.launcher.server import STATIC_DIR, LauncherApp, make_server
from flightrisk.launcher.settings import PresetStore, builtin_presets

TOKEN = "server-test-token-XYZ789"
SLEEPER = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGINT, lambda *a: sys.exit(0))\n"
    "print('child up; token', os.environ.get('FLIGHTRISK_EDGE_TOKEN'), flush=True)\n"
    "time.sleep(60)\n"
)


def _preflight_env(probe=None) -> PreflightEnv:
    return PreflightEnv(
        in_venv=lambda: True,
        python=sys.executable,
        module_found=lambda name: True,
        port_free=lambda host, port: True,
        tcp_reachable=lambda host, port: True,
        adapters=lambda: [Adapter("lo0", "127.0.0.1", 1), Adapter("en0", "192.168.10.2", 14)],
        weights=lambda: [
            {"name": "default", "label": "Default", "weights": "yolo11n.pt", "required": False, "found": "yolo11n.pt", "fallback": None},
            {"name": "top_down", "label": "Top", "weights": "models/heridal_yolo11s.pt", "required": True, "found": None, "fallback": "level"},
            {"name": "level", "label": "Level", "weights": "yolo11n.pt", "required": False, "found": None, "fallback": None},
        ],
        ceiling_file=lambda: (200, 60, None, "/x/ceiling.json"),
        tello_probe=probe or (lambda d: TelloProbe(True, battery=77, temp="50C", detail="fake")),
        environ={},
        is_windows=False,
    )


class Client:
    def __init__(self, port: int, session: str):
        self.port = port
        self.session = session

    def request(self, method, path, body=None, *, session=True, host=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if session:
            headers["X-Launcher-Session"] = self.session
        if origin:
            headers["Origin"] = origin
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode()
        conn.close()
        return resp.status, raw

    def get(self, path, **kw):
        status, raw = self.request("GET", path, **kw)
        return status, json.loads(raw) if raw.startswith(("{", "[")) else raw

    def post(self, path, body, **kw):
        status, raw = self.request("POST", path, body, **kw)
        return status, json.loads(raw)


@pytest.fixture
def launcher(tmp_path):
    quits = []
    manager = ProcessManager(
        Redactor(TOKEN),
        log_dir=tmp_path / "logs",
        cwd=tmp_path,
        popen=lambda argv, **kw: subprocess.Popen([sys.executable, "-c", SLEEPER], **kw),
        port_open=lambda host, port: True,
        edge_grace_s=5.0,
        ground_grace_s=5.0,
        poll_s=0.05,
    )
    app = LauncherApp(
        port=0,
        store=PresetStore(tmp_path / "launcher.json", secret=lambda: TOKEN),
        token_loader=lambda: EdgeToken(TOKEN, "environment"),
        manager=manager,
        preflight_env=_preflight_env(),
        python=sys.executable,
        on_quit=quits.append,
    )
    server, session = make_server(app)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        yield Client(app.port, session), app, quits
    finally:
        manager.stop_all()
        server.shutdown()
        server.server_close()


def _wait(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


REHEARSAL = builtin_presets()["Rehearsal with fake drones"].to_json()


def test_page_script_and_style_are_served(launcher):
    client, _app, _ = launcher
    status, page = client.request("GET", "/", session=False)
    assert status == 200
    assert client.session in page and "__SESSION__" not in page
    assert '<script src="/launcher.js">' in page
    for path in ("/launcher.js", "/launcher.css"):
        status, body = client.request("GET", path, session=False)
        assert status == 200 and len(body) > 500
    assert TOKEN not in page


def test_foreign_host_origin_and_missing_session_are_refused(launcher):
    client, _, _ = launcher
    assert client.request("GET", "/", session=False, host="evil.example:80")[0] == 403
    assert client.get("/api/state", session=False)[0] == 403
    assert client.get("/api/state", host=f"attacker.test:{client.port}")[0] == 403
    assert client.post("/api/stop", {"confirm": True}, origin="http://evil.example")[0] == 403
    assert client.get("/api/state", origin=f"http://127.0.0.1:{client.port}")[0] == 200


def test_bootstrap_lists_presets_and_never_the_token(launcher):
    client, _, _ = launcher
    status, data = client.get("/api/bootstrap")
    assert status == 200
    names = [p["name"] for p in data["presets"]]
    assert "Layout A (ground here)" in names and "Rehearsal with fake drones" in names
    assert data["token"] == {"status": "set", "source": "environment"}
    assert {p["name"]: p["available"] for p in data["profiles"]} == {"default": True, "top_down": False, "level": True}
    assert TOKEN not in json.dumps(data)


def test_validate_returns_issues_fleet_and_plan(launcher):
    client, _, _ = launcher
    status, data = client.post("/api/validate", {"settings": REHEARSAL})
    assert status == 200
    assert [i for i in data["issues"] if i["level"] == "error"] == []
    assert data["fleet"] == ["drone-1", "drone-2"]
    assert [p["name"] for p in data["plan"]] == ["ground", "edge-drone-1", "edge-drone-2"]
    bad = {**REHEARSAL, "dashboard_port": client.port}
    status, data = client.post("/api/validate", {"settings": bad})
    assert any(i["field"] == "dashboard_port" for i in data["issues"])
    assert client.post("/api/validate", {"settings": {**REHEARSAL, "edge_port": "x"}})[0] == 400


def test_start_logs_and_stop_through_the_api(launcher):
    client, _app, _ = launcher
    status, data = client.post("/api/start", {"settings": REHEARSAL})
    assert status == 200 and data["started"] is True, data
    assert _wait(lambda: [p["status"] for p in client.get("/api/state")[1]["processes"]] == ["running"] * 3)
    status, state = client.get("/api/state")
    assert [p["name"] for p in state["processes"]] == ["ground", "edge-drone-1", "edge-drone-2"]
    assert client.post("/api/start", {"settings": REHEARSAL})[0] == 409

    def log_text():
        return client.get("/api/logs?name=edge-drone-1&offset=0")[1]["text"]

    assert _wait(lambda: "child up" in log_text())
    assert TOKEN not in log_text() and "[redacted]" in log_text()
    # Checking the drones while edges run is refused: they hold the command ports.
    assert client.post("/api/preflight", {"settings": REHEARSAL, "probe_drones": True})[0] == 409

    assert client.post("/api/stop", {})[0] == 400
    assert client.post("/api/stop", {"confirm": True})[0] == 200
    assert _wait(lambda: {p["status"] for p in client.get("/api/state")[1]["processes"]} == {"stopped"})
    assert TOKEN not in json.dumps(client.get("/api/state")[1])


def test_restart_through_the_api(launcher):
    client, _, _ = launcher
    client.post("/api/start", {"settings": REHEARSAL})
    assert _wait(lambda: [p["status"] for p in client.get("/api/state")[1]["processes"]] == ["running"] * 3)
    pid = client.get("/api/state")[1]["processes"][1]["pid"]
    assert client.post("/api/restart", {"name": "edge-drone-1"})[0] == 200

    def new_pid():
        row = client.get("/api/state")[1]["processes"][1]
        return row["status"] == "running" and row["pid"] not in (None, pid)

    assert _wait(new_pid)
    assert client.post("/api/restart", {"name": "someone-else"})[0] == 404


def test_start_refused_on_errors_or_failed_checks(launcher):
    client, app, _ = launcher
    _, data = client.post("/api/start", {"settings": {**REHEARSAL, "ceiling_cm": 100}})
    assert data["started"] is False and data["issues"]
    app.preflight_env.port_free = lambda host, port: False
    _, data = client.post("/api/start", {"settings": REHEARSAL})
    assert data["started"] is False
    assert any(c["status"] == "FAIL" and "port" in c["name"].lower() for c in data["checks"])
    assert client.get("/api/state")[1]["processes"] == []


def test_preflight_shows_battery_from_the_probe(launcher):
    client, _, _ = launcher
    single = builtin_presets()["Single drone"].to_json()
    status, data = client.post("/api/preflight", {"settings": single, "probe_drones": True})
    assert status == 200 and data["ok"] is True
    row = next(c for c in data["checks"] if c["name"] == "Drone drone-1")
    assert row["data"]["battery"] == 77 and "battery 77%" in row["detail"]


def test_presets_save_and_delete_and_never_store_the_token(launcher, tmp_path):
    client, _, _ = launcher
    settings = {**REHEARSAL, "token": TOKEN}
    assert client.post("/api/presets/save", {"name": "Mine", "settings": settings})[0] == 200
    assert TOKEN not in (tmp_path / "launcher.json").read_text()
    names = [p["name"] for p in client.get("/api/bootstrap")[1]["presets"]]
    assert "Mine" in names
    assert client.post("/api/presets/save", {"name": "Single drone", "settings": REHEARSAL})[0] == 400
    assert client.post("/api/presets/delete", {"name": "Mine"})[0] == 200
    assert client.post("/api/presets/delete", {"name": "Single drone"})[0] == 400


def test_quit_needs_a_choice(launcher):
    client, _, quits = launcher
    assert client.post("/api/quit", {"action": "maybe"})[0] == 400
    assert client.post("/api/quit", {"action": "leave"})[0] == 200
    assert _wait(lambda: quits == ["leave"])


def test_non_json_body_is_refused(launcher):
    client, _, _ = launcher
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    conn.request("POST", "/api/stop", body="confirm=true", headers={
        "Host": f"127.0.0.1:{client.port}", "X-Launcher-Session": client.session,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    assert conn.getresponse().status == 415
    conn.close()


def test_server_listens_on_loopback_only():
    app = LauncherApp(port=0, store=PresetStore("/nonexistent/launcher.json"), token_loader=lambda: EdgeToken(None, None),
                      manager=ProcessManager(Redactor()), preflight_env=_preflight_env())
    with pytest.raises(ValueError):
        make_server(app, host="0.0.0.0")
    server, _ = make_server(app)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed, so the window script cannot be syntax-checked")
def test_window_script_parses():
    result = subprocess.run(["node", "--check", str(STATIC_DIR / "launcher.js")], capture_output=True, text=True, timeout=60,
                            check=False)
    assert result.returncode == 0, result.stderr


def test_builtin_presets_start_from_the_dashboards_saved_ceiling(launcher):
    client, app, _ = launcher
    app.preflight_env.ceiling_file = lambda: (160, 30, None, "/x/ceiling.json")
    presets = {p["name"]: p for p in client.get("/api/bootstrap")[1]["presets"]}
    assert (presets["Single drone"]["settings"]["ceiling_cm"], presets["Single drone"]["settings"]["margin_cm"]) == (160, 30)
    data = client.get("/api/bootstrap")[1]
    assert data["settings"]["ceiling_cm"] == 160
    # An unreadable file leaves the code defaults in place.
    app.preflight_env.ceiling_file = lambda: (200, 60, "bad file", "/x/ceiling.json")
    presets = {p["name"]: p for p in client.get("/api/bootstrap")[1]["presets"]}
    assert presets["Single drone"]["settings"]["ceiling_cm"] == 200


def test_quit_waits_for_a_stop_already_asked_for(launcher):
    client, app, _ = launcher
    client.post("/api/start", {"settings": REHEARSAL})
    assert _wait(lambda: [p["status"] for p in client.get("/api/state")[1]["processes"]] == ["running"] * 3)
    client.post("/api/stop", {"confirm": True})
    app.wait_for_stops()
    assert {p["status"] for p in client.get("/api/state")[1]["processes"]} == {"stopped"}


def test_launcher_runs_on_an_edge_only_install():
    """An edge laptop has only requirements-edge.txt: the launcher must not need Flask or the vision stack."""
    check = (
        "import importlib.abc, sys\n"
        "BLOCKED = ('flask', 'flask_socketio', 'insightface', 'ollama', 'open_clip', 'mediapipe', 'torch', 'ultralytics', 'cv2')\n"
        "class Blocker(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in BLOCKED:\n"
        "            raise ImportError(name + ' is blocked for this test')\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "import flightrisk.launcher.__main__, flightrisk.launcher.server\n"
        "from flightrisk.launcher.preflight import _weights\n"
        "assert {row['name'] for row in _weights()} >= {'default', 'top_down', 'level'}\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True, timeout=120, check=False,
                            cwd=str(STATIC_DIR.parents[2]))
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr
