"""Regression tests for the sensor and edge findings of the PR #177 review (B2, N1, N3, N4, N11).

* B2: a NaN, infinite, uniform, all-zero or all-negative depth map is degenerate:
  ``ObstacleGuard`` never calls it safe and ``ForwardCameraSensor`` reports it
  ``unavailable`` (reason ``error``), never ``clear``.
* N1: ``LatestFrame.wait_for`` counts its wait from the later of now and
  ``after + settle``, and ``DecodedFramePoller`` feeds the holder from the
  video decoder without touching ``get_frame`` or its frozen-frame counter.
* N3: ``EdgeRunner.last_detections_at`` is when the frame was obtained, not
  when processing ended.
* N4: ``sense(after, timeout_s)`` spends at most ``timeout_s`` in all.
* N11: every obstacle setting is parsed and validated whatever the mode, drone
  kind or source; integers are ASCII digits; the thresholds cannot switch the
  sensor off; non-default values are logged and reported.
* R2-B1 (round 2): while the Tello's decoder is stalled, ``get_frame`` hands
  the same array to ``on_frame`` on every poll. The holder never stamps that
  object again through either feed (the ``on_frame`` copy feed and
  ``DecodedFramePoller`` share one record), so a gate after a turn finds the
  view stale and never clears on the view from before the turn.
* R2-N3 (round 2): a frame whose pixels are all the same (the black frame a
  restarted decoder starts on) is refused before inference as ``stale``.

MiDaS is never loaded (``torch.hub.load`` is mocked or the guard is a fake),
no network is used and no drone is contacted.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from flightrisk.drone import obstacle_camera
from flightrisk.drone.obstacle_camera import ForwardCameraSensor
from flightrisk.drone.obstacle_sensor import (
    CAUSE_BLANK_FRAME,
    ENV_FAKE_OBSTACLE_Y_CM,
    ENV_OBSTACLE_MIN_SAFE_DEPTH,
    ENV_OBSTACLE_PERSON_FRACTION,
    ENV_OBSTACLE_SENSING,
    ENV_OBSTACLE_STEP_CM,
    ENV_OBSTACLE_TURN,
    EVENT_SETTING_NON_DEFAULT,
    PERSON_CUE_BLOCKED,
    PERSON_CUE_UNKNOWN,
    REASON_ERROR,
    REASON_STALE,
    REASON_TIMEOUT,
    CUE_DEPTH,
    CUE_PERSON,
    LatestFrame,
    NullSensor,
    TimedFrame,
)
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController
from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import (
    DecodedFramePoller,
    ObstacleWiring,
    _build_obstacle_wiring,
    _build_parser,
    _copying_frame_feed,
    _field_envelope,
    _obstacle_frame_poller,
)
from tests.lane_executor_support import FakeClock

FRAME_W, FRAME_H = 960, 720
H = W = 256


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Model(torch.nn.Module):
    """A MiDaS stand-in that returns whatever depth map the test sets."""

    def __init__(self):
        super().__init__()
        self.depth = torch.ones(1, H, W) * 0.1

    def forward(self, x):
        return self.depth.clone()


class _Transforms:
    small_transform = staticmethod(lambda img: torch.zeros(1, 3, H, W))


@pytest.fixture
def model(monkeypatch):
    m = _Model()
    monkeypatch.setattr(
        "torch.hub.load", lambda repo, name, **kw: m if name == "MiDaS_small" else _Transforms(),
    )
    return m


def _guard(center_stat="p90"):
    from flightrisk.drone.obstacle import ObstacleGuard

    return ObstacleGuard(min_safe_depth=0.35, check_interval=0.0, center_stat=center_stat)


def _close_wall():
    """A close wall straight ahead: the centre third of the middle row is the closest thing in view."""
    depth = torch.ones(1, H, W) * 0.1
    depth[0, 85:170, 85:170] = 0.9
    return depth


def _clear_scene():
    depth = torch.ones(1, H, W) * 0.1
    depth[0, 0:85, :] = 0.9
    return depth


def _view(height=FRAME_H, width=FRAME_W):
    """A camera frame with something in it; a frame whose pixels are all the same is refused as blank."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, : width // 2] = 80
    return frame


def _holder_with_frame():
    holder = LatestFrame()
    holder.update(_view())
    return holder


def _sensor(frames, runner=None, guard=None, **kw):
    kw.setdefault("settle_s", 0.0)
    kw.setdefault("frame_wait_s", 0.05)
    kw.setdefault("detection_wait_s", 0.05)
    kw.setdefault("warm", False)
    return ForwardCameraSensor(frames, runner, guard=guard, **kw)


def _after():
    return time.monotonic() - 1.0


def _det(x1, y1, x2, y2):
    return SimpleNamespace(bbox=(x1, y1, x2, y2), confidence=0.9)


CLOSE_CENTRE = _det(380, 100, 580, 600)


def _valid_check(safe=True, depth=0.1):
    return {
        "safe": safe, "center_depth": depth, "left_depth": 0.1, "right_depth": 0.1,
        "action": "clear" if safe else "go_left", "confidence": 0.9,
        "center_p90": depth, "center_stat": "p90", "degenerate": False, "degenerate_reason": "",
    }


class _FixedGuard:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def check_path(self, frame):
        self.calls += 1
        return self.result


# ---------------------------------------------------------------------------
# B2: degenerate depth maps
# ---------------------------------------------------------------------------


def _with_nan_pixel():
    depth = _close_wall()
    depth[0, 120, 120] = float("nan")
    return depth


def _all_nan():
    return torch.full((1, H, W), float("nan"))


def _all_zero():
    return torch.zeros(1, H, W)


def _all_negative():
    return -torch.linspace(1.0, 2.0, H * W).reshape(1, H, W)


def _uniform():
    return torch.ones(1, H, W) * 0.5


def _uniform_high():
    return torch.ones(1, H, W) * 5.0


def _with_inf_pixel():
    depth = _clear_scene()
    depth[0, 120, 120] = float("inf")
    return depth


def _with_minus_inf_pixel():
    depth = _close_wall()
    depth[0, 10, 10] = float("-inf")
    return depth


def _overflowing_scores():
    # Every value is finite, but dividing the huge centre values by the tiny
    # 95th percentile overflows float32 to infinity.
    depth = torch.ones(1, H, W) * 1e-3
    depth[0, 120:130, 120:130] = 3e38
    return depth


DEGENERATE_MAPS = [
    ("one_nan_pixel_in_a_close_wall", _with_nan_pixel, "non_finite"),
    ("all_nan", _all_nan, "non_finite"),
    ("all_zero", _all_zero, "uniform"),
    ("all_negative", _all_negative, "no_scale"),
    ("uniform", _uniform, "uniform"),
    ("uniform_high", _uniform_high, "uniform"),
    ("one_inf_pixel", _with_inf_pixel, "non_finite"),
    ("one_minus_inf_pixel", _with_minus_inf_pixel, "non_finite"),
    ("overflowing_scores", _overflowing_scores, "non_finite_score"),
]


class TestDegenerateDepthMaps:
    @pytest.mark.parametrize(("name", "make", "reason"), DEGENERATE_MAPS, ids=[m[0] for m in DEGENERATE_MAPS])
    @pytest.mark.parametrize("center_stat", ["mean", "p90"])
    def test_guard_never_calls_a_degenerate_map_safe(self, model, name, make, reason, center_stat):
        model.depth = make()
        with np.errstate(over="ignore", invalid="ignore"):
            result = _guard(center_stat).check_path(np.zeros((FRAME_H, FRAME_W, 3), np.uint8))
        assert result["safe"] is False
        assert result["degenerate"] is True
        assert result["degenerate_reason"] == reason
        assert result["center_stat"] == center_stat
        # The dashboard's single-drone search reacts only to go_left, go_right
        # and reverse; every degenerate map but the old uniform-high case asks for no move.
        expected_action = "reverse" if name == "uniform_high" else "unavailable"
        assert result["action"] == expected_action
        for key in ("center_depth", "left_depth", "right_depth", "center_p90", "confidence"):
            assert np.isfinite(result[key]), key

    @pytest.mark.parametrize(("name", "make", "reason"), DEGENERATE_MAPS, ids=[m[0] for m in DEGENERATE_MAPS])
    def test_sensor_reports_a_degenerate_map_unavailable(self, model, name, make, reason):
        model.depth = make()
        sensor = _sensor(_holder_with_frame(), None, _guard())
        with np.errstate(over="ignore", invalid="ignore"):
            result = sensor.sense(_after())
        assert result.unavailable
        assert result.reason == REASON_ERROR
        assert result.detail["degenerate"] == reason
        assert "degenerate depth map" in result.detail["error"]
        assert sensor.status()["obstacle_sensor_ok"] is False

    def test_the_same_close_wall_without_the_nan_is_blocked(self, model):
        model.depth = _close_wall()
        result = _sensor(_holder_with_frame(), None, _guard()).sense(_after())
        assert result.blocked and result.cue == CUE_DEPTH

    @pytest.mark.parametrize("center_stat", ["mean", "p90"])
    def test_valid_maps_keep_their_numbers(self, model, center_stat):
        depth = _clear_scene()
        depth[0, 85:170, 85:170] = 0.5
        depth[0, 85:170, 120:130] = 0.9
        model.depth = depth
        result = _guard(center_stat).check_path(np.zeros((FRAME_H, FRAME_W, 3), np.uint8))
        raw = depth.squeeze().numpy()
        norm = raw / np.percentile(raw, 95)
        centre = norm[85:170, 85:170]
        assert result["degenerate"] is False and result["degenerate_reason"] == ""
        assert result["center_p90"] == pytest.approx(float(np.percentile(centre, 90)), rel=1e-5)
        expected = float(np.percentile(centre, 90)) if center_stat == "p90" else float(centre.mean())
        assert result["center_depth"] == pytest.approx(expected, rel=1e-5)
        assert result["left_depth"] == pytest.approx(float(norm[85:170, :85].mean()), rel=1e-5)
        assert result["safe"] is (expected <= 0.35)

    @pytest.mark.parametrize(
        "check",
        [
            {**_valid_check(), "center_depth": float("nan")},
            {**_valid_check(), "center_depth": float("inf")},
            {**_valid_check(), "center_p90": float("nan")},
            {**_valid_check(), "left_depth": float("-inf")},
            {**_valid_check(), "degenerate": True, "degenerate_reason": "uniform"},
            {**_valid_check(), "degenerate": True},
        ],
        ids=["nan_score", "inf_score", "nan_p90", "minus_inf_left", "flagged", "flagged_without_reason"],
    )
    def test_sensor_never_clears_a_non_finite_or_flagged_check(self, check):
        result = _sensor(_holder_with_frame(), None, _FixedGuard(check)).sense(_after())
        assert result.unavailable and result.reason == REASON_ERROR

    def test_a_degenerate_result_is_logged_once_per_change(self, model, caplog):
        import logging

        from flightrisk.drone.obstacle_sensor import EVENT_SENSOR_ERROR

        sensor = _sensor(_holder_with_frame(), None, _guard())
        model.depth = _all_nan()
        with caplog.at_level(logging.ERROR):
            for _ in range(3):
                sensor.sense(_after())
            model.depth = _all_zero()
            sensor.sense(_after())
        events = [r for r in caplog.records if getattr(r, "fr_event", "") == EVENT_SENSOR_ERROR]
        assert len(events) == 2

    def test_visualization_survives_a_nan_map(self, model):
        model.depth = _all_nan()
        frame = np.zeros((72, 96, 3), np.uint8)
        with np.errstate(invalid="ignore"):
            vis = _guard().get_depth_visualization(frame)
        assert vis.shape == frame.shape


# ---------------------------------------------------------------------------
# N4: one time budget for the whole sense call
# ---------------------------------------------------------------------------


class _SlowFrames:
    """A frame holder whose wait takes ``arrive_after_s`` on a fake clock, cut short by ``limit_s``."""

    def __init__(self, clock: FakeClock, arrive_after_s: float):
        self.clock = clock
        self.arrive_after_s = arrive_after_s
        self.calls: list[tuple] = []
        self.frames_seen = 1

    def wait_for(self, after, settle_s, wait_s, limit_s=None):
        self.calls.append((after, settle_s, wait_s, limit_s))
        spent = self.arrive_after_s if limit_s is None else min(self.arrive_after_s, limit_s)
        self.clock.advance(spent)
        if spent < self.arrive_after_s:
            return None
        return TimedFrame(_view(8, 8), self.clock())


class _SlowGuard:
    """A guard whose inference takes ``seconds`` on the fake clock."""

    def __init__(self, clock: FakeClock, seconds: float, result=None):
        self.clock = clock
        self.seconds = seconds
        self.result = result or _valid_check()
        self.calls = 0

    def check_path(self, frame):
        self.calls += 1
        self.clock.advance(self.seconds)
        return self.result


def _budget_sensor(clock, frames, guard, runner):
    return ForwardCameraSensor(
        frames, runner, guard=guard, warm=False, clock=clock, sleep=clock.advance,
        settle_s=0.3, frame_wait_s=0.7, detection_wait_s=1.0,
    )


class TestSenseBudget:
    def test_the_person_wait_gets_only_what_is_left(self):
        clock = FakeClock(1000.0)
        stale = SimpleNamespace(last_detections=([], FRAME_W, FRAME_H), last_detections_at=900.0)
        frames, guard = _SlowFrames(clock, 0.7), _SlowGuard(clock, 1.0)
        sensor = _budget_sensor(clock, frames, guard, stale)
        start = clock()
        result = sensor.sense(after=990.0, timeout_s=2.0)
        elapsed = clock() - start
        # Frame wait 0.7 + inference 1.0 + person wait 1.0 would be 2.7 s; the person wait gets 0.3.
        assert elapsed == pytest.approx(2.0)
        assert elapsed <= 2.0 + 1e-9
        assert result.clear and result.person_cue == PERSON_CUE_UNKNOWN
        assert result.latency_ms <= 2000.0 + 1e-6
        assert frames.calls[0][3] == pytest.approx(2.0)

    def test_the_review_worst_case_stays_within_the_budget(self):
        # The review's case: frame 0.7 s, inference about 2.0 s, person 1.0 s (3.7 s in all).
        clock = FakeClock(1000.0)
        stale = SimpleNamespace(last_detections=([], FRAME_W, FRAME_H), last_detections_at=900.0)
        frames, guard = _SlowFrames(clock, 0.7), _SlowGuard(clock, 1.25)
        sensor = _budget_sensor(clock, frames, guard, stale)
        start = clock()
        result = sensor.sense(after=990.0, timeout_s=2.0)
        assert clock() - start <= 2.0 + 1e-9
        assert result.person_cue == PERSON_CUE_UNKNOWN

    def test_an_inference_that_ends_past_the_budget_is_a_timeout(self):
        clock = FakeClock(1000.0)
        fresh = SimpleNamespace(last_detections=([], FRAME_W, FRAME_H), last_detections_at=999.0)
        frames, guard = _SlowFrames(clock, 0.7), _SlowGuard(clock, 2.0)
        sensor = _budget_sensor(clock, frames, guard, fresh)
        start = clock()
        result = sensor.sense(after=990.0, timeout_s=2.0)
        assert result.unavailable and result.reason == REASON_TIMEOUT
        # No person wait was added after the over-long inference.
        assert clock() - start == pytest.approx(2.7)

    def test_a_frame_wait_that_uses_the_budget_is_stale(self):
        clock = FakeClock(1000.0)
        frames, guard = _SlowFrames(clock, 5.0), _SlowGuard(clock, 0.1)
        sensor = _budget_sensor(clock, frames, guard, None)
        start = clock()
        result = sensor.sense(after=990.0, timeout_s=2.0)
        assert result.unavailable and result.reason == REASON_STALE
        assert clock() - start == pytest.approx(2.0)
        assert guard.calls == 0

    def test_a_frame_at_the_very_end_of_the_budget_is_not_judged(self):
        clock = FakeClock(1000.0)
        frames, guard = _SlowFrames(clock, 2.0), _SlowGuard(clock, 0.1)
        sensor = _budget_sensor(clock, frames, guard, None)
        result = sensor.sense(after=990.0, timeout_s=2.0)
        assert result.unavailable and result.reason == REASON_TIMEOUT
        assert guard.calls == 0
        assert not sensor.status()["worker_busy"]

    def test_a_spent_budget_still_reads_a_result_that_is_already_there(self):
        clock = FakeClock(1000.0)
        fresh = SimpleNamespace(last_detections=([CLOSE_CENTRE], FRAME_W, FRAME_H), last_detections_at=999.0)
        frames, guard = _SlowFrames(clock, 0.7), _SlowGuard(clock, 1.3)
        sensor = _budget_sensor(clock, frames, guard, fresh)
        start = clock()
        result = sensor.sense(after=990.0, timeout_s=2.0)
        assert clock() - start == pytest.approx(2.0)
        assert result.blocked and result.cue == CUE_PERSON
        assert result.person_cue == PERSON_CUE_BLOCKED

    def test_a_spent_budget_without_a_fresh_result_is_unknown_never_clear(self):
        clock = FakeClock(1000.0)
        stale = SimpleNamespace(last_detections=([], FRAME_W, FRAME_H), last_detections_at=900.0)
        frames, guard = _SlowFrames(clock, 0.7), _SlowGuard(clock, 1.3)
        sensor = _budget_sensor(clock, frames, guard, stale)
        result = sensor.sense(after=990.0, timeout_s=2.0)
        assert result.person_cue == PERSON_CUE_UNKNOWN

    def test_real_clock_hung_model_returns_within_the_budget(self):
        guard = SimpleNamespace(gate=threading.Event())
        guard.check_path = lambda frame: guard.gate.wait(10)
        sensor = _sensor(_holder_with_frame(), None, guard)
        try:
            start = time.monotonic()
            result = sensor.sense(_after(), timeout_s=0.3)
            assert time.monotonic() - start < 3.0
            assert result.unavailable and result.reason == REASON_TIMEOUT
        finally:
            guard.gate.set()
        sensor.close()


# ---------------------------------------------------------------------------
# N1: the frame wait and the decoder poller
# ---------------------------------------------------------------------------


class _TickingClock:
    """A clock that moves ``tick`` seconds every time it is read, and can post a frame at a set time."""

    def __init__(self, start=100.0, tick=0.01):
        self.t = start
        self.tick = tick
        self.post_at: float | None = None
        self.holder: LatestFrame | None = None
        self._lock = threading.RLock()

    def __call__(self):
        with self._lock:
            self.t += self.tick
            now = self.t
            if self.post_at is not None and now >= self.post_at and self.holder is not None:
                self.post_at = None
                self.holder.update("late")  # reads the clock again; post_at is cleared first
            return now


def _ticking_holder(clock):
    holder = LatestFrame(clock)
    holder._POLL_S = 0.0005  # keep the real-time waits between clock reads short
    clock.holder = holder
    return holder


class TestFrameWaitDeadline:
    def test_a_call_before_the_settle_time_still_gets_the_whole_wait(self):
        clock = _TickingClock()
        holder = _ticking_holder(clock)
        holder.update("old")
        after = clock()
        assert holder.wait_for(after, settle_s=0.3, wait_s=0.7) is None
        # The wait runs to after + settle + wait (1.0 s), not to the call + wait (0.7 s).
        assert clock.t >= after + 1.0 - 1e-6
        assert clock.t < after + 1.1

    def test_a_frame_after_the_old_deadline_is_taken(self):
        clock = _TickingClock()
        holder = _ticking_holder(clock)
        after = clock()
        clock.post_at = after + 0.85  # after the old deadline (0.7), before the new one (1.0)
        got = holder.wait_for(after, settle_s=0.3, wait_s=0.7)
        assert got is not None and got.frame == "late"

    def test_a_late_call_gets_its_full_wait_from_now(self):
        clock = _TickingClock()
        holder = _ticking_holder(clock)
        after = clock() - 5.0  # the settle time ended long ago
        start = clock()
        assert holder.wait_for(after, settle_s=0.3, wait_s=0.5) is None
        assert clock.t >= start + 0.5 - 1e-6
        assert clock.t < start + 0.6

    def test_limit_caps_the_wait(self):
        clock = _TickingClock()
        holder = _ticking_holder(clock)
        after = clock()
        assert holder.wait_for(after, settle_s=0.3, wait_s=0.7, limit_s=0.2) is None
        assert clock.t < after + 0.3

    def test_a_zero_limit_looks_once_on_a_clock_that_never_moves(self):
        holder = LatestFrame(FakeClock(100.0))
        holder("old")
        assert holder.wait_for(after=100.0, settle_s=0.3, wait_s=0.7, limit_s=0.0) is None


class _Reader:
    """Stands in for djitellopy's ``BackgroundFrameRead``."""

    def __init__(self, frame=None, with_queue=False):
        self.frame = frame
        self.with_queue = with_queue

    def stop(self):
        """djitellopy's ``end()`` stops the reader when the client goes away."""


def _decoded_drone(reader=None, connected=True):
    client = SimpleNamespace(background_frame_read=reader)

    def _forbidden(*args, **kwargs):
        raise AssertionError("the poller must not call this")

    client.get_frame_read = _forbidden
    return SimpleNamespace(
        name="tello-1", tello=client, state=SimpleNamespace(is_connected=connected), get_frame=_forbidden,
    )


class TestDecodedFramePoller:
    def test_feeds_each_new_decoded_frame_once_as_a_copy(self):
        holder = LatestFrame()
        frame = np.zeros((4, 4, 3), np.uint8)
        reader = _Reader(frame)
        poller = DecodedFramePoller(_decoded_drone(reader), holder)
        assert poller.poll_once() is True
        assert poller.poll_once() is False  # the same decoded frame again
        assert holder.frames_seen == 1
        stored = holder.latest().frame
        assert stored is not frame
        frame[:] = 7
        assert int(stored.max()) == 0
        reader.frame = np.ones((4, 4, 3), np.uint8)
        assert poller.poll_once() is True
        assert holder.frames_seen == 2

    def test_never_calls_get_frame_or_opens_a_decoder(self):
        holder = LatestFrame()
        poller = DecodedFramePoller(_decoded_drone(None), holder)
        assert poller.poll_once() is False  # no decoder yet: nothing, and get_frame_read is not called
        assert holder.frames_seen == 0

    def test_feeds_nothing_while_disconnected(self):
        holder = LatestFrame()
        poller = DecodedFramePoller(_decoded_drone(_Reader(np.zeros((2, 2, 3))), connected=False), holder)
        assert poller.poll_once() is False
        assert holder.frames_seen == 0

    def test_leaves_a_queue_mode_decoder_alone(self):
        class QueueReader:
            with_queue = True

            @property
            def frame(self):
                raise AssertionError("reading a queue-mode frame would pop it")

        holder = LatestFrame()
        poller = DecodedFramePoller(_decoded_drone(QueueReader()), holder)
        assert poller.poll_once() is False

    def test_a_reader_error_is_logged_once_and_never_raised(self):
        class BrokenReader:
            with_queue = False

            @property
            def frame(self):
                raise RuntimeError("decoder gone")

        log = SimpleNamespace(records=[])
        log.warning = lambda event, **fields: log.records.append((event, fields))
        poller = DecodedFramePoller(_decoded_drone(BrokenReader()), LatestFrame(), log=log)
        assert poller.poll_once() is False
        assert poller.poll_once() is False
        assert [e for e, _ in log.records] == ["edge_obstacle_frame_poller_error"]

    def test_the_thread_feeds_the_holder_and_stops(self):
        holder = LatestFrame()
        reader = _Reader(np.zeros((2, 2, 3), np.uint8))
        poller = DecodedFramePoller(_decoded_drone(reader), holder, interval_s=0.005)
        poller.start()
        try:
            deadline = time.monotonic() + 5.0
            while holder.frames_seen < 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            assert holder.frames_seen == 1
            reader.frame = np.ones((2, 2, 3), np.uint8)
            while holder.frames_seen < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            assert holder.frames_seen == 2
        finally:
            poller.stop()
        assert not poller._thread.is_alive()

    def test_does_not_disturb_the_frozen_frame_counter(self, tello_on_wire):
        def frozen_counts(with_poller: bool):
            ctrl, _wire = tello_on_wire()
            ctrl.state.is_connected = True
            ctrl.tello.background_frame_read = _Reader(np.zeros((40, 40, 3), np.uint8))
            seen = []
            ctrl.on_frame(lambda f: seen.append(1))
            holder = LatestFrame()
            poller = DecodedFramePoller(ctrl, holder)
            counts = []
            for _ in range(6):
                if with_poller:
                    poller.poll_once()
                    poller.poll_once()
                assert ctrl.get_frame() is not None
                counts.append(ctrl._frozen_count)
            ctrl.state.is_connected = False
            ctrl.tello.background_frame_read = None
            return counts, len(seen), holder.frames_seen

        without, callbacks_without, _ = frozen_counts(False)
        with_, callbacks_with, fed = frozen_counts(True)
        assert with_ == without == [0, 1, 2, 3, 4, 5]
        assert callbacks_with == callbacks_without == 6  # on_frame still fires once per get_frame
        assert fed == 1  # the same decoded frame is fed once

    def test_only_a_working_camera_sensor_on_a_decoder_gets_a_poller(self):
        holder = LatestFrame()
        sensor = object()
        drone = _decoded_drone(_Reader())
        assert isinstance(_obstacle_frame_poller("tello", drone, ObstacleWiring(sensor=sensor, holder=holder)),
                          DecodedFramePoller)
        assert _obstacle_frame_poller("mavlink", drone, ObstacleWiring(sensor=sensor, holder=holder)) is None
        assert _obstacle_frame_poller("tello", drone, ObstacleWiring(sensor_error="x", holder=holder)) is None
        assert _obstacle_frame_poller("tello", drone, ObstacleWiring(sensor=NullSensor())) is None
        assert _obstacle_frame_poller("tello", drone, ObstacleWiring(sensor=sensor)) is None  # no holder
        fake = FakeTelloController(name="drone-1")
        assert _obstacle_frame_poller("tello", fake, ObstacleWiring(sensor=sensor, holder=holder)) is None


# ---------------------------------------------------------------------------
# R2-B1 and R2-N3: a stalled decoder's frame is never fresh, a blank frame is refused
# ---------------------------------------------------------------------------

#: The marker pixel value of the view before the turn (clear) and after it (a close wall).
PRE_TURN, POST_TURN = 1, 2


def _marked_view(marker, height=300, width=400):
    """A textured frame whose top-left pixel says which view it is."""
    frame = _view(height, width)
    frame[0, 0, 0] = marker
    return frame


class _MarkerGuard:
    """Clear only for the pre-turn view; any other view is a close wall. Records the views it judged."""

    def __init__(self):
        self.judged: list[int] = []

    def check_path(self, frame):
        marker = int(frame[0, 0, 0])
        self.judged.append(marker)
        return _valid_check(safe=marker == PRE_TURN, depth=0.1 if marker == PRE_TURN else 0.9)


class _StalledTello:
    """A real ``TelloController`` (djitellopy on a fake wire) whose decoder the test controls.

    Both feeds of the edge are wired to one holder on an injected clock: the
    frame loop's ``on_frame`` copy feed (``_copying_frame_feed``) and the
    decoder poller. ``get_frame`` is the controller's own, frozen-frame counter
    included.
    """

    def __init__(self, tello_on_wire, first_frame):
        self.clock = FakeClock(100.0)
        self.ctrl, _wire = tello_on_wire()
        self.ctrl.state.is_connected = True
        self.reader = _Reader(first_frame)
        self.ctrl.tello.background_frame_read = self.reader
        self.holder = LatestFrame(self.clock)
        self.ctrl.on_frame(_copying_frame_feed(self.holder))
        self.poller = DecodedFramePoller(self.ctrl, self.holder)
        self.guard = _MarkerGuard()
        # frame_wait_s=0 looks once: the injected clock does not move while the sensor waits.
        self.sensor = ForwardCameraSensor(
            self.holder, None, guard=self.guard, warm=False, clock=self.clock,
            settle_s=0.3, frame_wait_s=0.0, detection_wait_s=0.0,
        )

    def feed(self, order):
        """Run the feeds once each, in *order* (``loop`` is a frame-loop ``get_frame``, ``poller`` a poll)."""
        for which in order:
            if which == "loop":
                assert self.ctrl.get_frame() is not None
            else:
                self.poller.poll_once()

    def close(self):
        self.sensor.close()
        self.ctrl.state.is_connected = False
        self.ctrl.tello.background_frame_read = None


@pytest.fixture
def stalled_tello(tello_on_wire):
    made = []

    def _make(first_frame=None):
        drone = _StalledTello(tello_on_wire, _marked_view(PRE_TURN) if first_frame is None else first_frame)
        made.append(drone)
        return drone

    yield _make
    for drone in made:
        drone.close()


FEED_ORDERS = {
    "loop_then_poller": ("loop", "poller"),
    "poller_then_loop": ("poller", "loop"),
    "loop_only": ("loop",),
    "poller_only": ("poller",),
}


class TestStalledDecoder:
    @pytest.mark.parametrize("before", list(FEED_ORDERS), ids=list(FEED_ORDERS))
    @pytest.mark.parametrize("stalled_polls", [1, 5])
    def test_a_frame_from_before_the_turn_never_clears_the_gate_after_it(self, stalled_tello, before, stalled_polls):
        # The critic's probe: the drone sees a clear view, turns to face a wall, and the decoder stalls.
        drone = stalled_tello()
        drone.feed(FEED_ORDERS[before])
        assert drone.holder.frames_seen == 1
        turn_end = drone.clock()
        drone.clock.advance(0.35)  # past the settle time
        for _ in range(stalled_polls):
            # The decoder holds the pre-turn array; the frame loop hands it to on_frame again.
            drone.feed(("poller", "loop"))
        result = drone.sensor.sense(turn_end, 2.0)
        assert not result.clear
        assert result.unavailable and result.reason == REASON_STALE
        assert drone.guard.judged == []  # the pre-turn view was never judged after the turn
        # The frozen-frame recovery still counts every identical poll after the first.
        loop_polls = FEED_ORDERS[before].count("loop") + stalled_polls
        assert drone.ctrl._frozen_count == loop_polls - 1

    @pytest.mark.parametrize("first", ["loop", "poller"])
    def test_a_frozen_frame_never_refreshes_the_holder_through_either_feed(self, stalled_tello, first):
        drone = stalled_tello()
        drone.feed((first,))
        stamped = drone.holder.latest()
        assert stamped.arrived_at == 100.0
        for _ in range(4):
            drone.clock.advance(0.1)
            drone.feed(("loop", "poller", "loop"))
        assert drone.poller.poll_once() is False
        assert drone.holder.latest() is stamped
        assert drone.holder.frames_seen == 1

    @pytest.mark.parametrize("first", ["loop", "poller"])
    def test_a_new_frame_object_still_updates_the_holder(self, stalled_tello, first):
        drone = stalled_tello()
        drone.feed(("loop", "poller"))
        turn_end = drone.clock()
        drone.clock.advance(0.35)
        drone.feed(("loop", "poller"))
        # The decoder delivers the first view after the turn, as a new array.
        post_turn = _marked_view(POST_TURN)
        drone.reader.frame = post_turn
        drone.feed((first,))
        latest = drone.holder.latest()
        assert latest.arrived_at == pytest.approx(100.35)
        assert latest.frame is not post_turn and int(latest.frame[0, 0, 0]) == POST_TURN
        assert drone.holder.frames_seen == 2
        drone.clock.advance(0.1)
        drone.feed(("poller", "loop"))  # the other feed then sees the same object
        assert drone.holder.latest() is latest and drone.holder.frames_seen == 2
        result = drone.sensor.sense(turn_end, 2.0)
        assert result.blocked and result.cue == CUE_DEPTH
        assert drone.guard.judged == [POST_TURN]

    def test_the_same_pixels_in_a_new_array_are_a_new_frame(self, stalled_tello):
        # Identity, not content, decides: a still scene still gives new frames.
        drone = stalled_tello()
        drone.feed(("loop",))
        drone.clock.advance(0.5)
        drone.reader.frame = drone.reader.frame.copy()
        drone.feed(("poller", "loop"))
        assert drone.holder.frames_seen == 2
        assert drone.holder.latest().arrived_at == pytest.approx(100.5)

    def test_both_feeds_leave_the_frozen_frame_counter_alone(self, tello_on_wire):
        def frozen_counts(with_poller):
            ctrl, _wire = tello_on_wire()
            ctrl.state.is_connected = True
            ctrl.tello.background_frame_read = _Reader(_view(40, 40))
            clock = FakeClock(100.0)
            holder = LatestFrame(clock)
            ctrl.on_frame(_copying_frame_feed(holder))
            poller = DecodedFramePoller(ctrl, holder)
            counts = []
            for _ in range(6):
                clock.advance(0.1)
                if with_poller:
                    poller.poll_once()
                assert ctrl.get_frame() is not None
                counts.append(ctrl._frozen_count)
            ctrl.state.is_connected = False
            ctrl.tello.background_frame_read = None
            return counts, holder.frames_seen, holder.latest().arrived_at

        without = frozen_counts(False)
        with_ = frozen_counts(True)
        assert with_[0] == without[0] == [0, 1, 2, 3, 4, 5]
        assert with_[1] == without[1] == 1  # the frozen frame is fed once, by whichever feed saw it first
        assert with_[2] == without[2] == pytest.approx(100.1)  # and keeps its first arrival time

    def test_the_restarted_decoders_blank_frame_is_refused(self, stalled_tello):
        drone = stalled_tello()
        drone.feed(("loop", "poller"))
        turn_end = drone.clock()
        drone.clock.advance(0.35)
        # A stream restart: a new decoder starts on djitellopy's black frame, a new object.
        drone.reader = _Reader(np.zeros((300, 400, 3), np.uint8))
        drone.ctrl.tello.background_frame_read = drone.reader
        drone.feed(("poller", "loop"))
        assert drone.holder.frames_seen == 2
        assert drone.holder.latest().arrived_at == pytest.approx(100.35)
        result = drone.sensor.sense(turn_end, 2.0)
        assert result.unavailable and result.reason == REASON_STALE
        assert result.detail["cause"] == CAUSE_BLANK_FRAME
        assert drone.guard.judged == []
        # The first real frame after the restart is judged.
        drone.clock.advance(0.05)
        drone.reader.frame = _marked_view(POST_TURN)
        drone.feed(("loop", "poller"))
        assert drone.sensor.sense(turn_end, 2.0).blocked
        assert drone.guard.judged == [POST_TURN]


# ---------------------------------------------------------------------------
# N3: the person cue's stamp is when the frame was obtained
# ---------------------------------------------------------------------------


class TestDetectionStamp:
    def test_stamp_is_taken_before_processing(self):
        seen = {}

        class SlowDetector:
            def detect(self, frame):
                seen["detect_started"] = time.monotonic()
                time.sleep(0.05)
                return [{"bbox": [10, 10, 60, 90], "confidence": 0.9}]

        runner = EdgeRunner(detector=SlowDetector())
        before = time.monotonic()
        runner.process_frame(np.zeros((120, 160, 3), dtype=np.uint8))
        after = time.monotonic()
        assert before <= runner.last_detections_at <= seen["detect_started"]
        assert runner.last_detections_at < after - 0.04

    def test_a_result_of_a_frame_obtained_before_the_threshold_is_not_fresh(self):
        # A frame obtained mid-turn and processed slowly must not pass as a view after the stop.
        clock = FakeClock(1000.0)
        runner = SimpleNamespace(last_detections=([CLOSE_CENTRE], FRAME_W, FRAME_H), last_detections_at=999.9)
        frames = _SlowFrames(clock, 0.0)
        sensor = _budget_sensor(clock, frames, _SlowGuard(clock, 0.1), runner)
        result = sensor.sense(after=999.8, timeout_s=0.5)  # needs a frame obtained at 1000.1 or later
        assert result.person_cue == PERSON_CUE_UNKNOWN
        assert result.clear


# ---------------------------------------------------------------------------
# N11: settings validation
# ---------------------------------------------------------------------------


class _Log:
    def __init__(self):
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, name):
        if name in ("info", "warning", "error", "debug"):
            return lambda event, **fields: self.records.append((name, event, fields))
        raise AttributeError(name)

    def settings(self):
        return {f["variable"]: f["value"] for lv, e, f in self.records
                if e == EVENT_SETTING_NON_DEFAULT and lv == "warning"}


class _StubCameraSensor:
    def __init__(self, frames, runner=None, **kwargs):
        self.frames, self.runner, self.kwargs = frames, runner, kwargs

    def sense(self, after, timeout_s=2.0):  # pragma: no cover - not called here
        raise AssertionError("not used")

    def status(self):
        return {}


@pytest.fixture(autouse=False)
def stub_camera(monkeypatch):
    monkeypatch.setattr(obstacle_camera, "ForwardCameraSensor", _StubCameraSensor)


def _wire(kind, environ, source=None):
    log = _Log()
    if kind == "mavlink":
        args = _build_parser().parse_args(
            ["--drone", "mavlink", "--source", source or "file", "--frame-heading-deg", "0"]
        )
        envelope = _field_envelope(args, {})
        drone = object()
    else:
        args = _build_parser().parse_args(["--source", kind])
        envelope = SafetyEnvelope.from_env(alt_slot_cm=100, environ={})
        drone = FakeTelloController(name="drone-1")
    return _build_obstacle_wiring(args, log, drone, "runner", envelope, environ), log


KINDS = [("tello", None), ("fake", None), ("mavlink", "file"), ("mavlink", "camera")]
MALFORMED = [
    (ENV_OBSTACLE_STEP_CM, "5_0"),
    (ENV_OBSTACLE_STEP_CM, "５０"),  # fullwidth 50
    (ENV_OBSTACLE_STEP_CM, "٥٠"),  # Arabic-Indic 50
    (ENV_OBSTACLE_STEP_CM, "+50"),
    (ENV_OBSTACLE_MIN_SAFE_DEPTH, "0.3_5"),
    (ENV_OBSTACLE_MIN_SAFE_DEPTH, "٠.٥"),
    (ENV_OBSTACLE_MIN_SAFE_DEPTH, "inf"),
    (ENV_OBSTACLE_MIN_SAFE_DEPTH, "1e999"),
    (ENV_OBSTACLE_PERSON_FRACTION, "nan"),
    (ENV_OBSTACLE_PERSON_FRACTION, "0,5"),
    (ENV_OBSTACLE_TURN, "2"),
    (ENV_OBSTACLE_SENSING, "auto"),
    (ENV_FAKE_OBSTACLE_Y_CM, "far"),
]


@pytest.mark.usefixtures("stub_camera")
class TestSettings:
    @pytest.mark.parametrize(("kind", "source"), KINDS)
    @pytest.mark.parametrize(("name", "raw"), MALFORMED)
    @pytest.mark.parametrize("mode", ["", "off"])
    def test_a_malformed_setting_refuses_to_start_whatever_the_mode_kind_or_source(
        self, kind, source, name, raw, mode,
    ):
        environ = {name: raw}
        if mode and name != ENV_OBSTACLE_SENSING:
            environ[ENV_OBSTACLE_SENSING] = mode
        with pytest.raises(SystemExit):
            _wire(kind, environ, source)

    @pytest.mark.parametrize(
        ("name", "good", "bad"),
        [
            (ENV_OBSTACLE_MIN_SAFE_DEPTH, ["0.05", "0.35", "0.9"], ["0.04", "0.95", "1.0", "0", "-0.1"]),
            (ENV_OBSTACLE_PERSON_FRACTION, ["0.2", "0.55", "0.9"], ["0.19", "0.95", "1.0", "0.0"]),
        ],
    )
    def test_thresholds_cannot_switch_sensing_off(self, name, good, bad):
        for raw in good:
            wiring, _log = _wire("tello", {name: raw})
            key = "min_safe_depth" if name == ENV_OBSTACLE_MIN_SAFE_DEPTH else "person_fraction"
            assert wiring.sensor.kwargs[key] == float(raw)
        for raw in bad:
            with pytest.raises(SystemExit):
                _wire("tello", {name: raw})

    def test_the_ranges_match_the_contract(self):
        from flightrisk.drone.obstacle_sensor import (
            OBSTACLE_MIN_SAFE_DEPTH,
            OBSTACLE_MIN_SAFE_DEPTH_RANGE,
            OBSTACLE_PERSON_FRACTION,
            OBSTACLE_PERSON_FRACTION_RANGE,
        )

        assert OBSTACLE_MIN_SAFE_DEPTH_RANGE == (0.05, 0.9)
        assert OBSTACLE_PERSON_FRACTION_RANGE == (0.2, 0.9)
        assert OBSTACLE_MIN_SAFE_DEPTH_RANGE[0] < OBSTACLE_MIN_SAFE_DEPTH < OBSTACLE_MIN_SAFE_DEPTH_RANGE[1]
        assert OBSTACLE_PERSON_FRACTION_RANGE[0] < OBSTACLE_PERSON_FRACTION < OBSTACLE_PERSON_FRACTION_RANGE[1]

    def test_non_default_values_are_logged_loudly_once(self):
        environ = {
            ENV_OBSTACLE_STEP_CM: "30",
            ENV_OBSTACLE_TURN: "0",
            ENV_OBSTACLE_MIN_SAFE_DEPTH: "0.5",
            ENV_OBSTACLE_PERSON_FRACTION: "0.7",
        }
        _wiring, log = _wire("tello", environ)
        assert log.settings() == {
            ENV_OBSTACLE_STEP_CM: 30,
            ENV_OBSTACLE_TURN: False,
            ENV_OBSTACLE_MIN_SAFE_DEPTH: 0.5,
            ENV_OBSTACLE_PERSON_FRACTION: 0.7,
        }
        events = [r for r in log.records if r[1] == EVENT_SETTING_NON_DEFAULT]
        assert len(events) == 4
        assert all(f["default"] is not None for _lv, _e, f in events)

    def test_default_values_are_not_logged_even_when_set_explicitly(self):
        environ = {
            ENV_OBSTACLE_SENSING: "required",
            ENV_OBSTACLE_STEP_CM: "50",
            ENV_OBSTACLE_TURN: "1",
            ENV_OBSTACLE_MIN_SAFE_DEPTH: "0.35",
            ENV_OBSTACLE_PERSON_FRACTION: "0.55",
        }
        _wiring, log = _wire("tello", environ)
        assert log.settings() == {}

    def test_settings_are_logged_under_off_too(self):
        wiring, log = _wire("tello", {ENV_OBSTACLE_SENSING: "off", ENV_OBSTACLE_MIN_SAFE_DEPTH: "0.6"})
        assert isinstance(wiring.sensor, NullSensor)
        assert log.settings() == {ENV_OBSTACLE_SENSING: "off", ENV_OBSTACLE_MIN_SAFE_DEPTH: 0.6}

    def test_the_mavlink_step_default_is_the_field_step(self):
        _wiring, log = _wire("mavlink", {ENV_OBSTACLE_STEP_CM: "500"}, "file")
        assert log.settings() == {}
        _wiring, log = _wire("mavlink", {ENV_OBSTACLE_STEP_CM: "1000"}, "file")
        assert log.settings() == {ENV_OBSTACLE_STEP_CM: 1000}

    def test_only_off_disables_sensing(self):
        for environ in ({}, {ENV_OBSTACLE_MIN_SAFE_DEPTH: "0.9", ENV_OBSTACLE_PERSON_FRACTION: "0.9"},
                        {ENV_OBSTACLE_TURN: "0"}):
            wiring, _log = _wire("tello", environ)
            assert isinstance(wiring.sensor, _StubCameraSensor)
        wiring, _log = _wire("tello", {ENV_OBSTACLE_SENSING: "off"})
        assert isinstance(wiring.sensor, NullSensor)


class TestStatusReportsThresholds:
    def test_defaults_report_nothing(self):
        sensor = _sensor(_holder_with_frame(), None, _FixedGuard(_valid_check()))
        assert sensor.status()["non_default_thresholds"] == {}

    def test_non_default_thresholds_are_reported(self):
        sensor = _sensor(
            _holder_with_frame(), None, _FixedGuard(_valid_check()), min_safe_depth=0.5, person_fraction=0.7,
        )
        assert sensor.status()["non_default_thresholds"] == {"min_safe_depth": 0.5, "person_fraction": 0.7}
        sensor = _sensor(_holder_with_frame(), None, _FixedGuard(_valid_check()), person_fraction=0.6)
        assert sensor.status()["non_default_thresholds"] == {"person_fraction": 0.6}
