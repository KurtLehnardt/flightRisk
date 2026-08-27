"""Tests for DroneController protocol, DroneState, and DroneCapabilities."""

from unittest.mock import patch, MagicMock

import pytest

from amber.drone.controller import DroneCapabilities, DroneController, DroneState


class TestDroneState:
    """Test DroneState dataclass defaults."""

    def test_default_values(self):
        state = DroneState()
        assert state.battery == 0
        assert state.height == 0
        assert state.temperature == 0
        assert state.flight_time == 0
        assert state.is_flying is False
        assert state.is_connected is False

    def test_extended_fields_none_by_default(self):
        state = DroneState()
        assert state.latitude is None
        assert state.longitude is None
        assert state.altitude_msl is None
        assert state.heading is None
        assert state.ground_speed is None
        assert state.flight_mode is None

    def test_extended_fields_assignable(self):
        state = DroneState(
            latitude=37.7749,
            longitude=-122.4194,
            altitude_msl=100.5,
            heading=270,
            ground_speed=5.2,
            flight_mode="GUIDED",
        )
        assert state.latitude == 37.7749
        assert state.longitude == -122.4194
        assert state.altitude_msl == 100.5
        assert state.heading == 270
        assert state.ground_speed == 5.2
        assert state.flight_mode == "GUIDED"


class TestDroneCapabilities:
    """Test DroneCapabilities dataclass defaults."""

    def test_defaults(self):
        caps = DroneCapabilities()
        assert caps.has_gps is False
        assert caps.has_rtsp is False
        assert caps.min_move_cm == 20
        assert caps.max_move_cm == 500
        assert caps.max_altitude_m == 120
        assert caps.supports_missions is False


class TestDroneControllerProtocol:
    """Test that TelloController satisfies the DroneController protocol."""

    @patch("amber.drone.tello.Tello")
    def test_tello_is_drone_controller(self, mock_tello_cls):
        from amber.drone.tello import TelloController

        ctrl = TelloController(name="test", host="192.168.10.1")
        assert isinstance(ctrl, DroneController)

    @patch("amber.drone.tello.Tello")
    def test_tello_goto_gps_raises(self, mock_tello_cls):
        from amber.drone.tello import TelloController

        ctrl = TelloController(name="test", host="192.168.10.1")
        with pytest.raises(NotImplementedError, match="Tello does not have GPS"):
            ctrl.goto_gps(37.7749, -122.4194, 50.0)

    @patch("amber.drone.tello.Tello")
    def test_tello_capabilities(self, mock_tello_cls):
        from amber.drone.tello import TelloController

        ctrl = TelloController(name="test", host="192.168.10.1")
        assert ctrl.capabilities.has_gps is False
        assert ctrl.capabilities.max_altitude_m == 10

    def test_backward_compat_import(self):
        """Verify DroneState can still be imported from amber.drone.tello."""
        from amber.drone.tello import DroneState as TelloDroneState

        assert TelloDroneState is DroneState
