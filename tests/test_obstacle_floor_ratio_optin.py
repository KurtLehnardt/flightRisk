"""Tests for the opt-in ``floor_ratio`` obstacle metric (``FLIGHTRISK_OBSTACLE_METRIC``).

The default score must behave exactly as before; ``floor_ratio`` blocks a step
when ``centre_floor_ratio`` is above ``FLIGHTRISK_OBSTACLE_FLOOR_RATIO_MAX`` and
when the view has no floor reference. MiDaS is never loaded: the guard gets a
stand-in model that returns a synthetic depth map.
"""
from __future__ import annotations

import logging
import time

import numpy as np
import pytest
import torch

from flightrisk import fleet_protocol as fp
from flightrisk.drone import obstacle as obstacle_mod
from flightrisk.drone import obstacle_camera
from flightrisk.drone.coordinator import _person_cue
from flightrisk.drone.obstacle import ObstacleGuard, centre_floor_ratio
from flightrisk.drone.obstacle_camera import ForwardCameraSensor
from flightrisk.drone.obstacle_sensor import (
    ACTION_NO_FLOOR_REFERENCE,
    CUT_OBSTACLE,
    CUE_DEPTH,
    CUE_PERSON,
    DEPTH_METRIC_KEY,
    DEPTH_METRIC_MAX_KEY,
    DEPTH_METRIC_VALUE_KEY,
    DEPTH_STATE_BLOCKED,
    DEPTH_STATE_CLEAR,
    DEPTH_STATE_KEY,
    DEPTH_STATE_NO_FLOOR_REFERENCE,
    ENV_OBSTACLE_FLOOR_RATIO_MAX,
    ENV_OBSTACLE_METRIC,
    ENV_OBSTACLE_SENSING,
    EVENT_LANES_ENDED_SENSOR_DOWN,
    EVENT_OBSTACLE_GATE,
    EVENT_OBSTACLE_METRIC,
    EVENT_OBSTACLE_METRIC_UNVALIDATED,
    EVENT_SETTING_NON_DEFAULT,
    FLOOR_RATIO_FIELD_HIGHEST_CLEAR,
    FLOOR_RATIO_FIELD_LOWEST_WALL,
    FLOOR_RATIO_UNVALIDATED,
    OBSTACLE_FLOOR_RATIO_MAX,
    OBSTACLE_FLOOR_RATIO_MAX_RANGE,
    OBSTACLE_METRIC,
    OBSTACLE_METRIC_FLOOR_RATIO,
    OBSTACLE_METRIC_SCORE,
    OBSTACLE_SENSOR_DOWN_S,
    PERSON_CUE_AGE_KEY,
    PERSON_CUE_BLOCKED,
    PERSON_CUE_DOWN_KEY,
    REASON_ERROR,
    REASON_STALE,
    REASON_TIMEOUT,
    TELEMETRY_PERSON_CUE,
    TELEMETRY_SENSOR_OK,
    LatestFrame,
    depth_metric_fields,
    obstacle_metric_settings,
)
from flightrisk.launcher.commands import EdgeToken
from flightrisk import obs_events, obs_metrics
from flightrisk.launcher.preflight import (
    FAIL,
    INFO,
    OBSTACLE_METRIC_CHECK,
    OBSTACLE_METRIC_WARNING,
    WARN,
    obstacle_metric_check,
    run_preflight,
)
from flightrisk.launcher.settings import DroneSettings, LauncherSettings, validate
from scripts import obstacle_replay as replay
from tests.test_fleet_dashboard_obstacle import NODE
from tests.test_fleet_dashboard_obstacle import _run as run_js
from tests.test_fleet_dashboard_obstacle import _sensing
from tests.test_launcher_preflight import _env as preflight_env
from tests.test_obstacle_h6_fixes import _CueSensor, _executor
from tests import test_lane_executor_obstacle as lane_tests
from tests.test_lane_executor_obstacle import (
    TimelineSensor,
    assert_invariants,
    events,
    finished,
    lane_moves,
    lanes_of,
    start,
)
from tests.lane_executor_support import wait_for
from tests.test_review177_sensor import KINDS, _StubCameraSensor, _wire

SIZE = 256
FRAME_W, FRAME_H = 960, 720
#: The keys of a guard result before the metric option existed.
SCORE_RESULT_KEYS = {
    "safe", "center_depth", "left_depth", "right_depth", "action", "confidence",
    "center_p90", "center_stat", "degenerate", "degenerate_reason",
}
#: The keys of a camera sensor result's detail before the metric option existed.
SCORE_DETAIL_KEYS = {"depth_action", "depth_confidence", "center_stat", "min_safe_depth"}


def make_map(centre=0.7, floor=1.0, top=0.1, rest=0.2, left=None, size=SIZE, width=None):
    """A raw inverse-depth map (``size`` rows, ``width`` columns) with the given centre, floor band, top band and left side."""
    h, w = size, width or size
    m = np.full((h, w), rest, dtype=np.float32)
    band = int(round(h * 0.2))
    m[:band, :] = top
    if left is not None:
        m[h // 3:2 * h // 3, : w // 3] = left
    m[h // 3:2 * h // 3, w // 3:2 * w // 3] = centre
    col = int(w * 0.2)
    m[h - band:, col:w - col] = floor
    return m


#: The score blocks it (0.7 > 0.35); the ratio clears it (0.7 <= 0.8).
RATIO_CLEAR = make_map(centre=0.7, floor=1.0)
#: Both block it: ratio 0.9 > 0.8.
RATIO_BLOCKED = make_map(centre=0.9, floor=1.0)
#: The score clears it (a nearer thing on the left sets the scale); the ratio blocks it (1.0 > 0.8).
SCORE_CLEAR_RATIO_BLOCKED = make_map(centre=0.5, floor=0.5, left=2.0)
#: The floor band looks farther than the top band: no floor reference. The score clears it (0.25).
NO_FLOOR = make_map(centre=0.15, floor=0.1, top=0.6)


class _MapModel(torch.nn.Module):
    """A stand-in for MiDaS that returns whatever map the test sets."""

    def __init__(self):
        super().__init__()
        self.depth = RATIO_CLEAR

    def forward(self, x):
        return torch.from_numpy(np.asarray(self.depth, dtype=np.float32)).unsqueeze(0)


@pytest.fixture
def model(monkeypatch):
    stand_in = _MapModel()
    monkeypatch.setattr(
        "flightrisk.drone.midas_model.load_midas_small",
        lambda *a, **kw: (stand_in, lambda img: torch.zeros(1, 3, 8, 8)),
    )
    return stand_in


def _view():
    frame = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    frame[:, : FRAME_W // 2] = 80
    return frame


@pytest.fixture
def frames():
    holder = LatestFrame()
    holder.update(_view())
    return holder


@pytest.fixture
def checks(monkeypatch):
    seen = []
    monkeypatch.setattr(obstacle_camera.obs_events, "obstacle_check", lambda check, **kw: seen.append((check, kw)))
    return seen


@pytest.fixture
def ratios(monkeypatch):
    """The floor ratio histogram's recordings."""
    seen = []

    class Recorder:
        def record(self, value, attributes=None):
            seen.append((value, dict(attributes or {})))

    monkeypatch.setattr(obs_events.M, "obstacle_floor_ratio", Recorder())
    return seen


def _after():
    return time.monotonic() - 1.0


def _sensor(frames, **kw):
    kw.setdefault("settle_s", 0.0)
    kw.setdefault("frame_wait_s", 0.05)
    kw.setdefault("detection_wait_s", 0.05)
    kw.setdefault("warm", False)
    runner = kw.pop("runner", None)
    return ForwardCameraSensor(frames, runner, **kw)


def _guard(metric=OBSTACLE_METRIC_SCORE, **kw):
    return ObstacleGuard(check_interval=0.0, center_stat="p90", metric=metric, **kw)


def _runner(detections=()):
    from types import SimpleNamespace

    return SimpleNamespace(last_detections=(list(detections), FRAME_W, FRAME_H), last_detections_at=time.monotonic())


# ---------------------------------------------------------------------------
# The default score is unchanged
# ---------------------------------------------------------------------------


class TestDefaultUnchanged:
    def test_the_defaults(self):
        assert OBSTACLE_METRIC == OBSTACLE_METRIC_SCORE == "score"
        assert OBSTACLE_FLOOR_RATIO_MAX == 0.7986
        assert OBSTACLE_FLOOR_RATIO_MAX_RANGE == (0.5, 1.0)
        assert ObstacleGuard.__init__.__defaults__[-2:] == (OBSTACLE_METRIC_SCORE, OBSTACLE_FLOOR_RATIO_MAX)
        assert ForwardCameraSensor.__init__.__kwdefaults__["floor_ratio_max"] == OBSTACLE_FLOOR_RATIO_MAX
        assert FLOOR_RATIO_UNVALIDATED == "floor_ratio is unvalidated: 3 outdoor recordings at one height"

    def test_the_default_limit_lies_strictly_between_the_recorded_clear_and_wall_values(self):
        """Review of #206: a limit above the lowest wall value would have called that wall clear."""
        assert FLOOR_RATIO_FIELD_HIGHEST_CLEAR == 0.7978
        assert FLOOR_RATIO_FIELD_LOWEST_WALL == 0.7994
        assert FLOOR_RATIO_FIELD_HIGHEST_CLEAR < OBSTACLE_FLOOR_RATIO_MAX < FLOOR_RATIO_FIELD_LOWEST_WALL
        assert OBSTACLE_FLOOR_RATIO_MAX == pytest.approx(
            (FLOOR_RATIO_FIELD_HIGHEST_CLEAR + FLOOR_RATIO_FIELD_LOWEST_WALL) / 2, abs=1e-12)
        assert FLOOR_RATIO_FIELD_LOWEST_WALL - FLOOR_RATIO_FIELD_HIGHEST_CLEAR == pytest.approx(0.0016, abs=1e-12)
        # The replay's reading at the default limit: the highest clear value is clear, the lowest wall blocked.
        assert replay.is_clear(FLOOR_RATIO_FIELD_HIGHEST_CLEAR, OBSTACLE_FLOOR_RATIO_MAX)
        assert not replay.is_clear(FLOOR_RATIO_FIELD_LOWEST_WALL, OBSTACLE_FLOOR_RATIO_MAX)
        lo, hi = OBSTACLE_FLOOR_RATIO_MAX_RANGE
        assert lo < OBSTACLE_FLOOR_RATIO_MAX < hi

    @pytest.mark.parametrize("depth", [RATIO_CLEAR, RATIO_BLOCKED, SCORE_CLEAR_RATIO_BLOCKED, NO_FLOOR])
    def test_a_default_guard_result_has_the_old_keys_and_the_scores_decision(self, model, depth):
        model.depth = depth
        result = _guard().check_path(_view())
        assert set(result) == SCORE_RESULT_KEYS
        explicit = _guard(metric="score").check_path(_view())
        assert result == explicit
        assert result["safe"] == (result["center_depth"] <= 0.35)

    def test_the_default_decides_with_the_score_where_the_ratio_would_differ(self, model):
        model.depth = RATIO_CLEAR
        assert _guard().check_path(_view())["safe"] is False
        model.depth = SCORE_CLEAR_RATIO_BLOCKED
        assert _guard().check_path(_view())["safe"] is True
        model.depth = NO_FLOOR
        assert _guard().check_path(_view())["safe"] is True

    def test_the_default_sensor_builds_the_guard_as_before(self, frames, monkeypatch):
        built = []

        class Recording:
            def __init__(self, **kw):
                built.append(kw)

            def check_path(self, frame):
                return {}

        monkeypatch.setattr(obstacle_mod, "ObstacleGuard", Recording)
        ForwardCameraSensor(frames, None, warm=False)
        assert built == [{"min_safe_depth": 0.35, "check_interval": 0.0, "center_stat": "p90"}]
        ForwardCameraSensor(frames, None, warm=False, metric=OBSTACLE_METRIC_FLOOR_RATIO, floor_ratio_max=0.9)
        assert built[1] == {"min_safe_depth": 0.35, "check_interval": 0.0, "center_stat": "p90",
                            "metric": OBSTACLE_METRIC_FLOOR_RATIO, "floor_ratio_max": 0.9}

    def test_a_default_sensor_result_status_and_events_are_as_before(self, model, frames, checks, caplog):
        model.depth = SCORE_CLEAR_RATIO_BLOCKED
        sensor = _sensor(frames, runner=_runner())
        with caplog.at_level(logging.INFO, logger="flightrisk.drone.obstacle_camera"):
            result = sensor.sense(_after())
        assert result.clear and result.cue == CUE_DEPTH
        assert set(result.detail) == SCORE_DETAIL_KEYS
        assert result.depth_score == pytest.approx(0.25, abs=1e-6)
        status = sensor.status()
        assert not {DEPTH_METRIC_KEY, DEPTH_METRIC_VALUE_KEY, DEPTH_METRIC_MAX_KEY, DEPTH_STATE_KEY} & set(status)
        assert sensor.non_default_thresholds() == {}
        assert checks[-1][1]["min_safe_depth"] == 0.35
        assert not [r for r in caplog.records if getattr(r, "fr_event", None) == EVENT_OBSTACLE_METRIC]

    def test_the_default_edge_passes_no_metric_to_the_sensor(self, monkeypatch):
        monkeypatch.setattr(obstacle_camera, "ForwardCameraSensor", _StubCameraSensor)
        for environ in ({}, {ENV_OBSTACLE_METRIC: "score"}, {ENV_OBSTACLE_METRIC: " SCORE "}):
            wiring, log = _wire("tello", environ)
            assert "metric" not in wiring.sensor.kwargs and "floor_ratio_max" not in wiring.sensor.kwargs
            assert log.settings() == {}

    def test_default_telemetry_has_only_the_person_cue_keys(self):
        ex = _executor(_CueSensor({"person_cue_down": False, "person_detection_age_s": 0.4}))
        assert ex.telemetry()[TELEMETRY_PERSON_CUE] == {PERSON_CUE_DOWN_KEY: False, PERSON_CUE_AGE_KEY: 0.4}
        assert _person_cue({"person_cue_down": False, "detection_age_s": 0.4, DEPTH_METRIC_KEY: "score"}) == {
            PERSON_CUE_DOWN_KEY: False, PERSON_CUE_AGE_KEY: 0.4}


# ---------------------------------------------------------------------------
# The guard under floor_ratio
# ---------------------------------------------------------------------------


class TestGuardFloorRatio:
    def test_below_the_limit_is_clear(self, model):
        model.depth = RATIO_CLEAR
        result = _guard(OBSTACLE_METRIC_FLOOR_RATIO).check_path(_view())
        assert result["safe"] is True and result["action"] == "clear"
        assert result["floor_ratio"] == pytest.approx(0.7, abs=1e-6)
        assert result["floor_ratio_max"] == OBSTACLE_FLOOR_RATIO_MAX
        assert result["depth_state"] == DEPTH_STATE_CLEAR
        assert result["metric"] == OBSTACLE_METRIC_FLOOR_RATIO
        assert result["center_depth"] > 0.35  # the score is kept for comparison; it would have blocked

    def test_above_the_limit_is_blocked(self, model):
        model.depth = SCORE_CLEAR_RATIO_BLOCKED
        result = _guard(OBSTACLE_METRIC_FLOOR_RATIO).check_path(_view())
        assert result["safe"] is False and result["depth_state"] == DEPTH_STATE_BLOCKED
        assert result["floor_ratio"] == pytest.approx(1.0)
        assert result["action"] in ("go_left", "go_right", "reverse")
        assert result["center_depth"] < 0.35  # the score would have cleared it

    def test_the_limit_itself_is_clear(self, model):
        model.depth = RATIO_CLEAR
        ratio = centre_floor_ratio(RATIO_CLEAR)
        assert _guard(OBSTACLE_METRIC_FLOOR_RATIO, floor_ratio_max=ratio).check_path(_view())["safe"] is True
        assert _guard(OBSTACLE_METRIC_FLOOR_RATIO, floor_ratio_max=ratio - 1e-6).check_path(_view())["safe"] is False

    def test_no_floor_reference_is_blocked(self, model):
        model.depth = NO_FLOOR
        assert centre_floor_ratio(NO_FLOOR) is None
        result = _guard(OBSTACLE_METRIC_FLOOR_RATIO).check_path(_view())
        assert result["safe"] is False
        assert result["floor_ratio"] is None
        assert result["depth_state"] == DEPTH_STATE_NO_FLOOR_REFERENCE
        assert result["action"] == ACTION_NO_FLOOR_REFERENCE
        assert result["degenerate"] is False and result["confidence"] == 0.0

    @pytest.mark.parametrize("depth, reason", [
        (np.full((SIZE, SIZE), 0.3, dtype=np.float32), "uniform"),
        (np.where(np.arange(SIZE * SIZE).reshape(SIZE, SIZE) == 7, np.nan, 0.3).astype(np.float32), "non_finite"),
        (-make_map(), "no_scale"),
    ])
    def test_a_degenerate_map_is_handled_exactly_as_under_the_score(self, model, depth, reason):
        model.depth = depth
        ratio = _guard(OBSTACLE_METRIC_FLOOR_RATIO).check_path(_view())
        score = _guard().check_path(_view())
        assert ratio == score
        assert ratio["degenerate"] is True and ratio["degenerate_reason"] == reason and ratio["safe"] is False

    def test_bad_arguments_are_refused_before_the_model_loads(self, monkeypatch):
        def no_model(*a, **kw):
            raise AssertionError("the model must not load")

        monkeypatch.setattr("flightrisk.drone.midas_model.load_midas_small", no_model)
        with pytest.raises(ValueError):
            ObstacleGuard(metric="ratio")
        for bad in (0.0, -1.0, float("nan"), float("inf"), True, "0.8", None):
            with pytest.raises(ValueError):
                ObstacleGuard(metric=OBSTACLE_METRIC_FLOOR_RATIO, floor_ratio_max=bad)


# ---------------------------------------------------------------------------
# The camera sensor under floor_ratio
# ---------------------------------------------------------------------------


class TestSensorFloorRatio:
    def _floor_sensor(self, frames, **kw):
        return _sensor(frames, metric=OBSTACLE_METRIC_FLOOR_RATIO, **kw)

    def test_below_the_limit_is_clear(self, model, frames, checks, caplog, ratios):
        model.depth = RATIO_CLEAR
        sensor = self._floor_sensor(frames, runner=_runner())
        with caplog.at_level(logging.INFO, logger="flightrisk.drone.obstacle_camera"):
            result = sensor.sense(_after())
        assert result.clear and result.cue == CUE_DEPTH
        assert result.depth_score == pytest.approx(0.7, abs=1e-6)
        assert result.detail["metric"] == OBSTACLE_METRIC_FLOOR_RATIO
        assert result.detail["floor_ratio"] == result.depth_score
        assert result.detail["floor_ratio_max"] == OBSTACLE_FLOOR_RATIO_MAX
        assert result.detail["depth_state"] == DEPTH_STATE_CLEAR
        assert result.detail["score"] > 0.35
        assert checks[-1][1]["min_safe_depth"] is None  # no score-to-threshold histogram for a ratio decision
        (event,) = [r for r in caplog.records if getattr(r, "fr_event", None) == EVENT_OBSTACLE_METRIC]
        assert event.fr_fields["metric"] == OBSTACLE_METRIC_FLOOR_RATIO
        assert event.fr_fields["value"] == result.depth_score
        assert event.fr_fields["limit"] == OBSTACLE_FLOOR_RATIO_MAX
        assert event.fr_fields["state"] == "clear" and event.fr_fields["depth_state"] == DEPTH_STATE_CLEAR
        assert event.fr_fields["device"] == sensor._guard.device_name
        assert event.fr_fields["device"] in ("mps", "cpu", "cuda")
        assert ratios == [(pytest.approx(0.7, abs=1e-6), {"depth_state": "clear"})]

    def test_above_the_limit_is_blocked_with_the_cut_reason_obstacle(self, model, frames, checks):
        model.depth = SCORE_CLEAR_RATIO_BLOCKED
        result = self._floor_sensor(frames, runner=_runner()).sense(_after())
        assert result.blocked and result.cue == CUE_DEPTH and result.cut_reason == "obstacle"
        assert result.detail["depth_state"] == DEPTH_STATE_BLOCKED
        assert result.depth_score == pytest.approx(1.0)

    def test_a_lower_limit_blocks_what_the_default_limit_clears(self, model, frames):
        model.depth = RATIO_CLEAR
        assert self._floor_sensor(frames).sense(_after()).clear
        result = self._floor_sensor(frames, floor_ratio_max=0.6).sense(_after())
        assert result.blocked and result.detail["floor_ratio_max"] == 0.6

    def test_no_floor_reference_is_blocked_never_clear_or_unavailable(self, model, frames, checks, caplog, ratios):
        model.depth = NO_FLOOR
        sensor = self._floor_sensor(frames, runner=_runner())
        with caplog.at_level(logging.INFO, logger="flightrisk.drone.obstacle_camera"):
            result = sensor.sense(_after())
        assert result.blocked and not result.unavailable
        assert result.cue == CUE_DEPTH and result.cut_reason == "obstacle" and result.reason == ""
        assert result.depth_score is None
        assert result.detail["depth_state"] == DEPTH_STATE_NO_FLOOR_REFERENCE
        assert result.detail["floor_ratio"] is None
        assert checks[-1][0]["action"] == ACTION_NO_FLOOR_REFERENCE
        assert sensor.status()["obstacle_sensor_ok"] is True
        events = [r for r in caplog.records if getattr(r, "fr_event", None) == EVENT_OBSTACLE_METRIC]
        assert events[-1].fr_fields["depth_state"] == DEPTH_STATE_NO_FLOOR_REFERENCE
        assert events[-1].fr_fields["state"] == "blocked"
        assert ratios == []  # counted as the action no_floor_reference instead

    def test_a_close_person_still_blocks_and_names_the_person(self, model, frames):
        from types import SimpleNamespace

        model.depth = RATIO_CLEAR
        close = SimpleNamespace(bbox=(380, 100, 580, 600), confidence=0.9)
        result = self._floor_sensor(frames, runner=_runner([close])).sense(_after())
        assert result.blocked and result.cue == CUE_PERSON and result.person_cue == PERSON_CUE_BLOCKED
        assert result.detail["depth_state"] == DEPTH_STATE_CLEAR

    def test_a_degenerate_map_is_still_unavailable(self, model, frames):
        model.depth = np.full((SIZE, SIZE), 0.3, dtype=np.float32)
        result = self._floor_sensor(frames).sense(_after())
        assert result.unavailable and result.reason == REASON_ERROR
        assert "degenerate depth map: uniform" in result.detail["error"]

    def test_a_blank_frame_is_still_stale(self, model):
        holder = LatestFrame()
        holder.update(np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8))
        result = self._floor_sensor(holder).sense(_after())
        assert result.unavailable and result.reason == REASON_STALE

    def test_a_timeout_is_still_a_timeout(self, model, frames):
        import threading

        gate = threading.Event()

        class Slow:
            def check_path(self, frame):
                gate.wait(5)
                return {}

        sensor = self._floor_sensor(frames, guard=Slow())
        try:
            result = sensor.sense(_after(), timeout_s=0.1)
            assert result.unavailable and result.reason == REASON_TIMEOUT
        finally:
            gate.set()
            sensor.close()

    def test_a_guard_that_did_not_judge_with_the_ratio_is_a_failure_to_look(self, model, frames):
        result = self._floor_sensor(frames, guard=_guard()).sense(_after())
        assert result.unavailable and result.reason == REASON_ERROR
        assert "did not judge with the floor_ratio metric" in result.detail["error"]

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), "0.5", True])
    def test_a_ratio_that_is_not_a_finite_number_is_a_failure_to_look(self, frames, bad):
        class Bad:
            def check_path(self, frame):
                return {"safe": True, "center_depth": 0.1, "left_depth": 0.1, "right_depth": 0.1, "action": "clear",
                        "confidence": 0.5, "center_p90": 0.1, "center_stat": "p90", "degenerate": False,
                        "degenerate_reason": "", "metric": OBSTACLE_METRIC_FLOOR_RATIO, "floor_ratio": bad,
                        "floor_ratio_max": 0.8, "depth_state": "clear"}

        result = self._floor_sensor(frames, guard=Bad()).sense(_after())
        assert result.unavailable and result.reason == REASON_ERROR

    def test_the_sensors_limit_also_blocks_an_injected_guard_that_says_clear(self, model, frames):
        model.depth = RATIO_CLEAR  # ratio 0.7: the guard's 0.8 clears it, the sensor's 0.6 does not
        guard = _guard(OBSTACLE_METRIC_FLOOR_RATIO)
        result = self._floor_sensor(frames, guard=guard, floor_ratio_max=0.6).sense(_after())
        assert result.blocked and result.detail["depth_state"] == DEPTH_STATE_BLOCKED

    def test_status_reports_the_metric_of_the_latest_gate(self, model, frames):
        sensor = self._floor_sensor(frames, floor_ratio_max=0.9)
        status = sensor.status()
        assert status[DEPTH_METRIC_KEY] == OBSTACLE_METRIC_FLOOR_RATIO
        assert status[DEPTH_METRIC_VALUE_KEY] is None and status[DEPTH_STATE_KEY] is None
        assert status[DEPTH_METRIC_MAX_KEY] == 0.9
        assert sensor.non_default_thresholds() == {"floor_ratio_max": 0.9}
        model.depth = RATIO_CLEAR
        sensor.sense(_after())
        status = sensor.status()
        assert status[DEPTH_METRIC_VALUE_KEY] == pytest.approx(0.7, abs=1e-6)
        assert status[DEPTH_STATE_KEY] == DEPTH_STATE_CLEAR
        model.depth = NO_FLOOR
        sensor.sense(_after())
        assert sensor.status()[DEPTH_METRIC_VALUE_KEY] is None
        assert sensor.status()[DEPTH_STATE_KEY] == DEPTH_STATE_NO_FLOOR_REFERENCE
        # A gate that could not look (no frame captured after its settle time) leaves no value behind.
        model.depth = RATIO_CLEAR
        sensor.sense(_after())
        assert sensor.status()[DEPTH_METRIC_VALUE_KEY] is not None
        stale = sensor.sense(time.monotonic() + 10.0, timeout_s=0.1)
        assert stale.unavailable and stale.reason == REASON_STALE
        assert sensor.status()[DEPTH_METRIC_VALUE_KEY] is None and sensor.status()[DEPTH_STATE_KEY] is None

    def test_an_unknown_metric_is_refused(self, frames):
        with pytest.raises(ValueError):
            _sensor(frames, guard=object(), metric="ratio")

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -0.8, "0.8", True, None])
    def test_a_bad_limit_is_refused(self, frames, bad):
        with pytest.raises(ValueError):
            _sensor(frames, guard=object(), metric=OBSTACLE_METRIC_FLOOR_RATIO, floor_ratio_max=bad)


# ---------------------------------------------------------------------------
# Parity: the replay tool and the live gate compute the same value
# ---------------------------------------------------------------------------


class TestReplayParity:
    @pytest.mark.parametrize("depth", [RATIO_CLEAR, RATIO_BLOCKED, SCORE_CLEAR_RATIO_BLOCKED, NO_FLOOR,
                                       make_map(centre=0.83, floor=0.97, top=0.05, rest=0.31)])
    def test_the_same_map_gives_the_same_ratio(self, model, frames, depth):
        model.depth = depth
        guard = replay.build_guard(0.35)
        result = guard.check_path(_view())
        offline = replay.frame_metrics(result, replay.depth_map_of(guard))["centre_floor_ratio"]

        live = _sensor(frames, metric=OBSTACLE_METRIC_FLOOR_RATIO).sense(_after())
        assert live.depth_score == offline
        assert live.detail["floor_ratio"] == offline
        # The replay's reading of the same value and the live decision agree.
        assert replay.is_clear(offline, OBSTACLE_FLOOR_RATIO_MAX) == live.clear

    @staticmethod
    def _offline(model, depth):
        model.depth = depth
        guard = replay.build_guard(0.35)
        return replay.frame_metrics(guard.check_path(_view()), replay.depth_map_of(guard))

    def test_a_192_by_256_map_gives_the_same_ratio(self, model, frames):
        depth = make_map(centre=0.74, floor=0.95, top=0.08, rest=0.27, size=192, width=256)
        offline = self._offline(model, depth)["centre_floor_ratio"]
        assert offline == centre_floor_ratio(depth)
        live = _sensor(frames, metric=OBSTACLE_METRIC_FLOOR_RATIO).sense(_after())
        assert live.depth_score == offline and live.clear == replay.is_clear(offline, OBSTACLE_FLOOR_RATIO_MAX)

    def test_a_ratio_exactly_at_the_limit_is_clear_in_both(self, model, frames):
        depth = make_map(centre=0.7891, floor=0.9883, top=0.1, rest=0.25)
        offline = self._offline(model, depth)["centre_floor_ratio"]
        at_limit = _sensor(frames, metric=OBSTACLE_METRIC_FLOOR_RATIO, floor_ratio_max=offline).sense(_after())
        assert at_limit.clear and replay.is_clear(offline, offline)
        just_below = float(np.nextafter(offline, 0.0))
        under = _sensor(frames, metric=OBSTACLE_METRIC_FLOOR_RATIO, floor_ratio_max=just_below).sense(_after())
        assert under.blocked and not replay.is_clear(offline, just_below)
        assert under.depth_score == at_limit.depth_score == offline

    def test_a_degenerate_map_is_unavailable_live_but_still_has_a_ratio_offline(self, model, frames):
        """The intended difference: the replay reports a ratio for every map; the gate never judges a degenerate one."""
        depth = np.full((SIZE, SIZE), 0.3, dtype=np.float32)
        offline = self._offline(model, depth)
        assert offline["score"] is None
        assert offline["centre_floor_ratio"] == pytest.approx(1.0)
        live = _sensor(frames, metric=OBSTACLE_METRIC_FLOOR_RATIO).sense(_after())
        assert live.unavailable and live.reason == REASON_ERROR
        assert live.depth_score is None and "floor_ratio" not in live.detail


# ---------------------------------------------------------------------------
# The replay report and the histogram name the things a limit this close depends on
# ---------------------------------------------------------------------------


class TestDeviceAndHistogram:
    def test_the_guard_names_its_compute_device(self, model):
        guard = _guard(OBSTACLE_METRIC_FLOOR_RATIO)
        assert guard.device_name == guard._device.type
        assert guard.device_name in ("mps", "cpu", "cuda")

    def test_the_replay_report_names_the_compute_device(self, model, tmp_path):
        from tests.test_obstacle_metrics import write_recording

        write_recording(tmp_path / "a", [30, 40, 50])
        guard = replay.build_guard(0.35)
        out = __import__("io").StringIO()
        report = replay.run_metrics_report([("a", str(tmp_path / "a"))], 1, 0.35, guard=guard, out=out)
        assert report["device"] == guard.device_name
        assert f"Depth model device: {guard.device_name}" in out.getvalue()
        assert "replay one recording on both laptops" in out.getvalue()

    def test_a_guard_without_a_device_is_reported_as_unknown(self, tmp_path):
        from tests.test_obstacle_metrics import FakeGuard, write_recording

        write_recording(tmp_path / "a", [30, 40, 50])
        report = replay.run_metrics_report([("a", str(tmp_path / "a"))], 1, 0.35, guard=FakeGuard(),
                                           out=__import__("io").StringIO())
        assert report["device"] == "unknown"

    def test_the_histogram_has_fine_buckets_around_the_default_limit(self):
        buckets = obs_metrics.HISTOGRAM_BUCKETS["flightrisk.obstacle.floor_ratio"]
        assert buckets == sorted(buckets)
        assert OBSTACLE_FLOOR_RATIO_MAX in buckets
        assert [b for b in buckets if FLOOR_RATIO_FIELD_HIGHEST_CLEAR - 0.01 <= b <= FLOOR_RATIO_FIELD_LOWEST_WALL + 0.01]

    def test_the_histogram_skips_what_is_not_a_finite_ratio(self, ratios):
        for value in (None, float("nan"), float("inf"), True, "0.7"):
            obs_events.obstacle_floor_ratio(value, depth_state="blocked")
        obs_events.obstacle_floor_ratio(0.81, depth_state="blocked")
        assert ratios == [(0.81, {"depth_state": "blocked"})]


# ---------------------------------------------------------------------------
# On the lane executor: views without a floor reference are obstacles, never a sensor that is down
# ---------------------------------------------------------------------------


#: The lane executor rig of ``tests/test_lane_executor_obstacle.py``, as a fixture of this module.
rigs = lane_tests.rigs


class _ReplayedSensor(TimelineSensor):
    """Returns a result the real camera sensor gave, each call taking ``call_s`` on the fake clock."""

    def __init__(self, timeline, pose_provider, clock, call_s, result):
        super().__init__(timeline, pose_provider)
        self.clock, self.call_s, self.result = clock, call_s, result

    def sense(self, after, timeout_s=2.0):
        self.clock.advance(self.call_s)
        self.queue(self.result)
        return super().sense(after, timeout_s)


def test_views_without_a_floor_reference_for_longer_than_sensor_down_cut_lanes_as_obstacles(model, frames, rigs):
    model.depth = NO_FLOOR
    no_floor = _sensor(frames, metric=OBSTACLE_METRIC_FLOOR_RATIO).sense(_after())
    assert no_floor.blocked and no_floor.detail["depth_state"] == DEPTH_STATE_NO_FLOOR_REFERENCE

    rig = rigs(sensor=lambda timeline, envelope, clock: _ReplayedSensor(
        timeline, envelope.pose_snapshot, clock, 4.0, no_floor))
    lanes = lanes_of(("forward", 200), ("right", 100), ("back", 200), ("right", 100), ("forward", 200))
    started = rig.clock()
    start(rig, lanes)
    assert wait_for(lambda: finished(rig)), rig.timeline
    gates = events(rig, EVENT_OBSTACLE_GATE)
    assert len(gates) >= 3 and all(g["state"] == "blocked" for g in gates)
    assert rig.clock() - started > OBSTACLE_SENSOR_DOWN_S
    cuts = rig.executor.obstacle_cuts
    assert len(cuts) >= 3 and all(c.reason == CUT_OBSTACLE for c in cuts)
    assert events(rig, EVENT_LANES_ENDED_SENSOR_DOWN) == []
    assert rig.executor.obstacle_status()[TELEMETRY_SENSOR_OK] is True
    assert lane_moves(rig) == []
    assert_invariants(rig)


# ---------------------------------------------------------------------------
# Environment validation: the edge and the launcher
# ---------------------------------------------------------------------------

GOOD = [
    ({}, (OBSTACLE_METRIC_SCORE, OBSTACLE_FLOOR_RATIO_MAX)),
    ({ENV_OBSTACLE_METRIC: ""}, (OBSTACLE_METRIC_SCORE, OBSTACLE_FLOOR_RATIO_MAX)),
    ({ENV_OBSTACLE_METRIC: "score"}, (OBSTACLE_METRIC_SCORE, OBSTACLE_FLOOR_RATIO_MAX)),
    ({ENV_OBSTACLE_METRIC: " Floor_Ratio "}, (OBSTACLE_METRIC_FLOOR_RATIO, OBSTACLE_FLOOR_RATIO_MAX)),
    ({ENV_OBSTACLE_METRIC: "floor_ratio", ENV_OBSTACLE_FLOOR_RATIO_MAX: "0.5"}, (OBSTACLE_METRIC_FLOOR_RATIO, 0.5)),
    ({ENV_OBSTACLE_METRIC: "floor_ratio", ENV_OBSTACLE_FLOOR_RATIO_MAX: "1.0"}, (OBSTACLE_METRIC_FLOOR_RATIO, 1.0)),
    ({ENV_OBSTACLE_FLOOR_RATIO_MAX: "7.978e-1"}, (OBSTACLE_METRIC_SCORE, 0.7978)),
]
BAD = [
    {ENV_OBSTACLE_METRIC: "ratio"},
    {ENV_OBSTACLE_METRIC: "floor-ratio"},
    {ENV_OBSTACLE_METRIC: "centre_floor_ratio"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "0.49"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "1.01"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "1.5"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "0"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "-0.8"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "0.8_0"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "٠.٨"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "nan"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "inf"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "1e999"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "0,8"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "high", ENV_OBSTACLE_METRIC: "floor_ratio"},
]


class TestEnvironment:
    @pytest.mark.parametrize("environ, expected", GOOD)
    def test_good_values(self, environ, expected):
        assert obstacle_metric_settings(environ) == expected

    @pytest.mark.parametrize("environ", BAD)
    def test_bad_values_name_the_variable(self, environ):
        with pytest.raises(ValueError) as exc:
            obstacle_metric_settings(environ)
        assert next(iter(environ)) in str(exc.value)

    @pytest.mark.parametrize(("kind", "source"), KINDS)
    @pytest.mark.parametrize("environ", BAD)
    @pytest.mark.parametrize("mode", ["", "off"])
    def test_the_edge_refuses_to_start_whatever_the_mode_kind_or_source(self, monkeypatch, kind, source, environ,
                                                                        mode):
        monkeypatch.setattr(obstacle_camera, "ForwardCameraSensor", _StubCameraSensor)
        environ = dict(environ)
        if mode:
            environ[ENV_OBSTACLE_SENSING] = mode
        with pytest.raises(SystemExit) as exc:
            _wire(kind, environ, source)
        assert "Refusing to start" in str(exc.value)

    def test_the_edge_passes_the_opt_in_to_the_sensor_and_logs_it_loudly(self, monkeypatch):
        monkeypatch.setattr(obstacle_camera, "ForwardCameraSensor", _StubCameraSensor)
        wiring, log = _wire("tello", {ENV_OBSTACLE_METRIC: "floor_ratio", ENV_OBSTACLE_FLOOR_RATIO_MAX: "0.85"})
        assert wiring.sensor.kwargs["metric"] == OBSTACLE_METRIC_FLOOR_RATIO
        assert wiring.sensor.kwargs["floor_ratio_max"] == 0.85
        assert log.settings() == {ENV_OBSTACLE_METRIC: OBSTACLE_METRIC_FLOOR_RATIO, ENV_OBSTACLE_FLOOR_RATIO_MAX: 0.85}
        wiring, log = _wire("tello", {ENV_OBSTACLE_METRIC: "floor_ratio"})
        assert wiring.sensor.kwargs["floor_ratio_max"] == OBSTACLE_FLOOR_RATIO_MAX
        assert log.settings() == {ENV_OBSTACLE_METRIC: OBSTACLE_METRIC_FLOOR_RATIO}

    def test_the_edge_warns_once_at_startup_that_floor_ratio_is_unvalidated(self, monkeypatch):
        monkeypatch.setattr(obstacle_camera, "ForwardCameraSensor", _StubCameraSensor)
        _wiring, log = _wire("tello", {ENV_OBSTACLE_METRIC: "floor_ratio"})
        (warning,) = [(lv, f) for lv, e, f in log.records if e == EVENT_OBSTACLE_METRIC_UNVALIDATED]
        assert warning[0] == "warning"
        assert warning[1]["detail"].startswith(FLOOR_RATIO_UNVALIDATED)
        assert warning[1]["floor_ratio_max"] == OBSTACLE_FLOOR_RATIO_MAX
        assert "no effect" not in warning[1]["detail"]
        _wiring, log = _wire("tello", {ENV_OBSTACLE_METRIC: "floor_ratio", ENV_OBSTACLE_SENSING: "off"})
        (warning,) = [f for lv, e, f in log.records if e == EVENT_OBSTACLE_METRIC_UNVALIDATED]
        assert "no effect while obstacle sensing is off" in warning["detail"]
        for environ in ({}, {ENV_OBSTACLE_METRIC: "score"}, {ENV_OBSTACLE_FLOOR_RATIO_MAX: "0.9"}):
            _wiring, log = _wire("tello", environ)
            assert not [e for _lv, e, _f in log.records if e == EVENT_OBSTACLE_METRIC_UNVALIDATED]

    def test_a_limit_set_under_the_score_is_named_as_having_no_effect(self, monkeypatch):
        monkeypatch.setattr(obstacle_camera, "ForwardCameraSensor", _StubCameraSensor)
        _wiring, log = _wire("tello", {ENV_OBSTACLE_FLOOR_RATIO_MAX: "0.9"})
        (fields,) = [f for _lv, e, f in log.records if e == EVENT_SETTING_NON_DEFAULT]
        assert fields["variable"] == ENV_OBSTACLE_FLOOR_RATIO_MAX
        assert "no effect while FLIGHTRISK_OBSTACLE_METRIC is 'score'" in fields["detail"]
        _wiring, log = _wire("tello", {ENV_OBSTACLE_METRIC: "floor_ratio", ENV_OBSTACLE_FLOOR_RATIO_MAX: "0.9"})
        details = [f["detail"] for _lv, e, f in log.records if e == EVENT_SETTING_NON_DEFAULT]
        assert len(details) == 2 and not any("no effect" in d for d in details)

    @pytest.mark.parametrize("environ", BAD)
    def test_the_launcher_refuses_what_the_edge_refuses(self, environ):
        issues = [i for i in validate(LauncherSettings(), environ=environ) if i.field == "obstacle_metric"]
        assert len(issues) == 1 and issues[0].level == "error"
        assert next(iter(environ)) in issues[0].message

    @pytest.mark.parametrize("environ, _expected", GOOD)
    def test_the_launcher_accepts_what_the_edge_accepts(self, environ, _expected):
        assert not [i for i in validate(LauncherSettings(), environ=environ) if i.field == "obstacle_metric"]

    def test_the_launcher_checks_nothing_when_no_edge_runs_here(self):
        remote = LauncherSettings(drones=(DroneSettings(run_here=False),), network="cable")
        assert not [i for i in validate(remote, environ=BAD[0]) if i.field == "obstacle_metric"]


# ---------------------------------------------------------------------------
# Telemetry: inside the existing person cue object, never a new field
# ---------------------------------------------------------------------------

_FLOOR_STATUS = {
    "person_cue_down": False, "person_detection_age_s": 0.4,
    DEPTH_METRIC_KEY: OBSTACLE_METRIC_FLOOR_RATIO, DEPTH_METRIC_VALUE_KEY: 0.7123,
    DEPTH_METRIC_MAX_KEY: 0.8, DEPTH_STATE_KEY: DEPTH_STATE_CLEAR,
}


class TestTelemetry:
    def test_the_metric_rides_in_the_person_cue_object(self):
        ex = _executor(_CueSensor(_FLOOR_STATUS))
        cue = ex.telemetry()[TELEMETRY_PERSON_CUE]
        assert cue == {PERSON_CUE_DOWN_KEY: False, PERSON_CUE_AGE_KEY: 0.4, DEPTH_METRIC_KEY: "floor_ratio",
                       DEPTH_METRIC_VALUE_KEY: 0.7123, DEPTH_METRIC_MAX_KEY: 0.8, DEPTH_STATE_KEY: "clear"}

    def test_no_top_level_field_is_added(self):
        default = _executor(_CueSensor({"person_cue_down": False, "person_detection_age_s": 0.4}))
        opted = _executor(_CueSensor(_FLOOR_STATUS))
        for ex in (default, opted):
            ex._v2_telemetry = True
            ex._obstacle_state, ex._obstacle_latency_ms = "blocked", 210.0
        a, b = default.telemetry(), opted.telemetry()
        assert set(a) == set(b)
        assert len(b) <= fp.MAX_FIELDS
        assert fp.validate_message(b, proto=2) == "telemetry"

    def test_the_ground_keeps_the_cleaned_metric(self):
        sent = {"person_cue_down": False, "detection_age_s": 0.4, DEPTH_METRIC_KEY: "floor_ratio",
                DEPTH_METRIC_VALUE_KEY: None, DEPTH_METRIC_MAX_KEY: 0.8, DEPTH_STATE_KEY: "no_floor_reference",
                "extra": "dropped"}
        assert _person_cue(sent) == {PERSON_CUE_DOWN_KEY: False, PERSON_CUE_AGE_KEY: 0.4,
                                     DEPTH_METRIC_KEY: "floor_ratio", DEPTH_METRIC_VALUE_KEY: None,
                                     DEPTH_METRIC_MAX_KEY: 0.8, DEPTH_STATE_KEY: "no_floor_reference"}

    @pytest.mark.parametrize("junk", [
        {DEPTH_METRIC_KEY: "magic"},
        {DEPTH_METRIC_KEY: OBSTACLE_METRIC_SCORE, DEPTH_METRIC_VALUE_KEY: 0.3},
        {DEPTH_METRIC_KEY: None},
        "not a dict",
    ])
    def test_an_unknown_or_default_metric_adds_nothing(self, junk):
        assert depth_metric_fields(junk) == {}

    def test_bad_values_are_cleaned(self):
        out = depth_metric_fields({DEPTH_METRIC_KEY: "floor_ratio", DEPTH_METRIC_VALUE_KEY: float("nan"),
                                   DEPTH_METRIC_MAX_KEY: True, DEPTH_STATE_KEY: "maybe"})
        assert out == {DEPTH_METRIC_KEY: "floor_ratio", DEPTH_METRIC_VALUE_KEY: None, DEPTH_METRIC_MAX_KEY: None,
                       DEPTH_STATE_KEY: None}

    def test_the_real_sensor_status_reaches_the_wire(self, model, frames):
        model.depth = RATIO_BLOCKED
        sensor = _sensor(frames, metric=OBSTACLE_METRIC_FLOOR_RATIO)
        sensor.sense(_after())
        ex = _executor(sensor)
        cue = ex.telemetry()[TELEMETRY_PERSON_CUE]
        assert cue[DEPTH_METRIC_KEY] == "floor_ratio" and cue[DEPTH_STATE_KEY] == DEPTH_STATE_BLOCKED
        assert cue[DEPTH_METRIC_VALUE_KEY] == pytest.approx(0.9, abs=1e-6)
        assert _person_cue(cue) == cue


# ---------------------------------------------------------------------------
# The launcher's preflight warning
# ---------------------------------------------------------------------------


class TestLauncherWarning:
    def test_floor_ratio_is_a_warning_that_does_not_block(self):
        env = preflight_env(environ={ENV_OBSTACLE_METRIC: "floor_ratio", ENV_OBSTACLE_FLOOR_RATIO_MAX: "0.85"})
        checks = run_preflight(LauncherSettings(), EdgeToken(None, None), env, probe_drones=False)
        (row,) = [c for c in checks if c.name == OBSTACLE_METRIC_CHECK]
        assert row.status == WARN
        assert row.detail.startswith("Opt-in metric; threshold not validated indoors (H1)")
        assert "3 outdoor recordings at one height" in row.detail
        assert OBSTACLE_METRIC_WARNING == "Opt-in metric; threshold not validated indoors (H1)"
        assert "0.85" in row.detail and "LAND ALL" in row.detail
        assert row.data == {"metric": "floor_ratio", "floor_ratio_max": 0.85}

    @pytest.mark.parametrize("environ", [{}, {ENV_OBSTACLE_METRIC: "score"}])
    def test_the_default_adds_no_row(self, environ):
        checks = run_preflight(LauncherSettings(), EdgeToken(None, None), preflight_env(environ=environ),
                               probe_drones=False)
        assert not [c for c in checks if c.name == OBSTACLE_METRIC_CHECK]

    def test_no_row_when_no_edge_runs_here(self):
        remote = LauncherSettings(drones=(DroneSettings(run_here=False),), network="cable")
        checks = run_preflight(remote, EdgeToken(None, None),
                               preflight_env(environ={ENV_OBSTACLE_METRIC: "floor_ratio"}), probe_drones=False)
        assert not [c for c in checks if c.name == OBSTACLE_METRIC_CHECK]

    def test_sensing_off_makes_it_info(self):
        env = preflight_env(environ={ENV_OBSTACLE_METRIC: "floor_ratio", ENV_OBSTACLE_SENSING: " OFF "})
        checks = run_preflight(LauncherSettings(), EdgeToken(None, None), env, probe_drones=False)
        (row,) = [c for c in checks if c.name == OBSTACLE_METRIC_CHECK]
        assert row.status == INFO and "not used" in row.detail

    def test_a_value_the_edge_would_refuse_fails(self):
        row = obstacle_metric_check({ENV_OBSTACLE_METRIC: "ratio"})
        assert row.status == FAIL and ENV_OBSTACLE_METRIC in row.detail


# ---------------------------------------------------------------------------
# The dashboard: a short note in the SENSE badge
# ---------------------------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
class TestDashboardNote:
    def test_the_sense_badge_names_the_opt_in_metric(self, tmp_path):
        cue = {"person_cue_down": False, "detection_age_s": 0.4}
        metric = {**cue, "depth_metric": "floor_ratio", "depth_metric_max": OBSTACLE_FLOOR_RATIO_MAX}
        value, near, no_floor, stale, default = run_js(tmp_path, [
            ("senseBadge", [_sensing(obstacle_person_cue={**metric, "depth_metric_value": 0.712,
                                                          "depth_state": "clear"})]),
            ("senseBadge", [_sensing(obstacle_person_cue={**metric, "depth_metric_value": 0.79834,
                                                          "depth_state": "clear"})]),
            ("senseBadge", [_sensing(obstacle_state="blocked", obstacle_person_cue={
                **metric, "depth_metric_value": None, "depth_state": "no_floor_reference"})]),
            ("senseBadge", [_sensing(obstacle_sensor_ok=False, obstacle_person_cue={
                **metric, "depth_metric_value": None, "depth_state": None})]),
            ("senseBadge", [_sensing(obstacle_person_cue=cue)]),
        ])
        assert value["text"] == "SENSE ok 220 ms, floor ratio (unvalidated) 0.712 / 0.7986"
        assert "3 outdoor recordings at one height" in value["title"]
        # Four decimals: three would show 0.798 for values on both sides of the limit.
        assert near["text"] == "SENSE ok 220 ms, floor ratio (unvalidated) 0.7983 / 0.7986"
        assert no_floor["text"] == "SENSE ok 220 ms, floor ratio (unvalidated) no floor / 0.7986"
        assert stale["text"] == "SENSE STALE, floor ratio (unvalidated) -- / 0.7986"
        assert default == {"text": "SENSE ok 220 ms", "cls": "", "title": "The obstacle sensor works."}
