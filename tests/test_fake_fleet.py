"""Tests for the scales-to-N fake-edge demo (R-082).

Covers the launcher's command construction, its process teardown (including
a grandchild and a child that ignores SIGTERM), and an end-to-end run in
which four fake edges connect over a real token-authenticated
``GroundTransportSync`` on 127.0.0.1, the fleet supervisor plans four zones,
and dropping one edge re-assigns its lanes to the others.

No real drone is involved. The edges are in-process equivalents of
``python -m flightrisk.edge_device --source fake`` (same ``LaneExecutor``,
``FakeTelloController`` and transport). Every wait has a generous margin
because many test processes may share the machine.
"""

import importlib.util
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from flightrisk.dashboard.app import FleetSupervisor
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController
from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import DroneFrameSource, _make_sender, _wire_emergency_callback, run_edge_device
from flightrisk.observability import StructuredLogger
from flightrisk.transport import EdgeTransportSync, GroundTransportSync

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "demo_fake_fleet.py"
WAIT_S = 20.0


def _load_launcher():
    spec = importlib.util.spec_from_file_location("demo_fake_fleet", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_fake_fleet"] = module
    spec.loader.exec_module(module)
    return module


launcher = _load_launcher()


def _wait_for(predicate, timeout=WAIT_S, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return predicate()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers signal 0; ps reports its state.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


# ---------------------------------------------------------------------------
# Launcher command construction
# ---------------------------------------------------------------------------

def test_launcher_builds_n_edge_commands():
    commands = launcher.build_commands(4, edge_port=9123, dashboard_port=5999, python="py")
    labels = [label for label, _ in commands]
    assert labels == ["ground", "drone-1", "drone-2", "drone-3", "drone-4"]

    ground = commands[0][1]
    assert ground[:3] == ["py", "-m", "flightrisk.dashboard"]
    assert ground[ground.index("--source") + 1] == "edge"
    assert ground[ground.index("--fleet") + 1] == "drone-1,drone-2,drone-3,drone-4"
    assert ground[ground.index("--edge-host") + 1] == "127.0.0.1"
    assert ground[ground.index("--edge-port") + 1] == "9123"
    assert ground[ground.index("--port") + 1] == "5999"
    assert "--no-llm" in ground

    ids, slots = [], []
    for label, argv in commands[1:]:
        assert argv[:3] == ["py", "-m", "flightrisk.edge_device"]
        assert argv[argv.index("--source") + 1] == "fake"
        assert argv[argv.index("--ws-url") + 1] == "ws://127.0.0.1:9123"
        assert argv[argv.index("--drone-id") + 1] == label
        ids.append(label)
        slots.append(argv[argv.index("--alt-slot") + 1])
    assert len(set(ids)) == 4
    assert slots == ["100", "160", "100", "160"]  # the coordinator's slot order

    # The ids in --fleet are exactly the ids the edges announce.
    assert ground[ground.index("--fleet") + 1].split(",") == ids


def test_launcher_attach_skips_the_ground_station_and_rejects_bad_n():
    commands = launcher.build_commands(2, 9000, 5555, attach=True)
    assert [label for label, _ in commands] == ["drone-1", "drone-2"]
    for bad in (0, -1, launcher.MAX_DRONES + 1):
        with pytest.raises(ValueError):
            launcher.build_commands(bad, 9000, 5555)


def test_launcher_commands_are_loopback_only_and_carry_no_token():
    for n in (1, 3, 8):
        for _, argv in launcher.build_commands(n, 9000, 5555):
            text = " ".join(argv)
            assert "0.0.0.0" not in text
            assert launcher.TOKEN_ENV_VAR not in text
    printed = launcher.format_command(["a", "b c"])
    assert printed.startswith("FLIGHTRISK_EDGE_TOKEN=<token> ") and "'b c'" in printed


def test_dry_run_prints_every_command_and_starts_nothing(capsys):
    assert launcher.main(["--n", "3", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.count("flightrisk.edge_device") == 3
    assert out.count("flightrisk.dashboard") == 1


def test_geofence_covers_the_whole_area():
    assert launcher.geofence_cm_for("1200x400") == 1300
    assert launcher.geofence_cm_for("400") == 500


def test_default_area_gives_each_zone_three_lanes():
    assert launcher.default_area_cm(4) == "1200x400"
    assert launcher.default_area_cm(1) == "225x400"


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

_GRANDPARENT = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(120)\n"
)
_STUBBORN = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(120)\n"
)


def test_terminate_all_leaves_no_orphans(tmp_path):
    group = launcher.ProcessGroup()
    try:
        log = tmp_path / "grandparent.log"
        gp = group.spawn("gp", [sys.executable, "-u", "-c", _GRANDPARENT], dict(os.environ), log)
        stubborn_log = tmp_path / "stubborn.log"
        stubborn = group.spawn("stubborn", [sys.executable, "-u", "-c", _STUBBORN], dict(os.environ), stubborn_log)
        assert _wait_for(lambda: log.read_text().strip().isdigit())
        assert _wait_for(lambda: "ready" in stubborn_log.read_text())
        grandchild = int(log.read_text().strip())
        assert _pid_alive(grandchild)

        group.terminate_all(grace_s=1.0)

        assert gp.poll() is not None and stubborn.poll() is not None
        assert group.alive() == []
        assert _wait_for(lambda: not _pid_alive(grandchild), timeout=5.0), "the grandchild was orphaned"
    finally:
        for proc in group.procs.values():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)


def test_ctrl_c_tears_down_the_launched_processes(tmp_path):
    """Attach to a throwaway listener, send SIGINT and check the edges die."""
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    # Replace the edge command with a long sleep so the test needs no ML stack.
    shim = tmp_path / "shim.py"
    shim.write_text(
        "import sys, importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('demo_fake_fleet', {str(SCRIPT)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "m.build_edge_command = lambda k, p, python=sys.executable: [python, '-c', 'import time; print(\"edge\", flush=True); time.sleep(120)']\n"
        "raise SystemExit(m.main())\n"
    )
    log_dir = tmp_path / "logs"
    proc = subprocess.Popen(
        [sys.executable, "-u", str(shim), "--n", "3", "--attach", "--edge-port", str(port), "--log-dir", str(log_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        lines = []

        def _read():
            for line in proc.stdout:
                lines.append(line)

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        assert _wait_for(lambda: any("Started 3 fake edges" in line for line in lines))
        assert _wait_for(lambda: all((log_dir / f"drone-{k}.log").exists() and
                                     "edge" in (log_dir / f"drone-{k}.log").read_text() for k in (1, 2, 3)))
        children = subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True, text=True).stdout.split()
        assert len(children) == 3
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=30) == 0
        reader.join(5)
        assert any("All child processes stopped" in line for line in lines)
        for pid in children:
            assert _wait_for(lambda pid=pid: not _pid_alive(int(pid)), timeout=5.0), f"child {pid} was orphaned"
    finally:
        if proc.poll() is None:
            proc.kill()
        listener.close()


# ---------------------------------------------------------------------------
# N fake edges, a real ground transport, plan and re-task
# ---------------------------------------------------------------------------

class FakeEdge:
    """In-process equivalent of ``edge_device --source fake`` for one drone."""

    def __init__(self, drone_id: str, alt_slot_cm: int, port: int, token: str):
        self.drone_id = drone_id
        self.log = StructuredLogger(component=f"test-edge-{drone_id}")
        self.runner = EdgeRunner(detector=None)
        self.transport = EdgeTransportSync(ws_url=f"ws://127.0.0.1:{port}", runner=self.runner, token=token, max_retries=0)
        self.drone = FakeTelloController(name=drone_id, speed_cm_s=150.0)
        self.flight = LaneExecutor(
            self.drone, SafetyEnvelope.from_env(alt_slot_cm=alt_slot_cm), drone_id,
            send=_make_sender(self.transport, self.log), logger=self.log,
        )
        self.dropped = False
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"fake-edge-{drone_id}")

    def _run(self):
        run_edge_device(
            DroneFrameSource(self.drone, self.stop, min_interval_s=0.2),
            self.transport, self.runner, logger=self.log, stop_event=self.stop, flight=self.flight,
        )

    def start(self):
        assert self.drone.connect()
        self.flight.start()
        _wire_emergency_callback(self.transport, self.flight)
        self.transport.connect(timeout=WAIT_S)
        self.thread.start()

    def drop(self):
        """Vanish like a killed process: the socket closes, nothing lands the drone."""
        self.dropped = True
        self.stop.set()
        self.transport.disconnect(timeout=WAIT_S)
        self.flight.shutdown(timeout=0.5)

    def close(self):
        self.stop.set()
        if self.dropped:
            self.thread.join(timeout=10)
            return
        try:
            self.flight.shutdown(timeout=2.0)
        except Exception:
            pass
        try:
            self.transport.disconnect(timeout=WAIT_S)
        except Exception:
            pass
        self.thread.join(timeout=10)


@pytest.fixture
def fleet_of_four(monkeypatch):
    # The edges' default 500 cm geofence would refuse a re-tasked transit
    # across a 1200 cm area, as the launcher's own environment does.
    monkeypatch.setenv("FLIGHTRISK_GEOFENCE_CM", str(launcher.geofence_cm_for("1200x400")))
    token = secrets.token_urlsafe(16)
    ids = launcher.drone_ids(4)
    holder = {"supervisor": None}
    transport = GroundTransportSync(
        host="127.0.0.1", port=0, token=token, heartbeat_interval_s=1.0,
        on_control=lambda msg: holder["supervisor"] and holder["supervisor"].handle_control(msg),
    )
    sio = MagicMock()
    supervisor = FleetSupervisor(transport, ids, sio, area_cm=(1200, 400), strip_cm=75)
    holder["supervisor"] = supervisor
    transport.start(timeout=WAIT_S)
    edges = {
        drone_id: FakeEdge(drone_id, launcher.alt_slot_cm(k), transport.port, token)
        for k, drone_id in enumerate(ids, start=1)
    }
    try:
        for edge in edges.values():
            edge.start()
        yield supervisor, edges, sio
    finally:
        for edge in edges.values():
            edge.close()
        supervisor.stop()
        transport.stop(timeout=WAIT_S)


def _events(sio, name):
    return [call.args[1] for call in sio.emit.call_args_list if call.args and call.args[0] == name]


@pytest.mark.filterwarnings("ignore:coroutine .EdgeTransport.send. was never awaited")
def test_n_edges_plan_and_retask_on_loopback(fleet_of_four):
    supervisor, edges, sio = fleet_of_four
    coordinator = supervisor.coordinator

    # Every edge reports telemetry, which is what binds its socket to its id.
    def all_reported():
        status = coordinator.status()
        return all(status[d].get("battery") is not None for d in edges)
    assert _wait_for(all_reported), coordinator.status()

    plan = supervisor.plan()
    assert [z.drone_id for z in plan.zones] == list(edges)
    assert len(plan.zones) == 4
    assert [z.alt_cm for z in plan.zones] == [100, 160, 100, 160]
    assert len({z.x_cm for z in plan.zones}) == 4
    lanes_before = {d: len(coordinator.lanes(d)) for d in edges}
    assert all(count >= 3 for count in lanes_before.values())
    assert _events(sio, "fleet_plan")[-1]["zones"][0]["drone_id"] == "drone-1"

    # Each edge acks the lanes it was sent.
    assert _wait_for(lambda: all(
        supervisor.coordinator.status()[d].get("lanes_total") == lanes_before[d] or
        supervisor.coordinator.status()[d].get("lanes_total") is None for d in edges
    ))
    result = None
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        result = supervisor.start()
        if result.ok:
            break
        time.sleep(0.3)
    assert result is not None and result.ok, getattr(result, "refusals", None)

    # Wait until drone-4 is flying, then drop its edge.
    assert _wait_for(lambda: edges["drone-4"].drone.state.is_flying and
                     (coordinator.status()["drone-4"].get("height_cm") or 0) > 0)
    edges["drone-4"].drop()

    assert _wait_for(lambda: supervisor.check_losses() or coordinator._drones["drone-4"].lost)
    assert coordinator._drones["drone-4"].lost

    retask = _events(sio, "fleet_event")
    retask = [e for e in retask if e.get("event") == "retask"]
    assert retask and retask[0]["drone_id"] == "drone-4"
    gained = retask[0]["to"]
    assert gained and set(gained) <= {"drone-1", "drone-2", "drone-3"}
    for drone_id, extra in gained.items():
        assert extra > 0
        assert len(coordinator.lanes(drone_id)) == lanes_before[drone_id] + extra

    # The surviving edges accept the longer lane list.
    for drone_id in gained:
        assert _wait_for(
            lambda d=drone_id: coordinator._drones[d].ack_lanes_total == len(coordinator.lanes(d))
        ), drone_id
