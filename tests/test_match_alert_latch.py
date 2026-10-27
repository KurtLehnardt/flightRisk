"""Tests for the single-alert match latch and the operator Confirm/Dismiss flow.

Covers the flight-test report that a possible match re-alerted every few
seconds with no way to confirm it: exactly one alert per match event, no
matching/LLM work while an alert awaits the operator, Dismiss suppression and
expiry, re-notification of an unanswered alert, the Confirm flight sequence
(return home and land vs. land in place, idempotent, fail-safe), API-key
gating of the new control endpoint, and a thread-safety smoke test.
"""

from __future__ import annotations

import queue
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from flightrisk.dashboard import app as app_module
from flightrisk.dashboard import recovery as recovery_module
from flightrisk.dashboard.alert_latch import AlertPhase, MatchAlertLatch, alert_latch
from flightrisk.dashboard.alerts import _gemma_worker
from flightrisk.dashboard.pipeline import _emit_due_reminder, _frame_loop
from flightrisk.dashboard.recovery import (
    RTH_MIN_BATTERY_PCT,
    ConfirmRecovery,
    confirm_recovery,
    handle_alert_response,
    run_confirm_recovery,
)
from flightrisk.dashboard.state import app_state, gemma_queue
from flightrisk.drone.controller import DroneState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeDrone:
    """Explicit drone double (a MagicMock would invent ``return_to_home``)."""

    def __init__(self, battery=80, flying=True, rth=True, rth_exc=None, land_exc=None, hover_exc=None):
        self.state = DroneState(battery=battery, is_flying=flying, is_connected=True)
        self.calls: list = []
        self._rth_exc = rth_exc
        self._land_exc = land_exc
        self._hover_exc = hover_exc
        if rth:
            self.return_to_home = self._return_to_home

    def _return_to_home(self):
        self.calls.append("rth")
        if self._rth_exc:
            raise self._rth_exc

    def hover(self):
        self.calls.append("hover")
        if self._hover_exc:
            raise self._hover_exc

    def land(self):
        self.calls.append("land")
        if self._land_exc:
            raise self._land_exc
        self.state.is_flying = False

    def move(self, direction, distance_cm):
        self.calls.append(("move", direction, distance_cm))

    def rotate(self, degrees):
        self.calls.append(("rotate", degrees))

    def get_frame(self):
        return None


class FakeSocketIO:
    def __init__(self):
        self.events: list = []
        self._lock = threading.Lock()

    def emit(self, event, payload=None, *args, **kwargs):
        with self._lock:
            self.events.append((event, payload))

    def named(self, name):
        with self._lock:
            return [p for (e, p) in self.events if e == name]


def _fleet_with(drone):
    fleet = MagicMock()
    fleet.primary = drone
    fleet.count = 1
    return fleet


def _open_alert(track_key="3_3", match_id=None, extra_keys=()):
    alert_id = alert_latch.try_open([f"grid:{track_key}", *extra_keys])
    assert alert_id is not None
    payload = {"alert_id": alert_id, "track_id": track_key, "score": 0.9, "alert_level": "possible_match"}
    if match_id is not None:
        payload["match_id"] = match_id
    alert_latch.attach(alert_id, payload)
    return alert_id


def _drain_gemma_queue():
    while True:
        try:
            gemma_queue.get_nowait()
            gemma_queue.task_done()
        except queue.Empty:
            return


# ---------------------------------------------------------------------------
# Latch state machine (unit)
# ---------------------------------------------------------------------------

class TestLatchStateMachine:
    def test_first_open_wins_and_blocks_further_alerts(self):
        latch = MatchAlertLatch(clock=FakeClock())
        first = latch.try_open(["grid:1_1"])
        assert first is not None
        assert latch.phase is AlertPhase.AWAITING_RESPONSE
        assert not latch.matching_allowed()
        # Same or different candidates: nothing else opens while awaiting.
        assert latch.try_open(["grid:1_1"]) is None
        assert latch.try_open(["grid:9_9"]) is None
        assert latch.alert_id == first

    def test_dismiss_resumes_and_suppresses_candidate_until_expiry(self):
        clock = FakeClock()
        latch = MatchAlertLatch(dismiss_suppress_seconds=60, clock=clock)
        aid = latch.try_open(["grid:1_1", "track:7"])
        latch.attach(aid, {"alert_id": aid, "track_id": "1_1"})

        assert latch.dismiss(aid) == {"alert_id": aid, "track_id": "1_1"}
        assert latch.phase is AlertPhase.SEARCHING
        assert latch.matching_allowed()

        # The dismissed person (by grid cell or tracker id) stays quiet...
        assert latch.try_open(["grid:1_1"]) is None
        assert latch.try_open(["grid:5_5", "track:7"]) is None
        clock.advance(59)
        assert latch.is_suppressed(["track:7"])
        assert latch.try_open(["grid:1_1"]) is None
        # ...until the suppression window expires.
        clock.advance(2)
        assert not latch.is_suppressed(["grid:1_1", "track:7"])
        assert latch.try_open(["grid:1_1"]) is not None

    def test_new_candidate_alerts_normally_after_dismiss(self):
        latch = MatchAlertLatch(clock=FakeClock())
        aid = latch.try_open(["grid:1_1"])
        latch.dismiss(aid)
        assert latch.try_open(["grid:8_8"]) is not None

    def test_confirm_resolves_and_keeps_matching_off(self):
        latch = MatchAlertLatch(clock=FakeClock())
        aid = latch.try_open(["grid:1_1"])
        latch.attach(aid, {"alert_id": aid})
        assert latch.confirm(aid) == {"alert_id": aid}
        assert latch.phase is AlertPhase.RESOLVED
        assert not latch.matching_allowed()
        assert latch.try_open(["grid:2_2"]) is None
        # Idempotent: repeated or late answers change nothing.
        assert latch.confirm(aid) is None
        assert latch.dismiss(aid) is None
        assert latch.phase is AlertPhase.RESOLVED

    def test_stale_or_unknown_ids_are_ignored(self):
        latch = MatchAlertLatch(clock=FakeClock())
        aid = latch.try_open(["grid:1_1"])
        assert latch.confirm("not-the-id") is None
        assert latch.dismiss("") is None
        assert latch.phase is AlertPhase.AWAITING_RESPONSE
        assert latch.alert_id == aid

    def test_abort_releases_slot_without_suppression(self):
        latch = MatchAlertLatch(clock=FakeClock())
        aid = latch.try_open(["grid:1_1"])
        latch.abort(aid)
        assert latch.phase is AlertPhase.SEARCHING
        assert latch.try_open(["grid:1_1"]) is not None

    def test_renotify_reuses_same_alert(self):
        clock = FakeClock()
        latch = MatchAlertLatch(renotify_interval=30, clock=clock)
        aid = latch.try_open(["grid:1_1"])
        latch.attach(aid, {"alert_id": aid, "score": 0.8})

        assert latch.due_renotify() is None
        clock.advance(29.9)
        assert latch.due_renotify() is None
        clock.advance(0.2)
        first = latch.due_renotify()
        assert first["alert_id"] == aid
        assert first["renotify"] is True
        assert first["renotify_count"] == 1
        # Not again until another full interval passes.
        assert latch.due_renotify() is None
        clock.advance(30)
        assert latch.due_renotify()["renotify_count"] == 2
        # Still the single logical alert.
        assert latch.alert_id == aid
        assert latch.phase is AlertPhase.AWAITING_RESPONSE

    def test_no_renotify_after_response(self):
        clock = FakeClock()
        latch = MatchAlertLatch(renotify_interval=30, clock=clock)
        aid = latch.try_open(["grid:1_1"])
        latch.attach(aid, {"alert_id": aid})
        latch.dismiss(aid)
        clock.advance(100)
        assert latch.due_renotify() is None

    def test_reset_clears_everything(self):
        latch = MatchAlertLatch(clock=FakeClock())
        aid = latch.try_open(["grid:1_1"])
        latch.confirm(aid)
        latch.reset()
        assert latch.phase is AlertPhase.SEARCHING
        assert latch.try_open(["grid:1_1"]) is not None


class TestLatchThreadSafety:
    def test_concurrent_open_yields_exactly_one_alert(self):
        latch = MatchAlertLatch()
        n = 32
        barrier = threading.Barrier(n)
        results = []
        lock = threading.Lock()

        def worker(i):
            barrier.wait()
            r = latch.try_open([f"grid:{i}_{i}"])
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(results) == n
        assert len([r for r in results if r is not None]) == 1

    def test_concurrent_responses_apply_exactly_once(self):
        latch = MatchAlertLatch()
        aid = latch.try_open(["grid:1_1"])
        latch.attach(aid, {"alert_id": aid})
        n = 32
        barrier = threading.Barrier(n)
        applied = []
        lock = threading.Lock()

        def worker(i):
            barrier.wait()
            r = latch.confirm(aid) if i % 2 else latch.dismiss(aid)
            if r is not None:
                with lock:
                    applied.append(i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(applied) == 1


# ---------------------------------------------------------------------------
# Frame loop integration
# ---------------------------------------------------------------------------

def _setup_matching_pipeline(n_frames, bbox_for_frame, events, search_active=False):
    """Wire app_state for a frame-loop run that matches on every frame."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    crop = np.zeros((50, 50, 3), dtype=np.uint8)
    counter = {"frame": 0}

    def detect(_frame):
        i = counter["frame"]
        return [{"bbox": bbox_for_frame(i), "confidence": 0.9, "crop": crop}]

    detector = MagicMock()
    detector.detect.side_effect = detect
    detector.annotate.return_value = frame

    reid = MagicMock()
    reid.find_match.return_value = (0, 0.85)
    reid.compare.return_value = 0.85
    reid.match_threshold = 0.55

    scorer = MagicMock()
    scorer.score.return_value = {"combined_score": 0.85}
    scorer.alert_level.return_value = "possible_match"

    cap = MagicMock()
    cap.isOpened.return_value = True
    cap.read.return_value = (True, frame)

    def emit(event, payload=None, *args, **kwargs):
        events.append((event, payload))
        if event == "frame":
            counter["frame"] += 1
            if counter["frame"] >= n_frames:
                app_state.running = False

    sio = MagicMock()
    sio.emit = emit

    app_state.running = True
    app_state.detector = detector
    app_state.tracker = None
    app_state.reid = reid
    app_state.face = None
    app_state.scorer = scorer
    app_state.target_photo = "base64data"
    app_state.target_photo_path = "/fake/target.jpg"
    app_state.cap = cap
    app_state.fleet = None
    app_state.logger = None
    app_state.metrics = None
    app_state.recorder = None
    app_state.tracer = None
    app_state.otel_metrics = None
    app_state.reasoning = None
    app_state.db = None
    app_state.session_id = None
    app_state.search_active = search_active
    app_state.target_description = None
    app_state.signal_registry = None
    app_state.match_history = []
    return sio, reid


class TestFrameLoopSingleAlert:
    def test_rapid_moving_detections_emit_exactly_one_alert(self, clean_app_state):
        """The reported bug: a moving match re-alerted every few seconds."""
        events: list = []
        # Bbox centre jumps a full 50px grid cell every frame, which used to
        # defeat the per-grid-cell cooldown and fire a new alert each time.
        sio, reid = _setup_matching_pipeline(
            15, lambda i: [10 + 60 * i % 500, 10, 50 + 60 * i % 500, 50], events,
        )
        with patch("flightrisk.dashboard.pipeline._save_match_snapshot"):
            _frame_loop(sio)

        alerts = [p for e, p in events if e == "match_alert"]
        assert len(alerts) == 1
        assert alerts[0]["alert_id"] == alert_latch.alert_id
        assert alert_latch.phase is AlertPhase.AWAITING_RESPONSE
        # Matching ran once, then paused for the rest of the frames.
        assert reid.find_match.call_count == 1
        # Video kept flowing the whole time.
        assert len([e for e, _ in events if e == "frame"]) == 15

    def test_no_matching_while_awaiting(self, clean_app_state):
        _open_alert("0_0")
        events: list = []
        sio, reid = _setup_matching_pipeline(5, lambda i: [10, 10, 50, 50], events)
        with patch("flightrisk.dashboard.pipeline._save_match_snapshot"):
            _frame_loop(sio)
        reid.find_match.assert_not_called()
        reid.compare.assert_not_called()
        assert [e for e, _ in events if e == "match_alert"] == []

    def test_dismissed_candidate_suppressed_new_candidate_alerts(self, clean_app_state):
        from flightrisk.dashboard.alerts import _compute_track_key

        bbox = [10, 10, 50, 50]
        aid = _open_alert(_compute_track_key(bbox))
        assert alert_latch.dismiss(aid) is not None

        events: list = []
        sio, reid = _setup_matching_pipeline(3, lambda i: bbox, events)
        with patch("flightrisk.dashboard.pipeline._save_match_snapshot"):
            _frame_loop(sio)
        # Matching resumed, but the dismissed person did not re-alert.
        assert reid.find_match.call_count == 3
        assert [e for e, _ in events if e == "match_alert"] == []

        events.clear()
        sio, reid = _setup_matching_pipeline(3, lambda i: [400, 300, 460, 380], events)
        with patch("flightrisk.dashboard.pipeline._save_match_snapshot"):
            _frame_loop(sio)
        assert len([e for e, _ in events if e == "match_alert"]) == 1

    def test_alert_pauses_active_search_and_hovers(self, clean_app_state):
        events: list = []
        sio, _ = _setup_matching_pipeline(2, lambda i: [10, 10, 50, 50], events, search_active=True)
        drone = FakeDrone()
        drone.get_frame = lambda: np.zeros((480, 640, 3), dtype=np.uint8)
        app_state.fleet = _fleet_with(drone)
        with patch("flightrisk.dashboard.pipeline._save_match_snapshot"):
            _frame_loop(sio)
        assert "hover" in drone.calls
        assert len([e for e, _ in events if e == "search_paused"]) == 1
        # Paused, not stopped: Dismiss must be able to resume the pattern.
        assert app_state.search_active is True

    def test_db_failure_releases_latch(self, clean_app_state):
        events: list = []
        sio, _ = _setup_matching_pipeline(1, lambda i: [10, 10, 50, 50], events)
        app_state.db = MagicMock()

        def failing_add_match(**kwargs):
            # The loop swallows the error and retries next frame; stop it here.
            app_state.running = False
            raise RuntimeError("disk full")

        app_state.db.add_match.side_effect = failing_add_match
        app_state.session_id = "s1"
        with patch("flightrisk.dashboard.pipeline._save_match_snapshot"):
            _frame_loop(sio)
        assert app_state.db.add_match.call_count == 1
        assert [e for e, _ in events if e == "match_alert"] == []
        assert alert_latch.phase is AlertPhase.SEARCHING


class TestReminder:
    def test_unanswered_alert_is_renotified_as_same_alert(self, monkeypatch):
        clock = FakeClock()
        monkeypatch.setattr(alert_latch, "_clock", clock)
        aid = _open_alert("2_2")
        sio = FakeSocketIO()

        _emit_due_reminder(sio)
        assert sio.named("match_alert_reminder") == []

        clock.advance(alert_latch.renotify_interval + 0.1)
        _emit_due_reminder(sio)
        reminders = sio.named("match_alert_reminder")
        assert len(reminders) == 1
        assert reminders[0]["alert_id"] == aid
        assert reminders[0]["renotify"] is True
        assert sio.named("match_alert") == []


# ---------------------------------------------------------------------------
# Gemma worker gating
# ---------------------------------------------------------------------------

def _run_worker_briefly(sio, seconds=0.3):
    def stop():
        time.sleep(seconds)
        app_state.running = False

    t = threading.Thread(target=stop, daemon=True)
    t.start()
    _gemma_worker(sio)
    t.join(timeout=2)


class TestGemmaWorkerGating:
    def setup_method(self):
        _drain_gemma_queue()

    def teardown_method(self):
        _drain_gemma_queue()

    def test_describe_skipped_while_awaiting(self, clean_app_state):
        _open_alert("1_1")
        reasoning = MagicMock()
        reasoning.match_description.return_value = {"match": True, "confidence": "high"}
        app_state.reasoning = reasoning
        app_state.running = True
        crop = np.zeros((20, 20, 3), dtype=np.uint8)
        gemma_queue.put_nowait(("describe", "9_9", crop, "red shirt"))

        sio = FakeSocketIO()
        _run_worker_briefly(sio)

        reasoning.match_description.assert_not_called()
        assert sio.named("match_alert") == []

    def test_open_alerts_own_analysis_still_runs_but_cannot_fly(self, clean_app_state):
        aid = _open_alert("1_1")
        reasoning = MagicMock()
        reasoning.analyze_match.return_value = {"match": True, "confidence": "high", "reasoning": "same kid"}
        app_state.reasoning = reasoning
        app_state.running = True
        app_state.match_history = []
        app_state.db = None
        drone = FakeDrone()
        app_state.fleet = _fleet_with(drone)
        crop = np.zeros((20, 20, 3), dtype=np.uint8)
        gemma_queue.put_nowait(("analyze", "1_1", crop, crop))
        gemma_queue.put_nowait(("analyze", "5_5", crop, crop))

        sio = FakeSocketIO()
        _run_worker_briefly(sio)

        # Only the open alert's own analysis ran.
        assert reasoning.analyze_match.call_count == 1
        assert len(sio.named("alert_upgrade")) == 1
        # A high-confidence LLM verdict never confirms the alert or flies.
        assert alert_latch.phase is AlertPhase.AWAITING_RESPONSE
        assert alert_latch.alert_id == aid
        assert drone.calls == []
        assert sio.named("recovery_status") == []

    def test_describe_match_opens_single_alert(self, clean_app_state):
        reasoning = MagicMock()
        reasoning.match_description.return_value = {"match": True, "confidence": "high", "reasoning": "r"}
        app_state.reasoning = reasoning
        app_state.running = True
        app_state.scorer = None
        app_state.db = None
        app_state.match_history = []
        crop = np.zeros((20, 20, 3), dtype=np.uint8)
        gemma_queue.put_nowait(("describe", "1_1", crop, "red shirt"))
        gemma_queue.put_nowait(("describe", "2_2", crop, "red shirt"))

        sio = FakeSocketIO()
        with patch("flightrisk.dashboard.alerts._save_match_snapshot"):
            _run_worker_briefly(sio)

        alerts = sio.named("match_alert")
        assert len(alerts) == 1
        assert alerts[0]["alert_id"] == alert_latch.alert_id
        # The second description call was skipped entirely while awaiting.
        assert reasoning.match_description.call_count == 1


# ---------------------------------------------------------------------------
# Edge (ground-station) path
# ---------------------------------------------------------------------------

class TestEdgeSingleAlert:
    def test_many_edge_matches_emit_one_alert(self, clean_app_state):
        app_state.scorer = None
        app_state.db = None
        app_state.match_history = []
        sio = FakeSocketIO()
        det = MagicMock(crop_jpeg=None)
        for i in range(20):
            x = 10 + 60 * i
            result = {"bbox": (x, 10, x + 40, 50), "reid_score": 0.9, "face_score": 0.0}
            app_module._maybe_emit_edge_match(sio, det, result, None, time.time())
        alerts = sio.named("match_alert")
        assert len(alerts) == 1
        assert alerts[0]["alert_id"] == alert_latch.alert_id


# ---------------------------------------------------------------------------
# Confirm flight sequence
# ---------------------------------------------------------------------------

class TestConfirmRecoverySequence:
    def test_high_battery_returns_home_then_lands(self):
        drone = FakeDrone(battery=80)
        emitted = []
        outcome = run_confirm_recovery(drone, lambda e, d: emitted.append((e, d)))
        assert outcome == "returned_and_landed"
        assert drone.calls == ["hover", "rth", "land"]
        stages = [d["stage"] for e, d in emitted if e == "recovery_status"]
        assert stages == ["returning_home", "landing", "landed"]

    def test_battery_exactly_at_threshold_returns_home(self):
        drone = FakeDrone(battery=RTH_MIN_BATTERY_PCT)
        assert run_confirm_recovery(drone) == "returned_and_landed"
        assert drone.calls == ["hover", "rth", "land"]

    def test_low_battery_lands_immediately(self):
        drone = FakeDrone(battery=RTH_MIN_BATTERY_PCT - 1)
        emitted = []
        outcome = run_confirm_recovery(drone, lambda e, d: emitted.append((e, d)))
        assert outcome == "landed_in_place"
        assert drone.calls == ["hover", "land"]
        assert "rth" not in drone.calls
        assert any(d.get("reason") == "low_battery" for _, d in emitted)

    def test_unknown_battery_lands_in_place(self):
        drone = FakeDrone(battery=0)
        assert run_confirm_recovery(drone) == "landed_in_place"
        assert drone.calls == ["hover", "land"]

    def test_no_rth_capability_lands_in_place(self):
        drone = FakeDrone(battery=100, rth=False)
        emitted = []
        assert run_confirm_recovery(drone, lambda e, d: emitted.append((e, d))) == "landed_in_place"
        assert drone.calls == ["hover", "land"]
        assert any(d.get("reason") == "no_return_to_home" for _, d in emitted)

    def test_rth_exception_fails_safe_to_land(self):
        drone = FakeDrone(battery=90, rth_exc=RuntimeError("lost link"))
        assert run_confirm_recovery(drone) == "failsafe_landed"
        assert drone.calls == ["hover", "rth", "land"]

    def test_hover_exception_fails_safe_to_land(self):
        drone = FakeDrone(battery=90, hover_exc=RuntimeError("udp"))
        assert run_confirm_recovery(drone) == "failsafe_landed"
        assert drone.calls == ["hover", "land"]

    def test_land_failure_retries_then_reports(self):
        drone = FakeDrone(battery=20, land_exc=RuntimeError("no ack"))
        emitted = []
        outcome = run_confirm_recovery(drone, lambda e, d: emitted.append((e, d)))
        assert outcome == "failsafe_land_failed"
        assert drone.calls == ["hover", "land", "land"]
        assert emitted[-1][1]["stage"] == "error"

    def test_emit_failure_never_blocks_landing(self):
        drone = FakeDrone(battery=10)

        def broken_emit(event, data):
            raise RuntimeError("socket gone")

        assert run_confirm_recovery(drone, broken_emit) == "landed_in_place"
        assert drone.calls[-1] == "land"

    def test_no_drone_and_grounded_drone_are_noops(self):
        assert run_confirm_recovery(None) == "no_drone"
        grounded = FakeDrone(flying=False)
        assert run_confirm_recovery(grounded) == "not_flying"
        assert grounded.calls == []

    def test_runner_starts_only_once(self):
        runner = ConfirmRecovery()
        drone = FakeDrone(battery=80)
        assert runner.start(drone) is True
        assert runner.start(drone) is False
        runner.thread.join(timeout=2)
        assert drone.calls.count("land") == 1


# ---------------------------------------------------------------------------
# Operator response orchestration
# ---------------------------------------------------------------------------

class TestHandleAlertResponse:
    def test_confirm_stops_search_records_feedback_and_flies_once(self, clean_app_state):
        drone = FakeDrone(battery=80)
        app_state.fleet = _fleet_with(drone)
        app_state.search_active = True
        app_state.db = MagicMock()
        app_state.session_id = "sess-1"
        aid = _open_alert("1_1", match_id=42)
        sio = FakeSocketIO()

        body, status = handle_alert_response(aid, "confirm", sio)
        assert status == 200 and body["ok"] and body["recovery_started"] is True
        # Double click / second tab: ignored, no second flight sequence.
        body2, status2 = handle_alert_response(aid, "confirm", sio)
        assert status2 == 409 and body2["ok"] is False

        confirm_recovery.thread.join(timeout=2)
        assert drone.calls == ["hover", "rth", "land"]
        assert app_state.search_active is False
        assert alert_latch.phase is AlertPhase.RESOLVED
        app_state.db.add_feedback.assert_called_once_with(42, "sess-1", "confirmed", None)
        resolved = sio.named("match_alert_resolved")
        assert len(resolved) == 1 and resolved[0]["action"] == "confirm"

    def test_confirm_low_battery_lands_in_place(self, clean_app_state):
        drone = FakeDrone(battery=15)
        app_state.fleet = _fleet_with(drone)
        app_state.db = None
        aid = _open_alert("1_1")
        handle_alert_response(aid, "confirm", FakeSocketIO())
        confirm_recovery.thread.join(timeout=2)
        assert drone.calls == ["hover", "land"]

    def test_dismiss_resumes_and_records_rejection(self, clean_app_state):
        drone = FakeDrone()
        app_state.fleet = _fleet_with(drone)
        app_state.db = MagicMock()
        app_state.session_id = "sess-1"
        aid = _open_alert("1_1", match_id=7)
        sio = FakeSocketIO()

        body, status = handle_alert_response(aid, "dismiss", sio)
        assert status == 200 and body["ok"]
        assert alert_latch.phase is AlertPhase.SEARCHING
        assert alert_latch.is_suppressed(["grid:1_1"])
        app_state.db.add_feedback.assert_called_once_with(7, "sess-1", "rejected", None)
        # Dismiss never flies the drone.
        assert drone.calls == []
        assert confirm_recovery.thread is None
        # Repeat dismiss is a no-op.
        assert handle_alert_response(aid, "dismiss", sio)[1] == 409

    @pytest.mark.parametrize("alert_id,action", [
        ("x", "launch"), ("x", None), (None, "confirm"), ("", "dismiss"), (123, "confirm"),
    ])
    def test_rejects_bad_input(self, alert_id, action, clean_app_state):
        _open_alert("1_1")
        body, status = handle_alert_response(alert_id, action, FakeSocketIO())
        assert status == 400 and body["ok"] is False
        assert alert_latch.phase is AlertPhase.AWAITING_RESPONSE

    def test_feedback_db_error_does_not_block_confirm(self, clean_app_state):
        app_state.fleet = None
        app_state.db = MagicMock()
        app_state.db.add_feedback.side_effect = RuntimeError("locked")
        aid = _open_alert("1_1", match_id=1)
        _body, status = handle_alert_response(aid, "confirm", FakeSocketIO())
        assert status == 200
        assert alert_latch.phase is AlertPhase.RESOLVED


# ---------------------------------------------------------------------------
# HTTP / SocketIO endpoints and auth
# ---------------------------------------------------------------------------

class TestEndpointsAndAuth:
    def test_rest_respond_rejected_without_api_key(self, clean_app_state, monkeypatch):
        monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", "test-key-123")
        aid = _open_alert("1_1")
        with app_module.app.test_client() as client:
            resp = client.post(f"/api/alerts/{aid}/respond", json={"action": "confirm"})
            assert resp.status_code == 401
            resp = client.post(
                f"/api/alerts/{aid}/respond", json={"action": "confirm"},
                headers={"Authorization": "Bearer wrong"},
            )
            assert resp.status_code == 401
        assert alert_latch.phase is AlertPhase.AWAITING_RESPONSE
        assert confirm_recovery.thread is None

    def test_rest_respond_works_with_api_key(self, clean_app_state, monkeypatch):
        monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", "test-key-123")
        app_state.fleet = None
        app_state.db = None
        aid = _open_alert("1_1")
        with app_module.app.test_client() as client:
            resp = client.post(
                f"/api/alerts/{aid}/respond", json={"action": "dismiss"},
                headers={"Authorization": "Bearer test-key-123"},
            )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        assert alert_latch.phase is AlertPhase.SEARCHING

    def test_unauthenticated_socket_cannot_confirm(self, clean_app_state, monkeypatch):
        monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", "test-key-123")
        aid = _open_alert("1_1")
        sio_client = app_module.socketio.test_client(app_module.app)
        assert not sio_client.is_connected()
        assert alert_latch.phase is AlertPhase.AWAITING_RESPONSE
        assert alert_latch.alert_id == aid

    def test_socket_confirm_acks_and_resolves(self, clean_app_state, monkeypatch):
        monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", None)
        app_state.fleet = None
        app_state.db = None
        aid = _open_alert("1_1")
        sio_client = app_module.socketio.test_client(app_module.app)
        received = sio_client.get_received()
        # A (re)connecting browser is shown the pending alert immediately.
        assert any(r["name"] == "match_alert_reminder" and r["args"][0]["alert_id"] == aid for r in received)
        sio_client.emit("match_alert_response", {"alert_id": aid, "action": "confirm"})
        received = sio_client.get_received()
        sio_client.disconnect()
        acks = [r["args"][0] for r in received if r["name"] == "match_alert_response_ack"]
        assert len(acks) == 1 and acks[0]["ok"] is True
        assert alert_latch.phase is AlertPhase.RESOLVED

    def test_start_search_refused_after_confirmed_match(self, clean_app_state):
        drone = FakeDrone()
        app_state.fleet = _fleet_with(drone)
        aid = _open_alert("1_1")
        alert_latch.confirm(aid)
        sio_client = app_module.socketio.test_client(app_module.app)
        sio_client.get_received()
        sio_client.emit("start_search", {"pattern": "expanding_square"})
        received = sio_client.get_received()
        sio_client.disconnect()
        assert any(r["name"] == "error" for r in received)
        assert app_state.search_active is False


class TestSearchPauseResume:
    def test_search_pattern_holds_while_awaiting_and_resumes_on_dismiss(self, clean_app_state):
        drone = FakeDrone()
        app_state.fleet = _fleet_with(drone)
        app_state.obstacle_guard = None
        aid = _open_alert("1_1")

        sio_client = app_module.socketio.test_client(app_module.app)
        sio_client.get_received()
        sio_client.emit("start_search", {"pattern": "expanding_square"})
        try:
            time.sleep(0.5)
            assert [c for c in drone.calls if isinstance(c, tuple)] == []

            handle_alert_response(aid, "dismiss", FakeSocketIO())
            deadline = time.time() + 3
            while time.time() < deadline and not any(isinstance(c, tuple) for c in drone.calls):
                time.sleep(0.05)
            assert any(c[0] == "move" for c in drone.calls if isinstance(c, tuple))
        finally:
            app_state.search_active = False
            sio_client.disconnect()
            time.sleep(0.6)  # let the executor thread observe the stop


def test_recovery_module_threshold_is_conservative():
    # Guard against an accidental edit that would start long return legs on a
    # nearly empty pack (the frame loop force-lands at 10 %).
    assert 30 <= recovery_module.RTH_MIN_BATTERY_PCT <= 100
