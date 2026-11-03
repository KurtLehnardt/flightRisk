"""Tests for the edge wiring of per-lane obstacle sensing (``_build_obstacle_wiring``).

Everything runs against fakes: the fake controllers, ``FakeObstacleSensor`` and a
stub in place of ``ForwardCameraSensor``. MiDaS is never loaded.
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from flightrisk.drone import obstacle_camera
from flightrisk.drone.controller import Capability
from flightrisk.drone.fake import FakeDrone
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.obstacle_sensor import (
    ENV_FAKE_OBSTACLE_Y_CM,
    ENV_OBSTACLE_MIN_SAFE_DEPTH,
    ENV_OBSTACLE_PERSON_FRACTION,
    ENV_OBSTACLE_SENSING,
    ENV_OBSTACLE_STEP_CM,
    ENV_OBSTACLE_TURN,
    SENSING_FORWARD_ONLY,
    SENSING_OFF,
    SENSING_STEPPED,
    FakeObstacleSensor,
    LatestFrame,
    NullSensor,
)
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController
from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import (
    DroneFrameSource,
    _build_obstacle_wiring,
    _build_parser,
    run_edge_device,
)
from flightrisk.observability import StructuredLogger
from tests.test_edge_device import FakeSource, FakeTransport


class RecordingLog:
    """Collects (level, event, fields) triples."""

    def __init__(self):
        self.records: list[tuple[str, str, dict]] = []

    def _add(self, level):
        return lambda event, **fields: self.records.append((level, event, fields))

    def __getattr__(self, name):
        if name in ("info", "warning", "error", "debug"):
            return self._add(name)
        raise AttributeError(name)

    def events(self, level=None):
        return [e for lv, e, _ in self.records if level in (None, lv)]


class StubCameraSensor:
    """Stands in for ``ForwardCameraSensor``; records the constructor call."""

    instances: list["StubCameraSensor"] = []
    fail_with: Exception | None = None

    def __init__(self, frames, runner=None, **kwargs):
        if StubCameraSensor.fail_with is not None:
            raise StubCameraSensor.fail_with
        self.frames = frames
        self.runner = runner
        self.kwargs = kwargs
        StubCameraSensor.instances.append(self)

    def sense(self, after, timeout_s=2.0):  # pragma: no cover - not called here
        raise AssertionError("not used")

    def status(self):
        return {}


@pytest.fixture(autouse=True)
def stub_camera(monkeypatch):
    StubCameraSensor.instances = []
    StubCameraSensor.fail_with = None
    monkeypatch.setattr(obstacle_camera, "ForwardCameraSensor", StubCameraSensor)
    return StubCameraSensor


def _args(source: str, *argv: str):
    return _build_parser().parse_args(["--source", source, *argv])


def _mavlink_args():
    return _build_parser().parse_args(
        ["--drone", "mavlink", "--source", "camera", "--frame-heading-deg", "0"]
    )


def _wire(source="tello", environ=None, drone=None, runner="runner", log=None, args=None):
    log = log or RecordingLog()
    envelope = SafetyEnvelope.from_env(alt_slot_cm=100, environ={})
    if drone is None:
        drone = FakeTelloController(name="drone-1")
    args = args or _args(source)
    return _build_obstacle_wiring(args, log, drone, runner, envelope, environ or {}), log, drone, envelope


# -- capabilities ----------------------------------------------------------------


def test_fake_controllers_declare_stepped_sensing():
    assert FakeTelloController().capabilities.supports(Capability.STEPPED_SENSING)
    assert FakeDrone().capabilities.supports(Capability.STEPPED_SENSING)
    assert not FakeTelloController().capabilities.supports(Capability.ONBOARD_AVOIDANCE)
    assert not FakeDrone().capabilities.supports(Capability.ONBOARD_AVOIDANCE)


def test_tello_controller_declares_stepped_sensing(tello_on_wire):
    ctrl, _wire_ = tello_on_wire()
    assert ctrl.capabilities.supports(Capability.STEPPED_SENSING)
    assert not ctrl.capabilities.supports(Capability.ONBOARD_AVOIDANCE)
    assert ctrl.capabilities.supports(Capability.VIDEO)


# -- the Tello camera sensor -----------------------------------------------------


def test_tello_builds_a_camera_sensor_on_a_frame_holder():
    runner = object()
    wiring, log, drone, _ = _wire("tello", runner=runner)
    sensor = wiring.sensor
    assert isinstance(sensor, StubCameraSensor)
    assert sensor.runner is runner
    assert sensor.frames is wiring.holder
    assert sensor.kwargs["drone_id"] == "drone-1"
    assert wiring.sensor_error is None
    assert wiring.step_cm == 50
    assert wiring.turn is True
    kwargs = wiring.executor_kwargs()
    assert kwargs == {"obstacle_sensor": sensor, "obstacle_step_cm": 50, "obstacle_turn": True}


def test_tello_sensor_uses_the_monotonic_clock():
    import time

    wiring, *_ = _wire("tello")
    assert wiring.sensor.kwargs["clock"] is time.monotonic


def test_tello_threshold_knobs_reach_the_sensor():
    env = {ENV_OBSTACLE_MIN_SAFE_DEPTH: "0.5", ENV_OBSTACLE_PERSON_FRACTION: "0.7"}
    wiring, *_ = _wire("tello", environ=env)
    assert wiring.sensor.kwargs["min_safe_depth"] == 0.5
    assert wiring.sensor.kwargs["person_fraction"] == 0.7


def test_tello_sensor_failure_becomes_a_sensor_error(stub_camera):
    stub_camera.fail_with = RuntimeError("no torch")
    wiring, log, *_ = _wire("tello")
    assert wiring.sensor is None
    assert wiring.sensor_error == "RuntimeError: no torch"
    assert "edge_obstacle_sensor_unavailable" in log.events("error")
    assert wiring.executor_kwargs()["obstacle_sensor_error"] == "RuntimeError: no torch"
    assert "obstacle_sensor" not in wiring.executor_kwargs()


def test_a_drone_without_on_frame_is_a_sensor_error():
    wiring, *_ = _wire("tello", drone=object())
    assert wiring.sensor is None
    assert "on_frame" in wiring.sensor_error


def test_frame_holder_gets_a_copy_not_the_controllers_buffer():
    wiring, _log, drone, _ = _wire("tello")
    shared = np.zeros((4, 4, 3), np.uint8)
    drone._frame_callbacks[0](shared)
    shared[:] = 255
    stored = wiring.holder.latest().frame
    assert stored is not shared
    assert int(stored.max()) == 0


def test_holder_is_fed_by_the_drone_on_every_frame():
    wiring, _log, drone, _ = _wire("tello")
    drone.connect()
    for _ in range(5):
        drone.get_frame()
    assert wiring.holder.frames_seen == 5


@pytest.mark.parametrize("stride", [1, 2, 3])
def test_holder_sees_every_frame_whatever_the_stride(stride):
    wiring, _log, drone, _ = _wire("tello")
    drone.connect()
    stop = threading.Event()

    class Clock:
        t = 0.0

        def now(self):
            return self.t

        def wait(self, seconds):
            self.t += seconds
            return False

    clock = Clock()
    reads = {"n": 0}
    real_get_frame = drone.get_frame

    def counting():
        reads["n"] += 1
        if reads["n"] > 12:
            stop.set()
            return None
        return real_get_frame()

    drone.get_frame = counting
    source = DroneFrameSource(
        drone, stop, min_interval_s=1 / 30, frame_stride=stride, clock=clock.now, wait=clock.wait
    )
    yielded = sum(1 for _ in source)
    assert wiring.holder.frames_seen == 12
    if stride == 1:
        assert yielded == 12
    else:
        assert yielded < 12


# -- the fake drone --------------------------------------------------------------


def test_fake_drone_gets_a_fake_sensor_that_never_blocks_by_default():
    wiring, _log, _drone, envelope = _wire("fake")
    sensor = wiring.sensor
    assert isinstance(sensor, FakeObstacleSensor)
    assert sensor.blocked == []
    assert wiring.holder is None
    assert not StubCameraSensor.instances
    assert sensor.sense(after=0.0).clear


def test_fake_obstacle_y_blocks_from_that_plan_frame_y():
    wiring, log, _drone, envelope = _wire("fake", environ={ENV_FAKE_OBSTACLE_Y_CM: "250"})
    sensor = wiring.sensor
    assert len(sensor.blocked) == 1
    zone = sensor.blocked[0]
    assert zone.y_min == 250.0 and zone.heading_deg == 0.0
    assert sensor.sense(after=0.0).clear
    envelope.record_move("forward", 260)
    assert sensor.sense(after=0.0).blocked
    assert "edge_obstacle_fake" in log.events("info")


def test_fake_obstacle_y_must_be_a_number():
    with pytest.raises(SystemExit):
        _wire("fake", environ={ENV_FAKE_OBSTACLE_Y_CM: "far"})


# -- MAVLink ---------------------------------------------------------------------


def test_mavlink_builds_no_sensor_and_leaves_the_autopilot_to_preflight():
    wiring, log, *_ = _wire(args=_mavlink_args(), drone=object())
    assert wiring.sensor is None and wiring.sensor_error is None
    assert wiring.executor_kwargs() == {}
    assert not StubCameraSensor.instances


# -- the off override ------------------------------------------------------------


@pytest.mark.parametrize("source", ["tello", "fake"])
def test_off_override_gives_a_null_sensor_loudly(source):
    wiring, log, *_ = _wire(source, environ={ENV_OBSTACLE_SENSING: " OFF "})
    assert isinstance(wiring.sensor, NullSensor)
    assert "edge_obstacle_sensing_off" in log.events("warning")
    assert not StubCameraSensor.instances
    assert wiring.holder is None


def test_off_override_applies_to_mavlink_too():
    wiring, log, *_ = _wire(args=_mavlink_args(), drone=object(), environ={ENV_OBSTACLE_SENSING: "off"})
    assert isinstance(wiring.sensor, NullSensor)
    assert "edge_obstacle_sensing_off" in log.events("warning")


def test_off_override_skips_a_sensor_that_would_fail(stub_camera):
    stub_camera.fail_with = RuntimeError("boom")
    wiring, *_ = _wire("tello", environ={ENV_OBSTACLE_SENSING: "off"})
    assert isinstance(wiring.sensor, NullSensor)
    assert wiring.sensor_error is None


def test_explicit_required_is_the_default():
    wiring, *_ = _wire("tello", environ={ENV_OBSTACLE_SENSING: "required"})
    assert isinstance(wiring.sensor, StubCameraSensor)


@pytest.mark.parametrize("bad", ["on", "no", "auto"])
def test_unknown_sensing_value_refuses_to_start(bad):
    with pytest.raises(SystemExit):
        _wire("tello", environ={ENV_OBSTACLE_SENSING: bad})


# -- step and turn knobs ---------------------------------------------------------


def test_step_knob():
    wiring, *_ = _wire("fake", environ={ENV_OBSTACLE_STEP_CM: "30"})
    assert wiring.step_cm == 30
    assert wiring.executor_kwargs()["obstacle_step_cm"] == 30


@pytest.mark.parametrize("bad", ["19", "101", "x", "50.5", "-5"])
def test_step_knob_out_of_range_refuses_to_start(bad):
    with pytest.raises(SystemExit):
        _wire("fake", environ={ENV_OBSTACLE_STEP_CM: bad})


@pytest.mark.parametrize("bad", ["-0.1", "1.5", "nan", "deep"])
def test_threshold_knobs_out_of_range_refuse_to_start(bad):
    with pytest.raises(SystemExit):
        _wire("tello", environ={ENV_OBSTACLE_MIN_SAFE_DEPTH: bad})
    with pytest.raises(SystemExit):
        _wire("tello", environ={ENV_OBSTACLE_PERSON_FRACTION: bad})


def test_turn_kill_switch():
    wiring, *_ = _wire("fake", environ={ENV_OBSTACLE_TURN: "0"})
    assert wiring.turn is False
    assert wiring.executor_kwargs()["obstacle_turn"] is False


def test_turn_defaults_on():
    wiring, *_ = _wire("fake", environ={})
    assert wiring.turn is True


def test_bad_turn_value_refuses_to_start():
    with pytest.raises(SystemExit):
        _wire("fake", environ={ENV_OBSTACLE_TURN: "maybe"})


# -- into the executor -----------------------------------------------------------


def _executor(wiring, drone, envelope):
    return LaneExecutor(drone, envelope, "drone-1", send=lambda msg: None, **wiring.executor_kwargs())


def test_executor_steps_with_a_camera_sensor():
    wiring, _log, drone, envelope = _wire("tello")
    flight = _executor(wiring, drone, envelope)
    assert flight.obstacle_sensing == SENSING_STEPPED
    assert flight.obstacle_step_cm == 50


def test_executor_reports_forward_only_when_turning_is_off():
    wiring, _log, drone, envelope = _wire("fake", environ={ENV_OBSTACLE_TURN: "0"})
    assert _executor(wiring, drone, envelope).obstacle_sensing == SENSING_FORWARD_ONLY


def test_executor_reports_off_for_the_override():
    wiring, _log, drone, envelope = _wire("tello", environ={ENV_OBSTACLE_SENSING: "off"})
    assert _executor(wiring, drone, envelope).obstacle_sensing == SENSING_OFF


def test_executor_with_a_sensor_error_is_not_ok(stub_camera):
    stub_camera.fail_with = RuntimeError("weights missing")
    wiring, _log, drone, envelope = _wire("tello")
    flight = _executor(wiring, drone, envelope)
    assert flight.obstacle_sensing == SENSING_STEPPED
    assert flight.obstacle_status()["obstacle_sensor_ok"] is False


def test_executor_without_wiring_kwargs_flies_as_before():
    wiring, _log, drone, envelope = _wire(args=_mavlink_args(), drone=FakeTelloController(name="drone-1"))
    flight = _executor(wiring, drone, envelope)
    assert flight.obstacle_sensing is None


# -- the detection loop is unchanged ---------------------------------------------


def test_run_edge_device_still_sends_one_message_per_frame_with_a_sensor():
    wiring, _log, drone, envelope = _wire("fake")
    flight = _executor(wiring, drone, envelope)
    transport = FakeTransport()
    runner = EdgeRunner(detector=None)

    processed = run_edge_device(FakeSource(n=3), transport, runner, recv_timeout=0.0, flight=flight)

    assert processed == 3
    assert len(transport.sent) == 3
