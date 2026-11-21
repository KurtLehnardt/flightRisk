"""Tests for the edge frame stride: detection on only every Nth video frame.

Everything runs against fakes (no network, no hardware, no models).
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import (
    MAX_FRAME_STRIDE,
    DroneFrameSource,
    _build_parser,
    _flight_frame_source,
    _resolve_frame_stride,
    run_edge_device,
)
from flightrisk.observability import StructuredLogger

ENV = "FLIGHTRISK_EDGE_FRAME_STRIDE"
FRAME_S = 1 / 30


class FakeClock:
    """A clock that only moves when told to, so the timing tests do not depend on real time."""

    def __init__(self, stop: threading.Event | None = None):
        self.t = 0.0
        self.stop = stop

    def now(self) -> float:
        return self.t

    def wait(self, seconds: float) -> bool:
        self.t += seconds
        return self.stop.is_set() if self.stop is not None else False

    def advance(self, seconds: float) -> None:
        self.t += seconds


class LatestFrameDrone:
    """A 30 fps stream that always returns the newest frame, numbered by its stream index."""

    def __init__(self, clock: FakeClock):
        self.clock = clock

    def get_frame(self):
        frame = np.zeros((4, 4, 3), np.uint8)
        frame[0, 0, 0] = int(self.clock.t / FRAME_S + 1e-6) % 256
        return frame


def _consume(stride: int, busy_s: float, seconds: float = 30.0) -> int:
    """Count frames a consumer that spends *busy_s* on each frame receives in *seconds* of stream time."""
    stop = threading.Event()
    clock = FakeClock(stop)
    source = DroneFrameSource(
        LatestFrameDrone(clock), stop, min_interval_s=FRAME_S, frame_stride=stride,
        clock=clock.now, wait=clock.wait,
    )
    count = 0
    for _frame in source:
        count += 1
        clock.advance(busy_s)
        if clock.t >= seconds:
            break
    return count


class NumberedDrone:
    """A drone whose frames carry their own index in pixel (0, 0, 0)."""

    def __init__(self, total: int, stop: threading.Event):
        self.total = total
        self.stop = stop
        self.reads = 0

    def get_frame(self):
        if self.reads >= self.total:
            self.stop.set()
            return None
        self.reads += 1
        frame = np.zeros((4, 4, 3), np.uint8)
        frame[0, 0, 0] = self.reads
        return frame


def _yielded(stride: int, total: int = 12) -> tuple[list[int], int]:
    stop = threading.Event()
    clock = FakeClock(stop)
    drone = NumberedDrone(total, stop)
    source = DroneFrameSource(
        drone, stop, min_interval_s=FRAME_S, frame_stride=stride, clock=clock.now, wait=clock.wait
    )
    return [int(f[0, 0, 0]) for f in source], drone.reads


def _args(*argv: str):
    return _build_parser().parse_args(["--source", "fake", *argv])


# -- the source ---------------------------------------------------------------


def test_stride_one_yields_every_frame():
    frames, reads = _yielded(1)
    assert frames == list(range(1, 13))
    assert reads == 12


def test_default_stride_is_one():
    stop = threading.Event()
    source = DroneFrameSource(NumberedDrone(5, stop), stop, min_interval_s=0.0)
    assert [int(f[0, 0, 0]) for f in source] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    ("stride", "expected"),
    [(2, [1, 3, 5, 7, 9, 11]), (3, [1, 4, 7, 10]), (5, [1, 6, 11])],
)
def test_stride_n_yields_every_nth_frame(stride, expected):
    frames, _ = _yielded(stride)
    assert frames == expected


def test_skipped_frames_are_still_read_from_the_drone():
    # Dropped frames are still polled at the full rate (frozen-stream recovery counts identical polls).
    frames, reads = _yielded(3)
    assert len(frames) == 4
    assert reads == 12


def test_missing_frames_are_skipped_and_never_yielded():
    # Frames arrive every other poll (15 fps), so a stride of 2 (two 30 fps frame times) still
    # passes each real frame; a missing frame is polled past, not handed out and not counted.
    class Gappy:
        def __init__(self, stop):
            self.stop, self.calls = stop, 0

        def get_frame(self):
            self.calls += 1
            if self.calls > 12:
                self.stop.set()
                return None
            if self.calls % 2:
                return None
            frame = np.zeros((4, 4, 3), np.uint8)
            frame[0, 0, 0] = self.calls // 2
            return frame

    stop = threading.Event()
    clock = FakeClock(stop)
    source = DroneFrameSource(
        Gappy(stop), stop, min_interval_s=FRAME_S, frame_stride=2, clock=clock.now, wait=clock.wait
    )
    assert [int(f[0, 0, 0]) for f in source] == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("bad", [0, -1, 1.5, "x", "1_0", "\u0663", "-2", "1.5", None, True, 31, 1000, "31"])
def test_source_rejects_an_invalid_stride(bad):
    with pytest.raises(ValueError, match="frame stride"):
        DroneFrameSource(object(), threading.Event(), frame_stride=bad)


def test_source_accepts_a_numeric_string_stride():
    # A numeric string is accepted and converted, as it comes from the environment.
    stop = threading.Event()
    source = DroneFrameSource(NumberedDrone(1, stop), stop, frame_stride="2")
    assert source._frame_stride == 2


def test_source_accepts_the_largest_stride():
    DroneFrameSource(object(), threading.Event(), frame_stride=MAX_FRAME_STRIDE)
    assert MAX_FRAME_STRIDE == 30


# -- the stride cuts work in proportion even when processing is slower than the video ------


def test_slow_consumer_gets_about_one_nth_of_the_frames():
    # 330 ms per frame at 30 fps is the ZBook's detector. Stride N must give about 1/N of the
    # stride-1 rate; counting polls instead of time would give 0.91, 0.83 and 0.72 here.
    base = _consume(1, 0.33)
    assert base == pytest.approx(30 / 0.33, rel=0.03)
    for stride in (2, 3, 5):
        ratio = _consume(stride, 0.33) / base
        assert ratio == pytest.approx(1 / stride, rel=0.1), (stride, ratio)


def test_fast_consumer_still_gets_every_nth_frame():
    for stride in (2, 3, 5):
        stop = threading.Event()
        clock = FakeClock(stop)
        source = DroneFrameSource(
            LatestFrameDrone(clock), stop, min_interval_s=FRAME_S, frame_stride=stride,
            clock=clock.now, wait=clock.wait,
        )
        indexes = []
        for frame in source:
            indexes.append(int(frame[0, 0, 0]))
            if len(indexes) == 6:
                break
        assert [b - a for a, b in zip(indexes, indexes[1:])] == [stride] * 5


def test_stride_one_ignores_processing_time():
    # Stride 1 behaves as before: a frame per poll, however long the consumer takes.
    assert _consume(1, 0.33, seconds=3.3) == 10


def test_slow_consumer_polls_the_drone_at_the_full_rate_while_dropping_frames():
    stop = threading.Event()
    clock = FakeClock(stop)
    drone = LatestFrameDrone(clock)
    polls = []
    real = drone.get_frame
    drone.get_frame = lambda: (polls.append(clock.t), real())[1]
    source = DroneFrameSource(drone, stop, min_interval_s=FRAME_S, frame_stride=3, clock=clock.now, wait=clock.wait)
    next(source)
    clock.advance(0.33)
    polls.clear()
    next(source)
    gaps = [b - a for a, b in zip(polls, polls[1:])]
    assert len(polls) > 20
    assert all(g == pytest.approx(FRAME_S) for g in gaps)


# -- flag, environment, precedence --------------------------------------------


def test_default_without_flag_or_environment():
    assert _resolve_frame_stride(_args(), {}) == 1


def test_flag_is_parsed():
    assert _args("--frame-stride", "3").frame_stride == 3


def test_environment_sets_the_stride():
    assert _resolve_frame_stride(_args(), {ENV: "2"}) == 2


def test_blank_environment_means_default():
    assert _resolve_frame_stride(_args(), {ENV: "  "}) == 1


def test_flag_wins_over_environment():
    assert _resolve_frame_stride(_args("--frame-stride", "4"), {ENV: "2"}) == 4


def test_flag_of_one_wins_over_environment():
    assert _resolve_frame_stride(_args("--frame-stride", "1"), {ENV: "3"}) == 1


@pytest.mark.parametrize("bad", ["0", "-2", "1.5", "two", "1e1", "1_0", "\u0663", "31", "100"])
def test_invalid_flag_is_rejected_by_the_parser(bad, capsys):
    with pytest.raises(SystemExit):
        _args("--frame-stride", bad)
    err_line = capsys.readouterr().err.strip().splitlines()[-1]  # the usage text above also names the flag
    err = err_line
    assert "--frame-stride" in err
    assert err.count("--frame-stride") == 1  # argparse names the flag; the message does not repeat it
    assert "from 1 to 30" in err


def test_largest_stride_is_accepted_by_the_parser():
    assert _args("--frame-stride", "30").frame_stride == 30


@pytest.mark.parametrize("bad", ["0", "-2", "1.5", "two", "1_0", "\u0663", "31"])
def test_invalid_environment_is_rejected_with_its_name(bad):
    with pytest.raises(ValueError, match=ENV):
        _resolve_frame_stride(_args(), {ENV: bad})


def test_invalid_environment_is_ignored_when_the_flag_is_given():
    assert _resolve_frame_stride(_args("--frame-stride", "2"), {ENV: "junk"}) == 2


def test_stride_above_one_is_refused_for_sources_that_cannot_use_it():
    args = _build_parser().parse_args(["--source", "camera", "--frame-stride", "2"])
    with pytest.raises(ValueError, match="tello and --source fake"):
        _resolve_frame_stride(args, {})
    assert _resolve_frame_stride(_build_parser().parse_args(["--source", "camera"]), {ENV: "1"}) == 1


def test_flight_frame_source_receives_the_stride():
    args = _args("--frame-stride", "3")
    args.frame_stride = _resolve_frame_stride(args, {})
    stop = threading.Event()
    source = _flight_frame_source(args, NumberedDrone(1, stop), stop, StructuredLogger(component="test"))
    assert isinstance(source, DroneFrameSource)
    assert source._frame_stride == 3


# -- end to end through the edge loop ------------------------------------------


class CountingDetector:
    def __init__(self):
        self.calls: list[int] = []

    def detect(self, frame):
        self.calls.append(int(frame[0, 0, 0]))
        return []


class RecordingTransport:
    def __init__(self):
        self.sent = []

    def send_detections(self, msg):
        self.sent.append(msg)

    def recv(self, recv_timeout=0.0):
        return None

    def disconnect(self):
        pass


def _run(stride: int, stream_video: bool, total: int = 9):
    stop = threading.Event()
    detector = CountingDetector()
    runner = EdgeRunner(detector=detector, stream_video=stream_video, drone_id="drone-1")
    transport = RecordingTransport()
    clock = FakeClock(stop)
    source = DroneFrameSource(
        NumberedDrone(total, stop), stop, min_interval_s=FRAME_S, frame_stride=stride,
        clock=clock.now, wait=clock.wait,
    )
    run_edge_device(source, transport, runner, logger=StructuredLogger(component="test"), stop_event=stop)
    return detector, transport


def test_detector_runs_only_on_the_strided_frames():
    detector, transport = _run(stride=3, stream_video=False)
    assert detector.calls == [1, 4, 7]
    assert len(transport.sent) == 3


def test_stride_one_runs_the_detector_on_every_frame():
    detector, transport = _run(stride=1, stream_video=False)
    assert detector.calls == list(range(1, 10))
    assert len(transport.sent) == 9


def test_video_thumbnails_follow_the_processed_frames():
    # Thumbnails are built inside EdgeRunner.process_frame, so with --stream-video they are
    # sent for the processed frames only, at the stride rate; skipped frames send nothing.
    detector, transport = _run(stride=3, stream_video=True)
    assert len(transport.sent) == 3
    assert all(m.thumbnail_jpeg for m in transport.sent)
    assert detector.calls == [1, 4, 7]


# -- main(): the environment variable and the flag reach the flight edge ---------------------


def _captured_args(monkeypatch, argv, env=None):
    from flightrisk import edge_device

    captured = []
    monkeypatch.delenv(ENV, raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(edge_device, "_run_flight_edge", lambda args, *a, **k: captured.append(args))
    monkeypatch.setattr(edge_device, "_init_edge_telemetry", lambda log: False)
    edge_device.main(argv)
    return captured


def test_main_passes_the_environment_stride_to_the_flight_edge(monkeypatch):
    captured = _captured_args(monkeypatch, ["--source", "tello"], {ENV: "2"})
    assert [a.frame_stride for a in captured] == [2]


def test_main_defaults_the_stride_to_one(monkeypatch):
    captured = _captured_args(monkeypatch, ["--source", "tello"])
    assert [a.frame_stride for a in captured] == [1]


def test_main_accepts_the_flag_with_tello(monkeypatch):
    captured = _captured_args(monkeypatch, ["--source", "tello", "--frame-stride", "3"], {ENV: "2"})
    assert [a.frame_stride for a in captured] == [3]
    captured = _captured_args(monkeypatch, ["--drone", "tello", "--frame-stride", "2"])
    assert [a.frame_stride for a in captured] == [2]


def test_main_refuses_the_environment_stride_with_a_camera_and_names_the_variable(monkeypatch, capsys):
    from flightrisk import edge_device

    started = []
    monkeypatch.setattr(edge_device, "_run_flight_edge", lambda *a, **k: started.append(a))
    monkeypatch.setattr(edge_device, "_init_edge_telemetry", lambda log: False)
    monkeypatch.setenv(ENV, "2")
    with pytest.raises(SystemExit) as info:
        edge_device.main(["--source", "camera"])
    assert info.value.code == 2
    err = capsys.readouterr().err
    assert f"{ENV}=2 is set" in err and f"Unset {ENV}" in err
    assert started == []


def test_main_refuses_the_flag_with_mavlink(monkeypatch, capsys):
    from flightrisk import edge_device

    started = []
    monkeypatch.setattr(edge_device, "_run_flight_edge", lambda *a, **k: started.append(a))
    monkeypatch.setattr(edge_device, "_init_edge_telemetry", lambda log: False)
    monkeypatch.delenv(ENV, raising=False)
    with pytest.raises(SystemExit) as info:
        edge_device.main([
            "--drone", "mavlink", "--source", "camera", "--mavlink-address", "udpin://127.0.0.1:14540",
            "--alt-slot-m", "15", "--frame-heading-deg", "0", "--frame-stride", "2",
        ])
    assert info.value.code == 2
    err = capsys.readouterr().err
    assert "only applies to --source tello and --source fake" in err
    assert "Unset" not in err  # the stride came from the flag, not the environment
    assert started == []
