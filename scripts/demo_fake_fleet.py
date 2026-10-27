#!/usr/bin/env python3
"""Scales-to-N demo: N fake edge devices against one real ground station.

Starts the dashboard ground station with ``--source edge --fleet drone-1,...,drone-N``
and N ``python -m flightrisk.edge_device --source fake`` processes, all on
127.0.0.1 and sharing a random per-run ``FLIGHTRISK_EDGE_TOKEN``. The dashboard
splits the search area into N zones; when one edge dies, the coordinator
re-tasks its lanes to the others. No real drone is involved.

    python scripts/demo_fake_fleet.py --n 4
    python scripts/demo_fake_fleet.py --n 4 --drive --kill-drone drone-3
    python scripts/demo_fake_fleet.py --n 4 --attach        # ground station already running
    python scripts/demo_fake_fleet.py --n 2 --nodes 2       # two ground nodes, edges on node 1

With ``--nodes 2`` a second ground station starts beside the first (edge port
and dashboard port one higher, 9001 and 5556 by default). An edge connects to
one ground today, so all edges attach to node 1, or to the node named by
``--edge-node``; the other node idles as a standby. The ports reserved for the
future peer link (9100 and 9101) are printed but nothing listens on them yet.

Every command line is printed before it runs, so each process can be started
by hand. Every child is started in its own process group and is terminated
(then killed) on exit, on Ctrl-C and on SIGTERM.
"""

from __future__ import annotations

import argparse
import atexit
import os
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOPBACK = "127.0.0.1"
TOKEN_ENV_VAR = "FLIGHTRISK_EDGE_TOKEN"

#: The coordinator hands its zones these altitude slots in turn (cm), so the
#: edges are started with the same alternation to avoid a climb on assignment.
ALT_SLOTS_CM = (100, 160)

MAX_DRONES = 16
DEFAULT_DASHBOARD_PORT = 5555
DEFAULT_EDGE_PORT = 9000
DEFAULT_PEER_PORT = 9100
MAX_NODES = 2
TERM_GRACE_S = 8.0


def drone_ids(n: int) -> list[str]:
    return [f"drone-{k}" for k in range(1, n + 1)]


def default_area_cm(n: int) -> str:
    """Area giving each zone about 225 cm (three 75 cm lanes) across the 100 cm zone gaps."""
    return f"{325 * n - 100}x400"


def geofence_cm_for(area_cm: str, margin_cm: int = 100) -> int:
    """Edge geofence wide enough for any re-tasked transit across the whole area.

    The edge default is 500 cm around takeoff. After a drone is lost, a
    survivor flies a transit to the lost zone, which can lie farther away
    than that, and the edge's safety envelope refuses lanes beyond it. The
    drones here are fake, so the demo widens the fence to the area; real
    hardware keeps the default.
    """
    parts = [int(v) for v in area_cm.lower().split("x")]
    return max(500, max(parts) + margin_cm)


def alt_slot_cm(k: int) -> int:
    """Altitude slot for drone number ``k`` (1-based), matching the coordinator."""
    return ALT_SLOTS_CM[(k - 1) % len(ALT_SLOTS_CM)]


def build_dashboard_command(
    n: int, edge_port: int, dashboard_port: int, area_cm: str, strip_cm: int, python: str = sys.executable,
) -> list[str]:
    return [
        python, "-m", "flightrisk.dashboard",
        "--source", "edge",
        "--edge-host", LOOPBACK,
        "--edge-port", str(edge_port),
        "--port", str(dashboard_port),
        "--fleet", ",".join(drone_ids(n)),
        "--fleet-area-cm", area_cm,
        "--fleet-strip-cm", str(strip_cm),
        "--no-llm",
    ]


def build_edge_command(k: int, edge_port: int, python: str = sys.executable) -> list[str]:
    """Command line of the k-th fake edge (1-based)."""
    return [
        python, "-m", "flightrisk.edge_device",
        "--source", "fake",
        "--drone-id", f"drone-{k}",
        "--alt-slot", str(alt_slot_cm(k)),
        "--ws-url", f"ws://{LOOPBACK}:{edge_port}",
    ]


def node_label(node: int, nodes: int) -> str:
    """Label of ground node ``node`` (1-based): plain ``ground`` when there is only one."""
    return "ground" if nodes == 1 else f"ground-{node}"


def build_commands(
    n: int, edge_port: int, dashboard_port: int, area_cm: str | None = None, strip_cm: int = 75,
    attach: bool = False, python: str = sys.executable, nodes: int = 1, edge_node: int = 1,
) -> list[tuple[str, list[str]]]:
    """Return ``(label, argv)`` for each ground station (unless attaching) and each edge.

    Ground node ``k`` (1-based) listens for edges on ``edge_port + k - 1`` and serves its
    dashboard on ``dashboard_port + k - 1``. Every edge connects to node ``edge_node``.
    """
    if not 1 <= n <= MAX_DRONES:
        raise ValueError(f"--n must be between 1 and {MAX_DRONES}, got {n}")
    if not 1 <= nodes <= MAX_NODES:
        raise ValueError(f"--nodes must be between 1 and {MAX_NODES}, got {nodes}")
    if not 1 <= edge_node <= nodes:
        raise ValueError(f"--edge-node must be between 1 and {nodes}, got {edge_node}")
    if area_cm is None:
        area_cm = default_area_cm(n)
    commands: list[tuple[str, list[str]]] = []
    if not attach:
        for node in range(1, nodes + 1):
            commands.append((node_label(node, nodes), build_dashboard_command(
                n, edge_port + node - 1, dashboard_port + node - 1, area_cm, strip_cm, python)))
    for k in range(1, n + 1):
        commands.append((f"drone-{k}", build_edge_command(k, edge_port + edge_node - 1, python)))
    return commands


def format_command(argv: Sequence[str], token_env: bool = True) -> str:
    prefix = f"{TOKEN_ENV_VAR}=<token> " if token_env else ""
    return prefix + " ".join(shlex.quote(a) for a in argv)


def port_is_open(port: int, host: str = LOOPBACK) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


class ProcessGroup:
    """Child processes, each in its own session, all torn down together."""

    def __init__(self) -> None:
        self.procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def spawn(self, label: str, argv: Sequence[str], env: dict[str, str], log_path: Path | None = None) -> subprocess.Popen:
        out = open(log_path, "ab") if log_path is not None else subprocess.DEVNULL
        try:
            proc = subprocess.Popen(
                list(argv), cwd=str(REPO_ROOT), env=env, stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        finally:
            if log_path is not None:
                out.close()
        with self._lock:
            self.procs[label] = proc
        return proc

    def kill_one(self, label: str, sig: int = signal.SIGKILL) -> bool:
        proc = self.procs.get(label)
        if proc is None or proc.poll() is not None:
            return False
        _signal_group(proc, sig)
        return True

    def terminate_all(self, grace_s: float = TERM_GRACE_S) -> None:
        """SIGTERM every live process group, wait, then SIGKILL what is left."""
        with self._lock:
            procs = list(self.procs.values())
        for proc in procs:
            if proc.poll() is None:
                _signal_group(proc, signal.SIGTERM)
        deadline = time.monotonic() + grace_s
        for proc in procs:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _signal_group(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass

    def alive(self) -> list[str]:
        with self._lock:
            return [label for label, proc in self.procs.items() if proc.poll() is None]


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def drive_fleet(
    dashboard_port: int, group: ProcessGroup, kill_drone: str, kill_after_s: float,
    watch_s: float, n: int, out=print,
) -> bool:
    """Plan, start, kill one edge and print the fleet events the browser would see.

    Returns True when a ``retask`` fleet_event was observed after the kill.
    """
    import socketio  # python-socketio client; imported late so --help needs nothing

    events: list[tuple[float, str, dict]] = []
    lock = threading.Lock()
    client = socketio.Client(reconnection=False)
    t0 = time.monotonic()

    def record(name):
        def handler(data=None):
            with lock:
                events.append((time.monotonic() - t0, name, data if isinstance(data, dict) else {}))
        return handler

    for name in ("fleet_plan", "fleet_event", "fleet_status", "error"):
        client.on(name, record(name))
    client.connect(f"http://{LOOPBACK}:{dashboard_port}", wait_timeout=30)

    def wait_for(predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with lock:
                if predicate(list(events)):
                    return True
            time.sleep(0.2)
        return False

    def connected_edges(evs):
        for _, name, data in reversed(evs):
            if name == "fleet_status":
                drones = data.get("drones") or {}
                return sum(1 for d in drones.values() if d.get("battery") is not None)
        return 0

    out(f"[drive] waiting for {n} edges to report telemetry")
    if not wait_for(lambda evs: connected_edges(evs) >= n, 60):
        out("[drive] edges never all reported telemetry")
        client.disconnect()
        return False
    out("[drive] emit plan_fleet")
    client.emit("plan_fleet")
    if not wait_for(lambda evs: any(e[1] == "fleet_plan" for e in evs), 60):
        out("[drive] no fleet_plan received")
        client.disconnect()
        return False
    with lock:
        plan = [e for e in events if e[1] == "fleet_plan"][-1][2]
    out(f"[drive] fleet_plan: {len(plan.get('zones', []))} zones: "
        + ", ".join(f"{z['drone_id']}@x={z.get('x_cm')}cm/{len(z.get('lanes', []))} lanes/alt {z.get('alt_cm')}cm"
                    for z in plan.get("zones", [])))
    time.sleep(2.0)  # let the assign_lanes acks arrive
    out("[drive] emit start_fleet")
    client.emit("start_fleet")
    if not wait_for(lambda evs: any(e[1] == "fleet_event" and e[2].get("event") in ("start", "start_refused") for e in evs), 30):
        out("[drive] no start event")
        client.disconnect()
        return False
    with lock:
        started = [e for e in events if e[1] == "fleet_event" and e[2].get("event") in ("start", "start_refused")][-1]
    out(f"[drive] fleet_event {started[2]}")
    if started[2].get("event") != "start":
        client.disconnect()
        return False

    time.sleep(kill_after_s)
    with lock:
        mark = len(events)
    out(f"[drive] killing {kill_drone} (SIGKILL on its process group)")
    t_kill = time.monotonic() - t0
    group.kill_one(kill_drone)

    def retasked(evs):
        for _, name, data in evs[mark:]:
            if name == "fleet_event" and data.get("event") == "retask":
                return True
        return False

    ok = wait_for(retasked, watch_s)
    time.sleep(1.0)
    with lock:
        for t, name, data in events[mark:]:
            if name in ("fleet_event", "fleet_plan"):
                summary = data if name == "fleet_event" else {
                    "zones": [(z["drone_id"], len(z.get("lanes", []))) for z in data.get("zones", [])]
                }
                out(f"[drive] +{t - t_kill:5.1f}s after the kill: {name} {summary}")
    out(f"[drive] re-task observed: {ok}")
    client.disconnect()
    return ok


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run N fake edges against a real ground station on loopback.")
    p.add_argument("--n", type=int, required=True, help=f"Number of fake drones (1 to {MAX_DRONES}).")
    p.add_argument("--attach", action="store_true",
                   help="Do not start a ground station; connect to one already running on --edge-port. "
                        f"Set {TOKEN_ENV_VAR} to its token if it uses one.")
    p.add_argument("--dashboard-port", type=int, default=DEFAULT_DASHBOARD_PORT)
    p.add_argument("--edge-port", type=int, default=DEFAULT_EDGE_PORT)
    p.add_argument("--area-cm", default=None,
                   help="Fleet search area in cm: 400 or WxD. Default: as wide as 3 lanes per zone need "
                        "for N drones (WxD with W = 325*N - 100, D = 400).")
    p.add_argument("--strip-cm", type=int, default=75, help="Lane spacing in cm (default 75).")
    p.add_argument("--nodes", type=int, default=1,
                   help=f"Number of ground stations to start (1 to {MAX_NODES}, default 1). Node k uses "
                        "--edge-port and --dashboard-port plus k-1.")
    p.add_argument("--edge-node", type=int, default=1,
                   help="Ground node the edges connect to with --nodes 2 (default 1).")
    p.add_argument("--dry-run", action="store_true", help="Print the command lines and exit.")
    p.add_argument("--drive", action="store_true",
                   help="Drive the dashboard: plan, start, kill one edge, print the re-task events, then exit.")
    p.add_argument("--kill-drone", default=None, help="Edge to kill with --drive (default: the last drone).")
    p.add_argument("--kill-after-s", type=float, default=8.0, help="Seconds of flight before the kill (default 8).")
    p.add_argument("--watch-s", type=float, default=30.0, help="Seconds to watch for the re-task (default 30).")
    p.add_argument("--log-dir", type=Path, default=None, help="Write each child's output to <dir>/<label>.log.")
    p.add_argument("--ready-timeout-s", type=float, default=120.0)
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.area_cm is None:
        args.area_cm = default_area_cm(args.n)
    try:
        commands = build_commands(args.n, args.edge_port, args.dashboard_port, args.area_cm, args.strip_cm,
                                  args.attach, nodes=args.nodes, edge_node=args.edge_node)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    grounds = [c for c in commands if c[0].startswith("ground")]
    edge_port = args.edge_port + args.edge_node - 1
    dashboard_port = args.dashboard_port + args.edge_node - 1

    if args.attach:
        token = os.environ.get(TOKEN_ENV_VAR) or None
    else:
        token = secrets.token_urlsafe(24)
    print(f"Loopback only ({LOOPBACK}); token {'set from the environment' if args.attach and token else 'random per run' if token else 'not set'}.")
    for label, argv_ in commands:
        print(f"[{label}] {format_command(argv_, token_env=token is not None)}")
    if args.nodes > 1:
        print("Ground nodes: " + ", ".join(
            f"node {k}: edges :{args.edge_port + k - 1}, dashboard :{args.dashboard_port + k - 1}, "
            f"peer :{DEFAULT_PEER_PORT + k - 1} (reserved, unused)" for k in range(1, args.nodes + 1)))
        print(f"All edges connect to node {args.edge_node}; the other node is a standby with no edges.")
    if args.dry_run:
        return 0

    env = dict(os.environ)
    env.pop(TOKEN_ENV_VAR, None)
    if "FLIGHTRISK_GEOFENCE_CM" not in env:
        env["FLIGHTRISK_GEOFENCE_CM"] = str(geofence_cm_for(args.area_cm))
        print(f"Edge geofence widened to {env['FLIGHTRISK_GEOFENCE_CM']} cm (FLIGHTRISK_GEOFENCE_CM) so re-tasked "
              "transits are not refused; fake drones only.")
    if token:
        env[TOKEN_ENV_VAR] = token
    env.setdefault("PYTHONUNBUFFERED", "1")
    if args.log_dir is not None:
        args.log_dir.mkdir(parents=True, exist_ok=True)

    group = ProcessGroup()
    atexit.register(group.terminate_all)

    def _on_signal(signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_signal)
    result = 0
    try:
        if args.attach:
            if not port_is_open(edge_port):
                print(f"error: nothing is listening on {LOOPBACK}:{edge_port}", file=sys.stderr)
                return 1
        else:
            for node, (label, _) in enumerate(grounds, start=1):
                if port_is_open(args.edge_port + node - 1):
                    print(f"error: {LOOPBACK}:{args.edge_port + node - 1} is already in use; "
                          "use --attach or --edge-port", file=sys.stderr)
                    return 1
            for label, argv_ in grounds:
                group.spawn(label, argv_, env, args.log_dir / f"{label}.log" if args.log_dir else None)
            deadline = time.monotonic() + args.ready_timeout_s
            for node, (label, _) in enumerate(grounds, start=1):
                while not port_is_open(args.edge_port + node - 1):
                    if group.procs[label].poll() is not None:
                        print("error: the ground station exited during startup", file=sys.stderr)
                        return 1
                    if time.monotonic() > deadline:
                        print("error: the ground station did not open its edge port in time", file=sys.stderr)
                        return 1
                    time.sleep(0.5)
                print(f"Ground station is listening on {LOOPBACK}:{args.edge_port + node - 1}; "
                      f"dashboard at http://{LOOPBACK}:{args.dashboard_port + node - 1}")
        for label, argv_ in commands[len(grounds):]:
            group.spawn(label, argv_, env, args.log_dir / f"{label}.log" if args.log_dir else None)
        print(f"Started {args.n} fake edges. Ctrl-C stops everything.")

        if args.drive:
            kill_drone = args.kill_drone or f"drone-{args.n}"
            result = 0 if drive_fleet(dashboard_port, group, kill_drone, args.kill_after_s, args.watch_s, args.n) else 1
        else:
            while True:
                dead = [label for label, proc in group.procs.items() if proc.poll() is not None]
                if len(dead) == len(group.procs):
                    print("All processes exited.")
                    break
                time.sleep(1.0)
    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        group.terminate_all()
        left = group.alive()
        print("All child processes stopped." if not left else f"WARNING: still running: {left}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
