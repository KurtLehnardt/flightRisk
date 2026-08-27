"""Tests for amber.drone.fleet multi-drone manager."""

from unittest.mock import MagicMock, patch

import pytest

from amber.drone.controller import DroneController
from amber.drone.fleet import DroneFleet
from amber.drone.tello import DroneState


def _make_controller(name="drone", host="192.168.10.1", connect_ok=True):
    """Build a MagicMock that satisfies the DroneController protocol."""
    ctrl = MagicMock(spec=DroneController)
    ctrl.name = name
    ctrl.host = host
    ctrl.state = DroneState(
        battery=80, height=0, temperature=25,
        flight_time=0, is_flying=False, is_connected=True,
    )
    ctrl.connect.return_value = connect_ok
    return ctrl


@pytest.fixture
def mock_tello_connect():
    """Factory (for DroneFleet's `factory` param) whose controllers connect() successfully."""
    def _factory(name="drone", host="192.168.10.1"):
        return _make_controller(name=name, host=host, connect_ok=True)
    return _factory


@pytest.fixture
def mock_tello_fail():
    """Factory (for DroneFleet's `factory` param) whose controllers fail to connect()."""
    def _factory(name="drone", host="192.168.10.1"):
        return _make_controller(name=name, host=host, connect_ok=False)
    return _factory


class TestRegister:
    def test_register_success(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        assert fleet.register("d1") is True
        assert fleet.count == 1

    def test_register_duplicate_returns_false(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1")
        assert fleet.register("d1") is False
        assert fleet.count == 1

    def test_register_connect_fail_returns_false(self, mock_tello_fail):
        fleet = DroneFleet(factory=mock_tello_fail)
        assert fleet.register("d1") is False
        assert fleet.count == 0


class TestDeregister:
    def test_deregister_removes_drone(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1")
        assert fleet.deregister("d1") is True
        assert fleet.count == 0

    def test_deregister_nonexistent_returns_false(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        assert fleet.deregister("nope") is False


class TestGet:
    def test_get_returns_controller(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1")
        ctrl = fleet.get("d1")
        assert ctrl is not None
        assert ctrl.name == "d1"

    def test_get_nonexistent_returns_none(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        assert fleet.get("nope") is None


class TestDuplicateHost:
    def test_register_duplicate_host_returns_false(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1", host="192.168.10.1")
        assert fleet.register("d2", host="192.168.10.1") is False
        assert fleet.count == 1

    def test_has_host_returns_true(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1", host="192.168.10.1")
        assert fleet.has_host("192.168.10.1") is True

    def test_has_host_returns_false(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        assert fleet.has_host("192.168.10.1") is False


class TestPrimary:
    def test_primary_is_first_registered(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1", host="192.168.10.1")
        fleet.register("d2", host="192.168.10.2")
        assert fleet.primary.name == "d1"

    def test_primary_after_deregister_updates(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1", host="192.168.10.1")
        fleet.register("d2", host="192.168.10.2")
        fleet.deregister("d1")
        assert fleet.primary.name == "d2"

    def test_primary_empty_is_none(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        assert fleet.primary is None


class TestProperties:
    def test_count_correct(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        assert fleet.count == 0
        fleet.register("d1", host="192.168.10.1")
        assert fleet.count == 1
        fleet.register("d2", host="192.168.10.2")
        assert fleet.count == 2

    def test_drone_ids_returns_list(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1", host="192.168.10.1")
        fleet.register("d2", host="192.168.10.2")
        ids = fleet.drone_ids
        assert "d1" in ids
        assert "d2" in ids
        assert len(ids) == 2


class TestTelemetry:
    def test_get_all_telemetry_returns_dict(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1", host="192.168.10.1")
        fleet.register("d2", host="192.168.10.2")
        telemetry = fleet.get_all_telemetry()
        assert "d1" in telemetry
        assert "d2" in telemetry
        assert telemetry["d1"]["battery"] == 80
        assert telemetry["d1"]["is_connected"] is True


class TestBroadcast:
    def test_broadcast_command_calls_all(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1", host="192.168.10.1")
        fleet.register("d2", host="192.168.10.2")
        fleet.broadcast_command("hover")
        for did in fleet.drone_ids:
            fleet.get(did).hover.assert_called_once()


class TestDisconnectAll:
    def test_disconnect_all_clears_fleet(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1", host="192.168.10.1")
        fleet.register("d2", host="192.168.10.2")
        fleet.disconnect_all()
        assert fleet.count == 0
        assert fleet.primary is None
        assert fleet.drone_ids == []


class TestFactoryPattern:
    """Verify DroneFleet is backend-agnostic via constructor-injected factories."""

    def test_custom_factory_is_used_to_build_drones(self):
        built = []

        def custom_factory(name: str, host: str) -> DroneController:
            ctrl = _make_controller(name=name, host=host, connect_ok=True)
            built.append((name, host))
            return ctrl

        fleet = DroneFleet(factory=custom_factory)
        assert fleet.register("d1", host="10.0.0.5") is True
        assert built == [("d1", "10.0.0.5")]
        assert fleet.get("d1").host == "10.0.0.5"

    def test_default_factory_used_when_none_provided(self):
        # No factory passed — DroneFleet must fall back to building a
        # TelloController (lazily imported), never raise at construction time.
        with patch("amber.drone.tello.TelloController") as MockTello:
            MockTello.return_value = _make_controller(name="d1", host="192.168.10.1", connect_ok=True)
            fleet = DroneFleet()
            assert fleet.register("d1") is True
            MockTello.assert_called_once_with(name="d1", host="192.168.10.1")

    def test_two_fleets_with_different_factories_stay_isolated(self):
        factory_a_calls = []
        factory_b_calls = []

        def factory_a(name, host):
            factory_a_calls.append(name)
            return _make_controller(name=name, host=host, connect_ok=True)

        def factory_b(name, host):
            factory_b_calls.append(name)
            return _make_controller(name=name, host=host, connect_ok=True)

        fleet_a = DroneFleet(factory=factory_a)
        fleet_b = DroneFleet(factory=factory_b)
        fleet_a.register("d1")
        fleet_b.register("d1")

        assert factory_a_calls == ["d1"]
        assert factory_b_calls == ["d1"]


class TestSourceFactorySelection:
    """Verify the factory lambdas amber.dashboard.app._init_pipeline builds
    per `--source` mode (T3) wire up the correct controller backend."""

    def test_tello_source_builds_tello_controller_via_factory(self):
        with patch("amber.drone.tello.TelloController") as MockTello:
            MockTello.return_value = _make_controller(name="drone-1", host="192.168.10.1", connect_ok=True)
            from amber.drone.tello import TelloController

            factory = lambda n, h: TelloController(n, h)
            fleet = DroneFleet(factory=factory)
            assert fleet.register("drone-1") is True
            MockTello.assert_called_once_with("drone-1", "192.168.10.1")

    def test_mavlink_source_builds_mavlink_controller_via_factory(self):
        with patch("amber.drone.mavlink.MavlinkController") as MockMavlink:
            MockMavlink.return_value = _make_controller(name="drone-1", host="udp://:14540", connect_ok=True)
            from amber.drone.mavlink import MavlinkController

            factory = lambda n, h: MavlinkController(n, h, rtsp_url="rtsp://1.2.3.4:8554/camera")
            fleet = DroneFleet(factory=factory)
            assert fleet.register("drone-1", host="udp://:14540") is True
            MockMavlink.assert_called_once_with(
                "drone-1", "udp://:14540", rtsp_url="rtsp://1.2.3.4:8554/camera"
            )

    def test_default_factory_still_works_for_backward_compat(self):
        # No factory passed — mirrors sources ("webcam", "file", "edge")
        # that never construct a DroneFleet with an explicit factory, and
        # any legacy caller that predates the --source enum. DroneFleet
        # must still lazily fall back to TelloController.
        with patch("amber.drone.tello.TelloController") as MockTello:
            MockTello.return_value = _make_controller(name="drone-1", host="192.168.10.1", connect_ok=True)
            fleet = DroneFleet()
            assert fleet.register("drone-1") is True
            MockTello.assert_called_once_with(name="drone-1", host="192.168.10.1")
