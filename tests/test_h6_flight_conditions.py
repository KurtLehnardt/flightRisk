"""Tests for the round 2 review of PR #177: the conditions for the first real H6 flights.

H6 flies on a Windows edge laptop in Layout B (the ground and two Tello edges, four
cores, no GPU). The findings:

N1. The recorder called the decode lag the capture-to-arrival latency, so the
    settle time would be sized from about 50 ms when the video latency is about
    300 ms. It is ``decode_lag_ms`` now, a replay still reads the old column,
    and the H5 latency is measured against a clock filmed on the edge laptop.
N2. The person cue never reached the ground's status, its logs or the drone card.
N3. A Tello edge without ``--tello-local-ip`` gives its frames no capture stamps;
    with sensing required it now refuses to start.
N4/N8. Each Tello frame got a strided RGB-to-BGR copy for the sensor while YOLO,
    ReID and the JPEGs got the RGB frame as if it were BGR. Now one conversion
    per frame serves every consumer.
N5. The decoder's stale limit is ``FLIGHTRISK_MAX_DECODE_LAG_S``.
N6. A lost drone holds re-task paths for 20 s, the 15 s hover plus the descent.
N7. The takeoff time is stamped after the takeoff probe, and the probe shows on
    the SENSE badge.
N10. A stale frame keeps the cause that tainted the stream (in
    ``tests/test_obstacle_h6_fixes.py``, next to the decoder's other tests).

Fakes only: no drone, no network, no model weights.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from flightrisk import obs_events
from flightrisk.drone import tello_bound
from flightrisk.drone.coordinator import LOST_OBSTACLE_S
from flightrisk.drone.lane_flight import EVENT_TAKEOFF_SENSE
from flightrisk.drone.obstacle_sensor import (
    CHANNEL_ORDER_BGR,
    CHANNEL_ORDER_RGB,
    CUE_DEPTH,
    CUT_OBSTACLE,
    ENV_MAX_DECODE_LAG_S,
    ENV_OBSTACLE_SENSING,
    MAX_DECODE_LAG_S,
    OBSTACLE_STATE_UNAVAILABLE,
    STALE_CAPTURE_UNKNOWN,
    TELEMETRY_LATENCY_MS,
    TELEMETRY_PERSON_CUE,
    TELEMETRY_STATE,
    FrameStamp,
    LatestFrame,
    SenseResult,
)
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController
from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import (
    DecodedFramePoller,
    DroneFrameSource,
    EdgeFrames,
    ObstacleSettings,
    _build_parser,
    _check_capture_stamps,
    _decoder_frame_stamp,
    _flight_frame_source,
    feed_sensor_holder,
)
from scripts import obstacle_record, obstacle_replay
from tests import test_lane_executor_obstacle as obstacle_tests
from tests.lane_executor_support import LANES, wait_for
from tests.test_coordinator_obstacle import ready_fleet
from tests.test_coordinator_reflight import LANES as REFLIGHT_PLAN_LANES
from tests.test_coordinator_reflight import REFLIGHT, TRANSIT, Fleet
from tests.test_lane_executor_obstacle import answered, events, finished, start
from tests.test_obstacle_h6_fixes import StampingReader, TickClock, _rgb_scene

rigs = obstacle_tests.rigs

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "obstacle-sensing.md").read_text()


class StampedTello(FakeTelloController):
    """A fake Tello whose decoder stamps its frames (``BoundFrameRead``): a new RGB array per poll."""

    def __init__(self, reader: StampingReader, picture: np.ndarray, clock):
        super().__init__(name="drone-1")
        self.tello = SimpleNamespace(background_frame_read=reader)
        self.reader, self.picture, self.clock = reader, picture, clock

    def get_frame(self):
        if not self.state.is_connected:
            return None
        self.reader.decode(self.picture, self.clock())
        frame = self.reader.frame
        for cb in self._frame_callbacks:
            cb(frame)
        return frame


# ---------------------------------------------------------------------------
# N1: the recorder reports the decode lag, and H5 measures the real latency
# ---------------------------------------------------------------------------


def test_n1_the_recorder_writes_the_decode_lag_and_a_wall_clock_not_a_latency(tmp_path):
    clock = TickClock(50.0)
    reader = StampingReader(clock)
    drone = StampedTello(reader, _rgb_scene(), lambda: clock() - 0.04)  # decoded 40 ms after its first datagram
    drone.connect()
    out = tmp_path / "rec"

    def sleep(_s):
        clock.advance(0.05)

    meta = obstacle_record.record(drone, out, 0.1, clock=clock, sleep=sleep)
    assert meta["frames_written"] >= 1
    with open(out / "frames.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert "capture_to_arrival_ms" not in rows[0]
    assert {float(r["decode_lag_ms"]) for r in rows} == {40.0}
    for row in rows:
        assert row["wall_clock"] == obstacle_record.clock_text(float(row["wall_time"]))


def test_n1_clock_text_is_local_time_to_the_millisecond():
    wall = time.mktime((2026, 11, 21, 14, 3, 21, 0, 0, -1)) + 0.5129
    assert obstacle_record.clock_text(wall) == "14:03:21.512"


def test_n1_the_clock_mode_needs_no_recording_and_a_recording_still_needs_out():
    assert obstacle_record.parse_args(["--clock"]).clock is True
    with pytest.raises(SystemExit):
        obstacle_record.parse_args(["--seconds", "3"])


def test_n1_main_runs_the_clock_without_a_drone(monkeypatch):
    shown = []
    monkeypatch.setattr(obstacle_record, "show_clock", lambda: shown.append(True))
    assert obstacle_record.main(["--clock"]) == 0 and shown == [True]


@pytest.mark.parametrize("column", ["decode_lag_ms", "capture_to_arrival_ms"])
def test_n1_the_replay_reads_the_decode_lag_under_its_new_and_its_old_name(tmp_path, column):
    import cv2

    cv2.imwrite(str(tmp_path / "frame_000001.png"), _rgb_scene())
    with open(tmp_path / "frames.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["index", "file", "captured_at_s", column])
        writer.writeheader()
        writer.writerow({"index": 1, "file": "frame_000001.png", "captured_at_s": "9.5", column: "51.5"})
    guard = SimpleNamespace(check_path=lambda frame: {"safe": True})
    (result,) = obstacle_replay.run_replay(None, 1, 0.35, frames_dir=str(tmp_path), guard=guard)
    assert result["decode_lag_ms"] == 51.5 and result["captured_at_s"] == 9.5


def test_n1_the_h5_check_documents_the_filmed_clock_and_no_longer_reads_the_decode_lag_as_latency():
    h5 = next(line for line in DOC.splitlines() if line.startswith("| H5 |"))
    assert "--clock" in h5 and "wall_clock" in h5 and "decode_lag_ms" in h5
    assert "capture_to_arrival_ms" not in h5


# ---------------------------------------------------------------------------
# N2: the person cue reaches the ground's status, its log and the drone card
# ---------------------------------------------------------------------------


def test_n2_status_carries_the_person_cue_of_a_drone_with_obstacle_sensing():
    coord, edges, _ = ready_fleet()
    edges["drone-1"].telemetry(obstacle_sensing="stepped", obstacle_sensor_ok=True,
                               obstacle_person_cue={"person_cue_down": True, "detection_age_s": 6.5, "x": 1})
    edges["drone-2"].telemetry(obstacle_sensing="stepped", obstacle_sensor_ok=True)
    status = coord.status()
    assert status["drone-1"][TELEMETRY_PERSON_CUE] == {"person_cue_down": True, "detection_age_s": 6.5}
    assert status["drone-2"][TELEMETRY_PERSON_CUE] is None


@pytest.mark.parametrize("bad", ["down", {"person_cue_down": "yes"}, {"detection_age_s": 1.0}, 3])
def test_n2_a_malformed_person_cue_is_not_passed_on(bad):
    coord, edges, _ = ready_fleet()
    edges["drone-1"].telemetry(obstacle_sensing="stepped", obstacle_sensor_ok=True, obstacle_person_cue=bad)
    assert coord.status()["drone-1"][TELEMETRY_PERSON_CUE] is None


def test_n2_the_ground_logs_the_person_cue_going_down_once_and_coming_back(caplog):
    _coord, edges, _ = ready_fleet()
    caplog.set_level(logging.INFO, logger="flightrisk.drone.coordinator")
    down = {"person_cue_down": True, "detection_age_s": 4.25}
    for _ in range(3):
        edges["drone-1"].telemetry(obstacle_sensing="stepped", obstacle_person_cue=down)
    edges["drone-1"].telemetry(obstacle_sensing="stepped",
                               obstacle_person_cue={"person_cue_down": False, "detection_age_s": 0.2})
    lines = [r.getMessage() for r in caplog.records if "person cue" in r.getMessage()]
    assert lines == [
        "drone-1's obstacle person cue is down (newest detection 4.2 s old): its gates rely on the depth cue alone",
        "drone-1's obstacle person cue works again",
    ]


def test_n2_the_ground_telemetry_snapshot_carries_the_person_cue(monkeypatch):
    logged = []
    monkeypatch.setattr(obs_events, "log_event", lambda _log, _level, event, **fields: logged.append(fields))
    monkeypatch.setattr(obs_events, "_snapshot_throttle", SimpleNamespace(allow=lambda key: (True, 0)))
    obs_events.edge_telemetry("drone-1", {"type": "telemetry", "battery": 80,
                                          "obstacle_person_cue": {"person_cue_down": True, "detection_age_s": 3.0}})
    (fields,) = logged
    assert fields["readings"]["obstacle_person_cue_down"] is True
    assert fields["readings"]["obstacle_detection_age_s"] == 3.0


# The drone card's badge: tests/test_fleet_dashboard_obstacle.py (TestBrowserHelpers, under Node).


# ---------------------------------------------------------------------------
# N3: no capture stamps, no start while sensing is required
# ---------------------------------------------------------------------------


def _args(*argv):
    return _build_parser().parse_args(list(argv))


def test_n3_a_required_sensor_refuses_a_tello_edge_without_capture_stamps():
    with pytest.raises(SystemExit) as info:
        _check_capture_stamps(_args("--source", "tello"), {})
    text = str(info.value)
    assert "--tello-local-ip" in text and "capture stamps" in text and "Refusing to start" in text
    with pytest.raises(SystemExit):
        _check_capture_stamps(_args("--drone", "tello"), {ENV_OBSTACLE_SENSING: "required"})


def test_n3_main_refuses_before_anything_starts(monkeypatch):
    from flightrisk import edge_device

    monkeypatch.delenv(ENV_OBSTACLE_SENSING, raising=False)
    monkeypatch.delenv("FLIGHTRISK_TELLO_LOCAL_IP", raising=False)
    monkeypatch.setattr(edge_device, "_run_flight_edge", lambda *a, **k: pytest.fail("the edge started"))
    monkeypatch.setattr(edge_device, "_limit_torch_threads", lambda *a, **k: pytest.fail("models were prepared"))
    with pytest.raises(SystemExit, match="--tello-local-ip"):
        edge_device.main(["--source", "tello"])


def test_n3_sensing_off_only_warns_and_a_bound_or_fake_edge_is_fine():
    problem = _check_capture_stamps(_args("--source", "tello"), {ENV_OBSTACLE_SENSING: "off"})
    assert problem is not None and "capture stamps" in problem
    assert _check_capture_stamps(_args("--source", "tello", "--tello-local-ip", "192.168.10.2"), {}) is None
    assert _check_capture_stamps(_args("--source", "fake"), {}) is None


def test_n3_main_logs_the_warning_when_sensing_is_off(monkeypatch):
    from flightrisk import edge_device

    warned = []
    monkeypatch.setenv(ENV_OBSTACLE_SENSING, "off")
    monkeypatch.delenv("FLIGHTRISK_TELLO_LOCAL_IP", raising=False)
    monkeypatch.setattr(edge_device, "_run_flight_edge", lambda *a, **k: None)
    monkeypatch.setattr(edge_device, "_init_edge_telemetry", lambda log: False)
    monkeypatch.setattr(edge_device.StructuredLogger, "warning",
                        lambda self, event, **fields: warned.append(event))
    edge_device.main(["--source", "tello"])
    assert edge_device.EVENT_NO_CAPTURE_STAMPS in warned


# ---------------------------------------------------------------------------
# N4 and N8: one BGR conversion per frame, for every consumer
# ---------------------------------------------------------------------------


class _SpyDetector:
    """Records the frame it is given and finds one person in the middle."""

    def __init__(self):
        self.frames: list[np.ndarray] = []

    def detect(self, frame):
        self.frames.append(frame)
        return [{"bbox": [8, 4, 40, 28], "confidence": 0.9}]


class _SpyReID:
    def __init__(self):
        self.crops: list[np.ndarray] = []

    def extract_embedding(self, crop):
        self.crops.append(np.array(crop, copy=True))
        return np.zeros(4, np.float32)


class _Decoder:
    """A decoder whose newest frame the poller reads and the controller hands out: one new RGB array per decode."""

    with_queue = False

    def __init__(self, picture):
        self.picture = picture
        self.frame = None

    def decode(self):
        self.frame = self.picture.copy()


class DecoderTello(FakeTelloController):
    """A fake Tello whose ``get_frame`` returns its decoder's newest frame, as ``TelloController`` does."""

    def __init__(self, picture):
        super().__init__(name="drone-1")
        self.decoder = _Decoder(picture)
        self.tello = SimpleNamespace(background_frame_read=self.decoder)

    def get_frame(self):
        if not self.state.is_connected or self.decoder.frame is None:
            return None
        frame = self.decoder.frame
        for cb in self._frame_callbacks:
            cb(frame)
        return frame


def test_n4_yolo_reid_and_the_jpegs_get_bgr_from_a_tello():
    """Before the fix a Tello's RGB frame went to YOLO, ReID and the JPEG encoder, which all take BGR."""
    import cv2

    picture = _rgb_scene()
    drone = DecoderTello(picture)
    drone.connect()
    drone.decoder.decode()
    detector, reid = _SpyDetector(), _SpyReID()
    runner = EdgeRunner(detector=detector, reid=reid, drone_id="drone-1", stream_video=True)
    source = _flight_frame_source(_args("--source", "tello"), drone, threading.Event(), None)
    msg = runner.process_frame(next(source))
    bgr = picture[..., ::-1]
    assert np.array_equal(detector.frames[0], bgr)
    assert np.array_equal(reid.crops[0], bgr[4:28, 8:40])
    # The crop JPEG decodes (as BGR) to the true colours: a red left half stays red.
    crop = cv2.imdecode(np.frombuffer(msg.detections[0].crop_jpeg, np.uint8), cv2.IMREAD_COLOR)
    left = crop[:, :4].reshape(-1, 3).mean(axis=0)
    assert left[2] > 150 and left[0] < 80  # B, G, R: red is high, blue is low


def test_n4_one_conversion_per_frame_is_shared_by_the_frame_loop_and_both_sensor_feeds():
    picture = _rgb_scene()
    drone = DecoderTello(picture)
    frames = EdgeFrames.for_drone(drone)
    holder = LatestFrame()
    feed_sensor_holder(drone, holder, frames)
    poller = DecodedFramePoller.for_drone(drone, holder, frames=frames)
    source = DroneFrameSource(drone, frames=frames, min_interval_s=0.0)
    drone.connect()
    for n in range(1, 4):
        drone.decoder.decode()
        poller.poll_once()  # the poller sees the new frame first ...
        given = next(source)  # ... and the frame loop and its on_frame feed get the same conversion
        assert frames.conversions == n
        assert holder.latest().frame is given
        assert np.array_equal(given, picture[..., ::-1]) and not given.flags.writeable


def test_n4_the_frame_loops_bgr_frames_keep_their_capture_stamps():
    """The decoder knows its own arrays only: a converted frame is looked up as the frame it came from."""
    clock = TickClock(30.0)
    reader = StampingReader(clock)
    drone = StampedTello(reader, _rgb_scene(), clock)
    drone.connect()
    frames = EdgeFrames.for_drone(drone)
    runner = EdgeRunner(detector=_SpyDetector(), drone_id="drone-1")
    runner.frame_stamp = _decoder_frame_stamp(drone, frames)
    bgr = DroneFrameSource(drone, frames=frames, min_interval_s=0.0).__next__()
    assert runner.frame_stamp(bgr) == FrameStamp(30.0)
    runner.process_frame(bgr)
    assert runner.last_detections_at == 30.0  # the person cue can use it
    assert _decoder_frame_stamp(drone)(bgr).stale_cause == STALE_CAPTURE_UNKNOWN  # what the lookup alone gives


def test_n4_a_bgr_source_is_handed_on_unchanged():
    frame = np.zeros((4, 4, 3), np.uint8)
    webcam = SimpleNamespace(get_frame=lambda: frame)  # an OpenCV-order drone declares no channel order
    assert next(DroneFrameSource(webcam, min_interval_s=0.0)) is frame
    frames = EdgeFrames(CHANNEL_ORDER_BGR)
    assert frames.bgr(frame) is frame and frames.conversions == 0
    copy = frames.sensor_copy(frame)
    assert copy is not frame and np.array_equal(copy, frame)  # the holder still gets its own copy


def test_n4_the_shared_conversion_is_never_redone_and_refuses_an_unknown_order():
    frames = EdgeFrames(CHANNEL_ORDER_RGB)
    first = _rgb_scene()
    converted = frames.bgr(first)
    others = [_rgb_scene() for _ in range(3)]
    for frame in others:
        frames.bgr(frame)
    assert frames.bgr(first) is converted and frames.sensor_copy(first) is converted
    assert frames.conversions == 4
    assert frames.source_of(converted) is first
    stranger = np.zeros((2, 2, 3), np.uint8)
    assert frames.source_of(stranger) is stranger
    assert frames.bgr(None) is None
    with pytest.raises(ValueError):
        EdgeFrames("yuv")


# ---------------------------------------------------------------------------
# N5: the decoder's stale limit is a setting
# ---------------------------------------------------------------------------


def test_n5_the_decode_lag_limit_defaults_to_0_3_s_and_is_read_from_the_environment():
    assert tello_bound.max_decode_lag_s({}) == MAX_DECODE_LAG_S == 0.3
    assert tello_bound.max_decode_lag_s({ENV_MAX_DECODE_LAG_S: "0.8"}) == 0.8
    assert tello_bound.max_decode_lag_s({ENV_MAX_DECODE_LAG_S: " 2 "}) == 2.0


@pytest.mark.parametrize("bad", ["0.05", "2.5", "nan", "inf", "1_0", "slow", "0,5"])
def test_n5_a_bad_decode_lag_limit_is_refused(bad):
    with pytest.raises(ValueError, match=ENV_MAX_DECODE_LAG_S):
        tello_bound.max_decode_lag_s({ENV_MAX_DECODE_LAG_S: bad})
    envelope = SafetyEnvelope.from_env(alt_slot_cm=100, environ={})
    with pytest.raises(SystemExit, match=ENV_MAX_DECODE_LAG_S):
        ObstacleSettings("tello", envelope, {ENV_MAX_DECODE_LAG_S: bad})


def test_n5_the_edge_validates_and_reports_a_non_default_limit():
    envelope = SafetyEnvelope.from_env(alt_slot_cm=100, environ={})
    settings = ObstacleSettings("fake", envelope, {ENV_MAX_DECODE_LAG_S: "0.6"})
    assert settings.max_decode_lag_s == 0.6
    assert (ENV_MAX_DECODE_LAG_S, 0.6, 0.3) in settings.non_default


def test_n5_the_bound_tello_gives_its_decoder_the_limit(monkeypatch):
    made = []

    class Reader:
        def __init__(self, *args, **kwargs):
            made.append(kwargs)

        def start(self):
            pass

    monkeypatch.setattr(tello_bound, "BoundFrameRead", Reader)
    monkeypatch.setenv(ENV_MAX_DECODE_LAG_S, "1.25")
    client = SimpleNamespace(background_frame_read=None, address=("192.168.10.1", 8889), vs_udp_port=11111,
                             binding=SimpleNamespace(local_ip="192.168.10.2"), name="d", _socket_factory=None)
    tello_bound.BoundTello.get_frame_read(client)
    assert made[0]["max_lag_s"] == 1.25


# ---------------------------------------------------------------------------
# N6: a lost drone may still be descending after its 15 s hover
# ---------------------------------------------------------------------------


def test_n6_a_lost_drone_still_holds_the_re_flight_during_its_auto_land_descent():
    assert LOST_OBSTACLE_S == 20.0
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1", rel_x_cm=50, rel_y_cm=300)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    # 15 s of hover, then up to 4 s of descent: at 19 s it may still be in the air next to the segment.
    fleet.clock.advance(19.0)
    fleet.done("drone-2")
    assert len(fleet.assigns("drone-2")) == 1
    fleet.clock.advance(1.0)
    fleet.done("drone-2")
    assert fleet.assigns("drone-2")[-1]["lanes"] == list(REFLIGHT_PLAN_LANES) + TRANSIT + REFLIGHT


# ---------------------------------------------------------------------------
# N7: the takeoff is timed after the probe, and the probe shows on SENSE
# ---------------------------------------------------------------------------

PROBE_S = 2.0


def test_n7_the_start_ack_and_the_takeoff_time_come_after_the_probe(rigs):
    rig = rigs(obstacle_takeoff_probe=True)
    rig.sensor.on_call = lambda n: rig.clock.advance(PROBE_S) if n == 1 else None  # the probe takes 2 s
    t_start = rig.clock() + 0.5  # the start time ``start`` schedules
    start(rig, LANES)
    ack = answered(rig, "s-p-1")
    assert ack["type"] == "ack"
    assert ack["executed_at_ground_s"] == pytest.approx(t_start + PROBE_S)
    assert rig.executor._takeoff_at_local_s == pytest.approx(t_start + PROBE_S)
    assert wait_for(lambda: finished(rig)), rig.timeline


def test_n7_a_refused_takeoff_shows_sense_stale_with_the_probes_latency(rigs):
    rig = rigs(obstacle_takeoff_probe=True)
    rig.sensor.queue("stale")
    start(rig, LANES)
    assert answered(rig, "s-p-1")["type"] == "nack"
    status = rig.executor.obstacle_status()
    assert status[TELEMETRY_STATE] == OBSTACLE_STATE_UNAVAILABLE  # the drone card: SENSE STALE
    assert isinstance(status[TELEMETRY_LATENCY_MS], float)
    assert events(rig, EVENT_TAKEOFF_SENSE)[0]["ok"] is False


def test_n7_a_blocked_view_on_the_ground_shows_the_latency_and_no_obstacle_stop(rigs):
    rig = rigs(obstacle_takeoff_probe=True)
    rig.sensor.queue(SenseResult.make_blocked(CUE_DEPTH, latency_ms=180.0))
    seen = {}
    rig.sensor.on_call = lambda n: seen.setdefault("status", rig.executor.obstacle_status()) if n == 2 else None
    start(rig, LANES)
    assert answered(rig, "s-p-1")["type"] == "ack"
    assert wait_for(lambda: "status" in seen), rig.timeline
    # Before the first lane gate: the probe's latency, and no state (no lane was stopped).
    assert TELEMETRY_STATE not in seen["status"] and TELEMETRY_LATENCY_MS in seen["status"]
    assert wait_for(lambda: finished(rig)), rig.timeline
