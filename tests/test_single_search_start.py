"""The single-drone search's start gate (``start_search``, ``flightrisk/dashboard/single_search.py``).

The fleet start gate refuses a drone whose edge reports its drone link down,
its obstacle sensor unable to look, or a battery that is unknown or below
``MIN_START_BATTERY``. A single-drone search reads the same facts from the
controller when it starts and refuses the same cases. No drone is contacted:
the drones are test doubles.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from flightrisk.dashboard.app import app, socketio
from flightrisk.dashboard.single_search import single_search_refusal
from flightrisk.dashboard.state import app_state
from flightrisk.drone.controller import DroneLinkStatus
from flightrisk.drone.coordinator import MIN_START_BATTERY
from flightrisk.drone.fake import FakeDrone, FakeDroneConfig
from tests.single_drone_doubles import ready_single_drone

STALE = DroneLinkStatus(False, 12.0, "state_stale", "Tello unreachable (no state for 12 s)")


def _drone(**overrides):
    drone = ready_single_drone(MagicMock())
    drone.state.is_flying = True
    drone.move.return_value = True
    drone.rotate.return_value = True
    for name, value in overrides.items():
        getattr(drone, name).return_value = value
    return drone


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def test_a_healthy_drone_may_start():
    assert single_search_refusal(_drone()) is None


def test_the_battery_minimum_is_the_fleet_start_gates():
    assert MIN_START_BATTERY == 30
    assert single_search_refusal(_drone(read_battery=30)) is None
    refusal = single_search_refusal(_drone(read_battery=29))
    assert refusal == "battery 29% is below the 30% needed to start a search"


def test_a_drone_link_that_is_down_is_refused_with_the_controllers_text():
    refusal = single_search_refusal(_drone(link_status=STALE))
    assert refusal == "the drone link is down: Tello unreachable (no state for 12 s)"


def test_a_link_status_that_raises_or_is_not_a_status_counts_as_down():
    drone = _drone()
    drone.link_status.side_effect = OSError("network unreachable")
    assert single_search_refusal(drone) == "the drone link status is unreadable (OSError)"
    assert single_search_refusal(_drone(link_status="ok")) == "the drone link status is unreadable"


def test_a_controller_without_link_status_is_not_refused_for_it():
    """As on the edge: only a controller that reports a link status is gated on it."""
    drone = FakeDrone("sim-1")
    drone.connect()
    drone.takeoff()
    assert not hasattr(drone, "link_status")
    assert single_search_refusal(drone) is None


def test_a_controller_that_is_not_connected_is_refused():
    drone = _drone()
    drone.state.is_connected = False
    assert single_search_refusal(drone) == "the drone is not connected"


@pytest.mark.parametrize("battery", [None, 0, "80", float("nan"), True])
def test_an_unknown_battery_is_refused(battery):
    assert single_search_refusal(_drone(read_battery=battery)) == "no battery report from the drone"


def test_a_battery_that_cannot_be_read_is_refused():
    drone = _drone()
    drone.read_battery.side_effect = RuntimeError("no state")
    assert single_search_refusal(drone) == "no battery report from the drone"


def test_a_controller_without_read_battery_uses_its_snapshot():
    low = FakeDrone("sim-1", config=FakeDroneConfig(battery_pct=20.0))
    low.connect()
    assert single_search_refusal(low) == "battery 20% is below the 30% needed to start a search"
    unknown = FakeDrone("sim-2", config=FakeDroneConfig(battery_pct=None))
    unknown.connect()
    assert single_search_refusal(unknown) == "no battery report from the drone"


def test_no_video_frame_is_refused_because_the_guard_cannot_look():
    refusal = single_search_refusal(_drone(get_frame=None))
    assert refusal == "no video frame from the drone, so the obstacle guard cannot check the path"
    drone = _drone()
    drone.get_frame.side_effect = RuntimeError("decoder gone")
    assert "video is unreadable (RuntimeError)" in single_search_refusal(drone)


def test_the_checks_send_no_flight_command():
    drone = _drone(link_status=STALE)
    single_search_refusal(drone)
    single_search_refusal(_drone())
    for name in ("takeoff", "land", "move", "rotate", "hover", "rc_control"):
        getattr(drone, name).assert_not_called()


# ---------------------------------------------------------------------------
# The socket event
# ---------------------------------------------------------------------------


@pytest.fixture
def single(clean_app_state, monkeypatch):
    app_state.fleet_supervisor = None
    app_state.obstacle_guard = MagicMock(**{"check_path.return_value": {"safe": True, "action": "none",
                                                                       "center_depth": 2.0}})
    monkeypatch.setattr("flightrisk.dashboard.app.time.sleep", lambda s: None)
    client = socketio.test_client(app)
    client.get_received()

    def run(drone, data=None):
        app_state.fleet = MagicMock(primary=drone)
        client.emit("start_search", data or {"pattern": "parallel_track"})
        return client.get_received()

    yield run
    app_state.search_active = False
    time.sleep(0.05)
    client.disconnect()


def _errors(received):
    return [r["args"][0]["message"] for r in received if r["name"] == "error"]


@pytest.mark.parametrize("overrides, text", [
    ({"link_status": STALE}, "the drone link is down: Tello unreachable (no state for 12 s)"),
    ({"read_battery": 12}, "battery 12% is below the 30% needed to start a search"),
    ({"read_battery": None}, "no battery report from the drone"),
    ({"get_frame": None}, "no video frame from the drone"),
])
def test_start_search_refuses_with_a_clear_message_and_flies_nothing(single, overrides, text):
    drone = _drone(**overrides)
    received = single(drone)
    errors = _errors(received)
    assert errors and errors[0].startswith("Search not started: ") and text in errors[0]
    assert not any(r["name"] == "search_started" for r in received)
    assert app_state.search_active is False
    time.sleep(0.1)
    drone.move.assert_not_called()
    drone.rotate.assert_not_called()


def test_a_map_plan_is_refused_the_same_way(single, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_GEOFENCE_CM", "450")
    from tests.test_map_plan_dashboard import _spec

    drone = _drone(link_status=STALE)
    received = single(drone, {"pattern": "parallel_track", "plan": _spec(paths={"primary": [[0, 0], [0, 300]]})})
    assert any("drone link is down" in e for e in _errors(received))
    time.sleep(0.1)
    drone.move.assert_not_called()


def test_a_healthy_drone_still_starts_and_flies(single):
    drone = _drone()
    received = single(drone)
    assert [r["name"] for r in received if r["name"] == "search_started"] == ["search_started"]
    assert _errors(received) == []
    deadline = time.time() + 3
    while time.time() < deadline and not drone.move.called:
        time.sleep(0.01)
    assert drone.move.called


def test_a_drone_on_the_ground_keeps_its_own_message(single):
    drone = _drone(link_status=STALE)
    drone.state.is_flying = False
    assert _errors(single(drone)) == ["Drone must be flying to start search"]


def test_the_guard_is_checked_before_the_drone(single, monkeypatch):
    """An unavailable guard keeps its message; the drone's state is read last, just before the start."""
    monkeypatch.setattr("flightrisk.dashboard.app._obstacle_guard_for_search", lambda: (None, "MidasUnavailable: x"))
    drone = _drone(link_status=STALE)
    errors = _errors(single(drone))
    assert len(errors) == 1 and "obstacle guard is unavailable" in errors[0]
    drone.link_status.assert_not_called()


def test_a_frame_that_arrives_counts(single):
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    drone = ready_single_drone(_drone(get_frame=None), frame=frame)
    received = single(drone)
    assert any(r["name"] == "search_started" for r in received)
