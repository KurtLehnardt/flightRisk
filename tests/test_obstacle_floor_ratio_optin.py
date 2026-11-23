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
    EVENT_OBSTACLE_METRIC,
    OBSTACLE_FLOOR_RATIO_MAX,
    OBSTACLE_FLOOR_RATIO_MAX_RANGE,
    OBSTACLE_METRIC,
    OBSTACLE_METRIC_FLOOR_RATIO,
    OBSTACLE_METRIC_SCORE,
    PERSON_CUE_AGE_KEY,
    PERSON_CUE_BLOCKED,
    PERSON_CUE_DOWN_KEY,
    REASON_ERROR,
    REASON_STALE,
    REASON_TIMEOUT,
    TELEMETRY_PERSON_CUE,
    LatestFrame,
    depth_metric_fields,
    obstacle_metric_settings,
)
from flightrisk.launcher.commands import EdgeToken
from flightrisk.launcher.preflight import (
    FAIL,
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


def make_map(centre=0.7, floor=1.0, top=0.1, rest=0.2, left=None, size=SIZE):
    """A raw inverse-depth map with the given centre region, floor band, top band and, optionally, left side."""
    m = np.full((size, size), rest, dtype=np.float32)
    band = int(round(size * 0.2))
    m[:band, :] = top
    if left is not None:
        m[size // 3:2 * size // 3, : size // 3] = left
    m[size // 3:2 * size // 3, size // 3:2 * size // 3] = centre
    col = int(size * 0.2)
    m[size - band:, col:size - col] = floor
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
        assert OBSTACLE_FLOOR_RATIO_MAX == 0.80
        assert OBSTACLE_FLOOR_RATIO_MAX_RANGE == (0.5, 1.5)
        assert ObstacleGuard.__init__.__defaults__[-2:] == (OBSTACLE_METRIC_SCORE, OBSTACLE_FLOOR_RATIO_MAX)

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
        for bad in (0.0, -1.0, float("nan"), float("inf"), True):
            with pytest.raises(ValueError):
                ObstacleGuard(metric=OBSTACLE_METRIC_FLOOR_RATIO, floor_ratio_max=bad)


# ---------------------------------------------------------------------------
# The camera sensor under floor_ratio
# ---------------------------------------------------------------------------


class TestSensorFloorRatio:
    def _floor_sensor(self, frames, **kw):
        return _sensor(frames, metric=OBSTACLE_METRIC_FLOOR_RATIO, **kw)

    def test_below_the_limit_is_clear(self, model, frames, checks, caplog):
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

    def test_no_floor_reference_is_blocked_never_clear_or_unavailable(self, model, frames, checks, caplog):
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
        # A gate that could not look leaves no value behind.
        model.depth = RATIO_CLEAR
        sensor.sense(_after())
        holder_without_frame = LatestFrame()
        sensor._frames = holder_without_frame
        assert sensor.sense(_after()).unavailable
        assert sensor.status()[DEPTH_METRIC_VALUE_KEY] is None and sensor.status()[DEPTH_STATE_KEY] is None

    def test_an_unknown_metric_is_refused(self, frames):
        with pytest.raises(ValueError):
            _sensor(frames, guard=object(), metric="ratio")


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


# ---------------------------------------------------------------------------
# Environment validation: the edge and the launcher
# ---------------------------------------------------------------------------

GOOD = [
    ({}, (OBSTACLE_METRIC_SCORE, OBSTACLE_FLOOR_RATIO_MAX)),
    ({ENV_OBSTACLE_METRIC: ""}, (OBSTACLE_METRIC_SCORE, OBSTACLE_FLOOR_RATIO_MAX)),
    ({ENV_OBSTACLE_METRIC: "score"}, (OBSTACLE_METRIC_SCORE, OBSTACLE_FLOOR_RATIO_MAX)),
    ({ENV_OBSTACLE_METRIC: " Floor_Ratio "}, (OBSTACLE_METRIC_FLOOR_RATIO, OBSTACLE_FLOOR_RATIO_MAX)),
    ({ENV_OBSTACLE_METRIC: "floor_ratio", ENV_OBSTACLE_FLOOR_RATIO_MAX: "0.5"}, (OBSTACLE_METRIC_FLOOR_RATIO, 0.5)),
    ({ENV_OBSTACLE_METRIC: "floor_ratio", ENV_OBSTACLE_FLOOR_RATIO_MAX: "1.5"}, (OBSTACLE_METRIC_FLOOR_RATIO, 1.5)),
    ({ENV_OBSTACLE_FLOOR_RATIO_MAX: "7.978e-1"}, (OBSTACLE_METRIC_SCORE, 0.7978)),
]
BAD = [
    {ENV_OBSTACLE_METRIC: "ratio"},
    {ENV_OBSTACLE_METRIC: "floor-ratio"},
    {ENV_OBSTACLE_METRIC: "centre_floor_ratio"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "0.49"},
    {ENV_OBSTACLE_FLOOR_RATIO_MAX: "1.51"},
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
        value, no_floor, stale, default = run_js(tmp_path, [
            ("senseBadge", [_sensing(obstacle_person_cue={**cue, "depth_metric": "floor_ratio",
                                                          "depth_metric_value": 0.71234, "depth_metric_max": 0.8,
                                                          "depth_state": "clear"})]),
            ("senseBadge", [_sensing(obstacle_state="blocked", obstacle_person_cue={
                **cue, "depth_metric": "floor_ratio", "depth_metric_value": None, "depth_metric_max": 0.8,
                "depth_state": "no_floor_reference"})]),
            ("senseBadge", [_sensing(obstacle_sensor_ok=False, obstacle_person_cue={
                **cue, "depth_metric": "floor_ratio", "depth_metric_value": None, "depth_metric_max": 0.8,
                "depth_state": None})]),
            ("senseBadge", [_sensing(obstacle_person_cue=cue)]),
        ])
        assert value["text"] == "SENSE ok 220 ms, floor ratio 0.712 / 0.8"
        assert "not validated indoors (H1)" in value["title"]
        assert no_floor["text"] == "SENSE ok 220 ms, floor ratio no floor / 0.8"
        assert stale["text"] == "SENSE STALE, floor ratio -- / 0.8"
        assert default == {"text": "SENSE ok 220 ms", "cls": "", "title": "The obstacle sensor works."}
