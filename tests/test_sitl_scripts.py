"""Unit tests for scripts/sitl_fly_coverage.py, scripts/start_sitl.sh and docs/sitl-demo.md.

No SITL, network or real MAVSDK is used: the MAVSDK System is a fake.
"""

from __future__ import annotations

import argparse
import asyncio
import enum
import importlib.util
import subprocess
import sys
import threading
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

# Importing the sibling test module first installs its fake mavsdk tree, so the
# mavlink module binds the same fakes whichever test file is collected first.
import tests.test_mavlink  # noqa: F401  (imported for its side effect)
from flightrisk.drone import mavlink as mavlink_mod
from flightrisk.drone.geo import UTAH_DEMO_ORIGIN, haversine_m
from flightrisk.drone.mavlink import MavlinkController

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("sitl_fly_coverage", ROOT / "scripts" / "sitl_fly_coverage.py")
fly_script = importlib.util.module_from_spec(_spec)
sys.modules["sitl_fly_coverage"] = fly_script
_spec.loader.exec_module(fly_script)


def _args(*argv: str) -> argparse.Namespace:
    return fly_script.build_parser().parse_args(list(argv))


class _ControlMode(enum.Enum):
    PRIMARY = 1


class _GimbalMode(enum.Enum):
    YAW_FOLLOW = 0


class _SendMode(enum.Enum):
    ONCE = 0


class FakeSystem:
    """MAVSDK System stand-in whose position follows the last goto_location target."""

    def __init__(self, state, ground_amsl: float = 1600.0):
        self.state = state
        self.ground_amsl = ground_amsl
        self.last = (40.0, -111.0, ground_amsl)
        self.gotos: list[tuple[float, float, float]] = []
        self.action = MagicMock()
        self.action.set_takeoff_altitude = AsyncMock()
        self.action.set_current_speed = AsyncMock()
        self.action.arm = AsyncMock()
        self.action.land = AsyncMock()
        self.action.takeoff = AsyncMock(side_effect=self._takeoff)
        self.action.goto_location = AsyncMock(side_effect=self._goto)
        self.gimbal = MagicMock()
        self.gimbal.take_control = AsyncMock()
        self.gimbal.set_angles = AsyncMock()
        self.telemetry = MagicMock()
        self.telemetry.position = self._position

    def _publish(self):
        self.state.latitude, self.state.longitude, self.state.altitude_msl = self.last

    async def _takeoff(self):
        self.last = (self.last[0], self.last[1], self.ground_amsl + self.takeoff_alt)
        self._publish()

    takeoff_alt = 0.0

    async def _goto(self, lat, lon, alt, yaw):
        self.gotos.append((lat, lon, alt))
        self.last = (lat, lon, alt)
        self._publish()

    async def _position(self):
        while True:
            yield types.SimpleNamespace(
                latitude_deg=self.last[0], longitude_deg=self.last[1], absolute_altitude_m=self.last[2]
            )
            await asyncio.sleep(0.01)


@pytest.fixture
def fake_gimbal_module(monkeypatch):
    module = types.ModuleType("mavsdk.gimbal")
    module.ControlMode = _ControlMode
    module.GimbalMode = _GimbalMode
    module.SendMode = _SendMode
    monkeypatch.setitem(sys.modules, "mavsdk.gimbal", module)


@pytest.fixture
def flying_controller(monkeypatch):
    monkeypatch.setattr(mavlink_mod, "HAS_MAVSDK", True)
    ctrl = MavlinkController(name="fly-test", host="udp://:14540")
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    ctrl._loop = loop
    ctrl._loop_thread = thread
    ctrl._running = True
    system = FakeSystem(ctrl.state)

    async def set_alt(alt):
        system.takeoff_alt = alt

    system.action.set_takeoff_altitude = AsyncMock(side_effect=set_alt)
    ctrl._system = system
    ctrl.state.latitude, ctrl.state.longitude = 40.0, -111.0
    ctrl.state.altitude_msl = system.ground_amsl
    ctrl.state.heading = 0
    yield ctrl, system
    ctrl._running = False
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2.0)
    loop.close()
    ctrl._loop = None


def test_parse_origin_and_area():
    assert fly_script.parse_origin("40.6894,-111.7310") == (40.6894, -111.7310)
    assert fly_script.parse_area("300x200") == (300.0, 200.0)
    for bad in ("40.6894", "a,b", "95,0", "40,-200"):
        with pytest.raises(argparse.ArgumentTypeError):
            fly_script.parse_origin(bad)
    for bad in ("300", "0x10", "-3x4", "axb"):
        with pytest.raises(argparse.ArgumentTypeError):
            fly_script.parse_area(bad)


def test_default_args_use_design_demo_origin():
    args = _args()
    assert args.origin == UTAH_DEMO_ORIGIN
    assert args.preset == "OPEN_QUAD_4K"
    assert args.area == (300.0, 300.0)


def test_sitl_address_uses_port_stride():
    assert fly_script.sitl_address(0) == "tcpout://127.0.0.1:5760"
    assert fly_script.sitl_address(2) == "tcpout://127.0.0.1:5780"


def test_build_plan_uses_planned_altitude_and_lanes():
    plan = fly_script.build_plan(_args("--min-px", "24", "--area", "300x300"))
    cov = plan.coverage
    assert cov.alt_m == pytest.approx(120.0)
    assert cov.speed_mps == pytest.approx(12.0)
    assert plan.pitch_deg == -90.0
    assert len(plan.waypoints) == 2 * (int(-(-300 // cov.lane_spacing_m)) + 1)
    assert all(w.alt_m == pytest.approx(cov.alt_m) for w in plan.waypoints)
    assert haversine_m(plan.waypoints[0].lat, plan.waypoints[0].lon, *UTAH_DEMO_ORIGIN) < 1.0


def test_build_plan_max_lanes_and_bad_preset():
    plan = fly_script.build_plan(_args("--max-lanes", "2"))
    assert len(plan.waypoints) == 4
    with pytest.raises(ValueError):
        fly_script.build_plan(_args("--preset", "nope"))
    with pytest.raises(ValueError):
        fly_script.build_plan(_args("--preset", "Ground team"))


def test_dry_run_prints_plan_without_connecting(capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not connect on dry run")

    monkeypatch.setattr(mavlink_mod, "MavlinkController", boom)
    assert fly_script.main(["--dry-run", "--max-lanes", "1"]) == 0
    out = capsys.readouterr().out
    assert "Plan:" in out and "1:" in out and "2:" in out


def test_fly_script_plans_before_connect(monkeypatch, capsys):
    events: list[str] = []
    real_build = fly_script.build_plan

    def spy_plan(args):
        events.append("plan")
        return real_build(args)

    class FakeController:
        def __init__(self, name, host=None, rtsp_url=None):
            events.append(f"init {host}")

        def connect(self):
            events.append("connect")
            return False

    monkeypatch.setattr(fly_script, "build_plan", spy_plan)
    monkeypatch.setattr(mavlink_mod, "MavlinkController", FakeController)
    assert fly_script.main(["--instance", "1", "--strict-health"]) == 1
    assert events == ["plan", "init tcpout://127.0.0.1:5770", "connect"]


def test_bad_plan_does_not_connect(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("must not connect when planning fails")

    monkeypatch.setattr(mavlink_mod, "MavlinkController", boom)
    assert fly_script.main(["--preset", "nope"]) == 2


def test_fly_calls_goto_location_once_per_waypoint(flying_controller, fake_gimbal_module):
    ctrl, system = flying_controller
    plan = fly_script.build_plan(_args("--area", "300x300"))
    lines: list[str] = []

    fly_script.fly(ctrl, plan, out=lines.append, climb_timeout=5.0, leg_timeout=5.0)

    assert system.action.goto_location.await_count == len(plan.waypoints)
    # Altitudes sent to the autopilot are AMSL: ground plus the planned height.
    assert {round(a, 3) for _, _, a in system.gotos} == {round(1600.0 + plan.coverage.alt_m, 3)}
    system.action.set_takeoff_altitude.assert_awaited_once_with(pytest.approx(plan.coverage.alt_m))
    system.action.set_current_speed.assert_awaited_once_with(pytest.approx(plan.coverage.speed_mps))
    system.action.land.assert_awaited_once()
    system.gimbal.set_angles.assert_awaited_once_with(
        0, 0.0, -90.0, 0.0, _GimbalMode.YAW_FOLLOW, _SendMode.ONCE
    )
    assert sum(1 for line in lines if line.startswith("Waypoint")) == len(plan.waypoints)


def test_fly_continues_when_gimbal_fails(flying_controller, fake_gimbal_module):
    ctrl, system = flying_controller
    system.gimbal.take_control = AsyncMock(side_effect=RuntimeError("no gimbal"))
    plan = fly_script.build_plan(_args("--max-lanes", "1"))
    lines: list[str] = []

    fly_script.fly(ctrl, plan, out=lines.append, climb_timeout=5.0, leg_timeout=5.0)

    assert system.action.goto_location.await_count == 2
    assert any("Gimbal command failed" in line for line in lines)


def test_fly_geolocates_replay_detections(flying_controller, fake_gimbal_module):
    ctrl, _ = flying_controller
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    ctrl.get_frame = lambda: frame

    class Detector:
        def detect(self, f):
            # Box bottom centre at the image centre: straight below a nadir camera.
            return [{"bbox": [90, 20, 110, 50], "confidence": 0.9}]

    plan = fly_script.build_plan(_args("--max-lanes", "1"))
    found = fly_script.fly(ctrl, plan, detector=Detector(), out=lambda s: None, climb_timeout=5.0, leg_timeout=5.0)

    assert len(found) == 2
    for lat, lon, conf in found:
        assert conf == 0.9
        assert haversine_m(lat, lon, *UTAH_DEMO_ORIGIN) < 1.0 or haversine_m(lat, lon, plan.waypoints[1].lat, plan.waypoints[1].lon) < 1.0


def test_geolocate_detections_nadir_hits_drone_position():
    cam = fly_script.coverage.OPEN_QUAD_4K
    hits = fly_script.geolocate_detections(
        [{"bbox": [90, 40, 110, 50], "confidence": 0.8}], (100, 200), cam, 40.0, -111.0, 60.0, 0.0, -90.0
    )
    assert len(hits) == 1
    assert haversine_m(hits[0][0], hits[0][1], 40.0, -111.0) < 1.0


def test_geolocate_detections_above_horizon_dropped():
    cam = fly_script.coverage.OPEN_QUAD_4K
    hits = fly_script.geolocate_detections(
        [{"bbox": [90, 40, 110, 50], "confidence": 0.8}], (100, 200), cam, 40.0, -111.0, 60.0, 0.0, 30.0
    )
    assert hits == []


def test_start_sitl_script_syntax_and_argument_errors():
    script = str(ROOT / "scripts" / "start_sitl.sh")
    assert subprocess.run(["bash", "-n", script]).returncode == 0
    bad = subprocess.run(["bash", script, "--instance", "abc"], capture_output=True, text=True)
    assert bad.returncode == 2
    unknown = subprocess.run(["bash", script, "--nope"], capture_output=True, text=True)
    assert unknown.returncode == 2
    helped = subprocess.run(["bash", script, "--help"], capture_output=True, text=True)
    assert helped.returncode == 0 and "--instance" in helped.stdout


def test_start_sitl_pins_image_digest():
    text = (ROOT / "scripts" / "start_sitl.sh").read_text()
    assert "@sha256:" in text
    assert ":latest" not in text.split("IMAGE=")[1].splitlines()[0]


def test_sitl_doc_sections():
    text = (ROOT / "docs" / "sitl-demo.md").read_text()
    for needle in ("start_sitl.sh", "--instance", "amd64", "Real hardware"):
        assert needle in text, needle


def test_sitl_controller_overrides_connect_with_long_timeout(monkeypatch):
    monkeypatch.setattr(mavlink_mod, "HAS_MAVSDK", True)
    cls = fly_script._make_sitl_controller_class()
    assert issubclass(cls, MavlinkController)
    assert cls.connect is not MavlinkController.connect
    assert cls.connect_timeout_s > 30.0
    ctrl = cls("sitl-test", host=fly_script.sitl_address(0))
    assert ctrl.host == "tcpout://127.0.0.1:5760"
