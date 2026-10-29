"""The MAVLink fleet edge: ``--drone mavlink`` on the CLI and a MavlinkFleetDrone under the real LaneExecutor.

Everything runs against the simulated MAVSDK in ``tests.support.fake_mavsdk``
(no SITL, no autopilot, no radio). The Tello path is covered by its own tests
and must stay unchanged; the CLI tests here also check that every existing
form of ``--source`` still means what it meant.
"""
from __future__ import annotations

import argparse
import secrets
import sys
import threading
import time
import types

import numpy as np
import pytest

from flightrisk import edge_device
from flightrisk import fleet_protocol as fp
from flightrisk.drone.autopilot import ARDUPILOT
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.mavlink_fleet import MavlinkFleetDrone, MavlinkFleetOptions
from flightrisk.drone.mavsdk_link import MavsdkAutopilotLink
from flightrisk.drone.safety import FIELD_LIMITS, ROOM_LIMITS, SafetyEnvelope
from flightrisk.edge_device import (
    ResilientCaptureSource,
    _build_parser,
    _drone_kind,
    _resolve_drone_and_source,
    _startup_envelope,
)
from flightrisk.observability import StructuredLogger
from flightrisk.transport import GroundTransportSync
from tests.lane_executor_support import QuietLogger
from tests.support.fake_mavsdk import SimVehicle, install

WAIT_S = 15.0


def wait_for(predicate, timeout=WAIT_S, interval=0.02):
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result or time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def parse(argv):
    parser = _build_parser()
    args = parser.parse_args(argv)
    _resolve_drone_and_source(args, parser)
    return args


# ---------------------------------------------------------------------------
# CLI: --drone and --source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "source", "kind"),
    [
        ([], "camera", None),
        (["--source", "file", "--video", "x.mp4"], "file", None),
        (["--source", "tello"], "tello", "tello"),
        (["--source", "fake"], "fake", "fake"),
        (["--drone", "tello"], "tello", "tello"),
        (["--drone", "fake", "--source", "fake"], "fake", "fake"),
        (["--drone", "mavlink"], "camera", "mavlink"),
        (["--drone", "mavlink", "--source", "url", "--video-url", "rtsp://cam/1"], "url", "mavlink"),
        (["--drone", "mavlink", "--source", "file", "--video", "x.mp4"], "file", "mavlink"),
    ],
)
def test_drone_and_source_resolve(argv, source, kind):
    args = parse(argv)
    assert args.source == source and type(args.source) is str
    assert _drone_kind(args) == kind


def test_existing_parsing_is_unchanged():
    args = _build_parser().parse_args(["--source", "tello", "--alt-slot", "1.6"])
    assert args.source == "tello" and args.alt_slot == 160.0 and args.drone is None
    assert _build_parser().parse_args([]).source == "camera"
    assert _build_parser().parse_args([]).alt_slot == 100.0
    # A namespace built by hand (older callers) still works.
    assert _drone_kind(argparse.Namespace(source="fake")) == "fake"
    assert _drone_kind(argparse.Namespace(alt_slot=100.0)) is None


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--drone", "tello", "--source", "camera"], "takes its frames from the drone"),
        (["--drone", "mavlink", "--source", "tello"], "camera, file or url"),
        (["--drone", "mavlink", "--alt-slot", "120"], "use --alt-slot-m"),
        (["--drone", "mavlink", "--tello-host", "10.0.0.1"], "do not apply to --drone mavlink"),
        (["--source", "fake", "--alt-slot-m", "15"], "only apply to --drone mavlink"),
        (["--mavlink-address", "udpin://0.0.0.0:14540"], "only apply to --drone mavlink"),
        (["--drone", "fake", "--allow-unverified-failsafes"], "only apply to --drone mavlink"),
        (["--drone", "mavlink", "--speed-mps", "40"], "speed_mps"),
    ],
)
def test_invalid_combinations_are_refused(argv, message, capsys):
    with pytest.raises(SystemExit):
        parse(argv)
    assert message in capsys.readouterr().err


def test_url_source_needs_a_url(capsys):
    with pytest.raises(SystemExit):
        edge_device.main(["--drone", "mavlink", "--source", "url"])
    assert "--video-url is required" in capsys.readouterr().err


def test_field_envelope_defaults_flags_and_environment():
    env = {"FLIGHTRISK_CEILING_CM": "200", "FLIGHTRISK_GEOFENCE_CM": "450"}  # room settings never leak outdoors
    envelope = _startup_envelope(parse(["--drone", "mavlink"]), environ=env)
    assert envelope.limits is FIELD_LIMITS
    assert (envelope.alt_slot_cm, envelope.ceiling_cm, envelope.ceiling_margin_cm, envelope.geofence_cm) == (
        1500, 3000, 500, 3000)
    envelope = _startup_envelope(
        parse(["--drone", "mavlink", "--alt-slot-m", "20", "--ceiling-m", "40", "--geofence-m", "80"]),
        environ={"FLIGHTRISK_FIELD_CEILING_MARGIN_M": "6", "FLIGHTRISK_MIN_TAKEOFF_BATTERY": "50"},
    )
    assert (envelope.alt_slot_cm, envelope.ceiling_cm, envelope.ceiling_margin_cm, envelope.geofence_cm) == (
        2000, 4000, 600, 8000)
    assert envelope.min_takeoff_battery == 50
    # The Tello path still reads the room variables.
    room = _startup_envelope(parse(["--source", "fake"]), environ=env)
    assert room.limits is ROOM_LIMITS and room.ceiling_cm == 200 and room.geofence_cm == 450


@pytest.mark.parametrize(
    "argv",
    [
        ["--alt-slot-m", "28"],  # above the 30 - 5 = 25 m limit
        ["--ceiling-m", "150"],  # above 120 m
        ["--ceiling-margin-m", "1"],
        ["--geofence-m", "900"],
        ["--alt-slot-m", "2"],  # below the 5 m minimum
    ],
)
def test_unsafe_field_limits_refuse_the_start(argv):
    with pytest.raises(SystemExit, match="Refusing to start"):
        _startup_envelope(parse(["--drone", "mavlink", *argv]), environ={})


def test_mavlink_address_and_failsafe_action_resolution(monkeypatch):
    args = parse(["--drone", "mavlink"])
    assert edge_device._mavlink_address(args, environ={}) == "udpin://0.0.0.0:14540"
    assert edge_device._mavlink_address(args, environ={"FLIGHTRISK_MAVLINK_ADDRESS": "serial:///dev/ttyAMA0:921600"}) \
        == "serial:///dev/ttyAMA0:921600"
    args = parse(["--drone", "mavlink", "--mavlink-address", "tcpout://127.0.0.1:5760"])
    assert edge_device._mavlink_address(args, environ={"FLIGHTRISK_MAVLINK_ADDRESS": "x"}) == "tcpout://127.0.0.1:5760"
    assert edge_device._failsafe_action(parse(["--drone", "mavlink"]), environ={}) == "rtl"
    assert edge_device._failsafe_action(parse(["--drone", "mavlink"]), environ={"FLIGHTRISK_FAILSAFE_ACTION": "land"}) \
        == "land"
    with pytest.raises(ValueError, match="rtl or land"):
        edge_device._failsafe_action(parse(["--drone", "mavlink"]), environ={"FLIGHTRISK_FAILSAFE_ACTION": "hover"})


def test_build_drone_makes_a_mavlink_fleet_drone():
    args = parse(["--drone", "mavlink", "--mavlink-address", "tcpout://127.0.0.1:1", "--autopilot", "px4",
                  "--frame-heading-deg", "0", "--failsafe-action", "land", "--drone-id", "drone-3"])
    drone = edge_device._build_drone(args, StructuredLogger(component="test"))
    assert isinstance(drone, MavlinkFleetDrone)
    assert drone.name == "drone-3" and drone.host == "tcpout://127.0.0.1:1"
    assert drone.options.failsafe_action == "land" and drone.options.frame_heading_deg == 0
    assert drone.options.require_failsafes is True
    assert drone.link._configured_autopilot == "px4"


class _FlakyCapture:
    def __init__(self, script):
        self.script = script
        self.released = False

    def read(self):
        return self.script.pop(0) if self.script else (False, None)

    def release(self):
        self.released = True


def test_resilient_capture_reopens_and_stops_only_on_request():
    frame = np.zeros((4, 4, 3), np.uint8)
    opened = []
    scripts = [[(False, None)], [(True, frame), (False, None)], [(True, frame)]]

    def _open():
        cap = _FlakyCapture(scripts.pop(0) if scripts else [])
        opened.append(cap)
        return cap

    stop = threading.Event()
    source = ResilientCaptureSource(_open, stop, min_interval_s=0.0, retry_s=0.0)
    assert next(source) is frame
    assert next(source) is frame
    assert len(opened) == 3 and opened[0].released and opened[1].released
    stop.set()
    with pytest.raises(StopIteration):
        next(source)


# ---------------------------------------------------------------------------
# The real LaneExecutor on a MavlinkFleetDrone
# ---------------------------------------------------------------------------


class Edge:
    """A MavlinkFleetDrone on a simulated autopilot under a real LaneExecutor; records what it sends."""

    def __init__(self, monkeypatch, vehicle=None, *, link_loss_land_s=30.0, geofence_cm=3000, **options):
        self.vehicle = vehicle or SimVehicle(ARDUPILOT, time_scale=20.0)
        install(monkeypatch, self.vehicle)
        self.drone = MavlinkFleetDrone("drone-1", MavsdkAutopilotLink("tcpout://127.0.0.1:1"),
                                       MavlinkFleetOptions(frame_heading_deg=0.0, **options))
        self.envelope = SafetyEnvelope.for_field(geofence_cm=geofence_cm, link_loss_land_s=link_loss_land_s)
        self.drone.bind_slot_source(lambda: self.envelope.alt_slot_cm)
        self.sent: list[dict] = []
        self.log = QuietLogger()
        self.executor = LaneExecutor(self.drone, self.envelope, "drone-1", send=self._send, logger=self.log,
                                     telemetry_interval_s=0.1)
        assert self.drone.connect()
        self.executor.start()

    def _send(self, msg):
        fp.validate_message(msg)
        self.sent.append(msg)

    def ready(self):
        return wait_for(lambda: self.drone.link_status().ok)

    def of(self, mtype, for_=None):
        return [m for m in list(self.sent) if m["type"] == mtype and (for_ is None or m.get("for") == for_)]

    def handle(self, msg):
        self.executor.note_ground_message()
        self.executor.handle_message(msg, recv_t=self.executor.now())

    def close(self):
        self.executor.shutdown(timeout=5.0)
        self.drone.disconnect()


@pytest.fixture
def make_edge(monkeypatch):
    edges = []

    def _make(*args, **kwargs):
        edge = Edge(monkeypatch, *args, **kwargs)
        edges.append(edge)
        return edge

    yield _make
    for edge in edges:
        edge.close()


LANES = [
    {"direction": "forward", "distance_cm": 2000},
    {"direction": "right", "distance_cm": 800},
    {"direction": "back", "distance_cm": 2000},
]


def start_lanes(edge, lanes=LANES, alt_cm=1500):
    edge.handle({"type": "assign_lanes", "drone_id": "drone-1", "alt_cm": alt_cm, "lanes": lanes})
    assert edge.of("ack", "assign_lanes"), edge.of("nack")
    edge.handle({"type": "clock", "offset_s": 0.0})
    edge.handle({"type": "start_at", "t_ground_s": edge.executor.now() + 0.05})


def test_field_lanes_fly_with_measured_pose_in_telemetry(make_edge):
    edge = make_edge()
    assert edge.ready(), edge.drone.link_status()
    start_lanes(edge)
    assert wait_for(lambda: edge.of("ack", "start_at"))
    assert wait_for(lambda: edge.executor.lane_index == 3 and not edge.executor.is_busy), edge.log.names()
    # Up 20 m north, 8 m east, back south: ends 8 m east of the takeoff point at the 15 m slot.
    assert edge.vehicle.north == pytest.approx(0.0, abs=1.0)
    assert edge.vehicle.east == pytest.approx(8.0, abs=1.0)
    assert edge.vehicle.alt == pytest.approx(15.0, abs=0.7)
    telemetry = wait_for(lambda: [m for m in edge.of("telemetry") if m["is_flying"] and m["lane_index"] == 3])
    last = telemetry[-1]
    assert last["rel_x_cm"] == pytest.approx(800, abs=100) and last["rel_y_cm"] == pytest.approx(0, abs=100)
    assert last["height_cm"] == pytest.approx(1500, abs=70)
    assert last["lat"] == pytest.approx(edge.vehicle.home_lat, abs=1e-4)
    assert last["drone_link_ok"] is True
    assert last["alt_slot_cm"] == 1500 and last["ceiling_cm"] == 3000
    edge.handle({"type": "land"})
    assert wait_for(lambda: not edge.drone.state.is_flying)
    assert ("land",) in edge.vehicle.commands


def test_takeoff_refused_when_the_autopilot_failsafes_are_not_configured(make_edge):
    vehicle = SimVehicle(ARDUPILOT, time_scale=20.0)
    vehicle.params["FENCE_ENABLE"] = 0
    edge = make_edge(vehicle)
    assert wait_for(lambda: "FENCE_ENABLE=0" in (edge.drone.link_status().detail or ""))
    start_lanes(edge)
    # A v1 start is refused in the edge log (a v2 start also gets a drone_unreachable nack with the detail).
    assert wait_for(lambda: "edge_start_refused" in edge.log.names())
    assert not edge.of("ack", "start_at")
    assert vehicle.commands == []
    assert wait_for(lambda: [m for m in edge.of("telemetry")
                             if m.get("drone_link_ok") is False and "FENCE_ENABLE=0" in m["drone_link_detail"]])
    # Landing is never gated by the preflight: on the ground it succeeds and sends nothing.
    edge.handle({"type": "emergency_land_all"})
    assert edge.of("ack", "emergency_land_all") or edge.log.names()
    assert vehicle.commands == []


def test_ground_link_loss_returns_to_launch(make_edge):
    edge = make_edge(link_loss_land_s=0.6)
    assert edge.ready()
    edge.handle({"type": "takeoff"})
    assert wait_for(lambda: "edge_takeoff" in edge.log.names())
    assert wait_for(lambda: ("return_to_launch",) in edge.vehicle.commands), edge.log.names()
    assert "edge_link_lost" in edge.log.names()
    assert wait_for(lambda: not edge.drone.state.is_flying)
    assert ("land",) not in edge.vehicle.commands  # the retries never turned the return into a landing


def test_measured_geofence_breach_returns_to_launch(make_edge):
    edge = make_edge(geofence_cm=1000)
    assert edge.ready()
    edge.handle({"type": "takeoff"})
    assert wait_for(lambda: "edge_takeoff" in edge.log.names() and not edge.executor.is_busy)
    edge.vehicle.wind_drift_m(north=20.0, east=0.0)  # pushed 20 m out of a 10 m fence
    assert wait_for(lambda: ("return_to_launch",) in edge.vehicle.commands), edge.log.names()
    assert "edge_geofence_breach" in edge.log.names()


def test_slots_above_the_field_ceiling_are_refused(make_edge):
    edge = make_edge()
    edge.handle({"type": "assign_lanes", "drone_id": "drone-1", "alt_cm": 2800, "lanes": LANES})
    nack = edge.of("nack", "assign_lanes")
    assert nack and nack[0]["reason"] == "altitude"
    edge.handle({"type": "assign_lanes", "drone_id": "drone-1", "alt_cm": 2000, "lanes": LANES})
    assert edge.of("ack", "assign_lanes")
    assert edge.envelope.alt_slot_cm == 2000


def test_room_scale_ceiling_is_refused_on_a_field_drone(make_edge):
    edge = make_edge()
    edge.executor.handle_set_ceiling({"type": "set_ceiling", "cmd_id": "c-1", "ceiling_cm": 200,
                                      "margin_cm": 60, "alt_cm": 100})
    replies = [m for m in edge.sent if m.get("for") == "set_ceiling"]
    assert replies and replies[-1]["type"] == "nack"
    assert edge.envelope.ceiling_cm == 3000


def test_pilot_takeover_mid_lane_is_never_fought(make_edge):
    edge = make_edge(speed_mps=1.0)
    assert edge.ready()
    edge.vehicle.time_scale = 2.0
    start_lanes(edge)
    assert wait_for(lambda: edge.vehicle.alt > 14.0 and edge.vehicle.north > 1.0)
    edge.vehicle.set_pilot_mode("POSCTL")
    assert wait_for(lambda: not edge.executor.is_busy)
    commands = list(edge.vehicle.commands)
    time.sleep(0.5)
    assert edge.vehicle.commands == commands
    assert ("land",) not in commands and ("return_to_launch",) not in commands
    telemetry = wait_for(lambda: [m for m in edge.of("telemetry") if m.get("drone_link_ok") is False])
    assert "POSCTL" in telemetry[-1]["drone_link_detail"]
    # The operator can still land it explicitly.
    edge.handle({"type": "emergency_land_all"})
    assert wait_for(lambda: ("land",) in edge.vehicle.commands)


def test_manual_move_steps_work_at_field_scale(make_edge):
    edge = make_edge()
    assert edge.ready()
    edge.handle(fp.make_manual_hold(drone_id="drone-1", cmd_id="m-1"))
    edge.handle(fp.make_manual_takeoff(drone_id="drone-1", cmd_id="m-2"))
    assert wait_for(lambda: edge.of("ack", "manual_takeoff")), edge.of("nack")
    assert wait_for(lambda: not edge.executor.is_busy)
    north = edge.vehicle.north
    edge.handle(fp.make_manual_move("forward", distance_cm=100, drone_id="drone-1", cmd_id="m-3"))
    assert wait_for(lambda: edge.of("ack", "manual_move")), edge.of("nack")
    assert edge.vehicle.north == pytest.approx(north + 1.0, abs=0.4)


# ---------------------------------------------------------------------------
# The whole flying edge in-process: --drone mavlink, a real ground transport
# ---------------------------------------------------------------------------


class _FakeDetector:
    def __init__(self, *args, **kwargs):
        pass

    def detect(self, frame):
        return [{"bbox": [10, 10, 40, 80], "confidence": 0.9}]


class _FakeReID:
    def __init__(self, *args, **kwargs):
        pass

    def extract_embedding(self, crop):
        return np.ones(4, np.float32)


class _StillCapture:
    def __init__(self, *args, **kwargs):
        pass

    def read(self):
        time.sleep(0.05)
        return True, np.zeros((120, 160, 3), np.uint8)

    def release(self):
        pass


def test_mavlink_edge_end_to_end_over_the_ground_transport(monkeypatch):
    import cv2

    for name, attr, cls in (
        ("flightrisk.vision.detector", "PersonDetector", _FakeDetector),
        ("flightrisk.vision.reid", "PersonReID", _FakeReID),
    ):
        module = types.ModuleType(name)
        setattr(module, attr, cls)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(cv2, "VideoCapture", _StillCapture)
    monkeypatch.setattr(edge_device, "start_local_land_key", lambda *a, **k: None)
    vehicle = SimVehicle(ARDUPILOT, time_scale=20.0)
    install(monkeypatch, vehicle)
    token = secrets.token_hex(16)
    monkeypatch.setenv("FLIGHTRISK_EDGE_TOKEN", token)

    controls: list[dict] = []
    detections: list = []
    ground = GroundTransportSync(host="127.0.0.1", port=0, token=token, on_control=controls.append,
                                 on_message=detections.append)
    ground.start()
    stop = threading.Event()
    args = parse(["--drone", "mavlink", "--source", "camera", "--mavlink-address", "tcpout://127.0.0.1:1",
                  "--drone-id", "drone-1", "--frame-heading-deg", "0", "--ws-url", f"ws://127.0.0.1:{ground.port}"])
    envelope = _startup_envelope(args, environ={})
    edge = threading.Thread(target=edge_device._run_flight_edge,
                            args=(args, StructuredLogger(component="test-mavlink-edge"), stop, envelope),
                            daemon=True)
    edge.start()
    try:
        assert wait_for(lambda: "drone-1" in ground.drone_ids())
        assert wait_for(lambda: any(m.get("type") == "telemetry" and m.get("drone_link_ok") for m in list(controls)))
        ground.sync_clock("drone-1")
        ground.send_to_drone("drone-1", {"type": "assign_lanes", "drone_id": "drone-1", "alt_cm": 1500,
                                         "lanes": [{"direction": "forward", "distance_cm": 1000}],
                                         "zone": {"x_cm": 0, "y_cm": 0, "w_cm": 1000, "h_cm": 1000}})
        assert wait_for(lambda: any(m.get("for") == "assign_lanes" for m in list(controls)))
        assert [m for m in controls if m.get("for") == "assign_lanes"][-1]["type"] == "ack"
        ground.send_to_drone("drone-1", {"type": "start_at", "t_ground_s": ground.now() + 0.5})
        assert wait_for(lambda: vehicle.north > 9.0)
        assert wait_for(lambda: [m for m in list(detections) if m.detections and m.lat is not None])
        moving = [m for m in list(detections) if m.detections and m.lat is not None]
        assert moving[-1].drone_id == "drone-1" and moving[-1].rel_y_cm is not None
        ground.send_to_drone("drone-1", fp.make_land())
        assert wait_for(lambda: ("land",) in vehicle.commands)
    finally:
        stop.set()
        edge.join(20.0)
        ground.stop()
    assert not edge.is_alive()
