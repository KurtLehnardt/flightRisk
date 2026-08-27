"""Tests for MavlinkController — MAVSDK-Python wrapper.

All mavsdk imports are mocked so the test suite runs without the
mavsdk package installed.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Build a fake mavsdk module tree so MavlinkController can import it.
# ---------------------------------------------------------------------------

_mavsdk_module = types.ModuleType("mavsdk")
_mavsdk_action = types.ModuleType("mavsdk.action")
_mavsdk_offboard = types.ModuleType("mavsdk.offboard")
_mavsdk_telemetry = types.ModuleType("mavsdk.telemetry")
_mavsdk_manual_control = types.ModuleType("mavsdk.manual_control")


class _FakeSystem:
    """Stand-in for mavsdk.System that wires up async mocks."""

    def __init__(self):
        self.action = MagicMock()
        self.offboard = MagicMock()
        self.telemetry = MagicMock()
        self.manual_control = MagicMock()

        # Make action methods async
        self.action.arm = AsyncMock()
        self.action.takeoff = AsyncMock()
        self.action.land = AsyncMock()
        self.action.goto_location = AsyncMock()

        # Make offboard methods async
        self.offboard.set_velocity_body = AsyncMock()
        self.offboard.start = AsyncMock()
        self.offboard.stop = AsyncMock()

        # Make manual_control async
        self.manual_control.set_manual_control_input = AsyncMock()

        # connect
        self.connect = AsyncMock()

    def _make_async_iter(self, values):
        """Return an async iterator that yields values then hangs forever."""

        async def _gen():
            for v in values:
                yield v
            # Block so the subscription task stays alive
            await asyncio.sleep(999999)

        return _gen()


class _FakeVelocityBodyYawspeed:
    def __init__(self, vx=0.0, vy=0.0, vz=0.0, yaw=0.0):
        self.forward_m_s = vx
        self.right_m_s = vy
        self.down_m_s = vz
        self.yawspeed_deg_s = yaw


class _FakeOffboardError(Exception):
    pass


class _FakeActionError(Exception):
    pass


# Wire up the fake module tree
_mavsdk_module.System = _FakeSystem
_mavsdk_offboard.VelocityBodyYawspeed = _FakeVelocityBodyYawspeed
_mavsdk_offboard.OffboardError = _FakeOffboardError
_mavsdk_action.ActionError = _FakeActionError

sys.modules["mavsdk"] = _mavsdk_module
sys.modules["mavsdk.action"] = _mavsdk_action
sys.modules["mavsdk.offboard"] = _mavsdk_offboard
sys.modules["mavsdk.telemetry"] = _mavsdk_telemetry
sys.modules["mavsdk.manual_control"] = _mavsdk_manual_control

# Now we can import the module under test
from amber.drone.mavlink import MavlinkController, HAS_MAVSDK, _extract_ip  # noqa: E402
from amber.drone.controller import DroneController, DroneState  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _HealthItem:
    """Mimics a telemetry.health() async-iterator element."""

    def __init__(self, is_armable=True, is_global_position_ok=True):
        self.is_armable = is_armable
        self.is_global_position_ok = is_global_position_ok


class _PositionItem:
    def __init__(self, lat=37.0, lon=-122.0, alt=50.0):
        self.latitude_deg = lat
        self.longitude_deg = lon
        self.absolute_altitude_m = alt


class _HeadingItem:
    def __init__(self, deg=90):
        self.heading_deg = deg


class _BatteryItem:
    def __init__(self, pct=0.85):
        self.remaining_percent = pct


class _FlightModeItem:
    def __init__(self, mode="HOLD"):
        self._mode = mode

    def __str__(self):
        return self._mode


class _GpsInfoItem:
    class _FixType:
        def __init__(self, val=4):
            self.value = val

        def __str__(self):
            return f"FIX_{self.value}"

    def __init__(self, fix=4):
        self.fix_type = self._FixType(fix)


def _make_async_iter(values):
    """Create an async generator that yields values then blocks."""

    async def _gen():
        for v in values:
            yield v
        await asyncio.sleep(999999)

    return _gen()


def _make_controller(system: _FakeSystem | None = None) -> MavlinkController:
    """Create a MavlinkController and optionally inject a fake System."""
    ctrl = MavlinkController(name="test-mav", host="udp://:14540")
    if system is not None:
        ctrl._system = system
        ctrl._running = True
        ctrl._loop = asyncio.new_event_loop()
        ctrl._loop_thread = threading.Thread(
            target=ctrl._loop.run_forever, daemon=True
        )
        ctrl._loop_thread.start()
    return ctrl


def _teardown_controller(ctrl: MavlinkController) -> None:
    """Stop the event loop and clean up."""
    ctrl._running = False
    if ctrl._loop and ctrl._loop.is_running():
        ctrl._loop.call_soon_threadsafe(ctrl._loop.stop)
    if ctrl._loop_thread and ctrl._loop_thread.is_alive():
        ctrl._loop_thread.join(timeout=2.0)
    if ctrl._loop and not ctrl._loop.is_closed():
        ctrl._loop.close()
    ctrl._loop = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def fast_offboard(monkeypatch):
    """Patch asyncio.sleep to be instant so streaming-loop tests don't
    spend real wall-clock time waiting.
    """

    _real_sleep = asyncio.sleep

    async def _instant_sleep(delay, result=None):
        # Let zero-second sleeps through normally (cooperative yield),
        # but skip any real delays.
        return await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """MavlinkController must satisfy the DroneController protocol."""

    def test_isinstance_check(self):
        ctrl = MavlinkController(name="proto", host="udp://:14540")
        assert isinstance(ctrl, DroneController)

    def test_has_required_attributes(self):
        ctrl = MavlinkController(name="attr", host="udp://:14540")
        assert hasattr(ctrl, "name")
        assert hasattr(ctrl, "host")
        assert hasattr(ctrl, "state")
        assert hasattr(ctrl, "capabilities")
        assert isinstance(ctrl.state, DroneState)

    def test_capabilities_values(self):
        ctrl = MavlinkController(name="cap", host="udp://:14540")
        assert ctrl.capabilities.has_gps is True
        assert ctrl.capabilities.has_rtsp is True
        assert ctrl.capabilities.min_move_cm == 10
        assert ctrl.capabilities.max_move_cm == 50000
        assert ctrl.capabilities.max_altitude_m == 120
        assert ctrl.capabilities.supports_missions is True


class TestConstructor:
    def test_default_host(self):
        ctrl = MavlinkController(name="d")
        assert ctrl.host == "udp://:14540"

    def test_default_rtsp_url(self):
        ctrl = MavlinkController(name="d", host="udp://10.0.0.5:14540")
        assert ctrl.rtsp_url == "rtsp://10.0.0.5:8554/camera"

    def test_custom_rtsp_url(self):
        ctrl = MavlinkController(name="d", rtsp_url="rtsp://cam:554/live")
        assert ctrl.rtsp_url == "rtsp://cam:554/live"

    def test_extract_ip_udp(self):
        assert _extract_ip("udp://192.168.1.10:14540") == "192.168.1.10"

    def test_extract_ip_no_host(self):
        # udp://:14540 has no host -> fallback to 127.0.0.1
        assert _extract_ip("udp://:14540") == "127.0.0.1"

    def test_initial_state(self):
        ctrl = MavlinkController(name="s")
        assert ctrl.state.is_connected is False
        assert ctrl.state.is_flying is False
        assert ctrl.state.battery == 0


class TestMavsdkMissing:
    """When mavsdk is not installed, constructor should raise."""

    def test_raises_without_mavsdk(self):
        with patch("amber.drone.mavlink.HAS_MAVSDK", False):
            with pytest.raises(RuntimeError, match="mavsdk"):
                MavlinkController(name="no-sdk")


class TestAsyncBridge:
    """Verify _run() dispatches coroutines to the background loop."""

    def test_run_dispatches_coroutine(self):
        ctrl = MavlinkController(name="bridge")
        ctrl._loop = asyncio.new_event_loop()
        ctrl._loop_thread = threading.Thread(
            target=ctrl._loop.run_forever, daemon=True
        )
        ctrl._loop_thread.start()
        try:
            result = ctrl._run(asyncio.coroutine(lambda: 42)() if False else self._async_return(42))
            assert result == 42
        finally:
            _teardown_controller(ctrl)

    @staticmethod
    async def _async_return(val):
        return val

    def test_run_raises_without_loop(self):
        ctrl = MavlinkController(name="no-loop")
        with pytest.raises(RuntimeError, match="Event loop"):
            ctrl._run(self._async_return(1))

    def test_run_timeout(self):
        ctrl = MavlinkController(name="timeout")
        ctrl._loop = asyncio.new_event_loop()
        ctrl._loop_thread = threading.Thread(
            target=ctrl._loop.run_forever, daemon=True
        )
        ctrl._loop_thread.start()
        try:
            with pytest.raises(TimeoutError):
                ctrl._run(asyncio.sleep(999), timeout=0.1)
        finally:
            _teardown_controller(ctrl)


class TestConnect:
    def test_connect_success(self):
        system = _FakeSystem()
        system.telemetry.health = MagicMock(
            return_value=_make_async_iter([_HealthItem(True, True)])
        )
        system.telemetry.position = MagicMock(
            return_value=_make_async_iter([_PositionItem()])
        )
        system.telemetry.heading = MagicMock(
            return_value=_make_async_iter([_HeadingItem()])
        )
        system.telemetry.battery = MagicMock(
            return_value=_make_async_iter([_BatteryItem()])
        )
        system.telemetry.flight_mode = MagicMock(
            return_value=_make_async_iter([_FlightModeItem()])
        )
        system.telemetry.gps_info = MagicMock(
            return_value=_make_async_iter([_GpsInfoItem()])
        )

        with patch("amber.drone.mavlink.System", return_value=system):
            ctrl = MavlinkController(name="conn")
            try:
                result = ctrl.connect()
                assert result is True
                assert ctrl.state.is_connected is True
            finally:
                ctrl._running = False
                ctrl._stop_loop()


class TestDisconnect:
    def test_disconnect_cleans_up(self):
        system = _FakeSystem()
        ctrl = _make_controller(system)
        ctrl.state.is_connected = True
        ctrl.state.is_flying = False
        try:
            ctrl.disconnect()
        except Exception:
            pass
        assert ctrl.state.is_connected is False
        assert ctrl._loop is None


class TestTakeoff:
    def test_takeoff_calls_arm_and_takeoff(self):
        system = _FakeSystem()
        ctrl = _make_controller(system)
        try:
            ctrl.takeoff()
            system.action.arm.assert_awaited_once()
            system.action.takeoff.assert_awaited_once()
            assert ctrl.state.is_flying is True
        finally:
            _teardown_controller(ctrl)


class TestLand:
    def test_land_calls_action_land(self):
        system = _FakeSystem()
        ctrl = _make_controller(system)
        ctrl.state.is_flying = True
        try:
            ctrl.land()
            system.action.land.assert_awaited_once()
            assert ctrl.state.is_flying is False
        finally:
            _teardown_controller(ctrl)


@pytest.mark.usefixtures("fast_offboard")
class TestMoveDirections:
    """Test all 6 directions produce the correct velocity vectors."""

    @pytest.mark.parametrize(
        "direction,expected_vx,expected_vy,expected_vz",
        [
            ("forward", 1.0, 0.0, 0.0),
            ("back", -1.0, 0.0, 0.0),
            ("right", 0.0, 1.0, 0.0),
            ("left", 0.0, -1.0, 0.0),
            ("up", 0.0, 0.0, -1.0),
            ("down", 0.0, 0.0, 1.0),
        ],
    )
    def test_move_direction(self, direction, expected_vx, expected_vy, expected_vz):
        system = _FakeSystem()
        ctrl = _make_controller(system)
        try:
            ctrl.move(direction, 100)

            # First call to set_velocity_body is the pre-start setpoint
            calls = system.offboard.set_velocity_body.await_args_list
            assert len(calls) >= 1
            first_setpoint = calls[0].args[0]
            assert first_setpoint.forward_m_s == expected_vx
            assert first_setpoint.right_m_s == expected_vy
            assert first_setpoint.down_m_s == expected_vz

            system.offboard.start.assert_awaited()
            system.offboard.stop.assert_awaited()
        finally:
            _teardown_controller(ctrl)

    def test_move_unknown_direction_raises(self):
        system = _FakeSystem()
        ctrl = _make_controller(system)
        try:
            with pytest.raises(ValueError, match="Unknown direction"):
                ctrl.move("diagonal", 100)
        finally:
            _teardown_controller(ctrl)


@pytest.mark.usefixtures("fast_offboard")
class TestRotate:
    def test_rotate_positive(self):
        system = _FakeSystem()
        ctrl = _make_controller(system)
        try:
            ctrl.rotate(90)
            calls = system.offboard.set_velocity_body.await_args_list
            first = calls[0].args[0]
            assert first.yawspeed_deg_s > 0  # clockwise
            system.offboard.start.assert_awaited()
            system.offboard.stop.assert_awaited()
        finally:
            _teardown_controller(ctrl)

    def test_rotate_negative(self):
        system = _FakeSystem()
        ctrl = _make_controller(system)
        try:
            ctrl.rotate(-90)
            calls = system.offboard.set_velocity_body.await_args_list
            first = calls[0].args[0]
            assert first.yawspeed_deg_s < 0  # counter-clockwise
        finally:
            _teardown_controller(ctrl)


@pytest.mark.usefixtures("fast_offboard")
class TestHover:
    def test_hover_sends_zero_velocity(self):
        system = _FakeSystem()
        ctrl = _make_controller(system)
        try:
            ctrl.hover()
            calls = system.offboard.set_velocity_body.await_args_list
            setpoint = calls[0].args[0]
            assert setpoint.forward_m_s == 0.0
            assert setpoint.right_m_s == 0.0
            assert setpoint.down_m_s == 0.0
            assert setpoint.yawspeed_deg_s == 0.0
        finally:
            _teardown_controller(ctrl)


class TestRcControl:
    """Test rc_control normalizes int values to float.

    pitch/roll/yaw: -100..100 -> -1.0..1.0
    throttle (ud): -100..100 -> 0.0..1.0
    """

    @pytest.mark.parametrize(
        "input_val,expected_axis,expected_throttle",
        [
            (100, 1.0, 1.0),
            (-100, -1.0, 0.0),
            (0, 0.0, 0.5),
            (50, 0.5, 0.75),
            (-50, -0.5, 0.25),
        ],
    )
    def test_normalization(self, input_val, expected_axis, expected_throttle):
        system = _FakeSystem()
        ctrl = _make_controller(system)
        try:
            ctrl.rc_control(input_val, input_val, input_val, input_val)
            call_args = system.manual_control.set_manual_control_input.await_args
            pitch, roll, throttle, yaw = call_args.args
            # pitch, roll, yaw use -1..1
            assert abs(pitch - expected_axis) < 0.01
            assert abs(roll - expected_axis) < 0.01
            assert abs(yaw - expected_axis) < 0.01
            # throttle uses 0..1
            assert abs(throttle - expected_throttle) < 0.01
        finally:
            _teardown_controller(ctrl)

    def test_clamping_beyond_range(self):
        system = _FakeSystem()
        ctrl = _make_controller(system)
        try:
            ctrl.rc_control(200, -200, 150, -150)
            call_args = system.manual_control.set_manual_control_input.await_args
            pitch, roll, throttle, yaw = call_args.args
            # pitch/roll/yaw clamp to -1..1
            assert -1.0 <= pitch <= 1.0
            assert -1.0 <= roll <= 1.0
            assert -1.0 <= yaw <= 1.0
            # throttle clamps to 0..1
            assert 0.0 <= throttle <= 1.0
        finally:
            _teardown_controller(ctrl)


class TestGotoGps:
    def test_goto_gps_calls_goto_location(self):
        system = _FakeSystem()
        # Position stream that immediately reports arrival
        system.telemetry.position = MagicMock(
            return_value=_make_async_iter([_PositionItem(37.0, -122.0, 50.0)])
        )
        ctrl = _make_controller(system)
        ctrl.state.heading = 180
        try:
            ctrl.goto_gps(37.0, -122.0, 50.0)
            system.action.goto_location.assert_awaited_once_with(
                37.0, -122.0, 50.0, 180.0
            )
        finally:
            _teardown_controller(ctrl)


class TestTelemetryUpdates:
    """Verify real telemetry subscription coroutines update state."""

    def test_position_updates_state(self):
        system = _FakeSystem()
        system.telemetry.position = MagicMock(
            return_value=_make_async_iter([_PositionItem(38.0, -121.0, 100.0)])
        )
        ctrl = _make_controller(system)
        try:
            # Run the real _subscribe_position; it will consume the item
            # then block on the forever-sleep — use a short timeout to break out.
            with pytest.raises(TimeoutError):
                ctrl._run(ctrl._subscribe_position(system), timeout=0.3)
            assert ctrl.state.latitude == 38.0
            assert ctrl.state.longitude == -121.0
            assert ctrl.state.altitude_msl == 100.0
        finally:
            _teardown_controller(ctrl)

    def test_heading_updates_state(self):
        system = _FakeSystem()
        system.telemetry.heading = MagicMock(
            return_value=_make_async_iter([_HeadingItem(270)])
        )
        ctrl = _make_controller(system)
        try:
            with pytest.raises(TimeoutError):
                ctrl._run(ctrl._subscribe_heading(system), timeout=0.3)
            assert ctrl.state.heading == 270
        finally:
            _teardown_controller(ctrl)

    def test_battery_updates_state(self):
        system = _FakeSystem()
        system.telemetry.battery = MagicMock(
            return_value=_make_async_iter([_BatteryItem(0.42)])
        )
        ctrl = _make_controller(system)
        try:
            with pytest.raises(TimeoutError):
                ctrl._run(ctrl._subscribe_battery(system), timeout=0.3)
            assert ctrl.state.battery == 42
        finally:
            _teardown_controller(ctrl)

    def test_flight_mode_updates_state(self):
        system = _FakeSystem()
        system.telemetry.flight_mode = MagicMock(
            return_value=_make_async_iter([_FlightModeItem("MISSION")])
        )
        ctrl = _make_controller(system)
        try:
            with pytest.raises(TimeoutError):
                ctrl._run(ctrl._subscribe_flight_mode(system), timeout=0.3)
            assert ctrl.state.flight_mode == "MISSION"
        finally:
            _teardown_controller(ctrl)


@pytest.mark.usefixtures("fast_offboard")
class TestErrorHandling:
    def test_offboard_rejected_retries(self):
        """When offboard.start raises OffboardError, it should retry."""
        system = _FakeSystem()
        system.offboard.start = AsyncMock(
            side_effect=[_FakeOffboardError("COMMAND_DENIED"), None]
        )
        ctrl = _make_controller(system)
        try:
            ctrl.move("forward", 100)
            # start was called twice (initial fail + retry)
            assert system.offboard.start.await_count == 2
        finally:
            _teardown_controller(ctrl)

    def test_command_denied_on_move(self):
        """If both offboard.start attempts raise, the error propagates."""
        system = _FakeSystem()
        system.offboard.start = AsyncMock(
            side_effect=_FakeOffboardError("COMMAND_DENIED")
        )
        ctrl = _make_controller(system)
        try:
            with pytest.raises(_FakeOffboardError):
                ctrl.move("forward", 100)
        finally:
            _teardown_controller(ctrl)


class TestGetFrame:
    def test_get_frame_returns_array(self):
        ctrl = MavlinkController(name="frame")
        fake_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        mock_cap = MagicMock()
        mock_cap.read.return_value = (True, fake_frame)
        ctrl._cap = mock_cap
        frame = ctrl.get_frame()
        assert frame is not None
        assert frame.shape == (480, 640, 3)

    def test_get_frame_returns_none_on_failure(self):
        ctrl = MavlinkController(name="frame-fail")
        mock_cap = MagicMock()
        mock_cap.read.return_value = (False, None)
        ctrl._cap = mock_cap
        frame = ctrl.get_frame()
        assert frame is None

    @patch("amber.drone.mavlink.cv2")
    def test_get_frame_creates_capture(self, mock_cv2):
        ctrl = MavlinkController(name="cap-create")
        mock_cap = MagicMock()
        mock_cap.read.return_value = (True, np.zeros((480, 640, 3), dtype=np.uint8))
        mock_cv2.VideoCapture.return_value = mock_cap
        frame = ctrl.get_frame()
        mock_cv2.VideoCapture.assert_called_once_with(ctrl.rtsp_url)
        assert frame is not None


class TestOnFrame:
    def test_on_frame_registers_callback(self):
        ctrl = MavlinkController(name="cb")
        cb = MagicMock()
        ctrl._running = False  # prevent thread from starting
        ctrl._frame_callbacks = []
        ctrl.on_frame(cb)
        assert cb in ctrl._frame_callbacks
