"""Tests for the edge frame stride: detection on only every Nth video frame.

Everything runs against fakes (no network, no hardware, no models).
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import (
    DEFAULT_STRIDE_MAX_SKIP_S,
    MAX_FRAME_STRIDE,
    DroneFrameSource,
    _build_parser,
    _flight_frame_source,
    _resolve_frame_stride,
    _resolve_stride_max_skip,
    run_edge_device,
)
from flightrisk.observability import StructuredLogger

ENV = "FLIGHTRISK_EDGE_FRAME_STRIDE"
SKIP_ENV = "FLIGHTRISK_EDGE_STRIDE_MAX_SKIP_S"
SENSING_ENV = "FLIGHTRISK_OBSTACLE_SENSING"
#: The environment a stride above 1 needs: obstacle sensing switched off.
OFF = {SENSING_ENV: "off"}
FRAME_S = 1 / 30
#: More frames than any test here can legitimately consume; reaching it fails the test instead of hanging.
_MAX_FRAMES = 100_000


class FakeClock:
    """A clock that only moves when told to, so the timing tests do not depend on real time.

    With ``limit`` the clock sets ``stop`` once it passes that time, so a source that never hands
    out a frame ends instead of spinning forever.
    """

    def __init__(self, stop: threading.Event | None = None, limit: float | None = None):
        self.t = 0.0
        self.stop = stop
        self.limit = limit

    def now(self) -> float:
        return self.t

    def wait(self, seconds: float) -> bool:
        self.t += seconds
        if self.stop is not None and self.limit is not None and self.t > self.limit:
            self.stop.set()
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


def _consume(
    stride: int, busy_s: float, seconds: float = 30.0, max_skip_s: float = DEFAULT_STRIDE_MAX_SKIP_S,
) -> int:
    """Count frames a consumer that spends *busy_s* on each frame receives in *seconds* of stream time.

    Bounded twice, so a broken source fails the test instead of hanging it: the clock stops the
    source shortly after *seconds*, and the loop fails after ``_MAX_FRAMES`` frames.
    """
    stop = threading.Event()
    clock = FakeClock(stop, limit=seconds + 10.0)
    source = DroneFrameSource(
        LatestFrameDrone(clock), stop, min_interval_s=FRAME_S, frame_stride=stride,
        clock=clock.now, wait=clock.wait, max_skip_s=max_skip_s,
    )
    count = 0
    for _frame in source:
        count += 1
        if count > _MAX_FRAMES:
            pytest.fail(f"the source handed out more than {_MAX_FRAMES} frames")
        clock.advance(busy_s)
        if clock.t >= seconds:
            break
    return count


def _gaps_after_slow_frames(stride: int, busy_s: float, max_skip_s: float, frames: int = 4) -> list[float]:
    """Stream time from each handed-out frame to the next, for a consumer that spends *busy_s* on each."""
    stop = threading.Event()
    clock = FakeClock(stop, limit=frames * (busy_s + 10.0))
    source = DroneFrameSource(
        LatestFrameDrone(clock), stop, min_interval_s=FRAME_S, frame_stride=stride,
        clock=clock.now, wait=clock.wait, max_skip_s=max_skip_s,
    )
    times = []
    for _frame in source:
        times.append(clock.t)
        if len(times) == frames:
            break
        clock.advance(busy_s)
    assert len(times) == frames, "the source stopped before handing out enough frames"
    return [b - a for a, b in zip(times, times[1:])]


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
    # stride-1 rate while the skip cap does not bind; counting polls instead of time would give
    # 0.91, 0.83 and 0.72 here.
    base = _consume(1, 0.33)
    assert base == pytest.approx(30 / 0.33, rel=0.03)
    for stride in (2, 3, 5):
        ratio = _consume(stride, 0.33, max_skip_s=5.0) / base
        assert ratio == pytest.approx(1 / stride, rel=0.1), (stride, ratio)


def test_default_cap_leaves_strides_two_and_three_proportional_on_the_zbook():
    # At 330 ms the skips of stride 2 and 3 (0.33 s and 0.66 s) are under the 1 s cap.
    base = _consume(1, 0.33)
    for stride in (2, 3):
        assert _consume(stride, 0.33) / base == pytest.approx(1 / stride, rel=0.1)


def test_the_skip_after_a_slow_frame_is_capped():
    # The review's measurement: one 5 s frame at stride 10 left about 50 s with no detection, because
    # frames were dropped for (N - 1) x 5 s = 45 s after it. With the cap the next frame is taken at
    # most the cap (1 s by default) after the consumer comes back.
    gaps = _gaps_after_slow_frames(10, 5.0, DEFAULT_STRIDE_MAX_SKIP_S)
    assert all(5.0 <= gap <= 5.0 + DEFAULT_STRIDE_MAX_SKIP_S + FRAME_S for gap in gaps), gaps


def test_the_cap_is_configurable_and_bounds_every_stride():
    for stride in (2, 5, 30):
        for cap in (0.0, 0.5, 2.0):
            gaps = _gaps_after_slow_frames(stride, 1.5, cap)
            uncapped = (stride - 1) * 1.5
            expected = 1.5 + min(cap, uncapped)
            assert all(gap == pytest.approx(expected, abs=FRAME_S * 1.01) for gap in gaps), (stride, cap, gaps)


def test_a_zero_cap_takes_the_next_frame_as_soon_as_a_slow_consumer_returns():
    gaps = _gaps_after_slow_frames(10, 0.33, 0.0)
    assert all(gap == pytest.approx(0.33, abs=FRAME_S * 1.01) for gap in gaps), gaps


def test_the_cap_does_not_slow_a_fast_consumer_at_the_largest_stride():
    # A fast consumer at stride 30 skips 29 frame times, under the 1 s cap: still every 30th frame.
    stop = threading.Event()
    clock = FakeClock(stop, limit=60.0)
    source = DroneFrameSource(
        LatestFrameDrone(clock), stop, min_interval_s=FRAME_S, frame_stride=30, clock=clock.now, wait=clock.wait,
    )
    indexes = []
    for frame in source:
        indexes.append(int(frame[0, 0, 0]))
        if len(indexes) == 4:
            break
    assert [b - a for a, b in zip(indexes, indexes[1:])] == [30] * 3


@pytest.mark.parametrize("bad", [-0.1, 5.1, float("nan"), float("inf"), "1", None, True])
def test_source_rejects_an_invalid_skip_cap(bad):
    with pytest.raises(ValueError, match="skip cap"):
        DroneFrameSource(object(), threading.Event(), frame_stride=2, max_skip_s=bad)


def test_skip_cap_environment():
    assert _resolve_stride_max_skip({}) == DEFAULT_STRIDE_MAX_SKIP_S == 1.0
    assert _resolve_stride_max_skip({SKIP_ENV: " "}) == 1.0
    assert _resolve_stride_max_skip({SKIP_ENV: "0.5"}) == 0.5
    assert _resolve_stride_max_skip({SKIP_ENV: "0"}) == 0.0
    assert _resolve_stride_max_skip({SKIP_ENV: "5"}) == 5.0


@pytest.mark.parametrize("bad", ["-1", "5.5", "nan", "inf", "1_0", "one", "\u0661"])
def test_skip_cap_environment_rejects_bad_values_with_its_name(bad):
    with pytest.raises(ValueError, match=SKIP_ENV):
        _resolve_stride_max_skip({SKIP_ENV: bad})


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
    assert _resolve_frame_stride(_args(), {ENV: "2", **OFF}) == 2


def test_blank_environment_means_default():
    assert _resolve_frame_stride(_args(), {ENV: "  "}) == 1


def test_flag_wins_over_environment():
    assert _resolve_frame_stride(_args("--frame-stride", "4"), {ENV: "2", **OFF}) == 4


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
    assert _resolve_frame_stride(_args("--frame-stride", "2"), {ENV: "junk", **OFF}) == 2


# -- a stride above 1 is refused while obstacle sensing is required -----------------------------


@pytest.mark.parametrize("sensing", [None, "required", " Required "])
@pytest.mark.parametrize("source", ["tello", "fake"])
def test_a_stride_above_one_is_refused_while_obstacle_sensing_is_required(sensing, source):
    env = {} if sensing is None else {SENSING_ENV: sensing}
    args = _build_parser().parse_args(["--source", source, "--frame-stride", "2"])
    with pytest.raises(ValueError) as info:
        _resolve_frame_stride(args, env)
    message = str(info.value)
    assert "--frame-stride 2 is given" in message
    assert "refused while obstacle sensing is required" in message
    assert "person cue" in message and "Use a stride of 1" in message
    assert f"{SENSING_ENV}=off" in message


def test_an_environment_stride_is_refused_with_its_name_while_sensing_is_required():
    with pytest.raises(ValueError, match=f"{ENV}=3 is set, but a frame stride above 1 is refused"):
        _resolve_frame_stride(_args(), {ENV: "3"})


def test_stride_one_needs_no_override():
    assert _resolve_frame_stride(_args("--frame-stride", "1"), {}) == 1
    assert _resolve_frame_stride(_args(), {ENV: "1"}) == 1


def test_the_off_override_allows_a_stride():
    assert _resolve_frame_stride(_args("--frame-stride", "3"), OFF) == 3


def test_stride_above_one_is_refused_for_sources_that_cannot_use_it():
    args = _build_parser().parse_args(["--source", "camera", "--frame-stride", "2"])
    with pytest.raises(ValueError, match="tello and --source fake"):
        _resolve_frame_stride(args, {})
    assert _resolve_frame_stride(_build_parser().parse_args(["--source", "camera"]), {ENV: "1"}) == 1


def test_flight_frame_source_receives_the_stride():
    args = _args("--frame-stride", "3")
    args.frame_stride = _resolve_frame_stride(args, OFF)
    stop = threading.Event()
    source = _flight_frame_source(args, NumberedDrone(1, stop), stop, StructuredLogger(component="test"))
    assert isinstance(source, DroneFrameSource)
    assert source._frame_stride == 3
    assert source._max_skip_s == DEFAULT_STRIDE_MAX_SKIP_S


def test_flight_frame_source_receives_the_skip_cap():
    args = _args("--frame-stride", "3")
    args.frame_stride = 3
    args.stride_max_skip_s = 0.25
    stop = threading.Event()
    source = _flight_frame_source(args, NumberedDrone(1, stop), stop, StructuredLogger(component="test"))
    assert source._max_skip_s == 0.25


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
    for name in (ENV, SKIP_ENV, SENSING_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(edge_device, "_run_flight_edge", lambda args, *a, **k: captured.append(args))
    monkeypatch.setattr(edge_device, "_init_edge_telemetry", lambda log: False)
    edge_device.main(argv)
    return captured


def test_main_passes_the_environment_stride_to_the_flight_edge(monkeypatch):
    captured = _captured_args(monkeypatch, ["--source", "tello"], {ENV: "2", **OFF})
    assert [a.frame_stride for a in captured] == [2]


def test_main_defaults_the_stride_to_one(monkeypatch):
    captured = _captured_args(monkeypatch, ["--source", "tello"])
    assert [a.frame_stride for a in captured] == [1]
    assert [a.stride_max_skip_s for a in captured] == [DEFAULT_STRIDE_MAX_SKIP_S]


def test_main_passes_the_skip_cap_to_the_flight_edge(monkeypatch):
    captured = _captured_args(monkeypatch, ["--source", "tello"], {SKIP_ENV: "0.4"})
    assert [a.stride_max_skip_s for a in captured] == [0.4]


def test_main_accepts_the_flag_with_tello_when_sensing_is_off(monkeypatch):
    captured = _captured_args(monkeypatch, ["--source", "tello", "--frame-stride", "3"], {ENV: "2", **OFF})
    assert [a.frame_stride for a in captured] == [3]
    captured = _captured_args(monkeypatch, ["--drone", "tello", "--frame-stride", "2"], OFF)
    assert [a.frame_stride for a in captured] == [2]


def _forbid_starting(monkeypatch, edge_device):
    """Make every way of starting an edge fail the test at once, so a refusal that regresses cannot hang."""

    def _started(*args, **kwargs):
        raise AssertionError("the edge was started although it should have been refused")

    for name in ("_run_flight_edge", "_build_source", "_build_runner", "_build_transport"):
        monkeypatch.setattr(edge_device, name, _started)
    monkeypatch.setattr(edge_device, "_init_edge_telemetry", lambda log: False)


@pytest.mark.parametrize("argv, env", [
    (["--source", "tello", "--frame-stride", "2"], {}),
    (["--drone", "tello", "--frame-stride", "4"], {SENSING_ENV: "required"}),
    (["--source", "fake"], {ENV: "2"}),
])
def test_main_refuses_a_stride_while_sensing_is_required(monkeypatch, capsys, argv, env):
    from flightrisk import edge_device

    _forbid_starting(monkeypatch, edge_device)
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.delenv(SENSING_ENV, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as info:
        edge_device.main(argv)
    assert info.value.code == 2
    err = capsys.readouterr().err
    assert "refused while obstacle sensing is required" in err


def test_main_refuses_a_bad_skip_cap_with_its_name(monkeypatch, capsys):
    from flightrisk import edge_device

    _forbid_starting(monkeypatch, edge_device)
    monkeypatch.setenv(SKIP_ENV, "9")
    with pytest.raises(SystemExit) as info:
        edge_device.main(["--source", "tello"])
    assert info.value.code == 2
    assert f"{SKIP_ENV}='9'" in capsys.readouterr().err


def test_main_refuses_the_environment_stride_with_a_camera_and_names_the_variable(monkeypatch, capsys):
    from flightrisk import edge_device

    # A camera edge never reaches _run_flight_edge: it builds a capture source and a runner. Both are
    # replaced, so a refusal that regresses fails here instead of opening a camera and loading models.
    _forbid_starting(monkeypatch, edge_device)
    monkeypatch.setenv(ENV, "2")
    with pytest.raises(SystemExit) as info:
        edge_device.main(["--source", "camera"])
    assert info.value.code == 2
    err = capsys.readouterr().err
    assert f"{ENV}=2 is set" in err and f"Unset {ENV}" in err


def test_main_refuses_the_flag_with_mavlink(monkeypatch, capsys):
    from flightrisk import edge_device

    _forbid_starting(monkeypatch, edge_device)
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
