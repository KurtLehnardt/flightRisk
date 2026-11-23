"""The single-drone search's start gate (``start_search``, ``flightrisk/dashboard/single_search.py``).

The fleet start gate refuses a drone whose edge reports its drone link down,
its obstacle sensor unable to look, or a battery that is unknown or below
``MIN_START_BATTERY``. A single-drone search reads the same facts from the
controller when it starts and refuses the same cases, and it refuses a video
frame that is not current (the decoder's placeholder, a stale frame, or one
older than ``MAX_FRAME_AGE_S``). No drone is contacted: the drones are test
doubles, and the Tello controller runs on the fake wire of ``tests/conftest.py``.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from flightrisk.config import DroneConfig
from flightrisk.dashboard import single_search
from flightrisk.dashboard.app import app, socketio
from flightrisk.dashboard.single_search import (
    MAX_BATTERY_AGE_S,
    MAX_FRAME_AGE_S,
    single_search_refusal,
)
from flightrisk.dashboard.state import app_state
from flightrisk.drone.controller import DroneLinkStatus, FrameFreshness
from flightrisk.drone.coordinator import MIN_START_BATTERY
from flightrisk.drone.fake import FakeDrone, FakeDroneConfig
from flightrisk.drone.obstacle_sensor import FrameStamp
from flightrisk.drone.snapshot import PERCENT, Reading, TelemetrySnapshot
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
    assert refusal == "battery 29% is below the 30% needed to start a search; charge or swap it"


def test_the_start_minimum_is_above_the_battery_monitors_critical_level():
    """A drone allowed to start must not be one the battery monitor lands at once."""
    assert MIN_START_BATTERY > DroneConfig().battery_critical_threshold


def test_a_critical_level_set_above_the_start_minimum_still_refuses(monkeypatch):
    monkeypatch.setattr(single_search, "_critical_battery_pct", lambda: 40.0)
    refusal = single_search_refusal(_drone(read_battery=35))
    assert refusal == ("battery 35% is at or below the 40% critical level, which lands the drone at once; "
                       "charge or swap it")
    assert single_search_refusal(_drone(read_battery=41)) is None


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


@pytest.mark.parametrize("battery", [None, -1, "80", float("nan"), True])
def test_an_unknown_battery_is_refused(battery):
    assert single_search_refusal(_drone(read_battery=battery)) == "no battery report from the drone"


def test_a_real_zero_percent_battery_is_zero_not_unknown():
    refusal = single_search_refusal(_drone(read_battery=0))
    assert refusal == "battery 0% is below the 30% needed to start a search; charge or swap it"


class _SnapshotDrone:
    """A controller with a telemetry snapshot and no ``read_battery``, like the MAVLink controller."""

    name = "uav-1"

    def __init__(self, battery_pct, measured_at):
        self.state = MagicMock(is_connected=True)
        self._reading = Reading(battery_pct, PERCENT, "mavlink.battery", measured_at)

    def snapshot(self):
        return TelemetrySnapshot(backend="mavlink", drone_id=self.name, captured_at=time.time(),
                                 connected=True, battery_pct=self._reading)

    def get_frame(self):
        return np.zeros((4, 4, 3), dtype=np.uint8)


def test_a_snapshot_battery_reading_must_be_recent():
    """A MAVLink battery stream that stopped leaves an old value behind; it is refused, not trusted."""
    assert single_search_refusal(_SnapshotDrone(80.0, time.time() - 1.0)) is None
    refusal = single_search_refusal(_SnapshotDrone(80.0, time.time() - MAX_BATTERY_AGE_S - 25))
    assert refusal is not None and refusal.startswith("the drone's last battery report is 30 s old;")
    assert "wait for a fresh report" in refusal
    real_zero = single_search_refusal(_SnapshotDrone(0.0, time.time()))
    assert real_zero == "battery 0% is below the 30% needed to start a search; charge or swap it"


def test_a_battery_that_cannot_be_read_is_refused():
    drone = _drone()
    drone.read_battery.side_effect = RuntimeError("no state")
    assert single_search_refusal(drone) == "no battery report from the drone"


def test_a_controller_without_read_battery_uses_its_snapshot():
    low = FakeDrone("sim-1", config=FakeDroneConfig(battery_pct=20.0))
    low.connect()
    assert single_search_refusal(low) == "battery 20% is below the 30% needed to start a search; charge or swap it"
    unknown = FakeDrone("sim-2", config=FakeDroneConfig(battery_pct=None))
    unknown.connect()
    assert single_search_refusal(unknown) == "no battery report from the drone"


def test_no_video_frame_is_refused_because_the_guard_cannot_look():
    refusal = single_search_refusal(_drone(get_frame=None))
    assert refusal == "no video frame from the drone, so the obstacle guard cannot check the path"
    drone = _drone()
    drone.get_frame.side_effect = RuntimeError("decoder gone")
    assert "video is unreadable (RuntimeError)" in single_search_refusal(drone)


@pytest.mark.parametrize("freshness, text", [
    (FrameFreshness(decoded=False), "no video frame decoded from the drone yet"),
    (FrameFreshness(decoded=True, age_s=0.1, stale_cause="decoder backlog"),
     "the drone's newest video frame cannot be trusted (decoder backlog)"),
    (FrameFreshness(decoded=True, age_s=MAX_FRAME_AGE_S + 0.5), "the drone's newest video frame is 1.5 s old"),
    (FrameFreshness(decoded=True, age_s=None), "the age of the drone's video frame is unknown"),
    ("fresh", "the age of the drone's video frame is unreadable"),
])
def test_a_frame_that_is_not_current_is_refused(freshness, text):
    refusal = single_search_refusal(_drone(frame_freshness=freshness))
    assert refusal is not None and refusal.startswith(text)
    assert "obstacle guard cannot check the path" in refusal


def test_a_frame_age_that_cannot_be_read_is_refused():
    drone = _drone()
    drone.frame_freshness.side_effect = RuntimeError("reader gone")
    assert single_search_refusal(drone).startswith("the age of the drone's video frame is unreadable (RuntimeError)")


def test_a_frame_just_under_the_age_limit_may_start():
    assert single_search_refusal(_drone(frame_freshness=FrameFreshness(True, MAX_FRAME_AGE_S - 0.01))) is None


# ---------------------------------------------------------------------------
# The Tello controller's frame age (djitellopy's reader and the bound reader)
# ---------------------------------------------------------------------------


class _DjitelloReader:
    """djitellopy's ``BackgroundFrameRead`` as the controller sees it: a ``frame`` and nothing else.

    It starts with a black placeholder, like djitellopy's, and keeps handing
    out the same array until ``decode`` puts a new one there.
    """

    def __init__(self):
        self.frame = np.zeros((300, 400, 3), dtype=np.uint8)

    def stop(self):
        """djitellopy's ``Tello.end()`` stops the reader when the client is closed."""

    def decode(self, value=1):
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        frame[150, 200] = value
        self.frame = frame


def _ready_tello(tello_on_wire, reader):
    """A real ``TelloController`` on the fake wire with *reader* as its video, and a healthy link and battery."""
    ctrl, _wire = tello_on_wire()
    ctrl.state.is_connected = True
    ctrl.tello.background_frame_read = reader
    ctrl.link_status = lambda: DroneLinkStatus(True, 0.1)
    ctrl.read_battery = lambda: 80
    return ctrl


def test_tello_placeholder_frame_before_the_first_decode_is_refused(tello_on_wire):
    reader = _DjitelloReader()
    ctrl = _ready_tello(tello_on_wire, reader)
    for _ in range(5):
        assert ctrl.get_frame() is reader.frame  # the dashboard shows it, but it is not a decoded frame
    assert ctrl.frame_freshness() == FrameFreshness(decoded=False)
    assert single_search_refusal(ctrl).startswith("no video frame decoded from the drone yet")
    reader.decode()
    assert single_search_refusal(ctrl) is None


def test_tello_frame_that_stopped_changing_is_refused_once_too_old(tello_on_wire, monkeypatch):
    reader = _DjitelloReader()
    ctrl = _ready_tello(tello_on_wire, reader)
    ctrl.get_frame()
    reader.decode(1)
    ctrl.get_frame()
    fresh = ctrl.frame_freshness()
    assert fresh.decoded and fresh.age_s is not None and fresh.age_s < MAX_FRAME_AGE_S
    # The stream stalls: get_frame keeps returning the last frame, which only grows older.
    later = time.monotonic() + 2.5
    monkeypatch.setattr("flightrisk.drone.tello.time.monotonic", lambda: later)
    refusal = single_search_refusal(ctrl)
    assert refusal is not None and refusal.startswith("the drone's newest video frame is 2.5 s old")
    reader.decode(2)
    assert single_search_refusal(ctrl) is None


@pytest.fixture
def bound_reader():
    pytest.importorskip("av")
    from flightrisk.drone.tello_bound import BoundFrameRead

    now = [100.0]
    reader = BoundFrameRead("127.0.0.1", "127.0.0.1", 0, name="test", clock=lambda: now[0])
    yield reader, now
    reader.stop()


def _publish(reader, captured_at, cause=None):
    """What the bound reader's decoder does with a decoded frame: the newest frame and its capture stamp."""
    array = np.zeros((300, 400, 3), dtype=np.uint8)
    with reader.lock:
        reader._frame = array
        reader._stamps.append((array, FrameStamp(captured_at=captured_at, stale_cause=cause)))
    reader.frames_decoded += 1
    return array


def test_bound_reader_placeholder_stale_and_old_frames_are_refused(tello_on_wire, bound_reader):
    reader, now = bound_reader
    ctrl = _ready_tello(tello_on_wire, reader)
    # Before the first decode the reader hands out its black placeholder, which has no stamp.
    assert ctrl.get_frame() is not None
    assert ctrl.frame_freshness() == FrameFreshness(decoded=False)
    assert single_search_refusal(ctrl).startswith("no video frame decoded from the drone yet")
    # A frame decoded behind a backlog is stale, whatever its age.
    _publish(reader, captured_at=now[0] - 0.05, cause="decoder backlog")
    assert single_search_refusal(ctrl).startswith("the drone's newest video frame cannot be trusted (decoder backlog)")
    # A clean frame is current until its capture stamp is older than the limit.
    _publish(reader, captured_at=now[0] - 0.05)
    assert single_search_refusal(ctrl) is None
    now[0] += 3.0
    assert single_search_refusal(ctrl).startswith("the drone's newest video frame is 3.0 s old")


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
    ({"frame_freshness": FrameFreshness(decoded=False)}, "no video frame decoded from the drone yet"),
    ({"frame_freshness": FrameFreshness(True, 4.0)}, "the drone's newest video frame is 4.0 s old"),
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
