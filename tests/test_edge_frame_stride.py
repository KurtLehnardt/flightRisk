"""The edge's optional frame stride: run detection on every Nth frame only."""
from __future__ import annotations

import threading

import pytest

from flightrisk import edge_device
from flightrisk.edge_device import DroneFrameSource, _resolve_frame_stride


class _CountingDrone:
    """Returns frames numbered 1, 2, 3 ... and a ``None`` between each one to mimic 'no new frame yet'."""

    def __init__(self, n: int, stop: threading.Event):
        self._n = n
        self._i = 0
        self._stop = stop
        self._gap = False

    def get_frame(self):
        if self._i >= self._n:
            self._stop.set()
            return None
        self._gap = not self._gap
        if self._gap:
            return None
        self._i += 1
        return self._i


def _run(stride: int, frames: int) -> list[int]:
    stop = threading.Event()
    source = DroneFrameSource(_CountingDrone(frames, stop), stop, min_interval_s=0.0, frame_stride=stride)
    return list(source)


def test_default_stride_hands_on_every_frame():
    assert _run(1, 6) == [1, 2, 3, 4, 5, 6]


def test_stride_two_hands_on_every_other_frame():
    assert _run(2, 6) == [2, 4, 6]


def test_stride_three_hands_on_every_third_frame():
    assert _run(3, 9) == [3, 6, 9]


def test_a_stride_below_one_is_refused():
    with pytest.raises(ValueError):
        DroneFrameSource(object(), frame_stride=0)


def test_stride_comes_from_the_flag_first_then_the_environment_then_one():
    assert _resolve_frame_stride(2, {edge_device._FRAME_STRIDE_ENV_VAR: "5"}) == 2
    assert _resolve_frame_stride(None, {edge_device._FRAME_STRIDE_ENV_VAR: "5"}) == 5
    assert _resolve_frame_stride(None, {}) == 1
    assert _resolve_frame_stride(None, {edge_device._FRAME_STRIDE_ENV_VAR: "  "}) == 1


@pytest.mark.parametrize("bad", ["0", "-1", "two", "1.5"])
def test_a_bad_environment_stride_stops_the_edge_with_a_message(bad):
    with pytest.raises(SystemExit) as info:
        _resolve_frame_stride(None, {edge_device._FRAME_STRIDE_ENV_VAR: bad})
    assert edge_device._FRAME_STRIDE_ENV_VAR in str(info.value) or "frame stride" in str(info.value)


def test_a_zero_flag_stops_the_edge_with_a_message():
    with pytest.raises(SystemExit):
        _resolve_frame_stride(0, {})


def test_the_parser_accepts_the_flag():
    args = edge_device._build_parser().parse_args(["--source", "fake", "--frame-stride", "2"])
    assert args.frame_stride == 2
    assert edge_device._build_parser().parse_args(["--source", "fake"]).frame_stride is None
