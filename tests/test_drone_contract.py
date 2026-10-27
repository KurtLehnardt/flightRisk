"""Contract tests every registered drone backend must pass.

Parametrized over ``flightrisk.drone.registry.names()``, so a newly
registered backend is tested automatically -- and fails
``test_every_backend_has_a_harness`` until it gets a harness below that
mocks its hardware. Controllers are always built through the registry, the
same way the dashboard builds them.

Hardware is mocked: djitellopy's ``Tello`` for the Tello backend, a fake
MAVSDK ``System`` (async telemetry streams) and ``cv2.VideoCapture`` for
MAVLink.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib
import inspect
import time
from dataclasses import dataclass, field
from typing import Callable
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from flightrisk.drone import registry
from flightrisk.drone.controller import (
    BASIC_FLIGHT,
    Capability,
    CommandOutcome,
    CommandResult,
    DroneCapabilities,
    DroneController,
    FlightControl,
    Lifecycle,
    Motion,
    TelemetrySource,
    VideoSource,
)
from flightrisk.drone.snapshot import (
    IS_FLYING_COMMANDED,
    IS_FLYING_SENSED,
    READING_UNITS,
    Reading,
    TelemetrySnapshot,
    legacy_telemetry_payload,
)

ALL_READINGS = frozenset(READING_UNITS)


# ---------------------------------------------------------------------------
# Per-backend harnesses
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    #: Install hardware mocks (monkeypatch) -- called before the factory runs.
    install: Callable[[pytest.MonkeyPatch], None]
    #: Make the backend deliver one round of telemetry after connect().
    refresh: Callable[[object], None]
    #: Readings this backend cannot provide: must stay None.
    unsupported: frozenset[str]
    #: Context manager that makes the command channel busy, or None if the
    #: backend queues (blocks) instead of dropping commands.
    busy: Callable[[object], contextlib.AbstractContextManager] | None = None
    host: str = "test-host"
    teardown: Callable[[object], None] = field(default=lambda ctrl: None)


# --- Tello ------------------------------------------------------------------


def _install_tello(monkeypatch):
    tello_mod = importlib.import_module("flightrisk.drone.tello")
    fake = MagicMock(name="djitellopy.Tello")
    fake.get_battery.return_value = 87
    fake.get_height.return_value = 120
    fake.get_temperature.return_value = 45.5
    fake.get_flight_time.return_value = 30
    fake.get_speed_x.return_value = 5
    fake.get_speed_y.return_value = 0
    fake.get_speed_z.return_value = -2
    fake.get_distance_tof.return_value = 130
    fake.get_barometer.return_value = 15736.0
    fake.get_pitch.return_value = 1
    fake.get_roll.return_value = -2
    fake.get_yaw.return_value = 90
    fake.get_frame_read.return_value.frame = np.zeros((48, 64, 3), dtype=np.uint8)
    monkeypatch.setattr(tello_mod, "Tello", lambda host=None: fake)
    # No keepalive/state threads in tests; telemetry is refreshed by calling
    # one poll explicitly.
    monkeypatch.setattr(tello_mod.TelloController, "_start_keepalive", lambda self: None)
    monkeypatch.setattr(tello_mod.TelloController, "_start_state_polling", lambda self: None)


@contextlib.contextmanager
def _tello_busy(ctrl):
    ctrl._cmd_lock.acquire()
    try:
        yield
    finally:
        ctrl._cmd_lock.release()


TELLO = Harness(
    install=_install_tello,
    refresh=lambda ctrl: ctrl._poll_once(),
    unsupported=frozenset({"latitude_deg", "longitude_deg", "altitude_msl_m", "heading_deg"}),
    busy=_tello_busy,
    host="192.168.10.1",
)


# --- MAVLink ----------------------------------------------------------------


def _agen(*values):
    async def _gen():
        for v in values:
            yield v
        await asyncio.Event().wait()  # keep the subscription alive

    return _gen()


class _Item:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __str__(self):
        return str(self.__dict__.get("mode", "item"))


class _FakeMavSystem:
    def __init__(self):
        self.connect = AsyncMock()
        self.action = MagicMock()
        for name in ("arm", "takeoff", "land", "goto_location"):
            setattr(self.action, name, AsyncMock())
        self.offboard = MagicMock()
        for name in ("set_velocity_body", "start", "stop"):
            setattr(self.offboard, name, AsyncMock())
        self.manual_control = MagicMock()
        self.manual_control.set_manual_control_input = AsyncMock()
        fix = _Item(value=4)
        self.telemetry = MagicMock()
        self.telemetry.health = lambda: _agen(_Item(is_armable=True, is_global_position_ok=True))
        self.telemetry.position = lambda: _agen(
            _Item(latitude_deg=37.0, longitude_deg=-122.0, absolute_altitude_m=50.0)
        )
        self.telemetry.heading = lambda: _agen(_Item(heading_deg=90.0))
        self.telemetry.battery = lambda: _agen(_Item(remaining_percent=0.87))
        self.telemetry.flight_mode = lambda: _agen(_Item(mode="HOLD"))
        self.telemetry.gps_info = lambda: _agen(_Item(fix_type=fix))


class _FakeVelocity:
    def __init__(self, *args):
        self.args = args


class _FakeOffboardError(Exception):
    pass


class _FakeCapture:
    def __init__(self, *args, **kwargs):
        pass

    def read(self):
        return True, np.zeros((48, 64, 3), dtype=np.uint8)

    def release(self):
        pass


def _install_mavlink(monkeypatch):
    mav = importlib.import_module("flightrisk.drone.mavlink")
    monkeypatch.setattr(mav, "HAS_MAVSDK", True)
    monkeypatch.setattr(mav, "System", _FakeMavSystem, raising=False)
    monkeypatch.setattr(mav, "VelocityBodyYawspeed", _FakeVelocity, raising=False)
    monkeypatch.setattr(mav, "OffboardError", _FakeOffboardError, raising=False)
    monkeypatch.setattr(mav.cv2, "VideoCapture", _FakeCapture)


def _mavlink_refresh(ctrl):
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if {"battery", "position", "heading"} <= set(ctrl._measured_at):
            return
        time.sleep(0.01)
    raise AssertionError("MAVLink telemetry streams did not deliver")


def _mavlink_teardown(ctrl):
    if ctrl._loop is not None:
        ctrl.disconnect()


MAVLINK = Harness(
    install=_install_mavlink,
    refresh=_mavlink_refresh,
    unsupported=ALL_READINGS - {"battery_pct", "latitude_deg", "longitude_deg", "altitude_msl_m", "heading_deg"},
    busy=None,  # MAVLink queues commands behind the lock; nothing is dropped
    host="udp://:14540",
    teardown=_mavlink_teardown,
)


# --- Fake -------------------------------------------------------------------


@contextlib.contextmanager
def _fake_busy(ctrl):
    ctrl.set_busy(True)
    try:
        yield
    finally:
        ctrl.set_busy(False)


FAKE = Harness(
    install=lambda monkeypatch: None,
    refresh=lambda ctrl: None,
    unsupported=frozenset({"latitude_deg", "longitude_deg", "altitude_msl_m", "heading_deg", "tof_m", "baro_altitude_m"}),
    busy=_fake_busy,
    host="fake://drone",
)


HARNESSES: dict[str, Harness] = {"tello": TELLO, "mavlink": MAVLINK, "fake": FAKE}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(params=registry.names())
def backend(request):
    return request.param


@pytest.fixture
def harness(backend):
    if backend not in HARNESSES:
        pytest.fail(f"backend {backend!r} has no contract-test harness; add one to HARNESSES")
    return HARNESSES[backend]


@pytest.fixture
def make_drone(backend, harness, monkeypatch):
    """Build controllers through the registry, exactly as the dashboard does."""
    harness.install(monkeypatch)
    spec = registry.get(backend)
    built = []

    def _make(drone_id="contract-1"):
        ctrl = spec.create(drone_id, harness.host, spec.options({}))
        built.append(ctrl)
        return ctrl

    yield _make
    for ctrl in built:
        harness.teardown(ctrl)


@pytest.fixture
def connected(make_drone, harness):
    ctrl = make_drone()
    assert ctrl.connect() is True
    harness.refresh(ctrl)
    return ctrl


def _old_frame_payload(state):
    """The frame.telemetry dict exactly as pipeline.py built it before the refactor."""
    return {
        "battery": state.battery,
        "height": state.height,
        "temperature": state.temperature,
        "flight_time": state.flight_time,
        "is_flying": state.is_flying,
    }


def _old_fleet_payload(state):
    """One fleet.get_all_telemetry() entry exactly as fleet.py built it before."""
    return {**_old_frame_payload(state), "is_connected": state.is_connected}


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_every_backend_has_a_harness():
    missing = set(registry.names()) - set(HARNESSES)
    assert not missing, f"add contract-test harnesses for: {sorted(missing)}"


def test_builtin_backends_are_registered():
    assert {"tello", "mavlink", "fake"} <= set(registry.names())
    assert registry.DEFAULT_BACKEND in registry.names()


class TestRoles:
    def test_controller_implements_mandatory_roles(self, make_drone):
        ctrl = make_drone()
        for role in (Lifecycle, TelemetrySource, FlightControl, Motion, VideoSource, DroneController):
            assert isinstance(ctrl, role), role.__name__
        assert isinstance(ctrl.capabilities, DroneCapabilities)
        assert BASIC_FLIGHT <= ctrl.capabilities.features

    def test_optional_capabilities_match_methods(self, make_drone):
        ctrl = make_drone()
        caps = ctrl.capabilities
        assert caps.supports(Capability.RETURN_TO_HOME) == callable(getattr(ctrl, "return_to_home", None))
        assert caps.supports(Capability.GOTO_GPS) == callable(getattr(ctrl, "goto_gps", None))

    def test_backend_source_never_raises_not_implemented(self, make_drone):
        """'Unsupported' is a capability, never a NotImplementedError (Liskov)."""
        source = inspect.getsource(inspect.getmodule(type(make_drone())))
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Raise) and node.exc is not None:
                target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                assert getattr(target, "id", None) != "NotImplementedError"


class TestLifecycle:
    def test_connect_and_disconnect(self, make_drone, harness):
        ctrl = make_drone()
        assert ctrl.snapshot().connected is False
        assert ctrl.connect() is True
        harness.refresh(ctrl)
        assert ctrl.snapshot().connected is True
        ctrl.disconnect()
        snap = ctrl.snapshot()
        assert snap.connected is False
        assert snap.is_flying is False
        ctrl.disconnect()  # idempotent

    def test_identity(self, make_drone, backend):
        ctrl = make_drone("drone-7")
        snap = ctrl.snapshot()
        assert ctrl.name == "drone-7"
        assert snap.backend == backend
        assert snap.drone_id == "drone-7"


class TestTelemetry:
    def test_no_readings_before_any_measurement(self, make_drone):
        """Missing data is None, never a fake zero."""
        snap = make_drone().snapshot()
        assert isinstance(snap, TelemetrySnapshot)
        assert all(r is None for r in snap.readings().values()), snap.readings()

    def test_reading_types_units_and_age(self, connected):
        now = time.time()
        snap = connected.snapshot()
        assert isinstance(snap.captured_at, float)
        assert snap.is_flying_source in (IS_FLYING_COMMANDED, IS_FLYING_SENSED)
        assert snap.flight_mode is None or isinstance(snap.flight_mode, str)
        present = {name: r for name, r in snap.readings().items() if r is not None}
        assert present, "a connected backend must report at least one reading"
        for name, r in present.items():
            assert isinstance(r, Reading), name
            assert isinstance(r.value, (int, float)) and not isinstance(r.value, bool), name
            assert r.unit == READING_UNITS[name], name
            assert isinstance(r.source, str) and r.source, name
            assert r.measured_at <= now + 1.0, name
            assert r.age_s() >= 0.0, name

    def test_unsupported_sensors_are_none_and_others_present(self, connected, harness):
        readings = connected.snapshot().readings()
        for name in harness.unsupported:
            assert readings[name] is None, name
        for name in ALL_READINGS - harness.unsupported:
            assert readings[name] is not None, name

    def test_legacy_payloads_match_pre_refactor_shape(self, connected):
        """Compatibility adapter == the dicts the UI received before."""
        for _ in range(2):
            snap = connected.snapshot()
            assert legacy_telemetry_payload(snap) == _old_frame_payload(connected.state)
            assert list(legacy_telemetry_payload(snap)) == list(_old_frame_payload(connected.state))
            assert legacy_telemetry_payload(snap, include_connected=True) == _old_fleet_payload(connected.state)
            connected.takeoff()

    def test_legacy_payload_before_connect(self, make_drone):
        ctrl = make_drone()
        assert legacy_telemetry_payload(ctrl.snapshot(), include_connected=True) == _old_fleet_payload(ctrl.state)


class TestCommands:
    def test_commands_report_results_without_not_implemented(self, connected):
        calls = [
            ("takeoff", lambda: connected.takeoff()),
            ("move", lambda: connected.move("forward", 20)),
            ("rotate", lambda: connected.rotate(6)),
            ("rc_control", lambda: connected.rc_control(0, 0, 0, 0)),
            ("hover", lambda: connected.hover()),
        ]
        if connected.capabilities.supports(Capability.RETURN_TO_HOME):
            calls.append(("return_to_home", lambda: connected.return_to_home()))
        calls.append(("land", lambda: connected.land()))
        for name, call in calls:
            try:
                result = call()
            except NotImplementedError:  # pragma: no cover - the assertion is the point
                pytest.fail(f"{name} raised NotImplementedError")
            assert isinstance(result, CommandResult), name
            assert result.command == name
            assert result.ok, (name, result)

    def test_land_is_idempotent(self, connected):
        connected.takeoff()
        assert connected.snapshot().is_flying is True
        assert connected.land().ok
        assert connected.land().ok
        assert connected.snapshot().is_flying is False

    def test_dropped_move_is_reported_not_silent(self, connected, harness, backend):
        if harness.busy is None:
            pytest.skip(f"{backend} queues commands behind its lock instead of dropping them")
        connected.takeoff()
        with harness.busy(connected):
            result = connected.move("forward", 20)
        assert isinstance(result, CommandResult)
        assert result.outcome is CommandOutcome.DROPPED_BUSY
        assert not result.ok
        assert connected.move("forward", 20).ok

    def test_video_source(self, connected):
        frames = []
        connected.on_frame(frames.append)
        frame = connected.get_frame()
        assert frame is None or isinstance(frame, np.ndarray)
