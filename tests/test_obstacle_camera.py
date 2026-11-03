"""Tests for the forward camera obstacle sensor (ForwardCameraSensor).

The depth guard is a fake (or ObstacleGuard with torch.hub mocked), so no model
is downloaded and no network is used.
"""
from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from flightrisk.drone import obstacle_camera
from flightrisk.drone.obstacle_camera import ForwardCameraSensor, close_person_in_lane
from flightrisk.drone.obstacle_sensor import (
    ACTION_PERSON,
    ACTION_PERSON_UNKNOWN,
    ACTION_STALE,
    ACTION_TIMEOUT,
    CUE_DEPTH,
    CUE_PERSON,
    EVENT_PERSON_CUE_DOWN,
    EVENT_SENSOR_ERROR,
    OBSTACLE_PERSON_CUE_DOWN_AFTER,
    PERSON_CUE_BLOCKED,
    PERSON_CUE_CLEAR,
    PERSON_CUE_UNKNOWN,
    REASON_ERROR,
    REASON_STALE,
    REASON_TIMEOUT,
    LatestFrame,
    ObstacleSensor,
)

FRAME_W, FRAME_H = 960, 720


def _check(safe=True, depth=0.1, action=None):
    return {
        "safe": safe, "center_depth": depth, "left_depth": 0.1, "right_depth": 0.1,
        "action": action or ("clear" if safe else "go_left"), "confidence": 0.9,
        "center_p90": depth, "center_stat": "p90",
    }


class FakeGuard:
    def __init__(self, result=None):
        self.result = result or _check()
        self.calls = 0
        self.error: BaseException | None = None
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def check_path(self, frame):
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.error is not None:
            raise self.error
        return self.result


def _det(x1, y1, x2, y2):
    return SimpleNamespace(bbox=(x1, y1, x2, y2), confidence=0.9)


def _runner(detections=(), at=None):
    return SimpleNamespace(
        last_detections=(list(detections), FRAME_W, FRAME_H),
        last_detections_at=time.monotonic() if at is None else at,
    )


CLOSE_CENTRE = _det(380, 100, 580, 600)  # 500 of 720 px tall, centred


@pytest.fixture
def frames():
    holder = LatestFrame()
    holder.update(np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8))
    return holder


@pytest.fixture
def checks(monkeypatch):
    seen = []
    monkeypatch.setattr(
        obstacle_camera.obs_events, "obstacle_check",
        lambda check, **kw: seen.append((check, kw)),
    )
    return seen


def _sensor(frames, runner=None, guard=None, **kw):
    kw.setdefault("settle_s", 0.0)
    kw.setdefault("frame_wait_s", 0.05)
    kw.setdefault("detection_wait_s", 0.05)
    kw.setdefault("warm", False)
    sensor = ForwardCameraSensor(frames, runner, guard=guard or FakeGuard(), **kw)
    return sensor


def _after():
    return time.monotonic() - 1.0


def _close(sensor):
    """Close the sensor and wait for its helper thread to end, so a later thread count is not racing it."""
    sensor.close()
    sensor._worker._thread.join(5.0)


class TestConstruction:
    def test_satisfies_the_protocol(self, frames):
        assert isinstance(_sensor(frames), ObstacleSensor)

    def test_default_guard_uses_p90_and_no_cache(self, frames, monkeypatch):
        class Model(torch.nn.Module):
            def forward(self, x):
                # A valid clear view; a uniform map would be degenerate (unavailable).
                depth = torch.ones(1, 256, 256) * 0.1
                depth[0, 0:85, :] = 0.9
                return depth

        class Transforms:
            small_transform = staticmethod(lambda img: torch.zeros(1, 3, 256, 256))

        monkeypatch.setattr(
            "torch.hub.load",
            lambda repo, name, **kw: Model() if name == "MiDaS_small" else Transforms(),
        )
        sensor = ForwardCameraSensor(frames, None, settle_s=0.0, frame_wait_s=0.05)
        assert sensor._guard.center_stat == "p90"
        assert sensor._guard.check_interval == 0.0
        assert sensor.sense(_after()).clear

    def test_model_is_warmed_at_construction(self, frames):
        guard = FakeGuard()
        ForwardCameraSensor(frames, None, guard=guard)
        assert guard.calls == 1

    def test_warm_failure_propagates(self, frames):
        guard = FakeGuard()
        guard.error = RuntimeError("no weights")
        with pytest.raises(RuntimeError):
            ForwardCameraSensor(frames, None, guard=guard)


class TestDepthCue:
    def test_clear_view(self, frames, checks):
        result = _sensor(frames, _runner()).sense(_after())
        assert result.clear
        assert result.cue == CUE_DEPTH
        assert result.person_cue == PERSON_CUE_CLEAR
        assert result.depth_score == pytest.approx(0.1)
        assert result.latency_ms >= 0.0
        assert checks[-1][1]["action"] is None
        assert checks[-1][0]["action"] == "clear"

    def test_blocked_by_depth(self, frames, checks):
        guard = FakeGuard(_check(safe=False, depth=0.8))
        result = _sensor(frames, _runner(), guard).sense(_after())
        assert result.blocked
        assert result.cue == CUE_DEPTH
        assert result.cut_reason == "obstacle"
        assert checks[-1][0]["action"] == "go_left"

    def test_frame_older_than_settle_is_stale(self, checks):
        holder = LatestFrame()
        holder.update(np.zeros((8, 8, 3), dtype=np.uint8))
        sensor = _sensor(holder, _runner(), settle_s=0.3)
        # The frame arrived just now, so it is not newer than after + 0.3.
        result = sensor.sense(time.monotonic())
        assert result.unavailable and result.reason == REASON_STALE
        assert checks[-1][1]["action"] == ACTION_STALE
        assert sensor._guard.calls == 0

    def test_no_frame_at_all_is_stale(self):
        result = _sensor(LatestFrame(), _runner()).sense(_after())
        assert result.unavailable and result.reason == REASON_STALE

    def test_waits_for_a_frame_after_the_settle_time(self):
        holder = LatestFrame()
        sensor = _sensor(holder, _runner(), frame_wait_s=2.0)
        after = time.monotonic()
        timer = threading.Timer(0.1, lambda: holder.update(np.zeros((8, 8, 3), dtype=np.uint8)))
        timer.start()
        try:
            assert sensor.sense(after).clear
        finally:
            timer.join()


class TestPersonGeometry:
    def test_tall_central_box_counts(self):
        assert close_person_in_lane([CLOSE_CENTRE], FRAME_W, FRAME_H)

    def test_box_at_the_fraction_does_not_count(self):
        height = int(0.55 * FRAME_H)
        assert not close_person_in_lane([_det(380, 0, 580, height)], FRAME_W, FRAME_H)
        assert close_person_in_lane([_det(380, 0, 580, height + 2)], FRAME_W, FRAME_H)

    def test_short_box_does_not_count(self):
        assert not close_person_in_lane([_det(380, 300, 580, 600)], FRAME_W, FRAME_H)

    def test_off_centre_box_does_not_count(self):
        assert not close_person_in_lane([_det(20, 100, 220, 600)], FRAME_W, FRAME_H)
        assert not close_person_in_lane([_det(740, 100, 940, 600)], FRAME_W, FRAME_H)

    def test_box_edge_of_the_central_third(self):
        # Centre at x = 640 is the right edge of the middle third (320..640).
        assert close_person_in_lane([_det(540, 100, 740, 600)], FRAME_W, FRAME_H)
        assert not close_person_in_lane([_det(560, 100, 760, 600)], FRAME_W, FRAME_H)

    def test_empty_and_unsized(self):
        assert not close_person_in_lane([], FRAME_W, FRAME_H)
        assert not close_person_in_lane([CLOSE_CENTRE], 0, 0)


class TestPersonCue:
    def test_person_blocks_even_when_depth_is_clear(self, frames, checks):
        result = _sensor(frames, _runner([CLOSE_CENTRE])).sense(_after())
        assert result.blocked
        assert result.cue == CUE_PERSON
        assert result.person_cue == PERSON_CUE_BLOCKED
        assert result.cut_reason == "person"
        assert checks[-1][1]["action"] == ACTION_PERSON

    def test_stale_detections_are_unknown_and_depth_decides(self, frames, checks):
        old = _runner([CLOSE_CENTRE], at=time.monotonic() - 5.0)
        result = _sensor(frames, old).sense(_after() + 0.5)
        # A stale close person neither blocks nor clears.
        assert result.clear
        assert result.person_cue == PERSON_CUE_UNKNOWN
        assert checks[-1][1]["action"] == ACTION_PERSON_UNKNOWN

    def test_stale_detections_never_clear_a_depth_block(self, frames):
        guard = FakeGuard(_check(safe=False, depth=0.9))
        old = _runner([], at=time.monotonic() - 5.0)
        result = _sensor(frames, old, guard).sense(_after() + 0.5)
        assert result.blocked and result.cue == CUE_DEPTH
        assert result.person_cue == PERSON_CUE_UNKNOWN

    def test_no_runner_is_unknown(self, frames):
        result = _sensor(frames, None).sense(_after())
        assert result.clear and result.person_cue == PERSON_CUE_UNKNOWN

    def test_never_detected_is_unknown(self, frames):
        runner = SimpleNamespace(last_detections=None, last_detections_at=None)
        assert _sensor(frames, runner).sense(_after()).person_cue == PERSON_CUE_UNKNOWN

    def test_detection_older_than_settle_is_unknown(self, frames):
        after = time.monotonic()
        runner = _runner([CLOSE_CENTRE], at=after + 0.1)
        sensor = _sensor(frames, runner, settle_s=0.3, frame_wait_s=0.0)
        frames.update(np.zeros((8, 8, 3), dtype=np.uint8))
        frames._latest = type(frames._latest)(frames._latest.frame, after + 1.0)
        assert sensor.sense(after).person_cue == PERSON_CUE_UNKNOWN

    def test_waits_for_a_newer_result(self, frames):
        runner = _runner([], at=time.monotonic() - 5.0)
        sensor = _sensor(frames, runner, detection_wait_s=3.0)

        def publish():
            runner.last_detections = ([CLOSE_CENTRE], FRAME_W, FRAME_H)
            runner.last_detections_at = time.monotonic()

        timer = threading.Timer(0.15, publish)
        timer.start()
        try:
            start = time.monotonic()
            result = sensor.sense(_after() + 0.9)
            assert time.monotonic() - start < 2.5
        finally:
            timer.join()
        assert result.blocked and result.cue == CUE_PERSON

    def test_gives_up_waiting(self, frames):
        runner = _runner([], at=time.monotonic() - 5.0)
        sensor = _sensor(frames, runner, detection_wait_s=0.1)
        start = time.monotonic()
        result = sensor.sense(_after() + 0.9)
        assert result.person_cue == PERSON_CUE_UNKNOWN
        assert time.monotonic() - start < 2.0

    def test_does_not_wait_when_depth_already_blocks(self, frames):
        runner = _runner([], at=time.monotonic() - 5.0)
        guard = FakeGuard(_check(safe=False, depth=0.9))
        sensor = _sensor(frames, runner, guard, detection_wait_s=30.0)
        start = time.monotonic()
        assert sensor.sense(_after() + 0.9).blocked
        assert time.monotonic() - start < 5.0

    def test_person_cue_down_is_logged_once(self, frames, caplog):
        old = _runner([], at=time.monotonic() - 5.0)
        sensor = _sensor(frames, old, detection_wait_s=0.0)
        with caplog.at_level(logging.WARNING):
            for _ in range(OBSTACLE_PERSON_CUE_DOWN_AFTER + 3):
                sensor.sense(_after() + 0.9)
        events = [r for r in caplog.records if getattr(r, "fr_event", "") == EVENT_PERSON_CUE_DOWN]
        assert len(events) == 1
        assert sensor.status()["person_cue_down"] is True

    def test_person_cue_down_needs_a_streak(self, frames, caplog):
        old = _runner([], at=time.monotonic() - 5.0)
        fresh = _runner([])
        sensor = _sensor(frames, old, detection_wait_s=0.0)
        with caplog.at_level(logging.WARNING):
            for i in range(OBSTACLE_PERSON_CUE_DOWN_AFTER * 2):
                sensor._runner = fresh if i % 3 == 2 else old
                sensor.sense(_after() + 0.9 if i % 3 != 2 else _after())
        assert not [r for r in caplog.records if getattr(r, "fr_event", "") == EVENT_PERSON_CUE_DOWN]

    def test_person_cue_down_logs_again_after_recovery(self, frames, caplog):
        old = _runner([], at=time.monotonic() - 5.0)
        sensor = _sensor(frames, old, detection_wait_s=0.0)
        with caplog.at_level(logging.WARNING):
            for _ in range(OBSTACLE_PERSON_CUE_DOWN_AFTER):
                sensor.sense(_after() + 0.9)
            sensor._runner = _runner([])
            sensor.sense(_after())
            sensor._runner = old
            for _ in range(OBSTACLE_PERSON_CUE_DOWN_AFTER):
                sensor.sense(_after() + 0.9)
        events = [r for r in caplog.records if getattr(r, "fr_event", "") == EVENT_PERSON_CUE_DOWN]
        assert len(events) == 2


class TestTimeoutAndThreads:
    def test_hung_model_times_out_without_blocking(self, frames, checks):
        guard = FakeGuard()
        guard.gate = threading.Event()
        sensor = _sensor(frames, _runner(), guard)
        try:
            start = time.monotonic()
            result = sensor.sense(_after(), timeout_s=0.2)
            assert time.monotonic() - start < 5.0
            assert result.unavailable and result.reason == REASON_TIMEOUT
            assert checks[-1][1]["action"] == ACTION_TIMEOUT
            assert sensor.status()["worker_busy"] is True
        finally:
            guard.gate.set()
        _close(sensor)

    def test_busy_worker_times_out_at_once_and_adds_no_thread(self, frames):
        guard = FakeGuard()
        guard.gate = threading.Event()
        sensor = _sensor(frames, _runner(), guard)
        try:
            sensor.sense(_after(), timeout_s=0.1)
            threads = threading.active_count()
            start = time.monotonic()
            for _ in range(5):
                result = sensor.sense(_after(), timeout_s=30.0)
                assert result.unavailable and result.reason == REASON_TIMEOUT
                assert result.detail["worker_busy"] is True
            assert time.monotonic() - start < 5.0
            assert threading.active_count() == threads
            assert guard.calls == 1
        finally:
            guard.gate.set()
        _close(sensor)

    def test_recovers_after_the_hung_call_finishes(self, frames):
        guard = FakeGuard()
        guard.gate = threading.Event()
        sensor = _sensor(frames, _runner(), guard)
        sensor.sense(_after(), timeout_s=0.1)
        guard.gate.set()
        deadline = time.monotonic() + 5.0
        while sensor.status()["worker_busy"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sensor.sense(_after()).clear
        _close(sensor)

    def test_no_thread_per_call(self, frames):
        sensor = _sensor(frames, _runner())
        sensor.sense(_after())
        threads = threading.active_count()
        for _ in range(25):
            assert sensor.sense(_after()).clear
        assert threading.active_count() == threads
        _close(sensor)


class TestErrors:
    def test_guard_exception_is_unavailable_error(self, frames, checks):
        guard = FakeGuard()
        guard.error = RuntimeError("boom")
        result = _sensor(frames, _runner(), guard).sense(_after())
        assert result.unavailable and result.reason == REASON_ERROR
        assert "boom" in result.detail["error"]
        assert checks[-1][1]["action"] == "unavailable"

    def test_malformed_check_is_an_error(self, frames):
        result = _sensor(frames, _runner(), FakeGuard({"nope": 1})).sense(_after())
        assert result.unavailable and result.reason == REASON_ERROR

    def test_error_is_logged_once_per_change(self, frames, caplog):
        guard = FakeGuard()
        guard.error = RuntimeError("boom")
        sensor = _sensor(frames, _runner(), guard)

        def errors():
            return [r for r in caplog.records if getattr(r, "fr_event", "") == EVENT_SENSOR_ERROR]

        with caplog.at_level(logging.ERROR):
            for _ in range(4):
                sensor.sense(_after())
            assert len(errors()) == 1
            guard.error = ValueError("other")
            sensor.sense(_after())
            assert len(errors()) == 2
            guard.error = None
            assert sensor.sense(_after()).clear
            guard.error = ValueError("other")
            sensor.sense(_after())
            assert len(errors()) == 3


class TestStatus:
    def test_before_any_call(self, frames):
        status = _sensor(frames).status()
        assert status["calibrated"] is False
        assert status["obstacle_sensor_ok"] is True
        assert status["obstacle_state"] is None
        assert status["center_stat"] == "p90"

    def test_after_clear_and_failure(self, frames):
        guard = FakeGuard()
        sensor = _sensor(frames, _runner(), guard)
        sensor.sense(_after())
        status = sensor.status()
        assert status["obstacle_state"] == "clear"
        assert status["obstacle_sensor_ok"] is True
        assert status["obstacle_latency_ms"] >= 0.0
        assert status["frames_seen"] == 1
        guard.error = RuntimeError("x")
        sensor.sense(_after())
        status = sensor.status()
        assert status["obstacle_state"] == "unavailable"
        assert status["obstacle_sensor_ok"] is False
        assert status["reason"] == REASON_ERROR

    def test_calibrated_flag_is_configurable(self, frames):
        assert _sensor(frames, calibrated=True).status()["calibrated"] is True


class TestRunnerAttributes:
    def test_process_frame_stamps_last_detections(self):
        from flightrisk.edge import EdgeRunner

        class Detector:
            def detect(self, frame):
                return [{"bbox": [10, 10, 60, 90], "confidence": 0.9}]

        runner = EdgeRunner(detector=Detector())
        assert runner.last_detections is None and runner.last_detections_at is None
        before = time.monotonic()
        runner.process_frame(np.zeros((120, 160, 3), dtype=np.uint8))
        detections, width, height = runner.last_detections
        assert (width, height) == (160, 120)
        assert [d.bbox for d in detections] == [(10, 10, 60, 90)]
        assert before <= runner.last_detections_at <= time.monotonic()

    def test_empty_result_is_still_a_fresh_result(self):
        from flightrisk.edge import EdgeRunner

        class Detector:
            def detect(self, frame):
                return []

        runner = EdgeRunner(detector=Detector())
        runner.process_frame(np.zeros((120, 160, 3), dtype=np.uint8))
        assert runner.last_detections[0] == []
        assert runner.last_detections_at is not None
