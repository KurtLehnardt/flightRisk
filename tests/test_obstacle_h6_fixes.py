"""Tests for the obstacle sensing fixes that unblock hardware check H6.

H6 is the end-to-end stop in front of a person and a plant. The defects come
from the post-merge reviews of PR #177 (obstacle sensing) and PR #170 (frame
stride):

1. a frame stride starved the person cue, and telemetry did not show it;
2. a frame's freshness was judged on its arrival, not its capture, so a
   lagging decoder could pass an old view off as new;
3. MiDaS saw BGR in flight and RGB in a replay;
4. there was no tool to record what the sensor sees.

The stride's own tests are in ``tests/test_edge_frame_stride.py``. Everything
here runs against fakes: no drone, no network, no model weights.
"""

from __future__ import annotations

import csv
import json
import sys
import threading
import time
from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from flightrisk import fleet_protocol as fp
from flightrisk.drone.controller import DroneLinkStatus
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.obstacle_camera import ForwardCameraSensor
from flightrisk.drone.obstacle_sensor import (
    CHANNEL_ORDER_BGR,
    CHANNEL_ORDER_RGB,
    ENV_OBSTACLE_SENSING,
    ENV_OBSTACLE_SETTLE_S,
    OBSTACLE_PERSON_CUE_DOWN_AFTER,
    OBSTACLE_SENSE_TIMEOUT_S,
    OBSTACLE_SETTLE_S,
    PERSON_CUE_AGE_KEY,
    PERSON_CUE_DOWN_KEY,
    REASON_STALE,
    SENSOR_CHANNEL_ORDER,
    STALE_CAPTURE_UNKNOWN,
    STALE_CORRUPT_FRAME,
    STALE_DATA_DROPPED,
    STALE_DECODE_ERROR,
    STALE_DECODER_BACKLOG,
    STALE_UNTIL_KEY_FRAME,
    TELEMETRY_PERSON_CUE,
    FakeObstacleSensor,
    FrameStamp,
    LatestFrame,
    to_sensor_frame,
)
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController
from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import (
    DEFAULT_TORCH_THREADS,
    DecodedFramePoller,
    ObstacleSettings,
    _build_obstacle_wiring,
    _build_parser,
    _copying_frame_feed,
    _decoder_frame_stamp,
    _limit_torch_threads,
    _resolve_torch_threads,
    feed_sensor_holder,
    sense_timeout_for_settle,
)
from flightrisk.observability import StructuredLogger
from scripts import obstacle_record, obstacle_replay

H, W = 32, 48


class RecordingLog:
    """Collects (level, event, fields) triples."""

    def __init__(self):
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, name):
        if name in ("info", "warning", "error", "debug"):
            return lambda event, **fields: self.records.append((name, event, fields))
        raise AttributeError(name)

    def events(self):
        return [event for _, event, _ in self.records]


class TickClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, t: float = 100.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _rgb_scene() -> np.ndarray:
    """A small colour picture whose channels differ everywhere, as the Tello's decoder delivers it (RGB)."""
    rgb = np.zeros((H, W, 3), np.uint8)
    rgb[:, : W // 2] = (220, 30, 10)  # red on the left
    rgb[:, W // 2:] = (10, 40, 200)  # blue on the right
    rgb[H // 3: 2 * H // 3, W // 3: 2 * W // 3] = (30, 180, 60)  # a green plant in the centre
    rgb[0, :, 1] = np.arange(W, dtype=np.uint8)  # texture, so it is never blank
    return rgb


# ---------------------------------------------------------------------------
# Finding 3: the colour channel order
# ---------------------------------------------------------------------------


class _RecordingTransform:
    """MiDaS's input transform, standing in: records the image it is given and makes a tensor of it."""

    def __init__(self):
        self.images: list[np.ndarray] = []
        self.tensors: list[torch.Tensor] = []

    def __call__(self, img):
        self.images.append(np.array(img, copy=True))
        tensor = torch.from_numpy(np.ascontiguousarray(img)).permute(2, 0, 1).unsqueeze(0).float()
        self.tensors.append(tensor)
        return tensor


class _ClearModel(torch.nn.Module):
    def forward(self, x):
        depth = torch.ones(1, 24, 24) * 0.1
        depth[0, 0:8, :] = 0.9
        return depth


@pytest.fixture
def midas(monkeypatch):
    """The MiDaS loader replaced: a model that always sees a clear view and a transform that records its input."""
    transform = _RecordingTransform()
    model = _ClearModel()
    monkeypatch.setattr("flightrisk.drone.midas_model.load_midas_small", lambda *a, **kw: (model, transform))
    return transform


class DecoderTello(FakeTelloController):
    """A fake Tello whose frames are a new RGB array per poll, as a Tello's video decoder hands them out."""

    def __init__(self, picture: np.ndarray, **kwargs):
        super().__init__(name="drone-1", **kwargs)
        self.picture = picture

    def get_frame(self):
        if not self.state.is_connected:
            return None
        frame = self.picture.copy()
        for cb in self._frame_callbacks:
            cb(frame)
        return frame


def test_to_sensor_frame_reverses_rgb_and_copies_bgr():
    rgb = _rgb_scene()
    bgr = to_sensor_frame(rgb, CHANNEL_ORDER_RGB)
    assert SENSOR_CHANNEL_ORDER == CHANNEL_ORDER_BGR
    assert np.array_equal(bgr, rgb[..., ::-1]) and bgr.flags["C_CONTIGUOUS"]
    same = to_sensor_frame(bgr, CHANNEL_ORDER_BGR)
    assert np.array_equal(same, bgr) and same is not bgr
    grey = np.zeros((4, 4), np.uint8)
    assert to_sensor_frame(grey, CHANNEL_ORDER_RGB) is not grey
    with pytest.raises(ValueError, match="channel order"):
        to_sensor_frame(rgb, "yuv")


def test_live_and_replay_paths_give_midas_the_same_tensor(midas, tmp_path):
    """The same picture through the flight path and through record-then-replay reaches MiDaS identically.

    Before the fix the flight path handed the decoder's RGB to ``ObstacleGuard``, which assumes BGR
    and swapped it, so MiDaS saw BGR in flight and RGB in a replay of the same view.
    """
    picture = _rgb_scene()
    envelope = SafetyEnvelope.from_env(alt_slot_cm=100, environ={})
    args = _build_parser().parse_args(["--source", "tello"])

    # Flight: the Tello edge's own wiring, sensor and guard.
    live = DecoderTello(picture)
    wiring = _build_obstacle_wiring(args, RecordingLog(), live, None, envelope, {})
    assert wiring.sensor_error is None, wiring.sensor_error
    midas.tensors.clear()
    midas.images.clear()
    live.connect()
    after = time.monotonic() - 1.0
    assert live.get_frame() is not None
    result = wiring.sensor.sense(after, OBSTACLE_SENSE_TIMEOUT_S)
    wiring.sensor.close()
    assert result.clear, result
    (live_tensor,) = midas.tensors
    # MiDaS is given the true RGB picture.
    assert np.array_equal(midas.images[0], picture)

    # Calibration: the recorder writes the frames the sensor holds, the replay reads them back.
    recorder_drone = DecoderTello(picture)
    recorder_drone.connect()
    out = tmp_path / "rec"
    meta = obstacle_record.record(recorder_drone, out, 0.1)
    assert meta["frames_written"] >= 1
    midas.tensors.clear()
    midas.images.clear()
    results = obstacle_replay.run_replay(None, 1, 0.35, frames_dir=str(out))
    assert len(results) == meta["frames_written"]
    assert all(torch.equal(t, live_tensor) for t in midas.tensors)

    # And the input the old flight path gave MiDaS was not the picture.
    from flightrisk.drone.obstacle import ObstacleGuard

    ObstacleGuard(min_safe_depth=0.35, check_interval=0.0, center_stat="p90").check_path(picture)
    assert not torch.equal(midas.tensors[-1], live_tensor)


def test_the_mavlink_capture_feed_keeps_opencvs_bgr():
    holder = LatestFrame()
    feed = _copying_frame_feed(holder, CHANNEL_ORDER_BGR)
    bgr = _rgb_scene()[..., ::-1].copy()
    feed(bgr)
    stored = holder.latest().frame
    assert stored is not bgr and np.array_equal(stored, bgr)


def test_both_tello_feeds_convert_the_decoders_rgb():
    picture = _rgb_scene()
    drone = DecoderTello(picture)
    holder = LatestFrame()
    feed_sensor_holder(drone, holder)
    drone.connect()
    drone.get_frame()
    assert np.array_equal(holder.latest().frame, picture[..., ::-1])
    reader = SimpleNamespace(frame=picture.copy(), with_queue=False)
    polled = SimpleNamespace(name="d", tello=SimpleNamespace(background_frame_read=reader),
                             state=SimpleNamespace(is_connected=True))
    holder2 = LatestFrame()
    assert DecodedFramePoller(polled, holder2).poll_once()
    assert np.array_equal(holder2.latest().frame, picture[..., ::-1])


# ---------------------------------------------------------------------------
# Finding 4: the recorder and the replay of a recording
# ---------------------------------------------------------------------------


class StampingReader:
    """A decoder that stamps its frames like ``BoundFrameRead``: ``frame`` and ``frame_info``."""

    with_queue = False

    def __init__(self, clock):
        self.clock = clock
        self.frame = None
        self.stamps: list[tuple[np.ndarray, FrameStamp]] = []

    def decode(self, picture, captured_at, stale_cause=None):
        self.frame = picture.copy()
        self.stamps.append((self.frame, FrameStamp(captured_at, stale_cause)))

    def frame_info(self, frame):
        for array, stamp in self.stamps:
            if array is frame:
                return stamp
        return None

    def stop(self):
        pass


def test_recorder_writes_exactly_the_frames_the_sensor_holds_with_their_capture_times(tmp_path):
    clock = TickClock(50.0)
    reader = StampingReader(clock)
    picture = _rgb_scene()
    plan = [(49.8, None), (49.85, STALE_DECODER_BACKLOG), (49.9, None)]

    class ScriptedTello(FakeTelloController):
        """Each poll the decoder has a new frame; the second is stale and must not be recorded."""

        def __init__(self):
            super().__init__(name="drone-1")
            self.tello = SimpleNamespace(background_frame_read=reader)

        def get_frame(self):
            if not plan:
                return None
            captured, cause = plan.pop(0)
            reader.decode(picture, captured, cause)
            for cb in self._frame_callbacks:
                cb(reader.frame)
            return reader.frame

    drone = ScriptedTello()
    drone.connect()
    out = tmp_path / "rec"

    def sleep(_s):
        clock.advance(0.05)

    meta = obstacle_record.record(drone, out, 0.2, label="plant 150 cm", clock=clock, sleep=sleep)
    assert meta["frames_written"] == 2
    assert meta["stale_frames_refused"] == 1 and meta["last_stale_cause"] == STALE_DECODER_BACKLOG
    assert meta["capture_stamps"] is True and meta["channel_order"] == "bgr"
    assert json.loads((out / "meta.json").read_text())["label"] == "plant 150 cm"
    with open(out / "frames.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [float(r["captured_at_s"]) for r in rows] == [49.8, 49.9]
    assert all(float(r["decode_lag_ms"]) >= 0 for r in rows)
    import cv2

    for row in rows:
        back = cv2.imread(str(out / row["file"]), cv2.IMREAD_COLOR)
        assert np.array_equal(back, picture[..., ::-1])  # the sensor's BGR, pixel for pixel


def test_recorder_refuses_to_overwrite_a_recording(tmp_path):
    (tmp_path / "frames.csv").write_text("index\n")
    with pytest.raises(FileExistsError):
        obstacle_record.FrameRecorder(tmp_path)


def test_recorder_arguments():
    args = obstacle_record.parse_args(["--out", "rec/x", "--seconds", "3", "--tello-local-ip", "192.168.10.2"])
    assert (args.out, args.seconds, args.tello_local_ip, args.tello_host) == ("rec/x", 3.0, "192.168.10.2",
                                                                               "192.168.10.1")
    with pytest.raises(SystemExit):
        obstacle_record.parse_args(["--out", "x", "--seconds", "0"])
    with pytest.raises(SystemExit):
        obstacle_record.parse_args([])


def test_replay_takes_a_recording_or_a_video_but_not_both():
    assert obstacle_replay.parse_args(["--frames", "rec/x"]).frames == "rec/x"
    assert obstacle_replay.parse_args(["--video", "a.mp4"]).video == "a.mp4"
    with pytest.raises(SystemExit):
        obstacle_replay.parse_args(["--frames", "rec/x", "--video", "a.mp4"])
    with pytest.raises(SystemExit):
        obstacle_replay.parse_args(["--frames", "rec/x", "--stride", "0"])


def test_replay_of_a_recording_reports_capture_times_and_honours_the_stride(tmp_path):
    import cv2

    out = tmp_path / "rec"
    recorder = obstacle_record.FrameRecorder(out)
    holder = LatestFrame(TickClock(10.0))
    holder.add_listener(recorder.add)
    for i in range(4):
        frame = to_sensor_frame(_rgb_scene(), CHANNEL_ORDER_RGB)
        frame[1, 1, :] = i
        holder.offer(frame, stamp=FrameStamp(9.0 + i / 10))
    assert recorder.close() == 4
    seen = []
    guard = SimpleNamespace(check_path=lambda f: (seen.append(int(f[1, 1, 0])), {"safe": True})[1])
    csv_out = tmp_path / "replay.csv"
    results = obstacle_replay.run_replay(None, 2, 0.35, csv_out=str(csv_out), frames_dir=str(out), guard=guard)
    assert seen == [0, 2]
    assert [r["captured_at_s"] for r in results] == [9.0, 9.2]
    with open(csv_out, newline="") as f:
        assert [row["captured_at_s"] for row in csv.DictReader(f)] == ["9.0", "9.2"]
    assert cv2.imread(str(out / "frame_000001.png")) is not None


def test_replay_of_a_directory_without_an_index_exits(tmp_path, capsys):
    with pytest.raises(SystemExit):
        obstacle_replay.run_replay(None, 1, 0.35, frames_dir=str(tmp_path), guard=SimpleNamespace())
    assert "obstacle_record.py" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Finding 2: freshness by capture time, stale frames, settle, torch threads
# ---------------------------------------------------------------------------


def test_freshness_is_judged_on_capture_time_not_arrival():
    clock = TickClock(100.0)
    holder = LatestFrame(clock)
    move_end = 99.0
    # The decoder lags 1.5 s: the frame arrives now but shows the view from before the move ended.
    holder.offer(np.ones((2, 2, 3)), stamp=FrameStamp(captured_at=98.5))
    timed = holder.latest()
    assert timed.arrived_at == 100.0 and timed.captured_at == 98.5
    assert holder.wait_for(move_end, settle_s=0.3, wait_s=0) is None
    holder.offer(np.ones((2, 2, 3)), stamp=FrameStamp(captured_at=99.6))
    assert holder.wait_for(move_end, settle_s=0.3, wait_s=0).captured_at == 99.6


def test_a_capture_stamp_never_makes_a_frame_newer_than_its_arrival():
    holder = LatestFrame(TickClock(10.0))
    holder.offer(np.ones((2, 2, 3)), stamp=FrameStamp(captured_at=12.0))
    assert holder.latest().captured_at == 10.0


def test_a_frame_without_a_stamp_counts_from_its_arrival():
    holder = LatestFrame(TickClock(10.0))
    holder.offer(np.ones((2, 2, 3)))
    holder.offer(np.zeros((2, 2, 3)), stamp=FrameStamp())
    assert holder.latest().captured_at == holder.latest().arrived_at == 10.0


def test_a_stale_frame_is_refused_counted_and_the_last_good_view_kept():
    clock = TickClock(10.0)
    holder = LatestFrame(clock)
    good = np.ones((2, 2, 3))
    holder.offer(good, stamp=FrameStamp(9.9))
    kept = holder.latest()
    bad = np.zeros((2, 2, 3))
    clock.advance(0.1)
    assert holder.offer(bad, stamp=FrameStamp(10.0, STALE_CORRUPT_FRAME)) is False
    assert holder.offer(bad, stamp=FrameStamp(10.0)) is False  # the same object is never taken later
    assert holder.latest() is kept and holder.frames_seen == 1
    assert holder.stale_frames == 1 and holder.last_stale_cause == STALE_CORRUPT_FRAME


class _JudgeEverything:
    """A guard that calls every view clear: the plant at 100 cm looked like 150 cm before the move."""

    def __init__(self):
        self.judged = 0

    def check_path(self, frame):
        self.judged += 1
        return {"safe": True, "center_depth": 0.2, "left_depth": 0.1, "right_depth": 0.1, "action": "clear",
                "confidence": 0.9, "center_p90": 0.2, "center_stat": "p90", "degenerate": False,
                "degenerate_reason": ""}


def test_a_lagging_decoder_fails_closed_instead_of_clearing_an_old_view():
    """The review's failure: a 1.5 s decode lag made a plant at 100 cm read as 150 cm and the drone stepped."""
    clock = TickClock(100.0)
    holder = LatestFrame(clock)
    reader = StampingReader(clock)
    drone = SimpleNamespace(tello=SimpleNamespace(background_frame_read=reader))
    feed = _copying_frame_feed(holder, CHANNEL_ORDER_RGB, _decoder_frame_stamp(drone))
    guard = _JudgeEverything()
    sensor = ForwardCameraSensor(holder, None, guard=guard, warm=False, clock=clock, frame_wait_s=0.0,
                                 detection_wait_s=0.0)
    try:
        move_end = 100.0
        clock.advance(0.5)
        # Frames arrive after the move and the settle time, but were captured 1.5 s earlier.
        reader.decode(_rgb_scene(), captured_at=clock() - 1.5)
        feed(reader.frame)
        result = sensor.sense(move_end, 2.0)
        assert result.unavailable and result.reason == REASON_STALE
        assert guard.judged == 0
        # The decoder catches up: a frame captured after the settle time is judged.
        reader.decode(_rgb_scene(), captured_at=clock() - 0.1)
        feed(reader.frame)
        assert sensor.sense(move_end, 2.0).clear and guard.judged == 1
    finally:
        sensor.close()


def test_decoder_stamps_reach_the_feeds_and_unknown_frames_are_stale():
    reader = StampingReader(TickClock())
    drone = SimpleNamespace(tello=SimpleNamespace(background_frame_read=reader))
    stamp = _decoder_frame_stamp(drone)
    reader.decode(_rgb_scene(), 5.0)
    assert stamp(reader.frame) == FrameStamp(5.0)
    assert stamp(np.zeros((2, 2, 3))).stale_cause == STALE_CAPTURE_UNKNOWN
    # djitellopy's own decoder cannot stamp: the frame counts from its arrival, as before.
    plain = SimpleNamespace(tello=SimpleNamespace(background_frame_read=SimpleNamespace(frame=None)))
    assert _decoder_frame_stamp(plain)(np.zeros((2, 2, 3))) is None
    assert _decoder_frame_stamp(FakeTelloController(name="x"))(np.zeros((2, 2, 3))) is None


def test_the_poller_passes_the_decoders_stamp():
    clock = TickClock(20.0)
    reader = StampingReader(clock)
    drone = SimpleNamespace(name="d", tello=SimpleNamespace(background_frame_read=reader),
                            state=SimpleNamespace(is_connected=True))
    holder = LatestFrame(clock)
    poller = DecodedFramePoller(drone, holder)
    reader.decode(_rgb_scene(), 19.5)
    assert poller.poll_once() and holder.latest().captured_at == 19.5
    reader.decode(_rgb_scene(), 19.6, STALE_DECODE_ERROR)
    assert poller.poll_once() is False and holder.stale_frames == 1


# -- the person cue judges detections by their frame's capture time --------------------------


class _NoDetector:
    def detect(self, frame):
        return []


def test_detections_are_stamped_with_the_capture_time_and_stale_frames_never_feed_the_person_cue():
    runner = EdgeRunner(detector=_NoDetector(), drone_id="drone-1")
    stamps = {}
    runner.frame_stamp = lambda frame: stamps.get(id(frame))
    fresh = np.zeros((8, 8, 3), np.uint8)
    stamps[id(fresh)] = FrameStamp(captured_at=time.monotonic() - 1.2)
    runner.process_frame(fresh)
    assert runner.last_detections_at == stamps[id(fresh)].captured_at
    kept = runner.last_detections_at
    stale = np.zeros((8, 8, 3), np.uint8)
    stamps[id(stale)] = FrameStamp(captured_at=time.monotonic(), stale_cause=STALE_DECODER_BACKLOG)
    msg = runner.process_frame(stale)
    assert msg.frame_id == 2  # still processed and sent
    assert runner.last_detections_at == kept


def test_a_failing_stamp_lookup_never_stops_detection():
    runner = EdgeRunner(detector=_NoDetector(), drone_id="drone-1")

    def broken(frame):
        raise RuntimeError("decoder gone")

    runner.frame_stamp = broken
    before = time.monotonic()
    runner.process_frame(np.zeros((8, 8, 3), np.uint8))
    assert runner.last_detections_at >= before


# -- the bound decoder: capture stamps, backlog, corruption ------------------------------------


def _have_h264_encoder() -> bool:
    try:
        import av

        return "libx264" in av.codecs_available
    except ImportError:
        return False


def _access_units(frames: int = 12, gop: int = 6) -> list[bytes]:
    """An H.264 Annex B stream of textured 64x48 frames, one access unit per frame, an IDR every ``gop``."""
    import av

    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = 64, 48, "yuv420p"
    enc.time_base = Fraction(1, 30)
    enc.framerate = Fraction(30, 1)
    enc.gop_size = gop
    enc.options = {"tune": "zerolatency", "preset": "ultrafast", "keyint": str(gop), "scenecut": "0"}
    units = []
    for i in range(frames):
        image = np.zeros((48, 64, 3), np.uint8)
        image[:, :, 0] = (np.arange(64) * 3 + i * 7) % 256
        image[:, :, 2] = 90
        frame = av.VideoFrame.from_ndarray(image, format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        units.extend(bytes(p) for p in enc.encode(frame))
    units.extend(bytes(p) for p in enc.encode(None))
    return units


@pytest.fixture
def bound_reader():
    from flightrisk.drone.tello_bound import BoundFrameRead

    made = []

    def _make(clock, **kwargs):
        reader = BoundFrameRead("127.0.0.1", "127.0.0.1", 0, name="test", clock=clock, **kwargs)
        made.append(reader)
        return reader

    yield _make
    for reader in made:
        reader.stop()


def _decoded(reader) -> list[FrameStamp]:
    with reader.lock:
        return [stamp for _, stamp in reader._stamps]


@pytest.mark.skipif(not _have_h264_encoder(), reason="PyAV has no libx264 encoder here")
def test_bound_decoder_stamps_a_frame_with_the_arrival_of_its_first_datagram(bound_reader):
    clock = TickClock(10.0)
    reader = bound_reader(clock)
    arrivals = []
    for unit in _access_units(6):
        arrivals.append(clock())
        for i in range(0, len(unit), 1460):
            reader._decode(unit[i:i + 1460], clock())
            clock.advance(0.002)
        clock.advance(1 / 30)
    stamps = _decoded(reader)
    assert len(stamps) >= 4
    # Each frame is decoded when the next one starts, and stamped with the arrival of its own first datagram.
    assert [s.captured_at for s in stamps] == pytest.approx(arrivals[: len(stamps)])
    assert all(not s.stale for s in stamps)
    assert reader.frame_info(reader.frame) is stamps[-1]
    assert reader.frame_info(reader.frame.copy()) is None  # identity only


@pytest.mark.skipif(not _have_h264_encoder(), reason="PyAV has no libx264 encoder here")
def test_bound_decoder_marks_frames_decoded_behind_a_backlog_stale(bound_reader):
    clock = TickClock(10.0)
    reader = bound_reader(clock, max_lag_s=0.3)
    units = _access_units(8)
    # All the video arrives in real time ...
    queued = []
    for unit in units:
        for i in range(0, len(unit), 1460):
            queued.append((clock(), unit[i:i + 1460]))
        clock.advance(1 / 30)
    # ... and the decoder gets to it 1.5 s late.
    clock.advance(1.5)
    for arrived, data in queued:
        reader._decode(data, arrived)
    stamps = _decoded(reader)
    assert stamps and all(s.stale_cause == STALE_DECODER_BACKLOG for s in stamps)
    assert reader.stale_frames == len(stamps)


@pytest.mark.skipif(not _have_h264_encoder(), reason="PyAV has no libx264 encoder here")
def test_bound_decoder_drops_an_old_queue_and_distrusts_frames_until_the_next_intra_frame(bound_reader):
    clock = TickClock(10.0)
    reader = bound_reader(clock, max_queue_age_s=2.0)
    units = _access_units(12, gop=6)
    chunks = [[u[i:i + 1460] for i in range(0, len(u), 1460)] for u in units]
    # The first three frames are decoded in real time.
    for unit in chunks[:4]:
        for chunk in unit:
            reader._decode(chunk, clock())
        clock.advance(1 / 30)
    clean = len(_decoded(reader))
    assert clean >= 3 and not any(s.stale for s in _decoded(reader))
    # The decoder stalls; the receiver keeps queueing until the oldest datagram is 2 s old.
    for chunk in chunks[4][:1]:
        reader.queue_datagram(chunk, clock())
    clock.advance(2.5)
    for unit in chunks[5:]:
        for chunk in unit:
            reader.queue_datagram(chunk, clock())
        clock.advance(1 / 30)
    assert reader.datagrams_dropped == 1
    # The decoder resumes at the present: everything until the next intra frame is stale.
    while (item := reader._next_datagram()) is not None:
        arrived, data, gap = item
        if gap:
            reader._taint = STALE_DATA_DROPPED  # what update_frame does with a gap
        reader._decode(data, arrived)
    after = _decoded(reader)[clean:]
    assert after, "nothing decoded after the drop"
    first_clean = next(i for i, s in enumerate(after) if not s.stale)
    assert first_clean >= 1
    # Each keeps the cause that tainted the stream (review N10 of #177 round 2), not "damaged reference".
    assert {s.stale_cause for s in after[:first_clean]} <= {STALE_DATA_DROPPED, STALE_DECODER_BACKLOG}


def _fake_decoded(pts, key=False, corrupt=False, intra=False):
    return SimpleNamespace(
        pts=pts, key_frame=key, is_corrupt=corrupt, pict_type=1 if (intra or key) else 2,
        to_ndarray=lambda format: np.zeros((4, 4, 3), np.uint8),
    )


def test_bound_decoder_trusts_nothing_before_the_first_intra_frame_and_after_a_corrupt_one(bound_reader):
    clock = TickClock(10.0)
    reader = bound_reader(clock)
    for pts in range(8):
        reader._packet_started[pts] = clock()
    reader._publish(_fake_decoded(0))  # mid-GOP start: no clean reference yet
    reader._publish(_fake_decoded(1, key=True))
    reader._publish(_fake_decoded(2))
    reader._publish(_fake_decoded(3, corrupt=True))
    reader._publish(_fake_decoded(4))
    reader._publish(_fake_decoded(5, intra=True))  # a non-IDR intra picture also restores trust
    reader._publish(_fake_decoded(6))
    causes = [s.stale_cause for s in _decoded(reader)]
    assert causes == [STALE_UNTIL_KEY_FRAME, None, None, STALE_CORRUPT_FRAME, STALE_CORRUPT_FRAME, None, None]


def test_bound_decoder_distrusts_frames_after_a_decode_error(bound_reader):
    clock = TickClock(10.0)
    reader = bound_reader(clock)
    reader._taint = None

    class Broken:
        def parse(self, data):
            raise ValueError("Invalid data found when processing input")

    reader._codec = Broken()
    reader._decode(b"\x00\x00\x00\x01garbage", clock())
    assert reader.decode_errors == 1 and reader._taint == STALE_DECODE_ERROR
    reader._packet_started[0] = clock()
    reader._publish(_fake_decoded(0))
    assert _decoded(reader)[-1].stale_cause == STALE_DECODE_ERROR


def test_bound_decoder_receiver_and_decoder_threads_start_and_stop(bound_reader):
    reader = bound_reader(time.monotonic)
    reader.start()
    assert reader.receiver.is_alive() and reader.worker.is_alive()
    reader.stop()
    assert not reader.receiver.is_alive() and not reader.worker.is_alive()


# -- the settle time -----------------------------------------------------------------------


def _envelope():
    return SafetyEnvelope.from_env(alt_slot_cm=100, environ={})


def test_settle_defaults_to_0_3_s_and_is_read_from_the_environment():
    assert ObstacleSettings("tello", _envelope(), {}).settle_s == OBSTACLE_SETTLE_S == 0.3
    settings = ObstacleSettings("tello", _envelope(), {ENV_OBSTACLE_SETTLE_S: "0.8"})
    assert settings.settle_s == 0.8
    assert (ENV_OBSTACLE_SETTLE_S, 0.8, 0.3) in settings.non_default


@pytest.mark.parametrize("bad", ["0.05", "3.5", "nan", "inf", "0,5", "slow", "1_0"])
def test_a_bad_settle_refuses_to_start(bad):
    with pytest.raises(SystemExit, match=ENV_OBSTACLE_SETTLE_S):
        ObstacleSettings("tello", _envelope(), {ENV_OBSTACLE_SETTLE_S: bad})


def test_the_settle_reaches_the_sensor_and_lengthens_the_sense_budget(midas):
    args = _build_parser().parse_args(["--source", "tello"])
    log = RecordingLog()
    wiring = _build_obstacle_wiring(args, log, DecoderTello(_rgb_scene()), None, _envelope(),
                                    {ENV_OBSTACLE_SETTLE_S: "0.9"})
    try:
        assert wiring.sensor.settle_s == 0.9 and wiring.sensor.status()["settle_s"] == 0.9
        assert wiring.executor_kwargs()["obstacle_sense_timeout_s"] == pytest.approx(2.6)
        assert "edge_obstacle_setting_non_default" in log.events()
    finally:
        wiring.sensor.close()
    default = _build_obstacle_wiring(args, RecordingLog(), DecoderTello(_rgb_scene()), None, _envelope(), {})
    try:
        assert default.sensor.settle_s == 0.3
        assert "obstacle_sense_timeout_s" not in default.executor_kwargs()
    finally:
        default.sensor.close()
    assert sense_timeout_for_settle(0.3) == OBSTACLE_SENSE_TIMEOUT_S
    assert sense_timeout_for_settle(1.3) == pytest.approx(3.0)


# -- torch threads ---------------------------------------------------------------------------


def test_torch_threads_setting():
    assert _resolve_torch_threads({}) == DEFAULT_TORCH_THREADS == 2
    assert _resolve_torch_threads({"FLIGHTRISK_EDGE_TORCH_THREADS": "4"}) == 4
    assert _resolve_torch_threads({"FLIGHTRISK_EDGE_TORCH_THREADS": "0"}) == 0
    for bad in ("-1", "1.5", "65", "two", "٢"):
        with pytest.raises(ValueError, match="FLIGHTRISK_EDGE_TORCH_THREADS"):
            _resolve_torch_threads({"FLIGHTRISK_EDGE_TORCH_THREADS": bad})


class _FakeTorch:
    def __init__(self, interop_error=False):
        self.calls = []
        self.interop_error = interop_error

    def set_num_threads(self, n):
        self.calls.append(("threads", n))

    def set_num_interop_threads(self, n):
        if self.interop_error:
            raise RuntimeError("cannot set number of interop threads after parallel work has started")
        self.calls.append(("interop", n))

    def get_num_threads(self):
        return dict(self.calls).get("threads", 8)

    def get_num_interop_threads(self):
        return dict(self.calls).get("interop", 8)


def test_torch_threads_are_limited_per_edge(monkeypatch):
    fake = _FakeTorch()
    monkeypatch.setitem(sys.modules, "torch", fake)
    log = RecordingLog()
    assert _limit_torch_threads(2, log) == 2
    assert fake.calls == [("threads", 2), ("interop", 1)]
    assert ("info", "edge_torch_threads", {"threads": 2, "interop_threads": 1}) in log.records
    late = _FakeTorch(interop_error=True)
    monkeypatch.setitem(sys.modules, "torch", late)
    assert _limit_torch_threads(3, RecordingLog()) == 3
    assert late.calls == [("threads", 3)]


def test_zero_torch_threads_leaves_torch_alone(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)  # importing it would fail
    assert _limit_torch_threads(0, RecordingLog()) is None


def test_main_limits_torch_threads_before_any_model_loads(monkeypatch):
    from flightrisk import edge_device

    order = []
    monkeypatch.setenv("FLIGHTRISK_EDGE_TORCH_THREADS", "3")
    # Sensing off: a required sensor refuses a Tello edge without --tello-local-ip (no capture stamps).
    monkeypatch.setenv(ENV_OBSTACLE_SENSING, "off")
    monkeypatch.setattr(edge_device, "_limit_torch_threads", lambda n, log: order.append(("limit", n)))
    monkeypatch.setattr(edge_device, "_run_flight_edge", lambda args, *a, **k: order.append(("fly", None)))
    monkeypatch.setattr(edge_device, "_init_edge_telemetry", lambda log: False)
    edge_device.main(["--source", "tello"])
    assert order == [("limit", 3), ("fly", None)]


def test_main_refuses_a_bad_torch_thread_count(monkeypatch, capsys):
    from flightrisk import edge_device

    monkeypatch.setenv("FLIGHTRISK_EDGE_TORCH_THREADS", "lots")
    monkeypatch.setattr(edge_device, "_run_flight_edge", lambda *a, **k: pytest.fail("started"))
    with pytest.raises(SystemExit) as info:
        edge_device.main(["--source", "tello"])
    assert info.value.code == 2
    assert "FLIGHTRISK_EDGE_TORCH_THREADS='lots'" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Finding 1: the person cue's health in telemetry
# ---------------------------------------------------------------------------


def test_camera_sensor_reports_the_age_of_the_newest_detection():
    clock = TickClock(200.0)
    runner = SimpleNamespace(last_detections=None, last_detections_at=None)
    sensor = ForwardCameraSensor(LatestFrame(clock), runner, guard=_JudgeEverything(), warm=False, clock=clock)
    try:
        assert sensor.status()["person_detection_age_s"] is None
        runner.last_detections_at = 197.5
        assert sensor.status()["person_detection_age_s"] == 2.5
        assert sensor.status()["person_cue_down"] is False
        for _ in range(OBSTACLE_PERSON_CUE_DOWN_AFTER):
            sensor.sense(199.0, 0.0)  # no frame: the person cue is unknown each time
        assert sensor.status()["person_cue_down"] is True
    finally:
        sensor.close()


class _CueSensor:
    """A sensor whose status the test sets."""

    def __init__(self, status):
        self._status = status

    def sense(self, after, timeout_s=2.0):  # pragma: no cover - not called here
        raise AssertionError

    def status(self):
        if isinstance(self._status, Exception):
            raise self._status
        return self._status


class LinkedFakeTello(FakeTelloController):
    """A fake Tello that reports its link like the real controller, so telemetry has every Tello field."""

    def link_status(self):
        return DroneLinkStatus(True, 0.1)


def _executor(sensor, drone=None):
    drone = drone or FakeTelloController(name="drone-1")
    drone.connect()
    return LaneExecutor(drone, _envelope(), "drone-1", send=lambda m: None,
                        logger=StructuredLogger(component="test"), obstacle_sensor=sensor)


def test_lane_telemetry_carries_the_person_cue():
    ex = _executor(_CueSensor({"person_cue_down": True, "person_detection_age_s": 12.34}))
    assert ex.obstacle_status()[TELEMETRY_PERSON_CUE] == {PERSON_CUE_DOWN_KEY: True, PERSON_CUE_AGE_KEY: 12.34}
    assert ex.telemetry()[TELEMETRY_PERSON_CUE] == {"person_cue_down": True, "detection_age_s": 12.34}
    ex = _executor(_CueSensor({"person_cue_down": False, "person_detection_age_s": None}))
    assert ex.telemetry()[TELEMETRY_PERSON_CUE] == {"person_cue_down": False, "detection_age_s": None}


@pytest.mark.parametrize("status", [
    {"sensor": "fake"},
    RuntimeError("status failed"),
    "not a dict",
])
def test_a_sensor_without_a_person_cue_adds_nothing(status):
    assert TELEMETRY_PERSON_CUE not in _executor(_CueSensor(status)).telemetry()


def test_a_bad_detection_age_is_sent_as_null():
    for age in (float("inf"), float("nan"), -1.0, "3", True):
        ex = _executor(_CueSensor({"person_cue_down": False, "person_detection_age_s": age}))
        assert ex.telemetry()[TELEMETRY_PERSON_CUE][PERSON_CUE_AGE_KEY] is None


def test_the_fake_sensor_reports_no_person_cue():
    drone = FakeTelloController(name="drone-1")
    envelope = _envelope()
    sensor = FakeObstacleSensor(envelope.pose_snapshot)
    drone.connect()
    ex = LaneExecutor(drone, envelope, "drone-1", send=lambda m: None, logger=StructuredLogger(component="t"),
                      obstacle_sensor=sensor)
    assert TELEMETRY_PERSON_CUE not in ex.telemetry()


def test_a_full_tello_telemetry_message_with_the_person_cue_is_still_a_valid_fleet_message():
    # A Tello edge's v2 telemetry has 31 fields; the person cue is one more, the most a message may carry.
    ex = _executor(_CueSensor({"person_cue_down": True, "person_detection_age_s": 3.2}),
                   drone=LinkedFakeTello(name="drone-1"))
    ex._v2_telemetry = True
    ex._obstacle_state, ex._obstacle_latency_ms = "clear", 210.0
    msg = ex.telemetry()
    assert "drone_link_ok" in msg and "obstacle_state" in msg and TELEMETRY_PERSON_CUE in msg
    assert len(msg) <= fp.MAX_FIELDS
    assert fp.validate_message(msg, proto=2) == "telemetry"


def test_a_tello_flight_edge_stamps_detections_from_its_decoder(monkeypatch):
    """``_run_flight_edge`` gives the runner the drone's decoder stamps, so the person cue sees capture times."""
    from flightrisk import edge_device

    reader = StampingReader(TickClock())
    drone = FakeTelloController(name="drone-1")
    drone.tello = SimpleNamespace(background_frame_read=reader)
    runner = EdgeRunner(detector=_NoDetector(), drone_id="drone-1")
    seen = []

    class Executor:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

        def shutdown(self):
            pass

    class Transport:
        urls = ()

        def connect(self):
            pass

        def disconnect(self):
            pass

    monkeypatch.setattr(edge_device, "_build_runner", lambda *a, **k: runner)
    monkeypatch.setattr(edge_device, "_build_drone", lambda *a, **k: drone)
    monkeypatch.setattr(edge_device, "_build_obstacle_wiring", lambda *a, **k: edge_device.ObstacleWiring())
    monkeypatch.setattr(edge_device, "_build_transport", lambda *a, **k: Transport())
    monkeypatch.setattr(edge_device, "LaneExecutor", Executor)
    monkeypatch.setattr(edge_device, "start_local_land_key", lambda *a, **k: None)
    monkeypatch.setattr(edge_device, "_install_sigint_land", lambda *a, **k: None)
    monkeypatch.setattr(edge_device, "_wire_emergency_callback", lambda *a, **k: None)
    monkeypatch.setattr(edge_device, "run_edge_device", lambda source, transport, r, **k: seen.append(r))
    args = _build_parser().parse_args(["--source", "tello", "--drone-id", "drone-1"])
    edge_device._run_flight_edge(args, RecordingLog(), stop_event=threading.Event(), envelope=_envelope())
    assert seen == [runner]
    reader.decode(_rgb_scene(), 7.5)
    assert runner.frame_stamp(reader.frame) == FrameStamp(7.5)
