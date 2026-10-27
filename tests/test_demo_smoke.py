"""Integration checks for the demo launchers, the README section and a two-drone fleet run.

The end-to-end test drives the real ``FleetCoordinator`` with two in-process fake
edges. Each fake edge runs a clock far from the ground clock (one ahead, one
behind), so a sign error in the offset shows up as seconds, not milliseconds.
No network, drone or subprocess is involved in that test.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

from flightrisk.drone.coordinator import FleetCoordinator

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


# ---------------------------------------------------------------------------
# R-032: launchers
# ---------------------------------------------------------------------------

def _text(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_launcher_flags():
    ground = _text("demo_ground.sh")
    for needle in (
        "--source edge",
        '--edge-host "$FLIGHTRISK_EDGE_HOST"',
        "--no-llm",
        "FLIGHTRISK_APPEARANCE_ONLY=1",
        "FLIGHTRISK_SAVE_CAPTURES=0",
        "scripts/fleet_env.sh",
        "FLIGHTRISK_EDGE_TOKEN",
    ):
        assert needle in ground, needle
    assert "--host" not in ground.replace("--edge-host", ""), "dashboard must stay on its loopback default"

    edge_sh = _text("demo_edge.sh")
    edge_ps1 = _text("demo_edge.ps1")
    for name, text in (("demo_edge.sh", edge_sh), ("demo_edge.ps1", edge_ps1)):
        for needle in (
            "flightrisk.edge_device",
            "--source",
            "tello",
            "--tello-host",
            "FLIGHTRISK_TELLO_HOST",
            "--drone-id",
            "DRONE_ID",
            "--alt-slot",
            "ALT_CM",
            "--ws-url",
            "FLIGHTRISK_EDGE_WS",
            "FLIGHTRISK_EDGE_TOKEN",
            "fleet_env",
        ):
            assert needle in text, (name, needle)

    for name in ("fleet_env.sh", "fleet_env.ps1"):
        text = _text(name)
        assert "FLIGHTRISK_FLEET_NET" in text
        assert "station" in text and "ap" in text, name
        assert "10.42.0.1" in text and "192.168.10.1" in text, name


def test_launcher_syntax():
    result = subprocess.run(
        ["bash", "-n", "scripts/demo_ground.sh", "scripts/demo_edge.sh", "scripts/fleet_env.sh"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def _run(script: str, env_extra: dict[str, str], drop: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FLIGHTRISK_", "DEMO_")) and k not in ("DRONE_ID", "ALT_CM")}
    for key in drop:
        env.pop(key, None)
    env.update(env_extra)
    return subprocess.run(["bash", f"scripts/{script}"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)


AP = {"FLIGHTRISK_FLEET_NET": "ap", "FLIGHTRISK_EDGE_TOKEN": "test-token", "DEMO_DRY_RUN": "1"}


@pytest.mark.parametrize("script", ["demo_ground.sh", "demo_edge.sh"])
def test_launchers_refuse_without_token(script):
    result = _run(script, {"FLIGHTRISK_FLEET_NET": "ap"})
    assert result.returncode != 0
    assert "FLIGHTRISK_EDGE_TOKEN" in result.stderr
    assert "export FLIGHTRISK_EDGE_TOKEN=" in result.stderr


@pytest.mark.parametrize("script", ["demo_ground.sh", "demo_edge.sh"])
def test_launchers_refuse_without_fleet_net(script):
    result = _run(script, {"FLIGHTRISK_EDGE_TOKEN": "test-token"})
    assert result.returncode != 0
    assert "FLIGHTRISK_FLEET_NET" in result.stderr


def test_ground_command_line():
    result = _run("demo_ground.sh", AP)
    assert result.returncode == 0, result.stderr
    command = result.stdout.strip().splitlines()[-1]
    assert command.endswith("-m flightrisk.dashboard --source edge --edge-host 127.0.0.1,10.42.0.1 --fleet drone-1,drone-2 --no-llm")


def test_edge_ids_match_ground_fleet_list():
    ground = _run("demo_ground.sh", AP).stdout.strip().splitlines()[-1]
    fleet = ground.split("--fleet ")[1].split()[0].split(",")
    seen = []
    for drone_number, alt in (("1", "100"), ("2", "160")):
        result = _run("demo_edge.sh", {**AP, "DRONE_ID": drone_number})
        assert result.returncode == 0, result.stderr
        command = result.stdout.strip().splitlines()[-1]
        drone_id = re.search(r"--drone-id (\S+)", command).group(1)
        assert drone_id in fleet
        assert f"--alt-slot {alt}" in command
        assert "--source tello --tello-host 192.168.10.1" in command
        assert "--ws-url ws://10.42.0.1:9000" in command  # FLIGHTRISK_GROUND_LOCAL unset: cable address
        seen.append(drone_id)
    assert seen == fleet


def test_edge_launcher_ground_local_uses_loopback_only_in_ap_mode():
    result = _run("demo_edge.sh", {**AP, "DRONE_ID": "1", "FLIGHTRISK_GROUND_LOCAL": "1"})
    assert result.returncode == 0, result.stderr
    command = result.stdout.strip().splitlines()[-1]
    assert "--ws-url ws://127.0.0.1:9000" in command
    assert "--tello-host 192.168.10.1" in command
    ground = _run("demo_ground.sh", {**AP, "FLIGHTRISK_GROUND_LOCAL": "1"}).stdout.strip().splitlines()[-1]
    assert "--edge-host 127.0.0.1,10.42.0.1" in ground
    station = {**AP, "FLIGHTRISK_FLEET_NET": "station", "FLIGHTRISK_GROUND_IP": "192.168.1.20",
               "FLIGHTRISK_TELLO_HOST": "192.168.1.50", "FLIGHTRISK_GROUND_LOCAL": "1"}
    edge = _run("demo_edge.sh", {**station, "DRONE_ID": "1"}).stdout.strip().splitlines()[-1]
    assert "--ws-url ws://192.168.1.20:9000" in edge


def test_edge_launcher_overrides_and_rejects_bad_input():
    result = _run("demo_edge.sh", {**AP, "DRONE_ID": "drone-2", "ALT_CM": "130"})
    assert result.returncode == 0, result.stderr
    assert "--drone-id drone-2 --alt-slot 130" in result.stdout

    for bad in ({"DRONE_ID": "two"}, {"DRONE_ID": "1", "ALT_CM": "1.6"}, {"DRONE_ID": "1", "ALT_CM": "5"}):
        result = _run("demo_edge.sh", {**AP, **bad})
        assert result.returncode != 0, bad


def test_station_mode_uses_discovered_tello_host():
    env = {**AP, "FLIGHTRISK_FLEET_NET": "station", "FLIGHTRISK_GROUND_IP": "192.168.1.20", "FLIGHTRISK_TELLO_HOST": "192.168.1.50"}
    ground = _run("demo_ground.sh", env).stdout.strip().splitlines()[-1]
    assert "--edge-host 192.168.1.20" in ground
    edge = _run("demo_edge.sh", {**env, "DRONE_ID": "1"}).stdout.strip().splitlines()[-1]
    assert "--tello-host 192.168.1.50" in edge
    assert "--ws-url ws://192.168.1.20:9000" in edge


# ---------------------------------------------------------------------------
# R-033: README
# ---------------------------------------------------------------------------

def test_readme_hackathon_section():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Hackathon demo (Nucleus bounty)" in text
    section = text.split("## Hackathon demo (Nucleus bounty)", 1)[1]
    section = re.split(r"\n## ", section, maxsplit=1)[0]
    assert "798f21a" in section
    assert "hackathon/kickoff" in section
    assert "GPL-3.0-or-later" in section
    for doc in (
        "built-this-week", "pitch", "one-page-plan", "demo-script", "roadmap-budget",
        "privacy-design", "fleet-setup", "altitude-benchmark", "licensing",
    ):
        assert f"docs/{doc}.md" in section, doc
    # Weights are not in git, and the README must say how to reproduce them.
    assert "models/heridal_yolo11s.pt" in section
    assert "scripts/finetune_heridal.py" in section
    assert "not in git" in section
    for needle in ("station", "AP mode", "Offline", "127.0.0.1", "FLIGHTRISK_EDGE_TOKEN"):
        assert needle in section, needle
    for doc in ("pitch", "one-page-plan", "demo-script", "roadmap-budget", "privacy-design",
                "fleet-setup", "altitude-benchmark", "licensing"):
        assert (ROOT / "docs" / f"{doc}.md").exists(), doc


# ---------------------------------------------------------------------------
# R-034: two fake edges, real coordinator
# ---------------------------------------------------------------------------

class FakeEdge:
    """In-process EdgeLink with its own clock, ``monotonic() + true_offset_s``.

    Replies are delivered synchronously from inside ``send`` on loopback.
    ``start_at`` fires on a timer at the edge's local time ``t_ground_s + offset``,
    using the offset the ground station sent, after ``fire_delay_s`` of jitter.
    """

    def __init__(self, drone_id: str, true_offset_s: float, fire_delay_s: float, battery: int = 80):
        self.drone_id = drone_id
        self.true_offset_s = true_offset_s
        self.fire_delay_s = fire_delay_s
        self.battery = battery
        self.on_message = None
        self.sent: list[dict] = []
        self.received_offset_s: float | None = None
        self.assigned_lanes: list[dict] = []
        self.fired_ground_s: float | None = None
        self.fired = threading.Event()
        self._timers: list[threading.Timer] = []

    def local_now(self) -> float:
        return time.monotonic() + self.true_offset_s

    def _reply(self, msg: dict) -> None:
        msg = {"drone_id": self.drone_id, **msg}
        assert self.on_message is not None
        self.on_message(msg)

    def send(self, msg: dict) -> None:
        self.sent.append(msg)
        kind = msg["type"]
        if kind == "ping":
            t2 = self.local_now()
            self._reply({"type": "pong", "t1": msg["t1"], "t2": t2, "t3": t2})
        elif kind == "clock":
            self.received_offset_s = msg["offset_s"]
        elif kind == "assign_lanes":
            self.assigned_lanes = list(msg["lanes"])
            self._reply({"type": "ack", "for": "assign_lanes", "lanes_total": len(msg["lanes"]), "battery": self.battery})
        elif kind == "start_at":
            assert self.received_offset_s is not None, "start_at before clock"
            local_target = msg["t_ground_s"] + self.received_offset_s
            wait = max(0.0, local_target - self.local_now()) + self.fire_delay_s
            timer = threading.Timer(wait, self._fire)
            timer.daemon = True
            self._timers.append(timer)
            timer.start()

    def _fire(self) -> None:
        local_fire = self.local_now()
        # The true ground time of the firing, using the real offset (not the estimate).
        self.fired_ground_s = local_fire - self.true_offset_s
        self.fired.set()
        executed_at_ground_s = local_fire - self.received_offset_s
        self._reply({"type": "ack", "for": "start_at", "executed_at_ground_s": executed_at_ground_s})

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent]

    def close(self) -> None:
        for timer in self._timers:
            timer.cancel()


def test_two_fake_edges_end_to_end():
    started = time.monotonic()
    events: list[tuple[str, dict]] = []
    fake1 = FakeEdge("drone-1", true_offset_s=1234.5, fire_delay_s=0.010)
    fake2 = FakeEdge("drone-2", true_offset_s=-987.25, fire_delay_s=0.030)
    coordinator = FleetCoordinator(
        {"drone-1": fake1, "drone-2": fake2},
        area_cm=(400, 400),
        strip_cm=75,
        clock=time.monotonic,
        on_event=lambda name, payload: events.append((name, payload)),
        retask_wait_s=2.0,
    )
    try:
        # Plan: one zone per drone, different altitude slots, lanes delivered and acked.
        plan = coordinator.plan()
        assert [z.drone_id for z in plan.zones] == ["drone-1", "drone-2"]
        assert plan.zones[0].alt_cm != plan.zones[1].alt_cm
        original_lanes = {d: list(coordinator.lanes(d)) for d in ("drone-1", "drone-2")}
        assert original_lanes["drone-1"] and original_lanes["drone-2"]
        assert fake1.assigned_lanes == original_lanes["drone-1"]
        assert fake2.assigned_lanes == original_lanes["drone-2"]
        assert all(entry["acked"] for entry in coordinator.status().values())

        # Sync: the estimate is the edge clock minus the ground clock, within 5 ms.
        results = coordinator.sync()
        for fake in (fake1, fake2):
            sync = results[fake.drone_id]
            assert sync.synced and sync.samples > 0
            assert sync.offset_s == pytest.approx(fake.true_offset_s, abs=0.005)
            assert fake.received_offset_s == pytest.approx(fake.true_offset_s, abs=0.005)

        # Start: both fire at the scheduled ground time and report it in ground time.
        outcome = coordinator.start(lead_s=0.4)
        assert outcome.ok, outcome.refusals
        assert fake1.fired.wait(5.0) and fake2.fired.wait(5.0)
        for fake in (fake1, fake2):
            assert fake.fired_ground_s == pytest.approx(outcome.t_ground_s + fake.fire_delay_s, abs=0.5)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and any(
            entry["skew_ms"] is None for entry in coordinator.status().values()
        ):
            time.sleep(0.01)
        skew = coordinator.skew_ms()
        assert skew is not None and skew < 100.0

        # Detections of one person from both drones merge into one map pin.
        spot_x = plan.zones[1].x_cm + 40.0
        spot_y = 150.0
        for fake, zone, jitter in ((fake1, plan.zones[0], 0.0), (fake2, plan.zones[1], 15.0)):
            fake._reply({
                "type": "detections",
                "rel_x_cm": spot_x - zone.x_cm + jitter,
                "rel_y_cm": spot_y - zone.y_cm,
            })
        # Each drone reports relative to its own takeoff point; dedup works on the shared grid.
        assert len(coordinator.detections()) == 1
        merged = coordinator.detections()[0]
        assert set(merged.drone_ids) == {"drone-1", "drone-2"} and merged.count == 2
        fake2._reply({"type": "detections", "rel_x_cm": 100.0, "rel_y_cm": 300.0})
        assert len(coordinator.detections()) == 2
        assert [name for name, _ in events].count("fleet_detection") == 2

        # Drone-2 drops: its remaining lanes move to drone-1, which acks the longer plan.
        fake2._reply({"type": "telemetry", "battery": 75, "is_flying": True, "lane_index": 1,
                      "lanes_total": len(original_lanes["drone-2"]), "rel_x_cm": 0, "rel_y_cm": 0})
        appended = coordinator.on_drone_lost("drone-2")
        assert "drone-1" in appended and appended["drone-1"]
        retasked = coordinator.lanes("drone-1")
        assert len(retasked) > len(original_lanes["drone-1"])
        assert retasked[: len(original_lanes["drone-1"])] == original_lanes["drone-1"]
        assert fake1.assigned_lanes == retasked
        assert "land" in fake2.types()
        assert any(n == "fleet_event" and p.get("event") == "drone_lost" for n, p in events)
        assert not any(n == "fleet_event" and p.get("event") == "retask_failed" for n, p in events)

        # Land all reaches both drones, including the lost one.
        landed = coordinator.land_all()
        assert landed == {"drone-1": None, "drone-2": None}
        assert "emergency_land_all" in fake1.types()
        assert "emergency_land_all" in fake2.types()
    finally:
        fake1.close()
        fake2.close()
    assert time.monotonic() - started < 20.0
