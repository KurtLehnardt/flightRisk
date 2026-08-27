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

    def test_broadcast_command_returns_success_results(self, mock_tello_connect):
        """Every drone that succeeds must map to None in the results dict —
        this is the feedback that was previously swallowed entirely."""
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1", host="192.168.10.1")
        fleet.register("d2", host="192.168.10.2")
        results = fleet.broadcast_command("hover")
        assert results == {"d1": None, "d2": None}

    def test_broadcast_command_reports_per_drone_failure(self, mock_tello_connect):
        """A failing command on one drone must not be silently swallowed —
        it should show up as that drone's Exception in the results dict,
        while other drones still get called and report success."""
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1", host="192.168.10.1")
        fleet.register("d2", host="192.168.10.2")

        boom = RuntimeError("land failed")
        fleet.get("d1").land.side_effect = boom

        results = fleet.broadcast_command("land")

        assert results["d1"] is boom
        assert results["d2"] is None
        fleet.get("d2").land.assert_called_once()

    def test_broadcast_command_all_fail(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1", host="192.168.10.1")
        fleet.get("d1").hover.side_effect = RuntimeError("boom")

        results = fleet.broadcast_command("hover")

        assert isinstance(results["d1"], RuntimeError)

    def test_broadcast_command_passes_args_and_kwargs(self, mock_tello_connect):
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1", host="192.168.10.1")
        fleet.broadcast_command("move", "forward", distance_cm=50)
        fleet.get("d1").move.assert_called_once_with("forward", distance_cm=50)

    def test_broadcast_command_unknown_method_reported_not_raised(self, mock_tello_connect):
        """Calling a command that doesn't exist on the controller must be
        captured as a per-drone error, not raised out of broadcast_command."""
        fleet = DroneFleet(factory=mock_tello_connect)
        fleet.register("d1", host="192.168.10.1")

        results = fleet.broadcast_command("no_such_command")

        assert isinstance(results["d1"], Exception)


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
    """Verify amber.dashboard.app._init_pipeline (T3) wires up the correct
    controller backend per `--source` mode, by calling _init_pipeline()
    itself rather than duplicating its logic with hand-rolled lambdas
    (see TestFactoryPattern above, which covers DroneFleet in isolation)."""

    @staticmethod
    def _stub_heavy_components(state):
        """Pre-populate _state with mocks for everything _init_pipeline
        lazily constructs, so these tests don't need real ML models."""
        for key in (
            "detector", "reid", "face", "scorer", "tracker", "reasoning",
            "logger", "metrics", "db", "obstacle_guard", "canon",
        ):
            state[key] = MagicMock()
        state["db"].create_session.return_value = "session-1"

    def teardown_method(self, method):
        from amber.dashboard.app import _state

        # _init_pipeline() spawns a background auto-connect thread per
        # source; release it so it doesn't keep running (or touching
        # mocks torn down by other tests) afterward.
        stop = _state.get("auto_connect_stop")
        if stop is not None:
            stop.set()
        _state["fleet"] = None
        _state["cap"] = None
        _state["running"] = False

        # Reset every key _stub_heavy_components() populated with a
        # MagicMock — otherwise those mocks (e.g. _state["face"]) leak into
        # whatever test module runs next in the same pytest process (module
        # globals are shared process-wide) and break unrelated tests, e.g.
        # jsonify() choking on a MagicMock returned from a route handler.
        for key in (
            "detector", "reid", "face", "scorer", "tracker", "reasoning",
            "logger", "metrics", "db", "obstacle_guard", "canon",
        ):
            _state[key] = None

    def test_tello_source_builds_tello_controller_via_factory(self):
        from amber.dashboard.app import SourceConfig, _init_pipeline, _state

        self._stub_heavy_components(_state)
        # running=False keeps the background auto-connect thread from
        # doing any real work during the test — its loop condition checks
        # _state["running"] and returns immediately.
        _state["running"] = False

        with patch("amber.drone.tello.TelloController") as MockTello:
            MockTello.return_value = _make_controller(name="drone-1", host="192.168.10.1", connect_ok=True)
            _init_pipeline(SourceConfig(source="tello"))

            assert isinstance(_state["fleet"], DroneFleet)
            assert _state["source"] == "tello"

            # Exercise the actual factory _init_pipeline built (production
            # code), not a hand-rolled duplicate, to confirm it constructs
            # a TelloController with the expected args.
            _state["fleet"]._factory("drone-1", "192.168.10.1")
            MockTello.assert_called_once_with("drone-1", "192.168.10.1")

    def test_mavlink_source_builds_mavlink_controller_via_factory(self):
        from amber.dashboard.app import SourceConfig, _init_pipeline, _state

        self._stub_heavy_components(_state)
        _state["running"] = False

        with patch("amber.drone.mavlink.MavlinkController") as MockMavlink:
            MockMavlink.return_value = _make_controller(name="drone-1", host="udp://:14540", connect_ok=True)
            _init_pipeline(SourceConfig(
                source="mavlink",
                mavlink_address="udp://:14540",
                rtsp_url="rtsp://1.2.3.4:8554/camera",
            ))

            assert isinstance(_state["fleet"], DroneFleet)
            assert _state["source"] == "mavlink"

            _state["fleet"]._factory("drone-1", "udp://:14540")
            MockMavlink.assert_called_once_with(
                "drone-1", "udp://:14540", rtsp_url="rtsp://1.2.3.4:8554/camera"
            )

    def test_webcam_source_builds_no_fleet(self):
        """webcam/file/edge sources must not construct a DroneFleet at
        all — they only wire up local capture (or nothing, for edge)."""
        from amber.dashboard.app import SourceConfig, _init_pipeline, _state

        self._stub_heavy_components(_state)
        _state["running"] = False

        with patch("cv2.VideoCapture") as MockCap:
            _init_pipeline(SourceConfig(source="webcam"))

        assert _state["fleet"] is None
        MockCap.assert_called_once_with(0)

    def test_file_source_without_video_path_logs_error_and_no_capture(self):
        """PR #26 review fix: --source=file with no video path must not
        silently produce a dead pipeline — it should log an error and
        leave `cap` unset rather than calling cv2.VideoCapture(None)."""
        from amber.dashboard.app import SourceConfig, _init_pipeline, _state

        self._stub_heavy_components(_state)
        _state["running"] = False

        with patch("cv2.VideoCapture") as MockCap:
            _init_pipeline(SourceConfig(source="file", video_path=None))

        assert _state["fleet"] is None
        assert _state["cap"] is None
        _state["logger"].error.assert_called_once()
        MockCap.assert_not_called()
