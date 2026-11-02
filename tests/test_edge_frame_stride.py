"""Tests for the edge frame stride: detection on only every Nth video frame.

Everything runs against fakes (no network, no hardware, no models).
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import (
    DroneFrameSource,
    _build_parser,
    _flight_frame_source,
    _resolve_frame_stride,
    run_edge_device,
)
from flightrisk.observability import StructuredLogger

ENV = "FLIGHTRISK_EDGE_FRAME_STRIDE"


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
    drone = NumberedDrone(total, stop)
    source = DroneFrameSource(drone, stop, min_interval_s=0.0, frame_stride=stride)
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
    # The Tello stream must be drained: every frame is read even when it is not processed.
    frames, reads = _yielded(3)
    assert len(frames) == 4
    assert reads == 12


def test_missing_frames_do_not_count_toward_the_stride():
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
    source = DroneFrameSource(Gappy(stop), stop, min_interval_s=0.0, frame_stride=2)
    assert [int(f[0, 0, 0]) for f in source] == [1, 3, 5]


@pytest.mark.parametrize("bad", [0, -1, 1.5, "2", "x", None, True])
def test_source_rejects_an_invalid_stride(bad):
    if bad == "2":
        # A numeric string is accepted and converted, as it comes from the environment.
        stop = threading.Event()
        DroneFrameSource(NumberedDrone(1, stop), stop, frame_stride=bad)
        return
    with pytest.raises(ValueError, match="frame stride"):
        DroneFrameSource(object(), threading.Event(), frame_stride=bad)


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


@pytest.mark.parametrize("bad", ["0", "-2", "1.5", "two", "1e1"])
def test_invalid_flag_is_rejected_by_the_parser(bad, capsys):
    with pytest.raises(SystemExit):
        _args("--frame-stride", bad)
    assert "--frame-stride" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["0", "-2", "1.5", "two"])
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
    source = DroneFrameSource(NumberedDrone(total, stop), stop, min_interval_s=0.0, frame_stride=stride)
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
