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

import dataclasses
import importlib
import time

import numpy as np
import pytest

from flightrisk.drone.autopilot import ARDUPILOT
from flightrisk.drone.controller import Capability, CommandOutcome, CommandResult
from flightrisk.drone.fake import FakeDrone
from flightrisk.drone.mavlink_fleet import MavlinkFleetDrone, MavlinkFleetOptions
from flightrisk.drone.mavsdk_link import MavsdkAutopilotLink
from flightrisk.drone.safety import RelativePose
from flightrisk.drone.tello import FakeTelloController
from flightrisk.drone.template_backend import TemplateDrone
from tests.drone_conformance import CHECKS, Subject, assert_outcome_allowed
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

    return Subject("mavlink", _make, rc_sent=_rc_sent, teardown=_teardown, move_cm=50, slow_move_cm=200,
                   land_sent=lambda ctrl: vehicle.commands.count(("land",)))


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
                   slow_move_cm=3000, land_sent=lambda d: vehicles[-1].commands.count(("land",)))


SUBJECTS = {
    "sim": lambda mp: Subject("sim", lambda: FakeDrone(name="conf-1"),
                              rc_sent=lambda d: _last(d.calls, "rc_control"),
                              land_sent=lambda d: sum(1 for c in d.calls if c[0] == "land")),
    "fake": lambda mp: Subject("fake", lambda: FakeTelloController(name="conf-1", speed_cm_s=100.0),
                               rc_sent=lambda d: _last(d.calls, "rc"), slow_move_cm=400,
                               land_sent=lambda d: sum(1 for c in d.calls if c[0] == "land")),
    "mavlink": _mavlink_controller_subject,
    "mavlink-fleet": _mavlink_fleet_subject,
    "template": lambda mp: Subject("template", lambda: TemplateDrone("conf-1"),
                                   rc_sent=lambda d: _last(d.sent, "rc"),
                                   land_sent=lambda d: sum(1 for c in d.sent if c[0] == "land")),
}


@pytest.mark.parametrize("check", sorted(CHECKS))
@pytest.mark.parametrize("subject", sorted(SUBJECTS))
def test_conformance(subject, check, monkeypatch):
    CHECKS[check](SUBJECTS[subject](monkeypatch))


def test_template_is_not_registered():
    from flightrisk.drone import registry

    assert "template" not in registry.names()


def test_the_land_hook_catches_a_land_that_sends_nothing():
    """A backend whose land() reports OK without sending anything fails the conformance check."""

    class SilentLand(TemplateDrone):
        def land(self):
            self.state.is_flying = False
            return CommandResult("land")

    subject = Subject("silent", lambda: SilentLand("conf-1"), land_sent=lambda d: sum(1 for c in d.sent if c[0] == "land"))
    with pytest.raises(AssertionError, match="sent no land"):
        CHECKS["land_idempotent"](subject)


# -- The obstacle-sensing capabilities --------------------------------------------------------------
#
# The built-in fakes may or may not declare the new capabilities (the edge wiring adds the declarations
# to them), so these tests set the declaration explicitly instead of relying on either state.


def _declaring(make, *, add=(), remove=()):
    """A factory for a controller whose declared capabilities are changed after construction."""

    def _make():
        ctrl = make()
        features = (set(ctrl.capabilities.features) | set(add)) - set(remove)
        ctrl.capabilities = dataclasses.replace(ctrl.capabilities, features=frozenset(features))
        return ctrl

    return _make


_SENSING = (Capability.STEPPED_SENSING, Capability.ONBOARD_AVOIDANCE)


def _fake_subject(make, name):
    return Subject(name, make, rc_sent=lambda d: _last(d.calls, "rc"),
                   land_sent=lambda d: sum(1 for c in d.calls if c[0] == "land"))


@pytest.mark.parametrize("declared", [False, True], ids=["undeclared", "declared"])
@pytest.mark.parametrize("kind", ["sim", "fake"])
def test_obstacle_rules_hold_for_the_fakes_declared_or_not(kind, declared):
    """A fake passes the sensing rules with STEPPED_SENSING declared and with it removed."""
    base = (lambda: FakeDrone(name="conf-1")) if kind == "sim" else (
        lambda: FakeTelloController(name="conf-1", speed_cm_s=100.0))
    make = _declaring(base, add=[Capability.STEPPED_SENSING]) if declared else _declaring(base, remove=_SENSING)
    subject = _fake_subject(make, f"{kind}-{declared}")
    for name in ("capabilities_honest", "obstacle_capabilities", "command_results"):
        CHECKS[name](subject)


def test_a_fake_with_onboard_avoidance_needs_the_methods_that_come_with_it():
    """Declaring ONBOARD_AVOIDANCE without avoidance_status() and measured_pose() fails the honesty check."""
    make = _declaring(lambda: FakeDrone(name="conf-1"), add=[Capability.ONBOARD_AVOIDANCE])
    with pytest.raises(AssertionError, match="declares Capability.ONBOARD_AVOIDANCE without avoidance_status"):
        CHECKS["capabilities_honest"](_fake_subject(make, "bare"))


def test_a_fake_with_the_avoidance_methods_passes():
    class Avoiding(FakeDrone):
        def avoidance_status(self):
            return []

        def measured_pose(self):
            return RelativePose(1.0, 2.0, 3.0)

    make = _declaring(lambda: Avoiding(name="conf-1"), add=[Capability.ONBOARD_AVOIDANCE])
    for name in ("capabilities_honest", "obstacle_capabilities"):
        CHECKS[name](_fake_subject(make, "avoiding"))


def test_a_declared_avoidance_that_lists_problems_fails():
    class Lying(FakeDrone):
        def avoidance_status(self):
            return ["OA_TYPE is 0"]

        def measured_pose(self):
            return None

    make = _declaring(lambda: Lying(name="conf-1"), add=[Capability.ONBOARD_AVOIDANCE])
    with pytest.raises(AssertionError, match="avoidance_status\\(\\) lists"):
        CHECKS["obstacle_capabilities"](_fake_subject(make, "lying"))


def test_an_undeclared_avoidance_status_must_list_a_problem():
    class Quiet(FakeDrone):
        def avoidance_status(self):
            return []

    make = _declaring(lambda: Quiet(name="conf-1"), remove=_SENSING)
    with pytest.raises(AssertionError, match="is empty but ONBOARD_AVOIDANCE is not declared"):
        CHECKS["obstacle_capabilities"](_fake_subject(make, "quiet"))


def test_a_measured_pose_of_the_wrong_type_fails():
    class Wrong(FakeDrone):
        def measured_pose(self):
            return (1.0, 2.0)

    with pytest.raises(AssertionError):
        CHECKS["obstacle_capabilities"](_fake_subject(lambda: Wrong(name="conf-1"), "wrong"))


def test_stepped_sensing_without_on_frame_fails():
    class NoFrames(FakeDrone):
        on_frame = None

    make = _declaring(lambda: NoFrames(name="conf-1"), add=[Capability.STEPPED_SENSING], remove=[Capability.VIDEO])
    with pytest.raises(AssertionError, match="declares Capability.STEPPED_SENSING without on_frame"):
        CHECKS["capabilities_honest"](_fake_subject(make, "noframes"))


def test_only_a_drone_with_onboard_avoidance_may_return_stopped_short():
    stopped = CommandResult("move", CommandOutcome.STOPPED_SHORT, "stopped short")
    subject = Subject("short", lambda: None)

    class Pose(FakeDrone):
        def measured_pose(self):
            return None

    plain = _declaring(lambda: Pose(name="conf-1"), remove=_SENSING)()
    with pytest.raises(AssertionError, match="STOPPED_SHORT without declaring ONBOARD_AVOIDANCE"):
        assert_outcome_allowed(subject, plain, stopped)

    declared = _declaring(lambda: Pose(name="conf-1"), add=[Capability.ONBOARD_AVOIDANCE])()
    assert_outcome_allowed(subject, declared, stopped)
    assert_outcome_allowed(subject, plain, CommandResult("move"))  # any other outcome is fine


def test_stopped_short_without_measured_pose_fails():
    stopped = CommandResult("move", CommandOutcome.STOPPED_SHORT)
    declared = _declaring(lambda: FakeDrone(name="conf-1"), add=[Capability.ONBOARD_AVOIDANCE])()
    del_pose = type("NoPose", (), {"capabilities": declared.capabilities})()
    with pytest.raises(AssertionError, match="without measured_pose"):
        assert_outcome_allowed(Subject("nopose", lambda: None), del_pose, stopped)


def test_a_move_that_returns_stopped_short_without_the_capability_fails_the_check():
    class Rude(FakeDrone):
        def move(self, direction, distance_cm):
            return CommandResult("move", CommandOutcome.STOPPED_SHORT, "nope")

    make = _declaring(lambda: Rude(name="conf-1"), remove=_SENSING)
    with pytest.raises(AssertionError, match="STOPPED_SHORT without declaring"):
        CHECKS["obstacle_capabilities"](_fake_subject(make, "rude"))


def test_the_fleet_drone_declares_onboard_avoidance_only_with_an_empty_problem_list(monkeypatch):
    """The MAVLink fleet subject: the capability and avoidance_status() agree once connected."""
    subject = SUBJECTS["mavlink-fleet"](monkeypatch)
    ctrl = subject.make()
    try:
        assert ctrl.connect() is True
        subject.ready(ctrl)
        declared = ctrl.capabilities.supports(Capability.ONBOARD_AVOIDANCE)
        assert declared == (ctrl.avoidance_status() == [])
    finally:
        subject.teardown(ctrl)
