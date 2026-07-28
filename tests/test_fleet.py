"""Tests for amber.drone.fleet multi-drone manager."""

from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from amber.drone.fleet import DroneFleet
from amber.drone.tello import DroneState


@pytest.fixture
def mock_tello_connect():
    """Patch TelloController so connect() succeeds without hardware."""
    with patch("amber.drone.fleet.TelloController") as MockCtrl:
        def _make_ctrl(name="drone", host="192.168.10.1"):
            ctrl = MagicMock()
            ctrl.name = name
            ctrl.host = host
            ctrl.state = DroneState(
                battery=80, height=0, temperature=25,
                flight_time=0, is_flying=False, is_connected=True,
            )
            ctrl.connect.return_value = True
            return ctrl
        MockCtrl.side_effect = _make_ctrl
        yield MockCtrl


@pytest.fixture
def mock_tello_fail():
    """Patch TelloController so connect() fails."""
    with patch("amber.drone.fleet.TelloController") as MockCtrl:
        ctrl = MagicMock()
        ctrl.connect.return_value = False
        MockCtrl.return_value = ctrl
        yield MockCtrl


class TestRegister:
    def test_register_success(self, mock_tello_connect):
        fleet = DroneFleet()
        assert fleet.register("d1") is True
        assert fleet.count == 1

    def test_register_duplicate_returns_false(self, mock_tello_connect):
        fleet = DroneFleet()
        fleet.register("d1")
        assert fleet.register("d1") is False
        assert fleet.count == 1

    def test_register_connect_fail_returns_false(self, mock_tello_fail):
        fleet = DroneFleet()
        assert fleet.register("d1") is False
        assert fleet.count == 0


class TestDeregister:
    def test_deregister_removes_drone(self, mock_tello_connect):
        fleet = DroneFleet()
        fleet.register("d1")
        assert fleet.deregister("d1") is True
        assert fleet.count == 0

    def test_deregister_nonexistent_returns_false(self, mock_tello_connect):
        fleet = DroneFleet()
        assert fleet.deregister("nope") is False


class TestGet:
    def test_get_returns_controller(self, mock_tello_connect):
        fleet = DroneFleet()
        fleet.register("d1")
        ctrl = fleet.get("d1")
        assert ctrl is not None
        assert ctrl.name == "d1"

    def test_get_nonexistent_returns_none(self, mock_tello_connect):
        fleet = DroneFleet()
        assert fleet.get("nope") is None


class TestPrimary:
    def test_primary_is_first_registered(self, mock_tello_connect):
        fleet = DroneFleet()
        fleet.register("d1")
        fleet.register("d2")
        assert fleet.primary.name == "d1"

    def test_primary_after_deregister_updates(self, mock_tello_connect):
        fleet = DroneFleet()
        fleet.register("d1")
        fleet.register("d2")
        fleet.deregister("d1")
        assert fleet.primary.name == "d2"

    def test_primary_empty_is_none(self, mock_tello_connect):
        fleet = DroneFleet()
        assert fleet.primary is None


class TestProperties:
    def test_count_correct(self, mock_tello_connect):
        fleet = DroneFleet()
        assert fleet.count == 0
        fleet.register("d1")
        assert fleet.count == 1
        fleet.register("d2")
        assert fleet.count == 2

    def test_drone_ids_returns_list(self, mock_tello_connect):
        fleet = DroneFleet()
        fleet.register("d1")
        fleet.register("d2")
        ids = fleet.drone_ids
        assert "d1" in ids
        assert "d2" in ids
        assert len(ids) == 2


class TestTelemetry:
    def test_get_all_telemetry_returns_dict(self, mock_tello_connect):
        fleet = DroneFleet()
        fleet.register("d1")
        fleet.register("d2")
        telemetry = fleet.get_all_telemetry()
        assert "d1" in telemetry
        assert "d2" in telemetry
        assert telemetry["d1"]["battery"] == 80
        assert telemetry["d1"]["is_connected"] is True


class TestBroadcast:
    def test_broadcast_command_calls_all(self, mock_tello_connect):
        fleet = DroneFleet()
        fleet.register("d1")
        fleet.register("d2")
        fleet.broadcast_command("hover")
        for did in fleet.drone_ids:
            fleet.get(did).hover.assert_called_once()


class TestDisconnectAll:
    def test_disconnect_all_clears_fleet(self, mock_tello_connect):
        fleet = DroneFleet()
        fleet.register("d1")
        fleet.register("d2")
        fleet.disconnect_all()
        assert fleet.count == 0
        assert fleet.primary is None
        assert fleet.drone_ids == []
