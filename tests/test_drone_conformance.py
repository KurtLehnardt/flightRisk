"""Run the reusable conformance checks (``tests/drone_conformance.py``) against every subject.

Subjects: the dashboard's simulated drone (``sim``), the edge's simulated
Tello (``fake``), the dashboard's MAVLink controller and the fleet edge's
MAVLink drone (both over the simulated MAVSDK in ``tests.support.fake_mavsdk``,
no SITL), and the copyable template backend. The real Tello controller is
covered over a fake wire by ``tests/test_drone_contract.py``.

To check a new backend, add a ``Subject`` to ``SUBJECTS`` (see
``docs/drone-interface.md``, "Add your drone in 5 steps").
"""
from __future__ import annotations

import importlib
import time

import numpy as np
import pytest

from flightrisk.drone.autopilot import ARDUPILOT
from flightrisk.drone.fake import FakeDrone
from flightrisk.drone.mavlink_fleet import MavlinkFleetDrone, MavlinkFleetOptions
from flightrisk.drone.mavsdk_link import MavsdkAutopilotLink
from flightrisk.drone.tello import FakeTelloController
from flightrisk.drone.template_backend import TemplateDrone
from tests.drone_conformance import CHECKS, Subject
from tests.support.fake_mavsdk import SimVehicle, fake_system_class, install


def _last(calls, name):
    for call in reversed(calls):
        if call[0] == name:
            return tuple(call[1:])
    return None


class _Velocity:
    def __init__(self, *args):
        self.args = args


class _Capture:
    def __init__(self, *args, **kwargs):
        pass

    def read(self):
        return True, np.zeros((48, 64, 3), np.uint8)

    def release(self):
        pass


def _mavlink_controller_subject(monkeypatch) -> Subject:
    mav = importlib.import_module("flightrisk.drone.mavlink")
    vehicle = SimVehicle(ARDUPILOT, time_scale=5.0)
    monkeypatch.setattr(mav, "HAS_MAVSDK", True)
    monkeypatch.setattr(mav, "System", fake_system_class(vehicle), raising=False)
    monkeypatch.setattr(mav, "VelocityBodyYawspeed", _Velocity, raising=False)
    monkeypatch.setattr(mav, "OffboardError", RuntimeError, raising=False)
    monkeypatch.setattr(mav.cv2, "VideoCapture", _Capture)

    def _rc_sent(ctrl):
        sent = _last(vehicle.commands, "manual_control")
        if sent is None:
            return None
        pitch, roll, thrust, yaw = sent
        return (roll * 100, pitch * 100, thrust * 200 - 100, yaw * 100)

    def _make():
        return mav.MavlinkController("conf-1", "udpin://0.0.0.0:14540")

    def _teardown(ctrl):
        if ctrl._loop is not None:
            ctrl.disconnect()

    return Subject("mavlink", _make, rc_sent=_rc_sent, teardown=_teardown, move_cm=50, slow_move_cm=200)


def _mavlink_fleet_subject(monkeypatch) -> Subject:
    vehicles = []

    def _make():
        vehicle = SimVehicle(ARDUPILOT, time_scale=5.0)
        vehicles.append(vehicle)
        install(monkeypatch, vehicle)
        drone = MavlinkFleetDrone("conf-1", MavsdkAutopilotLink("tcpout://127.0.0.1:1"),
                                  MavlinkFleetOptions(frame_heading_deg=0.0))
        drone.bind_slot_source(lambda: 1500.0)
        return drone

    def _ready(drone):
        deadline = time.monotonic() + 10.0
        while not drone.link_status().ok:
            assert time.monotonic() < deadline, drone.link_status()
            time.sleep(0.02)

    return Subject("mavlink-fleet", _make, ready=_ready, teardown=lambda d: d.disconnect(), move_cm=100,
                   slow_move_cm=3000)


SUBJECTS = {
    "sim": lambda mp: Subject("sim", lambda: FakeDrone(name="conf-1"),
                              rc_sent=lambda d: _last(d.calls, "rc_control")),
    "fake": lambda mp: Subject("fake", lambda: FakeTelloController(name="conf-1", speed_cm_s=100.0),
                               rc_sent=lambda d: _last(d.calls, "rc"), slow_move_cm=400),
    "mavlink": _mavlink_controller_subject,
    "mavlink-fleet": _mavlink_fleet_subject,
    "template": lambda mp: Subject("template", lambda: TemplateDrone("conf-1"),
                                   rc_sent=lambda d: _last(d.sent, "rc")),
}


@pytest.mark.parametrize("check", sorted(CHECKS))
@pytest.mark.parametrize("subject", sorted(SUBJECTS))
def test_conformance(subject, check, monkeypatch):
    CHECKS[check](SUBJECTS[subject](monkeypatch))


def test_template_is_not_registered():
    from flightrisk.drone import registry

    assert "template" not in registry.names()
