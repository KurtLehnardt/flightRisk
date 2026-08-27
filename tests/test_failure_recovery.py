"""Failure/recovery tests: verify components degrade gracefully instead of
hanging or taking the whole system down when one thing goes wrong.

Covers:
  - MavlinkController.move() must not hang if the drone disconnects
    (its background event loop stops) mid-command.
  - DroneFleet.broadcast_command must keep notifying other drones even if
    one drone's command raises.
  - amber.dashboard.app._frame_loop must keep running frame-to-frame even
    if a single frame's detection call raises.
  - amber.dashboard.app._gemma_worker must keep draining the queue even if
    one queued LLM call times out / raises.
"""

import threading
import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from amber.drone.controller import DroneController
from amber.drone.fleet import DroneFleet

# Reuses the fake mavsdk module bootstrap from tests/test_mavlink.py (it
# registers fake `mavsdk`, `mavsdk.offboard`, etc. into sys.modules as an
# import-time side effect) so MavlinkController can be exercised here too
# without the real mavsdk package installed.
from tests.test_mavlink import (
    _FakeSystem,
    _make_controller,
    _teardown_controller,
)


class TestMavlinkDisconnectDuringMove:
    def test_move_fails_fast_when_loop_stopped_underneath(self):
        """Simulates a disconnect racing with an in-flight move(): the
        background asyncio loop is stopped before move() is called. `_run`
        must raise immediately (it checks `loop.is_running()` up front)
        rather than block forever on `future.result()`."""
        system = _FakeSystem()
        ctrl = _make_controller(system)
        try:
            ctrl._loop.call_soon_threadsafe(ctrl._loop.stop)
            ctrl._loop_thread.join(timeout=2.0)
            assert not ctrl._loop.is_running()

            start = time.monotonic()
            with pytest.raises(RuntimeError, match="Event loop"):
                ctrl.move("forward", 50)
            elapsed = time.monotonic() - start

            assert elapsed < 2.0, "move() should fail fast, not hang"
        finally:
            _teardown_controller(ctrl)

    def test_disconnect_call_itself_does_not_hang(self):
        """disconnect() while nothing is flying should also return promptly."""
        system = _FakeSystem()
        ctrl = _make_controller(system)

        start = time.monotonic()
        ctrl.disconnect()
        elapsed = time.monotonic() - start

        assert elapsed < 5.0
        assert ctrl.state.is_connected is False


class TestFleetBroadcastResilience:
    """DroneFleet.broadcast_command swallows per-drone exceptions
    (amber/drone/fleet.py) so one misbehaving drone can't stop the fleet
    from receiving a command."""

    @staticmethod
    def _make_ctrl(name, host):
        ctrl = MagicMock(spec=DroneController)
        ctrl.name = name
        ctrl.host = host
        ctrl.connect.return_value = True
        return ctrl

    def test_broadcast_continues_after_one_drone_raises(self):
        good1 = self._make_ctrl("d1", "1.1.1.1")
        bad = self._make_ctrl("d2", "2.2.2.2")
        bad.hover.side_effect = RuntimeError("comm link down")
        good2 = self._make_ctrl("d3", "3.3.3.3")

        controllers = {"d1": good1, "d2": bad, "d3": good2}
        fleet = DroneFleet(factory=lambda name, host: controllers[name])
        fleet.register("d1", host="1.1.1.1")
        fleet.register("d2", host="2.2.2.2")
        fleet.register("d3", host="3.3.3.3")

        fleet.broadcast_command("hover")

        good1.hover.assert_called_once()
        bad.hover.assert_called_once()
        good2.hover.assert_called_once()

    def test_broadcast_with_kwargs_still_reaches_all_drones_on_failure(self):
        good = self._make_ctrl("d1", "1.1.1.1")
        bad = self._make_ctrl("d2", "2.2.2.2")
        bad.move.side_effect = TimeoutError("no ack from drone")

        controllers = {"d1": good, "d2": bad}
        fleet = DroneFleet(factory=lambda name, host: controllers[name])
        fleet.register("d1", host="1.1.1.1")
        fleet.register("d2", host="2.2.2.2")

        fleet.broadcast_command("move", direction="forward", distance_cm=30)

        good.move.assert_called_once_with(direction="forward", distance_cm=30)
        bad.move.assert_called_once_with(direction="forward", distance_cm=30)


class TestFrameLoopResilience:
    """amber.dashboard.app._frame_loop wraps each iteration's body in a
    broad try/except so a single frame's failure (e.g. detector.detect
    raising) doesn't kill the background thread."""

    def test_frame_loop_continues_after_detection_raises(self, clean_app_state, monkeypatch):
        from amber.dashboard.app import _frame_loop, _state, socketio

        frame = np.zeros((10, 10, 3), dtype=np.uint8)
        cap = MagicMock()
        cap.isOpened.return_value = True
        cap.read.return_value = (True, frame)

        detect_calls = []

        def fake_detect(_frame):
            detect_calls.append(1)
            if len(detect_calls) == 1:
                raise RuntimeError("boom: detector blew up on this frame")
            # Second call succeeds — stop the loop after processing it so
            # the test terminates deterministically.
            _state["running"] = False
            return []

        detector = MagicMock()
        detector.detect.side_effect = fake_detect
        detector.annotate.return_value = frame

        _state.update({
            "running": True,
            "cap": cap,
            "fleet": None,
            "detector": detector,
            "reid": None,
            "face": None,
            "scorer": None,
            "tracker": None,
            "reasoning": None,
            "recorder": None,
            "logger": None,
            "metrics": None,
            "tracer": None,
            "otel_metrics": None,
            "target_photo": None,
            "target_photo_path": None,
            "target_description": None,
            "persons_detected": 0,
            "fps": 0,
            "drone_telemetry": {},
        })

        mock_emit = MagicMock()
        monkeypatch.setattr("amber.dashboard.app.socketio.emit", mock_emit)

        # Blocking call: returns on its own once fake_detect flips
        # _state["running"] to False during the second iteration.
        _frame_loop(socketio)

        assert len(detect_calls) == 2, "loop must have survived the first exception and retried"
        assert _state["running"] is False


class TestGemmaWorkerResilience:
    """amber.dashboard.app._gemma_worker catches exceptions per queued item
    so one bad/timed-out LLM call doesn't stop later items from being
    processed."""

    def test_worker_continues_after_one_llm_call_raises(self, clean_app_state, monkeypatch):
        from amber.dashboard.app import _gemma_queue, _gemma_worker, _state, socketio

        # Drain any leftover items from other tests before starting.
        while not _gemma_queue.empty():
            _gemma_queue.get_nowait()
            _gemma_queue.task_done()

        reasoning = MagicMock()
        reasoning.analyze_match.side_effect = [
            TimeoutError("llm call timed out"),
            {"match": True, "confidence": "high", "reasoning": "confirmed match"},
        ]
        _state["reasoning"] = reasoning
        _state["match_history"] = []
        _state["db"] = None
        _state["running"] = True

        emitted = []
        monkeypatch.setattr(
            "amber.dashboard.app.socketio.emit",
            lambda event, *args, **kwargs: emitted.append((event, args[0] if args else None)),
        )

        crop = np.zeros((10, 10, 3), dtype=np.uint8)
        ref = np.zeros((10, 10, 3), dtype=np.uint8)
        _gemma_queue.put(("analyze", "trackA", crop, ref))
        _gemma_queue.put(("analyze", "trackB", crop, ref))

        worker_thread = threading.Thread(target=_gemma_worker, args=(socketio,), daemon=True)
        worker_thread.start()
        try:
            _gemma_queue.join()
        finally:
            _state["running"] = False
            worker_thread.join(timeout=3.0)

        assert not worker_thread.is_alive()
        assert reasoning.analyze_match.call_count == 2

        alert_events = [e for e in emitted if e[0] == "alert_upgrade"]
        assert len(alert_events) == 1
        assert alert_events[0][1]["track_id"] == "trackB"
