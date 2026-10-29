"""scripts/field_fleet_ground.py: the headless field-scale ground, with an in-process ``--drone mavlink`` edge.

The edge flies a simulated autopilot (``tests.support.fake_mavsdk``); no SITL,
no hardware. Loopback only, on a throwaway port.
"""
from __future__ import annotations

import importlib.util
import secrets
import socket
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

from flightrisk import edge_device
from flightrisk.drone.autopilot import ARDUPILOT
from flightrisk.edge_device import (
    _build_parser,
    _resolve_drone_and_source,
    _startup_envelope,
)
from flightrisk.observability import StructuredLogger
from tests.support.fake_mavsdk import SimVehicle, install

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "field_fleet_ground.py"


def _load():
    spec = importlib.util.spec_from_file_location("field_fleet_ground", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_plan_only_prints_the_edge_settings(capsys):
    assert _load().main(["--drones", "a-1,a-2", "--area-m", "48x20", "--slots-m", "10,15", "--plan-only"]) == 0
    out = capsys.readouterr().out
    assert "--drone-id a-1 --alt-slot-m 10 --geofence-m" in out
    assert "--drone-id a-2 --alt-slot-m 15" in out


def test_unsafe_plan_is_refused(capsys):
    assert _load().main(["--drones", "a-1,a-2", "--slots-m", "10,12", "--plan-only"]) == 2
    assert "at least 5 m apart" in capsys.readouterr().err


class _Detector:
    def __init__(self, *args, **kwargs):
        pass

    def detect(self, frame):
        return []


class _Capture:
    def __init__(self, *args, **kwargs):
        pass

    def read(self):
        time.sleep(0.05)
        return True, np.zeros((120, 160, 3), np.uint8)

    def release(self):
        pass


def test_ground_script_flies_a_simulated_mavlink_edge(monkeypatch, capsys):
    import cv2

    for name, attr in (("flightrisk.vision.detector", "PersonDetector"), ("flightrisk.vision.reid", "PersonReID")):
        module = types.ModuleType(name)
        setattr(module, attr, _Detector)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(cv2, "VideoCapture", _Capture)
    monkeypatch.setattr(edge_device, "start_local_land_key", lambda *a, **k: None)
    vehicle = SimVehicle(ARDUPILOT, time_scale=20.0)
    vehicle.params["RTL_ALT"] = 0
    install(monkeypatch, vehicle)
    monkeypatch.setenv("FLIGHTRISK_EDGE_TOKEN", secrets.token_hex(16))
    port = _free_port()

    script = _load()
    result: dict = {}
    ground = threading.Thread(target=lambda: result.setdefault("rc", script.main([
        "--drones", "f-1", "--area-m", "24x20", "--slots-m", "10", "--port", str(port),
        "--connect-timeout-s", "30", "--flight-timeout-s", "60"])), daemon=True)
    ground.start()

    parser = _build_parser()
    args = parser.parse_args(["--drone", "mavlink", "--mavlink-address", "tcpout://127.0.0.1:1", "--drone-id", "f-1",
                              "--alt-slot-m", "10", "--geofence-m", "30", "--frame-heading-deg", "0",
                              "--ws-url", f"ws://127.0.0.1:{port}"])
    _resolve_drone_and_source(args, parser)
    stop = threading.Event()
    edge = threading.Thread(target=edge_device._run_flight_edge,
                            args=(args, StructuredLogger(component="test-field-ground"), stop,
                                  _startup_envelope(args, environ={})), daemon=True)
    edge.start()
    try:
        ground.join(90.0)
        assert not ground.is_alive()
        out = capsys.readouterr().out
        assert result.get("rc") == 0, out
        assert "all lanes flown" in out
        # One 24 m wide zone with ~10 m strips: forward 20 m, right one strip, back 20 m.
        assert vehicle.north == pytest.approx(0.0, abs=1.5)
        assert vehicle.east > 5.0
        assert ("land",) in vehicle.commands  # LAND ALL at the end
    finally:
        stop.set()
        edge.join(20.0)
