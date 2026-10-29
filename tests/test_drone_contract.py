"""Contract tests every registered drone backend must pass.

Parametrized over ``flightrisk.drone.registry.names()``, so a newly
registered backend is tested automatically -- and fails
``test_every_backend_has_a_harness`` until it gets a harness below that
mocks its hardware. Controllers are always built through the registry, the
same way the dashboard builds them.

Hardware is faked at the lowest level each backend talks to: the Tello
backend runs the real djitellopy client over ``FakeTelloWire`` (the fake
command socket and state stream from ``tests/conftest.py`` that main's Tello
tests use), so its replies and state packets arrive as on hardware; MAVLink
gets a fake MAVSDK ``System`` (async telemetry streams) and
``cv2.VideoCapture``.

Command results are read the way every caller reads them: normalized with
``command_result()`` (see ``docs/drone-interface.md``). Backends may return a
``CommandResult`` or the legacy ``True``/``None`` (executed) and ``False``
(not executed).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib
import inspect
import threading
import time
import types
from collections.abc import Callable
from dataclasses import dataclass, field
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
    command_result,
)
from flightrisk.drone.snapshot import (
    IS_FLYING_COMMANDED,
    IS_FLYING_SENSED,
    READING_UNITS,
    Reading,
    TelemetrySnapshot,
    legacy_telemetry_payload,
)
from tests.conftest import FakeTelloWire, _tello_hosts

ALL_READINGS = frozenset(READING_UNITS)


# ---------------------------------------------------------------------------
# Per-backend harnesses
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    #: Install hardware fakes (monkeypatch) -- called before the factory
    #: runs. Returns the host to build the controller with, or None for
    #: ``host``.
    install: Callable[[pytest.MonkeyPatch], str | None]
    #: Make the backend deliver one round of telemetry after connect().
    refresh: Callable[[object], None]
    #: Readings this backend cannot provide: must stay None.
    unsupported: frozenset[str]
    #: Context manager that makes the command channel busy, as another
    #: command in flight would.
    busy: Callable[[object], contextlib.AbstractContextManager]
    #: Whether a move sent while ``busy`` is dropped (reported as
    #: DROPPED_BUSY) rather than queued behind the busy command.
    drops_when_busy: bool = True
    host: str = "test-host"
    #: Called on each controller right after the registry built it.
    prepare: Callable[[object], None] = field(default=lambda ctrl: None)
    teardown: Callable[[object], None] = field(default=lambda ctrl: None)


@contextlib.contextmanager
def _hold_cmd_lock(ctrl):
    """Hold the controller's command lock, as a flight command in flight does."""
    ctrl._cmd_lock.acquire()
    try:
        yield
    finally:
        ctrl._cmd_lock.release()


# --- Tello ------------------------------------------------------------------

#: The FakeTelloWire behind each Tello host built in the current test.
_TELLO_WIRES: dict[str, FakeTelloWire] = {}


def _install_tello(monkeypatch):
    """Run the real djitellopy client over a FakeTelloWire, as ``tello_on_wire`` does."""
    import djitellopy.tello as djitello

    tello_mod = importlib.import_module("flightrisk.drone.tello")
    host = next(_tello_hosts)
    wire = FakeTelloWire(host)
    _TELLO_WIRES[host] = wire
    # threads_initialized stops djitellopy from binding its real UDP
    # sockets; client_socket is the socket every command is sent on.
    monkeypatch.setattr(djitello, "threads_initialized", True)
    monkeypatch.setattr(djitello, "client_socket", wire, raising=False)
    # No keepalive/state-poll threads in tests; telemetry is refreshed by
    # calling one poll explicitly.
    monkeypatch.setattr(tello_mod.TelloController, "_start_keepalive", lambda self: None)
    monkeypatch.setattr(tello_mod.TelloController, "_start_state_polling", lambda self: None)
    return host


def _prepare_tello(ctrl):
    # State packets (about 20 a second) of a Tello resting on the ground with
    # its motors off: connect() waits for the first one, the poll reads
    # them, and a land is confirmed by them as on hardware.
    _TELLO_WIRES[ctrl.host].start_state(height_cm=0)
    # The video decoder is not under test; serve a still frame.
    still = types.SimpleNamespace(frame=np.zeros((48, 64, 3), dtype=np.uint8))
    ctrl.tello.get_frame_read = lambda *args, **kwargs: still


def _teardown_tello(ctrl):
    wire = _TELLO_WIRES.pop(ctrl.host, None)
    ctrl._running = False
    if wire is not None:
        wire.stop_state()
        wire.cancel_pending()
    # Keep djitellopy's end() (run when the client is garbage collected)
    # from sending anything after the fake socket is gone.
    ctrl.tello.is_flying = False
    ctrl.tello.stream_on = False


TELLO = Harness(
    install=_install_tello,
    refresh=lambda ctrl: ctrl._poll_once(),
    unsupported=frozenset({"latitude_deg", "longitude_deg", "altitude_msl_m", "heading_deg"}),
    busy=_hold_cmd_lock,
    prepare=_prepare_tello,
    teardown=_teardown_tello,
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
    def __init__(self, **_kwargs):
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
        self.telemetry.battery = lambda: _agen(_Item(remaining_percent=87.0))  # MAVSDK 2+: 0 to 100
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
    busy=_hold_cmd_lock,
    drops_when_busy=False,  # MAVLink queues commands behind the lock; nothing is dropped
    host="udp://:14540",
    teardown=_mavlink_teardown,
)


# --- Simulated drone (sim) --------------------------------------------------


@contextlib.contextmanager
def _sim_busy(ctrl):
    ctrl.set_busy(True)
    try:
        yield
    finally:
        ctrl.set_busy(False)


SIM = Harness(
    install=lambda monkeypatch: None,
    refresh=lambda ctrl: None,
    unsupported=frozenset({"latitude_deg", "longitude_deg", "altitude_msl_m", "heading_deg", "tof_m", "baro_altitude_m"}),
    busy=_sim_busy,
    host="sim://drone",
)


HARNESSES: dict[str, Harness] = {"tello": TELLO, "mavlink": MAVLINK, "sim": SIM}


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
    host = harness.install(monkeypatch) or harness.host
    spec = registry.get(backend)
    built = []

    def _make(drone_id="contract-1"):
        ctrl = spec.create(drone_id, host, spec.options({}))
        harness.prepare(ctrl)
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
    assert {"tello", "mavlink", "sim"} <= set(registry.names())
    # "fake" is the edge device's simulated Tello (--source fake), never a
    # dashboard backend name.
    assert "fake" not in registry.names()
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
                value = call()
            except NotImplementedError:  # pragma: no cover - the assertion is the point
                pytest.fail(f"{name} raised NotImplementedError")
            assert value is None or isinstance(value, (bool, CommandResult)), (name, value)
            result = command_result(value, name)
            assert result.command == name
            assert result.ok, (name, result)

    def test_land_is_idempotent(self, connected):
        connected.takeoff()
        assert connected.snapshot().is_flying is True
        assert command_result(connected.land(), "land").ok
        assert command_result(connected.land(), "land").ok
        assert connected.snapshot().is_flying is False

    def test_dropped_move_is_reported_not_silent(self, connected, harness, backend):
        if not harness.drops_when_busy:
            pytest.skip(f"{backend} queues commands behind its lock instead of dropping them")
        connected.takeoff()
        with harness.busy(connected):
            value = connected.move("forward", 20)
        result = command_result(value, "move")
        assert result.outcome is CommandOutcome.DROPPED_BUSY
        assert not result.ok
        assert not value  # falsy, like the bare False it replaces
        assert command_result(connected.move("forward", 20), "move").ok

    @pytest.mark.parametrize("command", ["land", "takeoff"])
    def test_land_and_takeoff_are_never_dropped_while_the_channel_is_busy(self, connected, harness, command):
        """FlightControl contract: a busy channel is waited for (or, for a
        land, interrupted), never a reason to drop a land or a takeoff."""
        if command == "land":
            assert command_result(connected.takeoff(), "takeoff").ok
        outcome: dict = {}

        def run():
            try:
                outcome["value"] = getattr(connected, command)()
            except BaseException as exc:  # noqa: BLE001 -- reported by the assertion below
                outcome["error"] = exc

        with harness.busy(connected):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            # The busy command finishes a moment later.
            worker.join(0.3)
        worker.join(30.0)
        assert not worker.is_alive(), f"{command} did not return once the channel was free"
        assert "error" not in outcome, outcome
        result = command_result(outcome["value"], command)
        assert result.outcome is not CommandOutcome.DROPPED_BUSY
        assert result.ok, result
        assert connected.snapshot().is_flying is (command == "takeoff")

    def test_video_source(self, connected):
        frames = []
        connected.on_frame(frames.append)
        frame = connected.get_frame()
        assert frame is None or isinstance(frame, np.ndarray)
