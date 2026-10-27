"""Tests for the drone-side edge-device core loop (``run_edge_device``).

These are fast and self-contained: no real camera, network, or vision
model. The frame source is a fake yielding a few synthetic BGR frames, the
transport is a fake recording ``send_detections`` calls and replaying queued
control messages from ``recv``, and the runner is a real ``EdgeRunner`` with
``detector=None`` (which emits empty detections and, when streaming is on, a
thumbnail -- exactly the behaviour we assert on).
"""

import numpy as np
import pytest

from flightrisk.edge import DetectionMessage, EdgeRunner
from flightrisk.edge_device import run_edge_device


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSource:
    """Yields ``n`` synthetic BGR frames, then stops. Tracks release()."""

    def __init__(self, n: int = 3, shape=(480, 640, 3)):
        self._frames = [np.zeros(shape, np.uint8) for _ in range(n)]
        self._i = 0
        self.released = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._i >= len(self._frames):
            raise StopIteration
        frame = self._frames[self._i]
        self._i += 1
        return frame

    def release(self):
        self.released = True


class FakeTransport:
    """Records send_detections calls; replays queued control messages.

    Each queued control message is returned by a single ``recv`` call; once
    the queue is drained ``recv`` returns ``None`` (nothing waiting), which
    is how the loop's drain step terminates.
    """

    def __init__(self, control_queue=None):
        self.sent = []
        self._control = list(control_queue or [])
        self.recv_calls = 0
        self.connected = True
        self.disconnected = False

    def send_detections(self, msg, timeout=30):
        self.sent.append(msg)

    def recv(self, recv_timeout=None, timeout=30):
        self.recv_calls += 1
        if self._control:
            return self._control.pop(0)
        return None

    def disconnect(self, timeout=30):
        self.disconnected = True
        self.connected = False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_one_send_per_frame():
    src = FakeSource(n=3)
    transport = FakeTransport()
    runner = EdgeRunner(detector=None)

    processed = run_edge_device(src, transport, runner, recv_timeout=0.0)

    assert processed == 3
    assert len(transport.sent) == 3
    assert all(isinstance(m, DetectionMessage) for m in transport.sent)
    # Frame dimensions propagate from the synthetic frame shape.
    assert transport.sent[0].frame_width == 640
    assert transport.sent[0].frame_height == 480
    # Loop tears everything down on exit.
    assert transport.disconnected is True
    assert src.released is True


def test_stream_video_control_flips_runner():
    src = FakeSource(n=3)
    # A single stream_video=True control message, delivered after frame 1.
    transport = FakeTransport(control_queue=[{"type": "stream_video", "enabled": True}])
    runner = EdgeRunner(detector=None, stream_video=False)

    run_edge_device(src, transport, runner, recv_timeout=0.0)

    # The runner's streaming mode was flipped on by the control message.
    assert runner.stream_video is True
    # Frame 1 was processed before the toggle -> no thumbnail.
    assert transport.sent[0].thumbnail_jpeg is None
    # Frames after the toggle carry a full-frame thumbnail.
    assert transport.sent[1].thumbnail_jpeg is not None
    assert transport.sent[2].thumbnail_jpeg is not None


def test_stream_video_control_can_disable():
    src = FakeSource(n=2)
    transport = FakeTransport(control_queue=[{"type": "stream_video", "enabled": False}])
    runner = EdgeRunner(detector=None, stream_video=True)

    run_edge_device(src, transport, runner, recv_timeout=0.0)

    assert runner.stream_video is False
    # First frame streamed (thumbnail present), later frames don't.
    assert transport.sent[0].thumbnail_jpeg is not None
    assert transport.sent[1].thumbnail_jpeg is None


def test_set_target_handled_without_raising():
    src = FakeSource(n=2)
    transport = FakeTransport(
        control_queue=[{"type": "set_target", "reid_embedding": [0.1, 0.2], "face_embedding": None}]
    )
    runner = EdgeRunner(detector=None)

    # Must complete cleanly -- the edge retains the target rather than scoring.
    run_edge_device(src, transport, runner, recv_timeout=0.0)

    assert len(transport.sent) == 2
    assert transport.disconnected is True
    # Target was retained on the runner side.
    assert getattr(runner, "_edge_target", None) == {
        "reid_embedding": [0.1, 0.2],
        "face_embedding": None,
    }


def test_unknown_control_type_ignored():
    src = FakeSource(n=2)
    transport = FakeTransport(control_queue=[{"type": "bogus", "foo": 1}])
    runner = EdgeRunner(detector=None, stream_video=False)

    run_edge_device(src, transport, runner, recv_timeout=0.0)

    # Unknown message is ignored: no crash, no state change.
    assert runner.stream_video is False
    assert len(transport.sent) == 2


def test_disconnect_called_on_empty_source():
    src = FakeSource(n=0)
    transport = FakeTransport()
    runner = EdgeRunner(detector=None)

    processed = run_edge_device(src, transport, runner, recv_timeout=0.0)

    assert processed == 0
    assert transport.sent == []
    assert transport.disconnected is True
    assert src.released is True


def test_stop_event_halts_loop():
    class StopAfterFirst:
        def __init__(self):
            self.calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls > 1  # allow first frame, then stop

    src = FakeSource(n=5)
    transport = FakeTransport()
    runner = EdgeRunner(detector=None)

    processed = run_edge_device(
        src, transport, runner, recv_timeout=0.0, stop_event=StopAfterFirst()
    )

    assert processed == 1
    assert len(transport.sent) == 1
    assert transport.disconnected is True


# ---------------------------------------------------------------------------
# --drone-id is checked at startup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("drone_id", ["drone-1", "Drone_2", "d.3", "lab:drone-4", "x" * 64])
def test_drone_id_that_is_a_protocol_identifier_is_accepted(drone_id):
    from flightrisk import fleet_protocol as fp
    from flightrisk.edge_device import _build_parser

    args = _build_parser().parse_args(["--source", "fake", "--drone-id", drone_id])
    assert args.drone_id == drone_id
    # The id the edge starts with is one every fleet message accepts.
    fp.validate_message(fp.make_telemetry(drone_id, 80, 100, False, 0.0, 0.0, 0.0, None, None, 0.1, None))


@pytest.mark.parametrize("drone_id", ["", "x" * 65, "drone 1", "drône-1", "-drone", "drone/1", "drone-1\n", "a;b"])
def test_drone_id_that_the_protocol_would_refuse_fails_at_startup(drone_id, capsys):
    from flightrisk.edge_device import _build_parser

    with pytest.raises(SystemExit) as info:
        _build_parser().parse_args(["--source", "fake", f"--drone-id={drone_id}"])
    assert info.value.code == 2
    assert "--drone-id must be 1 to 64 characters" in capsys.readouterr().err
