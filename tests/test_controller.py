"""Tests for DroneController protocol, DroneState, and DroneCapabilities."""

from unittest.mock import patch

import pytest

from flightrisk.drone.controller import (
    DroneCapabilities,
    DroneController,
    DroneState,
    GpsDroneController,
)


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

    @patch("flightrisk.drone.tello.Tello")
    def test_tello_is_drone_controller(self, mock_tello_cls):
        from flightrisk.drone.tello import TelloController

        ctrl = TelloController(name="test", host="192.168.10.1")
        assert isinstance(ctrl, DroneController)

    @patch("flightrisk.drone.tello.Tello")
    def test_tello_has_no_goto_gps(self, mock_tello_cls):
        """TelloController must not implement goto_gps at all — the standard
        Tello has no GPS, and a method that only raises NotImplementedError
        is a Liskov Substitution violation (callers can't trust the
        DroneController interface without knowing the concrete type)."""
        from flightrisk.drone.tello import TelloController

        ctrl = TelloController(name="test", host="192.168.10.1")
        assert not hasattr(ctrl, "goto_gps")

    @patch("flightrisk.drone.tello.Tello")
    def test_tello_is_not_gps_drone_controller(self, mock_tello_cls):
        from flightrisk.drone.tello import TelloController

        ctrl = TelloController(name="test", host="192.168.10.1")
        assert not isinstance(ctrl, GpsDroneController)

    @patch("flightrisk.drone.tello.Tello")
    def test_tello_capabilities(self, mock_tello_cls):
        from flightrisk.drone.tello import TelloController

        ctrl = TelloController(name="test", host="192.168.10.1")
        assert ctrl.capabilities.has_gps is False
        assert ctrl.capabilities.max_altitude_m == 10

    def test_backward_compat_import(self):
        """Verify DroneState can still be imported from flightrisk.drone.tello."""
        from flightrisk.drone.tello import DroneState as TelloDroneState

        assert TelloDroneState is DroneState

    def test_non_conforming_object_fails_protocol(self):
        """A class missing the required attributes/methods must fail the isinstance check."""

        class FakeDrone:
            pass  # missing all required attributes/methods

        assert not isinstance(FakeDrone(), DroneController)

    def test_drone_capabilities_reexported_from_tello(self):
        """Verify DroneCapabilities can be imported from flightrisk.drone.tello."""
        from flightrisk.drone.tello import DroneCapabilities as TelloDroneCapabilities

        assert TelloDroneCapabilities is DroneCapabilities


class TestGpsDroneControllerProtocol:
    """Test the GpsDroneController subset protocol (goto_gps support)."""

    def _make_fake_gps_drone(self):
        """A minimal object satisfying GpsDroneController structurally."""

        class FakeGpsDrone:
            name = "fake"
            host = "udp://:14540"
            state = DroneState()
            capabilities = DroneCapabilities(has_gps=True)

            def connect(self) -> bool:
                return True

            def disconnect(self) -> None: ...
            def get_frame(self): return None
            def on_frame(self, cb): ...
            def takeoff(self) -> None: ...
            def land(self) -> None: ...
            def move(self, direction, distance_cm) -> None: ...
            def rotate(self, degrees) -> None: ...
            def hover(self) -> None: ...
            def rc_control(self, lr, fb, ud, yaw) -> None: ...
            def goto_gps(self, lat, lon, alt, timeout=300.0) -> None: ...

        return FakeGpsDrone()

    def test_fake_gps_drone_satisfies_gps_protocol(self):
        drone = self._make_fake_gps_drone()
        assert isinstance(drone, DroneController)
        assert isinstance(drone, GpsDroneController)

    @patch("flightrisk.drone.tello.Tello")
    def test_drone_controller_does_not_require_goto_gps(self, mock_tello_cls):
        """The base DroneController protocol must be satisfiable without
        goto_gps — this is the whole point of the LSP fix."""
        from flightrisk.drone.tello import TelloController

        ctrl = TelloController(name="test", host="192.168.10.1")
        assert isinstance(ctrl, DroneController)
        assert not isinstance(ctrl, GpsDroneController)

    def test_object_missing_goto_gps_fails_gps_protocol(self):
        class NoGpsDrone:
            name = "no-gps"
            host = "1.2.3.4"
            state = DroneState()
            capabilities = DroneCapabilities()

            def connect(self) -> bool: return True
            def disconnect(self) -> None: ...
            def get_frame(self): return None
            def on_frame(self, cb): ...
            def takeoff(self) -> None: ...
            def land(self) -> None: ...
            def move(self, direction, distance_cm) -> None: ...
            def rotate(self, degrees) -> None: ...
            def hover(self) -> None: ...
            def rc_control(self, lr, fb, ud, yaw) -> None: ...
            # No goto_gps.

        drone = NoGpsDrone()
        assert isinstance(drone, DroneController)
        assert not isinstance(drone, GpsDroneController)
